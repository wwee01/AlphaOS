"""TIME-2 (docs/roadmap/alphaos-time2-broker-time-exit-spec.md): broker-side
time-exit ENFORCEMENT for alpaca_paper positions -- max_holding_days has
NEVER closed a live position (0 time_expiry exits in the system's history)
because PositionManager.monitor() is architecturally barred from ever
touching the broker. Enforcement lives in OrderManager.enforce_time_exits(),
wired into reconcile() immediately after _cancel_stale_entries() (ENTRY-TTL-
1's own wiring/rationale, reused).

Covers all 11 spec test obligations PLUS the blind Opus audit's fixup round
(2026-08-28):

 1. Flag OFF -> byte-identical to today (differential probe).
 2. Flag ON, past window -> cancel -> verify -> close, IN THAT ORDER
    (asserts call ORDERING against the fake broker, not just end state).
 3. Flag ON, NOT past window -> untouched.
 4. Per-trade window honored -- the operator's ruling (2026-08-26): each
    position's OWN stamped max_holding_days, never a forced constant.
 5. A legacy position stamped 3 exits at 3 (no floor, no forcing).
 6. Cancel unverified -> NO close is submitted (the safety test that
    matters most).
 7. Close raises after legs were cancelled+verified -> position ends
    protected (re-placed OCO) OR a CRITICAL protection incident is opened
    through the SAME mechanism the broker protection watchdog uses; never
    left naked.
 8. TIME-2 makes zero market-data calls -- broker-managed enforcement
    cannot be corrupted by a stale/broken price feed the way the local
    watchdog's own freshness guard exists to prevent, because it never
    consults one; the exit price always comes from the broker's own fill.
    NOTE (audit MEDIUM-3, declared deviation): spec obligation 8's literal
    "stale data -> no action" guard is NOT implemented as a FreshnessGuard
    gate -- a broker liquidation is not a price-conditional decision, so
    there is no price read to guard. The auditor judged this defensible but
    undeclared; it is now declared explicitly here.
 9. exit_reason='time_expiry' reaches trade_outcomes with a correct
    realized_r through the SAME exit path every other exit uses.
10. Both pre-existing architecture ratchet tests still pass unmodified
    (test_entry_ttl.py's broker-isolation guard, test_daily_brief.py's
    report-module isolation guard) -- run alongside this file, not
    duplicated wholesale here; a lightweight local echo of the
    broker-isolation guard is included as cheap insurance (see
    test_position_manager_still_has_no_broker_strings below).
11. Cold-import subprocess check on every touched module.

Audit fixup round (each a named test below):
 BLOCKER-1: the re-placed OCO's broker_order_id is persisted onto the
    position so the NEXT enforcement pass cancels the RIGHT (new) legs, not
    the dead old bracket's -- proven with a two-pass probe.
 BLOCKER-2: a broker-position precondition (list_positions()) is checked
    BOTH in the main enforcement path and in the recovery path, so a
    symbol the broker already reports flat is never acted on -- proven
    with a two-pass probe simulating a deferred liquidation completing
    between passes.
 HIGH-1: a partial liquidation fill is never recorded as a full close.
 HIGH-2: submit_protective_oco (a NEW order) is gated on the kill switch.
 HIGH-3: run_monitor_job's docstring is amended to stop claiming an
    absolute ("never submits/closes") that TIME-2 (when armed) violates.
 HIGH-4: check_position() correctly reports a re-protected position as
    PROTECTED (a direct consequence of the BLOCKER-1 fix, verified here).
 MEDIUM-1: one position's unexpected failure (incl. step 4, the exit
    record write) never aborts the pass for other positions.
 MEDIUM-2: the flag is now also captured in journal_store's own
    provenance config-snapshot fingerprint, not just build_config_hashes().
 ALSO: the protection-incident coupling goes through a PUBLIC
    protection_watchdog.open_protection_incident() entry point, not the
    private _record_check.

Round 2 (re-audit APPROVE-WITH-FINDINGS, 2026-08-28) adds:
 R1 (gates arming): a failed LOCAL persist of the re-placed OCO's
    broker_order_id reproduced the B1 orphan through a non-broker failure
    (disk I/O, locked DB, full disk) -- the write is now wrapped, and on
    failure falls through to the SAME CRITICAL incident path a failed
    re-placement already uses. Proven with an injected DB write failure,
    run twice, asserting no false success and no later false close.
 R2: a submit that returns no broker_order_id (same remedy as R1) is now
    also treated as a replace failure, never success.
 R3 (nit): the broker-flat stand-down is now classified as a deferral,
    not an error -- it was a correct, intended no-op inflating the error
    count an operator reads.
 M4 (orchestrator.py, explicit one-file authorization): a time-exit close
    lands in recon["time_exits_closed"], not recon["exits"] -- routed into
    Orchestrator.run_monitor_once()'s summary so an armed operator's
    monitor pass doesn't log "0 exit(s)" for a pass that actually closed a
    position.

All datetimes are fixed/injected (enforce_time_exits(now=...) takes an
explicit clock; no test relies on real wall-clock "today") -- house law,
matching test_hold1_trading_day_holding_period.py's own convention. Offline,
in-memory, mock/paper mode. No real money, no network (NTFY_TOPIC unset ->
alerts.send_alert no-ops without ever reaching urlopen). The one test that
touches KillSwitch (a FILE-backed marker) points it at a tmp path and always
releases it, never the project's own data/KILL_SWITCH.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone

from alphaos.broker.alpaca_client import AlpacaClient
from alphaos.constants import ExecutionSource
from alphaos.execution import protection_watchdog
from alphaos.execution.order_manager import OrderManager
from alphaos.journal.journal_store import JournalStore
from alphaos.safety import KillSwitch
from alphaos.util.ids import new_id
from alphaos.util.market_calendar import is_trading_day, trading_days_between
from conftest import make_settings

_UTC = timezone.utc


def _utc(d: date) -> datetime:
    # 17:00 UTC lands mid-day ET regardless of EST/EDT -- safely inside the
    # same ET calendar date as the UTC date (test_hold1's own convention).
    return datetime(d.year, d.month, d.day, 17, 0, 0, tzinfo=_UTC)


def _opened_date_for(now_date: date, trading_days_ago: int) -> date:
    """The calendar date such that trading_days_between(result, now_date) ==
    trading_days_ago -- derived from the SAME market_calendar helpers the
    code under test uses (never hand-counted), so the fixture can never
    silently drift out of sync with a calendar rule change."""
    d = now_date
    counted = 0
    while counted < trading_days_ago:
        d -= timedelta(days=1)
        if is_trading_day(d):
            counted += 1
    assert trading_days_between(d, now_date) == trading_days_ago
    return d


# A fixed, known trading day (Tuesday, no holiday nearby) -- matches
# test_hold1_trading_day_holding_period.py's own reference date.
_NOW_DATE = date(2026, 7, 14)
_NOW = _utc(_NOW_DATE)


# --------------------------------------------------------------------- fakes
class _FakeLeg:
    def __init__(self, role, limit_price=None, stop_price=None, time_in_force="gtc"):
        self.id = uuid.uuid4().hex
        self.order_type = "limit" if role == "take_profit" else "stop"
        self.limit_price = limit_price
        self.stop_price = stop_price
        self.status = "new"
        self.filled_qty = 0
        self.filled_avg_price = None
        self.time_in_force = time_in_force
        self.legs: list = []


class _FakeBracketOrder:
    """A filled bracket entry -- the parent order get_order(boid) returns,
    carrying the two live protective legs. Also reused (with status/fill
    overridden by the caller) to shape a re-placed protective OCO's
    response, since a real Alpaca OCO order carries its sibling leg the
    same way a bracket's children do -- BLOCKER-1's fix depends on the next
    pass being able to read genuine stop/target legs off it."""

    def __init__(self, symbol, qty, side, entry, target, stop, tif="gtc"):
        self.id = uuid.uuid4().hex
        self.client_order_id = uuid.uuid4().hex
        self.symbol = symbol
        self.side = side
        self.qty = qty
        self.order_class = "bracket"
        self.status = "filled"
        self.filled_qty = qty
        self.filled_avg_price = entry
        self.limit_price = entry
        self.stop_price = None
        self.submitted_at = "2026-07-01T13:30:00Z"
        self.filled_at = "2026-07-01T13:31:00Z"
        self.time_in_force = tif
        self.legs = [
            _FakeLeg("take_profit", limit_price=target, time_in_force=tif),
            _FakeLeg("stop_loss", stop_price=stop, time_in_force=tif),
        ]


class _FakeSimpleOrder:
    """A close order (no legs of its own)."""

    def __init__(self, symbol, qty, filled_qty, side="sell", filled_avg_price=None, status=None):
        self.id = uuid.uuid4().hex
        self.client_order_id = uuid.uuid4().hex
        self.symbol = symbol
        self.side = side
        self.qty = qty
        self.order_class = "simple"
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price
        self.status = status or ("filled" if filled_avg_price is not None else "accepted")
        self.limit_price = None
        self.stop_price = None
        self.submitted_at = "2026-07-14T14:00:00Z"
        self.filled_at = "2026-07-14T14:00:01Z" if filled_avg_price is not None else None
        self.time_in_force = "day"
        self.legs: list = []


class _FakeBrokerPosition:
    def __init__(self, symbol, qty):
        self.symbol = symbol
        self.qty = qty
        self.side = "long"
        self.avg_entry_price = None
        self.market_value = None
        self.unrealized_pl = None
        self.current_price = None


class FakeTradingClient:
    """SDK-agnostic fake -- same interface AlpacaClient calls, same pattern
    as tests/test_alpaca_paper_execution.py's FakeTradingClient, extended
    with close_position/submit_oco (TIME-2's new broker capabilities), a
    REAL get_all_positions() driven by an explicit qty-by-symbol map (audit
    fixup BLOCKER-2 needs this to be genuinely controllable across passes),
    and a unified call_log so tests can assert exact call ORDER, not just
    which calls happened."""

    FAKE = True

    def __init__(self):
        self.orders: dict = {}        # id (parent OR leg OR close/oco order) -> object
        self._by_symbol: dict = {}    # symbol -> the CURRENT live bracket/OCO parent order
        self._broker_qty: dict = {}   # symbol -> current broker qty (0/absent = flat)
        self.call_log: list = []      # [(op, id_or_symbol), ...] in call order
        self.raise_on_cancel_for: set = set()
        self.raise_on_close_for: set = set()
        self.raise_on_oco_for: set = set()
        self.raise_on_list_positions = False
        self.cancel_result_status: dict = {}  # leg_id -> status to set instead of "canceled"
        self.close_fill_price: dict = {}      # symbol -> price (explicit None = leave unfilled)
        self.close_fill_qty: dict = {}        # symbol -> override filled_qty (partial-fill tests)

    # ---- fixture builder ----
    def register_bracket(self, symbol, qty, side, entry, target, stop, tif="gtc", broker_order_id=None):
        o = _FakeBracketOrder(symbol, qty, side, entry, target, stop, tif)
        if broker_order_id:
            o.id = broker_order_id
        self.orders[o.id] = o
        for leg in o.legs:
            self.orders[leg.id] = leg
        self._by_symbol[symbol] = o
        self._broker_qty[symbol] = qty
        return o

    def set_broker_qty(self, symbol, qty):
        """Test-driver hook: directly set the broker's CURRENT reported qty
        for a symbol -- used to simulate a fill/close completing BETWEEN
        enforce_time_exits() passes (BLOCKER-2's exact scenario)."""
        if qty:
            self._broker_qty[symbol] = qty
        else:
            self._broker_qty.pop(symbol, None)

    def leg(self, symbol, role):
        o = self._by_symbol[symbol]
        want = "limit" if role == "take_profit" else "stop"
        return next(leg for leg in o.legs if leg.order_type == want)

    # ---- SDK-agnostic interface used by AlpacaClient ----
    def get_order_by_id(self, oid):
        self.call_log.append(("get_order", oid))
        return self.orders[oid]

    def cancel_order_by_id(self, oid):
        self.call_log.append(("cancel", oid))
        if oid in self.raise_on_cancel_for:
            raise RuntimeError(f"cancel failed for {oid}")
        leg = self.orders[oid]
        leg.status = self.cancel_result_status.get(oid, "canceled")

    def close_position(self, symbol):
        self.call_log.append(("close", symbol))
        if symbol in self.raise_on_close_for:
            raise RuntimeError(f"close failed for {symbol}")
        parent = self._by_symbol[symbol]
        requested_qty = self._broker_qty.get(symbol, parent.qty)
        price = self.close_fill_price.get(symbol, "__default__")
        if price == "__default__":
            price = parent.filled_avg_price
        filled_qty = self.close_fill_qty.get(symbol, requested_qty if price is not None else 0)
        order = _FakeSimpleOrder(symbol, requested_qty, filled_qty, filled_avg_price=price)
        self.orders[order.id] = order
        if price is not None:
            # A real liquidation only reduces the broker's book once it
            # actually fills -- an accepted-but-unfilled close (price=None)
            # must leave the reported qty UNCHANGED (BLOCKER-2's fixture
            # needs this: the fill only happens when the TEST explicitly
            # calls set_broker_qty(), simulating it completing between
            # passes).
            remaining = max(self._broker_qty.get(symbol, requested_qty) - filled_qty, 0)
            self.set_broker_qty(symbol, remaining)
        return order

    def submit_oco(self, spec):
        self.call_log.append(("oco", spec["symbol"]))
        if spec["symbol"] in self.raise_on_oco_for:
            raise RuntimeError(f"oco failed for {spec['symbol']}")
        order = _FakeBracketOrder(spec["symbol"], spec["qty"], spec["side"],
                                  entry=None, target=spec["target"], stop=spec["stop"], tif=spec["tif"])
        order.status, order.filled_qty, order.filled_avg_price = "accepted", 0, None
        self.orders[order.id] = order
        for leg in order.legs:
            self.orders[leg.id] = leg
        # The re-placed order is now the LIVE protective order for this
        # symbol (a real Alpaca account has only one at a time here).
        self._by_symbol[spec["symbol"]] = order
        return order

    def get_all_positions(self):
        self.call_log.append(("list_positions", None))
        if self.raise_on_list_positions:
            raise RuntimeError("list_positions failed")
        return [_FakeBrokerPosition(sym, qty) for sym, qty in self._broker_qty.items() if qty]

    def get_orders(self):
        return []


def _paper_om(fake, journal=None, kill_switch=None, **over):
    cfg = {
        "ALPHAOS_MODE": "paper", "EXECUTION_PROVIDER": "alpaca_paper",
        "ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "ALPACA_PAPER": "true",
        "ALPACA_BASE_URL": "https://paper-api.alpaca.markets", "REAL_TRADING_ENABLED": "false",
        "TIME_EXIT_ENFORCEMENT_ENABLED": "true",
    }
    cfg.update(over)
    s = make_settings(**cfg)
    journal = journal or JournalStore(":memory:")
    alpaca = AlpacaClient(s, journal, trading_client=fake)
    om = OrderManager(s, journal, alpaca=alpaca, kill_switch=kill_switch)
    return s, journal, om


def _open_broker_position(journal, fake, *, symbol="XLV", qty=10.0, direction="long",
                          entry=100.0, stop=94.0, target=112.0, max_holding_days=5,
                          opened_market_date: date, tif="gtc"):
    """Insert an open, broker-managed ``positions`` row AND register the
    matching bracket (with two live legs, and a matching broker-side
    position qty) at the fake broker under the SAME broker_order_id --
    independent of OrderManager.execute_proposal()/reconcile()'s own fill
    flow (already covered by tests/test_alpaca_paper_execution.py), so
    trading-day fixtures here stay exact and hand-controlled, matching
    test_hold1's own directness."""
    side = "sell" if direction == "short" else "buy"
    boid = uuid.uuid4().hex
    fake.register_bracket(symbol, qty, side, entry, target, stop, tif, broker_order_id=boid)

    position_id = new_id("pos")
    journal.insert("positions", {
        "position_id": position_id, "order_id": new_id("ord"), "symbol": symbol,
        "direction": direction, "strategy": "swing", "qty": qty, "avg_entry_price": entry,
        "stop_price": stop, "target_price": target, "max_holding_days": max_holding_days,
        "opened_at": f"{opened_market_date.isoformat()}T14:00:00+00:00",
        "opened_market_date": opened_market_date.isoformat(),
        "status": "open", "current_price": entry, "unrealized_pnl": 0.0,
        "execution_source": ExecutionSource.ALPACA_PAPER.value, "broker_order_id": boid,
        "is_short": 1 if direction == "short" else 0, "trade_id": new_id("trade"),
    })
    return journal.one("SELECT * FROM positions WHERE position_id = ?", (position_id,)), boid


def _snapshot(journal) -> dict:
    """A full-content fingerprint of every table an enforcement pass could
    possibly touch -- used for the flag-OFF differential probe (obligation
    1). Row order doesn't matter for equality here since these tables are
    either empty or single-row in the tests that use this."""
    tables = ("positions", "exits", "trade_outcomes", "paper_orders",
              "paper_fills", "order_events", "system_events", "protection_checks")
    return {t: sorted(str(r) for r in journal.query(f"SELECT * FROM {t}")) for t in tables}


# ============================================================ obligation 1
def test_flag_off_is_byte_identical_to_today():
    """Differential probe: same fixture, same starting rows -- flag OFF must
    leave every table (and the broker) completely untouched, even for a
    position that is unambiguously past its window."""
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake, TIME_EXIT_ENFORCEMENT_ENABLED="false")
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    _open_broker_position(journal, fake, max_holding_days=5, opened_market_date=opened)

    before = _snapshot(journal)
    result = om.enforce_time_exits(now=_NOW)
    after = _snapshot(journal)

    assert result == {"closed": [], "deferred": [], "errors": []}
    assert before == after
    assert fake.call_log == []  # not one broker read/write was made


def test_flag_off_via_reconcile_also_untouched():
    """Same probe through the REAL entry point (reconcile()), proving the
    wiring itself is dark when the flag is off, not just the standalone
    method."""
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake, TIME_EXIT_ENFORCEMENT_ENABLED="false")
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    _open_broker_position(journal, fake, max_holding_days=5, opened_market_date=opened)

    before = _snapshot(journal)
    om.reconcile()
    after = _snapshot(journal)
    assert before == after


