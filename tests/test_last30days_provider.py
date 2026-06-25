"""last30days provider abstraction (Roadmap 2.5): factory gating, deterministic
mock, and the CLI provider's parse/failure behaviour — all HERMETIC (subprocess
is monkeypatched; no real process, no network)."""

from __future__ import annotations

import json
import subprocess

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
    s = make_settings(LAST30DAYS_ENABLED="true", LAST30DAYS_PROVIDER="cli")
    assert isinstance(make_last30days_provider(s), CliLast30DaysProvider)


def test_factory_force_ignores_master_switch():
    s = make_settings(LAST30DAYS_ENABLED="false", LAST30DAYS_PROVIDER="mock")
    assert make_last30days_provider(s) is None                       # scan path: disabled
    assert isinstance(make_last30days_provider(s, force=True), MockLast30DaysProvider)  # probe path


def test_mock_is_deterministic():
    p = MockLast30DaysProvider()
    a = p.get_research_for_symbol("AAPL", build_query("AAPL"))
    b = p.get_research_for_symbol("AAPL", build_query("AAPL"))
    assert a.clusters == b.clusters
    assert a.sentiment_hint == b.sentiment_hint
    assert a.provider == "mock"


def _cli():
    s = make_settings(LAST30DAYS_ENABLED="true", LAST30DAYS_PROVIDER="cli",
                      LAST30DAYS_REPO_PATH="/tmp/last30days-fake")  # avoids auto-resolve glob
    return CliLast30DaysProvider(s)


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

    monkeypatch.setattr(l30p.subprocess, "run", lambda *a, **k: _R())
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

    monkeypatch.setattr(l30p.subprocess, "run", lambda *a, **k: _R())
    with pytest.raises(RuntimeError):
        _cli().get_research_for_symbol("NVDA", "NVDA stock")


def test_cli_raises_on_bad_json(monkeypatch):
    class _R:
        returncode = 0
        stdout = "not-json"
        stderr = ""

    monkeypatch.setattr(l30p.subprocess, "run", lambda *a, **k: _R())
    with pytest.raises(json.JSONDecodeError):
        _cli().get_research_for_symbol("NVDA", "NVDA stock")


def test_cli_raises_on_timeout(monkeypatch):
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="last30days", timeout=1)

    monkeypatch.setattr(l30p.subprocess, "run", _boom)
    with pytest.raises(subprocess.TimeoutExpired):
        _cli().get_research_for_symbol("NVDA", "NVDA stock")


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
        return _R()

    s = make_settings(LAST30DAYS_ENABLED="true", LAST30DAYS_PROVIDER="cli",
                      LAST30DAYS_REPO_PATH="/tmp/last30days-fake",
                      LAST30DAYS_PYTHON="/opt/py/python3.12",
                      LAST30DAYS_SOURCES="reddit,hackernews,polymarket,github")
    monkeypatch.setattr(l30p.subprocess, "run", _capture)
    CliLast30DaysProvider(s).get_research_for_symbol("NVDA", "NVDA stock")
    cmd = captured["cmd"]
    assert isinstance(cmd, list)                              # no shell=True string
    assert cmd[0] == "/opt/py/python3.12"
    assert "--emit" in cmd and cmd[cmd.index("--emit") + 1] == "json"
    assert "reddit,hackernews,polymarket,github" in cmd
    assert "--quick" in cmd
