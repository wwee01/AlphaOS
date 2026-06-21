"""Day-trade experiment is gated and separated from swing (test #8)."""

from __future__ import annotations

import pytest

from alphaos.approval import ApprovalEngine
from alphaos.constants import ReasonCode, Strategy
from alphaos.strategy.daytrade_experiment import DaytradeExperiment
from conftest import make_settings, make_proposal
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator


def test_daytrade_disabled_by_default():
    dt = DaytradeExperiment()
    assert dt.is_enabled is False
    with pytest.raises(RuntimeError):
        dt.build_proposal(None, None)


def test_auto_mode_cannot_approve_daytrade(journal):
    s = make_settings(APPROVAL_MODE="auto", REQUIRE_MANUAL_APPROVAL="false")
    eng = ApprovalEngine(s, journal)
    prop = make_proposal(strategy=Strategy.DAYTRADE_EXPERIMENT.value)
    outcome = eng.consider(prop, risk_ok=True, freshness_ok=True)
    assert outcome.approved is False
    assert outcome.reason == ReasonCode.DAYTRADE_GATED.value


def test_books_are_separated_by_strategy():
    orch = Orchestrator(settings=make_settings(), journal=JournalStore(":memory:"))
    orch.seed_demo()  # opens a SWING position
    assert orch.journal.count_open_positions(strategy=Strategy.SWING.value) == 1
    assert orch.journal.count_open_positions(strategy=Strategy.DAYTRADE_EXPERIMENT.value) == 0
    orch.close()
