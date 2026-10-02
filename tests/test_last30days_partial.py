"""Bounded incomplete receipts; no research/model/network operation."""

from datetime import datetime, timedelta, timezone
import io
import json
import time
from types import SimpleNamespace

import pytest

from alphaos.research.last30days_partial import (
    PartialCapture,
    read_partial,
    digest,
    project,
)
from alphaos.research.last30days_source_policy import POLICY_VERSION


def sample(tmp_path):
    started = datetime.now(timezone.utc) - timedelta(seconds=1)
    stream = (tmp_path / "partial").open("w+b")
    cap = PartialCapture(
        stream.fileno(),
        "a" * 32,
        ["hackernews", "reddit"],
        POLICY_VERSION,
        started.isoformat(),
        time.monotonic() - 1,
        45,
    )
    event = {
        "event": "stage_end",
        "stage": "retrieval",
        "source": "hackernews",
        "call_id": "1:2",
        "at_seconds": 0.2,
        "elapsed_seconds": 0.1,
        "outcome": "returned",
        "item_count": 2,
    }
    raw = [{}, {}]
    items = [
        SimpleNamespace(
            item_id="item-a",
            source="hackernews",
            published_at="2026-01-01",
            date_confidence="high",
            engagement={"score": 12, "SECRET": "PRIVATE"},
            title="PRIVATE",
            body="PRIVATE",
            url="https://SECRET",
        ),
        SimpleNamespace(
            item_id="item-b",
            source="hackernews",
            published_at=None,
            date_confidence="low",
            engagement={},
        ),
    ]
    cap.returned("hackernews", raw, event)
    cap.record("hackernews", raw, items)
    diag = {
        "events": [
            {
                "event": "stage_start",
                "stage": "retrieval",
                "source": "hackernews",
                "call_id": "1:2",
                "at_seconds": 0.1,
            },
            event,
            {
                "event": "stage_start",
                "stage": "retrieval",
                "source": "reddit",
                "call_id": "2:3",
                "at_seconds": 0.1,
            },
        ],
        "source_policy": {
            "version": POLICY_VERSION,
            "status": "active",
            "violations": 0,
            "requested": ["hackernews", "reddit"],
            "dispatch_counts": {"hackernews": 1, "reddit": 1},
        },
    }
    args = dict(
        nonce="a" * 32,
        requested=["hackernews", "reddit"],
        policy_version=POLICY_VERSION,
        started_utc=started.isoformat(),
        deadline=45,
        diagnostics=diag,
    )
    stream.seek(0)
    data = stream.read()
    stream.close()
    return data, args


