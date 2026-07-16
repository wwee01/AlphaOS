"""SETUP-1 (S1a): pure, read-only card-selection context + selector for the
second setup card, ``post_earnings_reaction_v1`` (Fable strategy consult,
2026-07-16 -- see docs/roadmap/edge-lab-stage1-audit.md and the SETUP-1
mechanisms specification recorded in the session transcript / decision log
once S1c lands).

Two concerns kept structurally separate, per the mechanisms spec's own
Amendment 2:

* Context loading (``build_selector_context``) -- the ONLY function in
  this module that touches the journal. Runs ONCE per scan, before any
  candidate is created, so every candidate produced by that scan shares
  the IDENTICAL frozen information set: no per-candidate N+1 cache
  queries, and no mid-scan cache refresh can split-brain a batch (a row
  written after the context is built simply isn't in it).
* Card selection (``select_card``) -- pure, journal-free and disk-free,
  takes the frozen context plus one candidate's (symbol, market_date) and
  returns an assignment. Determinism here is the whole point: the same
  context + same inputs must always produce the same assignment, which is
  what the golden-fixture hash below exists to catch a silent regression
  in.

NOT WIRED INTO PRODUCTION (S1a scope, by explicit instruction). Nothing in
this module is imported by orchestrator.py, candidate_scanner.py, or any
scheduler job -- it exists, is fully tested, and has zero runtime effect.
Wiring is S1c's job, and per the mechanisms spec's activation ordering,
S1c may not run until H-PER-1P/H-PER-1N are preregistered (S1b) -- so no
production candidate can be PER-stamped before that formal evidence
contract exists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from alphaos.cards.registry import get_default_card
from alphaos.util.market_calendar import is_trading_day, nth_trading_day_after

# Version of the SELECTION ALGORITHM/ORDERING itself -- distinct from the
# post_earnings_reaction card's own content_hash (which governs its
# PARAMETERS: K, timing rule, placebo shift -- see the card YAML, S1b). Any
# semantic change to context-loading or selection logic in this module
# forces a bump here, backed by two independent proofs: (1) the golden-
# fixture hash below, which breaks on ANY output change, forcing a
# deliberate re-pin; (2) S1b's card-registry startup check, which refuses
# to start if the card's own declared ``requires_selector`` no longer
# matches this constant.
SELECTOR_VERSION = "card_selector_v1"

PER_CARD_ID = "post_earnings_reaction"
PER_CARD_VERSION = 1
K_TRADING_DAYS = 3

# Two consecutive missed daily pulls (the job runs once per SGT calendar
# day, weekends included -- see cadence._once_daily_due -- so this is
# genuinely ~2 missed cycles, never a weekend false-positive).
CACHE_HEALTH_STALE_HOURS = 48.0


class CacheHealth:
    """Every value ``compute_cache_health`` can return. Deliberately plain
    string constants (not a StrEnum) to match this module's own
    dependency-free, pure-dataclass style -- no schema-level enum needed
    since this never gets persisted as its own column type (S1b stamps the
    STRING value directly onto ``candidates.card_assignment_status``)."""

    OK = "ok"
    REFRESH_FAILED_RECENT = "refresh_failed_recent"
    STALE = "stale"
    CACHE_EMPTY = "cache_empty"
    UNKNOWN = "unknown"


class EarningsTimingClass:
    BMO = "bmo"
    AMC = "amc"
    UNKNOWN = "unknown"


# --------------------------------------------------------------- cache health
def _parse_result_summary(row: dict) -> Optional[dict]:
    """Audit-fixup (correctness MED): a JSON payload that parses but whose
    TOP-LEVEL value isn't a dict (e.g. a bare list/string/number -- never
    expected from the real writer, but not impossible from a hand-edited
    or corrupted row) must degrade EXACTLY like invalid JSON, not escape
    as an uncaught AttributeError on the next ``.get()`` call. Before this
    fix, that escape was caught by compute_cache_health's own broad
    except-Exception and mapped to UNKNOWN -- a state this module reserves
    for a failure of the health check's OWN read, not one bad row's
    payload (see compute_cache_health's docstring). A non-dict payload now
    reads as unusable the same way invalid JSON does, so it degrades to
    STALE/REFRESH_FAILED_RECENT depending on whether another usable run
    exists in-window, never masking one behind UNKNOWN."""
    raw = row.get("result_summary_json")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _run_is_usable(row: dict) -> bool:
    """A single ``job_runs`` row for ``earnings_calendar_pull`` counts as a
    usable refresh iff: ``status='completed'`` AND its own recorded
    ``n_fetched > 0`` AND its own recorded ``warnings`` list is empty.

    All three are checked, not just ``n_fetched``: a non-empty ``warnings``
    list means some individual row FAILED TO WRITE after a successful
    fetch (see ``earnings_calendar_service._write_entry``'s own per-row
    try/except) -- a signal ``n_fetched`` alone can't see. ``status`` is
    checked too though it does the least real-world work of the three:
    ``run_earnings_calendar_pull_job``'s own thin wrapper always reports
    ``status='completed'`` even when the fetch itself failed entirely
    (its docstring: "no gating needed... never raises" -- internal
    failures degrade ``n_fetched``/``warnings`` instead); ``status='failed'``
    at the ``job_runs`` level is only reachable via a JobRunner-level
    catastrophic failure (an exception escaping the thin wrapper itself),
    kept here as defense against that rarer path, not the expected one.

    KNOWN RESIDUAL GAP (documented, not closed by this check): a malformed
    CSV row is silently dropped inside ``alpha_vantage_client._parse_csv``
    (a bare ``continue``, zero counters, zero warnings) -- a partial
    upstream format break could shrink ``n_fetched`` with no observable
    signal distinguishing "a quiet earnings day" from "the feed partially
    failed to parse." No stored ground-truth expected-row-count exists
    anywhere to compare against. Closing this is a separate, small,
    unrelated fix to ``earnings_calendar_service.py`` (an
    ``n_skipped_malformed`` counter), out of SETUP-1's scope.
    """
    if row.get("status") != "completed":
        return False
    summary = _parse_result_summary(row)
    if summary is None:
        return False
    result = summary.get("earnings_calendar_result")
    if not isinstance(result, dict):
        return False
    # Audit-fixup (correctness LOW): validate n_fetched as a real positive
    # integer rather than trusting truthiness -- a bare `if not
    # result.get("n_fetched")` would read a malformed non-numeric value
    # (e.g. a stray "0" string, or "abc") as usable, since both are
    # truthy. The real writer only ever emits an int (see
    # earnings_calendar_service.update_earnings_calendar), so this is
    # defense against a hand-edited or corrupted row, not the expected
    # path -- but it costs nothing to make explicit.
    n_fetched = result.get("n_fetched")
    if n_fetched is None:
        return False
    try:
        if int(n_fetched) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    if result.get("warnings"):
        return False
    return True


def compute_cache_health(journal, as_of_utc: str) -> str:
    """One health read for the WHOLE scan -- called once by
    ``build_selector_context``, never per-candidate. Never raises: any
    read/parse error becomes ``UNKNOWN`` rather than propagating (a health
    check must not be able to crash a scan).

    ``CACHE_EMPTY`` is checked first and overrides every other state (an
    empty cache is a bootstrap condition, not a refresh-quality one).
    Otherwise: the latest FINISHED ``earnings_calendar_pull`` run within
    the last ``CACHE_HEALTH_STALE_HOURS`` decides ``OK`` vs
    ``REFRESH_FAILED_RECENT`` (some run in that same window WAS usable,
    just not the latest one) vs ``STALE`` (nothing usable in the window at
    all, whether from silence or repeated failure -- the distinction
    doesn't change how the selector behaves, only the diagnostic label)."""
    try:
        has_cache = journal.scalar("SELECT 1 FROM earnings_calendar_cache LIMIT 1")
        if not has_cache:
            return CacheHealth.CACHE_EMPTY

        window_start = (
            datetime.fromisoformat(as_of_utc) - timedelta(hours=CACHE_HEALTH_STALE_HOURS)
        ).isoformat()
        rows = journal.query(
            "SELECT id, status, result_summary_json FROM job_runs "
            "WHERE job_type = 'earnings_calendar_pull' AND finished_at_utc IS NOT NULL "
            "AND finished_at_utc >= ? AND finished_at_utc < ? "
            # Audit-fixup (correctness LOW): secondary sort by id DESC --
            # two runs can share the exact same finished_at_utc (coarse-
            # precision fixtures/backfills; live runs carry microsecond
            # timestamps so this is practically unreachable there), and
            # without a tiebreak SQLite's order among ties is unspecified,
            # making "the latest run" nondeterministic. id DESC matches
            # this module's own higher-id-is-later convention
            # (_resolve_current_belief's supersession rule).
            "ORDER BY finished_at_utc DESC, id DESC",
            (window_start, as_of_utc),
        )
        if not rows:
            return CacheHealth.STALE
        if _run_is_usable(rows[0]):
            return CacheHealth.OK
        if any(_run_is_usable(r) for r in rows[1:]):
            return CacheHealth.REFRESH_FAILED_RECENT
        return CacheHealth.STALE
    except Exception:  # noqa: BLE001 -- a health check must never crash a scan
        return CacheHealth.UNKNOWN


# ------------------------------------------------------------- frozen context
@dataclass(frozen=True)
class SelectorContext:
    """Everything ``select_card`` needs, frozen once per scan. Nothing on
    this object is ever mutated after construction -- a new scan builds a
    new context; this one is simply discarded, never updated in place."""

    assignment_as_of_utc: str
    cache_health: str
    default_card: dict
    # symbol -> list of CURRENT-BELIEF cache rows for that symbol (almost
    # always 0 or 1; 2+ only in the pathological case of two distinct
    # fiscal-quarter events both landing in-window at once).
    current_belief_by_symbol: dict = field(default_factory=dict)
    selector_version: str = SELECTOR_VERSION


def _event_key(row: dict) -> tuple:
    """Groups a reschedule chain together: same (symbol, fiscal_date_ending)
    when fiscal is known, else a singleton keyed on (symbol, report_date)
    -- NULL-fiscal rows have no inferable reschedule chain, so none is
    invented."""
    fiscal = row.get("fiscal_date_ending")
    if fiscal:
        return (row["symbol"], "fiscal", fiscal)
    return (row["symbol"], "report_date", row["report_date"])


def _resolve_current_belief(rows: list[dict]) -> list[dict]:
    """Group rows by event (see ``_event_key``); within a group, current
    belief = the row with the latest ``created_at_utc`` (tie -> highest
    ``id``). Returns one row per group.

    Deliberately does NO ``report_date`` filtering -- that must happen
    strictly AFTER this resolution (mechanisms spec Amendment/correction
    #1). Filtering by date FIRST can resurrect an obsolete row: if a newer
    pre-scan row reschedules an event's report_date OUTSIDE a naive date
    window, filtering first would exclude the very row that should have
    won supersession, leaving the older (wrong) row looking like the
    current belief and falsely opening a PER window on an event that no
    longer exists at that date."""
    groups: dict[tuple, dict] = {}
    for r in rows:
        key = _event_key(r)
        current = groups.get(key)
        if current is None or (r["created_at_utc"], r["id"]) > (current["created_at_utc"], current["id"]):
            groups[key] = r
    return list(groups.values())


def build_selector_context(journal, assignment_as_of_utc: str, universe_symbols) -> SelectorContext:
    """THE ONLY function in this module that touches the journal (or, via
    ``get_default_card``, the card YAML on disk). Called ONCE per scan,
    before any candidate is created.

    ``universe_symbols`` scopes the cache read to the current scan's
    symbols (core + shadow union) -- a PERFORMANCE bound only, never a
    correctness dependency: the read is otherwise unbounded by calendar
    age (no fixed lookback-days pruning of cache rows). The full-market
    cache is small enough (a handful of rows per symbol per quarter, over
    ~520 symbols) that correctness should not rest on an assumption about
    the vendor's own forward-fetch horizon.

    Loads rows with ``created_at_utc`` strictly BEFORE
    ``assignment_as_of_utc`` (never ``<=`` -- a cache row stamped at the
    exact same instant the scan batch was minted is excluded, removing any
    same-timestamp ambiguity; candidate inserts always postdate batch
    minting in the real pipeline, so this can never exclude information
    that genuinely existed first)."""
    cache_health = compute_cache_health(journal, assignment_as_of_utc)
    default_card = get_default_card()

    symbols = list(universe_symbols or [])
    if not symbols:
        return SelectorContext(assignment_as_of_utc, cache_health, default_card, {})

    placeholders = ",".join("?" for _ in symbols)
    rows = journal.query(
        f"SELECT id, symbol, report_date, fiscal_date_ending, timing, created_at_utc "
        f"FROM earnings_calendar_cache WHERE symbol IN ({placeholders}) AND created_at_utc < ?",
        (*symbols, assignment_as_of_utc),
    )
    current_belief = _resolve_current_belief(rows)
    by_symbol: dict[str, list[dict]] = {}
    for r in current_belief:
        by_symbol.setdefault(r["symbol"], []).append(r)
    return SelectorContext(assignment_as_of_utc, cache_health, default_card, by_symbol)


# ------------------------------------------------------- timing/window rules
def _timing_class(cache_timing: Optional[str]) -> str:
    """Alpha Vantage's own literal ``timeOfTheDay`` values (confirmed
    against the real CSV shape in ``alpha_vantage_client.py``/its tests):
    ``'pre-market'`` -> BMO, ``'post-market'`` -> AMC. NULL or any other
    literal -> UNKNOWN, which is treated identically to AMC below (the
    conservative choice: an unknown-timing release can never be assumed
    to have preceded the open)."""
    if cache_timing == "pre-market":
        return EarningsTimingClass.BMO
    if cache_timing == "post-market":
        return EarningsTimingClass.AMC
    return EarningsTimingClass.UNKNOWN


def _window_open_date(report_date: date, timing_class: str) -> date:
    """BMO -> the report date itself (a pre-market release precedes that
    day's 09:30 ET open, hence precedes every one of AlphaOS's own scan
    windows that day). AMC and UNKNOWN -> the NEXT trading day (an
    after-close release -- or an unknown-timing release, conservatively
    treated the same way -- cannot have been known before that day's
    16:00 ET close, so no scan on the report date itself can see it).

    If a BMO report_date happens to land on a non-trading day (bad vendor
    data -- a report can't genuinely occur on a day the market is shut),
    roll forward to the next real trading day rather than opening a
    window on a day with no scan windows at all."""
    if timing_class == EarningsTimingClass.BMO:
        return report_date if is_trading_day(report_date) else nth_trading_day_after(report_date, 1)
    return nth_trading_day_after(report_date, 1)


def _window_dates(open_date: date) -> tuple:
    """K=3 trading days, INCLUSIVE of both ends: the open date plus the
    next 2 trading days. Weekends/holidays are skipped by construction of
    ``nth_trading_day_after``'s own trading-day arithmetic."""
    return (open_date, nth_trading_day_after(open_date, 1), nth_trading_day_after(open_date, 2))


def eligible_events_for_symbol(context: SelectorContext, symbol: str, market_date: date) -> list[dict]:
    """Every current-belief event for ``symbol`` whose PER window contains
    ``market_date``, each tagged with its resolved timing class and window
    for ``select_card``'s own tie-break. Pure -- reads only the frozen
    context, touches nothing else."""
    out = []
    for row in context.current_belief_by_symbol.get(symbol, []):
        report_date = date.fromisoformat(row["report_date"])
        tclass = _timing_class(row.get("timing"))
        open_date = _window_open_date(report_date, tclass)
        window = _window_dates(open_date)
        if market_date in window:
            out.append({**row, "_timing_class": tclass, "_window_open": open_date, "_window": window})
    return out


# --------------------------------------------------------------- selection
def select_card(context: SelectorContext, symbol: str, market_date: date) -> dict:
    """Pure. Returns ``{"card_id", "card_version", "card_assignment_ref",
    "card_assignment_status", "card_selector_version"}``. Never touches the
    journal or disk -- takes the already-frozen context.

    Ordered, first-match-wins: ``post_earnings_reaction_v1`` ahead of the
    default card. Exactly one card per candidate, always.

    Degraded/empty/unknown cache health ALWAYS assigns the default card
    (eligibility could not be evaluated at all), with
    ``card_assignment_status`` recording why. A HEALTHY cache that
    genuinely finds no eligible event also assigns the default card, but
    with status ``'ok'`` -- evaluated, and not eligible. The two states
    are never conflated: status ``'ok'`` + default card means "checked,
    doesn't apply"; any other status means "couldn't check."""
    if context.cache_health != CacheHealth.OK:
        return {
            "card_id": context.default_card["card_id"],
            "card_version": context.default_card["version"],
            "card_assignment_ref": None,
            "card_assignment_status": context.cache_health,
            "card_selector_version": SELECTOR_VERSION,
        }

    events = eligible_events_for_symbol(context, symbol, market_date)
    if not events:
        return {
            "card_id": context.default_card["card_id"],
            "card_version": context.default_card["version"],
            "card_assignment_ref": None,
            "card_assignment_status": CacheHealth.OK,
            "card_selector_version": SELECTOR_VERSION,
        }

    # Pathological multi-event overlap (two distinct fiscal quarters both
    # landing in-window at once): the event with the most recent
    # report_date wins, deterministically -- never an error, never a
    # silent pick of "whichever the group-by happened to order first."
    # Audit-fixup (correctness LOW): secondary key on id -- two events CAN
    # share the exact same report_date (same calendar day, different
    # fiscal quarters), and without a tiebreak the sort silently fell back
    # to Python's stable-sort input order, which traced back to an
    # ORDER-BY-less SQL read (build_selector_context) rather than any
    # deliberate rule. id DESC (higher id wins) matches
    # _resolve_current_belief's own higher-id-is-later-inserted convention.
    events.sort(key=lambda e: (e["report_date"], e["id"]), reverse=True)
    chosen = events[0]
    return {
        "card_id": PER_CARD_ID,
        "card_version": PER_CARD_VERSION,
        "card_assignment_ref": chosen["id"],
        "card_assignment_status": CacheHealth.OK,
        "card_selector_version": SELECTOR_VERSION,
    }
