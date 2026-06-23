"""Market Interest Scanner (Roadmap 2.3) — deterministic scoring + flagging."""

from __future__ import annotations

from alphaos.config.settings import load_settings
from alphaos.scanner.interest_scanner import InterestScanner


def _s(**over):
    return load_settings(load_env_file=False, env={"ALPHAOS_MODE": "mock", **over})


def _snap(**over):
    base = {
        "symbol": "X", "last_price": 100.0, "prev_close": 98.0, "bid": 99.98, "ask": 100.02,
        "spread_pct": 0.0004, "change_pct": 0.02, "rel_volume": 1.5, "bar_open": 98.5,
        "bar_high": 100.5, "bar_low": 98.0, "dollar_volume": 5e9,
    }
    base.update(over)
    return base


def test_score_is_deterministic():
    sc = InterestScanner(_s())
    a, b = sc.score(_snap()), sc.score(_snap())
    assert a.interest_score == b.interest_score
    assert a.structure_hint == b.structure_hint
    assert 0.0 <= a.interest_score <= 1.0


def test_strong_move_scores_higher_than_quiet():
    sc = InterestScanner(_s())
    strong = sc.score(_snap(change_pct=0.08, rel_volume=3.0))
    quiet = sc.score(_snap(change_pct=0.001, rel_volume=1.0, bar_high=100.1, bar_low=99.9))
    assert strong.interest_score > quiet.interest_score


def test_breakout_structure_detected():
    sc = InterestScanner(_s())
    sig = sc.score(_snap(last_price=100.0, bar_high=100.2, change_pct=0.04, rel_volume=1.6))
    assert sig.near_day_high is True
    assert sig.structure_hint == "breakout"
    assert "breakout" in sig.shortlist_reason


def test_relative_strength_vs_index():
    sc = InterestScanner(_s())
    sig = sc.score(_snap(change_pct=0.05), spy={"change_pct": 0.01}, qqq={"change_pct": 0.02})
    assert round(sig.rel_strength_vs_spy, 4) == 0.04
    assert round(sig.rel_strength_vs_qqq, 4) == 0.03


def test_missing_data_is_flagged_not_crash():
    sc = InterestScanner(_s())
    sig = sc.score({"symbol": "Y", "last_price": 50.0, "bar_high": 50.5, "bar_low": 49.5})
    assert "no_prev_close" in sig.missing_data_flags
    assert "no_bid_ask" in sig.missing_data_flags
    assert "no_rel_volume" in sig.missing_data_flags
    assert "no_change_pct" in sig.missing_data_flags
    assert 0.0 <= sig.interest_score <= 1.0
