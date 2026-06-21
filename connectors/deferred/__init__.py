"""DEFERRED connectors — NOT wired into the v1 runtime.

Every entry point here raises ``NotImplementedError("deferred in v1")`` rather
than hitting the network or returning fabricated data. See DEFERRED.md for each
connector's purpose and the explicit trigger that would justify activating it.

The active runtime must never import these modules. An import-graph test enforces
that; only tests that specifically check deferred behavior may import them.
"""

from alphaos.constants import DEFERRED_IN_V1


class DeferredConnectorError(NotImplementedError):
    """Raised when deferred connector code is invoked in v1."""