# ============================================================ obligation 2
def test_enforced_exit_calls_cancel_then_verify_then_close_in_order():
    """Asserts call ORDERING against the fake broker (not just the end
    state): both legs cancelled, both legs re-read (verified), and ONLY
    THEN close_position -- exactly the spec's step sequence."""
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    pos, boid = _open_broker_position(journal, fake, symbol="XLV", max_holding_days=5,
                                      opened_market_date=opened)
    tp_id = fake.leg("XLV", "take_profit").id
    sl_id = fake.leg("XLV", "stop_loss").id
    fake.close_fill_price["XLV"] = 105.0

    result = om.enforce_time_exits(now=_NOW)

    assert len(result["closed"]) == 1 and result["closed"][0]["symbol"] == "XLV"
    log = fake.call_log
    cancel_indices = [i for i, c in enumerate(log) if c[0] == "cancel" and c[1] in (tp_id, sl_id)]
    verify_indices = [i for i, c in enumerate(log) if c[0] == "get_order" and c[1] in (tp_id, sl_id)]
    close_indices = [i for i, c in enumerate(log) if c[0] == "close" and c[1] == "XLV"]
    assert len(cancel_indices) == 2, "both legs must be cancelled"
    assert len(verify_indices) == 2, "both legs must be re-read to verify the cancel"
    assert len(close_indices) == 1, "close must be submitted exactly once"
    assert max(cancel_indices) < min(verify_indices), "every cancel must precede every verify read"
    assert max(verify_indices) < close_indices[0], "every verify read must precede the close"

    # Both legs are actually cancelled at the broker.
    assert fake.leg("XLV", "take_profit").status == "canceled"
    assert fake.leg("XLV", "stop_loss").status == "canceled"

    # The position is closed and recorded.
    row = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert row["status"] == "closed"


