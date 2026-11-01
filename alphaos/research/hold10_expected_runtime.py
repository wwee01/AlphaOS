"""Opt-in, inactive expected scoped runtime agreement; never execution attestation.

Hashes remain exact marshal-code/path/interpreter identities. A separately supplied
review-held profile pin is required: this API does not decide what policy to trust.
"""

from __future__ import annotations

import marshal
import platform
import sys

from alphaos.research import hold10_collection_audit as a
from alphaos.research import hold10_designated_registry as d
from alphaos.research import hold10_policy_binding as policy
from alphaos.research import hold10_prospective as v1
from alphaos.research import runtime_identity as runtime

VERSION = "hold10_expected_scoped_runtime_v1"
SCOPE = "selected_settings_clients_callable_marshal_path_interpreter_exact_v1"
LIMITS = [
    "Local review-held pins are not authenticated human approval or execution proof.",
    "Marshal hashes include source path and line metadata; cross-path/interpreter portability is unsupported.",
    "Selected settings, clients and Python callables only; transitive dependencies, closures/default values and actual model execution remain unverified.",
    "The wrapper records its own environment; it does not authenticate the interpreter or resist a malicious writer.",
    "Pre/post agreement cannot detect changes restored within a decision.",
]


def interpreter_identity():
    """Typed, non-secret compatibility scope; deliberately no environment dump."""
    return {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
        "cache_tag": sys.implementation.cache_tag,
        "marshal_version": marshal.version,
        "platform": sys.platform,
        "machine": platform.machine(),
        "byteorder": sys.byteorder,
    }


def _environment(value):
    expected = interpreter_identity()
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError("exact_interpreter_scope_required")
    for name, example in expected.items():
        if type(value[name]) is not type(example):
            raise ValueError("typed_interpreter_scope_required")
        if isinstance(value[name], str) and not 0 < len(value[name]) <= 128:
            raise ValueError("bounded_interpreter_scope_required")
    return v1._copy(value)


def runtime_profile(observation):
    """Project an explicitly reviewed setup, never infer approval from a candidate.

    Absolute process instance IDs are incomparable across restarts. Preserve their
    alias partition so distinct instances cannot silently become shared instances.
    """
    value = v1._copy(observation)
    if runtime._validate_runtime(value):
        raise ValueError("complete_selected_runtime_scope_required")
    groups: dict[str, list[str]] = {}
    for name, fn in sorted(value["callables"].items()):
        token = fn.pop("bound_instance_token")
        if (token is None) != (fn["bound_class"] is None):
            raise ValueError("bound_instance_scope_required")
        if token is not None:
            groups.setdefault(token, []).append(name)
    return {
        "settings": value["settings"],
        "effective_clients": value["effective_clients"],
        "callables": value["callables"],
        "bound_instance_groups": sorted(groups.values()),
    }


def _profile(value):
    # Reconstruct synthetic tokens solely to reuse the strict typed validator.
    v1._exact(
        value,
        {"settings", "effective_clients", "callables", "bound_instance_groups"},
        "exact_runtime_profile_required",
    )
    copied = v1._copy(value)
    names = set(copied["callables"])
    groups = copied["bound_instance_groups"]
    if not isinstance(groups, list) or groups != sorted(groups):
        raise ValueError("canonical_instance_groups_required")
    seen = set()
    tokens = {}
    for index, group in enumerate(groups):
        if not isinstance(group, list) or not group or group != sorted(group):
            raise ValueError("canonical_instance_groups_required")
        for name in group:
            if name not in names or name in seen:
                raise ValueError("unique_instance_groups_required")
            seen.add(name)
            tokens[name] = str(index)
    for name, fn in copied["callables"].items():
        fn["bound_instance_token"] = tokens.get(name)
    reconstructed = {
        "settings": copied["settings"],
        "effective_clients": copied["effective_clients"],
        "callables": copied["callables"],
        "limitations": runtime.LIMITS,
        "full_runtime_attested": False,
    }
    if runtime_profile(reconstructed) != value:
        raise ValueError("canonical_runtime_profile_required")
    return v1._copy(value)


def create_manifest(
    contract,
    binding,
    *,
    reviewed_profile,
    expected_profile_sha256,
    reviewed_environment,
    expected_environment_sha256,
    expected_contract_sha256,
    expected_binding_sha256,
):
    q = v1._contract(contract)
    v1.h._pin(q, expected_contract_sha256, "contract")
    b = policy.validate_binding(
        binding, q, expected_binding_sha256=expected_binding_sha256
    )
    profile = _profile(reviewed_profile)
    environment = _environment(reviewed_environment)
    v1.h._pin(profile, expected_profile_sha256, "reviewed_runtime_profile")
    v1.h._pin(environment, expected_environment_sha256, "reviewed_environment")
    now = v1._clock()
    if now >= v1.c._dt(q["start_at_utc"]):
        raise ValueError("future_runtime_manifest_required")
    return _manifest_value(q, b, profile, environment, now.isoformat())


