"""ENTRY-TTL-1: working-order staleness watchdog
(docs/roadmap/entry-ttl-1-working-order-staleness.md).

Pure trigger logic (``alphaos.execution.entry_staleness``) is tested with an
injected clock and zero I/O. The side-effecting cancel flow
(``OrderManager._cancel_stale_entries`` / ``_cancel_order_row`` /
``cancel_order_operator``) is exercised hermetically against the same
``FakeTradingClient`` used by test_alpaca_paper_execution.py -- no SDK/
network. House law throughout: injectable clock everywhere (no sleeps, no
monkeypatching global time), a mock/fake broker for cancel-call assertions,
in-memory JournalStore fixtures, and ``alerts.send_alert`` monkeypatched with
exactly-once assertions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from alphaos.broker.alpaca_client import AlpacaClient
from alphaos.constants import (
    MarketSession,
    OrderState,
    ProposalStatus,
    ReasonCode,
)
from alphaos.execution import entry_staleness as es
from alphaos.execution.order_manager import OrderManager
from alphaos.journal.journal_store import JournalStore
from alphaos.safety import KillSwitch
from alphaos.util import alerts, timeutils
from conftest import make_proposal, make_settings
from test_alpaca_paper_execution import FakeTradingClient, _seed_proposal

# ------------------------------------------------------------------ fixtures
# Reused throughout: Thursday 2026-07-02 -> Monday 2026-07-06 is exactly 1
# trading day (Friday July 3 is the NYSE-observed Independence Day holiday) --
# the same worked example market_calendar.trading_days_between()'s own
# docstring cites. Good for pinning both the TTL-count arithmetic AND the
# "weekend/holiday spans counted correctly" requirement (spec test 1) in one
# set of fixed instants.
SUBMITTED_THU = "2026-07-02T14:30:00+00:00"       # Thursday, 10:30 ET
NOW_THU = datetime(2026, 7, 2, 14, 30, tzinfo=timezone.utc)   # same day, age 0
NOW_FRI_HOLIDAY = datetime(2026, 7, 3, 14, 30, tzinfo=timezone.utc)  # observed holiday, age 0
NOW_MON = datetime(2026, 7, 6, 14, 30, tzinfo=timezone.utc)   # age 1 (only Monday counts)
NOW_TUE = datetime(2026, 7, 7, 14, 30, tzinfo=timezone.utc)   # age 2


def _usable_snapshot(now: datetime, last_price) -> dict:
    """A FreshnessGuard-clean snapshot as of ``now`` (regular session, quote
    10s old, bar 30s old -- well within the RTH thresholds)."""
    from datetime import timedelta

    return {
        "market_session": MarketSession.REGULAR.value,
        "quote_timestamp": timeutils.to_iso(now - timedelta(seconds=10)),
        "bar_timestamp": timeutils.to_iso(now - timedelta(seconds=30)),
        "received_at": timeutils.to_iso(now),
        "last_price": last_price,
    }


def _stale_snapshot(now: datetime, last_price) -> dict:
    """A snapshot whose quote is far past the RTH freshness threshold (60s) --
    FreshnessGuard.assess() must report is_usable=False."""
    from datetime import timedelta

    return {
        "market_session": MarketSession.REGULAR.value,
        "quote_timestamp": timeutils.to_iso(now - timedelta(seconds=5000)),
        "bar_timestamp": timeutils.to_iso(now - timedelta(seconds=5000)),
        "received_at": timeutils.to_iso(now),
        "last_price": last_price,
    }


class _FakeMarket:
    """Minimal MarketDataClient double: symbol -> pre-built snapshot dict."""

    def __init__(self):
        self.snapshots: dict = {}

    def set_snapshot(self, symbol: str, snapshot: dict) -> None:
        self.snapshots[symbol] = snapshot

    def get_snapshot(self, symbol: str) -> dict:
        return self.snapshots.get(symbol, {})


class _RaisingCancelClient(FakeTradingClient):
    """Simulates the broker rejecting a cancel because the order already
    filled between the reconcile read and the cancel call (spec 3.5's race)."""

    def cancel_order_by_id(self, oid):
        raise RuntimeError("order already filled at the broker")


def _paper_om(fake_broker, fake_market=None, **over):
    """Same shape as test_alpaca_paper_execution.py's own ``_paper_om``, plus
    an injectable market client for the drift leg."""
    cfg = {
        "ALPHAOS_MODE": "paper", "EXECUTION_PROVIDER": "alpaca_paper",
        "ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "ALPACA_PAPER": "true",
        "ALPACA_BASE_URL": "https://paper-api.alpaca.markets", "REAL_TRADING_ENABLED": "false",
    }
    cfg.update(over)
    s = make_settings(**cfg)
    journal = JournalStore(":memory:")
    alpaca = AlpacaClient(s, journal, trading_client=fake_broker)
    om = OrderManager(s, journal, alpaca=alpaca, market_data=fake_market)
    return s, journal, om


def _submit_and_leave_unfilled(om, journal, **prop_kwargs):
    """Submit a bracket that goes straight to accepted/unfilled (the
    FakeTradingClient default -- see _FakeOrder.__init__'s status='accepted')
    and stamp submitted_at to a caller-chosen value (the fake always uses a
    fixed 2026-06-22 stamp; ENTRY-TTL-1 tests need to control it directly)."""
    prop = make_proposal(**prop_kwargs)
    _seed_proposal(journal, prop)
    res = om.execute_proposal(prop)
    assert res.blocked is False
    assert res.state == OrderState.ACCEPTED.value
    return prop, res.order


def _set_submitted_at(journal, order_id, submitted_at):
    journal.conn.execute(
        "UPDATE paper_orders SET submitted_at = ? WHERE order_id = ?", (submitted_at, order_id)
    )
    journal.conn.commit()


# =============================================================================
# 1) TTL: fires at exactly N trading days; N-1 does not; weekend/holiday spans
#    counted correctly.
# =============================================================================
def test_ttl_does_not_fire_before_n_trading_days_have_elapsed():
    fires, age = es.evaluate_ttl(SUBMITTED_THU, NOW_MON, ttl_trading_days=2)
    assert fires is False and age == 1


def test_ttl_fires_at_exactly_n_trading_days():
    fires, age = es.evaluate_ttl(SUBMITTED_THU, NOW_TUE, ttl_trading_days=2)
    assert fires is True and age == 2


def test_ttl_holiday_and_weekend_do_not_count_as_elapsed_trading_days():
    """The observed Independence Day holiday (Fri) + the weekend must not
    advance the TTL clock -- only Monday, the next real trading day, does."""
    fires_thu, age_thu = es.evaluate_ttl(SUBMITTED_THU, NOW_THU, ttl_trading_days=1)
    fires_fri, age_fri = es.evaluate_ttl(SUBMITTED_THU, NOW_FRI_HOLIDAY, ttl_trading_days=1)
    fires_mon, age_mon = es.evaluate_ttl(SUBMITTED_THU, NOW_MON, ttl_trading_days=1)
    assert (fires_thu, age_thu) == (False, 0)
    assert (fires_fri, age_fri) == (False, 0)  # the holiday itself never counts
    assert (fires_mon, age_mon) == (True, 1)


def test_ttl_leg_disabled_at_zero_never_fires_regardless_of_age():
    fires, age = es.evaluate_ttl(SUBMITTED_THU, NOW_TUE, ttl_trading_days=0)
    assert fires is False
    assert age is None  # age is not even computed once the leg is off


def test_ttl_missing_submitted_at_fails_safe_toward_cancellation():
    """A missing/unparseable submitted_at must not let an order sit forever
    just because of a data/journal defect (mirrors proposals/ttl.py's own
    fail-safe convention)."""
    fires, age = es.evaluate_ttl(None, NOW_TUE, ttl_trading_days=2)
    assert fires is True
    assert age is None  # the age itself is genuinely unknown, never fabricated


# =============================================================================
# 2) Drift: long above threshold, short below; direction never inverted;
#    at-threshold boundary pinned.
# =============================================================================
def test_drift_fires_for_long_above_threshold():
    fires, pct = es.evaluate_drift("long", 100.0, 102.5, max_adverse_drift_pct=2.0)
    assert fires is True
    assert pct == pytest.approx(2.5)


def test_drift_does_not_fire_for_long_just_below_threshold():
    fires, _ = es.evaluate_drift("long", 100.0, 101.99, max_adverse_drift_pct=2.0)
    assert fires is False


def test_drift_at_threshold_boundary_is_inclusive_for_long():
    fires, pct = es.evaluate_drift("long", 100.0, 102.0, max_adverse_drift_pct=2.0)
    assert fires is True
    assert pct == pytest.approx(2.0)


def test_drift_fires_for_short_below_threshold():
    fires, pct = es.evaluate_drift("short", 100.0, 97.5, max_adverse_drift_pct=2.0)
    assert fires is True
    assert pct == pytest.approx(2.5)


def test_drift_at_threshold_boundary_is_inclusive_for_short():
    fires, pct = es.evaluate_drift("short", 100.0, 98.0, max_adverse_drift_pct=2.0)
    assert fires is True
    assert pct == pytest.approx(2.0)


def test_drift_direction_never_inverted_long_move_down_does_not_fire():
    """A long that drifted DOWN (favorable-direction-agnostic for the
    watchdog, but not 'adverse' for a long) must never fire the long rule."""
    fires, pct = es.evaluate_drift("long", 100.0, 90.0, max_adverse_drift_pct=2.0)
    assert fires is False
    assert pct == pytest.approx(-10.0)  # reported, but not adverse for a long


def test_drift_direction_never_inverted_short_move_up_does_not_fire():
    fires, pct = es.evaluate_drift("short", 100.0, 110.0, max_adverse_drift_pct=2.0)
    assert fires is False
    assert pct == pytest.approx(-10.0)


def test_drift_leg_disabled_at_zero_never_fires():
    fires, pct = es.evaluate_drift("long", 100.0, 1000.0, max_adverse_drift_pct=0)
    assert fires is False and pct is None


def test_drift_never_guesses_on_missing_price():
    fires, pct = es.evaluate_drift("long", 100.0, None, max_adverse_drift_pct=2.0)
    assert fires is False and pct is None


# =============================================================================
# 3) Stale/unusable snapshot -> drift leg skipped, TTL leg still fires.
# =============================================================================
def test_unusable_snapshot_skips_drift_but_ttl_still_fires():
    decision = es.evaluate(
        direction="long", submitted_at=SUBMITTED_THU, intended_entry_price=100.0,
        now=NOW_TUE, last_price=500.0,  # a huge adverse move -- must be IGNORED
        price_usable=False,             # ... because the snapshot is unusable
        ttl_trading_days=2, max_adverse_drift_pct=2.0,
    )
    assert decision.should_cancel is True
    assert decision.trigger == "ttl"
    assert decision.age_trading_days == 2
    assert decision.drift_pct is None  # never guessed


def test_unusable_snapshot_and_ttl_not_yet_due_means_no_cancel_at_all():
    decision = es.evaluate(
        direction="long", submitted_at=SUBMITTED_THU, intended_entry_price=100.0,
        now=NOW_MON, last_price=500.0, price_usable=False,
        ttl_trading_days=2, max_adverse_drift_pct=2.0,
    )
    assert decision.should_cancel is False
    assert decision.trigger is None


def test_ttl_and_drift_double_fire_reports_ttl_deterministically():
    """When both legs would fire the same pass, the trigger label is always
    'ttl' -- a deterministic tie-break, not a race between the two checks."""
    decision = es.evaluate(
        direction="long", submitted_at=SUBMITTED_THU, intended_entry_price=100.0,
        now=NOW_TUE, last_price=110.0, price_usable=True,
        ttl_trading_days=2, max_adverse_drift_pct=2.0,
    )
    assert decision.should_cancel is True
    assert decision.trigger == "ttl"
    assert decision.drift_pct == pytest.approx(10.0)  # still carried for audit context


# =============================================================================
# Integration: OrderManager._cancel_stale_entries / _cancel_order_row /
# reconcile() wiring, exercised against FakeTradingClient.
# =============================================================================
def test_reconcile_cancels_a_ttl_stale_unfilled_entry_and_alerts_once(monkeypatch):
    alert_calls = []
    monkeypatch.setattr(
        alerts, "send_alert",
        lambda settings, title, message, priority="default", journal=None: alert_calls.append(
            (title, message, priority)
        ) or True,
    )
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_TTL_TRADING_DAYS="2", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="0")
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)

    # Injected clock at the exact TTL boundary (age == 2 trading days).
    result = om._cancel_stale_entries(now=NOW_TUE)
    assert len(result["cancelled"]) == 1
    assert fake.orders[row["broker_order_id"]].status == "canceled"

    order_row = journal.one("SELECT * FROM paper_orders WHERE order_id = ?", (row["order_id"],))
    assert order_row["state"] == OrderState.CANCELLED.value

    prop_row = journal.proposal_by_id(prop.proposal_id)
    assert prop_row["status"] == ProposalStatus.EXPIRED.value

    events = journal.query(
        "SELECT * FROM order_events WHERE order_id = ? AND new_state = ?",
        (row["order_id"], OrderState.CANCELLED.value),
    )
    assert len(events) == 1
    detail = json.loads(events[0]["detail_json"])
    assert detail["reason_code"] == ReasonCode.ORDER_STALE_CANCELLED.value
    assert detail["trigger"] == "ttl"
    assert detail["age_trading_days"] == 2

    assert len(alert_calls) == 1  # exactly one alert for this cancel


