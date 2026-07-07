"""PR11 Portfolio Health engine (alphaos/reports/position_health.py). Pure
read, never touches orders/execution -- EXIT_REVIEW is a human flag, never
an auto-exit. Hermetic; direct row construction throughout (never depends
on what an organic mock scan happens to produce -- see the §H.1 date-flake
class this codebase has been burned by three times already)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from alphaos.reports.position_health import (
    AT_RISK_R_THRESHOLD,
    THESIS_AT_RISK,
    THESIS_BROKEN,
    THESIS_INTACT,
    VERDICT_ATTENTION,
    VERDICT_EXIT_REVIEW,
    VERDICT_HOLD,
    assess_positions,
)
from alphaos.util import timeutils
from alphaos.util.ids import new_id
from conftest import make_settings


class _FakeMarket:
    """Duck-typed market client -- assess_positions only ever calls
    get_snapshot(symbol), so a real MarketDataClient isn't needed."""

    def __init__(self, prices: dict, raises_for: set = frozenset()):
        self._prices = prices
        self._raises_for = raises_for

    def get_snapshot(self, symbol):
        if symbol in self._raises_for:
            raise RuntimeError("simulated market data outage")
        price = self._prices.get(symbol)
        return {"last_price": price} if price is not None else None


def _open_position(journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0,
                   max_holding_days=3, opened_days_ago=0.0, direction="long", **overrides):
    position_id = new_id("pos")
    opened_at = timeutils.to_iso(timeutils.now_utc() - timedelta(days=opened_days_ago))
    row = {
        "position_id": position_id, "symbol": symbol, "direction": direction,
        "strategy": "swing", "qty": 10, "avg_entry_price": entry, "stop_price": stop,
        "target_price": target, "max_holding_days": max_holding_days, "opened_at": opened_at,
        "status": "open", "protection_status": "protected",
        "trade_id": new_id("trade"), "candidate_id": new_id("cand"), "proposal_id": new_id("prop"),
    }
    row.update(overrides)
    journal.insert("positions", row)
    return row


def _open_incident(journal, position_id, symbol="AAPL"):
    journal.insert("protection_checks", {
        "check_id": new_id("chk"), "position_id": position_id, "symbol": symbol,
        "protection_status": "unprotected", "severity": "critical", "detail": "test incident",
    })


