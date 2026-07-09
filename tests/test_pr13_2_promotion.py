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


def test_promoting_does_not_change_live_eligible_cards_by_itself(journal):
    """Promotion writes a decision row -- it does NOT itself flip
    setup_cards.state, so live_eligible_cards() (which reads setup_cards)
    correctly does not show this card as live_eligible until an operator
    actually edits the card's own YAML state field (out of scope for v0's
    graduation-only mechanism -- see promotion.py's own module docstring)."""
    hid, card_id, version = _make_eligible(journal)
    promotion.promote_card(journal, hid, decided_by="ck")
    cards = live_eligible_cards(journal)
    assert card_id not in [c["card_id"] for c in cards]


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


def test_cmd_autonomy_readiness_smoke(orchestrator):
    from alphaos.__main__ import cmd_autonomy_readiness

    assert cmd_autonomy_readiness(orchestrator) == 0


def test_cmd_hypothesis_mark_met_smoke(orchestrator):
    from alphaos.__main__ import cmd_hypothesis_mark_status

    _insert_hypothesis(orchestrator.journal, "H-TEST", status="resolved")
    assert cmd_hypothesis_mark_status(orchestrator, "H-TEST", "met", "ck") == 0
