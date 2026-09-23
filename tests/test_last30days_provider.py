"""last30days provider abstraction (Roadmap 2.5): factory gating, deterministic
mock, and the CLI provider's parse/failure behaviour — all HERMETIC (subprocess
is monkeypatched; no real process, no network)."""

from __future__ import annotations

import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

from alphaos.research import last30days_provider as l30p
from alphaos.research.last30days_provider import (
    CliLast30DaysProvider,
    MockLast30DaysProvider,
    build_query,
    make_last30days_provider,
)
from conftest import make_settings


def test_factory_none_when_master_switch_off():
    assert make_last30days_provider(make_settings(LAST30DAYS_ENABLED="false")) is None


def test_factory_none_when_provider_disabled():
    s = make_settings(LAST30DAYS_ENABLED="true", LAST30DAYS_PROVIDER="disabled")
    assert make_last30days_provider(s) is None


def test_factory_mock_when_enabled():
    s = make_settings(LAST30DAYS_ENABLED="true", LAST30DAYS_PROVIDER="mock")
    assert isinstance(make_last30days_provider(s), MockLast30DaysProvider)


def test_factory_cli_when_configured():
    # Non-mock settings: proves real CLI selection outside mock mode (the
    # mock-mode guard below is what changes behavior specifically in mock mode).
    s = make_settings(ALPHAOS_MODE="paper", LAST30DAYS_ENABLED="true", LAST30DAYS_PROVIDER="cli")
    assert isinstance(make_last30days_provider(s), CliLast30DaysProvider)


def test_factory_force_ignores_master_switch():
    s = make_settings(LAST30DAYS_ENABLED="false", LAST30DAYS_PROVIDER="mock")
    assert make_last30days_provider(s) is None                       # scan path: disabled
    assert isinstance(make_last30days_provider(s, force=True), MockLast30DaysProvider)  # probe path


def test_factory_falls_back_to_mock_when_mock_mode_even_if_cli_configured():
    """HANDOVER.md footgun #4: ALPHAOS_MODE=mock must not construct the live,
    subprocess-shelling CliLast30DaysProvider even if LAST30DAYS_PROVIDER=cli
    is left on in .env -- it should transparently degrade to the mock provider."""
    s = make_settings(ALPHAOS_MODE="mock", LAST30DAYS_ENABLED="true", LAST30DAYS_PROVIDER="cli")
    assert isinstance(make_last30days_provider(s), MockLast30DaysProvider)


def test_factory_force_still_allows_live_cli_from_mock_mode_settings():
    """An explicit manual probe (force=True) may still reach the real provider
    even from mock-mode settings -- this is a deliberate operator action, not
    an unattended/scheduled path."""
    s = make_settings(ALPHAOS_MODE="mock", LAST30DAYS_ENABLED="true", LAST30DAYS_PROVIDER="cli")
    assert isinstance(make_last30days_provider(s, force=True), CliLast30DaysProvider)


def test_mock_is_deterministic():
    p = MockLast30DaysProvider()
    a = p.get_research_for_symbol("AAPL", build_query("AAPL"))
    b = p.get_research_for_symbol("AAPL", build_query("AAPL"))
    assert a.clusters == b.clusters
    assert a.sentiment_hint == b.sentiment_hint
    assert a.provider == "mock"


def _cli():
    s = make_settings(LAST30DAYS_ENABLED="true", LAST30DAYS_PROVIDER="cli",
                      LAST30DAYS_REPO_PATH="/tmp/last30days-fake",
                      LAST30DAYS_CMD="fake-cli")  # parsing fixture, not verified upstream
    return CliLast30DaysProvider(s)


def _patch_process(monkeypatch, result):
    def start(*args, **kwargs):
        result.communicate = lambda timeout: (result.stdout, result.stderr)
        return result
    monkeypatch.setattr(l30p.subprocess, "Popen", start)


