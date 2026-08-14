"""GROUP-B / PRE-1a: the provider-health rung in the alert ladder.

Covers:
* ai/labeller_health.py's generalized grader (summarize_failsafe_rows,
  is_openai_reject_row, evaluate_failsafe_health's source_label) -- proving
  the labeller's existing behavior is byte-identical AND the evaluator can
  now be graded the same way.
* the detection signal is risk_flags_json containing OPENAI_REJECT, NOT a
  null prompt_hash (RR-floor / NO_ATR_DATA rejections are hash-less too and
  must NOT count as a fail-safe).
* ai/openai_client.py's post_process rejection paths preserve ai_lineage +
  token usage (the cost-undercount fix).
* reports/daily_brief.py::_one_action gains the provider-health rung above
  stale backup, at priority="high" only when it is the winning rung.
* render_compact's "did anything happen" line.

Hermetic throughout -- no real OpenAI calls; ai/openai_client.py stays in
mock mode (no API key) for every test here.
"""

from __future__ import annotations

from alphaos.ai.labeller_health import (
    evaluate_failsafe_health,
    is_openai_reject_row,
    summarize_failsafe_rows,
)
from alphaos.ai.openai_client import OpenAIClient
from alphaos.reports.daily_brief import (
    _evaluator_health,
    _one_action,
    _one_action_priority,
    build_daily_brief,
    render_compact,
    render_markdown,
)
from alphaos.util.ids import new_id
from conftest import make_settings


# ------------------------------------------------------ generalized grader
def test_evaluate_failsafe_health_labeller_message_is_byte_identical_by_default():
    """source_label defaults to 'Labeller' -- every pre-existing caller
    (orchestrator.py, api/routes.py, daily_recon.py) that doesn't pass it
    must see EXACTLY the same message text as before this ticket."""
    summary = {"total": 10, "fail_safe": 3, "fail_safe_rate": 0.3, "by_failsafe_reason": {"timeout": 3}}
    health = evaluate_failsafe_health(summary, warn_rate=0.25, critical_rate=0.5, min_sample=5)
    assert health["message"].startswith("Labeller fail-safe rate is 30%")
    assert "the labeller is silently failing safe to reject" in health["message"]


def test_evaluate_failsafe_health_source_label_generalizes_the_message():
    summary = {"total": 10, "fail_safe": 6, "fail_safe_rate": 0.6, "by_failsafe_reason": {"OPENAI_REJECT": 6}}
    health = evaluate_failsafe_health(
        summary, warn_rate=0.25, critical_rate=0.5, min_sample=5, source_label="Evaluator",
    )
    assert health["level"] == "critical"
    assert health["message"].startswith("Evaluator fail-safe rate is 60%")
    assert "the evaluator is silently failing safe to reject" in health["message"]


def test_summarize_failsafe_rows_matches_labeller_source_summary_shape():
    """Same total/fail_safe/fail_safe_rate/by_failsafe_reason keys and
    arithmetic as JournalStore.labeller_source_summary -- proves this is a
    genuine generalization, not a second, independently-drifting formula."""
    rows = [{"x": "fail"}, {"x": "ok"}, {"x": "fail"}, {"x": "ok"}]
    summary = summarize_failsafe_rows(
        rows, is_failsafe=lambda r: r["x"] == "fail", reason_of=lambda r: "reason_a",
    )
    assert summary["total"] == 4
    assert summary["fail_safe"] == 2
    assert summary["fail_safe_rate"] == 0.5
    assert summary["by_failsafe_reason"] == {"reason_a": 2}


def test_summarize_failsafe_rows_empty_is_zero_rate_not_a_zero_division():
    summary = summarize_failsafe_rows([], is_failsafe=lambda r: True, reason_of=lambda r: "x")
    assert summary == {"total": 0, "fail_safe": 0, "fail_safe_rate": 0.0, "by_failsafe_reason": {}}


# ---------------------------------------------- detection signal correctness
def test_is_openai_reject_row_true_only_for_openai_reject_flag():
    assert is_openai_reject_row({"risk_flags_json": '["OPENAI_REJECT"]'}) is True
    assert is_openai_reject_row({"risk_flags_json": '["OPENAI_REJECT", "STALE_DATA"]'}) is True


def test_is_openai_reject_row_false_for_rr_floor_and_no_atr_even_though_hash_less():
    """THE detection-signal correctness test the spec calls out by name:
    RR-floor and NO_ATR_DATA rejections are hash-less on openai_evaluations
    too, but they are NORMAL, working-as-designed rejections -- must never
    be counted as a provider fail-safe just because prompt_hash is NULL."""
    assert is_openai_reject_row({"risk_flags_json": '["REWARD_RISK_TOO_LOW"]'}) is False
    assert is_openai_reject_row({"risk_flags_json": '["NO_ATR_DATA"]'}) is False


