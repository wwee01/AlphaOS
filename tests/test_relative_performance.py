"""Relative-performance report (PR9.5): pure compute over equity_snapshots +
benchmark_bars. Worked examples with known expected outputs (hand-computed
independently, not derived from the implementation), plus floor-gating and
empty/degenerate-input behavior. No I/O, no clock, no RNG in the function
under test -- everything here is deterministic by construction.
"""

from __future__ import annotations

from alphaos.journal.journal_store import JournalStore
from alphaos.reports.relative_performance import (
    MIN_PAIRED_DAYS_FOR_BETA,
    MIN_PAIRED_DAYS_FOR_RELATIVE_RETURN,
    build_relative_performance_report,
    compute_relative_performance,
)
from conftest import make_settings


def _eq_rows(pairs):
    return [{"market_date": d, "equity": e} for d, e in pairs]


def _bench_rows(pairs):
    return [{"bar_date": d, "close": c} for d, c in pairs]


# ----------------------------------------------------------- worked example
# Hand-computed independently (see the session's own verification script):
# equity 100000 -> 101000 -> 100500 -> 102000 -> 103000 -> 102500
# SPY       500 ->    505 ->    503 ->    508 ->    510 ->    509
# equity total return = 2.5% ; SPY total return = 1.8% ; excess = 0.7%
_WORKED_DATES = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07", "2026-07-08"]
_WORKED_EQUITY = [100000, 101000, 100500, 102000, 103000, 102500]
_WORKED_SPY = [500, 505, 503, 508, 510, 509]


def test_worked_example_total_and_excess_return_matches_hand_computed_values():
    equity_rows = _eq_rows(zip(_WORKED_DATES, _WORKED_EQUITY))
    bench_rows = _bench_rows(zip(_WORKED_DATES, _WORKED_SPY))
    assert len(equity_rows) - 1 == 5 == MIN_PAIRED_DAYS_FOR_RELATIVE_RETURN  # exactly at the floor

    rep = compute_relative_performance(equity_rows, bench_rows)

    assert rep["paired_trading_days"] == 5
    assert rep["equity_total_return_pct"] == 2.5
    assert rep["benchmark_total_return_pct"] == 1.8
    assert rep["excess_return_pct"] == 0.7


def test_worked_example_equity_curve_matches_hand_computed_cumulative_values():
    equity_rows = _eq_rows(zip(_WORKED_DATES, _WORKED_EQUITY))
    bench_rows = _bench_rows(zip(_WORKED_DATES, _WORKED_SPY))

    rep = compute_relative_performance(equity_rows, bench_rows)

    curve = {c["date"]: c["cum_return_pct"] for c in rep["equity_curve"]}
    assert curve["2026-07-01"] == 0.0                    # first point is the baseline
    assert curve["2026-07-02"] == 1.0                    # 101000/100000 - 1
    assert curve["2026-07-08"] == 2.5                    # matches the total return above


# ------------------------------------------------------------------ floors
def test_below_relative_return_floor_shows_no_comparison():
    equity_rows = _eq_rows(zip(_WORKED_DATES[:4], _WORKED_EQUITY[:4]))  # only 3 paired days
    bench_rows = _bench_rows(zip(_WORKED_DATES[:4], _WORKED_SPY[:4]))

    rep = compute_relative_performance(equity_rows, bench_rows)

    assert rep["paired_trading_days"] == 3
    assert rep["equity_total_return_pct"] is None
    assert rep["excess_return_pct"] is None
    assert "below floor" in rep["relative_return_note"]
    # the equity curve itself is NOT floor-gated -- it still renders
    assert len(rep["equity_curve"]) == 4


def test_below_beta_floor_shows_no_beta_even_with_enough_for_relative_return():
    equity_rows = _eq_rows(zip(_WORKED_DATES, _WORKED_EQUITY))  # 5 paired days: enough for
    bench_rows = _bench_rows(zip(_WORKED_DATES, _WORKED_SPY))   # return, not enough for beta (20)

    rep = compute_relative_performance(equity_rows, bench_rows)

    assert rep["excess_return_pct"] is not None       # return floor met
    assert rep["rolling_beta"] is None                # beta floor NOT met
    assert "below floor" in rep["rolling_beta_note"]


def test_rolling_beta_matches_hand_computed_value_on_a_synthetic_series():
    """30-day synthetic series (repeating the worked-example's 5-day return
    pattern 6x) -- comfortably above the 20-day beta floor. Expected beta
    hand-computed independently (see the session's own verification script)."""
    import datetime

    base_date = datetime.date(2026, 1, 1)
    equity = [100000.0]
    spy = [500.0]
    daily_eq_multipliers = [1.01, 0.99505, 1.014925, 1.009804, 0.995146]  # from the worked example
    daily_spy_multipliers = [1.01, 0.996040, 1.009940, 1.003937, 0.998039]
    for _ in range(6):
        for m_eq, m_spy in zip(daily_eq_multipliers, daily_spy_multipliers):
            equity.append(equity[-1] * m_eq)
            spy.append(spy[-1] * m_spy)

    dates = [(base_date + datetime.timedelta(days=i)).isoformat() for i in range(len(equity))]
    equity_rows = _eq_rows(zip(dates, equity))
    bench_rows = _bench_rows(zip(dates, spy))
    assert len(equity_rows) - 1 == 30 >= MIN_PAIRED_DAYS_FOR_BETA

    rep = compute_relative_performance(equity_rows, bench_rows)

    assert rep["rolling_beta"] is not None
    # Hand-computed beta for one 5-day cycle was ~1.3379; a periodic repeat of
    # the exact same 5-day pattern preserves that ratio over any 20-day window.
    assert abs(rep["rolling_beta"] - 1.3379) < 0.01


