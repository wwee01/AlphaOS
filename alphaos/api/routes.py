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

from types import SimpleNamespace
from typing import Optional, cast

from fastapi import APIRouter, Depends

from alphaos.ai.labeller_health import evaluate_failsafe_health
from alphaos.api.deps import get_journal, get_kill_switch, get_market, get_settings
from alphaos.config.settings import Settings
from alphaos.constants import PLAYBOOK_V1
from alphaos.dashboard.streamlit_app import AUTONOMY_LEVEL_LABEL, _heartbeat_age_seconds
from alphaos.data.market_data import MarketDataClient
from alphaos.execution.protection_watchdog import status_report as protection_status_report
from alphaos.hypotheses import list_drafts
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.reports.attribution import build_attribution_report
from alphaos.reports.daily_brief import build_daily_brief
from alphaos.reports.governance_report import build_governance_report
from alphaos.reports.hypothesis_report import build_hypothesis_report
from alphaos.reports.journal_feed import build_journal_feed
from alphaos.reports.metrics import compute_metrics
from alphaos.reports.position_health import assess_positions
from alphaos.reports.tqs_report import build_tqs_report
from alphaos.reports.trade_packet import assemble_trade_packet
from alphaos.safety import KillSwitch
from alphaos.scanner.candidate_scanner import (
    SHADOW_INTEREST_SCORE_VERSION_CURRENT, SHADOW_V1_CHANGE_SCALE, SHADOW_V1_DAY_RANGE_MIN,
    SHADOW_V1_MOMENTUM_CHANGE_CAP, SHADOW_V1_MOMENTUM_RELVOL_CAP, SHADOW_V1_REL_VOL_SCALE,
)
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
    market: MarketDataClient = Depends(get_market),
    kill_switch: KillSwitch = Depends(get_kill_switch),
) -> dict:
    """`build_daily_brief(journal, settings, KillSwitch())`'s dict, verbatim
    -- the exact same function the Tonight tab (streamlit_app.tab_tonight)
    and the `alphaos brief` CLI / scheduler digest alert already call.
    Every key/value is unchanged; only a top-level `as_of` is added.

    ND-2 fix (previously a documented, tested ND-1 characteristic -- see
    git history / tests/test_api_console.py for the prior mechanism): this
    endpoint now passes build_daily_brief() the same `journal=None`-built
    `market` dependency `/api/v1/positions` already uses (get_market() in
    alphaos/api/deps.py), instead of letting that function construct its own
    MarketDataClient from the request's read-only journal. Previously, in
    MOCK MODE, that internal construction meant the FIRST open position's
    snapshot fetch aborted (the client's one-time "market data is mocked"
    notice attempted a write through the read-only journal), degrading that
    position's current_r/verdict relative to `/api/v1/positions` for the
    same DB state. Passing a pre-built, journal-less client removes the
    write attempt entirely -- both endpoints now report identical current_r
    for the same position, verified by
    tests/test_api_console.py::test_tonight_matches_build_daily_brief_field_for_field
    and test_tonight_positions_health_current_r_matches_positions_endpoint."""
    brief = build_daily_brief(journal, settings, kill_switch, market=market)
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


# ============================================================== ND-2 routes
#
# Same discipline as ND-1 above: every handler wraps the exact function/query
# streamlit_app.py's corresponding tab already calls -- see each handler's
# docstring for its Streamlit call site. VIEW-ONLY (docs/roadmap/
# console-migration-nd.md §4 ND-2): the Approvals endpoint below returns the
# same proposal-queue data streamlit_app.tab_approval_center() renders, and
# this module (routes.py) still defines no POST/approve/reject route of its
# own -- as of ND-4, that write surface exists in a SEPARATE module
# (alphaos/api/write_routes.py's PIN-gated `/api/v1/actions/approve` and
# `.../reject`), keeping this file's routes uniformly read-only rather than
# mixing a write route in among them.


