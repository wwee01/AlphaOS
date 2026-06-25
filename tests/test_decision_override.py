"""Gated labeller decision override (Roadmap 2.6): the combinator, the arming
gate (inert while mock), real-driver detection, the audit trail, and the safety
floor (gates/approval intact, data-integrity rejects never upgraded). Hermetic."""

from __future__ import annotations

from types import SimpleNamespace

from alphaos.ai.playbook_classifier import PlaybookClassification
from alphaos.constants import (
    OFFICIAL_LABELS,
    CatalystStatus,
    Decision,
    DecisionAdjustment,
    EnrichmentSource,
    LABEL_VERSION_V1,
    Last30DaysProvider,
    Last30DaysStatus,
)
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from conftest import make_settings

R, W, P = Decision.REJECT.value, Decision.WATCH.value, Decision.PROPOSE.value


def _orch(**over):
    base = {"LAST30DAYS_ENABLED": "true", "LAST30DAYS_PROVIDER": "mock"}
    base.update(over)
    return Orchestrator(settings=make_settings(**base), journal=JournalStore(":memory:"))


# --------------------------------------------------------------- combinator
def test_combinator_downgrade_only_when_not_armed():
    o = _orch()
    assert o._combine_decision(P, W, True, override_active=False) == W   # restrict
    assert o._combine_decision(W, P, True, override_active=False) == W   # NO upgrade
    assert o._combine_decision(P, R, True, override_active=False) == R
    o.close()


def test_combinator_symmetric_when_armed():
    o = _orch()
    assert o._combine_decision(W, P, True, override_active=True) == P    # UPGRADE
    assert o._combine_decision(P, W, True, override_active=True) == W    # downgrade
    assert o._combine_decision(R, P, True, override_active=True) == P    # upgrade w/ levels
    o.close()


def test_combinator_cannot_upgrade_without_valid_levels():
    o = _orch()
    # data-integrity reject (no levels) can NEVER be upgraded by narrative
    assert o._combine_decision(R, P, False, override_active=True) == R
    assert o._combine_decision(W, P, False, override_active=True) == W
    # downgrade is still allowed without levels
    assert o._combine_decision(P, R, False, override_active=True) == R
    o.close()


# ------------------------------------------------------------- arming gate
def test_override_not_armed_when_flag_off():
    o = _orch(LABELLER_DECISION_OVERRIDE_ENABLED="false")
    assert o._override_armed() is False
    o.close()


def test_override_inert_while_mock_even_if_enabled():
    o = _orch(LABELLER_DECISION_OVERRIDE_ENABLED="true")   # mock mode
    assert o._override_armed() is False
    assert o.system_health()["labeller_decision_override"] == "enabled_inert_while_mock"
    o.close()


def test_override_armed_only_with_real_ai_and_flag():
    o = Orchestrator(
        settings=make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="sk-test",
                               ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s",
                               LABELLER_DECISION_OVERRIDE_ENABLED="true"),
        journal=JournalStore(":memory:"),
    )
    assert o._override_armed() is True
    o.close()


# ------------------------------------------------------- real-driver gate
def test_real_driver_rejects_mock_signals():
    o = _orch()
    catalyst = SimpleNamespace(catalyst_status=CatalystStatus.CONFIRMED.value,
                               catalyst_type="earnings", enrichment_source=EnrichmentSource.MOCK.value)
    last30 = SimpleNamespace(last30days_status=Last30DaysStatus.AVAILABLE.value,
                             provider=Last30DaysProvider.MOCK.value, sentiment_label="bullish")
    has, driver, _ = o._real_decision_driver(catalyst, last30)
    assert has is False and driver == ""
    o.close()


def test_real_driver_accepts_live_catalyst():
    o = _orch()
    catalyst = SimpleNamespace(catalyst_status=CatalystStatus.CONFIRMED.value,
                               catalyst_type="earnings", enrichment_source=EnrichmentSource.ALPACA.value)
    has, driver, detail = o._real_decision_driver(catalyst, None)
    assert has is True and "catalyst:confirmed" in driver and "catalyst" in detail
    o.close()


def test_real_driver_accepts_live_sentiment_but_not_unknown():
    o = _orch()
    live_known = SimpleNamespace(last30days_status=Last30DaysStatus.AVAILABLE.value,
                                 provider=Last30DaysProvider.CLI.value, sentiment_label="bearish")
    live_unknown = SimpleNamespace(last30days_status=Last30DaysStatus.AVAILABLE.value,
                                   provider=Last30DaysProvider.CLI.value, sentiment_label="unknown")
    assert o._real_decision_driver(None, live_known)[0] is True
    assert o._real_decision_driver(None, live_unknown)[0] is False
    o.close()


# ------------------------------------------------------------- flow / audit
def test_scan_records_one_adjustment_per_labelled_candidate():
    o = _orch()
    summ = o.run_scan_once()
    rows = o.journal.query("SELECT * FROM decision_adjustments")
    assert len(rows) == summ.labelled
    for r in rows:
        assert r["eval_decision"] and r["final_decision"] and r["adjustment"]
        assert r["override_enabled"] == 0          # default off
    o.close()


