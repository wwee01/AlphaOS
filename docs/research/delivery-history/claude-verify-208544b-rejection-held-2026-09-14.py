"""Round-four verification probes on 208544b: N3 rejection proof (real alpaca-py HTTP path) and N4 held-symbol recovery."""
import sys, os, json
sys.path.insert(0, os.path.join(os.environ["ALPHAOS_REPO"], "tests"))
from copy import deepcopy
from hashlib import sha256
import pytest
from requests import ReadTimeout

from alphaos.broker.alpaca_client import AlpacaClient
from alphaos.execution import ledger_ops, protection_watchdog
from alphaos.execution.entry_intent_resolution import resolve_entry_intent
from alphaos.execution.entry_submission import pending_entry_intent
from alphaos.journal.journal_store import JournalStore
from conftest import make_proposal
from test_alpaca_paper_execution import _seed_proposal
from test_entry_submission_recovery import setup as entry_setup, restart, paper_row
from test_entry_rejection_proof import intercept, response
from test_entry_held_recovery import ordinary_holding


def _real(manager, settings, journal, monkeypatch):
    pytest.importorskip("alpaca")
    real = AlpacaClient(settings, journal)
    sdk = real._trading_client(); sdk._session.trust_env, sdk._retry_wait = False, 0
    def no_socket(*a, **k): raise AssertionError("unmocked socket creation forbidden")
    monkeypatch.setattr("urllib3.util.connection.create_connection", no_socket)
    manager.alpaca = real
    return real


WASH = {"code": 40310000, "message": "potential wash trade detected. use complex orders"}


# ================================================================ N3 positive: real SDK 403 -> proof -> audited apply
def test_n3a_real_sdk_403_wash_trade_is_provable_and_operator_resolves_as_rejected(tmp_path, monkeypatch):
    fake, settings, journal, manager, proposal = entry_setup(tmp_path, monkeypatch)
    _real(manager, settings, journal, monkeypatch)
    calls = intercept(monkeypatch, status=403, body=WASH)
    approvals_before = journal.count_rows("approvals")
    r = manager.execute_proposal(proposal)
    # an unrelated open incident (opened later) must survive the resolution
    journal.insert("protection_checks", {"check_id": "pcheck_other", "position_id": "pos_other", "symbol": "QQQ",
                                         "protection_status": "unprotected", "severity": "critical", "detail": "x"})
    assert r.submission_uncertain and not r.blocked and len(calls) == 1
    meta = ledger_ops.metadata(paper_row(journal))
    proof = meta["broker_rejection_proof"]
    assert meta["phase"] == "unknown" and proof["kind"] == "broker_confirmed_rejection"
    rej = proof["rejection"]
    assert rej["classification"] == "wash_trade" and rej["http_status"] == 403 and rej["error_code"] == 40310000
    assert rej["response_body_sha256"] == sha256(json.dumps(WASH).encode()).hexdigest()
    assert rej["request"] == json.loads(calls[0].body)                     # bound to the exact sent request
    assert "message" not in rej and "potential wash" not in json.dumps(meta)   # response text not persisted
    assert pending_entry_intent(journal) and protection_watchdog.has_blocking_incident(journal)
    # restart against a deterministic empty fake broker for corroboration reads
    journal, manager = restart(fake, settings, journal)
    pv = resolve_entry_intent(manager, proposal.proposal_id)
    assert pv["ok"] and pv["status"] == "eligible_for_abandonment" and pv["evidence"]["proof"]["kind"] == "broker_confirmed_rejection"
    assert ledger_ops.metadata(paper_row(journal))["phase"] == "unknown"     # preview wrote nothing
    assert resolve_entry_intent(manager, proposal.proposal_id, apply=True)["status"] == "refused"   # note required
    ap = resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="verified 403 wash-trade rejection")
    assert ap["ok"] and ap["applied"] and ap["status"] == "abandoned_rejected"
    row = paper_row(journal)
    assert row["state"] == "rejected" and row["client_order_id"] == proposal.proposal_id
    assert ledger_ops.metadata(row)["phase"] == "abandoned_rejected"
    assert pending_entry_intent(journal) is None
    assert journal.proposal_by_id(proposal.proposal_id)["status"] == "blocked"
    assert journal.count_rows("approvals") == approvals_before             # no refund, no new receipt
    # only its own incident cleared; the unrelated one still blocks
    assert journal.one("SELECT resolved_at_utc FROM protection_checks WHERE check_id='pcheck_other'")["resolved_at_utc"] is None
    assert journal.one("SELECT resolved_at_utc FROM protection_checks WHERE position_id=?", (f"entry_{row['order_id']}",))["resolved_at_utc"]
    assert manager.execute_proposal(proposal).blocked and fake.submits == 0 and len(calls) == 1   # zero re-POSTs
    assert resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="x")["status"] == "already_resolved"