@router.get("/approvals")
def approvals(journal: JournalStore = Depends(get_journal)) -> dict:
    """The open-proposal queue, verbatim -- the exact data
    streamlit_app.tab_approval_center() renders (TTL fields, TQS shadow
    score, exit plan, margin flag), enriched exactly the way
    `Orchestrator.list_open_proposals()` already does (TTL seconds-remaining
    computed fresh per call, freshness/TQS looked up per proposal). This
    route itself remains READ-ONLY (still `mode=ro`, still no PIN/nonce) --
    as of ND-4, `POST /api/v1/actions/approve` and `.../reject`
    (alphaos/api/write_routes.py) exist as separate, PIN-gated routes that
    act on the same rows this endpoint lists; this handler only ever reads.

    `list_open_proposals()` is defined on `Orchestrator`, but reading its
    body (alphaos/orchestrator.py) shows it touches only `self.journal` --
    no AI client, no market data, no order manager. Rather than construct a
    full `Orchestrator` (its __init__ builds OpenAIClient, ClaudeReviewer,
    NewsService, OrderManager, etc. -- exactly the constructor cost ND-1's
    `deps.py` module docstring already flags as a reason to avoid it), this
    calls the unbound method against a minimal `SimpleNamespace(journal=...)`
    stand-in that provides the one attribute it actually reads. This reuses
    the exact function body (same TTL math, same TQS join, same sort-free
    ordering) with zero logic re-derived, at zero Orchestrator constructor
    cost -- the same "avoid the heavy object, verify what's really needed
    first" reasoning `get_market()` documents in deps.py, applied to a
    method instead of a class swap. `cast()` below is a type-only lie for
    mypy (the shim is not really an `Orchestrator`) -- verified true at
    runtime because `list_open_proposals`'s body is read above, not assumed."""
    shim = cast(Orchestrator, SimpleNamespace(journal=journal))
    proposals = Orchestrator.list_open_proposals(shim)
    return {"proposals": proposals, "as_of": _as_of()}


def _candidate_id_str(row: dict) -> str:
    """`row["candidate_id"]` as `str`, or `""` if absent/non-string --
    `""` never collides with a real candidate_id (`new_id("cand")` always
    mints a non-empty prefixed id), so `hindsight.get(_candidate_id_str(row))`
    safely misses (`None`) for a row with no candidate_id, matching
    streamlit_app.py's own `hindsight.get(x.get("candidate_id"))` for that
    same case -- typed narrowly here only because `JournalStore.
    attribution_by_candidate()` (unlike streamlit_app.py) is in mypy's fully
    checked set."""
    value = row.get("candidate_id")
    return value if isinstance(value, str) else ""


@router.get("/decisions")
def decisions(journal: JournalStore = Depends(get_journal)) -> dict:
    """The decision funnel (candidates -> proposed/watch -> rejected/blocked
    -> filled) + the trade ledger -- the same journal reads
    streamlit_app.tab_candidate_flow() (labels summary, proposed/watch/
    rejected/blocked sections) and tab_open_trades()/tab_closed_trades()
    already use. Scope note: tab_candidate_flow() also renders several
    separate research-layer summaries (catalyst enrichment, last30days,
    narrative polarity, decision-adjustment, user-override detail) that are
    a distinct concern from "the decision funnel and the trade ledger" this
    endpoint's name promises -- each candidate row already carries its own
    catalyst_status/last30days_status/sentiment_label/etc. fields verbatim
    (unchanged from the journal row), so that context isn't lost, just not
    separately re-aggregated here. Rejected/blocked rows carry the same
    per-candidate hindsight join tab_candidate_flow() performs
    (`attribution_by_candidate`), as the RAW attribution row (or None) under
    `hindsight_raw` -- formatting it into the "pending" / "+N.NNR (mock)"
    string is display logic, done client-side by
    console/src/decisions.js:formatHindsight(), mirroring
    streamlit_app._hindsight_cell() exactly (same "never show 0 for an
    unresolved replay" rule) rather than re-deriving it here."""
    label_summary = journal.label_summary()
    proposed = journal.proposed_candidates(100)
    # 2026-07-17 (Research-tab split): core-only is now a hard filter inside
    # these methods (was incidental on SHADOW_LABELLING_ENABLED being off),
    # and watch/rejected are latest-per-symbol (was an append-only per-scan
    # log -- 134 rows for 21 symbols read as noise). Each returned row
    # carries occurrence_count/first_seen_at_utc/history; blocked/proposed
    # stay as-is (proposed is already a self-pruning "currently pending"
    # list, per journal_store.py's own docstring on why it isn't deduped).
    watch = journal.watch_candidates_latest(100)
    rejected = journal.rejected_candidates_latest(100)
    blocked = journal.blocked_proposals(200)
    rejected_hindsight = journal.attribution_by_candidate(
        [cid for c in rejected if (cid := _candidate_id_str(c))]
    )
    blocked_hindsight = journal.attribution_by_candidate(
        [cid for c in blocked if (cid := _candidate_id_str(c))]
    )
    closed_trades = journal.closed_outcomes(500)
    return {
        "label_summary": label_summary,
        "proposed": proposed,
        "watch": watch,
        "rejected": [
            {**c, "hindsight_raw": rejected_hindsight.get(_candidate_id_str(c))} for c in rejected
        ],
        "blocked": [
            {**c, "hindsight_raw": blocked_hindsight.get(_candidate_id_str(c))} for c in blocked
        ],
        "open_trades": journal.open_positions(),
        "closed_trades": closed_trades,
        "closed_trade_metrics": compute_metrics(closed_trades),
        "as_of": _as_of(),
    }


