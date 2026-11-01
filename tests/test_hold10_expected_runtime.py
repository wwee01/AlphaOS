"""Synthetic local expectation, chronology and full-census refusal tests."""

from copy import deepcopy
from datetime import datetime

import pytest

from alphaos.orchestrator import Orchestrator
from alphaos.journal.journal_store import JournalStore
from alphaos.research import hold10_expected_runtime as er
from alphaos.research import hold10_collection_audit as a
from alphaos.research import hold10_prospective as v1
from alphaos.research import runtime_identity as ri
from conftest import make_settings
from test_hold10_policy_binding import fixture, start


def setup(tmp_path, monkeypatch):
    fx = fixture(tmp_path, monkeypatch)
    q, binding = fx[2:4]
    o = Orchestrator(settings=make_settings(), journal=JournalStore(":memory:"))
    observed = ri.observe_runtime(o)
    o.close()
    profile = er.runtime_profile(observed)
    # Explicit synthetic expected profile, not a candidate observation.
    profile["settings"]["orchestrator"]["values"].update(
        q["combined_policy"]["configuration"]
    )
    profile["effective_clients"]["evaluator"]["values"].update(
        model=q["protocol"]["policies"]["tested"]["identity"]["model"], use_mock=False
    )
    profile["effective_clients"]["classifier"]["values"].update(
        model=q["combined_policy"]["classifier_identity"]["model"], use_mock=False
    )
    environment = er.interpreter_identity()
    args = dict(
        reviewed_profile=profile,
        expected_profile_sha256=v1.sha(profile),
        reviewed_environment=environment,
        expected_environment_sha256=v1.sha(environment),
        expected_contract_sha256=v1.sha(q),
        expected_binding_sha256=v1.sha(binding),
    )
    manifest = er.create_manifest(q, binding, **args)
    api, designation = start(fx)
    path = fx[0] / "expected-audit.db"
    identity = a.CollectionAuditJournal.create(
        path, designation_sha256=designation["designation_sha256"], enabled=True
    )
    journal = a.CollectionAuditJournal(
        path, expected_identity_sha256=identity["identity_sha256"], enabled=True
    )
    return fx, args, manifest, api, designation, journal, observed


def freeze(data, manifest=None):
    _, _, expected, api, designation, journal, _ = data
    expected = expected if manifest is None else manifest
    return er.freeze_manifest(
        api,
        journal,
        expected,
        expected_manifest_sha256=v1.sha(expected),
        expected_designation_sha256=designation["designation_sha256"],
    )


def test_profile_alias_partition_survives_restart_but_not_alias_changes(
    tmp_path, monkeypatch
):
    observed = setup(tmp_path, monkeypatch)[-1]
    original = er.runtime_profile(observed)
    changed = deepcopy(observed)
    for fn in changed["callables"].values():
        if fn["bound_instance_token"] is not None:
            fn["bound_instance_token"] = "new-" + fn["bound_instance_token"]
    assert er.runtime_profile(changed) == original
    bound = [
        fn
        for fn in changed["callables"].values()
        if fn["bound_instance_token"] is not None
    ]
    old_token = bound[0]["bound_instance_token"]
    other = next(fn for fn in bound if fn["bound_instance_token"] != old_token)
    other["bound_instance_token"] = old_token
    assert er.runtime_profile(changed) != original


def test_review_profile_pin_scope_and_policy_overlap_required(tmp_path, monkeypatch):
    fx, args, *_ = setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        er.create_manifest(fx[2], fx[3], **dict(args, expected_profile_sha256="0" * 64))
    broken = deepcopy(args["reviewed_profile"])
    broken["settings"]["orchestrator"]["values"]["min_reward_risk"] += 1
    with pytest.raises(ValueError, match="policy_configuration_mismatch"):
        er.create_manifest(
            fx[2],
            fx[3],
            **dict(
                args, reviewed_profile=broken, expected_profile_sha256=v1.sha(broken)
            ),
        )
    missing = deepcopy(args["reviewed_profile"])
    del missing["callables"]["evaluate"]
    with pytest.raises(ValueError):
        er.create_manifest(
            fx[2],
            fx[3],
            **dict(
                args, reviewed_profile=missing, expected_profile_sha256=v1.sha(missing)
            ),
        )


