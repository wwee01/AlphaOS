"""L1 accounting from actual audit numbers and adverse offline lifecycle evidence."""
from copy import deepcopy
import json

import pytest

from alphaos.attribution import discover_events, resolve_pending
from alphaos.attribution.resolve import ATTRIBUTION_VERSION
from alphaos.reports.attribution import build_attribution_report
from alphaos.reports.trade_accounting import build_trade_accounting, corrected_attribution_basis
from alphaos.reports.trade_packet import assemble_trade_packet
from alphaos.util import alerts
from conftest import make_proposal, make_settings
from test_alpaca_paper_execution import FakeTradingClient, _paper_om, _seed_proposal
from test_attribution_flow import _cand, _co, _proposal, _trade_outcome
from alphaos.journal.journal_store import JournalStore


def fixture(symbol="AVGO"):
    if symbol == "AVGO":
        qty, entry, exit_price, stop, gross, costs, net, stored = 43, 370.238837, 347.153488, 348.74, -992.67, 3.08, -995.75, -1.074
    else:
        qty, entry, exit_price, stop, gross, costs, net, stored = 27, 617.49, 670.18, 581.39, 1422.63, 3.48, 1419.15, 1.46
    position = {"position_id": "pos", "order_id": "entry", "symbol": symbol,
                "direction": "long", "stop_price": stop}
    outcome = {"outcome_id": "out", "position_id": "pos", "symbol": symbol, "direction": "long",
               "qty": qty, "entry_price": entry, "exit_price": exit_price, "gross_pnl": gross,
               "costs": costs, "net_pnl": net, "realized_r": stored}
    orders = [{"order_id": oid, "symbol": symbol, "stop_loss_price": stop if oid == "entry" else None}
              for oid in ("entry", "exit")]
    fills = [{"fill_id": oid, "order_id": oid, "position_id": "pos", "symbol": symbol,
              "qty": qty, "price": price, "side": side, "commission": 0,
              "filled_at": "2026-09-03T15:50:45Z", "created_at_utc": "2026-09-03T15:50:45Z"}
             for oid, price, side in [("entry", entry, "buy"), ("exit", exit_price, "sell")]]
    return outcome, position, orders, fills


@pytest.mark.parametrize("symbol,net_r", [("AVGO", -1.077126950829296), ("META", 1.455986457371499)])
def test_actual_audited_money_and_net_r_without_history_rewrite(symbol, net_r):
    source = fixture(symbol)
    frozen = deepcopy(source)
    result = build_trade_accounting(*source)
    assert source == frozen
    assert result["status"] == "verified_ledger_arithmetic"
    assert result["net_r"] == pytest.approx(net_r)
    assert result["stored_realized_r"] == source[0]["realized_r"]
    assert result["stored_realized_r_basis"] == "gross"
    assert result["source_sha256"] and not result["broker_fees_verified"]
    assert all(row["broker_reported_filled_at"] is None for row in result["fill_timestamps"])
    assert result["lessons_generated"] is False


@pytest.mark.parametrize("change", ["cost_missing", "qty_missing", "zero_risk", "bad_net", "bad_stored_r",
                                    "missing_exit_fill", "wrong_qty", "wrong_side", "foreign_position", "nan"])
def test_missing_or_contradictory_evidence_never_manufactures_net_r(change):
    outcome, position, orders, fills = fixture()
    if change == "cost_missing":
        outcome["costs"] = None
    elif change == "qty_missing":
        outcome["qty"] = None
    elif change == "zero_risk":
        position["stop_price"] = outcome["entry_price"]
    elif change == "bad_net":
        outcome["net_pnl"] += 10
    elif change == "bad_stored_r":
        outcome["realized_r"] = 3
    elif change == "missing_exit_fill":
        fills.pop()
    elif change == "wrong_qty":
        fills[-1]["qty"] -= 1
    elif change == "wrong_side":
        fills[-1]["side"] = "buy"
    elif change == "foreign_position":
        fills[-1]["position_id"] = "foreign"
    elif change == "nan":
        outcome["costs"] = float("nan")
    result = build_trade_accounting(outcome, position, orders, fills)
    assert result["status"] == "invalid_or_incomplete_evidence"
    assert result["net_r"] is None and result["blockers"]


def test_legacy_basis_correction_retains_exact_stored_claim():
    for old, new in [("realized_net", "realized_gross"), ("net_vs_gross", "gross_vs_gross")]:
        source = {"r_basis": old, "actual_path_r": 1.46, "attribution_version": "2.0.0"}
        frozen = deepcopy(source)
        result = corrected_attribution_basis(source)
        assert source == frozen
        assert result["stored_basis"] == old and result["corrected_basis"] == new
        assert result["correction_required"]


