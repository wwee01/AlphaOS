"""Saved-batch adapter tests: no runtime, network, broker or implicit schema."""

import json
import sqlite3

import pytest

from alphaos.baseline.rules import _input_sha
from alphaos.research import hold10_enrollment as a
from alphaos.research import hold10_receipts as h
from alphaos.stats.frozen_policy_inference import canonical_sha256 as sha


def put(c, table, values):
    c.execute(
        f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    )
    c.commit()


def setup(population):
    c = sqlite3.connect(":memory:")
    for table, fields in a.FIELDS.items():
        c.execute(f"CREATE TABLE {table} ({fields})")
    put(
        c,
        "scan_batches",
        {
            "id": 1,
            "scan_batch_id": "batch",
            "status": "completed",
            "started_at_utc": "2026-01-02T15:00:00Z",
            "created_at_utc": "2026-01-02T15:00:00Z",
            "completed_at_utc": "2026-01-02T15:10:00Z",
            "candidates_found": 3,
        },
    )
    for n in range(1, 5):
        put(
            c,
            "candidates",
            {
                "id": n,
                "candidate_id": f"c{n}",
                "scan_batch_id": "batch",
                "symbol": f"A{n}",
                "direction": "long",
                "interest_score": 0.8 if n == 1 else 0.4,
                "price_at_scan": 100.0,
                "shadow_tier": 1 if n == 4 else 0,
                "created_at_utc": f"2026-01-02T15:0{n}:00Z",
            },
        )
    for n in (1, 2):
        common = {"id": n, "candidate_id": f"c{n}", "symbol": f"A{n}"}
        put(
            c,
            "candidate_packets",
            {
                **common,
                "packet_id": f"pk{n}",
                "scan_batch_id": "batch",
                "packet_json": json.dumps({"symbol": f"A{n}", "last_price": 100.0}),
                "created_at_utc": f"2026-01-02T15:0{n}:05Z",
            },
        )
        snap = {
            "symbol": f"A{n}",
            "is_mock": False,
            "last_price": 100.0,
            "received_at": "2026-01-02T15:00:00Z",
            "atr_policy": {"atr_14": 5.0, "min_reward_risk": 2.0},
        }
        decision = "propose" if n == 1 else "watch"
        put(
            c,
            "openai_evaluations",
            {
                **common,
                "eval_id": f"e{n}",
                "decision": decision,
                "direction": "long",
                "entry": 100.0,
                "stop": 95.0,
                "target": 120.0,
                "max_holding_days": 5,
                "validation_status": "passed",
                "is_mock": 0,
                "model": "saved-AI",
                "model_provider": "saved-provider",
                "prompt_template_version": "v1",
                "schema_version": "v1",
                "system_prompt_hash": "saved-system-hash",
                "snapshot_json": json.dumps(snap),
                "created_at_utc": f"2026-01-02T15:0{n}:10Z",
            },
        )
        put(
            c,
            "decision_adjustments",
            {
                **common,
                "adjustment_id": f"ad{n}",
                "scan_batch_id": "batch",
                "packet_id": f"pk{n}",
                "eval_decision": decision,
                "final_decision": "watch" if n == 1 else "propose",
                "evidence_json": json.dumps(
                    {"watch_advisory_diagnostic": {"eval_id": f"e{n}"}}
                ),
                "created_at_utc": f"2026-01-02T15:0{n}:11Z",
            },
        )
        for index, rule in enumerate((a.REFERENCE, "propose_all_v1_hold10")):
            raw = {
                "symbol": f"A{n}",
                "direction": "long",
                "interest_score": 0.8 if n == 1 else 0.4,
                "last_price": 100.0,
            }
            action = "propose" if n == 1 or index else "no_action"
            put(
                c,
                "shadow_baseline_decisions",
                {
                    **common,
                    "id": 2 * n + index,
                    "baseline_decision_id": f"b{n}{index}",
                    "scan_batch_id": "batch",
                    "rule_version": rule,
                    "decision": action,
                    "direction": "long" if action == "propose" else None,
                    "entry": 100.0 if action == "propose" else None,
                    "stop": 90.0 if action == "propose" else None,
                    "target": 120.0 if action == "propose" else None,
                    "max_holding_days": 10 if action == "propose" else None,
                    "setup_card_id": "catalyst_momentum_v3",
                    "input_sha": _input_sha(
                        raw,
                        "threshold_v1" if index == 0 else "propose_all_v1",
                        atr_14=5.0,
                        min_reward_risk=2.0,
                        max_holding_days_default=10,
                    ),
                    "decision_at_utc": f"2026-01-02T15:0{n}:12Z",
                    "created_at_utc": f"2026-01-02T15:0{n}:13Z",
                },
            )
    p = {
        "version": h.VERSION,
        "protocol_id": f"hold10-draft:adapter-{population}",
        "status": "draft",
        "predecessor_id": h.PREDECESSOR,
        "horizon_sessions": 10,
        "direction": "greater",
        "effect_target": 0.05,
        "statistic": "paired_mean_candidate_R",
        "policies": {
            "tested": {
                "source": "openai_evaluations",
                "decision_meaning": population,
                "identity": {
                    "model": "saved-AI",
                    "model_provider": "saved-provider",
                    "prompt_template_version": "v1",
                    "schema_version": "v1",
                    "system_prompt_hash": "saved-system-hash",
                },
            },
            "reference": {
                "rule_version": a.REFERENCE,
                "setup_card_id": "catalyst_momentum_v3",
            },
        },
        "risk_contract": {
            "R_basis": "each_arm_original_stop_distance",
            "risk_budget": 100.0,
            "policy_version": "draft-common-risk",
            "currency": "USD",
        },
        "source_revision": "pinned",
        "measurement_method": "saved_daily_complete_horizon_bracket_v1",
        "population": population,
        "enrollment_adapter": {
            "version": a.VERSION,
            "population_scope": "core_completed_scan_batch",
            "reference_rule": a.REFERENCE,
            "ai_horizon": "fixed_10_daily_proxy_preserve_original_saved_hold",
        },
    }
    request = {
        "cohort_id": "cohort",
        "scan_batch_id": "batch",
        "as_of_utc": "2026-01-02T16:00:00Z",
        "source": {"kind": "archived_copy", "sha256": "a" * 64},
    }
    return c, p, request


