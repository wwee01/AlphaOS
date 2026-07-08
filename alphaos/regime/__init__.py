"""REG-1: regime classifier + packet stamping (shadow/measurement only).

See ``alphaos/regime/classifier.py``. No live decision changes -- this
package measures; REG-2 (a separate, later, evidence-gated PR) acts.
"""

from alphaos.regime.classifier import REGIME_RULES_V1, classify_regime_series

__all__ = ["REGIME_RULES_V1", "classify_regime_series"]