def _manifest_value(q, b, profile, environment, created_at):
    configured = profile["settings"]["orchestrator"]["values"]
    if any(
        configured[key] != q["combined_policy"]["configuration"][key]
        for key in policy.CONFIG_FIELDS
    ):
        raise ValueError("runtime_profile_policy_configuration_mismatch")
    clients = profile["effective_clients"]
    if (
        clients["evaluator"]["values"]["model"]
        != q["protocol"]["policies"]["tested"]["identity"]["model"]
        or clients["classifier"]["values"]["model"]
        != q["combined_policy"]["classifier_identity"]["model"]
        or any(
            clients[group]["values"]["use_mock"]
            for group in ("evaluator", "classifier")
        )
    ):
        raise ValueError("runtime_profile_policy_client_mismatch")
    return {
        "version": VERSION,
        "scope": SCOPE,
        "contract_sha256": v1.sha(q),
        "binding_sha256": v1.sha(b),
        "selected_source_revision": b["selected_source_revision"],
        "selected_source_files": b["selected_source_files"],
        "profile": profile,
        "profile_sha256": v1.sha(profile),
        "environment": environment,
        "environment_sha256": v1.sha(environment),
        "created_at_utc": created_at,
        "limitations": LIMITS,
        "full_runtime_attested": False,
        **v1.FLAGS,
    }


def _validate_manifest(value, intent, designation, expected_sha256):
    v1.h._pin(value, expected_sha256, "expected_runtime_manifest")
    rebuilt = _manifest_value(
        intent["contract"],
        intent["binding"],
        _profile(value["profile"]),
        _environment(value["environment"]),
        value["created_at_utc"],
    )
    if rebuilt != value or not (
        v1.c._dt(intent["binding"]["recorded_at_utc"])
        <= v1.c._dt(value["created_at_utc"])
        <= v1.c._dt(intent["created_at_utc"])
    ):
        raise ValueError("runtime_manifest_binding_or_clock_invalid")


def freeze_manifest(
    api, journal, manifest, *, expected_manifest_sha256, expected_designation_sha256
):
    """Durably pin one manifest before start; no replacement or late enrollment."""
    if not api.enabled or not journal.enabled:
        return {"status": "disabled", **v1.FLAGS}
    root, folder = api._folder()
    with d._lock(root, folder), journal._locked() as (db, identity, events, size):
        designation = api._manifest(folder, expected_designation_sha256)
        intent = d._read(folder / "intent.json")
        if identity["designation_sha256"] != expected_designation_sha256:
            raise ValueError("audit_designation_mismatch")
        # Revalidation here uses the real durable event clock, not manifest text alone.
        _validate_manifest(manifest, intent, designation, expected_manifest_sha256)
        payload = {
            "manifest": v1._copy(manifest),
            "manifest_sha256": expected_manifest_sha256,
            "designation_sha256": expected_designation_sha256,
            "registration_sha256": designation["registration_sha256"],
        }
        if events:
            if (
                len(events) == 1
                and events[0]["kind"] == "expected_runtime_manifest"
                and events[0]["payload"] == payload
            ):
                return events[0].materialize()
            raise ValueError("empty_journal_required_for_manifest_freeze")
        if v1._clock() >= v1.c._dt(intent["contract"]["start_at_utc"]):
            raise ValueError("future_manifest_freeze_required")
        return journal._append(
            db, identity, events, size, "expected_runtime_manifest", payload
        )


def _frozen(events, identity, expected_receipt_sha256):
    if (
        not events
        or events[0]["kind"] != "expected_runtime_manifest"
        or sum(e["kind"] == "expected_runtime_manifest" for e in events) != 1
    ):
        raise ValueError("frozen_runtime_manifest_missing")
    event = events[0]
    if event["receipt_sha256"] != expected_receipt_sha256:
        raise ValueError("runtime_manifest_receipt_pin_mismatch")
    payload = event["payload"]
    if payload["designation_sha256"] != identity["designation_sha256"]:
        raise ValueError("runtime_manifest_journal_mismatch")
    v1.h._pin(
        payload["manifest"], payload["manifest_sha256"], "expected_runtime_manifest"
    )
    return event


class ExpectedRuntimeSink:
    """Explicit producer adapter; does not activate collectors or change decisions."""

    def __init__(self, journal, *, expected_manifest_receipt_sha256):
        self.journal = journal
        self.expected = expected_manifest_receipt_sha256

    def append_runtime_identity(self, payload):
        if not self.journal.enabled:
            return {"status": "disabled", **v1.FLAGS}
        with self.journal._locked() as (db, identity, events, size):
            frozen = _frozen(events, identity, self.expected)
            if v1.c._dt(payload["recorded_at_utc"]) <= v1.c._dt(
                frozen["recorded_at_utc"]
            ):
                raise ValueError("producer_must_follow_manifest_freeze")
            saved = v1._copy(payload)
            saved["expected_runtime_context"] = {
                "version": VERSION,
                "manifest_receipt_sha256": self.expected,
                "environment": interpreter_identity(),
            }
            if a._size(saved) > 1024 * 1024:
                raise ValueError("bounded_runtime_receipt_required")
            return self.journal._append(
                db, identity, events, size, "runtime_identity", saved
            )["receipt_sha256"]


