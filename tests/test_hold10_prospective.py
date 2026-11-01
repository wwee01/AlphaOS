"""Manual registry boundaries; every clock/source is synthetic and offline."""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from alphaos.research import hold10_prospective as m
from test_hold10_enrollment import setup


def fixture(tmp_path, monkeypatch, *, incomplete=False):
    root = tmp_path.resolve()
    (root / "CURRENT-STATUS.md").write_text("Synthetic scratch workspace")
    source, protocol, _ = setup("final_proposed")
    if not incomplete:
        source.execute("DELETE FROM candidates WHERE candidate_id='c3'")
        source.execute("UPDATE scan_batches SET candidates_found=2")
    source.execute("ALTER TABLE scan_batches ADD COLUMN scheduler_run_id TEXT")
    source.execute("UPDATE scan_batches SET scheduler_run_id='owner'")
    source.execute(
        "CREATE TABLE candidate_labels(label_id,candidate_id,packet_id,scan_batch_id,symbol,model,label_version,label_source,validation_status,is_mock,created_at_utc)"
    )
    for row in source.execute(
        "SELECT id,evidence_json,final_decision FROM decision_adjustments"
    ).fetchall():
        evidence = json.loads(row[1])
        evidence["watch_advisory_diagnostic"].update(
            confidence_floor=0.5,
            min_reward_risk=1.2,
            current_decision=row[2],
            is_mock=False,
            label_id=f"label{row[0]}",
        )
        source.execute(
            "INSERT INTO candidate_labels VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"label{row[0]}",
                f"c{row[0]}",
                f"pk{row[0]}",
                "batch",
                f"A{row[0]}",
                "saved-classifier",
                "v1",
                "openai",
                "passed",
                0,
                f"2026-01-02T15:0{row[0]}:06Z",
            ),
        )
        source.execute(
            "UPDATE decision_adjustments SET evidence_json=? WHERE id=?",
            (json.dumps(evidence), row[0]),
        )
    source.execute(
        "CREATE TABLE job_runs(job_run_id,job_type,lock_key,status,started_at_utc,finished_at_utc,result_summary_json,scheduler_run_id)"
    )
    source.execute(
        "INSERT INTO job_runs VALUES(?,?,?,?,?,?,?,?)",
        (
            "job",
            "scan",
            "scan:2026-01-02T09:30",
            "completed",
            "2026-01-02T14:59:00Z",
            "2026-01-02T15:11:00Z",
            json.dumps({"scan_summary": {"scan_batch_id": "batch"}}),
            "owner",
        ),
    )
    source.commit()
    path = root / "copy.db"
    with sqlite3.connect(path) as target:
        source.backup(target)
    source.close()
    protocol["source_revision"] = "a" * 40
    clock = [datetime.fromisoformat("2026-01-01T12:00:00+00:00")]
    monkeypatch.setattr(m, "_now", lambda: clock[0])
    contract = {
        "version": m.VERSION,
        "registration_id": "hold10-prospective-draft:synthetic",
        "family_id": m.FAMILY,
        "protocol": protocol,
        "combined_policy": {
            "source_revision": "a" * 40,
            "files": {p: "a" * 64 for p in m.POLICY_FILES},
            "classifier_identity": {
                "model": "saved-classifier",
                "label_version": "v1",
                "label_source": "openai",
            },
            "nonsecret_settings_sha256": "a" * 64,
            "configuration": {
                "labeller_decision_override_enabled": True,
                "label_min_confidence_to_propose": 0.5,
                "min_reward_risk": 1.2,
            },
            "execution_identity": "declared_source_and_settings_not_runtime_attestation",
        },
        "start_at_utc": "2026-01-02T05:00:00+00:00",
        "end_at_utc": "2026-02-02T05:00:00+00:00",
        "close_at_utc": "2026-02-20T22:00:00+00:00",
        "schedule": {
            "calendar_sha256": m._calendar_sha(),
            "windows_et": [["09:30", "16:00"]],
            "scope": "full_closure_calendar_all_retained_core_batches",
        },
        "stopping_rule": "one_fixed_deadline_no_extension",
        "analysis_mode": "disabled_pending_method_validation",
        "original_constraints": deepcopy(m.FLOORS),
        **m.FLAGS,
    }
    return root, path, contract, clock


def register(q, path=":memory:"):
    db = sqlite3.connect(path)
    m.initialize_prospective_registry(db, purpose="inactive_manual_prospective_dry_run")
    api = m.ProspectiveHold10(db, dry_run=True)
    state = api.register(q, expected_contract_sha256=m.sha(q))
    return db, api, state