def test_reconcile_invokes_the_staleness_pass_and_surfaces_its_result_keys(monkeypatch):
    """reconcile() wiring (spec 3.2: "Rides OrderManager.reconcile()"):
    verified via a spy on _cancel_stale_entries rather than depending on
    real wall-clock TTL arithmetic (house law: no wall-clock-dependent test
    behavior -- SUBMITTED_THU is a fixed 2026 date that would spuriously
    already be TTL-stale against the real "now" the test happens to run
    under)."""
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_TTL_TRADING_DAYS="2", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="0")
    calls = []
    original = om._cancel_stale_entries

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(om, "_cancel_stale_entries", spy)
    rec = om.reconcile()
    assert len(calls) == 1
    assert set(rec) >= {"stale_cancelled", "stale_errors", "stale_partial_fill_alerts"}


def test_reconcile_cancels_a_drift_stale_unfilled_entry():
    fake = FakeTradingClient()
    market = _FakeMarket()
    s, journal, om = _paper_om(
        fake, fake_market=market,
        ENTRY_ORDER_TTL_TRADING_DAYS="0", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="2.0",
    )
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="MSFT", entry=100.0, stop=97.0, target=106.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)
    market.set_snapshot("MSFT", _usable_snapshot(NOW_THU, last_price=102.5))  # +2.5% adverse, same day

    result = om._cancel_stale_entries(now=NOW_THU)  # zero trading-day age -- TTL leg is OFF anyway
    assert len(result["cancelled"]) == 1
    assert result["cancelled"][0]["trigger"] == "drift"
    assert result["cancelled"][0]["drift_pct"] == pytest.approx(2.5)
    assert fake.orders[row["broker_order_id"]].status == "canceled"


