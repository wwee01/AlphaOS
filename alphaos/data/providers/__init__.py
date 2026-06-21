"""Market-data providers behind the generic ``MarketDataClient`` interface.

v1 ships two providers that emit the SAME Alpaca-shaped snapshot schema:
* ``AlpacaDataProvider`` — live IEX data (the only active live provider in v1).
* ``MockDataProvider``   — offline, clearly-labelled mock data.

The rest of the system talks to ``MarketDataClient``, never to a provider
directly, so a richer provider can be slotted in later without touching the
scanner, risk engine, or freshness guard.
"""

from alphaos.data.providers.base import MarketDataProvider, SNAPSHOT_FIELDS
from alphaos.data.providers.mock_provider import MockDataProvider
from alphaos.data.providers.alpaca_data import AlpacaDataProvider

__all__ = [
    "MarketDataProvider",
    "SNAPSHOT_FIELDS",
    "MockDataProvider",
    "AlpacaDataProvider",
]
