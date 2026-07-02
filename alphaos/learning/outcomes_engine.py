"""Pure, network-free compute for the counterfactual outcome ledger.

Forward returns/R for the days AFTER a decision was recorded, and bracket
replay: whether the decision's recorded stop/target would have been hit first,
using bars observed since. This is NOT a de-novo historical backtest — it only
replays decisions AlphaOS actually made/recorded, against bars that came after
them. Every function here takes plain dicts/lists; no journal, no network, no
side effects — fully unit-testable with fixture bars.

Sign conventions match ``position_manager.py`` exactly (same "R = pnl_per_share
/ risk_per_share" definition, same long/short stop-and-target breach logic used
by the live watchdog's ``_check_exit``), so a replayed decision is directly
comparable to what AlphaOS's own exit logic would actually have done.
"""

from __future__ import annotations

from typing import Optional

from alphaos.constants import TradeDirection

# Replay/forward-window default when no explicit hold-days is known for a
# candidate (matches the swing playbook's default hold).
DEFAULT_REPLAY_WINDOW_DAYS = 5


def _is_short(direction: Optional[str]) -> bool:
    return direction == TradeDirection.SHORT.value


def signed_return_pct(reference: Optional[float], price: Optional[float],
                      direction: Optional[str]) -> Optional[float]:
    """% return in the direction taken (positive = favorable). Works without a
    stop — unlike R, a return % needs no risk level to normalize against."""
    if reference is None or price is None or not reference:
        return None
    pnl_per_share = (reference - price) if _is_short(direction) else (price - reference)
    return round(pnl_per_share / reference, 4)


def signed_r(reference: Optional[float], price: Optional[float], direction: Optional[str],
            stop: Optional[float]) -> Optional[float]:
    """R = pnl_per_share / risk_per_share (risk_per_share = |reference - stop|).
    None when there's no usable stop to normalize against."""
    if reference is None or price is None or not stop:
        return None
    risk_per_share = abs(float(reference) - float(stop))
    if not risk_per_share:
        return None
    pnl_per_share = (reference - price) if _is_short(direction) else (price - reference)
    return round(pnl_per_share / risk_per_share, 4)


def forward_window_stats(reference: Optional[float], stop: Optional[float], direction: Optional[str],
                         bars: list[dict], n_days: int) -> dict:
    """Stats for the first ``n_days`` bars: {return_pct, r, max_favorable_r,
    max_adverse_r, bars_used}. ``bars`` must already be filtered to strictly
    AFTER the decision point (callers own that filtering — this function never
    looks at dates, only order). ``bars_used < n_days`` signals the window
    isn't fully resolved yet; callers decide pending/partial/complete from
    that. Point-in-time return/R use the last available bar's close; the
    favorable/adverse extremes use each bar's high/low within the window."""
    window = [b for b in (bars or []) if b.get("close") is not None][:n_days]
    if not window:
        return {"return_pct": None, "r": None, "max_favorable_r": None,
                "max_adverse_r": None, "bars_used": 0}
    last_close = window[-1]["close"]
    return_pct = signed_return_pct(reference, last_close, direction)
    r = signed_r(reference, last_close, direction, stop)

    max_favorable_r = max_adverse_r = None
    if stop and reference is not None:
        risk_per_share = abs(float(reference) - float(stop))
        if risk_per_share:
            favorable, adverse = [], []
            for b in window:
                high, low = b.get("high"), b.get("low")
                if high is None or low is None:
                    continue
                if _is_short(direction):
                    favorable.append((reference - low) / risk_per_share)
                    adverse.append((reference - high) / risk_per_share)
                else:
                    favorable.append((high - reference) / risk_per_share)
                    adverse.append((low - reference) / risk_per_share)
            if favorable:
                max_favorable_r = round(max(favorable), 4)
                max_adverse_r = round(min(adverse), 4)

    return {
        "return_pct": return_pct, "r": r,
        "max_favorable_r": max_favorable_r, "max_adverse_r": max_adverse_r,
        "bars_used": len(window),
    }


def replay_bracket(entry: Optional[float], stop: Optional[float], target: Optional[float],
                   direction: Optional[str], bars: list[dict],
                   max_days: Optional[int] = None) -> dict:
    """Replay whether the recorded stop or target would have been hit first,
    using daily bars observed AFTER the decision (idealized fills at the exact
    level, no slippage modeled — this is a decision replay, not a backtest).
    Long/short breach convention matches ``position_manager._check_exit``
    exactly. Returns ``{result, replay_r, replay_exit_reason}``:

    * ``target_hit`` / ``stop_hit`` — replay_r = +reward:risk / exactly -1.0
    * ``neither``            — window exhausted with no level touched;
      replay_r = mark-to-market R at the last available close
    * ``ambiguous_same_bar`` — both levels fall within one day's high/low
      range; daily OHLC can't order same-day touches, so replay_r is left
      None rather than guessed
    * ``unavailable``        — no usable levels or bars
    """
    window_days = max_days if (max_days and max_days > 0) else DEFAULT_REPLAY_WINDOW_DAYS
    if entry is None or not stop or not target or not bars:
        return {"result": "unavailable", "replay_r": None, "replay_exit_reason": "no_levels_or_bars"}
    risk_per_share = abs(float(entry) - float(stop))
    if not risk_per_share:
        return {"result": "unavailable", "replay_r": None, "replay_exit_reason": "no_risk_per_share"}
    reward_per_share = abs(float(target) - float(entry))
    rr = round(reward_per_share / risk_per_share, 4)
    is_short = _is_short(direction)

    window = [b for b in bars if b.get("high") is not None and b.get("low") is not None][:window_days]
    if not window:
        return {"result": "unavailable", "replay_r": None, "replay_exit_reason": "no_usable_bars"}

    last_close = None
    for b in window:
        high, low = b["high"], b["low"]
        close = b.get("close")
        last_close = close if close is not None else last_close
        if is_short:
            stop_breach = high >= stop
            target_breach = low <= target
        else:
            stop_breach = low <= stop
            target_breach = high >= target
        if stop_breach and target_breach:
            return {"result": "ambiguous_same_bar", "replay_r": None,
                    "replay_exit_reason": "both_levels_touched_same_bar"}
        if stop_breach:
            return {"result": "stop_hit", "replay_r": -1.0, "replay_exit_reason": "stop"}
        if target_breach:
            return {"result": "target_hit", "replay_r": rr, "replay_exit_reason": "target"}

    mtm_r = signed_r(entry, last_close, direction, stop) if last_close is not None else None
    return {"result": "neither", "replay_r": mtm_r, "replay_exit_reason": "window_exhausted"}
