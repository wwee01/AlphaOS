"""MFE/MAE tracking (Fable 5 review PR1). The running excursion is tracked in R
terms per monitor pass (monitoring_snapshots, pre-existing) and folded into the
exit tick at close — replacing a prior bug where close_position() wrote a crude
%-based approximation instead of the already-tracked running extremum. Also
covers the idempotent backfill for closed trades from before this existed.
Hermetic; no network, no order/approval side effects beyond the pre-existing,
unchanged close_position() exit-order write."""

from __future__ import annotations

from alphaos.execution.mfe_mae_backfill import backfill_mfe_mae, excursion_from_bars
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from conftest import inject_pending_proposal, make_settings


def _orch(**over):
    return Orchestrator(settings=make_settings(**over), journal=JournalStore(":memory:"))


def _open_position(o, symbol="AAPL"):
    pid, _ = inject_pending_proposal(o, symbol=symbol)
    ok, msg = o.approve_proposal(pid, approver="test")
    assert ok, msg
    return o.journal.one("SELECT * FROM positions WHERE status = 'open' AND symbol = ?", (symbol,))


# --------------------------------------------------------------- live tracking
def test_monitor_pass_folds_running_mfe_mae_in_r_terms():
    o = _orch()
    pos = _open_position(o)
    risk = abs(pos["avg_entry_price"] - pos["stop_price"])
    # inject_pending_proposal sizes stop=3%/target=6% of entry, which makes
    # target ALWAYS exactly entry + 2*risk (algebraically, not a coincidence) —
    # so a "+2R favorable" probe can land exactly ON the target depending on
    # the mock price's rounding (mock prices are seeded per calendar trading
    # day, so this is not always reproducible — it bit a real run once). Use
    # 1.5R instead: comfortably short of the 2R target regardless of entry.
    up = pos["avg_entry_price"] + 1.5 * risk     # +1.5R favorable, short of target
    down = pos["avg_entry_price"] - 0.5 * risk   # -0.5R adverse

    o.positions.monitor(price_overrides={"AAPL": up})
    snap1 = o.journal.one("SELECT * FROM monitoring_snapshots ORDER BY id DESC LIMIT 1")
    # Textbook excursion semantics: entry itself is an implicit R=0 observation,
    # so a trade that's only ever been favorable has MAE=0 (never dipped below
    # entry) — NOT the observed +1.5R reported as if it were also "adverse".
    assert snap1["unrealized_r"] == 1.5 and snap1["mfe"] == 1.5 and snap1["mae"] == 0.0

    o.positions.monitor(price_overrides={"AAPL": down})
    snap2 = o.journal.one("SELECT * FROM monitoring_snapshots ORDER BY id DESC LIMIT 1")
    # mfe stays at the prior high-water mark; mae drops to the new low
    assert snap2["unrealized_r"] == -0.5 and snap2["mfe"] == 1.5 and snap2["mae"] == -0.5
    o.close()


def test_close_position_folds_exit_tick_into_running_excursion():
    o = _orch()
    pos = _open_position(o)
    risk = abs(pos["avg_entry_price"] - pos["stop_price"])
    up = pos["avg_entry_price"] + 1.5 * risk  # clearly short of the (always-exact) 2R target
    down = pos["avg_entry_price"] - 0.5 * risk
    exit_price = pos["avg_entry_price"] + 0.1 * risk   # exits near breakeven

    o.positions.monitor(price_overrides={"AAPL": up})
    o.positions.monitor(price_overrides={"AAPL": down})
    o.positions.close_position(pos["position_id"], exit_price, "manual_test", triggered_by="test")

    out = o.journal.one("SELECT * FROM trade_outcomes ORDER BY id DESC LIMIT 1")
    # The final return alone (+0.1R) would have wrongly clamped mfe/mae under the
    # old %-based approximation (mfe=0.1%, mae=0.0). The fix preserves the full
    # observed path: +1.5R favorable, -0.5R adverse — in R terms, not %.
    assert out["mfe"] == 1.5
    assert out["mae"] == -0.5
    assert out["mfe_mae_source"] == "live_tracked"
    assert out["realized_r"] == 0.1
    o.close()


