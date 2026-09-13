"""Round-three verification probes on 8c23a53: N1 recovery, terminal-zero, N2 deferral, plus new adversarial cases."""
import sys, os, threading, time
sys.path.insert(0, os.path.join(os.environ["ALPHAOS_REPO"], "tests"))
import pytest
from requests import ConnectionError as ReqConnectionError, ReadTimeout

from alphaos.broker.alpaca_client import AlpacaClient, AlpacaSubmissionNotSent
from alphaos.execution import ledger_ops, protection_watchdog
from alphaos.execution.entry_intent_resolution import resolve_entry_intent
from alphaos.execution.entry_submission import EntrySubmission, pending_entry_intent
from alphaos.risk.account_exposure import read_exposure, ExposureUnavailable
from alphaos.journal.journal_store import JournalStore
from conftest import make_proposal, inject_pending_proposal
from test_entry_submission_recovery import setup as entry_setup, restart, paper_row, NotFound
from test_alpaca_paper_execution import _seed_proposal
from test_entry_transport_proof import fail_connect


# ====================================================================== N1: positive proof (real SDK transport)
def test_n1_real_sdk_connect_refused_is_provable_and_operator_releases_gate(tmp_path, monkeypatch):
    pytest.importorskip("alpaca")
    fake, settings, journal, manager, proposal = entry_setup(tmp_path, monkeypatch)
    real = AlpacaClient(settings, journal); real._trading_client()._session.trust_env = False
    manager.alpaca = real
    outbound = fail_connect(monkeypatch)
    r = manager.execute_proposal(proposal)
    assert r.submission_uncertain and outbound == [1]
    meta = ledger_ops.metadata(paper_row(journal))
    assert meta["phase"] == "unknown" and meta["no_dispatch_proof"]["transport"]["reason"] == "new_connection_failure"
    assert pending_entry_intent(journal) and protection_watchdog.has_blocking_incident(journal)
    # restart; corroboration reads against a deterministic empty broker (404 / flat / no orders)
    journal, manager = restart(fake, settings, journal)
    preview = resolve_entry_intent(manager, proposal.proposal_id)
    assert preview["ok"] and preview["status"] == "eligible_for_abandonment" and not preview["applied"]
    assert ledger_ops.metadata(paper_row(journal))["phase"] == "unknown"        # preview writes nothing
    refused = resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="")
    assert refused["status"] == "refused" and "note" in refused["reason"]
    applied = resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="verified: connection refused before send")
    assert applied["ok"] and applied["applied"] and applied["status"] == "abandoned_unsent"
    row = paper_row(journal)
    assert row["state"] == "failed" and row["client_order_id"] == proposal.proposal_id     # identity preserved
    assert ledger_ops.metadata(row)["phase"] == "abandoned_unsent"
    assert pending_entry_intent(journal) is None and not protection_watchdog.has_blocking_incident(journal)
    assert journal.proposal_by_id(proposal.proposal_id)["status"] == "blocked"
    # same proposal can never be retried; no POST
    again = manager.execute_proposal(proposal)
    assert again.blocked and fake.submits == 0 and outbound == [1]
    # a different proposal is no longer gated
    assert resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="x")["status"] == "already_resolved"


# ====================================================================== N1: negative -- my original generic trigger stays ambiguous
def test_n1_generic_connection_error_remains_ambiguous_and_unresolvable(tmp_path, monkeypatch):
    fake, settings, journal, manager, proposal = entry_setup(tmp_path, monkeypatch)
    def refuse(spec): raise ReqConnectionError("connection refused")
    fake.before_post = refuse
    r = manager.execute_proposal(proposal)
    assert r.submission_uncertain
    assert "no_dispatch_proof" not in ledger_ops.metadata(paper_row(journal))
    res = resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="operator says it never went")
    assert res["status"] == "refused" and "ambiguous" in res["reason"]
    assert pending_entry_intent(journal) is not None                              # gate stays closed by design


# ====================================================================== N1: crash before / after the dispatch claim
def test_n1_crash_before_claim_is_recoverable_but_after_claim_is_not(tmp_path, monkeypatch):
    fake, settings, journal, manager, proposal = entry_setup(tmp_path, monkeypatch)
    from alphaos.execution.order_manager import ProtectionPath
    entry = EntrySubmission(manager)
    row, owned = entry.create(proposal, ProtectionPath.BROKER_BRACKET if hasattr(ProtectionPath, "BROKER_BRACKET") else list(ProtectionPath)[0])
    assert owned and ledger_ops.metadata(row)["phase"] == "prepared"
    assert pending_entry_intent(journal) is not None
    journal, manager = restart(fake, settings, journal)
    assert manager.reconcile()["errors"]                                          # still gated
    pv = resolve_entry_intent(manager, proposal.proposal_id)
    assert pv["ok"] and pv["evidence"]["proof"]["kind"] == "dispatch_not_claimed"
    # a paused sender that wakes after abandonment cannot POST
    stale_row = journal.order_by_id(row["order_id"])
    ap = resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="crash before dispatch claim")
    assert ap["applied"]
    with pytest.raises(ValueError):
        EntrySubmission(manager).claim_dispatch(stale_row)
    assert manager.execute_proposal(proposal).blocked and fake.submits == 0
    # after-claim variant
    fake2, settings2, journal2, manager2, proposal2 = entry_setup(tmp_path / "b", monkeypatch)
    entry2 = EntrySubmission(manager2)
    row2, _ = entry2.create(proposal2, list(ProtectionPath)[0]); entry2.claim_dispatch(row2)
    res = resolve_entry_intent(manager2, proposal2.proposal_id, apply=True, note="x")
    assert res["status"] == "refused" and "ambiguous" in res["reason"]