# ================================================================ N3 negatives through the real SDK
@pytest.mark.parametrize("case", ["429_then_403", "unknown_message", "duplicate_client_id", "html_body", "insufficient_bp_variant"])
def test_n3b_ambiguous_rejections_stay_gated_without_proof(tmp_path, monkeypatch, case):
    fake, settings, journal, manager, proposal = entry_setup(tmp_path, monkeypatch)
    _real(manager, settings, journal, monkeypatch)
    from requests.adapters import HTTPAdapter
    calls = []
    def send(adapter, request, **kwargs):
        calls.append(request)
        if case == "429_then_403":
            return response(429, request, {"code": 429, "message": "rate limited"}) if len(calls) == 1 else response(403, request, WASH)
        if case == "unknown_message":
            return response(403, request, {"code": 40310000, "message": "account is restricted from trading"})
        if case == "duplicate_client_id":
            return response(422, request, {"code": 42210000, "message": "client_order_id must be unique"})
        if case == "html_body":
            r = response(403, request, b"<html>forbidden</html>"); r.headers["Content-Type"] = "text/html"; return r
        if case == "insufficient_bp_variant":
            return response(403, request, {"code": 40310000, "message": "insufficient day trading buying power"})
    monkeypatch.setattr(HTTPAdapter, "send", send)
    r = manager.execute_proposal(proposal)
    assert r.submission_uncertain
    meta = ledger_ops.metadata(paper_row(journal))
    assert "broker_rejection_proof" not in meta and meta["phase"] == "unknown"
    journal, manager = restart(fake, settings, journal)
    res = resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="x")
    assert res["status"] == "refused" and "ambiguous" in res["reason"]
    assert pending_entry_intent(journal) is not None


# ================================================================ N3: tamper / late order / audit rollback
def test_n3c_tampered_proof_late_order_and_audit_failure_all_keep_the_gate(tmp_path, monkeypatch):
    fake, settings, journal, manager, proposal = entry_setup(tmp_path, monkeypatch)
    _real(manager, settings, journal, monkeypatch)
    intercept(monkeypatch, status=403, body=WASH)
    manager.execute_proposal(proposal)
    journal, manager = restart(fake, settings, journal)
    row = paper_row(journal); meta = ledger_ops.metadata(row); good = deepcopy(meta)
    # (a) tamper: change the recorded request qty -> proof no longer matches the intent
    meta["broker_rejection_proof"]["rejection"]["request"]["qty"] = 11.0
    with ledger_ops.atomic(journal) as t:
        ledger_ops.update(t, "paper_orders", "order_id", row["order_id"], {"raw_request_json": meta})
    assert "refused" == resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="x")["status"]
    with ledger_ops.atomic(journal) as t:
        ledger_ops.update(t, "paper_orders", "order_id", row["order_id"], {"raw_request_json": good})
    # (b) broker now knows the client id -> normal reconciliation, not abandonment
    fake.submit({"symbol": "XYZ", "qty": 10, "side": "buy", "entry": 100, "target": 106, "stop": 97, "tif": "gtc", "client_order_id": proposal.proposal_id})
    assert "broker knows" in resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="x")["reason"]
    fake.orders.clear()
    # (c) audit write fails -> rollback, gate + incident intact
    monkeypatch.setattr(JournalStore, "log_system_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit down")))
    assert resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="x")["status"] == "refused"
    assert ledger_ops.metadata(paper_row(journal))["phase"] == "unknown"
    assert pending_entry_intent(journal) is not None and protection_watchdog.has_blocking_incident(journal)