def capture(api, root, path, state):
    snap = m.c.SavedLedger(path, m.c._file_hash(path), root=root)
    try:
        return api.capture(
            snap,
            state["registration"]["registration_id"],
            expected_registration_sha256=state["registration"]["receipt_sha256"],
            expected_previous_capture_sha256=state["latest_capture_sha256"],
        )
    finally:
        snap.close()


def test_default_disabled_touches_no_connection():
    api = m.ProspectiveHold10(object())
    assert api.register(None, expected_contract_sha256=None)["status"] == "disabled"
    assert (
        api.capture(
            None,
            None,
            expected_registration_sha256=None,
            expected_previous_capture_sha256=None,
        )["status"]
        == "disabled"
    )


def test_actual_register_then_timely_capture_full_roster_restart(tmp_path, monkeypatch):
    root, path, q, clock = fixture(tmp_path, monkeypatch)
    original = path.read_bytes()
    db, api, state = register(q, tmp_path / "receiver.db")
    assert state["state"] == "registered_future"
    assert len(state["schedule_slots"]) == 20  # Jan2–30 excluding MLK
    clock[0] = datetime.fromisoformat("2026-01-02T15:20:00+00:00")
    state = capture(api, root, path, state)
    assert state["full_candidate_count"] == 2
    assert state["timely_complete_batch_count"] == 1
    assert state["frozen_batches"][0]["export"]["manifest"]["all_opportunity_ids"] == [
        "c1",
        "c2",
    ]
    assert not state["activated"] and not state["prospective_execution_certified"]
    db.close()
    db = sqlite3.connect(tmp_path / "receiver.db")
    assert m.ProspectiveHold10(db).read(q["registration_id"]) == state
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "at", ["2026-01-05T14:30:00+00:00", "2026-01-05T15:00:00+00:00"]
)
def test_first_outcome_open_and_later_are_irrevocably_late(tmp_path, monkeypatch, at):
    root, path, q, clock = fixture(tmp_path, monkeypatch)
    _, api, state = register(q)
    clock[0] = datetime.fromisoformat(at)
    state = capture(api, root, path, state)
    assert state["timely_complete_batch_count"] == 0
    assert state["frozen_batches"][0]["status"] == "late_first_observation"


def test_commit_crossing_open_cannot_claim_preoutcome(tmp_path, monkeypatch):
    root, path, q, clock = fixture(tmp_path, monkeypatch)
    _, api, state = register(q)
    clock[0] = datetime.fromisoformat("2026-01-05T14:29:59+00:00")
    original = m._observe

    def crossing(*args):
        result = original(*args)
        clock[0] = datetime.fromisoformat("2026-01-05T14:30:00+00:00")
        return result

    monkeypatch.setattr(m, "_observe", crossing)
    state = capture(api, root, path, state)
    assert state["frozen_batches"][0]["status"] == "late_at_commit"
    assert state["timely_complete_batch_count"] == 0


def test_late_registration_no_retrospective_relabel(tmp_path, monkeypatch):
    _, _, q, clock = fixture(tmp_path, monkeypatch)
    clock[0] = datetime.fromisoformat(q["start_at_utc"])
    with pytest.raises(ValueError, match="registration_must_precede"):
        register(q)


@pytest.mark.parametrize(
    "field,value",
    [
        ("activated", True),
        ("family_id", "alternative"),
        ("analysis_mode", "evaluate_now"),
        ("stopping_rule", "wait_for_significance"),
    ],
)
def test_fixed_inactive_family_question_and_stop(tmp_path, monkeypatch, field, value):
    _, _, q, _ = fixture(tmp_path, monkeypatch)
    q[field] = value
    with pytest.raises(ValueError):
        register(q)


def test_alias_and_changed_interval_under_same_family_refused(tmp_path, monkeypatch):
    _, _, q, _ = fixture(tmp_path, monkeypatch)
    _, api, state = register(q)
    assert api.register(q, expected_contract_sha256=m.sha(q)) == state
    q["registration_id"] += "-renamed"
    with pytest.raises(ValueError, match="one_local_family"):
        api.register(q, expected_contract_sha256=m.sha(q))


def test_missing_plans_frozen_before_later_improvement(tmp_path, monkeypatch):
    root, path, q, clock = fixture(tmp_path, monkeypatch, incomplete=True)
    _, api, state = register(q)
    clock[0] = datetime.fromisoformat("2026-01-02T15:20:00+00:00")
    state = capture(api, root, path, state)
    assert state["full_candidate_count"] == 3
    assert state["frozen_batches"][0]["status"] == "timely_incomplete_input_freeze"
    with sqlite3.connect(path) as source:
        source.execute("DELETE FROM candidates WHERE candidate_id='c3'")
        source.execute("UPDATE scan_batches SET candidates_found=2")
    state = capture(api, root, path, state)
    assert state["full_candidate_count"] == 3
    assert "frozen_batch_inputs_changed:batch" in state["coverage_blockers"]


