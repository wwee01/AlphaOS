"""Shared test fixtures.

Tests run fully offline on an in-memory SQLite DB in mock mode — no external
keys, no network. Fixture news (clearly labelled TEST_FIXTURE_NEWS) is allowed
ONLY inside tests and never reaches the runtime path.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

import pytest

from alphaos.cards.registry import get_default_card
from alphaos.config.settings import ALPACA_DATA_BASE_URL, load_settings
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


def _stub_known_network_calls(real_urlopen, *, allow_alpaca_live):
    """Pass through every real urlopen call EXCEPT the two known real-network
    paths reachable from tests:

    * ntfy.sh (alerts) is a hard block -- scoped by URL, not a blanket block,
      so a test that forgets to monkeypatch alerts.send_alert fails loudly.
    * Alpaca market data (alpaca_data.py's get_snapshot/get_snapshots,
      reachable whenever ALPHAOS_MODE=paper + non-empty Alpaca keys are set,
      fake or real) is faked with the same urllib.error.URLError its own
      except-block already handles -- the provider falls back to its normal
      _empty() snapshot, identical in shape to what a real unauthenticated
      call (e.g. the fake "k"/"s" keys used throughout the suite) would
      already produce, just without the real round-trip. Skipped when
      RUN_LIVE_ALPACA_TESTS=true, matching test_live_alpaca.py's own opt-in
      gate for exercising the real endpoint. Tests that want the mapped
      response shape (not just _empty()) monkeypatch urlopen themselves --
      see test_shadow_tier_universe.py -- which overrides this default for
      the remainder of that test."""

    def _urlopen(request, *args, **kwargs):
        url = getattr(request, "full_url", None) or (request if isinstance(request, str) else "")
        if "ntfy.sh" in url:
            raise AssertionError(
                "A test attempted a real urllib.request.urlopen() call to ntfy.sh -- "
                "alerts.send_alert must be monkeypatched in every test that reaches "
                "paper/live mode + a configured NTFY_TOPIC, not left to rely on the "
                "empty-topic no-op. See tests/test_alerts.py for the monkeypatch pattern."
            )
        if not allow_alpaca_live and url.startswith(ALPACA_DATA_BASE_URL):
            raise urllib.error.URLError(
                "network access disabled in tests (see conftest._block_real_network_calls)"
            )
        return real_urlopen(request, *args, **kwargs)

    return _urlopen


@pytest.fixture(autouse=True)
def _block_real_network_calls(monkeypatch):
    """Defense-in-depth backstop (scope/safety audit LOW-2, PR9's immediate-
    alerts audit; extended for the Alpaca market-data path per the hermetic-
    Alpaca-data-tests fixup): the zero-network-leak guarantee previously
    rested only on conventions (mock-mode default + unset NTFY_TOPIC default)
    with no hard stop, and the Alpaca data path had no stop at all -- two
    scheduler tests using fake Alpaca keys were making real HTTPS calls to
    data.alpaca.markets on every run. Tests that intentionally exercise
    urlopen (tests/test_alerts.py, tests/test_shadow_tier_universe.py)
    already monkeypatch it themselves, which overrides this default for
    their duration -- this fixture only catches call sites nobody stubbed."""
    allow_alpaca_live = os.environ.get("RUN_LIVE_ALPACA_TESTS") == "true"
    monkeypatch.setattr(
        urllib.request, "urlopen",
        _stub_known_network_calls(urllib.request.urlopen, allow_alpaca_live=allow_alpaca_live),
    )


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
                  strategy="swing", qty=10, requires_margin=False, candidate_id=None,
                  with_card=True, invalidation_reason=None):
    """``with_card=True`` (default) stamps the real default setup card, matching
    what the current pipeline always produces (Roadmap PR10's exit-first
    invariant blocks _execute() on a proposal missing card_id/invalidation_reason,
    so every OTHER test relying on execution actually succeeding needs a
    complete proposal here). Pass ``with_card=False`` to build a deliberately
    legacy/incomplete proposal for testing that exact blocking behavior."""
    card = get_default_card() if with_card else None
    return TradeProposal(
        symbol=symbol, direction=direction, strategy=strategy, entry=entry, stop=stop,
        target=target, max_holding_days=3, qty=qty, risk_per_share=abs(entry - stop),
        dollar_risk=abs(entry - stop) * qty, expected_r=2.0, same_day_exit_eligible=True,
        candidate_id=candidate_id or new_id("cand"), eval_id="ev_test",
        requires_margin=requires_margin, status="pending_approval",
        card_id=card["card_id"] if card else None,
        card_version=card["version"] if card else None,
        invalidation_reason=(
            invalidation_reason if invalidation_reason is not None
            else (card["invalidation_rule"] if card else None)
        ),
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