def test_no_cancel_when_neither_leg_fires():
    fake = FakeTradingClient()
    market = _FakeMarket()
    s, journal, om = _paper_om(
        fake, fake_market=market,
        ENTRY_ORDER_TTL_TRADING_DAYS="2", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="2.0",
    )
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="NVDA", entry=100.0, stop=97.0, target=106.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)
    market.set_snapshot("NVDA", _usable_snapshot(NOW_MON, last_price=100.5))  # +0.5%, well under 2%

    result = om._cancel_stale_entries(now=NOW_MON)  # age 1, TTL needs 2
    assert result["cancelled"] == []
    assert fake.orders[row["broker_order_id"]].status == "accepted"  # untouched


# =============================================================================
# 4) Filled-between-read-and-cancel race -> benign, proposal NOT expired,
#    fill mirrored next pass.
# =============================================================================
def test_broker_cancel_error_is_benign_and_never_expires_the_proposal():
    fake = _RaisingCancelClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_TTL_TRADING_DAYS="2", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="0")
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="TSLA", entry=100.0, stop=97.0, target=106.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)

    result = om._cancel_stale_entries(now=NOW_TUE)
    assert result["cancelled"] == []
    assert len(result["errors"]) == 1

    order_row = journal.one("SELECT * FROM paper_orders WHERE order_id = ?", (row["order_id"],))
    assert order_row["state"] == OrderState.ACCEPTED.value  # NOT marked cancelled

    prop_row = journal.proposal_by_id(prop.proposal_id)
    assert prop_row["status"] != ProposalStatus.EXPIRED.value  # NEVER expired on a race

    # No CANCELLED order_events row was ever written for this order.
    events = journal.query(
        "SELECT * FROM order_events WHERE order_id = ? AND new_state = ?",
        (row["order_id"], OrderState.CANCELLED.value),
    )
    assert events == []

    # The next reconcile pass (broker now shows the order still "accepted"
    # in this fake, but the point is: nothing here pre-empted it) can run
    # again with no residual bad state.
    fake.fill_entry("TSLA", price=100.0)
    rec = om.reconcile()
    assert len(rec["opened"]) == 1
    assert journal.proposal_by_id(prop.proposal_id)["status"] == "filled"


