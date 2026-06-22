"""Configurable stop/target sizing.

Stop distance and target reward:risk are driven by settings (so the mock
baseline is tunable and reachable), and a configured minimum reward:risk clamps
any proposal (the guard that also bounds live OpenAI output)."""

from __future__ import annotations

import pytest

from alphaos.ai.openai_client import OpenAIClient
from alphaos.config.settings import SettingsError
from alphaos.constants import Decision, ReasonCode
from conftest import make_settings


def _cand(direction="long", momentum=0.8):
    return {"symbol": "TEST", "direction": direction, "momentum_score": momentum,
            "candidate_id": "cand_test"}


def _snap(price=100.0):
    return {"last_price": price}


def test_default_target_is_reachable_1_5r():
    eng = OpenAIClient(make_settings())  # defaults: 3% stop, 1.5R
    ev = eng.evaluate(_cand("long"), _snap(100.0))
    assert ev.decision == Decision.PROPOSE.value
    assert ev.stop == 97.0           # 3% below entry
    assert ev.target == 104.5        # 3% * 1.5 = 4.5% above entry (was 6%)
    assert ev.expected_r == 1.5


def test_short_geometry_and_custom_config():
    eng = OpenAIClient(make_settings(STOP_LOSS_PCT="0.02", TARGET_REWARD_RISK="2.0"))
    longev = eng.evaluate(_cand("long"), _snap(100.0))
    assert (longev.stop, longev.target, longev.expected_r) == (98.0, 104.0, 2.0)
    shortev = eng.evaluate(_cand("short"), _snap(100.0))
    assert (shortev.stop, shortev.target, shortev.expected_r) == (102.0, 96.0, 2.0)


def test_min_reward_risk_guard_rejects_low_rr():
    # Target RR below the floor -> any proposal is downgraded to reject.
    eng = OpenAIClient(make_settings(TARGET_REWARD_RISK="1.0", MIN_REWARD_RISK="1.5"))
    ev = eng.evaluate(_cand("long"), _snap(100.0))
    assert ev.decision == Decision.REJECT.value
    assert ReasonCode.REWARD_RISK_TOO_LOW.value in ev.risk_flags


def test_invalid_sizing_config_fails_fast():
    with pytest.raises(SettingsError):
        make_settings(STOP_LOSS_PCT="0.8")        # >= 0.5
    with pytest.raises(SettingsError):
        make_settings(TARGET_REWARD_RISK="0")     # must be > 0
