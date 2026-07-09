"""BASELINE: the two frozen deterministic rules (spec item 1) -- pure
compute, no clock/DB/RNG (house pattern #4's enricher/pure-compute split;
the orchestration layer in ``alphaos.baseline.tracker`` looks up ATR/
settings and passes plain values in here).

Both rules bracket the AI between "propose everything the labeller sees"
and "propose only the historically-median-interest-score-or-better
candidate" -- neither one conditions on anything the AI itself decided, so
attribution can honestly ask whether the AI's incremental selectivity (or
its narrative judgment) adds R over either extreme, GIVEN a candidate
reached the labeller (audit C2's own "conditional added-R" framing -- never
overclaimed as unconditional edge).

Rule v1 is immutable once shipped (Prime Directive 7): a future change to
either rule's formula is threshold_v2 / propose_all_v2, a NEW pre-registered
arm, never an in-place edit to what v1 already means.
"""

from __future__ import annotations

from typing import Optional

from alphaos.constants import TradeDirection
from alphaos.data.atr import atr_stop_price
from alphaos.lineage.hashing import stable_hash

THRESHOLD_V1 = "threshold_v1"
PROPOSE_ALL_V1 = "propose_all_v1"
BASELINE_RULE_VERSIONS = (THRESHOLD_V1, PROPOSE_ALL_V1)

# BASELINE spec item 1: "X = the historical median interest score of
# AI-proposed candidates, computed at build time, frozen as a literal."
# Computed 2026-07-09 against the real production DB (data/alphaos.db,
# read-only query): median interest_score among the n=15 real (non-mock,
# is_mock=0) candidates whose openai_evaluations.decision == 'propose',
# spanning 2026-07-01 to 2026-07-08 (this system's entire live history at
# build time). Sorted scores: [0.4277, 0.5056, 0.5105, 0.5361, 0.5406,
# 0.5568, 0.5828, 0.661, 0.6612, 0.7182, 0.7227, 0.7735, 0.7842, 0.7955,
# 0.8044] -- median = the 8th value, 0.661.
#
# Honest caveat: n=15 is well below this codebase's own MIN_TRUSTWORTHY_
# CLUSTERS floor (20, alphaos/stats/effective_n.py) -- a small, early
# sample. Frozen as a literal regardless, per spec: BASELINE's own purpose
# is to start accumulating PAIRED evidence now (every week without it is
# unrecoverable), not to wait for a large sample before picking a rule. A
# future recompute against a larger sample is threshold_v2, never an
# in-place edit to this literal.
THRESHOLD_V1_INTEREST_SCORE = 0.661

# BASELINE spec item 2: "Bracket construction: the identical live function
# (one sizing formula law)." The live AI path has no formulaic TARGET (the
# model picks it narratively) but DOES enforce one floor every PROPOSE must
# clear: settings.min_reward_risk (default 1.2, alphaos/config/settings.py).
# Reusing that SAME floor value as BASELINE's own deterministic target ratio
# is the only "identical live function" available for target construction --
# documented explicitly as a reversible decision (see the BASELINE build
# report), not silently assumed. The STOP formula has a real shared
# function to reuse instead: alphaos.data.atr.atr_stop_price(), the exact
# same formula alphaos.ai.openai_client._apply_atr_stop() applies on the
# live path.


def _build_bracket(
    row: dict, *, atr_14: Optional[float], min_reward_risk: float, max_holding_days_default: int,
) -> dict:
    """Shared by both rules. ``row``: typed public fields (symbol, direction,
    last_price) -- exactly the ScanContext-shaped inputs BASELINE is allowed
    to use (spec item 2, post-SC). Returns ``{"direction", "entry", "stop",
    "target", "max_holding_days"}`` on success, or ``{"unavailable_reason"}``
    when a bracket cannot be honestly constructed -- NEVER a fabricated
    partial bracket (unknown != safe)."""
    direction = row.get("direction") or TradeDirection.LONG.value
    entry = row.get("last_price")
    if entry is None:
        return {"unavailable_reason": "no_entry_price"}
    entry = float(entry)

    if atr_14 is None or atr_14 <= 0:
        return {"unavailable_reason": "no_atr_data"}

    stop = round(atr_stop_price(entry, atr_14, direction), 2)
    risk_per_share = abs(entry - stop)
    if not risk_per_share:
        return {"unavailable_reason": "zero_risk_per_share"}

    reward_per_share = min_reward_risk * risk_per_share
    is_short = direction == TradeDirection.SHORT.value
    target = round((entry - reward_per_share) if is_short else (entry + reward_per_share), 2)

    return {
        "direction": direction, "entry": entry, "stop": stop, "target": target,
        "max_holding_days": max_holding_days_default,
    }


