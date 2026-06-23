"""Cost-model calibration (Roadmap 1.5).

Compares AlphaOS's *assumed* cost/slippage/fill model against *actual* Alpaca
paper execution behaviour, so the slippage/cost assumptions can be calibrated
before routine forward-evidence collection.

Capture vs derive:
* ``build_calibration_row`` captures the EXPECTED/approval-time context at
  submission (the only data not otherwise recoverable: approval bid/ask/mid/
  spread + the modeled assumptions + submitted limit).
* ``build_calibration_report`` DERIVES the ACTUALS at report time by joining
  ``paper_orders`` / ``paper_fills`` / ``order_events`` (actual fill price, fill
  delay, status sequence), so calibration recomputes automatically as more
  Alpaca paper fills accumulate — no row ever needs updating.

Nothing here changes strategy or execution behaviour; it is read/measure only.
The recommended model is deliberately CONSERVATIVE and is labelled PRELIMINARY
until the sample is large enough.
"""

from __future__ import annotations

from typing import Optional

from alphaos.constants import ExecutionSource, TradeDirection
from alphaos.util import timeutils
from alphaos.util.ids import new_id

# Minimum filled samples before the recommended model stops being "preliminary".
MIN_CALIBRATION_SAMPLE = 20


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _mean(xs) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def _median(xs) -> Optional[float]:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    mid = n // 2
    return round((xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2), 4)


def _percentile(xs, pct) -> Optional[float]:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return round(xs[0], 4)
    k = (len(xs) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 4)


# --------------------------------------------------------------------- capture
def build_calibration_row(settings, proposal, snap: dict, order_row: dict) -> dict:
    """Build one execution_calibration row from the approval-time snapshot and the
    submitted order. Pure — the caller persists it (best-effort, after the order)."""
    bid, ask = _f(snap.get("bid")), _f(snap.get("ask"))
    last = _f(snap.get("last_price"))
    mid = (bid + ask) / 2 if (bid is not None and ask is not None) else last
    spread = (ask - bid) if (bid is not None and ask is not None) else _f(snap.get("spread"))
    spread_pct = _f(snap.get("spread_pct"))
    if spread_pct is None and spread is not None and mid:
        spread_pct = spread / mid

    qty = abs(_f(proposal.qty) or 0.0)
    expected_entry = _f(getattr(proposal, "entry", None))
    slippage_bps = float(settings.cost_slippage_bps)
    modeled_cost = (qty * expected_entry * (slippage_bps / 10_000.0)) if expected_entry else None
    src = order_row.get("execution_source")
    return {
        "calibration_id": new_id("cal"),
        "proposal_id": getattr(proposal, "proposal_id", None),
        "candidate_id": getattr(proposal, "candidate_id", None),
        "trade_id": getattr(proposal, "trade_id", None),
        "order_id": order_row.get("order_id"),
        "symbol": getattr(proposal, "symbol", None),
        "side": order_row.get("side"),
        "execution_provider": order_row.get("execution_provider"),
        "broker_managed": 1 if src == ExecutionSource.ALPACA_PAPER.value else 0,
        "expected_entry": expected_entry,
        "approval_bid": bid,
        "approval_ask": ask,
        "approval_mid": round(mid, 6) if mid is not None else None,
        "approval_spread": round(spread, 6) if spread is not None else None,
        "approval_spread_pct": round(spread_pct, 6) if spread_pct is not None else None,
        "submitted_limit_price": _f(order_row.get("limit_price")) or expected_entry,
        "modeled_slippage_bps": slippage_bps,
        "modeled_cost_estimate": round(modeled_cost, 4) if modeled_cost is not None else None,
    }


# ---------------------------------------------------------------------- report
def _realized_slippage_per_share(side: Optional[str], expected: Optional[float],
                                 fill: Optional[float]) -> Optional[float]:
    """Adverse fill cost per share (positive = worse than expected)."""
    if expected is None or fill is None:
        return None
    is_short = side in ("sell_short", "sell")
    return (expected - fill) if is_short else (fill - expected)


def _row_actuals(journal, cal: dict) -> dict:
    """Derive the actual execution outcome for one calibration row from the
    order / fills / events (no stored actuals — always recomputed)."""
    order_id = cal.get("order_id")
    order = journal.order_by_id(order_id) if order_id else None
    fills = journal.fills_for_order(order_id) if order_id else []
    events = journal.order_events_for_order(order_id) if order_id else []
    entry_fill = fills[0] if fills else None  # entry order carries only its entry fill

    expected = _f(cal.get("expected_entry"))
    fill_price = _f(entry_fill["price"]) if entry_fill else None
    slip_ps = _realized_slippage_per_share(cal.get("side"), expected, fill_price)
    slip_bps = (slip_ps / expected * 10_000.0) if (slip_ps is not None and expected) else None

    submitted_at = (order or {}).get("submitted_at")
    filled_at = (entry_fill or {}).get("filled_at") or (order or {}).get("filled_at")
    delay = None
    if submitted_at and filled_at:
        a, b = timeutils.parse_iso(submitted_at), timeutils.parse_iso(filled_at)
        if a and b:
            delay = round((b - a).total_seconds(), 3)

    return {
        "proposal_id": cal.get("proposal_id"),
        "trade_id": cal.get("trade_id"),
        "symbol": cal.get("symbol"),
        "side": cal.get("side"),
        "execution_provider": cal.get("execution_provider"),
        "broker_managed": bool(cal.get("broker_managed")),
        "expected_entry": expected,
        "approval_mid": _f(cal.get("approval_mid")),
        "approval_spread_pct": _f(cal.get("approval_spread_pct")),
        "submitted_limit_price": _f(cal.get("submitted_limit_price")),
        "actual_fill_price": fill_price,
        "filled": fill_price is not None,
        "realized_slippage_per_share": round(slip_ps, 6) if slip_ps is not None else None,
        "realized_slippage_bps": round(slip_bps, 4) if slip_bps is not None else None,
        "modeled_slippage_bps": _f(cal.get("modeled_slippage_bps")),
        "modeled_cost_estimate": _f(cal.get("modeled_cost_estimate")),
        "fill_delay_seconds": delay,
        "order_state": (order or {}).get("state"),
        "order_status_sequence": [e.get("new_state") for e in events],
    }