def test_reconcile_wires_enforcement_immediately_after_stale_entry_cancel():
    """Structural proof of the wiring itself (spec 2): reconcile() must call
    _cancel_stale_entries() and THEN enforce_time_exits(), in that order --
    mirrors ENTRY-TTL-1's own wiring for the identical reason (state-sync
    has already run, so a same-pass fill is reflected before any exit
    decision)."""
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    calls: list = []
    orig_stale = om._cancel_stale_entries
    orig_enforce = om.enforce_time_exits

    def _stale_spy(*a, **kw):
        calls.append("stale")
        return orig_stale(*a, **kw)

    def _enforce_spy(*a, **kw):
        calls.append("enforce")
        return orig_enforce(*a, **kw)

    om._cancel_stale_entries = _stale_spy
    om.enforce_time_exits = _enforce_spy

    result = om.reconcile()

    assert calls == ["stale", "enforce"]
    assert "time_exits_closed" in result and "time_exits_deferred" in result and "time_exits_errors" in result


# ============================================================ obligation 3
def test_not_yet_due_position_is_untouched():
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    # One trading day short of its 5-day window.
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=4)
    pos, boid = _open_broker_position(journal, fake, symbol="PLTR", max_holding_days=5,
                                      opened_market_date=opened)

    result = om.enforce_time_exits(now=_NOW)

    assert result == {"closed": [], "deferred": [], "errors": []}
    # The only broker call made at all is the top-of-pass list_positions()
    # snapshot -- no leg/cancel/close call for a NOT-yet-due position.
    assert all(c[0] == "list_positions" for c in fake.call_log)
    row = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert row["status"] == "open"
    assert fake.leg("PLTR", "take_profit").status == "new"
    assert fake.leg("PLTR", "stop_loss").status == "new"


