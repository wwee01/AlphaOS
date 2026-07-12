"""alphaos/api/deps.py -- per-request FastAPI dependencies for the ND-1
read-only console API.

Side-effect-free startup (ND-1 plan doc §2.1) means none of these are built
at app-creation/import time: every dependency below constructs a lightweight
object FRESH PER REQUEST -- Settings (env read only), a structurally
read-only JournalStore (SQLite `mode=ro`, closed after the request), a
MarketDataClient (construction alone makes no network call -- see
alphaos/data/market_data.py's __init__; only get_snapshot() does), and a
KillSwitch (a marker-file check, no I/O at construction). This mirrors
`alphaos/reports/daily_brief.py`'s own "journal + settings, not a full
Orchestrator" pattern -- the plan doc explicitly calls out checking whether a
needed function requires the full Orchestrator (which has non-trivial
constructor work: OpenAIClient, ClaudeReviewer, NewsService, etc.) and, if
so, sourcing the data a lighter way instead. None of ND-1's four endpoints
need anything Orchestrator-only provides, so Orchestrator is never
constructed by this API.
"""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends

from alphaos.config.settings import Settings, load_settings
from alphaos.data.market_data import MarketDataClient
from alphaos.journal.journal_store import JournalStore
from alphaos.safety import KillSwitch


def get_settings() -> Settings:
    """Fresh per request. load_settings() only reads env/.env -- no
    scheduler start, no provider call, no scan (§2.1)."""
    return load_settings()


def get_journal(settings: Settings = Depends(get_settings)) -> Iterator[JournalStore]:
    """A structurally read-only JournalStore (SQLite `mode=ro` -- see
    JournalStore.__init__'s `read_only` kwarg), opened fresh per request and
    always closed after, even on error. Writes through this handle are
    impossible at the SQLite driver level, not merely absent by caller
    discipline (ND-1 plan doc §3, "Read-only DB mode")."""
    journal = JournalStore(settings.db_path, read_only=True)
    try:
        yield journal
    finally:
        journal.close()


def get_market(settings: Settings = Depends(get_settings)) -> MarketDataClient:
    """Same MarketDataClient class build_daily_brief() / assess_positions()
    / streamlit_app.main() already use -- constructed here with
    ``journal=None`` rather than the request's read-only JournalStore.

    This is the one deliberate deviation from "just pass the same journal
    everywhere", and it earns its place: MarketDataClient._warn_once() (the
    one-time-per-instance mock-mode "market data is mocked" notice) and
    AlpacaDataProvider's own freshness/incident notices both write a
    ``system_events`` row through whatever journal they are given -- a
    best-effort, log-and-continue write that is exactly what a
    *structurally* read-only API must never attempt. Both call sites already
    guard on ``if self.journal is not None`` (their own pre-existing
    "logging is optional" escape hatch -- see alphaos/data/market_data.py
    and alphaos/data/providers/alpaca_data.py), so ``journal=None`` is a
    zero-line change to those modules: it simply exercises an option they
    already support. ``get_snapshot()``/``get_snapshots()`` themselves need
    no journal at all, so live-price reads are unaffected.

    Without this, a mock-mode request would have every position's
    ``get_snapshot()`` call raise ``sqlite3.OperationalError`` (attempted
    write to a read-only DB) INSIDE assess_positions()'s own
    ``except Exception: pass`` guard -- silently downgrading every position
    to ``current_r=None``/``freshness_status="no_snapshot"`` on every single
    request (a fresh MarketDataClient, and therefore a fresh unset
    ``_warned`` flag, every time), never surfacing as an error -- and,
    because ``current_r`` feeds ``_thesis_status()``, suppressing every
    AT_RISK/ATTENTION verdict down to INTACT/HOLD as well (audit note
    2026-07-12: the degradation is verdicts and stop/target distances, not
    just the raw R number). Passing ``journal=None`` prevents the write
    attempt from ever happening, rather than relying on that broad except
    to paper over it. ``/api/v1/tonight`` still carries a bounded
    first-position version of this via build_daily_brief()'s own internal
    client -- see routes.tonight()'s docstring."""
    return MarketDataClient(settings, journal=None)


def get_kill_switch() -> KillSwitch:
    """A marker-file check (alphaos/safety.py) -- no I/O at construction,
    only when is_engaged()/reason() are actually called."""
    return KillSwitch()
