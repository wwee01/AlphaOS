"""Relative-performance report (PR9.5): the "beat the S&P" measurement the
2026-07-06 exit review found nowhere in this codebase. Pure computation over
``equity_snapshots``/``benchmark_bars`` rows (already captured by
``benchmark_capture.py``) -- descriptive only, exactly like every other
report in this package (``outcomes_summary.py``, ``metrics.py``): floors
before means, caveats always present, no statistical claims below floor.

Terminology, deliberately modest: "excess return" (equity return minus
benchmark return over the same window), not a CAPM-adjusted "true alpha" --
this codebase does not overclaim what a handful of trading days can support.
"""

from __future__ import annotations

import json
import math
from typing import Optional
from datetime import date
from alphaos.util.market_calendar import trading_days_between

from alphaos.util import timeutils

# Below this many PAIRED trading days (both an equity snapshot and a matching
# SPY bar exist), a total/excess-return number is descriptive noise, not a
# comparison worth trusting even loosely.
MIN_PAIRED_DAYS_FOR_RELATIVE_RETURN = 5
# A rolling beta needs enough points for the covariance/variance ratio to mean
# anything at all; below this, don't show one.
MIN_PAIRED_DAYS_FOR_BETA = 20
ROLLING_BETA_WINDOW_DAYS = 20
# A per-month excess-return line is shown once a month has at least this many
# paired trading days -- even a still-in-progress current month, explicitly
# marked as such (never conflated with a completed month's number).
MIN_PAIRED_DAYS_FOR_MONTHLY_LINE = 10

RELATIVE_PERFORMANCE_CAVEAT = (
    "Descriptive only — no statistical claims. \"Excess return\" is a simple "
    "equity-minus-benchmark difference, not a risk-adjusted or CAPM alpha; "
    "beta is a full covariance/variance ratio over the trailing window, not a "
    "forecast. Treat every number here as provisional until the sample floors "
    "are met, and even then as paper performance (see the cost-calibration "
    "report for the upper-bound caveat)."
)


def _pct(x: Optional[float]) -> Optional[float]:
    return round(x * 100, 4) if x is not None else None


