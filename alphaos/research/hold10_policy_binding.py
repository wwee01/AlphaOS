"""Selected-byte/config binding and recorded lineage audit, never attestation."""

from __future__ import annotations

from datetime import timezone
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess

from alphaos.lineage.hashing import stable_hash
from alphaos.research import hold10_prospective as v1
from alphaos.research import hold10_receipts as h

VERSION = "hold10_selected_policy_binding_v1"
CONFIG_FIELDS = (
    "labeller_decision_override_enabled",
    "label_min_confidence_to_propose",
    "min_reward_risk",
)
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PACKAGE_BYTES = 65536
LIMITS = [
    "Selected six source files are not the complete executed dependency closure.",
    "Observed file bytes and declared configuration slice do not prove loaded runtime bytes/settings.",
    "Full nonsecret-settings preimage is unverified; its declared fingerprint is not replaced by a slice hash.",
    "Recorded stage lineage and prompt identities are local evidence, not authenticated model execution.",
]


def _copy(value):
    raw = h._json(value)
    if len(raw.encode()) > MAX_PACKAGE_BYTES:
        raise ValueError("bounded_policy_package_required")
    return json.loads(raw)


def _git(root, args):
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, timeout=3, check=True
        )
        if len(result.stdout) > MAX_FILE_BYTES:
            raise ValueError()
        return result.stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError("selected_git_evidence_unavailable") from None


def selected_revision_files(code_root, revision):
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("exact_source_revision_required")
    root = Path(code_root).resolve(strict=True)
    result = {}
    for name in sorted(v1.POLICY_FILES):
        target = f"{revision}:{name}"
        size = _git(root, ["cat-file", "-s", target]).strip()
        if not size.isdigit() or int(size) > MAX_FILE_BYTES:
            raise ValueError("bounded_selected_git_blob_required")
        result[name] = hashlib.sha256(_git(root, ["show", target])).hexdigest()
    return result


def _working_files(root):
    result = {}
    for name in sorted(v1.POLICY_FILES):
        path = root / name
        if any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
            raise ValueError("regular_selected_source_required")
        before = path.stat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > MAX_FILE_BYTES
        ):
            raise ValueError("regular_bounded_selected_source_required")
        raw = path.read_bytes()
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("selected_source_changed_during_read")
        result[name] = hashlib.sha256(raw).hexdigest()
    return result


def create_policy_binding(
    contract, *, expected_contract_sha256, code_root, configuration
):
    """Verify actual selected bytes; read only the three explicit config fields.

    configuration may be an exact mapping or an object with those attributes.
    Never serialize Settings, inspect __dict__, read .env, or hash secrets.
    """
    q = v1._contract(contract)
    h._pin(q, expected_contract_sha256, "contract")
    if isinstance(configuration, dict):
        v1._exact(configuration, CONFIG_FIELDS, "exact_configuration_slice_required")
        config = _copy(configuration)
    else:
        config = _copy({key: getattr(configuration, key) for key in CONFIG_FIELDS})
    if v1.sha(config) != v1.sha(q["combined_policy"]["configuration"]):
        raise ValueError("configuration_slice_contract_mismatch")
    started = v1._clock()
    root = Path(code_root).resolve(strict=True)
    revision = q["combined_policy"]["source_revision"]
    if _git(root, ["rev-parse", "HEAD"]).decode().strip() != revision:
        raise ValueError("inspected_checkout_revision_mismatch")
    expected = q["combined_policy"]["files"]
    if (
        selected_revision_files(root, revision) != expected
        or _working_files(root) != expected
    ):
        raise ValueError("selected_source_bytes_contract_mismatch")
    if (
        _working_files(root) != expected
        or _git(root, ["rev-parse", "HEAD"]).decode().strip() != revision
    ):
        raise ValueError("selected_source_changed_during_binding")
    return _copy(
        {
            "version": VERSION,
            "contract_sha256": expected_contract_sha256,
            "selected_source_revision": revision,
            "selected_source_files": expected,
            "configuration_slice": config,
            "configuration_slice_sha256": v1.sha(config),
            "declared_full_settings_sha256": q["combined_policy"][
                "nonsecret_settings_sha256"
            ],
            "inspection_started_at_utc": started.isoformat(),
            "recorded_at_utc": v1._clock().isoformat(),
            "full_settings_preimage_verified": False,
            "runtime_execution_attested": False,
            "coverage": "selected_source_bytes_and_explicit_combination_settings_only",
            "limitations": LIMITS,
            **v1.FLAGS,
        }
    )


