"""Official news / catalyst enrichment (Roadmap 2.4).

Takes a shortlisted candidate packet and produces structured catalyst CONTEXT:
is there a known official catalyst, what kind, how fresh, how confident, and what
risk it implies. It is CONTEXT, not execution authority:

* it NEVER forces a proposal, bypasses a gate, mints an official label, or executes;
* it only adds risk tags + explanation + an ADVISORY suggested-label-review;
* it FAILS SAFE: no provider -> ``unavailable``; provider error -> ``error``/
  ``unavailable`` (never crashes the scan); old news -> ``stale``; conflicting
  headlines -> ``conflicting``; nothing relevant -> ``none_found``.

OFFICIAL/market news only (no social, no web scraping, no last30days).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from alphaos.constants import (
    CATALYST_TYPE_TO_LABEL,
    CONTEXT_UNAVAILABLE_V1,
    CatalystStatus,
    CatalystType,
    EnrichmentSource,
    Severity,
)
from alphaos.news.official_news_provider import make_news_provider
from alphaos.util import timeutils
from alphaos.util.ids import new_id

_NONE = "none"


@dataclass
class CatalystContext:
    symbol: str
    catalyst_status: str
    catalyst_summary: str
    catalyst_type: str
    catalyst_confidence: float
    catalyst_sources: list
    catalyst_timestamp_utc: Optional[str]
    catalyst_age_minutes: Optional[float]
    official_news_context: str
    analyst_context: str
    earnings_context: str
    filing_context: str
    sector_context: str
    macro_context: str
    catalyst_risk_tags: list
    catalyst_missing_context: list
    enrichment_source: str
    enrichment_status: str            # ok | disabled | error
    enrichment_error: Optional[str]
    catalyst_suggested_label: Optional[str] = None
    label_review_required: bool = False
    source_count: int = 0

    def to_packet_dict(self) -> dict:
        """Compact catalyst context for the candidate packet / AI (no raw articles —
        source NAMES only)."""
        return {
            "catalyst_status": self.catalyst_status,
            "catalyst_type": self.catalyst_type,
            "catalyst_summary": self.catalyst_summary,
            "catalyst_confidence": self.catalyst_confidence,
            "catalyst_age_minutes": self.catalyst_age_minutes,
            "catalyst_sources": self.catalyst_sources,        # names only
            "catalyst_risk_tags": self.catalyst_risk_tags,
            "official_news_context": self.official_news_context,
            "analyst_context": self.analyst_context,
            "earnings_context": self.earnings_context,
            "filing_context": self.filing_context,
            "sector_context": self.sector_context,
            "macro_context": self.macro_context,
        }

    def to_row(self, candidate_id: str, packet_id: Optional[str], scan_batch_id: Optional[str]) -> dict:
        return {
            "catalyst_id": new_id("cat"),
            "candidate_id": candidate_id,
            "packet_id": packet_id,
            "scan_batch_id": scan_batch_id,
            "symbol": self.symbol,
            "catalyst_status": self.catalyst_status,
            "catalyst_type": self.catalyst_type,
            "catalyst_summary": self.catalyst_summary,
            "catalyst_confidence": self.catalyst_confidence,
            "catalyst_sources_json": self.catalyst_sources,
            "catalyst_timestamp_utc": self.catalyst_timestamp_utc,
            "catalyst_age_minutes": self.catalyst_age_minutes,
            "source_count": self.source_count,
            "official_news_context": self.official_news_context,
            "analyst_context": self.analyst_context,
            "earnings_context": self.earnings_context,
            "filing_context": self.filing_context,
            "sector_context": self.sector_context,
            "macro_context": self.macro_context,
            "catalyst_risk_tags_json": self.catalyst_risk_tags,
            "catalyst_missing_context_json": self.catalyst_missing_context,
            "catalyst_suggested_label": self.catalyst_suggested_label,
            "label_review_required": 1 if self.label_review_required else 0,
            "enrichment_source": self.enrichment_source,
            "enrichment_status": self.enrichment_status,
            "enrichment_error": self.enrichment_error,
        }


_ANALYST = {CatalystType.ANALYST_UPGRADE.value, CatalystType.ANALYST_DOWNGRADE.value}
_NOT_COMPANY_SPECIFIC = {CatalystType.SECTOR_NEWS.value, CatalystType.MACRO.value}


class CatalystEnricher:
    def __init__(self, settings, journal=None, provider=None):
        self.s = settings
        self.journal = journal
        # provider injectable for tests; else built from config (None if disabled)
        self._provider = provider if provider is not None else make_news_provider(settings)

    # ---------------------------------------------------------------- public
    def enrich(self, packet) -> CatalystContext:
        """Enrich a candidate packet with catalyst context. Never raises."""
        symbol = getattr(packet, "symbol", None)
        if not self.s.news_enrichment_enabled or self._provider is None:
            return self._empty(symbol, CatalystStatus.UNAVAILABLE.value,
                               EnrichmentSource.DISABLED.value, "disabled")
        try:
            articles = self._provider.get_news_for_symbol(symbol, self.s.news_lookback_hours)
        except Exception as exc:  # fail-safe: never crash the scan
            if self.journal is not None:
                self.journal.log_system_event(
                    Severity.WARNING, "catalyst",
                    f"news provider failed for {symbol}; failing safe.", {"error": str(exc)},
                )
            status = (CatalystStatus.UNAVAILABLE.value if self.s.news_fail_open_as_unavailable
                      else CatalystStatus.ERROR.value)
            return self._empty(symbol, status, getattr(self._provider, "name", "unknown"),
                               "error", error=str(exc))
        return self._from_articles(symbol, articles)

    # --------------------------------------------------------------- helpers
    def _from_articles(self, symbol, articles) -> CatalystContext:
        src = getattr(self._provider, "name", EnrichmentSource.MOCK.value)
        articles = [a for a in (articles or []) if a][: self.s.news_max_articles_per_symbol]
        if not articles:
            ctx = self._empty(symbol, CatalystStatus.NONE_FOUND.value, src, "ok")
            ctx.catalyst_type = CatalystType.NO_CLEAR_CATALYST.value
            ctx.catalyst_summary = "No relevant official catalyst found in the lookback window."
            ctx.catalyst_risk_tags = ["no_catalyst_found"]
            return ctx

        now = timeutils.now_utc()

        def age_min(a):
            ts = timeutils.parse_iso(getattr(a, "published_at_utc", None))
            return round((now - ts).total_seconds() / 60.0, 1) if ts else None

        newest = max(articles, key=lambda a: getattr(a, "published_at_utc", "") or "")
        newest_age = age_min(newest)
        cats = {getattr(a, "category", CatalystType.UNKNOWN.value) for a in articles}
        ctype = self._normalize_type(getattr(newest, "category", CatalystType.UNKNOWN.value))
        max_age_min = self.s.news_max_age_hours * 60.0

        # --- status derivation ---
        if newest_age is not None and newest_age > max_age_min:
            status = CatalystStatus.STALE.value
        elif (_ANALYST <= cats) or len(cats) >= 3:
            status = CatalystStatus.CONFLICTING.value
        elif len(articles) >= 2 or (getattr(newest, "relevance_score", 0) or 0) >= 0.8:
            status = CatalystStatus.CONFIRMED.value
        else:
            status = CatalystStatus.POSSIBLE.value

        confidence = self._confidence(status, articles, newest)
        sources = sorted({getattr(a, "source", "?") for a in articles})
        summary = f"{ctype.replace('_', ' ')}: {getattr(newest, 'title', '')[:140]}".strip()
        risk_tags = self._risk_tags(status, ctype)
        suggested = CATALYST_TYPE_TO_LABEL.get(ctype)

        ctx = CatalystContext(
            symbol=symbol, catalyst_status=status, catalyst_summary=summary, catalyst_type=ctype,
            catalyst_confidence=confidence, catalyst_sources=sources,
            catalyst_timestamp_utc=getattr(newest, "published_at_utc", None),
            catalyst_age_minutes=newest_age,
            official_news_context=summary,
            analyst_context=summary if ctype in _ANALYST else _NONE,
            earnings_context=summary if ctype == CatalystType.EARNINGS.value else _NONE,
            filing_context=summary if ctype == CatalystType.SEC_FILING.value else _NONE,
            sector_context=summary if ctype == CatalystType.SECTOR_NEWS.value else _NONE,
            macro_context=summary if ctype == CatalystType.MACRO.value else _NONE,
            catalyst_risk_tags=risk_tags,
            catalyst_missing_context=["last30days", "social_sentiment"],
            enrichment_source=src, enrichment_status="ok", enrichment_error=None,
            catalyst_suggested_label=suggested, label_review_required=False,
            source_count=len(articles),
        )
        return ctx

    @staticmethod
    def _normalize_type(cat: Optional[str]) -> str:
        cat = (cat or "").lower()
        valid = {t.value for t in CatalystType}
        return cat if cat in valid else CatalystType.UNKNOWN.value

    @staticmethod
    def _confidence(status, articles, newest) -> float:
        base = {
            CatalystStatus.CONFIRMED.value: 0.8,
            CatalystStatus.POSSIBLE.value: 0.5,
            CatalystStatus.CONFLICTING.value: 0.35,
            CatalystStatus.STALE.value: 0.25,
        }.get(status, 0.0)
        rel = getattr(newest, "relevance_score", None)
        if rel is not None:
            base = round(min(0.95, (base + float(rel)) / 2 + 0.1), 3)
        return round(base, 3)

    @staticmethod
    def _risk_tags(status, ctype) -> list:
        tags = []
        if status == CatalystStatus.CONFLICTING.value:
            tags.append("conflicting_headlines")
        if status == CatalystStatus.STALE.value:
            tags.append("stale_catalyst")
        if ctype in _NOT_COMPANY_SPECIFIC:
            tags += ["sector_sympathy", "catalyst_not_company_specific"]
        if ctype in _ANALYST:
            tags.append("analyst_action")
        if ctype == CatalystType.EARNINGS.value:
            tags.append("earnings_event_risk")
        if ctype == CatalystType.M_AND_A.value:
            tags.append("event_risk_m_and_a")
        return tags or ["catalyst_context_only"]

    def _empty(self, symbol, status, source, enrichment_status, error=None) -> CatalystContext:
        risk = {
            CatalystStatus.UNAVAILABLE.value: ["catalyst_unavailable"],
            CatalystStatus.ERROR.value: ["catalyst_error"],
        }.get(status, ["catalyst_context_only"])
        return CatalystContext(
            symbol=symbol, catalyst_status=status,
            catalyst_summary="catalyst enrichment " + enrichment_status,
            catalyst_type=CatalystType.NO_CLEAR_CATALYST.value, catalyst_confidence=0.0,
            catalyst_sources=[], catalyst_timestamp_utc=None, catalyst_age_minutes=None,
            official_news_context=CONTEXT_UNAVAILABLE_V1, analyst_context=CONTEXT_UNAVAILABLE_V1,
            earnings_context=CONTEXT_UNAVAILABLE_V1, filing_context=CONTEXT_UNAVAILABLE_V1,
            sector_context=CONTEXT_UNAVAILABLE_V1, macro_context=CONTEXT_UNAVAILABLE_V1,
            catalyst_risk_tags=risk, catalyst_missing_context=["official_news", "last30days", "social_sentiment"],
            enrichment_source=source, enrichment_status=enrichment_status, enrichment_error=error,
            catalyst_suggested_label=None, label_review_required=False, source_count=0,
        )
