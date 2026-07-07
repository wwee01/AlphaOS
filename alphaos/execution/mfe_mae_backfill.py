"""MFE/MAE backfill for closed trades recorded before intra-trade excursion
tracking existed (or whose ``monitoring_snapshots`` history is otherwise
missing). Idempotent: only ever touches ``trade_outcomes`` rows where
``mfe_mae_source IS NULL``; once a row is processed it always gets a source
tag (never left NULL again), so re-running converges and never reprocesses.

Two sources, tried in order of fidelity:
1. ``monitoring_snapshots`` — the exact per-monitor-pass observed history for
   that position, if any was recorded (source: ``backfilled_from_snapshots``).
2. Historical DAILY bars for the symbol over the position's holding window —
   coarser (only day-level high/low, no intraday path), retrospective
   (source: ``backfilled_from_bars``).
If neither is available, the row is tagged ``unavailable`` and its existing
mfe/mae are left untouched (never invented).

Pure compute (``excursion_from_bars``) takes plain bar dicts, so it is fully
unit-testable without any network access. This module never changes exit,
stop/target, or order behavior — it only ever writes
``trade_outcomes.mfe`` / ``.mae`` / ``.mfe_mae_source``.
"""

from __future__ import annotations

from typing import Optional

from alphaos.constants import TradeDirection
from alphaos.util import timeutils


def excursion_from_bars(entry: Optional[float], stop: Optional[float], direction: Optional[str],
                        bars: list[dict]) -> tuple:
    """(mfe_r, mae_r) from daily bars (each needs 'high'/'low'). Textbook
    excursion semantics, matching the live-tracked path (_fold_excursion): the
    entry moment itself is an implicit R=0 observation, so MFE >= 0 and
    MAE <= 0 always — a trade only ever favorable gets MAE=0, not a
    spuriously "adverse" positive value (Opus audit MEDIUM-1). None/None when
    there's no usable stop, or when the bars carry no real high/low data at
    all (a genuinely unknown excursion is never reported as "flat")."""
    if not bars or entry is None or not stop:
        return None, None
    risk_per_share = abs(float(entry) - float(stop))
    if not risk_per_share:
        return None, None
    is_short = direction == TradeDirection.SHORT.value
    favorable, adverse = [], []
    for b in bars:
        high, low = b.get("high"), b.get("low")
        if high is None or low is None:
            continue
        if is_short:
            favorable.append((float(entry) - float(low)) / risk_per_share)
            adverse.append((float(entry) - float(high)) / risk_per_share)
        else:
            favorable.append((float(high) - float(entry)) / risk_per_share)
            adverse.append((float(low) - float(entry)) / risk_per_share)
    if not favorable:
        return None, None   # no usable bar data at all -- genuinely unknown, not "flat"
    favorable.append(0.0)
    adverse.append(0.0)
    return round(max(favorable), 4), round(min(adverse), 4)


def _update(journal, outcome_id: str, mfe, mae, source: str) -> None:
    journal.conn.execute(
        "UPDATE trade_outcomes SET mfe = ?, mae = ?, mfe_mae_source = ? WHERE outcome_id = ?",
        (mfe, mae, source, outcome_id),
    )
    journal.conn.commit()


def backfill_mfe_mae(journal, bars_provider=None, limit: int = 500) -> dict:
    """Process up to ``limit`` un-backfilled trade_outcomes rows. Returns
    ``{total, from_snapshots, from_bars, unavailable}``. ``bars_provider`` (if
    given) needs a ``get_daily_bars(symbol, start, end) -> list[dict]`` method;
    pass a fixture/fake in tests — this function makes no network calls itself."""
    rows = journal.query(
        "SELECT * FROM trade_outcomes WHERE mfe_mae_source IS NULL ORDER BY id LIMIT ?", (limit,))
    counts = {"total": len(rows), "from_snapshots": 0, "from_bars": 0, "unavailable": 0}

    for row in rows:
        position_id = row.get("position_id")
        snap = journal.one(
            "SELECT MAX(mfe) AS max_mfe, MIN(mae) AS min_mae FROM monitoring_snapshots "
            "WHERE position_id = ?", (position_id,))
        if snap and snap.get("max_mfe") is not None:
            _update(journal, row["outcome_id"], snap["max_mfe"], snap["min_mae"], "backfilled_from_snapshots")
            counts["from_snapshots"] += 1
            continue

        pos = journal.one("SELECT avg_entry_price, stop_price, direction, opened_at "
                          "FROM positions WHERE position_id = ?", (position_id,))
        bars: list[dict] = []
        if bars_provider is not None and pos and pos.get("opened_at"):
            start = timeutils.parse_iso(pos["opened_at"])
            end = timeutils.parse_iso(row.get("created_at_utc")) or timeutils.now_utc()
            if start is not None:
                bars = bars_provider.get_daily_bars(
                    row["symbol"], start.date().isoformat(), end.date().isoformat()) or []

        entry = (pos or {}).get("avg_entry_price")
        stop = (pos or {}).get("stop_price")
        direction = (pos or {}).get("direction")
        mfe, mae = excursion_from_bars(entry, stop, direction, bars)
        if mfe is not None:
            _update(journal, row["outcome_id"], mfe, mae, "backfilled_from_bars")
            counts["from_bars"] += 1
        else:
            _update(journal, row["outcome_id"], row.get("mfe"), row.get("mae"), "unavailable")
            counts["unavailable"] += 1

    return counts
