"""Outcome metrics: win rate, expectancy, profit factor, drawdown, etc."""

from __future__ import annotations

from alphaos.reports.metrics import compute_metrics


def _o(net, gross=None, costs=0.0, classification="profit-taking", same_day=0, hold=2.0):
    return {
        "net_pnl": net, "gross_pnl": gross if gross is not None else net,
        "costs": costs, "classification": classification,
        "is_same_day": same_day, "holding_days": hold,
    }


def test_empty_outcomes():
    m = compute_metrics([])
    assert m["trades"] == 0
    assert m["win_rate"] is None
    assert m["small_sample"] is True


def test_basic_metrics():
    outs = [
        _o(100.0, classification="profit-taking"),
        _o(-50.0, classification="risk-control", same_day=1),
        _o(200.0),
        _o(-50.0, classification="risk-control"),
    ]
    m = compute_metrics(outs)
    assert m["trades"] == 4
    assert m["wins"] == 2 and m["losses"] == 2
    assert m["win_rate"] == 0.5
    assert m["net_pnl"] == 200.0
    assert m["expectancy"] == 50.0
    # profit factor = (100+200) / (50+50) = 3.0
    assert m["profit_factor"] == 3.0
    assert m["avg_win"] == 150.0
    assert m["avg_loss"] == -50.0
    assert m["same_day_exit_rate"] == 0.25
    assert m["by_classification"]["risk-control"] == 2


def test_max_drawdown():
    # equity curve: +100, +50 (peak 150), -200 (trough -50) -> dd from 150 to -50 = -200
    outs = [_o(100.0), _o(50.0), _o(-200.0)]
    m = compute_metrics(outs)
    assert m["max_drawdown"] == -200.0


def test_costs_aggregate():
    outs = [_o(99.0, gross=100.0, costs=1.0), _o(-51.0, gross=-50.0, costs=1.0)]
    m = compute_metrics(outs)
    assert m["gross_pnl"] == 50.0
    assert m["total_costs"] == 2.0
    assert m["net_pnl"] == 48.0