def test_manifest_freeze_reopen_single_binding_and_no_late_enrollment(
    tmp_path, monkeypatch
):
    data = setup(tmp_path, monkeypatch)
    fx, _, manifest, _, _, journal, _ = data
    row = freeze(data)
    assert freeze(data) == row
    reopened = a.CollectionAuditJournal(
        journal.path, expected_identity_sha256=journal.expected, enabled=True
    )
    assert reopened.read()["events"][0] == row
    bad = deepcopy(manifest)
    bad["selected_source_revision"] = "0" * 40
    with pytest.raises(ValueError):
        freeze(data, bad)
    fx[-1][0] = datetime.fromisoformat(fx[2]["start_at_utc"])
    # Existing identical freeze is readable; another empty journal cannot enroll late.
    path = fx[0] / "late.db"
    ident = a.CollectionAuditJournal.create(
        path, designation_sha256=data[4]["designation_sha256"], enabled=True
    )
    late = a.CollectionAuditJournal(
        path, expected_identity_sha256=ident["identity_sha256"], enabled=True
    )
    with pytest.raises(ValueError, match="future_manifest_freeze"):
        er.freeze_manifest(
            data[3],
            late,
            manifest,
            expected_manifest_sha256=v1.sha(manifest),
            expected_designation_sha256=data[4]["designation_sha256"],
        )
    assert late.read()["events"] == []


def test_sink_wrong_receipt_and_pre_freeze_producer_refused(tmp_path, monkeypatch):
    data = setup(tmp_path, monkeypatch)
    row = freeze(data)
    payload = {"recorded_at_utc": row["recorded_at_utc"]}
    with pytest.raises(ValueError, match="pin_mismatch"):
        er.ExpectedRuntimeSink(
            data[5], expected_manifest_receipt_sha256="0" * 64
        ).append_runtime_identity(payload)
    with pytest.raises(ValueError, match="must_follow"):
        er.ExpectedRuntimeSink(
            data[5], expected_manifest_receipt_sha256=row["receipt_sha256"]
        ).append_runtime_identity(payload)
    assert len(data[5].read()["events"]) == 1


@pytest.mark.parametrize("frozen", [False, True])
def test_missing_runtime_or_manifest_retains_complete_refusal_census(
    tmp_path, monkeypatch, frozen
):
    data = setup(tmp_path, monkeypatch)
    fx, _, _, api, designation, journal, _ = data
    row = freeze(data) if frozen else {"receipt_sha256": "0" * 64}
    fx[-1][0] = datetime.fromisoformat("2026-01-02T15:20:00+00:00")
    source = v1.c.SavedLedger(fx[1], v1.c._file_hash(fx[1]), root=fx[0])
    kwargs = dict(
        attempt_id="expected",
        expected_designation_sha256=designation["designation_sha256"],
        expected_previous_capture_sha256=None,
        expected_manifest_receipt_sha256=row["receipt_sha256"],
    )
    try:
        result = er.collect_designated_expected_runtime(api, journal, source, **kwargs)
        assert result["status"] == "refused_expected_runtime_agreement", result
        prepared = next(
            e["payload"]
            for e in journal.read()["events"]
            if e["kind"] == "collection_prepared"
        )
        assert prepared["expected_runtime_audit"]["eligible_count"] == 2
        assert prepared["expected_runtime_audit"]["complete_count"] == 0
        assert len(prepared["policy_audit"]["candidates"]) == 2
        assert not api.read(
            expected_designation_sha256=designation["designation_sha256"]
        )["state"]["captures"]
        assert (
            er.collect_designated_expected_runtime(api, journal, source, **kwargs)[
                "status"
            ]
            == "already_recorded"
        )
        changed = dict(kwargs, expected_manifest_receipt_sha256="1" * 64)
        assert (
            er.collect_designated_expected_runtime(api, journal, source, **changed)[
                "status"
            ]
            == "admission_refused"
        )
    finally:
        source.close()