def build_calibration_report(journal, settings, min_sample: int = MIN_CALIBRATION_SAMPLE) -> dict:
    """Compare modeled vs actual execution across all captured calibration rows.

    Conservative + honest: the recommended slippage is the WORSE of the current
    assumption and the observed 75th percentile, and stays PRELIMINARY until at
    least ``min_sample`` filled samples exist.
    """
    cals = journal.query("SELECT * FROM execution_calibration ORDER BY id ASC")
    rows = [_row_actuals(journal, c) for c in cals]
    filled = [r for r in rows if r["filled"]]

    observed_bps = [r["realized_slippage_bps"] for r in filled if r["realized_slippage_bps"] is not None]
    modeled_bps = _f(settings.cost_slippage_bps)
    n = len(filled)
    preliminary = n < min_sample

    # Conservative recommendation: never below the current assumption; lean to the
    # adverse tail (p75) of what we've actually observed.
    if observed_bps:
        p75 = _percentile(observed_bps, 75)
        recommended = round(max(modeled_bps, p75), 4)
    else:
        recommended = modeled_bps  # no data yet -> keep the current assumption

    report = {
        "summary": {
            "calibration_rows": len(rows),
            "filled_samples": n,
            "pending_fill": len(rows) - n,
            "broker_managed": sum(1 for r in rows if r["broker_managed"]),
            "min_sample": min_sample,
            "remaining_sample_needed": max(0, min_sample - n),
            "preliminary": preliminary,
        },
        "modeled": {
            "slippage_bps": modeled_bps,
            "commission_per_share": settings.cost_commission_per_share,
        },
        "observed": {
            "mean_slippage_bps": _mean(observed_bps),
            "median_slippage_bps": _median(observed_bps),
            "p75_slippage_bps": _percentile(observed_bps, 75),
            "p95_slippage_bps": _percentile(observed_bps, 95),
            "mean_fill_delay_seconds": _mean([r["fill_delay_seconds"] for r in filled]),
        },
        "recommended_model": {
            "slippage_bps": recommended,
            "basis": "max(current_assumption, observed_p75)" if observed_bps else "no data — current assumption kept",
            "conservative": True,
            "preliminary": preliminary,
            "delta_vs_modeled_bps": (round(recommended - modeled_bps, 4) if modeled_bps is not None else None),
        },
        "per_trade": rows,
        "note": (
            f"PRELIMINARY — only {n}/{min_sample} filled paper samples. "
            "Do not retune live cost assumptions yet; collect more Alpaca paper fills."
            if preliminary else
            f"{n} filled samples (>= {min_sample}); recommendation is usable but stays conservative."
        ),
    }
    return report


def render_markdown(report: dict) -> str:
    s, m, o, rec = report["summary"], report["modeled"], report["observed"], report["recommended_model"]
    lines = [
        "# AlphaOS Cost-Model Calibration Report",
        "",
        f"_Status: **{'PRELIMINARY' if s['preliminary'] else 'usable (still conservative)'}** · "
        f"filled samples: **{s['filled_samples']}/{s['min_sample']}** · "
        f"remaining needed: **{s['remaining_sample_needed']}**_",
        "",
        "## Modeled vs observed (entry slippage)",
        f"- Modeled slippage: **{m['slippage_bps']} bps/side** · commission **{m['commission_per_share']}/share**",
        f"- Observed mean / median: **{o['mean_slippage_bps']} / {o['median_slippage_bps']} bps**",
        f"- Observed p75 / p95: **{o['p75_slippage_bps']} / {o['p95_slippage_bps']} bps**",
        f"- Mean fill delay: **{o['mean_fill_delay_seconds']} s**",
        "",
        "## Recommended (conservative)",
        f"- Slippage: **{rec['slippage_bps']} bps** ({rec['basis']}); "
        f"delta vs modeled **{rec['delta_vs_modeled_bps']} bps**",
        f"- {report['note']}",
        "",
        "## Per-trade",
    ]
    for r in report["per_trade"]:
        lines.append(
            f"- `{r['symbol']}` {r['side']} trade `{r['trade_id']}` · "
            f"exp {r['expected_entry']} → fill {r['actual_fill_price']} · "
            f"slip {r['realized_slippage_bps']} bps · delay {r['fill_delay_seconds']}s · "
            f"{'broker-managed' if r['broker_managed'] else 'sim'} · "
            f"{'FILLED' if r['filled'] else 'pending'} · seq {r['order_status_sequence']}"
        )
    return "\n".join(lines)