def _covariance(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / n


def _variance(xs: list[float]) -> float:
    n = len(xs)
    mean_x = sum(xs) / n
    return sum((x - mean_x) ** 2 for x in xs) / n


def compute_relative_performance(equity_rows: list[dict], benchmark_rows: list[dict]) -> dict:
    """Pure. ``equity_rows`` and ``benchmark_rows`` must each be pre-sorted
    ASCENDING by date (market_date / bar_date respectively) -- callers own
    the query's ORDER BY. No clock, no I/O, no RNG.

    A "paired day" is an interval ending on a date where BOTH an equity snapshot and a benchmark
    bar exist -- only paired days ever contribute to a return/beta
    comparison; an equity-only day still extends the equity curve but is
    not itself a benchmark endpoint. Adjacent paired observations span intervening
    weekends or data gaps; a missing session valuation suppresses daily beta.
    """
    bench_close_by_date = {r["bar_date"]: r["close"] for r in benchmark_rows if r.get("close") is not None}

    equity_curve = []
    paired = []  # list of (date, equity_return, bench_return)
    cum_equity = 1.0
    prev_equity_row = None
    prev_paired_row = None
    benchmark_dates = sorted(bench_close_by_date)
    benchmark_index = {d: i for i, d in enumerate(benchmark_dates)}
    multi_session_intervals = 0

    for row in equity_rows:
        d, eq = row["market_date"], row["equity"]
        if prev_equity_row is None:
            equity_curve.append({"date": d, "cum_return_pct": 0.0})
        else:
            prev_eq = prev_equity_row["equity"]
            r_eq = (eq / prev_eq - 1.0) if prev_eq else 0.0
            cum_equity *= (1.0 + r_eq)
            equity_curve.append({"date": d, "cum_return_pct": _pct(cum_equity - 1.0)})
        prev_equity_row = row

        # A non-comparable valuation interrupts the evidence window; never
        # bridge a deposit, unknown cashflow or invalid valuation silently.
        if row.get("comparison_eligible", True) is False:
            prev_paired_row = None
            continue
        if d not in bench_close_by_date or not bench_close_by_date[d] or not eq:
            continue
        if prev_paired_row is not None:
            prev_d, prev_eq = prev_paired_row["market_date"], prev_paired_row["equity"]
            r_eq = eq / prev_eq - 1.0
            r_bench = bench_close_by_date[d] / bench_close_by_date[prev_d] - 1.0
            paired.append((d, r_eq, r_bench))
            if (benchmark_index[d] - benchmark_index[prev_d] > 1
                    or trading_days_between(date.fromisoformat(prev_d), date.fromisoformat(d)) > 1):
                multi_session_intervals += 1
        prev_paired_row = row

    n_paired = len(paired)
    paired_eq_returns = [p[1] for p in paired]
    paired_bench_returns = [p[2] for p in paired]

    # --- total/excess return over the full paired window ---
    invalid_valuations = sum(r.get("comparison_eligible", True) is False for r in equity_rows)
    if n_paired >= MIN_PAIRED_DAYS_FOR_RELATIVE_RETURN and not invalid_valuations and not multi_session_intervals:
        cum_eq_total = 1.0
        cum_bench_total = 1.0
        for _, r_eq, r_bench in paired:
            cum_eq_total *= (1.0 + r_eq)
            cum_bench_total *= (1.0 + r_bench)
        equity_total_return_pct = _pct(cum_eq_total - 1.0)
        benchmark_total_return_pct = _pct(cum_bench_total - 1.0)
        excess_return_pct = round(equity_total_return_pct - benchmark_total_return_pct, 4)
        relative_return_note = f"paired_days={n_paired}"
    else:
        equity_total_return_pct = None
        benchmark_total_return_pct = None
        excess_return_pct = None
        relative_return_note = (
            f"paired_days={n_paired} (< {MIN_PAIRED_DAYS_FOR_RELATIVE_RETURN}); "
            "below floor, no comparison shown"
        )

    if invalid_valuations:
        relative_return_note = f"unavailable: {invalid_valuations} valuations have reported or unknown cashflows"

    if multi_session_intervals:
        relative_return_note = "unavailable: missing session valuations; cashflow continuity is unverified"

    # --- rolling beta over the trailing window ---
    if n_paired >= MIN_PAIRED_DAYS_FOR_BETA and not multi_session_intervals and not invalid_valuations:
        window_eq = paired_eq_returns[-ROLLING_BETA_WINDOW_DAYS:]
        window_bench = paired_bench_returns[-ROLLING_BETA_WINDOW_DAYS:]
        bench_var = _variance(window_bench)
        beta = round(_covariance(window_eq, window_bench) / bench_var, 4) if bench_var else None
        beta_note = f"window={len(window_eq)} trading days"
    else:
        beta = None
        beta_note = f"paired_days={n_paired} (< {MIN_PAIRED_DAYS_FOR_BETA}); below floor, no beta shown"

    if invalid_valuations:
        beta_note = "unavailable: reported or unknown cashflows"
    if multi_session_intervals:
        beta_note = "unavailable: missing valuations create multi-session intervals"

    # --- per-calendar-month excess return ---
    months: dict[str, list[tuple]] = {}
    for d, r_eq, r_bench in paired:
        months.setdefault(d[:7], []).append((r_eq, r_bench))
    current_month = timeutils.market_date().strftime("%Y-%m") if equity_rows else None
    monthly: dict[str, dict] = {}
    for month, rows in sorted(months.items()):
        n = len(rows)
        entry = {
            "paired_days": n,
            "is_complete_month": month != current_month,
        }
        if n >= MIN_PAIRED_DAYS_FOR_MONTHLY_LINE and not invalid_valuations and not multi_session_intervals:
            cum_eq, cum_bench = 1.0, 1.0
            for r_eq, r_bench in rows:
                cum_eq *= (1.0 + r_eq)
                cum_bench *= (1.0 + r_bench)
            entry["equity_return_pct"] = _pct(cum_eq - 1.0)
            entry["benchmark_return_pct"] = _pct(cum_bench - 1.0)
            entry["excess_return_pct"] = round(entry["equity_return_pct"] - entry["benchmark_return_pct"], 4)
            entry["note"] = f"paired_days={n}"
        else:
            entry["equity_return_pct"] = None
            entry["benchmark_return_pct"] = None
            entry["excess_return_pct"] = None
            entry["note"] = f"paired_days={n} (< {MIN_PAIRED_DAYS_FOR_MONTHLY_LINE}); below floor"
        monthly[month] = entry

    return {
        "equity_curve": equity_curve,
        "paired_trading_days": n_paired,
        "multi_session_intervals": multi_session_intervals,
        "equity_total_return_pct": equity_total_return_pct,
        "benchmark_total_return_pct": benchmark_total_return_pct,
        "excess_return_pct": excess_return_pct,
        "relative_return_note": relative_return_note,
        "rolling_beta": beta,
        "rolling_beta_note": beta_note,
        "monthly": monthly,
        "caveat": RELATIVE_PERFORMANCE_CAVEAT,
    }


def build_relative_performance_report(journal, settings, limit: int = 3650) -> dict:
    """PURE READ. Never writes, never touches gates/execution. ``limit`` is a
    generous ~10-year row cap, not a meaningful floor -- floors are applied
    inside compute_relative_performance on PAIRED days, not raw row counts."""
    from alphaos.reports.close_equity import VALUATION_METHOD

    legacy_rows = journal.query(
        "SELECT market_date, equity, equity_source, is_mock FROM equity_snapshots "
        "WHERE is_mock = 0 ORDER BY market_date ASC LIMIT ?", (limit,),
    )
    tables = journal.query("SELECT name FROM sqlite_master WHERE type='table' AND name='equity_close_snapshots'")
    equity_rows = journal.query(
        "SELECT * FROM equity_close_snapshots WHERE is_mock = 0 "
        "AND valuation_method_version = ? ORDER BY market_date ASC LIMIT ?",
        (VALUATION_METHOD, limit),
    ) if tables else []
    # The broker's documented closing-minute interval is a useful account
    # observation, but empirical daily/one-minute disagreement means that it
    # is not an independently verified match for the SPY closing valuation.
    # A clean cashflow response alone must never promote this method.
    for row in equity_rows:
        row["comparison_eligible"] = False
    benchmark_rows = journal.query(
        "SELECT bar_date, close FROM benchmark_bars WHERE symbol = 'SPY' "
        "ORDER BY bar_date ASC LIMIT ?", (limit,),
    )
    rep = compute_relative_performance(equity_rows, benchmark_rows)
    rep["provisional_account_value_curve"] = [
        {"date": r["market_date"], "equity": r["equity"],
         "valuation_at_utc": None, "source_timestamp_utc": r["source_timestamp_utc"],
         "recorded_documented_window_end_utc": r["valuation_at_utc"], "cashflow_status": r["cashflow_status"]}
        for r in equity_rows
    ]
    # Raw account values may include external flows; do not present their
    # percentage change as a verified investment-return curve.
    rep["equity_curve"] = []
    rep["legacy_snapshot_count"] = len(legacy_rows)
    rep["legacy_equity_curve"] = compute_relative_performance(legacy_rows, [])["equity_curve"]
    rep["valuation_method"] = VALUATION_METHOD
    rep["excluded_cashflow_observations"] = sum(
        r["cashflow_status"] not in {"none_reported", "internal_only"} for r in equity_rows)
    rep["excluded_timing_observations"] = len(equity_rows)
    rep["measurement_status"] = "provisional_timing_unverified" if equity_rows else "awaiting_verified_closes"
    if equity_rows:
        rep["relative_return_note"] = "unavailable: broker closing-window timing is provisional; SPY comparison is not verified"
        rep["rolling_beta_note"] = "unavailable: account and benchmark valuation timing is not verified"
    rep["caveat"] += (
        " Legacy account snapshots are preserved but excluded: their valuation times were not matched to SPY close."
        " The benchmark is SPY price return, not dividend-reinvested total return."
        " Reported or unknown cashflows interrupt the comparison; no cashflow-adjusted performance is claimed."
        " Closing-minute account values are provisional and excluded from benchmark comparisons."
        " No method currently certifies a common account/SPY closing valuation."
    )
    rep["as_of"] = timeutils.market_date().isoformat()
    rep["mode"] = settings.mode.value
    rep["equity_snapshot_count"] = len(equity_rows)
    rep["benchmark_bar_count"] = len(benchmark_rows)
    # Manual reconstructed NAV diagnostics are separate from broker equity.
    # The mere presence of a candidate never enables comparison or a gate.
    from alphaos.reports.common_valuation import CATEGORY, METHOD, SOURCE_LIMITATION
    latest = journal.one("SELECT detail_json FROM system_events WHERE category=? ORDER BY id DESC LIMIT 1", (CATEGORY,))
    rep["common_valuation"] = {"status": "not_collected", "comparison_eligible": False,
                               "source_limitations": [SOURCE_LIMITATION]}
    if latest:
        try:
            detail = json.loads(latest["detail_json"])
            if (not isinstance(detail, dict) or detail.get("method") != METHOD
                    or detail.get("status") not in {"reconstructed_candidate", "blocked"}):
                raise ValueError("unknown common valuation evidence schema")
            for key in ("blockers", "strict_comparison_blockers", "source_limitations"):
                if not isinstance(detail.get(key), list) or any(not isinstance(item, str) for item in detail[key]):
                    raise ValueError("invalid common valuation diagnostic list")
            if detail["status"] == "reconstructed_candidate":
                for key in ("reconstructed_nav", "benchmark_price"):
                    if (isinstance(detail.get(key), bool) or not isinstance(detail.get(key), (int, float))
                            or not math.isfinite(detail[key]) or detail[key] <= 0):
                        raise ValueError("invalid common valuation numeric evidence")
                if (detail.get("feed") not in {"iex", "sip"} or detail.get("adjustment") != "raw"
                        or not isinstance(detail.get("market_date"), str)):
                    raise ValueError("invalid common valuation provenance")
                date.fromisoformat(detail["market_date"])
            rep["common_valuation"] = {key: detail.get(key) for key in (
                "method", "status", "market_date", "reconstructed_nav", "benchmark_price", "feed", "adjustment",
                "valuation_cutoff_utc", "source_interval_start_utc", "source_interval_end_exclusive_utc",
                "blockers", "strict_comparison_blockers", "source_limitations", "input_sha256")}
            rep["common_valuation"]["comparison_eligible"] = False
        except (TypeError, ValueError, AttributeError, OverflowError):
            rep["common_valuation"] = {"status": "evidence_unreadable", "comparison_eligible": False}
    return rep


def render_markdown(rep: dict) -> str:
    lines = [
        f"# Relative Performance Report — {rep.get('as_of', '')}",
        f"_mode: {rep.get('mode')}_",
        "",
        f"- Equity snapshots: **{rep['equity_snapshot_count']}**  ·  "
        f"Benchmark bars: **{rep['benchmark_bar_count']}**  ·  "
        f"Paired trading days: **{rep['paired_trading_days']}**",
        "",
    ]
    if rep["excess_return_pct"] is not None:
        lines += [
            f"- Equity total return: **{rep['equity_total_return_pct']}%**",
            f"- SPY total return (same window): **{rep['benchmark_total_return_pct']}%**",
            f"- Excess return: **{rep['excess_return_pct']}%**",
        ]
    else:
        lines.append(f"- Relative return: _{rep['relative_return_note']}_")
    if rep.get("legacy_snapshot_count"):
        lines.append(f"- Legacy snapshots excluded from comparison: **{rep['legacy_snapshot_count']}** (valuation timing unverified)")
    if rep.get("provisional_account_value_curve"):
        latest = rep["provisional_account_value_curve"][-1]
        lines.append(f"- Provisional account value ({latest['date']}): **{latest['equity']:,.2f}**; "
                     "closing-window timing is unverified, so no investment return or SPY comparison is claimed.")
    common = rep.get("common_valuation", {})
    if common.get("status") == "reconstructed_candidate":
        lines.append(f"- Separate reconstructed NAV candidate ({common.get('market_date')}): "
                     f"**{common['reconstructed_nav']:,.2f}**, using {common.get('feed')} final-minute marks; "
                     "economic activity completeness is unverified, so this does not enable performance comparisons.")
    elif common.get("status") == "blocked":
        lines.append("- Common valuation capture blocked: " + "; ".join(common.get("blockers") or ["evidence incomplete"]))
    lines.append(
        f"- Rolling beta: **{rep['rolling_beta']}**" if rep["rolling_beta"] is not None
        else f"- Rolling beta: _{rep['rolling_beta_note']}_"
    )
    lines += ["", "## By month"]
    if rep["monthly"]:
        for month, m in rep["monthly"].items():
            tag = "" if m["is_complete_month"] else " (in progress)"
            if m["excess_return_pct"] is not None:
                lines.append(
                    f"- {month}{tag}: equity={m['equity_return_pct']}% "
                    f"SPY={m['benchmark_return_pct']}% excess={m['excess_return_pct']}%"
                )
            else:
                lines.append(f"- {month}{tag}: _{m['note']}_")
    else:
        lines.append("- (no paired trading days yet)")
    lines += ["", f"> ⚠️ {rep['caveat']}"]
    return "\n".join(lines)
