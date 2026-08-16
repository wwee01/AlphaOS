"""GROUP-B / PRE-1b: the once-daily preflight self-test.

Covers:
* JobType.PREFLIGHT is in cadence.default_lock_key's once-daily tuple (the
  2026-07-09 TEXT-0 lesson -- omitting it causes re-dispatch every tick).
* cadence.is_due("preflight", ...) is genuinely once-per-day.
* run_preflight_job is registered in job_runner's dispatch table.
* each of the 7 checks, individually, mocked (NO real API/network calls
  anywhere in this file).
* any fail -> exactly one alert, priority=high, naming every failed check.
* all pass -> no alert.
* reports/daily_brief.py::_preflight_health reads the job_runs row back.
"""

from __future__ import annotations

import json

from alphaos.scheduler import cadence, job_runner, jobs
from alphaos.scheduler.job_runner import JobRunner
from alphaos.reports.daily_brief import _preflight_health
from conftest import make_settings


# --------------------------------------------------------- cadence wiring
def test_preflight_is_in_the_once_daily_lock_key_tuple(settings):
    """TEXT-0 lesson: default_lock_key must return the SAME (date-keyed) key
    across two different instants on the same SGT day -- proves PREFLIGHT
    fell into the once-daily branch, not the generic per-instant fallback."""
    from datetime import datetime, timedelta, timezone

    now1 = datetime(2026, 8, 13, 1, 0, 0, tzinfo=timezone.utc)
    now2 = now1 + timedelta(hours=6)
    key1 = cadence.default_lock_key(cadence.JobType.PREFLIGHT, settings, now1)
    key2 = cadence.default_lock_key(cadence.JobType.PREFLIGHT, settings, now2)
    assert key1 == key2
    assert key1.startswith("preflight:")


def test_preflight_registered_in_job_dispatch_table():
    assert job_runner._JOB_FUNCS[cadence.JobType.PREFLIGHT] is jobs.run_preflight_job


def test_preflight_is_due_before_configured_time_and_not_after(settings, journal):
    from datetime import datetime, timezone

    before = datetime(2026, 8, 13, 23, 0, 0, tzinfo=timezone.utc)  # ~07:00 SGT
    due, reason = cadence.is_due(cadence.JobType.PREFLIGHT, settings, journal, before)
    assert due is False
    assert "before" in reason

    after = datetime(2026, 8, 13, 23, 30, 0, tzinfo=timezone.utc)  # ~07:30 SGT, past 07:15 default
    due2, reason2 = cadence.is_due(cadence.JobType.PREFLIGHT, settings, journal, after)
    assert due2 is True


def test_preflight_does_not_re_dispatch_after_completing_once_today(orchestrator):
    """Direct reproduction of the TEXT-0 failure shape: run it once via the
    real JobRunner, then assert cadence says NOT due again the same day."""
    from datetime import datetime, timezone

    now = datetime(2026, 8, 13, 23, 30, 0, tzinfo=timezone.utc)
    runner = JobRunner(orchestrator)
    lock_key = cadence.default_lock_key(cadence.JobType.PREFLIGHT, orchestrator.settings, now)
    result = runner.run_job(cadence.JobType.PREFLIGHT, lock_key=lock_key)
    assert result["status"] == "completed"

    due, reason = cadence.is_due(cadence.JobType.PREFLIGHT, orchestrator.settings, orchestrator.journal, now)
    assert due is False
    assert "already completed today" in reason


# ------------------------------------------------------------- individual checks
def test_check_openai_reachable_mock_mode_is_ok_not_applicable(orchestrator):
    result = jobs._preflight_check_openai_reachable(orchestrator)
    assert result["ok"] is True
    assert "not applicable" in result["detail"]


class _FakeOrch:
    """Lightweight orch-shaped fake -- avoids fighting Settings' frozen-
    dataclass ``is_mock``/``has_openai_key`` properties just to force the
    live (not-mock, has-key) branch for these OpenAI-specific checks. Real
    Settings/journal are used everywhere else in this file; this is scoped
    to the two tests that need a live-branch entry with zero real calls."""

    def __init__(self, journal, **settings_overrides):
        import types

        self.journal = journal
        defaults = dict(
            is_mock=False, has_openai_key=True, openai_api_key="sk-fake-test-key",
            openai_primary_model="gpt-test", scheduler_ai_cost_cap_calls_per_30d=999,
            mode="paper",
        )
        defaults.update(settings_overrides)
        self.settings = types.SimpleNamespace(**defaults)


