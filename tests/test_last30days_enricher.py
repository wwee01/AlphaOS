"""last30days enricher (Roadmap 2.5): status derivation, fail-safe, the distinct
budget-skip record, and the advisory-only contract. Hermetic — providers are
stubs; nothing shells out."""

from __future__ import annotations

from alphaos.constants import CONTEXT_UNAVAILABLE_V1, Last30DaysStatus
from alphaos.research.last30days_enricher import Last30DaysEnricher
from alphaos.research.last30days_provider import Last30DaysResult
from conftest import make_settings


class _StubProvider:
    name = "stub"

    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc

    def get_research_for_symbol(self, symbol, query):
        if self._exc is not None:
            raise self._exc
        return self._result


def _pkt(symbol="AAPL"):
    class _P:
        pass

    p = _P()
    p.symbol = symbol
    return p


def _settings(**over):
    return make_settings(LAST30DAYS_ENABLED="true", **over)


def _result(symbol="AAPL", clusters=None, **kw):
    return Last30DaysResult(symbol=symbol, query=f"{symbol} stock",
                            clusters=clusters or [], **kw)


def test_available_populates_context():
    res = _result(clusters=[
        {"title": "t1", "score": 30.0, "sources": ["reddit", "hackernews"]},
        {"title": "t2", "score": 20.0, "sources": ["reddit"]},
    ], item_count=5, sources_used=["hackernews", "reddit"], newest_age_hours=10.0,
        sentiment_hint="bullish")
    ctx = Last30DaysEnricher(_settings(), provider=_StubProvider(result=res)).enrich(_pkt())
    assert ctx.last30days_status == Last30DaysStatus.AVAILABLE.value
    assert ctx.last30days_context != CONTEXT_UNAVAILABLE_V1
    assert ctx.sentiment_label == "bullish"
    assert ctx.cluster_count == 2
    assert "narrative_present" in ctx.risk_tags
    assert ctx.label_review_required is False        # v1 never auto-suggests a label


def test_none_found_when_no_clusters():
    ctx = Last30DaysEnricher(_settings(), provider=_StubProvider(result=_result())).enrich(_pkt())
    assert ctx.last30days_status == Last30DaysStatus.NONE_FOUND.value
    assert ctx.last30days_status != Last30DaysStatus.SKIPPED_BUDGET_CAP.value
    assert "no_narrative_found" in ctx.risk_tags


def test_stale_when_older_than_lookback():
    res = _result(clusters=[{"title": "old", "score": 5.0, "sources": ["reddit"]}],
                  newest_age_hours=10_000.0)
    ctx = Last30DaysEnricher(_settings(), provider=_StubProvider(result=res)).enrich(_pkt())
    assert ctx.last30days_status == Last30DaysStatus.STALE.value
    assert "stale_narrative" in ctx.risk_tags


def test_fail_open_on_provider_error():
    e = Last30DaysEnricher(_settings(), provider=_StubProvider(exc=RuntimeError("boom")))
    ctx = e.enrich(_pkt())                            # must NOT raise
    assert ctx.last30days_status == Last30DaysStatus.UNAVAILABLE.value
    assert ctx.enrichment_status == "error"
    assert "boom" in (ctx.enrichment_error or "")


def test_fail_closed_when_configured():
    s = _settings(LAST30DAYS_FAIL_OPEN_AS_UNAVAILABLE="false")
    ctx = Last30DaysEnricher(s, provider=_StubProvider(exc=RuntimeError("x"))).enrich(_pkt())
    assert ctx.last30days_status == Last30DaysStatus.ERROR.value


def test_disabled_returns_unavailable():
    # No injected provider + master switch off -> make_last30days_provider returns
    # None -> the enricher reports the disabled state as unavailable.
    e = Last30DaysEnricher(make_settings(LAST30DAYS_ENABLED="false"))
    ctx = e.enrich(_pkt())
    assert ctx.last30days_status == Last30DaysStatus.UNAVAILABLE.value
    assert ctx.enrichment_status == "disabled"
    assert ctx.last30days_context == CONTEXT_UNAVAILABLE_V1


def test_skipped_budget_cap_is_distinct():
    e = Last30DaysEnricher(_settings(), provider=_StubProvider())
    ctx = e.skipped_budget_cap(_pkt(), rank=11, interest_score=0.42)
    assert ctx.last30days_status == Last30DaysStatus.SKIPPED_BUDGET_CAP.value
    assert ctx.enrichment_status == "skipped"
    assert ctx.enrichment_error is None
    assert ctx.interest_rank == 11
    assert ctx.interest_score == 0.42
    assert ctx.reason                                 # records WHY it was skipped
    # explicitly NOT confused with "ran but found nothing" or "provider missing"
    assert ctx.last30days_status != Last30DaysStatus.NONE_FOUND.value
    assert ctx.last30days_status != Last30DaysStatus.UNAVAILABLE.value


def test_to_row_has_expected_shape():
    res = _result(clusters=[{"title": "t", "score": 9.0, "sources": ["reddit"]}],
                  item_count=1, sources_used=["reddit"], newest_age_hours=5.0,
                  sentiment_hint="neutral")
    ctx = Last30DaysEnricher(_settings(), provider=_StubProvider(result=res)).enrich(_pkt("MSFT"))
    row = ctx.to_row("cand1", "pkt1", "scan1")
    for k in ("last30days_id", "candidate_id", "packet_id", "scan_batch_id", "symbol",
              "last30days_status", "summary", "top_themes_json", "source_coverage_json",
              "sentiment_label", "risk_tags_json", "provider", "enrichment_status"):
        assert k in row
    assert row["candidate_id"] == "cand1"
    assert row["symbol"] == "MSFT"
