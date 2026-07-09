"""EVAL-1 addendum (Fable 5 review): the primary evaluator's snapshot input
is now journaled alongside every evaluation, on every path -- mock, live,
outright rejection, AND the min-reward:risk downgrade path specifically,
since that one constructs a BRAND NEW OpenAIEvaluation object after the
main evaluate() branch already ran, and an earlier draft of this change
stamped the snapshot too early and would have missed it. Previously the
primary evaluator was the one real AI call in this codebase whose input
could never be replayed after the fact (unlike the labeller's packet_json,
which EVAL-1 already replays).
"""

from __future__ import annotations

from alphaos.ai.openai_client import OpenAIClient, OpenAIEvaluation
from conftest import make_settings


# ------------------------------------------------------- dataclass + to_row
def test_openai_evaluation_to_row_carries_snapshot_json():
    ev = OpenAIEvaluation(
        eval_id="ev1", candidate_id="c1", symbol="AAPL", model="gpt-5.4-mini",
        direction="long", entry=100.0, stop=97.0, target=106.0, max_holding_days=3,
        expected_r=2.0, confidence=0.8, decision="propose", reasoning_summary="test",
        snapshot={"last_price": 100.0, "spread_pct": 0.01},
    )

    row = ev.to_row()

    assert row["snapshot_json"] == {"last_price": 100.0, "spread_pct": 0.01}


def test_openai_evaluation_defaults_snapshot_json_to_empty_dict():
    ev = OpenAIEvaluation(
        eval_id="ev1", candidate_id="c1", symbol="AAPL", model="mock",
        direction="long", entry=100.0, stop=97.0, target=106.0, max_holding_days=3,
        expected_r=2.0, confidence=0.8, decision="propose", reasoning_summary="test",
        is_mock=True,
    )

    row = ev.to_row()

    assert row["snapshot_json"] == {}


# ------------------------------------------------------------ evaluate() paths
def test_evaluate_stamps_snapshot_on_the_mock_propose_path():
    settings = make_settings()
    client = OpenAIClient(settings)
    snapshot = {"last_price": 100.0, "freshness_status": "usable"}

    evaluation = client.evaluate({"symbol": "AAPL", "direction": "long", "momentum_score": 0.9}, snapshot)

    assert evaluation.snapshot == snapshot


def test_evaluate_stamps_snapshot_on_the_mock_watch_path():
    settings = make_settings()
    client = OpenAIClient(settings)
    snapshot = {"last_price": 100.0, "freshness_status": "usable"}

    evaluation = client.evaluate({"symbol": "AAPL", "direction": "long", "momentum_score": 0.01}, snapshot)

    assert evaluation.decision == "watch"
    assert evaluation.snapshot == snapshot


def test_evaluate_stamps_snapshot_on_a_stale_freshness_rejection():
    settings = make_settings()
    client = OpenAIClient(settings)
    snapshot = {"last_price": 100.0}

    evaluation = client.evaluate(
        {"symbol": "AAPL", "direction": "long", "momentum_score": 0.9}, snapshot, freshness_status="stale",
    )

    assert evaluation.decision == "reject"
    assert evaluation.snapshot == snapshot


def test_evaluate_stamps_snapshot_even_when_min_reward_risk_swaps_in_a_new_rejection():
    """Regression guard: _enforce_min_reward_risk constructs a BRAND NEW
    OpenAIEvaluation via _rejection() when a mock PROPOSE's expected_r falls
    below the configured floor -- an earlier draft stamped snapshot BEFORE
    calling this guard, which would silently miss the swapped-in object."""
    settings = make_settings(MIN_REWARD_RISK="100")  # impossibly high floor -- always triggers the downgrade
    client = OpenAIClient(settings)
    snapshot = {"last_price": 100.0, "freshness_status": "usable"}

    evaluation = client.evaluate({"symbol": "AAPL", "direction": "long", "momentum_score": 0.9}, snapshot)

    assert evaluation.decision == "reject"
    assert "reward:risk" in evaluation.reasoning_summary
    assert evaluation.snapshot == snapshot


def test_evaluate_snapshot_stamp_is_a_reference_not_a_defensive_copy():
    """Documents current behavior precisely (not a design requirement) --
    useful if a future caller ever mutates the snapshot dict after the
    call and is surprised the journaled copy changed too."""
    settings = make_settings()
    client = OpenAIClient(settings)
    snapshot = {"last_price": 100.0}

    evaluation = client.evaluate({"symbol": "AAPL", "direction": "long", "momentum_score": 0.9}, snapshot)

    assert evaluation.snapshot is snapshot


# ==================================================================== E2E
def test_real_mock_scan_journals_snapshot_on_openai_evaluations(orchestrator):
    orchestrator.run_scan_once()

    row = orchestrator.journal.one("SELECT * FROM openai_evaluations LIMIT 1")

    assert row is not None
    assert row["snapshot_json"] is not None
    assert row["snapshot_json"] != "{}"
