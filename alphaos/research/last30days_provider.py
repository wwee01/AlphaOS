"""last30days research provider abstraction (Roadmap 2.5).

A small, swappable interface so AlphaOS can gather recent community/social
narrative without vendoring the ``last30days`` skill. v1 ships:

* ``MockLast30DaysProvider`` — deterministic, offline, hermetic (the TEST default
  and the safe default when enrichment is enabled without a configured CLI).
* ``CliLast30DaysProvider`` — shells out to a GLOBALLY-INSTALLED last30days skill
  via an explicit Python 3.12 interpreter (NOT system ``python3``). Disabled by
  default; only used when ``LAST30DAYS_PROVIDER=cli``. Never breaks the scan — it
  raises on any failure and the enricher fails open as ``unavailable``.

KEYLESS by default: only Reddit / Hacker News / Polymarket / GitHub (no
ScrapeCreators / yt-dlp / X cookies / paid sources). This returns NORMALIZED
research only — it is narrative CONTEXT, not a trade signal, and never executes.
"""

from __future__ import annotations

import abc
import glob
import hashlib
import json
import math
import os
import random
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from alphaos.constants import MOCK_L30D_SOURCE, Last30DaysProvider, SentimentLabel
from alphaos.research.last30days_worker import SOURCES, STAGES, stop_owned_child
from alphaos.util import timeutils

# Keyless-only source pool used by the deterministic mock.
_KEYLESS_SOURCES = ["reddit", "hackernews", "polymarket", "github"]

# Where Claude Code installs the skill; versioned, so we resolve the newest.
_CLI_CACHE_GLOB = "~/.claude/plugins/cache/last30days-skill/last30days/*/skills/last30days"
_WORKER = str(Path(__file__).with_name("last30days_worker.py"))


def _read_telemetry(stream) -> dict:
    """Read bounded metadata, never journal raw provider stdout/stderr."""
    stream.seek(0)
    data = stream.read(131073)
    diagnostic: dict = {"events": [], "truncated": len(data) > 131072}
    for line in data[:131072].splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            diagnostic["malformed"] = True
            continue
        if not isinstance(event, dict):
            diagnostic["malformed"] = True
            continue
        kind = event.get("event")
        if not isinstance(kind, str) or kind not in {"worker_start", "worker_end", "hooks", "cleanup", "stage_start", "stage_end"}:
            continue
        row = {"event": kind}
        for key in ("at_seconds", "elapsed_seconds", "item_count"):
            value = event.get(key)
            if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e9:
                row[key] = value
        if kind in {"stage_start", "stage_end"}:
            for key, allowed in (("stage", STAGES), ("source", SOURCES)):
                value = event.get(key)
                row[key] = value if isinstance(value, str) and value in allowed else "unknown"
            call_id = event.get("call_id")
            if isinstance(call_id, str) and len(call_id) < 80 and all(c in "0123456789:" for c in call_id):
                row["call_id"] = call_id
            if isinstance(event.get("outcome"), str) and event["outcome"] in {"returned", "error"}:
                row["outcome"] = event["outcome"]
        if kind == "hooks":
            for key in ("installed", "missing"):
                values = event.get(key)
                row[key] = sorted({s for s in (values if isinstance(values, list) else [])
                                   if isinstance(s, str) and s in STAGES})
        if kind == "cleanup":
            counts = event.get("counts")
            row["counts"] = {k: v for k, v in (counts.items() if isinstance(counts, dict) else [])
                             if k in {"identity_unverified", "already_reaped", "already_exited",
                                      "detached_group_stopped", "child_stopped"}
                             and type(v) is int and 0 <= v <= 10000}
        diagnostic["events"].append(row)
    if len(diagnostic["events"]) >= 512:
        diagnostic["truncated"] = True  # worker event cap, independent of byte cap
    cleanups = [event for event in diagnostic["events"] if event["event"] == "cleanup"]
    hooks = [event for event in diagnostic["events"] if event["event"] == "hooks"]
    diagnostic["stage_coverage"] = (
        "complete" if hooks and set(hooks[-1]["installed"]) == STAGES and not hooks[-1]["missing"]
        else "incomplete" if hooks else "unavailable"
    )
    diagnostic["cleanup_scope"] = "registered_direct_children" if cleanups else "unverified"
    # Reaped/exited parents cannot prove their former descendant groups gone.
    diagnostic["unverified_descendant_roots"] = (
        sum(cleanups[-1]["counts"].get(status, 0)
            for status in ("identity_unverified", "already_reaped", "already_exited"))
        if cleanups else None
    )
    return diagnostic