def test_returned_items_retained_without_narrative_or_completion(tmp_path):
    data, args = sample(tmp_path)
    v = read_partial(io.BytesIO(data), **args)
    assert (
        v["status"] == "incomplete_source_evidence"
        and v["retained_unique_item_count"] == 2
    )
    assert (
        v["unfinished_calls"] == ["2:3"]
        and v["returned_calls_without_checkpoint"] == []
    )
    assert not v["research_complete"] and not v["model_context_eligible"]
    assert v["items"][0]["engagement"] == {"score": 12}
    assert v["items"][0]["freshness_limitation"] == "publication_date_only"
    assert v["items"][1]["freshness_limitation"] == "publication_time_missing"
    assert not any(
        s in json.dumps(v) for s in ["PRIVATE", "SECRET", "https://", "item-a"]
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "nonce",
        "source",
        "late",
        "before",
        "future",
        "nan",
        "count",
        "hash",
        "foreign_item",
        "future_publication",
        "date_precision",
        "duplicate_call",
        "unrequested_dispatch",
        "missing_end",
        "truncated_telemetry",
        "source_violation",
    ],
)
def test_unbound_receipts_never_expose_items(tmp_path, mutation):
    data, args = sample(tmp_path)
    row = json.loads(data)
    if mutation == "nonce":
        row["nonce"] = "b" * 32
    elif mutation == "source":
        row["source"] = "jobs"
    elif mutation == "late":
        row["captured_seconds"] = 46
    elif mutation == "before":
        row["captured_at_utc"] = "2025-01-01T00:00:00+00:00"
    elif mutation == "future":
        row["captured_at_utc"] = "2099-01-01T00:00:00+00:00"
    elif mutation == "nan":
        row["captured_seconds"] = float("nan")
    elif mutation == "count":
        row["normalized_item_count"] = 9
    elif mutation == "hash":
        row["items"][0]["projection_sha256"] = "0" * 64
    elif mutation == "foreign_item":
        row["items"][0]["source"] = "jobs"
    elif mutation == "future_publication":
        row["items"][0]["published_date"] = "2099-01-01"
        row["items"][0]["projection_sha256"] = digest(
            {k: v for k, v in row["items"][0].items() if k != "projection_sha256"}
        )
    elif mutation == "date_precision":
        row["items"][0]["published_date"] = "2026-01-01T00:00:00"
        row["items"][0]["projection_sha256"] = digest(
            {k: v for k, v in row["items"][0].items() if k != "projection_sha256"}
        )
    elif mutation == "unrequested_dispatch":
        args["diagnostics"]["source_policy"]["dispatch_counts"]["jobs"] = 1
    elif mutation == "missing_end":
        args["diagnostics"]["events"].pop(1)
    elif mutation == "truncated_telemetry":
        args["diagnostics"]["truncated"] = True
    elif mutation == "source_violation":
        args["diagnostics"]["source_policy"]["violations"] = 1
    data = (json.dumps(row) + "\n").encode()
    if mutation == "duplicate_call":
        data *= 2
    v = read_partial(io.BytesIO(data), **args)
    assert (
        v["status"] == "unverified" and v["items"] == [] and not v["research_complete"]
    )


def test_absent_checkpoint_retains_returned_and_unfinished_denominators(tmp_path):
    _, args = sample(tmp_path)
    v = read_partial(io.BytesIO(), **args)
    assert v["status"] == "no_retained_source_items"
    assert v["returned_calls_without_checkpoint"] == ["1:2"] and v[
        "unfinished_calls"
    ] == ["2:3"]
    assert v["checkpoint_coverage_incomplete"] is True


def test_empty_return_and_error_are_not_positive_evidence(tmp_path):
    data, args = sample(tmp_path)
    row = json.loads(data)
    row.update(items=[], raw_item_count=0, normalized_item_count=0)
    args["diagnostics"]["events"][1]["item_count"] = 0
    args["diagnostics"]["events"].append(
        {
            "event": "stage_end",
            "stage": "retrieval",
            "source": "reddit",
            "call_id": "2:3",
            "at_seconds": 0.3,
            "outcome": "error",
        }
    )
    v = read_partial(io.BytesIO((json.dumps(row) + "\n").encode()), **args)
    assert v["status"] == "no_retained_source_items" and v["errored_calls"] == ["2:3"]
    assert v["unfinished_calls"] == [] and not v["research_complete"]


def test_writer_drops_duplicate_and_invalid_items_with_exact_counts(tmp_path):
    from tempfile import TemporaryFile

    start = datetime.now(timezone.utc)
    item = SimpleNamespace(
        item_id="abc", source="reddit", published_at=None, engagement={}
    )
    bad = SimpleNamespace(
        item_id="bad", source="reddit", published_at="2099-01-01", engagement={}
    )
    with TemporaryFile() as f:
        cap = PartialCapture(
            f.fileno(),
            "a" * 32,
            ["reddit"],
            POLICY_VERSION,
            start.isoformat(),
            time.monotonic(),
            45,
        )
        raw = [{}, {}, {}]
        cap.returned("reddit", raw, {"call_id": "1:2", "at_seconds": 0})
        cap.record("reddit", raw, [item, item, bad])
        f.seek(0)
        v = json.loads(f.read())
    assert (
        v["normalized_item_count"] == 3
        and len(v["items"]) == 1
        and v["rejected_items"] == v["duplicate_items"] == 1
    )


def test_source_metadata_never_serializes_unknown_text():
    item = SimpleNamespace(
        item_id="https://secret.invalid/",
        source="reddit",
        published_at=None,
        engagement={"score": 2, "token": "SECRET"},
        title="SECRET",
        body="SECRET",
    )
    v = project(item, "reddit", datetime.now(timezone.utc))
    assert "SECRET" not in json.dumps(v) and "secret.invalid" not in json.dumps(v)
    assert v["engagement"] == {"score": 2}