def test_is_openai_reject_row_handles_missing_and_malformed_json():
    assert is_openai_reject_row({"risk_flags_json": None}) is False
    assert is_openai_reject_row({}) is False
    assert is_openai_reject_row({"risk_flags_json": "not json but has OPENAI_REJECT in it"}) is True
    assert is_openai_reject_row({"risk_flags_json": "not json at all"}) is False


def _insert_eval(journal, symbol, risk_flags):
    import json

    journal.insert("openai_evaluations", {
        "eval_id": new_id("eval"), "candidate_id": new_id("cand"), "symbol": symbol,
        "model": "mock", "direction": "long", "decision": "reject",
        "reasoning_summary": "test", "risk_flags_json": json.dumps(risk_flags), "is_mock": 0,
    })


def test_evaluator_health_reads_openai_evaluations_and_grades_via_the_same_thresholds(journal):
    settings = make_settings(
        LABELLER_FAILSAFE_WARN_RATE="0.25", LABELLER_FAILSAFE_CRITICAL_RATE="0.5",
        LABELLER_FAILSAFE_MIN_SAMPLE="2",
    )
    # 3 real OPENAI_REJECT rows (provider actually failing), 2 healthy
    # RR-floor rejects (must NOT count) -- rate should be 3/5 = 0.6 -> critical.
    for _ in range(3):
        _insert_eval(journal, "NVDA", ["OPENAI_REJECT"])
    for _ in range(2):
        _insert_eval(journal, "AMD", ["REWARD_RISK_TOO_LOW"])

    health = _evaluator_health(journal, settings)
    assert health["sample"] == 5
    assert health["fail_safe"] == 3
    assert health["rate"] == 0.6
    assert health["level"] == "critical"
    assert "Evaluator" in health["message"]


def test_evaluator_health_all_rr_floor_rejections_stays_ok_despite_100pct_hash_less(journal):
    """The regression this whole detection signal exists to prevent: a
    perfectly healthy day where every rejection is RR-floor/NO_ATR_DATA
    (100% hash-less) must NOT read as a dead provider."""
    settings = make_settings(
        LABELLER_FAILSAFE_WARN_RATE="0.25", LABELLER_FAILSAFE_CRITICAL_RATE="0.5",
        LABELLER_FAILSAFE_MIN_SAMPLE="2",
    )
    for _ in range(5):
        _insert_eval(journal, "AMD", ["REWARD_RISK_TOO_LOW"])
    health = _evaluator_health(journal, settings)
    assert health["fail_safe"] == 0
    assert health["level"] == "ok"


# ------------------------------------------------ post_process lineage fix
def _fake_live_evaluation(candidate, model_provider="openai", prompt_hash="abc123"):
    from alphaos.ai.openai_client import OpenAIEvaluation
    from alphaos.constants import Decision

    ev = OpenAIEvaluation(
        eval_id=new_id("eval"), candidate_id=candidate.get("candidate_id", ""), symbol=candidate["symbol"],
        model="gpt-test", direction="long", entry=100.0, stop=95.0, target=130.0, max_holding_days=3,
        expected_r=6.0, confidence=0.8, decision=Decision.PROPOSE.value, reasoning_summary="test",
    )
    ev.model_provider = model_provider
    ev.prompt_hash = prompt_hash
    ev.system_prompt_hash = "sys456"
    ev.prompt_tokens = 500
    ev.completion_tokens = 120
    ev.total_tokens = 620
    return ev


def test_rr_floor_rejection_preserves_ai_lineage_and_tokens_from_a_real_call():
    """The cost-undercount fix: a REAL live call that then gets downgraded to
    REWARD_RISK_TOO_LOW must keep its ai_lineage + token usage, never read
    as if no call happened. Floor is set well above the fixture's own
    expected_r=6.0 so the rejection is guaranteed to fire regardless of the
    default settings fixture's own configured min_reward_risk."""
    candidate = {"candidate_id": new_id("cand"), "symbol": "NVDA"}
    live_eval = _fake_live_evaluation(candidate)
    floored_settings = make_settings(MIN_REWARD_RISK="999")
    floored_client = OpenAIClient(floored_settings)

    rejected = floored_client._enforce_min_reward_risk(live_eval, candidate)

    assert rejected.decision == "reject"
    assert rejected.model_provider == "openai"
    assert rejected.prompt_hash == "abc123"
    assert rejected.system_prompt_hash == "sys456"
    assert rejected.prompt_tokens == 500
    assert rejected.completion_tokens == 120
    assert rejected.total_tokens == 620


def test_no_atr_data_rejection_preserves_ai_lineage_and_tokens_from_a_real_call(settings):
    """Same fix, the OTHER post_process rejection path (_apply_atr_stop's
    NO_ATR_DATA branch -- no atr_history row exists in this hermetic journal
    fixture, so it always rejects)."""
    client = OpenAIClient(settings)
    candidate = {"candidate_id": new_id("cand"), "symbol": "NVDA"}
    live_eval = _fake_live_evaluation(candidate)

    rejected = client._apply_atr_stop(live_eval, candidate)

    assert rejected.decision == "reject"
    assert rejected.model_provider == "openai"
    assert rejected.prompt_hash == "abc123"
    assert rejected.prompt_tokens == 500
    assert rejected.total_tokens == 620