def test_short_accounting_uses_positive_original_risk_and_correct_fill_sides():
    outcome, position, orders, fills = fixture("META")
    outcome["direction"] = position["direction"] = "short"
    outcome["exit_price"] = 2*outcome["entry_price"] - outcome["exit_price"]
    position["stop_price"] = 2*outcome["entry_price"] - position["stop_price"]
    orders[0]["stop_loss_price"] = position["stop_price"]
    fills[0]["side"], fills[1]["side"] = "sell", "buy_to_cover"
    fills[1]["price"] = outcome["exit_price"]
    result = build_trade_accounting(outcome, position, orders, fills)
    assert result["status"] == "verified_ledger_arithmetic"
    assert result["net_r"] == pytest.approx(1.455986457371499)


def test_changed_position_stop_cannot_redefine_original_net_r():
    outcome, position, orders, fills = fixture()
    position["stop_price"] -= 1
    result = build_trade_accounting(outcome, position, orders, fills)
    assert result["net_r"] is None
    assert result["blockers"] == ["position_stop_differs_from_original_entry_order"]


@pytest.mark.parametrize("field,value", [("broker_order_id", "foreign"), ("incremental_qty", 1),
    ("incremental_price", 999), ("broker_reported_filled_at", "wrong"), ("observed_at_utc", "wrong")])
def test_mismatched_cumulative_receipt_cannot_certify_timestamp(field, value):
    outcome, position, orders, fills = fixture()
    fill = fills[0]
    fill["broker_order_id"] = "broker"
    receipt = {"version": "broker_cumulative_fill_v1", "broker_order_id": "broker",
               "incremental_qty": fill["qty"], "incremental_price": fill["price"],
               "broker_reported_filled_at": fill["filled_at"], "observed_at_utc": fill["created_at_utc"],
               "timestamp_source": "broker"}
    receipt[field] = value
    fill["raw_broker_fill_reference"] = json.dumps(receipt)
    result = build_trade_accounting(outcome, position, orders, fills)
    assert result["status"] == "verified_ledger_arithmetic"
    assert result["fill_timestamps"][0]["broker_reported_filled_at"] is None
    assert not result["fill_timestamps"][0]["individual_increment_execution_time_verified"]


@pytest.mark.parametrize("raw", [None, "null", "[]", '"text"', "not-json"])
def test_malformed_legacy_exit_metadata_cannot_block_valid_close(monkeypatch, raw):
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **kw: None)
    fake = FakeTradingClient()
    _, journal, manager = _paper_om(fake)
    proposal = make_proposal(symbol="XYZ", entry=100, stop=97, target=106, qty=10)
    _seed_proposal(journal, proposal)
    submitted = manager.execute_proposal(proposal)
    fake.fill_entry("XYZ", 100)
    row = journal.order_by_id(submitted.order["order_id"])
    pid = manager._open_real_position(row, manager.alpaca.get_order(row["broker_order_id"]))
    journal.insert("paper_orders", {"order_id": "legacy-exit", "symbol": "XYZ", "raw_response_json": raw})
    class OnceCosts:
        commission_per_share, min_commission, slippage_bps = 0, 0, 1
        calls = 0
        def costs(self, *args):
            from alphaos.execution.costs import CostModel
            self.calls += 1
            assert self.calls == 1
            return CostModel().costs(*args)
    manager.positions.cost_model = OnceCosts()
    closed = manager.positions.close_position(pid, 106, "target", recorded_exit_order_id="legacy-exit")
    assert closed and journal.position_by_id(pid)["status"] == "closed"
    response = json.loads(journal.order_by_id("legacy-exit")["raw_response_json"])
    assert response["prior_raw_response"] == raw
    assert response["accounting_receipt"]["net_pnl"] == 59.79
    assert response["accounting_receipt"]["close_execution_at"] is None
    journal.close()


