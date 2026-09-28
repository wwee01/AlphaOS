"""Offline persistence/chronology/CAS tests; never instantiate runtime services."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, date, time, timedelta, timezone
import sqlite3
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest

from alphaos.research import hold10_receipts as h
from alphaos.stats.frozen_policy_inference import canonical_sha256 as sha
from alphaos.util.market_calendar import nth_trading_day_after


def fixture():
    p = {
        "version": h.VERSION,
        "protocol_id": "hold10-draft:test",
        "status": "draft",
        "predecessor_id": h.PREDECESSOR,
        "horizon_sessions": 10,
        "direction": "greater",
        "effect_target": 0.05,
        "statistic": "paired_mean_candidate_R",
        "policies": {
            "tested": {"policy": "AI-v1"},
            "reference": {"policy": "threshold-hold10"},
        },
        "risk_contract": {
            "policy_version": "same-budget-v1",
            "currency": "USD",
            "risk_budget": 100.0,
            "R_basis": "each_arm_original_stop_distance",
        },
        "source_revision": "frozen-code",
        "measurement_method": "saved_daily_complete_horizon_bracket_v1",
        "population": "final_proposed",
    }
    inputs = {
        "symbol": "AAA",
        "signal": "available-before-decision",
        "input_version": 1,
    }
    arms = {}
    for arm in sorted(h.ARMS):
        stop = 90.0 if arm == "tested" else 80.0
        arms[arm] = {
            "symbol": "AAA",
            "decision": "propose",
            "direction": "long",
            "entry": 100.0,
            "stop": stop,
            "target": 120.0,
            "original_risk_per_share": 100.0 - stop,
            "input_sha256": sha(inputs),
            "horizon_sessions": 10,
            "policy_sha256": sha(p["policies"][arm]),
            "risk_contract_sha256": sha(p["risk_contract"]),
        }
    m = {
        "cohort_id": "batch-1",
        "protocol_sha256": sha(p),
        "all_opportunity_ids": ["candidate-1"],
        "source": {
            "kind": "existing_current_inputs",
            "sha256": "a" * 64,
            "scope": "all declared input opportunities",
        },
        "opportunities": [
            {
                "opportunity_id": "candidate-1",
                "input": inputs,
                "input_sha256": sha(inputs),
                "decision_at_utc": "2026-01-02T15:00:00Z",
                "input_available_at_utc": "2026-01-02T14:59:59Z",
                "eligible": True,
                "eligibility_reason": "final proposed",
                "arms": arms,
            }
        ],
    }
    bars = []
    for i in range(1, 11):
        d = nth_trading_day_after(date(2026, 1, 2), i)
        end = datetime.combine(d, time(16), ZoneInfo("America/New_York")).astimezone(
            timezone.utc
        )
        bars.append(
            {
                "bar_id": f"bar{i}",
                "symbol": "AAA",
                "market_date": d.isoformat(),
                "open": 100.0,
                "high": 121.0 if i == 6 else 105.0,
                "low": 95.0,
                "close": 101.0,
                "source_feed": "test",
                "created_at_utc": (end + timedelta(seconds=1)).isoformat(),
            }
        )
    return p, m, bars


def setup(tmp_path, monkeypatch):
    path = tmp_path / "copy.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE preserved (id INTEGER PRIMARY KEY, frozen TEXT)")
    c.execute("INSERT INTO preserved VALUES(1,'frozen old result')")
    c.commit()
    h.initialize_dry_run_receipts(c, purpose="scratch_or_manual_dry_run")
    store = h.Hold10Receipts(c, dry_run=True)
    clock = ["2026-01-01T00:00:00Z"]
    monkeypatch.setattr(h, "_now", lambda: clock[0])
    p, m, bars = fixture()
    store.register_protocol(p, expected_sha256=sha(p))
    clock[0] = "2026-01-02T15:00:01Z"
    return path, c, store, clock, p, m, bars


def enroll(store, p, m):
    return store.enroll_cohort(
        p["protocol_id"],
        m,
        expected_protocol_sha256=sha(p),
        expected_manifest_sha256=sha(m),
    )


def measure(store, member, bars, previous=None):
    return store.record_measurement(
        member["member_id"],
        bars,
        expected_member_receipt_sha256=member["receipt_sha256"],
        expected_bars_sha256=sha(bars),
        expected_previous_receipt_sha256=previous,
    )


def test_disabled_by_default_has_no_schema_or_writes(tmp_path):
    c = sqlite3.connect(tmp_path / "untouched.db")
    p, m, bars = fixture()
    store = h.Hold10Receipts(c)
    assert store.register_protocol(p, expected_sha256=sha(p))["status"] == "disabled"
    assert enroll(store, p, m)["status"] == "disabled"
    assert (
        store.record_measurement(
            "x", bars, expected_member_receipt_sha256="x", expected_bars_sha256="x"
        )["status"]
        == "disabled"
    )
    assert c.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0


def test_restart_preserves_original_rows_protocol_manifest_and_both_arms(
    tmp_path, monkeypatch
):
    path, c, store, clock, p, m, _ = setup(tmp_path, monkeypatch)
    view = enroll(store, p, m)
    assert view["member_count"] == view["missing_measurements"] == 2
    assert view["prospective_enrollment"] is view["formal_allowed"] is False
    assert {x["payload"]["original_arm_risk_per_share"] for x in view["members"]} == {
        10.0,
        20.0,
    }
    assert len({x["payload"]["input_sha256"] for x in view["members"]}) == 1
    assert len({x["payload"]["common_risk_sha256"] for x in view["members"]}) == 1
    c.close()
    c = sqlite3.connect(path)
    h.initialize_dry_run_receipts(c, purpose="scratch_or_manual_dry_run")
    store = h.Hold10Receipts(c, dry_run=True)
    clock[0] = "2026-01-30T00:00:00Z"
    assert enroll(store, p, m) == view
    assert c.execute("SELECT * FROM preserved").fetchall() == [(1, "frozen old result")]


def test_missing_arm_and_missing_measurement_are_retained(tmp_path, monkeypatch):
    _, _, store, _, p, m, _ = setup(tmp_path, monkeypatch)
    del m["opportunities"][0]["arms"]["reference"]
    view = enroll(store, p, m)
    assert view["member_count"] == view["missing_measurements"] == 2
    missing = next(x for x in view["members"] if x["arm"] == "reference")
    assert missing["payload"]["plan_status"] == "missing"
    receipt = measure(store, missing, [])
    assert receipt["payload"]["measurement"]["status"] == "blocked"
    assert (
        store.cohort_view(m["cohort_id"], expected_manifest_sha256=sha(m))[
            "missing_measurements"
        ]
        == 2
    )


@pytest.mark.parametrize(
    "kind", ["archived_copy", "synthetic", "late_protocol", "late_enrollment"]
)
def test_copied_legacy_or_late_receipts_never_become_prospective(
    tmp_path, monkeypatch, kind
):
    _, _, store, clock, p, m, _ = setup(tmp_path, monkeypatch)
    if kind in {"archived_copy", "synthetic"}:
        m["source"]["kind"] = kind
    elif kind == "late_protocol":
        p["protocol_id"] += "late"
        store.register_protocol(p, expected_sha256=sha(p))
        m["protocol_sha256"] = sha(p)
    else:
        clock[0] = "2026-01-20T00:00:00Z"
    view = enroll(store, p, m)
    assert all(
        x["payload"]["timing_classification"] == "retrospective_or_synthetic_dry_run"
        for x in view["members"]
    )
    assert all(not x["payload"]["prospective_enrollment"] for x in view["members"])


def test_recording_time_is_internal_and_lock_delay_cannot_fake_timeliness(
    tmp_path, monkeypatch
):
    _, _, store, _, p, m, _ = setup(tmp_path, monkeypatch)
    sequence = iter(["2026-01-05T14:29:59Z", "2026-01-05T14:30:01Z"])
    monkeypatch.setattr(h, "_now", lambda: next(sequence))
    view = enroll(store, p, m)
    assert view["cohort"]["recorded_at_utc"] == "2026-01-05T14:30:01Z"
    assert all(
        x["payload"]["timing_classification"] == "retrospective_or_synthetic_dry_run"
        for x in view["members"]
    )


@pytest.mark.parametrize(
    "change",
    [
        "missing_id",
        "duplicate_id",
        "future_input",
        "future_decision",
        "backdate",
        "input_hash",
        "policy",
        "risk",
        "horizon",
        "denominator",
        "symbol",
        "action",
    ],
)
def test_tampered_or_incomplete_identity_refuses_atomic_enrollment(
    tmp_path, monkeypatch, change
):
    _, c, store, _, p, m, _ = setup(tmp_path, monkeypatch)
    o = m["opportunities"][0]
    plan = o["arms"]["reference"]
    if change == "missing_id":
        m["all_opportunity_ids"].append("missing")
    elif change == "duplicate_id":
        m["opportunities"].append(deepcopy(o))
    elif change == "future_input":
        o["input_available_at_utc"] = "2026-01-02T15:00:02Z"
    elif change == "future_decision":
        o["decision_at_utc"] = "2026-01-02T16:00:00Z"
    elif change == "backdate":
        m["recorded_at_utc"] = "2025-01-01T00:00:00Z"
    elif change == "input_hash":
        o["input"]["x"] = "changed"
    elif change == "policy":
        plan["policy_sha256"] = "a" * 64
    elif change == "risk":
        plan["risk_contract_sha256"] = "b" * 64
    elif change == "horizon":
        plan["horizon_sessions"] = 5
    elif change == "denominator":
        plan["original_risk_per_share"] = 19.0
    elif change == "symbol":
        plan["symbol"] = "FOREIGN"
    else:
        plan["decision"] = "unknown"
    with pytest.raises(ValueError):
        enroll(store, p, m)
    assert c.execute(f"SELECT count(*) FROM {h.TABLES[1]}").fetchone()[0] == 0
    assert c.execute(f"SELECT count(*) FROM {h.TABLES[2]}").fetchone()[0] == 0


def test_crash_between_member_inserts_rolls_back_whole_cohort(tmp_path, monkeypatch):
    _, c, store, _, p, m, _ = setup(tmp_path, monkeypatch)
    original = h._insert
    calls = []

    def crashing(connection, table, keys, payload, now):
        answer = original(connection, table, keys, payload, now)
        calls.append(table)
        if table == h.TABLES[2]:
            raise RuntimeError("simulated crash")
        return answer

    monkeypatch.setattr(h, "_insert", crashing)
    with pytest.raises(RuntimeError):
        enroll(store, p, m)
    assert calls == [h.TABLES[1], h.TABLES[2]]
    assert c.execute(f"SELECT count(*) FROM {h.TABLES[1]}").fetchone()[0] == 0
    assert c.execute(f"SELECT count(*) FROM {h.TABLES[2]}").fetchone()[0] == 0
    monkeypatch.setattr(h, "_insert", original)
    assert enroll(store, p, m)["member_count"] == 2


def test_concurrent_same_cohort_is_one_immutable_pair(tmp_path, monkeypatch):
    path, c, _, _, p, m, _ = setup(tmp_path, monkeypatch)
    c.close()
    barrier = Barrier(2)

    def write():
        connection = sqlite3.connect(path, timeout=10)
        store = h.Hold10Receipts(connection, dry_run=True)
        barrier.wait()
        result = enroll(store, p, m)
        connection.close()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda _: write(), range(2)))
    assert answers[0] == answers[1]
    c = sqlite3.connect(path)
    assert c.execute(f"SELECT count(*) FROM {h.TABLES[2]}").fetchone()[0] == 2


def test_pending_then_day6_barrier_preserves_revisions_and_risk_denominators(
    tmp_path, monkeypatch
):
    _, c, store, clock, p, m, bars = setup(tmp_path, monkeypatch)
    view = enroll(store, p, m)
    first = measure(store, view["members"][0], [])
    assert first["payload"]["measurement"]["status"] == "blocked"
    clock[0] = "2026-01-20T00:00:00Z"
    with pytest.raises(ValueError, match="revision_conflict"):
        measure(store, view["members"][0], bars)
    second = measure(store, view["members"][0], bars, first["receipt_sha256"])
    assert second["revision"] == 2
    assert second["payload"]["measurement"]["exit_session"] == bars[5]["market_date"]
    assert (
        second["payload"]["measurement"]["value"] == 1.0
    )  # reference risk20, reward20
    assert (
        measure(store, view["members"][0], bars) == second
    )  # exact retry is idempotent despite stale CAS token
    changed = deepcopy(bars)
    changed[0]["low"] = 79.0
    with pytest.raises(ValueError, match="completed_measurement_is_immutable"):
        measure(store, view["members"][0], changed, second["receipt_sha256"])
    assert c.execute(f"SELECT count(*) FROM {h.TABLES[3]}").fetchone()[0] == 2


def test_foreign_bars_or_source_pin_tampering_cannot_book_a_measurement(
    tmp_path, monkeypatch
):
    _, c, store, clock, p, m, bars = setup(tmp_path, monkeypatch)
    member = enroll(store, p, m)["members"][0]
    clock[0] = "2026-01-20T00:00:00Z"
    wrong = deepcopy(bars)
    wrong[0]["symbol"] = "BBB"
    with pytest.raises(ValueError, match="foreign_or_malformed"):
        measure(store, member, wrong)
    with pytest.raises(ValueError, match="bars_pin_mismatch"):
        store.record_measurement(
            member["member_id"],
            bars,
            expected_member_receipt_sha256=member["receipt_sha256"],
            expected_bars_sha256="a" * 64,
        )
    assert c.execute(f"SELECT count(*) FROM {h.TABLES[3]}").fetchone()[0] == 0


def test_database_update_delete_and_direct_hash_tamper_fail_closed(
    tmp_path, monkeypatch
):
    _, c, store, _, p, m, _ = setup(tmp_path, monkeypatch)
    enroll(store, p, m)
    for table in h.TABLES[:3]:
        for sql in (
            f"DELETE FROM {table}",
            f"UPDATE {table} SET recorded_at_utc='2000-01-01T00:00:00Z'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                c.execute(sql)
            c.rollback()
    c.execute(f"DROP TRIGGER {h.TABLES[1]}_immutable_update")
    c.execute(f"UPDATE {h.TABLES[1]} SET recorded_at_utc='2000-01-01T00:00:00Z'")
    c.commit()
    with pytest.raises(ValueError, match="saved_receipt_identity_mismatch"):
        store.cohort_view(m["cohort_id"], expected_manifest_sha256=sha(m))


def test_original_registration_namespace_and_activation_are_refused(
    tmp_path, monkeypatch
):
    _, c, store, _, p, _, _ = setup(tmp_path, monkeypatch)
    for key, value in (
        ("protocol_id", h.PREDECESSOR),
        ("activated", True),
        ("effect_target", 0.0),
        ("horizon_sessions", 5),
    ):
        wrong = {**p, key: value}
        with pytest.raises(ValueError):
            store.register_protocol(wrong, expected_sha256=sha(wrong))
    assert c.execute(f"SELECT count(*) FROM {h.TABLES[0]}").fetchone()[0] == 1


def test_empty_manifest_and_initializer_reopen_are_idempotent(tmp_path, monkeypatch):
    path, c, store, _, p, m, _ = setup(tmp_path, monkeypatch)
    m["all_opportunity_ids"] = []
    m["opportunities"] = []
    view = enroll(store, p, m)
    assert view["member_count"] == view["missing_measurements"] == 0
    schema = c.execute(
        "SELECT type,name,sql FROM sqlite_master ORDER BY name"
    ).fetchall()
    c.close()
    c = sqlite3.connect(path)
    h.initialize_dry_run_receipts(c, purpose="scratch_or_manual_dry_run")
    assert (
        c.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
        == schema
    )
    assert (
        h.Hold10Receipts(c).cohort_view(m["cohort_id"], expected_manifest_sha256=sha(m))
        == view
    )
    assert not c.in_transaction
    with pytest.raises(ValueError, match="explicit_dry_run"):
        h.initialize_dry_run_receipts(c, purpose="activate")


@pytest.mark.parametrize(
    "part", ["protocol", "manifest", "opportunity", "declared_id", "symbol", "bars"]
)
def test_malformed_input_shapes_fail_closed(tmp_path, monkeypatch, part):
    _, c, store, _, p, m, _ = setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        if part == "protocol":
            store.register_protocol([], expected_sha256=sha([]))
        elif part == "manifest":
            store.enroll_cohort(
                p["protocol_id"],
                [],
                expected_protocol_sha256=sha(p),
                expected_manifest_sha256=sha([]),
            )
        elif part == "bars":
            member = enroll(store, p, m)["members"][0]
            measure(store, member, {})
        else:
            if part == "opportunity":
                m["opportunities"] = [1]
            elif part == "declared_id":
                m["all_opportunity_ids"] = [[]]
            else:
                m["opportunities"][0]["input"].pop("symbol")
            enroll(store, p, m)
    assert not c.in_transaction


def test_concurrent_distinct_measurements_require_revision_cas(tmp_path, monkeypatch):
    path, c, store, clock, p, m, bars = setup(tmp_path, monkeypatch)
    member = enroll(store, p, m)["members"][0]
    clock[0] = "2026-01-20T00:00:00Z"
    c.close()
    barrier = Barrier(2)

    def write(source):
        connection = sqlite3.connect(path, timeout=10)
        worker = h.Hold10Receipts(connection, dry_run=True)
        barrier.wait()
        try:
            return measure(worker, member, source)
        except ValueError as exc:
            return str(exc)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(write, [[], bars[:5]]))
    assert sum(isinstance(answer, dict) for answer in answers) == 1
    assert answers.count("measurement_revision_conflict") == 1
    c = sqlite3.connect(path)
    assert c.execute(f"SELECT count(*) FROM {h.TABLES[3]}").fetchone()[0] == 1
    view = h.Hold10Receipts(c).cohort_view(
        m["cohort_id"], expected_manifest_sha256=sha(m)
    )
    assert view["missing_measurements"] == 2


def test_view_is_one_snapshot_while_second_connection_appends(tmp_path, monkeypatch):
    path, c, store, clock, p, m, bars = setup(tmp_path, monkeypatch)
    c.execute("PRAGMA journal_mode=WAL")
    view = enroll(store, p, m)
    clock[0] = "2026-01-20T00:00:00Z"
    original = h._read
    observed = []

    def read_then_append(connection, table, key, value):
        result = original(connection, table, key, value)
        if table == h.TABLES[1] and not observed:
            observed.append(True)
            second = sqlite3.connect(path)
            other = h.Hold10Receipts(second, dry_run=True)
            for member in view["members"]:
                measure(other, member, bars)
            second.close()
        return result

    monkeypatch.setattr(h, "_read", read_then_append)
    during = store.cohort_view(m["cohort_id"], expected_manifest_sha256=sha(m))
    assert (
        during["missing_measurements"] == 2
    )  # complete prior snapshot, not a mixed pair
    after = store.cohort_view(m["cohort_id"], expected_manifest_sha256=sha(m))
    assert after["missing_measurements"] == 0
    assert {
        o["payload"]["measurement"]["value"] for o in after["latest_measurements"]
    } == {1.0, 2.0}


def test_missing_old_revision_is_detected_even_when_latest_hash_is_unchanged(
    tmp_path, monkeypatch
):
    _, c, store, clock, p, m, bars = setup(tmp_path, monkeypatch)
    member = enroll(store, p, m)["members"][0]
    first = measure(store, member, [])
    clock[0] = "2026-01-20T00:00:00Z"
    measure(store, member, bars, first["receipt_sha256"])
    c.execute(f"DROP TRIGGER {h.TABLES[3]}_immutable_delete")
    c.execute(f"DELETE FROM {h.TABLES[3]} WHERE revision=1")
    c.commit()
    with pytest.raises(ValueError, match="measurement_history_identity_or_chronology"):
        store.cohort_view(m["cohort_id"], expected_manifest_sha256=sha(m))
    assert not c.in_transaction


def test_caller_mutation_cannot_change_frozen_preimages_during_enrollment(
    tmp_path, monkeypatch
):
    _, _, store, _, p, m, _ = setup(tmp_path, monkeypatch)
    expected_manifest = sha(m)
    original = deepcopy(m)

    def mutate_caller():
        m["opportunities"][0]["input"]["signal"] = "changed after pin"
        return "2026-01-02T15:00:01Z"

    monkeypatch.setattr(h, "_now", mutate_caller)
    view = store.enroll_cohort(
        p["protocol_id"],
        m,
        expected_protocol_sha256=sha(p),
        expected_manifest_sha256=expected_manifest,
    )
    assert view["cohort"]["payload"] == original
    assert all(
        member["payload"]["input"] == original["opportunities"][0]["input"]
        for member in view["members"]
    )
    assert sha(m) != expected_manifest