def test_close_position_with_no_prior_monitor_pass_still_folds_exit_tick():
    """If the position closes on the very first tick (no prior monitoring_snapshots
    row), the exit itself is still one observation and mfe/mae reflect it."""
    o = _orch()
    pos = _open_position(o)
    risk = abs(pos["avg_entry_price"] - pos["stop_price"])
    exit_price = pos["avg_entry_price"] + risk   # +1R, no prior monitor() call
    o.positions.close_position(pos["position_id"], exit_price, "target", triggered_by="test")
    out = o.journal.one("SELECT * FROM trade_outcomes ORDER BY id DESC LIMIT 1")
    # +1R and nothing else observed -> MAE=0 (never adverse), not +1.0.
    assert out["mfe"] == 1.0 and out["mae"] == 0.0
    o.close()


def test_mfe_mae_change_does_not_alter_exit_or_order_behavior():
    """The fix only changes what gets WRITTEN to trade_outcomes.mfe/.mae — the
    exit order/fill/position-close path is unchanged."""
    o = _orch()
    pos = _open_position(o)
    before_orders = o.journal.count_rows("paper_orders")
    before_fills = o.journal.count_rows("paper_fills")
    exit_price = pos["avg_entry_price"] + 0.1
    result = o.positions.close_position(pos["position_id"], exit_price, "manual_test", triggered_by="test")
    assert result is not None
    # close_position legitimately records ONE exit order + fill (pre-existing,
    # unchanged behavior) — the MFE/MAE fix adds no additional order activity.
    assert o.journal.count_rows("paper_orders") == before_orders + 1
    assert o.journal.count_rows("paper_fills") == before_fills + 1
    assert o.journal.one("SELECT status FROM positions WHERE position_id = ?",
                         (pos["position_id"],))["status"] == "closed"
    o.close()


def test_real_money_stays_unreachable_after_close():
    o = _orch()
    pos = _open_position(o)
    o.positions.close_position(pos["position_id"], pos["avg_entry_price"] + 0.1, "manual_test")
    h = o.system_health()
    assert h["real_money_trading"] == "unreachable"
    assert h["manual_approval"] == "required"
    o.close()


# ---------------------------------------------------------------- pure engine
def test_excursion_from_bars_long():
    bars = [
        {"high": 105.0, "low": 99.0},   # +0.5R fav / -0.1R adv  (risk=10, entry=100)
        {"high": 110.0, "low": 95.0},   # +1.0R fav / -0.5R adv
    ]
    mfe, mae = excursion_from_bars(entry=100.0, stop=90.0, direction="long", bars=bars)
    assert mfe == 1.0 and mae == -0.5


def test_excursion_from_bars_short():
    # short: entry=100, stop=110 (risk=10); favorable = price going DOWN
    bars = [{"high": 105.0, "low": 92.0}]
    mfe, mae = excursion_from_bars(entry=100.0, stop=110.0, direction="short", bars=bars)
    assert mfe == 0.8   # (100-92)/10
    assert mae == -0.5  # (100-105)/10


def test_excursion_from_bars_no_stop_returns_none():
    assert excursion_from_bars(100.0, None, "long", [{"high": 105, "low": 99}]) == (None, None)


def test_excursion_from_bars_no_bars_returns_none():
    assert excursion_from_bars(100.0, 90.0, "long", []) == (None, None)


