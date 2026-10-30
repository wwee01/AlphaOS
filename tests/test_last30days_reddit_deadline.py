"""Offline deadline arithmetic and real worker failure/partial boundaries."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import shutil
import urllib.error

import pytest

from alphaos.research import last30days_reddit_deadline as deadline
from alphaos.research import last30days_provider as provider


def pin_reddit_fixture(worker_dir, scripts, monkeypatch):
    """Explicitly pinned synthetic upstream; never weakens production pins."""
    modules = {}
    for module, name, _, parameters in deadline.HOOKS:
        modules.setdefault(module, []).append(
            "def " + name + "(" + ",".join(sorted(parameters)) + "): return []\n")
    for filename in deadline.SUPPORTED_FILES:
        path = scripts / filename
        if not path.exists():
            imports = "import time, urllib.request, urllib.error, concurrent.futures\n"
            path.write_text(imports + "".join(modules.get(path.stem, [])))
    # Independent 3.8.1 signature fixture: this transport takes post_url, not url.
    # Deriving it from HOOKS hid the initial incompatible installed hook.
    (scripts / "lib/reddit_shreddit.py").write_text(
        "def fetch_comments(post_url: str, timeout: int = 12): return {}\n")
    hashes = {name: hashlib.sha256((scripts / name).read_bytes()).hexdigest()
              for name in deadline.SUPPORTED_FILES}
    target = worker_dir / "last30days_reddit_deadline.py"
    shutil.copyfile(Path(deadline.__file__), target)
    with target.open("a") as stream:
        stream.write("\n# Test-only synthetic provider pins.\nSUPPORTED_FILES=" + repr(hashes) + "\n")
    monkeypatch.setattr(provider, "REDDIT_FILES", hashes)
    return hashes


def guard_fixture(now=10., seconds=45.):
    clock = [now]
    events, stops = [], []
    guard = deadline.RedditDeadline(0., seconds, lambda kind, **row: events.append({"event": kind, **row}),
                                   lambda: stops.append(True), clock=lambda: clock[0])
    return guard, clock, events, stops


def test_infeasible_retry_fails_before_sleep_and_cannot_be_swallowed():
    guard, clock, events, stops = guard_fixture()
    sleeps = []
    def upstream():
        try:
            guard.sleep(sleeps.append, 120.)
        except Exception:
            return []
    with pytest.raises(deadline.RedditDeadlineExceeded):
        guard.wrap(upstream, "tier0")()
    assert sleeps == [] and stops and guard.reason == "wait_exceeds_remaining"
    with pytest.raises(deadline.RedditDeadlineExceeded):
        guard.check()
    assert any(e.get("wait_seconds") == 120 for e in events)


@pytest.mark.parametrize("value", [-1., float("inf"), float("nan"), True])
def test_invalid_wait_is_explicit_incomplete(value):
    guard, _, _, _ = guard_fixture()
    with pytest.raises(deadline.RedditDeadlineExceeded):
        guard.wrap(lambda: guard.sleep(lambda _: None, value), "tier0")()
    assert guard.reason == "invalid_wait"


def test_equal_remaining_wait_refuses_no_zero_budget_retry():
    guard, _, _, _ = guard_fixture()
    with pytest.raises(deadline.RedditDeadlineExceeded):
        guard.wrap(lambda: guard.sleep(lambda _: pytest.fail("must not sleep"), 35.), "tier0")()


def test_fast_retry_and_response_preserve_body_and_timeout_without_shared_mutation():
    guard, _, events, stops = guard_fixture()
    seen = []
    class Reply(io.BytesIO):
        headers = {"Content-Type": "application/json"}
    def open_response(request, timeout):
        seen.append((request, timeout))
        return Reply(b'{"original":true}')
    def action():
        guard.sleep(lambda n: seen.append(n), 2.)
        with guard.urlopen(open_response, "same request", timeout=15) as result:
            assert result.headers == {"Content-Type": "application/json"}
            return result.read()
    assert guard.wrap(action, "tier0")() == b'{"original":true}'
    assert seen == [2., ("same request", 15)] and not stops
    assert any(e.get("outcome") == "returned" for e in events)


def test_request_uses_parent_remaining_not_new_local_budget():
    guard, clock, _, _ = guard_fixture(now=42.)
    seen = []
    def opening(*args, **kwargs):
        seen.append(kwargs["timeout"])
        return io.BytesIO(b"same")
    def action():
        with guard.urlopen(opening, object(), timeout=15) as response:
            return response.read()
    assert guard.wrap(action, "rss")() == b"same"
    assert seen == [3.]


def test_late_body_poisoned_not_returned_as_success():
    guard, clock, _, stops = guard_fixture()
    class Slow(io.BytesIO):
        def read(self, *args, **kwargs):
            clock[0] = 45.
            return b"late"
    with pytest.raises(deadline.RedditDeadlineExceeded):
        guard.wrap(lambda: guard.urlopen(lambda *a, **k: Slow(), "req", timeout=15).read(), "rss")()
    assert stops


def test_unrelated_source_transport_and_sleep_unchanged():
    guard, _, _, stops = guard_fixture()
    calls = []
    assert guard.urlopen(lambda *a, **k: (a, k), "other", timeout=None) == (("other",), {"timeout": None})
    guard.sleep(calls.append, 120)
    assert calls == [120] and not stops


def test_enrichment_wait_is_remaining_and_never_truncated_success():
    guard, clock, _, stops = guard_fixture(now=40.)
    observed = []
    def waiting(futures, timeout):
        observed.append(timeout)
        return {"done"}, {"pending"}
    with pytest.raises(deadline.RedditDeadlineExceeded):
        guard.wrap(lambda: guard.wait(waiting, {"done", "pending"}, timeout=45), "enrichment_wait")()
    assert observed == [5.] and stops and guard.reason == "incomplete_enrichment"


def test_all_finished_enrichment_keeps_original_return():
    guard, _, _, stops = guard_fixture()
    result = ({1, 2}, set())
    assert guard.wait(lambda *a, **k: result, [1, 2], timeout=45) is result
    assert not stops


def test_finish_cannot_claim_install_failure_or_failed_run_completed():
    guard, _, events, _ = guard_fixture()
    guard.finish()
    assert events[-1]["status"] == "unsupported"
    guard.installed = True
    guard.finish()
    assert events[-1]["status"] == "incomplete"
    guard.completed_runs = 1
    guard.finish()
    assert events[-1]["status"] == "completed"


def test_telemetry_projects_no_raw_request_header_body_or_error():
    records = [
        {"event": "reddit_deadline", "version": deadline.VERSION, "status": "installed",
         "module_sha256": deadline.SUPPORTED_FILES},
        {"event": "reddit_deadline", "version": deadline.VERSION, "action": "request_end",
         "phase": "tier0", "outcome": "http_error", "http_status": 429,
         "url": "PRIVATE_URL", "headers": {"authorization": "SECRET"}, "body": "PRIVATE_BODY"},
        {"event": "reddit_deadline", "version": deadline.VERSION, "status": "completed", "completed_runs": 1},
    ]
    report = provider._read_telemetry(io.BytesIO("\n".join(map(json.dumps, records)).encode()))
    assert report["reddit_deadline"]["status"] == "completed"
    assert "SECRET" not in json.dumps(report) and "PRIVATE" not in json.dumps(report)


def test_changed_selected_transport_fails_closed_before_import(tmp_path, monkeypatch):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib/http.py").write_text("original")
    monkeypatch.setattr(deadline, "SUPPORTED_FILES", {"lib/http.py": hashlib.sha256(b"original").hexdigest()})
    deadline.verify_supported(tmp_path / "last30days.py")
    (tmp_path / "lib/http.py").write_text("changed")
    with pytest.raises(RuntimeError, match="contract unsupported"):
        deadline.verify_supported(tmp_path / "last30days.py")


def test_real_default_worker_infeasible_wait_retains_other_returned_source(tmp_path, monkeypatch):
    from test_last30days_partial_flow import default_fixture
    adapter, settings = default_fixture(tmp_path, monkeypatch, hang=False)
    scripts = tmp_path / "upstream/scripts"
    pipeline = scripts / "lib/pipeline.py"
    text = pipeline.read_text().replace("if source=='reddit': time.sleep(0)",
        "if source=='reddit':\n  time.sleep(.06)\n  from lib import reddit_public\n  reddit_public._fetch_json('https://PRIVATE',15)")
    pipeline.write_text(text)
    public = scripts / "lib/reddit_public.py"
    public.write_text(public.read_text() + '''
from types import SimpleNamespace
def offline_open(request,timeout):
 raise urllib.error.HTTPError('PRIVATE',429,'SECRET',{'Retry-After':'120'},None)
urllib=SimpleNamespace(error=urllib.error,request=SimpleNamespace(urlopen=offline_open))
def _fetch_json(url,timeout):
 try:
  with urllib.request.urlopen(url,timeout=timeout) as response: return response.read()
 except urllib.error.HTTPError as error:
  time.sleep(float(error.headers['Retry-After']))
 return []
''')
    pin_reddit_fixture(tmp_path / "adapter", scripts, monkeypatch)
    hashes = {n: hashlib.sha256((scripts / n).read_bytes()).hexdigest()
              for n in ("last30days.py", "lib/pipeline.py", "lib/planner.py")}
    with (tmp_path / "adapter/last30days_source_policy.py").open("a") as stream:
        stream.write("\nSUPPORTED_FILES=" + repr(hashes) + "\n")
    adapter.s = replace(settings, last30days_timeout_seconds=3.)
    with pytest.raises(TimeoutError) as error:
        adapter.get_research_for_symbol("TEST", "TEST")
    diagnostic = error.value.last30days_diagnostics
    assert diagnostic["outcome"] == "deadline_refused"
    assert diagnostic["elapsed_seconds"] < 2.
    assert diagnostic["reddit_deadline"]["status"] == "refused"
    assert any(e.get("http_status") == 429 for e in diagnostic["events"])
    assert diagnostic["partial_evidence"]["model_context_eligible"] is False
    assert len(diagnostic["partial_evidence"]["items"]) == 2
    assert "PRIVATE" not in json.dumps(diagnostic)


def test_http_status_retained_without_sensitive_message():
    guard, _, events, _ = guard_fixture()
    def failed(*args, **kwargs):
        raise urllib.error.HTTPError("SECRET", 429, "SECRET", {}, None)
    with pytest.raises(urllib.error.HTTPError):
        guard.wrap(lambda: guard.urlopen(failed, "secret", timeout=15), "tier0")()
    assert any(e.get("http_status") == 429 for e in events)
    assert "SECRET" not in json.dumps(events)

@pytest.mark.parametrize("phase", ["body", "executor"])
def test_noncooperative_body_or_executor_cannot_extend_outer_deadline(tmp_path, monkeypatch, phase):
    from test_last30days_partial_flow import default_fixture
    import time
    adapter, settings = default_fixture(tmp_path, monkeypatch, hang=False)
    scripts = tmp_path / "upstream/scripts"
    pipeline = scripts / "lib/pipeline.py"
    if phase == "body":
        invocation = "from lib import reddit_public; reddit_public._fetch_json('PRIVATE',15)"
        public = scripts / "lib/reddit_public.py"
        public.write_text(public.read_text() + '''
from types import SimpleNamespace
class Body:
 def __enter__(self): return self
 def __exit__(self,*args): return False
 def read(self): time.sleep(5); return b'late'
urllib=SimpleNamespace(request=SimpleNamespace(urlopen=lambda *args,**kwargs:Body()))
def _fetch_json(url,timeout):
 with urllib.request.urlopen(url,timeout=timeout) as response: return response.read()
''')
    else:
        invocation = "from lib import reddit_keyless; reddit_keyless._enrich([1], 'quick')"
        keyless = scripts / "lib/reddit_keyless.py"
        keyless.write_text(keyless.read_text() + '''
from concurrent.futures import ThreadPoolExecutor
def _enrich(posts,depth):
 with ThreadPoolExecutor(max_workers=1) as pool:
  futures=[pool.submit(time.sleep,5)]
  concurrent.futures.wait(futures,timeout=45)
 return posts
''')
    pipeline.write_text(pipeline.read_text().replace("if source=='reddit': time.sleep(0)",
                                                    "if source=='reddit': " + invocation))
    pin_reddit_fixture(tmp_path / "adapter", scripts, monkeypatch)
    hashes = {n: hashlib.sha256((scripts / n).read_bytes()).hexdigest()
              for n in ("last30days.py", "lib/pipeline.py", "lib/planner.py")}
    with (tmp_path / "adapter/last30days_source_policy.py").open("a") as stream:
        stream.write("\nSUPPORTED_FILES=" + repr(hashes) + "\n")
    adapter.s = replace(settings, last30days_timeout_seconds=.5)
    began = time.monotonic()
    with pytest.raises(TimeoutError) as error:
        adapter.get_research_for_symbol("TEST", "TEST")
    assert time.monotonic() - began < 2.
    diagnostic = error.value.last30days_diagnostics
    assert diagnostic["deadline_seconds"] == .5
    assert diagnostic["outcome"] in {"timeout", "deadline_refused"}
    assert diagnostic["partial_evidence"]["model_context_eligible"] is False
    assert diagnostic["partial_evidence"]["retained_unique_item_count"] == 2


def test_unsupported_transport_refuses_before_upstream_import_or_work(tmp_path, monkeypatch):
    from test_last30days_partial_flow import default_fixture
    adapter, _ = default_fixture(tmp_path, monkeypatch, hang=False)
    marker = tmp_path / "source-work"
    target = tmp_path / "upstream/scripts/lib/reddit_public.py"
    target.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n")
    with pytest.raises(RuntimeError) as error:
        adapter.get_research_for_symbol("TEST", "TEST")
    diagnostic = error.value.last30days_diagnostics
    assert diagnostic["reddit_deadline"]["status"] == "unverified"
    assert not marker.exists()
    assert not any(e.get("event") == "stage_start" for e in diagnostic["events"])

