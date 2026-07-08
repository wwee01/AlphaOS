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