def validate_binding(binding, contract, *, expected_binding_sha256):
    b = _copy(binding)
    h._pin(b, expected_binding_sha256, "policy_binding")
    q = v1._contract(contract)
    v1._exact(
        b,
        {
            "version",
            "contract_sha256",
            "selected_source_revision",
            "selected_source_files",
            "configuration_slice",
            "configuration_slice_sha256",
            "declared_full_settings_sha256",
            "inspection_started_at_utc",
            "recorded_at_utc",
            "full_settings_preimage_verified",
            "runtime_execution_attested",
            "coverage",
            "limitations",
            *v1.FLAGS,
        },
        "exact_policy_binding_required",
    )
    if (
        b["version"] != VERSION
        or b["contract_sha256"] != v1.sha(q)
        or any(b[k] != value for k, value in v1.FLAGS.items())
    ):
        raise ValueError("policy_binding_contract_or_mode_mismatch")
    p = q["combined_policy"]
    if (
        b["selected_source_revision"] != p["source_revision"]
        or b["selected_source_files"] != p["files"]
        or v1.sha(b["configuration_slice"]) != v1.sha(p["configuration"])
        or b["configuration_slice_sha256"] != v1.sha(b["configuration_slice"])
        or b["declared_full_settings_sha256"] != p["nonsecret_settings_sha256"]
    ):
        raise ValueError("policy_binding_content_mismatch")
    if (
        b["full_settings_preimage_verified"] is not False
        or b["runtime_execution_attested"] is not False
        or b["limitations"] != LIMITS
        or b["coverage"]
        != "selected_source_bytes_and_explicit_combination_settings_only"
    ):
        raise ValueError("unattested_selected_scope_required")
    if (
        not v1.c._dt(b["inspection_started_at_utc"])
        <= v1.c._dt(b["recorded_at_utc"])
        <= v1._clock()
    ):
        raise ValueError("policy_binding_clock_invalid")
    return b


def _plain(db, table):
    row = db.execute(
        "SELECT type,sql FROM sqlite_master WHERE name=?", (table,)
    ).fetchone()
    if row is None or row[0] != "table" or "VIRTUAL" in row[1].upper():
        raise ValueError("plain_saved_provenance_table_required")


def _rows(db, table, key, value, extra=""):
    _plain(db, table)
    projections = {
        "candidates": v1.e.FIELDS["candidates"] + ",lineage_id",
        "openai_evaluations": v1.e.FIELDS["openai_evaluations"] + ",lineage_id",
        "decision_adjustments": v1.e.FIELDS["decision_adjustments"]
        + ",lineage_id,override_enabled,label_decision,ai_lineage_json",
        "shadow_baseline_decisions": v1.e.FIELDS["shadow_baseline_decisions"]
        + ",lineage_id",
        "candidate_labels": "label_id,candidate_id,packet_id,scan_batch_id,symbol,model,label_version,label_source,label_decision,validation_status,is_mock,created_at_utc",
        "config_versions": "id,config_hash,created_at_utc",
        "lineage_snapshots": "lineage_id,git_commit_sha,git_branch,git_dirty,app_version,schema_version,config_hash,scanner_config_hash,risk_config_hash,protection_config_hash,scheduler_config_hash,earnings_config_hash,proposal_ttl_config_hash,tqs_config_hash,attribution_config_hash,shadow_tier_config_hash,regime_config_hash,text_archive_config_hash,baseline_config_hash,canary_config_hash,scanner_version,scanner_rule_version,universe_version_hash,playbook_version,strategy_version,feature_engine_version,market_data_provider,created_at_utc,created_at_sgt",
    }
    names = projections[table].split(",")
    present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    projection = ",".join(
        name if name in present else f"NULL AS {name}" for name in names
    )
    cur = db.execute(
        f"SELECT {projection} FROM {table} WHERE {key}=? {extra} ORDER BY rowid LIMIT 1001",
        (value,),
    )
    rows = [dict(zip([x[0] for x in cur.description], row)) for row in cur]
    if len(rows) > 1000:
        raise ValueError("saved_identity_multiplicity_bound_exceeded")
    return rows