def test_check_openai_reachable_respects_the_cost_cap_without_a_real_call(journal, monkeypatch):
    """Live branch entered (not mock, has a key) but over the cost cap --
    must skip WITHOUT ever importing openai or attempting a network call."""
    monkeypatch.setattr(jobs.cost_guard, "check_scan_budget", lambda *a, **k: (False, "999/999 used -- cap reached"))
    orch = _FakeOrch(journal)

    result = jobs._preflight_check_openai_reachable(orch)

    assert result["ok"] is True
    assert "cost cap" in result["detail"]


def test_check_openai_reachable_failure_surfaces_as_a_named_fail(journal, monkeypatch):
    """A mocked OpenAI client that raises -- proves the failure path, with
    ZERO real network access (the openai.OpenAI class itself is replaced in
    sys.modules for the duration of this test only)."""
    import types

    monkeypatch.setattr(jobs.cost_guard, "check_scan_budget", lambda *a, **k: (True, "0/999 used"))

    class _FakeCompletions:
        def create(self, **kwargs):
            raise RuntimeError("simulated 429 insufficient_quota")

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeOpenAI:
        def __init__(self, api_key):
            self.chat = _FakeChat()

    fake_openai_module = types.SimpleNamespace(OpenAI=_FakeOpenAI)
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_openai_module)
    orch = _FakeOrch(journal)

    result = jobs._preflight_check_openai_reachable(orch)

    assert result["ok"] is False
    assert "unreachable" in result["detail"]


def test_check_openai_reachable_uses_max_completion_tokens_not_max_tokens(journal, monkeypatch):
    """Audit-fixup BLOCKER-1: gpt-5.x chat.completions REJECTS `max_tokens`
    (400) -- this repo has already learned this twice
    (ai/playbook_classifier.py, ai/last30days_polarity.py both use
    `max_completion_tokens`). As originally written this check would 400 on
    every healthy day and page at priority=high daily. Captures the ACTUAL
    kwargs passed to the fake client's .create() call -- proves the
    parameter name directly, not just that SOME call happened."""
    monkeypatch.setattr(jobs.cost_guard, "check_scan_budget", lambda *a, **k: (True, "0/999 used"))
    captured_kwargs = {}

    class _FakeCompletions:
        def create(self, **kwargs):
            captured_kwargs.update(kwargs)

            class _Resp:
                model = "gpt-5.6-luna"

            return _Resp()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeOpenAI:
        def __init__(self, api_key):
            self.chat = _FakeChat()

    import types

    fake_openai_module = types.SimpleNamespace(OpenAI=_FakeOpenAI)
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_openai_module)
    orch = _FakeOrch(journal, openai_primary_model="gpt-5.6-luna")

    result = jobs._preflight_check_openai_reachable(orch)

    assert result["ok"] is True
    assert "max_completion_tokens" in captured_kwargs
    assert captured_kwargs["max_completion_tokens"] == 5
    assert "max_tokens" not in captured_kwargs


def test_check_openai_reachable_paper_mode_blank_key_is_a_real_failure(journal):
    """Audit-fixup HIGH-2 (the ticket's own thesis): ALPHAOS_MODE=paper with
    a blank OPENAI_API_KEY (rotation, bad deploy, a bad .env edit) is NOT
    the same as a genuine mock deployment -- OpenAIClient silently degrades
    to mock output while live paper trading continues. The old code
    returned ok=True ('not applicable') for this exact state, which is
    precisely the fail-open this ticket exists to eliminate."""
    import types

    orch = types.SimpleNamespace(
        journal=journal,
        settings=types.SimpleNamespace(
            is_mock=False, has_openai_key=False, openai_api_key="",
            openai_primary_model="gpt-5.6-luna", scheduler_ai_cost_cap_calls_per_30d=999,
            mode="paper",
        ),
    )

    result = jobs._preflight_check_openai_reachable(orch)

    assert result["ok"] is False
    assert "paper" in result["detail"]
    assert "OPENAI_API_KEY" in result["detail"]


def test_check_alpaca_reachable_not_applicable_for_simulated_internal(orchestrator):
    result = jobs._preflight_check_alpaca_reachable(orchestrator)
    assert result["ok"] is True
    assert "not applicable" in result["detail"]


def test_check_market_data_freshness_mock_mode_is_usable(orchestrator):
    result = jobs._preflight_check_market_data_freshness(orchestrator)
    assert result["ok"] is True
    assert "SPY" in result["detail"]


