"""INSTR-1 part 2: ATR(14) capture -- the daily write side of ATR-scaled
stops. Write-only: computes ATR(14) once per symbol per trading day from
daily-bar history fetched via the EXISTING ``AlpacaBarsProvider.get_daily_bars``
(no new provider/client code -- that function already exists, it was simply
never called from a live/scheduled path before tonight; see
``docs/roadmap/ported`` decision log). Stores results in ``atr_history``,
read ONLY by ``OpenAIClient``'s live-only stop override
(``alphaos/ai/openai_client.py``) -- never by any gate/risk/execution path
directly.

Deliberately a ONCE-DAILY job, not a live per-scan fetch: ATR only changes
once a day (it is built from completed daily bars), so fetching it on every
scan window would 3x the new API load for zero benefit. Mirrors
``benchmark_capture.py``'s own cadence and isolation pattern exactly.

Scoped to the CORE-BOOK universe only (``DEFAULT_UNIVERSE``) -- the shadow
tier structurally never reaches the evaluator/proposal path (EXP-0's own
guarantee: no AI calls, no proposals), so it has no use for ATR data;
fetching it there would be new API load with no behavioral effect, exactly
what this design otherwise avoids.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from alphaos.constants import Severity
from alphaos.data.atr import ATR_PERIOD, ATR_RULES_V1, compute_atr
from alphaos.data.providers.alpaca_bars import make_bars_provider
from alphaos.scanner.candidate_scanner import DEFAULT_UNIVERSE
from alphaos.util import timeutils
from alphaos.util.ids import new_id

# ATR(14) needs 15 daily bars; padding calendar days absorbs weekends/
# holidays without needing a trading calendar (mirrors benchmark_capture's
# own "fetch a few pages of padding, let the provider return what exists"
# approach, at a much smaller scale since this needs weeks not years).
_LOOKBACK_CALENDAR_DAYS = 30
_BARS_LIMIT = 60


def _update_atr_for_symbol(journal, provider, symbol: str, market_dt: date) -> bool:
    """Returns True iff a NEW row was written (idempotent per (symbol, date,
    rules_version) -- the unique index is the real backstop, this check is
    just to skip an unnecessary fetch on a same-day re-run)."""
    existing = journal.one(
        "SELECT 1 FROM atr_history WHERE symbol = ? AND market_date = ? AND rules_version = ?",
        (symbol, market_dt.isoformat(), ATR_RULES_V1),
    )
    if existing:
        return False

    start = market_dt - timedelta(days=_LOOKBACK_CALENDAR_DAYS)
    bars = provider.get_daily_bars(symbol, start.isoformat(), market_dt.isoformat(), limit=_BARS_LIMIT)
    atr = compute_atr([
        {"high": b.get("high"), "low": b.get("low"), "close": b.get("close")} for b in (bars or [])
    ])
    if atr is None:
        # Scope/safety audit finding: an exception-raising provider already
        # logs a WARNING below (the caller's except block); an insufficient-
        # bars result (a thin/sparse feed on this symbol -- the free IEX
        # tier's own sparsity, per EXP-0's spec) previously logged NOTHING
        # at all, so a persistent per-symbol gap was completely invisible.
        # Every future PROPOSE for this symbol will now fail-safe-reject
        # (NO_ATR_DATA) -- this line is what makes that visible in
        # system_events rather than silently accumulating forever.
        journal.log_system_event(
            Severity.INFO, "atr_update",
            f"{symbol}: insufficient daily-bar history for ATR({ATR_PERIOD}) -- "
            f"{len(bars or [])} bars fetched, none written.",
        )
        return False

    journal.insert("atr_history", {
        "atr_id": new_id("atr"),
        "symbol": symbol,
        "market_date": market_dt.isoformat(),
        "atr_14": round(atr, 4),
        "rules_version": ATR_RULES_V1,
        "n_bars_fetched": len(bars or []),
    })
    return True


def update_atr_history(
    journal, settings, symbols: Optional[list[str]] = None,
    now: Optional[datetime] = None, bars_provider=None,
) -> dict:
    """Idempotent daily ATR capture over ``symbols`` (default: the core-book
    universe). Never raises -- one symbol's fetch failure is isolated and
    logged, never aborts the rest of the run (this codebase's own per-item
    isolation law).

    ``bars_provider`` is a test-only injection point; production call sites
    (the scheduler job, the CLI) always omit it and get
    ``make_bars_provider``'s real result (``None`` in mock/offline mode --
    nothing to fetch against, not a failure)."""
    market_dt = timeutils.market_date(now)
    symbols = symbols if symbols is not None else DEFAULT_UNIVERSE
    result: dict = {
        "market_date": market_dt.isoformat(), "n_written": 0,
        "n_symbols": len(symbols), "warnings": [],
    }

    provider = bars_provider if bars_provider is not None else make_bars_provider(settings, journal)
    if provider is None:
        return result

    for symbol in symbols:
        try:
            if _update_atr_for_symbol(journal, provider, symbol, market_dt):
                result["n_written"] += 1
        except Exception as exc:  # noqa: BLE001 - one symbol's failure must never abort the run
            msg = f"{symbol}: ATR update failed: {exc}"
            result["warnings"].append(msg)
            try:
                journal.log_system_event(Severity.WARNING, "atr_update", msg)
            except Exception:  # noqa: BLE001 - best-effort logging must not itself crash
                pass

    return result