def audit_saved_policy(
    snapshot, contract, binding, *, expected_binding_sha256, code_root=None
):
    """Input-only retained census with exact stage IDs; missingness is reported.

    code_root optionally resolves historical selected bytes from exact git
    objects. No current bytes are substituted for a historical revision.
    """
    b = validate_binding(
        binding, contract, expected_binding_sha256=expected_binding_sha256
    )
    q = v1._contract(contract)
    if not isinstance(snapshot, v1.c.SavedLedger):
        raise ValueError("immutable_retained_copy_required")
    snapshot.verify()
    before = v1._clock()
    candidates = []
    historical = {}
    with v1.e._snapshot(snapshot.conn):
        observation = v1._observe(
            snapshot,
            {"payload": {"contract": q, "schedule_slots": v1.schedule_slots(q)}},
            before,
        )
        for candidate in (
            observation["inventory"]["linked_candidates"]
            + observation["inventory"]["orphan_candidates"]
        ):
            if candidate["shadow_tier"] not in (None, 0):
                continue
            cid, bid = candidate["candidate_id"], candidate["scan_batch_id"]
            issues, missing, stages = [], [], {}
            stage_specs = [
                ("candidate", "candidates", "candidate_id"),
                ("evaluation", "openai_evaluations", "candidate_id"),
                ("adjustment", "decision_adjustments", "candidate_id"),
                ("reference", "shadow_baseline_decisions", "candidate_id"),
            ]
            stage_rows = {}
            for name, table, key in stage_specs:
                rows = _rows(
                    snapshot.conn,
                    table,
                    key,
                    cid,
                    "AND rule_version='threshold_v1_hold10'"
                    if name == "reference"
                    else "",
                )
                if len(rows) != 1:
                    missing.append(name + "_missing_or_ambiguous")
                    continue
                row = rows[0]
                stage_rows[name] = row
                if row.get("symbol") != candidate["symbol"] or (
                    "scan_batch_id" in row and row["scan_batch_id"] != bid
                ):
                    issues.append(name + "_foreign_identity")
                lineage_id = row.get("lineage_id")
                stage = {
                    "lineage_id": lineage_id,
                    "recorded_at_utc": row.get("created_at_utc"),
                    "selected_input_row_sha256": v1.sha(row),
                }
                stages[name] = stage
                try:
                    at = v1.c._dt(row.get("created_at_utc"))
                    if not v1.c._dt(candidate["created_at_utc"]) <= at <= before:
                        issues.append(name + "_invalid_chronology")
                except (TypeError, ValueError):
                    issues.append(name + "_invalid_chronology")
                lineages = (
                    _rows(snapshot.conn, "lineage_snapshots", "lineage_id", lineage_id)
                    if lineage_id
                    else []
                )
                if len(lineages) != 1:
                    missing.append(name + "_lineage_unavailable")
                    continue
                lineage = lineages[0]
                for required_hash in (
                    "config_hash",
                    "scanner_config_hash",
                    "risk_config_hash",
                    "baseline_config_hash",
                    "scheduler_config_hash",
                ):
                    if not isinstance(
                        lineage.get(required_hash), str
                    ) or not re.fullmatch(r"[0-9a-f]{16}", lineage[required_hash]):
                        missing.append(name + "_" + required_hash + "_unavailable")
                fields = {
                    k: v
                    for k, v in lineage.items()
                    if k not in {"id", "lineage_id", "created_at_utc", "created_at_sgt"}
                }
                valid = stable_hash(fields) == lineage_id
                if not valid:
                    issues.append(name + "_lineage_hash_mismatch")
                revision = lineage.get("git_commit_sha")
                exact = revision == b["selected_source_revision"]
                stage.update(
                    {
                        "lineage_hash_valid": valid,
                        "recorded_revision": revision,
                        "recorded_revision_matches_contract": exact,
                        "recorded_git_dirty": lineage.get("git_dirty"),
                        "recorded_config_hash": lineage.get("config_hash"),
                        "lineage_created_at_utc": lineage.get("created_at_utc"),
                        "selected_recorded_revision_bytes_match": None,
                    }
                )
                try:
                    if v1.c._dt(lineage["created_at_utc"]) > v1.c._dt(
                        row["created_at_utc"]
                    ):
                        issues.append(name + "_lineage_after_stage")
                except (ValueError, TypeError):
                    issues.append(name + "_lineage_time_invalid")
                if not exact:
                    issues.append(name + "_exact_contract_revision_mismatch")
                if code_root is not None and revision:
                    if revision not in historical:
                        try:
                            historical[revision] = selected_revision_files(
                                code_root, revision
                            )
                        except ValueError:
                            historical[revision] = None
                    if historical[revision] is not None:
                        stage["selected_recorded_revision_bytes_match"] = (
                            historical[revision] == b["selected_source_files"]
                        )
                # config_versions uses a different selected preimage; even an
                # exact hash match must not certify the full lineage preimage.
                stage["exact_config_version_matches"] = len(
                    _rows(
                        snapshot.conn,
                        "config_versions",
                        "config_hash",
                        lineage.get("config_hash"),
                    )
                )
            adj = stage_rows.get("adjustment", {})
            ev = stage_rows.get("evaluation", {})
            try:
                evidence = v1.e._object_json(
                    adj.get("evidence_json"), "invalid_adjustment_evidence"
                )
                diagnostic = evidence["watch_advisory_diagnostic"]
                if diagnostic.get("is_mock") is not False:
                    issues.append("diagnostic_mock_identity_mismatch")
                for field in ("prompt_hash", "system_prompt_hash"):
                    if not isinstance(ev.get(field), str) or not re.fullmatch(
                        r"[0-9a-f]{16}", ev[field]
                    ):
                        missing.append("primary_" + field + "_unavailable")
                if ev.get("validation_status") != "passed" or ev.get("is_mock") != 0:
                    issues.append("primary_validation_or_mock_mismatch")
                if (
                    diagnostic.get("primary_decision") != ev.get("decision")
                    or adj.get("eval_decision") != ev.get("decision")
                    or diagnostic.get("current_decision") != adj.get("final_decision")
                ):
                    issues.append("primary_or_final_decision_link_mismatch")
                if diagnostic.get("eval_id") != ev.get("eval_id") or not ev.get(
                    "eval_id"
                ):
                    issues.append("exact_evaluation_link_mismatch")
                labels = _rows(
                    snapshot.conn,
                    "candidate_labels",
                    "label_id",
                    diagnostic.get("label_id"),
                )
                if len(labels) != 1:
                    missing.append("exact_classifier_unavailable")
                else:
                    label = labels[0]
                    if diagnostic.get("classifier_decision") != label.get(
                        "label_decision"
                    ) or adj.get("label_decision") != label.get("label_decision"):
                        issues.append("classifier_decision_link_mismatch")
                    ai = v1.e._object_json(
                        adj.get("ai_lineage_json"), "invalid_ai_lineage"
                    ).get("label", {})
                    if (
                        ai.get("model") != label.get("model")
                        or ai.get("model_provider") != label.get("label_source")
                        or ai.get("is_mock") is not False
                    ):
                        missing.append("classifier_call_identity_unverifiable")
                    for field in ("prompt_hash", "system_prompt_hash"):
                        if not isinstance(ai.get(field), str) or not re.fullmatch(
                            r"[0-9a-f]{16}", ai[field]
                        ):
                            missing.append("classifier_" + field + "_unavailable")
                    if (
                        any(
                            label.get(k) != val
                            for k, val in {
                                "candidate_id": cid,
                                "scan_batch_id": bid,
                                "symbol": candidate["symbol"],
                                "packet_id": adj.get("packet_id"),
                                **q["combined_policy"]["classifier_identity"],
                            }.items()
                        )
                        or label.get("is_mock") != 0
                        or label.get("validation_status") != "passed"
                    ):
                        issues.append("classifier_identity_or_validation_mismatch")
                    if (
                        not v1.c._dt(candidate["created_at_utc"])
                        <= v1.c._dt(label["created_at_utc"])
                        <= v1.c._dt(ev["created_at_utc"])
                    ):
                        issues.append("classifier_chronology_mismatch")
                config = b["configuration_slice"]
                if (
                    type(diagnostic.get("confidence_floor")) not in (int, float)
                    or type(diagnostic.get("min_reward_risk")) not in (int, float)
                    or diagnostic.get("confidence_floor")
                    != config["label_min_confidence_to_propose"]
                    or diagnostic.get("min_reward_risk") != config["min_reward_risk"]
                    or adj.get("override_enabled")
                    != int(config["labeller_decision_override_enabled"])
                ):
                    issues.append("recorded_combination_settings_mismatch")
                if any(
                    ev.get(k) != val
                    for k, val in q["protocol"]["policies"]["tested"][
                        "identity"
                    ].items()
                ):
                    issues.append("recorded_primary_policy_identity_mismatch")
                if v1.c._dt(ev["created_at_utc"]) > v1.c._dt(adj["created_at_utc"]):
                    issues.append("evaluation_after_adjustment")
                reference = stage_rows.get("reference", {})
                if (
                    reference.get("setup_card_id")
                    != q["protocol"]["policies"]["reference"]["setup_card_id"]
                ):
                    issues.append("reference_setup_identity_mismatch")
                if v1.c._dt(adj["created_at_utc"]) > v1.c._dt(
                    reference.get("created_at_utc")
                ):
                    issues.append("reference_before_resolved_adjustment")
            except (ValueError, TypeError, KeyError, AttributeError):
                missing.append("combination_evidence_unverifiable")
            configs = {
                s.get("recorded_config_hash")
                for s in stages.values()
                if s.get("recorded_config_hash")
            }
            if len(configs) > 1:
                issues.append("stage_configuration_hashes_differ")
            candidates.append(
                {
                    "candidate_id": cid,
                    "scan_batch_id": bid,
                    "stages": stages,
                    "contradictions": sorted(set(issues)),
                    "missing_evidence": sorted(set(missing)),
                    "recorded_identity_status": "contradictory"
                    if issues
                    else "missing"
                    if missing
                    else "recorded_consistent",
                    "full_settings_preimage_verified": False,
                    "runtime_execution_attested": False,
                }
            )
    snapshot.verify()
    return {
        "version": VERSION,
        "policy_binding_sha256": expected_binding_sha256,
        "source_sha256": snapshot.expected_sha256,
        "source_inspected_at_utc": before.astimezone(timezone.utc).isoformat(),
        "recorded_at_utc": v1._clock().isoformat(),
        "batch_count": len(observation["inventory"]["batches"]),
        "core_candidate_count": len(candidates),
        "candidates": candidates,
        "contradictory_candidate_count": sum(
            bool(x["contradictions"]) for x in candidates
        ),
        "missing_candidate_count": sum(bool(x["missing_evidence"]) for x in candidates),
        "recorded_consistent_candidate_count": sum(
            x["recorded_identity_status"] == "recorded_consistent" for x in candidates
        ),
        "execution_binding_status": "unattested",
        "runtime_execution_attested": False,
        "coverage_blockers": [
            "executed_combined_policy_not_attested",
            "complete_settings_preimage_unavailable",
            "selected_source_dependency_closure_unverified",
        ],
        "limitations": LIMITS
        + [
            "Recorded revision mismatch is an exact contract mismatch, not by itself proof selected policy bytes changed.",
            "git_dirty may reflect untracked artifacts; it neither authenticates nor proves a selected-file change.",
        ],
        **v1.FLAGS,
    }