@dataclass
class Last30DaysResult:
    """Normalized last30days research for one symbol (the internal provider schema).

    ``clusters`` are compact ``{title, score, sources}`` groupings; we never carry
    raw posts/URLs into the packet or the AI prompt.
    """

    symbol: str
    query: str
    clusters: list = field(default_factory=list)
    item_count: int = 0
    sources_used: list = field(default_factory=list)
    newest_age_hours: Optional[float] = None
    sentiment_hint: Optional[str] = None      # advisory; mock sets it, CLI leaves None
    provider: str = Last30DaysProvider.MOCK.value
    raw_meta: dict = field(default_factory=dict)


class Last30DaysResearchProvider(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def get_research_for_symbol(self, symbol: str, query: str) -> Last30DaysResult:
        ...


def build_query(symbol: str) -> str:
    """Compact, deterministic research query for a ticker."""
    return f"{symbol} stock"


class MockLast30DaysProvider(Last30DaysResearchProvider):
    """Deterministic, offline, clearly-mock research provider.

    Per symbol+market-day it returns a reproducible scenario: a recent narrative
    (with an advisory sentiment skew), nothing (``none_found``), or a stale
    narrative. Sources are real keyless source names but cluster titles are
    clearly mock so nothing is mistaken for live data.
    """

    name = Last30DaysProvider.MOCK.value

    def get_research_for_symbol(self, symbol: str, query: str) -> Last30DaysResult:
        rng = random.Random(f"l30:{symbol}:{timeutils.market_date()}")
        roll = rng.random()

        if roll < 0.6:   # -> available
            n = rng.randint(2, 4)
            titles = [
                f"{symbol} discussion on r/stocks ({MOCK_L30D_SOURCE})",
                f"{symbol} momentum thread ({MOCK_L30D_SOURCE})",
                f"{symbol} setup chatter ({MOCK_L30D_SOURCE})",
                f"{symbol} earnings expectations ({MOCK_L30D_SOURCE})",
            ]
            clusters = []
            for i in range(n):
                k = rng.randint(1, len(_KEYLESS_SOURCES))
                clusters.append({
                    "title": titles[i % len(titles)],
                    "score": round(rng.uniform(10.0, 60.0), 1),
                    "sources": rng.sample(_KEYLESS_SOURCES, k),
                })
            sources_used = sorted({s for c in clusters for s in c["sources"]})
            return Last30DaysResult(
                symbol=symbol, query=query, clusters=clusters,
                item_count=sum(len(c["sources"]) for c in clusters),
                sources_used=sources_used,
                newest_age_hours=round(rng.uniform(2.0, 300.0), 1),
                sentiment_hint=rng.choice([
                    SentimentLabel.BULLISH.value, SentimentLabel.BEARISH.value,
                    SentimentLabel.MIXED.value, SentimentLabel.NEUTRAL.value,
                ]),
                provider=self.name,
            )
        if roll < 0.85:   # -> none_found
            return Last30DaysResult(symbol=symbol, query=query, provider=self.name)
        # -> stale (narrative older than the 30d window)
        return Last30DaysResult(
            symbol=symbol, query=query,
            clusters=[{"title": f"{symbol} old thread ({MOCK_L30D_SOURCE})",
                       "score": 12.0, "sources": ["reddit"]}],
            item_count=1, sources_used=["reddit"], newest_age_hours=1000.0,
            sentiment_hint=SentimentLabel.NEUTRAL.value, provider=self.name,
        )


class CliLast30DaysProvider(Last30DaysResearchProvider):  # pragma: no cover - live, disabled by default
    """Shells out to a globally-installed last30days skill. Used ONLY when
    LAST30DAYS_PROVIDER=cli. Never breaks the scan: it raises on any failure
    (bad python / missing script / timeout / non-zero exit / bad JSON) and the
    enricher fails open as ``unavailable``."""

    name = Last30DaysProvider.CLI.value

    def __init__(self, settings):
        self.s = settings

    def get_research_for_symbol(self, symbol: str, query: str) -> Last30DaysResult:
        cmd = self._build_cmd(query)
        instrumented = len(cmd) > 1 and cmd[1] == _WORKER
        timeout = float(self.s.last30days_timeout_seconds)
        started = time.monotonic()
        diagnostic = {"version": "last30days_runtime_v1", "instrumented": instrumented,
                      "deadline_seconds": timeout}
        if instrumented:
            try:
                diagnostic["provider_script_sha256"] = hashlib.sha256(Path(cmd[2]).read_bytes()).hexdigest()
            except OSError:
                diagnostic["provider_script_sha256"] = None
        # The bootstrap retains direct-child handles, including helpers that
        # detach with setsid(). Outer killpg alone cannot stop those helpers.
        with tempfile.TemporaryFile() as telemetry:
            env = dict(os.environ, ALPHAOS_L30_TELEMETRY_FD=str(telemetry.fileno()))
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    cwd=self._repo_path() or None, start_new_session=True,
                    pass_fds=(telemetry.fileno(),), env=env,
                )
                try:
                    stdout, _stderr = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    diagnostic["outcome"] = "timeout"
                    diagnostic["deadline_observed_seconds"] = round(time.monotonic() - started, 6)
                    if instrumented:
                        proc.terminate()  # bootstrap shuts down only its verified children
                        try:
                            proc.communicate(timeout=1)
                        except subprocess.TimeoutExpired:
                            pass
                    try:
                        diagnostic["outer_cleanup"] = stop_owned_child(proc)
                    except OSError:
                        diagnostic["outer_cleanup"] = "identity_unverified"
                    try:
                        proc.wait(timeout=5)
                        diagnostic["parent_reap"] = "reaped"
                    except subprocess.TimeoutExpired:
                        # Its default string includes the possibly sensitive command.
                        diagnostic["parent_reap"] = "unverified"
                    if proc.stdout:
                        proc.stdout.close()
                    if proc.stderr:
                        proc.stderr.close()
                    # Never include TimeoutExpired.cmd (templates may contain credentials).
                    cleanup = ("process group stopped; detached descendants not guaranteed"
                               if diagnostic["outer_cleanup"] == "detached_group_stopped"
                               else "registered cleanup requested; full descendant cleanup not guaranteed")
                    raise TimeoutError(f"last30days timed out after {timeout:g}s; {cleanup}") from None
                if proc.returncode != 0:
                    diagnostic["outcome"] = "nonzero_exit"
                    raise RuntimeError(f"last30days exited {proc.returncode}; provider output omitted")
                data = json.loads(stdout)
                try:
                    result = self._parse(data, symbol, query)
                except (ValueError, TypeError, AttributeError, KeyError):
                    raise ValueError("last30days result is malformed; provider output omitted") from None
                diagnostic["outcome"] = "returned"
            except Exception as exc:
                diagnostic.setdefault("outcome", "invalid_result" if isinstance(exc, (ValueError, TypeError))
                                      else "provider_error")
                diagnostic["elapsed_seconds"] = round(time.monotonic() - started, 6)
                diagnostic.update(_read_telemetry(telemetry))
                exc.last30days_diagnostics = diagnostic
                raise
            diagnostic["elapsed_seconds"] = round(time.monotonic() - started, 6)
            diagnostic.update(_read_telemetry(telemetry))
            result.raw_meta["diagnostics"] = diagnostic
            return result

    # ---- command construction (no shell=True; list args) ----
    def _build_cmd(self, query: str) -> list:
        sources = self.s.last30days_sources
        if self.s.last30days_cmd:
            tmpl = (self.s.last30days_cmd
                    .replace("{topic}", query)
                    .replace("{sources}", sources)
                    .replace("{python}", os.path.expanduser(self.s.last30days_python))
                    .replace("{repo}", self._repo_path()))
            return shlex.split(tmpl)
        python = os.path.expanduser(self.s.last30days_python)
        script = os.path.join(self._repo_path(), "scripts", "last30days.py")
        profile = "--deep" if (self.s.last30days_profile or "quick").lower() == "deep" else "--quick"
        return [python, _WORKER, script, query, "--emit", "json", "--search", sources, profile]

    def _repo_path(self) -> str:
        if self.s.last30days_repo_path:
            return os.path.expanduser(self.s.last30days_repo_path)
        # Auto-resolve the newest installed skill version (survives version bumps).
        matches = sorted(glob.glob(os.path.expanduser(_CLI_CACHE_GLOB)))
        return matches[-1] if matches else ""

    @staticmethod
    def _parse(data: dict, symbol: str, query: str) -> Last30DaysResult:
        if not isinstance(data, dict):
            raise ValueError("last30days result is not an object")
        clusters_raw = (data or {}).get("clusters") or []
        clusters = [
            {
                "title": (c.get("title") or "")[:140],
                "score": float(c.get("score") or 0.0),
                "sources": list(c.get("sources") or []),
            }
            for c in clusters_raw
        ]
        item_count = sum(len(c.get("candidate_ids") or []) for c in clusters_raw) or len(clusters)
        sources_used = sorted({s for c in clusters for s in c["sources"]})
        return Last30DaysResult(
            symbol=symbol, query=query, clusters=clusters, item_count=item_count,
            sources_used=sources_used, newest_age_hours=None, sentiment_hint=None,
            provider=Last30DaysProvider.CLI.value,
            raw_meta={"plan_source": ((data or {}).get("artifacts") or {}).get("plan_source"),
                      "sources_with_errors": sorted(s for s in (data.get("errors_by_source") or {})
                                                    if s in SOURCES)},
        )


