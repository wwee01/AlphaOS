"""REG-1: journal-facing regime service -- the ongoing per-scan computation
plus the one-off historical backfill. Both READ SPY history exclusively from
``benchmark_bars`` (PR9.5's own table, extended via ``_backfill_benchmark_bars``'s
``initial_lookback_days`` override for the deeper history this classifier
needs) -- there is no second bar-history fetcher (one-source rule).

Shadow/measurement only: nothing here is read by any gate/eval/risk/execution
path. A missing ``regime_days`` row (insufficient trailing history, or a
benchmark-spine outage) never blocks a scan -- see ``ensure_regime_for_today``'s
own docstring.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any, Optional

from alphaos.constants import Severity
from alphaos.regime.classifier import (
    MIN_BARS_FOR_FIRST_CLASSIFICATION,
    REGIME_RULES_V1,
    classify_regime_series,
)
from alphaos.reports.benchmark_capture import BENCHMARK_SYMBOL, _backfill_benchmark_bars
from alphaos.util import timeutils
from alphaos.util.ids import new_id

# Calendar-day buffer added on top of MIN_BARS_FOR_FIRST_CLASSIFICATION when
# querying benchmark_bars for a single day's classification -- trading days
# are ~5/7 of calendar days plus holidays, so this comfortably covers the
# gap between "trading days needed" and "calendar days to query back".
_CALENDAR_DAY_BUFFER = 120


def _read_spy_closes(journal, limit_calendar_days: Optional[int] = None) -> list:
    """Ascending-by-date ``{"date", "close"}`` list from ``benchmark_bars``.
    ``limit_calendar_days=None`` reads the FULL stored history (the backfill's
    use); an int reads only the trailing window (the per-scan use, cheaper)."""
    if limit_calendar_days is None:
        rows = journal.query(
            "SELECT bar_date, close FROM benchmark_bars WHERE symbol = ? "
            "AND close IS NOT NULL ORDER BY bar_date ASC",
            (BENCHMARK_SYMBOL,),
        )
    else:
        cutoff = (timeutils.market_date() - timedelta(days=limit_calendar_days)).isoformat()
        rows = journal.query(
            "SELECT bar_date, close FROM benchmark_bars WHERE symbol = ? "
            "AND close IS NOT NULL AND bar_date >= ? ORDER BY bar_date ASC",
            (BENCHMARK_SYMBOL, cutoff),
        )
    return [{"date": r["bar_date"], "close": r["close"]} for r in rows]


def _insert_regime_day(journal, row: dict) -> bool:
    """Idempotent insert -- True if a NEW row was written, False if
    (market_date, regime_rules_version) already existed (same idiom as
    universe_days/benchmark_bars: attempt the insert, let the unique index
    reject a duplicate via IntegrityError)."""
    try:
        journal.insert("regime_days", {
            "regime_day_id": new_id("regday"),
            "market_date": row["date"],
            "regime": row["regime"],
            "regime_rules_version": row["rules_version"],
            "spy_close": row["spy_close"],
            "sma_50": row["sma_50"],
            "sma_200": row["sma_200"],
            "realized_vol_20d": row["realized_vol_20d"],
            "vol_percentile_1y": row["vol_percentile_1y"],
            "dev_from_sma50_pct": row["dev_from_sma50_pct"],
            "chop_streak_days": row["chop_streak_days"],
            "computed_at_utc": timeutils.stamp().utc,
        })
        return True
    except sqlite3.IntegrityError:
        return False  # idx_regime_days_date_version backstop -- already recorded


def ensure_regime_for_today(journal, settings, market_dt=None, bars_provider=None) -> Optional[dict]:
    """The ONGOING per-scan entry point: return today's ``regime_days`` row,
    computing + inserting it first if it doesn't exist yet under the current
    rules version. Called once per scan (idempotent across a day's multiple
    scan windows -- the unique index makes a second same-day attempt a cheap
    no-op read).

    Returns None if there is not yet enough trailing SPY history to classify
    (a cold-start gap, or a benchmark-spine outage) -- callers MUST treat
    that as "regime unknown for today", stamping NULL and journaling a loud
    alert, NEVER blocking the scan itself on this (see module docstring).
    Never raises.
    """
    try:
        today = market_dt if market_dt is not None else timeutils.market_date()
        existing = journal.one(
            "SELECT * FROM regime_days WHERE market_date = ? AND regime_rules_version = ?",
            (today.isoformat(), REGIME_RULES_V1),
        )
        if existing:
            return dict(existing)

        # Best-effort: extend benchmark_bars coverage before reading (a no-op
        # in mock/offline mode or if already up to date -- see
        # _backfill_benchmark_bars's own fail-safe design).
        _backfill_benchmark_bars(journal, settings, BENCHMARK_SYMBOL, today, bars_provider=bars_provider)

        window_days = (MIN_BARS_FOR_FIRST_CLASSIFICATION * 7 // 5) + _CALENDAR_DAY_BUFFER
        bars = _read_spy_closes(journal, limit_calendar_days=window_days)
        classified = classify_regime_series(bars)
        if not classified:
            return None  # insufficient trailing history -- caller alerts, never blocks

        latest = classified[-1]
        _insert_regime_day(journal, latest)
        # Re-read rather than trust the just-computed dict verbatim, so the
        # returned shape always matches the DB row shape (incl. regime_day_id).
        return dict(journal.one(
            "SELECT * FROM regime_days WHERE market_date = ? AND regime_rules_version = ?",
            (latest["date"], latest["rules_version"]),
        ))
    except Exception as exc:  # noqa: BLE001 - never let a regime-lookup failure block a scan
        try:
            journal.log_system_event(
                Severity.WARNING, "regime", f"ensure_regime_for_today failed: {exc}",
            )
        except Exception:  # noqa: BLE001 - best-effort logging must not itself crash
            pass
        return None


def backfill_regime_days(journal, settings, bars_provider=None, initial_lookback_days=None) -> dict:
    """The ONE-OFF backfill: extends ``benchmark_bars`` to
    ``initial_lookback_days`` (default ``settings.regime_backfill_lookback_days``)
    of SPY history, classifies the full available series, inserts every NEW
    ``regime_days`` row (existing (date, version) rows are left untouched --
    a derivation, never a mutation, per the spec), then stamps ``regime``/
    ``regime_rules_version`` onto every pre-existing ``candidate_packets``
    row that's still NULL (date-joined via each packet's scan_batch ->
    started_at_utc -> market_date, never a naive UTC-date truncation of the
    packet's own created_at_utc, which could misattribute an evening-UTC/
    morning-ET premarket scan to the wrong trading day).

    Journals exactly one system_event ``regime_backfill`` with row counts +
    rules_version. Idempotent: a re-run under the same rules_version inserts
    zero new regime_days rows and stamps zero additional packets (all already
    non-NULL). Never raises to the caller -- returns a result dict with a
    ``"error"`` key on failure instead.
    """
    lookback = initial_lookback_days if initial_lookback_days is not None else settings.regime_backfill_lookback_days
    result: dict[str, Any] = {
        "rules_version": REGIME_RULES_V1,
        "regime_days_written": 0,
        "regime_days_already_present": 0,
        "packets_stamped": 0,
        "packets_skipped_no_regime_for_date": 0,
    }
    try:
        today = timeutils.market_date()
        _backfill_benchmark_bars(
            journal, settings, BENCHMARK_SYMBOL, today,
            bars_provider=bars_provider, initial_lookback_days=lookback,
        )

        bars = _read_spy_closes(journal, limit_calendar_days=None)
        classified = classify_regime_series(bars)
        for row in classified:
            if _insert_regime_day(journal, row):
                result["regime_days_written"] += 1
            else:
                result["regime_days_already_present"] += 1

        regime_by_date = {
            r["market_date"]: r
            for r in journal.query(
                "SELECT market_date, regime, regime_rules_version FROM regime_days "
                "WHERE regime_rules_version = ?", (REGIME_RULES_V1,),
            )
        }

        unstamped = journal.query(
            "SELECT p.packet_id, p.candidate_id, b.started_at_utc FROM candidate_packets p "
            "LEFT JOIN scan_batches b ON b.scan_batch_id = p.scan_batch_id "
            "WHERE p.regime IS NULL"
        )
        for p in unstamped:
            started = p.get("started_at_utc")
            dt = timeutils.parse_iso(started) if started else None
            if dt is None:
                result["packets_skipped_no_regime_for_date"] += 1
                continue
            packet_market_date = timeutils.market_date(dt).isoformat()
            regime_row = regime_by_date.get(packet_market_date)
            if regime_row is None:
                result["packets_skipped_no_regime_for_date"] += 1
                continue
            journal.conn.execute(
                "UPDATE candidate_packets SET regime = ?, regime_rules_version = ? "
                "WHERE packet_id = ? AND regime IS NULL",
                (regime_row["regime"], regime_row["regime_rules_version"], p["packet_id"]),
            )
            result["packets_stamped"] += 1
        journal.conn.commit()

        journal.log_system_event(
            Severity.INFO, "regime_backfill",
            f"regime_backfill: {result['regime_days_written']} new regime_days rows, "
            f"{result['packets_stamped']} packets stamped (rules {REGIME_RULES_V1}).",
            result,
        )
    except Exception as exc:  # noqa: BLE001 - a backfill failure must be visible, never silent
        result["error"] = str(exc)
        try:
            journal.log_system_event(Severity.ERROR, "regime_backfill", f"backfill failed: {exc}")
        except Exception:  # noqa: BLE001
            pass
    return result
