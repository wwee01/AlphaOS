"""Synthetic SDK transport -> actual producer -> durable capture -> bracket measurement.

All registration-clock overrides and market/model responses are test fixtures;
none are natural prospective observations or research eligibility evidence.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import sqlite3
import json

import pytest

from alphaos.research import hold10_expected_runtime as er
from alphaos.research.runtime_identity import observe_runtime

from alphaos.research import hold10_collection_audit as a
from alphaos.research import hold10_designated_registry as d
from alphaos.research import hold10_policy_binding as p
from alphaos.research import hold10_prospective as v1
from alphaos.research import hold10_prospective_compact as compact
from alphaos.research import hold10_receipts as h
from alphaos.util.market_calendar import nth_trading_day_after
from runtime_identity_fixture import synthetic_transport_scan
from test_hold10_policy_binding import fixture


class MemorySink:
    def __init__(self):
        self.payloads = []

    def append_runtime_identity(self, payload):
        self.payloads.append(payload)
        return v1.sha(payload)


@pytest.mark.parametrize(
    "runtime_case",
    ["legacy", "matched", "settings", "callable", "missing", "environment"],
)
def test_actual_hook_to_durable_collection_and_measurement(
    tmp_path, monkeypatch, runtime_case
):
    root, _, q, _, clock = fixture(tmp_path, monkeypatch)
    code = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(code)
    warm, _, _ = synthetic_transport_scan(
        monkeypatch, MemorySink(), prompt_version="v4"
    )
    evaluation = warm.journal.query("SELECT * FROM openai_evaluations")[0]
    classifier = warm.journal.query("SELECT * FROM candidate_labels")[0]
    reference = warm.journal.query(
        "SELECT * FROM shadow_baseline_decisions WHERE rule_version='threshold_v1_hold10'"
    )[0]
    q["protocol"]["policies"]["tested"]["identity"] = {
        k: evaluation[k] for k in q["protocol"]["policies"]["tested"]["identity"]
    }
    q["protocol"]["policies"]["reference"]["setup_card_id"] = reference["setup_card_id"]
    q["combined_policy"]["classifier_identity"] = {
        k: classifier[k] for k in q["combined_policy"]["classifier_identity"]
    }
    q["combined_policy"]["configuration"] = {
        k: getattr(warm.settings, k) for k in q["combined_policy"]["configuration"]
    }
    warm_identity = observe_runtime(warm)
    reviewed_profile = er.runtime_profile(warm_identity)
    reviewed_environment = er.interpreter_identity()
    warm.close()
    now = datetime.now(timezone.utc)
    start = now.astimezone(ZoneInfo("America/New_York")).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    q["start_at_utc"] = start.isoformat()
    q["end_at_utc"] = (start + timedelta(days=28)).isoformat()
    q["close_at_utc"] = (start + timedelta(days=50)).isoformat()
    clock[0] = start - timedelta(days=1)
    binding = p.create_policy_binding(
        q,
        expected_contract_sha256=v1.sha(q),
        code_root=code,
        configuration=q["combined_policy"]["configuration"],
    )
    expected_manifest = er.create_manifest(
        q,
        binding,
        reviewed_profile=reviewed_profile,
        expected_profile_sha256=v1.sha(reviewed_profile),
        reviewed_environment=reviewed_environment,
        expected_environment_sha256=v1.sha(reviewed_environment),
        expected_contract_sha256=v1.sha(q),
        expected_binding_sha256=v1.sha(binding),
    )
    api = d.DesignatedHold10(root, dry_run=True)
    designation = api.register(
        q,
        binding,
        expected_contract_sha256=v1.sha(q),
        expected_binding_sha256=v1.sha(binding),
    )
    pin = designation["designation_sha256"]
    identity = a.CollectionAuditJournal.create(
        root / "path-audit.db", designation_sha256=pin, enabled=True
    )
    journal = a.CollectionAuditJournal(
        root / "path-audit.db",
        expected_identity_sha256=identity["identity_sha256"],
        enabled=True,
    )
    sink = journal
    frozen = None
    if runtime_case != "legacy":
        frozen = er.freeze_manifest(
            api,
            journal,
            expected_manifest,
            expected_manifest_sha256=v1.sha(expected_manifest),
            expected_designation_sha256=pin,
        )
        sink = er.ExpectedRuntimeSink(
            journal, expected_manifest_receipt_sha256=frozen["receipt_sha256"]
        )
    if runtime_case == "missing":
        sink = MemorySink()

    def alter(o):
        if runtime_case == "settings":
            from dataclasses import replace

            o.openai.settings = replace(
                o.openai.settings,
                label_max_output_tokens=o.openai.settings.label_max_output_tokens + 1,
            )
        if runtime_case == "callable":
            from types import MethodType

            original = o.openai._apply_atr_stop

            def changed(self, *args, **kwargs):
                return original(*args, **kwargs)

            o.openai._apply_atr_stop = MethodType(changed, o.openai)

    if runtime_case == "environment":
        monkeypatch.setattr(
            er,
            "interpreter_identity",
            lambda: dict(reviewed_environment, version="0.0.0"),
        )
    monkeypatch.setattr(v1, "_now", lambda: datetime.now(timezone.utc))
    producer, summary, calls = synthetic_transport_scan(
        monkeypatch, sink, prompt_version="v4", before_scan=alter
    )
    saved = root / "producer-source.db"
    with sqlite3.connect(saved) as target:
        producer.journal.conn.backup(target)
    source = v1.c.SavedLedger(saved, v1.c._file_hash(saved), root=root)
    before = saved.read_bytes()
    collect = (
        a.collect_designated
        if runtime_case == "legacy"
        else er.collect_designated_expected_runtime
    )
    extra = (
        {}
        if frozen is None
        else {"expected_manifest_receipt_sha256": frozen["receipt_sha256"]}
    )
    result = collect(
        api,
        journal,
        source,
        attempt_id="actual-hook",
        expected_designation_sha256=pin,
        expected_previous_capture_sha256=None,
        **extra,
    )
    census = [
        e for e in journal.read()["events"] if e["kind"] == "collection_prepared"
    ][0]["payload"]
    if runtime_case != "missing":
        assert census["runtime_audit"]["complete_count"] == summary.candidates > 0, (
            census
        )
    if runtime_case not in {"legacy", "matched"}:
        assert result["status"] == "refused_expected_runtime_agreement", result
        expected = census["expected_runtime_audit"]
        assert expected["eligible_count"] == summary.candidates
        assert expected["complete_count"] == 0
        assert all(row["gaps"] for row in expected["candidates"])
        assert not api.read(expected_designation_sha256=pin)["state"]["captures"]
        reopened = a.CollectionAuditJournal(
            journal.path, expected_identity_sha256=journal.expected, enabled=True
        )
        assert (
            collect(
                api,
                reopened,
                source,
                attempt_id="actual-hook",
                expected_designation_sha256=pin,
                expected_previous_capture_sha256=None,
                **extra,
            )["status"]
            == "already_recorded"
        )
        assert source.expected_sha256 == v1.c._file_hash(saved)
        source.close()
        producer.close()
        return
    if runtime_case == "matched":
        assert census["expected_runtime_audit"]["complete_count"] == summary.candidates
        assert census["expected_runtime_audit"]["full_runtime_attested"] is False
        restarted = observe_runtime(producer)
        assert (
            restarted["callables"]["evaluate"]["bound_instance_token"]
            != warm_identity["callables"]["evaluate"]["bound_instance_token"]
        )
        assert er.runtime_profile(restarted) == reviewed_profile
    else:
        assert "expected_runtime_audit" not in census
    assert result["status"] == "captured", census["policy_audit"]
    journal = a.CollectionAuditJournal(
        journal.path, expected_identity_sha256=identity["identity_sha256"], enabled=True
    )
    assert len(journal.read()["events"]) > 2
    api = d.DesignatedHold10(root, dry_run=True)
    reread = api.read(expected_designation_sha256=pin)
    state = reread["state"]
    conn, _ = api._db(root / d.DIRECTORY)
    try:
        capture = compact.CompactProspectiveHold10(conn).expand_capture(
            q["registration_id"],
            1,
            expected_capture_sha256=state["latest_capture_sha256"],
        )
    finally:
        conn.close()
    exports = [
        x["export"] for x in capture["payload"]["dispositions"] if x.get("export")
    ]
    assert exports and calls
    receiver = sqlite3.connect(":memory:")
    h.initialize_dry_run_receipts(receiver, purpose="scratch_or_manual_dry_run")
    measurements = h.Hold10Receipts(receiver, dry_run=True)
    measurements.register_protocol(q["protocol"], expected_sha256=v1.sha(q["protocol"]))
    selected = []
    for export in exports:
        cohort = measurements.enroll_cohort(
            q["protocol"]["protocol_id"],
            export["manifest"],
            expected_protocol_sha256=v1.sha(q["protocol"]),
            expected_manifest_sha256=export["manifest_sha256"],
        )
        selected.extend(cohort["members"])
    assert selected, exports
    member = next(
        m
        for m in selected
        if m["payload"]["eligible"]
        and m["arm"] == "tested"
        and any(
            other["opportunity_id"] == m["opportunity_id"]
            and other["arm"] == "reference"
            and other["payload"]["plan_status"] != "missing"
            and other["payload"]["plan_status"] != "unavailable"
            for other in selected
        )
    )
    first = member["payload"]["input"]["symbol"]
    outcome_date = (
        datetime.fromisoformat(member["payload"]["decision_at_utc"])
        .astimezone(ZoneInfo("America/New_York"))
        .date()
    )
    plan = member["payload"]["plan"]
    bars = []
    for day in range(1, 11):
        date = nth_trading_day_after(outcome_date, day)
        bars.append(
            {
                "bar_id": f"synthetic-{day}",
                "symbol": first,
                "market_date": date.isoformat(),
                "open": plan["entry"],
                "high": plan["entry"],
                "low": plan["entry"],
                "close": plan["entry"],
                "source_feed": "synthetic-test",
                "created_at_utc": date.isoformat() + "T23:00:00Z",
            }
        )
    monkeypatch.setattr(h, "_now", lambda: (start + timedelta(days=31)).isoformat())
    missing = measurements.record_measurement(
        member["member_id"],
        bars[:2],
        expected_member_receipt_sha256=member["receipt_sha256"],
        expected_bars_sha256=v1.sha(bars[:2]),
    )
    assert missing["payload"]["measurement"]["status"] != "complete"
    complete = measurements.record_measurement(
        member["member_id"],
        bars,
        expected_member_receipt_sha256=member["receipt_sha256"],
        expected_bars_sha256=v1.sha(bars),
        expected_previous_receipt_sha256=missing["receipt_sha256"],
    )
    assert complete["payload"]["measurement"]["status"] == "complete", json.dumps(
        complete["payload"]["measurement"]
    )
    assert isinstance(complete["payload"]["measurement"]["value"], (int, float))
    assert (
        measurements.record_measurement(
            member["member_id"],
            bars,
            expected_member_receipt_sha256=member["receipt_sha256"],
            expected_bars_sha256=v1.sha(bars),
        )
        == complete
    )
    reference = next(
        m
        for m in selected
        if m["opportunity_id"] == member["opportunity_id"] and m["arm"] == "reference"
    )
    reference_result = measurements.record_measurement(
        reference["member_id"],
        bars,
        expected_member_receipt_sha256=reference["receipt_sha256"],
        expected_bars_sha256=v1.sha(bars),
    )
    assert reference_result["payload"]["measurement"]["status"] == "complete", reference
    assert isinstance(reference_result["payload"]["measurement"]["value"], (int, float))
    assert len(selected) == summary.candidates * 2
    assert saved.read_bytes() == before
    assert not state["formal_allowed"]
    source.close()
    receiver.close()
    producer.close()
