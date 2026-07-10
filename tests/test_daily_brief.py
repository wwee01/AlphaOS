"""PR11 Daily Brief (alphaos/reports/daily_brief.py). Pure read composition
over other report modules; never writes, never touches gates/execution.
Hermetic; direct row construction throughout.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from alphaos.reports.daily_brief import (
    EXPIRING_SOON_SECONDS,
    MIN_TRADES_FOR_MOONSHOT_ESTIMATE,
    MOONSHOT_TARGET_MONTHLY_PCT,
    _atr_health,
    _fused_jobs,
    _hypothesis_resolution_status,
    _moonshot_gap,
    _one_action,
    _text_archive_health,
    build_daily_brief,
    render_compact,
    render_markdown,
)
from alphaos.scheduler.cadence import JobType
from alphaos.reports.position_health import VERDICT_EXIT_REVIEW, VERDICT_HOLD
from alphaos.util import timeutils
from alphaos.util.ids import new_id
from conftest import make_settings


# ------------------------------------------------------------- empty journal
def test_brief_renders_every_key_on_an_empty_journal(orchestrator):
    """The empty-state is a first-class case, not an error -- every
    top-level key must exist even when nothing has ever happened."""
    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)

    expected_keys = {
        "date_sgt", "kill_switch_engaged", "kill_switch_reason", "market_condition",
        "needs_you", "positions_health", "todays_activity", "best_candidate",
        "what_learned", "moonshot_gap", "one_action",
    }
    assert expected_keys <= brief.keys()
    assert brief["positions_health"] == []
    assert brief["best_candidate"] is None
    assert brief["one_action"]  # always a non-empty string
    assert brief["moonshot_gap"]["status"] == "below_sample_floor"


def test_render_markdown_and_compact_never_raise_on_empty_journal(orchestrator):
    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)

    md = render_markdown(brief)
    compact = render_compact(brief)

    assert "AlphaOS Daily Brief" in md
    assert "AlphaOS Daily Brief" in compact
    assert len(compact) < 1000  # must clear alerts.py's truncation cap comfortably


# ------------------------------------------------------- one-action priority
def _needs_you(incident=0, fused=None, pending=None, hypothesis_resolution=None):
    return {
        "pending_approval_count": len(pending or []),
        "pending_approvals": pending or [],
        "open_incident_count": incident,
        "open_incidents": [],
        "fused_jobs": fused or [],
        "hypothesis_resolution": hypothesis_resolution,
    }


def _ok_moonshot():
    return {"status": "ok"}


def _below_floor_moonshot():
    return {"status": "below_sample_floor", "data_progress": "1/5 resolved real trades this month"}


def test_priority_incident_beats_everything():
    needs_you = _needs_you(
        incident=1,
        fused=[{"job_type": "scan", "reason": "x", "streak": 3}],
        pending=[{"seconds_remaining": 30}],
    )
    positions_health = [{"symbol": "AAPL", "verdict": VERDICT_EXIT_REVIEW}]

    action = _one_action(needs_you, positions_health, _ok_moonshot())

    assert "incident" in action.lower()


def test_priority_fused_job_beats_expiring_and_exit_review():
    needs_you = _needs_you(
        incident=0,
        fused=[{"job_type": "monitor", "reason": "x", "streak": 3}],
        pending=[{"seconds_remaining": 30}],
    )
    positions_health = [{"symbol": "AAPL", "verdict": VERDICT_EXIT_REVIEW}]

    action = _one_action(needs_you, positions_health, _ok_moonshot())

    assert "self-halted" in action
    assert "monitor" in action


def test_priority_expiring_approval_beats_exit_review():
    needs_you = _needs_you(incident=0, fused=[], pending=[{"seconds_remaining": 30}])
    positions_health = [{"symbol": "AAPL", "verdict": VERDICT_EXIT_REVIEW}]

    action = _one_action(needs_you, positions_health, _ok_moonshot())

    assert "expiring" in action


def test_priority_exit_review_beats_below_floor_note():
    needs_you = _needs_you(incident=0, fused=[], pending=[])
    positions_health = [{"symbol": "AAPL", "verdict": VERDICT_EXIT_REVIEW}]

    action = _one_action(needs_you, positions_health, _below_floor_moonshot())

    assert "EXIT_REVIEW" in action
    assert "AAPL" in action


def test_exit_review_symbol_list_is_capped_for_a_large_flagged_count():
    """Audit-caught: an unbounded EXIT_REVIEW symbol join could exceed
    alerts.py's 1000-char truncation cap at large position counts and cut
    off mid-ticker. one_action must stay short regardless of how many
    positions are flagged."""
    from alphaos.reports.daily_brief import MAX_SYMBOLS_IN_ONE_ACTION

    needs_you = _needs_you(incident=0, fused=[], pending=[])
    positions_health = [
        {"symbol": f"VERYLONGTICKER{i:03d}", "verdict": VERDICT_EXIT_REVIEW} for i in range(200)
    ]

    action = _one_action(needs_you, positions_health, _ok_moonshot())

    assert len(action) < 300  # nowhere near alerts.py's 1000-char cap
    assert "+195 more" in action
    assert action.count("VERYLONGTICKER") == MAX_SYMBOLS_IN_ONE_ACTION


def test_priority_below_floor_note_when_nothing_else_pending():
    needs_you = _needs_you(incident=0, fused=[], pending=[])
    positions_health = [{"symbol": "AAPL", "verdict": VERDICT_HOLD}]

    action = _one_action(needs_you, positions_health, _below_floor_moonshot())

    assert "below the data floor" in action


def test_priority_nothing_needs_you_is_the_true_floor():
    needs_you = _needs_you(incident=0, fused=[], pending=[])
    positions_health = [{"symbol": "AAPL", "verdict": VERDICT_HOLD}]

    action = _one_action(needs_you, positions_health, _ok_moonshot())

    assert action == "Nothing needs you right now."


def test_expiring_soon_threshold_excludes_far_future_approvals():
    """A pending approval with ample time left must NOT count as 'expiring'."""
    needs_you = _needs_you(
        incident=0, fused=[], pending=[{"seconds_remaining": EXPIRING_SOON_SECONDS + 500}],
    )
    positions_health = []

    action = _one_action(needs_you, positions_health, _ok_moonshot())

    assert action == "Nothing needs you right now."


def test_expiring_soon_excludes_already_expired_approvals():
    """A NEGATIVE seconds_remaining (already lapsed) is a different concern
    than 'about to expire' -- must not double-count as the expiring bucket."""
    needs_you = _needs_you(incident=0, fused=[], pending=[{"seconds_remaining": -30}])

    action = _one_action(needs_you, [], _ok_moonshot())

    assert action == "Nothing needs you right now."


# --------------------------------- PR12 hypothesis-resolution alert (_one_action)
def _first_ever_resolution():
    return {
        "resolved_today_count": 1,
        "resolved_today": [{"hypothesis_id": "H-WIN-1", "last_verdict": "inconclusive"}],
        "is_first_ever": True,
    }


def _routine_resolution():
    return {
        "resolved_today_count": 1,
        "resolved_today": [{"hypothesis_id": "H-TQS-1", "last_verdict": "forward-test-candidate"}],
        "is_first_ever": False,
    }


def test_priority_first_ever_hypothesis_resolution_pings_to_build_the_generator():
    needs_you = _needs_you(incident=0, fused=[], pending=[], hypothesis_resolution=_first_ever_resolution())
    positions_health = [{"symbol": "AAPL", "verdict": VERDICT_HOLD}]

    action = _one_action(needs_you, positions_health, _ok_moonshot())

    assert "H-WIN-1" in action
    assert "first time" in action
    assert "ask Claude" in action
    assert "LLM-based hypothesis generator" in action


def test_priority_routine_hypothesis_resolution_does_not_pitch_the_generator():
    """A LATER (non-first) resolution is still worth surfacing, but must not
    repeat the "ask Claude to build the generator" pitch every time --
    that message is reserved for the one-time milestone."""
    needs_you = _needs_you(incident=0, fused=[], pending=[], hypothesis_resolution=_routine_resolution())
    positions_health = [{"symbol": "AAPL", "verdict": VERDICT_HOLD}]

    action = _one_action(needs_you, positions_health, _ok_moonshot())

    assert "H-TQS-1" in action
    assert "resolved today" in action
    assert "ask Claude" not in action


def test_priority_hypothesis_resolution_beats_below_floor_note():
    needs_you = _needs_you(incident=0, fused=[], pending=[], hypothesis_resolution=_first_ever_resolution())
    positions_health = [{"symbol": "AAPL", "verdict": VERDICT_HOLD}]

    action = _one_action(needs_you, positions_health, _below_floor_moonshot())

    assert "H-WIN-1" in action
    assert "below the data floor" not in action


def test_priority_exit_review_beats_hypothesis_resolution():
    needs_you = _needs_you(incident=0, fused=[], pending=[], hypothesis_resolution=_first_ever_resolution())
    positions_health = [{"symbol": "AAPL", "verdict": VERDICT_EXIT_REVIEW}]

    action = _one_action(needs_you, positions_health, _ok_moonshot())

    assert "EXIT_REVIEW" in action
    assert "H-WIN-1" not in action


def test_priority_expiring_approval_beats_hypothesis_resolution():
    needs_you = _needs_you(
        incident=0, fused=[], pending=[{"seconds_remaining": 30}], hypothesis_resolution=_first_ever_resolution(),
    )

    action = _one_action(needs_you, [], _ok_moonshot())

    assert "expiring" in action
    assert "H-WIN-1" not in action


def test_no_hypothesis_resolution_falls_through_to_below_floor_note():
    """hypothesis_resolution=None (the common case -- no resolutions today)
    must not itself become an action; the chain falls through normally."""
    needs_you = _needs_you(incident=0, fused=[], pending=[], hypothesis_resolution=None)
    positions_health = [{"symbol": "AAPL", "verdict": VERDICT_HOLD}]

    action = _one_action(needs_you, positions_health, _below_floor_moonshot())

    assert "below the data floor" in action


# --------------------------- PR12 hypothesis-resolution alert (_hypothesis_resolution_status)
def _insert_hypothesis_row(journal, hypothesis_id, status="testing", resolved_at_utc=None, last_verdict=None):
    journal.insert("hypothesis_proposals", {
        "hypothesis_id": hypothesis_id,
        "risk_class": "B",
        "claim": f"test claim for {hypothesis_id}",
        "analysis_not_before": "2026-01-01",
        "status": status,
        "resolved_at_utc": resolved_at_utc,
        "last_verdict": last_verdict,
    })


def test_hypothesis_resolution_status_none_on_a_quiet_day(journal):
    _insert_hypothesis_row(journal, "H-TQS-1", status="testing")
    assert _hypothesis_resolution_status(journal, "2026-07-11T00:00:00+00:00") is None


def test_hypothesis_resolution_status_detects_first_ever_resolution(journal):
    _insert_hypothesis_row(
        journal, "H-WIN-1", status="resolved",
        resolved_at_utc="2026-07-11T10:00:00+00:00", last_verdict="inconclusive",
    )
    _insert_hypothesis_row(journal, "H-TQS-1", status="testing")  # still pending -- must not count

    result = _hypothesis_resolution_status(journal, "2026-07-11T00:00:00+00:00")

    assert result["resolved_today_count"] == 1
    assert result["resolved_today"][0]["hypothesis_id"] == "H-WIN-1"
    assert result["is_first_ever"] is True


def test_hypothesis_resolution_status_not_first_ever_when_an_older_one_already_resolved(journal):
    """The registry demonstrated a resolution on an EARLIER day -- today's
    resolution is real and worth surfacing, but is NOT the first-ever
    milestone, so the generator pitch must not fire again."""
    _insert_hypothesis_row(
        journal, "H-TQS-1", status="resolved",
        resolved_at_utc="2026-07-05T10:00:00+00:00", last_verdict="forward-test-candidate",
    )
    _insert_hypothesis_row(
        journal, "H-WIN-1", status="resolved",
        resolved_at_utc="2026-07-11T10:00:00+00:00", last_verdict="inconclusive",
    )

    result = _hypothesis_resolution_status(journal, "2026-07-11T00:00:00+00:00")

    assert result["resolved_today_count"] == 1
    assert result["resolved_today"][0]["hypothesis_id"] == "H-WIN-1"
    assert result["is_first_ever"] is False


def test_hypothesis_resolution_status_scoped_to_today_only(journal):
    """A hypothesis resolved YESTERDAY must not appear in today's list --
    since_sgt scoping matches every other _today() helper in this module."""
    _insert_hypothesis_row(
        journal, "H-TQS-1", status="resolved",
        resolved_at_utc="2026-07-10T10:00:00+00:00", last_verdict="forward-test-candidate",
    )

    assert _hypothesis_resolution_status(journal, "2026-07-11T00:00:00+00:00") is None


def test_hypothesis_resolution_status_multiple_resolving_same_day_are_all_first_ever(journal):
    """Two hypotheses clearing their floor on the exact same day is still
    the registry's first-ever resolution collectively -- both belong in
    resolved_today, and the milestone still fires."""
    _insert_hypothesis_row(
        journal, "H-WIN-1", status="resolved",
        resolved_at_utc="2026-07-11T09:00:00+00:00", last_verdict="inconclusive",
    )
    _insert_hypothesis_row(
        journal, "H-TQS-1", status="resolved",
        resolved_at_utc="2026-07-11T10:00:00+00:00", last_verdict="forward-test-candidate",
    )

    result = _hypothesis_resolution_status(journal, "2026-07-11T00:00:00+00:00")

    assert result["resolved_today_count"] == 2
    assert result["is_first_ever"] is True


def test_render_markdown_includes_hypothesis_resolution_milestone_line(orchestrator):
    _insert_hypothesis_row(
        orchestrator.journal, "H-WIN-1", status="resolved",
        resolved_at_utc=timeutils.now_utc().isoformat(), last_verdict="inconclusive",
    )
    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    md = render_markdown(brief)
    assert "Hypotheses resolved today" in md
    assert "H-WIN-1" in md
    assert "first-ever resolution" in md


def test_render_markdown_omits_hypothesis_resolution_line_on_a_quiet_day(orchestrator):
    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    md = render_markdown(brief)
    assert "Hypotheses resolved today" not in md


# ------------------------------------------------------------- moonshot gap
def test_moonshot_gap_below_floor_withholds_arithmetic(journal):
    settings = make_settings()
    gap = _moonshot_gap(journal, settings)

    assert gap["status"] == "below_sample_floor"
    assert gap["trades_this_month"] == 0
    assert "implied_monthly_pct" not in gap


def test_moonshot_gap_known_inputs_produce_known_output(journal):
    """Hand-verified: 5 trades this month, mean realized_r=0.4, risk=1% ->
    implied_monthly_pct = 0.4 * 5 * 0.01 * 100 = 2.0%."""
    settings = make_settings(MAX_RISK_PER_TRADE_PCT="0.01")
    now = timeutils.now_utc()

    for i, r in enumerate([0.2, 0.3, 0.4, 0.5, 0.6]):  # mean = 0.4
        pos_id = new_id("pos")
        journal.insert("positions", {"position_id": pos_id, "symbol": "AAPL", "is_demo": 0})
        journal.insert("trade_outcomes", {
            "outcome_id": new_id("out"), "position_id": pos_id, "symbol": "AAPL",
            "realized_r": r,
        })

    gap = _moonshot_gap(journal, settings, now=now)

    assert gap["status"] == "ok"
    assert gap["trades_this_month"] == 5
    assert gap["expectancy_r"] == pytest.approx(0.4, abs=1e-6)
    assert gap["implied_monthly_pct"] == pytest.approx(2.0, abs=1e-6)
    assert gap["target_monthly_pct"] == MOONSHOT_TARGET_MONTHLY_PCT
    assert gap["binding_constraint"] == "frequency"  # 2% << 10% target at this expectancy/risk


def test_moonshot_gap_excludes_demo_positions(journal):
    settings = make_settings()
    now = timeutils.now_utc()
    for i in range(MIN_TRADES_FOR_MOONSHOT_ESTIMATE):
        pos_id = new_id("pos")
        journal.insert("positions", {"position_id": pos_id, "symbol": "DEMO", "is_demo": 1})
        journal.insert("trade_outcomes", {
            "outcome_id": new_id("out"), "position_id": pos_id, "symbol": "DEMO", "realized_r": 1.0,
        })

    gap = _moonshot_gap(journal, settings, now=now)

    assert gap["status"] == "below_sample_floor"
    assert gap["trades_this_month"] == 0


def test_moonshot_gap_excludes_last_months_trades(journal):
    settings = make_settings()
    now = timeutils.now_utc()
    old = timeutils.to_iso(now - timedelta(days=45))
    for i in range(MIN_TRADES_FOR_MOONSHOT_ESTIMATE):
        pos_id = new_id("pos")
        journal.insert("positions", {"position_id": pos_id, "symbol": "AAPL", "is_demo": 0})
        journal.conn.execute(
            "INSERT INTO trade_outcomes (outcome_id, position_id, symbol, realized_r, "
            "created_at_utc, created_at_sgt) VALUES (?, ?, ?, ?, ?, ?)",
            (new_id("out"), pos_id, "AAPL", 1.0, old, old),
        )
    journal.conn.commit()

    gap = _moonshot_gap(journal, settings, now=now)

    assert gap["status"] == "below_sample_floor"
    assert gap["trades_this_month"] == 0


def test_moonshot_gap_negative_expectancy(journal):
    settings = make_settings()
    now = timeutils.now_utc()
    for r in [-0.2, -0.3, -0.1, 0.1, -0.5]:  # mean negative
        pos_id = new_id("pos")
        journal.insert("positions", {"position_id": pos_id, "symbol": "AAPL", "is_demo": 0})
        journal.insert("trade_outcomes", {
            "outcome_id": new_id("out"), "position_id": pos_id, "symbol": "AAPL", "realized_r": r,
        })

    gap = _moonshot_gap(journal, settings, now=now)

    assert gap["status"] == "ok"
    assert gap["expectancy_r"] < 0
    assert gap["binding_constraint"] == "expectancy"


# --------------------------------------------- PORT-1 survivorship denominator
def test_moonshot_gap_survivorship_caveat_present_on_empty_registry(journal):
    """An honest 0/0/0 -- the mechanism exists, nothing has registered a
    hypothesis yet (PR12 is the future writer), never omitted."""
    settings = make_settings()
    gap = _moonshot_gap(journal, settings)

    assert gap["preregistration_family"] == {
        "hypotheses_registered": 0, "hypotheses_tested": 0, "promoted": 0,
    }
    assert "hypotheses" in gap["survivorship_caveat"].lower() or "system-level" in gap["survivorship_caveat"].lower()


def test_moonshot_gap_survivorship_caveat_present_on_ok_status_too(journal):
    """The caveat is about the PREREGISTRATION family, not about whether the
    trade-count floor was met -- must appear on the "ok" branch as well."""
    settings = make_settings(MAX_RISK_PER_TRADE_PCT="0.01")
    now = timeutils.now_utc()
    for r in [0.2, 0.3, 0.4, 0.5, 0.6]:
        pos_id = new_id("pos")
        journal.insert("positions", {"position_id": pos_id, "symbol": "AAPL", "is_demo": 0})
        journal.insert("trade_outcomes", {
            "outcome_id": new_id("out"), "position_id": pos_id, "symbol": "AAPL", "realized_r": r,
        })

    gap = _moonshot_gap(journal, settings, now=now)

    assert gap["status"] == "ok"
    assert gap["preregistration_family"] == {
        "hypotheses_registered": 0, "hypotheses_tested": 0, "promoted": 0,
    }
    assert gap["survivorship_caveat"]


def test_moonshot_gap_survivorship_counts_full_family_not_just_promoted(journal):
    """The count is over promoted + demoted + withdrawn -- never just the
    promoted subset (contract doc port spec item 5)."""
    from alphaos.stats.preregistration import evaluate_hypothesis, register_hypothesis

    settings = make_settings()
    rejected_id = register_hypothesis(
        journal, hypothesis="rejected one", metric="delta_r",
        floor_effective_n=2, floor_span_days=1, analysis_not_before="2026-09-01",
    )
    evaluate_hypothesis(
        journal, rejected_id,
        [{"symbol": "AAPL", "decision_date": "2026-01-01", "delta_r": -5.0},
         {"symbol": "MSFT", "decision_date": "2026-01-02", "delta_r": -5.0}],
        value_key="delta_r", seed=1,
    )
    register_hypothesis(  # never evaluated -- must NOT count toward hypotheses_tested
        journal, hypothesis="still pending", metric="delta_r",
        floor_effective_n=2, floor_span_days=1, analysis_not_before="2026-09-01",
    )

    gap = _moonshot_gap(journal, settings)

    assert gap["preregistration_family"] == {
        "hypotheses_registered": 2, "hypotheses_tested": 1, "promoted": 0,
    }


def test_render_markdown_includes_survivorship_caveat_line(orchestrator):
    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    md = render_markdown(brief)
    assert "hypotheses_tested=" in md
    assert "promoted=" in md


# ------------------------------------------------------- floors/caveats present
def test_market_condition_caveat_always_present(orchestrator):
    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    assert brief["market_condition"]["caveat"]


def test_what_learned_caveat_always_present(orchestrator):
    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    assert brief["what_learned"]["caveat"]


def test_what_learned_excludes_mock_rows(journal):
    from alphaos.reports.daily_brief import _what_learned

    since = timeutils.to_iso(timeutils.now_utc() - timedelta(hours=1))
    journal.insert("attribution_records", {
        "attribution_id": new_id("attr"), "attribution_type": "propose_blocked",
        "attribution_version": "v2", "agent": "alphaos", "source_id": new_id("s"),
        "symbol": "AAPL", "resolved_status": "resolved", "delta_r": 1.0,
        "data_quality_status": "ok", "is_mock": 1,
    })
    journal.conn.execute(
        "UPDATE attribution_records SET resolved_at_utc = ? WHERE symbol = 'AAPL'",
        (timeutils.to_iso(timeutils.now_utc()),),
    )
    journal.conn.commit()

    learned = _what_learned(journal, since)

    assert learned["count"] == 0  # mock row correctly excluded


def _insert_resolved_attribution_row(journal, symbol="AAPL", delta_r=0.34):
    journal.insert("attribution_records", {
        "attribution_id": new_id("attr"), "attribution_type": "propose_blocked",
        "attribution_version": "v2", "agent": "alphaos", "source_id": new_id("s"),
        "symbol": symbol, "resolved_status": "resolved", "delta_r": delta_r,
        "data_quality_status": "ok", "is_mock": 0,
    })
    journal.conn.execute(
        "UPDATE attribution_records SET resolved_at_utc = ? WHERE symbol = ?",
        (timeutils.to_iso(timeutils.now_utc()), symbol),
    )
    journal.conn.commit()


def test_learned_sentence_never_renders_a_per_event_delta_r_figure(journal):
    """audit C4: a single resolved event's ΔR is not a verdict -- the
    rendered brief must never show a bare per-event number, even for exactly
    one resolved row (the smallest possible sample, and the case most likely
    to be mistaken for a meaningful result)."""
    from alphaos.reports.daily_brief import _what_learned

    since = timeutils.to_iso(timeutils.now_utc() - timedelta(hours=1))
    _insert_resolved_attribution_row(journal, symbol="AAPL", delta_r=0.34)

    learned = _what_learned(journal, since)

    assert learned["count"] == 1
    assert len(learned["sentences"]) == 1
    assert "AAPL" in learned["sentences"][0]
    assert "0.34" not in learned["sentences"][0]
    assert "ΔR" not in learned["sentences"][0]
    assert learned["caveat"]  # still present, unconditionally


def test_render_markdown_omits_delta_r_figure_and_keeps_caveat(orchestrator):
    """End-to-end: even with a real resolved row flowing all the way through
    build_daily_brief -> render_markdown, no per-event ΔR number reaches the
    rendered text, and the standing caveat is still there."""
    _insert_resolved_attribution_row(orchestrator.journal, symbol="TSLA", delta_r=-1.2)

    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    md = render_markdown(brief)

    assert "TSLA" in md
    assert "ΔR=" not in md
    assert "-1.2" not in md
    assert "This is NOT a per-event verdict" in md  # ATTRIBUTION_V2_CAVEAT text


def test_what_learned_total_resolved_today_is_not_capped_by_the_sentence_limit(journal):
    """audit MEDIUM (both independent audits, 2026-07-10): the aggregate
    headline count must reflect the TRUE number of today's resolutions, not
    just how many sentence bullets are shown (LIMIT-capped at
    UP_TO_N_LEARNED_SENTENCES=3) -- otherwise a busy day silently
    under-reports its own activity count."""
    from alphaos.reports.daily_brief import _what_learned

    since = timeutils.to_iso(timeutils.now_utc() - timedelta(hours=1))
    for i in range(5):
        _insert_resolved_attribution_row(journal, symbol=f"SYM{i}", delta_r=0.1)

    learned = _what_learned(journal, since)

    assert learned["total_resolved_today"] == 5  # the true count, never capped
    assert len(learned["sentences"]) == 3         # the display list stays capped
    assert learned["count"] == 3                  # unchanged meaning: len(sentences)


def test_render_markdown_aggregate_line_uses_true_count_not_sentence_cap(orchestrator):
    for i in range(5):
        _insert_resolved_attribution_row(orchestrator.journal, symbol=f"SYM{i}", delta_r=0.1)

    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    md = render_markdown(brief)

    assert "5 decision(s) resolved today" in md
    assert "3 decision(s) resolved today" not in md


# ------------------------------------------------------------------ no-read
def test_no_decision_path_reads_brief_or_health_modules():
    import pathlib

    import alphaos.approval as approval_mod
    import alphaos.risk.risk_engine as risk_mod
    from alphaos.execution import order_manager as order_mod

    for mod, name in ((approval_mod, "approval.py"), (risk_mod, "risk_engine.py"),
                      (order_mod, "order_manager.py")):
        text = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert "daily_brief" not in text and "position_health" not in text, \
            f"{name} references a PR11 report module"


# --------------------------------------------------------- job + alert wiring
def test_daily_digest_job_sends_a_compact_alert(orchestrator, monkeypatch):
    from alphaos.scheduler.jobs import run_daily_digest_job

    calls = []
    monkeypatch.setattr(
        "alphaos.util.alerts.send_alert",
        lambda settings, title, message, priority="default", journal=None: calls.append(
            (title, message, priority)
        ) or True,
    )

    result = run_daily_digest_job(orchestrator, runner=None)

    assert result["status"] == "completed"
    assert len(calls) == 1
    title, message, priority = calls[0]
    assert title == result["brief"]["one_action"]
    assert "AlphaOS Daily Brief" in message


def test_fused_jobs_detects_a_real_fuse_via_job_runs_rows(orchestrator):
    """A genuine fuse (N consecutive failed job_runs rows), not a hand-built
    fused_jobs list -- proves _fused_jobs actually reads cadence.is_fused
    correctly for every JobType, not just the ones the unit tests hand it."""
    threshold = orchestrator.settings.scheduler_max_consecutive_failures
    for i in range(threshold):
        st = timeutils.stamp()
        orchestrator.journal.insert("job_runs", {
            "job_run_id": new_id("jr"), "job_type": JobType.MONITOR.value,
            "status": "failed", "started_at_utc": st.utc, "started_at_sgt": st.local_sgt,
        })

    fused = _fused_jobs(orchestrator.journal, orchestrator.settings)

    assert any(f["job_type"] == JobType.MONITOR.value for f in fused)
    assert not any(f["job_type"] == JobType.SCAN.value for f in fused)  # unrelated job type unaffected

    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    assert brief["needs_you"]["fused_jobs"]
    assert "self-halted" in brief["one_action"]


# ------------------------------------------------------- TEXT-0 health line
def _insert_text_doc(journal, seen_at: str, accession: str, sha256: str = "0" * 64) -> None:
    journal.insert("text_documents", {
        "document_id": new_id("txtdoc"), "cik": "320193", "ticker_at_time": "AAPL",
        "form_type": "8-K", "edgar_forms_version": "edgar_forms_v1", "accession_no": accession,
        "published_at": seen_at, "seen_at": seen_at, "source_url": "https://example.test/doc",
        "sha256": sha256, "byte_size": 10, "storage_path": f"data/text_archive/x/{accession}.gz",
        "source": "edgar", "fetch_run_id": "fetchrun_test",
    })


def _insert_job_run(journal, job_type: str, status: str, finished_at_utc: str,
                    result_summary: dict = None) -> None:
    import json as _json
    journal.insert("job_runs", {
        "job_run_id": new_id("jobrun"), "job_type": job_type, "trigger_source": "test",
        "lock_key": new_id("lock"), "started_at_utc": finished_at_utc, "started_at_sgt": finished_at_utc,
        "finished_at_utc": finished_at_utc, "finished_at_sgt": finished_at_utc, "duration_ms": 1,
        "status": status,
        "result_summary_json": _json.dumps(result_summary) if result_summary is not None else None,
    })


def test_text_archive_health_none_on_an_empty_archive(journal):
    since_sgt = timeutils.stamp().utc
    assert _text_archive_health(journal, since_sgt) is None


def test_text_archive_health_counts_and_no_gap(journal):
    today = timeutils.market_date()
    days = []
    d = today - timedelta(days=1)
    while len(days) < 4:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.sort()
    for i, day in enumerate(days):
        _insert_text_doc(journal, f"{day.isoformat()}T12:00:00+00:00", f"acc-{i}")

    since_sgt = timeutils.stamp().utc  # nothing archived "tonight" in this test
    _insert_job_run(journal, "text_archive_pull", "completed", since_sgt, {
        "status": "completed", "pull_result": {"fetch_errors": 2},
    })

    result = _text_archive_health(journal, since_sgt)

    assert result["total"] == len(days)
    assert result["docs_last_night"] == 0
    assert result["fetch_errors_last_night"] == 2
    assert result["oldest_gap"] is None


def test_text_archive_health_docs_last_night_scoped_to_since_sgt(journal):
    today = timeutils.market_date()
    yesterday = today - timedelta(days=1)
    _insert_text_doc(journal, f"{yesterday.isoformat()}T08:00:00+00:00", "acc-old")

    since_sgt = timeutils.stamp().utc
    _insert_text_doc(journal, since_sgt, "acc-new")

    result = _text_archive_health(journal, since_sgt)

    assert result["total"] == 2
    assert result["docs_last_night"] == 1  # only the one at/after since_sgt


def test_text_archive_health_fetch_errors_excludes_other_job_types_and_incomplete_runs(journal):
    today = timeutils.market_date()
    _insert_text_doc(journal, f"{(today - timedelta(days=1)).isoformat()}T12:00:00+00:00", "acc-1")
    since_sgt = timeutils.stamp().utc

    _insert_job_run(journal, "text_archive_pull", "completed", since_sgt,
                    {"status": "completed", "pull_result": {"fetch_errors": 3}})
    _insert_job_run(journal, "text_archive_pull", "started", since_sgt, None)  # not completed -- excluded
    _insert_job_run(journal, "benchmark_spine", "completed", since_sgt,
                    {"status": "completed", "some_other_shape": True})  # different job -- excluded
    before_window = timeutils.to_iso(timeutils.now_utc() - timedelta(days=2))
    _insert_job_run(journal, "text_archive_pull", "completed", before_window,
                    {"status": "completed", "pull_result": {"fetch_errors": 99}})  # outside window

    result = _text_archive_health(journal, since_sgt)

    assert result["fetch_errors_last_night"] == 3


def test_text_archive_health_gap_on_the_oldest_eligible_boundary_day(journal):
    """Regression guard: an off-by-one on the gap-walk's inclusive upper
    bound (yesterday) would silently miss a gap sitting exactly there."""
    today = timeutils.market_date()
    yesterday = today - timedelta(days=1)
    d = yesterday - timedelta(days=1)
    while d.weekday() >= 5:  # earliest doc must land on a weekday too
        d -= timedelta(days=1)
    earliest = d
    _insert_text_doc(journal, f"{earliest.isoformat()}T12:00:00+00:00", "acc-earliest")
    # yesterday deliberately left unarchived -- if yesterday is a weekday,
    # it's the gap; walk forward from earliest to confirm.
    gap_day = yesterday if yesterday.weekday() < 5 else None
    if gap_day is None:
        pytest.skip("yesterday falls on a weekend this run -- boundary case not exercisable today")

    since_sgt = timeutils.stamp().utc
    result = _text_archive_health(journal, since_sgt)

    assert result["oldest_gap"] == gap_day.isoformat()


def test_text_archive_health_weekend_never_counts_as_a_gap(journal):
    """A Friday-to-Monday archive with every WEEKDAY covered (including
    'yesterday') and nothing archived over the weekend itself must report no
    gap -- weekends are never a probable trading day. Every weekday from the
    earliest doc through yesterday is inserted so the only unarchived days
    are the weekend, isolating this from an unrelated real gap."""
    today = timeutils.market_date()
    yesterday = today - timedelta(days=1)
    friday = yesterday
    while friday.weekday() != 4:
        friday -= timedelta(days=1)
    monday = friday + timedelta(days=3)
    if monday > yesterday:
        pytest.skip("not enough calendar room before today for this run's date")

    d, i = friday, 0
    while d <= yesterday:
        if d.weekday() < 5:
            _insert_text_doc(journal, f"{d.isoformat()}T12:00:00+00:00", f"acc-{i}")
            i += 1
        d += timedelta(days=1)

    since_sgt = timeutils.stamp().utc
    result = _text_archive_health(journal, since_sgt)

    assert result["oldest_gap"] is None


def test_text_archive_health_rendered_in_markdown_brief(orchestrator):
    _insert_text_doc(orchestrator.journal, timeutils.stamp().utc, "acc-render")

    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    md = render_markdown(brief)

    assert brief["text_archive_health"] is not None
    assert "Text archive" in md
    assert "oldest gap" in md


def test_text_archive_health_section_omitted_when_never_archived(orchestrator):
    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    md = render_markdown(brief)

    assert brief["text_archive_health"] is None
    assert "## Text archive" not in md


def _insert_atr(journal, symbol: str, market_date: str, rules_version: str = "atr_rules_v1") -> None:
    journal.insert("atr_history", {
        "atr_id": new_id("atr"), "symbol": symbol, "market_date": market_date,
        "atr_14": 1.23, "rules_version": rules_version, "n_bars_fetched": 15,
    })


def test_atr_health_none_when_never_captured(journal):
    assert _atr_health(journal) is None


def test_atr_health_full_coverage_reports_no_missing_symbols(journal):
    from alphaos.scanner.candidate_scanner import DEFAULT_UNIVERSE

    today = timeutils.market_date().isoformat()
    for symbol in DEFAULT_UNIVERSE:
        _insert_atr(journal, symbol, today)

    result = _atr_health(journal)

    assert result["as_of_date"] == today
    assert result["n_covered"] == len(DEFAULT_UNIVERSE)
    assert result["n_universe"] == len(DEFAULT_UNIVERSE)
    assert result["missing_symbols"] == []


def test_atr_health_partial_coverage_lists_missing_symbols(journal):
    from alphaos.scanner.candidate_scanner import DEFAULT_UNIVERSE

    today = timeutils.market_date().isoformat()
    covered = DEFAULT_UNIVERSE[:5]
    for symbol in covered:
        _insert_atr(journal, symbol, today)

    result = _atr_health(journal)

    assert result["n_covered"] == 5
    assert result["n_universe"] == len(DEFAULT_UNIVERSE)
    assert set(result["missing_symbols"]) == set(DEFAULT_UNIVERSE) - set(covered)


def test_atr_health_uses_only_the_latest_capture_date(journal):
    """An older, fully-covered date must not mask a gap on the latest date --
    the health line is 'are we covered AS OF NOW', not 'were we ever
    covered'. A symbol covered only on the older, superseded date is a real,
    reportable gap."""
    from alphaos.scanner.candidate_scanner import DEFAULT_UNIVERSE

    older = (timeutils.market_date() - timedelta(days=1)).isoformat()
    newer = timeutils.market_date().isoformat()
    for symbol in DEFAULT_UNIVERSE:
        _insert_atr(journal, symbol, older)
    for symbol in DEFAULT_UNIVERSE[:3]:  # only a subset re-covered on the latest date
        _insert_atr(journal, symbol, newer)

    result = _atr_health(journal)

    assert result["as_of_date"] == newer
    assert result["n_covered"] == 3
    assert set(result["missing_symbols"]) == set(DEFAULT_UNIVERSE[3:])


def test_atr_health_ignores_rows_from_a_different_rules_version(journal):
    """A future atr_rules_v2 row must never silently count as v1 coverage --
    versioned-formula-constants law (§H.8): cross-version rows never mix."""
    from alphaos.scanner.candidate_scanner import DEFAULT_UNIVERSE

    today = timeutils.market_date().isoformat()
    for symbol in DEFAULT_UNIVERSE:
        _insert_atr(journal, symbol, today, rules_version="atr_rules_v2_hypothetical")

    result = _atr_health(journal)

    assert result is None  # zero v1 rows -> never-captured state, not fabricated coverage


def test_atr_health_rendered_in_markdown_brief_with_a_gap(orchestrator):
    from alphaos.scanner.candidate_scanner import DEFAULT_UNIVERSE

    today = timeutils.market_date().isoformat()
    _insert_atr(orchestrator.journal, DEFAULT_UNIVERSE[0], today)

    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    md = render_markdown(brief)

    assert brief["atr_health"] is not None
    assert "ATR coverage" in md
    assert "GAP" in md
    assert DEFAULT_UNIVERSE[1] in md  # a missing symbol is actually named


def test_atr_health_section_omitted_when_never_captured(orchestrator):
    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    md = render_markdown(brief)

    assert brief["atr_health"] is None
    assert "## ATR coverage" not in md


def test_digest_position_health_mirrors_tqs_shadow_shape(orchestrator):
    from alphaos.scheduler.digest import build_daily_digest

    orchestrator.run_scan_once()
    pending = orchestrator.list_open_proposals()
    if pending:
        orchestrator.approve_proposal(pending[0]["proposal_id"], approver="test")

    digest = build_daily_digest(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)

    ph = digest["position_health"]
    assert "open_position_count" in ph
    assert "verdict_histogram" in ph
    assert "thesis_histogram" in ph
    assert sum(ph["verdict_histogram"].values()) == ph["open_position_count"]