@router.get("/learning")
def learning(settings: Settings = Depends(get_settings), journal: JournalStore = Depends(get_journal)) -> dict:
    """The Learning tab's four read-only sub-panels (TQS / Attribution /
    Hypotheses / Journal), verbatim -- the exact report-builder functions
    streamlit_app.tab_learning() calls via `orch.tqs_shadow_report()` /
    `orch.attribution_report()` / `orch.hypothesis_report()` /
    `orch.hypothesis_drafts_list(status="draft")` /
    `orch.learning_journal_feed()`. Every one of those Orchestrator methods
    is itself a one-line delegate to a free function taking only
    `journal`/`settings` (see alphaos/orchestrator.py lines ~1336-1417) --
    called directly here for the same reason `/approvals` avoids
    constructing a full Orchestrator.

    Reporting-law discipline (streamlit_app._learned_sentence()'s own
    docstring calls this "the reporting law": aggregate tone, no
    moralizing, and -- audit C4 -- never a per-event raw number standing in
    for a verdict): `build_attribution_report()`'s v2 aggregates already
    bake in the floor gate at the SOURCE (`_floor_gated_v2_aggregate()` in
    alphaos/reports/attribution.py sets `mean_delta_r`/`sum_delta_r` to
    `None` and `status` to `"below_sample_floor"` whenever the effective-N
    or span-day floor isn't cleared) -- this endpoint passes that dict
    through unchanged, so a floor-gated aggregate is STRUCTURALLY incapable
    of reaching the console with a populated mean/sum ΔR. The frontend
    (console/src/pages/Learning.jsx) renders the row exactly the way
    streamlit_app._attribution_v2_agg_row() does: mean/sum ΔR only when
    `status == "ok"`, otherwise a "n=X/floor below floor" status string --
    ported as a pure, tested function (console/src/learning.js:
    formatAttributionRow()) rather than re-derived inline in JSX. TQS scores
    are passed through paired with `data_confidence`/`bucket_histogram`
    exactly as `build_tqs_report()` already shapes them (score is never
    shown alone anywhere in this codebase; the frontend inherits that by
    rendering the same dict, not by recomputing it)."""
    return {
        "tqs": build_tqs_report(journal, limit=1000),
        "attribution": build_attribution_report(journal, settings, limit=1000),
        "hypotheses": build_hypothesis_report(journal),
        "hypothesis_drafts": list_drafts(journal, status="draft"),
        "journal_feed": build_journal_feed(journal, limit=50),
        "as_of": _as_of(),
    }


@router.get("/research")
def research(settings: Settings = Depends(get_settings), journal: JournalStore = Depends(get_journal)) -> dict:
    """The Research tab -- the shadow-tier (EXP-0/EXP-1) instrument's OWN
    surface, split out from /decisions 2026-07-17 specifically so shadow
    measurement data never shares a page with live trade decisions again
    (Fable 5 strategic consult: Decisions answers "what did the live
    machine decide"; shadow rows can never answer that -- the orchestrator
    itself raises if one ever reaches a trade decision).

    Deliberately reports READINESS, never audit conclusions: "how many
    trading days captured vs the ~20-day bar" (journal.shadow_instrument_
    health()), never percentiles/recommended-constant values. Those are a
    build-time, hand-run, version-bumped act (scripts/shadow_saturation_
    audit.py's own docstring: "never a guess, never env-tunable, never
    retro-scored") -- surfacing them on a 15s-poll console would recreate
    the exact premature-application pressure the operator already declined
    once on a 6-day sample (2026-07-16 hold decision). This endpoint answers
    "is it time to go run the script", not "what should the constants be".

    `constants.provisional` is derived from the version string's own "_v1"
    suffix (SHADOW_INTEREST_SCORE_VERSION_CURRENT), never a second flag --
    see candidate_scanner.py's comment on why that alias is the single
    source of truth for guessed-vs-audited."""
    version = SHADOW_INTEREST_SCORE_VERSION_CURRENT
    return {
        "shadow_tier_enabled": settings.shadow_tier_enabled,
        "shadow_labelling_enabled": settings.shadow_labelling_enabled,
        "constants": {
            "interest_score_version": version,
            "provisional": version.endswith("_v1"),
            "values": {
                "change_scale": SHADOW_V1_CHANGE_SCALE,
                "rel_vol_scale": SHADOW_V1_REL_VOL_SCALE,
                "day_range_min": SHADOW_V1_DAY_RANGE_MIN,
                "momentum_change_cap": SHADOW_V1_MOMENTUM_CHANGE_CAP,
                "momentum_relvol_cap": SHADOW_V1_MOMENTUM_RELVOL_CAP,
            },
        },
        "universe_config": {
            "min_adv_usd": settings.shadow_tier_min_adv_usd,
            "max_adv_usd": settings.shadow_tier_max_adv_usd,
            "min_price": settings.shadow_tier_min_price,
            "max_price": settings.shadow_tier_max_price,
            "target_count": settings.shadow_tier_target_count,
            "max_count": settings.shadow_tier_max_count,
        },
        "capture": journal.shadow_instrument_health(),
        "recent_captures": journal.shadow_recent_captures(25),
        "as_of": _as_of(),
    }