def test_raw_evaluate_exception_rejection_has_no_lineage_to_inherit(settings, monkeypatch):
    """The ONE rejection path that correctly has NOTHING to inherit -- the
    live call itself failed before any response existed."""
    client = OpenAIClient(settings)
    client.use_mock = False  # force the live branch's own try/except
    monkeypatch.setattr(
        client, "_live_eval", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    candidate = {"candidate_id": new_id("cand"), "symbol": "NVDA", "direction": "long"}
    rejected = client.raw_evaluate(candidate, snapshot={"last_price": 100.0})
    assert rejected.decision == "reject"
    assert rejected.model_provider is None
    assert rejected.prompt_tokens is None


def test_rejection_without_inherit_from_defaults_to_none_lineage(settings):
    """No regression for every OTHER _rejection() call site that doesn't
    pass inherit_from (e.g. a mock-mode freshness rejection)."""
    client = OpenAIClient(settings)
    candidate = {"candidate_id": new_id("cand"), "symbol": "AMD", "direction": "long"}
    rejected = client._rejection(candidate, "stale", ["STALE_DATA"])
    assert rejected.model_provider is None
    assert rejected.prompt_hash is None
    assert rejected.prompt_tokens is None


# ------------------------------------------------------------- _one_action rung
def _needs_you(**overrides):
    base = {
        "pending_approvals": [], "pending_approval_count": 0, "open_incidents": [],
        "open_incident_count": 0, "fused_jobs": [], "hypothesis_resolution": None,
        "hypothesis_drafts_pending": None, "positions_past_time_window": [],
    }
    base.update(overrides)
    return base


def test_one_action_provider_health_ranks_above_stale_backup():
    evaluator_health = {"level": "critical", "message": "Evaluator fail-safe rate is 100% [CRITICAL]."}
    backup_health = {"stale": True, "days_since_success": 3}
    action = _one_action(
        _needs_you(), positions_health=[], moonshot_gap={"status": "below_sample_floor", "data_progress": "0/5"},
        backup_health=backup_health, evaluator_health=evaluator_health,
    )
    assert "Evaluator fail-safe rate is 100%" in action


def test_one_action_incident_still_ranks_above_provider_health():
    evaluator_health = {"level": "critical", "message": "Evaluator fail-safe rate is 100% [CRITICAL]."}
    action = _one_action(
        _needs_you(open_incident_count=1), positions_health=[],
        moonshot_gap={"status": "below_sample_floor", "data_progress": "0/5"}, evaluator_health=evaluator_health,
    )
    assert "open protection incident" in action


def test_one_action_healthy_provider_falls_through_to_nothing_needs_you():
    evaluator_health = {"level": "ok", "message": None}
    action = _one_action(
        _needs_you(), positions_health=[], moonshot_gap={"status": "ok"}, evaluator_health=evaluator_health,
    )
    assert action == "Nothing needs you right now."


def test_one_action_priority_high_only_when_provider_health_is_the_winning_rung():
    evaluator_health = {"level": "critical", "message": "x"}
    assert _one_action_priority(_needs_you(), evaluator_health) == "high"
    # An open incident preempts the headline -- priority stays default (out
    # of scope for this narrow fix).
    assert _one_action_priority(_needs_you(open_incident_count=1), evaluator_health) == "default"
    assert _one_action_priority(_needs_you(), {"level": "ok"}) == "default"
    assert _one_action_priority(_needs_you(), None) == "default"


# ------------------------------------------------------- full brief wiring
def test_build_daily_brief_healthy_provider_example(orchestrator):
    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    assert brief["evaluator_health"]["level"] == "ok"
    assert brief["one_action_priority"] == "default"
    assert "Nothing needs you" in brief["one_action"] or brief["one_action"]


def test_build_daily_brief_provider_down_example(orchestrator):
    for _ in range(10):
        _insert_eval(orchestrator.journal, "NVDA", ["OPENAI_REJECT"])
    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    assert brief["evaluator_health"]["level"] == "critical"
    assert brief["one_action_priority"] == "high"
    assert "Evaluator fail-safe rate" in brief["one_action"]
    md = render_markdown(brief)
    assert "AI evaluator health: CRITICAL" in md


def test_render_compact_did_anything_happen_line(orchestrator):
    for _ in range(5):
        _insert_eval(orchestrator.journal, "NVDA", ["OPENAI_REJECT"])
    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    compact = render_compact(brief)
    assert "proposal(s) produced" in compact
    assert "AI errors: 5" in compact
    assert len(compact) < 1000
