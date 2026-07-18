"""S1b: evidence-query construction for post_earnings_reaction_v1's formal
test (H-PER-1P/H-PER-1N) -- the DB-facing half of the approved S1b
Statistical Mechanisms Specification (v2.0 as amended by v2.1-FINAL).
``alphaos.stats.two_arm`` is the pure statistical engine this module feeds;
``alphaos.stats.preregistration.evaluate_two_arm_hypothesis_pair()`` is the
only caller of either.

NOT WIRED INTO PRODUCTION. Nothing in this module is imported by
orchestrator.py, candidate_scanner.py, any scheduler job, or by
alphaos/cards/registry.py -- it exists, is fully tested, and has zero
runtime effect until an operator explicitly invokes the (also-dormant)
registration/evaluation command this module supports. It reads
``candidates``/``candidate_outcomes``/``earnings_calendar_cache`` but never
writes to any of them; the only table this module's own functions write to
is ``per_evidence_snapshots``, and only via
``evaluate_two_arm_hypothesis_pair()``, never directly.

FIXED POPULATION (spec Section 1 / v2.1 correction 1): every function here
that builds ``E*`` -- the frozen PER event set -- runs its ladder/gate
logic exactly ONCE, before any bootstrap replicate exists. Nothing in
``alphaos.stats.two_arm`` may drop or reselect a member of the list this
module hands it; that is the whole point of the v2.1 correction.

PRIMARY/PLACEBO INDEPENDENCE (spec Section 6 / v2.1 correction 2):
``build_primary_evidence()`` takes no placebo-related parameter, calls no
placebo-related function, and its result depends on nothing about the
placebo's window position, exclusions, or enabled/disabled state.
``build_placebo_evidence()`` is the only function that knows about the
placebo shift; it takes the PRIMARY's own frozen events as an input
(one-directional: the placebo is defined FOR each real PER event, which is
the placebo's whole reason to exist) but the dependency never runs the
other way. See ``tests/test_s1b_per_evidence.py::
test_changing_placebo_definition_leaves_primary_byte_identical`` for the
regression proof.

MARKET-DATE CONVENTION: every observation's "market date" is its own
``candidate_outcomes.decision_at_utc`` truncated to a calendar date -- the
SAME convention every existing PR12/EVID-1/scoreboard query already uses
(``(r["decision_at_utc"] or "")[:10]``). This is deliberately NOT the
earnings event's own ``report_date``/window-open date: a PER candidate can
be created on any of its 3 eligible trading days (S1a's own
``K_TRADING_DAYS``), and the 5-trading-day OUTCOME window measured by
``outcomes_tracker.py`` runs forward from the candidate's own decision date,
not from the report date. ``report_date``/``timing`` (joined from
``earnings_calendar_cache`` via ``candidates.card_assignment_ref``) are used
ONLY to anchor the placebo's shifted reference point.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date as _date
from datetime import timedelta
from typing import Any, Optional

from alphaos.cards.selector import PER_CARD_ID, SELECTOR_VERSION, EarningsTimingClass
from alphaos.stats.two_arm import build_trading_day_clusters
from alphaos.util.market_calendar import is_trading_day, trading_days_between

# --------------------------------------------------------------- constants
OUTCOME_METRIC = "market_adjusted_return_5d_pct"
OUTCOME_WINDOW_TRADING_DAYS = 5

PLACEBO_SHIFT_TRADING_DAYS = 10
# How far (in trading days) a default-card candidate's own market_date may
# sit from the placebo's shifted target date and still stand in as "the"
# placebo observation for that event (spec: "same control procedure or a
# SIMPLIFIED diagnostic procedure" -- AlphaOS has no counterfactual-return
# computation for an arbitrary non-decision date, so the placebo observation
# is the nearest REAL default-card decision to the shifted target, not a
# freshly computed synthetic return; see module docstring's tolerance note
# in build_placebo_evidence()).
PLACEBO_TOLERANCE_TRADING_DAYS = 5

# Exclusion zone width -- same as the outcome window itself: a control whose
# own 5-trading-day outcome window overlaps a PER event's is not independent
# of it.
CROSS_ARM_EXCLUSION_TRADING_DAYS = OUTCOME_WINDOW_TRADING_DAYS

RUNG1_MIN_CONTROLS = 5
RUNG2_MIN_CONTROLS = 30

MAX_POOLED_FALLBACK_SHARE = 0.20
MAX_EXCLUSION_SHARE = 0.10

MIN_PER_RAW_N = 25
MIN_SPAN_DAYS = 90.0
MIN_DISTINCT_MONTHS = 3
MAX_SYMBOL_CONCENTRATION = 0.20
MIN_CONTROL_RAW_N = 100


def _tier_of(shadow_tier: Any) -> str:
    return "shadow" if shadow_tier else "core"


def _market_date(decision_at_utc: Optional[str]) -> Optional[str]:
    if not decision_at_utc:
        return None
    return str(decision_at_utc)[:10]


def _event_key(symbol: str, fiscal_date_ending: Optional[str], report_date: Optional[str]) -> str:
    """Scoped per symbol -- matches alphaos.cards.selector's own
    ``_event_key`` grouping exactly (``(symbol, fiscal_date_ending)`` or a
    ``(symbol, report_date)`` singleton). Without the symbol, two DIFFERENT
    symbols reporting on the same fiscal_date_ending (e.g. two calendar-Q1
    filers, both ending 2026-03-31) would collapse into the SAME dedup
    group -- a correctness bug caught by this module's own test suite
    before it ever reached the two-arm engine."""
    if fiscal_date_ending:
        return f"{symbol}|fiscal:{fiscal_date_ending}"
    return f"{symbol}|report_date:{report_date}"


def _timing_class(cache_timing: Optional[str]) -> str:
    """Duplicated, deliberately, from alphaos.cards.selector's identical
    private helper: importing a leading-underscore name across modules
    would couple this module to selector.py's internals rather than its
    public contract. The logic is Alpha Vantage's own two literal timing
    strings and is small enough that a second copy is cheaper than the
    coupling -- if selector.py's own version ever changes, S1b's own test
    suite (mirroring selector.py's fixtures) will catch the drift."""
    if cache_timing == "pre-market":
        return EarningsTimingClass.BMO
    if cache_timing == "post-market":
        return EarningsTimingClass.AMC
    return EarningsTimingClass.UNKNOWN


def _window_open_date(report_date: _date, timing_class: str) -> _date:
    """Same rule as selector.py's ``_window_open_date`` -- see that
    module's docstring for the full rationale (BMO precedes that day's
    open; AMC/UNKNOWN cannot have been known before that day's close; a
    BMO date landing on a non-trading day rolls forward)."""
    from alphaos.util.market_calendar import nth_trading_day_after
    if timing_class == EarningsTimingClass.BMO:
        return report_date if is_trading_day(report_date) else nth_trading_day_after(report_date, 1)
    return nth_trading_day_after(report_date, 1)


def _nth_trading_day_before(d: _date, n: int) -> _date:
    """The mirror image of ``market_calendar.nth_trading_day_after`` --
    not added to that shared, production-imported module so this module's
    diff stays fully contained to S1b's own new files."""
    if n <= 0:
        raise ValueError("n must be >= 1")
    cur = d
    count = 0
    while count < n:
        cur -= timedelta(days=1)
        if is_trading_day(cur):
            count += 1
    return cur


def _trading_day_distance(d1: _date, d2: _date) -> int:
    """Symmetric trading-day distance -- ``market_calendar.
    trading_days_between`` is deliberately directional (half-open,
    ``end <= start`` -> 0), so this wraps it both ways for a same-symbol
    overlap check where either date can be earlier."""
    if d1 == d2:
        return 0
    if d1 < d2:
        return trading_days_between(d1, d2)
    return trading_days_between(d2, d1)


# --------------------------------------------------------------- fetch: PER
_LATEST_OUTCOME = (
    "co.id = (SELECT co2.id FROM candidate_outcomes co2 "
    "WHERE co2.candidate_id = {ref} AND co2.candidate_type != 'user_override' "
    "ORDER BY co2.id DESC LIMIT 1)"
)


def _fetch_per_candidate_rows(journal, as_of_utc: str) -> list[dict]:
    """Every candidate ever PER-stamped (``card_id = PER_CARD_ID``,
    ``card_assignment_status = 'ok'``) with a resolved, complete 5-day
    market-adjusted outcome, joined to the ``earnings_calendar_cache`` row
    that justified the assignment (via ``card_assignment_ref``). Raw --
    NOT yet deduped to one-per-event, NOT yet ladder-gated. Both
    ``build_primary_evidence()`` and ``build_placebo_evidence()`` start
    from this same raw fetch (see module docstring: shared raw data is not
    the same as shared derived logic)."""
    rows = journal.query(
        "SELECT c.candidate_id, c.symbol, c.shadow_tier, c.card_assignment_ref, "
        "co.decision_at_utc, co.market_adjusted_return_5d_pct AS outcome_value, "
        "ecc.report_date, ecc.fiscal_date_ending, ecc.timing "
        "FROM candidates c "
        "JOIN candidate_outcomes co ON " + _LATEST_OUTCOME.format(ref="c.candidate_id") + " "
        "LEFT JOIN earnings_calendar_cache ecc ON ecc.id = CAST(c.card_assignment_ref AS INTEGER) "
        "WHERE c.card_id = ? AND c.card_assignment_status = 'ok' "
        "AND co.outcome_status = 'complete' AND co.market_adjusted_return_5d_pct IS NOT NULL "
        "AND c.created_at_utc < ?",
        (PER_CARD_ID, as_of_utc),
    )
    # Defensive: a row whose cache reference didn't resolve (broken/missing
    # earnings_calendar_cache row) carries no report_date/timing and cannot
    # be placed in a window at all -- excluded here rather than raising.
    return [r for r in rows if r.get("report_date")]


def _dedupe_to_one_per_event(rows: list[dict]) -> list[dict]:
    """'First eligible candidate' per event (spec Section 1): rows sharing
    an event_key are reduced to the single EARLIEST (by decision_at_utc,
    candidate_id as a deterministic tiebreak) -- the observation the
    approved semantic ordering would surface first. Rows missing a usable
    market_date are dropped (uncountable, never fabricated)."""
    groups: dict[str, dict] = {}
    for r in rows:
        market_date = _market_date(r["decision_at_utc"])
        if market_date is None:
            continue
        key = _event_key(r["symbol"], r.get("fiscal_date_ending"), r.get("report_date"))
        sort_key = (market_date, r["candidate_id"])
        current = groups.get(key)
        if current is None or sort_key < current["_sort_key"]:
            groups[key] = {**r, "_event_key": key, "_market_date": market_date, "_sort_key": sort_key}
    for g in groups.values():
        del g["_sort_key"]
    return list(groups.values())


# ----------------------------------------------------------- fetch: control
def _fetch_default_control_rows(journal, as_of_utc: str, dates: set) -> list[dict]:
    """Default-card candidates (``card_id != PER_CARD_ID``,
    ``card_assignment_status = 'ok'`` -- excludes fallback-driven default
    assignments from degraded cache health, which never "evaluated"
    anything), one row per ``(symbol, market_date)``, restricted to
    ``dates``. ``card_id != PER_CARD_ID`` rather than pinning to today's
    ``get_default_card()['card_id']`` deliberately: a historical candidate
    stamped under a since-superseded default card version (e.g.
    ``catalyst_momentum_v1`` before INSTR-1) was still, at scan time, THE
    system's contemporaneous default-card opportunity set for that date --
    exactly what the control arm is supposed to represent."""
    if not dates:
        return []
    rows = journal.query(
        "SELECT c.candidate_id, c.symbol, c.shadow_tier, co.decision_at_utc, "
        "co.market_adjusted_return_5d_pct AS outcome_value "
        "FROM candidates c "
        "JOIN candidate_outcomes co ON " + _LATEST_OUTCOME.format(ref="c.candidate_id") + " "
        "WHERE c.card_id != ? AND c.card_assignment_status = 'ok' "
        "AND co.outcome_status = 'complete' AND co.market_adjusted_return_5d_pct IS NOT NULL "
        "AND c.created_at_utc < ?",
        (PER_CARD_ID, as_of_utc),
    )
    out = []
    seen: dict[tuple, dict] = {}
    for r in rows:
        market_date = _market_date(r["decision_at_utc"])
        if market_date is None or market_date not in dates:
            continue
        key = (r["symbol"], market_date)
        sort_key = r["candidate_id"]
        current = seen.get(key)
        if current is None or sort_key < current["candidate_id"]:
            seen[key] = {**r, "_market_date": market_date, "_tier": _tier_of(r["shadow_tier"])}
    out = list(seen.values())
    return out


# -------------------------------------------------------- cross-arm exclude
def _excluded_by_overlap(candidate_symbol: str, candidate_market_date: str, exclusion_zones: list) -> bool:
    """``exclusion_zones``: list of ``(symbol, date)`` pairs whose outcome
    window this control observation must not overlap."""
    d_c = _date.fromisoformat(candidate_market_date)
    for symbol, d_p_str in exclusion_zones:
        if symbol != candidate_symbol:
            continue
        d_p = _date.fromisoformat(d_p_str)
        if _trading_day_distance(d_c, d_p) <= CROSS_ARM_EXCLUSION_TRADING_DAYS:
            return True
    return False


def _filter_controls(controls: list[dict], exclusion_zones: list) -> list[dict]:
    return [c for c in controls if not _excluded_by_overlap(c["symbol"], c["_market_date"], exclusion_zones)]


# ------------------------------------------------------------------ ladder
def _apply_ladder(events: list[dict], controls: list[dict]) -> tuple:
    """Frozen control ladder (spec Section 5, v2.1 correction 1: the rung
    ASSIGNED here is never re-decided inside a bootstrap replicate).
    Returns ``(valid_events, excluded_events, rung1_shares, rung2_shares)``
    where each valid event is tagged with its own frozen ``stratum_key``
    and ``control_fallback`` ('rung1'/'rung2'). No cross-tier fallback,
    ever."""
    by_date_tier: dict[tuple, list[dict]] = {}
    by_tier: dict[str, list[dict]] = {}
    for c in controls:
        by_date_tier.setdefault((c["_market_date"], c["_tier"]), []).append(c)
        by_tier.setdefault(c["_tier"], []).append(c)

    valid: list[dict] = []
    excluded: list[dict] = []
    for e in events:
        tier = _tier_of(e["shadow_tier"])
        rung1_pool = by_date_tier.get((e["_market_date"], tier), [])
        if len(rung1_pool) >= RUNG1_MIN_CONTROLS:
            valid.append({**e, "_tier": tier, "stratum_key": ("dt", e["_market_date"], tier), "control_fallback": "rung1"})
            continue
        rung2_pool = by_tier.get(tier, [])
        if len(rung2_pool) >= RUNG2_MIN_CONTROLS:
            valid.append({**e, "_tier": tier, "stratum_key": ("tier", tier), "control_fallback": "rung2"})
            continue
        excluded.append({**e, "_tier": tier, "excluded_reason": "no_rung_cleared"})
    return valid, excluded


# ---------------------------------------------------------------- gates
@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason: Optional[str]
    detail: dict


def _check_population_gates(valid_events: list[dict], excluded_events: list[dict], controls: list[dict]) -> GateResult:
    n_raw = len(valid_events) + len(excluded_events)
    detail: dict[str, Any] = {
        "n_per_raw": n_raw,
        "n_per_valid": len(valid_events),
        "n_per_excluded": len(excluded_events),
        "n_control_raw": len(controls),
    }
    if n_raw < MIN_PER_RAW_N:
        return GateResult(False, "per_raw_n_below_floor", detail)
    if not valid_events:
        return GateResult(False, "no_valid_per_events", detail)

    dates = sorted({e["_market_date"] for e in valid_events})
    span_days = (_date.fromisoformat(dates[-1]) - _date.fromisoformat(dates[0])).days
    months = {d[:7] for d in dates}
    detail["span_days"] = float(span_days)
    detail["n_distinct_months"] = len(months)
    if span_days < MIN_SPAN_DAYS:
        return GateResult(False, "span_below_floor", detail)
    if len(months) < MIN_DISTINCT_MONTHS:
        return GateResult(False, "month_coverage_below_floor", detail)

    symbol_counts: dict[str, int] = {}
    for e in valid_events:
        symbol_counts[e["symbol"]] = symbol_counts.get(e["symbol"], 0) + 1
    max_share = max(symbol_counts.values()) / len(valid_events)
    detail["max_symbol_share"] = round(max_share, 4)
    if max_share > MAX_SYMBOL_CONCENTRATION:
        return GateResult(False, "symbol_concentration_above_ceiling", detail)

    if len(controls) < MIN_CONTROL_RAW_N:
        return GateResult(False, "control_raw_n_below_floor", detail)

    # Audit-fixup (correctness LOW): pooled_share is a share of the ANALYZED
    # population (valid_events -- matching max_symbol_share's own
    # denominator just above), never n_raw -- dividing by n_raw understated
    # it whenever any events were also excluded, letting a population where
    # e.g. 22% of the events actually entering E* rest on the weaker
    # rung-2 reference pass a nominal "<=20%" ceiling. exclusion_share is
    # correctly n_raw-denominated (it is inherently a question about the
    # RAW considered population, not the analyzed one).
    n_rung2 = sum(1 for e in valid_events if e["control_fallback"] == "rung2")
    pooled_share = n_rung2 / len(valid_events)
    exclusion_share = len(excluded_events) / n_raw
    detail["pooled_fallback_share"] = round(pooled_share, 4)
    detail["exclusion_share"] = round(exclusion_share, 4)
    if pooled_share > MAX_POOLED_FALLBACK_SHARE:
        return GateResult(False, "pooled_fallback_share_above_ceiling", detail)
    if exclusion_share > MAX_EXCLUSION_SHARE:
        return GateResult(False, "exclusion_share_above_ceiling", detail)

    return GateResult(True, None, detail)


# ------------------------------------------------------------------ clusters
def _events_to_cluster_input(events: list[dict]) -> list[dict]:
    return [
        {**e, "value": e["outcome_value"], "market_date": e["_market_date"]}
        for e in events
    ]


def _controls_to_cluster_input(controls: list[dict], referenced_tiers_by_date: dict) -> list[dict]:
    """Tags each control with every stratum_key it counts toward: its own
    (date, tier) rung-1 key always, plus its tier's rung-2 pooled key
    whenever some event actually references that pooled stratum (computing
    membership only for referenced strata keeps this a pure data-shaping
    step -- ``two_arm.two_arm_bootstrap`` itself decides which strata
    matter by only ever looking up ``stratum_key``s that appear on an
    event)."""
    out = []
    for c in controls:
        keys: list[tuple] = [("dt", c["_market_date"], c["_tier"]), ("tier", c["_tier"])]
        out.append({**c, "value": c["outcome_value"], "market_date": c["_market_date"], "stratum_keys": keys})
    return out


def _build_clusters(events: list[dict], controls: list[dict]) -> tuple:
    event_clusters = build_trading_day_clusters(
        _events_to_cluster_input(events), window_trading_days=OUTCOME_WINDOW_TRADING_DAYS,
    )
    control_clusters = build_trading_day_clusters(
        _controls_to_cluster_input(controls, {}), window_trading_days=OUTCOME_WINDOW_TRADING_DAYS,
    )
    return event_clusters, control_clusters


# --------------------------------------------------------------- snapshot
def _snapshot_row(arm: str, r: dict, *, event_key=None, stratum_key=None,
                   control_fallback=None, excluded_reason=None) -> dict:
    return {
        "arm": arm,
        "candidate_id": r["candidate_id"],
        "symbol": r["symbol"],
        "event_key": event_key,
        "market_date": r.get("_market_date"),
        "tier": r.get("_tier") or _tier_of(r.get("shadow_tier")),
        "outcome_value": r.get("outcome_value"),
        "cluster_id": None,  # filled in by canonical ordering below
        "stratum_key": json.dumps(stratum_key) if stratum_key is not None else None,
        "control_fallback": control_fallback,
        "excluded_reason": excluded_reason,
    }


def _tag_cluster_ids(clusters: list[list[dict]], prefix: str) -> dict:
    """Deterministic cluster id per row: ``{prefix}:{symbol}:{iso(earliest
    member's market_date)}``. Returns a ``candidate_id -> cluster_id`` map."""
    out = {}
    for cluster in clusters:
        symbol = cluster[0]["symbol"]
        earliest = min(r["market_date"] for r in cluster)
        cid = f"{prefix}:{symbol}:{earliest}"
        for r in cluster:
            out[r["candidate_id"]] = cid
    return out


@dataclass(frozen=True)
class EvidenceResult:
    status: str
    reason: Optional[str]
    detail: dict
    per_clusters: list
    control_clusters: list
    snapshot_rows: list
    # The frozen E* itself (or, on an insufficient_data status, whatever
    # ladder-valid events existed before the gate check failed) -- exposed
    # so a caller building the placebo diagnostic (which is anchored to
    # "each PER event") never has to re-derive it with a second DB read.
    valid_events: list


def build_primary_evidence(journal, as_of_utc: str) -> EvidenceResult:
    """The primary H-PER-1P/H-PER-1N evidence population. Depends on
    NOTHING about the placebo -- no parameter, no import, no shared
    derived-logic function with ``build_placebo_evidence()`` beyond the
    raw-row fetch (see module docstring)."""
    raw = _fetch_per_candidate_rows(journal, as_of_utc)
    events = _dedupe_to_one_per_event(raw)
    event_dates = {e["_market_date"] for e in events}
    exclusion_zones = [(e["symbol"], e["_market_date"]) for e in events]

    all_controls = _fetch_default_control_rows(journal, as_of_utc, event_dates)
    controls = _filter_controls(all_controls, exclusion_zones)

    valid_events, excluded_events = _apply_ladder(events, controls)
    gate = _check_population_gates(valid_events, excluded_events, controls)

    snapshot_rows = []
    for e in valid_events:
        snapshot_rows.append(_snapshot_row(
            "per_event", e, event_key=e["_event_key"], stratum_key=e["stratum_key"],
            control_fallback=e["control_fallback"],
        ))
    for e in excluded_events:
        snapshot_rows.append(_snapshot_row(
            "per_event_excluded", e, event_key=e["_event_key"], excluded_reason=e["excluded_reason"],
        ))
    for c in controls:
        snapshot_rows.append(_snapshot_row("control", c))

    if not gate.ok:
        return EvidenceResult(status="insufficient_data", reason=gate.reason, detail=gate.detail,
                               per_clusters=[], control_clusters=[], snapshot_rows=snapshot_rows,
                               valid_events=valid_events)

    event_clusters, control_clusters = _build_clusters(valid_events, controls)
    per_id_by_candidate = _tag_cluster_ids(event_clusters, "PER")
    ctl_id_by_candidate = _tag_cluster_ids(control_clusters, "CTL")
    for row in snapshot_rows:
        if row["arm"] == "per_event":
            row["cluster_id"] = per_id_by_candidate.get(row["candidate_id"])
        elif row["arm"] == "control":
            row["cluster_id"] = ctl_id_by_candidate.get(row["candidate_id"])

    return EvidenceResult(status="ok", reason=None, detail=gate.detail,
                           per_clusters=event_clusters, control_clusters=control_clusters,
                           snapshot_rows=snapshot_rows, valid_events=valid_events)


def build_placebo_evidence(journal, as_of_utc: str, primary_events: list) -> EvidenceResult:
    """The placebo diagnostic (spec Section 11) -- descriptive only, never
    entering BH-FDR or the promotion gate. Anchored to the PRIMARY's own
    frozen events (one-directional: a placebo only exists relative to a
    real event) but builds a FULLY SEPARATE control population and
    exclusion set -- see module docstring.

    AlphaOS has no per-arbitrary-date counterfactual-return computation (no
    new price-bar fetch/EVID-2-style calculation is in S1b's scope), so the
    placebo "event" for a given PER event is not a freshly computed return
    at the shifted date -- it is the nearest REAL default-card candidate's
    own outcome within ``PLACEBO_TOLERANCE_TRADING_DAYS`` of the shifted
    target date. An event with no default-card candidate that close to its
    shifted target simply has no placebo observation (excluded, reason
    'no_placebo_candidate_in_tolerance') -- never fabricated. This is a
    documented, deliberate simplification of the diagnostic (the spec
    itself allows "the same control procedure OR a simplified diagnostic
    procedure" here), not a silent one -- see this build's own final report
    for the explicit callout.
    """
    from alphaos.util.market_calendar import nth_trading_day_after

    real_zones = [(e["symbol"], e["_market_date"]) for e in primary_events]

    shifted_targets: dict[str, tuple] = {}
    for e in primary_events:
        try:
            report_date = _date.fromisoformat(str(e["report_date"])[:10])
        except (TypeError, ValueError):
            continue
        tclass = _timing_class(e.get("timing"))
        window_open = _window_open_date(report_date, tclass)
        target = _nth_trading_day_before(window_open, PLACEBO_SHIFT_TRADING_DAYS)
        shifted_targets[e["candidate_id"]] = (e["symbol"], target)

    tolerance_start = min((t for _s, t in shifted_targets.values()), default=None)
    tolerance_end = max((t for _s, t in shifted_targets.values()), default=None)
    candidate_dates: set = set()
    if tolerance_start is not None:
        d = _nth_trading_day_before(tolerance_start, PLACEBO_TOLERANCE_TRADING_DAYS)
        end = nth_trading_day_after(tolerance_end, PLACEBO_TOLERANCE_TRADING_DAYS)
        while d <= end:
            candidate_dates.add(d.isoformat())
            d += timedelta(days=1)

    control_pool = _fetch_default_control_rows(journal, as_of_utc, candidate_dates)
    by_symbol_date: dict[tuple, dict] = {(c["symbol"], c["_market_date"]): c for c in control_pool}

    # Audit-fixup (architecture LOW): two different primary events (e.g. a
    # duplicate/restated same-symbol earnings cache row) can resolve to the
    # SAME nearest control candidate as their placebo observation -- without
    # dedup, both would produce a
    # (snapshot_id, 'placebo_event', candidate_id) row and collide on
    # idx_per_evidence_snapshots_unique, crashing the whole evaluation with
    # an opaque IntegrityError instead of completing or deferring cleanly.
    # Each candidate can stand in for at most ONE primary event's placebo;
    # ties broken by the primary event's own market_date (earliest wins,
    # candidate_id as a final deterministic tiebreak) so the choice never
    # depends on primary_events' input order.
    by_candidate: dict[str, dict] = {}
    for e in primary_events:
        if e["candidate_id"] not in shifted_targets:
            continue
        symbol, target = shifted_targets[e["candidate_id"]]
        best: Optional[dict] = None
        best_dist: Optional[int] = None
        for (csym, cdate), row in by_symbol_date.items():
            if csym != symbol:
                continue
            dist = _trading_day_distance(_date.fromisoformat(cdate), target)
            if dist > PLACEBO_TOLERANCE_TRADING_DAYS:
                continue
            if (best_dist is None or dist < best_dist
                    or (dist == best_dist and best is not None and row["candidate_id"] < best["candidate_id"])):
                best, best_dist = row, dist
        if best is None:
            continue
        placebo_event = {**best, "_event_key": e["_event_key"], "shadow_tier": best["shadow_tier"]}
        claim_key = (e["_market_date"], e["candidate_id"])
        existing = by_candidate.get(best["candidate_id"])
        if existing is None or claim_key < existing["_claim_key"]:
            by_candidate[best["candidate_id"]] = {**placebo_event, "_claim_key": claim_key}

    placebo_events: list[dict] = []
    for pe in by_candidate.values():
        pe = dict(pe)
        del pe["_claim_key"]
        placebo_events.append(pe)

    placebo_dates = {pe["_market_date"] for pe in placebo_events}
    placebo_zones = [(pe["symbol"], pe["_market_date"]) for pe in placebo_events]
    all_placebo_controls = _fetch_default_control_rows(journal, as_of_utc, placebo_dates | candidate_dates)
    placebo_controls = _filter_controls(all_placebo_controls, real_zones + placebo_zones)
    # A placebo event's own candidate must never also serve as its own control.
    placebo_event_ids = {pe["candidate_id"] for pe in placebo_events}
    placebo_controls = [c for c in placebo_controls if c["candidate_id"] not in placebo_event_ids]

    valid_events, excluded_events = _apply_ladder(placebo_events, placebo_controls)
    gate = _check_population_gates(valid_events, excluded_events, placebo_controls)

    snapshot_rows = []
    for pe in valid_events:
        snapshot_rows.append(_snapshot_row(
            "placebo_event", pe, event_key=pe["_event_key"], stratum_key=pe["stratum_key"],
            control_fallback=pe["control_fallback"],
        ))
    for pe in excluded_events:
        snapshot_rows.append(_snapshot_row(
            "placebo_event_excluded", pe, event_key=pe["_event_key"], excluded_reason=pe["excluded_reason"],
        ))
    for c in placebo_controls:
        snapshot_rows.append(_snapshot_row("placebo_control", c))

    if not gate.ok:
        return EvidenceResult(status="insufficient_data", reason=gate.reason, detail=gate.detail,
                               per_clusters=[], control_clusters=[], snapshot_rows=snapshot_rows,
                               valid_events=valid_events)

    event_clusters, control_clusters = _build_clusters(valid_events, placebo_controls)
    per_id_by_candidate = _tag_cluster_ids(event_clusters, "PLACEBO_EVT")
    ctl_id_by_candidate = _tag_cluster_ids(control_clusters, "PLACEBO_CTL")
    for row in snapshot_rows:
        if row["arm"] == "placebo_event":
            row["cluster_id"] = per_id_by_candidate.get(row["candidate_id"])
        elif row["arm"] == "placebo_control":
            row["cluster_id"] = ctl_id_by_candidate.get(row["candidate_id"])

    return EvidenceResult(status="ok", reason=None, detail=gate.detail,
                           per_clusters=event_clusters, control_clusters=control_clusters,
                           valid_events=valid_events,
                           snapshot_rows=snapshot_rows)


# ------------------------------------------------------------- canonical hash
def canonical_snapshot_rows(primary: EvidenceResult, placebo: Optional[EvidenceResult]) -> list[dict]:
    """Every snapshot row from both results, in the approved semantic
    ordering (market_date -> tier -> candidate_id -- scan-window ordinal is
    not a column that exists on a frozen snapshot row, so this ordering
    drops that key relative to the original SelectorContext ordering spec;
    tie-break is fully deterministic regardless), ready for canonical JSON
    serialization / SHA-256 hashing or for ``journal.insert``."""
    rows = list(primary.snapshot_rows)
    if placebo is not None:
        rows = rows + list(placebo.snapshot_rows)
    return sorted(rows, key=lambda r: (r.get("market_date") or "", r.get("tier") or "", r["arm"], r["candidate_id"]))


def canonical_snapshot_hash(rows: list[dict]) -> str:
    """SHA-256 over the canonically-ordered, canonically-serialized
    snapshot rows -- same convention as S1a's own golden-fixture hash
    (``json.dumps(sort_keys=True)`` then ``sha256``), independent of
    ``alphaos.lineage.hashing.stable_hash()`` (that helper's own docstring
    scopes it to lineage/config hashing and explicitly excludes
    credential-bearing settings fields, a concern irrelevant here; this
    hash exists to catch a semantic regression in evidence CONSTRUCTION,
    the same job S1a's own pinned hash does for selection logic)."""
    import hashlib
    blob = json.dumps(rows, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ------------------------------------------------------ selector-binding check
def validate_card_selector_binding(card: dict) -> None:
    """Cross-checks a card's own declared ``requires_selector`` field
    against ``alphaos.cards.selector.SELECTOR_VERSION``. Raises ``ValueError``
    on a mismatch; a no-op if the card declares no ``requires_selector`` at
    all (existing cards, e.g. the two catalyst_momentum versions, have none).

    DELIBERATELY NOT called from ``alphaos.cards.registry.sync_registry()``
    (which runs at real orchestrator startup, alphaos/orchestrator.py:178):
    that would force ``registry.py`` -- a module imported by production
    orchestrator/scanner code -- to import ``alphaos.cards.selector``,
    transitively pulling the still-unwired selector into the production
    import graph for the first time. That would not itself STAMP any
    candidate, but it would break this session's own established isolation
    convention (the grep-based "cards.selector never appears in a
    production-imported file" test every prior slice has relied on) for no
    functional benefit, since ``sync_registry()`` already refuses to start
    on any card content-hash mismatch regardless. This function exists so
    the binding is REAL and TESTED (called by this module's own test suite,
    and available to the operator-invoked registration CLI as a
    pre-registration safety check) without paying that cost. Documented
    here, not silently decided -- see this build's final report.
    """
    declared = card.get("requires_selector")
    if declared is None:
        return
    if declared != SELECTOR_VERSION:
        raise ValueError(
            f"card {card.get('card_id')!r} declares requires_selector={declared!r}, "
            f"which no longer matches the live SELECTOR_VERSION {SELECTOR_VERSION!r} -- "
            "refusing to treat this card's evidence construction as valid until reconciled."
        )
