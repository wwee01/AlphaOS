"""Offline broker fixtures only: never place orders or invoke the orchestrator."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from conftest import make_settings
from alphaos.journal.journal_store import JournalStore
from alphaos.monitoring.broker_reads import BrokerReadError, ObservationRefused, PaperReads, order
from alphaos.monitoring.incidents import record_transitions
from alphaos.monitoring.observer import ledger_snapshot, observe

NOW = datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)
STAMP = NOW.isoformat()


def settings(**over):
    return make_settings(ALPHAOS_MODE="paper", EXECUTION_PROVIDER="alpaca_paper",
                         ALPACA_PAPER="true", ALPACA_API_KEY="fixture-key",
                         ALPACA_SECRET_KEY="fixture-secret", **over)


class Broker:
    receipts = []

    def __init__(self, orders=None, positions=None, open_orders=None, hours=None):
        self.orders = orders or {}
        self.position_rows = positions or []
        self.open_rows = open_orders or []
        self.hours = hours or {"date": "2026-10-13", "open": "09:30", "close": "16:00", "source": "broker_calendar"}
        self.lookups = []

    def account(self):
        return {"status": "ACTIVE", "credential_capabilities": "server_enforced_read_only_unverified"}

    def positions(self):
        return self.position_rows

    def open_orders(self):
        return self.open_rows

    def session_hours(self, now):
        return self.hours

    def exact_order(self, broker_id=None, client_id=None):
        self.lookups.append((broker_id, client_id))
        return self.orders.get(broker_id or client_id)


def local():
    return {"orders": [], "positions": [], "fills": {}, "broker_fills": [], "chains": {},
            "scan": {"scan_batch_id": "scan", "status": "completed", "universe_count": 20,
                     "candidates_found": 0, "proposals_created": 0, "errors_count": 0,
                     "completed_at_utc": STAMP, "started_at_utc": STAMP},
            "scan_data": {"n": 20, "usable": 20}, "position_data": {}, "scan_jobs": [],
            "risk_outcomes": [{"result": "fail", "fail_reason": "wide_spread", "n": 4}],
            "monitor_health": {"ok": True, "issue_codes": [], "stale_after_minutes": 30},
            "captured_at_utc": STAMP, "scope": {"history_days": 7}}


def trade(qty=10):
    data = local()
    row = {"order_id": "o", "broker_order_id": "b", "client_order_id": "c", "symbol": "AAPL",
           "qty": qty, "state": "filled", "side": "buy", "direction": "long", "order_class": "bracket",
           "order_type": "limit", "time_in_force": "gtc", "proposal_id": "p", "candidate_id": "candidate",
           "risk_check_id": "risk", "entry_price": 100, "intended_entry_price": 100}
    data["orders"] = [row]
    data["positions"] = [{"position_id": "pos", "order_id": "o", "symbol": "AAPL", "qty": qty, "direction": "long"}]
    data["fills"] = {"o": [{"qty": qty, "price": 100, "filled_at": STAMP, "broker_order_id": "b"}]}
    snap = {"snapshot_id": "snap", "quote_timestamp": STAMP, "bar_timestamp": STAMP,
            "received_at": STAMP, "created_at_utc": STAMP, "market_session": "regular",
            "is_usable": 1, "is_mock": 0, "dollar_volume": 100000000}
    data["position_data"] = {"pos": snap}
    data["chains"] = {"o": {
        "proposal": {"proposal_id": "p", "candidate_id": "candidate", "entry": 100, "stop": 95, "target": 110, "lineage_id": "lin"},
        "candidate": {"candidate_id": "candidate", "symbol": "AAPL", "lineage_id": "lin"},
        "evaluation": {"eval_id": "eval", "candidate_id": "candidate", "lineage_id": "lin", "is_mock": 0},
        "risk": {"risk_check_id": "risk", "proposal_id": "p", "result": "pass", "position_size": qty, "entry_price": 100, "stop_price": 95, "target_price": 110},
        "approval": {"approved": 1}, "lineage": {"lineage_id": "lin"}, "card": {"card_id": "card"}, "snapshot": snap}}
    broker_order = {"broker_order_id": "b", "client_order_id": "c", "symbol": "AAPL", "side": "buy",
                    "qty": qty, "status": "filled", "state": "filled", "filled_qty": qty,
                    "filled_avg_price": 100, "filled_at": STAMP, "updated_at": STAMP, "time_in_force": "gtc",
                    "legs": [{"broker_order_id": "stop", "symbol": "AAPL", "side": "sell", "stop_price": 95,
                              "qty": qty, "filled_qty": 0, "status": "new", "role": "stop_loss"}]}
    return data, Broker({"b": broker_order}, [{"symbol": "AAPL", "qty": qty, "side": "long"}])


def codes(report):
    return {f["code"] for f in report["findings"]}


def test_healthy_zero_candidates_and_risk_rejections_are_not_failures():
    report = observe(local(), settings(), Broker(), NOW)
    assert report["ok"] and report["activity"]["classification"] == "healthy_zero_candidates"
    assert report["risk_gate_outcomes"][0]["result"] == "fail"


def test_matching_order_fills_position_and_lineage():
    data, broker = trade()
    report = observe(data, settings(), broker, NOW)
    assert report["ok"], report["findings"]
    assert broker.lookups == [("b", "c")]
    assert report["entry_evidence"][0]["source_sha256"]


@pytest.mark.parametrize("field,value,code", [("broker_order_id", "other", "order_identity_mismatch"), ("client_order_id", "other", "order_identity_mismatch"), ("qty", 11, "order_terms_mismatch"), ("filled_avg_price", 101, "fill_price_mismatch"), ("filled_qty", 8, "fill_quantity_mismatch"), ("state", "accepted", "order_state_mismatch"), ("filled_at", None, "fill_timestamp_unverified")])
def test_exact_mismatches(field, value, code):
    data, broker = trade()
    broker.orders["b"][field] = value
    assert code in codes(observe(data, settings(), broker, NOW))


def test_missing_order_uses_client_lookup_and_does_not_match_by_symbol():
    data, broker = trade()
    broker.orders = {"other": deepcopy(broker.orders["b"])}
    assert "order_missing" in codes(observe(data, settings(), broker, NOW))
    assert broker.lookups == [("b", "c"), (None, "c")]


def test_partial_fill_with_held_bracket_exposes_unprotected_quantity():
    data, broker = trade()
    broker.orders["b"].update(status="partially_filled", state="partially_filled", filled_qty=4)
    broker.orders["b"]["legs"][0]["status"] = "held"
    broker.position_rows[0]["qty"] = 4
    report = observe(data, settings(), broker, NOW)
    assert {"partial_fill", "fill_quantity_mismatch", "position_quantity_mismatch", "missing_protective_order"} <= codes(report)


def test_reconciled_partial_fill_still_records_review_warning():
    data, broker = trade(4)
    data["orders"][0]["state"] = "partially_filled"
    broker.orders["b"].update(status="partially_filled", state="partially_filled")
    assert codes(observe(data, settings(), broker, NOW)) == {"partial_fill"}


@pytest.mark.parametrize("status", ["expired", "canceled", "pending_cancel", "held"])
def test_missing_active_protection(status):
    data, broker = trade()
    broker.orders["b"]["legs"][0]["status"] = status
    assert "missing_protective_order" in codes(observe(data, settings(), broker, NOW))


def test_unrelated_stop_cannot_protect_position():
    data, broker = trade()
    broker.orders["b"]["legs"] = []
    broker.open_rows = [{"broker_order_id": "stray", "symbol": "AAPL", "side": "sell", "qty": 10, "status": "new", "stop_price": 95, "legs": []}]
    assert {"missing_protective_order", "unattributed_open_order"} <= codes(observe(data, settings(), broker, NOW))


def test_multiple_origins_do_not_double_use_one_stop():
    data, broker = trade()
    data["positions"].append({**data["positions"][0], "position_id": "pos2"})
    broker.position_rows[0]["qty"] = 20
    assert "missing_protective_order" in codes(observe(data, settings(), broker, NOW))


def test_exit_partial_fill_reconciles_exact_leg_receipt():
    data, broker = trade()
    broker.orders["b"]["legs"][0].update(status="partially_filled", filled_qty=2, filled_at=STAMP)
    data["positions"][0]["qty"] = broker.position_rows[0]["qty"] = 8
    assert "exit_fill_mismatch" in codes(observe(data, settings(), broker, NOW))
    data["broker_fills"] = [{"broker_order_id": "stop", "qty": 2, "price": 95}]
    assert "exit_fill_mismatch" not in codes(observe(data, settings(), broker, NOW))


def test_stale_data_at_entry_and_current_position():
    data, broker = trade()
    data["chains"]["o"]["snapshot"]["quote_timestamp"] = (NOW - timedelta(minutes=2)).isoformat()
    assert {"entry_data_stale", "position_data_stale"} <= codes(observe(data, settings(), broker, NOW))


def test_historical_entry_data_not_aged_against_now():
    data, broker = trade()
    old = (NOW - timedelta(days=1)).isoformat()
    historical = deepcopy(data["chains"]["o"]["snapshot"])
    historical.update(quote_timestamp=old, bar_timestamp=old, created_at_utc=old, received_at=old)
    data["chains"]["o"]["snapshot"] = historical
    assert observe(data, settings(), broker, NOW)["ok"]


def test_zero_candidates_with_stale_or_absent_capture_is_unverified():
    data = local()
    data["scan_data"] = {"n": 20, "usable": 0}
    report = observe(data, settings(), Broker(), NOW)
    assert "zero_candidates_data_unverified" in codes(report)
    assert report["activity"]["classification"] == "unverified_inactivity"


def test_scan_attendance_uses_real_scheduler_key_and_early_close():
    data = local()
    s = settings(SCHEDULER_SCAN_WINDOWS="09:35-10:00,15:30-15:45")
    later = NOW.replace(hour=20)
    early = {"date": "2026-10-13", "open": "09:30", "close": "13:00", "source": "broker_calendar"}
    report = observe(data, s, Broker(hours=early), later)
    assert len([f for f in report["findings"] if f["code"] == "scan_window_missing"]) == 1
    data["scan_jobs"] = [{"lock_key": "scan:2026-10-13T09:35", "status": "completed", "id": 1, "result_summary_json": "{}"}]
    assert "scan_window_missing" not in codes(observe(data, s, Broker(hours=early), later))


def test_holiday_and_weekend_skip_scans_but_require_monitor():
    data = local()
    data["monitor_health"] = {"ok": False, "issue_codes": ["monitor_stale"]}
    closed = {"date": "2026-10-17", "open": None, "close": None, "source": "broker_calendar"}
    report = observe(data, settings(), Broker(hours=closed), NOW.replace(day=17, hour=20))
    assert codes(report) == {"monitor_stale"}


def test_lineage_risk_and_feed_coverage_do_not_silently_pass():
    data, broker = trade()
    data["chains"]["o"]["lineage"] = None
    data["chains"]["o"]["risk"]["result"] = "fail"
    data["scan_jobs"] = [{"lock_key": "other", "status": "completed", "id": 1, "result_summary_json": json.dumps({"scan_summary": {"scan_batch_id": "scan", "shadow_tier_scanned": 500, "shadow_tier_feed_coverage": .786}})}]
    assert {"lineage_missing", "risk_receipt_mismatch", "scan_data_coverage"} <= codes(observe(data, settings(), broker, NOW))


def test_broker_failure_is_unknown_not_flat_or_recovered(tmp_path):
    data, broker = trade()
    broker.orders["b"]["legs"] = []
    first = observe(data, settings(), broker, NOW)
    path = tmp_path / "test.paper-observer.json"
    record_transitions(first, path, settings())
    def fail():
        raise RuntimeError("fixture-secret must never appear")
    broker.positions = fail
    report = observe(data, settings(), broker, NOW)
    assert codes(report) == {"broker_unavailable"}
    assert "fixture-secret" not in json.dumps(report)
    transition = record_transitions(report, path, settings())
    assert not any(e["key"].startswith("missing_protective_order") and e["transition"] == "recovered" for e in transition["events"])


def test_incidents_deduplicate_and_recover_then_reopen(tmp_path):
    data, broker = trade()
    broker.orders["b"]["legs"] = []
    path = tmp_path / "test.paper-observer.json"
    first = observe(data, settings(), broker, NOW)
    assert record_transitions(first, path, settings())["events"][0]["transition"] == "opened"
    first["observed_at_utc"] = (NOW + timedelta(minutes=15)).isoformat()
    assert not record_transitions(first, path, settings())["events"]
    _, healthy = trade()
    recovered = observe(data, settings(), healthy, NOW)
    assert record_transitions(recovered, path, settings())["events"][0]["transition"] == "recovered"
    assert record_transitions(first, path, settings())["events"][0]["transition"] == "opened"


def test_optional_alert_failure_retries_and_records_recovery(tmp_path, monkeypatch):
    data, broker = trade()
    broker.orders["b"]["legs"] = []
    report = observe(data, settings(), broker, NOW)
    path = tmp_path / "test.paper-observer.json"
    sends = []
    def send(*args, **kwargs):
        sends.append(kwargs)
        return len(sends) > 1
    monkeypatch.setattr("alphaos.monitoring.incidents.alerts.send_alert", send)
    assert record_transitions(report, path, settings(), notify=True)["pending_count"] == 1
    assert record_transitions(report, path, settings(), notify=True)["delivered"]
    assert not record_transitions(report, path, settings(), notify=True)["events"]
    assert len(sends) == 2
    assert "AAPL" not in sends[0]["message"] and "fixture-secret" not in sends[0]["message"]


@pytest.mark.parametrize("over", [{"ALPACA_BASE_URL": "https://api.alpaca.markets"}, {"ALPACA_PAPER": "false"}, {"REAL_TRADING_ENABLED": "true"}, {"ALPHAOS_MODE": "mock"}])
def test_unsafe_host_mode_rejected_before_transport(over):
    # Bypass Settings' own earlier refusal to exercise observer's guard too.
    s = settings()
    names = {"ALPACA_BASE_URL": "alpaca_base_url", "ALPACA_PAPER": "alpaca_paper", "REAL_TRADING_ENABLED": "real_trading_enabled_raw", "ALPHAOS_MODE": "mode"}
    s = SimpleNamespace(**vars(s))
    for key, value in over.items():
        setattr(s, names[key], value == "true" if key == "ALPACA_PAPER" else value)
    with pytest.raises(ObservationRefused):
        PaperReads(s, "account", session=object())


class Session:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        status, body = self.responses.pop(0)
        return SimpleNamespace(status_code=status, headers={"Date": "fixture-date"},
                               iter_content=lambda chunk_size: [json.dumps(body).encode()], close=lambda: None)


def test_wrong_account_and_unpinned_access_rejected():
    session = Session([(200, {"id": "other", "status": "ACTIVE"})])
    broker = PaperReads(settings(), "account", session=session)
    with pytest.raises(ObservationRefused):
        broker.positions()
    with pytest.raises(ObservationRefused, match="wrong_account"):
        broker.account()
    with pytest.raises(ObservationRefused):
        broker.positions()
    assert len(session.calls) == 1
    with pytest.raises(ObservationRefused):
        PaperReads(settings(), "", session=session)


def test_get_only_transport_no_redirect_or_credential_output():
    session = Session([(200, {"id": "account", "status": "ACTIVE"}), (404, {}), (302, {})])
    broker = PaperReads(settings(), "account", session=session)
    account = broker.account()
    assert broker.exact_order(client_id="c") is None
    with pytest.raises(BrokerReadError, match="broker_http_302"):
        broker.positions()
    assert all(url.startswith("https://paper-api.alpaca.markets/") and kw["allow_redirects"] is False for url, kw in session.calls)
    assert "fixture-secret" not in json.dumps(broker.receipts + [account])


def test_order_normalization_preserves_ids_and_source_timestamps():
    value = order({"id": "b", "client_order_id": "c", "status": "partially_filled", "filled_qty": "4", "updated_at": STAMP, "legs": [{"id": "stop", "type": "stop", "stop_price": "95"}]})
    assert value["broker_order_id"] == "b" and value["updated_at"] == STAMP
    assert value["legs"][0]["broker_order_id"] == "stop"


def test_snapshot_requires_read_only_and_does_not_write_db(tmp_path):
    path = tmp_path / "ledger.db"
    writer = JournalStore(str(path))
    writer.insert("scan_batches", {"scan_batch_id": "scan", "scan_type": "post_open", "status": "completed", "universe_count": 20, "candidates_found": 0, "started_at_utc": STAMP, "completed_at_utc": STAMP})
    with pytest.raises(ValueError, match="read_only_journal_required"):
        ledger_snapshot(writer, settings(), NOW)
    writer.close()
    before = path.read_bytes()
    reader = JournalStore(str(path), read_only=True)
    snap = ledger_snapshot(reader, settings(), NOW)
    reader.close()
    assert snap["orders"] == [] and path.read_bytes() == before


def test_ledger_only_cannot_claim_broker_health():
    report = observe(local(), settings(), now=NOW)
    assert not report["ok"] and not report["broker_verified"]
    assert codes(report) == {"broker_unverified"}


def test_take_profit_and_written_exit_prices_checked():
    data, broker = trade()
    data["positions"][0]["target_price"] = 110
    data["orders"][0].update(stop_loss_price=95, take_profit_price=110)
    broker.orders["b"]["legs"][0]["stop_price"] = 90
    assert {"missing_profit_order", "protective_terms_mismatch"} <= codes(observe(data, settings(), broker, NOW))


def test_poll_deadline_and_open_order_truncation_fail_closed():
    broker = PaperReads(settings(), "account", session=Session([]))
    broker.deadline = 0
    with pytest.raises(BrokerReadError, match="poll_deadline_exceeded"):
        broker.account()
    session = Session([(200, {"id": "account", "status": "ACTIVE"}), (200, [{}] * 500)])
    broker = PaperReads(settings(), "account", session=session)
    broker.account()
    with pytest.raises(BrokerReadError, match="open_orders_incomplete"):
        broker.open_orders()


def test_out_of_scope_order_is_not_falsely_recovered(tmp_path):
    data, broker = trade()
    broker.orders["b"]["filled_qty"] = 1
    report = observe(data, settings(), broker, NOW)
    path = tmp_path / "test.paper-observer.json"
    record_transitions(report, path, settings())
    later = observe(local(), settings(), Broker(), NOW)
    events = record_transitions(later, path, settings())["events"]
    assert not any(e["key"] == "fill_quantity_mismatch:o" and e["transition"] == "recovered" for e in events)


def test_full_sqlite_chain_old_open_origin_and_read_only_report(tmp_path):
    path = tmp_path / "ledger.db"
    data, broker = trade()
    writer = JournalStore(str(path))
    writer.insert("paper_orders", {**data["orders"][0], "execution_source": "alpaca_paper", "created_at_utc": (NOW - timedelta(days=30)).isoformat()})
    writer.insert("positions", {**data["positions"][0], "execution_source": "alpaca_paper", "status": "open"})
    chain = deepcopy(data["chains"]["o"])
    chain["proposal"].update(symbol="AAPL", eval_id="eval", card_id="card", card_version=1)
    chain["candidate"]["price_snapshot_id"] = "snap"
    chain["evaluation"]["symbol"] = "AAPL"
    chain["card"].update(version=1, state="active", content_hash="fixture")
    chain["snapshot"].update(symbol="AAPL", provider="alpaca", feed="iex")
    chain["approval"]["proposal_id"] = "p"
    for kind, table in [("proposal", "trade_proposals"), ("candidate", "candidates"), ("evaluation", "openai_evaluations"), ("risk", "risk_checks"), ("approval", "approvals"), ("snapshot", "price_snapshots"), ("lineage", "lineage_snapshots"), ("card", "setup_cards")]:
        if kind == "approval":
            chain[kind]["approval_id"] = "approval"
        writer.insert(table, chain[kind])
    writer.insert("paper_fills", {**data["fills"]["o"][0], "fill_id": "f", "order_id": "o", "symbol": "AAPL", "execution_source": "alpaca_paper"})
    writer.close()
    before = path.read_bytes()
    reader = JournalStore(str(path), read_only=True)
    snapshot = ledger_snapshot(reader, settings(), NOW)
    reader.close()
    assert [r["order_id"] for r in snapshot["orders"]] == ["o"]
    assert snapshot["chains"]["o"]["risk"]["risk_check_id"] == "risk"
    report = observe(snapshot, settings(), broker, NOW)
    assert not {"lineage_missing", "risk_receipt_mismatch", "fill_quantity_mismatch"} & codes(report)
    record_transitions(report, tmp_path / "test.paper-observer.json", settings())
    assert path.read_bytes() == before


def test_future_source_timestamp_and_short_protection_direction():
    data, broker = trade()
    data["position_data"]["pos"]["quote_timestamp"] = (NOW + timedelta(minutes=1)).isoformat()
    assert "position_data_stale" in codes(observe(data, settings(), broker, NOW))
    data, broker = trade()
    data["positions"][0]["direction"] = "short"
    broker.position_rows[0].update(qty=-10, side="short")
    assert "missing_protective_order" in codes(observe(data, settings(), broker, NOW))
    broker.orders["b"]["legs"][0]["side"] = "buy"
    assert "missing_protective_order" not in codes(observe(data, settings(), broker, NOW))


def test_stale_position_incident_cannot_recover_on_broker_failure(tmp_path):
    data, broker = trade()
    data["position_data"]["pos"]["quote_timestamp"] = (NOW - timedelta(minutes=2)).isoformat()
    path = tmp_path / "test.paper-observer.json"
    record_transitions(observe(data, settings(), broker, NOW), path, settings())
    def fail():
        raise BrokerReadError("fixture")
    broker.account = fail
    events = record_transitions(observe(data, settings(), broker, NOW), path, settings())["events"]
    assert not any(e["key"] == "position_data_stale:pos" and e["transition"] == "recovered" for e in events)


def test_repeated_failed_scan_receipts_are_one_incident(tmp_path):
    data = local()
    data["scan"]["status"] = "failed"
    path = tmp_path / "test.paper-observer.json"
    record_transitions(observe(data, settings(), Broker(), NOW), path, settings())
    data["scan"]["scan_batch_id"] = "new-scan"
    assert record_transitions(observe(data, settings(), Broker(), NOW), path, settings())["events"] == []


@pytest.mark.parametrize("positions", [[{"symbol": "AAPL", "qty": "nan", "side": "long"}], [None], [{"symbol": "AAPL", "qty": 1, "side": "long"}] * 2])
def test_malformed_positions_fail_before_flat_inference(positions):
    session = Session([(200, {"id": "account", "status": "ACTIVE"}), (200, positions)])
    broker = PaperReads(settings(), "account", session=session)
    broker.account()
    with pytest.raises(BrokerReadError, match="positions_invalid_shape"):
        broker.positions()


def test_cli_ledger_only_never_constructs_broker_or_sends_alert(monkeypatch, capsys):
    from alphaos.monitoring import __main__ as cli
    monkeypatch.setattr(cli, "load_settings", settings)
    monkeypatch.setattr(cli, "JournalStore", lambda *a, **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, "ledger_snapshot", lambda *a, **kw: local())
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected broker/state/alert action")
    monkeypatch.setattr(cli, "PaperReads", forbidden)
    monkeypatch.setattr(cli, "record_transitions", forbidden)
    monkeypatch.setattr("alphaos.monitoring.incidents.alerts.send_alert", forbidden)
    assert cli.main(["--ledger-only"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert not report["broker_verified"] and report["completed_at_utc"]


def test_cli_missing_pin_rejected_before_loading_secrets(monkeypatch):
    from alphaos.monitoring import __main__ as cli
    def forbidden():
        raise AssertionError("Should not load credentials without pin")
    monkeypatch.setattr(cli, "load_settings", forbidden)
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
