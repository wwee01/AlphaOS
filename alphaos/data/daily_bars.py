"""INSTR-3: the `daily_bars` table -- persisted daily OHLCV history the
nightly ATR job (``alphaos/reports/atr_service.py``) now writes instead of
discarding (see that module's own docstring). Two read consumers, both
measurement/prompt-augmentation-time only, NEVER the live scan/eval path
directly:

* ``alphaos.scanner.trend.compute_trend_score`` (trend_rules_v1, stamped
  onto the `candidates` row at creation time), and
* ``OpenAIClient._augment_snapshot_for_prompt``'s v3-only MULTI_DAY_CONTEXT
  block.

This module intentionally never imports ``alphaos.data.providers.alpaca_bars``
(or any other network-capable provider) -- it is a pure DB read/write layer,
so anything importing it stays structurally incapable of making a network
call (see ``tests/test_instr3_trend_context.py``'s own AST test pinning
this).
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from alphaos.util.ids import new_id


def persist_daily_bars(journal, symbol: str, bars: list[dict], source_feed: Optional[str]) -> int:
    """Idempotent upsert of ``bars`` (each a ``{"date", "open", "high",
    "low", "close", "volume"}`` dict -- ``AlpacaBarsProvider.get_daily_bars``'s
    own return shape) into ``daily_bars``, keyed UNIQUE(symbol, market_date).
    Returns the count of NEW rows actually written -- a re-run over
    overlapping bars (e.g. the ATR job's own ~25-calendar-day fetch window,
    which re-covers dates already persisted on a prior day) writes zero for
    those dates. Same try/except-IntegrityError idiom as
    ``alphaos/reports/benchmark_capture.py``'s own idempotent bar insert --
    the unique index is the real backstop, this is just the expected-races
    idiom this codebase uses throughout (house pattern #2)."""
    written = 0
    for bar in bars or []:
        market_date = bar.get("date")
        if not market_date:
            continue
        try:
            journal.insert("daily_bars", {
                "bar_id": new_id("bar"),
                "symbol": symbol,
                "market_date": market_date,
                "open": bar.get("open"),
                "high": bar.get("high"),
                "low": bar.get("low"),
                "close": bar.get("close"),
                "volume": bar.get("volume"),
                "source_feed": source_feed,
            })
            written += 1
        except sqlite3.IntegrityError:
            pass  # idx_daily_bars_symbol_date backstop -- already have this date
    return written


def get_recent_daily_bars(journal, symbol: Optional[str], before_date: str, limit: int) -> list[dict]:
    """Up to ``limit`` most recent COMPLETED daily bars for ``symbol``
    strictly before ``before_date`` (an ISO ``YYYY-MM-DD`` string --
    lexicographic comparison is date-correct for this format, same
    convention this codebase already uses elsewhere, e.g.
    ``substr(created_at_utc, 1, 10)`` date-string comparisons). Excludes the
    scan day itself by construction -- both trend_rules_v1 and
    MULTI_DAY_CONTEXT's own "completed sessions only" rule. Returned
    OLDEST-FIRST (ascending by ``market_date``) -- the shape both callers
    want (a rolling window ending at the most recent completed session).

    Returns ``[]`` (never raises on missing data) when nothing is persisted
    yet for this symbol -- an honest "no history", not an error; callers
    treat a short/empty result as an honest-absence signal, never fabricate
    a partial-window number from it."""
    rows = journal.query(
        "SELECT market_date, open, high, low, close, volume FROM daily_bars "
        "WHERE symbol = ? AND market_date < ? ORDER BY market_date DESC LIMIT ?",
        (symbol, before_date, limit),
    )
    return list(reversed(rows))