def inspect(c, p, r):
    return a.build_saved_hold10_manifest(
        c,
        protocol=p,
        expected_protocol_sha256=sha(p),
        request=r,
        expected_request_sha256=sha(r),
    )


def persist(c, receiver, p, r, manifest_pin):
    return a.enroll_saved_hold10(
        c,
        receiver,
        enabled=True,
        protocol=p,
        expected_protocol_sha256=sha(p),
        request=r,
        expected_request_sha256=sha(r),
        expected_manifest_sha256=manifest_pin,
    )


def test_disabled_does_not_even_touch_connections_or_parse_inputs():
    class Bomb:
        def __getattr__(self, key):
            raise AssertionError("default must not touch DB")

    answer = a.enroll_saved_hold10(Bomb(), Bomb(), protocol=object(), request=object())
    assert answer["status"] == "disabled" and answer["writes"] is False


@pytest.mark.parametrize(
    "population,selected", [("primary_proposed", "c1"), ("final_proposed", "c2")]
)
def test_full_input_first_membership_and_explicit_population(population, selected):
    c, p, r = setup(population)
    before = "\n".join(c.iterdump())
    calls = []
    c.set_trace_callback(calls.append)
    result = inspect(c, p, r)
    m = result["manifest"]
    assert m["all_opportunity_ids"] == ["c1", "c2", "c3"]
    assert result["excluded_shadow_candidates"] == 1
    assert [o["opportunity_id"] for o in m["opportunities"] if o["eligible"]] == [
        selected
    ]
    assert result["membership_states"] == {
        "selected": 1,
        "abstained": 1,
        "unresolved": 1,
    }
    assert m["opportunities"][2]["arms"] == {}
    assert all(not d["propose_all_used_as_arm"] for d in result["diagnostics"])
    assert m["opportunities"][0]["arms"]["reference"]["original_risk_per_share"] == 10.0
    if population == "primary_proposed":
        assert m["opportunities"][0]["arms"]["tested"]["original_risk_per_share"] == 5.0
        assert (
            m["opportunities"][0]["arms"]["tested"]["original_saved_max_holding_days"]
            == 5
        )
    assert not any(
        any(
            t in sql.lower()
            for t in ("candidate_outcomes", "trade_outcomes", "daily_bars", "replay_r")
        )
        for sql in calls
    )
    c.set_trace_callback(None)
    assert before == "\n".join(c.iterdump())


