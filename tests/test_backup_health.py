"""OPS-B: alphaos/reports/backup_health.py -- pure read of the JSON status
file deploy/backup_ledger.sh writes. No subprocess/bash involved (see
tests/test_backup_ledger.py for the end-to-end bash-script tests); this
file covers the Python-side read/render logic plus settings validation and
daily-brief wiring.
"""

from __future__ import annotations

import json

import pytest

from alphaos.reports.backup_health import build_backup_health, render_markdown
from conftest import make_settings


def test_build_backup_health_none_when_status_file_missing(tmp_path):
    assert build_backup_health(str(tmp_path / "does_not_exist.json")) is None


def test_build_backup_health_none_on_corrupt_json(tmp_path):
    """A torn/corrupt status file must never crash the daily brief -- treated
    the same as 'never run', never raised."""
    status_file = tmp_path / "backup_status.json"
    status_file.write_text("{not valid json")

    assert build_backup_health(str(status_file)) is None


def test_build_backup_health_none_when_json_is_not_an_object(tmp_path):
    """audit LOW (correctness, 2026-07-10): syntactically valid JSON that
    isn't a dict (a bare list/number/string) must not flow through as
    "truthy, not a dict" and crash render_markdown's own .get() calls."""
    status_file = tmp_path / "backup_status.json"

    for non_dict_json in ("[1, 2, 3]", "42", '"just a string"', "null", "true"):
        status_file.write_text(non_dict_json)
        assert build_backup_health(str(status_file)) is None


def test_build_backup_health_reads_a_real_status_file(tmp_path):
    status_file = tmp_path / "backup_status.json"
    status_file.write_text(json.dumps({
        "nightly_backup_ok_at_utc": "2026-07-10T00:00:00Z",
        "nightly_backup_date": "2026-07-10",
        "env_enc_armed": True,
        "offsite_configured": True,
        "offsite_last_ok_month": "2026-07",
    }))

    health = build_backup_health(str(status_file))

    assert health["env_enc_armed"] is True
    assert health["offsite_configured"] is True


def test_render_markdown_no_runs_yet():
    md = render_markdown(None)
    assert "No backup run recorded yet" in md


def test_render_markdown_warns_when_env_enc_not_armed():
    health = {
        "nightly_backup_ok_at_utc": "2026-07-10T00:00:00Z", "nightly_backup_date": "2026-07-10",
        "env_enc_armed": False, "offsite_configured": False, "offsite_last_ok_month": None,
    }
    md = render_markdown(health)
    assert "NOT ARMED" in md
    assert "NOT CONFIGURED" in md


def test_render_markdown_shows_armed_and_configured_state():
    health = {
        "nightly_backup_ok_at_utc": "2026-07-10T00:00:00Z", "nightly_backup_date": "2026-07-10",
        "env_enc_armed": True, "offsite_configured": True, "offsite_last_ok_month": "2026-07",
    }
    md = render_markdown(health)
    assert "armed" in md
    assert "configured, last OK 2026-07" in md
    assert "NOT ARMED" not in md
    assert "NOT CONFIGURED" not in md


# ------------------------------------------------------------ daily brief
def test_backup_health_none_reflected_in_daily_brief(orchestrator, monkeypatch):
    from alphaos.reports import daily_brief

    monkeypatch.setattr(daily_brief, "_backup_health", lambda: None)
    brief = daily_brief.build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)

    assert brief["backup_health"] is None
    md = daily_brief.render_markdown(brief)
    assert "## Backups" not in md  # omitted entirely when never run, same pattern as eval/atr health


def test_backup_health_present_renders_in_daily_brief(orchestrator, monkeypatch):
    from alphaos.reports import daily_brief

    fake_health = {
        "nightly_backup_ok_at_utc": "2026-07-10T00:00:00Z", "nightly_backup_date": "2026-07-10",
        "env_enc_armed": True, "offsite_configured": True, "offsite_last_ok_month": "2026-07",
    }
    monkeypatch.setattr(daily_brief, "_backup_health", lambda: fake_health)
    brief = daily_brief.build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)

    assert brief["backup_health"] == fake_health
    md = daily_brief.render_markdown(brief)
    assert "## Backups" in md


# ------------------------------------------------------------------ settings
def test_backup2_method_defaults_empty(settings):
    assert settings.backup2_method == ""
    assert settings.backup2_dest == ""


def test_backup2_method_accepts_rclone_and_disk():
    assert make_settings(BACKUP2_METHOD="rclone").backup2_method == "rclone"
    assert make_settings(BACKUP2_METHOD="disk").backup2_method == "disk"


def test_backup2_method_rejects_invalid_value():
    with pytest.raises(Exception):
        make_settings(BACKUP2_METHOD="dropbox")


# ------------------------------------------------- staleness (2026-07-17 audit)
# The status file is written ONLY on success, so "OK" in the file proves
# nothing about any night since -- backups failed silently Jul 12-16 while
# every renderer showed "Nightly: OK 2026-07-11". Fixed clock via the `now`
# param, never the wall clock (repo false-green discipline).

def _utc(y, m, d, hh=12):
    from datetime import datetime, timezone
    return datetime(y, m, d, hh, 0, 0, tzinfo=timezone.utc)


def _status_file(tmp_path, date_str):
    p = tmp_path / "backup_status.json"
    p.write_text(json.dumps({
        "nightly_backup_ok_at_utc": f"{date_str}T21:30:00Z", "nightly_backup_date": date_str,
        "env_enc_armed": True, "offsite_configured": True, "offsite_last_ok_month": "2026-07",
    }))
    return str(p)


def test_backup_health_fresh_run_is_not_stale(tmp_path):
    health = build_backup_health(_status_file(tmp_path, "2026-07-16"), now=_utc(2026, 7, 17))
    assert health["stale"] is False
    assert health["days_since_success"] == 1
    assert "Nightly: OK" in render_markdown(health)
    assert "STALE" not in render_markdown(health)


def test_backup_health_old_success_reads_stale_not_ok(tmp_path):
    """A 5-day-old success file is the Jul-12-16 outage, not a healthy state."""
    health = build_backup_health(_status_file(tmp_path, "2026-07-11"), now=_utc(2026, 7, 16))
    assert health["stale"] is True
    assert health["days_since_success"] == 5
    md = render_markdown(health)
    assert "STALE" in md
    assert "backup-error.log" in md
    assert "Nightly: OK" not in md


def test_backup_health_unparseable_date_reads_stale_never_green(tmp_path):
    p = tmp_path / "backup_status.json"
    p.write_text(json.dumps({"nightly_backup_date": "not-a-date"}))
    health = build_backup_health(str(p), now=_utc(2026, 7, 16))
    assert health["stale"] is True
    assert health["days_since_success"] is None


def test_stale_backup_reaches_the_one_action_headline():
    """The pushed ntfy title must surface a backup outage -- during Jul 12-16
    it said 'Nothing needs you right now' every day."""
    from alphaos.reports.daily_brief import _one_action

    quiet_needs_you = {
        "open_incident_count": 0, "fused_jobs": [], "pending_approvals": [],
        "hypothesis_resolution": None,
    }
    stale = {"stale": True, "days_since_success": 5}
    action = _one_action(quiet_needs_you, [], {"status": "ok"}, stale)
    assert "backup" in action.lower()
    assert "5 day(s)" in action

    fresh = {"stale": False, "days_since_success": 0}
    action = _one_action(quiet_needs_you, [], {"status": "ok"}, fresh)
    assert "backup" not in action.lower()
