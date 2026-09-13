"""Audit acceptance: no new frozen claims from known-invalid contracts."""
from datetime import datetime, timezone
import json

import pytest

from alphaos.stats.research_validity import ResearchContractQuarantinedError
from alphaos.stats.preregistration import evaluate_hypothesis, register_hypothesis
from alphaos.hypotheses import registry
from alphaos.hypotheses.resolver import resolve_due_hypotheses
from alphaos.hypotheses.queries import h_pol_1_rows
from alphaos.cards.promotion import check_promotion_preconditions
from alphaos.cards.per_evidence import _dedupe_to_one_per_event, _market_date
from alphaos.ai.last30days_polarity import Last30DaysPolarityClassifier
from alphaos.learning.outcomes_tracker import update_pending_outcomes
from alphaos.learning.replay_corrections import build_replay_corrections
from alphaos.reports.hypothesis_report import build_hypothesis_report
from alphaos.util import timeutils
from test_replay_maturity_repair import seed, Bars


@pytest.mark.parametrize("metric_fn", [f"h_{n}_1_rows" for n in ("tqs", "cat", "int", "win", "ttl", "rej", "pol")])
def test_direct_writer_rejects_every_identified_metric_contract(journal, metric_fn):
    pid = register_hypothesis(journal, "registered claim", "metric", floor_effective_n=1,
        floor_span_days=0, analysis_not_before="2020-01-01", params={"metric_fn_name": metric_fn})
    # Prior/operator approval cannot validate an invalid estimand or method.
    journal.conn.execute("UPDATE preregistrations SET strong_prior_pre_documented=1,operator_approved_for_forward_test=1 WHERE prereg_id=?", (pid,))
    journal.conn.commit()
    before = journal.query("SELECT * FROM preregistrations")
    with pytest.raises(ResearchContractQuarantinedError, match="No evidence was written"):
        evaluate_hypothesis(journal, pid, [{"symbol": f"S{i}", "decision_date": "2020-01-01", "r": 99} for i in range(100)], "r")
    assert journal.query("SELECT * FROM preregistrations") == before


@pytest.mark.parametrize("metric,params", [
    ("mean_ai_delta_r threshold_v1", {}), ("mean_ai_delta_r threshold_v1_hold10", {}),
    ("shadow interest", {"shadow_tier": 1}), ("per_excess_market_adjusted_5d", {}),
])
def test_other_audited_registration_families_cannot_use_generic_writer(journal, metric, params):
    pid = register_hypothesis(journal, "claim", metric, floor_effective_n=1,
        floor_span_days=0, analysis_not_before="2020-01-01", params=params)
    with pytest.raises(ResearchContractQuarantinedError):
        evaluate_hypothesis(journal, pid, [], "r")
    assert journal.one("SELECT evaluated_at_utc FROM preregistrations WHERE prereg_id=?", (pid,))["evaluated_at_utc"] is None


