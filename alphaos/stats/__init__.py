"""PORT-1: the statistical-discipline layer (shadow/measurement only).

Ported from NightDesk's Thesis Research Layer (#85) and paired forward
measurement (#81) -- see docs/roadmap/ported/nightdesk-stats-contract.md.
No live decision changes: this package measures sample-size discipline and
multiple-comparisons risk; nothing here gates, sizes, or executes a trade.

- ``effective_n``: correlated-observation clustering (the ONE shared floor
  function every count-based sample-size check must use).
- ``bootstrap``: clustered bootstrap CI + one-sided p-value.
- ``fdr``: Benjamini-Hochberg + Bonferroni + the always-fresh verdict
  function (never cache a verdict outside ``fdr.compute_verdicts()``'s own
  return value).
- ``preregistration``: the ``preregistrations`` registry -- register once,
  evaluate at most once (evidence immutable thereafter).
"""

from alphaos.stats.effective_n import MIN_TRUSTWORTHY_CLUSTERS, effective_n
from alphaos.stats.fdr import (
    bh_q_values,
    bonferroni_significant,
    compute_verdicts,
    expected_false_positives,
    preregistration_family_summary,
)

__all__ = [
    "MIN_TRUSTWORTHY_CLUSTERS",
    "effective_n",
    "bh_q_values",
    "bonferroni_significant",
    "compute_verdicts",
    "expected_false_positives",
    "preregistration_family_summary",
]