# -------------------------------------------------------------------- backfill
def test_backfill_prefers_monitoring_snapshots_when_present():
    o = _orch()
    pos = _open_position(o)
    risk = abs(pos["avg_entry_price"] - pos["stop_price"])
    o.positions.monitor(price_overrides={"AAPL": pos["avg_entry_price"] + 1.5 * risk})
    o.positions.close_position(pos["position_id"], pos["avg_entry_price"] + 0.1, "manual_test")
    # Simulate an old row from before mfe_mae_source existed.
    out_id = o.journal.one("SELECT outcome_id FROM trade_outcomes ORDER BY id DESC LIMIT 1")["outcome_id"]
    o.journal.conn.execute(
        "UPDATE trade_outcomes SET mfe = 0.0, mae = 0.0, mfe_mae_source = NULL WHERE outcome_id = ?",
        (out_id,))
    o.journal.conn.commit()

    res = backfill_mfe_mae(o.journal, bars_provider=None)
    assert res == {"total": 1, "from_snapshots": 1, "from_bars": 0, "unavailable": 0}
    row = o.journal.one("SELECT * FROM trade_outcomes WHERE outcome_id = ?", (out_id,))
    # Only ever favorable (+1.5R, one observation) -> MAE=0, not +1.5.
    assert row["mfe"] == 1.5 and row["mae"] == 0.0
    assert row["mfe_mae_source"] == "backfilled_from_snapshots"
    o.close()


class _FakeBars:
    def __init__(self, bars):
        self.bars = bars

    def get_daily_bars(self, symbol, start, end):
        return self.bars


def test_backfill_falls_back_to_bars_when_no_snapshots():
    o = _orch()
    pos = _open_position(o)
    # No monitor() call -> no monitoring_snapshots rows for this position.
    o.positions.close_position(pos["position_id"], pos["avg_entry_price"] + 0.1, "manual_test")
    out = o.journal.one("SELECT * FROM trade_outcomes ORDER BY id DESC LIMIT 1")
    o.journal.conn.execute(
        "UPDATE trade_outcomes SET mfe_mae_source = NULL WHERE outcome_id = ?", (out["outcome_id"],))
    o.journal.conn.commit()

    risk = abs(pos["avg_entry_price"] - pos["stop_price"])
    provider = _FakeBars([{"date": "2026-01-02", "high": pos["avg_entry_price"] + risk,
                          "low": pos["avg_entry_price"] - 0.2 * risk}])
    res = backfill_mfe_mae(o.journal, bars_provider=provider)
    assert res["from_bars"] == 1 and res["from_snapshots"] == 0
    row = o.journal.one("SELECT * FROM trade_outcomes WHERE outcome_id = ?", (out["outcome_id"],))
    assert row["mfe"] == 1.0 and row["mae"] == -0.2
    assert row["mfe_mae_source"] == "backfilled_from_bars"
    o.close()


def test_backfill_marks_unavailable_without_overwriting_existing_values():
    o = _orch()
    pos = _open_position(o)
    o.positions.close_position(pos["position_id"], pos["avg_entry_price"] + 0.1, "manual_test")
    out = o.journal.one("SELECT * FROM trade_outcomes ORDER BY id DESC LIMIT 1")
    o.journal.conn.execute(
        "UPDATE trade_outcomes SET mfe = 0.42, mae = -0.1, mfe_mae_source = NULL WHERE outcome_id = ?",
        (out["outcome_id"],))
    o.journal.conn.commit()

    res = backfill_mfe_mae(o.journal, bars_provider=_FakeBars([]))  # no bars available
    assert res["unavailable"] == 1
    row = o.journal.one("SELECT * FROM trade_outcomes WHERE outcome_id = ?", (out["outcome_id"],))
    assert row["mfe"] == 0.42 and row["mae"] == -0.1   # untouched, not invented
    assert row["mfe_mae_source"] == "unavailable"
    o.close()


