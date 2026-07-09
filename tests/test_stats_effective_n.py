"""PORT-1: alphaos.stats.effective_n -- deterministic, direct-construction
fixtures only (no wall-clock, no RNG), per this codebase's own §H.1 test
discipline.
"""

from __future__ import annotations

from alphaos.stats.effective_n import MIN_TRUSTWORTHY_CLUSTERS, effective_n


def _row(symbol, d, holding_days=None):
    r = {"symbol": symbol, "decision_date": d}
    if holding_days is not None:
        r["max_holding_days"] = holding_days
    return r


# ------------------------------------------------------------------- basics
def test_empty_input_returns_zero_effective_n():
    out = effective_n([])
    assert out == {
        "effective_n": 0, "n_raw": 0, "n_deduped": 0, "span_days": None,
        "trustworthy": False, "clusters": [],
    }


def test_rows_missing_symbol_or_date_are_excluded_not_fabricated():
    rows = [
        {"symbol": None, "decision_date": "2026-01-01"},
        {"symbol": "AAPL", "decision_date": None},
        {"symbol": "AAPL", "decision_date": "not-a-date"},
        _row("AAPL", "2026-01-01"),
    ]
    out = effective_n(rows)
    assert out["n_raw"] == 1
    assert out["effective_n"] == 1


# ------------------------------------------------------ the spec's own example
def test_ten_rows_three_symbol_days_yields_effective_n_three():
    """PORT-1 spec's own acceptance example: 10 rows, 3 symbol-days -> n_eff=3.
    No max_holding_days -> same-day-only clustering, so distinct (symbol,date)
    pairs are simply 3 singleton clusters once deduped."""
    rows = (
        [_row("AAPL", "2026-01-05")] * 4
        + [_row("MSFT", "2026-01-05")] * 3
        + [_row("AAPL", "2026-01-06")] * 3
    )
    out = effective_n(rows)
    assert out["n_raw"] == 10
    assert out["n_deduped"] == 3
    assert out["effective_n"] == 3
    assert out["span_days"] == 1.0


# --------------------------------------------------------------- dedup rules
def test_dedup_keeps_first_occurrence_per_symbol_date():
    rows = [
        {"symbol": "AAPL", "decision_date": "2026-01-01", "tag": "first"},
        {"symbol": "AAPL", "decision_date": "2026-01-01", "tag": "second"},
    ]
    out = effective_n(rows)
    assert out["n_deduped"] == 1
    assert out["clusters"][0][0]["tag"] == "first"


# ------------------------------------------------------------ overlap window
def test_overlapping_holding_windows_on_same_symbol_cluster_together():
    # Day 1, held 5 days -> window [1, 6]. Day 5 starts inside that window ->
    # same cluster (share realized market moves during the overlap).
    rows = [
        _row("AAPL", "2026-01-01", holding_days=5),
        _row("AAPL", "2026-01-05", holding_days=1),
    ]
    out = effective_n(rows)
    assert out["effective_n"] == 1
    assert len(out["clusters"][0]) == 2


def test_non_overlapping_holding_windows_on_same_symbol_stay_separate():
    # Day 1, held 1 day -> window [1, 2]. Day 10 starts well after -> no overlap.
    rows = [
        _row("AAPL", "2026-01-01", holding_days=1),
        _row("AAPL", "2026-01-10", holding_days=1),
    ]
    out = effective_n(rows)
    assert out["effective_n"] == 2


def test_transitive_chain_overlap_merges_into_one_cluster():
    """A overlaps B, B overlaps C, A does NOT overlap C directly -- still one
    connected component (interval-graph connected components, not naive
    pairwise-only grouping)."""
    rows = [
        _row("AAPL", "2026-01-01", holding_days=3),   # window [01, 04]
        _row("AAPL", "2026-01-04", holding_days=3),   # window [04, 07] -- overlaps first at day 4
        _row("AAPL", "2026-01-07", holding_days=0),   # window [07, 07] -- overlaps second at day 7
    ]
    out = effective_n(rows)
    assert out["effective_n"] == 1
    assert len(out["clusters"][0]) == 3


def test_different_symbols_never_cluster_even_with_overlapping_dates():
    rows = [
        _row("AAPL", "2026-01-01", holding_days=10),
        _row("MSFT", "2026-01-01", holding_days=10),
    ]
    out = effective_n(rows)
    assert out["effective_n"] == 2


# -------------------------------------------------------- graceful degradation
def test_missing_max_holding_days_degrades_to_same_day_only_clustering():
    """attribution_records doesn't carry max_holding_days -- a row missing it
    must NOT be fabricated into an overlapping window; two different-day
    observations on the same symbol stay separate clusters."""
    rows = [_row("AAPL", "2026-01-01"), _row("AAPL", "2026-01-02")]
    out = effective_n(rows)
    assert out["effective_n"] == 2


def test_negative_or_unparseable_holding_days_treated_as_zero():
    rows = [_row("AAPL", "2026-01-01", holding_days=-5), _row("AAPL", "2026-01-02", holding_days="garbage")]
    out = effective_n(rows)
    assert out["effective_n"] == 2


# ------------------------------------------------------------ trustworthy floor
def test_trustworthy_floor_boundary():
    just_under = [_row(f"SYM{i}", "2026-01-01") for i in range(MIN_TRUSTWORTHY_CLUSTERS - 1)]
    at_floor = [_row(f"SYM{i}", "2026-01-01") for i in range(MIN_TRUSTWORTHY_CLUSTERS)]
    assert effective_n(just_under)["trustworthy"] is False
    assert effective_n(at_floor)["trustworthy"] is True


# ------------------------------------------------------------- custom key names
def test_custom_key_names():
    rows = [{"sym": "AAPL", "dt": "2026-01-01", "hold": 2}]
    out = effective_n(rows, symbol_key="sym", date_key="dt", holding_days_key="hold")
    assert out["effective_n"] == 1
