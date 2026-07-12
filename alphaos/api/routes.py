"""alphaos/api/routes.py -- ND-1 read-only endpoints.

Every handler is a thin wrapper: it gathers plain values via the exact
functions/queries `alphaos/dashboard/streamlit_app.py` already uses for the
same view, then returns them as JSON. No business logic is re-derived here
(docs/roadmap/console-migration-nd.md §1: "the frontend computes nothing
business-critical, ever; it formats and displays" -- the same discipline
applies to this API layer, which computes nothing beyond trivial JSON-
shaping: a sum-excluding-None and a length, both directly mirroring
render_annunciator()'s own inline computation over the SAME assess_positions()
list).

Unknown-never-zero (§2.5): every "None" value below is a genuine "cannot be
measured right now", passed straight through as JSON `null` -- never
coerced to 0 or an empty-but-truthy value. The frontend is responsible for
rendering `null` as "n/a"/"unknown", never silently as 0 (see
console/src/format.js).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from alphaos.api.deps import get_journal, get_kill_switch, get_market, get_settings
from alphaos.config.settings import Settings
from alphaos.dashboard.streamlit_app import AUTONOMY_LEVEL_LABEL, _heartbeat_age_seconds
from alphaos.data.market_data import MarketDataClient
from alphaos.journal.journal_store import JournalStore
from alphaos.reports.daily_brief import build_daily_brief
from alphaos.reports.position_health import assess_positions
from alphaos.safety import KillSwitch
from alphaos.util import timeutils

router = APIRouter(prefix="/api/v1")


def _as_of() -> str:
    return timeutils.to_iso(timeutils.now_utc())


@router.get("/health")
def health(settings: Settings = Depends(get_settings)) -> dict:
    return {"status": "ok", "db_path": settings.db_path, "as_of": _as_of()}


@router.get("/annunciator")
def annunciator(
    settings: Settings = Depends(get_settings),
    journal: JournalStore = Depends(get_journal),
    market: MarketDataClient = Depends(get_market),
    kill_switch: KillSwitch = Depends(get_kill_switch),
) -> dict:
    """The annunciator strip's exact fields (ND-1 plan doc §4): mode,
    kill-switch state, autonomy level line, heartbeat, open-position count +
    total open R, approvals pending count.

    Sourced from streamlit_app.render_annunciator()'s own call sites, read
    directly rather than re-derived:
    * `_heartbeat_age_seconds()` / `AUTONOMY_LEVEL_LABEL` -- imported
      verbatim from streamlit_app.py, the same module-level function/
      constant render_annunciator() itself uses.
    * `positions_health` -- the same `assess_positions()` call
      streamlit_app.main() makes once per render and passes into both
      render_annunciator() and tab_positions_health(); this endpoint makes
      its own call (a separate HTTP request has no page-level render to
      share it with), matching the same accepted "double-compute" pattern
      daily_brief.py's own module docstring already documents for
      assess_positions() vs. build_daily_brief().
    * `total_open_r` / `unmeasurable_positions` -- the identical
      sum-excluding-None-values computation render_annunciator() performs
      inline over that same list (unknown-never-zero: `total_open_r` is
      `null`, never a fabricated `0`, when every open position's R is
      currently unmeasurable).
    * `approvals_pending_count` -- `len(journal.open_proposals())`, the
      exact expression render_annunciator() uses.
    """
    positions_health = assess_positions(journal, settings, market)
    r_values = [p["current_r"] for p in positions_health if p.get("current_r") is not None]
    total_open_r = round(sum(r_values), 2) if r_values else None
    unmeasurable_positions = len(positions_health) - len(r_values)
    return {
        "mode": settings.mode.value,
        "autonomy_level_label": AUTONOMY_LEVEL_LABEL,
        "kill_switch_engaged": kill_switch.is_engaged(),
        "kill_switch_reason": kill_switch.reason(),
        "heartbeat_age_seconds": _heartbeat_age_seconds(journal),
        "open_position_count": len(positions_health),
        "total_open_r": total_open_r,
        "unmeasurable_positions": unmeasurable_positions,
        "approvals_pending_count": len(journal.open_proposals()),
        "as_of": _as_of(),
    }


@router.get("/tonight")
def tonight(
    settings: Settings = Depends(get_settings),
    journal: JournalStore = Depends(get_journal),
    kill_switch: KillSwitch = Depends(get_kill_switch),
) -> dict:
    """`build_daily_brief(journal, settings, KillSwitch())`'s dict, verbatim
    -- the exact same function the Tonight tab (streamlit_app.tab_tonight)
    and the `alphaos brief` CLI / scheduler digest alert already call.
    Every key/value is unchanged; only a top-level `as_of` is added.

    Known ND-1 characteristic (see get_market()'s docstring in
    alphaos/api/deps.py for the full mechanism): unlike `/api/v1/positions`,
    this endpoint hands `build_daily_brief()` the request's real read-only
    journal (as it must, for its many other journal reads), and that
    function constructs its OWN internal MarketDataClient from it -- so in
    MOCK MODE, `brief['positions_health']`'s first position may show
    `current_r=None` even when `/api/v1/positions` reports a real value for
    the identical position. Not a bug in the sense of incorrect code; a
    documented, tested (tests/test_api_console.py) consequence of reusing
    daily_brief.py verbatim against a structurally read-only DB. Flagged for
    a proper fix in ND-2."""
    brief = build_daily_brief(journal, settings, kill_switch)
    return {**brief, "as_of": _as_of()}


@router.get("/positions")
def positions(
    settings: Settings = Depends(get_settings),
    journal: JournalStore = Depends(get_journal),
    market: MarketDataClient = Depends(get_market),
) -> dict:
    """`assess_positions()`'s list, verbatim -- the exact function
    streamlit_app.tab_positions_health() renders from (verdicts, R fields,
    symbol, days held, etc.). No reshaping."""
    return {"positions": assess_positions(journal, settings, market), "as_of": _as_of()}
