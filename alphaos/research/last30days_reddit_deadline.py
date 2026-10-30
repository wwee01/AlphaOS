"""Deadline composition for the reviewed Reddit transport, in the worker only.

No installed file is changed. Module-local proxies leave unrelated sources alone.
A refusal poisons the whole attempt and asks the existing process supervisor to
stop it; upstream broad Exception handlers cannot turn truncation into success.
Socket timeouts are not total request timers: the outer process deadline remains
the hard boundary for DNS, response bodies and executor shutdown.
"""
from __future__ import annotations

import functools
import hashlib
import importlib
import inspect
import math
from pathlib import Path
import socket
import threading
import time
import urllib.error
from typing import Any, NoReturn

VERSION = "reddit_attempt_deadline_v1"
SUPPORTED_FILES = {
    "lib/reddit.py": "b932a37da46e1375f8fdd83172c5c7a3aa3d52db1913b9878961404cc7937cf4",
    "lib/reddit_public.py": "88a70d514b932e7a8e29dc898f592176a9356504ddd68120e2b114c719628515",
    "lib/reddit_keyless.py": "92ef5008ef657f7b91a5258319b20df6a898c06343ab23f85166b553b6f665e1",
    "lib/reddit_rss.py": "c9acd5302509b681738230b5750caf13f5c85679232ac644a7a19420729ff68c",
    "lib/reddit_listing.py": "ce336df5783394a0354f852e1bbe7e212e447148ef224a98167fdfd04f782fa5",
    "lib/reddit_shreddit.py": "62b440f1ec5a9e1cb72f576ff56ed2a907ce0064705746f980897013081a4099",
    "lib/reddit_enrich.py": "652a9c5abecf22f3c26ff6d6b732a43703113a559ce31acab7af012eb25d4e7a",
    "lib/http.py": "1b729a3d35c565bdb27a605d478b7f6786478cc53f769ecd24dbdc20fcecbe54",
}
PHASES = frozenset({"public", "keyless", "tier0", "rss", "listing", "comments",
                    "enrichment_wait", "keyed_search", "keyed_comments", "attempt"})
REASONS = frozenset({"deadline_exhausted", "wait_exceeds_remaining", "invalid_wait",
                     "incomplete_enrichment", "invalid_timeout"})
ACTIONS = frozenset({"phase_start", "phase_end", "request_start", "request_end", "wait", "refused"})
OUTCOMES = frozenset({"returned", "error", "http_error", "dns_error", "network_error", "timeout"})
HOOKS = (
    ("reddit_public", "search_reddit_public", "public", {"topic", "from_date", "to_date"}),
    ("reddit_public", "_fetch_json", "tier0", {"url", "timeout"}),
    ("reddit_keyless", "search_and_enrich", "keyless", {"topic", "depth"}),
    ("reddit_keyless", "_enrich", "enrichment_wait", {"posts", "depth"}),
    ("reddit_rss", "_fetch_feed", "rss", {"url", "query"}),
    ("reddit_listing", "_fetch_one", "listing", {"subreddit", "sort", "query"}),
    ("reddit_shreddit", "fetch_comments", "comments", {"post_url", "timeout"}),
    ("reddit", "_global_search", "keyed_search", {"query", "token"}),
    ("reddit", "_subreddit_search", "keyed_search", {"subreddit", "query", "token"}),
    ("reddit", "fetch_post_comments", "keyed_comments", {"url", "token"}),
)


class RedditDeadlineExceeded(BaseException):
    """Intentionally bypass upstream 'empty on Exception' fallback handlers."""


class Proxy:
    def __init__(self, target, **overrides):
        self._target = target
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._target, name)


def verify_supported(script):
    root = Path(script).resolve().parent
    try:
        if not all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
                   for name, digest in SUPPORTED_FILES.items()):
            raise ValueError
    except (OSError, ValueError):
        raise RuntimeError("last30days Reddit deadline contract unsupported") from None


