"""Offline selected-source proof and populated designated registry lifecycle."""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from alphaos.journal.schema import SCHEMA
from alphaos.lineage.hashing import stable_hash
from alphaos.research import hold10_designated_registry as registry
from alphaos.research import hold10_policy_binding as policy
from alphaos.research import hold10_prospective as v1
from test_hold10_prospective import fixture as old_fixture


def fixture(tmp_path, monkeypatch):
    root, path, q, clock = old_fixture(tmp_path, monkeypatch)
    code = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=code, text=True
    ).strip()
    q["protocol"]["source_revision"] = revision
    q["protocol"]["policies"]["tested"]["identity"]["system_prompt_hash"] = "5" * 16
    q["combined_policy"]["source_revision"] = revision
    q["combined_policy"]["files"] = {
        name: hashlib.sha256((code / name).read_bytes()).hexdigest()
        for name in v1.POLICY_FILES
    }
    binding = policy.create_policy_binding(
        q,
        expected_contract_sha256=v1.sha(q),
        code_root=code,
        configuration=q["combined_policy"]["configuration"],
    )
    with sqlite3.connect(path) as db:
        for table in (
            "candidates",
            "openai_evaluations",
            "decision_adjustments",
            "shadow_baseline_decisions",
        ):
            fields = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            if "lineage_id" not in fields:
                db.execute(f"ALTER TABLE {table} ADD COLUMN lineage_id TEXT")
        for column, kind in [
            ("override_enabled", "INTEGER"),
            ("label_decision", "TEXT"),
            ("ai_lineage_json", "TEXT"),
        ]:
            fields = {
                r[1] for r in db.execute("PRAGMA table_info(decision_adjustments)")
            }
            if column not in fields:
                db.execute(
                    f"ALTER TABLE decision_adjustments ADD COLUMN {column} {kind}"
                )
        db.execute("ALTER TABLE candidate_labels ADD COLUMN label_decision TEXT")
        db.execute(next(sql for name, sql in SCHEMA if name == "lineage_snapshots"))
        db.execute("CREATE TABLE config_versions(id,config_hash,created_at_utc)")
        names = [r[1] for r in db.execute("PRAGMA table_info(lineage_snapshots)")]
        fields = {
            name: None
            for name in names
            if name not in {"id", "lineage_id", "created_at_utc", "created_at_sgt"}
        }
        fields.update(git_commit_sha=revision, git_branch="synthetic", git_dirty=1)
        fields.update({name: "1" * 16 for name in fields if name.endswith("_hash")})
        lid = stable_hash(fields)
        values = {
            "id": 1,
            "lineage_id": lid,
            **fields,
            "created_at_utc": "2026-01-01T11:00:00Z",
            "created_at_sgt": "synthetic",
        }
        db.execute(
            "INSERT INTO lineage_snapshots VALUES("
            + ",".join("?" for _ in names)
            + ")",
            [values[k] for k in names],
        )
        for table in (
            "candidates",
            "openai_evaluations",
            "decision_adjustments",
            "shadow_baseline_decisions",
        ):
            db.execute(f"UPDATE {table} SET lineage_id=?", (lid,))
        for cid, raw, final, original in db.execute(
            "SELECT candidate_id,evidence_json,final_decision,eval_decision FROM decision_adjustments"
        ).fetchall():
            data = json.loads(raw)
            data["watch_advisory_diagnostic"].update(
                primary_decision=original,
                classifier_decision=final,
                current_decision=final,
            )
            ai = {
                "label": {
                    "model": "saved-classifier",
                    "is_mock": False,
                    "model_provider": "openai",
                    "prompt_hash": "2" * 16,
                    "system_prompt_hash": "3" * 16,
                }
            }
            db.execute(
                "UPDATE decision_adjustments SET override_enabled=1,label_decision=?,evidence_json=?,ai_lineage_json=? WHERE candidate_id=?",
                (final, json.dumps(data), json.dumps(ai), cid),
            )
            db.execute(
                "UPDATE candidate_labels SET label_decision=? WHERE candidate_id=?",
                (final, cid),
            )
        db.execute("UPDATE openai_evaluations SET prompt_hash=?", ("4" * 16,))
        db.execute("UPDATE openai_evaluations SET system_prompt_hash=?", ("5" * 16,))
    return root, path, q, binding, clock


def start(fx):
    root, _, q, b, _ = fx
    api = registry.DesignatedHold10(root, dry_run=True)
    result = api.register(
        q, b, expected_contract_sha256=v1.sha(q), expected_binding_sha256=v1.sha(b)
    )
    return api, result


def capture(fx, api, result):
    root, path, _, _, _ = fx
    source = v1.c.SavedLedger(path, v1.c._file_hash(path), root=root)
    try:
        return api.capture(
            source,
            expected_designation_sha256=result["designation_sha256"],
            expected_previous_capture_sha256=result["state"]["latest_capture_sha256"],
        )
    finally:
        source.close()


def test_populated_designated_register_capture_reopen_cancel(tmp_path, monkeypatch):
    fx = fixture(tmp_path, monkeypatch)
    api, result = start(fx)
    fx[-1][0] = datetime.fromisoformat("2026-01-02T15:20:00+00:00")
    result = capture(fx, api, result)
    assert result["state"]["full_candidate_count"] == 2
    assert result["policy_audit"]["recorded_consistent_candidate_count"] == 2
    assert result["policy_audit"]["runtime_execution_attested"] is False
    reopened = registry.DesignatedHold10(fx[0], dry_run=True).read(
        expected_designation_sha256=result["designation_sha256"]
    )
    assert reopened["state"] == result["state"]
    assert "policy_audit" not in reopened  # Response, not durable execution proof.
    cancelled = api.cancel(
        expected_designation_sha256=result["designation_sha256"],
        expected_previous_capture_sha256=result["state"]["latest_capture_sha256"],
    )
    assert cancelled["state"]["state"] == "cancelled"