# ============================================================ obligation 4
def test_per_trade_window_is_honored_not_a_forced_constant():
    """The operator's ruling, pinned: a position stamped 5 exits on day 5;
    one stamped 10, opened the SAME day, does NOT exit on day 5 -- each
    position's OWN stamped max_holding_days governs, never a global
    constant."""
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    pos5, _ = _open_broker_position(journal, fake, symbol="MU", max_holding_days=5,
                                     opened_market_date=opened)
    pos10, _ = _open_broker_position(journal, fake, symbol="XLV", max_holding_days=10,
                                      opened_market_date=opened)
    fake.close_fill_price["MU"] = 50.0

    result = om.enforce_time_exits(now=_NOW)

    closed_symbols = {c["symbol"] for c in result["closed"]}
    assert closed_symbols == {"MU"}
    mu_row = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos5["position_id"],))
    xlv_row = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos10["position_id"],))
    assert mu_row["status"] == "closed"
    assert xlv_row["status"] == "open"
    # The 10-day position's broker legs were never touched.
    assert fake.leg("XLV", "take_profit").status == "new"
    assert fake.leg("XLV", "stop_loss").status == "new"


# ============================================================ obligation 5
def test_legacy_position_stamped_3_exits_at_3_no_floor_no_forcing():
    """A card-v2-era legacy position stamped max_holding_days=3 exits
    exactly at 3 -- no floor, no forcing to some other default."""
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=3)
    pos, _ = _open_broker_position(journal, fake, symbol="LEGACY", max_holding_days=3,
                                   opened_market_date=opened)
    fake.close_fill_price["LEGACY"] = 101.0

    result = om.enforce_time_exits(now=_NOW)

    assert len(result["closed"]) == 1 and result["closed"][0]["symbol"] == "LEGACY"
    row = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert row["status"] == "closed"


# ============================================================ obligation 6
def test_cancel_unverified_means_no_close_is_submitted():
    """The safety test that matters most: one leg's cancel call does NOT
    raise, but the broker's post-cancel state is still non-terminal
    (pending_cancel, still processing) -- a non-raising cancel is not proof.
    NO close may ever be submitted in this state; the position stays open
    and protected."""
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    pos, _ = _open_broker_position(journal, fake, symbol="AAPL", max_holding_days=5,
                                   opened_market_date=opened)
    tp_id = fake.leg("AAPL", "take_profit").id
    fake.cancel_result_status[tp_id] = "pending_cancel"  # accepted, not yet terminal

    result = om.enforce_time_exits(now=_NOW)

    assert result["closed"] == []
    assert len(result["deferred"]) == 1
    assert all(c[0] != "close" for c in fake.call_log)
    row = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert row["status"] == "open"
    warn = journal.one(
        "SELECT * FROM system_events WHERE category = 'time_exit_enforcement' "
        "AND message LIKE '%not fully verified%'"
    )
    assert warn is not None


def test_cancel_raises_means_no_close_is_submitted():
    """The cancel CALL itself raising is the same fail direction as an
    unverified cancel -- benign-defer, no close, retry next pass."""
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    pos, _ = _open_broker_position(journal, fake, symbol="MSFT", max_holding_days=5,
                                   opened_market_date=opened)
    sl_id = fake.leg("MSFT", "stop_loss").id
    fake.raise_on_cancel_for.add(sl_id)

    result = om.enforce_time_exits(now=_NOW)

    assert result["closed"] == []
    assert all(c[0] != "close" for c in fake.call_log)
    row = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert row["status"] == "open"


def test_leg_races_a_fill_during_cancel_window_no_close_submitted():
    """A leg's verify re-read shows it FILLED (raced a fill during the
    cancel window) -- the same hole ENTRY-TTL-1's audit found on the entry
    side. Must never close on top of this."""
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    pos, _ = _open_broker_position(journal, fake, symbol="TSLA", max_holding_days=5,
                                   opened_market_date=opened)
    sl = fake.leg("TSLA", "stop_loss")

    real_cancel = fake.cancel_order_by_id

    def _cancel_then_fill(oid):
        real_cancel(oid)
        if oid == sl.id:
            sl.status = "filled"
            sl.filled_qty = 10.0
            sl.filled_avg_price = 94.0

    fake.cancel_order_by_id = _cancel_then_fill

    result = om.enforce_time_exits(now=_NOW)

    assert result["closed"] == []
    assert all(c[0] != "close" for c in fake.call_log)
    row = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert row["status"] == "open"  # the NEXT reconcile pass's leg-fill loop records the real exit


# ============================================================ obligation 7
def test_close_fails_after_legs_cancelled_reprotects_and_stays_open():
    """Close raises after legs were cancelled+verified -- re-placement of a
    stop+target OCO succeeds -- position remains open, no CRITICAL
    incident, safe to retry next pass."""
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    pos, _ = _open_broker_position(journal, fake, symbol="NVDA", max_holding_days=5,
                                   opened_market_date=opened)
    fake.raise_on_close_for.add("NVDA")

    result = om.enforce_time_exits(now=_NOW)

    assert result["closed"] == []
    assert len(result["deferred"]) == 1
    assert result["deferred"][0].get("reprotected") is True
    assert result["deferred"][0].get("new_broker_order_id")
    assert ("oco", "NVDA") in fake.call_log
    row = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert row["status"] == "open"  # never closed
    assert protection_watchdog.has_blocking_incident(journal) is None  # protection restored


