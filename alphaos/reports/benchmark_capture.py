"""Benchmark spine capture (PR9.5): the daily write side of measuring
performance against the S&P 500 -- the one number the exit review found
AlphaOS could not answer at all. Write-only, measurement-only: never read by
any gate/eval/labeller/risk/execution path (mirrors every prior shadow layer's
own isolation -- TQS, attribution, decision lineage).

Two independent, individually fail-safe halves per call:
  1. Capture today's paper-account equity (idempotent -- at most one row per
     market_date).
  2. Backfill any missing SPY daily bars up to today (idempotent -- at most
     one row per (symbol, date); a public price series, safe to backfill
     historical dates unlike our own equity, which can only ever be recorded
     forward from today).

Neither half's failure blocks the other, and neither ever raises to the
caller -- this runs inside the scheduler's daily_digest-style cadence and
must behave like every other job function (see alphaos/scheduler/jobs.py's
own docstring: only genuinely unexpected exceptions may propagate, and this
module has none of those left uncaught).
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Optional

from alphaos.broker.alpaca_client import AlpacaClient
from alphaos.constants import Severity
from alphaos.data.providers.alpaca_bars import make_bars_provider
from alphaos.util import timeutils
from alphaos.util.ids import new_id

BENCHMARK_SYMBOL = "SPY"
# First-ever run backfills this many days of SPY history so the comparison
# series has meaningful depth from day 1 (SPY's history is public record;
# this is NOT the "can't backfill honestly" constraint that applies to our
# own equity_snapshots below).
INITIAL_BENCHMARK_LOOKBACK_DAYS = 90
# A bare get_daily_bars call truncates silently at its own `limit` default --
# a gap bigger than one page would otherwise only close over many days of
# reruns. Paging on this exact size (passed explicitly, not left implicit)
# closes any gap within a single run instead.
_BARS_PAGE_SIZE = 200
# Safety backstop only. 25 pages * 200 = 5000 trading days (~19 years) --
# every plausible gap closes in 1-2 pages; this only engages if the
# scheduler missed that much history outright, which the fuse/heartbeat/
# backup stack would already have surfaced loudly long before.
_MAX_BACKFILL_PAGES = 25


def _capture_equity(journal, settings, alpaca_client=None) -> tuple[float, str]:
    """Returns (equity, source). Tries the real broker-reported paper equity
    first; falls back to the static PAPER_EQUITY config constant on any
    failure (mock mode, no keys, network error, SDK missing) -- never raises,
    always logs a WARNING on the fallback path so a persistent broker-read
    failure is visible, not silently invisible.

    ``alpaca_client`` is injectable (a fake in tests, matching the
    ``trading_client=`` injection AlpacaClient itself already supports) --
    production call sites always omit it and get a real client."""
    if not settings.is_mock and settings.has_alpaca_keys:
        try:
            client = alpaca_client if alpaca_client is not None else AlpacaClient(settings, journal)
            account = client.get_account()
            equity = account.get("equity")
            if equity is not None:
                return float(equity), "live_broker"
        except Exception as exc:  # noqa: BLE001 - fail safe to the static config fallback
            journal.log_system_event(
                Severity.WARNING, "benchmark_spine",
                f"live equity read failed; falling back to static PAPER_EQUITY: {exc}",
            )
    return float(settings.paper_equity), "static_config"


def _backfill_benchmark_bars(journal, settings, symbol: str, market_dt: date, bars_provider=None) -> int:
    """Fetch + insert any SPY daily bars between the last cached date (or
    ``INITIAL_BENCHMARK_LOOKBACK_DAYS`` back, on the very first run) and
    today. Returns the count actually written (0 in mock/offline mode, or if
    already up to date, or on a provider-side fetch failure -- all
    indistinguishable non-errors to the caller by design; the
    system_events WARNING is where a real operator would notice a
    persistent gap).

    Pages through the range ``_BARS_PAGE_SIZE`` bars at a time so a gap
    larger than one page still closes fully within a single run (see module
    docstring for ``_BARS_PAGE_SIZE``/``_MAX_BACKFILL_PAGES``).

    ``bars_provider`` is injectable (a fake in tests); production call sites
    always omit it and get ``make_bars_provider``'s real result."""
    provider = bars_provider if bars_provider is not None else make_bars_provider(settings, journal)
    if provider is None:
        return 0  # mock/offline -- nothing to fetch against, not a failure

    last = journal.one(
        "SELECT MAX(bar_date) AS d FROM benchmark_bars WHERE symbol = ?", (symbol,)
    )
    if last and last.get("d"):
        start = date.fromisoformat(last["d"]) + timedelta(days=1)
    else:
        start = market_dt - timedelta(days=INITIAL_BENCHMARK_LOOKBACK_DAYS)

    if start > market_dt:
        return 0  # already up to date (e.g. a second run the same day)

    written = 0
    for _ in range(_MAX_BACKFILL_PAGES):
        bars = provider.get_daily_bars(symbol, start.isoformat(), market_dt.isoformat(), limit=_BARS_PAGE_SIZE)
        if not bars:
            break  # nothing more in range (provider failure or genuinely caught up)

        newest = start
        for bar in bars:
            bar_date = bar.get("date")
            if not bar_date:
                continue
            try:
                journal.insert("benchmark_bars", {
                    "bar_id": new_id("bar"),
                    "symbol": symbol,
                    "bar_date": bar_date,
                    "open": bar.get("open"),
                    "high": bar.get("high"),
                    "low": bar.get("low"),
                    "close": bar.get("close"),
                    "volume": bar.get("volume"),
                })
                written += 1
            except sqlite3.IntegrityError:
                pass  # idx_benchmark_bars_symbol_date backstop -- already have this date
            parsed = date.fromisoformat(bar_date)
            if parsed > newest:
                newest = parsed

        if newest >= market_dt or len(bars) < _BARS_PAGE_SIZE:
            break  # caught up, or a short page means nothing further is available
        start = newest + timedelta(days=1)
    else:
        journal.log_system_event(
            Severity.WARNING, "benchmark_spine",
            f"{symbol} backfill hit the {_MAX_BACKFILL_PAGES}-page safety cap before "
            f"reaching {market_dt.isoformat()}; remaining gap will close on a later run",
        )

    return written