# ------------------------------------------------------------- thesis/verdict
def test_intact_when_healthy_no_incident_no_earnings(journal):
    settings = make_settings()
    pos = _open_position(journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    market = _FakeMarket({"AAPL": 101.0})  # +1/3 R, above the -0.5 floor

    health = assess_positions(journal, settings, market)

    assert len(health) == 1
    h = health[0]
    assert h["thesis_status"] == THESIS_INTACT
    assert h["verdict"] == VERDICT_HOLD


def test_at_risk_when_current_r_at_or_below_threshold(journal):
    settings = make_settings()
    _open_position(journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    # -0.5R exactly: pnl = -0.5 * risk_per_share * qty -> price = entry - 0.5*3 = 98.5
    market = _FakeMarket({"AAPL": 98.5})

    health = assess_positions(journal, settings, market)

    h = health[0]
    assert h["current_r"] == pytest.approx(AT_RISK_R_THRESHOLD, abs=1e-6)
    assert h["thesis_status"] == THESIS_AT_RISK
    assert h["verdict"] == VERDICT_ATTENTION


def test_at_risk_when_earnings_within_hold_window(journal):
    settings = make_settings()
    pos = _open_position(journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    journal.conn.execute(
        "UPDATE trade_proposals SET earnings_within_hold_window = 1 WHERE proposal_id = ?",
        (pos["proposal_id"],),
    )
    # Seed a minimal trade_proposals row first since UPDATE above needs a row to hit.
    journal.conn.commit()
    journal.insert("trade_proposals", {
        "proposal_id": pos["proposal_id"], "candidate_id": pos["candidate_id"], "symbol": "AAPL",
        "earnings_within_hold_window": 1,
    })
    market = _FakeMarket({"AAPL": 105.0})  # healthy R -- earnings flag alone must trigger AT_RISK

    health = assess_positions(journal, settings, market)

    h = health[0]
    assert h["earnings_within_hold_window"] is True
    assert h["thesis_status"] == THESIS_AT_RISK
    assert h["verdict"] == VERDICT_ATTENTION


def test_broken_when_open_protection_incident(journal):
    settings = make_settings()
    pos = _open_position(journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    _open_incident(journal, pos["position_id"], symbol="AAPL")
    market = _FakeMarket({"AAPL": 105.0})  # healthy R -- incident alone must trigger BROKEN

    health = assess_positions(journal, settings, market)

    h = health[0]
    assert h["thesis_status"] == THESIS_BROKEN
    assert h["verdict"] == VERDICT_EXIT_REVIEW


def test_broken_overrides_at_risk_when_both_conditions_present(journal):
    """BROKEN takes precedence -- an incident on top of a bad-R position must
    not be diluted into a mere AT_RISK reading."""
    settings = make_settings()
    pos = _open_position(journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    _open_incident(journal, pos["position_id"], symbol="AAPL")
    market = _FakeMarket({"AAPL": 90.0})  # deeply negative R AND an incident

    health = assess_positions(journal, settings, market)

    assert health[0]["thesis_status"] == THESIS_BROKEN


def test_resolved_incident_does_not_trigger_broken(journal):
    """A CLOSED incident (resolved_at_utc set) must not count as 'open' --
    only unresolved incidents matter."""
    settings = make_settings()
    pos = _open_position(journal, symbol="AAPL")
    journal.insert("protection_checks", {
        "check_id": new_id("chk"), "position_id": pos["position_id"], "symbol": "AAPL",
        "protection_status": "unprotected", "severity": "critical", "detail": "old, resolved",
        "resolved_at_utc": timeutils.to_iso(timeutils.now_utc()),
    })
    market = _FakeMarket({"AAPL": 105.0})

    health = assess_positions(journal, settings, market)

    assert health[0]["thesis_status"] == THESIS_INTACT


def test_at_risk_when_stop_equals_entry_a_degenerate_risk_basis(journal):
    """A live price IS available but current_r comes back None (stop==entry,
    risk_per_share=0) -- this must surface as AT_RISK, never silently as
    INTACT. 'Can't compute this position's risk' is not the same claim as
    'this position is fine' (unknown-never-zero, same posture every other
    measurement layer in this codebase follows)."""
    settings = make_settings()
    _open_position(journal, symbol="AAPL", entry=100.0, stop=100.0, target=106.0)
    market = _FakeMarket({"AAPL": 105.0})  # a real, fresh price -- just no usable risk basis

    h = assess_positions(journal, settings, market)[0]

    assert h["current_r"] is None
    assert h["thesis_status"] == THESIS_AT_RISK
    assert h["verdict"] == VERDICT_ATTENTION


# --------------------------------------------------------------- R math
def test_distance_to_stop_and_target_r_hand_verified(journal):
    settings = make_settings()
    # entry=100, stop=97 (risk_per_share=3), target=106 -> target is +2R.
    _open_position(journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0)
    market = _FakeMarket({"AAPL": 101.5})  # +0.5R

    h = assess_positions(journal, settings, market)[0]

    assert h["current_r"] == pytest.approx(0.5, abs=1e-6)
    assert h["distance_to_stop_r"] == pytest.approx(1.5, abs=1e-6)   # 0.5 - (-1)
    assert h["distance_to_target_r"] == pytest.approx(1.5, abs=1e-6)  # 2.0 - 0.5


def test_short_position_r_math(journal):
    settings = make_settings()
    # short: entry=100, stop=103 (risk_per_share=3), target=94 (+2R favorable)
    _open_position(journal, symbol="AAPL", entry=100.0, stop=103.0, target=94.0, direction="short")
    market = _FakeMarket({"AAPL": 98.5})  # price fell 1.5 -> +0.5R for a short

    h = assess_positions(journal, settings, market)[0]

    assert h["current_r"] == pytest.approx(0.5, abs=1e-6)


# ------------------------------------------------------------- days_held
def test_days_held_computed_from_opened_at(journal):
    settings = make_settings()
    _open_position(journal, symbol="AAPL", opened_days_ago=2.0, max_holding_days=5)
    market = _FakeMarket({"AAPL": 100.0})

    h = assess_positions(journal, settings, market)[0]

    assert h["days_held"] == pytest.approx(2.0, abs=0.01)
    assert h["max_holding_days"] == 5


# --------------------------------------------------------- fail-safe / coverage
def test_never_raises_on_a_bad_snapshot(journal):
    """A market-data outage for one symbol must not crash the whole sweep --
    that row reports itself unusable, the rest still work."""
    settings = make_settings()
    _open_position(journal, symbol="AAPL")
    _open_position(journal, symbol="MSFT")
    market = _FakeMarket({"MSFT": 300.0}, raises_for={"AAPL"})

    health = assess_positions(journal, settings, market)

    assert len(health) == 2
    aapl = next(h for h in health if h["symbol"] == "AAPL")
    msft = next(h for h in health if h["symbol"] == "MSFT")
    assert aapl["current_r"] is None
    assert aapl["freshness_status"] == "no_snapshot"
    assert msft["current_r"] is not None


def test_empty_when_no_open_positions(journal):
    settings = make_settings()
    market = _FakeMarket({})

    assert assess_positions(journal, settings, market) == []


def test_non_vacuity_real_scan_produces_assessable_position(orchestrator):
    """A real scan + approval, not just synthetic rows -- confirms the whole
    pipeline (candidate_scanner -> orchestrator -> assess_positions) agrees
    on what a position looks like."""
    orchestrator.run_scan_once()
    pending = orchestrator.list_open_proposals()
    assert pending  # non-vacuity guard
    ok, _ = orchestrator.approve_proposal(pending[0]["proposal_id"], approver="test")
    assert ok

    health = assess_positions(orchestrator.journal, orchestrator.settings, orchestrator.market)

    assert len(health) == 1
    assert health[0]["thesis_status"] in (THESIS_INTACT, THESIS_AT_RISK, THESIS_BROKEN)


# ----------------------------------------------------------- no-touch proof
def test_exit_review_never_touches_orders_or_execution():
    """Grep proof: position_health.py must never import/call anything from
    the order-submission or exit-execution surface. EXIT_REVIEW is a human
    flag; assess_positions is pure read."""
    import pathlib

    import alphaos.reports.position_health as ph_mod

    text = pathlib.Path(ph_mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("execute_proposal", "close_position", "OrderManager(", "approve_proposal"):
        assert forbidden not in text, f"position_health.py references {forbidden}"


def test_exit_review_behavior_never_creates_orders_or_exits(journal):
    """Behavior half of the no-touch proof: assessing a BROKEN position must
    leave paper_orders/exits completely untouched."""
    settings = make_settings()
    pos = _open_position(journal, symbol="AAPL")
    _open_incident(journal, pos["position_id"], symbol="AAPL")
    market = _FakeMarket({"AAPL": 90.0})

    health = assess_positions(journal, settings, market)

    assert health[0]["verdict"] == VERDICT_EXIT_REVIEW
    assert journal.count_rows("paper_orders") == 0
    assert journal.count_rows("exits") == 0
