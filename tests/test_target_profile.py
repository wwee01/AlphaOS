"""Target-profile tracking (evidence only; no trading-behavior change).

Proves: default proposals are configured_standard; the configured stop/target
sizing is persisted; target_profile survives proposal -> order -> position ->
exit -> outcome; metrics group by target_profile; and the R:R guard still blocks
sub-minimum proposals."""

from __future__ import annotations

from alphaos.ai.openai_client import OpenAIClient
from alphaos.constants import Decision, ReasonCode, TargetProfile
from alphaos.reports.metrics import compute_metrics_by_target_profile
from conftest import make_proposal, make_settings


STD = TargetProfile.CONFIGURED_STANDARD.value


def test_default_proposal_uses_configured_standard():
    assert make_proposal().target_profile == STD


def test_target_profile_survives_proposal_to_outcome(orchestrator):
    orch = orchestrator
    res = orch.seed_demo()                      # creates -> tags -> approves -> opens
    assert res["approved"] is True
    pid = res["proposal_id"]
    j = orch.journal

    # proposal: profile + the configured sizing snapshot persisted
    prop = j.proposal_by_id(pid)
    assert prop["target_profile"] == STD
    assert prop["target_reward_risk"] == orch.settings.target_reward_risk
    assert prop["stop_loss_pct"] == orch.settings.stop_loss_pct
    assert prop["target_price_source"] is not None

    # order
    order = j.one("SELECT * FROM paper_orders WHERE proposal_id = ?", (pid,))
    assert order["target_profile"] == STD

    # position (carries the sizing snapshot too)
    pos = j.one("SELECT * FROM positions WHERE order_id = ?", (order["order_id"],))
    assert pos["target_profile"] == STD
    assert pos["target_reward_risk"] == orch.settings.target_reward_risk

    # force a target hit -> exit + outcome
    out = orch.run_monitor_once(price_overrides={pos["symbol"]: float(pos["target_price"]) + 1.0})
    assert out["exits"], "expected a target exit"

    exit_row = j.one("SELECT * FROM exits WHERE position_id = ?", (pos["position_id"],))
    assert exit_row["target_profile"] == STD

    outcome = j.one("SELECT * FROM trade_outcomes WHERE position_id = ?", (pos["position_id"],))
    assert outcome["target_profile"] == STD
    assert outcome["target_reward_risk"] == orch.settings.target_reward_risk  # configured value persisted
    assert outcome["stop_loss_pct"] == orch.settings.stop_loss_pct


def test_metrics_group_by_target_profile():
    outs = [
        {"net_pnl": 10, "gross_pnl": 11, "costs": 1, "holding_days": 1, "classification": "a", "target_profile": STD},
        {"net_pnl": -5, "gross_pnl": -5, "costs": 0, "holding_days": 2, "classification": "b", "target_profile": STD},
        {"net_pnl": 20, "gross_pnl": 21, "costs": 1, "holding_days": 1, "classification": "c", "target_profile": "extended"},
    ]
    grouped = compute_metrics_by_target_profile(outs)
    assert set(grouped) == {STD, "extended"}
    assert grouped[STD]["trades"] == 2
    assert grouped["extended"]["trades"] == 1
    # descriptive-only caveat preserved per group (< 30 trades)
    assert grouped[STD]["small_sample"] is True
    assert "descriptive only" in grouped[STD]["note"]
    # the standard metrics are present per group
    for key in ("win_rate", "expectancy", "profit_factor", "avg_hold_days", "total_costs"):
        assert key in grouped[STD]


def test_rr_guard_still_blocks_below_min():
    eng = OpenAIClient(make_settings(TARGET_REWARD_RISK="1.0", MIN_REWARD_RISK="1.5"))
    ev = eng.evaluate(
        {"symbol": "T", "direction": "long", "momentum_score": 0.8, "candidate_id": "c"},
        {"last_price": 100.0},
    )
    assert ev.decision == Decision.REJECT.value
    assert ReasonCode.REWARD_RISK_TOO_LOW.value in ev.risk_flags
