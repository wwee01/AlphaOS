"""EARN-1: earnings-calendar capture -- the daily write side of the live
earnings-proximity provider. Write-only: fetches the ENTIRE forward-looking
calendar in ONE call (alphaos.earnings.alpha_vantage_client.
fetch_earnings_calendar) and journals every new/revised (symbol,
report_date, fiscal_date_ending) triple into ``earnings_calendar_cache``,
read ONLY by ``AlphaVantageEarningsProvider``'s live per-symbol lookup
(``alphaos/earnings/earnings_provider.py``) -- never by any gate/risk/
execution path directly.

Deliberately a ONCE-DAILY job, not a live per-scan fetch: the vendor's free
tier is 25 requests/day and a single call already returns the full
multi-month calendar, so fetching more often would burn budget for zero
new information (the same "once-daily, no new pipeline" discipline as
INSTR-1's ATR capture and PR9.5's benchmark spine).

Append-only, not upsert: a (symbol, report_date, fiscal_date_ending)
triple already seen is never rewritten -- only a genuinely NEW triple (a
new fiscal period, or the SAME fiscal period's report_date having shifted)
adds a row. This is a point-in-time record (TEXT-0's own seen_at law:
``created_at_utc``, auto-stamped by ``JournalStore.insert()``, is the ONLY
field any future backtest may condition on -- never ``report_date`` itself,
which is the event date, not the discovery date).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from alphaos.constants import Severity
from alphaos.earnings.alpha_vantage_client import fetch_earnings_calendar
from alphaos.util import timeutils
from alphaos.util.ids import new_id


def _write_entry(journal, row: dict, source: str) -> bool:
    """Returns True iff a NEW row was written (idempotent per (symbol,
    report_date, fiscal_date_ending) -- the unique index is the real
    backstop, this check is just to skip an unnecessary insert attempt on a
    same-day or later re-run)."""
    existing = journal.one(
        "SELECT 1 FROM earnings_calendar_cache WHERE symbol = ? AND report_date = ? "
        "AND fiscal_date_ending IS ?",
        (row["symbol"], row["report_date"], row.get("fiscal_date_ending")),
    )
    if existing:
        return False

    journal.insert("earnings_calendar_cache", {
        "entry_id": new_id("earncal"),
        "symbol": row["symbol"],
        "company_name": row.get("company_name"),
        "report_date": row["report_date"],
        "fiscal_date_ending": row.get("fiscal_date_ending"),
        "estimate_eps": row.get("estimate_eps"),
        "currency": row.get("currency"),
        "timing": row.get("timing"),
        "source": source,
    })
    return True


def update_earnings_calendar(
    journal, settings, now: Optional[datetime] = None, fetch_fn=None,
) -> dict:
    """Idempotent daily earnings-calendar capture. Never raises -- one row's
    write failure is isolated and logged, never aborts the rest of the run
    (this codebase's own per-item isolation law).

    ``fetch_fn`` is a test-only injection point (mirrors
    ``atr_service.update_atr_history``'s own ``bars_provider`` parameter);
    production call sites (the scheduler job, the CLI) always omit it and
    get ``fetch_earnings_calendar``'s real result.
    """
    market_dt = timeutils.market_date(now)
    result: dict = {
        "market_date": market_dt.isoformat(), "n_fetched": 0, "n_written": 0, "warnings": [],
    }

    fetch = fetch_fn if fetch_fn is not None else fetch_earnings_calendar
    rows = fetch(settings, journal)
    if rows is None:
        journal.log_system_event(
            Severity.WARNING, "earnings_calendar",
            "earnings calendar fetch returned no usable data; nothing written this run.",
        )
        return result

    result["n_fetched"] = len(rows)
    for row in rows:
        try:
            if _write_entry(journal, row, "alpha_vantage"):
                result["n_written"] += 1
        except Exception as exc:  # noqa: BLE001 - one row's failure must never abort the run
            msg = f"{row.get('symbol')}: earnings_calendar_cache write failed: {exc}"
            result["warnings"].append(msg)
            try:
                journal.log_system_event(Severity.WARNING, "earnings_calendar", msg)
            except Exception:  # noqa: BLE001 - best-effort logging must not itself crash
                pass

    return result