def test_outcome_presence_cannot_select_membership_or_change_frozen_export():
    c, p, r = setup("primary_proposed")
    before = inspect(c, p, r)
    c.execute("CREATE TABLE candidate_outcomes(candidate_id,replay_r,replay_status)")
    put(
        c,
        "candidate_outcomes",
        {"candidate_id": "c3", "replay_r": -999.0, "replay_status": "complete"},
    )
    assert inspect(c, p, r) == before
    c.execute("UPDATE candidate_outcomes SET replay_r=999")
    c.commit()
    assert inspect(c, p, r) == before


@pytest.mark.parametrize(
    "mutation,issue,arm",
    [
        ("missing_baseline", "threshold_v1_hold10:missing", "reference"),
        (
            "foreign_symbol",
            "threshold_v1_hold10:identity_or_chronology_mismatch",
            "reference",
        ),
        (
            "foreign_batch",
            "threshold_v1_hold10:identity_or_chronology_mismatch",
            "reference",
        ),
        (
            "future_baseline",
            "threshold_v1_hold10:identity_or_chronology_mismatch",
            "reference",
        ),
        ("wrong_input_sha", "baseline_input_hash_unverified", "reference"),
        ("wrong_horizon", "baseline_saved_horizon_mismatch", "reference"),
        ("duplicate_eval", "openai_evaluations:duplicate", "tested"),
        ("future_snapshot", "ai_input_after_decision", "tested"),
        ("foreign_snapshot", "ai_input_symbol_or_mock_mismatch", "tested"),
        ("mock_eval", "ai_mock_or_unvalidated", "tested"),
        ("invalid_levels", "tested:invalid_original_bracket", "tested"),
        ("different_model", "ai_saved_policy_identity_mismatch", "tested"),
        ("different_card", "baseline_saved_card_mismatch", "reference"),
        ("unknown_control", "baseline_unknown_action_not_cash", "reference"),
    ],
)
def test_missing_bad_sources_stay_as_member_with_explicit_missing_arm(
    mutation, issue, arm
):
    c, p, r = setup("primary_proposed")
    where = "candidate_id='c1' AND rule_version='threshold_v1_hold10'"
    if mutation == "missing_baseline":
        c.execute("DELETE FROM shadow_baseline_decisions WHERE " + where)
    elif mutation == "duplicate_eval":
        c.execute(
            "INSERT INTO openai_evaluations SELECT * FROM openai_evaluations WHERE candidate_id='c1'"
        )
    elif mutation in {"future_snapshot", "foreign_snapshot"}:
        raw = json.loads(
            c.execute(
                "SELECT snapshot_json FROM openai_evaluations WHERE candidate_id='c1'"
            ).fetchone()[0]
        )
        raw.update(
            {"received_at": "2026-01-03T00:00:00Z"}
            if mutation == "future_snapshot"
            else {"symbol": "WRONG"}
        )
        c.execute(
            "UPDATE openai_evaluations SET snapshot_json=? WHERE candidate_id='c1'",
            (json.dumps(raw),),
        )
    elif mutation == "mock_eval":
        c.execute("UPDATE openai_evaluations SET is_mock=1 WHERE candidate_id='c1'")
    elif mutation == "invalid_levels":
        c.execute("UPDATE openai_evaluations SET stop=200 WHERE candidate_id='c1'")
    elif mutation == "different_model":
        c.execute(
            "UPDATE openai_evaluations SET model='other-policy' WHERE candidate_id='c1'"
        )
    else:
        key, value = {
            "foreign_symbol": ("symbol", "WRONG"),
            "foreign_batch": ("scan_batch_id", "other"),
            "future_baseline": ("created_at_utc", "2027-01-01T00:00:00Z"),
            "wrong_input_sha": ("input_sha", "bad"),
            "wrong_horizon": ("max_holding_days", 5),
            "different_card": ("setup_card_id", "unrelated-card"),
            "unknown_control": ("decision", "unknown"),
        }[mutation]
        c.execute(
            f"UPDATE shadow_baseline_decisions SET {key}=? WHERE " + where, (value,)
        )
    c.commit()
    result = inspect(c, p, r)
    assert result["manifest"]["all_opportunity_ids"] == ["c1", "c2", "c3"]
    assert issue in result["diagnostics"][0]["issues"]
    assert arm in result["diagnostics"][0]["missing_arms"]
    assert not result["formal_allowed"]
    receiver = sqlite3.connect(":memory:")
    h.initialize_dry_run_receipts(receiver, purpose="scratch_or_manual_dry_run")
    h.Hold10Receipts(receiver, dry_run=True).register_protocol(
        p, expected_sha256=sha(p)
    )
    persisted = persist(c, receiver, p, r, result["manifest_sha256"])
    assert persisted["status"] == "dry_run_enrolled", persisted
    assert persisted["receipts"]["member_count"] == 6