def test_check_market_data_freshness_closed_session_is_ok_not_a_failure(orchestrator, monkeypatch):
    """Audit-fixup HIGH-1: SCHEDULER_PREFLIGHT_TIME (07:15 SGT) is 19:15 ET
    the PREVIOUS day -- on SGT Sunday/Monday that previous-day ET instant
    lands on a real weekend, so a genuine Alpaca snapshot stamps
    market_session='closed'. The mock market-data provider always hardcodes
    market_session='regular' (never exercising this path), so this test
    drives an ALPACA-SHAPED snapshot directly (a plain dict carrying
    market_session='closed', exactly as the real provider would stamp it)
    through the real MarketDataClient.get_snapshot -- proving the fix
    against the actual FreshnessGuard.assess() precedence
    (snapshot['market_session'] wins over any wall-clock guess), not a
    reimplementation of the guard's own logic."""
    from alphaos.data.market_data import MarketDataClient

    closed_snapshot = {
        "provider": "alpaca", "feed": "iex", "market_session": "closed",
        "quote_timestamp": None, "bar_timestamp": None, "last_price": 450.0,
    }
    monkeypatch.setattr(MarketDataClient, "get_snapshot", lambda self, symbol: closed_snapshot)

    result = jobs._preflight_check_market_data_freshness(orchestrator)

    assert result["ok"] is True
    assert "market closed" in result["detail"]


def test_check_market_data_freshness_stale_during_market_hours_still_fails(orchestrator, monkeypatch):
    """The closed-session carve-out must not swallow a REAL staleness
    failure during actual market hours."""
    from alphaos.data.market_data import MarketDataClient

    stale_snapshot = {
        "provider": "alpaca", "feed": "iex", "market_session": "regular",
        "quote_timestamp": "2000-01-01T00:00:00+00:00", "bar_timestamp": "2000-01-01T00:00:00+00:00",
        "last_price": 450.0,
    }
    monkeypatch.setattr(MarketDataClient, "get_snapshot", lambda self, symbol: stale_snapshot)

    result = jobs._preflight_check_market_data_freshness(orchestrator)

    assert result["ok"] is False


def test_check_canary_staleness_not_enabled_is_ok(orchestrator):
    result = jobs._preflight_check_canary_staleness(orchestrator)
    assert result["ok"] is True
    assert "not applicable" in result["detail"]


def test_check_canary_staleness_fails_past_threshold(orchestrator, monkeypatch):
    orchestrator.settings = make_settings(CANARY_ENABLED="true")
    stale_report = {"status": "ok", "run_id": "canaryrun_old", "started_at_sgt": "2000-01-01T00:00:00+08:00"}
    monkeypatch.setattr(
        "alphaos.reports.canary_report.build_canary_report", lambda journal, run_id=None: stale_report,
    )
    result = jobs._preflight_check_canary_staleness(orchestrator)
    assert result["ok"] is False
    assert "canaryrun_old" in result["detail"]


def test_check_backup_age_no_run_yet_is_ok(orchestrator, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no data/backup_status.json here
    result = jobs._preflight_check_backup_age(orchestrator)
    assert result["ok"] is True
    assert "no backup run" in result["detail"]


def test_check_backup_age_stale_fails(orchestrator, monkeypatch):
    monkeypatch.setattr(
        "alphaos.reports.backup_health.build_backup_health",
        lambda: {"stale": True, "days_since_success": 9},
    )
    result = jobs._preflight_check_backup_age(orchestrator)
    assert result["ok"] is False
    assert "9 day" in result["detail"]


def test_check_journal_and_disk_ok_on_a_healthy_journal(orchestrator):
    result = jobs._preflight_check_journal_and_disk(orchestrator)
    assert result["ok"] is True
    assert "MB free" in result["detail"]


def test_check_journal_and_disk_fails_when_low_headroom(orchestrator, monkeypatch):
    import collections

    fake_usage = collections.namedtuple("Usage", "total used free")(100, 99, 1024 * 1024)  # 1MB free
    monkeypatch.setattr(jobs.shutil, "disk_usage", lambda path: fake_usage)
    result = jobs._preflight_check_journal_and_disk(orchestrator)
    assert result["ok"] is False
    assert "low disk headroom" in result["detail"]


def test_check_kill_switch_state_always_ok_reported_not_judged(orchestrator):
    result = jobs._preflight_check_kill_switch_state(orchestrator)
    assert result["ok"] is True
    assert "kill_switch_engaged=" in result["detail"]
    assert "shadow_label_suspended=" in result["detail"]


# --------------------------------------------------------------- full job
def test_run_preflight_job_all_pass_sends_no_alert(orchestrator, monkeypatch):
    alerts_sent = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: alerts_sent.append(k))

    result = jobs.run_preflight_job(orchestrator, runner=None)

    assert result["status"] == "completed"
    assert result["preflight_result"]["ok"] is True
    assert len(result["preflight_result"]["checks"]) == 7
    assert alerts_sent == []


