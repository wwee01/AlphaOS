"""Enforce explicit --search on the reviewed last30days 3.8.1 routing contract.

Stdlib only: imported by the external interpreter in our isolated worker. The
installed provider remains untouched. Unknown routing code fails before import;
upgrades require review rather than silently reverting to implicit sources.
"""
from __future__ import annotations

from collections import Counter
import functools
import hashlib
import inspect
from pathlib import Path
import threading
from typing import NoReturn

POLICY_VERSION = "explicit_search_sources_v1"
POLICY_ENV = "ALPHAOS_L30_SOURCE_POLICY"
SUPPORTED_FILES = {
    "last30days.py": "c14ead20703326ca3866522c47d03b5095b63f56043c0ce2fba24608ca9dedff",
    "lib/pipeline.py": "aaa25c38990230b33ecb2a40b5baa1fca68b5aa1ec800ed45ddd991d18e48f3e",
    "lib/planner.py": "8208901838578e8bcd2b572afa04d2534c93650287564e8303823796f33fde8d",
}
SOURCE_NAMES = frozenset({
    "reddit", "hackernews", "polymarket", "github", "x", "youtube",
    "grounding", "jobs", "bluesky", "digg", "instagram", "pinterest",
    "threads", "tiktok", "truthsocial", "xiaohongshu", "perplexity",
})
ALIASES = {"hn": "hackernews", "bsky": "bluesky", "truth": "truthsocial",
           "web": "grounding", "xhs": "xiaohongshu", "xquik": "x"}


class SourcePolicyError(RuntimeError):
    """Safe generic refusal; never includes upstream content or credentials."""


def requested_from_argv(args):
    if args.count("--search") != 1:
        raise SourcePolicyError("last30days explicit source request missing")
    index = args.index("--search") + 1
    if index >= len(args):
        raise SourcePolicyError("last30days explicit source request missing")
    sources = {ALIASES.get(s.strip().lower(), s.strip().lower()) for s in args[index].split(",")}
    if not sources or not sources <= SOURCE_NAMES:
        raise SourcePolicyError("last30days explicit source request invalid")
    return sorted(sources)


def verify_supported(script):
    root = Path(script).parent
    try:
        matches = Path(script).name == "last30days.py" and all(
            hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
            for name, digest in SUPPORTED_FILES.items()
        )
    except OSError:
        matches = False
    if not matches:
        raise SourcePolicyError("last30days source routing contract unsupported")


def verify_imports(script, pipeline, planner):
    root = Path(script).resolve().parent
    for module, name in ((pipeline, "lib/pipeline.py"), (planner, "lib/planner.py")):
        actual = getattr(module, "__file__", None)
        if not actual or Path(actual).resolve() != (root / name).resolve():
            raise SourcePolicyError("last30days source routing import unsupported")
    verify_supported(script)


class SourcePolicy:
    def __init__(self, requested, emit=lambda *args, **kwargs: None):
        self.requested = frozenset(requested)
        if not self.requested or not self.requested <= SOURCE_NAMES:
            raise SourcePolicyError("last30days explicit source request invalid")
        self.lock = threading.Lock()
        self.observed: Counter[str] = Counter()
        self.suppressed = set()
        self.eligible = set()
        self.planned = set()
        self.violations = 0
        self.completed_runs = 0
        self.emit = emit

    def summary(self, status):
        with self.lock:
            return {"version": POLICY_VERSION, "status": status,
                    "requested": sorted(self.requested), "eligible": sorted(self.eligible),
                    "planned": sorted(self.planned), "suppressed": sorted(self.suppressed),
                    "dispatch_counts": dict(sorted(self.observed.items())),
                    "violations": self.violations, "completed_runs": self.completed_runs}

    def _refuse(self) -> NoReturn:
        with self.lock:
            self.violations += 1
        self.emit("source_policy", **self.summary("incomplete"))
        raise SourcePolicyError("last30days source dispatch outside explicit request")

    def _scope_available(self, available):
        removed = set(available) - self.requested
        with self.lock:
            self.suppressed.update(removed & SOURCE_NAMES)
        available[:] = [s for s in available if s in self.requested]
        self.eligible.update(available)

    def _scope_plan(self, plan):
        for subquery in plan.subqueries:
            removed = set(subquery.sources) - self.requested
            self.suppressed.update(removed & SOURCE_NAMES)
            subquery.sources[:] = [s for s in subquery.sources if s in self.requested]
            self.planned.update(subquery.sources)
        for source in list(plan.source_weights):
            if not isinstance(source, str) or source not in self.requested:
                del plan.source_weights[source]

    def install(self, pipeline, planner):
        # Hashes bind source control flow; signatures also reject wrong/shadowed
        # imports and partially incompatible hooks before any pipeline run.
        for module, name, parameters in (
            (pipeline, "run", {"requested_sources", "topic"}),
            (pipeline, "_ensure_jobs_in_plan", {"plan", "available", "explicit", "topic"}),
            (pipeline, "_retrieve_stream", {"source"}),
            (pipeline, "_run_supplemental_searches", {"depth", "mock"}),
            (pipeline, "_retry_thin_sources", {"plan"}),
            (planner, "plan_query", {"available_sources", "requested_sources"}),
        ):
            fn = getattr(module, name, None)
            if not callable(fn) or not parameters <= set(inspect.signature(fn).parameters):
                raise SourcePolicyError("last30days source routing hooks unsupported")
        original_run = pipeline.run
        original_plan = planner.plan_query
        original_jobs = pipeline._ensure_jobs_in_plan
        original_retrieve = pipeline._retrieve_stream
        original_supplemental = pipeline._run_supplemental_searches

        @functools.wraps(original_run)
        def run(*args, **kwargs):
            if set(pipeline.normalize_requested_sources(kwargs.get("requested_sources")) or []) != self.requested:
                self._refuse()
            result = original_run(*args, **kwargs)
            # Upstream catches retrieval exceptions. A guard violation must
            # still invalidate the whole result, never appear as valid partial data.
            if self.violations:
                self._refuse()
            self.completed_runs += 1
            return result

        @functools.wraps(original_plan)
        def plan(*args, **kwargs):
            self._scope_available(kwargs["available_sources"])
            result = original_plan(*args, **kwargs)
            self._scope_plan(result)
            return result

        @functools.wraps(original_jobs)
        def jobs(plan, available, *, explicit, topic):
            # This list is also used for later direct GitHub enrichment. Mutate
            # it in place before any source dispatch, including external plans.
            self._scope_available(available)
            self._scope_plan(plan)
            self.emit("source_policy", **self.summary("active"))
            result = original_jobs(plan, available, explicit=explicit, topic=topic)
            self._scope_plan(plan)
            return result

        @functools.wraps(original_retrieve)
        def retrieve(*args, **kwargs):
            source = kwargs.get("source")
            if not isinstance(source, str) or source not in self.requested:
                self._refuse()
            with self.lock:
                self.observed[source] += 1
            self.emit("source_policy", **self.summary("active"))
            return original_retrieve(*args, **kwargs)

        @functools.wraps(original_supplemental)
        def supplemental(*args, **kwargs):
            # The pinned deep supplemental implementation directly calls X
            # backends, bypassing _retrieve_stream. It has no other source lane.
            if "x" not in self.requested:
                return None
            return original_supplemental(*args, **kwargs)

        pipeline.run = run
        planner.plan_query = plan
        pipeline._ensure_jobs_in_plan = jobs
        pipeline._retrieve_stream = retrieve
        pipeline._run_supplemental_searches = supplemental