def test_cli_parses_canned_json(monkeypatch):
    canned = {
        "clusters": [
            {"title": "NVDA earnings thread", "score": 42.0,
             "sources": ["reddit", "hackernews"], "candidate_ids": ["a", "b"]},
            {"title": "NVDA chip chatter", "score": 18.0, "sources": ["reddit"], "candidate_ids": ["c"]},
        ],
        "artifacts": {"plan_source": "deterministic"},
    }

    class _R:
        returncode = 0
        stdout = json.dumps(canned)
        stderr = "[safari] cookie warning (ignored)"

    _patch_process(monkeypatch, _R())
    res = _cli().get_research_for_symbol("NVDA", "NVDA stock")
    assert res.item_count == 3                                # 2 + 1 candidate_ids
    assert res.clusters[0]["title"] == "NVDA earnings thread"
    assert res.sources_used == ["hackernews", "reddit"]
    assert res.provider == "cli"
    assert res.raw_meta.get("plan_source") == "deterministic"


def test_cli_raises_on_nonzero_exit(monkeypatch):
    class _R:
        returncode = 1
        stdout = ""
        stderr = "boom"

    _patch_process(monkeypatch, _R())
    with pytest.raises(RuntimeError):
        _cli().get_research_for_symbol("NVDA", "NVDA stock")


def test_cli_raises_on_bad_json(monkeypatch):
    class _R:
        returncode = 0
        stdout = "not-json"
        stderr = ""

    _patch_process(monkeypatch, _R())
    with pytest.raises(json.JSONDecodeError):
        _cli().get_research_for_symbol("NVDA", "NVDA stock")


def test_cli_timeout_stops_descendant_process(tmp_path):
    """A real harmless child would outlive subprocess.run's parent-only kill."""
    marker = tmp_path / "child-completed"
    child = "import time,pathlib; time.sleep(.7); pathlib.Path(" + repr(str(marker)) + ").write_text('leaked')"
    parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(10)"
    provider = CliLast30DaysProvider(SimpleNamespace(last30days_timeout_seconds=.2))
    provider._build_cmd = lambda query: [sys.executable, "-c", parent]
    provider._repo_path = lambda: str(tmp_path)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="process group stopped"):
        provider.get_research_for_symbol("TEST", "TEST stock")
    assert time.monotonic() - started < 3
    time.sleep(.8)
    assert not marker.exists(), "timed-out CLI left a child process running"


def test_cli_builds_python312_command(monkeypatch):
    """The command must use the configured (3.12) interpreter, --emit json, the
    keyless source list, and never shell out via a string."""
    captured = {}

    class _R:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def _capture(cmd, *a, **k):
        captured["cmd"] = cmd
        result = _R()
        result.communicate = lambda timeout: (result.stdout, result.stderr)
        receipt = {"event": "source_policy", "version": l30p.POLICY_VERSION,
                   "status": "completed", "violations": 0, "completed_runs": 1,
                   "requested": sorted(s.last30days_sources.split(",")), "dispatch_counts": {}}
        os.write(k["pass_fds"][0], (json.dumps(receipt) + "\n").encode())
        captured["kwargs"] = k
        return result

    s = make_settings(LAST30DAYS_ENABLED="true", LAST30DAYS_PROVIDER="cli",
                      LAST30DAYS_REPO_PATH="/tmp/last30days-fake",
                      LAST30DAYS_PYTHON="/opt/py/python3.12",
                      LAST30DAYS_SOURCES="reddit,hackernews,polymarket,github")
    monkeypatch.setattr(l30p.subprocess, "Popen", _capture)
    CliLast30DaysProvider(s).get_research_for_symbol("NVDA", "NVDA stock")
    cmd = captured["cmd"]
    assert isinstance(cmd, list)                              # no shell=True string
    assert cmd[0] == "/opt/py/python3.12"
    assert "--emit" in cmd and cmd[cmd.index("--emit") + 1] == "json"
    assert "reddit,hackernews,polymarket,github" in cmd
    assert "--quick" in cmd
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["env"][l30p.POLICY_ENV] == l30p.POLICY_VERSION
