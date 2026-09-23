"""Supervise the installed CLI and enforce the default adapter's explicit sources.

Executed by the configured provider Python, so this file uses only the stdlib.
The inherited private telemetry descriptor carries allowlisted metadata only.
No upstream files are modified. An unsupported hook is reported, not invented.
"""

from __future__ import annotations

import functools
import importlib
import json
import os
import runpy
import signal
import subprocess
import sys
import threading
import time

SOURCES = frozenset({
    "reddit", "hackernews", "polymarket", "github", "x", "youtube",
    "grounding", "jobs", "bluesky", "digg", "instagram", "pinterest",
    "threads", "tiktok", "truthsocial", "xiaohongshu", "perplexity",
})
STAGES = frozenset({
    "planner", "retrieval", "supplemental", "thin_retry", "finalize",
    "rerank", "score_fun", "github_enrichment", "cluster",
})
HOOKS = (
    ("lib.planner", "plan_query", "planner"),
    ("lib.pipeline", "_retrieve_stream", "retrieval"),
    ("lib.pipeline", "_run_supplemental_searches", "supplemental"),
    ("lib.pipeline", "_retry_thin_sources", "thin_retry"),
    ("lib.pipeline", "_finalize_items_by_source", "finalize"),
    ("lib.rerank", "rerank_candidates", "rerank"),
    ("lib.rerank", "score_fun", "score_fun"),
    ("lib.github", "enrich_candidates_with_stars", "github_enrichment"),
    ("lib.pipeline", "cluster_candidates", "cluster"),
)


class Telemetry:
    def __init__(self, fd: int):
        self.fd = fd
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.events = 0

    def emit(self, kind: str, **detail):
        with self.lock:
            # Bound diagnostics even if a future provider loops indefinitely.
            if self.events >= 512:
                return
            self.events += 1
            row = {"event": kind, "at_seconds": round(time.monotonic() - self.started, 6), **detail}
            try:
                os.write(self.fd, (json.dumps(row, allow_nan=False) + "\n").encode())
            except OSError:
                pass  # telemetry cannot alter the research result

    def wrap(self, fn, stage):
        @functools.wraps(fn)
        def measured(*args, **kwargs):
            source = kwargs.get("source")
            source = source if isinstance(source, str) and source in SOURCES else "unknown"
            started = time.monotonic()
            call_id = f"{threading.get_ident()}:{time.monotonic_ns()}"
            self.emit("stage_start", stage=stage, source=source, call_id=call_id)
            try:
                result = fn(*args, **kwargs)
            except BaseException:
                self.emit("stage_end", stage=stage, source=source, call_id=call_id,
                          elapsed_seconds=round(time.monotonic() - started, 6), outcome="error")
                raise
            count = None
            if stage == "retrieval" and isinstance(result, tuple) and result and isinstance(result[0], list):
                count = len(result[0])
            self.emit("stage_end", stage=stage, source=source, call_id=call_id,
                      elapsed_seconds=round(time.monotonic() - started, 6), outcome="returned",
                      item_count=count)
            return result
        return measured


def stop_owned_child(child) -> str:
    """Kill only our still-unreaped direct child (and its anchored own group).

    CPython's wait lock serializes waitpid with the provider's communicate/wait
    threads. A zero WNOHANG result while holding that lock proves this is still
    our child. Even if it exits immediately afterwards, its unreaped PID cannot
    be reused before signalling. Never act on an already-reaped numeric PID.
    """
    lock = getattr(child, "_waitpid_lock", None)
    if lock is None:
        return "identity_unverified"
    if not lock.acquire(timeout=0.2):
        return "identity_unverified"
    try:
        if child.returncode is not None:
            return "already_reaped"
        try:
            pid, status = os.waitpid(child.pid, os.WNOHANG)
        except ChildProcessError:
            return "identity_unverified"
        if pid:
            child._handle_exitstatus(status)
            return "already_exited"
        try:
            if os.getpgid(child.pid) == child.pid:
                os.killpg(child.pid, signal.SIGKILL)
                return "detached_group_stopped"
            os.kill(child.pid, signal.SIGKILL)
            return "child_stopped"
        except ProcessLookupError:
            return "already_exited"
    finally:
        lock.release()


