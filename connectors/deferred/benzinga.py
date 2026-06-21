"""DEFERRED: Benzinga news connector.

News is off in v1 (no-news momentum baseline). This connector is kept as a
labelled seam only — every entry point raises ``deferred in v1`` and never hits
the network or returns fabricated news.

Activation trigger: see connectors/deferred/DEFERRED.md ("News layer").
"""

from __future__ import annotations

from alphaos.constants import DEFERRED_IN_V1


class BenzingaConnector:
    """Reserved Benzinga news connector. Inert in v1."""

    name = "benzinga"

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(DEFERRED_IN_V1)

    def fetch(self, symbol: str):
        raise NotImplementedError(DEFERRED_IN_V1)