# =============================================================================
# 5) Partial fill -> alert, no cancel (and deduped across passes).
# =============================================================================
def test_partially_filled_entry_is_alerted_not_cancelled(monkeypatch):
    alert_calls = []
    monkeypatch.setattr(
        alerts, "send_alert",
        lambda settings, title, message, priority="default", journal=None: alert_calls.append(title) or True,
    )
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_TTL_TRADING_DAYS="1", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="0")
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AMD", entry=50.0, stop=48.0, target=54.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)
    journal.conn.execute(
        "UPDATE paper_orders SET state = ? WHERE order_id = ?",
        (OrderState.PARTIALLY_FILLED.value, row["order_id"]),
    )
    journal.conn.commit()

    result = om._cancel_stale_entries(now=NOW_TUE)  # well past the TTL boundary
    assert result["cancelled"] == []
    assert result["partial_fill_alerts"] == [row["order_id"]]
    assert len(alert_calls) == 1
    assert fake.orders[row["broker_order_id"]].status == "accepted"  # cancel_order NEVER called

    order_row = journal.one("SELECT * FROM paper_orders WHERE order_id = ?", (row["order_id"],))
    assert order_row["state"] == OrderState.PARTIALLY_FILLED.value  # untouched


def test_partial_fill_alert_is_not_repeated_on_a_second_pass(monkeypatch):
    alert_calls = []
    monkeypatch.setattr(
        alerts, "send_alert",
        lambda settings, title, message, priority="default", journal=None: alert_calls.append(title) or True,
    )
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_TTL_TRADING_DAYS="1", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="0")
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AMD", entry=50.0, stop=48.0, target=54.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)
    journal.conn.execute(
        "UPDATE paper_orders SET state = ? WHERE order_id = ?",
        (OrderState.PARTIALLY_FILLED.value, row["order_id"]),
    )
    journal.conn.commit()

    r1 = om._cancel_stale_entries(now=NOW_MON)
    r2 = om._cancel_stale_entries(now=NOW_TUE)
    assert r1["partial_fill_alerts"] == [row["order_id"]]
    assert r2["partial_fill_alerts"] == []  # already alerted -- deduped
    assert len(alert_calls) == 1


# =============================================================================
# 6) Kill switch engaged -> cancel still fires.
# =============================================================================
def test_kill_switch_engaged_does_not_block_staleness_cancel(tmp_path):
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_TTL_TRADING_DAYS="2", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="0")
    # Submit WHILE the kill switch is clear -- execute_proposal()'s own
    # safety preflight independently refuses all NEW submissions once
    # engaged (existing, unrelated law); this test is about the STALENESS
    # PASS's exemption from an *already*-engaged switch, not about bypassing
    # the submission-time gate.
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)

    om.kill_switch = KillSwitch(str(tmp_path / "KILL_SWITCH"))
    om.kill_switch.engage("test: entry-ttl kill switch exemption")
    assert om.kill_switch.is_engaged() is True

    result = om._cancel_stale_entries(now=NOW_TUE)
    assert len(result["cancelled"]) == 1
    assert fake.orders[row["broker_order_id"]].status == "canceled"


# =============================================================================
# 7) Master flag off / both legs 0 -> no-op; configured-but-inert warning.
# =============================================================================
def test_master_switch_off_is_a_full_no_op_even_when_ttl_is_due():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(
        fake, ENTRY_ORDER_STALENESS_ENABLED="false",
        ENTRY_ORDER_TTL_TRADING_DAYS="1", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="2.0",
    )
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)

    result = om._cancel_stale_entries(now=NOW_TUE)
    assert result == {"cancelled": [], "errors": [], "partial_fill_alerts": [],
                      "missing_broker_id_alerts": []}
    assert fake.orders[row["broker_order_id"]].status == "accepted"


def test_both_legs_zero_is_a_full_no_op():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(
        fake, ENTRY_ORDER_TTL_TRADING_DAYS="0", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="0",
    )
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)

    result = om._cancel_stale_entries(now=NOW_TUE)
    assert result["cancelled"] == []
    assert fake.orders[row["broker_order_id"]].status == "accepted"


def test_configured_but_inert_startup_warning_fires_when_master_on_both_legs_zero():
    s = make_settings(ENTRY_ORDER_TTL_TRADING_DAYS="0", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="0")
    checks = s.validate_startup()
    inert = [c for c in checks if c.name == "entry_order_staleness_configured_but_inert"]
    assert len(inert) == 1
    assert inert[0].ok is False
    assert inert[0].severity.value == "warning"
    assert s.startup_ok() is True  # a WARNING must never block startup


def test_configured_but_inert_warning_absent_when_a_leg_is_armed():
    s = make_settings(ENTRY_ORDER_TTL_TRADING_DAYS="2", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="0")
    checks = s.validate_startup()
    assert [c for c in checks if c.name == "entry_order_staleness_configured_but_inert"] == []


def test_configured_but_inert_warning_absent_when_master_switch_off():
    s = make_settings(
        ENTRY_ORDER_STALENESS_ENABLED="false",
        ENTRY_ORDER_TTL_TRADING_DAYS="0", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="0",
    )
    checks = s.validate_startup()
    assert [c for c in checks if c.name == "entry_order_staleness_configured_but_inert"] == []


def test_negative_ttl_trading_days_rejected_at_load():
    from alphaos.config.settings import SettingsError

    with pytest.raises(SettingsError):
        make_settings(ENTRY_ORDER_TTL_TRADING_DAYS="-1")