def test_contradiction_refuses_whole_capture_but_keeps_audit_denominator(
    tmp_path, monkeypatch
):
    fx = fixture(tmp_path, monkeypatch)
    api, result = start(fx)
    with sqlite3.connect(fx[1]) as db:
        db.execute(
            "UPDATE decision_adjustments SET override_enabled=0 WHERE candidate_id='c1'"
        )
    fx[-1][0] = datetime.fromisoformat("2026-01-02T15:20:00+00:00")
    refused = capture(fx, api, result)
    assert refused["status"] == "capture_refused_binding_contradiction"
    assert refused["policy_audit"]["core_candidate_count"] == 2
    assert (
        api.read(expected_designation_sha256=result["designation_sha256"])["state"][
            "captures"
        ]
        == []
    )


def test_missing_lineage_is_missing_preserved_census_not_attested(
    tmp_path, monkeypatch
):
    fx = fixture(tmp_path, monkeypatch)
    api, result = start(fx)
    with sqlite3.connect(fx[1]) as db:
        db.execute("UPDATE candidates SET lineage_id=NULL WHERE candidate_id='c1'")
    fx[-1][0] = datetime.fromisoformat("2026-01-02T15:20:00+00:00")
    result = capture(fx, api, result)
    assert result["state"]["full_candidate_count"] == 2
    assert result["policy_audit"]["missing_candidate_count"] == 1


@pytest.mark.parametrize("phase", ["registry.identity.json", "designation.json"])
def test_explicit_resume_known_durable_stages(tmp_path, monkeypatch, phase):
    fx = fixture(tmp_path, monkeypatch)
    original = registry._publish

    def interrupted(path, value):
        original(path, value)
        if path.name == phase:
            raise RuntimeError("after durable phase")

    monkeypatch.setattr(registry, "_publish", interrupted)
    with pytest.raises(RuntimeError):
        start(fx)
    monkeypatch.setattr(registry, "_publish", original)
    root, _, q, b, _ = fx
    api = registry.DesignatedHold10(root, dry_run=True)
    if phase == "designation.json":
        pin = v1.sha(registry._read(root / registry.DIRECTORY / phase))
        result = api.register(
            q,
            b,
            expected_contract_sha256=v1.sha(q),
            expected_binding_sha256=v1.sha(b),
            expected_designation_sha256=pin,
        )
    else:
        result = api.register(
            q,
            b,
            expected_contract_sha256=v1.sha(q),
            expected_binding_sha256=v1.sha(b),
            resume=True,
        )
    assert result["state"]["state"] == "registered_future"


@pytest.mark.parametrize("change", ["symlink", "hardlink", "replace", "missing"])
def test_authoritative_registry_replacement_or_alias_refused(
    tmp_path, monkeypatch, change
):
    fx = fixture(tmp_path, monkeypatch)
    api, result = start(fx)
    path = fx[0] / registry.DIRECTORY / "registry.sqlite"
    saved = fx[0] / "retained.db"
    path.rename(saved)
    if change == "symlink":
        path.symlink_to(saved)
    elif change == "hardlink":
        path.hardlink_to(saved)
    elif change == "replace":
        path.write_bytes(saved.read_bytes())
    with pytest.raises((ValueError, FileNotFoundError)):
        api.read(expected_designation_sha256=result["designation_sha256"])
    assert saved.exists()


def test_policy_slice_boolean_cannot_alias_numeric_contract(tmp_path, monkeypatch):
    _, _, q, b, _ = fixture(tmp_path, monkeypatch)
    q["combined_policy"]["configuration"]["label_min_confidence_to_propose"] = 1.0
    b["contract_sha256"] = v1.sha(q)
    b["configuration_slice"] = deepcopy(q["combined_policy"]["configuration"])
    b["configuration_slice"]["label_min_confidence_to_propose"] = True
    b["configuration_slice_sha256"] = v1.sha(q["combined_policy"]["configuration"])
    with pytest.raises(ValueError):
        policy.validate_binding(b, q, expected_binding_sha256=v1.sha(b))


def test_read_preserves_binding_before_immutable_intent_order(tmp_path, monkeypatch):
    fx = fixture(tmp_path, monkeypatch)
    api, result = start(fx)
    folder = fx[0] / registry.DIRECTORY
    intent = registry._read(folder / "intent.json")
    intent["binding"]["recorded_at_utc"] = "2026-01-01T12:01:00+00:00"
    intent["binding_sha256"] = v1.sha(intent["binding"])
    manifest = deepcopy(result["designation"])
    manifest.update(
        binding_sha256=intent["binding_sha256"],
        intent_sha256=v1.sha(intent),
        created_at_utc="2026-01-01T12:01:01+00:00",
    )
    (folder / "intent.json").write_text(v1.h._json(intent))
    (folder / "designation.json").write_text(v1.h._json(manifest))
    fx[-1][0] = datetime.fromisoformat("2026-01-01T12:03:00+00:00")
    # Even a caller-pinned, internally rehashed fixture must preserve chronology.
    with pytest.raises(ValueError, match="designation_clock_invalid"):
        api.read(expected_designation_sha256=v1.sha(manifest))


def test_default_disabled_without_workspace():
    api = registry.DesignatedHold10(object())
    assert (
        api.register(
            None, None, expected_contract_sha256=None, expected_binding_sha256=None
        )["status"]
        == "disabled"
    )
    assert (
        api.capture(
            None,
            expected_designation_sha256=None,
            expected_previous_capture_sha256=None,
        )["status"]
        == "disabled"
    )
