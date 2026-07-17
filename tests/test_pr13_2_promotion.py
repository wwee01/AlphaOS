"""PR13 slice 2: card promotion (graduation) + manual demotion. Covers:
* check_promotion_preconditions() -- every reason code in isolation, and
  the full happy path.
* promote_card()/demote_card() -- the actual write actions, decided_by
  guard, terminal-demotion refusal, race handling.
* is_terminally_demoted()/live_eligible_cards() -- both demotion
  mechanisms (automatic card_demotions, manual promotion_decisions) are
  checked, neither alone.
* mark_hypothesis_status() -- the only writer of MET/FAILED/WITHDRAWN,
  gated on 'resolved', operator-only.
* autonomy_readiness report + CLI dry-run/--confirm semantics.

All offline, in-memory, mock mode. No real money, no network.
"""

from __future__ import annotations

import sqlite3

import pytest

from alphaos.cards import promotion
from alphaos.cards.scoreboard import live_eligible_cards
from alphaos.hypotheses import mark_hypothesis_status


def _insert_card(journal, card_id, version, state="shadow"):
    journal.insert("setup_cards", {
        "card_id": card_id, "version": version, "state": state,
        "content_hash": f"hash-{card_id}-v{version}",
    })


def _insert_hypothesis(
    journal, hypothesis_id, card_id=None, risk_class="B", status="resolved",
    last_q_value=0.05, prereg_id="prereg1",
):
    journal.insert("hypothesis_proposals", {
        "hypothesis_id": hypothesis_id,
        "risk_class": risk_class,
        "claim": f"test claim for {hypothesis_id}",
        "card_id": card_id,
        "prereg_id": prereg_id,
        "status": status,
        "analysis_not_before": "2026-01-01",
        "last_q_value": last_q_value,
        "last_verdict": "forward-test-candidate",
        "last_reason": "test reason",
    })


def _seed_clustered_candidates(journal, card_id, card_version, n, values, start="2026-01-01"):
    from datetime import date as _date, timedelta
    base = _date.fromisoformat(start)
    for i in range(n):
        cid = f"{card_id}-cand-{i}"
        journal.insert("candidates", {
            "candidate_id": cid, "symbol": f"SYM{i}", "card_id": card_id, "card_version": card_version,
        })
        journal.insert("candidate_outcomes", {
            "outcome_id": f"out-{cid}", "candidate_id": cid, "symbol": f"SYM{i}",
            "candidate_type": "proposal", "decision_at_utc": f"{(base + timedelta(days=i)).isoformat()}T12:00:00+00:00",
            "outcome_status": "resolved", "replay_r": values[i % len(values)],
        })


def _make_eligible(journal, hypothesis_id="H-TEST", card_id="test_card", card_version=1):
    """A fully-eligible fixture: shadow card with a cleared scoreboard,
    hypothesis MET with q_value well below the floor."""
    _insert_card(journal, card_id, card_version, state="shadow")
    _insert_hypothesis(journal, hypothesis_id, card_id=card_id, status="met", last_q_value=0.02)
    _seed_clustered_candidates(journal, card_id, card_version, n=35, values=[0.6, -0.1])
    return hypothesis_id, card_id, card_version


# ------------------------------------------------------- precondition checks
def test_precondition_hypothesis_not_found(journal):
    check = promotion.check_promotion_preconditions(journal, "H-NOPE")
    assert check == {"eligible": False, "reason_code": "HYPOTHESIS_NOT_FOUND",
                      "detail": "no such hypothesis_id: 'H-NOPE'", "card_id": None, "card_version": None}


def test_precondition_no_card_id(journal):
    _insert_hypothesis(journal, "H-TQS-1", card_id=None, status="met")
    check = promotion.check_promotion_preconditions(journal, "H-TQS-1")
    assert check["eligible"] is False
    assert check["reason_code"] == "NO_CARD_ID"


def test_precondition_hypothesis_not_met(journal):
    _insert_hypothesis(journal, "H-CAT-1", card_id="catalyst_continuation_pullback_v1", status="resolved")
    check = promotion.check_promotion_preconditions(journal, "H-CAT-1")
    assert check["eligible"] is False
    assert check["reason_code"] == "HYPOTHESIS_NOT_MET"


def test_precondition_card_not_registered(journal):
    _insert_hypothesis(journal, "H-CAT-1", card_id="catalyst_continuation_pullback_v1", status="met")
    check = promotion.check_promotion_preconditions(journal, "H-CAT-1")
    assert check["eligible"] is False
    assert check["reason_code"] == "CARD_NOT_REGISTERED"