@router.get("/governance")
def governance(
    settings: Settings = Depends(get_settings),
    journal: JournalStore = Depends(get_journal),
    kill_switch: KillSwitch = Depends(get_kill_switch),
) -> dict:
    """`build_governance_report()`'s full dict, verbatim -- the exact
    function streamlit_app.tab_governance() renders from via
    `orch.governance_report(autonomy_level_label=AUTONOMY_LEVEL_LABEL)`
    (itself a one-line delegate, called directly here). Autonomy may/may-not
    panel, kill-switch explanation, hard limits, real-money lock, trading
    calendar -- every string is generated by that module from live
    settings/journal/kill-switch state, never hand-written here (see
    governance_report.py's own module docstring for the content rulings
    this preserves: no fake L2 criteria, no liquidation language, no LIVE
    badge, no unlock affordance). The only kill-switch CONTROL in this
    console is the annunciator strip (`/api/v1/annunciator`, unchanged from
    ND-1); this endpoint only explains the same state, exactly like the
    Streamlit tab it mirrors."""
    rep = build_governance_report(journal, settings, kill_switch, autonomy_level_label=AUTONOMY_LEVEL_LABEL)
    return {**rep, "as_of": _as_of()}


def _labeller_failsafe(journal: JournalStore, settings: Settings) -> dict:
    """Mirrors `Orchestrator._labeller_failsafe_health()` exactly
    (alphaos/orchestrator.py lines ~2638-2660) -- that method only touches
    `self.journal`/`self.settings`, so it's reproduced here call-for-call
    against the request's own journal/settings rather than requiring an
    Orchestrator instance."""
    summary = journal.labeller_source_summary(limit=50)
    health = evaluate_failsafe_health(
        summary,
        settings.labeller_failsafe_warn_rate,
        settings.labeller_failsafe_critical_rate,
        settings.labeller_failsafe_min_sample,
    )
    return {
        "level": health["level"],
        "message": health["message"],
        "total": summary["total"],
        "openai": summary["openai"],
        "mock": summary["mock"],
        "fail_safe": summary["fail_safe"],
        "fail_safe_rate": summary["fail_safe_rate"],
        "by_source": summary["by_source"],
        "by_failsafe_reason": summary["by_failsafe_reason"],
        "top_reason": health.get("top_reason"),
    }