def test_close_and_reprotect_both_fail_opens_critical_incident_never_naked():
    """Close raises AND re-placing the OCO also fails -- a CRITICAL
    protection incident is opened through the SAME mechanism the broker
    protection watchdog uses, so this position blocks new entries exactly
    like any other unprotected position -- and the position record itself
    is never silently marked closed (it stays open, honestly reflecting
    that it may be naked at the broker)."""
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    pos, _ = _open_broker_position(journal, fake, symbol="AMD", max_holding_days=5,
                                   opened_market_date=opened)
    fake.raise_on_close_for.add("AMD")
    fake.raise_on_oco_for.add("AMD")

    result = om.enforce_time_exits(now=_NOW)

    assert result["closed"] == []
    assert len(result["errors"]) == 1
    assert result["errors"][0].get("critical") is True
    row = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert row["status"] == "open"  # never falsely marked closed

    incident = protection_watchdog.has_blocking_incident(journal)
    assert incident is not None
    assert incident["symbol"] == "AMD"
    assert incident["protection_status"] == "unprotected"
    assert incident["severity"] == "critical"
    assert incident["position_id"] == pos["position_id"]


# ============================================================ obligation 8
def test_enforcement_never_touches_market_data_immune_to_stale_feed():
    """Broker-side time-exit enforcement makes ZERO market-data calls -- the
    exit price always comes from the broker's own fill, never a local price
    snapshot -- so it structurally cannot be corrupted by the stale/
    unusable-data condition PositionManager's own watchdog exists to
    refuse. Proven by breaking MarketDataClient entirely and confirming
    enforcement still succeeds identically. (Audit MEDIUM-3: this IS the
    declared substitute for spec obligation 8's literal wording -- see the
    module docstring's NOTE.)"""
    import alphaos.data.market_data as market_data_mod

    def _boom(*a, **kw):
        raise AssertionError("enforce_time_exits() must never construct MarketDataClient")

    original_init = market_data_mod.MarketDataClient.__init__
    market_data_mod.MarketDataClient.__init__ = _boom
    try:
        fake = FakeTradingClient()
        _, journal, om = _paper_om(fake)
        opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
        pos, _ = _open_broker_position(journal, fake, symbol="XLV", max_holding_days=5,
                                       opened_market_date=opened)
        fake.close_fill_price["XLV"] = 107.5

        result = om.enforce_time_exits(now=_NOW)

        assert len(result["closed"]) == 1
        row = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos["position_id"],))
        assert row["status"] == "closed"
    finally:
        market_data_mod.MarketDataClient.__init__ = original_init


# ============================================================ obligation 9
def test_time_expiry_reaches_trade_outcomes_with_correct_realized_r():
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    entry, stop, qty = 100.0, 94.0, 10.0
    pos, _ = _open_broker_position(journal, fake, symbol="XLV", entry=entry, stop=stop,
                                   target=112.0, qty=qty, max_holding_days=5,
                                   opened_market_date=opened)
    exit_price = 105.0
    fake.close_fill_price["XLV"] = exit_price

    result = om.enforce_time_exits(now=_NOW)
    assert len(result["closed"]) == 1

    exit_row = journal.one("SELECT * FROM exits WHERE position_id = ?", (pos["position_id"],))
    assert exit_row is not None
    assert exit_row["exit_reason"] == "time_expiry"
    assert exit_row["exit_price"] == exit_price

    outcome = journal.one("SELECT * FROM trade_outcomes WHERE position_id = ?", (pos["position_id"],))
    assert outcome is not None
    expected_risk_per_share = abs(entry - stop)
    expected_r = round((exit_price - entry) / expected_risk_per_share, 3)
    assert outcome["realized_r"] == expected_r
    assert outcome["gross_pnl"] == round((exit_price - entry) * qty, 2)
    assert outcome["win"] == 1  # exit_price > entry
    assert outcome["exit_id"] == exit_row["exit_id"]

    # The exit fill order carries the honest alpaca_paper labelling, not a
    # simulated_internal one -- consistent with every other broker exit.
    exit_order = journal.one(
        "SELECT * FROM paper_orders WHERE execution_source = ? AND symbol = 'XLV' AND side = 'sell'",
        (ExecutionSource.ALPACA_PAPER.value,),
    )
    assert exit_order is not None


# ========================================================== obligation 10
def test_position_manager_still_has_no_broker_strings():
    """Cheap local echo of test_entry_ttl.py's own ratchet (which remains
    the authority, run unmodified alongside this file): TIME-2 must not
    have widened PositionManager's architectural broker isolation."""
    import pathlib

    from alphaos.execution import position_manager as pm_mod

    text = pathlib.Path(str(pm_mod.__file__)).read_text(encoding="utf-8")
    assert "self.alpaca." not in text
    assert ".cancel_order(" not in text
    assert ".submit_bracket(" not in text