def audit_expected(
    events, identity, designation, intent, runtime_audit, *, expected_receipt_sha256
):
    """Return every core candidate, including all missing/contradictory records."""
    global_gaps = []
    frozen = None
    try:
        frozen = _frozen(events, identity, expected_receipt_sha256)
        manifest = frozen["payload"]["manifest"]
        _validate_manifest(
            manifest, intent, designation, frozen["payload"]["manifest_sha256"]
        )
        # Validate retained chronology without moving the clock.
        if manifest["version"] != VERSION or manifest["scope"] != SCOPE:
            raise ValueError("manifest_scope_invalid")
        _profile(manifest["profile"])
        _environment(manifest["environment"])
        if (
            manifest["contract_sha256"] != designation["contract_sha256"]
            or manifest["binding_sha256"] != designation["binding_sha256"]
            or manifest["selected_source_revision"]
            != intent["binding"]["selected_source_revision"]
            or manifest["selected_source_files"]
            != intent["binding"]["selected_source_files"]
            or manifest["profile_sha256"] != v1.sha(manifest["profile"])
            or manifest["environment_sha256"] != v1.sha(manifest["environment"])
            or frozen["payload"]["registration_sha256"]
            != designation["registration_sha256"]
            or not v1.c._dt(manifest["created_at_utc"])
            <= v1.c._dt(intent["created_at_utc"])
            <= v1.c._dt(frozen["recorded_at_utc"])
            < v1.c._dt(intent["contract"]["start_at_utc"])
        ):
            raise ValueError("manifest_binding_or_chronology_invalid")
    except (ValueError, KeyError, TypeError):
        global_gaps.append("missing_or_invalid_frozen_manifest")
        frozen = None
    result = []
    for candidate in runtime_audit["candidates"]:
        cid = candidate["candidate_id"]
        gaps = list(global_gaps)
        if candidate["status"] != "consistent":
            gaps.append("runtime_receipts_not_complete_and_consistent")
        rows = [
            e
            for e in events
            if e["kind"] == "runtime_identity"
            and e["payload"].get("candidate_id") == cid
        ]
        if len(rows) != 2:
            gaps.append("exact_started_finished_pair_required")
        if frozen is not None:
            manifest = frozen["payload"]["manifest"]
            for row in rows:
                payload = row["payload"]
                if payload.get("expected_runtime_context") != {
                    "version": VERSION,
                    "manifest_receipt_sha256": expected_receipt_sha256,
                    "environment": manifest["environment"],
                }:
                    gaps.append("manifest_context_or_environment_mismatch")
                try:
                    if (
                        not v1.c._dt(frozen["recorded_at_utc"])
                        < v1.c._dt(payload["recorded_at_utc"])
                        <= v1.c._dt(row["recorded_at_utc"])
                    ):
                        gaps.append("manifest_producer_chronology_invalid")
                    for key in ("before", "after"):
                        if (
                            key in payload
                            and runtime_profile(payload[key]) != manifest["profile"]
                        ):
                            gaps.append("expected_runtime_profile_mismatch")
                except (ValueError, KeyError, TypeError):
                    gaps.append("runtime_profile_scope_invalid")
        result.append(
            {
                "candidate_id": cid,
                "status": "matched" if not gaps else "refused",
                "gaps": sorted(set(gaps)),
            }
        )
    return {
        "version": VERSION,
        "scope": SCOPE,
        "expected_manifest_receipt_sha256": expected_receipt_sha256,
        "eligible_count": len(result),
        "complete_count": sum(x["status"] == "matched" for x in result),
        "global_gaps": global_gaps,
        "candidates": result,
        "full_runtime_attested": False,
        "limitations": LIMITS,
    }


def collect_designated_expected_runtime(
    api,
    journal,
    snapshot,
    *,
    attempt_id,
    expected_designation_sha256,
    expected_previous_capture_sha256,
    expected_manifest_receipt_sha256,
):
    """No missing-runtime fallback; legacy collection is explicitly non-equivalent."""
    result = a._collect_guarded(
        api,
        journal,
        snapshot,
        attempt_id=attempt_id,
        expected_designation_sha256=expected_designation_sha256,
        expected_previous_capture_sha256=expected_previous_capture_sha256,
        runtime_requirement={
            "version": VERSION,
            "manifest_receipt_sha256": expected_manifest_receipt_sha256,
        },
    )

    terminal = result.get("audit", {}).get("payload", {}).get("status")
    return result | {
        "expected_runtime_scope": SCOPE,
        "expected_runtime_agreement": result["status"] == "captured"
        or (result["status"] == "already_recorded" and terminal == "captured"),
        "full_runtime_attested": False,
    }