def test_negative_drift_pct_rejected_at_load():
    from alphaos.config.settings import SettingsError

    with pytest.raises(SettingsError):
        make_settings(ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="-0.5")


def test_settings_defaults_match_spec():
    s = make_settings()
    assert s.entry_order_staleness_enabled is True
    assert s.entry_order_ttl_trading_days == 2
    assert s.entry_order_max_adverse_drift_pct == pytest.approx(2.0)


# =============================================================================
# 8) Proposal transitions to 'expired' additively; row never deleted;
#    order_events chain intact with reason + trigger detail.
# =============================================================================
def test_cancel_never_deletes_rows_only_appends_events():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_TTL_TRADING_DAYS="2", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="0")
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)

    events_before = journal.query("SELECT * FROM order_events WHERE order_id = ?", (row["order_id"],))
    om._cancel_stale_entries(now=NOW_TUE)
    events_after = journal.query("SELECT * FROM order_events WHERE order_id = ?", (row["order_id"],))

    # Additive: every prior event is still present, plus exactly one new one.
    assert len(events_after) == len(events_before) + 1
    assert events_after[: len(events_before)] == events_before

    # The paper_orders row itself still exists (never deleted), just updated.
    assert journal.one("SELECT * FROM paper_orders WHERE order_id = ?", (row["order_id"],)) is not None
    # The trade_proposals row still exists too.
    assert journal.proposal_by_id(prop.proposal_id) is not None


def test_cancel_never_resurrects_a_rejected_or_filled_proposal():
    """The status-guard mirrors reconcile()'s own 'filled' transition guard:
    a proposal that already reached a different terminal status must never
    be overwritten to 'expired' by a race (e.g. a cancel racing a fill that
    already flipped the proposal to 'filled' through a separate path)."""
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_TTL_TRADING_DAYS="2", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="0")
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)
    journal.conn.execute(
        "UPDATE trade_proposals SET status = 'filled' WHERE proposal_id = ?", (prop.proposal_id,)
    )
    journal.conn.commit()

    om._cancel_stale_entries(now=NOW_TUE)
    assert journal.proposal_by_id(prop.proposal_id)["status"] == "filled"  # never overwritten


# =============================================================================
# 9) Protective legs of FILLED positions are never touched (swap-style
#    probe): a filled bracket with an open position must survive untouched.
# =============================================================================
def test_filled_entry_with_open_position_is_never_touched_even_if_stale():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_TTL_TRADING_DAYS="1", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="0")
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)

    fake.fill_entry("AAPL", price=100.0)
    om.reconcile()  # opens the position; order_id now has a positions row
    assert journal.count_open_positions() == 1

    # cancel_order_by_id would raise if ever called on an already-filled
    # order in the real SDK; the fake would happily "cancel" it if asked, so
    # this probe checks the CALL never happens, not just the outcome.
    calls_before = fake.orders[row["broker_order_id"]].status
    result = om._cancel_stale_entries(now=NOW_TUE)  # deep past TTL
    assert result["cancelled"] == []
    assert result["errors"] == []
    assert fake.orders[row["broker_order_id"]].status == calls_before == "filled"
    assert journal.count_open_positions() == 1  # position wholly unaffected


# =============================================================================
# 10) Idempotency: a second pass over an already-cancelled order does
#     nothing.
# =============================================================================
def test_second_pass_over_an_already_cancelled_order_is_a_no_op(monkeypatch):
    alert_calls = []
    monkeypatch.setattr(
        alerts, "send_alert",
        lambda settings, title, message, priority="default", journal=None: alert_calls.append(title) or True,
    )
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_TTL_TRADING_DAYS="2", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="0")
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)

    r1 = om._cancel_stale_entries(now=NOW_TUE)
    assert len(r1["cancelled"]) == 1
    events_after_first = journal.query("SELECT * FROM order_events WHERE order_id = ?", (row["order_id"],))

    r2 = om._cancel_stale_entries(now=NOW_TUE)  # same clock, second pass
    assert r2["cancelled"] == []  # already cancelled -- no longer even queried
    events_after_second = journal.query("SELECT * FROM order_events WHERE order_id = ?", (row["order_id"],))

    assert events_after_second == events_after_first  # no duplicate CANCELLED event
    assert len(alert_calls) == 1  # no repeat alert either


# =============================================================================
# 11) Alert sent exactly once per cancel.
# =============================================================================
def test_alert_sent_exactly_once_per_cancel(monkeypatch):
    alert_calls = []
    monkeypatch.setattr(
        alerts, "send_alert",
        lambda settings, title, message, priority="default", journal=None: alert_calls.append(title) or True,
    )
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_TTL_TRADING_DAYS="1", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="0")
    prop_a, row_a = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    prop_b, row_b = _submit_and_leave_unfilled(om, journal, symbol="MSFT", entry=200.0, stop=194.0, target=212.0)
    _set_submitted_at(journal, row_a["order_id"], SUBMITTED_THU)
    _set_submitted_at(journal, row_b["order_id"], SUBMITTED_THU)

    result = om._cancel_stale_entries(now=NOW_MON)
    assert len(result["cancelled"]) == 2
    assert len(alert_calls) == 2  # one alert per cancelled order, no more, no less


# =============================================================================
# 12) CLI targeted cancel: works by proposal_id and order_id, refuses
#     unknown ids, same audit trail, distinct reason code.
# =============================================================================
def test_cli_registers_cancel_order_with_one_positional_identifier():
    from alphaos.__main__ import build_parser

    args = build_parser().parse_args(["cancel_order", "some_id_or_proposal"])
    assert args.command == "cancel_order"
    assert args.identifier == "some_id_or_proposal"


