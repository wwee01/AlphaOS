"""Shared test fixtures.

Tests run fully offline on an in-memory SQLite DB in mock mode — no external
keys, no network. Fixture news (clearly labelled TEST_FIXTURE_NEWS) is allowed
ONLY inside tests and never reaches the runtime path.
"""

from __future__ import annotations

import pytest

from alphaos.config.settings import load_settings
from alphaos.constants import TEST_FIXTURE_NEWS_LABEL
from alphaos.journal.journal_store import JournalStore
from alphaos.news.news_service import NewsItem
from alphaos.orchestrator import Orchestrator
from alphaos.strategy.proposal import TradeProposal
from alphaos.util import timeutils
from alphaos.util.ids import new_id


def make_settings(**overrides):
    env = {
        "ALPHAOS_MODE": "mock",
        "APPROVAL_MODE": "manual",
        "REAL_TRADING_ENABLED": "false",
        "ALPHAOS_DB_PATH": ":memory:",
        "MAX_AUTO_APPROVALS_PER_DAY": "1",
    }
    env.update({k: str(v) for k, v in overrides.items()})
    return load_settings(load_env_file=False, env=env)


@pytest.fixture
def settings():
    return make_settings()


@pytest.fixture
def journal():
    store = JournalStore(":memory:")
    yield store
    store.close()


@pytest.fixture
def orchestrator(settings, journal):
    orch = Orchestrator(settings=settings, journal=journal)
    yield orch


def fixture_news_item(symbol="AAPL"):
    """A TEST_FIXTURE_NEWS item — only valid inside tests."""
    return NewsItem(
        symbol=symbol,
        provider="test_fixture",
        source_url="https://example.test/fixture",
        source_name="Test Fixture",
        headline=f"{symbol} announces strong guidance",
        published_at=timeutils.now_utc().isoformat(),
        fetched_at=timeutils.now_utc().isoformat(),
        summary="Fixture catalyst for tests.",
        catalyst_type="guidance",
        is_fixture=True,
        label=TEST_FIXTURE_NEWS_LABEL,
    )


def make_proposal(symbol="AAPL", direction="long", entry=100.0, stop=97.0, target=106.0,
                  strategy="swing", qty=10, requires_margin=False, candidate_id=None):
    return TradeProposal(
        symbol=symbol, direction=direction, strategy=strategy, entry=entry, stop=stop,
        target=target, max_holding_days=3, qty=qty, risk_per_share=abs(entry - stop),
        dollar_risk=abs(entry - stop) * qty, expected_r=2.0, same_day_exit_eligible=True,
        candidate_id=candidate_id or new_id("cand"), eval_id="ev_test",
        requires_margin=requires_margin, status="pending_approval",
    )


def inject_pending_proposal(orch, symbol="AAPL"):
    """Insert a candidate + eval + pending proposal so manual approval can run."""
    cand_id = new_id("cand")
    snap = orch.market.get_snapshot(symbol)
    entry = float(snap["last_price"])
    stop = round(entry * 0.97, 2)
    target = round(entry * 1.06, 2)
    orch.journal.insert("candidates", {
        "candidate_id": cand_id, "symbol": symbol, "direction": "long", "strategy": "swing",
        "momentum_score": 0.7, "news_status": "available", "status": "proposed",
    })
    eval_id = new_id("eval")
    orch.journal.insert("openai_evaluations", {
        "eval_id": eval_id, "candidate_id": cand_id, "symbol": symbol, "model": "mock",
        "direction": "long", "entry": entry, "stop": stop, "target": target,
        "max_holding_days": 3, "expected_r": 2.0, "confidence": 0.8, "decision": "propose",
        "reasoning_summary": "test", "is_mock": 1,
    })
    prop = make_proposal(symbol=symbol, entry=entry, stop=stop, target=target, candidate_id=cand_id)
    prop.eval_id = eval_id
    orch._stamp_proposal_ttl(prop, snap)  # PR6: fresh by construction, not expired-by-omission
    orch.journal.insert("trade_proposals", prop.to_row())
    return prop.proposal_id, entry