def _input_sha(row: dict, rule_version: str, *, atr_14: Optional[float],
               min_reward_risk: float, max_holding_days_default: int) -> str:
    return stable_hash({
        "rule_version": rule_version,
        "symbol": row.get("symbol"),
        "direction": row.get("direction"),
        "interest_score": row.get("interest_score"),
        "last_price": row.get("last_price"),
        "atr_14": atr_14,
        "min_reward_risk": min_reward_risk,
        "max_holding_days_default": max_holding_days_default,
    })


def apply_threshold_v1(
    row: dict, *, atr_14: Optional[float], min_reward_risk: float, max_holding_days_default: int,
) -> dict:
    """PROPOSE iff interest_score >= THRESHOLD_V1_INTEREST_SCORE, else
    NO_ACTION (a real, meaningful rule decision -- distinct from UNAVAILABLE,
    a data gap). ``row.get("interest_score")`` missing entirely is treated as
    UNAVAILABLE, never silently coerced to a confident no_action (unknown !=
    safe)."""
    input_sha = _input_sha(
        row, THRESHOLD_V1, atr_14=atr_14, min_reward_risk=min_reward_risk,
        max_holding_days_default=max_holding_days_default,
    )
    base = {"rule_version": THRESHOLD_V1, "input_sha": input_sha}

    interest_score = row.get("interest_score")
    if interest_score is None:
        return {**base, "decision": "unavailable", "decision_reason": "no_interest_score",
                "direction": None, "entry": None, "stop": None, "target": None,
                "max_holding_days": None}

    if interest_score < THRESHOLD_V1_INTEREST_SCORE:
        return {**base, "decision": "no_action", "decision_reason": "below_threshold",
                "direction": None, "entry": None, "stop": None, "target": None,
                "max_holding_days": None}

    bracket = _build_bracket(
        row, atr_14=atr_14, min_reward_risk=min_reward_risk,
        max_holding_days_default=max_holding_days_default,
    )
    if "unavailable_reason" in bracket:
        return {**base, "decision": "unavailable", "decision_reason": bracket["unavailable_reason"],
                "direction": None, "entry": None, "stop": None, "target": None,
                "max_holding_days": None}
    return {**base, "decision": "propose", "decision_reason": "above_threshold", **bracket}


def apply_propose_all_v1(
    row: dict, *, atr_14: Optional[float], min_reward_risk: float, max_holding_days_default: int,
) -> dict:
    """PROPOSE every candidate this rule is called for, unconditionally --
    the only way it can resolve to anything else is UNAVAILABLE (a bracket
    genuinely could not be constructed), never a rule-driven no_action (this
    rule has no threshold logic to say no with)."""
    input_sha = _input_sha(
        row, PROPOSE_ALL_V1, atr_14=atr_14, min_reward_risk=min_reward_risk,
        max_holding_days_default=max_holding_days_default,
    )
    base = {"rule_version": PROPOSE_ALL_V1, "input_sha": input_sha}

    bracket = _build_bracket(
        row, atr_14=atr_14, min_reward_risk=min_reward_risk,
        max_holding_days_default=max_holding_days_default,
    )
    if "unavailable_reason" in bracket:
        return {**base, "decision": "unavailable", "decision_reason": bracket["unavailable_reason"],
                "direction": None, "entry": None, "stop": None, "target": None,
                "max_holding_days": None}
    return {**base, "decision": "propose", "decision_reason": "propose_all", **bracket}


# rule_version -> apply function, for the tracker's own loop (avoids an
# if/elif ladder that would need editing every time a new rule arm is added).
RULE_FUNCTIONS = {
    THRESHOLD_V1: apply_threshold_v1,
    PROPOSE_ALL_V1: apply_propose_all_v1,
}
