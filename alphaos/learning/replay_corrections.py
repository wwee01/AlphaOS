"""Explicit, append-only retrospective replay evidence; never a live writer.

The original candidate outcome and every frozen preregistration remain
untouched. Consumers must not silently substitute these diagnostics for
original or prospective evidence. Inputs include the original row and the
exact cached bars so every correction can be reproduced and identified.
"""

from __future__ import annotations

from datetime import date
import hashlib
import json

from alphaos.learning.outcomes_engine import REPLAY_METHOD_VERSION, replay_bracket_with_maturity
from alphaos.util import timeutils


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def build_replay_corrections(conn, *, as_of_date: str, limit: int = 5000) -> dict:
    """SELECT-only, including against an unmigrated read-only ledger copy.

    Uses the already stamped window. Missing/invalid windows are reported,
    never inferred from today's card. Cache data is retrospective; the
    cutoff bounds outcome bar dates, not historical availability of data.
    """
    date.fromisoformat(as_of_date)
    if limit <= 0:
        raise ValueError("limit must be positive")
    rows = conn.execute(
        "SELECT * FROM candidate_outcomes WHERE outcome_status='complete' "
        "AND replay_result IS NOT NULL ORDER BY id ASC LIMIT ?", (limit,),
    ).fetchall()
    records = []
    skipped = {"already_versioned": 0, "missing_window": 0, "missing_decision_date": 0,
               "missing_forward_bars": 0}
    for raw in rows:
        row = dict(raw)
        if row.get("replay_method_version") is not None:
            skipped["already_versioned"] += 1
            continue
        window = row.get("replay_window_days")
        if not isinstance(window, int) or window <= 0:
            skipped["missing_window"] += 1
            continue
        decision = timeutils.parse_iso(row.get("decision_at_utc"))
        if decision is None:
            skipped["missing_decision_date"] += 1
            continue
        bars = [dict(b) for b in conn.execute(
            "SELECT market_date AS date, open, high, low, close, volume, source_feed "
            "FROM daily_bars WHERE symbol=? AND market_date>? AND market_date<=? "
            "ORDER BY market_date ASC", (row["symbol"], decision.date().isoformat(), as_of_date),
        )]
        bars = [b for b in bars if b["high"] is not None and b["low"] is not None][:window]
        if not bars:
            skipped["missing_forward_bars"] += 1
            continue
        corrected = replay_bracket_with_maturity(
            row.get("entry_reference_price"), row.get("stop_price"), row.get("target_price"),
            row.get("direction_hint"), bars, max_days=window,
        )
        original_json = canonical_json(row)
        inputs = {
            "method_version": REPLAY_METHOD_VERSION,
            "evidence_kind": "retrospective_cached_bars_not_confirmatory",
            "outcome_id": row["outcome_id"], "as_of_date": as_of_date,
            "original_sha256": hashlib.sha256(original_json.encode()).hexdigest(),
            "entry": row.get("entry_reference_price"), "stop": row.get("stop_price"),
            "target": row.get("target_price"), "direction": row.get("direction_hint"),
            "window_days": window, "bars": bars,
        }
        input_json = canonical_json(inputs)
        input_sha = hashlib.sha256(input_json.encode()).hexdigest()
        records.append({
            "correction_id": "replayfix_" + input_sha[:32],
            "outcome_id": row["outcome_id"], "method_version": REPLAY_METHOD_VERSION,
            "input_sha256": input_sha, "original_json": original_json,
            "corrected_json": canonical_json(corrected), "input_json": input_json,
            "value_changed": any(row.get(old) != corrected[new] for old, new in (
                ("replay_result", "result"), ("replay_r", "replay_r"),
                ("replay_exit_reason", "replay_exit_reason"),
            )),
        })
    return {
        "method_version": REPLAY_METHOD_VERSION, "as_of_date": as_of_date,
        "evidence_kind": "retrospective_cached_bars_not_confirmatory",
        "source_rows_read": len(rows), "skipped": skipped, "records": records,
        "n_corrections": len(records), "n_value_changed": sum(r["value_changed"] for r in records),
        "n_pending": sum(json.loads(r["corrected_json"])["replay_status"] == "pending" for r in records),
    }


def append_replay_corrections(conn, report: dict) -> int:
    """One transaction inserts evidence only; unchanged reruns insert zero.

    Refuses stale original rows and forged/edited input evidence. Does not
    migrate schema, update outcomes, or write preregistrations. The caller
    must explicitly opt into this operation on an already migrated copy.
    """
    if conn.in_transaction:
        raise ValueError("correction append requires its own transaction")
    count = 0
    stamp = timeutils.stamp()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        for record in report["records"]:
            if hashlib.sha256(record["input_json"].encode()).hexdigest() != record["input_sha256"]:
                raise ValueError("correction input hash mismatch")
            if record["correction_id"] != "replayfix_" + record["input_sha256"][:32]:
                raise ValueError("correction identifier does not match input hash")
            inputs = json.loads(record["input_json"])
            if (record["method_version"] != REPLAY_METHOD_VERSION
                    or inputs["method_version"] != REPLAY_METHOD_VERSION
                    or inputs["outcome_id"] != record["outcome_id"]
                    or inputs["original_sha256"] != hashlib.sha256(record["original_json"].encode()).hexdigest()):
                raise ValueError("correction identity mismatch")
            expected = replay_bracket_with_maturity(
                inputs["entry"], inputs["stop"], inputs["target"], inputs["direction"],
                inputs["bars"], max_days=inputs["window_days"],
            )
            if canonical_json(expected) != record["corrected_json"]:
                raise ValueError("correction output does not match frozen inputs")
            current = conn.execute("SELECT * FROM candidate_outcomes WHERE outcome_id=?", (record["outcome_id"],)).fetchone()
            if current is None or canonical_json(dict(current)) != record["original_json"]:
                raise ValueError("original outcome changed since correction preview; rerun the preview")
            cursor = conn.execute(
                "INSERT INTO replay_evidence_corrections "
                "(correction_id,outcome_id,method_version,input_sha256,original_json,corrected_json,input_json,"
                "created_at_utc,created_at_sgt) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(outcome_id,method_version,input_sha256) DO NOTHING",
                tuple(record[k] for k in ("correction_id", "outcome_id", "method_version", "input_sha256",
                                         "original_json", "corrected_json", "input_json")) + (stamp.utc, stamp.local_sgt),
            )
            count += cursor.rowcount
    return count
