"""EARN-1: raw urllib + CSV client for Alpha Vantage's EARNINGS_CALENDAR
endpoint (the vendor choice -- see alphaos-pr-implementation-specs.md's
EARN-1 section; free tier, 25 requests/day, one call/day needed). Same
"no SDK, stdlib only" pattern as alphaos/data/providers/alpaca_bars.py and
alphaos/text_archive/sec_edgar.py.

Fails safe to ``None`` on any error -- never raises, and never returns a
fabricated empty-but-successful result: ``None`` means "couldn't fetch";
an empty list would wrongly imply "fetched successfully, zero upcoming
earnings anywhere in the whole market," which is never a real state for
this vendor's own rolling multi-month horizon, so an apparently-empty
response is treated the same as a failure (see ``_parse_csv``).
"""

from __future__ import annotations

import csv
import io
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Optional

from alphaos.constants import Severity

ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co/query"
EARNINGS_CALENDAR_HORIZON = "3month"


def fetch_earnings_calendar(settings, journal=None) -> Optional[list[dict]]:
    """ONE HTTP call returns the vendor's entire forward-looking calendar
    (not per-symbol) -- the whole point of the once-daily capture design,
    see earnings_calendar_service.py. Returns a list of ``{"symbol",
    "company_name", "report_date", "fiscal_date_ending", "estimate_eps",
    "currency", "timing"}`` dicts, or ``None`` on any failure (missing key,
    network error, malformed/error response)."""
    if not settings.has_alpha_vantage_key:
        _log(journal, "No Alpha Vantage API key configured; earnings calendar unavailable.")
        return None

    # NEVER log/interpolate this `url` variable (or anything derived from it,
    # e.g. a caught exception's own `.url`/`.filename` attributes) -- it
    # embeds apikey as a plaintext query parameter (2026-07-09 scope/safety
    # audit LOW finding). The exception handler below logs only str(exc),
    # which for urllib.error.URLError/HTTPError never includes the request
    # URL -- keep it that way; if a future edit ever needs to log request
    # details, redact the apikey param first, never pass `url` through as-is.
    query = urllib.parse.urlencode({
        "function": "EARNINGS_CALENDAR",
        "horizon": EARNINGS_CALENDAR_HORIZON,
        "apikey": settings.alpha_vantage_api_key,
    })
    url = f"{ALPHA_VANTAGE_BASE_URL}?{query}"
    try:
        # No explicit Accept header: this endpoint's default response IS
        # CSV, and an explicit "Accept: text/csv" was empirically found to
        # trip the vendor's server into a 406 Not Acceptable (verified
        # against the real API during EARN-1's build) -- asking for
        # anything makes it refuse, asking for nothing gets the right shape.
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=settings.earnings_proximity_timeout_seconds) as resp:
            # utf-8-sig (not utf-8): tolerates a leading UTF-8 BOM, which
            # would otherwise corrupt the first fieldname ("﻿symbol")
            # and make the CSV-shape check below fail-safe-reject a
            # genuinely valid response. Behaves identically to utf-8 when no
            # BOM is present (the vendor doesn't send one today).
            body = resp.read().decode("utf-8-sig", errors="replace")
    except (urllib.error.URLError, ValueError) as exc:
        _log(journal, f"Earnings calendar fetch failed: {exc}")
        return None

    return _parse_csv(body, journal)


def _parse_csv(body: str, journal=None) -> Optional[list[dict]]:
    try:
        reader = csv.DictReader(io.StringIO(body))
    except csv.Error as exc:
        _log(journal, f"Earnings calendar CSV parse failed: {exc}")
        return None

    if not reader.fieldnames or "symbol" not in reader.fieldnames or "reportDate" not in reader.fieldnames:
        # An error/rate-limit response comes back as JSON or a short text
        # message, not this CSV shape -- never silently parse that as "zero
        # rows" (unknown != safe).
        _log(
            journal,
            f"Earnings calendar response doesn't look like the expected CSV "
            f"(fieldnames={reader.fieldnames!r}); treating as unavailable.",
        )
        return None

    rows: list[dict] = []
    try:
        for row in reader:
            symbol = (row.get("symbol") or "").strip()
            report_date = (row.get("reportDate") or "").strip()
            if not symbol or not report_date:
                continue  # malformed row -- skip it, never fabricate a placeholder
            try:
                date.fromisoformat(report_date)
            except ValueError:
                continue  # non-ISO reportDate -- skip rather than cache an unparseable date
            rows.append({
                "symbol": symbol,
                "company_name": (row.get("name") or "").strip() or None,
                "report_date": report_date,
                "fiscal_date_ending": (row.get("fiscalDateEnding") or "").strip() or None,
                "estimate_eps": _parse_float(row.get("estimate")),
                "currency": (row.get("currency") or "").strip() or None,
                "timing": (row.get("timeOfTheDay") or "").strip() or None,
            })
    except (csv.Error, UnicodeDecodeError) as exc:
        _log(journal, f"Earnings calendar CSV parse failed mid-stream: {exc}")
        return None

    if not rows:
        # A genuinely empty result is not a real state for a rolling
        # multi-month market-wide calendar -- treat as unavailable rather
        # than silently caching "nothing" (unknown != safe).
        _log(journal, "Earnings calendar response parsed but yielded zero rows; treating as unavailable.")
        return None
    return rows


def _parse_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _log(journal, message: str) -> None:
    if journal is not None:
        try:
            journal.log_system_event(Severity.WARNING, "earnings_calendar", message)
        except Exception:  # noqa: BLE001 - best-effort logging must not itself crash
            pass