# ====================================================================== N1: refusal on contradictory evidence / audit rollback
def test_n1_apply_refuses_when_broker_knows_id_or_exposure_exists_and_rolls_back_on_audit_failure(tmp_path, monkeypatch):
    pytest.importorskip("alpaca")
    fake, settings, journal, manager, proposal = entry_setup(tmp_path, monkeypatch)
    real = AlpacaClient(settings, journal); real._trading_client()._session.trust_env = False
    manager.alpaca = real; fail_connect(monkeypatch)
    manager.execute_proposal(proposal)
    journal, manager = restart(fake, settings, journal)
    # (a) broker suddenly knows the client id -> normal reconciliation, not abandonment
    fake.submit({"symbol": "XYZ", "qty": 10, "side": "buy", "entry": 100, "target": 106, "stop": 97, "tif": "gtc", "client_order_id": proposal.proposal_id})
    res = resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="x")
    assert res["status"] == "refused" and "broker knows" in res["reason"]
    fake.orders.clear(); fake._by_symbol.clear() if hasattr(fake, "_by_symbol") else None
    # (b) local open position in the symbol -> refused
    journal.insert("positions", {"position_id": "pos_x", "order_id": "ord_other", "symbol": "XYZ", "direction": "long",
                                 "strategy": "swing", "qty": 5, "avg_entry_price": 99, "stop_price": 95, "target_price": 110,
                                 "opened_at": "2026-09-01T14:00:00+00:00", "opened_market_date": "2026-09-01",
                                 "status": "open", "current_price": 99, "unrealized_pnl": 0, "execution_source": "alpaca_paper",
                                 "broker_order_id": "b", "is_short": 0, "trade_id": "t"})
    res = resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="x")
    assert res["status"] == "refused" and "position" in res["reason"]
    journal.conn.execute("DELETE FROM positions"); journal.conn.commit()
    # (c) audit write fails -> abandonment rolled back, incident still open
    monkeypatch.setattr(JournalStore, "log_system_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit down")))
    res = resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="x")
    assert res["status"] == "refused"
    assert ledger_ops.metadata(paper_row(journal))["phase"] == "unknown"
    assert pending_entry_intent(journal) is not None and protection_watchdog.has_blocking_incident(journal)


# ====================================================================== N3 (NEW): definitive HTTP 4xx rejection has no exit
def test_n3_synchronous_broker_rejection_is_treated_as_uncertain_and_cannot_be_resolved(tmp_path, monkeypatch):
    """Alpaca answers the POST with 403 (wash trade / buying power) or 422 and creates NO order.
    Round three classifies that as 'response unavailable' -> 404 forever -> gate closed;
    entry_intent_resolve refuses (no transport proof). Reachable: an add-on entry in a held
    symbol is rejected 403 'potential wash trade' because our own take-profit leg is open."""
    from alpaca.common.exceptions import APIError
    fake, settings, journal, manager, proposal = entry_setup(tmp_path, monkeypatch)
    def reject(spec):
        raise APIError('{"code":40310000,"message":"potential wash trade detected. use complex orders"}')
    fake.before_post = reject
    r = manager.execute_proposal(proposal)
    assert r.submission_uncertain and fake.submits == 1
    for _ in range(5):
        assert manager.reconcile()["errors"]
    assert pending_entry_intent(journal) is not None
    res = resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="broker returned 403")
    assert res["status"] == "refused" and "ambiguous" in res["reason"]
    other = make_proposal(symbol="ABC", entry=50, stop=48, target=54, qty=10); _seed_proposal(journal, other)
    assert manager.execute_proposal(other).blocked and fake.submits == 1
    with pytest.raises(ExposureUnavailable):
        read_exposure(manager.alpaca, journal)


