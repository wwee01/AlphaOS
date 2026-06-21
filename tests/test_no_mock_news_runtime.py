"""Mock/fixture news is never used in runtime candidate evaluation (test #10)."""

from __future__ import annotations

from alphaos.constants import Decision, NewsStatus, TEST_FIXTURE_NEWS_LABEL
from alphaos.news.news_service import NewsService
from conftest import make_settings, fixture_news_item


def test_news_unavailable_in_mock_mode(journal):
    s = make_settings()
    svc = NewsService(s, journal)
    items, status = svc.get_news("AAPL")
    assert items == []
    assert status == NewsStatus.NEWS_UNAVAILABLE


def test_fixture_news_is_filtered_out_of_runtime(journal):
    s = make_settings()
    svc = NewsService(s, journal)
    # Simulate a connector accidentally returning a fixture item.
    svc.benzinga.fetch = lambda symbol: [fixture_news_item(symbol)]
    items, status = svc.get_news("AAPL")
    assert items == []
    assert status == NewsStatus.NEWS_UNAVAILABLE
    # A loud CRITICAL event records the filtering.
    crit = journal.query("SELECT * FROM system_events WHERE severity='critical' AND category='news'")
    assert crit, "expected a critical system event when fixture news is filtered"


def test_scan_runtime_uses_no_fixture_news_and_makes_no_proposals(orchestrator):
    summary = orchestrator.run_scan_once()
    j = orchestrator.journal
    # No fixture-labelled news persisted.
    bad = j.query(
        "SELECT * FROM news_items WHERE is_fixture = 1 OR label = ?", (TEST_FIXTURE_NEWS_LABEL,)
    )
    assert bad == []
    # Without verifiable news, the news-confirmed playbook makes no proposals.
    assert summary.proposed == 0
    assert j.count_rows("trade_proposals", "status != 'blocked'") == 0
    # Every detected candidate is NEWS_UNAVAILABLE and not promoted past watch/reject.
    cands = j.recent_candidates(100)
    for c in cands:
        assert c["news_status"] == NewsStatus.NEWS_UNAVAILABLE.value
        assert c["status"] in ("watch", "rejected", "detected")


def test_openai_eval_without_news_does_not_propose(orchestrator):
    """Direct check: the OpenAI mock never 'proposes' without verifiable news."""
    cand = {"candidate_id": "c1", "symbol": "AAPL", "direction": "long", "momentum_score": 0.9}
    snap = orchestrator.market.get_snapshot("AAPL")
    ev = orchestrator.openai.evaluate(cand, snap, [], NewsStatus.NEWS_UNAVAILABLE, "usable")
    assert ev.decision != Decision.PROPOSE.value
