"""Read-only, versioned audit of recorded entry origin and final risk evidence.

Exact stored anchors are checked in one SQLite read snapshot. This does not
rewrite factual_trade_review_v1, authenticate model calls, verify paid fees, or
change trading policy. A recorded passing proposal risk is not evidence of the
final account check: that requires the separately bound submission-stage event.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from typing import Any
from uuid import uuid4

from alphaos.util import timeutils

VERSION = "origin_provenance_v1"
FINAL_STAGE = "final_account_submission"
FINAL_SCOPE = "account_limits_and_remaining_capacity"
_HASH = re.compile(r"[0-9a-f]{16}\Z")  # lineage.hashing.stable_hash convention


def _now() -> datetime:
    return timeutils.now_utc()


def _json(value: Any) -> Any:
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result

    def constant(_):
        raise ValueError("nonfinite JSON")

    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
    except (TypeError, ValueError, RecursionError):
        return None


def _number(value: Any, *, positive: bool = True) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() and (not positive or result > 0) else None


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, bytes):
        return {"invalid_bytes": value.hex()}
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_nonfinite": repr(value)}
    return value


class _Audit:
    def __init__(self, journal, now):
        self.journal = journal
        self.now = now
        self.limits: set[str] = set()
        self.evidence: dict[str, Any] = {}

    def row(self, name, table, key, anchor):
        rows = (
            self.journal.query(f"SELECT * FROM {table} WHERE {key}=?", (anchor,))
            if anchor
            else []
        )
        if len(rows) != 1:
            self.limits.add(name + "_anchor_unverifiable")
            row = {}
        else:
            row = dict(rows[0])
        # No prompt/response bodies in the diagnostic output or hash preimage.
        self.evidence[name] = {
            k: v
            for k, v in row.items()
            if k
            not in {
                "raw_json",
                "raw_request_json",
                "raw_response_json",
                "snapshot_json",
                "reasoning",
                "reasoning_summary",
                "news_sources_json",
            }
        }
        return row

    def time(self, row, name):
        value = row.get("created_at_utc")
        if not isinstance(value, str) or not value.strip():
            self.limits.add(name + "_timestamp_missing")
            return None
        try:
            at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            self.limits.add(name + "_timestamp_malformed")
            return None
        if at.utcoffset() is None:
            self.limits.add(name + "_timestamp_naive")
            return None
        if at > self.now:
            self.limits.add(name + "_timestamp_future")
        return at

    def before(self, left, right, code):
        if left is not None and right is not None and left > right:
            self.limits.add(code)

    def link(self, row, expected, name, *, optional=()):
        for key, value in expected.items():
            actual = row.get(key)
            if key in optional and actual in (None, ""):
                continue
            if value in (None, "") or actual in (None, ""):
                self.limits.add(name + "_" + key + "_missing")
            elif actual != value:
                self.limits.add(name + "_" + key + "_mismatch")

    def model(self, row, name, *, primary):
        fields = ["model", "model_provider", "prompt_hash", "system_prompt_hash"]
        if primary:
            fields += ["prompt_template_version", "schema_version"]
        for key in fields:
            value = row.get(key)
            if not isinstance(value, str) or not value.strip():
                self.limits.add(name + "_" + key + "_missing")
            elif key.endswith("hash") and not _HASH.fullmatch(value):
                self.limits.add(name + "_" + key + "_malformed")
        if primary and row.get("validation_status") != "passed":
            self.limits.add(name + "_validation_not_passed")


def _final_risk(a, proposal, order, risk, proposal_at, risk_at, order_at):
    before = set(a.limits)
    selected = []
    for row in a.journal.query(
        "SELECT * FROM system_events WHERE category='entry_risk_snapshot' ORDER BY id"
    ):
        detail = _json(row.get("detail_json"))
        if not isinstance(detail, dict) or detail.get("risk_check_id") != order.get(
            "risk_check_id"
        ):
            continue
        selected.append((dict(row), detail))
    a.evidence["final_risk_events"] = [
        {
            "event_id": row.get("event_id"),
            "created_at_utc": row.get("created_at_utc"),
            "detail": detail,
        }
        for row, detail in selected
    ]
    event_id = None
    market_rechecked = None
    if len(selected) != 1:
        a.limits.add(
            "final_execution_risk_event_unavailable"
            if not selected
            else "final_execution_risk_event_ambiguous"
        )
    else:
        row, detail = selected[0]
        event_id = row.get("event_id")
        a.link(
            detail,
            {
                "proposal_id": proposal.get("proposal_id"),
                "risk_check_id": order.get("risk_check_id"),
                "stage": FINAL_STAGE,
                "scope": FINAL_SCOPE,
            },
            "final_execution_risk",
        )
        at = a.time(row, "final_execution_risk_event")
        a.before(proposal_at, risk_at, "final_execution_risk_before_proposal")
        a.before(risk_at, at, "final_execution_risk_event_before_risk")
        a.before(at, order_at, "final_execution_risk_event_after_entry")
        market_rechecked = detail.get("market_snapshot_rechecked")
        if type(market_rechecked) is not bool:
            a.limits.add("final_execution_risk_market_scope_unverifiable")
        qty = _number(order.get("qty"))
        if (
            qty is None
            or _number(detail.get("proposal_qty")) != qty
            or _number(proposal.get("qty")) != qty
        ):
            a.limits.add("final_execution_risk_submitted_quantity_mismatch")
        decision = detail.get("risk")
        if not isinstance(decision, dict):
            decision = {}
        if decision.get("approved") is not True or decision.get("block_reasons") != []:
            a.limits.add("final_execution_risk_decision_not_passed")
        context = decision.get("account_context")
        if not isinstance(context, dict):
            context = {}
        equity = _number(context.get("equity"))
        gross = _number(context.get("gross_committed_notional"), positive=False)
        slots = _number(context.get("open_positions"), positive=False)
        pnl = _number(context.get("account_pnl_today"), positive=False)
        if (
            equity is None
            or gross is None
            or gross < 0
            or slots is None
            or slots < 0
            or slots != slots.to_integral_value()
            or pnl is None
        ):
            a.limits.add("final_execution_risk_account_context_unverifiable")
        sizing = decision.get("sizing")
        sizing = sizing if isinstance(sizing, dict) else {}
        maximum = _number(risk.get("position_size"))
        if (
            maximum is None
            or _number(sizing.get("shares")) != maximum
            or qty is None
            or qty > maximum
        ):
            a.limits.add("final_execution_risk_capacity_unverifiable")
        if _number(risk.get("max_risk_amount")) is None or _number(
            sizing.get("risk_budget")
        ) != _number(risk.get("max_risk_amount")):
            a.limits.add("final_execution_risk_budget_mismatch")
        for field in (
            "daily_loss_check_result",
            "max_trades_check_result",
            "max_open_positions_check_result",
        ):
            if risk.get(field) != "pass":
                a.limits.add("final_execution_risk_" + field + "_not_passed")
        for field in ("liquidity_check_result", "spread_check_result"):
            expected = "pass" if market_rechecked is True else "not_rechecked"
            if risk.get(field) != expected:
                a.limits.add("final_execution_risk_" + field + "_scope_mismatch")
    # Final evidence is invalid if its underlying exact risk row failed origin
    # validation; absent historical event is unavailable, never inferred.
    gaps = sorted(a.limits - before)
    risk_gaps = sorted(
        code
        for code in before
        if code.startswith(("risk_", "entry_order_", "proposal_"))
        or code == "order_and_proposal_risk_anchor_differ"
    )
    gaps = sorted(set(gaps + risk_gaps))
    return {
        "status": "verified_recorded_final_execution_risk"
        if not gaps
        else "unavailable_or_invalid",
        "risk_check_id": order.get("risk_check_id"),
        "event_id": event_id,
        "scope": FINAL_SCOPE,
        "market_snapshot_rechecked": market_rechecked,
        "limitations": gaps,
    }


def _validate(journal, outcome):
    a = _Audit(journal, _now())
    supplied = outcome if isinstance(outcome, dict) else {}
    actual = a.row(
        "outcome", "trade_outcomes", "outcome_id", supplied.get("outcome_id")
    )
    # Accept callers' projections, but reject every supplied contradictory field.
    if any(
        key not in actual or value != actual[key] for key, value in supplied.items()
    ):
        a.limits.add("outcome_input_mismatch")
    pos = a.row("position", "positions", "position_id", actual.get("position_id"))
    order = a.row("entry_order", "paper_orders", "order_id", pos.get("order_id"))
    prop = a.row(
        "proposal", "trade_proposals", "proposal_id", actual.get("proposal_id")
    )
    candidate = a.row(
        "candidate", "candidates", "candidate_id", prop.get("candidate_id")
    )
    evaluation = a.row(
        "evaluation", "openai_evaluations", "eval_id", prop.get("eval_id")
    )
    risk = a.row("risk", "risk_checks", "risk_check_id", order.get("risk_check_id"))
    identity = {
        key: prop.get(key)
        for key in ("candidate_id", "proposal_id", "trade_id", "symbol")
    }
    for name, row in (("outcome", actual), ("position", pos), ("entry_order", order)):
        a.link(row, identity, name)
        a.link(row, {"direction": prop.get("direction")}, name)
    a.link(
        candidate,
        {"candidate_id": prop.get("candidate_id"), "symbol": prop.get("symbol")},
        "candidate",
    )
    a.link(
        evaluation,
        {"candidate_id": prop.get("candidate_id"), "symbol": prop.get("symbol")},
        "evaluation",
    )
    a.link(
        risk,
        {key: prop.get(key) for key in ("proposal_id", "candidate_id", "trade_id")},
        "risk",
    )
    a.link(pos, {"eval_id": prop.get("eval_id")}, "position")
    if order.get("risk_check_id") != prop.get("risk_check_id") or not order.get(
        "risk_check_id"
    ):
        a.limits.add("order_and_proposal_risk_anchor_differ")
    if prop.get("direction") not in {"long", "short"}:
        a.limits.add("proposal_direction_unverifiable")
    sides = {"buy"} if prop.get("direction") == "long" else {"sell", "sell_short"}
    if order.get("side") not in sides:
        a.limits.add("entry_order_side_mismatch")
    # Intent fields stay distinct from rounded broker prices and actual fills.
    # order_schema/entry_submission persist these original proposal terms.
    for term, order_field in (
        ("entry", "intended_entry_price"),
        ("stop", "stop_loss_price"),
        ("target", "take_profit_price"),
    ):
        intended = _number(prop.get(term))
        if intended is None or _number(risk.get(term + "_price")) != intended:
            a.limits.add("risk_" + term + "_intent_mismatch")
        if intended is None or _number(order.get(order_field)) != intended:
            a.limits.add("entry_order_" + term + "_intent_mismatch")
    requested, maximum = _number(order.get("qty")), _number(risk.get("position_size"))
    if requested is None or _number(prop.get("qty")) != requested:
        a.limits.add("entry_order_quantity_intent_mismatch")
    if requested is None or maximum is None or requested > maximum:
        a.limits.add("risk_submitted_quantity_exceeds_or_lacks_capacity")
    a.model(evaluation, "evaluation", primary=True)
    if (
        risk.get("result") != "pass"
        or risk.get("fail_reason") not in (None, "")
        or _json(risk.get("block_reasons_json")) != []
    ):
        a.limits.add("risk_decision_not_passed")
    times = {
        name: a.time(row, name)
        for name, row in (
            ("candidate", candidate),
            ("evaluation", evaluation),
            ("proposal", prop),
            ("risk", risk),
            ("entry_order", order),
            ("outcome", actual),
        )
    }
    for left, right in (
        ("candidate", "evaluation"),
        ("evaluation", "proposal"),
        ("evaluation", "risk"),
        ("proposal", "entry_order"),
        ("risk", "entry_order"),
        ("entry_order", "outcome"),
    ):
        a.before(times[left], times[right], left + "_after_" + right)
    approvals = journal.query(
        "SELECT * FROM approvals WHERE proposal_id=? AND approved=1 ORDER BY id",
        (prop.get("proposal_id"),),
    )
    eligible = []
    # Later approvals do not replace the unique original approval. Malformed
    # timestamps cannot be classified as later and remain explicit limitations.
    for row in approvals:
        at = a.time(row, "approval")
        if (
            at is not None
            and times["entry_order"] is not None
            and at <= times["entry_order"]
        ):
            eligible.append((row, at))
    a.evidence["approvals"] = [dict(row) for row in approvals]
    approval_id = None
    if len(eligible) != 1:
        a.limits.add("unique_origin_approval_unverifiable")
    else:
        row, at = eligible[0]
        approval_id = row.get("approval_id")
        a.link(
            row,
            {key: prop.get(key) for key in ("proposal_id", "candidate_id", "symbol")},
            "approval",
        )
        a.before(times["proposal"], at, "approval_before_proposal")
        if row.get("freshness_ok") != 1 or row.get("risk_ok") != 1:
            a.limits.add("approval_checks_not_passed")
    claude_id = prop.get("claude_review_id")
    if claude_id:
        claude = a.row("claude_review", "claude_reviews", "review_id", claude_id)
        a.link(
            claude,
            {
                "candidate_id": prop.get("candidate_id"),
                "symbol": prop.get("symbol"),
                "eval_id": prop.get("eval_id"),
                "proposal_id": prop.get("proposal_id"),
            },
            "claude_review",
            optional=("proposal_id",),
        )
        a.model(claude, "claude_review", primary=False)
        at = a.time(claude, "claude_review")
        a.before(times["evaluation"], at, "claude_review_before_evaluation")
        a.before(at, times["entry_order"], "claude_review_after_entry")
    origin_gaps = sorted(a.limits)
    final = _final_risk(
        a, prop, order, risk, times["proposal"], times["risk"], times["entry_order"]
    )
    evidence_hash = hashlib.sha256(
        json.dumps(_safe(a.evidence), sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    return {
        "version": VERSION,
        "complete": not a.limits,
        "recorded_origin_status": "verified_recorded_origin"
        if not origin_gaps
        else "limited_recorded_origin",
        "recorded_origin_limitations": origin_gaps,
        "final_execution_risk": final,
        "limitations": sorted(a.limits),
        "source_sha256": evidence_hash,
        "ids": {
            **identity,
            "outcome_id": actual.get("outcome_id"),
            "position_id": pos.get("position_id"),
            "order_id": order.get("order_id"),
            "eval_id": prop.get("eval_id"),
            "risk_check_id": order.get("risk_check_id"),
            "approval_id": approval_id,
            "claude_review_id": claude_id,
        },
        "model_call_authenticated": False,
        "broker_fees_verified": False,
        "policy_updated": False,
        "causal_lesson_claimed": False,
        "identity_basis": "recorded_metadata_and_local_anchors_not_provider_authentication",
    }


def validate_origin_provenance(journal, outcome: dict | None) -> dict:
    """Audit exact entry anchors in a single read snapshot; never initialize DB.

    SAVEPOINT also preserves an existing caller transaction. Database/schema
    errors propagate; malformed saved fields become named evidence limitations.
    The source digest detects changes, not authenticity of the underlying facts.
    """
    savepoint = "origin_audit_" + uuid4().hex
    journal.conn.execute("SAVEPOINT " + savepoint)
    try:
        return _validate(journal, outcome)
    finally:
        journal.conn.execute("RELEASE SAVEPOINT " + savepoint)
