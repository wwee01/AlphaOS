"""Disabled-by-default, append-only HOLD10 receipt dry runs.

No runtime hook, activation command, provider, broker or formal evaluator.
The caller supplies existing inputs. Recording time comes from this process,
never a supplied historical timestamp. All receipts remain DRY RUN even when
their timing would otherwise be eligible for future prospective collection.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, time, timezone
import json
import math
import sqlite3
from zoneinfo import ZoneInfo

from alphaos.research.successor_measurement import finite, reconstruct_bracket, utc
from alphaos.stats.frozen_policy_inference import canonical_sha256
from alphaos.util.market_calendar import nth_trading_day_after

VERSION = "hold10_receipt_dry_run_v1"
PREDECESSOR = "prereg_35bbce762e7f"
ARMS = {"tested", "reference"}
TABLES = (
    "research_hold10_protocols",
    "research_hold10_cohorts",
    "research_hold10_members",
    "research_hold10_measurements",
)
_COMMON = "payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, receipt_sha256 TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode='dry_run')"
DDL = (
    f"CREATE TABLE IF NOT EXISTS {TABLES[0]} (protocol_id TEXT PRIMARY KEY, {_COMMON})",
    f"CREATE TABLE IF NOT EXISTS {TABLES[1]} (cohort_id TEXT PRIMARY KEY, protocol_id TEXT NOT NULL REFERENCES {TABLES[0]}(protocol_id), {_COMMON})",
    f"CREATE TABLE IF NOT EXISTS {TABLES[2]} (member_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL REFERENCES {TABLES[1]}(cohort_id), opportunity_id TEXT NOT NULL, arm TEXT NOT NULL CHECK(arm IN ('tested','reference')), {_COMMON}, UNIQUE(cohort_id,opportunity_id,arm))",
    f"CREATE TABLE IF NOT EXISTS {TABLES[3]} (measurement_id TEXT PRIMARY KEY, member_id TEXT NOT NULL REFERENCES {TABLES[2]}(member_id), revision INTEGER NOT NULL CHECK(revision>0), previous_receipt_sha256 TEXT, {_COMMON}, UNIQUE(member_id,revision), UNIQUE(member_id,payload_sha256))",
) + tuple(
    f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable research receipt'); END"
    for table in TABLES
    for action in ("UPDATE", "DELETE")
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    # Check first to refuse noncanonical NaN, Infinity and unsupported objects.
    canonical_sha256(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing_{name}")
    return value


def _sha(value: object, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"invalid_{name}")
    return value


def _pin(payload: object, expected: str, name: str) -> None:
    if canonical_sha256(payload) != _sha(expected, name):
        raise ValueError(f"{name}_mismatch")


@contextmanager
def _atomic(c: sqlite3.Connection):
    if c.in_transaction:
        raise ValueError("receipt_api_requires_own_short_transaction")
    c.execute("BEGIN IMMEDIATE")
    try:
        yield
        c.commit()
    except BaseException:
        c.rollback()
        raise


def initialize_dry_run_receipts(c: sqlite3.Connection, *, purpose: str) -> None:
    """Explicit scratch/manual initializer. Importing/constructing never writes.

    Root owns eventual schema integration. This initializer changes only these
    new tables/triggers and refuses an unrelated enclosing caller transaction.
    """
    if purpose != "scratch_or_manual_dry_run":
        raise ValueError("explicit_dry_run_initialization_required")
    with _atomic(c):
        for sql in DDL:
            c.execute(sql)


def _read(c: sqlite3.Connection, table: str, key: str, value: str) -> dict | None:
    cursor = c.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,))
    raw = cursor.fetchone()
    if raw is None:
        return None
    row = dict(zip([d[0] for d in cursor.description], raw))
    payload = json.loads(row.pop("payload_json"))
    _pin(payload, row["payload_sha256"], "saved_payload_identity")
    receipt = row.pop("receipt_sha256")
    _pin(row, receipt, "saved_receipt_identity")
    if row["mode"] != "dry_run":
        raise ValueError("unsupported_receipt_mode")
    utc(row["recorded_at_utc"])
    return {**row, "receipt_sha256": receipt, "payload": payload}


def _insert(
    c: sqlite3.Connection, table: str, keys: dict, payload: dict, now: str
) -> dict:
    row = {
        **keys,
        "payload_sha256": canonical_sha256(payload),
        "recorded_at_utc": now,
        "mode": "dry_run",
    }
    receipt = canonical_sha256(row)
    columns = {**row, "receipt_sha256": receipt, "payload_json": _json(payload)}
    c.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        tuple(columns.values()),
    )
    return {**row, "receipt_sha256": receipt, "payload": payload}


def _existing(
    c: sqlite3.Connection, table: str, key: str, value: str, payload: dict
) -> dict | None:
    previous = _read(c, table, key, value)
    if previous and previous["payload_sha256"] != canonical_sha256(payload):
        raise ValueError("immutable_identity_already_has_different_content")
    return previous


def _protocol_contract(p: dict) -> None:
    if not isinstance(p, dict):
        raise ValueError("protocol_object_required")
    if (
        p.get("version") != VERSION
        or p.get("status") != "draft"
        or p.get("predecessor_id") != PREDECESSOR
        or p.get("horizon_sessions") != 10
        or p.get("direction") != "greater"
        or p.get("effect_target") != 0.05
        or p.get("statistic") != "paired_mean_candidate_R"
    ):
        raise ValueError("only_unactivated_original_hold10_draft_supported")
    if not _text(p.get("protocol_id"), "protocol_id").startswith("hold10-draft:"):
        raise ValueError("new_protocol_namespace_required_no_original_registration_ids")
    if "recorded_at_utc" in p or p.get("activated") or p.get("formal_allowed"):
        raise ValueError("caller_cannot_supply_receipt_time_or_activation")
    if (
        not isinstance(p.get("policies"), dict)
        or set(p["policies"]) != ARMS
        or any(not isinstance(v, dict) or not v for v in p["policies"].values())
    ):
        raise ValueError("both_saved_policy_preimages_required")
    if not isinstance(p.get("risk_contract"), dict):
        raise ValueError("saved_common_risk_contract_required")
    risk = p["risk_contract"]
    if risk.get("R_basis") != "each_arm_original_stop_distance":
        raise ValueError("original_arm_risk_denominators_required")
    if not finite(risk.get("risk_budget")) or risk["risk_budget"] <= 0:
        raise ValueError("positive_common_risk_budget_required")
    _text(risk.get("policy_version"), "risk_policy_version")
    _text(risk.get("currency"), "risk_currency")
    _text(p.get("source_revision"), "source_revision")
    if p.get("measurement_method") != "saved_daily_complete_horizon_bracket_v1":
        raise ValueError("explicit_supported_measurement_method_required")
    if p.get("population") not in {"primary_proposed", "final_proposed"}:
        raise ValueError("explicit_proposed_population_required")


class Hold10Receipts:
    """No method writes unless caller explicitly opts into DRY RUN persistence."""

    def __init__(self, connection: sqlite3.Connection, *, dry_run: bool = False):
        self.c = connection
        self.dry_run = dry_run is True

    def register_protocol(self, protocol: dict, *, expected_sha256: str) -> dict:
        if not self.dry_run:
            return {"status": "disabled", "formal_allowed": False}
        protocol = json.loads(_json(protocol))
        _pin(protocol, expected_sha256, "protocol_pin")
        _protocol_contract(protocol)
        with _atomic(self.c):
            old = _existing(
                self.c, TABLES[0], "protocol_id", protocol["protocol_id"], protocol
            )
            return old or _insert(
                self.c,
                TABLES[0],
                {"protocol_id": protocol["protocol_id"]},
                protocol,
                _now(),
            )

    def enroll_cohort(
        self,
        protocol_id: str,
        manifest: dict,
        *,
        expected_protocol_sha256: str,
        expected_manifest_sha256: str,
    ) -> dict:
        if not self.dry_run:
            return {"status": "disabled", "formal_allowed": False}
        manifest = json.loads(_json(manifest))
        _pin(manifest, expected_manifest_sha256, "manifest_pin")
        if not isinstance(manifest, dict):
            raise ValueError("manifest_object_required")
        if manifest.get("protocol_sha256") != expected_protocol_sha256:
            raise ValueError("manifest_protocol_identity_mismatch")
        if (
            "recorded_at_utc" in manifest
            or manifest.get("prospective")
            or manifest.get("activated")
        ):
            raise ValueError("caller_cannot_backdate_or_activate_cohort")
        cohort_id = _text(manifest.get("cohort_id"), "cohort_id")
        source = manifest.get("source")
        if not isinstance(source, dict) or source.get("kind") not in {
            "archived_copy",
            "existing_current_inputs",
            "synthetic",
        }:
            raise ValueError("explicit_source_provenance_required")
        _sha(source.get("sha256"), "source_identity")
        _text(source.get("scope"), "source_scope")
        opportunities = manifest.get("opportunities")
        declared = manifest.get("all_opportunity_ids")
        if not isinstance(opportunities, list) or not isinstance(declared, list):
            raise ValueError("full_input_opportunity_manifest_required")
        if any(not isinstance(o, dict) for o in opportunities):
            raise ValueError("opportunity_object_required")
        ids = [_text(o.get("opportunity_id"), "opportunity_id") for o in opportunities]
        declared = [_text(oid, "declared_opportunity_id") for oid in declared]
        if (
            len(set(ids)) != len(ids)
            or len(set(declared)) != len(declared)
            or set(ids) != set(declared)
        ):
            raise ValueError("manifest_missing_extra_or_duplicate_opportunities")
        now = _now()
        protocol = _read(self.c, TABLES[0], "protocol_id", protocol_id)
        if not protocol:
            raise ValueError("unknown_protocol")
        _pin(protocol["payload"], expected_protocol_sha256, "protocol_pin")
        prepared = self._prepare_members(protocol, manifest, now)
        with _atomic(self.c):
            # Short transaction: verify the same protocol immediately before
            # insertion; no computation or provider calls inside the transaction.
            current = _read(self.c, TABLES[0], "protocol_id", protocol_id)
            if current != protocol:
                raise ValueError("protocol_changed_during_enrollment")
            old = _existing(self.c, TABLES[1], "cohort_id", cohort_id, manifest)
            if old:
                return self.cohort_view(
                    cohort_id, expected_manifest_sha256=expected_manifest_sha256
                )
            recorded = _now()
            if utc(recorded) < utc(now):
                raise ValueError("recording_clock_moved_backwards")
            _insert(
                self.c,
                TABLES[1],
                {"cohort_id": cohort_id, "protocol_id": protocol_id},
                manifest,
                recorded,
            )
            for member in prepared:
                timely = manifest["source"][
                    "kind"
                ] == "existing_current_inputs" and utc(
                    protocol["recorded_at_utc"]
                ) <= utc(member["decision_at_utc"]) <= utc(recorded) <= utc(
                    member["measurement_start_utc"]
                )
                member["timing_classification"] = (
                    "timing_eligible_dry_run_only"
                    if timely
                    else "retrospective_or_synthetic_dry_run"
                )
                keys = {
                    k: member.pop(k) for k in ("member_id", "opportunity_id", "arm")
                }
                _insert(
                    self.c,
                    TABLES[2],
                    {**keys, "cohort_id": cohort_id},
                    member,
                    recorded,
                )
        return self.cohort_view(
            cohort_id, expected_manifest_sha256=expected_manifest_sha256
        )

    def _prepare_members(self, protocol: dict, manifest: dict, now: str) -> list[dict]:
        p = protocol["payload"]
        risk_sha = canonical_sha256(p["risk_contract"])
        result = []
        for opportunity in manifest["opportunities"]:
            oid = opportunity["opportunity_id"]
            inputs = opportunity.get("input")
            if not isinstance(inputs, dict) or not inputs:
                raise ValueError("saved_original_opportunity_input_required")
            _text(inputs.get("symbol"), "input_symbol")
            _pin(inputs, opportunity.get("input_sha256"), "original_input_pin")
            decision = utc(opportunity.get("decision_at_utc"))
            available = utc(opportunity.get("input_available_at_utc"))
            if not available <= decision <= utc(now):
                raise ValueError("future_decision_or_lookahead_input")
            first = nth_trading_day_after(
                decision.astimezone(ZoneInfo("America/New_York")).date(), 1
            )
            start = datetime.combine(
                first, time(9, 30), ZoneInfo("America/New_York")
            ).astimezone(timezone.utc)
            timely = (
                manifest["source"]["kind"] == "existing_current_inputs"
                and utc(protocol["recorded_at_utc"]) <= decision <= utc(now) <= start
            )
            eligible = opportunity.get("eligible")
            if type(eligible) is not bool:
                raise ValueError("explicit_input_eligibility_required")
            _text(opportunity.get("eligibility_reason"), "eligibility_reason")
            arms = opportunity.get("arms", {})
            if not isinstance(arms, dict) or not set(arms) <= ARMS:
                raise ValueError("unknown_arm")
            for arm in sorted(ARMS):
                plan = arms.get(arm)
                denominator = None
                if plan is not None:
                    if not isinstance(plan, dict):
                        raise ValueError("invalid_arm_plan")
                    if (
                        plan.get("input_sha256") != opportunity["input_sha256"]
                        or plan.get("horizon_sessions") != 10
                        or plan.get("policy_sha256")
                        != canonical_sha256(p["policies"][arm])
                        or plan.get("risk_contract_sha256") != risk_sha
                    ):
                        raise ValueError(
                            "arm_input_horizon_policy_or_common_risk_mismatch"
                        )
                    if plan.get("decision") not in {
                        "propose",
                        "no_action",
                        "unavailable",
                    }:
                        raise ValueError("unknown_plan_action_not_cash")
                    if plan["decision"] == "propose":
                        entry, stop, target = (
                            plan.get(k) for k in ("entry", "stop", "target")
                        )
                        if not (
                            finite(entry)
                            and finite(stop)
                            and finite(target)
                            and min(entry, stop, target) > 0
                        ):
                            raise ValueError("invalid_original_levels")
                        if not (
                            (plan.get("direction") == "long" and stop < entry < target)
                            or (
                                plan.get("direction") == "short"
                                and target < entry < stop
                            )
                        ):
                            raise ValueError("invalid_original_bracket_direction")
                        denominator = abs(entry - stop)
                        if not finite(
                            plan.get("original_risk_per_share")
                        ) or not math.isclose(
                            plan["original_risk_per_share"],
                            denominator,
                            rel_tol=1e-12,
                            abs_tol=1e-12,
                        ):
                            raise ValueError("original_arm_risk_denominator_mismatch")
                    if plan.get("symbol") != inputs.get("symbol"):
                        raise ValueError("arm_symbol_mismatch")
                member_id = canonical_sha256([manifest["cohort_id"], oid, arm])
                result.append(
                    {
                        "member_id": member_id,
                        "opportunity_id": oid,
                        "arm": arm,
                        "protocol_receipt_sha256": protocol["receipt_sha256"],
                        "input": inputs,
                        "input_sha256": opportunity["input_sha256"],
                        "plan": plan,
                        "plan_sha256": canonical_sha256(plan),
                        "common_risk_contract": p["risk_contract"],
                        "common_risk_sha256": risk_sha,
                        "original_arm_risk_per_share": denominator,
                        "horizon_sessions": 10,
                        "decision_at_utc": decision.isoformat(),
                        "measurement_start_utc": start.isoformat(),
                        "eligible": eligible,
                        "eligibility_reason": opportunity["eligibility_reason"],
                        "plan_status": "missing" if plan is None else plan["decision"],
                        "timing_classification": "timing_eligible_dry_run_only"
                        if timely
                        else "retrospective_or_synthetic_dry_run",
                        "prospective_enrollment": False,
                        "formal_allowed": False,
                    }
                )
        return result

    def record_measurement(
        self,
        member_id: str,
        bars: list[dict],
        *,
        expected_member_receipt_sha256: str,
        expected_bars_sha256: str,
        expected_previous_receipt_sha256: str | None = None,
    ) -> dict:
        if not self.dry_run:
            return {"status": "disabled", "formal_allowed": False}
        bars = json.loads(_json(bars))
        _pin(bars, expected_bars_sha256, "bars_pin")
        if not isinstance(bars, list):
            raise ValueError("source_bars_list_required")
        member = _read(self.c, TABLES[2], "member_id", member_id)
        if not member or member["receipt_sha256"] != expected_member_receipt_sha256:
            raise ValueError("member_receipt_pin_mismatch")
        now, payload = _now(), member["payload"]
        if utc(now) < utc(member["recorded_at_utc"]):
            raise ValueError("recording_clock_moved_backwards")
        if any(
            not isinstance(b, dict) or b.get("symbol") != payload["input"].get("symbol")
            for b in bars
        ):
            raise ValueError("foreign_or_malformed_source_bars")
        if (
            not payload["eligible"]
            or payload["plan"] is None
            or payload["plan_status"] == "unavailable"
        ):
            result = {
                "status": "blocked",
                "value": None,
                "reason": "ineligible_or_missing_arm_plan",
            }
        else:
            plan = {**payload["plan"], "decision_at_utc": payload["decision_at_utc"]}
            result = reconstruct_bracket(plan, bars, 10, now)
        data = {
            "member_receipt_sha256": member["receipt_sha256"],
            "source_bars": bars,
            "source_bars_sha256": expected_bars_sha256,
            "measurement": result,
            "original_arm_risk_per_share": payload["original_arm_risk_per_share"],
            "prospective_enrollment": False,
            "formal_allowed": False,
        }
        content_sha = canonical_sha256(data)
        measurement_id = canonical_sha256([member_id, content_sha])
        with _atomic(self.c):
            if _read(self.c, TABLES[2], "member_id", member_id) != member:
                raise ValueError("member_changed_during_measurement")
            history = self._measurement_history(member)
            old = next(
                (r for r in history if r["measurement_id"] == measurement_id), None
            )
            if old:
                return old
            last = history[-1] if history else None
            if (
                last["receipt_sha256"] if last else None
            ) != expected_previous_receipt_sha256:
                raise ValueError("measurement_revision_conflict")
            if last and last["payload"]["measurement"]["status"] == "complete":
                raise ValueError(
                    "completed_measurement_is_immutable_new_correction_protocol_required"
                )
            recorded = _now()
            if utc(recorded) < utc(now):
                raise ValueError("recording_clock_moved_backwards")
            return _insert(
                self.c,
                TABLES[3],
                {
                    "measurement_id": measurement_id,
                    "member_id": member_id,
                    "revision": last["revision"] + 1 if last else 1,
                    "previous_receipt_sha256": last["receipt_sha256"] if last else None,
                },
                data,
                recorded,
            )

    def cohort_view(self, cohort_id: str, *, expected_manifest_sha256: str) -> dict:
        # An explicit read transaction gives all members/arms one SQLite
        # snapshot even when another connection appends a measurement. The
        # enrollment idempotency path already owns a short write transaction.
        owns_transaction = not self.c.in_transaction
        if owns_transaction:
            self.c.execute("BEGIN")
        try:
            return self._cohort_view(
                cohort_id, expected_manifest_sha256=expected_manifest_sha256
            )
        finally:
            if owns_transaction:
                self.c.rollback()

    def _measurement_history(self, member: dict) -> list[dict]:
        ids = self.c.execute(
            f"SELECT measurement_id FROM {TABLES[3]} WHERE member_id=? ORDER BY revision",
            (member["member_id"],),
        ).fetchall()
        history: list[dict] = []
        for index, (mid,) in enumerate(ids, 1):
            measurement = _read(self.c, TABLES[3], "measurement_id", mid)
            if measurement is None:
                raise ValueError("missing_measurement_receipt")
            prior = history[-1] if history else None
            if (
                measurement["revision"] != index
                or measurement["previous_receipt_sha256"]
                != (prior["receipt_sha256"] if prior else None)
                or measurement["payload"]["member_receipt_sha256"]
                != member["receipt_sha256"]
                or mid
                != canonical_sha256(
                    [member["member_id"], measurement["payload_sha256"]]
                )
                or utc(measurement["recorded_at_utc"])
                < utc((prior or member)["recorded_at_utc"])
            ):
                raise ValueError("measurement_history_identity_or_chronology_mismatch")
            _pin(
                measurement["payload"]["source_bars"],
                measurement["payload"]["source_bars_sha256"],
                "saved_bars_pin",
            )
            if prior and prior["payload"]["measurement"]["status"] == "complete":
                raise ValueError("measurement_revision_after_frozen_completion")
            history.append(measurement)
        return history

    def _cohort_view(self, cohort_id: str, *, expected_manifest_sha256: str) -> dict:
        cohort = _read(self.c, TABLES[1], "cohort_id", cohort_id)
        if not cohort:
            raise ValueError("unknown_cohort")
        _pin(cohort["payload"], expected_manifest_sha256, "manifest_pin")
        protocol = _read(self.c, TABLES[0], "protocol_id", cohort["protocol_id"])
        if protocol is None:
            raise ValueError("missing_protocol_receipt")
        _pin(
            protocol["payload"],
            cohort["payload"].get("protocol_sha256"),
            "cohort_protocol_pin",
        )
        expected = self._prepare_members(
            protocol, cohort["payload"], cohort["recorded_at_utc"]
        )
        expected_by_id = {m["member_id"]: m for m in expected}
        ids = self.c.execute(
            f"SELECT member_id FROM {TABLES[2]} WHERE cohort_id=? ORDER BY opportunity_id,arm",
            (cohort_id,),
        ).fetchall()
        members = [_read(self.c, TABLES[2], "member_id", row[0]) for row in ids]
        if len(members) != 2 * len(cohort["payload"]["opportunities"]):
            raise ValueError("missing_member_receipts")
        observations = []
        for member in members:
            assert member is not None
            intended = expected_by_id.get(member["member_id"])
            if intended is None:
                raise ValueError("unexpected_member_receipt")
            metadata = {
                k: intended.pop(k) for k in ("member_id", "opportunity_id", "arm")
            }
            if (
                any(member[k] != v for k, v in metadata.items())
                or member["payload"] != intended
            ):
                raise ValueError("member_disagrees_with_frozen_manifest")
            if member["recorded_at_utc"] != cohort["recorded_at_utc"]:
                raise ValueError("member_receipt_not_atomic_with_cohort")
            history = self._measurement_history(member)
            measurement = history[-1] if history else None
            observations.append(measurement)
        return {
            "cohort": cohort,
            "members": members,
            "latest_measurements": observations,
            "opportunities": len(cohort["payload"]["opportunities"]),
            "member_count": len(members),
            "missing_measurements": sum(
                o is None or o["payload"]["measurement"]["status"] != "complete"
                for o in observations
            ),
            "prospective_enrollment": False,
            "formal_allowed": False,
            "promotion_allowed": False,
        }