def test_precondition_card_not_shadow(journal):
    _insert_card(journal, "catalyst_continuation_pullback_v1", 3, state="live_eligible")
    _insert_hypothesis(journal, "H-CAT-1", card_id="catalyst_continuation_pullback_v1", status="met")
    check = promotion.check_promotion_preconditions(journal, "H-CAT-1")
    assert check["eligible"] is False
    assert check["reason_code"] == "CARD_NOT_SHADOW"


def test_precondition_terminally_demoted_via_automatic_card_demotions(journal):
    _insert_card(journal, "test_card", 1, state="shadow")
    _insert_hypothesis(journal, "H-TEST", card_id="test_card", status="met")
    journal.insert("card_demotions", {
        "demotion_id": "demo1", "card_id": "test_card", "card_version": 1,
        "reason": "test", "triggering_snapshot_id_1": "s1", "triggering_snapshot_id_2": "s2",
        "alert_sent": True, "demoted_at_utc": "2026-01-01T00:00:00+00:00", "demoted_at_sgt": "2026-01-01T08:00:00+08:00",
    })
    check = promotion.check_promotion_preconditions(journal, "H-TEST")
    assert check["eligible"] is False
    assert check["reason_code"] == "CARD_VERSION_TERMINALLY_DEMOTED"


def test_precondition_terminally_demoted_via_manual_promotion_decisions(journal):
    _insert_card(journal, "test_card", 1, state="shadow")
    _insert_hypothesis(journal, "H-TEST", card_id="test_card", status="met")
    promotion.demote_card(journal, "test_card", 1, decided_by="ck", reason="manual override")
    check = promotion.check_promotion_preconditions(journal, "H-TEST")
    assert check["eligible"] is False
    assert check["reason_code"] == "CARD_VERSION_TERMINALLY_DEMOTED"


def test_precondition_already_promoted(journal):
    hid, card_id, version = _make_eligible(journal)
    promotion.promote_card(journal, hid, decided_by="ck")
    check = promotion.check_promotion_preconditions(journal, hid)
    assert check["eligible"] is False
    assert check["reason_code"] == "ALREADY_PROMOTED"


def test_precondition_floors_not_met(journal):
    _insert_card(journal, "test_card", 1, state="shadow")
    _insert_hypothesis(journal, "H-TEST", card_id="test_card", status="met")
    _seed_clustered_candidates(journal, "test_card", 1, n=5, values=[0.3])  # far below the 30-sample floor
    check = promotion.check_promotion_preconditions(journal, "H-TEST")
    assert check["eligible"] is False
    assert check["reason_code"] == "FLOORS_NOT_MET"


def test_precondition_q_value_floor(journal):
    _insert_card(journal, "test_card", 1, state="shadow")
    _insert_hypothesis(journal, "H-TEST", card_id="test_card", status="met", last_q_value=0.5)
    _seed_clustered_candidates(journal, "test_card", 1, n=35, values=[0.6, -0.1])
    check = promotion.check_promotion_preconditions(journal, "H-TEST")
    assert check["eligible"] is False
    assert check["reason_code"] == "Q_VALUE_FLOOR"


def test_precondition_q_value_missing_treated_as_not_cleared(journal):
    _insert_card(journal, "test_card", 1, state="shadow")
    _insert_hypothesis(journal, "H-TEST", card_id="test_card", status="met", last_q_value=None)
    _seed_clustered_candidates(journal, "test_card", 1, n=35, values=[0.6, -0.1])
    check = promotion.check_promotion_preconditions(journal, "H-TEST")
    assert check["eligible"] is False
    assert check["reason_code"] == "Q_VALUE_FLOOR"


def test_precondition_research_ref_missing_for_class_c_fixture(journal):
    """No real Class C hypothesis names a card today -- this is a synthetic
    fixture proving PD#9's research_ref gate actually fires when one does."""
    _insert_card(journal, "test_card", 1, state="shadow")
    _insert_hypothesis(journal, "H-TEST", card_id="test_card", risk_class="C", status="met")
    _seed_clustered_candidates(journal, "test_card", 1, n=35, values=[0.6, -0.1])
    check = promotion.check_promotion_preconditions(journal, "H-TEST")
    assert check["eligible"] is False
    assert check["reason_code"] == "RESEARCH_REF_MISSING"