def test_operator_cancel_by_order_id_uses_the_operator_reason_code(monkeypatch):
    alert_calls = []
    monkeypatch.setattr(
        alerts, "send_alert",
        lambda settings, title, message, priority="default", journal=None: alert_calls.append(title) or True,
    )
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_STALENESS_ENABLED="false")
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)

    res = om.cancel_order_operator(row["order_id"])
    assert res["ok"] is True
    assert fake.orders[row["broker_order_id"]].status == "canceled"

    events = journal.query(
        "SELECT * FROM order_events WHERE order_id = ? AND new_state = ?",
        (row["order_id"], OrderState.CANCELLED.value),
    )
    detail = json.loads(events[0]["detail_json"])
    assert detail["reason_code"] == ReasonCode.ORDER_CANCELLED_BY_OPERATOR.value
    assert detail["trigger"] == "operator"
    assert journal.proposal_by_id(prop.proposal_id)["status"] == ProposalStatus.EXPIRED.value
    assert len(alert_calls) == 1


def test_operator_cancel_by_proposal_id_resolves_to_the_same_order():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_STALENESS_ENABLED="false")
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)

    res = om.cancel_order_operator(prop.proposal_id)
    assert res["ok"] is True
    assert res["order_id"] == row["order_id"]
    assert fake.orders[row["broker_order_id"]].status == "canceled"


def test_operator_cancel_refuses_unknown_identifier():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_STALENESS_ENABLED="false")

    res = om.cancel_order_operator("does_not_exist_anywhere")
    assert res["ok"] is False
    assert "no paper_orders row found" in res["error"]


def test_operator_cancel_refuses_an_already_filled_order():
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_STALENESS_ENABLED="false")
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    fake.fill_entry("AAPL", price=100.0)
    om.reconcile()
    assert journal.count_open_positions() == 1

    res = om.cancel_order_operator(row["order_id"])
    assert res["ok"] is False
    assert fake.orders[row["broker_order_id"]].status == "filled"  # cancel_order never called


def test_cmd_cancel_order_dispatch_returns_exit_codes_matching_ok(monkeypatch):
    """cmd_cancel_order is a thin wrapper: exit 0 on ok=True, exit 1 on
    ok=False, via the SAME orch.orders.cancel_order_operator() the CLI
    parser above wires up (spec 3.7's 'refuses -- exit 1' requirement)."""
    import types

    from alphaos.__main__ import cmd_cancel_order

    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_STALENESS_ENABLED="false")
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    orch_stub = types.SimpleNamespace(orders=om)

    assert cmd_cancel_order(orch_stub, row["order_id"]) == 0
    assert cmd_cancel_order(orch_stub, "not_a_real_id") == 1


# =============================================================================
# 13) Monitor-law amendment tested structurally: the only broker-mutating
#     call reachable from run_monitor_once's own path is cancel_order.
# =============================================================================
def test_reconcile_and_staleness_pass_reach_no_broker_mutation_other_than_cancel_order():
    """Structural guard (mirrors test_s1c_activation.py's own AST/source-grep
    style): scans OrderManager.reconcile() plus every ENTRY-TTL-1 helper it
    calls (_cancel_stale_entries, _cancel_order_row, _alert_partial_fill_once,
    _staleness_price_snapshot) for ``self.alpaca.<method>(`` call sites. The
    ONLY ones allowed are the pre-existing ``get_order`` (state sync) and the
    new ``cancel_order`` -- never submit_bracket/flatten_paper/submit_order,
    which would violate run_monitor_job's amended-but-still-narrow law."""
    import inspect
    import re

    from alphaos.execution import order_manager as om_mod

    methods = (
        om_mod.OrderManager.reconcile,
        om_mod.OrderManager._cancel_stale_entries,
        om_mod.OrderManager._cancel_order_row,
        om_mod.OrderManager._alert_partial_fill_once,
        om_mod.OrderManager._alert_missing_broker_id_once,
        om_mod.OrderManager._staleness_price_snapshot,
        om_mod.OrderManager.cancel_order_operator,
    )
    combined = "\n".join(inspect.getsource(fn) for fn in methods)
    broker_calls = set(re.findall(r"self\.alpaca\.(\w+)\(", combined))
    assert broker_calls <= {"get_order", "cancel_order"}, broker_calls
    # Audit F4/F5 hardening: banned list widened, plus an aliasing guard --
    # `broker = self.alpaca` would let calls dodge the regex above entirely.
    # (`close_position(` is deliberately NOT banned here: reconcile()'s own
    # `self.positions.close_position(...)` is a LEDGER mirror of a broker
    # OCO leg fill, not a broker call -- the self.alpaca regex is the
    # broker-call authority.)
    for banned in ("submit_bracket", "flatten_paper", "submit_order", ".submit(",
                   "replace_order(", "= self.alpaca\n", "= self.alpaca "):
        assert banned not in combined, f"{banned!r} unexpectedly reachable from reconcile()"


def test_position_manager_monitor_and_protection_watchdog_never_touch_the_broker():
    """The other two calls run_monitor_once makes (protection_watchdog.
    run_watchdog_pass, PositionManager.monitor) must still never reach the
    broker at all -- confirms ENTRY-TTL-1 didn't widen either of THEM."""
    import pathlib

    from alphaos.execution import position_manager as pm_mod
    from alphaos.execution import protection_watchdog as pw_mod

    for mod in (pm_mod, pw_mod):
        text = pathlib.Path(str(mod.__file__)).read_text(encoding="utf-8")
        assert "self.alpaca." not in text
        assert ".cancel_order(" not in text
        assert ".submit_bracket(" not in text


def test_run_monitor_job_docstring_documents_the_narrow_cancel_exemption():
    """The law-amendment itself (spec 3.2): run_monitor_job's docstring must
    say the monitor may cancel an unfilled entry, while still saying it never
    submits or closes a position."""
    from alphaos.scheduler.jobs import run_monitor_job

    doc = (run_monitor_job.__doc__ or "").lower()
    assert "cancel" in doc
    assert "unfilled" in doc
    assert "never submits" in doc or "never submit" in doc
    assert "never closes" in doc or "never close" in doc


