"""EXPLORE-1 needs its versioned evaluator; legacy calls must spend no look."""

from datetime import datetime, timezone
import json

import pytest

from alphaos.hypotheses import resolver
from alphaos.stats import preregistration as prereg
from alphaos.stats.research_validity import (
    ResearchContractQuarantinedError,
    registration_validity,
)


VERSION = "explore1_immutable_contract_v1"
REASON = "VERSIONED_EVALUATOR_REQUIRED"


def register(journal, hypothesis="H-EXP1-A-EDGE", params=None):
    return prereg.register_hypothesis(
        journal, hypothesis, "test return", 1_000_000, 1_000_000.0,
        "2027-01-16T01:00:00Z",
        params={"registration_version": VERSION} if params is None else params,
    )


def link(journal, pid, hypothesis="H-EXP1-A-EDGE"):
    journal.insert("hypothesis_proposals", {
        "hypothesis_id": hypothesis, "risk_class": "A", "claim": "test claim",
        "prereg_id": pid, "status": "testing", "metric_fn_name": "test_metric",
        "analysis_not_before": "2020-01-01", "created_at_utc": "2020-01-01T00:00:00Z",
        "created_at_sgt": "2020-01-01T08:00:00+08:00",
    })


def dump(journal):
    return list(journal.conn.iterdump())


def forbidden(*args, **kwargs):
    pytest.fail("legacy computation must not be called")


@pytest.mark.parametrize("hypothesis", ["H-EXP1-A-EDGE", "H-EXP1-POOL-EDGE", "H-EXP1-IC-EVAL"])
def test_direct_refuses_before_statistics_and_preserves_entire_registry(journal, monkeypatch, hypothesis):
    register(journal, "ordinary prior registration", params={})
    pid = register(journal, hypothesis)
    before = dump(journal)
    changes = journal.conn.total_changes
    monkeypatch.setattr(prereg, "_effective_n", forbidden)
    monkeypatch.setattr(prereg, "clustered_bootstrap", forbidden)
    with pytest.raises(ValueError, match=REASON) as caught:
        prereg.evaluate_hypothesis(journal, pid, [], "return")
    assert type(caught.value).__name__ == "LegacyFinalizationRefusedError"
    assert not isinstance(caught.value, ResearchContractQuarantinedError)
    assert dump(journal) == before
    assert journal.conn.total_changes == changes


@pytest.mark.parametrize("encoded", [False, True])
def test_marker_returns_distinct_non_quarantine_reason(encoded):
    params = {"registration_version": VERSION}
    result = registration_validity({"params_json": json.dumps(params) if encoded else params})
    assert result["quarantined"] is False
    assert result["legacy_finalization_refused"] is True
    assert result["reason_code"] == REASON


@pytest.mark.parametrize("now", [
    datetime(2026, 11, 18, tzinfo=timezone.utc),
    datetime(2027, 1, 16, 1, tzinfo=timezone.utc),
    datetime(2030, 1, 1, tzinfo=timezone.utc),
])
def test_resolver_refuses_accidental_link_before_metric_without_writes(journal, monkeypatch, now):
    for hypothesis in ("H-EXP1-A-EDGE", "H-EXP1-POOL-EDGE", "H-EXP1-IC-EVAL"):
        link(journal, register(journal, hypothesis), hypothesis)
    before = dump(journal)
    changes = journal.conn.total_changes
    monkeypatch.setitem(resolver.METRIC_FUNCTIONS, "test_metric", forbidden)
    monkeypatch.setattr(resolver, "effective_n", forbidden)
    monkeypatch.setattr(resolver, "evaluate_hypothesis", forbidden)
    summary = resolver.resolve_due_hypotheses(journal, now=now)
    assert len(summary["legacy_refused"]) == 3
    assert all(item["reason_code"] == REASON and not item["quarantined"]
               for item in summary["legacy_refused"])
    assert summary["errors"] == summary["evaluated"] == summary["quarantined"] == []
    assert dump(journal) == before
    assert journal.conn.total_changes == changes


def test_operator_prior_force_and_floor_fields_cannot_bypass(journal):
    pid = register(journal, params={
        "registration_version": VERSION, "force": True, "allow_legacy": True,
    })
    journal.conn.execute(
        "UPDATE preregistrations SET floor_effective_n=0,floor_span_days=0,"
        "analysis_not_before='2000-01-01',strong_prior_pre_documented=1,"
        "operator_approved_for_forward_test=1 WHERE prereg_id=?", (pid,),
    )
    journal.conn.commit()
    before = dump(journal)
    with pytest.raises(ValueError, match=REASON):
        prereg.evaluate_hypothesis(journal, pid, [], "return")
    assert dump(journal) == before


@pytest.mark.parametrize("params", [{}, {"registration_version": "unrelated_v1"},
                                  {"registration_version": None}])
def test_unrelated_registration_keeps_legacy_evaluation(journal, params):
    # Text alone does not select this route: the exact immutable marker does.
    pid = register(journal, params=params)
    result = prereg.evaluate_hypothesis(journal, pid, [], "return", n_resamples=10)
    assert result["evaluated_at_utc"]
    assert result["evidence_status"] == "insufficient_data"


def test_unrelated_resolver_still_calls_metric(journal, monkeypatch):
    pid = register(journal, "ordinary contract", params={})
    link(journal, pid, "ordinary contract")
    calls = []

    def metric(journal):
        calls.append(True)
        return [], "return", None

    monkeypatch.setitem(resolver.METRIC_FUNCTIONS, "test_metric", metric)
    summary = resolver.resolve_due_hypotheses(journal, now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert calls == [True]
    assert summary["not_yet_sufficient"] == ["ordinary contract"]
    assert summary["legacy_refused"] == []


@pytest.mark.parametrize("params", [{"metric_fn_name": "h_tqs_1_rows"}, {"shadow_tier": 1}])
def test_existing_quarantine_takes_precedence_and_is_unchanged(journal, params):
    prior = registration_validity({"params_json": params})
    marked = {**params, "registration_version": VERSION}
    assert registration_validity({"params_json": marked}) == prior
    pid = register(journal, params=marked)
    before = dump(journal)
    with pytest.raises(ResearchContractQuarantinedError):
        prereg.evaluate_hypothesis(journal, pid, [], "return")
    assert dump(journal) == before


def test_invalid_params_remain_quarantined():
    assert registration_validity({"params_json": "{"}) == {
        "quarantined": True, "reason_code": "INVALID_REGISTRATION_PARAMS",
        "reason": "registration parameters cannot be verified",
    }


def test_unrelated_validity_result_is_unchanged():
    assert registration_validity({"params_json": {}}) == {
        "quarantined": False, "reason_code": None,
        "reason": "not flagged by this audit; this is not a validity certification",
    }


def test_actual_explore_shape_cannot_use_existing_per_pair_writer(journal, monkeypatch):
    from alphaos.cards import per_evidence
    from alphaos.stats import two_arm

    ids = [register(journal, hypothesis) for hypothesis in ("H-EXP1-A-EDGE", "H-EXP1-POOL-EDGE")]
    before = dump(journal)
    monkeypatch.setattr(per_evidence, "fetch_active_per_card_identity", forbidden)
    monkeypatch.setattr(per_evidence, "build_primary_evidence", forbidden)
    monkeypatch.setattr(two_arm, "two_arm_bootstrap", forbidden)
    result = prereg.evaluate_two_arm_hypothesis_pair(journal, *ids, "2030-01-01T00:00:00Z")
    assert result["outcome"] == "deferred"
    assert result["reason"] == "identity_fields_missing"
    assert dump(journal) == before