def test_byte_and_item_bounds_are_explicit(tmp_path):
    from alphaos.research.last30days_partial import MAX_BYTES

    _, args = sample(tmp_path)
    assert read_partial(io.BytesIO(b"x" * (MAX_BYTES + 1)), **args)["limitations"] == [
        "receipt_byte_limit_exceeded"
    ]


def test_late_planner_source_receipt_can_still_be_retained(tmp_path):
    data, args = sample(tmp_path)
    row = json.loads(data)
    # Preserve actual relative receipt clock but position retrieval start/end
    # just before it: this represents tiny remaining budget, never extra calls.
    e = row["captured_seconds"]
    args["diagnostics"]["events"][0]["at_seconds"] = e - 0.03
    args["diagnostics"]["events"][1]["at_seconds"] = e - 0.01
    row["stage_return_seconds"] = e - 0.01
    v = read_partial(io.BytesIO((json.dumps(row) + "\n").encode()), **args)
    assert v["retained_unique_item_count"] == 2 and not v["research_complete"]


@pytest.mark.parametrize(
    "returned_at,captured_at,expected_items",
    [(4.8, 5.0, 1), (44.7, 44.8, 1), (44.9, 45.1, 0)],
)
def test_saved_deadline_modes_on_exact_45_second_logical_clock(
    tmp_path, monkeypatch, returned_at, captured_at, expected_items
):
    """Synthetic structural item, not recovery of absent historical content.

    Retained modes: ordinary planner with unfinished Reddit; 43.573s planner
    with other retrieval returns near45s; late normalization excluded entirely.
    No sleeps, retries, external providers or new calls are used by this replay.
    """
    from alphaos.research import last30days_partial as mod

    start = datetime.now(timezone.utc) - timedelta(seconds=60)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return start + timedelta(seconds=captured_at)

    monkeypatch.setattr(mod, "datetime", Clock)
    monkeypatch.setattr(mod.time, "monotonic", lambda: 100 + captured_at)
    with (tmp_path / "receipt").open("w+b") as f:
        cap = PartialCapture(
            f.fileno(),
            "b" * 32,
            ["hackernews", "reddit"],
            POLICY_VERSION,
            start.isoformat(),
            100,
            45,
        )
        event = {
            "event": "stage_end",
            "stage": "retrieval",
            "source": "hackernews",
            "call_id": "1:2",
            "at_seconds": returned_at,
            "outcome": "returned",
            "item_count": 1,
        }
        raw = [{}]
        cap.returned("hackernews", raw, event)
        cap.record(
            "hackernews",
            raw,
            [
                SimpleNamespace(
                    item_id="synthetic",
                    source="hackernews",
                    published_at=None,
                    engagement={},
                )
            ],
        )
        diagnostic = {
            "source_policy": {
                "version": POLICY_VERSION,
                "status": "active",
                "violations": 0,
                "requested": ["hackernews", "reddit"],
                "dispatch_counts": {"hackernews": 1, "reddit": 1},
            },
            "events": [
                {
                    "event": "stage_start",
                    "stage": "retrieval",
                    "source": "hackernews",
                    "call_id": "1:2",
                    "at_seconds": returned_at - 0.1,
                },
                event,
                {
                    "event": "stage_start",
                    "stage": "retrieval",
                    "source": "reddit",
                    "call_id": "3:4",
                    "at_seconds": returned_at - 0.1,
                },
            ],
        }
        result = read_partial(
            f,
            nonce="b" * 32,
            requested=["hackernews", "reddit"],
            policy_version=POLICY_VERSION,
            started_utc=start.isoformat(),
            deadline=45,
            diagnostics=diagnostic,
        )
    assert result["retained_unique_item_count"] == expected_items
    assert result["unfinished_calls"] == ["3:4"] and not result["research_complete"]
    if captured_at > 45:
        assert result["returned_calls_without_checkpoint"] == ["1:2"]