def test_explicit_wrong_final_eval_id_is_not_treated_as_the_primary_output():
    c, p, r = setup("final_proposed")
    c.execute(
        "UPDATE decision_adjustments SET evidence_json=? WHERE candidate_id='c2'",
        (json.dumps({"watch_advisory_diagnostic": {"eval_id": "foreign"}}),),
    )
    c.commit()
    result = inspect(c, p, r)
    assert result["diagnostics"][1]["membership_state"] == "unresolved"
    assert "final_evaluation_id_mismatch" in result["diagnostics"][1]["issues"]


def test_missing_explicit_final_eval_id_retains_weaker_legacy_link_limitation():
    c, p, r = setup("final_proposed")
    c.execute(
        "UPDATE decision_adjustments SET evidence_json=? WHERE candidate_id='c2'",
        (json.dumps({"legacy_diagnostic": True}),),
    )
    c.commit()
    result = inspect(c, p, r)
    assert result["diagnostics"][1]["membership_state"] == "selected"
    assert (
        "final_eval_id_not_saved_candidate_packet_time_link_only"
        in result["diagnostics"][1]["issues"]
    )
    assert not result["formal_allowed"]
    assert not result["prospective_enrollment"]


@pytest.mark.parametrize("population", ["primary_proposed", "final_proposed"])
@pytest.mark.parametrize("case", ["missing_eval", "late_packet"])
def test_unusable_decision_observation_placeholder_retains_full_batch(population, case):
    c, p, r = setup(population)
    if case == "missing_eval":
        c.execute("DELETE FROM openai_evaluations WHERE candidate_id='c1'")
    else:
        c.execute(
            "UPDATE candidate_packets SET created_at_utc='2026-01-02T15:01:30Z' "
            "WHERE candidate_id='c1'"
        )
    c.commit()
    result = inspect(c, p, r)
    opp = result["manifest"]["opportunities"][0]
    assert (
        opp["decision_anchor_basis"]
        == "input_observation_placeholder_unusable_decision"
    )
    assert opp["decision_at_utc"] == opp["input_available_at_utc"]
    assert "tested" not in opp["arms"]
    assert opp["original_ai_decision_at_utc"] == (
        None if case == "missing_eval" else "2026-01-02T15:01:10Z"
    )
    receiver = sqlite3.connect(":memory:")
    h.initialize_dry_run_receipts(receiver, purpose="scratch_or_manual_dry_run")
    h.Hold10Receipts(receiver, dry_run=True).register_protocol(
        p, expected_sha256=sha(p)
    )
    persisted = persist(c, receiver, p, r, result["manifest_sha256"])
    assert persisted["status"] == "dry_run_enrolled", persisted
    assert persisted["receipts"]["member_count"] == 6
    assert not persisted["formal_allowed"]


