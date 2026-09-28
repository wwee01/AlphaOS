"""Factual, source-linked closed-trade reviews; no policy learning or broker IO.

Append-only reviews are written by the research worker, including a late review
of retained old outcomes. They never fill in old lessons or claim retrospective
broker provenance. Each version's first review is immutable; the read surface
checks its source hash against current evidence before presenting it as current.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

from alphaos.reports.trade_packet import assemble_trade_packet
from alphaos.reports.trade_accounting import source_sha256
from alphaos.util import timeutils

VERSION = "factual_trade_review_v1"


def _failed_source(value):
    """Preserve malformed numeric evidence explicitly, never as financial zero."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_nonfinite_number": repr(value)}
    if isinstance(value, dict):
        return {key: _failed_source(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_failed_source(item) for item in value]
    if isinstance(value, bytes):
        return {"unexpected_bytes_hex": value.hex()}
    return value


def _scope(packet):
    orders = packet.get("orders") or []
    candidate = packet.get("candidate") or {}
    if candidate.get("is_mock") == 1 or any(row.get("is_demo") == 1 or row.get("execution_source") == "mock" for row in orders):
        return "mock_or_demo"
    if orders and all(row.get("execution_source") == "alpaca_paper" and row.get("is_demo") == 0 for row in orders):
        return "alpaca_paper"
    return "unknown_execution_scope"


def _origin_links(journal, packet):
    """Bind proposal/order anchors, never a later candidate re-evaluation."""
    limits = []
    proposal = packet.get("proposal") or {}
    position = packet.get("position") or {}
    order = next((r for r in packet.get("orders", []) if r["order_id"] == position.get("order_id")), {})
    risk_id = order.get("risk_check_id") or proposal.get("risk_check_id")
    if order.get("risk_check_id") and proposal.get("risk_check_id") and order["risk_check_id"] != proposal["risk_check_id"]:
        limits.append("order_and_proposal_risk_anchor_differ")
    for key, table, id_col, anchor in (
            ("openai_evaluation", "openai_evaluations", "eval_id", proposal.get("eval_id")),
            ("risk_check", "risk_checks", "risk_check_id", risk_id),
            ("claude_review", "claude_reviews", "review_id", proposal.get("claude_review_id"))):
        row = journal.one(f"SELECT * FROM {table} WHERE {id_col}=?", (anchor,)) if anchor else None
        if row and (row.get("candidate_id") != proposal.get("candidate_id") or
                    (row.get("symbol") and row["symbol"] != proposal.get("symbol")) or
                    (row.get("proposal_id") and row["proposal_id"] != proposal.get("proposal_id")) or
                    (key == "claude_review" and row.get("eval_id") != proposal.get("eval_id"))):
            limits.append(key + "_origin_mismatch")
            row = None
        packet[key] = row
    # The old schema has no approval_id on orders. A unique approved receipt
    # preceding order creation is usable; ambiguity/unknown flags stay explicit.
    candidates = journal.query("SELECT * FROM approvals WHERE proposal_id=? AND approved=1 ORDER BY id", (proposal.get("proposal_id"),))
    approvals = []
    for row in candidates:
        try:
            at, created = (datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                           for v in (row["created_at_utc"], order.get("created_at_utc")))
            if (at.utcoffset() is not None and created.utcoffset() is not None and at <= created
                    and row.get("candidate_id") == proposal.get("candidate_id")
                    and row.get("symbol") == proposal.get("symbol")):
                approvals.append(row)
        except (ValueError, TypeError):
            continue
    packet["approval"] = approvals[0] if len(approvals) == 1 else None
    if len(approvals) != 1:
        limits.append("unique_origin_approval_unverifiable")
    elif approvals[0].get("freshness_ok") != 1 or approvals[0].get("risk_ok") != 1:
        limits.append("origin_approval_checks_unverified")
    return limits


def build_trade_review(journal, outcome: dict) -> dict:
    packet = assemble_trade_packet(journal, position_id=outcome["position_id"])
    accounting = packet.get("accounting") or {}
    # Never silently use a different outcome returned by a latest-row lookup.
    packet_outcome = packet.get("outcome") or {}
    if packet_outcome.get("outcome_id") != outcome["outcome_id"]:
        raise ValueError("outcome_packet_identity_mismatch")
    origin_limits = _origin_links(journal, packet)
    evidence = {key: packet.get(key) for key in (
        "ids", "candidate", "openai_evaluation", "claude_review", "risk_check",
        "proposal", "approval", "orders", "fills", "position", "exit", "outcome")}
    source_hash = source_sha256(evidence)
    limitations = list(accounting.get("blockers", [])) + list(accounting.get("identity_limitations", [])) + origin_limits
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
            "data_scope": _scope(packet),
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
            failed_source = _failed_source({"outcome": outcome})
            review = {"version": VERSION, "outcome_id": outcome["outcome_id"],
                      "position_id": outcome["position_id"], "symbol": outcome["symbol"],
                      "source_sha256": source_sha256(failed_source), "source_evidence": failed_source,
                      "review_status": "failed", "limitations": [str(exc)],
                      "facts": [], "policy_updated": False, "causal_lesson_claimed": False,
                      "data_scope": "unknown_execution_scope",
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
    result["status_counts_basis"] = "stored_first_review_status_not_current_source_verification"
    result["eligible_counts_scope"] = "all_closed_outcomes_including_mock_demo_and_unknown"
    for row in journal.query("SELECT * FROM trade_learning_records WHERE method_version=? ORDER BY id DESC LIMIT ?", (VERSION, limit)):
        outcome = journal.one("SELECT * FROM trade_outcomes WHERE outcome_id=?", (row["outcome_id"],))
        review = None
        try:
            saved = json.loads(row["review_json"])
            if (not isinstance(saved, dict) or saved.get("version") != row["method_version"]
                    or saved.get("outcome_id") != row["outcome_id"]
                    or saved.get("position_id") != row["position_id"]
                    or saved.get("review_status") != row["review_status"]
                    or saved.get("source_sha256") != row["source_sha256"]
                    or source_sha256(saved.get("source_evidence")) != row["source_sha256"]
                    or not isinstance(saved.get("symbol"), str)
                    or saved.get("data_scope") not in {"alpaca_paper", "mock_or_demo", "unknown_execution_scope"}
                    or any(not isinstance(saved.get(key), list) or
                           any(not isinstance(item, str) for item in saved[key])
                           for key in ("facts", "limitations"))):
                raise ValueError("invalid_saved_review")
            review = saved
            current = build_trade_review(journal, outcome) if outcome else None
            # Verify derived text/arithmetic too; a matching source hash alone
            # does not authenticate a stored review's claims.
            unchanged = current is not None and current == saved
        except (ValueError, TypeError, KeyError, OverflowError):
            unchanged = False
        if review is None:
            review = {"version": VERSION, "outcome_id": row["outcome_id"],
                      "position_id": row["position_id"], "symbol": (outcome or {}).get("symbol", "unknown"),
                      "source_sha256": row["source_sha256"], "facts": [],
                      "data_scope": "unknown_execution_scope",
                      "limitations": ["saved_review_unverifiable"], "review_status": "invalid_saved_review"}
        result["records"].append({"recorded_at_utc": row["created_at_utc"],
                                  "source_unchanged": unchanged, "review": review})
    result["status"] = "recorded_factual_reviews"
    result["display_limit"] = limit
    return result