# ====================================================================== N4 (NEW): terminal-zero recovery impossible for a held symbol
def test_n4_terminal_zero_rejection_in_a_held_symbol_can_never_clear(tmp_path, monkeypatch):
    """Broker creates the order then rejects it with zero fill (async reject). If the account
    already holds the symbol, _verify_recovery refuses (local position / broker exposure / our
    own protective leg = 'related open order'), and entry_intent_resolve refuses because the
    broker knows the id -> permanent gate. Reachable: XLK prop_768a75bce2d2 (2026-08-04) was
    proposed while XLK was held."""
    fake, settings, journal, manager, proposal = entry_setup(tmp_path, monkeypatch)
    # existing held position in the same symbol, with its own protective bracket at the broker
    fake.submit({"symbol": "XYZ", "qty": 5, "side": "buy", "entry": 99, "target": 110, "stop": 95, "tif": "gtc", "client_order_id": "prop_old"})
    held = fake._find("XYZ"); held.status, held.filled_qty, held.filled_avg_price = "filled", 5, 99
    from test_alpaca_paper_execution import _FakePosition
    fake._positions["XYZ"] = _FakePosition("XYZ", 5, "long", 99.0)          # broker also holds it
    journal.insert("positions", {"position_id": "pos_old", "order_id": "ord_old", "symbol": "XYZ", "direction": "long",
                                 "strategy": "swing", "qty": 5, "avg_entry_price": 99, "stop_price": 95, "target_price": 110,
                                 "opened_at": "2026-09-01T14:00:00+00:00", "opened_market_date": "2026-09-01",
                                 "status": "open", "current_price": 99, "unrealized_pnl": 0, "execution_source": "alpaca_paper",
                                 "broker_order_id": held.id, "is_short": 0, "trade_id": "t_old"})
    fake.response_error, fake.lookup_error = ReadTimeout("response lost"), ReadTimeout("lookup unavailable")
    r = manager.execute_proposal(proposal)
    assert r.submission_uncertain and fake.submits == 2          # 1 held-position seed + 1 add-on
    order = next(o for o in fake.orders.values() if o.client_order_id == proposal.proposal_id)   # our add-on
    order.status, order.legs, order.filled_qty = "rejected", [], 0
    fake.lookup_error = None
    for _ in range(5):
        rec = manager.reconcile()
        assert rec["errors"], rec
    assert ledger_ops.metadata(paper_row(journal) if False else journal.one("SELECT * FROM paper_orders WHERE client_order_id=?", (proposal.proposal_id,)))["phase"] != "acknowledged"
    assert pending_entry_intent(journal) is not None
    res = resolve_entry_intent(manager, proposal.proposal_id, apply=True, note="rejected by broker")
    assert res["status"] == "refused" and "reconcile" in res["reason"]        # points back to reconciliation, which refuses too
    other = make_proposal(symbol="ABC", entry=50, stop=48, target=54, qty=10); _seed_proposal(journal, other)
    assert manager.execute_proposal(other).blocked and fake.submits == 2


# ====================================================================== terminal-zero: happy path + mismatched terms still block
def test_tz_matching_rejected_parent_without_children_recovers_once(tmp_path, monkeypatch):
    fake, settings, journal, manager, proposal = entry_setup(tmp_path, monkeypatch)
    fake.response_error, fake.lookup_error = ReadTimeout("lost"), ReadTimeout("lookup unavailable")
    r = manager.execute_proposal(proposal); assert r.submission_uncertain
    order = fake._find("XYZ"); order.status, order.legs, order.filled_qty = "rejected", [], 0
    fake.lookup_error = None
    journal, manager = restart(fake, settings, journal)
    rec = manager.reconcile()
    assert rec["reconciled"] == 1 and rec["errors"] == []
    assert pending_entry_intent(journal) is None and not protection_watchdog.has_blocking_incident(journal)
    assert manager.execute_proposal(proposal).blocked and fake.submits == 1     # never resubmitted
    assert journal.query("SELECT * FROM paper_fills") == [] and journal.open_positions() == []


@pytest.mark.parametrize("mutate", ["limit", "partial_fill", "active", "legs_filled"])
def test_tz_conflicting_terminal_evidence_keeps_gate_closed(tmp_path, monkeypatch, mutate):
    fake, settings, journal, manager, proposal = entry_setup(tmp_path, monkeypatch)
    fake.response_error, fake.lookup_error = ReadTimeout("lost"), ReadTimeout("lookup unavailable")
    manager.execute_proposal(proposal)
    order = fake._find("XYZ"); order.status, order.filled_qty = "rejected", 0
    if mutate == "limit": order.legs = []; order.limit_price = 101
    elif mutate == "partial_fill": order.legs = []; order.filled_qty, order.filled_avg_price = 3, 100
    elif mutate == "active": order.legs = []; order.status = "accepted"
    elif mutate == "legs_filled":
        order.legs[0].filled_qty = 1
    fake.lookup_error = None
    rec = manager.reconcile()
    assert rec["errors"] and pending_entry_intent(journal) is not None
    assert fake.submits == 1
