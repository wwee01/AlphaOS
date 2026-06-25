"""last30days research / narrative-context enrichment (Roadmap 2.5).

Takes a shortlisted candidate packet and produces structured narrative CONTEXT:
is there recent community narrative, what themes, which sources, an ADVISORY
sentiment, and what risk it implies. It is CONTEXT, not execution authority:

* it NEVER forces a proposal, bypasses a gate, mints/overwrites an official label,
  affects sizing, or executes;
* it NEVER enters the no-news OpenAI momentum eval;
* it only adds risk tags + an explanation (and may suggest a label review later);
* it FAILS SAFE: disabled/no provider -> ``unavailable``; provider error ->
  ``unavailable``/``error`` (never crashes the scan); old -> ``stale``; nothing
  -> ``none_found``.

A SEPARATE social/research layer from official news (Roadmap 2.4). The per-scan
budget cap is enforced by the orchestrator; candidates that are eligible but
outside the cap get an explicit ``skipped_budget_cap`` row (distinct from
``none_found`` / ``unavailable`` / ``error`` / ``disabled``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from alphaos.constants import (
    CONTEXT_UNAVAILABLE_V1,
    L30D_SKIPPED_REASON,
    Last30DaysProvider,
    Last30DaysStatus,
    SentimentLabel,
    Severity,
)
from alphaos.research.last30days_provider import build_query, make_last30days_provider
from alphaos.util.ids import new_id


@dataclass
class Last30DaysContext:
    symbol: str
    last30days_status: str
    summary: str
    top_themes: list
    source_coverage: list
    item_count: int
    cluster_count: int
    top_score: Optional[float]
    sentiment_label: str
    sentiment_score: Optional[float]
    newest_age_hours: Optional[float]
    risk_tags: list
    last30days_context: str       # compact packet string
    sentiment_context: str        # compact packet string
    label_review_required: bool
    query: Optional[str]
    reason: Optional[str]
    interest_rank: Optional[int]
    interest_score: Optional[float]
    provider: str
    enrichment_status: str        # ok | skipped | disabled | error
    enrichment_error: Optional[str]

    def to_packet_dict(self) -> dict:
        """The two compact fields folded into the candidate packet / AI prompt."""
        return {
            "last30days_context": self.last30days_context,
            "sentiment_context": self.sentiment_context,
        }

    def to_row(self, candidate_id: str, packet_id: Optional[str], scan_batch_id: Optional[str]) -> dict:
        return {
            "last30days_id": new_id("l30"),
            "candidate_id": candidate_id,
            "packet_id": packet_id,
            "scan_batch_id": scan_batch_id,
            "symbol": self.symbol,
            "last30days_status": self.last30days_status,
            "summary": self.summary,
            "top_themes_json": self.top_themes,
            "source_coverage_json": self.source_coverage,
            "item_count": self.item_count,
            "cluster_count": self.cluster_count,
            "top_score": self.top_score,
            "sentiment_label": self.sentiment_label,
            "sentiment_score": self.sentiment_score,
            "newest_age_hours": self.newest_age_hours,
            "risk_tags_json": self.risk_tags,
            "last30days_context": self.last30days_context,
            "sentiment_context": self.sentiment_context,
            "label_review_required": 1 if self.label_review_required else 0,
            "query": self.query,
            "reason": self.reason,
            "interest_rank": self.interest_rank,
            "interest_score": self.interest_score,
            "provider": self.provider,
            "enrichment_status": self.enrichment_status,
            "enrichment_error": self.enrichment_error,
        }


class Last30DaysEnricher:
    def __init__(self, settings, journal=None, provider=None):
        self.s = settings
        self.journal = journal
        # provider injectable for tests; else built from config (None if disabled)
        self._provider = provider if provider is not None else make_last30days_provider(settings)

    # ---------------------------------------------------------------- public
    def enrich(self, packet, rank: Optional[int] = None,
               interest_score: Optional[float] = None) -> Last30DaysContext:
        """Enrich a candidate packet with last30days narrative context. Never raises."""
        symbol = getattr(packet, "symbol", None)
        # The LAST30DAYS_ENABLED master switch is enforced upstream by
        # make_last30days_provider (returns None when disabled), so a None provider
        # IS the disabled state. The manual probe injects a forced provider to test
        # the live path without enabling it in scans.
        if self._provider is None:
            return self._empty(symbol, Last30DaysStatus.UNAVAILABLE.value,
                               Last30DaysProvider.DISABLED.value, "disabled",
                               rank=rank, interest_score=interest_score)
        query = build_query(symbol)
        try:
            result = self._provider.get_research_for_symbol(symbol, query)
        except Exception as exc:  # fail-safe: never crash the scan
            if self.journal is not None:
                self.journal.log_system_event(
                    Severity.WARNING, "last30days",
                    f"last30days provider failed for {symbol}; failing safe.", {"error": str(exc)},
                )
            status = (Last30DaysStatus.UNAVAILABLE.value if self.s.last30days_fail_open_as_unavailable
                      else Last30DaysStatus.ERROR.value)
            ctx = self._empty(symbol, status, getattr(self._provider, "name", "unknown"),
                              "error", error=str(exc), rank=rank, interest_score=interest_score)
            return ctx
        return self._from_result(symbol, result, rank, interest_score)

    def skipped_budget_cap(self, packet, rank: Optional[int] = None,
                           interest_score: Optional[float] = None,
                           reason: str = L30D_SKIPPED_REASON) -> Last30DaysContext:
        """Eligible candidate outside the per-scan cap: an explicit, distinct
        ``skipped_budget_cap`` record (NO provider call, NO narrative). This is
        deliberately NOT ``none_found`` (which means we checked and found little)
        nor ``unavailable`` (provider missing)."""
        provider = getattr(self._provider, "name", None) or (self.s.last30days_provider or "none")
        ctx = self._empty(getattr(packet, "symbol", None),
                          Last30DaysStatus.SKIPPED_BUDGET_CAP.value, provider, "skipped",
                          rank=rank, interest_score=interest_score)
        ctx.reason = reason
        ctx.summary = "not enriched: outside last30days budget cap"
        ctx.last30days_context = "not enriched (budget cap)"
        ctx.risk_tags = ["last30days_skipped_budget_cap"]
        return ctx

    # --------------------------------------------------------------- helpers
    def _from_result(self, symbol, result, rank, interest_score) -> Last30DaysContext:
        src = getattr(result, "provider", Last30DaysProvider.MOCK.value)
        clusters = list(getattr(result, "clusters", []) or [])
        lookback = float(self.s.last30days_lookback_hours)
        age = getattr(result, "newest_age_hours", None)

        # --- status derivation ---
        if not clusters:
            return self._none_found(symbol, result, rank, interest_score)
        if age is not None and age > lookback:
            status = Last30DaysStatus.STALE.value
        else:
            status = Last30DaysStatus.AVAILABLE.value

        themes = [c.get("title", "") for c in clusters if c.get("title")][: self.s.last30days_max_themes]
        coverage = sorted(getattr(result, "sources_used", []) or
                          {s for c in clusters for s in (c.get("sources") or [])})
        top_score = max((float(c.get("score") or 0.0) for c in clusters), default=None)
        sentiment = (getattr(result, "sentiment_hint", None) or SentimentLabel.UNKNOWN.value)
        sentiment_score = (round(min(0.9, 0.4 + 0.1 * len(clusters)), 2)
                           if sentiment != SentimentLabel.UNKNOWN.value else None)

        if status == Last30DaysStatus.STALE.value:
            summary = "Only stale narrative (older than the lookback window)."
            risk_tags = ["stale_narrative"]
            l30_ctx = summary
        else:
            summary = (f"{len(clusters)} themes across {','.join(coverage) or 'n/a'} "
                       f"({getattr(result, 'item_count', 0)} items): " + "; ".join(themes))
            risk_tags = ["narrative_present"]
            if len(coverage) < 2:
                risk_tags.append("low_social_coverage")
            if sentiment == SentimentLabel.BEARISH.value:
                risk_tags.append("sentiment_bearish")
            l30_ctx = summary[:280]

        sent_ctx = (f"{sentiment}" + (f" ({sentiment_score})" if sentiment_score is not None else "")
                    + " — advisory, keyless") if sentiment != SentimentLabel.UNKNOWN.value \
            else "unknown — advisory, keyless"

        return Last30DaysContext(
            symbol=symbol, last30days_status=status, summary=summary,
            top_themes=themes, source_coverage=list(coverage),
            item_count=int(getattr(result, "item_count", 0) or 0), cluster_count=len(clusters),
            top_score=top_score, sentiment_label=sentiment, sentiment_score=sentiment_score,
            newest_age_hours=age, risk_tags=risk_tags, last30days_context=l30_ctx,
            sentiment_context=sent_ctx, label_review_required=False,
            query=getattr(result, "query", None), reason=None, interest_rank=rank,
            interest_score=interest_score, provider=src, enrichment_status="ok",
            enrichment_error=None,
        )

    def _none_found(self, symbol, result, rank, interest_score) -> Last30DaysContext:
        ctx = self._empty(symbol, Last30DaysStatus.NONE_FOUND.value,
                          getattr(result, "provider", Last30DaysProvider.MOCK.value), "ok",
                          rank=rank, interest_score=interest_score)
        ctx.summary = "No clear recent narrative found in the last30days window."
        ctx.last30days_context = "no clear narrative (last30days)"
        ctx.risk_tags = ["no_narrative_found"]
        ctx.query = getattr(result, "query", None)
        return ctx

    def _empty(self, symbol, status, provider, enrichment_status, error=None,
               rank=None, interest_score=None) -> Last30DaysContext:
        return Last30DaysContext(
            symbol=symbol, last30days_status=status,
            summary="last30days enrichment " + enrichment_status,
            top_themes=[], source_coverage=[], item_count=0, cluster_count=0, top_score=None,
            sentiment_label=SentimentLabel.UNKNOWN.value, sentiment_score=None,
            newest_age_hours=None,
            risk_tags={
                Last30DaysStatus.UNAVAILABLE.value: ["last30days_unavailable"],
                Last30DaysStatus.ERROR.value: ["last30days_error"],
                Last30DaysStatus.SKIPPED_BUDGET_CAP.value: ["last30days_skipped_budget_cap"],
            }.get(status, ["last30days_context_only"]),
            last30days_context=CONTEXT_UNAVAILABLE_V1, sentiment_context=CONTEXT_UNAVAILABLE_V1,
            label_review_required=False, query=None, reason=None, interest_rank=rank,
            interest_score=interest_score, provider=provider,
            enrichment_status=enrichment_status, enrichment_error=error,
        )
