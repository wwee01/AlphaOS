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
import json
import os
import random
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from alphaos.constants import MOCK_L30D_SOURCE, Last30DaysProvider, SentimentLabel
from alphaos.util import timeutils

# Keyless-only source pool used by the deterministic mock.
_KEYLESS_SOURCES = ["reddit", "hackernews", "polymarket", "github"]

# Where Claude Code installs the skill; versioned, so we resolve the newest.
_CLI_CACHE_GLOB = "~/.claude/plugins/cache/last30days-skill/last30days/*/skills/last30days"


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
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=float(self.s.last30days_timeout_seconds), cwd=self._repo_path() or None,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"last30days exited {proc.returncode}: {(proc.stderr or '')[:200]}"
            )
        data = json.loads(proc.stdout)   # JSON on stdout; warnings go to stderr
        return self._parse(data, symbol, query)

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
        return [python, script, query, "--emit", "json", "--search", sources, profile]

    def _repo_path(self) -> str:
        if self.s.last30days_repo_path:
            return os.path.expanduser(self.s.last30days_repo_path)
        # Auto-resolve the newest installed skill version (survives version bumps).
        matches = sorted(glob.glob(os.path.expanduser(_CLI_CACHE_GLOB)))
        return matches[-1] if matches else ""

    @staticmethod
    def _parse(data: dict, symbol: str, query: str) -> Last30DaysResult:
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
            raw_meta={"plan_source": ((data or {}).get("artifacts") or {}).get("plan_source")},
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
