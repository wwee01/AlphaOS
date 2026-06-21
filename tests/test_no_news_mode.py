"""No-news mode: evaluation runs on price/structure, sentinels enforced, and
invented catalysts are rejected (Change Prompt §2, §9)."""

from __future__ import annotations

import json

from alphaos.ai.validation import enforce_no_news_sentinels, validate_no_news_eval
from alphaos.constants import (
    CATALYST_NOT_AVAILABLE_V1,
    Decision,
    FAILED_VALIDATION_INVENTED_CATALYST,
    NEWS_STATUS_DISABLED_V1,
)


def test_scan_produces_proposal_with_no_news_sentinels(orchestrator):
    summary = orchestrator.run_scan_once()
    assert summary.proposed > 0  # no-news momentum baseline can propose

    ev = orchestrator.journal.one(
        "SELECT * FROM openai_evaluations WHERE decision = ? LIMIT 1", (Decision.PROPOSE.value,)
    )
    assert ev is not None
    assert ev["catalyst_type"] == CATALYST_NOT_AVAILABLE_V1
    assert ev["news_status"] == NEWS_STATUS_DISABLED_V1
    assert json.loads(ev["news_sources_json"]) == []
    assert ev["validation_status"] == "passed"


def test_openai_mock_eval_is_no_news(orchestrator):
    cand = {"candidate_id": "c1", "symbol": "AAPL", "direction": "long", "momentum_score": 0.9}
    snap = orchestrator.market.get_snapshot("AAPL")
    ev = orchestrator.openai.evaluate(cand, snap, freshness_status="usable")
    assert ev.catalyst_type == CATALYST_NOT_AVAILABLE_V1
    assert ev.news_status == NEWS_STATUS_DISABLED_V1
    assert ev.news_sources == []


def test_baseline_recorded_as_no_news(orchestrator):
    orchestrator.run_scan_once()
    rows = orchestrator.journal.query("SELECT * FROM baseline_outcomes LIMIT 5")
    assert rows
    for r in rows:
        assert r["baseline_type"] == "momentum_continuation_no_news_v1"
        assert r["news_status"] == NEWS_STATUS_DISABLED_V1
        assert r["catalyst"] == CATALYST_NOT_AVAILABLE_V1
        assert r["no_news_baseline"] == 1


def test_validation_rejects_nonempty_news_sources():
    obj = {"news_sources": ["https://example.com/x"], "catalyst": "not_available_v1", "reasoning_summary": "ok"}
    assert validate_no_news_eval(obj) == FAILED_VALIDATION_INVENTED_CATALYST


def test_validation_rejects_named_catalyst():
    obj = {"news_sources": [], "catalyst": "FDA approval", "reasoning_summary": "ok"}
    assert validate_no_news_eval(obj) == FAILED_VALIDATION_INVENTED_CATALYST


def test_validation_rejects_invented_catalyst_phrases():
    for phrase in ("analyst upgrade expected", "likely news-driven move", "earnings beat", "M&A rumor"):
        obj = {"news_sources": [], "catalyst": "not_available_v1", "reasoning_summary": phrase}
        assert validate_no_news_eval(obj) == FAILED_VALIDATION_INVENTED_CATALYST, phrase


def test_validation_passes_clean_price_structure_thesis():
    obj = {
        "news_sources": [],
        "catalyst": "not_available_v1",
        "reasoning_summary": "Strong relative strength and clean trend structure on rising volume.",
    }
    assert validate_no_news_eval(obj) is None


def test_enforce_sentinels():
    out = enforce_no_news_sentinels({"catalyst": "x", "news_sources": ["y"], "news_status": "z"})
    assert out["catalyst"] == CATALYST_NOT_AVAILABLE_V1
    assert out["news_status"] == "disabled_v1"
    assert out["news_sources"] == []