# =============================================================================
# Daily brief observability (spec 3.8): read-only age/drift columns, no
# network calls added.
# =============================================================================
def test_daily_brief_working_orders_carries_age_trading_days_and_no_network_drift():
    from alphaos.journal.journal_store import JournalStore as _JS
    from alphaos.orchestrator import Orchestrator
    from alphaos.reports.daily_brief import build_daily_brief

    journal = _JS(":memory:")
    orch = Orchestrator(settings=make_settings(), journal=journal)
    journal.insert("paper_orders", {
        "order_id": "ord_test_1", "symbol": "AAPL", "side": "buy", "qty": 10,
        "entry_price": 100.0, "stop_loss_price": 97.0, "take_profit_price": 106.0,
        "state": "accepted", "broker_order_id": "brk_test_1",
        "submitted_at": SUBMITTED_THU,
    })
    brief = build_daily_brief(journal, orch.settings, orch.kill_switch, now=NOW_TUE)
    wo = brief["working_orders"]
    assert len(wo) == 1
    assert wo[0]["age_trading_days"] == 2
    assert wo[0]["drift_pct"] is None  # PURE READ report: never fetches a live price
    journal.close()


# =============================================================================
# Audit fixups (2 Opus audits, 2026-08-02): MAJOR-1/MAJOR-2 verify-after-
# cancel + synced-this-pass gating; MINOR-2 missing-broker-id; MINOR-3 input
# hardening; F3 stale-snapshot integration; F6 TTL-only snapshot skip.
# =============================================================================
class _RacedFillClient(FakeTradingClient):
    """Audit MAJOR-1's exact scenario: the entry partially fills in the
    window between the last get_order and the cancel; Alpaca ACCEPTS the
    cancel (cancels the remainder), and the post-cancel re-read reports
    filled_qty > 0 with a terminal 'canceled' status."""

    def cancel_order_by_id(self, oid):
        o = self.orders[oid]
        o.filled_qty = 5
        o.filled_avg_price = 100.0
        o.status = "canceled"


class _PendingCancelClient(FakeTradingClient):
    """Cancel accepted but still processing: post-cancel re-read reports
    pending_cancel with zero filled -- NOT yet terminally dead. A LATER
    cancel of an already-canceled order is a no-op (never resurrects
    pending_cancel), matching the broker's own idempotent behavior --
    without this, the convergence retry would flap between states."""

    def cancel_order_by_id(self, oid):
        if self.orders[oid].status != "canceled":
            self.orders[oid].status = "pending_cancel"


class _VerifyReadFailsClient(FakeTradingClient):
    """Cancel succeeds; the verify-after-cancel get_order re-read fails."""

    def __init__(self):
        super().__init__()
        self._cancelled = set()

    def cancel_order_by_id(self, oid):
        self.orders[oid].status = "canceled"
        self._cancelled.add(oid)

    def get_order_by_id(self, oid):
        if oid in self._cancelled:
            raise RuntimeError("get_order 500 after cancel")
        return self.orders[oid]


def test_cancel_racing_a_fill_opens_the_position_instead_of_lying(monkeypatch):
    """Audit MAJOR-1: a non-raising cancel must NEVER be treated as proof the
    order didn't fill. When the post-cancel re-read shows filled_qty > 0, the
    filled shares are a REAL broker position: open it locally, mark the
    proposal 'filled' (never 'expired'), and alert -- the orphaned-broker-
    position hole the audit reproduced must be closed."""
    sent = []
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: sent.append(k.get("title") or a[1]) or True)
    fake = _RacedFillClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_TTL_TRADING_DAYS="1")
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)

    result = om._cancel_stale_entries(now=NOW_TUE)
    assert result["cancelled"] == []
    assert len(result["errors"]) == 1 and result["errors"][0].get("raced_fill") is True

    pos = journal.one("SELECT * FROM positions WHERE order_id = ?", (row["order_id"],))
    assert pos is not None and pos["status"] == "open"
    proposal = journal.one("SELECT status FROM trade_proposals WHERE proposal_id = ?", (prop.proposal_id,))
    assert proposal["status"] == "filled"          # never 'expired' on this branch
    assert any("raced a fill" in t for t in sent)  # loud, specific alert


def test_pending_cancel_defers_all_ledger_writes_and_converges_next_pass(monkeypatch):
    """Audit MAJOR-1 (async-cancel window): while the broker still reports
    pending_cancel, NOTHING is written locally -- the row stays live, and the
    pass after the broker finishes processing completes the cancel cleanly."""
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: True)
    fake = _PendingCancelClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_TTL_TRADING_DAYS="1")
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)

    result1 = om._cancel_stale_entries(now=NOW_TUE)
    assert result1["cancelled"] == []
    assert result1["errors"] and result1["errors"][0].get("deferred") is True
    reread = journal.one("SELECT state FROM paper_orders WHERE order_id = ?", (row["order_id"],))
    assert reread["state"] == OrderState.ACCEPTED.value  # untouched
    proposal = journal.one("SELECT status FROM trade_proposals WHERE proposal_id = ?", (prop.proposal_id,))
    assert proposal["status"] != "expired"

    # Broker finishes processing; the next pass converges.
    fake.orders[row["broker_order_id"]].status = "canceled"
    result2 = om._cancel_stale_entries(now=NOW_TUE)
    assert len(result2["cancelled"]) == 1
    reread2 = journal.one("SELECT state FROM paper_orders WHERE order_id = ?", (row["order_id"],))
    assert reread2["state"] == OrderState.CANCELLED.value
    proposal2 = journal.one("SELECT status FROM trade_proposals WHERE proposal_id = ?", (prop.proposal_id,))
    assert proposal2["status"] == "expired"