def test_backfill_is_idempotent():
    o = _orch()
    pos = _open_position(o)
    o.positions.close_position(pos["position_id"], pos["avg_entry_price"] + 0.1, "manual_test")
    out_id = o.journal.one("SELECT outcome_id FROM trade_outcomes ORDER BY id DESC LIMIT 1")["outcome_id"]
    o.journal.conn.execute(
        "UPDATE trade_outcomes SET mfe_mae_source = NULL WHERE outcome_id = ?", (out_id,))
    o.journal.conn.commit()

    first = backfill_mfe_mae(o.journal, bars_provider=None)
    assert first["total"] == 1
    second = backfill_mfe_mae(o.journal, bars_provider=None)
    assert second["total"] == 0   # nothing left with mfe_mae_source IS NULL
    o.close()


def test_backfill_no_rows_is_safe():
    o = _orch()
    res = backfill_mfe_mae(o.journal, bars_provider=None)
    assert res == {"total": 0, "from_snapshots": 0, "from_bars": 0, "unavailable": 0}
    o.close()


# ------------------------------------------- MEDIUM-1: textbook 0R-anchored excursion
# (Opus audit MEDIUM-1: entry itself is an implicit R=0 observation, so MFE is
# always >= 0 and MAE is always <= 0 — never the least-favorable-OBSERVED point
# reported as "adverse" when the trade in fact never dipped below entry.)
def test_always_favorable_trade_has_mfe_positive_mae_zero():
    o = _orch()
    pos = _open_position(o)
    risk = abs(pos["avg_entry_price"] - pos["stop_price"])
    o.positions.monitor(price_overrides={"AAPL": pos["avg_entry_price"] + 0.3 * risk})
    o.positions.monitor(price_overrides={"AAPL": pos["avg_entry_price"] + 0.8 * risk})
    o.positions.close_position(pos["position_id"], pos["avg_entry_price"] + 1.2 * risk, "target")
    out = o.journal.one("SELECT * FROM trade_outcomes ORDER BY id DESC LIMIT 1")
    assert out["mfe"] == 1.2   # best point reached
    assert out["mae"] == 0.0   # never adverse -> MAE=0, NOT the smallest favorable point (+0.3)
    o.close()


def test_always_adverse_trade_has_mfe_zero_mae_negative():
    o = _orch()
    pos = _open_position(o)
    risk = abs(pos["avg_entry_price"] - pos["stop_price"])
    o.positions.monitor(price_overrides={"AAPL": pos["avg_entry_price"] - 0.2 * risk})
    o.positions.monitor(price_overrides={"AAPL": pos["avg_entry_price"] - 0.6 * risk})
    out_row = o.positions.close_position(pos["position_id"], pos["avg_entry_price"] - 0.3 * risk, "stop")
    assert out_row is not None
    out = o.journal.one("SELECT * FROM trade_outcomes ORDER BY id DESC LIMIT 1")
    assert out["mfe"] == 0.0    # never favorable -> MFE=0, NOT the least-bad point (-0.2)
    assert out["mae"] == -0.6   # worst point reached


def test_favorable_then_adverse_path_captures_both_correctly():
    """The 0-anchor must be a no-op when the path genuinely visits both
    territories — this must keep matching the original path-dependent test."""
    o = _orch()
    pos = _open_position(o)
    risk = abs(pos["avg_entry_price"] - pos["stop_price"])
    # 1.5R, not 2R: inject_pending_proposal's target is always exactly entry +
    # 2*risk, so a "+2R" probe can land exactly on it depending on the mock
    # price's rounding — see the comment in
    # test_monitor_pass_folds_running_mfe_mae_in_r_terms above.
    o.positions.monitor(price_overrides={"AAPL": pos["avg_entry_price"] + 1.5 * risk})
    o.positions.monitor(price_overrides={"AAPL": pos["avg_entry_price"] - 0.5 * risk})
    o.positions.close_position(pos["position_id"], pos["avg_entry_price"] + 0.1 * risk, "manual_test")
    out = o.journal.one("SELECT * FROM trade_outcomes ORDER BY id DESC LIMIT 1")
    assert out["mfe"] == 1.5 and out["mae"] == -0.5   # unaffected by the 0-anchor fix
    o.close()