def test_precondition_research_ref_present_clears_class_c(journal):
    _insert_card(journal, "test_card", 1, state="shadow")
    _insert_hypothesis(journal, "H-TEST", card_id="test_card", risk_class="C", status="met")
    _seed_clustered_candidates(journal, "test_card", 1, n=35, values=[0.6, -0.1])
    check = promotion.check_promotion_preconditions(journal, "H-TEST", research_ref="docs/research/foo.md")
    assert check["eligible"] is True


def test_precondition_eligible_happy_path(journal):
    hid, card_id, version = _make_eligible(journal)
    check = promotion.check_promotion_preconditions(journal, hid)
    assert check == {"eligible": True, "reason_code": None, "detail": "all preconditions met",
                      "card_id": card_id, "card_version": version}


# ------------------------------------------------------------- promote_card
def test_promote_card_writes_decision_and_never_touches_setup_cards(journal):
    hid, card_id, version = _make_eligible(journal)
    before = journal.one("SELECT * FROM setup_cards WHERE card_id = ? AND version = ?", (card_id, version))

    row = promotion.promote_card(journal, hid, decided_by="ck", research_ref=None)

    after = journal.one("SELECT * FROM setup_cards WHERE card_id = ? AND version = ?", (card_id, version))
    assert before == after  # setup_cards.state is NEVER mutated (Prime Directive 7)
    assert row["card_id"] == card_id
    assert row["card_version"] == version
    assert row["direction"] == "promote"
    assert row["from_state"] == "shadow"
    assert row["to_state"] == "live_eligible"
    assert row["decided_by"] == "ck"
    assert row["preregistration_id"] == "prereg1"
    assert row["hypothesis_id"] == hid


def test_promote_card_refuses_when_decided_by_is_system(journal):
    hid, _, _ = _make_eligible(journal)
    with pytest.raises(ValueError, match="system"):
        promotion.promote_card(journal, hid, decided_by="system")
    assert journal.count_rows("promotion_decisions") == 0


def test_promote_card_refuses_when_not_eligible(journal):
    _insert_hypothesis(journal, "H-TEST", card_id=None, status="met")
    with pytest.raises(ValueError, match="NO_CARD_ID"):
        promotion.promote_card(journal, "H-TEST", decided_by="ck")


def test_promoting_makes_the_card_live_eligible_via_the_decision_row_not_a_state_write(journal):
    """2026-07-17 audit fix: promotion writes ONLY a decision row -- it still
    does NOT itself flip setup_cards.state (that remains true; see
    promotion.py's own module docstring, "never touches setup_cards or any
    YAML file"). What was WRONG before this fix is that live_eligible_cards()
    had no read-time inclusion clause for that decision row, so a genuinely
    promoted card could never appear there at all -- graduation was built but
    silently inert. (The design this test used to assert -- "an operator
    hand-edits the card's YAML state field" -- turns out to be structurally
    impossible: sync_registry() hashes `state` as part of a card's content,
    so editing it without a version bump is REFUSED at startup, and a version
    bump would make it a mutation, not a graduation, contradicting this same
    module's own graduation-vs-mutation law.)

    live_eligible_cards() now derives eligibility the same way it already
    derives demotion: by EXISTENCE of a promotion_decisions row, read at query
    time, never by mutating setup_cards.state -- setup_cards.state stays
    'shadow' on this card even after promotion, and that's correct."""
    hid, card_id, version = _make_eligible(journal)
    promotion.promote_card(journal, hid, decided_by="ck")

    stored = journal.one("SELECT state FROM setup_cards WHERE card_id = ? AND version = ?", (card_id, version))
    assert stored["state"] == "shadow"  # unchanged -- promotion.py's own law

    cards = live_eligible_cards(journal)
    assert card_id in [c["card_id"] for c in cards]  # but it IS live-eligible now


def test_promoted_then_demoted_card_does_not_reappear_via_the_new_inclusion_path(journal):
    """Anti-double-jeopardy must survive the new promotion-inclusion clause:
    demote_card() doesn't require state='live_eligible' (it only checks
    registration + not-already-terminal, see check_demotion_preconditions),
    so a card promoted-but-still-state='shadow' CAN be demoted -- and once
    demoted, it must be excluded again despite having a 'promote' row on
    record, not leak back in because the OR-clause alone would otherwise
    re-admit it."""
    hid, card_id, version = _make_eligible(journal)
    promotion.promote_card(journal, hid, decided_by="ck")
    assert card_id in [c["card_id"] for c in live_eligible_cards(journal)]

    promotion.demote_card(journal, card_id, version, decided_by="ck", reason="reconsidered")

    cards = live_eligible_cards(journal)
    assert card_id not in [c["card_id"] for c in cards]