# ========================================================== obligation 11
def test_cold_import_every_touched_module():
    for module_name in (
        "alphaos.broker.alpaca_client",
        "alphaos.execution.order_manager",
        "alphaos.execution.protection_watchdog",
        "alphaos.config.settings",
        "alphaos.journal.journal_store",
        "alphaos.scheduler.jobs",
    ):
        result = subprocess.run(
            [sys.executable, "-c", f"import {module_name}"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"cold import of {module_name!r} failed in a fresh interpreter:\n{result.stderr}"
        )


# ==================================================== config/settings axis
def test_flag_defaults_false_validated_and_moves_config_hash():
    from alphaos.lineage.config_snapshot import build_config_hashes

    default_settings = make_settings(
        ALPHAOS_MODE="paper", EXECUTION_PROVIDER="alpaca_paper",
        ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s", ALPACA_PAPER="true",
        ALPACA_BASE_URL="https://paper-api.alpaca.markets", REAL_TRADING_ENABLED="false",
    )
    assert default_settings.time_exit_enforcement_enabled is False

    on_settings = make_settings(
        ALPHAOS_MODE="paper", EXECUTION_PROVIDER="alpaca_paper",
        ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s", ALPACA_PAPER="true",
        ALPACA_BASE_URL="https://paper-api.alpaca.markets", REAL_TRADING_ENABLED="false",
        TIME_EXIT_ENFORCEMENT_ENABLED="true",
    )
    assert on_settings.time_exit_enforcement_enabled is True
    assert (
        build_config_hashes(default_settings)["config_hash"]
        != build_config_hashes(on_settings)["config_hash"]
    )


def test_flag_on_with_no_broker_connected_warns_configured_but_inert():
    """The startup-validation trap: enabled but EXECUTION_PROVIDER isn't
    alpaca_paper -- can never fire, WARNING (not blocking)."""
    from alphaos.constants import Severity

    s = make_settings(TIME_EXIT_ENFORCEMENT_ENABLED="true")  # default EXECUTION_PROVIDER
    checks = s.validate_startup()
    hit = next((c for c in checks if c.name == "time_exit_enforcement_configured_but_inert"), None)
    assert hit is not None
    assert hit.severity == Severity.WARNING
    assert s.startup_ok() is True  # never blocks startup


# ======================================================= audit fixup: MEDIUM-2
def test_medium2_flag_moves_the_journal_store_provenance_fingerprint():
    """The spec's "in the config fingerprint" requirement covers BOTH
    fingerprint mechanisms in this codebase: build_config_hashes() (proven
    above) AND journal_store.record_config_version()'s own hand-curated
    provenance snapshot written onto every config_versions row -- the audit
    flagged the second one as missed. Arming the first flag able to close a
    live position must move THIS hash too, or no provenance record
    distinguishes trades made before vs. after arming."""
    fake = FakeTradingClient()
    s_off, journal, _ = _paper_om(fake, TIME_EXIT_ENFORCEMENT_ENABLED="false")
    journal.record_config_version(s_off)
    row_off = journal.one("SELECT config_hash FROM config_versions ORDER BY id DESC LIMIT 1")

    s_on = make_settings(
        ALPHAOS_MODE="paper", EXECUTION_PROVIDER="alpaca_paper",
        ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s", ALPACA_PAPER="true",
        ALPACA_BASE_URL="https://paper-api.alpaca.markets", REAL_TRADING_ENABLED="false",
        TIME_EXIT_ENFORCEMENT_ENABLED="true",
    )
    journal.record_config_version(s_on)
    row_on = journal.one("SELECT config_hash FROM config_versions ORDER BY id DESC LIMIT 1")
    assert row_off["config_hash"] != row_on["config_hash"]


# ======================================================= audit fixup: BLOCKER-1
def test_blocker1_retry_after_reprotect_cancels_the_new_legs_never_orphaned():
    """Two-pass probe (auditor's own probe language): pass 1's close raises
    after legs are cancelled+verified; re-protection succeeds. Pass 2 must
    cancel the NEW (re-placed) legs -- never vacuously "verify" zero cancels
    against the dead old bracket and close on top of a still-live
    replacement, which would orphan it (a resting sell-stop + sell-limit
    with no position behind it)."""
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    pos, old_boid = _open_broker_position(journal, fake, symbol="XLV", qty=10.0,
                                          max_holding_days=5, opened_market_date=opened)
    old_tp = fake.leg("XLV", "take_profit")
    old_sl = fake.leg("XLV", "stop_loss")

    # --- pass 1: close raises -> recovery re-places protection successfully ---
    fake.raise_on_close_for.add("XLV")
    result1 = om.enforce_time_exits(now=_NOW)

    assert result1["closed"] == []
    assert len(result1["deferred"]) == 1
    assert result1["deferred"][0].get("reprotected") is True
    new_boid = result1["deferred"][0]["new_broker_order_id"]
    assert new_boid and new_boid != old_boid

    row = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert row["broker_order_id"] == new_boid, "BLOCKER-1: the re-placed OCO's id must be persisted"
    assert old_tp.status == "canceled" and old_sl.status == "canceled"

    new_order = fake.orders[new_boid]
    new_tp = next(leg for leg in new_order.legs if leg.order_type == "limit")
    new_sl = next(leg for leg in new_order.legs if leg.order_type == "stop")
    assert new_tp.status == "new" and new_sl.status == "new"  # untouched so far

    # --- pass 2: close now succeeds; the retry MUST cancel the NEW legs ---
    fake.raise_on_close_for.discard("XLV")
    fake.close_fill_price["XLV"] = 105.0
    log_before = len(fake.call_log)
    result2 = om.enforce_time_exits(now=_NOW)
    pass2_log = fake.call_log[log_before:]

    cancels_this_pass = [c for c in pass2_log if c[0] == "cancel"]
    assert cancels_this_pass, (
        "ORPHAN: position closed but the re-placed OCO was NEVER cancelled. "
        f"cancels this pass={cancels_this_pass}"
    )
    assert {c[1] for c in cancels_this_pass} == {new_tp.id, new_sl.id}, (
        "must cancel the NEW (re-placed) legs, not the dead old bracket's"
    )
    assert new_tp.status == "canceled" and new_sl.status == "canceled"

    assert len(result2["closed"]) == 1
    row2 = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert row2["status"] == "closed"


# ======================================================= audit fixup: BLOCKER-2
def test_blocker2_deferred_close_fills_between_passes_never_submits_unhedged_oco():
    """Two-pass probe: pass 1's liquidation is accepted but not yet filled
    (deferred, nothing recorded). Between passes it actually fills at the
    broker. Pass 2 must recognize the broker now reports this symbol FLAT
    and stand down completely -- never retry close() (which would 404/error
    on a flat symbol) and, critically, never fall into recovery and submit
    a protective OCO onto nothing (an OPENING order, not protection)."""
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    pos, boid = _open_broker_position(journal, fake, symbol="XLV", qty=10.0,
                                      max_holding_days=5, opened_market_date=opened)

    # --- pass 1: close accepted, not yet filled -> deferred, nothing recorded ---
    fake.close_fill_price["XLV"] = None
    result1 = om.enforce_time_exits(now=_NOW)
    assert result1["closed"] == []
    assert len(result1["deferred"]) == 1
    assert result1["deferred"][0].get("pending_close_order")
    row = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert row["status"] == "open"

    # Between passes: the liquidation actually fills at the broker.
    fake.set_broker_qty("XLV", 0)

    # --- pass 2: broker is now FLAT -- must stand down entirely ---
    log_before = len(fake.call_log)
    result2 = om.enforce_time_exits(now=_NOW)
    pass2_log = fake.call_log[log_before:]

    assert not any(c[0] == "close" for c in pass2_log), (
        "must never retry close() on a symbol the broker already reports flat"
    )
    assert not any(c[0] == "oco" for c in pass2_log), (
        f"2nd-pass broker calls: {pass2_log} -- RECOVERY SUBMITTED A PROTECTIVE OCO "
        f"FOR A SYMBOL THE BROKER IS FLAT IN"
    )
    assert result2["closed"] == []
    stood_down = any(o.get("broker_flat") for o in result2["errors"]) or \
        any(o.get("broker_flat") for o in result2["deferred"])
    assert stood_down, f"expected a broker_flat stand-down outcome, got {result2}"


# ======================================================= audit fixup: HIGH-1
def test_high1_partial_liquidation_fill_never_recorded_as_a_full_close():
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    pos, _ = _open_broker_position(journal, fake, symbol="XLV", qty=10.0, max_holding_days=5,
                                   opened_market_date=opened)
    fake.close_fill_price["XLV"] = 105.0
    fake.close_fill_qty["XLV"] = 6.0  # only 6 of 10 shares filled

    result = om.enforce_time_exits(now=_NOW)

    assert result["closed"] == []
    assert len(result["deferred"]) == 1
    assert result["deferred"][0].get("partial_fill") is True
    assert result["deferred"][0]["filled_qty"] == 6.0

    # Never a full close recorded.
    assert journal.one("SELECT * FROM exits WHERE position_id = ?", (pos["position_id"],)) is None
    row = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert row["status"] == "open"

    # The residual is loudly flagged as unprotected -- blocks new entries.
    incident = protection_watchdog.has_blocking_incident(journal)
    assert incident is not None
    assert incident["symbol"] == "XLV"
    assert incident["protection_status"] == "unprotected"
    assert incident["severity"] == "critical"


# ======================================================= audit fixup: HIGH-2
def test_high2_kill_switch_blocks_the_oco_new_order_during_recovery():
    """KillSwitch's own contract: presence means engaged, blocks ALL new
    orders. submit_protective_oco is a new order -- it must never fire while
    engaged, even though the cancel/close path above it is deliberately left
    ungated (an explicit, not-yet-ruled operator question)."""
    fake = FakeTradingClient()
    tmp_dir = tempfile.mkdtemp(prefix="time2_killswitch_")
    ks_path = os.path.join(tmp_dir, "KILL_SWITCH")
    kill_switch = KillSwitch(path=ks_path)
    try:
        _, journal, om = _paper_om(fake, kill_switch=kill_switch)
        opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
        pos, _ = _open_broker_position(journal, fake, symbol="XLV", max_holding_days=5,
                                       opened_market_date=opened)
        fake.raise_on_close_for.add("XLV")
        kill_switch.engage("audit fixup HIGH-2 test")

        result = om.enforce_time_exits(now=_NOW)

        assert not any(c[0] == "oco" for c in fake.call_log), (
            "must never submit a NEW order while the kill switch is engaged"
        )
        assert result["closed"] == []
        assert len(result["errors"]) == 1
        assert result["errors"][0].get("critical") is True
        assert "kill switch" in result["errors"][0].get("replace_error", "").lower()
        row = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos["position_id"],))
        assert row["status"] == "open"
    finally:
        kill_switch.release()
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


