"""WATCH-1 changes only its recorded alternative, never the order decision."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator, ScanSummary
from alphaos.research.watch_advisory import decision_diagnostic
from conftest import make_settings
from scripts.watch_advisory_report import report
from test_decision_override import _watch_ready_candidate


def _inputs():
    evaluation = SimpleNamespace(
        eval_id="eval-1", direction="long", decision="propose", entry=100,
        stop=97, target=106, max_holding_days=7, validation_status="passed",
        data_freshness_status="usable", is_mock=False,
    )
    classification = SimpleNamespace(
        label_id="label-1", label_decision="watch", primary_label="Momentum",
        confidence=0.9, validation_status="passed", is_mock=False,
    )
    return evaluation, classification


def _diag(evaluation, classification, current="watch"):
    return decision_diagnostic(evaluation, classification, current,
                               confidence_floor=0.5, min_reward_risk=2)


def test_only_valid_long_watch_veto_changes_and_inputs_stay_identical():
    evaluation, classification = _inputs()
    before = copy.deepcopy((vars(evaluation), vars(classification)))
    diag = _diag(evaluation, classification)
    assert diag["current_decision"] == "watch"
    assert diag["challenger_decision_before_risk"] == "propose"
    assert diag["changed"] is True
    assert diag["downstream_gates"] == "not_evaluated"
    assert (vars(evaluation), vars(classification)) == before


@pytest.mark.parametrize("base", ["propose", "watch", "reject"])
@pytest.mark.parametrize("label", ["propose", "watch", "reject"])
@pytest.mark.parametrize("armed", [True, False])
def test_preserves_all_other_existing_combination_branches(base, label, armed):
    evaluation, classification = _inputs()
    evaluation.decision = base
    classification.label_decision = label
    # Call the actual production combinator (no full orchestrator/broker needed).
    orch = Orchestrator.__new__(Orchestrator)
    current = orch._combine_decision(base, label, True, armed)
    diag = _diag(evaluation, classification, current)
    expected = "propose" if base == "propose" and label == "watch" else current
    assert diag["challenger_decision_before_risk"] == expected
    assert diag["current_decision"] == current


@pytest.mark.parametrize("which, field, value", [
    ("eval", "direction", "short"),
    ("eval", "validation_status", "failed_validation"),
    ("eval", "validation_status", None),
    ("eval", "data_freshness_status", "stale"),
    ("eval", "entry", None),
    ("eval", "entry", float("nan")),
    ("eval", "stop", float("inf")),
    ("eval", "stop", 100),
    ("eval", "stop", -1),
    ("eval", "target", 99),
    ("eval", "target", 105),
    ("eval", "max_holding_days", None),
    ("eval", "max_holding_days", True),
    ("label", "label_decision", "reject"),
    ("label", "validation_status", "low_confidence"),
    ("label", "validation_status", "missing_decision"),
    ("label", "primary_label", "Other/Unclassified"),
    ("label", "primary_label", "invented"),
    ("label", "confidence", None),
    ("label", "confidence", float("nan")),
    ("label", "confidence", 0.49),
    ("label", "confidence", 1.1),
])
def test_invalid_inputs_cannot_release_a_watch(which, field, value):
    evaluation, classification = _inputs()
    setattr(evaluation if which == "eval" else classification, field, value)
    diag = _diag(evaluation, classification)
    assert diag["changed"] is False
    assert diag["challenger_decision_before_risk"] == "watch"
    assert diag["exclusion_reasons"]
    # Diagnostic output must remain strict JSON even for non-finite AI prices.
    json.dumps(diag, allow_nan=False)


def test_live_decision_stays_watch_while_alternative_is_durably_recorded():
    with_journal = JournalStore(":memory:")
    orch = Orchestrator(settings=make_settings(), journal=with_journal)
    try:
        cand, evaluation, classification = _watch_ready_candidate(orch)
        evaluation.decision = "propose"
        classification.label_decision = "watch"
        current = orch._resolve_decision(
            cand, evaluation, classification, "prospective-batch", ScanSummary(scan_id="test"),
        )
        assert current == "watch"  # run_scan_once continues without calling proposal handling
        row = with_journal.one("SELECT * FROM decision_adjustments")
        diag = json.loads(row["evidence_json"])["watch_advisory_diagnostic"]
        assert row["final_decision"] == "watch"
        assert diag["challenger_decision_before_risk"] == "propose"
        assert diag["is_mock"] is True
        assert with_journal.count_rows("trade_proposals") == 0
        assert with_journal.count_rows("paper_orders") == 0
    finally:
        orch.close()


def test_readonly_report_preserves_file_and_excludes_legacy_and_mock(tmp_path):
    db = tmp_path / "copy.db"
    journal = JournalStore(str(db))
    evaluation, classification = _inputs()
    diag = _diag(evaluation, classification)
    for name, evidence in [
        ("legacy", {}),
        ("current", {"watch_advisory_diagnostic": diag}),
        ("mock", {"watch_advisory_diagnostic": {**diag, "is_mock": True}}),
    ]:
        journal.insert("decision_adjustments", {
            "adjustment_id": name, "candidate_id": name, "symbol": "XYZ",
            "eval_decision": "propose", "label_decision": "watch",
            "final_decision": "watch", "adjustment": "downgraded",
            "evidence_json": evidence,
        })
    journal.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    result = report(str(db))
    assert result["counts"]["no_recorded_diagnostic"] == 1
    assert result["counts"]["mock_or_unknown_provenance"] == 1
    assert result["counts"]["changed_candidate_events"] == 1
    assert len(result["changed_events"]) == 1
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_report_does_not_create_missing_database(tmp_path):
    import sqlite3

    db = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        report(str(db))
    assert not db.exists()
