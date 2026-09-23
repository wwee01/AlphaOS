"""No-network regressions for real detached helpers and safe stage receipts."""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from alphaos.research.last30days_enricher import Last30DaysEnricher
from alphaos.research import last30days_provider as l30p
from alphaos.research.last30days_provider import CliLast30DaysProvider, _read_telemetry
from alphaos.research.last30days_worker import STAGES, stop_owned_child
from conftest import make_settings


def provider_fixture(tmp_path, body, *, retrieval="return [], None", timeout=2):
    scripts = tmp_path / "scripts"
    lib = scripts / "lib"
    lib.mkdir(parents=True)
    (lib / "__init__.py").write_text("")
    (lib / "planner.py").write_text("def plan_query(**kwargs):\n    return {}\n")
    (lib / "pipeline.py").write_text(
        "import time\n"
        "def _retrieve_stream(**kwargs):\n    " + retrieval + "\n"
        "def _run_supplemental_searches(**kwargs): pass\n"
        "def _retry_thin_sources(**kwargs): pass\n"
        "def _finalize_items_by_source(**kwargs): pass\n"
        "def cluster_candidates(**kwargs): pass\n"
    )
    (lib / "rerank.py").write_text("def rerank_candidates(**kwargs): pass\ndef score_fun(**kwargs): pass\n")
    (lib / "github.py").write_text("def enrich_candidates_with_stars(**kwargs): pass\n")
    (scripts / "last30days.py").write_text(body)
    settings = make_settings(
        LAST30DAYS_ENABLED="true", LAST30DAYS_PROVIDER="cli", LAST30DAYS_PYTHON=sys.executable,
        LAST30DAYS_REPO_PATH=str(tmp_path), LAST30DAYS_SOURCES="reddit,hackernews,polymarket,github,x,youtube",
        # Deliberately synthetic upstream: exercise supervisor through the
        # existing custom-command contract without claiming pinned routing.
        LAST30DAYS_CMD=shlex.join([sys.executable, l30p._WORKER, str(scripts / "last30days.py")]),
    )
    settings = replace(settings, last30days_timeout_seconds=timeout)
    return CliLast30DaysProvider(settings), settings


def test_success_preserves_result_and_measures_actual_stages(tmp_path):
    body = '''
import json, sys
from lib import pipeline, planner
planner.plan_query(topic="TOP_SECRET_QUERY")
pipeline._retrieve_stream(source="reddit", query="TOP_SECRET_QUERY")
sys.stderr.write("https://private.example/?api_key=TOP_SECRET_KEY\\n")
print(json.dumps({"clusters":[{"title":"actual theme","score":5,"sources":["reddit"],"candidate_ids":["a"]}],
                  "errors_by_source":{"youtube":"TOP_SECRET_KEY"}}))
'''
    provider, _ = provider_fixture(tmp_path, body, retrieval="time.sleep(.03); return [{'title':'PRIVATE'}], None")
    result = provider.get_research_for_symbol("TEST", "TEST stock")
    assert result.clusters[0]["title"] == "actual theme"
    assert result.raw_meta["sources_with_errors"] == ["youtube"]
    diag = result.raw_meta["diagnostics"]
    assert diag["instrumented"] is True
    assert diag["outcome"] == "returned"
    assert diag["source_policy"]["status"] == "unverified"
    assert diag["elapsed_seconds"] >= .03
    hooks = next(e for e in diag["events"] if e["event"] == "hooks")
    assert hooks["missing"] == []
    assert set(hooks["installed"]) == STAGES
    retrieval = next(e for e in diag["events"] if e.get("stage") == "retrieval" and e["event"] == "stage_end")
    assert retrieval["source"] == "reddit"
    assert retrieval["elapsed_seconds"] >= .03 and retrieval["item_count"] == 1
    assert "PRIVATE" not in json.dumps(diag)
    assert "SECRET" not in json.dumps(diag)
    assert "private.example" not in json.dumps(diag)


