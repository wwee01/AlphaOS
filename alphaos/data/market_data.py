"""Generic market-data interface.

The rest of AlphaOS depends ONLY on this class, never on a concrete provider, so
a richer data source can be slotted in later without touching the scanner, risk
engine, freshness guard, or execution.

v1 wiring:
* offline/mock mode  -> ``MockDataProvider``  (Alpaca-shaped, labelled mock)
* live mode          -> ``AlpacaDataProvider`` (free IEX tier; the only active
  live provider in v1 — Massive is deferred)

There is exactly ONE active data source in v1. Missing Alpaca credentials in
live mode never silently fall back to mock or any other provider — the snapshot
comes back unusable and the freshness guard blocks it.
"""

from __future__ import annotations

from alphaos.config.settings import Settings
from alphaos.constants import DataProvider, Severity
from alphaos.data.providers.alpaca_data import AlpacaDataProvider
from alphaos.data.providers.mock_provider import MockDataProvider


class MarketDataClient:
    def __init__(self, settings: Settings, journal=None):
        self.settings = settings
        self.journal = journal
        self.use_mock = settings.offline_mode
        self._warned = False

        if self.use_mock:
            self.provider = MockDataProvider(feed=settings.market_data_feed)
        elif settings.data_provider == DataProvider.ALPACA.value:
            self.provider = AlpacaDataProvider(settings, journal)
        else:  # pragma: no cover - load_settings already rejects this
            raise ValueError(f"Unsupported DATA_PROVIDER: {settings.data_provider!r}")

    # ------------------------------------------------------------------ public
    @property
    def provider_name(self) -> str:
        return self.provider.name

    @property
    def feed(self) -> str:
        return self.settings.market_data_feed

    @property
    def mode(self) -> str:
        return self.settings.market_data_mode

    def get_snapshot(self, symbol: str) -> dict:
        if self.use_mock:
            self._warn_once()
        return self.provider.get_snapshot(symbol)

    def get_snapshots(self, symbols: list[str]) -> list[dict]:
        """EXP-0: delegates to the provider's own batch implementation
        (``AlpacaDataProvider`` does one HTTP call per ~100 symbols instead of
        one per symbol; ``MockDataProvider`` falls back to the base class's
        per-symbol loop). The mock-mode warning fires once per batch call
        here, not once per symbol as calling ``get_snapshot`` in a loop
        would."""
        if self.use_mock:
            self._warn_once()
        return self.provider.get_snapshots(symbols)

    # ------------------------------------------------------------------ helpers
    def _warn_once(self) -> None:
        if self._warned or self.journal is None:
            return
        self._warned = True
        self.journal.log_system_event(
            Severity.WARNING,
            "market_data",
            "Market data is MOCKED (alpaca_mock, offline). Clearly labelled; not live.",
        )