# ----------------------------------------------------------------- monthly
def test_monthly_line_marks_current_month_in_progress_vs_a_complete_month(monkeypatch):
    from alphaos.util import timeutils

    monkeypatch.setattr(timeutils, "market_date", lambda dt=None: __import__("datetime").date(2026, 8, 15))

    # 12 paired trading days in July (a completed month) + 3 in August (in progress).
    import datetime
    july_dates = [(datetime.date(2026, 7, 1) + datetime.timedelta(days=i)).isoformat() for i in range(12)]
    aug_dates = [(datetime.date(2026, 8, 1) + datetime.timedelta(days=i)).isoformat() for i in range(3)]
    all_dates = july_dates + aug_dates
    equity = [100000 * (1.001 ** i) for i in range(len(all_dates))]
    spy = [500 * (1.0005 ** i) for i in range(len(all_dates))]

    rep = compute_relative_performance(_eq_rows(zip(all_dates, equity)), _bench_rows(zip(all_dates, spy)))

    assert rep["monthly"]["2026-07"]["is_complete_month"] is True
    assert rep["monthly"]["2026-08"]["is_complete_month"] is False


def test_monthly_line_below_floor_shows_note_not_a_fabricated_number():
    equity_rows = _eq_rows(zip(_WORKED_DATES[:3], _WORKED_EQUITY[:3]))  # 2 paired days, all July
    bench_rows = _bench_rows(zip(_WORKED_DATES[:3], _WORKED_SPY[:3]))

    rep = compute_relative_performance(equity_rows, bench_rows)

    month = rep["monthly"]["2026-07"]
    assert month["equity_return_pct"] is None
    assert "below floor" in month["note"]


# ------------------------------------------------------------ degenerate input
def test_empty_input_never_raises():
    rep = compute_relative_performance([], [])

    assert rep["paired_trading_days"] == 0
    assert rep["equity_curve"] == []
    assert rep["excess_return_pct"] is None
    assert rep["rolling_beta"] is None


def test_single_equity_row_no_bench_match_never_raises():
    rep = compute_relative_performance(_eq_rows([("2026-07-01", 100000)]), [])

    assert rep["paired_trading_days"] == 0
    assert len(rep["equity_curve"]) == 1
    assert rep["equity_curve"][0]["cum_return_pct"] == 0.0


def test_unmatched_dates_are_excluded_from_pairing_but_not_from_the_equity_curve():
    """An equity snapshot on a date with no matching SPY bar (e.g. a data gap)
    still extends the equity curve, but contributes no paired return."""
    equity_rows = _eq_rows([("2026-07-01", 100000), ("2026-07-02", 101000), ("2026-07-03", 102000)])
    bench_rows = _bench_rows([("2026-07-01", 500), ("2026-07-03", 505)])  # missing 07-02

    rep = compute_relative_performance(equity_rows, bench_rows)

    assert len(rep["equity_curve"]) == 3          # all 3 equity points render
    assert rep["paired_trading_days"] == 0        # neither day-over-day pair has both bench dates


# -------------------------------------------------------------------- caveat
def test_caveat_is_always_present():
    rep = compute_relative_performance([], [])
    assert rep["caveat"]
    assert "descriptive" in rep["caveat"].lower() or "no statistical" in rep["caveat"].lower()


# ----------------------------------------------------------- report wrapper
def test_build_report_excludes_mock_equity_rows():
    journal = JournalStore(":memory:")
    settings = make_settings()
    from alphaos.util.ids import new_id

    journal.insert("equity_snapshots", {
        "snapshot_id": new_id("eqsnap"), "market_date": "2026-07-01", "equity": 100000.0,
        "equity_source": "static_config", "is_mock": 1,
    })

    rep = build_relative_performance_report(journal, settings)

    assert rep["equity_snapshot_count"] == 0  # the mock row is excluded
    journal.close()


def test_build_report_reads_real_rows_in_ascending_date_order():
    journal = JournalStore(":memory:")
    settings = make_settings()
    from alphaos.util.ids import new_id

    for d, e in [("2026-07-03", 300), ("2026-07-01", 100), ("2026-07-02", 200)]:
        journal.insert("equity_snapshots", {
            "snapshot_id": new_id("eqsnap"), "market_date": d, "equity": e,
            "equity_source": "static_config", "is_mock": 0,
        })

    rep = build_relative_performance_report(journal, settings)

    dates_in_curve = [c["date"] for c in rep["equity_curve"]]
    assert dates_in_curve == ["2026-07-01", "2026-07-02", "2026-07-03"]  # ASC, not insert order
    journal.close()


def test_render_markdown_never_raises_on_empty_report():
    from alphaos.reports.relative_performance import render_markdown

    journal = JournalStore(":memory:")
    settings = make_settings()
    rep = build_relative_performance_report(journal, settings)

    md = render_markdown(rep)  # must not raise even with zero data

    assert "Relative Performance Report" in md
    assert "no paired trading days yet" in md
    journal.close()


# ---------------------------------------------------------------- no-read grep
def test_relative_performance_module_never_referenced_by_decision_paths():
    import pathlib

    import alphaos.approval as approval_mod
    import alphaos.risk.risk_engine as risk_mod

    for mod, name in ((approval_mod, "approval.py"), (risk_mod, "risk_engine.py")):
        text = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert "relative_performance" not in text, f"{name} references the relative-performance module"