@pytest.mark.parametrize(
    "case",
    [
        "incomplete_batch",
        "bad_count",
        "future_candidate",
        "future_capture",
        "unset_population",
        "different_baseline",
        "unbound_policy",
    ],
)
def test_global_scope_contract_errors_are_visible_and_no_partial_receipts(case):
    c, p, r = setup("final_proposed")
    if case == "incomplete_batch":
        c.execute("UPDATE scan_batches SET status='running'")
    elif case == "bad_count":
        c.execute("UPDATE scan_batches SET candidates_found=2")
    elif case == "future_candidate":
        c.execute(
            "UPDATE candidates SET created_at_utc='2026-01-03T00:00:00Z' WHERE candidate_id='c3'"
        )
    elif case == "future_capture":
        r["as_of_utc"] = "2999-01-01T00:00:00Z"
    elif case == "unset_population":
        p.pop("population")
    elif case == "different_baseline":
        p["enrollment_adapter"]["reference_rule"] = "propose_all_v1_hold10"
    else:
        p["policies"]["tested"]["decision_meaning"] = "primary_proposed"
    c.commit()
    receiver = sqlite3.connect(":memory:")
    result = persist(c, receiver, p, r, "a" * 64)
    assert result["status"] == "diagnostic_error"
    assert receiver.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0


def test_persistence_needs_existing_schema_protocol_and_external_manifest_pin(tmp_path):
    c, p, r = setup("primary_proposed")
    exported = inspect(c, p, r)
    path = tmp_path / "receipts.db"
    receiver = sqlite3.connect(path)
    assert (
        persist(c, receiver, p, r, exported["manifest_sha256"])["status"]
        == "diagnostic_error"
    )
    assert receiver.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    h.initialize_dry_run_receipts(receiver, purpose="scratch_or_manual_dry_run")
    h.Hold10Receipts(receiver, dry_run=True).register_protocol(
        p, expected_sha256=sha(p)
    )
    denied = persist(c, receiver, p, r, "b" * 64)
    assert denied["error_code"] == "manifest_changed_since_inspection"
    assert receiver.execute(f"SELECT count(*) FROM {h.TABLES[1]}").fetchone()[0] == 0
    answer = persist(c, receiver, p, r, exported["manifest_sha256"])
    assert answer["status"] == "dry_run_enrolled"
    assert (
        answer["receipts"]["member_count"]
        == answer["receipts"]["missing_measurements"]
        == 6
    )
    assert all(
        m["payload"]["timing_classification"] == "retrospective_or_synthetic_dry_run"
        for m in answer["receipts"]["members"]
    )
    receiver.close()
    receiver = sqlite3.connect(path)
    assert persist(c, receiver, p, r, exported["manifest_sha256"]) == answer
    assert receiver.execute(f"SELECT count(*) FROM {h.TABLES[3]}").fetchone()[0] == 0


def test_source_change_after_review_requires_new_manifest_pin():
    c, p, r = setup("primary_proposed")
    result = inspect(c, p, r)
    c.execute("UPDATE openai_evaluations SET target=125 WHERE candidate_id='c1'")
    c.commit()
    receiver = sqlite3.connect(":memory:")
    denied = persist(c, receiver, p, r, result["manifest_sha256"])
    assert denied["error_code"] == "manifest_changed_since_inspection"
    assert receiver.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0


def test_diagnostic_error_does_not_leak_database_exception_payload():
    c, p, r = setup("primary_proposed")

    class BadSource:
        @property
        def in_transaction(self):
            raise RuntimeError("PRIVATE RAW PAYLOAD")

    result = persist(BadSource(), sqlite3.connect(":memory:"), p, r, "a" * 64)
    assert (
        result["status"] == "diagnostic_error"
        and result["error_type"] == "RuntimeError"
    )
    assert "PRIVATE" not in json.dumps(result)