def test_promotion_decisions_index_is_unique_and_catches_a_duplicate_direction_row(journal):
    """Correctness-audit HIGH-2 regression: (card_id, card_version,
    direction) must be a UNIQUE index, not a plain one, or a concurrent
    race that gets past the application-level ALREADY_PROMOTED check would
    silently insert a SECOND row instead of being caught at the DB level
    (which is exactly what promote_card()'s own sqlite3.IntegrityError
    catch claims to handle -- before this fix, that catch was dead code)."""
    _insert_card(journal, "test_card", 1, state="shadow")
    journal.insert("promotion_decisions", {
        "decision_id": "dec1", "card_id": "test_card", "card_version": 1,
        "from_state": "shadow", "to_state": "live_eligible", "direction": "promote",
        "decided_by": "ck", "decided_at_utc": "2026-01-01T00:00:00+00:00",
        "decided_at_sgt": "2026-01-01T08:00:00+08:00",
    })
    with pytest.raises(sqlite3.IntegrityError):
        journal.insert("promotion_decisions", {
            "decision_id": "dec2", "card_id": "test_card", "card_version": 1,
            "from_state": "shadow", "to_state": "live_eligible", "direction": "promote",
            "decided_by": "someone_else", "decided_at_utc": "2026-01-01T00:00:01+00:00",
            "decided_at_sgt": "2026-01-01T08:00:01+08:00",
        })


def test_mark_hypothesis_status_detects_a_concurrent_race_via_rowcount(journal, monkeypatch):
    """Correctness-audit HIGH-1 regression: a losing racer's own UPDATE
    (WHERE status='resolved' no longer matches, because a concurrent
    winner already flipped it) must be DETECTED via cursor.rowcount, never
    silently re-read the winner's row and report false success. Simulates
    the race deterministically by hooking journal.one() (the SELECT
    mark_hypothesis_status uses for its own initial check): the FIRST call
    returns the real 'resolved' row as normal, but as a side effect also
    performs a 'concurrent' winner's write immediately afterward -- so by
    the time mark_hypothesis_status's own UPDATE runs, the row has already
    moved away from 'resolved' (sqlite3.Connection.execute itself can't be
    monkeypatched on an instance -- it's a read-only C-level attribute --
    so journal.one() is the highest-fidelity injection point available)."""
    _insert_hypothesis(journal, "H-TEST", status="resolved")
    real_one = journal.one
    calls = {"n": 0}

    def _one_with_concurrent_winner(sql, params=()):
        result = real_one(sql, params)
        calls["n"] += 1
        if calls["n"] == 1 and "hypothesis_id = ?" in sql:
            journal.conn.execute(
                "UPDATE hypothesis_proposals SET status = 'failed' WHERE hypothesis_id = ?",
                ("H-TEST",),
            )
            journal.conn.commit()
        return result

    monkeypatch.setattr(journal, "one", _one_with_concurrent_winner)

    with pytest.raises(ValueError, match="concurrent operator"):
        mark_hypothesis_status(journal, "H-TEST", "met", decided_by="ck")

    row = journal.one("SELECT status FROM hypothesis_proposals WHERE hypothesis_id = ?", ("H-TEST",))
    assert row["status"] == "failed"  # the concurrent winner's write survives untouched


# -------------------------------------------------------------- demote_card
def test_demote_card_writes_decision(journal):
    _insert_card(journal, "test_card", 1, state="live_eligible")
    row = promotion.demote_card(journal, "test_card", 1, decided_by="ck", reason="lost confidence")
    assert row["card_id"] == "test_card"
    assert row["card_version"] == 1
    assert row["direction"] == "demote"
    assert row["hypothesis_id"] is None
    assert row["preregistration_id"] is None
    assert row["decided_by"] == "ck"


def test_demote_card_refuses_when_already_terminally_demoted(journal):
    _insert_card(journal, "test_card", 1, state="live_eligible")
    promotion.demote_card(journal, "test_card", 1, decided_by="ck", reason="first demotion")
    with pytest.raises(ValueError, match="CARD_VERSION_TERMINALLY_DEMOTED"):
        promotion.demote_card(journal, "test_card", 1, decided_by="ck", reason="second attempt")
    assert journal.count_rows("promotion_decisions", "card_id = ?", ("test_card",)) == 1