class RedditDeadline:
    def __init__(self, started, seconds, emit, stop, *, clock=time.monotonic):
        now = clock()
        if (type(started) not in (int, float) or type(seconds) not in (int, float)
                or not math.isfinite(started) or not math.isfinite(seconds)
                or seconds <= 0 or started > now):
            raise RuntimeError("last30days Reddit deadline clock invalid")
        self.end = started + seconds
        self.clock, self.emit, self.stop = clock, emit, stop
        self.local = threading.local()
        self.lock = threading.Lock()
        self.reason = None
        self.events = self.omitted = 0
        self.installed = False
        self.completed_runs = 0

    def event(self, action, **fields):
        with self.lock:
            if self.events >= 128 and action != "refused":
                self.omitted += 1
                return
            self.events += 1
        self.emit("reddit_deadline", version=VERSION, action=action,
                  phase=getattr(self.local, "phase", "attempt"),
                  remaining_seconds=round(max(0., self.end - self.clock()), 6), **fields)

    def refuse(self, reason) -> NoReturn:
        with self.lock:
            first = self.reason is None
            if first:
                self.reason = reason
        if first:
            self.event("refused", reason=reason)
        self.stop()
        raise RedditDeadlineExceeded("last30days Reddit deadline refused")

    def check(self):
        if self.reason:
            self.refuse(self.reason)
        remaining = self.end - self.clock()
        if remaining <= 0:
            self.refuse("deadline_exhausted")
        return remaining

    def wrap(self, fn, phase):
        @functools.wraps(fn)
        def scoped(*args, **kwargs):
            prior = getattr(self.local, "phase", None)
            self.local.phase = phase
            try:
                self.check()
                self.event("phase_start")
                result = fn(*args, **kwargs)
                self.check()
                self.event("phase_end", outcome="returned")
                return result
            except BaseException:
                self.event("phase_end", outcome="error")
                raise
            finally:
                self.local.phase = prior
        return scoped

    def sleep(self, original, seconds):
        if not getattr(self.local, "phase", None):
            return original(seconds)
        remaining = self.check()
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            self.refuse("invalid_wait")
        self.event("wait", wait_seconds=min(float(seconds), 1e9))
        if seconds >= remaining:
            self.refuse("wait_exceeds_remaining")
        original(seconds)
        self.check()

    def urlopen(self, original, *args, **kwargs):
        if not getattr(self.local, "phase", None):
            return original(*args, **kwargs)
        remaining = self.check()
        timeout = kwargs.get("timeout")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            self.refuse("invalid_timeout")
        kwargs["timeout"] = min(timeout, remaining)
        self.event("request_start", timeout_seconds=kwargs["timeout"])
        try:
            response = original(*args, **kwargs)
        except urllib.error.HTTPError as exc:
            self.event("request_end", outcome="http_error", http_status=exc.code)
            self.check()
            raise
        except urllib.error.URLError as exc:
            self.event("request_end", outcome=("dns_error" if isinstance(exc.reason, socket.gaierror)
                                               else "network_error"))
            self.check()
            raise
        except (TimeoutError, OSError):
            self.event("request_end", outcome="timeout")
            self.check()
            raise
        return Response(response, self)

    def wait(self, original, futures, timeout=None, **kwargs):
        remaining = self.check()
        bounded = remaining if timeout is None else min(timeout, remaining)
        result = original(futures, timeout=bounded, **kwargs)
        self.check()
        if result[1]:
            self.refuse("incomplete_enrichment")
        return result

    def finish(self):
        status = "refused" if self.reason else (
            "completed" if self.installed and self.completed_runs == 1 else
            "incomplete" if self.installed else "unsupported")
        self.emit("reddit_deadline", version=VERSION, status=status,
                  reason=self.reason, omitted_events=self.omitted, completed_runs=self.completed_runs)


class Response(Proxy):
    def __init__(self, response, guard):
        super().__init__(response)
        self.guard = guard

    def __enter__(self):
        self._target = self._target.__enter__()
        self.guard.check()
        return self

    def read(self, *args, **kwargs):
        self.guard.check()
        result = self._target.read(*args, **kwargs)
        self.guard.check()
        return result

    def __exit__(self, kind, value, traceback):
        try:
            return self._target.__exit__(kind, value, traceback)
        finally:
            self.guard.event("request_end", outcome="returned" if kind is None else "error")
            self.guard.check()


def install(script, pipeline, guard):
    verify_supported(script)
    root = Path(script).resolve().parent
    modules: dict[str, Any] = {Path(name).stem: importlib.import_module("lib." + Path(name).stem)
               for name in SUPPORTED_FILES}
    for name, module in modules.items():
        if Path(getattr(module, "__file__", "")).resolve() != root / "lib" / (name + ".py"):
            raise RuntimeError("last30days Reddit deadline import unsupported")
    verify_supported(script)
    for module_name, name, _, parameters in HOOKS:
        fn = getattr(modules[module_name], name, None)
        if not callable(fn) or not parameters <= set(inspect.signature(fn).parameters):
            raise RuntimeError("last30days Reddit deadline hooks unsupported")
    for module_name, name, phase, _ in HOOKS:
        setattr(modules[module_name], name, guard.wrap(getattr(modules[module_name], name), phase))
    # Only these module references change; shared urllib/time modules remain intact.
    for name in ("reddit_public", "http"):
        module = modules[name]
        original_time, original_urllib = module.time, module.urllib
        module.time = Proxy(original_time, sleep=functools.partial(guard.sleep, original_time.sleep))
        request = Proxy(original_urllib.request,
                        urlopen=functools.partial(guard.urlopen, original_urllib.request.urlopen))
        module.urllib = Proxy(original_urllib, request=request)
    keyless = modules["reddit_keyless"]
    keyless.concurrent = Proxy(keyless.concurrent, futures=Proxy(
        keyless.concurrent.futures, wait=functools.partial(guard.wait, keyless.concurrent.futures.wait)))
    original_run = pipeline.run

    @functools.wraps(original_run)
    def run(*args, **kwargs):
        result = original_run(*args, **kwargs)
        guard.check()  # sticky even if future upstream catches BaseException
        guard.completed_runs += 1
        return result

    pipeline.run = run
    guard.installed = True
    guard.emit("reddit_deadline", version=VERSION, status="installed",
               module_sha256=dict(SUPPORTED_FILES))