# ======================================================= audit fixup: HIGH-3
def test_high3_run_monitor_job_docstring_documents_the_time2_amendment():
    """The job's docstring must no longer claim the pre-TIME-2 absolute
    ("never submits/closes") without qualification -- it must name the
    TIME-2 amendment explicitly, the same way it already names ENTRY-TTL-1's.
    The pre-existing ratchet test (test_run_monitor_job_docstring_documents_
    the_narrow_cancel_exemption in test_entry_ttl.py) still passes
    unmodified -- this test checks the SAME required substrings plus the new
    TIME-2-specific ones."""
    from alphaos.scheduler.jobs import run_monitor_job

    doc = run_monitor_job.__doc__ or ""
    assert "TIME-2" in doc
    assert "enforce_time_exits" in doc
    assert "TIME_EXIT_ENFORCEMENT_ENABLED" in doc

    doc_lower = doc.lower()
    assert "cancel" in doc_lower
    assert "unfilled" in doc_lower
    assert ("never submits" in doc_lower) or ("never submit" in doc_lower)
    assert ("never closes" in doc_lower) or ("never close" in doc_lower)


# ======================================================= audit fixup: HIGH-4
def test_high4_check_position_sees_reprotected_position_as_protected():
    """Direct consequence of the BLOCKER-1 fix: once broker_order_id points
    at the re-placed OCO, protection_watchdog.check_position() (run through
    the REAL run_watchdog_pass(), not hand-built) must read the NEW live
    legs and report PROTECTED -- not a false CRITICAL "no downside
    protection" incident stacked on top of the real one."""
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake)
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    _open_broker_position(journal, fake, symbol="XLV", max_holding_days=5, opened_market_date=opened)
    fake.raise_on_close_for.add("XLV")

    result = om.enforce_time_exits(now=_NOW)
    assert result["deferred"][0].get("reprotected") is True

    summary = protection_watchdog.run_watchdog_pass(journal, om.alpaca, s)
    assert summary["protected"] == 1
    assert summary["unprotected"] == 0
    assert summary["new_incidents"] == []


# ======================================================= audit fixup: MEDIUM-1
def test_medium1_one_positions_step4_failure_never_aborts_the_others():
    """A DB/ledger failure recording the exit (step 4) for ONE position must
    never abort enforce_time_exits() for every OTHER position in the same
    pass -- and must never be silently swallowed either (a CRITICAL
    closed_mismatch incident is opened, since the broker close already
    succeeded by this point)."""
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    pos_a, _ = _open_broker_position(journal, fake, symbol="AAA", max_holding_days=5,
                                     opened_market_date=opened)
    pos_b, _ = _open_broker_position(journal, fake, symbol="BBB", max_holding_days=5,
                                     opened_market_date=opened)
    fake.close_fill_price["AAA"] = 101.0
    fake.close_fill_price["BBB"] = 102.0

    orig_close_position = om.positions.close_position

    def _boom(position_id, *a, **kw):
        if position_id == pos_a["position_id"]:
            raise RuntimeError("simulated DB error recording the exit")
        return orig_close_position(position_id, *a, **kw)

    om.positions.close_position = _boom

    result = om.enforce_time_exits(now=_NOW)

    aaa_outcome = next(o for o in result["errors"] if o["symbol"] == "AAA")
    assert aaa_outcome.get("closed_mismatch") is True
    row_a = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos_a["position_id"],))
    assert row_a["status"] == "open"  # never falsely marked closed despite the broker close succeeding
    incident = protection_watchdog.has_blocking_incident(journal)
    assert incident is not None and incident["symbol"] == "AAA"
    assert incident["protection_status"] == "closed_mismatch"

    # BBB, in the SAME pass, still closed successfully -- unaffected.
    assert any(c["symbol"] == "BBB" for c in result["closed"])
    row_b = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos_b["position_id"],))
    assert row_b["status"] == "closed"


def test_medium1_unexpected_error_in_time_exit_due_never_aborts_other_positions():
    """Belt-and-suspenders outer-loop guard: even an unexpected exception
    BEFORE _enforce_time_exit_one is ever reached (e.g. inside
    _time_exit_due) must not abort the pass for other, unrelated
    positions."""
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    pos_a, _ = _open_broker_position(journal, fake, symbol="CCC", max_holding_days=5,
                                     opened_market_date=opened)
    pos_b, _ = _open_broker_position(journal, fake, symbol="DDD", max_holding_days=5,
                                     opened_market_date=opened)
    fake.close_fill_price["DDD"] = 50.0

    orig_due = om._time_exit_due

    def _boom(pos, now_et_date):
        if pos["symbol"] == "CCC":
            raise RuntimeError("simulated unexpected failure")
        return orig_due(pos, now_et_date)

    om._time_exit_due = _boom

    result = om.enforce_time_exits(now=_NOW)

    ccc_outcome = next(o for o in result["errors"] if o["symbol"] == "CCC")
    assert ccc_outcome.get("unexpected") is True
    assert any(c["symbol"] == "DDD" for c in result["closed"])