class ChildSupervisor:
    def __init__(self, telemetry):
        self.telemetry = telemetry
        self.children = []
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.closing = False

    def install(self):
        owner = self

        class OwnedPopen(subprocess.Popen):
            def __init__(self, *args, **kwargs):
                # Serialize child construction with cleanup. Signal handlers
                # merely set an event; they never acquire this lock inline.
                with owner.lock:
                    if owner.stopping.is_set() or owner.closing:
                        raise RuntimeError("last30days worker stopping")
                    super().__init__(*args, **kwargs)
                    owner.children.append(self)

        setattr(subprocess, "Popen", OwnedPopen)
        signal.signal(signal.SIGTERM, lambda *_: self.stopping.set())
        thread = threading.Thread(target=self._shutdown_on_signal, daemon=True)
        thread.start()

    def cleanup(self):
        counts: dict[str, int] = {}
        with self.lock:
            self.closing = True
            for child in self.children:
                try:
                    status = stop_owned_child(child)
                except (OSError, AttributeError):
                    status = "identity_unverified"
                counts[status] = counts.get(status, 0) + 1
        self.telemetry.emit("cleanup", counts=counts)

    def _shutdown_on_signal(self):
        self.stopping.wait()
        self.cleanup()
        # Avoid joining upstream executor threads still waiting on network IO.
        os._exit(124)


def main():
    script = os.path.abspath(sys.argv[1])
    telemetry = Telemetry(int(os.environ.pop("ALPHAOS_L30_TELEMETRY_FD")))
    supervisor = ChildSupervisor(telemetry)
    supervisor.install()
    telemetry.emit("worker_start")
    policy = None
    policy_requested = os.environ.pop("ALPHAOS_L30_SOURCE_POLICY", "")
    try:
        if policy_requested:
            # Import our adjacent stdlib module, not an external provider hook.
            from last30days_source_policy import (  # type: ignore[import-not-found]
                POLICY_VERSION, SourcePolicy, requested_from_argv, verify_imports, verify_supported,
            )
            if policy_requested != POLICY_VERSION:
                raise RuntimeError("last30days source policy version unsupported")
            verify_supported(script)  # before upstream imports or discovery
            policy = SourcePolicy(requested_from_argv(sys.argv[2:]), telemetry.emit)
            sys.path.insert(0, os.path.dirname(script))
            pipeline = importlib.import_module("lib.pipeline")
            planner = importlib.import_module("lib.planner")
            verify_imports(script, pipeline, planner)
            policy.install(pipeline, planner)
            telemetry.emit("source_policy", **policy.summary("installed"))
        else:
            sys.path.insert(0, os.path.dirname(script))
        installed = []
        missing = []
        for module_name, name, stage in HOOKS:
            try:
                module = importlib.import_module(module_name)
                fn = getattr(module, name)
                if not callable(fn):
                    raise AttributeError(name)
            except (ImportError, AttributeError):
                missing.append(stage)
            else:
                setattr(module, name, telemetry.wrap(fn, stage))
                installed.append(stage)
        telemetry.emit("hooks", installed=installed, missing=missing)
        sys.argv = [script, *sys.argv[2:]]
        runpy.run_path(script, run_name="__main__")
    finally:
        if policy_requested:
            if policy is None:
                telemetry.emit("source_policy", version=policy_requested, status="unsupported")
            else:
                status = "completed" if policy.completed_runs == 1 and not policy.violations else "incomplete"
                telemetry.emit("source_policy", **policy.summary(status))
        # Normal completion also must not leave live direct helpers behind.
        # Do not signal the shutdown thread here: a SystemExit(0) must keep its
        # original return code. Only an external SIGTERM triggers os._exit.
        # Keep the owned factory installed through exit: an upstream daemon
        # thread must not bypass the closing guard by spawning during cleanup.
        supervisor.cleanup()
        telemetry.emit("worker_end")


if __name__ == "__main__":
    main()
