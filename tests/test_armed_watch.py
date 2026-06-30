"""Armed Watch + labeller reasoning (Roadmap 2.8 Part A/B): an armed override that
stays WATCH is recorded as a near-action watchlist item (NOT a reject), with the
labeller's reasoning for why it didn't upgrade. Hermetic."""

from __future__ import annotations

from alphaos.ai.playbook_classifier import PlaybookClassification
from alphaos.constants import ArmedWatchReason, ArmingClassification, Decision, LABEL_VERSION_V1
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from conftest import make_settings
from alphaos.util.ids import new_id


def _label(decision, readiness="near_action"):
    def _make(p):
        return PlaybookClassification(
            label_id=new_id("lbl"), candidate_id=p.candidate_id, symbol=p.symbol,
            primary_label="Momentum", secondary_labels=[], candidate_tags=[], risk_tags=[],
            direction="long", label_decision=decision, confidence=0.8,
            reason_for_label="setup not clean enough yet", thesis_stub="", invalidation="",
            main_risk="", missing_context=[], suggested_new_tags=[], label_version=LABEL_VERSION_V1,
            label_source="mock", validation_status="passed", model="mock", is_mock=True, raw={},
            missing_conditions=["clear_entry_trigger"], upgrade_blockers=["mixed_evidence"],
            proposal_readiness=readiness, what_would_upgrade="a confirmed breakout",
        )
    return _make


def _scan_orch(monkeypatch, label_decision, has_driver=True,
               arming=ArmingClassification.NORMAL_DRIVER.value):
    o = Orchestrator(settings=make_settings(
        LAST30DAYS_ENABLED="true", LABELLER_DECISION_OVERRIDE_ENABLED="true",
        INTEREST_SCAN_TOP_N="6", MAX_CANDIDATES_TO_AI="6"), journal=JournalStore(":memory:"))
    monkeypatch.setattr(o, "_override_armed", lambda: True)
    detail = {"last30days": {"arming_classification": arming}} if has_driver else {}
    monkeypatch.setattr(o, "_real_decision_driver",
                        lambda c, l, d, p=None: (has_driver, "drv" if has_driver else "", detail))
    monkeypatch.setattr(o.labeller, "classify", _label(label_decision))
    _orig = o.openai.evaluate

    def watch_eval(cand, snap, freshness_status="usable"):
        ev = _orig(cand, snap, freshness_status)
        ev.decision = Decision.WATCH.value     # force eval=watch so there's something to (not) upgrade
        return ev

    monkeypatch.setattr(o.openai, "evaluate", watch_eval)
    return o


def test_armed_watch_created_when_armed_but_label_stays_watch(monkeypatch):
    o = _scan_orch(monkeypatch, Decision.WATCH.value)
    summ = o.run_scan_once()
    assert summ.armed_watch > 0
    rows = o.journal.query("SELECT * FROM decision_adjustments WHERE armed_watch = 1")
    assert rows
    for r in rows:
        assert r["eval_decision"] == Decision.WATCH.value
        assert r["label_decision"] == Decision.WATCH.value
        assert r["final_decision"] == Decision.WATCH.value
        assert r["override_armed"] == 1
        assert r["armed_watch_reason"] == ArmedWatchReason.LABELLER_DID_NOT_UPGRADE.value
    o.close()


def test_armed_watch_not_created_when_upgrade_happens(monkeypatch):
    o = _scan_orch(monkeypatch, Decision.PROPOSE.value)     # label proposes -> upgrade
    summ = o.run_scan_once()
    assert summ.decision_upgraded > 0
    assert summ.armed_watch == 0
    assert o.journal.count_rows("decision_adjustments", "armed_watch = 1") == 0
    o.close()


def test_no_armed_watch_when_not_armed(monkeypatch):
    o = _scan_orch(monkeypatch, Decision.WATCH.value, has_driver=False)  # no driver -> not armed
    summ = o.run_scan_once()
    assert summ.armed_watch == 0
    o.close()


def test_armed_watch_stores_labeller_reasoning(monkeypatch):
    o = _scan_orch(monkeypatch, Decision.WATCH.value)
    o.run_scan_once()
    r = o.journal.one("SELECT * FROM decision_adjustments WHERE armed_watch = 1 LIMIT 1")
    assert r["labeller_reason"]
    assert r["proposal_readiness"] == "near_action"
    import json
    assert "clear_entry_trigger" in json.loads(r["labeller_missing_conditions_json"])
    assert "mixed_evidence" in json.loads(r["labeller_upgrade_blockers_json"])
    o.close()


def test_high_risk_armed_watch_flagged(monkeypatch):
    o = _scan_orch(monkeypatch, Decision.WATCH.value,
                   arming=ArmingClassification.HIGH_RISK_NARRATIVE.value)
    o.run_scan_once()
    rows = o.journal.query("SELECT * FROM decision_adjustments WHERE armed_watch = 1")
    assert rows and all(r["arming_classification"] == ArmingClassification.HIGH_RISK_NARRATIVE.value for r in rows)
    o.close()


def test_armed_watch_tagged_on_candidate_not_a_reject(monkeypatch):
    o = _scan_orch(monkeypatch, Decision.WATCH.value)
    o.run_scan_once()
    cands = o.journal.query("SELECT * FROM candidates WHERE armed_watch = 1")
    assert cands
    # armed-watch candidates are NOT rejects
    assert all(c["status"] != "rejected" for c in cands)
    o.close()


def test_labeller_reasoning_does_not_change_decision():
    # Part B fields are advisory only — a plain mock scan still labels + decides as before.
    o = Orchestrator(settings=make_settings(), journal=JournalStore(":memory:"))
    summ = o.run_scan_once()
    assert summ.labelled > 0
    labels = o.journal.query("SELECT proposal_readiness FROM candidate_labels")
    assert labels and all(l["proposal_readiness"] for l in labels)   # populated, advisory
    o.close()