def _open_short_position(o, symbol="AAPL", entry=100.0, stop=105.0, target=88.0):
    """A short position inserted directly (bypassing approval/proposal — not
    what this test is about) so close_position()'s short-direction fold can be
    exercised in isolation."""
    import datetime
    from alphaos.util.ids import new_id
    position_id = new_id("pos")
    o.journal.insert("positions", {
        "position_id": position_id, "symbol": symbol, "direction": "short", "strategy": "swing",
        "qty": 10, "avg_entry_price": entry, "stop_price": stop, "target_price": target,
        "max_holding_days": 5, "opened_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "opened_market_date": datetime.date.today().isoformat(), "status": "open",
        "current_price": entry, "unrealized_pnl": 0.0, "is_short": 1,
    })
    return o.journal.one("SELECT * FROM positions WHERE position_id = ?", (position_id,))


def test_short_close_position_folds_favorable_and_adverse_correctly():
    """Short: price falling is favorable, price rising is adverse — the exact
    inverse of long. Confirms the 0-anchor fix is direction-correct, not just
    long-tested."""
    o = _orch()
    pos = _open_short_position(o, entry=100.0, stop=105.0)   # risk = 5
    risk = abs(pos["avg_entry_price"] - pos["stop_price"])
    # Price drops 3R in AlphaOS's favor (short profits on a fall), then closes
    # slightly less favorably than the best point but still favorable overall.
    o.positions.monitor(price_overrides={"AAPL": pos["avg_entry_price"] - 3 * risk})
    o.positions.close_position(pos["position_id"], pos["avg_entry_price"] - 1 * risk, "manual_test")
    out = o.journal.one("SELECT * FROM trade_outcomes ORDER BY id DESC LIMIT 1")
    assert out["direction"] == "short"
    assert out["mfe"] == 3.0    # best favorable point (price -3R from entry)
    assert out["mae"] == 0.0    # never adverse (price never rose above entry)
    o.close()


def test_short_close_position_adverse_excursion_correctly_signed():
    o = _orch()
    pos = _open_short_position(o, entry=100.0, stop=105.0)   # risk = 5
    risk = abs(pos["avg_entry_price"] - pos["stop_price"])
    # Price rises 2R against the short (adverse) before the position exits
    # near breakeven.
    o.positions.monitor(price_overrides={"AAPL": pos["avg_entry_price"] + 2 * risk})
    o.positions.close_position(pos["position_id"], pos["avg_entry_price"] + 0.1 * risk, "manual_test")
    out = o.journal.one("SELECT * FROM trade_outcomes ORDER BY id DESC LIMIT 1")
    assert out["mfe"] == 0.0     # never favorable
    assert out["mae"] == -2.0    # worst point, correctly signed for short
    o.close()


def test_fold_excursion_still_returns_none_without_a_stop():
    """The 0-anchor must NOT apply when R is genuinely undefined (no stop) —
    that would misreport 'unknown' as 'flat'."""
    o = _orch()
    pos = _open_position(o)
    o.journal.conn.execute(
        "UPDATE positions SET stop_price = NULL WHERE position_id = ?", (pos["position_id"],))
    o.journal.conn.commit()
    mfe, mae = o.positions._fold_excursion(pos["position_id"], None)
    assert mfe is None and mae is None
    o.close()


def test_excursion_from_bars_always_favorable_gives_mae_zero():
    bars = [{"high": 108.0, "low": 102.0}, {"high": 112.0, "low": 105.0}]   # never below entry=100
    mfe, mae = excursion_from_bars(entry=100.0, stop=90.0, direction="long", bars=bars)
    assert mfe == 1.2 and mae == 0.0


def test_excursion_from_bars_always_adverse_gives_mfe_zero():
    bars = [{"high": 99.0, "low": 92.0}]   # never above entry=100
    mfe, mae = excursion_from_bars(entry=100.0, stop=90.0, direction="long", bars=bars)
    assert mfe == 0.0 and mae == -0.8