def test_enabling_override_in_mock_changes_nothing():
    """Inert-while-mock: turning the flag on must not add a single proposal."""
    off = _orch(LABELLER_DECISION_OVERRIDE_ENABLED="false").run_scan_once().proposed
    on = _orch(LABELLER_DECISION_OVERRIDE_ENABLED="true").run_scan_once().proposed
    assert on == off


def _propose_label(packet):
    from alphaos.util.ids import new_id
    return PlaybookClassification(
        label_id=new_id("lbl"), candidate_id=packet.candidate_id, symbol=packet.symbol,
        primary_label="Momentum", secondary_labels=[], candidate_tags=[], risk_tags=[],
        direction="long", label_decision=Decision.PROPOSE.value, confidence=0.9,
        reason_for_label="forced propose", thesis_stub="", invalidation="", main_risk="",
        missing_context=[], suggested_new_tags=[], label_version=LABEL_VERSION_V1,
        label_source="mock", validation_status="passed", model="mock", is_mock=True, raw={},
    )


def test_armed_upgrade_promotes_watch_to_propose_with_audit(monkeypatch):
    """With arming + a real driver forced, a PROPOSE label upgrades a WATCH eval —
    and the move is journaled as 'upgraded' with its driver. Still no execution."""
    o = _orch()
    monkeypatch.setattr(o.labeller, "classify", _propose_label)        # force PROPOSE labels
    monkeypatch.setattr(o, "_override_armed", lambda: True)            # arm
    monkeypatch.setattr(o, "_real_decision_driver",
                        lambda c, l: (True, "last30days:bullish", {"last30days": {"sentiment": "bullish"}}))
    summ = o.run_scan_once()

    assert summ.decision_upgraded > 0
    ups = o.journal.query("SELECT * FROM decision_adjustments WHERE adjustment = ?",
                          (DecisionAdjustment.UPGRADED.value,))
    assert ups
    for r in ups:
        assert r["eval_decision"] == Decision.WATCH.value
        assert r["final_decision"] == Decision.PROPOSE.value
        assert r["override_armed"] == 1
        assert r["driver"] == "last30days:bullish"          # driver recorded for learning
    tagged = o.journal.query("SELECT * FROM candidates WHERE decision_adjustment = ?",
                             (DecisionAdjustment.UPGRADED.value,))
    assert tagged and all(t["decision_adjustment_reason"] for t in tagged)

    # SAFETY: an upgraded proposal still passed through gates + manual approval; it
    # never auto-executed and never bypassed approval.
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_rows("paper_fills") == 0
    assert o.journal.count_open_positions() == 0
    assert o.journal.count_rows("approvals") == 0
    # labels stay official
    labels = o.journal.query("SELECT primary_label FROM candidate_labels")
    assert labels and all(l["primary_label"] in OFFICIAL_LABELS for l in labels)
    o.close()


def test_armed_upgrade_increases_proposals_vs_downgrade_only(monkeypatch):
    """Concrete proof the upgrade path can CREATE a proposal the downgrade-only
    path would not — the capability the user asked for."""
    base = _orch()
    monkeypatch.setattr(base.labeller, "classify", _propose_label)
    proposed_floor = base.run_scan_once().proposed     # downgrade-only: PROPOSE label can't lift WATCH
    base.close()

    armed = _orch()
    monkeypatch.setattr(armed.labeller, "classify", _propose_label)
    monkeypatch.setattr(armed, "_override_armed", lambda: True)
    monkeypatch.setattr(armed, "_real_decision_driver", lambda c, l: (True, "catalyst:confirmed:earnings", {}))
    proposed_armed = armed.run_scan_once().proposed
    armed.close()

    assert proposed_armed > proposed_floor


def test_real_money_unreachable_with_override_enabled():
    o = _orch(LABELLER_DECISION_OVERRIDE_ENABLED="true")
    assert o.system_health()["real_money_trading"] == "unreachable"
    o.close()


def test_dashboard_readonly_with_adjustments(monkeypatch):
    import sys

    sys.path.insert(0, "tests")
    from test_approval_execution import _fake_st

    from alphaos.dashboard import streamlit_app

    o = _orch()
    monkeypatch.setattr(o.labeller, "classify", _propose_label)
    monkeypatch.setattr(o, "_override_armed", lambda: True)
    monkeypatch.setattr(o, "_real_decision_driver", lambda c, l: (True, "forced", {}))
    o.run_scan_once()
    j = o.journal
    assert j.count_rows("decision_adjustments") > 0
    tabs = [r["name"] for r in j.query("SELECT name FROM sqlite_master WHERE type='table'")]
    before = sum(j.count_rows(t) for t in tabs)
    monkeypatch.setattr(streamlit_app, "st", _fake_st())
    streamlit_app.main(orch=o)
    after = sum(j.count_rows(t) for t in tabs)
    assert after == before
    o.close()