def test_run_preflight_job_one_failure_sends_exactly_one_high_priority_alert_naming_it(orchestrator, monkeypatch):
    alerts_sent = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: alerts_sent.append(k))
    monkeypatch.setattr(
        jobs, "_preflight_check_backup_age",
        lambda orch: {"ok": False, "detail": "backup stale: 9 day(s) since last success"},
    )

    result = jobs.run_preflight_job(orchestrator, runner=None)

    assert result["preflight_result"]["ok"] is False
    assert result["preflight_result"]["checks"]["backup_age"]["ok"] is False
    assert len(alerts_sent) == 1
    alert = alerts_sent[0]
    assert alert["priority"] == "high"
    assert "backup_age" in alert["title"]
    assert "backup stale" in alert["message"]


def test_run_preflight_job_a_check_that_raises_is_recorded_not_propagated(orchestrator, monkeypatch):
    """One check's own bug must never sink the whole job (or the other 6
    checks) -- individually attributable failure, per the spec's own
    'not a single undifferentiated string' requirement."""
    monkeypatch.setattr(
        jobs, "_preflight_check_alpaca_reachable",
        lambda orch: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: None)

    result = jobs.run_preflight_job(orchestrator, runner=None)

    assert result["status"] == "completed"  # the JOB completed; one check failed
    assert result["preflight_result"]["checks"]["alpaca_reachable"]["ok"] is False
    assert "boom" in result["preflight_result"]["checks"]["alpaca_reachable"]["detail"]
    # every other check still ran and is individually attributable
    assert result["preflight_result"]["checks"]["backup_age"]["ok"] is True


# ------------------------------------------------------ daily brief surfacing
def test_preflight_health_always_a_dict_never_run_yet(orchestrator):
    """Audit-fixup MEDIUM-3: ALWAYS a dict now (was None) -- a preflight job
    that STOPS running must not silently delete its own digest line."""
    health = _preflight_health(orchestrator.journal)
    assert health == {"ran": False, "ok": None, "checks": {}, "as_of_sgt": None}


def test_preflight_health_reads_back_the_latest_completed_run(orchestrator):
    from alphaos.util import timeutils
    from alphaos.util.ids import new_id

    st = timeutils.stamp()
    orchestrator.journal.insert("job_runs", {
        "job_run_id": new_id("jobrun"), "job_type": "preflight", "trigger_source": "scheduler",
        "lock_key": "preflight:2026-08-13", "started_at_utc": st.utc, "started_at_sgt": st.local_sgt,
        "status": "completed", "finished_at_utc": st.utc, "finished_at_sgt": st.local_sgt,
        "result_summary_json": json.dumps({
            "status": "completed",
            "preflight_result": {"ok": False, "checks": {"openai_reachable": {"ok": False, "detail": "down"}}},
        }),
    })

    health = _preflight_health(orchestrator.journal)
    assert health["ran"] is True
    assert health["ok"] is False
    assert health["checks"]["openai_reachable"]["ok"] is False


def test_render_markdown_preflight_section_always_present_and_attributable(orchestrator):
    """Audit-fixup MEDIUM-3: the digest line is ALWAYS present (never-run
    state renders honestly instead of vanishing), and when a run exists,
    every check's own detail string reaches the render -- never a single
    undifferentiated 'preflight failed'."""
    from alphaos.reports.daily_brief import build_daily_brief, render_markdown

    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    md = render_markdown(brief)
    assert "## Preflight: no run has completed yet" in md

    from alphaos.util import timeutils
    from alphaos.util.ids import new_id

    st = timeutils.stamp()
    orchestrator.journal.insert("job_runs", {
        "job_run_id": new_id("jobrun"), "job_type": "preflight", "trigger_source": "scheduler",
        "lock_key": "preflight:2026-08-13", "started_at_utc": st.utc, "started_at_sgt": st.local_sgt,
        "status": "completed", "finished_at_utc": st.utc, "finished_at_sgt": st.local_sgt,
        "result_summary_json": json.dumps({
            "status": "completed",
            "preflight_result": {
                "ok": False,
                "checks": {
                    "openai_reachable": {"ok": False, "detail": "OpenAI unreachable: 429 insufficient_quota"},
                    "alpaca_reachable": {"ok": True, "detail": "not applicable"},
                },
            },
        }),
    })
    brief2 = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    md2 = render_markdown(brief2)
    assert "## Preflight" in md2 and "1 FAILED" in md2
    assert "openai_reachable: FAIL — OpenAI unreachable: 429 insufficient_quota" in md2
    assert "alpaca_reachable: ok — not applicable" in md2
