#!/usr/bin/env python3
"""Read recorded WATCH-1 diagnostics; never replay history or initialize a DB.

Example: python -m scripts.watch_advisory_report --db COPY.db
All counts are candidate events, not independent trades or portfolio returns.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3

from alphaos.research.watch_advisory import POLICY_VERSION, PROTOCOL_ID


def _valid_record(diag, row) -> bool:
    decisions = {"propose", "watch", "reject"}
    reasons = diag.get("exclusion_reasons")
    if (type(diag.get("changed")) is not bool or not isinstance(reasons, list)
            or not all(isinstance(reason, str) for reason in reasons)):
        return False
    if not all(isinstance(diag.get(key), str) and diag[key] for key in ("eval_id", "label_id")):
        return False
    for inner, outer in (("primary_decision", "eval_decision"),
                         ("classifier_decision", "label_decision"),
                         ("current_decision", "final_decision")):
        value = diag.get(inner)
        if not isinstance(value, str) or value not in decisions or value != row[outer]:
            return False
    if diag["changed"]:
        return (diag["primary_decision"] == "propose" and diag["classifier_decision"] == "watch"
                and diag["current_decision"] == "watch"
                and diag.get("challenger_decision_before_risk") == "propose"
                and diag.get("direction") == "long" and not reasons)
    return diag.get("challenger_decision_before_risk") == diag["current_decision"]


def report(db_path: str) -> dict:
    counts: Counter = Counter()
    exclusions: Counter = Counter()
    transitions: Counter = Counter()
    changed_events = []
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute(
            "SELECT adjustment_id, candidate_id, symbol, scan_batch_id, created_at_utc, "
            "eval_decision, label_decision, final_decision, evidence_json "
            "FROM decision_adjustments ORDER BY id"
        ):
            counts["source_adjustment_rows"] += 1
            try:
                evidence = json.loads(row["evidence_json"] or "{}")
                diag = evidence.get("watch_advisory_diagnostic")
            except (ValueError, AttributeError):
                counts["malformed_evidence"] += 1
                continue
            if not isinstance(diag, dict):
                counts["no_recorded_diagnostic"] += 1
                continue
            if (diag.get("protocol_id") != PROTOCOL_ID
                    or diag.get("policy_version") != POLICY_VERSION
                    or diag.get("mode") != "decision_diagnostic_only"):
                counts["other_protocol_or_version"] += 1
                continue
            if diag.get("is_mock") is not False:
                counts["mock_or_unknown_provenance"] += 1
                continue
            if not _valid_record(diag, row):
                counts["malformed_diagnostic"] += 1
                continue
            counts["recorded_nonmock_diagnostics"] += 1
            transition = (f"{diag.get('current_decision')}->"
                          f"{diag.get('challenger_decision_before_risk')}")
            transitions[transition] += 1
            exclusions.update(diag.get("exclusion_reasons") or [])
            if diag.get("changed") is True:
                counts["changed_candidate_events"] += 1
                changed_events.append({
                    "adjustment_id": row["adjustment_id"],
                    "candidate_id": row["candidate_id"],
                    "symbol": row["symbol"],
                    "scan_batch_id": row["scan_batch_id"],
                    "recorded_at_utc": row["created_at_utc"],
                    "eval_id": diag.get("eval_id"),
                    "label_id": diag.get("label_id"),
                })
    finally:
        conn.close()
    return {
        "protocol_id": PROTOCOL_ID, "policy_version": POLICY_VERSION,
        "scope": "all recorded decision adjustments; nonmock WATCH-1 diagnostics only",
        "counts": dict(sorted(counts.items())),
        "transitions": dict(sorted(transitions.items())),
        "exclusion_reason_counts": dict(sorted(exclusions.items())),
        "changed_events": changed_events,
        "limitations": [
            "Legacy rows are missing diagnostics, never assumed unchanged or reconstructed.",
            "Candidates without a classifier adjustment are outside this diagnostic stream.",
            "Changed events precede risk/slot/approval/fill checks and can repeat a symbol.",
            "No additional trades, P&L, portfolio comparison or promotion is established.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Existing ledger or consistent copied ledger")
    args = parser.parse_args()
    print(json.dumps(report(args.db), indent=2))


if __name__ == "__main__":
    main()
