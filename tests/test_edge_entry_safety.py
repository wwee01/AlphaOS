"""P0.3/P0.4: offline entry capacity and bounded pending retries."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from alphaos.orchestrator import ScanSummary
from alphaos.risk import account_exposure as exposure_module
from alphaos.risk.account_exposure import read_exposure as real_read_exposure
from conftest import inject_pending_proposal
from test_account_exposure import _account, _order, _position
from test_entry_deferral import paper_entry as _paper_entry
from test_proposal_ttl_flow import _propose_evaluation


paper_entry = _paper_entry


def transient(*args):
    error = getattr(exposure_module, 'ExposureUnreconciled', exposure_module.ExposureUnavailable)
    raise error('broker position is not reconciled to the local ledger')


def test_symbol_occupancy_includes_broker_position_and_pending_entry():
    state = exposure_module.summarize_exposure(
        _account(), [_position()], [_order()], {'entry1': {'symbol': 'MSFT'}})
    assert set(state['occupied_symbols']) == {'AAPL', 'MSFT'}


def test_final_symbol_cap_refuses_before_approval(paper_entry):
    orch, _, exposure, attempted = paper_entry
    exposure['occupied_symbols'] = ['AAPL']
    pid, _ = inject_pending_proposal(orch)
    ok, _ = orch.approve_proposal(pid)
    assert not ok and not attempted
    assert orch.journal.proposal_by_id(pid)['status'] == 'blocked'
    assert orch.journal.count_rows('approvals') == 0


def test_initial_unreconciled_proposal_remains_pending(paper_entry, monkeypatch):
    orch, _, _, attempted = paper_entry
    monkeypatch.setattr(exposure_module, 'read_exposure', transient)
    cand, evaluation, summary = _propose_evaluation(orch)
    orch._handle_proposal(cand, evaluation, summary)
    row = orch.journal.one('SELECT * FROM trade_proposals WHERE candidate_id=?', (cand['candidate_id'],))
    assert row['status'] == 'pending_approval'
    assert summary.entry_deferred == 1 and not attempted
    assert orch.journal.count_rows('approvals') == 0


def test_monitor_unready_stays_pending_without_approval(paper_entry, monkeypatch):
    orch, _, _, attempted = paper_entry
    monkeypatch.setattr('alphaos.scheduler.monitor_health.monitor_health', lambda *a: {
        'entry_ready': False, 'detail': 'monitor has not reconciled yet'})
    pid, _ = inject_pending_proposal(orch)
    ok, message = orch.approve_proposal(pid)
    assert not ok and 'deferred' in message
    assert orch.journal.proposal_by_id(pid)['status'] == 'pending_approval'
    assert not attempted and orch.journal.count_rows('approvals') == 0


def test_next_scan_retry_rechecks_then_submits_once(paper_entry, monkeypatch):
    orch, _, exposure, attempted = paper_entry
    monkeypatch.setattr(exposure_module, 'read_exposure', transient)
    cand, evaluation, summary = _propose_evaluation(orch)
    orch._handle_proposal(cand, evaluation, summary)
    row = orch.journal.one('SELECT * FROM trade_proposals WHERE candidate_id=?', (cand['candidate_id'],))
    original_expiry = row['proposal_expires_at_utc']
    monkeypatch.setattr(exposure_module, 'read_exposure', lambda *a: dict(exposure))
    retry = ScanSummary(scan_id="retry-scan")
    orch._retry_deferred_entries(retry, unattended=False)
    assert attempted == [row['proposal_id']] and retry.auto_submitted == 1
    assert orch.journal.proposal_by_id(row['proposal_id'])['proposal_expires_at_utc'] == original_expiry
    orch._retry_deferred_entries(ScanSummary(scan_id="retry-scan"), unattended=False)
    assert len(attempted) == 1 and orch.journal.count_rows('approvals') == 1


def deferred_proposal(orch, monkeypatch):
    monkeypatch.setattr(exposure_module, "read_exposure", transient)
    cand, evaluation, summary = _propose_evaluation(orch)
    orch._handle_proposal(cand, evaluation, summary)
    return orch.journal.one("SELECT * FROM trade_proposals WHERE candidate_id=?", (cand["candidate_id"],))


def test_next_actual_scan_retries_pending_without_new_candidate(paper_entry, monkeypatch):
    orch, _, exposure, attempted = paper_entry
    row = deferred_proposal(orch, monkeypatch)
    monkeypatch.setattr(exposure_module, "read_exposure", lambda *a: dict(exposure))
    monkeypatch.setattr(orch.scanner, "scan", lambda **kw: SimpleNamespace(scan_id="empty-next", candidates=[]))
    summary = orch.run_scan_once()
    assert attempted == [row["proposal_id"]] and summary.auto_submitted == 1


def test_capture_only_scan_never_retries(paper_entry, monkeypatch):
    orch, _, exposure, attempted = paper_entry
    row = deferred_proposal(orch, monkeypatch)
    monkeypatch.setattr(exposure_module, "read_exposure", lambda *a: dict(exposure))
    monkeypatch.setattr(orch.scanner, "scan", lambda **kw: SimpleNamespace(scan_id="empty-degraded", candidates=[]))
    orch.run_scan_once(ai_degraded=True)
    assert not attempted and orch.journal.proposal_by_id(row["proposal_id"])["status"] == "pending_approval"


def test_campaign_retry_excludes_newly_deferred_current_batch(paper_entry, monkeypatch):
    orch, _, _, _ = paper_entry
    row = deferred_proposal(orch, monkeypatch)
    orch.journal.conn.execute("UPDATE trade_proposals SET scan_batch_id='current' WHERE proposal_id=?", (row["proposal_id"],))
    orch.journal.conn.commit()
    seen = []
    monkeypatch.setattr(orch, "approve_proposal", lambda pid, **kw: (seen.append(pid) or False, "waiting"))
    orch._retry_deferred_entries(ScanSummary("current"), unattended=True, exclude_batch_id="current")
    assert seen == []
    orch._retry_deferred_entries(ScanSummary("next"), unattended=True, exclude_batch_id="next")
    assert seen == [row["proposal_id"]]
    assert orch.journal.proposal_by_id(row["proposal_id"])["proposal_expires_at_utc"] == row["proposal_expires_at_utc"]


def test_retry_expiry_is_original_and_terminal(paper_entry, monkeypatch):
    orch, clock, exposure, attempted = paper_entry
    row = deferred_proposal(orch, monkeypatch)
    clock[0] += timedelta(hours=2)
    monkeypatch.setattr(exposure_module, "read_exposure", lambda *a: pytest.fail("expired retry read exposure"))
    orch._retry_deferred_entries(ScanSummary(scan_id="expired"), unattended=False)
    current = orch.journal.proposal_by_id(row["proposal_id"])
    assert current["status"] == "expired" and current["proposal_expires_at_utc"] == row["proposal_expires_at_utc"]
    assert not attempted and orch.journal.count_rows("approvals") == 0


@pytest.mark.parametrize("mutation", ["manual", "cap", "high_risk", "rejected", "superseded"])
def test_retry_cannot_inherit_stale_approval_authority(paper_entry, monkeypatch, mutation):
    orch, _, exposure, attempted = paper_entry
    row = deferred_proposal(orch, monkeypatch)
    if mutation == "manual":
        orch.settings = replace(orch.settings, approval_mode="manual", require_manual_approval=True)
    elif mutation == "cap":
        orch.settings = replace(orch.settings, max_auto_approvals_per_day=0)
    elif mutation == "high_risk":
        orch.journal.conn.execute("UPDATE trade_proposals SET arming_classification='high_risk_narrative' WHERE proposal_id=?", (row["proposal_id"],))
        orch.journal.conn.commit()
        orch.settings = replace(orch.settings, last30days_high_risk_narrative_manual_only=True)
    else:
        orch._set_proposal_status(row["proposal_id"], mutation)
    orch.approvals.settings = orch.settings
    monkeypatch.setattr(exposure_module, "read_exposure", lambda *a: pytest.fail("ineligible retry read exposure"))
    orch._retry_deferred_entries(ScanSummary(scan_id="ineligible"), unattended=False)
    assert not attempted and orch.journal.count_rows("approvals") == 0


def test_manual_deferral_does_not_schedule_automatic_retry(paper_entry, monkeypatch):
    orch, _, exposure, attempted = paper_entry
    pid, _ = inject_pending_proposal(orch)
    monkeypatch.setattr(exposure_module, "read_exposure", transient)
    assert not orch.approve_proposal(pid)[0]
    monkeypatch.setattr(exposure_module, "read_exposure", lambda *a: dict(exposure))
    orch._retry_deferred_entries(ScanSummary(scan_id="manual-only"), unattended=True)
    assert not attempted and orch.journal.count_rows("approvals") == 0


@pytest.mark.parametrize("unreconciled", [False, True])
def test_initial_high_risk_narrative_never_obtains_automatic_authority(paper_entry, monkeypatch, unreconciled):
    orch, _, _, attempted = paper_entry
    orch.settings = replace(orch.settings, last30days_high_risk_narrative_manual_only=True)
    orch.approvals.settings = orch.settings
    if unreconciled:
        monkeypatch.setattr(exposure_module, "read_exposure", transient)
    monkeypatch.setattr(orch.approvals, "consider", lambda *a, **kw: pytest.fail("manual-only considered automatically"))
    cand, evaluation, summary = _propose_evaluation(orch)
    cand.arming_classification = "high_risk_narrative"
    orch._handle_proposal(cand, evaluation, summary, unattended=True)
    row = orch.journal.one("SELECT * FROM trade_proposals WHERE candidate_id=?", (cand["candidate_id"],))
    assert row["status"] == "pending_approval"
    assert row["arming_classification"] == "high_risk_narrative"
    assert not orch.journal.one("SELECT 1 FROM system_events WHERE category='entry_retry_pending'")
    orch._retry_deferred_entries(ScanSummary(scan_id="manual-only"), unattended=True)
    assert not attempted and orch.journal.count_rows("approvals") == 0


@pytest.mark.parametrize("issue", ["protection_blocking", "execution_incident", "health_unavailable", "monitor_fused"])
def test_monitor_hard_faults_remain_terminal(paper_entry, monkeypatch, issue):
    orch, _, _, attempted = paper_entry
    monkeypatch.setattr("alphaos.scheduler.monitor_health.monitor_health", lambda *a: {
        "entry_ready": False, "issue_codes": [issue], "protection_blocking": issue == "protection_blocking"})
    pid, _ = inject_pending_proposal(orch)
    assert not orch.approve_proposal(pid)[0]
    assert orch.journal.proposal_by_id(pid)["status"] == "blocked"
    assert not attempted and orch.journal.count_rows("approvals") == 0


def test_unknown_exposure_stays_terminal(paper_entry, monkeypatch):
    orch, _, _, attempted = paper_entry
    def unknown(*a):
        raise exposure_module.ExposureUnavailable("unrecognized order or protection fault")
    monkeypatch.setattr(exposure_module, "read_exposure", unknown)
    cand, evaluation, summary = _propose_evaluation(orch)
    orch._handle_proposal(cand, evaluation, summary)
    row = orch.journal.one("SELECT * FROM trade_proposals WHERE candidate_id=?", (cand["candidate_id"],))
    assert row["status"] == "blocked" and summary.risk_blocked == 1
    assert not attempted and orch.journal.count_rows("approvals") == 0


def test_transient_exposure_does_not_mask_invalid_stop(paper_entry, monkeypatch):
    orch, _, _, attempted = paper_entry
    monkeypatch.setattr(exposure_module, "read_exposure", transient)
    cand, evaluation, summary = _propose_evaluation(orch)
    evaluation.stop = evaluation.entry
    orch._handle_proposal(cand, evaluation, summary)
    row = orch.journal.one("SELECT * FROM trade_proposals WHERE candidate_id=?", (cand["candidate_id"],))
    assert row["status"] == "blocked" and summary.risk_blocked == 1
    assert not attempted


@pytest.mark.parametrize("last_state", ["occupied", "transient"])
def test_exposure_changes_at_final_locked_gate(paper_entry, monkeypatch, last_state):
    orch, _, exposure, attempted = paper_entry
    calls = []
    def changing(*a):
        calls.append(1)
        if len(calls) > 1:
            if last_state == "transient":
                return transient()
            return dict(exposure, occupied_symbols=["AAPL"])
        return dict(exposure)
    monkeypatch.setattr(exposure_module, "read_exposure", changing)
    pid, _ = inject_pending_proposal(orch)
    assert not orch.approve_proposal(pid)[0]
    assert len(calls) == 2 and not attempted and orch.journal.count_rows("approvals") == 0
    assert orch.journal.proposal_by_id(pid)["status"] == ("blocked" if last_state == "occupied" else "pending_approval")


def test_other_symbol_is_not_blocked_by_symbol_occupancy(paper_entry):
    orch, _, exposure, attempted = paper_entry
    exposure["occupied_symbols"] = ["MSFT"]
    pid, _ = inject_pending_proposal(orch)
    assert orch.approve_proposal(pid)[0] and attempted == [pid]


@pytest.mark.parametrize("state,occupied", [("accepted", True), ("partially_filled", True), ("cancelled", False), ("filled", False)])
def test_local_working_entry_reserves_symbol_until_reconciled(state, occupied):
    broker = SimpleNamespace(get_account=_account, list_positions=lambda: [], list_open_orders=lambda: [])
    journal = SimpleNamespace(
        open_positions=lambda: [], count_open_positions=lambda: 0,
        query=lambda sql: ([{"broker_order_id": "entry1", "symbol": "AAPL", "state": state}]
                           if "SELECT broker_order_id" in sql else []))
    result = exposure_module.read_exposure(broker, journal)
    assert ("AAPL" in result["occupied_symbols"]) is occupied


@pytest.mark.parametrize("phase", ["prepared", "submitting", "unknown", "reconciling"])
def test_uncertain_submission_is_hard_not_transient(paper_entry, phase):
    orch, _, _, _ = paper_entry
    orch.journal.insert("paper_orders", {
        "order_id": "uncertain", "client_order_id": "unresolved", "symbol": "XYZ",
        "order_type": "bracket", "execution_source": "alpaca_paper", "state": "submitted",
        "raw_request_json": {"purpose": "paper_entry", "phase": phase},
    })
    broker = SimpleNamespace(get_account=lambda: pytest.fail("ambiguous intent read broker"))
    with pytest.raises(exposure_module.ExposureUnavailable) as error:
        real_read_exposure(broker, orch.journal)
    assert not isinstance(error.value, exposure_module.ExposureUnreconciled)


@pytest.mark.parametrize("fault", [None, "unknown", "identity", "quantity", "protection", "closed_origin"])
def test_only_verified_protected_fill_may_defer_missing_local_origin(paper_entry, fault):
    orch, _, _, _ = paper_entry
    row = {"order_id": "fresh-fill", "proposal_id": "p-fill", "client_order_id": "p-fill",
           "broker_order_id": "broker-fill", "symbol": "AAPL", "direction": "long",
           "execution_source": "alpaca_paper", "order_type": "bracket", "order_class": "bracket",
           "state": "accepted", "qty": 100, "limit_price": 100, "stop_loss_price": 95,
           "take_profit_price": 110, "time_in_force": "gtc",
           "raw_request_json": {"purpose": "paper_entry", "phase": "acknowledged"}}
    if fault != "unknown":
        orch.journal.insert("paper_orders", row)
    if fault == "closed_origin":
        orch.journal.insert("positions", {"position_id": "old", "order_id": "fresh-fill", "symbol": "AAPL", "status": "closed"})
    parent = {"broker_order_id": "broker-fill", "client_order_id": "p-fill", "symbol": "AAPL",
              "side": "buy", "order_class": "bracket", "qty": 100, "filled_qty": 100,
              "limit_price": 100, "time_in_force": "gtc", "status": "filled", "legs": [
                  {"broker_order_id": "sl", "symbol": "AAPL", "side": "sell", "role": "stop_loss", "qty": 100,
                   "filled_qty": 0, "stop_price": 95, "time_in_force": "gtc", "status": "new"},
                  {"broker_order_id": "tp", "symbol": "AAPL", "side": "sell", "role": "take_profit", "qty": 100,
                   "filled_qty": 0, "limit_price": 110, "time_in_force": "gtc", "status": "new"}]}
    if fault == "identity":
        parent["client_order_id"] = "external"
    if fault == "quantity":
        parent["filled_qty"] = 99
    if fault == "protection":
        parent["legs"][0]["status"] = "canceled"
    broker = SimpleNamespace(get_account=_account, list_positions=lambda: [_position()], list_open_orders=lambda: [], get_order=lambda oid: parent)
    with pytest.raises(exposure_module.ExposureUnavailable) as error:
        real_read_exposure(broker, orch.journal)
    assert isinstance(error.value, exposure_module.ExposureUnreconciled) is (fault is None)