# =============================================================== ALSO: public API
def test_also_incident_coupling_uses_the_public_entry_point_not_private():
    """order_manager.py must go through the PUBLIC
    protection_watchdog.open_protection_incident(), never the private
    _record_check directly (no stability contract, and it previously wrote
    scheduler_run_id=None/broker_qty=None regardless of whether better data
    was available)."""
    import pathlib

    from alphaos.execution import order_manager as om_mod

    text = pathlib.Path(str(om_mod.__file__)).read_text(encoding="utf-8")
    assert "_record_check" not in text
    assert "protection_watchdog.open_protection_incident(" in text


# ======================================================= round 2 fixup: R1
def test_r1_failed_local_persist_of_new_oco_id_never_reported_as_success():
    """Auditor's own probe (B1-2): a failed LOCAL persist of the re-placed
    OCO's broker_order_id reproduces the B1 orphan through a non-broker
    failure (disk I/O, locked DB, full disk) -- the OCO is live at the
    broker, but if the failed write is reported as success anyway, the
    position row keeps pointing at the DEAD old bracket; the next pass
    would verify vacuously against it and close while the new legs stay
    resting with nothing behind them. Run TWICE (the injected failure is
    sticky, simulating an ongoing disk/DB problem, not a one-off blip): the
    position must NEVER be reported as reprotected, and must NEVER
    transition to closed while an unpersisted, untracked OCO is live."""
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    pos, old_boid = _open_broker_position(journal, fake, symbol="AAPL", max_holding_days=5,
                                          opened_market_date=opened)
    fake.raise_on_close_for.add("AAPL")

    # sqlite3.Connection.execute is a read-only C-level attribute -- it can't
    # be monkeypatched directly. Swap the whole connection for a thin proxy
    # that intercepts ONLY the one UPDATE statement and forwards everything
    # else (queries, commit, cursor, ...) straight to the real connection.
    class _BoomOnPersistConn:
        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, *a, **kw):
            if "UPDATE positions SET broker_order_id" in sql:
                raise RuntimeError("simulated disk I/O failure persisting broker_order_id")
            return self._real.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._real, name)

    journal.conn = _BoomOnPersistConn(journal.conn)

    for _ in range(2):
        result = om.enforce_time_exits(now=_NOW)
        assert not any(o.get("reprotected") for o in result["deferred"]), (
            "must never report a failed persist as a successful reprotect"
        )
        assert len(result["errors"]) == 1
        assert result["errors"][0].get("critical") is True
        row = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos["position_id"],))
        assert row["broker_order_id"] == old_boid, "must never silently update to an unconfirmed id"
        assert row["status"] == "open", (
            f"FAIL R1: position closed despite an unpersisted, untracked re-placed OCO "
            f"(broker_order_id still {row['broker_order_id']!r})"
        )

    # A human sees this: a blocking CRITICAL incident, with the abandoned
    # OCO's own broker_order_id preserved in the audit trail so it can be
    # found and cancelled manually.
    incident = protection_watchdog.has_blocking_incident(journal)
    assert incident is not None
    assert incident["symbol"] == "AAPL"
    assert incident["protection_status"] == "unprotected"
    assert incident["severity"] == "critical"
    assert "broker_order_id=" in incident["detail"]


# ======================================================= round 2 fixup: R2
def test_r2_oco_submitted_with_no_broker_order_id_treated_as_replace_failure():
    """Same remedy as R1: a submit that raises nothing but returns NO
    broker_order_id is not a successful re-protect either -- there is
    nothing to persist, so the next pass could never find the right legs."""
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    pos, old_boid = _open_broker_position(journal, fake, symbol="MSFT", max_holding_days=5,
                                          opened_market_date=opened)
    fake.raise_on_close_for.add("MSFT")

    orig_submit_oco = fake.submit_oco

    def _submit_oco_no_id(spec):
        order = orig_submit_oco(spec)
        fake.orders.pop(order.id, None)
        order.id = None  # simulate a broker response carrying no id
        return order

    fake.submit_oco = _submit_oco_no_id

    result = om.enforce_time_exits(now=_NOW)

    assert not any(o.get("reprotected") for o in result["deferred"])
    assert len(result["errors"]) == 1
    assert result["errors"][0].get("critical") is True
    assert "no broker_order_id" in result["errors"][0]["replace_error"]
    row = journal.one("SELECT * FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert row["broker_order_id"] == old_boid
    assert row["status"] == "open"

    incident = protection_watchdog.has_blocking_incident(journal)
    assert incident is not None and incident["symbol"] == "MSFT"


# ======================================================= round 2 fixup: R3
def test_r3_broker_flat_standdown_classified_as_deferred_not_error():
    """A correct, intended stand-down (spec eligibility: never act on a
    stale broker view) must land in "deferred", not "errors" -- it was
    previously miscounted as an error, inflating the count an operator
    reads off enforce_time_exits()'s summary."""
    fake = FakeTradingClient()
    _, journal, om = _paper_om(fake)
    opened = _opened_date_for(_NOW_DATE, trading_days_ago=5)
    _open_broker_position(journal, fake, symbol="XLV", max_holding_days=5, opened_market_date=opened)
    fake.set_broker_qty("XLV", 0)  # broker already reports this symbol flat

    result = om.enforce_time_exits(now=_NOW)

    assert result["errors"] == [], "a correct stand-down must not inflate the error count"
    assert len(result["deferred"]) == 1
    assert result["deferred"][0].get("broker_flat") is True


# ======================================================= round 2 fixup: M4
def test_m4_time_exits_closed_routed_into_orchestrator_monitor_summary():
    """Orchestrator.run_monitor_once() previously logged "0 exit(s)" and
    returned exits: [] for a monitor pass that ACTUALLY closed a
    past-window position via TIME-2 -- the close lands in
    recon["time_exits_closed"], not recon["exits"]. Pure observability fix
    (orchestrator.py, explicitly authorized one-file touch): route it into
    the same summary an operator already reads, with no ledger/behavior
    change (the underlying tables were always correct)."""
    from alphaos.orchestrator import Orchestrator

    journal = JournalStore(":memory:")
    orch = Orchestrator(settings=make_settings(), journal=journal)
    fake_exit = {
        "exit_id": "exit_fake1", "position_id": "pos_fake1", "symbol": "XLV",
        "exit_reason": "time_expiry", "exit_price": 105.0, "classification": "profit-taking",
        "is_same_day": False, "net_pnl": 50.0, "realized_r": 0.83,
    }
    orch.orders.reconcile = lambda: {
        "reconciled": 0, "opened": [], "exits": [],
        "time_exits_closed": [{"ok": True, "position_id": "pos_fake1", "symbol": "XLV", "exit": fake_exit}],
        "time_exits_deferred": [], "time_exits_errors": [],
        "stale_cancelled": [], "stale_errors": [], "stale_partial_fill_alerts": [],
    }
    orch.positions.monitor = lambda price_overrides=None: []

    result = orch.run_monitor_once()

    assert fake_exit in result["exits"]
    assert len(result["exits"]) == 1
    log_row = journal.one(
        "SELECT * FROM system_events WHERE category = 'monitor' ORDER BY id DESC LIMIT 1"
    )
    assert "1 exit(s)" in log_row["message"]