def test_demote_card_refuses_when_decided_by_is_system(journal):
    _insert_card(journal, "test_card", 1, state="live_eligible")
    with pytest.raises(ValueError, match="system"):
        promotion.demote_card(journal, "test_card", 1, decided_by="system", reason="x")


def test_demote_card_refuses_unregistered_card(journal):
    with pytest.raises(ValueError, match="CARD_NOT_REGISTERED"):
        promotion.demote_card(journal, "nope", 1, decided_by="ck", reason="x")


# --------------------------------------------------------- terminal-demotion
def test_is_terminally_demoted_checks_both_tables(journal):
    _insert_card(journal, "card_a", 1, state="live_eligible")
    _insert_card(journal, "card_b", 1, state="live_eligible")
    journal.insert("card_demotions", {
        "demotion_id": "demo1", "card_id": "card_a", "card_version": 1,
        "reason": "auto", "triggering_snapshot_id_1": "s1", "triggering_snapshot_id_2": "s2",
        "alert_sent": True, "demoted_at_utc": "2026-01-01T00:00:00+00:00", "demoted_at_sgt": "2026-01-01T08:00:00+08:00",
    })
    promotion.demote_card(journal, "card_b", 1, decided_by="ck", reason="manual")

    assert promotion.is_terminally_demoted(journal, "card_a", 1) is True
    assert promotion.is_terminally_demoted(journal, "card_b", 1) is True
    assert promotion.is_terminally_demoted(journal, "card_a", 2) is False  # a different version, untouched
    assert promotion.is_terminally_demoted(journal, "card_c", 1) is False


def test_live_eligible_cards_excludes_manually_demoted_cards(journal):
    _insert_card(journal, "card_a", 1, state="live_eligible")
    _insert_card(journal, "card_b", 1, state="live_eligible")
    promotion.demote_card(journal, "card_a", 1, decided_by="ck", reason="manual")

    cards = live_eligible_cards(journal)

    assert [c["card_id"] for c in cards] == ["card_b"]


# -------------------------------------------------------- mark_hypothesis_status
def test_mark_hypothesis_status_writes_met(journal):
    _insert_hypothesis(journal, "H-TEST", status="resolved")
    row = mark_hypothesis_status(journal, "H-TEST", "met", decided_by="ck")
    assert row["status"] == "met"


def test_mark_hypothesis_status_requires_resolved(journal):
    _insert_hypothesis(journal, "H-TEST", status="testing")
    with pytest.raises(ValueError, match="not 'resolved'"):
        mark_hypothesis_status(journal, "H-TEST", "met", decided_by="ck")


def test_mark_hypothesis_status_refuses_system_decided_by(journal):
    _insert_hypothesis(journal, "H-TEST", status="resolved")
    with pytest.raises(ValueError, match="system"):
        mark_hypothesis_status(journal, "H-TEST", "met", decided_by="system")


def test_mark_hypothesis_status_refuses_unknown_status(journal):
    _insert_hypothesis(journal, "H-TEST", status="resolved")
    with pytest.raises(ValueError, match="not an operator-settable status"):
        mark_hypothesis_status(journal, "H-TEST", "testing", decided_by="ck")


def test_mark_hypothesis_status_unknown_hypothesis(journal):
    with pytest.raises(ValueError, match="no such hypothesis_id"):
        mark_hypothesis_status(journal, "H-NOPE", "met", decided_by="ck")


# ----------------------------------------------------------- autonomy_readiness
def test_autonomy_readiness_report_lists_only_carded_hypotheses(journal):
    from alphaos.reports.autonomy_readiness import build_autonomy_readiness_report, render_markdown

    _insert_hypothesis(journal, "H-NOCARD", card_id=None, status="met")
    hid, card_id, version = _make_eligible(journal, hypothesis_id="H-CARDED")

    rep = build_autonomy_readiness_report(journal)

    assert rep["n_checked"] == 1  # H-NOCARD never appears
    assert rep["checks"][0]["hypothesis_id"] == "H-CARDED"
    assert rep["n_eligible"] == 1
    markdown = render_markdown(rep)
    assert "H-CARDED" in markdown
    assert "H-NOCARD" not in markdown