def test_failed_batch_not_replaced_by_successful_retry(tmp_path, monkeypatch):
    root, path, q, clock = fixture(tmp_path, monkeypatch)
    with sqlite3.connect(path) as source:
        source.execute("UPDATE scan_batches SET status='failed'")
    _, api, state = register(q)
    clock[0] = datetime.fromisoformat("2026-01-02T15:20:00+00:00")
    state = capture(api, root, path, state)
    with sqlite3.connect(path) as source:
        source.execute("UPDATE scan_batches SET status='completed'")
    state = capture(api, root, path, state)
    assert state["frozen_batches"][0]["status"] == "failed_batch"
    assert state["timely_complete_batch_count"] == 0


def test_zero_missing_slots_and_expiry_not_silently_complete(tmp_path, monkeypatch):
    root, path, q, clock = fixture(tmp_path, monkeypatch)
    with sqlite3.connect(path) as source:
        for table in m.e.FIELDS:
            source.execute(f"DELETE FROM {table}")
        source.execute("DELETE FROM job_runs")
    _, api, state = register(q)
    clock[0] = datetime.fromisoformat("2026-01-02T22:00:00+00:00")
    state = capture(api, root, path, state)
    assert (
        state["full_candidate_count"] == 0 and len(state["missing_elapsed_slots"]) == 1
    )
    clock[0] = datetime.fromisoformat(q["close_at_utc"])
    state = api.read(q["registration_id"])
    assert state["state"] == "expired" and len(state["missing_elapsed_slots"]) == 20
    with pytest.raises(ValueError, match="open_registered"):
        capture(api, root, path, state)


def test_cancel_append_only_and_blocks_capture(tmp_path, monkeypatch):
    root, path, q, clock = fixture(tmp_path, monkeypatch)
    db, api, state = register(q)
    result = api.cancel(
        q["registration_id"],
        expected_registration_sha256=state["registration"]["receipt_sha256"],
        expected_previous_capture_sha256=None,
    )
    assert result["state"] == "cancelled"
    clock[0] = datetime.fromisoformat("2026-01-02T15:20:00+00:00")
    with pytest.raises(ValueError, match="open_registered"):
        capture(api, root, path, result)
    assert db.execute(f"SELECT count(*) FROM {m.TABLES[0]}").fetchone()[0] == 1


def test_cas_stale_previous_no_extra_capture(tmp_path, monkeypatch):
    root, path, q, clock = fixture(tmp_path, monkeypatch)
    db, api, state = register(q)
    clock[0] = datetime.fromisoformat("2026-01-02T15:20:00+00:00")
    capture(api, root, path, state)
    with pytest.raises(ValueError, match="latest_capture_pin"):
        capture(api, root, path, state)
    assert db.execute(f"SELECT count(*) FROM {m.TABLES[1]}").fetchone()[0] == 1


@pytest.mark.parametrize("table", m.TABLES)
@pytest.mark.parametrize("action", ["update", "delete"])
def test_corrupt_guard_refused_on_read_and_initializer(
    tmp_path, monkeypatch, table, action
):
    _, _, q, _ = fixture(tmp_path, monkeypatch)
    db, api, _ = register(q)
    db.execute(f"DROP TRIGGER {table}_immutable_{action}")
    with pytest.raises(ValueError, match="schema_or_immutability"):
        api.read(q["registration_id"])
    with pytest.raises(ValueError, match="schema_or_immutability"):
        m.initialize_prospective_registry(
            db, purpose="inactive_manual_prospective_dry_run"
        )


def test_initializer_refuses_existing_application_database():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE paper_orders(id)")
    with pytest.raises(ValueError, match="dedicated_registry"):
        m.initialize_prospective_registry(
            db, purpose="inactive_manual_prospective_dry_run"
        )
    assert db.execute("SELECT name FROM sqlite_master").fetchall() == [
        ("paper_orders",)
    ]


