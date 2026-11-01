"""Fixed multi-cohort HOLD10 draft study, explicitly initialized manual storage.

Existing input/measurement receipts remain authoritative. No collector, startup
hook, schema migration, historical rewrite, formal verdict or activation.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
import json
import sqlite3

from alphaos.research import hold10_analysis as a
from alphaos.research import hold10_receipts as h
from alphaos.research.successor_inference import bundle_pins, evaluate_frozen
from alphaos.research.successor_measurement import utc
from alphaos.stats.frozen_policy_inference import canonical_sha256 as sha

METHOD = "hold10_fixed_study_draft_v1"
TABLES = ("research_hold10_studies", "research_hold10_study_analyses")
DDL = (
    f"CREATE TABLE IF NOT EXISTS {TABLES[0]} (study_id TEXT PRIMARY KEY, {h._COMMON})",
    f"CREATE TABLE IF NOT EXISTS {TABLES[1]} (analysis_id TEXT PRIMARY KEY, study_id TEXT NOT NULL UNIQUE REFERENCES {TABLES[0]}(study_id), capture_sha256 TEXT NOT NULL, {h._COMMON})",
) + tuple(
    f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable HOLD10 study receipt'); END"
    for table in TABLES
    for action in ("UPDATE", "DELETE")
)
FLAGS = {
    "formal_allowed": False,
    "promotion_allowed": False,
    "prospective_enrollment": False,
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _freeze(value):
    return json.loads(h._json(value))


@lru_cache(maxsize=1)
def _expected_schema():
    # SQLite canonicalizes CREATE statements; compare its own normalized schema
    # including implicit unique indexes, not brittle substring trigger checks.
    c = sqlite3.connect(":memory:")
    try:
        for statement in DDL:
            c.execute(statement)
        return tuple(
            c.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name"
            ).fetchall()
        )
    finally:
        c.close()


def _schema_objects(c):
    expected = _expected_schema()
    names = [row[1] for row in expected]
    marks = ",".join("?" for _ in names)
    return tuple(
        tuple(row)
        for row in c.execute(
            f"SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name IN ({marks}) OR tbl_name IN (?,?) ORDER BY name",
            (*names, *TABLES),
        )
    )


def _verify_schema(c):
    if _schema_objects(c) != _expected_schema():
        raise ValueError("study_receipt_schema_or_immutable_triggers_mismatch")


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


def initialize_dry_run_studies(connection, *, purpose):
    if purpose != "scratch_or_manual_dry_run":
        raise ValueError("explicit_dry_run_initialization_required")
    with h._atomic(connection):
        if _schema_objects(connection):
            _verify_schema(connection)
            return
        for sql in DDL:
            connection.execute(sql)
        _verify_schema(connection)


def _contract(study):
    if (
        not isinstance(study, dict)
        or study.get("method") != METHOD
        or study.get("status") != "draft"
    ):
        raise ValueError("fixed_draft_study_required")
    if not h._text(study.get("study_id"), "study_id").startswith("hold10-study-draft:"):
        raise ValueError("new_study_namespace_required")
    if any(study.get(k) is not False for k in FLAGS):
        raise ValueError("study_cannot_activate_or_claim_prospective")
    h._text(study.get("protocol_id"), "protocol_id")
    h._sha(study.get("protocol_sha256"), "protocol_sha256")
    h._text(study.get("source_revision"), "source_revision")
    h._text(study.get("source_kind"), "source_kind")
    s = study["analysis_specification"]
    if (
        not isinstance(s, dict)
        or s.get("method") != a.METHOD
        or s.get("status") != "draft"
        or s.get("predecessor_id") != h.PREDECESSOR
        or s.get("original_constraints") != a.FLOORS
        or not h._text(s.get("analysis_id"), "analysis_id").startswith(
            "hold10-analysis-draft:"
        )
        or s.get("stopping_rule") != "fixed_pinned_snapshot_draft_only"
        or s.get("population") not in {"primary_proposed", "final_proposed"}
        or s.get("formal_allowed")
        or s.get("activated")
    ):
        raise ValueError("fixed_original_analysis_contract_required")
    h._text(s.get("family_id"), "family_id")
    if (
        type(s.get("resamples")) is not int
        or not 100 <= s["resamples"] <= 100000
        or type(s.get("seed")) is not int
        or s["seed"] < 0
        or type(s.get("dependence_buffer_seconds")) is not int
        or s["dependence_buffer_seconds"] < 0
        or not h.finite(s.get("confidence_level"))
        or not 0.5 < s["confidence_level"] < 1
        or not h.finite(s.get("max_component_share"))
        or not 0 < s["max_component_share"] < 1
    ):
        raise ValueError("invalid_fixed_analysis_settings")
    end = utc(study["enrollment_at_or_before_utc"])
    if end > utc(s["analysis_at_utc"]):
        raise ValueError("enrollment_after_fixed_analysis_cutoff")
    roster = study["cohorts"]
    if not isinstance(roster, list) or not roster or len(roster) > 1000:
        raise ValueError("nonempty_bounded_explicit_cohort_roster_required")
    ids = []
    for item in roster:
        if not isinstance(item, dict) or set(item) != {"cohort_id", "manifest_sha256"}:
            raise ValueError("exact_cohort_manifest_pin_required")
        ids.append(h._text(item["cohort_id"], "cohort_id"))
        h._sha(item["manifest_sha256"], "manifest_sha256")
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_cohort_in_roster")
    return s


def _inputs(study, protocol, cohorts, *, cutoff):
    """Validate only immutable input membership; no measurements are selected."""
    a._receipt(protocol, "protocol", cutoff)
    h._protocol_contract(protocol["payload"])
    if (
        protocol["protocol_id"] != study["protocol_id"]
        or protocol["payload_sha256"] != study["protocol_sha256"]
        or protocol["payload"]["source_revision"] != study["source_revision"]
        or protocol["payload"]["population"]
        != study["analysis_specification"]["population"]
    ):
        raise ValueError("study_protocol_or_source_revision_mismatch")
    seen: set[str] = set()
    batches: set[str] = set()
    if len(cohorts) != len(study["cohorts"]):
        raise ValueError("missing_or_extra_study_cohort")
    for pin, cohort in zip(study["cohorts"], cohorts):
        a._receipt(cohort, "cohort", cutoff)
        manifest = cohort["payload"]
        if (
            cohort["cohort_id"] != pin["cohort_id"]
            or manifest["cohort_id"] != pin["cohort_id"]
            or cohort["payload_sha256"] != pin["manifest_sha256"]
            or cohort["protocol_id"] != protocol["protocol_id"]
            or manifest["protocol_sha256"] != protocol["payload_sha256"]
            or utc(cohort["recorded_at_utc"]) < utc(protocol["recorded_at_utc"])
        ):
            raise ValueError("study_cohort_manifest_or_protocol_mismatch")
        source = manifest["source"]
        if source.get("kind") != study["source_kind"] or (
            source.get("source_revision") is not None
            and source["source_revision"] != study["source_revision"]
        ):
            raise ValueError("incompatible_cohort_source_identity")
        batch = source.get("batch", {}).get("scan_batch_id")
        if batch is not None:
            if batch in batches:
                raise ValueError("duplicate_source_scan_batch")
            batches.add(batch)
        ids = manifest["all_opportunity_ids"]
        opportunities = manifest["opportunities"]
        if not isinstance(ids, list) or not isinstance(opportunities, list):
            raise ValueError("full_opportunity_manifest_required")
        if (
            len(ids) != len(set(ids))
            or set(ids) != {o["opportunity_id"] for o in opportunities}
            or len(ids) != len(opportunities)
        ):
            raise ValueError("cohort_membership_mismatch")
        if seen.intersection(ids):
            raise ValueError("duplicate_opportunity_across_cohorts")
        seen.update(ids)
        if any(
            utc(o["decision_at_utc"]) > utc(study["enrollment_at_or_before_utc"])
            for o in opportunities
        ):
            raise ValueError("opportunity_after_fixed_enrollment_cutoff")


def _study_receipt(receipt, now):
    a._receipt(receipt, "study", now)
    contract = receipt["payload"]
    spec = _contract(contract)
    if receipt["study_id"] != contract["study_id"] or utc(
        receipt["recorded_at_utc"]
    ) > utc(spec["analysis_at_utc"]):
        raise ValueError("study_identity_or_registration_cutoff_mismatch")
    return contract


def analyze_study_capture(capture, *, expected_capture_sha256):
    """One full-roster joint diagnostic; incomplete cohorts remain blockers."""
    result: dict = {
        "method": METHOD,
        "status": "invalid_study_analysis",
        **FLAGS,
        "formal_p": None,
        "q_value": None,
        "inference": None,
        "blockers": [],
    }
    try:
        capture = _freeze(capture)
        h._pin(capture, expected_capture_sha256, "study_capture")
        actual = utc(_now())
        contract = _study_receipt(capture["study"], actual)
        spec = contract["analysis_specification"]
        cutoff = utc(spec["analysis_at_utc"])
        if cutoff > actual:
            raise ValueError("analysis_cutoff_after_actual_time")
        caps = capture["cohorts"]
        if not isinstance(caps, list) or not caps:
            raise ValueError("complete_study_capture_required")
        protocol = caps[0]["protocol"]
        if any(cap["protocol"] != protocol for cap in caps):
            raise ValueError("contradictory_protocol_receipts")
        _inputs(
            contract, protocol, [cap["view"]["cohort"] for cap in caps], cutoff=cutoff
        )
        members, outcomes, unknown, missing = [], [], [], []
        selected: list[datetime] = []
        semantic_blockers = []
        opportunities = known_abstentions = 0
        per_cohort = []
        inference_contract = None
        for cap in caps:
            # Reuse complete chain/source validation and exact single-cohort
            # arithmetic construction, not its batch-specific support verdict.
            report = a.analyze_hold10_capture(
                cap,
                spec,
                expected_capture_sha256=sha(cap),
                expected_specification_sha256=sha(spec),
            )
            if report["status"] == "invalid_receipt_analysis":
                raise ValueError(
                    "invalid_underlying_cohort:" + ",".join(report["blockers"])
                )
            if "unverified_ai_threshold_policy_identity" in report["blockers"]:
                semantic_blockers.append("unverified_ai_threshold_policy_identity")
            numerical = report["inference_inputs"]
            if (
                inference_contract is not None
                and numerical["contract"] != inference_contract
            ):
                raise ValueError("contradictory_cohort_inference_contract")
            inference_contract = numerical["contract"]
            members.extend(numerical["members"])
            outcomes.extend(numerical["outcomes"])
            unknown.extend(report["unresolved_opportunity_ids"])
            missing.extend(report["selected_missing_member_ids"])
            opportunities += report["opportunities"]
            known_abstentions += report["known_abstentions"]
            selected.extend(
                utc(o["decision_at_utc"])
                for o in cap["view"]["cohort"]["payload"]["opportunities"]
                if o["eligible"]
                and o["opportunity_id"] not in report["unresolved_opportunity_ids"]
            )
            per_cohort.append(
                {
                    "cohort_id": cap["view"]["cohort"]["cohort_id"],
                    "capture_sha256": sha(cap),
                    "selected_opportunities": report["selected_opportunities"],
                    "selected_missing_member_ids": report[
                        "selected_missing_member_ids"
                    ],
                }
            )
        span = (
            (max(selected) - min(selected)).total_seconds() / 86400 if selected else 0.0
        )
        blockers = semantic_blockers
        if unknown:
            blockers.append("unresolved_population_membership")
        if not selected:
            blockers.append("no_selected_opportunities")
        if cutoff < utc("2026-10-05T00:00:00Z"):
            blockers.append("original_analysis_not_before_2026_10_05")
        if span < 28:
            blockers.append("original_28_day_span_not_met")
        if missing:
            blockers.append("incomplete_selected_cohort_no_complete_case_inference")
        assert inference_contract is not None
        pins = bundle_pins(inference_contract, members, outcomes)
        numerical = evaluate_frozen(
            inference_contract, members, outcomes, expected_pins=pins
        )
        if numerical["status"] != "draft_inference":
            blockers.extend(numerical.get("blockers", []))
        result.update(
            status="blocked_draft_study" if blockers else "draft_study_analysis",
            blockers=list(dict.fromkeys(blockers)),
            capture_sha256=expected_capture_sha256,
            study_receipt_sha256=capture["study"]["receipt_sha256"],
            cohort_count=len(caps),
            opportunities=opportunities,
            member_count=sum(cap["view"]["member_count"] for cap in caps),
            selected_opportunities=len(selected),
            selected_span_days=span,
            known_abstentions=known_abstentions,
            unresolved_opportunity_ids=unknown,
            selected_missing_member_ids=missing,
            per_cohort=per_cohort,
            dependence_support={
                k: numerical.get(k)
                for k in (
                    "n_components",
                    "components_per_arm",
                    "max_component_share_per_arm",
                    "component_members",
                )
            },
            inference_inputs={
                "contract": inference_contract,
                "members": members,
                "outcomes": outcomes,
                "pins": pins,
            },
            inference=numerical if not blockers else None,
        )
        return result
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        return {
            **result,
            "status": "invalid_study_analysis",
            "blockers": [str(exc)],
            "inference": None,
        }


class Hold10StudyReceipts:
    def __init__(self, connection, *, dry_run=False):
        self.c, self.dry_run = connection, dry_run is True

    def read_study(self, study_id):
        with _snapshot(self.c):
            _verify_schema(self.c)
            receipt = h._read(self.c, TABLES[0], "study_id", study_id)
            if receipt is not None:
                _study_receipt(receipt, utc(_now()))
            return receipt

    def register_study(self, contract, *, expected_study_sha256):
        if not self.dry_run:
            return {"status": "disabled", **FLAGS}
        contract = _freeze(contract)
        h._pin(contract, expected_study_sha256, "study_contract")
        spec = _contract(contract)
        with h._atomic(self.c):
            _verify_schema(self.c)
            old = self.read_study(contract["study_id"])
            if old is not None:
                if old["payload"] != contract:
                    raise ValueError("immutable_study_identity_conflict")
                return old
            now = _now()
            if utc(now) > utc(spec["analysis_at_utc"]):
                raise ValueError("study_registration_after_fixed_analysis_cutoff")
            protocol = h._read(
                self.c, h.TABLES[0], "protocol_id", contract["protocol_id"]
            )
            cohorts = [
                h._read(self.c, h.TABLES[1], "cohort_id", p["cohort_id"])
                for p in contract["cohorts"]
            ]
            _inputs(contract, protocol, cohorts, cutoff=utc(now))
            return h._insert(
                self.c, TABLES[0], {"study_id": contract["study_id"]}, contract, now
            )

    def capture(self, study_id, *, expected_study_receipt_sha256):
        with _snapshot(self.c):
            _verify_schema(self.c)
            receipt = self.read_study(study_id)
            if (
                receipt is None
                or receipt["receipt_sha256"] != expected_study_receipt_sha256
            ):
                raise ValueError("study_receipt_pin_mismatch")
            caps = [
                a.capture_hold10_receipts(
                    self.c,
                    p["cohort_id"],
                    expected_manifest_sha256=p["manifest_sha256"],
                )
                for p in receipt["payload"]["cohorts"]
            ]
            now = utc(_now())
            for cap in caps:
                a._validate_capture(cap, now)
            _inputs(
                receipt["payload"],
                caps[0]["protocol"],
                [cap["view"]["cohort"] for cap in caps],
                cutoff=now,
            )
            if any(cap["protocol"] != caps[0]["protocol"] for cap in caps):
                raise ValueError("contradictory_protocol_receipts")
            return {"study": receipt, "cohorts": caps}

    def record_analysis(self, capture, *, expected_capture_sha256):
        if not self.dry_run:
            return {"status": "disabled", **FLAGS}
        capture = _freeze(capture)
        result = analyze_study_capture(
            capture, expected_capture_sha256=expected_capture_sha256
        )
        if result["status"] == "invalid_study_analysis":
            return result
        study = capture["study"]
        spec = study["payload"]["analysis_specification"]
        payload = {"capture": capture, "result": result, **FLAGS}
        with h._atomic(self.c):
            _verify_schema(self.c)
            old = self.read_analysis(spec["analysis_id"])
            if old is not None:
                if old["payload"] != payload:
                    raise ValueError("immutable_study_analysis_conflict")
                return old
            current = self.capture(
                study["study_id"], expected_study_receipt_sha256=study["receipt_sha256"]
            )
            if sha(current) != expected_capture_sha256:
                raise ValueError("source_receipt_revision_conflict")
            now = _now()
            if utc(now) < utc(spec["analysis_at_utc"]) or utc(now) < utc(
                study["recorded_at_utc"]
            ):
                raise ValueError("recording_clock_moved_backwards")
            return h._insert(
                self.c,
                TABLES[1],
                {
                    "analysis_id": spec["analysis_id"],
                    "study_id": study["study_id"],
                    "capture_sha256": expected_capture_sha256,
                },
                payload,
                now,
            )

    def read_analysis(self, analysis_id):
        with _snapshot(self.c):
            _verify_schema(self.c)
            receipt = h._read(self.c, TABLES[1], "analysis_id", analysis_id)
            if receipt is None:
                return None
            a._receipt(receipt, "analysis", utc(_now()))
            payload = receipt["payload"]
            cap = payload["capture"]
            result = analyze_study_capture(
                cap, expected_capture_sha256=receipt["capture_sha256"]
            )
            study = cap["study"]
            spec = study["payload"]["analysis_specification"]
            if (
                result["status"] == "invalid_study_analysis"
                or result != payload["result"]
                or self.read_study(study["study_id"]) != study
                or receipt["study_id"] != study["study_id"]
                or receipt["analysis_id"] != spec["analysis_id"]
                or utc(receipt["recorded_at_utc"]) < utc(spec["analysis_at_utc"])
                or any(payload.get(k) is not False for k in FLAGS)
            ):
                raise ValueError("saved_study_analysis_disagrees_with_pinned_inputs")
            current = self.capture(
                study["study_id"], expected_study_receipt_sha256=study["receipt_sha256"]
            )
            for old, fresh in zip(cap["cohorts"], current["cohorts"]):
                for mid, history in old["measurement_histories"].items():
                    if fresh["measurement_histories"][mid][: len(history)] != history:
                        raise ValueError(
                            "saved_measurement_chain_no_longer_backed_by_store"
                        )
            return receipt