def test_autonomy_readiness_report_empty_state(journal):
    from alphaos.reports.autonomy_readiness import build_autonomy_readiness_report, render_markdown

    rep = build_autonomy_readiness_report(journal)
    assert rep["n_checked"] == 0
    assert "no seeded hypothesis" in render_markdown(rep)


# ------------------------------------------------------------------- CLI
def test_cmd_card_promote_dry_run_does_not_write(orchestrator):
    from alphaos.__main__ import cmd_card_promote

    hid, card_id, version = _make_eligible(orchestrator.journal)
    exit_code = cmd_card_promote(orchestrator, hid, "ck", None, confirm=False)

    assert exit_code == 0
    assert orchestrator.journal.count_rows("promotion_decisions") == 0


def test_cmd_card_promote_confirm_writes(orchestrator):
    from alphaos.__main__ import cmd_card_promote

    hid, card_id, version = _make_eligible(orchestrator.journal)
    exit_code = cmd_card_promote(orchestrator, hid, "ck", None, confirm=True)

    assert exit_code == 0
    assert orchestrator.journal.count_rows("promotion_decisions") == 1


def test_cmd_card_promote_not_eligible_returns_1(orchestrator):
    from alphaos.__main__ import cmd_card_promote

    _insert_hypothesis(orchestrator.journal, "H-TEST", card_id=None, status="met")
    exit_code = cmd_card_promote(orchestrator, "H-TEST", "ck", None, confirm=True)

    assert exit_code == 1


def test_cmd_card_demote_dry_run_does_not_write(orchestrator):
    from alphaos.__main__ import cmd_card_demote

    _insert_card(orchestrator.journal, "card_a", 1, state="live_eligible")
    exit_code = cmd_card_demote(orchestrator, "card_a", 1, "ck", "test reason", confirm=False)

    assert exit_code == 0
    assert orchestrator.journal.count_rows("promotion_decisions") == 0


def test_cmd_card_demote_dry_run_reports_ineligible_card_honestly(orchestrator):
    """Scope/safety-audit LOW regression: previously this dry-run skipped
    any check at all and would print "Would demote" even for an
    unregistered or already-terminally-demoted card, contradicting what a
    --confirm run would then do. Now it runs the same precondition check
    demote_card() itself uses."""
    from alphaos.__main__ import cmd_card_demote

    exit_code = cmd_card_demote(orchestrator, "nope_card", 1, "ck", "test reason", confirm=False)

    assert exit_code == 1
    assert orchestrator.journal.count_rows("promotion_decisions") == 0


def test_cmd_autonomy_readiness_smoke(orchestrator):
    from alphaos.__main__ import cmd_autonomy_readiness

    assert cmd_autonomy_readiness(orchestrator) == 0


def test_cmd_hypothesis_mark_met_smoke(orchestrator):
    from alphaos.__main__ import cmd_hypothesis_mark_status

    _insert_hypothesis(orchestrator.journal, "H-TEST", status="resolved")
    assert cmd_hypothesis_mark_status(orchestrator, "H-TEST", "met", "ck", confirm=True) == 0


def test_cmd_hypothesis_mark_status_dry_run_does_not_write(orchestrator):
    """Fable5 strategy review fix: this write is permanent (reversible
    decision #9's own accepted trade-off), so it now has the SAME dry-run-
    by-default ceremony as card_promote -- no write happens without
    --confirm."""
    from alphaos.__main__ import cmd_hypothesis_mark_status

    _insert_hypothesis(orchestrator.journal, "H-TEST", status="resolved")
    exit_code = cmd_hypothesis_mark_status(orchestrator, "H-TEST", "met", "ck", confirm=False)

    assert exit_code == 0
    row = orchestrator.journal.one(
        "SELECT status FROM hypothesis_proposals WHERE hypothesis_id = ?", ("H-TEST",)
    )
    assert row["status"] == "resolved"  # unchanged


def test_cmd_hypothesis_mark_status_confirm_writes(orchestrator):
    from alphaos.__main__ import cmd_hypothesis_mark_status

    _insert_hypothesis(orchestrator.journal, "H-TEST", status="resolved")
    exit_code = cmd_hypothesis_mark_status(orchestrator, "H-TEST", "met", "ck", confirm=True)

    assert exit_code == 0
    row = orchestrator.journal.one(
        "SELECT status FROM hypothesis_proposals WHERE hypothesis_id = ?", ("H-TEST",)
    )
    assert row["status"] == "met"