def test_verify_read_failure_after_cancel_defers_everything(monkeypatch):
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: True)
    fake = _VerifyReadFailsClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_TTL_TRADING_DAYS="1")
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)

    result = om._cancel_stale_entries(now=NOW_TUE)
    assert result["cancelled"] == []
    assert result["errors"] and result["errors"][0].get("deferred") is True
    reread = journal.one("SELECT state FROM paper_orders WHERE order_id = ?", (row["order_id"],))
    assert reread["state"] == OrderState.ACCEPTED.value
    proposal = journal.one("SELECT status FROM trade_proposals WHERE proposal_id = ?", (prop.proposal_id,))
    assert proposal["status"] != "expired"


def test_staleness_pass_skips_rows_not_synced_this_pass(monkeypatch):
    """Audit MAJOR-2: a row whose broker re-read FAILED this reconcile pass
    has unverified local state -- the staleness pass must skip it entirely
    (no cancel attempt), retrying only once a pass has verified it."""
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: True)
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_TTL_TRADING_DAYS="1")
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)

    cancels = []
    original_cancel = fake.cancel_order_by_id
    fake.cancel_order_by_id = lambda oid: cancels.append(oid) or original_cancel(oid)

    # Simulate "this row's get_order failed in the sync loop": empty synced set.
    result = om._cancel_stale_entries(now=NOW_TUE, synced_order_ids=set())
    assert result["cancelled"] == [] and result["errors"] == []
    assert cancels == []

    # Once verified (row present in the synced set), the cancel proceeds.
    result2 = om._cancel_stale_entries(now=NOW_TUE, synced_order_ids={row["order_id"]})
    assert len(result2["cancelled"]) == 1
    assert len(cancels) == 1


def test_missing_broker_order_id_alerts_once_and_never_retries(monkeypatch):
    """Audit MINOR-2: no broker_order_id -> nothing cancellable at the
    broker; alert exactly once, skip on every later pass (no unbounded
    retry/log spam)."""
    sent = []
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: sent.append(1) or True)
    fake = FakeTradingClient()
    s, journal, om = _paper_om(fake, ENTRY_ORDER_TTL_TRADING_DAYS="1")
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)
    journal.conn.execute("UPDATE paper_orders SET broker_order_id = NULL WHERE order_id = ?", (row["order_id"],))
    journal.conn.commit()

    r1 = om._cancel_stale_entries(now=NOW_TUE)
    r2 = om._cancel_stale_entries(now=NOW_TUE)
    r3 = om._cancel_stale_entries(now=NOW_TUE)
    assert r1["missing_broker_id_alerts"] == [row["order_id"]]
    assert r2["missing_broker_id_alerts"] == [] and r3["missing_broker_id_alerts"] == []
    assert r1["cancelled"] == r2["cancelled"] == r3["cancelled"] == []
    assert len(sent) == 1


def test_stale_snapshot_never_cancels_but_fresh_one_does(monkeypatch):
    """Audit F3 (test-vacuity fix): the ONE integration path the original
    suite never exercised -- _staleness_price_snapshot with an ACTUALLY
    stale snapshot. A 5000s-old quote showing +50% adverse drift must
    produce zero cancels (drift leg skipped, never guessed); the identical
    fresh snapshot is the discriminating control that proves the assertion
    isn't vacuous."""
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: True)

    for snapshot_fn, expected_cancels in ((_stale_snapshot, 0), (_usable_snapshot, 1)):
        fake = FakeTradingClient()
        market = _FakeMarket()
        s, journal, om = _paper_om(
            fake, market,
            ENTRY_ORDER_TTL_TRADING_DAYS="0",      # TTL off: drift is the only live leg
            ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="2.0",
        )
        prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
        _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)
        market.set_snapshot("AAPL", snapshot_fn(NOW_THU, last_price=150.0))  # +50% adverse

        result = om._cancel_stale_entries(now=NOW_THU)
        assert len(result["cancelled"]) == expected_cancels, (
            f"{snapshot_fn.__name__}: expected {expected_cancels} cancels, got {result}"
        )


def test_ttl_only_mode_never_fetches_a_price_snapshot(monkeypatch):
    """Audit F6: with the drift leg disabled (0), the staleness pass must
    not fetch a single snapshot -- ~hundreds of pointless market-data calls
    per day otherwise. Uses a market double that fails the test if touched."""
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **k: True)

    class _ExplodingMarket:
        def get_snapshot(self, symbol):
            raise AssertionError("TTL-only mode must never fetch a snapshot")

    fake = FakeTradingClient()
    s, journal, om = _paper_om(
        fake, _ExplodingMarket(),
        ENTRY_ORDER_TTL_TRADING_DAYS="1", ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT="0",
    )
    prop, row = _submit_and_leave_unfilled(om, journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    _set_submitted_at(journal, row["order_id"], SUBMITTED_THU)

    result = om._cancel_stale_entries(now=NOW_TUE)
    assert len(result["cancelled"]) == 1  # TTL still fired, zero snapshot fetches


def test_evaluate_drift_rejects_garbage_inputs():
    """Audit MINOR-3: negative entry price -> not evaluable (never a
    nonsense percentage); unrecognized direction -> not evaluable (never
    silently treated as long)."""
    assert es.evaluate_drift("long", -100.0, 1.0, 2.0) == (False, None)
    assert es.evaluate_drift("long", 0.0, 1.0, 2.0) == (False, None)
    assert es.evaluate_drift("sideways", 100.0, 150.0, 2.0) == (False, None)
    assert es.evaluate_drift("", 100.0, 150.0, 2.0) == (False, None)
