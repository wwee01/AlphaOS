"""TIME-2 (docs/roadmap/alphaos-time2-broker-time-exit-spec.md): broker-side
time-exit ENFORCEMENT for alpaca_paper positions -- max_holding_days has
NEVER closed a live position (0 time_expiry exits in the system's history)
because PositionManager.monitor() is architecturally barred from ever
touching the broker. Enforcement lives in OrderManager.enforce_time_exits(),
wired into reconcile() immediately after _cancel_stale_entries() (ENTRY-TTL-
1's own wiring/rationale, reused).

Covers all 11 spec test obligations:
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
 9. exit_reason='time_expiry' reaches trade_outcomes with a correct
    realized_r through the SAME exit path every other exit uses.
10. Both pre-existing architecture ratchet tests still pass unmodified
    (test_entry_ttl.py's broker-isolation guard, test_daily_brief.py's
    report-module isolation guard) -- run alongside this file, not
    duplicated wholesale here; a lightweight local echo of the
    broker-isolation guard is included as cheap insurance (see
    test_position_manager_still_has_no_broker_strings below).
11. Cold-import subprocess check on every touched module.

All datetimes are fixed/injected (enforce_time_exits(now=...) takes an
explicit clock; no test relies on real wall-clock "today") -- house law,
matching test_hold1_trading_day_holding_period.py's own convention. Offline,
in-memory, mock/paper mode. No real money, no network (NTFY_TOPIC unset ->
alerts.send_alert no-ops without ever reaching urlopen).
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

from alphaos.broker.alpaca_client import AlpacaClient
from alphaos.constants import ExecutionSource
from alphaos.execution import protection_watchdog
from alphaos.execution.order_manager import OrderManager
from alphaos.journal.journal_store import JournalStore
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
    carrying the two live protective legs."""

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
    """A close/OCO-replacement order with no legs of its own."""

    def __init__(self, symbol, qty, side="sell", filled_avg_price=None, status=None):
        self.id = uuid.uuid4().hex
        self.client_order_id = uuid.uuid4().hex
        self.symbol = symbol
        self.side = side
        self.qty = qty
        self.order_class = "simple"
        self.filled_qty = qty if filled_avg_price is not None else 0
        self.filled_avg_price = filled_avg_price
        self.status = status or ("filled" if filled_avg_price is not None else "accepted")
        self.limit_price = None
        self.stop_price = None
        self.submitted_at = "2026-07-14T14:00:00Z"
        self.filled_at = "2026-07-14T14:00:01Z" if filled_avg_price is not None else None
        self.time_in_force = "day"
        self.legs: list = []


class FakeTradingClient:
    """SDK-agnostic fake -- same interface AlpacaClient calls, same pattern
    as tests/test_alpaca_paper_execution.py's FakeTradingClient, extended
    with close_position/submit_oco (TIME-2's new broker capabilities) and a
    unified call_log so tests can assert exact call ORDER, not just which
    calls happened."""

    FAKE = True

    def __init__(self):
        self.orders: dict = {}       # id (parent OR leg OR close/oco order) -> object
        self._by_symbol: dict = {}   # symbol -> bracket parent order
        self.call_log: list = []     # [(op, id_or_symbol), ...] in call order
        self.raise_on_cancel_for: set = set()
        self.raise_on_close_for: set = set()
        self.raise_on_oco_for: set = set()
        self.cancel_result_status: dict = {}  # leg_id -> status to set instead of "canceled"
        self.close_fill_price: dict = {}      # symbol -> price (None = leave unfilled)

    # ---- fixture builder ----
    def register_bracket(self, symbol, qty, side, entry, target, stop, tif="gtc", broker_order_id=None):
        o = _FakeBracketOrder(symbol, qty, side, entry, target, stop, tif)
        if broker_order_id:
            o.id = broker_order_id
        self.orders[o.id] = o
        for leg in o.legs:
            self.orders[leg.id] = leg
        self._by_symbol[symbol] = o
        return o

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
        price = self.close_fill_price.get(symbol, "__default__")
        if price == "__default__":
            price = parent.filled_avg_price
        order = _FakeSimpleOrder(symbol, parent.qty, filled_avg_price=price)
        self.orders[order.id] = order
        return order

    def submit_oco(self, spec):
        self.call_log.append(("oco", spec["symbol"]))
        if spec["symbol"] in self.raise_on_oco_for:
            raise RuntimeError(f"oco failed for {spec['symbol']}")
        order = _FakeSimpleOrder(spec["symbol"], spec["qty"], status="accepted")
        self.orders[order.id] = order
        return order

    # not exercised by TIME-2 but part of the interface other AlpacaClient
    # methods use; harmless empty defaults keep the fake usable everywhere.
    def get_all_positions(self):
        return []

    def get_orders(self):
        return []


def _paper_om(fake, journal=None, **over):
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
    om = OrderManager(s, journal, alpaca=alpaca)
    return s, journal, om


def _open_broker_position(journal, fake, *, symbol="XLV", qty=10.0, direction="long",
                          entry=100.0, stop=94.0, target=112.0, max_holding_days=5,
                          opened_market_date: date, tif="gtc"):
    """Insert an open, broker-managed ``positions`` row AND register the
    matching bracket (with two live legs) at the fake broker under the SAME
    broker_order_id -- independent of OrderManager.execute_proposal()/
    reconcile()'s own fill flow (already covered by
    tests/test_alpaca_paper_execution.py), so trading-day fixtures here stay
    exact and hand-controlled, matching test_hold1's own directness."""
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
    assert fake.call_log == []
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
    assert fake.call_log == [c for c in fake.call_log if c[0] != "close"], "no close call was made"
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
    assert len(fake.call_log) and fake.call_log[-1] == ("oco", "NVDA")
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
    enforcement still succeeds identically."""
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
        "alphaos.config.settings",
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