def test_source_view_cannot_read_outcomes(tmp_path, monkeypatch):
    root, path, q, clock = fixture(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE candidates RENAME TO old_candidates")
        db.execute("CREATE TABLE outcomes(secret)")
        db.execute(
            "CREATE VIEW candidates AS SELECT * FROM old_candidates WHERE NOT EXISTS(SELECT 1 FROM outcomes)"
        )
    _, api, state = register(q)
    clock[0] = datetime.fromisoformat("2026-01-02T15:20:00+00:00")
    with pytest.raises(ValueError, match="plain_saved_input"):
        capture(api, root, path, state)


def test_bad_pins_unknown_fields_future_clock_and_source_hash(tmp_path, monkeypatch):
    root, path, q, clock = fixture(tmp_path, monkeypatch)
    db, api, state = register(q)
    wrong = deepcopy(q)
    wrong["notes"] = "cosmetic"
    with pytest.raises(ValueError, match="exact_prospective"):
        api.register(wrong, expected_contract_sha256=m.sha(wrong))
    with pytest.raises(ValueError, match="snapshot_hash"):
        m.c.SavedLedger(path, "0" * 64, root=root)
    clock[0] = datetime.fromisoformat("2025-12-31T00:00:00+00:00")
    with pytest.raises(ValueError, match="future_clock"):
        api.read(q["registration_id"])
    assert db.execute(f"SELECT count(*) FROM {m.TABLES[1]}").fetchone()[0] == 0


def test_no_outcome_or_bar_tables_are_needed(tmp_path, monkeypatch):
    root, path, q, clock = fixture(tmp_path, monkeypatch)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    _, api, state = register(q)
    clock[0] = datetime.fromisoformat("2026-01-02T15:20:00+00:00")
    state = capture(api, root, path, state)
    assert state["formal_inference"] is None
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == before


def test_unchanged_source_after_open_retains_first_receipt_without_false_change(
    tmp_path, monkeypatch
):
    root, path, q, clock = fixture(tmp_path, monkeypatch)
    _, api, state = register(q)
    clock[0] = datetime.fromisoformat("2026-01-02T15:20:00+00:00")
    first = capture(api, root, path, state)
    clock[0] = datetime.fromisoformat("2026-01-05T15:00:00+00:00")
    second = capture(api, root, path, first)
    assert second["frozen_batches"] == first["frozen_batches"]
    assert "frozen_batch_inputs_changed:batch" not in second["coverage_blockers"]
    with sqlite3.connect(path) as source:
        source.execute(
            "UPDATE openai_evaluations SET target=130 WHERE candidate_id='c2'"
        )
    third = capture(api, root, path, second)
    assert "frozen_batch_inputs_changed:batch" in third["coverage_blockers"]


def test_linked_candidate_after_interval_is_retained_with_boundary_gap(
    tmp_path, monkeypatch
):
    root, path, q, clock = fixture(tmp_path, monkeypatch)
    with sqlite3.connect(path) as source:
        source.execute(
            "UPDATE scan_batches SET started_at_utc='2026-01-30T20:00:00Z',created_at_utc='2026-01-30T20:00:00Z',completed_at_utc='2026-02-02T12:05:00Z'"
        )
        source.execute("UPDATE candidates SET created_at_utc='2026-02-02T12:00:00Z'")
    _, api, state = register(q)
    clock[0] = datetime.fromisoformat("2026-02-02T12:10:00+00:00")
    state = capture(api, root, path, state)
    assert state["full_candidate_count"] == 2
    assert (
        state["frozen_batches"][0]["status"] == "candidate_outside_registered_interval"
    )


def test_classifier_revision_mismatch_retains_missing_input(tmp_path, monkeypatch):
    root, path, q, clock = fixture(tmp_path, monkeypatch)
    with sqlite3.connect(path) as source:
        source.execute("UPDATE candidate_labels SET model='foreign'")
    _, api, state = register(q)
    clock[0] = datetime.fromisoformat("2026-01-02T15:20:00+00:00")
    state = capture(api, root, path, state)
    assert state["full_candidate_count"] == 2
    assert state["timely_complete_batch_count"] == 0


def test_source_read_then_concurrent_cancel_refuses_capture(tmp_path, monkeypatch):
    root, path, q, clock = fixture(tmp_path, monkeypatch)
    db, api, state = register(q, tmp_path / "receiver.db")
    other = sqlite3.connect(tmp_path / "receiver.db")
    clock[0] = datetime.fromisoformat("2026-01-02T15:20:00+00:00")
    original = m._observe

    def race(*args):
        result = original(*args)
        m.ProspectiveHold10(other, dry_run=True).cancel(
            q["registration_id"],
            expected_registration_sha256=state["registration"]["receipt_sha256"],
            expected_previous_capture_sha256=None,
        )
        return result

    monkeypatch.setattr(m, "_observe", race)
    with pytest.raises(ValueError, match="concurrent_registry_change"):
        capture(api, root, path, state)
    assert db.execute(f"SELECT count(*) FROM {m.TABLES[1]}").fetchone()[0] == 1
    assert api.read(q["registration_id"])["state"] == "cancelled"


def test_exception_after_insert_rolls_back_whole_capture(tmp_path, monkeypatch):
    root, path, q, clock = fixture(tmp_path, monkeypatch)
    db, api, state = register(q)
    clock[0] = datetime.fromisoformat("2026-01-02T15:20:00+00:00")
    original = m.h._insert

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt("synthetic process interruption")

    monkeypatch.setattr(m.h, "_insert", crash)
    with pytest.raises(KeyboardInterrupt):
        capture(api, root, path, state)
    assert db.execute(f"SELECT count(*) FROM {m.TABLES[1]}").fetchone()[0] == 0
    assert not db.in_transaction


def test_history_bound_refuses_without_partial_append(tmp_path, monkeypatch):
    root, path, q, clock = fixture(tmp_path, monkeypatch)
    db, api, state = register(q)
    clock[0] = datetime.fromisoformat("2026-01-02T15:20:00+00:00")
    first = capture(api, root, path, state)
    monkeypatch.setattr(m, "MAX_CAPTURES", 1)
    with pytest.raises(ValueError, match="bounded_capture_history"):
        capture(api, root, path, first)
    assert db.execute(f"SELECT count(*) FROM {m.TABLES[1]}").fetchone()[0] == 1


def test_owned_copy_before_clock_callback_mutates_caller(tmp_path, monkeypatch):
    _, _, q, clock = fixture(tmp_path, monkeypatch)
    pin = m.sha(q)
    monkeypatch.setattr(m, "_now", lambda: (q.update(family_id="foreign"), clock[0])[1])
    db = sqlite3.connect(":memory:")
    m.initialize_prospective_registry(db, purpose="inactive_manual_prospective_dry_run")
    result = m.ProspectiveHold10(db, dry_run=True).register(
        q, expected_contract_sha256=pin
    )
    assert result["registration"]["payload"]["contract"]["family_id"] == m.FAMILY


def test_read_refuses_resealed_incomplete_to_complete_status(tmp_path, monkeypatch):
    root, path, q, clock = fixture(tmp_path, monkeypatch, incomplete=True)
    db, api, state = register(q)
    clock[0] = datetime.fromisoformat("2026-01-02T15:20:00+00:00")
    capture(api, root, path, state)
    row = db.execute(f"SELECT * FROM {m.TABLES[1]}")
    raw = dict(zip([col[0] for col in row.description], row.fetchone()))
    payload = json.loads(raw.pop("payload_json"))
    payload["dispositions"][0]["status"] = "timely_input_freeze"
    raw.pop("receipt_sha256")
    raw["payload_sha256"] = m.sha(payload)
    trigger = db.execute(
        "SELECT sql FROM sqlite_master WHERE name=?",
        (m.TABLES[1] + "_immutable_update",),
    ).fetchone()[0]
    db.execute(f"DROP TRIGGER {m.TABLES[1]}_immutable_update")
    db.execute(
        f"UPDATE {m.TABLES[1]} SET payload_json=?,payload_sha256=?,receipt_sha256=?",
        (m.h._json(payload), raw["payload_sha256"], m.sha(raw)),
    )
    db.execute(trigger)
    db.commit()
    with pytest.raises(ValueError, match="incomplete_saved_inputs"):
        api.read(q["registration_id"])


def test_full_capture_capacity_still_allows_one_terminal_withdrawal(
    tmp_path, monkeypatch
):
    root, path, q, clock = fixture(tmp_path, monkeypatch)
    _, api, state = register(q)
    clock[0] = datetime.fromisoformat("2026-01-02T15:20:00+00:00")
    first = capture(api, root, path, state)
    monkeypatch.setattr(m, "MAX_CAPTURES", 1)
    exact_bytes = len(m.h._json(first["captures"][0]["payload"]).encode())
    assert len(m.h._json(first["registration"]["payload"]).encode()) < exact_bytes
    monkeypatch.setattr(m, "MAX_HISTORY_BYTES", exact_bytes)
    result = api.cancel(
        q["registration_id"],
        expected_registration_sha256=first["registration"]["receipt_sha256"],
        expected_previous_capture_sha256=first["latest_capture_sha256"],
    )
    assert result["state"] == "cancelled" and len(result["captures"]) == 2
    assert api.read(q["registration_id"]) == result
    assert (
        api.cancel(
            q["registration_id"],
            expected_registration_sha256=result["registration"]["receipt_sha256"],
            expected_previous_capture_sha256=result["latest_capture_sha256"],
        )
        == result
    )
