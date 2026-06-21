"""Day-trade experiment module — GATED, paper-only stub.

Day trading is a separately gated, paper-only experimental module — never the
default. Per the resolved decisions:
* it is disabled by default,
* auto mode cannot approve day-trade experiment trades,
* its trades are tagged DAYTRADE_EXPERIMENT and kept in a separate book.

v1 ships the separation and the gate, not a working intraday engine.
"""

from __future__ import annotations

from alphaos.ai.openai_client import OpenAIEvaluation
from alphaos.constants import Strategy, TradeDirection
from alphaos.risk.risk_engine import PositionSizing
from alphaos.strategy.proposal import TradeProposal


class DaytradeExperiment:
    name = Strategy.DAYTRADE_EXPERIMENT.value

    def __init__(self, enabled: bool = False):
        # Disabled by default. Even when enabled, it stays paper-only and cannot
        # be auto-approved (enforced in the approval path).
        self.enabled = enabled

    @property
    def is_enabled(self) -> bool:
        return self.enabled

    def build_proposal(
        self, evaluation: OpenAIEvaluation, sizing: PositionSizing, is_demo: bool = False
    ) -> TradeProposal:
        if not self.enabled:
            raise RuntimeError("Day-trade experiment is gated/disabled by default in v1.")
        direction = evaluation.direction or TradeDirection.LONG.value
        return TradeProposal(
            symbol=evaluation.symbol,
            direction=direction,
            strategy=self.name,
            entry=float(evaluation.entry),
            stop=float(evaluation.stop),
            target=float(evaluation.target),
            max_holding_days=0,  # intraday by definition
            qty=sizing.shares,
            risk_per_share=sizing.risk_per_share,
            dollar_risk=sizing.dollar_risk,
            expected_r=evaluation.expected_r,
            same_day_exit_eligible=True,
            candidate_id=evaluation.candidate_id,
            eval_id=evaluation.eval_id,
            requires_margin=direction == TradeDirection.SHORT.value,
            margin_approved=False,
            status="proposed",
            is_demo=is_demo,
        )
