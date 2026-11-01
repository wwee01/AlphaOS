"""Pinned HOLD10 receipt capture to draft analysis; never formal or prospective."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import cast

from alphaos.research import hold10_receipts as h
from alphaos.research.successor_inference import (
    METHOD as INFERENCE_METHOD,
    bundle_pins,
    evaluate_frozen,
)
from alphaos.research.successor_measurement import reconstruct_bracket, utc
from alphaos.stats.frozen_policy_inference import canonical_sha256 as sha

METHOD = "hold10_receipt_analysis_draft_v1"
FLOORS = {
    "effect_target": 0.05,
    "direction": "greater",
    "horizon_sessions": 10,
    "floor_effective_n": 30,
    "floor_span_days": 28,
    "analysis_not_before": "2026-10-05",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


class _NoDatabase:
    def __getattr__(self, name):
        raise RuntimeError("pure_plan_validation_must_not_access_database")


def _freeze(value):
    return json.loads(h._json(value))


def _receipt(row, kind, cutoff):
    if not isinstance(row, dict) or row.get("mode") != "dry_run":
        raise ValueError("invalid_dry_run_receipt")
    h._pin(row["payload"], row["payload_sha256"], "receipt_payload")
    h._pin(
        {k: v for k, v in row.items() if k not in {"payload", "receipt_sha256"}},
        row["receipt_sha256"],
        "receipt_metadata",
    )
    if utc(row["recorded_at_utc"]) > cutoff:
        raise ValueError(kind + "_recorded_after_analysis_cutoff")


def capture_hold10_receipts(connection, cohort_id, *, expected_manifest_sha256):
    """One read snapshot, including full chains and latest revision identities.

    This snapshot proves latestness only at its read transaction. Persistence
    must compare it again under its own short write transaction.
    """
    owned = not connection.in_transaction
    if owned:
        connection.execute("BEGIN")
    try:
        store = h.Hold10Receipts(connection)
        view = store.cohort_view(
            cohort_id, expected_manifest_sha256=expected_manifest_sha256
        )
        protocol = h._read(
            connection, h.TABLES[0], "protocol_id", view["cohort"]["protocol_id"]
        )
        return {
            "protocol": protocol,
            "view": view,
            "measurement_histories": {
                m["member_id"]: store._measurement_history(m) for m in view["members"]
            },
        }
    finally:
        if owned:
            connection.rollback()


def _validate_capture(capture, cutoff):
    p, view = capture["protocol"], capture["view"]
    cohort = view["cohort"]
    _receipt(p, "protocol", cutoff)
    _receipt(cohort, "cohort", cutoff)
    h._protocol_contract(p["payload"])
    manifest = cohort["payload"]
    if (
        cohort["protocol_id"] != p["protocol_id"]
        or p["protocol_id"] != p["payload"]["protocol_id"]
        or manifest["protocol_sha256"] != p["payload_sha256"]
        or cohort["cohort_id"] != manifest["cohort_id"]
        or utc(cohort["recorded_at_utc"]) < utc(p["recorded_at_utc"])
    ):
        raise ValueError("cohort_protocol_link_mismatch")
    opportunities = manifest["opportunities"]
    if not isinstance(opportunities, list) or any(
        not isinstance(x, dict) for x in opportunities
    ):
        raise ValueError("full_opportunity_manifest_required")
    ids = [h._text(o["opportunity_id"], "opportunity_id") for o in opportunities]
    declared = manifest["all_opportunity_ids"]
    if (
        not isinstance(declared, list)
        or len(set(ids)) != len(ids)
        or len(set(declared)) != len(declared)
        or set(ids) != set(declared)
    ):
        raise ValueError("manifest_membership_mismatch")
    # Reuse the receipt store's deterministic plan/risk/timing validator, with
    # an explicit I/O fuse instead of opening or initializing any database.
    validator = h.Hold10Receipts(cast(sqlite3.Connection, _NoDatabase()))
    prepared = validator._prepare_members(p, manifest, cohort["recorded_at_utc"])
    expected = {m["member_id"]: m for m in prepared}
    members, latest, histories = (
        view["members"],
        view["latest_measurements"],
        capture["measurement_histories"],
    )
    if (
        not isinstance(members, list)
        or not isinstance(latest, list)
        or not isinstance(histories, dict)
    ):
        raise ValueError("receipt_collections_required")
    if any(not isinstance(m, dict) for m in members) or len(members) != len(expected):
        raise ValueError("missing_or_extra_member_receipts")
    member_ids = [m["member_id"] for m in members]
    if (
        len(set(member_ids)) != len(member_ids)
        or set(member_ids) != set(expected)
        or set(histories) != set(expected)
        or len(latest) != len(members)
    ):
        raise ValueError("member_or_history_identity_mismatch")
    measured = {}
    for member, last in zip(members, latest):
        _receipt(member, "member", cutoff)
        intended = dict(expected[member["member_id"]])
        keys = {k: intended.pop(k) for k in ("member_id", "opportunity_id", "arm")}
        if (
            any(member[k] != v for k, v in keys.items())
            or member["cohort_id"] != cohort["cohort_id"]
            or member["recorded_at_utc"] != cohort["recorded_at_utc"]
            or member["payload"] != intended
        ):
            raise ValueError("member_disagrees_with_manifest")
        history = histories[member["member_id"]]
        if not isinstance(history, list):
            raise ValueError("measurement_chain_required")
        previous = None
        for revision, measurement in enumerate(history, 1):
            _receipt(measurement, "measurement", cutoff)
            data = measurement["payload"]
            if (
                measurement["member_id"] != member["member_id"]
                or measurement["revision"] != revision
                or measurement["previous_receipt_sha256"]
                != (previous["receipt_sha256"] if previous else None)
                or data["member_receipt_sha256"] != member["receipt_sha256"]
                or measurement["measurement_id"]
                != sha([member["member_id"], measurement["payload_sha256"]])
                or utc(measurement["recorded_at_utc"])
                < utc((previous or member)["recorded_at_utc"])
            ):
                raise ValueError("measurement_chain_identity_or_chronology_mismatch")
            if previous and previous["payload"]["measurement"]["status"] == "complete":
                raise ValueError("measurement_revision_after_frozen_completion")
            h._pin(data["source_bars"], data["source_bars_sha256"], "measurement_bars")
            payload = member["payload"]
            if not isinstance(data["source_bars"], list) or any(
                not isinstance(bar, dict)
                or bar.get("symbol") != payload["input"]["symbol"]
                for bar in data["source_bars"]
            ):
                raise ValueError("foreign_or_malformed_measurement_bars")
            if (
                data["original_arm_risk_per_share"]
                != payload["original_arm_risk_per_share"]
                or data["prospective_enrollment"] is not False
                or data["formal_allowed"] is not False
            ):
                raise ValueError("measurement_risk_or_mode_mismatch")
            if data["measurement"]["status"] == "complete":
                if (
                    not payload["eligible"]
                    or payload["plan"] is None
                    or payload["plan_status"] == "unavailable"
                ):
                    raise ValueError("unusable_member_claims_complete_measurement")
                actual = reconstruct_bracket(
                    {**payload["plan"], "decision_at_utc": payload["decision_at_utc"]},
                    data["source_bars"],
                    10,
                    measurement["recorded_at_utc"],
                )
                if actual != data["measurement"]:
                    raise ValueError("derived_measurement_disagrees_with_saved_sources")
            previous = measurement
        if last != previous:
            raise ValueError("view_does_not_use_latest_supplied_revision")
        measured[member["member_id"]] = previous
    missing = sum(
        m is None or m["payload"]["measurement"]["status"] != "complete"
        for m in measured.values()
    )
    if (
        view["member_count"] != len(members)
        or view["opportunities"] != len(opportunities)
        or view["missing_measurements"] != missing
        or any(
            view[k] is not False
            for k in ("prospective_enrollment", "formal_allowed", "promotion_allowed")
        )
    ):
        raise ValueError("view_counters_or_flags_disagree_with_receipts")
    return p, cohort, members, measured


def analyze_hold10_capture(
    capture, specification, *, expected_capture_sha256, expected_specification_sha256
):
    """Validate/reconstruct frozen evidence, then compute draft-only inference.

    An external export cannot attest that a newer database revision was omitted;
    the storage adapter must perform the final latest-capture CAS.
    """
    result: dict = {
        "method": METHOD,
        "status": "invalid_receipt_analysis",
        "formal_allowed": False,
        "promotion_allowed": False,
        "prospective_enrollment": False,
        "formal_p": None,
        "q_value": None,
        "blockers": [],
        "inference": None,
        "latestness": "supplied_capture_only_requires_storage_CAS",
    }
    try:
        capture, specification = _freeze(capture), _freeze(specification)
        h._pin(capture, expected_capture_sha256, "capture_pin")
        h._pin(
            specification, expected_specification_sha256, "analysis_specification_pin"
        )
        if (
            specification["method"] != METHOD
            or specification["status"] != "draft"
            or specification["predecessor_id"] != h.PREDECESSOR
            or specification["original_constraints"] != FLOORS
            or not h._text(specification["analysis_id"], "analysis_id").startswith(
                "hold10-analysis-draft:"
            )
        ):
            raise ValueError(
                "original_hold10_constraints_and_new_draft_identity_required"
            )
        if specification.get("formal_allowed") or specification.get("activated"):
            raise ValueError("draft_analysis_cannot_activate_or_finalize")
        h._text(specification["family_id"], "fixed_draft_family")
        if specification["stopping_rule"] != "fixed_pinned_snapshot_draft_only":
            raise ValueError("explicit_fixed_snapshot_stopping_required")
        cutoff = utc(specification["analysis_at_utc"])
        if cutoff > utc(_now()):
            raise ValueError("analysis_cutoff_after_actual_time")
        protocol, cohort, members, measured = _validate_capture(capture, cutoff)
        if specification["population"] != protocol["payload"]["population"]:
            raise ValueError("analysis_population_disagrees_with_protocol")
        opportunities = cohort["payload"]["opportunities"]
        unknown = [
            o["opportunity_id"]
            for o in opportunities
            if (
                not o["eligible"]
                and o["eligibility_reason"]
                != specification["population"] + ":abstained"
            )
            or (
                o["eligible"]
                and (
                    o["eligibility_reason"].endswith(":unresolved")
                    or o["eligibility_reason"].endswith(":abstained")
                )
            )
        ]
        selected = [
            o
            for o in opportunities
            if o["eligible"] and o["opportunity_id"] not in unknown
        ]
        decisions = [utc(o["decision_at_utc"]) for o in selected]
        span = (
            (max(decisions) - min(decisions)).total_seconds() / 86400
            if decisions
            else 0.0
        )
        result.update(
            status="draft_analysis",
            opportunities=len(opportunities),
            member_count=len(members),
            selected_opportunities=len(selected),
            known_abstentions=len(opportunities) - len(selected) - len(unknown),
            unresolved_opportunity_ids=unknown,
            selected_span_days=span,
            capture_sha256=expected_capture_sha256,
            specification_sha256=expected_specification_sha256,
            source_protocol_receipt_sha256=protocol["receipt_sha256"],
            selected_missing_member_ids=[
                m["member_id"]
                for m in members
                if m["payload"]["eligible"]
                and (
                    measured[m["member_id"]] is None
                    or measured[m["member_id"]]["payload"]["measurement"]["status"]
                    != "complete"
                )
            ],
        )
        policies = protocol["payload"]["policies"]
        if (
            policies["tested"].get("source") != "openai_evaluations"
            or policies["tested"].get("decision_meaning") != specification["population"]
            or policies["reference"].get("rule_version") != "threshold_v1_hold10"
            or policies["reference"].get("setup_card_id") != "catalyst_momentum_v3"
        ):
            result["blockers"].append("unverified_ai_threshold_policy_identity")
        if unknown:
            result["blockers"].append("unresolved_population_membership")
        if not selected:
            result["blockers"].append("no_selected_opportunities")
        if cutoff < utc("2026-10-05T00:00:00Z"):
            result["blockers"].append("original_analysis_not_before_2026_10_05")
        if span < 28:
            result["blockers"].append("original_28_day_span_not_met")
        if result["selected_missing_member_ids"]:
            result["blockers"].append(
                "incomplete_selected_cohort_no_complete_case_inference"
            )
        c = {
            "method": INFERENCE_METHOD,
            "status": "draft",
            "statistic": "paired_mean",
            "direction": "greater",
            "effect_target": 0.05,
            "min_components_per_arm": 30,
            "analysis_at_utc": specification["analysis_at_utc"],
            **{
                k: specification[k]
                for k in (
                    "confidence_level",
                    "resamples",
                    "seed",
                    "max_component_share",
                    "dependence_buffer_seconds",
                )
            },
        }
        inputs, outcomes = [], []
        for m in members:
            p = m["payload"]
            if not p["eligible"] or m["opportunity_id"] in unknown:
                continue
            measurement = measured[m["member_id"]]
            observation = measurement["payload"]["measurement"] if measurement else {}
            bars = measurement["payload"]["source_bars"] if measurement else []
            source = {
                "member_receipt": m,
                "cohort_receipt_sha256": cohort["receipt_sha256"],
            }
            inputs.append(
                {
                    "member_id": m["member_id"],
                    "observation_id": m["member_id"],
                    "opportunity_id": m["opportunity_id"],
                    "pair_id": m["opportunity_id"],
                    "arm": m["arm"],
                    "input": source,
                    "input_sha256": sha(source),
                    "dependence_keys": sorted({"bar:" + sha(b) for b in bars}),
                }
            )
            evidence = measurement or {"status": "missing_measurement_receipt"}
            outcomes.append(
                {
                    "member_id": m["member_id"],
                    "status": observation.get("status", "missing"),
                    "value": observation.get("value"),
                    "evidence": evidence,
                    "evidence_sha256": sha(evidence),
                    "start_utc": observation.get("measurement_start_utc"),
                    "end_utc": observation.get("dependence_end_utc"),
                    "observed_at_utc": measurement["recorded_at_utc"]
                    if measurement
                    else specification["analysis_at_utc"],
                }
            )
        pins = bundle_pins(c, inputs, outcomes)
        result["inference_inputs"] = {
            "contract": c,
            "members": inputs,
            "outcomes": outcomes,
            "pins": pins,
        }
        # Validate settings and expose support/missingness even before calendar
        # readiness, but never publish an interval/p from an ineligible cohort.
        numerical = evaluate_frozen(c, inputs, outcomes, expected_pins=pins)
        result["dependence_support"] = {
            k: numerical.get(k)
            for k in (
                "n_components",
                "components_per_arm",
                "max_component_share_per_arm",
                "component_members",
            )
        }
        if numerical["status"] != "draft_inference":
            result["blockers"].extend(numerical.get("blockers", []))
        if not result["blockers"]:
            result["inference"] = numerical
        else:
            result["status"] = "blocked_draft_analysis"
        return result
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        return {
            **result,
            "status": "invalid_receipt_analysis",
            "blockers": [str(exc)],
            "inference": None,
        }
