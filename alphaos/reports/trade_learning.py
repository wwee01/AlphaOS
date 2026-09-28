"""Factual, source-linked closed-trade reviews; no policy learning or broker IO.

Append-only reviews are written by the research worker, including a late review
of retained old outcomes. They never fill in old lessons or claim retrospective
broker provenance. Each version's first review is immutable; the read surface
checks its source hash against current evidence before presenting it as current.
"""
from __future__ import annotations

import json
from typing import Any

from alphaos.reports.trade_packet import assemble_trade_packet
from alphaos.reports.trade_accounting import source_sha256
from alphaos.util import timeutils

VERSION = "factual_trade_review_v1"


def build_trade_review(journal, outcome: dict) -> dict:
    packet = assemble_trade_packet(journal, position_id=outcome["position_id"])
    accounting = packet.get("accounting") or {}
    # Never silently use a different outcome returned by a latest-row lookup.
    packet_outcome = packet.get("outcome") or {}
    if packet_outcome.get("outcome_id") != outcome["outcome_id"]:
        raise ValueError("outcome_packet_identity_mismatch")
    evidence = {key: packet.get(key) for key in (
        "ids", "candidate", "openai_evaluation", "claude_review", "risk_check",
        "proposal", "approval", "orders", "fills", "position", "exit", "outcome")}
    source_hash = source_sha256(evidence)
    limitations = list(accounting.get("blockers", [])) + list(accounting.get("identity_limitations", []))
    required = ("candidate", "openai_evaluation", "risk_check", "proposal", "approval", "exit")
    limitations += ["missing_" + key for key in required if not packet.get(key)]
    fill_times = accounting.get("fill_timestamps", [])
    times_complete = bool(fill_times) and all(
        row.get("timestamp_basis") == "recorded_broker_cumulative_receipt" for row in fill_times)
    if not times_complete:
        limitations.append("broker_cumulative_fill_time_receipts_incomplete")
    arithmetic = accounting.get("status") == "verified_ledger_arithmetic"
    closure = packet.get("exit") or {}
    facts: list[str] = []
    if arithmetic:
        facts.append(f"Closed {outcome['qty']:g} {outcome['symbol']} shares; "
                     f"gross P&L {accounting['gross_pnl']:.2f}, modeled costs "
                     f"{accounting['modeled_costs']:.2f}, net P&L {accounting['net_pnl']:.2f}.")
        facts.append(f"Net R {accounting['net_r']:.6f}; stored realized R is gross before modeled costs.")
    else:
        facts.append("Net R is withheld because complete reconciled ledger arithmetic is unavailable.")
    reason = closure.get("exit_reason")
    if reason:
        facts.append(f"Recorded exit reason: {reason}.")
    facts.append("A single outcome does not establish the cause of profit/loss or a strategy change.")
    return {"version": VERSION, "outcome_id": outcome["outcome_id"],
            "position_id": outcome["position_id"], "symbol": outcome["symbol"],
            "source_sha256": source_hash, "source_evidence": evidence,
            "accounting": accounting, "facts": facts,
            "limitations": sorted(set(limitations)),
            "review_status": "linked_factual_review" if arithmetic and not limitations else "limited_factual_review",
            "outcome_recorded_at_utc": outcome.get("created_at_utc"),
            "generated_by": "deterministic_ledger_review", "policy_updated": False,
            "causal_lesson_claimed": False, "broker_fees_verified": False}


def record_pending_trade_reviews(journal, limit: int = 100) -> dict:
    """Bounded backlog processing; immutable idempotent rows, separate from close.

    Failure records retain the exact source and error without blocking later
    outcomes. They remain explicitly failed until a separately versioned repair;
    no failure is counted as completed learning. INSERT OR IGNORE handles races.
    """
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    outcomes = journal.query(
        "SELECT o.* FROM trade_outcomes o WHERE NOT EXISTS "
        "(SELECT 1 FROM trade_learning_records r WHERE r.outcome_id=o.outcome_id "
        "AND r.method_version=?) ORDER BY o.id LIMIT ?", (VERSION, limit))
    inserted = failed = 0
    for outcome in outcomes:
        try:
            review = build_trade_review(journal, outcome)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            review = {"version": VERSION, "outcome_id": outcome["outcome_id"],
                      "position_id": outcome["position_id"], "symbol": outcome["symbol"],
                      "source_sha256": source_sha256(outcome), "source_evidence": {"outcome": outcome},
                      "review_status": "failed", "limitations": [str(exc)],
                      "facts": [], "policy_updated": False, "causal_lesson_claimed": False,
                      "generated_by": "deterministic_ledger_review"}
            failed += 1
        stamp = timeutils.stamp()
        with journal.conn:
            cur = journal.conn.execute(
                "INSERT OR IGNORE INTO trade_learning_records "
                "(outcome_id,position_id,method_version,source_sha256,review_status,review_json,"
                "created_at_utc,created_at_sgt) VALUES(?,?,?,?,?,?,?,?)",
                (outcome["outcome_id"], outcome["position_id"], VERSION, review["source_sha256"],
                 review["review_status"], json.dumps(review, sort_keys=True, allow_nan=False), stamp.utc, stamp.local_sgt))
            inserted += cur.rowcount
    pending = journal.one(
        "SELECT COUNT(*) n FROM trade_outcomes o WHERE NOT EXISTS "
        "(SELECT 1 FROM trade_learning_records r WHERE r.outcome_id=o.outcome_id AND r.method_version=?)",
        (VERSION,))["n"]
    return {"version": VERSION, "attempted": len(outcomes), "inserted": inserted,
            "failed": failed, "pending": pending, "policy_updated": False}


def trade_learning_report(journal, limit: int = 50) -> dict:
    """Read-only compatibility with pre-migration DBs; explicit whole-cohort counts."""
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    eligible = journal.one("SELECT COUNT(*) n FROM trade_outcomes")["n"]
    result: dict[str, Any] = {"version": VERSION, "eligible": eligible, "recorded": 0,
                              "status_counts": {}, "records": [], "policy_updated": False}
    if not journal.one("SELECT name FROM sqlite_master WHERE type='table' AND name='trade_learning_records'"):
        result["status"] = "not_installed"
        return result
    counts = journal.query("SELECT review_status,COUNT(*) n FROM trade_learning_records WHERE method_version=? GROUP BY review_status", (VERSION,))
    result["status_counts"] = {row["review_status"]: row["n"] for row in counts}
    result["recorded"] = sum(result["status_counts"].values())
    for row in journal.query("SELECT * FROM trade_learning_records WHERE method_version=? ORDER BY id DESC LIMIT ?", (VERSION, limit)):
        review = json.loads(row["review_json"])
        outcome = journal.one("SELECT * FROM trade_outcomes WHERE outcome_id=?", (row["outcome_id"],))
        try:
            current = build_trade_review(journal, outcome) if outcome else None
            unchanged = current is not None and current["source_sha256"] == row["source_sha256"]
        except (ValueError, TypeError, KeyError, OverflowError):
            unchanged = False
        result["records"].append({"recorded_at_utc": row["created_at_utc"],
                                  "source_unchanged": unchanged, "review": review})
    result["status"] = "recorded_factual_reviews"
    result["display_limit"] = limit
    return result
