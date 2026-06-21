"""DEFERRED: Massive market-data connector.

Moved out of the active runtime in v1 (Alpaca/IEX is the sole active provider).
Kept as a labelled seam: the class + method shape are preserved so re-wiring is
cheap, but every entry point raises ``deferred in v1`` — it never hits the
network or returns fake data.

Activation trigger: see connectors/deferred/DEFERRED.md ("Richer market data").
"""

from __future__ import annotations

from alphaos.constants import DEFERRED_IN_V1


class MassiveDataConnector:
    """Reserved Massive connector. Inert in v1."""

    name = "massive"

    def __init__(self, *args, **kwargs):  # accept any signature for future wiring
        raise NotImplementedError(DEFERRED_IN_V1)

    def get_snapshot(self, symbol: str) -> dict:
        raise NotImplementedError(DEFERRED_IN_V1)

    def get_snapshots(self, symbols):
        raise NotImplementedError(DEFERRED_IN_V1)