@pytest.mark.parametrize("detach", ["start_new_session=True", "preexec_fn=os.setsid"])
def test_timeout_kills_verified_detached_helper_and_its_child(tmp_path, detach):
    marker = tmp_path / "escaped"
    grandchild = "import time,pathlib; time.sleep(.9); pathlib.Path(" + repr(str(marker)) + ").write_text('leak')"
    child = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(grandchild) + "]); time.sleep(10)"
    body = "import subprocess,sys,time,os\nsubprocess.Popen([sys.executable,'-c'," + repr(child) + "]," + detach + ")\ntime.sleep(10)\n"
    provider, _ = provider_fixture(tmp_path, body, timeout=.4)
    started = time.monotonic()
    with pytest.raises(TimeoutError) as caught:
        provider.get_research_for_symbol("TEST", "TEST stock")
    assert time.monotonic() - started < 2
    diag = caught.value.last30days_diagnostics
    cleanup = next(e for e in diag["events"] if e["event"] == "cleanup")
    assert cleanup["counts"]["detached_group_stopped"] == 1
    assert diag["unverified_descendant_roots"] == 0
    assert diag["outcome"] == "timeout"
    time.sleep(.7)
    assert not marker.exists(), "detached provider helper outlived its deadline"


def test_old_outer_group_cleanup_reproduces_detached_leak(tmp_path):
    """Same harmless scenario, old killpg behavior: descendant survives."""
    marker = tmp_path / "old-leak"
    ready = tmp_path / "ready"
    child = "import time,pathlib; time.sleep(.35); pathlib.Path(" + repr(str(marker)) + ").write_text('leak')"
    body = ("import subprocess,sys,time,pathlib; subprocess.Popen([sys.executable,'-c'," + repr(child)
            + "],start_new_session=True); pathlib.Path(" + repr(str(ready)) + ").touch(); time.sleep(10)")
    proc = subprocess.Popen([sys.executable, "-c", body], start_new_session=True)
    try:
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        os.killpg(proc.pid, 9)
        proc.wait(timeout=2)
        time.sleep(.5)
        assert marker.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_timeout_retains_incomplete_stage_instead_of_inventing_result(tmp_path):
    body = "from lib import pipeline\npipeline._retrieve_stream(source='youtube')\nprint('{}')\n"
    provider, _ = provider_fixture(tmp_path, body, retrieval="time.sleep(10); return [], None", timeout=.3)
    with pytest.raises(TimeoutError) as caught:
        provider.get_research_for_symbol("TEST", "TEST stock")
    stages = [e for e in caught.value.last30days_diagnostics["events"] if e.get("stage") == "retrieval"]
    assert len(stages) == 1 and stages[0]["event"] == "stage_start"
    assert stages[0]["source"] == "youtube"


def test_missing_hooks_report_limited_stage_coverage(tmp_path):
    provider, _ = provider_fixture(tmp_path, "print('{}')\n")
    (tmp_path / "scripts/lib/pipeline.py").write_text("")
    diag = provider.get_research_for_symbol("TEST", "TEST stock").raw_meta["diagnostics"]
    hooks = next(e for e in diag["events"] if e["event"] == "hooks")
    assert "retrieval" in hooks["missing"]
    assert not any(e.get("stage") == "retrieval" for e in diag["events"])
    assert diag["stage_coverage"] == "incomplete"


def test_hook_import_failure_is_explicit_and_does_not_leak_helpers(tmp_path):
    marker = tmp_path / "startup-leak"
    child = "import time,pathlib; time.sleep(.4); pathlib.Path(" + repr(str(marker)) + ").touch()"
    provider, _ = provider_fixture(tmp_path, "print('{}')\n")
    (tmp_path / "scripts/lib/planner.py").write_text(
        "import subprocess,sys\nsubprocess.Popen([sys.executable,'-c'," + repr(child)
        + "],start_new_session=True)\nraise ValueError('PRIVATE_PROVIDER_DETAILS')\n")
    with pytest.raises(RuntimeError) as caught:
        provider.get_research_for_symbol("TEST", "TEST stock")
    assert caught.value.last30days_diagnostics["stage_coverage"] == "unavailable"
    assert "PRIVATE_PROVIDER_DETAILS" not in str(caught.value)
    time.sleep(.5)
    assert not marker.exists()


