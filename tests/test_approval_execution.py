"""Approval -> execution path + read-only dashboard render (Roadmap 3).

Covers the operator console contract:
* a generated proposal does nothing until explicitly approved,
* approval re-runs freshness/spread/risk gates AT APPROVAL TIME and only then
  creates exactly one simulated order/fill/position (with trade_id preserved),
* stale-quote / wide-spread approvals are blocked and journaled,
* approving twice never creates a duplicate order,
* rejection journals and removes the proposal from the actionable queue,
* the Streamlit dashboard writes nothing on render (no scan/startup audit rows),
* the new CLI approve/reject/proposals commands work end-to-end.

All offline, in-memory, mock mode. No real money, no network.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from alphaos.config.settings import load_settings
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from conftest import make_settings, inject_pending_proposal


def _orch(**overrides):
    s = make_settings(APPROVAL_MODE="manual", **overrides)
    journal = JournalStore(":memory:")
    return Orchestrator(settings=s, journal=journal)


def _counts(journal):
    return (
        journal.count_rows("paper_orders"),
        journal.count_rows("paper_fills"),
        journal.count_open_positions(),
    )


# --------------------------------------------------------------------------- 1
def test_proposal_generated_but_not_approved_creates_no_order():
    orch = _orch()
    pid, _ = inject_pending_proposal(orch)
    # No approval issued -> absolutely nothing executed.
    assert _counts(orch.journal) == (0, 0, 0)
    assert orch.journal.proposal_by_id(pid)["status"] == "pending_approval"
    # It is sitting in the actionable queue.
    assert any(p["proposal_id"] == pid for p in orch.list_open_proposals())
    orch.close()


# --------------------------------------------------------------------------- 2
def test_approved_fresh_creates_exactly_one_order_fill_position_with_trade_id():
    orch = _orch()
    pid, _ = inject_pending_proposal(orch)

    ok, msg = orch.approve_proposal(pid, approver="tester")
    assert ok, msg

    j = orch.journal
    assert _counts(j) == (1, 1, 1)  # exactly one of each
    assert j.proposal_by_id(pid)["status"] == "filled"
    # And it has left the actionable queue.
    assert all(p["proposal_id"] != pid for p in orch.list_open_proposals())

    # trade_id correlation: proposal -> order -> fill -> position.
    trade_id = j.proposal_by_id(pid)["trade_id"]
    assert trade_id
    orders = j.orders_for_proposal(pid)
    assert len(orders) == 1 and orders[0]["trade_id"] == trade_id
    fills = j.fills_for_order(orders[0]["order_id"])
    assert len(fills) == 1 and fills[0]["trade_id"] == trade_id
    pos = j.position_for_trade(trade_id)
    assert pos and pos["proposal_id"] == pid and pos["status"] == "open"
    orch.close()


# --------------------------------------------------------------------------- 3
def test_approval_with_stale_quote_is_blocked_and_journaled():
    orch = _orch()
    pid, entry = inject_pending_proposal(orch)

    base = dict(orch.market.get_snapshot("AAPL"))
    base.update(
        last_price=entry,                       # no drift; the freshness gate must fire first
        market_session="regular",
        quote_timestamp="2020-01-01T00:00:00+00:00",
        source_timestamp="2020-01-01T00:00:00+00:00",
        bar_timestamp="2020-01-01T00:00:00+00:00",
    )
    orch.market.get_snapshot = lambda symbol: base

    ok, msg = orch.approve_proposal(pid, approver="tester")
    assert ok is False
    assert "not usable" in msg

    assert _counts(orch.journal) == (0, 0, 0)
    # Stale data is transient -> the proposal stays approvable for a retry.
    assert orch.journal.proposal_by_id(pid)["status"] == "pending_approval"
    events = orch.journal.query(
        "SELECT * FROM system_events WHERE category='approval' AND message LIKE '%blocked%'"
    )
    assert events, "stale-quote block should be journaled to system_events"
    orch.close()


# --------------------------------------------------------------------------- 4
def test_approval_with_wide_spread_is_blocked_and_journaled():
    orch = _orch(MAX_SPREAD_PCT="0.02")
    pid, entry = inject_pending_proposal(orch)

    base = dict(orch.market.get_snapshot("AAPL"))
    base.update(last_price=entry, spread_pct=0.5)   # fresh, but a 50% spread
    orch.market.get_snapshot = lambda symbol: base

    ok, msg = orch.approve_proposal(pid, approver="tester")
    assert ok is False
    assert "WIDE_SPREAD" in msg

    assert _counts(orch.journal) == (0, 0, 0)
    assert orch.journal.proposal_by_id(pid)["status"] == "blocked"
    rc = orch.journal.risk_check_for_proposal(pid)
    assert rc and rc["result"] == "fail" and rc["spread_check_result"] == "fail"
    orch.close()


# --------------------------------------------------------------------------- 5
def test_double_approval_creates_no_duplicate_order():
    orch = _orch()
    pid, _ = inject_pending_proposal(orch)

    ok1, _ = orch.approve_proposal(pid, approver="tester")
    assert ok1
    ok2, msg2 = orch.approve_proposal(pid, approver="tester")
    assert ok2 is False
    assert "already" in msg2 or "not approvable" in msg2

    # Still exactly one of each — no duplicate.
    assert _counts(orch.journal) == (1, 1, 1)
    orch.close()


# --------------------------------------------------------------------------- 6
def test_rejected_proposal_creates_no_order_and_is_journaled():
    orch = _orch()
    pid, _ = inject_pending_proposal(orch)

    ok, msg = orch.reject_proposal(pid, approver="tester", reason="not convinced")
    assert ok and msg == "rejected"

    assert _counts(orch.journal) == (0, 0, 0)
    assert orch.journal.proposal_by_id(pid)["status"] == "rejected"
    # Rejection is journaled as an approvals row + leaves the actionable queue.
    rejections = orch.journal.query("SELECT * FROM approvals WHERE label='REJECTED'")
    assert len(rejections) == 1 and rejections[0]["proposal_id"] == pid
    assert all(p["proposal_id"] != pid for p in orch.list_open_proposals())
    orch.close()


# ----------------------------------------------------------- trade_id -> monitor
def test_trade_id_links_proposal_through_to_monitoring_snapshot():
    orch = _orch()
    pid, entry = inject_pending_proposal(orch)
    assert orch.approve_proposal(pid)[0]
    trade_id = orch.journal.proposal_by_id(pid)["trade_id"]
    pos = orch.journal.position_for_trade(trade_id)

    # Monitor once with a non-exit price (between stop and target) -> a snapshot
    # is recorded and carries the same trade_id; the position stays open.
    orch.run_monitor_once(price_overrides={"AAPL": entry})
    snaps = orch.journal.monitoring_snapshots_for_position(pos["position_id"])
    assert snaps and snaps[-1]["trade_id"] == trade_id
    assert orch.journal.position_by_id(pos["position_id"])["status"] == "open"
    orch.close()


# ------------------------------------------------------------- dashboard render
def _fake_st():
    """A Streamlit stand-in: buttons are never 'pressed', layout helpers return
    the right number of context-manager mocks. Lets us render headlessly.

    OPS-A: represents a genuine loopback connection by default (real IP +
    real bind address, not just "any truthy mock") so this stand-in exercises
    the SAME allow path a real local browser hits -- every existing test
    using this fixture is implicitly asserting "renders fully for a real
    local connection". Tests that want to exercise the refusal path build
    their own mock with a non-loopback ip_address/bind address instead of
    starting from this one (see test_dashboard.py's
    test_non_loopback_request_is_refused_*)."""
    st = MagicMock(name="st")
    st.button.return_value = False
    st.sidebar.button.return_value = False
    st.checkbox.return_value = False
    st.text_input.return_value = ""
    st.selectbox.return_value = None
    st.tabs.side_effect = lambda labels: [MagicMock(name=f"tab{i}") for i in range(len(labels))]
    st.context.ip_address = "127.0.0.1"
    st.get_option.return_value = "127.0.0.1"

    def _cols(spec):
        n = spec if isinstance(spec, int) else len(spec)
        out = []
        for i in range(n):
            col = MagicMock(name=f"col{i}")
            col.button.return_value = False   # column-buttons are never "pressed" on render
            out.append(col)
        return out

    st.columns.side_effect = _cols
    return st


def test_dashboard_render_writes_nothing(monkeypatch):
    from alphaos.dashboard import streamlit_app

    journal = JournalStore(":memory:")
    orch = Orchestrator(settings=make_settings(), journal=journal)
    watched = (
        "scan_batches", "scheduler_runs", "config_versions", "system_events",
        "paper_orders", "paper_fills", "positions", "candidates", "trade_proposals",
    )
    before = {t: journal.count_rows(t) for t in watched}
    assert sum(before.values()) == 0  # a genuinely clean ledger to start

    monkeypatch.setattr(streamlit_app, "st", _fake_st())
    streamlit_app.main(orch=orch)   # one full render, zero user actions

    after = {t: journal.count_rows(t) for t in watched}
    assert after == before, f"render wrote rows: {{k: (before, after)}} -> {after}"
    orch.close()


# -------------------------------------------------------------------- CLI wiring
def test_cli_proposals_approve_reject(tmp_path, monkeypatch):
    from alphaos import __main__ as cli

    db = str(tmp_path / "cli.db")
    env = {
        "ALPHAOS_MODE": "mock", "APPROVAL_MODE": "manual",
        "REAL_TRADING_ENABLED": "false", "ALPHAOS_DB_PATH": db,
        "MAX_AUTO_APPROVALS_PER_DAY": "1",
    }
    monkeypatch.setattr(cli, "load_settings", lambda: load_settings(load_env_file=False, env=env))

    # Seed a pending proposal on the same file-backed DB.
    seed = Orchestrator(settings=load_settings(load_env_file=False, env=env))
    pid, _ = inject_pending_proposal(seed)
    seed.close()

    assert cli.main(["proposals"]) == 0          # lists the queue
    assert cli.main(["approve", pid]) == 0        # approves + fills

    check = Orchestrator(settings=load_settings(load_env_file=False, env=env))
    assert check.journal.count_rows("paper_fills") == 1
    assert check.journal.count_open_positions() == 1

    # Second approve is idempotent: non-zero exit, no duplicate.
    assert cli.main(["approve", pid]) == 1
    assert check.journal.count_rows("paper_fills") == 1
    check.close()


def test_cli_reject_removes_from_queue(tmp_path, monkeypatch):
    from alphaos import __main__ as cli

    db = str(tmp_path / "cli2.db")
    env = {
        "ALPHAOS_MODE": "mock", "APPROVAL_MODE": "manual",
        "REAL_TRADING_ENABLED": "false", "ALPHAOS_DB_PATH": db,
    }
    monkeypatch.setattr(cli, "load_settings", lambda: load_settings(load_env_file=False, env=env))

    seed = Orchestrator(settings=load_settings(load_env_file=False, env=env))
    pid, _ = inject_pending_proposal(seed)
    seed.close()

    assert cli.main(["reject", pid, "--reason", "cli test"]) == 0

    check = Orchestrator(settings=load_settings(load_env_file=False, env=env))
    assert check.journal.proposal_by_id(pid)["status"] == "rejected"
    assert check.journal.count_rows("paper_fills") == 0
    assert check.list_open_proposals() == []
    check.close()
