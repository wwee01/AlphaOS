"""Weekly review.

Aggregates recent closed-trade outcomes into the standard metric set
(expectancy, profit factor, win rate, drawdown, cost drag, ...). Descriptive
only — no statistical claims on a small forward sample.
"""

from __future__ import annotations

from alphaos.reports.metrics import compute_metrics
from alphaos.util import timeutils


class WeeklyReview:
    def __init__(self, settings, journal):
        self.settings = settings
        self.journal = journal

    def generate(self, limit: int = 500) -> dict:
        outcomes = self.journal.query(
            "SELECT * FROM trade_outcomes ORDER BY id DESC LIMIT ?", (limit,)
        )
        metrics = compute_metrics(outcomes)
        return {
            "as_of": timeutils.market_date().isoformat(),
            "mode": self.settings.mode.value,
            "execution_provider": self.settings.execution_provider,
            **metrics,
        }