def make_last30days_provider(settings, force: bool = False) -> Optional[Last30DaysResearchProvider]:
    """Build the configured provider, or None if disabled. Never raises.

    ``force=True`` ignores the ``last30days_enabled`` master switch (used by the
    manual ``last30days_probe`` CLI, which is an explicit operator action) but
    still respects the configured provider type.

    In mock mode (``settings.is_mock``), the live ``CliLast30DaysProvider`` is
    never constructed even if LAST30DAYS_PROVIDER=cli -- it is transparently
    substituted with ``MockLast30DaysProvider``, mirroring the mock
    short-circuit already used by the OpenAI evaluator/classifier/polarity
    clients (``settings.is_mock or not settings.has_openai_key``). Without
    this, ALPHAOS_MODE=mock did not disable a real subprocess shell-out
    (HANDOVER.md footgun #4) -- a scheduled or ad-hoc mock-mode scan could
    still hang/spend on the live provider process. ``force=True`` still
    bypasses this (an explicit manual probe may hit the real provider even
    from mock-mode settings).
    """
    if not force and not settings.last30days_enabled:
        return None
    provider = (settings.last30days_provider or "mock").lower()
    if provider in ("disabled", "none", ""):
        return None
    if provider == Last30DaysProvider.CLI.value:
        if settings.is_mock and not force:
            return MockLast30DaysProvider()
        return CliLast30DaysProvider(settings)
    return MockLast30DaysProvider()
