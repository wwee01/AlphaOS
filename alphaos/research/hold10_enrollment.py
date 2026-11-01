"""Input-first saved-batch HOLD10 adapter. No runtime hook or implicit writes.

This imports saved AI versus threshold_v1_hold10 decisions into the disabled
receipt API. It never reads outcomes, replays, prices after the decision or
broker state. All exports/enrollment remain draft DRY RUN, never prospective.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, time, timezone
import json
import sqlite3
from typing import NoReturn
from zoneinfo import ZoneInfo

from alphaos.baseline.rules import _input_sha
from alphaos.research.hold10_receipts import Hold10Receipts, _protocol_contract
from alphaos.research.successor_measurement import finite, utc
from alphaos.stats.frozen_policy_inference import canonical_sha256 as sha
from alphaos.util.market_calendar import nth_trading_day_after

VERSION = "hold10_saved_batch_adapter_v1"
REFERENCE = "threshold_v1_hold10"
AI_IDENTITY_FIELDS = (
    "model",
    "model_provider",
    "prompt_template_version",
    "schema_version",
    "system_prompt_hash",
)
ET = ZoneInfo("America/New_York")

FIELDS = {
    "scan_batches": "id,scan_batch_id,status,started_at_utc,completed_at_utc,created_at_utc,candidates_found",
    "candidates": "id,candidate_id,scan_batch_id,symbol,direction,interest_score,price_at_scan,shadow_tier,created_at_utc",
    "candidate_packets": "id,packet_id,candidate_id,scan_batch_id,symbol,packet_json,created_at_utc",
    "openai_evaluations": "id,eval_id,candidate_id,symbol,decision,direction,entry,stop,target,max_holding_days,validation_status,is_mock,model,model_provider,prompt_template_version,schema_version,prompt_hash,system_prompt_hash,snapshot_json,created_at_utc",
    "decision_adjustments": "id,adjustment_id,candidate_id,scan_batch_id,packet_id,symbol,eval_decision,final_decision,evidence_json,created_at_utc",
    "shadow_baseline_decisions": "id,baseline_decision_id,candidate_id,scan_batch_id,symbol,rule_version,decision,direction,entry,stop,target,max_holding_days,setup_card_id,input_sha,decision_at_utc,created_at_utc",
}


class InvalidSavedInput(ValueError):
    """A code-only failure, safe to return without raw source/error payloads."""


def _fail(code) -> NoReturn:
    raise InvalidSavedInput(code)


def _freeze(value):
    sha(value)  # rejects nonfinite/unserializable values
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


def _pinned(value, expected, code):
    if not isinstance(expected, str) or len(expected) != 64 or sha(value) != expected:
        _fail(code)


def _text(value, code):
    if not isinstance(value, str) or not value.strip():
        _fail(code)
    return value


@contextmanager
def _snapshot(c):
    owned = not c.in_transaction
    if owned:
        c.execute("BEGIN")
    try:
        yield
    finally:
        if owned:
            c.rollback()


def _rows(c, table, where, args):
    cursor = c.execute(
        f"SELECT {FIELDS[table]} FROM {table} WHERE {where} ORDER BY id", args
    )
    names = [col[0] for col in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _object_json(raw, code):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail(code)
            result[key] = value
        return result

    try:
        value = (
            json.loads(raw, object_pairs_hook=unique_pairs)
            if isinstance(raw, str)
            else raw
        )
        value = _freeze(value)
    except (ValueError, TypeError):
        _fail(code)
    if not isinstance(value, dict) or not value:
        _fail(code)
    return value


def _contract(protocol, request, expected_protocol_sha256, expected_request_sha256):
    _pinned(protocol, expected_protocol_sha256, "protocol_pin_mismatch")
    _pinned(request, expected_request_sha256, "request_pin_mismatch")
    _protocol_contract(protocol)
    if protocol.get("enrollment_adapter") != {
        "version": VERSION,
        "population_scope": "core_completed_scan_batch",
        "reference_rule": REFERENCE,
        "ai_horizon": "fixed_10_daily_proxy_preserve_original_saved_hold",
    }:
        _fail("explicit_adapter_contract_required")
    policies = protocol["policies"]
    if (
        policies["tested"].get("source") != "openai_evaluations"
        or policies["tested"].get("decision_meaning") != protocol["population"]
        or policies["reference"].get("rule_version") != REFERENCE
    ):
        _fail("pinned_ai_threshold_policy_identity_required")
    identity = policies["tested"].get("identity")
    if not isinstance(identity, dict) or set(identity) != set(AI_IDENTITY_FIELDS):
        _fail("fixed_ai_policy_identity_required")
    for key in AI_IDENTITY_FIELDS:
        _text(identity[key], "fixed_ai_policy_identity_required")
    if policies["reference"].get("setup_card_id") != "catalyst_momentum_v3":
        _fail("fixed_hold10_reference_card_required")
    if not isinstance(request, dict):
        _fail("request_object_required")
    for key in ("cohort_id", "scan_batch_id"):
        _text(request.get(key), f"missing_{key}")
    if utc(request.get("as_of_utc")) > datetime.now(timezone.utc):
        _fail("future_capture_boundary")
    source = request.get("source")
    if not isinstance(source, dict) or source.get("kind") not in {
        "archived_copy",
        "existing_current_inputs",
    }:
        _fail("saved_source_kind_required")
    digest = source.get("sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        _fail("saved_source_identity_required")


def build_saved_hold10_manifest(
    connection: sqlite3.Connection,
    *,
    protocol: dict,
    expected_protocol_sha256: str,
    request: dict,
    expected_request_sha256: str,
) -> dict:
    """SELECT-only export over one consistent observed batch.

    Membership is all core candidates in the declared completed batch, even
    without an evaluation/control. Source hashes identify observations; they
    do not certify scheduled-scan completeness or original policy execution.
    """
    protocol, request = _freeze(protocol), _freeze(request)
    _contract(protocol, request, expected_protocol_sha256, expected_request_sha256)
    boundary, batch_id = utc(request["as_of_utc"]), request["scan_batch_id"]
    with _snapshot(connection):
        batches = _rows(connection, "scan_batches", "scan_batch_id=?", (batch_id,))
        if len(batches) != 1:
            _fail("unique_observed_batch_required")
        batch = batches[0]
        if (
            batch["status"] != "completed"
            or not batch.get("completed_at_utc")
            or not utc(batch["created_at_utc"])
            <= utc(batch["completed_at_utc"])
            <= boundary
            or not utc(batch["started_at_utc"]) <= utc(batch["completed_at_utc"])
        ):
            _fail("completed_batch_before_capture_required")
        # Select and freeze the entire membership before inspecting either arm.
        candidates = _rows(connection, "candidates", "scan_batch_id=?", (batch_id,))
        core = [r for r in candidates if r["shadow_tier"] in (None, 0)]
        if any(r["shadow_tier"] not in (None, 0, 1) for r in candidates):
            _fail("unknown_candidate_population")
        ids = [_text(r["candidate_id"], "candidate_id_required") for r in core]
        if len(set(ids)) != len(ids):
            _fail("duplicate_candidate_identity")
        # The stored count is a cross-check, not an independent market census.
        if batch["candidates_found"] != len(core):
            _fail("observed_core_batch_count_mismatch")
        for r in core:
            _text(r["symbol"], "candidate_symbol_required")
            if (
                not utc(batch["started_at_utc"])
                <= utc(r["created_at_utc"])
                <= utc(batch["completed_at_utc"])
            ):
                _fail("candidate_outside_completed_batch_chronology")
        saved = {}
        for candidate in core:
            cid = candidate["candidate_id"]
            saved[cid] = {
                table: _rows(connection, table, "candidate_id=?", (cid,))
                for table in (
                    "candidate_packets",
                    "openai_evaluations",
                    "decision_adjustments",
                    "shadow_baseline_decisions",
                )
            }
    manifest = {
        "cohort_id": request["cohort_id"],
        "protocol_sha256": expected_protocol_sha256,
        "all_opportunity_ids": ids,
        "source": {
            **request["source"],
            "scope": "all observed core candidates in one completed declared batch",
            "adapter_version": VERSION,
            "request": request,
            "request_sha256": expected_request_sha256,
            "batch": batch,
            "batch_sha256": sha(batch),
            "provenance_certified": False,
            "all_scheduled_opportunities_certified": False,
        },
        "opportunities": [],
    }
    diagnostics = []
    for candidate in core:
        opportunity, detail = _opportunity(
            candidate, saved[candidate["candidate_id"]], batch, protocol
        )
        manifest["opportunities"].append(opportunity)
        diagnostics.append(detail)
    return {
        "status": "dry_run_export",
        "manifest": manifest,
        "manifest_sha256": sha(manifest),
        "diagnostics": diagnostics,
        "declared_opportunities": len(core),
        "excluded_shadow_candidates": len(candidates) - len(core),
        "membership_states": dict(Counter(d["membership_state"] for d in diagnostics)),
        "issue_counts": dict(
            Counter(issue for d in diagnostics for issue in d["issues"])
        ),
        "prospective_enrollment": False,
        "formal_allowed": False,
        "promotion_allowed": False,
    }


def _opportunity(candidate, saved, batch, protocol):
    cid, symbol = candidate["candidate_id"], candidate["symbol"]
    issues, sources = [], {}
    end = utc(batch["completed_at_utc"])

    def unique(table, rule=None):
        rows = [
            r for r in saved[table] if rule is None or r.get("rule_version") == rule
        ]
        name = rule or table
        if len(rows) != 1:
            issues.append(f"{name}:missing" if not rows else f"{name}:duplicate")
            return None
        row = rows[0]
        sources[name] = row
        try:
            valid = (
                row["candidate_id"] == cid
                and row["symbol"] == symbol
                and row.get("scan_batch_id", batch["scan_batch_id"])
                == batch["scan_batch_id"]
                and utc(candidate["created_at_utc"])
                <= utc(row["created_at_utc"])
                <= end
            )
        except (ValueError, TypeError):
            valid = False
        if not valid:
            issues.append(f"{name}:identity_or_chronology_mismatch")
            return None
        return row

    packet = unique("candidate_packets")
    ev = unique("openai_evaluations")
    baseline = unique("shadow_baseline_decisions", REFERENCE)
    # This control is recorded for visibility only, never a paired arm.
    other = [
        r
        for r in saved["shadow_baseline_decisions"]
        if r.get("rule_version") == "propose_all_v1_hold10"
    ]
    adj = (
        unique("decision_adjustments")
        if protocol["population"] == "final_proposed"
        else None
    )
    packet_input, ai_input = None, None
    if packet:
        try:
            packet_input = _object_json(
                packet["packet_json"], "packet_input_missing_or_malformed"
            )
            if packet_input.get("symbol") != symbol:
                _fail("packet_input_symbol_mismatch")
        except InvalidSavedInput as exc:
            issues.append(str(exc))
            packet_input = None
    if ev:
        try:
            if any(
                ev.get(k) != protocol["policies"]["tested"]["identity"][k]
                for k in AI_IDENTITY_FIELDS
            ):
                _fail("ai_saved_policy_identity_mismatch")
            if ev["is_mock"] != 0 or ev["validation_status"] != "passed":
                _fail("ai_mock_or_unvalidated")
            ai_input = _object_json(
                ev["snapshot_json"], "ai_input_missing_or_malformed"
            )
            if ai_input.get("symbol") != symbol or ai_input.get("is_mock") is not False:
                _fail("ai_input_symbol_or_mock_mismatch")
            for key in (
                "received_at",
                "quote_timestamp",
                "bar_timestamp",
                "source_timestamp",
            ):
                if ai_input.get(key) and utc(ai_input[key]) > utc(ev["created_at_utc"]):
                    _fail("ai_input_after_decision")
            if packet and utc(packet["created_at_utc"]) > utc(ev["created_at_utc"]):
                _fail("packet_recorded_after_ai_decision")
        except (InvalidSavedInput, ValueError, TypeError) as exc:
            issues.append(
                str(exc)
                if isinstance(exc, InvalidSavedInput)
                else "ai_input_timestamp_invalid"
            )
            ai_input = None
    primary = ev.get("decision") if ev else None
    decision = primary
    anchor = ev["created_at_utc"] if ev else candidate["created_at_utc"]
    if protocol["population"] == "final_proposed":
        decision = None
        if adj and ev:
            try:
                evidence = _object_json(
                    adj["evidence_json"], "final_evidence_missing_or_malformed"
                )
                diagnostic = evidence.get("watch_advisory_diagnostic")
                if diagnostic is None:
                    issues.append(
                        "final_eval_id_not_saved_candidate_packet_time_link_only"
                    )
                if diagnostic is not None and (
                    not isinstance(diagnostic, dict)
                    or diagnostic.get("eval_id") != ev["eval_id"]
                ):
                    _fail("final_evaluation_id_mismatch")
            except InvalidSavedInput as exc:
                issues.append(str(exc))
                adj = None
        if adj and ev:
            if (
                adj["eval_decision"] != primary
                or not packet
                or adj["packet_id"] != packet["packet_id"]
                or utc(adj["created_at_utc"]) < utc(ev["created_at_utc"])
            ):
                issues.append("final_decision_lineage_mismatch")
            else:
                decision, anchor = adj["final_decision"], adj["created_at_utc"]
    if decision not in {"propose", "watch", "reject"}:
        issues.append("population_decision_unresolved")
        state = "unresolved"
    else:
        state = "selected" if decision == "propose" else "abstained"
    inputs = {
        "symbol": symbol,
        "candidate": candidate,
        "packet": packet_input,
        "ai_snapshot": ai_input,
        "input_contract": "common_saved_opportunity_bundle_not_policy_reexecution",
    }
    input_sha = sha(inputs)
    risk_sha = sha(protocol["risk_contract"])

    def plan(arm, row, action):
        if row is None:
            return None
        out = {
            "symbol": symbol,
            "decision": action,
            "input_sha256": input_sha,
            "horizon_sessions": 10,
            "policy_sha256": sha(protocol["policies"][arm]),
            "risk_contract_sha256": risk_sha,
            "original_saved_max_holding_days": row.get("max_holding_days"),
            "source_row_sha256": sha(row),
        }
        if action == "propose":
            entry, stop, target = (row.get(k) for k in ("entry", "stop", "target"))
            if not (
                finite(entry)
                and finite(stop)
                and finite(target)
                and min(entry, stop, target) > 0
                and (
                    (row.get("direction") == "long" and stop < entry < target)
                    or (row.get("direction") == "short" and target < entry < stop)
                )
            ):
                issues.append(f"{arm}:invalid_original_bracket")
                return None
            out.update(
                entry=entry,
                stop=stop,
                target=target,
                direction=row["direction"],
                original_risk_per_share=abs(entry - stop),
            )
        return out

    arms = {}
    if ev and ai_input and packet_input and state != "unresolved":
        arms["tested"] = plan(
            "tested", ev, "propose" if state == "selected" else "unavailable"
        )
    if baseline:
        try:
            if (
                baseline["setup_card_id"]
                != protocol["policies"]["reference"]["setup_card_id"]
            ):
                _fail("baseline_saved_card_mismatch")
            if not ev or not ai_input:
                _fail("baseline_input_preimage_unavailable")
            if (
                not utc(ev["created_at_utc"])
                <= utc(baseline["decision_at_utc"])
                <= utc(baseline["created_at_utc"])
            ):
                _fail("baseline_decision_chronology_mismatch")
            if (
                utc(anchor).astimezone(ET).date()
                != utc(baseline["decision_at_utc"]).astimezone(ET).date()
            ):
                _fail("baseline_decision_session_mismatch")
            policy = ai_input.get("atr_policy")
            if (
                not isinstance(policy, dict)
                or "atr_14" not in policy
                or not finite(policy.get("min_reward_risk"))
                or policy["min_reward_risk"] <= 0
            ):
                _fail("baseline_input_preimage_unavailable")
            row = {
                "symbol": symbol,
                "direction": candidate["direction"],
                "interest_score": candidate["interest_score"],
                "last_price": ai_input.get("last_price"),
            }
            calculated = _input_sha(
                row,
                "threshold_v1",
                atr_14=policy.get("atr_14"),
                min_reward_risk=policy["min_reward_risk"],
                max_holding_days_default=10,
            )
            if calculated != baseline["input_sha"]:
                _fail("baseline_input_hash_unverified")
            if baseline["decision"] not in {"propose", "no_action", "unavailable"}:
                _fail("baseline_unknown_action_not_cash")
            if baseline["decision"] == "propose" and baseline["max_holding_days"] != 10:
                _fail("baseline_saved_horizon_mismatch")
            arms["reference"] = plan("reference", baseline, baseline["decision"])
            anchor = max(anchor, baseline["decision_at_utc"], key=utc)
        except (InvalidSavedInput, ValueError, TypeError) as exc:
            issues.append(
                str(exc)
                if isinstance(exc, InvalidSavedInput)
                else "baseline_source_malformed"
            )
    arms = {k: v for k, v in arms.items() if v is not None}
    # Bind the paired decision anchor without borrowing timestamps from rejected
    # foreign/late source rows. All raw saved rows remain in the manifest even
    # when they cannot supply a usable arm. Outputs never enter market inputs.
    available = (
        ev["created_at_utc"]
        if ai_input and ev
        else packet["created_at_utc"]
        if packet_input and packet
        else candidate["created_at_utc"]
    )
    anchor_basis = "saved_paired_decision"
    if ev is None or utc(available) > utc(anchor):
        # Preserve unusable/missing-decision members without inventing an AI
        # decision time. This observation placeholder cannot supply an AI plan.
        anchor = max(anchor, available, key=utc)
        anchor_basis = "input_observation_placeholder_unusable_decision"
    opportunity = {
        "opportunity_id": cid,
        "input": inputs,
        "input_sha256": input_sha,
        "decision_at_utc": anchor,
        "decision_anchor_basis": anchor_basis,
        "input_available_at_utc": available,
        "eligible": state == "selected",
        "eligibility_reason": f"{protocol['population']}:{state}",
        "arms": arms,
        "adapter_issues": sorted(set(issues)),
        "saved_decision_sources": saved,
        "original_ai_decision_at_utc": ev["created_at_utc"] if ev else None,
    }
    first_date = nth_trading_day_after(utc(anchor).astimezone(ET).date(), 1)
    start = datetime.combine(first_date, time(9, 30), ET)
    return opportunity, {
        "candidate_id": cid,
        "membership_state": state,
        "issues": sorted(set(issues)),
        "missing_arms": sorted({"tested", "reference"} - set(arms)),
        "propose_all_diagnostic_rows": len(other),
        "propose_all_used_as_arm": False,
        "source_rows_sha256": sha(saved),
        "batch_finished_before_measurement_start": end <= start,
        "batch_timing_is_not_prospective_certification": True,
    }


def enroll_saved_hold10(
    source_connection,
    receipt_connection,
    *,
    enabled: bool = False,
    protocol=None,
    expected_protocol_sha256=None,
    request=None,
    expected_request_sha256=None,
    expected_manifest_sha256=None,
) -> dict:
    """Optional isolated diagnostic write. Never initializes schema/protocol.

    Callers must first inspect/pin an export and explicitly initialize/register
    the scratch receiver through the receipt API. No live decision is accepted,
    returned, changed or executed by this function. Failures remain visible.
    """
    if enabled is not True:
        return {
            "status": "disabled",
            "writes": False,
            "prospective_enrollment": False,
            "formal_allowed": False,
        }
    stage = "input_export"
    try:
        export = build_saved_hold10_manifest(
            source_connection,
            protocol=protocol,
            expected_protocol_sha256=expected_protocol_sha256,
            request=request,
            expected_request_sha256=expected_request_sha256,
        )
        stage = "manifest_pin"
        _pinned(
            export["manifest"],
            expected_manifest_sha256,
            "manifest_changed_since_inspection",
        )
        stage = "receipt_persistence"
        view = Hold10Receipts(receipt_connection, dry_run=True).enroll_cohort(
            protocol["protocol_id"],
            export["manifest"],
            expected_protocol_sha256=expected_protocol_sha256,
            expected_manifest_sha256=expected_manifest_sha256,
        )
        return {**export, "status": "dry_run_enrolled", "receipts": view}
    except (
        Exception
    ) as exc:  # diagnostic boundary: visible failure, never changes a live decision
        code = str(exc) if isinstance(exc, InvalidSavedInput) else "diagnostic_failed"
        return {
            "status": "diagnostic_error",
            "stage": stage,
            "error_code": code,
            "error_type": type(exc).__name__,
            "prospective_enrollment": False,
            "formal_allowed": False,
        }