def test_cmd_hypothesis_mark_status_not_eligible_returns_1_even_with_confirm(orchestrator):
    """A hypothesis that's still 'testing' (not yet resolved) must be
    refused BEFORE any write is attempted, confirm or not."""
    from alphaos.__main__ import cmd_hypothesis_mark_status

    _insert_hypothesis(orchestrator.journal, "H-TEST", status="testing")
    exit_code = cmd_hypothesis_mark_status(orchestrator, "H-TEST", "met", "ck", confirm=True)

    assert exit_code == 1
    row = orchestrator.journal.one(
        "SELECT status FROM hypothesis_proposals WHERE hypothesis_id = ?", ("H-TEST",)
    )
    assert row["status"] == "testing"  # unchanged


def test_cmd_hypothesis_mark_status_cannot_re_adjudicate_an_already_met_hypothesis(orchestrator):
    """Scope/safety-audit LOW regression: the 'testing' case above proves
    refusal-before-resolution, but not the specific one-way-door property
    this whole ceremony exists to protect -- that an ALREADY-adjudicated
    hypothesis (status='met') can never be flipped again, even to a
    DIFFERENT terminal status, even with --confirm. Covered only
    transitively via the 'testing' case before this test; a future refactor
    that special-cased 'testing' could pass every other test while quietly
    reopening this exact door."""
    from alphaos.__main__ import cmd_hypothesis_mark_status

    _insert_hypothesis(orchestrator.journal, "H-TEST", status="met")
    exit_code = cmd_hypothesis_mark_status(orchestrator, "H-TEST", "failed", "ck", confirm=True)

    assert exit_code == 1
    row = orchestrator.journal.one(
        "SELECT status FROM hypothesis_proposals WHERE hypothesis_id = ?", ("H-TEST",)
    )
    assert row["status"] == "met"  # unchanged -- never flipped to 'failed'


def test_cmd_hypothesis_mark_status_unknown_hypothesis_returns_1(orchestrator):
    from alphaos.__main__ import cmd_hypothesis_mark_status

    assert cmd_hypothesis_mark_status(orchestrator, "H-NOPE", "met", "ck", confirm=True) == 1


def test_check_status_change_preconditions_dry_run_shows_the_claim_text(orchestrator):
    """The whole point of the fix: an operator must see the hypothesis's
    own claim text in the preview before committing a permanent
    adjudication -- not just a bare eligible/not-eligible flag."""
    from alphaos.hypotheses import check_status_change_preconditions

    _insert_hypothesis(orchestrator.journal, "H-TEST", status="resolved")
    check = check_status_change_preconditions(orchestrator.journal, "H-TEST", "met")

    assert check["eligible"] is True
    assert check["claim"] == "test claim for H-TEST"
    assert check["current_status"] == "resolved"
    assert check["new_status"] == "met"


def test_check_status_change_preconditions_invalid_status_reason_code():
    from alphaos.hypotheses import check_status_change_preconditions
    from alphaos.journal.journal_store import JournalStore

    journal = JournalStore(":memory:")
    check = check_status_change_preconditions(journal, "H-ANYTHING", "resolved")
    assert check["eligible"] is False
    assert check["reason_code"] == "INVALID_STATUS"
    journal.close()


def test_check_status_change_preconditions_matches_mark_hypothesis_status_exactly(orchestrator):
    """The preview and the enforcement must never drift apart --
    mark_hypothesis_status() calls THIS SAME function internally rather
    than a separately-maintained copy of the same checks (mirrors
    promote_card()'s own relationship to check_promotion_preconditions()).

    Correctness-audit LOW regression: a plain substring search over
    inspect.getsource() passes vacuously off the function's OWN docstring
    (which itself mentions ``check_status_change_preconditions()`` in
    prose) even with the real call removed -- verified empirically by
    stripping the real call line and confirming a substring-only version of
    this test still passed. An AST walk for an actual Call node has no such
    blind spot: prose text in a docstring is a Constant/Expr node, never a
    Call, so this can only pass if the function genuinely invokes
    check_status_change_preconditions()."""
    import ast
    import inspect
    import textwrap

    from alphaos.hypotheses.registry import mark_hypothesis_status

    source = inspect.getsource(mark_hypothesis_status)
    tree = ast.parse(textwrap.dedent(source))
    calls = [
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "check_status_change_preconditions" in calls
