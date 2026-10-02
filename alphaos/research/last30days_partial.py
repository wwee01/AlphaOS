"""Bounded item-level receipts from returned, normalized retrievals.

Structural source evidence only: no posts, titles, URLs, prompts or narrative.
This never produces a completed research result or a model-facing context.
Stdlib only, for the same isolated external interpreter as the worker.
"""

from __future__ import annotations

from datetime import datetime, timezone
import functools
import hashlib
import json
import math
import os
import re
import threading
import time

VERSION = "last30days_partial_items_v1"
MAX_BYTES = 131072
MAX_CALLS = 64
MAX_ITEMS = 128
MAX_PER_CALL = 24
ENV_KEYS = (
    "ALPHAOS_L30_PARTIAL_FD",
    "ALPHAOS_L30_NONCE",
    "ALPHAOS_L30_START_UTC",
    "ALPHAOS_L30_START_MONOTONIC",
    "ALPHAOS_L30_DEADLINE",
)
ENGAGEMENT = frozenset(
    {
        "score",
        "upvotes",
        "downvotes",
        "num_comments",
        "comments",
        "likes",
        "views",
        "view_count",
        "replies",
        "retweets",
        "shares",
        "stars",
        "forks",
        "watchers",
        "volume",
        "open_interest",
    }
)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def instant(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("invalid evidence time")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.utcoffset() is None:
        raise ValueError("naive evidence time")
    return dt.astimezone(timezone.utc)


def finite(value, maximum=1e15):
    return (
        type(value) in (int, float) and math.isfinite(value) and abs(value) <= maximum
    )


def project(item, source, captured):
    # Do not serialize a whole upstream dataclass: it includes bodies/URLs.
    identity = getattr(item, "item_id", None)
    if (
        not isinstance(identity, str)
        or not identity
        or len(identity) > 1024
        or getattr(item, "source", None) != source
    ):
        raise ValueError("unbound item")
    row = {
        "item_identity_sha256": digest([source, identity]),
        "source": source,
        "published_at_utc": None,
        "published_date": None,
        "date_confidence": getattr(item, "date_confidence", None),
        "engagement": {},
    }
    if row["date_confidence"] not in ("high", "med", "low"):
        row["date_confidence"] = "unknown"
    published = getattr(item, "published_at", None)
    if published:
        if isinstance(published, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", published):
            day = datetime.fromisoformat(published).date()
            if day > captured.date():
                raise ValueError("future publication date")
            row["published_date"] = day.isoformat()
        else:
            when = instant(published)
            if when > captured:
                raise ValueError("future publication time")
            row["published_at_utc"] = when.isoformat()
    values = getattr(item, "engagement", {})
    if not isinstance(values, dict):
        raise ValueError("invalid engagement")
    for key in ENGAGEMENT:
        if key in values:
            if not finite(values[key]):
                raise ValueError("nonfinite engagement")
            row["engagement"][key] = values[key]
    row["projection_sha256"] = digest(row)
    return row


class PartialCapture:
    def __init__(
        self,
        fd,
        nonce,
        requested,
        policy_version,
        started_utc,
        started_monotonic,
        deadline,
    ):
        self.fd, self.nonce = fd, nonce
        self.requested, self.policy_version = sorted(requested), policy_version
        self.started_utc = instant(started_utc)
        self.started, self.deadline = started_monotonic, deadline
        self.lock = threading.Lock()
        self.pending = {}
        self.bytes = self.items = self.calls = 0

    def returned(self, source, raw_items, event):
        if (
            source not in self.requested
            or not isinstance(raw_items, list)
            or event is None
        ):
            return
        with self.lock:
            if len(self.pending) < MAX_CALLS:
                # Retain the object so an unrelated list cannot reuse its id.
                self.pending[id(raw_items)] = (raw_items, source, event)

    def install(self, pipeline):
        original = pipeline._normalize_score_dedupe

        @functools.wraps(original)
        def normalized(source, raw_items, *args, **kwargs):
            result = original(source, raw_items, *args, **kwargs)
            try:
                self.record(source, raw_items, result)
            except Exception:
                # Optional capture must not alter source work or returned JSON.
                pass
            return result

        pipeline._normalize_score_dedupe = normalized

    def record(self, source, raw_items, result):
        with self.lock:
            linked = self.pending.pop(id(raw_items), None)
            if (
                not linked
                or linked[0] is not raw_items
                or linked[1] != source
                or not isinstance(result, list)
                or self.calls >= MAX_CALLS
            ):
                return
            elapsed = time.monotonic() - self.started
            captured = datetime.now(timezone.utc)
            if not 0 <= elapsed <= self.deadline or captured < self.started_utc:
                return
            event = linked[2]
            items: list[dict] = []
            rejected = duplicates = 0
            omitted = max(0, len(result) - MAX_PER_CALL)
            seen = set()
            # Bound projection work as well as bytes; do not walk a huge
            # upstream result merely to count items we cannot retain.
            for item in result[:MAX_PER_CALL]:
                if len(items) >= MAX_PER_CALL or self.items + len(items) >= MAX_ITEMS:
                    omitted += 1
                    continue
                try:
                    row = project(item, source, captured)
                except (ValueError, TypeError, AttributeError, OverflowError):
                    rejected += 1
                    continue
                if row["item_identity_sha256"] in seen:
                    duplicates += 1
                    continue
                seen.add(row["item_identity_sha256"])
                items.append(row)
            record = {
                "version": VERSION,
                "nonce": self.nonce,
                "requested": self.requested,
                "source_policy_version": self.policy_version,
                "source": source,
                "call_id": event["call_id"],
                "stage_return_seconds": event["at_seconds"],
                "captured_seconds": round(elapsed, 6),
                "captured_at_utc": captured.isoformat(),
                "raw_item_count": len(raw_items),
                "normalized_item_count": len(result),
                "rejected_items": rejected,
                "duplicate_items": duplicates,
                "omitted_items": omitted,
                "items": items,
            }
            payload = (
                json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n"
            ).encode()
            if self.bytes + len(payload) > MAX_BYTES:
                return
            written = os.write(self.fd, payload)
            self.bytes += written
            self.calls += 1
            self.items += len(items)


def read_partial(
    stream, *, nonce, requested, policy_version, started_utc, deadline, diagnostics
):
    """Validate private receipts and preserve incomplete/missing denominators.

    A file cap, invalid linkage or malformed record cannot produce trusted items.
    Missing checkpoints remain missing; returned source functions are not overall
    successful source collection, finalized narrative, or official labels.
    """
    result = {
        "version": VERSION,
        "status": "unverified",
        "research_complete": False,
        "model_context_eligible": False,
        "requested_sources": sorted(requested),
        "source_error_coverage": "not_observed_internal_errors_may_be_suppressed",
        "items": [],
        "calls": [],
        "limitations": [],
    }
    stream.seek(0)
    data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        result["limitations"] = ["receipt_byte_limit_exceeded"]
        return result
    policy = diagnostics.get("source_policy", {})
    if (
        diagnostics.get("truncated")
        or diagnostics.get("malformed")
        or policy.get("version") != policy_version
        or policy.get("violations") != 0
        or policy.get("status") not in ("active", "incomplete", "completed")
        or policy.get("requested") != sorted(requested)
        or not set(policy.get("dispatch_counts", {})) <= set(requested)
    ):
        result["limitations"] = ["source_policy_or_telemetry_unverified"]
        return result
    start = instant(started_utc)
    starts: dict[str, dict] = {}
    ends: dict[str, dict] = {}
    try:
        for e in diagnostics["events"]:
            if e.get("stage") != "retrieval" or e["event"] not in (
                "stage_start",
                "stage_end",
            ):
                continue
            target = starts if e["event"] == "stage_start" else ends
            call = e["call_id"]
            if call in target:
                raise ValueError("duplicate stage identity")
            target[call] = e
        used = set()
        item_ids = set()
        rejected = omitted = duplicates = 0
        call_rows = []
        retained = []
        for line in data.splitlines():
            row = json.loads(line)
            if set(row) != {
                "version",
                "nonce",
                "requested",
                "source_policy_version",
                "source",
                "call_id",
                "stage_return_seconds",
                "captured_seconds",
                "captured_at_utc",
                "raw_item_count",
                "normalized_item_count",
                "rejected_items",
                "duplicate_items",
                "omitted_items",
                "items",
            }:
                raise ValueError("invalid receipt shape")
            call, source = row["call_id"], row["source"]
            if (
                row["version"] != VERSION
                or row["nonce"] != nonce
                or row["requested"] != sorted(requested)
                or row["source_policy_version"] != policy_version
                or source not in requested
                or call in used
            ):
                raise ValueError("invalid receipt identity")
            first, last = starts[call], ends[call]
            if (
                first["source"] != source
                or last["source"] != source
                or last.get("outcome") != "returned"
                or row["stage_return_seconds"] != last["at_seconds"]
                or not 0
                <= first["at_seconds"]
                <= last["at_seconds"]
                <= row["captured_seconds"]
                <= deadline
                or not finite(row["captured_seconds"])
            ):
                raise ValueError("unlinked or late receipt")
            captured = instant(row["captured_at_utc"])
            if not 0 <= (captured - start).total_seconds() <= deadline:
                raise ValueError("receipt outside request clock")
            if captured > datetime.now(timezone.utc):
                raise ValueError("future receipt")
            if abs((captured - start).total_seconds() - row["captured_seconds"]) > 0.1:
                raise ValueError("contradictory receipt clock")
            for key in (
                "raw_item_count",
                "normalized_item_count",
                "rejected_items",
                "duplicate_items",
                "omitted_items",
            ):
                if type(row[key]) is not int or not 0 <= row[key] <= 100000:
                    raise ValueError("invalid item count")
            if (
                last.get("item_count") != row["raw_item_count"]
                or not isinstance(row["items"], list)
                or len(row["items"]) > MAX_PER_CALL
                or row["normalized_item_count"] > row["raw_item_count"]
                or row["normalized_item_count"]
                != len(row["items"])
                + row["rejected_items"]
                + row["duplicate_items"]
                + row["omitted_items"]
            ):
                raise ValueError("inconsistent item count")
            for item in row["items"]:
                if set(item) != {
                    "item_identity_sha256",
                    "source",
                    "published_at_utc",
                    "published_date",
                    "date_confidence",
                    "engagement",
                    "projection_sha256",
                }:
                    raise ValueError("invalid item projection")
                if (
                    item["source"] != source
                    or not re.fullmatch("[0-9a-f]{64}", item["item_identity_sha256"])
                    or item["date_confidence"] not in ("high", "med", "low", "unknown")
                    or not isinstance(item["engagement"], dict)
                    or not set(item["engagement"]) <= ENGAGEMENT
                    or not all(finite(v) for v in item["engagement"].values())
                    or item["projection_sha256"]
                    != digest(
                        {k: v for k, v in item.items() if k != "projection_sha256"}
                    )
                ):
                    raise ValueError("unbound item projection")
                if (
                    item["published_at_utc"] is not None
                    and instant(item["published_at_utc"]) > captured
                ):
                    raise ValueError("future publication")
                if item["published_date"] is not None:
                    if not isinstance(item["published_date"], str) or not re.fullmatch(
                        r"\d{4}-\d{2}-\d{2}", item["published_date"]
                    ):
                        raise ValueError("invalid publication date resolution")
                    if (
                        datetime.fromisoformat(item["published_date"]).date()
                        > captured.date()
                    ):
                        raise ValueError("future publication date")
                if (
                    item["published_at_utc"] is not None
                    and item["published_date"] is not None
                ):
                    raise ValueError("ambiguous publication resolution")
                key = item["item_identity_sha256"]
                if key in item_ids:
                    duplicates += 1
                else:
                    freshness = (
                        "publication_time_missing"
                        if item["published_at_utc"] is None
                        and item["published_date"] is None
                        else "publication_date_only"
                        if item["published_date"] is not None
                        else "reported_publication_instant_not_independently_verified"
                    )
                    retained.append(
                        {
                            **item,
                            "call_id": call,
                            "captured_at_utc": row["captured_at_utc"],
                            "freshness_limitation": freshness,
                        }
                    )
                    item_ids.add(key)
            used.add(call)
            call_rows.append({k: v for k, v in row.items() if k != "items"})
            rejected += row["rejected_items"]
            omitted += row["omitted_items"]
            duplicates += row["duplicate_items"]
        if len(used) > MAX_CALLS or len(retained) > MAX_ITEMS:
            raise ValueError("evidence limit exceeded")
        for source in requested:
            observed = sum(e["source"] == source for e in starts.values())
            if observed != policy.get("dispatch_counts", {}).get(source, 0):
                raise ValueError("dispatch receipts incomplete")
        returned = {c for c, e in ends.items() if e.get("outcome") == "returned"}
        result.update(
            status="incomplete_source_evidence"
            if retained
            else "no_retained_source_items",
            nonce=nonce,
            items=retained,
            calls=call_rows,
            retained_unique_item_count=len(retained),
            rejected_item_count=rejected,
            omitted_item_count=omitted,
            duplicate_item_count=duplicates,
            checkpoint_coverage_incomplete=bool(returned - used),
            capture_limits={
                "max_bytes": MAX_BYTES,
                "max_calls": MAX_CALLS,
                "max_items": MAX_ITEMS,
                "max_items_per_call": MAX_PER_CALL,
            },
            returned_calls_without_checkpoint=sorted(returned - used),
            unfinished_calls=sorted(set(starts) - set(ends)),
            errored_calls=sorted(
                c for c, e in ends.items() if e.get("outcome") == "error"
            ),
            not_started_sources=sorted(
                set(requested) - {e["source"] for e in starts.values()}
            ),
            receipt_sha256=hashlib.sha256(data).hexdigest(),
            limitations=[
                "structural_metadata_only_no_narrative",
                "not_full_source_or_research_completion",
                "native_identity_hashed_no_remote_authentication",
                "date_only_publication_is_not_an_instant",
            ],
        )
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        result["limitations"] = ["malformed_or_unbound_partial_receipt"]
    return result
