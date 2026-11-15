"""Bounded local observer receipts, never an API client or calendar fallback."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from zoneinfo import ZoneInfo

from alphaos.research import swing_data_policy as v2
from alphaos.util import timeutils

LEASE_SECONDS = 900
MAX_REPORT_BYTES = 2_000_000
MAX_REPORTS_PER_DATE = 650


def private_directory(path: Path) -> None:
    info = path.lstat()
    if (
        path.absolute() != path.resolve()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("private_directory_required")


def private_bytes(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > limit
        ):
            raise ValueError("bounded_private_regular_file_required")
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("file_too_large")
        return raw


def load_catalog(directory: Path, date: str) -> list[dict]:
    private_directory(directory)
    day = datetime.strptime(date, "%Y-%m-%d")
    paths = sorted(
        p
        for offset in (0, 1)
        for p in directory.glob(
            "paper-observer-poll-"
            + (day + timedelta(days=offset)).strftime("%Y%m%d")
            + "T*.json"
        )
    )
    if len(paths) > MAX_REPORTS_PER_DATE:
        raise ValueError("observer_catalog_limit")
    result = []
    for path in paths:
        match = re.fullmatch(
            r"paper-observer-poll-(\d{8}T\d{12}Z)-\d+\.json", path.name
        )
        if not match:
            raise ValueError("invalid_observer_filename")
        written = datetime.strptime(match[1], "%Y%m%dT%H%M%S%fZ").replace(
            tzinfo=timezone.utc
        )
        if timeutils.to_et(written).date().isoformat() != date:
            continue
        # Failed/partial reports remain visible; do not silently select an older
        # healthy report when a newer applicable observer result is unusable.
        item: dict = {"written_at_utc": written.isoformat(), "error": None}
        try:
            raw = private_bytes(path, MAX_REPORT_BYTES)
            # Filename time precedes serialization. Never treat a receipt as
            # available before the file's final observed write time.
            whole, nanos = divmod(path.lstat().st_mtime_ns, 1_000_000_000)
            file_written = (
                datetime.fromtimestamp(whole, timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S"
                )
                + f".{nanos:09d}Z"
            )
            file_stamp, name_stamp = (
                v2._stamp(file_written),
                v2._stamp(written.isoformat()),
            )
            assert file_stamp is not None and name_stamp is not None
            item["written_at_utc"] = (
                file_written if file_stamp[1] > name_stamp[1] else written.isoformat()
            )
            report = json.loads(raw)
            item.update(
                {
                    k: report.get(k)
                    for k in (
                        "version",
                        "broker_verified",
                        "observed_at_utc",
                        "completed_at_utc",
                        "market_calendar",
                    )
                }
            )
            item["report_sha256"] = sha256(raw).hexdigest()
            item["receipts"] = [
                {
                    k: x.get(k)
                    for k in (
                        "path",
                        "params",
                        "http_status",
                        "body_sha256",
                        "request_at_utc",
                        "received_at_utc",
                    )
                }
                for x in report.get("broker_receipts", [])
                if isinstance(x, dict) and x.get("path") == "/v2/calendar"
            ]
        except (ValueError, OSError, TypeError, AttributeError):
            item["error"] = "observer_report_invalid"
        result.append(item)
    return result


def bounds_at(
    catalog: list[dict], date: str, assessed_at: str
) -> tuple[dict | None, str]:
    assessment = v2._stamp(assessed_at)
    if assessment is None:
        return None, "assessment_unverified"
    eligible = []
    for record in catalog:
        stamp = v2._stamp(record.get("written_at_utc"))
        if stamp is not None and stamp[1] <= assessment[1]:
            eligible.append((stamp[1], record))
    if not eligible:
        return None, "calendar_missing_at_assessment"
    newest = max(pair[0] for pair in eligible)
    latest = [record for stamp, record in eligible if stamp == newest]
    if (
        len(
            {sha256(json.dumps(r, sort_keys=True).encode()).hexdigest() for r in latest}
        )
        != 1
    ):
        return None, "calendar_conflicting_latest"
    report = latest[0]
    if (
        report.get("error")
        or report.get("version") != "paper_observer_v1"
        or report.get("broker_verified") is not True
    ):
        return None, "newest_observer_unverified"
    hours = v2._mapping(report.get("market_calendar"))
    receipts = report.get("receipts")
    if (
        hours.get("date") != date
        or hours.get("source") != "broker_calendar"
        or not isinstance(receipts, list)
        or len(receipts) != 1
    ):
        return None, "calendar_identity_unverified"
    receipt = receipts[0]
    if (
        receipt.get("path") != "/v2/calendar"
        or receipt.get("http_status") != 200
        or receipt.get("params") != {"start": date, "end": date}
        or any(
            not isinstance(x, str) or not re.fullmatch(r"[a-f0-9]{64}", x)
            for x in (receipt.get("body_sha256"), report.get("report_sha256"))
        )
    ):
        return None, "calendar_receipt_unverified"
    stamps = [
        v2._stamp(x)
        for x in (
            report.get("observed_at_utc"),
            receipt.get("request_at_utc"),
            receipt.get("received_at_utc"),
            report.get("completed_at_utc"),
            report.get("written_at_utc"),
        )
    ]
    if any(x is None for x in stamps):
        return None, "calendar_clock_unverified"
    seconds = [x[1] for x in stamps if x is not None]
    if seconds != sorted(seconds) or seconds[-1] > assessment[1]:
        return None, "calendar_clock_order"
    if assessment[1] - seconds[2] > LEASE_SECONDS:
        return None, "calendar_receipt_stale"
    request_stamp = stamps[1]
    assert request_stamp is not None
    if timeutils.to_et(request_stamp[0]).date().isoformat() != date:
        return None, "calendar_request_wrong_date"
    if hours.get("open") is None and hours.get("close") is None:
        return None, "verified_closed_day"
    try:
        values = [hours[k] for k in ("open", "close")]
        if any(
            not isinstance(x, str) or not re.fullmatch(r"\d{2}:\d{2}", x)
            for x in values
        ):
            raise ValueError("bound syntax")
        if values[0] != "09:30" or not "09:30" < values[1] <= "16:00":
            raise ValueError("bound range")
        opening, closing = [
            datetime.strptime(date + "T" + x, "%Y-%m-%dT%H:%M").replace(
                tzinfo=ZoneInfo("America/New_York")
            )
            for x in values
        ]
        if timeutils.market_session(opening).value != "regular":
            raise ValueError("shared calendar conflict")
    except (ValueError, TypeError, KeyError):
        return None, "calendar_bounds_unverified"
    evidence = {
        "date": date,
        "open_at_utc": opening.astimezone(timezone.utc).isoformat(),
        "close_at_utc": closing.astimezone(timezone.utc).isoformat(),
        "received_at_utc": receipt["received_at_utc"],
        "available_at_utc": report["written_at_utc"],
        "report_sha256": report["report_sha256"],
        "response_sha256": receipt["body_sha256"],
        "lease_seconds": LEASE_SECONDS,
        "hash_meaning": "trusted_retained_lineage_not_independent_body_authentication",
    }
    return evidence, "verified"
