"""Weekly review — v1 stub.

The full weekly review (expectancy, profit factor, drawdown, fill rate, missed-
vs-taken performance, rule adherence) needs a meaningful forward sample. v1
ships a placeholder that aggregates the week's outcomes without claiming
conclusions.
"""

from __future__ import annotations

from alphaos.util import timeutils


class WeeklyReview:
    def __init__(self, settings, journal):
        self.settings = settings
        self.journal = journal

    def generate(self) -> dict:
        outcomes = self.journal.query("SELECT * FROM trade_outcomes ORDER BY id DESC LIMIT 500")
        wins = [o for o in outcomes if (o.get("win") or 0) == 1]
        net = round(sum((o.get("net_pnl") or 0) for o in outcomes), 2)
        return {
            "as_of": timeutils.market_date().isoformat(),
            "trades": len(outcomes),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(outcomes), 3) if outcomes else None,
            "net_pnl": net,
            "note": "v1 stub — no statistical conclusions; sample too small.",
        }
