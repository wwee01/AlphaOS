"""Explicit bounded GET-only inputs for the cash-basis economic bridge.

No scheduler, ledger, broker mutation, paid-feed fallback or strategy activation.
The retained opening capture is read unchanged. Output is an exclusive 0600 file.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
import os
from pathlib import Path
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from alphaos.config.settings import ALPACA_DATA_BASE_URL, PAPER_BASE_URL
from alphaos.reports.common_valuation import (
    _dt,
    collect_common_valuation,
    evidence_hash,
)
from alphaos.reports.common_valuation_source import CommonValuationSource


class EconomicSource(CommonValuationSource):
    MAX_CALLS = 37  # prior collector's 27 + at most ten corporate-action pages

    def __init__(self, settings, *, activity_start, transport=None, clock=None):
        if (
            not settings.is_paper
            or not settings.alpaca_paper
            or settings.real_trading_enabled_raw != "false"
        ):
            raise ValueError("economic capture requires explicit paper-only settings")
        super().__init__(settings, transport=transport, clock=clock)
        self.activity_start = _dt(activity_start)
        self.partial = {}
        self.failed_stage = None

    def _http_get(self, base, path, params):
        allowed = {
            PAPER_BASE_URL: {
                "/v2/account",
                "/v2/positions",
                "/v2/calendar",
                "/v2/account/activities",
            },
            ALPACA_DATA_BASE_URL: {"/v2/stocks/bars", "/v1/corporate-actions"},
        }
        if path not in allowed.get(base, set()):
            raise ValueError("endpoint outside economic GET allowlist")

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise ValueError("economic GET redirect refused")

        url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "APCA-API-KEY-ID": self.settings.alpaca_api_key,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.build_opener(NoRedirect()).open(
                request, timeout=15
            ) as response:
                return json.load(response)
        except (urllib.error.URLError, ValueError):
            raise ValueError("economic GET unavailable; raw error redacted") from None

    def _get(self, path, params=None, *, data=False):
        self.failed_stage = path
        part = super()._get(path, params, data=data)
        # Store only normalized method results, never raw account responses.
        self.partial.setdefault("completed_requests", []).append(
            {
                "path": path,
                "requested_at_utc": part["requested_at_utc"],
                "received_at_utc": part["received_at_utc"],
            }
        )
        return part

    def account(self):
        part = super().account()
        self.partial.setdefault("accounts", []).append(part)
        return part

    def positions(self):
        part = super().positions()
        self.partial.setdefault("positions", []).append(part)
        return part

    def activities(self, start):
        part = super().activities(min(_dt(start), self.activity_start))
        # Descriptions can contain account numbers. Keep accounting fields only.
        allowed = {
            "id",
            "activity_type",
            "activity_sub_type",
            "created_at",
            "date",
            "status",
            "transaction_time",
            "order_id",
            "symbol",
            "qty",
            "price",
            "side",
            "type",
            "cum_qty",
            "leaves_qty",
            "order_status",
            "currency",
            "net_amount",
            "per_share_amount",
        }
        part["rows"] = [
            {k: v for k, v in row.items() if k in allowed} for row in part["rows"]
        ]
        self.partial.setdefault("activities", []).append(part)
        return part

    def final_minute_bars(self, symbols, close_at):
        part = super().final_minute_bars(symbols, close_at)
        self.partial["bars"] = part
        return part

    def corporate_actions(self, symbols, start, end):
        request: dict[str, Any] = {
            "symbols": sorted(symbols),
            "start": str(start),
            "end": str(end),
            "data_quality": "all",
            "sort": "asc",
            "limit": 1000,
        }
        params = {**request, "symbols": ",".join(request["symbols"])}
        pages: list[dict] = []
        rows: dict[str, list] = {}
        tokens: set[str] = set()
        for _ in range(self.MAX_PAGES):
            self.failed_stage = "/v1/corporate-actions"
            self.calls += 1
            if self.calls > self.MAX_CALLS:
                raise ValueError("economic capture request cap reached")
            before = self.now()
            value = self.transport(
                ALPACA_DATA_BASE_URL, "/v1/corporate-actions", params
            )
            after = self.now()
            if not isinstance(value, dict) or not isinstance(
                value.get("corporate_actions"), dict
            ):
                raise ValueError("corporate-action response malformed")
            for kind, values in value["corporate_actions"].items():
                if not isinstance(values, list) or any(
                    not isinstance(v, dict) for v in values
                ):
                    raise ValueError("corporate-action response malformed")
                rows.setdefault(kind, []).extend(values)
            token = value.get("next_page_token")
            pages.append(
                {
                    "requested_at_utc": before.isoformat(),
                    "received_at_utc": after.isoformat(),
                    "request": dict(params),
                    "next_page_token": token,
                }
            )
            self.partial["corporate_actions"] = {
                "pages": pages,
                "rows": rows,
                "pagination_exhausted": False,
            }
            if not token:
                return {
                    "request": request,
                    "rows": rows,
                    "pages": pages,
                    "pagination_exhausted": True,
                    "requested_at_utc": pages[0]["requested_at_utc"],
                    "received_at_utc": after.isoformat(),
                }
            if not isinstance(token, str) or token in tokens:
                raise ValueError("corporate-action pagination token invalid")
            tokens.add(token)
            params["page_token"] = token
        raise ValueError("corporate-action pagination cap reached")


def collect_economic_inputs(source, start_capture):
    """Retain the opening receipt and capture one common closing endpoint."""
    end_capture = collect_common_valuation(source)
    a, b = start_capture["raw_evidence"], end_capture["raw_evidence"]
    symbols = {"SPY"}
    for bundle in (a, b):
        symbols.update(row["symbol"] for row in bundle["positions_after"]["value"])
        symbols.update(
            row["symbol"]
            for row in bundle["activities_after"]["rows"]
            if row.get("activity_type") == "FILL"
        )
    # This API filters by process_date, not ex/pay date. Retain a wide explicit
    # window and disclose late/missing announcements; it is not a completeness certificate.
    start = date.fromisoformat(a["session"]["date"]) - timedelta(days=366)
    end = date.fromisoformat(b["session"]["date"]) + timedelta(days=366)
    actions = source.corporate_actions(symbols, start, end)
    return {
        "schema": "cash_basis_economic_inputs_v1",
        "start_capture": start_capture,
        "end_capture": end_capture,
        "corporate_actions": actions,
        "captured_at_utc": source.now().isoformat(),
        "get_count": source.calls,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--collect", action="store_true", required=True)
    args = parser.parse_args(argv)
    if args.output.exists() or not args.output.parent.is_dir():
        parser.error("output must be a new file in an existing directory")
    # Validate the archive before credentials are loaded or any GET is sent.
    start = json.loads(args.start.read_text())
    if start.get("input_sha256") != evidence_hash(start["raw_evidence"]):
        parser.error("opening capture hash mismatch")
    from alphaos.config.settings import load_settings

    source = EconomicSource(
        load_settings(),
        activity_start=start["raw_evidence"]["activities_after"]["request"]["after"],
    )
    try:
        result = collect_economic_inputs(source, start)
        status = 0
    except Exception as exc:
        result = {
            "status": "capture_failed",
            "error_type": type(exc).__name__,
            "get_count": source.calls,
            "start_capture": start,
            "partial_evidence": source.partial,
            "failed_stage": source.failed_stage,
        }
        status = 2
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(result, handle, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")
    print(
        f"Economic input capture {'complete' if status == 0 else 'incomplete'}; {source.calls} GET requests."
    )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