def test_entry_partial_fill_exit_to_report_with_distinct_broker_and_booking_times(monkeypatch):
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **kw: None)
    fake = FakeTradingClient()
    settings, journal, manager = _paper_om(fake)
    proposal = make_proposal(symbol="XYZ", entry=100, stop=97, target=110, qty=10)
    _seed_proposal(journal, proposal)
    submitted = manager.execute_proposal(proposal)
    assert not submitted.blocked
    row = journal.order_by_id(submitted.order["order_id"])
    parent = fake._find("XYZ")
    norm = {**manager.alpaca.get_order(parent.id), "filled_qty": 4, "filled_avg_price": 100,
            "status": "partially_filled", "state": "partially_filled", "filled_at": "2026-09-03T15:40:48Z"}
    pid = manager._open_real_position(row, norm)
    norm.update(filled_qty=10, filled_avg_price=101.2, status="filled", state="filled",
                filled_at="2026-09-03T15:41:00Z")
    manager._open_real_position(row, norm)
    assert [(f["qty"], f["price"]) for f in journal.query("SELECT * FROM paper_fills ORDER BY id")] == [(4, 100), (6, 102)]
    pos = journal.position_by_id(pid)
    fake.set_position("XYZ", 4)
    exit_norm = {"broker_order_id": "actual-target", "client_order_id": "target-client", "symbol": "XYZ",
                 "side": "sell", "order_type": "limit", "order_class": "bracket", "qty": 10,
                 "filled_qty": 6, "filled_avg_price": 105, "filled_at": "2026-09-15T13:39:13Z",
                 "submitted_at": "2026-09-03T15:40:48Z", "time_in_force": "gtc", "limit_price": 105,
                 "status": "partially_filled", "state": "partially_filled"}
    assert manager._record_broker_exit(pos, exit_norm, "target") is None
    fake.remove_position("XYZ")
    exit_norm.update(filled_qty=10, filled_avg_price=106, filled_at="2026-09-15T13:39:20Z", state="filled", status="filled")
    manager._record_broker_exit(pos, exit_norm, "target")
    packet = assemble_trade_packet(journal, position_id=pid)
    assert len(packet["orders"]) == 2 and len(packet["fills"]) == 4
    accounting = packet["accounting"]
    assert accounting["status"] == "verified_ledger_arithmetic"
    assert accounting["entry_vwap"] == 101.2 and accounting["exit_vwap"] == 106
    assert accounting["gross_pnl"] == 48 and accounting["modeled_costs"] == 0.21
    assert accounting["net_pnl"] == 47.79 and accounting["net_r"] == pytest.approx(47.79 / 42)
    assert all(t["broker_reported_filled_at"] and t["ledger_recorded_at_utc"] != t["broker_reported_filled_at"]
               for t in accounting["fill_timestamps"])
    exit_order = next(o for o in packet["orders"] if o["broker_order_id"] == "actual-target")
    assert (exit_order["order_type"], exit_order["order_class"], exit_order["time_in_force"]) == ("limit", "bracket", "gtc")
    assert exit_order["execution_mode"] == "alpaca_paper" and exit_order["fill_price_basis"] == "alpaca_fill"
    assert exit_order["data_provider"] == row["data_provider"] and exit_order["data_feed"] == row["data_feed"]
    receipt = json.loads(exit_order["raw_response_json"])["accounting_receipt"]
    assert receipt["stored_realized_r_basis"] == "gross" and receipt["net_r"] == pytest.approx(47.79 / 42)
    assert receipt["close_execution_at"] != receipt["accounting_recorded_at_utc"]
    before = {table: journal.query("SELECT * FROM " + table + " ORDER BY id")
              for table in ["paper_orders", "paper_fills", "positions", "exits", "trade_outcomes"]}
    manager._record_broker_exit(pos, exit_norm, "target")
    again = assemble_trade_packet(journal, position_id=pid)
    report = build_attribution_report(journal, settings)
    assert again["accounting"] == accounting
    assert report["trade_accounting"][0]["accounting"]["net_r"] == accounting["net_r"]
    assert {table: journal.query("SELECT * FROM " + table + " ORDER BY id") for table in before} == before
    journal.close()


def test_mixed_legacy_rows_resolve_without_reenrollment_or_relabeling_and_new_rows_use_gross():
    journal, settings = JournalStore(":memory:"), make_settings()
    ids = {}
    for name in ["legacy_resolved", "legacy_partial", "legacy_pending", "new"]:
        cand = _cand(journal)
        prop = _proposal(journal, cand, "filled")
        if name != "legacy_pending":
            _trade_outcome(journal, prop, 1.46)
        _co(journal, cand, replay_result="target_hit", replay_r=2.0)
        ids[name] = prop
        if name != "new":
            journal.insert("attribution_records", {
                "attribution_id": name, "attribution_type": "propose_approved_executed",
                "attribution_version": "2.0.0", "agent": "execution", "source_id": prop,
                "proposal_id": prop, "candidate_id": cand, "symbol": "AAPL", "data_quality_status": "ok",
                "resolved_status": name.removeprefix("legacy_"),
                "actual_path_r": 1.46 if name != "legacy_pending" else None,
                "r_basis": "realized_net" if name != "legacy_pending" else None,
            })
    original = journal.one("SELECT * FROM attribution_records WHERE attribution_id='legacy_resolved'")
    assert discover_events(journal, settings)["total"] == 1
    result = resolve_pending(journal, settings)
    assert result["resolved"] == 2 and result["still_pending"] == 1
    _trade_outcome(journal, ids["legacy_pending"], 1.46)
    assert resolve_pending(journal, settings)["resolved"] == 1
    assert len(journal.query("SELECT * FROM attribution_records")) == 4
    assert journal.one("SELECT * FROM attribution_records WHERE attribution_id='legacy_resolved'") == original
    old = journal.one("SELECT * FROM attribution_records WHERE attribution_id='legacy_partial'")
    assert old["r_basis"] == "realized_net" and old["attribution_version"] == "2.0.0" and old["actual_path_r"] == 1.46
    new = journal.one("SELECT * FROM attribution_records WHERE proposal_id=?", (ids["new"],))
    assert new["r_basis"] == "gross_vs_gross" and new["attribution_version"] == ATTRIBUTION_VERSION == "2.1.0"
    assert discover_events(journal, settings)["total"] == 0
    journal.close()