def test_resolver_and_promotion_cannot_treat_operator_met_as_validity(journal):
    register_hypothesis(journal, registry._BASELINE_HYPOTHESIS_TEXT, registry._BASELINE_METRIC_TEXT,
        floor_effective_n=30, floor_span_days=28, analysis_not_before="2020-01-01")
    rows = registry.seed_all(journal)
    summary = resolve_due_hypotheses(journal, now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert len(summary["quarantined"]) == 8
    assert summary["evaluated"] == [] and summary["errors"] == []
    for r in rows:
        if not r.get("card_id"):
            continue
        journal.conn.execute("UPDATE hypothesis_proposals SET status='met',last_q_value=0.00001 WHERE hypothesis_id=?", (r["hypothesis_id"],))
        journal.conn.commit()
        assert check_promotion_preconditions(journal, r["hypothesis_id"], "approved research")["reason_code"] == "RESEARCH_CONTRACT_QUARANTINED"
    report = build_hypothesis_report(journal)
    progress = [r["progress"] for r in report["hypotheses"] if r["progress"]]
    assert progress and all(not p["resolver_ready"] and p["research_validity"]["quarantined"] for p in progress)


def test_polarity_query_consumes_actual_producer_alignment(journal):
    for name, sentiment, value in (("aligned", "bullish", 1.0), ("conflicting", "bearish", -1.0)):
        alignment = Last30DaysPolarityClassifier._alignment(sentiment, "long")
        assert alignment == name
        journal.insert("last30days_polarity", {"polarity_id": name, "candidate_id": name, "symbol": "AAPL", "direction_alignment": alignment})
        journal.insert("candidate_outcomes", {"outcome_id": name, "candidate_id": name,
            "symbol": "AAPL", "candidate_type": "candidate", "replay_r": value,
            "outcome_status": "complete", "decision_at_utc": "2026-01-05T16:00:00+00:00"})
    rows, key, controls = h_pol_1_rows(journal)
    assert len(rows) == len(controls) == 1
    assert rows[0][key] == -2.0


def test_per_first_event_uses_exact_timestamp_and_stable_ties():
    base = {"symbol": "AAPL", "fiscal_date_ending": "2026-03-31", "report_date": "2026-04-20"}
    rows = [{**base, "candidate_id": "a-later", "decision_at_utc": "2026-04-21T19:00:00Z"},
            {**base, "candidate_id": "z-first", "decision_at_utc": "2026-04-21T10:00:00-04:00"},
            {**base, "candidate_id": "a-first-tie", "decision_at_utc": "2026-04-21T14:00:00Z"},
            {**base, "candidate_id": "bad", "decision_at_utc": "broken"}]
    assert _dedupe_to_one_per_event(rows)[0]["candidate_id"] == "a-first-tie"
    assert _dedupe_to_one_per_event(list(reversed(rows)))[0]["candidate_id"] == "a-first-tie"
    assert _market_date("2026-04-21T00:30:00Z") == "2026-04-20"


@pytest.mark.parametrize("decision,forward_date", [
    ("2026-01-06T00:30:00Z", "2026-01-06"),  # winter: prior US evening
    ("2026-07-07T00:30:00Z", "2026-07-07"),  # DST: prior US evening
])
def test_live_and_correction_replay_share_us_session_anchor(journal, monkeypatch, decision, forward_date):
    seed(journal)
    journal.conn.execute("UPDATE candidate_outcomes SET decision_at_utc=?", (decision,))
    journal.conn.commit()
    monkeypatch.setattr(timeutils, "now_utc", lambda: datetime(2027, 1, 1, tzinfo=timezone.utc))
    bar = {"date": forward_date, "high": 121.0, "low": 99.0, "close": 120.0}
    update_pending_outcomes(journal, Bars([bar]))
    row = journal.one("SELECT * FROM candidate_outcomes")
    assert row["replay_result"] == "target_hit" and row["replay_bars_used"] == 1
    # Independent historical fixture: same new correction anchor, original untouched.
    seed(journal, legacy=True, candidate_id="legacy", outcome_id="legacy")
    journal.conn.execute("UPDATE candidate_outcomes SET decision_at_utc=? WHERE outcome_id='legacy'", (decision,))
    journal.conn.commit()
    journal.insert("daily_bars", {"bar_id": "b", "symbol": "AAPL", "market_date": forward_date,
        "high": 121.0, "low": 99.0, "close": 120.0, "source_feed": "test"})
    before = journal.one("SELECT * FROM candidate_outcomes WHERE outcome_id='legacy'")
    report = build_replay_corrections(journal.conn, as_of_date=forward_date)
    assert json.loads(report["records"][0]["corrected_json"])["result"] == "target_hit"
    assert journal.one("SELECT * FROM candidate_outcomes WHERE outcome_id='legacy'") == before


def test_per_cli_selects_v2_pair_but_cannot_freeze_it(orchestrator, capsys):
    from alphaos.__main__ import cmd_per_register, cmd_per_evaluate
    from alphaos.cards.per_evidence import PER_HYPOTHESIS_POS_V2, PER_HYPOTHESIS_NEG_V2, PER_METRIC_POS_V2, PER_METRIC_NEG_V2
    cmd_per_register(orchestrator)
    ids = [register_hypothesis(orchestrator.journal, hypothesis, metric, floor_effective_n=20,
        floor_span_days=90, analysis_not_before="2026-10-15") for hypothesis, metric in (
            (PER_HYPOTHESIS_POS_V2, PER_METRIC_POS_V2), (PER_HYPOTHESIS_NEG_V2, PER_METRIC_NEG_V2))]
    before = orchestrator.journal.query("SELECT * FROM preregistrations ORDER BY id")
    capsys.readouterr()
    assert cmd_per_evaluate(orchestrator) == 0
    out = json.loads(capsys.readouterr().out)["per_evaluate"]
    assert [out["prereg_id_pos"], out["prereg_id_neg"]] == ids
    assert out["reason"] == "research_contract_quarantined"
    assert orchestrator.journal.query("SELECT * FROM preregistrations ORDER BY id") == before


@pytest.mark.parametrize("decision,forward_date", [
    ("2026-01-06T00:30:00Z", "2026-01-06"), ("2026-07-07T00:30:00Z", "2026-07-07"),
])
def test_baseline_uses_same_us_anchor_and_preserves_completed_rows(journal, monkeypatch, decision, forward_date):
    from alphaos.baseline.tracker import _resolve_pending_rows_for_arm_set
    monkeypatch.setattr(timeutils, "now_utc", lambda: datetime(2027, 1, 1, tzinfo=timezone.utc))
    for name, status in (("pending", "pending"), ("historical", "complete")):
        journal.insert("shadow_baseline_decisions", {"baseline_decision_id": name, "candidate_id": name,
            "symbol": "AAPL", "rule_version": "threshold_v1", "decision": "propose", "input_sha": name,
            "decision_at_utc": decision, "entry": 100, "stop": 90, "target": 120, "direction": "long",
            "replay_status": status, "replay_r": 0.25 if name == "historical" else None})
    old = journal.one("SELECT * FROM shadow_baseline_decisions WHERE baseline_decision_id='historical'")
    _resolve_pending_rows_for_arm_set(journal, Bars([{"date": forward_date, "high": 121, "low": 99, "close": 120}]),
                                     20, ("threshold_v1",), 10)
    assert journal.one("SELECT replay_result FROM shadow_baseline_decisions WHERE baseline_decision_id='pending'")["replay_result"] == "target_hit"
    assert journal.one("SELECT * FROM shadow_baseline_decisions WHERE baseline_decision_id='historical'") == old
