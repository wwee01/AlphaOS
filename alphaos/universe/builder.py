"""EXP-0: the shadow-tier universe builder (``alphaos universe_build`` CLI).

One-off / quarterly-refresh tool, NEVER a scheduler job: screens Alpaca's
tradable-assets list down to a liquidity band the current 20-name core book
doesn't cover, and writes a reviewed, committed, git-versioned JSON file
(``alphaos/universe/shadow_universe.json`` by default). The scanner's
shadow-tier scan pass (``alphaos/scanner/candidate_scanner.py``) reads that
COMMITTED FILE at scan time -- it never calls Alpaca's assets endpoint itself.

Pure screening logic (``build_shadow_universe``) is separated from disk I/O
(``write_universe_file``) so the screen can be tested with injected fake
providers and no filesystem writes, matching the rest of this codebase's
injectable-provider test style (``bars_provider=``/``alpaca_client=`` on the
benchmark spine, etc).

HONESTY NOTE: ETF exclusion and the recent-IPO flag are BEST-EFFORT
heuristics (Alpaca's asset/bars data has no dedicated "is ETF" or "listing
date" field) -- see ``alphaos/data/providers/alpaca_assets.py``'s module
docstring. Symbols whose bars history could not be fetched at all are
SKIPPED, not included with a fabricated zero ADV (unknown != safe, same law
as the freshness guard) -- ``build_shadow_universe``'s ``skipped`` list
records why, so a screen-run gap is visible, never silent.

HONESTY NOTE (feed-relative ADV, added after a Fable5 review of the v1
screen, 2026-07-10): ``adv_20d_usd`` is computed from bars on
``settings.market_data_feed`` (IEX on the free Alpaca tier) -- IEX volume
is roughly 2-3% of the CONSOLIDATED tape, so these dollar figures are NOT
comparable to a "$X/day on the whole market" claim; a name showing $45M
here can easily be a $1B+/day consolidated name. This is stamped into
``screen_params["feed"]`` below precisely so a later refresh on a
different feed (e.g. after upgrading to SIP) is visibly incomparable to
this version, rather than silently producing a different-shaped universe
under identical-looking band parameters.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from typing import Any, Optional

from alphaos.data.providers.alpaca_assets import make_assets_provider
from alphaos.data.providers.alpaca_bars import make_bars_provider
from alphaos.util import timeutils

# Calendar-day window fetched per symbol for BOTH the ADV computation and the
# recent-IPO proxy. ~400 covers a full trading year (~252 sessions) plus
# weekends/holidays with room to spare.
_BARS_LOOKBACK_CALENDAR_DAYS = 400
# Fewer than this many daily bars returned over the ~400-calendar-day window
# is treated as "no full year of history available" -- a recent-IPO proxy,
# not a fact (see module docstring).
_RECENT_IPO_BAR_COUNT_THRESHOLD = 200


def _adv_usd(bars: list[dict], lookback_days: int) -> Optional[float]:
    """Mean dollar volume (close * volume) over the most recent
    ``lookback_days`` bars. None if there is nothing to average (empty
    input) -- callers must treat that as unscreenable, never as a real 0."""
    window = bars[-lookback_days:] if len(bars) > lookback_days else bars
    dvs = [
        b["close"] * b["volume"] for b in window
        if b.get("close") is not None and b.get("volume") is not None
    ]
    if not dvs:
        return None
    return sum(dvs) / len(dvs)


def build_shadow_universe(settings, journal=None, assets_provider=None, bars_provider=None, now=None) -> dict:
    """Screen the tradable universe down to the shadow-tier band. Returns a
    dict: ``{"as_of_date", "screen_params", "symbols": [...], "screened":
    N, "passed": N, "skipped": [{"symbol", "reason"}, ...]}``.

    ``assets_provider``/``bars_provider`` are injectable (fakes in tests);
    production call sites (the CLI) omit them and get the real Alpaca-backed
    providers, or None in mock/offline mode (an empty screen -- there is
    nothing live to screen against; this is a live-data tool by nature, same
    as ``benchmark_capture``'s bars backfill).
    """
    assets = assets_provider if assets_provider is not None else make_assets_provider(settings, journal)
    bars = bars_provider if bars_provider is not None else make_bars_provider(settings, journal)
    as_of = timeutils.market_date(now)

    screen_params = {
        "min_adv_usd": settings.shadow_tier_min_adv_usd,
        "max_adv_usd": settings.shadow_tier_max_adv_usd,
        "min_price": settings.shadow_tier_min_price,
        "max_price": settings.shadow_tier_max_price,
        "adv_lookback_days": settings.shadow_tier_adv_lookback_days,
        "target_count": settings.shadow_tier_target_count,
        "max_count": settings.shadow_tier_max_count,
        # Fable5 review, 2026-07-10: the feed that defined the ADV band --
        # see the module HONESTY NOTE above. Makes a later refresh on a
        # different feed visibly incomparable to this version.
        "feed": settings.market_data_feed,
    }

    result: dict[str, Any] = {
        "as_of_date": as_of.isoformat(),
        "screen_params": screen_params,
        "symbols": [],
        "screened": 0,
        "passed": 0,
        "skipped": [],
    }

    if assets is None or bars is None:
        # Mock/offline mode, or credentials unavailable -- nothing live to
        # screen. Not an error: callers (the CLI) surface this plainly.
        return result

    candidates = assets.get_tradable_us_equities()
    result["screened"] = len(candidates)

    start = (as_of - timedelta(days=_BARS_LOOKBACK_CALENDAR_DAYS)).isoformat()
    end = as_of.isoformat()
    passed: list[dict] = []
    for asset in candidates:
        sym = asset.get("symbol")
        if not sym:
            continue
        if asset.get("is_probable_etf"):
            result["skipped"].append({"symbol": sym, "reason": "probable_etf"})
            continue

        symbol_bars = bars.get_daily_bars(sym, start, end, limit=_BARS_LOOKBACK_CALENDAR_DAYS)
        if not symbol_bars:
            result["skipped"].append({"symbol": sym, "reason": "no_bars_data"})
            continue

        adv = _adv_usd(symbol_bars, settings.shadow_tier_adv_lookback_days)
        price = symbol_bars[-1].get("close")
        if adv is None or price is None:
            result["skipped"].append({"symbol": sym, "reason": "insufficient_bars_data"})
            continue

        if not (settings.shadow_tier_min_adv_usd <= adv <= settings.shadow_tier_max_adv_usd):
            result["skipped"].append({"symbol": sym, "reason": "adv_out_of_band"})
            continue
        if not (settings.shadow_tier_min_price <= price <= settings.shadow_tier_max_price):
            result["skipped"].append({"symbol": sym, "reason": "price_out_of_band"})
            continue

        passed.append({
            "symbol": sym,
            "name": asset.get("name", ""),
            "exchange": asset.get("exchange"),
            "adv_20d_usd": round(adv, 2),
            "price": price,
            "recent_ipo": len(symbol_bars) < _RECENT_IPO_BAR_COUNT_THRESHOLD,
            "spac_flag": False,  # best-effort: no reliable signal available from this data source (see docstring)
        })

    # Deterministic selection when the screen overflows the hard cap: most
    # liquid first (safer within an already-liquidity-defined band), symbol
    # as a stable tiebreak -- never an arbitrary/unstable ordering.
    passed.sort(key=lambda s: (-s["adv_20d_usd"], s["symbol"]))
    if len(passed) > settings.shadow_tier_max_count:
        overflow = passed[settings.shadow_tier_max_count:]
        for s in overflow:
            result["skipped"].append({"symbol": s["symbol"], "reason": "max_count_cap"})
        passed = passed[:settings.shadow_tier_max_count]

    result["symbols"] = passed
    result["passed"] = len(passed)
    return result


def write_universe_file(payload: dict, path: str) -> dict:
    """Serialize ``payload`` (from ``build_shadow_universe``) with an
    incrementing ``version`` (read from the CURRENT file at ``path`` if one
    exists, else 1) + a full sha256 content hash, and write it to ``path``.
    Returns the written document (including ``version``/``sha256``). Does
    NOT commit to git -- that, and reviewing the symbol list, stays a
    deliberate operator action (the spec's own acceptance gate)."""
    prev_version = 0
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                prev_version = int(json.load(f).get("version", 0))
        except (json.JSONDecodeError, OSError, ValueError):
            prev_version = 0  # unreadable prior file -- treat as if absent, never crash the builder

    doc = {**payload, "version": prev_version + 1}
    canonical = json.dumps(doc, sort_keys=True, default=str)
    doc["sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
        f.write("\n")

    return doc


def load_universe_file(path: str) -> Optional[dict]:
    """Read the committed shadow-universe file, or None if it doesn't exist
    yet (a fresh checkout before the first ``universe_build`` run) or is
    unreadable -- callers (the scanner) must treat both as "no shadow tier
    configured yet", never crash the core scan over it."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