# ================================================================ N4 positive: ordinary holding + rejected add-on (async reject, zero fill)
def test_n4a_rejected_addon_in_held_symbol_recovers_and_leaves_holding_protected(tmp_path, monkeypatch):
    fake, settings, journal, manager, proposal = entry_setup(tmp_path, monkeypatch)
    position, parent = ordinary_holding(fake, journal, manager)
    holding_before = journal.open_positions(); fills_before = journal.query("SELECT * FROM paper_fills")
    fake.response_error, fake.lookup_error = ReadTimeout("lost"), ReadTimeout("lookup unavailable")
    r = manager.execute_proposal(proposal)
    assert r.submission_uncertain and fake.submits == 2
    failed = next(o for o in fake.orders.values() if o.client_order_id == proposal.proposal_id)
    failed.status, failed.filled_qty, failed.legs = "rejected", 0, []
    fake.lookup_error = None
    journal, manager = restart(fake, settings, journal)
    rec = manager.reconcile()
    assert rec["errors"] == [] and pending_entry_intent(journal) is None
    row = journal.one("SELECT * FROM paper_orders WHERE proposal_id=?", (proposal.proposal_id,))
    assert row["state"] == "rejected" and ledger_ops.metadata(row)["phase"] == "acknowledged"
    ev = ledger_ops.metadata(row)["recovery_evidence"]
    assert ev["attributed_positions"][0]["position_id"] == position["position_id"] and ev["broker_position"]["qty"] == 5
    assert journal.open_positions() == holding_before and journal.query("SELECT * FROM paper_fills") == fills_before
    assert parent.status == "filled" and [l.status for l in parent.legs] == ["new", "held"]     # protection untouched
    assert not protection_watchdog.has_blocking_incident(journal)
    assert manager.execute_proposal(proposal).blocked and fake.submits == 2                    # zero re-POSTs
    # and the operator command reports it as already handled by reconciliation (no double action)
    assert resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="x")["status"] == "refused"


# ================================================================ N4 positive via operator path (real-SDK 403 while holding)
def test_n4b_403_addon_while_holding_resolves_via_operator_command(tmp_path, monkeypatch):
    fake, settings, journal, manager, proposal = entry_setup(tmp_path, monkeypatch)
    position, parent = ordinary_holding(fake, journal, manager)
    _real(manager, settings, journal, monkeypatch)
    intercept(monkeypatch, status=403, body=WASH)
    r = manager.execute_proposal(proposal)
    assert r.submission_uncertain
    journal, manager = restart(fake, settings, journal)          # fake broker still holds XYZ + protection
    ap = resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="403 wash trade against own tp leg")
    assert ap["applied"] and ap["status"] == "abandoned_rejected", ap
    assert ap["resolution"]["exposure_verification"]["attributed_positions"][0]["qty"] == 5
    assert journal.open_positions()[0]["position_id"] == position["position_id"]
    assert pending_entry_intent(journal) is None


# ================================================================ N4 refusals
@pytest.mark.parametrize("cause", ["unknown_open_order", "extra_broker_qty", "changed_between_reads", "target_related_fill"])
def test_n4c_related_or_unexplained_exposure_keeps_the_gate(tmp_path, monkeypatch, cause):
    fake, settings, journal, manager, proposal = entry_setup(tmp_path, monkeypatch)
    position, parent = ordinary_holding(fake, journal, manager)
    fake.response_error, fake.lookup_error = ReadTimeout("lost"), ReadTimeout("lookup unavailable")
    manager.execute_proposal(proposal)
    failed = next(o for o in fake.orders.values() if o.client_order_id == proposal.proposal_id)
    failed.status, failed.filled_qty, failed.legs = "rejected", 0, []
    fake.lookup_error = None
    if cause == "unknown_open_order":
        fake.add_stray_order("XYZ")
    elif cause == "extra_broker_qty":
        fake.set_position("XYZ", 7)
    elif cause == "changed_between_reads":
        orig = fake.get_all_positions; n = {"i": 0}
        def flapping():
            n["i"] += 1
            if n["i"] == 2: fake.set_position("XYZ", 6)
            return orig()
        fake.get_all_positions = flapping
    elif cause == "target_related_fill":
        journal.insert("paper_fills", {"fill_id": "fill_related", "order_id": paper_row(journal)["order_id"] if False else journal.one("SELECT order_id FROM paper_orders WHERE proposal_id=?", (proposal.proposal_id,))["order_id"],
                                       "symbol": "XYZ", "side": "buy", "qty": 1, "price": 100, "execution_source": "alpaca_paper", "fill_source": "alpaca_paper"})
    rec = manager.reconcile()
    assert rec["errors"] and pending_entry_intent(journal) is not None
    assert fake.submits == 2
