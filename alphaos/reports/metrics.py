"""Outcome metrics computed from ``trade_outcomes`` rows.

Win rate, avg win/loss, expectancy, profit factor, max drawdown, avg hold,
same-day-exit frequency, cost drag, and a by-classification breakdown. These are
descriptive only — callers must not claim statistical significance on a small
forward sample (the ``small_sample`` flag and ``note`` make that explicit).
"""

from __future__ import annotations

from typing import Optional

# Below this many closed trades, treat results as anecdotal, not statistical.
MIN_MEANINGFUL_SAMPLE = 30


def _num(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def compute_metrics(outcomes: list[dict]) -> dict:
    n = len(outcomes)
    if n == 0:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": None,
            "gross_pnl": 0.0, "net_pnl": 0.0, "total_costs": 0.0,
            "avg_win": None, "avg_loss": None, "expectancy": None,
            "profit_factor": None, "max_drawdown": 0.0, "avg_hold_days": None,
            "same_day_exit_rate": None, "by_classification": {},
            "small_sample": True, "note": "no closed trades yet",
        }

    nets = [_num(o.get("net_pnl")) for o in outcomes]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    gross = round(sum(_num(o.get("gross_pnl")) for o in outcomes), 2)
    total_costs = round(sum(_num(o.get("costs")) for o in outcomes), 2)
    net = round(sum(nets), 2)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    # Undefined (None) when there are no losses yet — avoids non-JSON 'inf'.
    profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 0 else None

    holds = [_num(o.get("holding_days")) for o in outcomes if o.get("holding_days") is not None]
    same_day = sum(1 for o in outcomes if (o.get("is_same_day") or 0) == 1)

    by_class: dict[str, int] = {}
    for o in outcomes:
        c = o.get("classification") or "unknown"
        by_class[c] = by_class.get(c, 0) + 1

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n, 3),
        "gross_pnl": gross,
        "net_pnl": net,
        "total_costs": total_costs,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "expectancy": round(net / n, 2),                       # avg net P&L per trade
        "profit_factor": profit_factor,
        "max_drawdown": _max_drawdown(nets),
        "avg_hold_days": round(sum(holds) / len(holds), 3) if holds else None,
        "same_day_exit_rate": round(same_day / n, 3),
        "by_classification": by_class,
        "small_sample": n < MIN_MEANINGFUL_SAMPLE,
        "note": (
            f"sample={n} (< {MIN_MEANINGFUL_SAMPLE}); descriptive only, not statistically significant"
            if n < MIN_MEANINGFUL_SAMPLE
            else f"sample={n}"
        ),
    }


def compute_metrics_by_target_profile(outcomes: list[dict]) -> dict:
    """Group closed-trade outcomes by ``target_profile`` and compute the same
    descriptive metric set per group (win rate, expectancy, profit factor, avg
    hold, cost drag, ...). The small-sample caveat applies *per group*: under
    MIN_MEANINGFUL_SAMPLE trades a group's numbers are descriptive only, never a
    significance claim. In v1 every system trade is ``configured_standard``, so
    this typically yields a single group until other profiles are introduced.
    """
    groups: dict[str, list] = {}
    for o in outcomes:
        key = o.get("target_profile") or "configured_standard"
        groups.setdefault(key, []).append(o)
    return {profile: compute_metrics(rows) for profile, rows in sorted(groups.items())}


def _max_drawdown(nets: list[float]) -> float:
    """Peak-to-trough drawdown of the cumulative net-P&L equity curve."""
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for x in nets:
        equity += x
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return round(max_dd, 2)
