"""Swing strategy (the v1 default): news-confirmed momentum continuation.

HOLD-2 audit-fixup (S-e): the holding-window horizon is CARD-DRIVEN, not a
fixed "1-5 trading days" -- catalyst_momentum_v1/v2 (and prompt v1/v2/v3's
own frozen prompt text) cap it at 3, catalyst_momentum_v3 (prompt v4) at
10; see alphaos/cards/*.yaml's own max_holding_days_default and
alphaos/ai/prompt_templates.py's LEGACY_MAX_HOLDING_DAYS_BOUND /
build_no_news_system_prompt() for where the number actually comes from.
This module itself carries no horizon literal beyond the defensive
fallback below.

Turns an OpenAI 'propose' evaluation plus a risk-engine sizing into a concrete,
journal-ready TradeProposal. Shorts are flagged ``requires_margin`` so they hit
the explicit-approval gate before any short-execution capability is used.
"""

from __future__ import annotations


from alphaos.ai.openai_client import OpenAIEvaluation
from alphaos.constants import Strategy, TradeDirection
from alphaos.risk.risk_engine import PositionSizing
from alphaos.strategy.proposal import TradeProposal


class SwingStrategy:
    name = Strategy.SWING.value

    def build_proposal(
        self, evaluation: OpenAIEvaluation, sizing: PositionSizing, is_demo: bool = False
    ) -> TradeProposal:
        direction = evaluation.direction or TradeDirection.LONG.value
        requires_margin = direction == TradeDirection.SHORT.value  # conservative default
        return TradeProposal(
            symbol=evaluation.symbol,
            direction=direction,
            strategy=self.name,
            entry=float(evaluation.entry),
            stop=float(evaluation.stop),
            target=float(evaluation.target),
            # Defensive fallback ONLY (a malformed/missing evaluator
            # response) -- never the normal path. Under v4, this is
            # structurally unreachable for a PROPOSE: validate_max_holding_
            # days_range() (alphaos/ai/validation.py) already rejects any
            # PROPOSE whose max_holding_days is missing/non-integral/out of
            # range BEFORE evaluate() ever returns, so a v4 evaluation that
            # reaches this line always carries a real, validated int.
            max_holding_days=int(evaluation.max_holding_days or 3),
            qty=sizing.shares,
            risk_per_share=sizing.risk_per_share,
            dollar_risk=sizing.dollar_risk,
            expected_r=evaluation.expected_r,
            same_day_exit_eligible=True,  # swing trades remain same-day-exit eligible
            candidate_id=evaluation.candidate_id,
            eval_id=evaluation.eval_id,
            requires_margin=requires_margin,
            margin_approved=False,
            status="proposed",
            is_demo=is_demo,
        )