def test_duplicate_json_key_is_unverifiable_even_if_last_symbol_would_match():
    c, p, r = setup("primary_proposed")
    raw = c.execute(
        "SELECT snapshot_json FROM openai_evaluations WHERE candidate_id='c1'"
    ).fetchone()[0]
    raw = '{"symbol":"WRONG",' + raw[1:]
    c.execute(
        "UPDATE openai_evaluations SET snapshot_json=? WHERE candidate_id='c1'", (raw,)
    )
    c.commit()
    result = inspect(c, p, r)
    assert "ai_input_missing_or_malformed" in result["diagnostics"][0]["issues"]
    assert "tested" in result["diagnostics"][0]["missing_arms"]


def test_empty_completed_core_batch_is_explicit_not_no_op_or_fake_sample():
    c, p, r = setup("primary_proposed")
    c.execute("DELETE FROM candidates WHERE shadow_tier=0")
    c.execute("UPDATE scan_batches SET candidates_found=0")
    c.commit()
    result = inspect(c, p, r)
    assert result["declared_opportunities"] == 0
    assert result["manifest"]["all_opportunity_ids"] == []
    assert result["excluded_shadow_candidates"] == 1


def test_empty_core_batch_reversed_start_completion_is_rejected_before_enrollment():
    c, p, r = setup("primary_proposed")
    c.execute("DELETE FROM candidates WHERE shadow_tier=0")
    c.execute(
        "UPDATE scan_batches SET candidates_found=0,started_at_utc='2026-01-02T15:11:00Z'"
    )
    c.commit()
    receiver = sqlite3.connect(":memory:")
    result = persist(c, receiver, p, r, "a" * 64)
    assert result["status"] == "diagnostic_error"
    assert result["error_code"] == "completed_batch_before_capture_required"
    assert receiver.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0


def test_late_completed_batch_is_exported_as_diagnostic_not_certified_timing():
    c, p, r = setup("primary_proposed")
    c.execute("UPDATE scan_batches SET completed_at_utc='2026-01-05T15:00:00Z'")
    c.commit()
    r["as_of_utc"] = "2026-01-05T16:00:00Z"
    result = inspect(c, p, r)
    assert all(
        not d["batch_finished_before_measurement_start"] for d in result["diagnostics"]
    )
    assert not result["prospective_enrollment"]


def test_writes_and_outcome_reads_denied_by_sqlite_authorizer_export_still_succeeds():
    c, p, r = setup("primary_proposed")
    denied = []

    def authorize(action, arg1, arg2, database, trigger):
        if action in {
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_TABLE,
        }:
            denied.append((action, arg1))
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ and arg1 not in a.FIELDS:
            denied.append((action, arg1))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    c.set_authorizer(authorize)
    result = inspect(c, p, r)
    assert result["declared_opportunities"] == 3
    assert denied == []


def test_missing_final_receipt_never_falls_back_to_primary_propose():
    c, p, r = setup("final_proposed")
    c.execute("DELETE FROM decision_adjustments WHERE candidate_id='c1'")
    c.commit()
    result = inspect(c, p, r)
    assert result["diagnostics"][0]["membership_state"] == "unresolved"
    assert result["manifest"]["opportunities"][0]["eligible"] is False
    assert "tested" in result["diagnostics"][0]["missing_arms"]


def test_input_and_arm_reads_share_one_sqlite_snapshot(tmp_path, monkeypatch):
    c, p, r = setup("primary_proposed")
    expected = inspect(c, p, r)
    path = tmp_path / "source.db"
    disk = sqlite3.connect(path)
    c.backup(disk)
    c.close()
    disk.execute("PRAGMA journal_mode=WAL")
    original = a._rows
    changed = []

    def read_then_change(connection, table, where, args):
        answer = original(connection, table, where, args)
        if table == "candidates" and not changed:
            changed.append(True)
            writer = sqlite3.connect(path)
            writer.execute(
                "UPDATE openai_evaluations SET decision='reject' WHERE candidate_id='c1'"
            )
            writer.commit()
            writer.close()
        return answer

    monkeypatch.setattr(a, "_rows", read_then_change)
    assert inspect(disk, p, r) == expected
    assert inspect(disk, p, r)["manifest_sha256"] != expected["manifest_sha256"]
    assert not disk.in_transaction
