"""Same-day exits classify correctly (test #7)."""

from __future__ import annotations

from datetime import timedelta

from alphaos.constants import ExitClassification
from alphaos.execution import exit_rules
from alphaos.util import timeutils
from conftest import make_settings
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator


def test_classify_each_category():
    assert exit_rules.classify_exit("stop") == ExitClassification.RISK_CONTROL
    assert exit_rules.classify_exit("stop_loss") == ExitClassification.RISK_CONTROL
    assert exit_rules.classify_exit("target") == ExitClassification.PROFIT_TAKING
    assert exit_rules.classify_exit("thesis_invalidation") == ExitClassification.THESIS_INVALIDATION
    assert exit_rules.classify_exit("manual") == ExitClassification.MANUAL_USER
    assert exit_rules.classify_exit("daytrade") == ExitClassification.EXPERIMENTAL_DAYTRADE
    assert exit_rules.classify_exit("data_quality") == ExitClassification.ERROR_DATA_QUALITY


def test_time_expiry_classified_by_outcome():
    assert exit_rules.classify_exit("time_expiry", pnl=10) == ExitClassification.PROFIT_TAKING
    assert exit_rules.classify_exit("time_expiry", pnl=-10) == ExitClassification.RISK_CONTROL


def test_is_same_day():
    today = timeutils.market_date().isoformat()
    yesterday = (timeutils.market_date() - timedelta(days=1)).isoformat()
    assert exit_rules.is_same_day_exit(today) is True
    assert exit_rules.is_same_day_exit(yesterday) is False
    assert exit_rules.is_same_day_exit(None) is False


def test_integration_same_day_stop_exit_is_risk_control():
    orch = Orchestrator(settings=make_settings(), journal=JournalStore(":memory:"))
    orch.seed_demo()
    pos = orch.journal.open_positions()[0]
    ex = orch.positions.close_position(pos["position_id"], exit_price=1.0, exit_reason="stop")
    assert ex["classification"] == ExitClassification.RISK_CONTROL.value
    assert ex["is_same_day"] is True
    outcome = orch.journal.one("SELECT * FROM trade_outcomes WHERE position_id = ?", (pos["position_id"],))
    assert outcome["is_same_day"] == 1
    assert outcome["classification"] == ExitClassification.RISK_CONTROL.value
    orch.close()