def test_stage_error_has_timing_but_no_exception_payload(tmp_path):
    provider, _ = provider_fixture(tmp_path, "from lib import pipeline\npipeline._retrieve_stream(source='x')\n",
                                   retrieval="raise RuntimeError('SECRET_CREDENTIAL')")
    with pytest.raises(RuntimeError) as caught:
        provider.get_research_for_symbol("TEST", "TEST stock")
    diag = caught.value.last30days_diagnostics
    error = next(e for e in diag["events"] if e.get("outcome") == "error")
    assert error["source"] == "x" and error["elapsed_seconds"] >= 0
    assert "SECRET" not in json.dumps(diag)


def test_reaped_parent_is_not_proof_its_detached_descendants_are_gone(tmp_path):
    # A provider-spawned child that has already been waited for is no longer
    # an identity anchor. This test needs no real escaped process.
    body = "import subprocess,sys,time\np=subprocess.Popen([sys.executable,'-c','pass'],start_new_session=True)\np.wait()\ntime.sleep(10)\n"
    provider, _ = provider_fixture(tmp_path, body, timeout=.3)
    with pytest.raises(TimeoutError) as caught:
        provider.get_research_for_symbol("TEST", "TEST stock")
    assert caught.value.last30days_diagnostics["unverified_descendant_roots"] == 1
    assert "not guaranteed" in str(caught.value)


@pytest.mark.parametrize("state", ["reaped", "not_our_child", "exited", "no_lock"])
def test_cleanup_never_signals_unverified_or_reaped_pid(monkeypatch, state):
    child = SimpleNamespace(pid=45678, returncode=0 if state == "reaped" else None,
                            _waitpid_lock=threading.Lock(), _handle_exitstatus=lambda _: None)
    if state == "no_lock":
        del child._waitpid_lock
    def waitpid(*_):
        if state == "not_our_child":
            raise ChildProcessError()
        return (45678, 0) if state == "exited" else (0, 0)
    monkeypatch.setattr(os, "waitpid", waitpid)
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("signalled unverified PID"))
    monkeypatch.setattr(os, "killpg", lambda *_: pytest.fail("signalled unverified group"))
    assert stop_owned_child(child) in {"already_reaped", "already_exited", "identity_unverified"}


def test_cleanup_holds_wait_lock_through_identity_check_and_signal(monkeypatch):
    child = SimpleNamespace(pid=45678, returncode=None, _waitpid_lock=threading.Lock())
    monkeypatch.setattr(os, "waitpid", lambda *_: (0, 0))
    monkeypatch.setattr(os, "getpgid", lambda _: 45678)
    def killpg(pid, sig):
        assert child._waitpid_lock.locked()
        assert pid == 45678 and sig == 9
    monkeypatch.setattr(os, "killpg", killpg)
    assert stop_owned_child(child) == "detached_group_stopped"
    assert not child._waitpid_lock.locked()


def test_custom_command_deadline_is_preserved_and_coverage_is_not_claimed(tmp_path):
    provider, settings = provider_fixture(tmp_path, "print('{}')")
    provider.s = replace(settings, last30days_timeout_seconds=.1,
                         last30days_cmd=shlex.quote(sys.executable) + " -c 'import time; time.sleep(10)'")
    with pytest.raises(TimeoutError) as caught:
        provider.get_research_for_symbol("TEST", "TEST stock")
    diag = caught.value.last30days_diagnostics
    assert diag["instrumented"] is False and diag["events"] == []
    assert diag["unverified_descendant_roots"] is None
    assert diag["elapsed_seconds"] < 2
    assert "time.sleep" not in str(caught.value)