@router.get("/system")
def system(
    settings: Settings = Depends(get_settings),
    journal: JournalStore = Depends(get_journal),
    kill_switch: KillSwitch = Depends(get_kill_switch),
) -> dict:
    """System Health + Scan Batches + Scheduler Runs + System Events,
    consolidated into one payload (ND-2 plan doc: "5 Streamlit tabs collapse
    into System & Audit's... concept" -- these four plus a recent-candidates
    list for the trade-packet picker below).

    Deliberate deviation from `orch.system_health()`: that method reads
    `self.orders.broker_connected`, which requires constructing an
    `OrderManager` (itself needing a `PositionManager` + `KillSwitch`) --
    but `OrderManager.__init__` sets `broker_connected` from nothing but
    `settings.is_paper and settings.has_alpaca_keys` (alphaos/execution/
    order_manager.py line ~71), no I/O, no provider call. Every other line
    of `system_health()`'s dict is already a pure `self.settings`/
    `self.journal`/`self.kill_switch` read or a call to a free function
    (`evaluate_failsafe_health`, `protection_watchdog.status_report`) --
    so this reproduces that same dict, field for field, against this
    request's own settings/journal/kill_switch plus the one inlined boolean
    expression, instead of constructing an `OrderManager` (or a full
    `Orchestrator`) whose only other job here would be exposing that one
    field. Same "avoid the heavy constructor, the value underneath is
    side-effect-free" reasoning as `/approvals` and `deps.get_market()`."""
    s = settings
    last_snap = journal.one(
        "SELECT freshness_status, market_session FROM price_snapshots ORDER BY id DESC LIMIT 1"
    )
    freshness = (last_snap or {}).get("freshness_status") or "n/a"
    health = {
        "playbook": PLAYBOOK_V1,
        "ai_primary": f"openai / {'configured' if s.has_openai_key else 'missing key (mock)'}",
        "ai_reviewer": f"anthropic / optional / {'configured' if s.has_anthropic_key else 'missing key'}",
        "market_data_provider": s.data_provider,
        "market_data_feed": s.market_data_feed,
        "market_data_mode": s.market_data_mode,
        "market_data_limited": "free/IEX — limited-market data",
        "market_data_freshness": freshness,
        "news_provider": "disabled_v1",
        "benzinga": "deferred_v1",
        "web_scraper": "disabled_v1",
        "massive": "deferred_v1",
        "last30days_research": (
            "disabled_v1" if not s.last30days_enabled
            else f"{s.last30days_provider} (keyless, context-only)"
        ),
        "labeller_decision_override": (
            "downgrade_only" if not s.labeller_decision_override_enabled
            else ("armed (symmetric)" if (s.has_openai_key and not s.is_mock)
                  else "enabled_inert_while_mock")
        ),
        "last30days_polarity": (
            "disabled_v1" if not s.last30days_polarity_enabled
            else (f"{s.last30days_polarity_model} (arming "
                  f"{'on' if s.last30days_polarity_arming_allowed else 'off'})"
                  + ("" if (s.has_openai_key and not s.is_mock) else ", mock"))
        ),
        "execution_provider": s.execution_provider,
        "real_alpaca_paper_execution": "enabled" if s.real_paper_execution else "not_enabled_v1",
        "real_money_trading": "unreachable",
        "manual_approval": "required" if s.effective_approval_mode.value == "manual" else "auto (capped)",
        "kill_switch": "ENGAGED" if kill_switch.is_engaged() else "off",
        "broker_connected": s.is_paper and s.has_alpaca_keys,
        "open_positions": journal.count_open_positions(),
        "labeller_failsafe": _labeller_failsafe(journal, settings),
        "protection_watchdog": protection_status_report(journal),
    }
    return {
        "health": health,
        "startup_checks": [c.as_dict() for c in settings.validate_startup()],
        "recent_snapshots": journal.query(
            "SELECT symbol, provider, freshness_status, is_usable, data_delay_seconds, source_timestamp "
            "FROM price_snapshots ORDER BY id DESC LIMIT 20"
        ),
        "recent_events": journal.recent_system_events(50),
        "scan_batches": journal.recent_scan_batches(50),
        "scheduler_runs": journal.recent_scheduler_runs(50),
        "recent_candidates": journal.recent_candidates(100),
        "as_of": _as_of(),
    }


@router.get("/system/trade-packet")
def system_trade_packet(
    candidate_id: Optional[str] = None,
    trade_id: Optional[str] = None,
    journal: JournalStore = Depends(get_journal),
) -> dict:
    """The Trade Packet drill-down -- `assemble_trade_packet()` verbatim,
    the exact function streamlit_app.tab_trade_packet() calls once an
    operator picks a candidate_id/trade_id from its selectbox. This is a
    separate endpoint (not folded into `/system` above) because it's the
    one System & Audit sub-view that takes real user input rather than
    rendering a fixed recent-N list; `/system`'s own `recent_candidates`
    list supplies the ids an operator can pick from. Neither id given ->
    `packet: null` (an honest "nothing selected yet", not an error)."""
    if not candidate_id and not trade_id:
        return {"packet": None, "as_of": _as_of()}
    packet = assemble_trade_packet(journal, candidate_id=candidate_id, trade_id=trade_id)
    return {"packet": packet, "as_of": _as_of()}
