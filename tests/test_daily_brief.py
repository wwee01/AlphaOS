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
    _fused_jobs,
    _moonshot_gap,
    _one_action,
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
def _needs_you(incident=0, fused=None, pending=None):
    return {
        "pending_approval_count": len(pending or []),
        "pending_approvals": pending or [],
        "open_incident_count": incident,
        "open_incidents": [],
        "fused_jobs": fused or [],
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


# ------------------------------------------------------- floors/caveats present
def test_market_condition_caveat_always_present(orchestrator):
    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    assert brief["market_condition"]["caveat"]


def test_what_learned_caveat_always_present(orchestrator):
    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    assert brief["what_learned"]["caveat"]


def _resolved_attribution_row(journal, symbol="AAPL", delta_r=1.23, resolved_at_utc=None):
    journal.insert("attribution_records", {
        "attribution_id": new_id("attr"), "attribution_type": "propose_blocked",
        "attribution_version": "v2", "agent": "alphaos", "source_id": new_id("s"),
        "symbol": symbol, "resolved_status": "resolved", "delta_r": delta_r,
        "data_quality_status": "ok", "is_mock": 0,
        "resolved_at_utc": resolved_at_utc or timeutils.to_iso(timeutils.now_utc()),
    })


def test_single_resolved_row_renders_no_per_event_delta_r(orchestrator):
    """BRIEF-FIX-1 (audit C4): one resolved real row must NOT surface its
    ΔR figure anywhere in the rendered brief -- per-event verdicts are
    forbidden (§H.9); ΔR appears only floor-gated via `alphaos attribution`.
    The standing caveat must be present."""
    from alphaos.reports.attribution import ATTRIBUTION_V2_CAVEAT

    _resolved_attribution_row(orchestrator.journal, delta_r=1.23)

    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    md = render_markdown(brief)

    assert "ΔR=" not in md
    assert "1.23" not in md  # the row's delta_r figure never reaches the operator
    assert "AAPL: propose blocked resolved." in md  # descriptive line survives, number-free
    assert "1 decision(s) resolved today, 1 cumulative" in md
    assert ATTRIBUTION_V2_CAVEAT in md


def test_learned_headline_is_floor_gated_below_floor(journal):
    from alphaos.reports.daily_brief import _what_learned

    since = timeutils.to_iso(timeutils.now_utc() - timedelta(hours=1))
    _resolved_attribution_row(journal)

    learned = _what_learned(journal, since)

    assert learned["count"] == 1
    assert learned["cumulative_resolved_count"] == 1
    assert learned["aggregate_floor_met"] is False
    assert "once floors met" in learned["headline"]
    assert all("ΔR" not in s for s in learned["sentences"])


def test_learned_headline_flips_wording_once_aggregate_floor_met(journal):
    """30 resolved rows spread over 29 calendar days meets both v2 floors
    (n>=30, span>=28d); the headline then points at the attribution report
    as *available* rather than 'once floors met' -- still never a ΔR figure."""
    from alphaos.reports.attribution import (
        MIN_RESOLVED_FOR_V2_AGGREGATE,
        MIN_SPAN_DAYS_FOR_V2_AGGREGATE,
    )
    from alphaos.reports.daily_brief import _what_learned

    now = timeutils.now_utc()
    since = timeutils.to_iso(now - timedelta(hours=1))
    for i in range(MIN_RESOLVED_FOR_V2_AGGREGATE):  # days 0..29 -> span 29d >= 28d floor
        _resolved_attribution_row(
            journal, symbol=f"SYM{i:02d}", resolved_at_utc=timeutils.to_iso(now - timedelta(days=i)),
        )
    assert MIN_RESOLVED_FOR_V2_AGGREGATE - 1 >= MIN_SPAN_DAYS_FOR_V2_AGGREGATE  # fixture sanity

    learned = _what_learned(journal, since)

    assert learned["cumulative_resolved_count"] == MIN_RESOLVED_FOR_V2_AGGREGATE
    assert learned["aggregate_floor_met"] is True
    assert "once floors met" not in learned["headline"]
    assert "alphaos attribution" in learned["headline"]
    assert all("ΔR" not in s for s in learned["sentences"])


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
