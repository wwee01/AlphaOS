"""One-provider consistency: sizing, freshness, proposals, and simulated fills
all pull from the SAME Alpaca market-data interface (Change Prompt §1, §9)."""

from __future__ import annotations

from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from conftest import make_settings


def test_single_active_provider_in_mock():
    orch = Orchestrator(settings=make_settings(), journal=JournalStore(":memory:"))
    # Exactly one provider object behind the generic interface.
    assert orch.market.provider_name == "alpaca_mock"
    assert orch.market.feed == "iex"
    assert orch.market.mode == "mock"

    orch.run_scan_once()
    # Every snapshot came from the one provider/feed.
    providers = orch.journal.query("SELECT DISTINCT provider, feed FROM price_snapshots")
    assert providers == [{"provider": "alpaca_mock", "feed": "iex"}]

    orch.close()


def test_fills_reference_same_data_provider():
    orch = Orchestrator(settings=make_settings(), journal=JournalStore(":memory:"))
    orch.seed_demo()  # exercises sizing + freshness + proposal + simulated fill
    orch.run_monitor_once(price_overrides={"DEMO": 10_000_000})
    fills = orch.journal.query("SELECT DISTINCT data_provider, data_feed FROM paper_fills")
    assert fills == [{"data_provider": "alpaca_mock", "data_feed": "iex"}]
    orch.close()