def test_unverified_cleanup_and_reap_timeout_never_claim_success_or_leak_command(monkeypatch, tmp_path):
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(["SECRET_COMMAND"], .1)
    proc = SimpleNamespace(stdout=io.StringIO(), stderr=io.StringIO(), communicate=timed_out, wait=timed_out)
    monkeypatch.setattr(l30p.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(l30p, "stop_owned_child", lambda _: "identity_unverified")
    provider = CliLast30DaysProvider(SimpleNamespace(last30days_timeout_seconds=.1))
    provider._build_cmd = lambda _: ["SECRET_COMMAND"]
    provider._repo_path = lambda: str(tmp_path)
    with pytest.raises(TimeoutError) as caught:
        provider.get_research_for_symbol("TEST", "TEST stock")
    assert "SECRET" not in str(caught.value)
    assert "process group stopped" not in str(caught.value)
    assert caught.value.last30days_diagnostics["parent_reap"] == "unverified"


def test_diagnostics_allowlist_excludes_stderr_queries_and_malformed_values():
    data = [{"event":"stage_end", "stage":"retrieval", "source":"youtube", "call_id":"1:2",
             "elapsed_seconds":.2, "outcome":"returned", "stderr":"SECRET", "query":"SECRET"},
            {"event":"stage_start", "stage":[], "source":{}, "call_id":"SECRET"},
            {"event":"hooks", "installed":["planner","SECRET"], "missing":None}]
    diag = _read_telemetry(io.BytesIO(b"broken\n" + b"\n".join(json.dumps(e).encode() for e in data)))
    assert diag["malformed"] is True
    assert "SECRET" not in json.dumps(diag)
    assert len(diag["events"]) == 3


def test_event_cap_is_reported_even_below_byte_limit():
    event = json.dumps({"event": "worker_start", "at_seconds": 0}).encode() + b"\n"
    diag = _read_telemetry(io.BytesIO(event * 512))
    assert diag["truncated"] is True


class Journal:
    def __init__(self):
        self.events = []

    def log_system_event(self, severity, category, message, detail):
        self.events.append(detail)


@pytest.mark.parametrize("failed_phase", ["started", "finished", "both"])
def test_telemetry_persistence_failure_preserves_decision_facing_context(tmp_path, failed_phase):
    provider, settings = provider_fixture(tmp_path, "print('{}')\n")
    class BrokenJournal(Journal):
        def log_system_event(self, severity, category, message, detail):
            if failed_phase == "both" or detail["phase"] == failed_phase:
                raise RuntimeError("PRIVATE_DATABASE_DETAIL")
            super().log_system_event(severity, category, message, detail)
    packet = SimpleNamespace(symbol="TEST")
    baseline = Last30DaysEnricher(settings, None, provider).enrich(packet)
    ctx = Last30DaysEnricher(settings, BrokenJournal(), provider).enrich(packet)
    assert ctx.to_packet_dict() == baseline.to_packet_dict()
    assert ctx.enrichment_status == baseline.enrichment_status == "ok"
    assert ctx.reason == "runtime_telemetry_incomplete"
    assert "last30days_telemetry_unavailable" in ctx.risk_tags
    assert ctx.label_review_required is False
    assert "PRIVATE_DATABASE_DETAIL" not in str(ctx)


@pytest.mark.parametrize("body,timeout,expected", [
    ("print('{}')\n", 2, "ok"),
    ("import time\ntime.sleep(10)\n", .2, "error"),
    ("print('[]')\n", 2, "error"),
    ("print('not-json')\n", 2, "error"),
])
def test_enricher_journals_every_attempt_success_or_failure_with_linkage(tmp_path, body, timeout, expected):
    provider, settings = provider_fixture(tmp_path, body, timeout=timeout)
    journal = Journal()
    packet = SimpleNamespace(symbol="TEST", candidate_id="cand_test", packet_id="pkt_test")
    ctx = Last30DaysEnricher(settings, journal, provider).enrich(packet)
    assert ctx.enrichment_status == expected
    start, finish = journal.events
    assert start["phase"] == "started" and finish["phase"] == "finished"
    assert start["attempt_id"] == finish["attempt_id"]
    assert finish["candidate_id"] == "cand_test" and finish["packet_id"] == "pkt_test"
    assert finish["enrichment_status"] == expected and finish["elapsed_seconds"] > 0
    assert finish["diagnostics"]["elapsed_seconds"] > 0
    assert finish["configured_sources"] == ["github", "hackernews", "polymarket", "reddit", "x", "youtube"]
    assert not ctx.label_review_required
    assert "diagnostics" not in ctx.to_packet_dict()