def capture_benchmark_spine(
    journal, settings, now: Optional[object] = None, alpaca_client=None, bars_provider=None,
) -> dict:
    """Idempotent daily capture. Safe to call more than once on the same
    market date (equity: no-ops if already captured; bars: only fetches the
    gap since the last cached date). Never raises.

    ``alpaca_client``/``bars_provider`` are test-only injection points;
    production call sites (the scheduler job, the CLI) always omit them."""
    market_dt = timeutils.market_date(now)
    result: dict = {
        "market_date": market_dt.isoformat(),
        "equity_snapshot": None,
        "benchmark_bars_written": 0,
        "warnings": [],
    }

    try:
        existing = journal.one(
            "SELECT 1 FROM equity_snapshots WHERE market_date = ?", (market_dt.isoformat(),)
        )
        if not existing:
            equity, source = _capture_equity(journal, settings, alpaca_client=alpaca_client)
            journal.insert("equity_snapshots", {
                "snapshot_id": new_id("eqsnap"),
                "market_date": market_dt.isoformat(),
                "equity": equity,
                "equity_source": source,
                "is_mock": settings.is_mock,
            })
            result["equity_snapshot"] = {"equity": equity, "source": source}
    except Exception as exc:  # noqa: BLE001 - never let equity capture crash the caller
        msg = f"equity snapshot failed: {exc}"
        result["warnings"].append(msg)
        try:
            journal.log_system_event(Severity.WARNING, "benchmark_spine", msg)
        except Exception:  # noqa: BLE001 - best-effort logging must not itself crash
            pass

    try:
        result["benchmark_bars_written"] = _backfill_benchmark_bars(
            journal, settings, BENCHMARK_SYMBOL, market_dt, bars_provider=bars_provider,
        )
    except Exception as exc:  # noqa: BLE001 - never let the bars fetch crash the caller
        msg = f"benchmark bars backfill failed: {exc}"
        result["warnings"].append(msg)
        try:
            journal.log_system_event(Severity.WARNING, "benchmark_spine", msg)
        except Exception:  # noqa: BLE001 - best-effort logging must not itself crash
            pass

    return result
