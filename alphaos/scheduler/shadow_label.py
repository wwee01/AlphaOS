"""EXP-1: shadow-tier AI labelling (docs/roadmap/alphaos-pr-implementation-specs.md,
"EXP-1 -- Shadow small/mid catalyst universe (the payload)").

This module owns everything EXP-0's deterministic shadow capture does NOT:
selection (mechanism 2: top-K + explore, versioned), the shadow AI-cost
sub-cap (mechanism 7), the feed-coverage arming gate (mechanism 8), and
auto-suspend (mechanism 13). It NEVER calls the labeller itself -- that stays
inside ``Orchestrator._label_shadow_shortlist``, which reuses the EXISTING
``Orchestrator._label_candidate`` unchanged (mechanism 5's founder ruling).

Zero decision surface: nothing here ever builds a proposal, and this module
imports nothing from the approval/execution/risk stack.
"""

from __future__ import annotations

import hashlib
import json
import random
import statistics
from datetime import timedelta
from typing import Optional

# SUSP-1 audit-fixup (2026-08, MUST FIX 1a): these come from alphaos.constants,
# NOT alphaos.canary.run -- importing from canary.run here used to be a real
# circular import (alphaos.canary -> canary.run -> alphaos.scheduler (via
# cost_guard) -> scheduler.digest -> scheduler.shadow_label -> back into the
# still-initializing canary.run). constants.py is a leaf module with zero
# alphaos.* imports of its own, so it can never re-enter this cycle.
from alphaos.constants import (
    CANARY_CONFIRMATION_STATUS_CONFIRMED,
    CANARY_CONFIRMATION_STATUS_IDENTITY_IMMEDIATE,
    CANARY_CONFIRMATION_STATUS_NOT_CONFIRMED,
    CANARY_CONFIRMATION_STATUS_UNCONFIRMED_PAGE,
    DRIFT_TIER_1,
    DRIFT_TIER_2,
    Severity,
    SHADOW_SELECTION_ARM_EXPLORE,
    SHADOW_SELECTION_ARM_TOP_K,
    SHADOW_SELECTION_VERSION_V1,
)
from alphaos.safety import ShadowLabelSuspendSwitch
from alphaos.scheduler import cost_guard
from alphaos.util import alerts, timeutils

# --------------------------------------------------------------- constants
FEED_COVERAGE_TRAILING_DAYS = 14
AUTO_SUSPEND_COVERAGE_CONSECUTIVE_DAYS = 3
# audit-fixup (correctness LOW): the auto-suspend check fetches this many
# CALENDAR days of history to find AUTO_SUSPEND_COVERAGE_CONSECUTIVE_DAYS
# actual TRADING days within it (universe_days has no row on non-trading
# days, so the map is trading-days-only even though the cutoff is calendar-
# based). The original +2 padding covers one ordinary weekend but not a
# holiday cluster (e.g. a Monday holiday adjacent to a weekend, or two
# holidays in the same stretch) -- a rare span like that could silently
# under-trigger this safety mechanism (the unsafe direction) by finding
# fewer than 3 trading-day entries in the window. +9 comfortably spans any
# realistic US-market holiday cluster (this codebase's own HOL-2 early-
# close item names Thanksgiving-week as the tightest case) while still
# being a small, cheap query. Independently mitigated regardless: the
# separate trailing-14-day feed-coverage gate (check_feed_coverage_gate,
# below) still blocks arming on sustained bad coverage even if this
# specific auto-suspend edge case were missed.
AUTO_SUSPEND_LOOKBACK_CALENDAR_PADDING_DAYS = 9


# ------------------------------------------------------------- cost guard
def shadow_calls_in_last_30_days(journal) -> int:
    """Real (non-mock) shadow-tier labeller calls in the trailing 30 days --
    the ``candidate_labels.shadow_tier = 1`` slice of the SAME pool
    ``cost_guard.calls_in_last_30_days`` counts (mechanism 6: one additive
    column, not a second cost-accounting mechanism)."""
    since = timeutils.to_iso(timeutils.now_utc() - timedelta(days=30))
    return journal.count_rows(
        "candidate_labels", "shadow_tier = 1 AND is_mock = 0 AND created_at_utc >= ?", (since,),
    )


def shadow_calls_today(journal) -> int:
    start = journal.start_of_trading_day_utc()
    return journal.count_rows(
        "candidate_labels", "shadow_tier = 1 AND is_mock = 0 AND created_at_utc >= ?", (start,),
    )


def check_shadow_budget(settings, journal, planned_calls: int) -> tuple[bool, str]:
    """EXP-1 mechanism 7: pre-flight -- ``planned_calls`` is computed BEFORE
    the first real call. A would-breach of the shadow 30-day sub-cap, the
    shadow daily cap, OR the shared GLOBAL 30-day cap refuses the WHOLE
    window with zero client invocations (never a partial run that spends
    some calls then gives up partway through). Never raises; fails toward
    "don't run" (same conservative bias as cost_guard.check_scan_budget)."""
    if planned_calls <= 0:
        return True, "no calls planned"
    try:
        used_30d_shadow = shadow_calls_in_last_30_days(journal)
        used_today_shadow = shadow_calls_today(journal)
        used_30d_global = cost_guard.calls_in_last_30_days(journal)
    except Exception as exc:  # noqa: BLE001 - never crash the caller; fail toward "don't run"
        return False, f"error checking shadow AI cost cap: {exc}"

    cap_30d_shadow = settings.shadow_ai_cap_calls_per_30d
    if used_30d_shadow + planned_calls > cap_30d_shadow:
        return False, (
            f"shadow 30-day sub-cap would be exceeded: {used_30d_shadow} used + "
            f"{planned_calls} planned > {cap_30d_shadow} cap -- refusing the whole window"
        )

    cap_today_shadow = settings.shadow_ai_cap_calls_per_day
    if used_today_shadow + planned_calls > cap_today_shadow:
        return False, (
            f"shadow daily cap would be exceeded: {used_today_shadow} used + "
            f"{planned_calls} planned > {cap_today_shadow} cap -- refusing the whole window"
        )

    cap_30d_global = settings.scheduler_ai_cost_cap_calls_per_30d
    if used_30d_global + planned_calls > cap_30d_global:
        return False, (
            f"global shared 30-day AI cost cap would be exceeded: {used_30d_global} used + "
            f"{planned_calls} planned > {cap_30d_global} cap -- refusing the whole window "
            f"(the live evaluator's own share of the shared pool is protected first)"
        )

    return True, (
        f"{used_30d_shadow}+{planned_calls}/{cap_30d_shadow} shadow-30d, "
        f"{used_today_shadow}+{planned_calls}/{cap_today_shadow} shadow-daily, "
        f"{used_30d_global}+{planned_calls}/{cap_30d_global} global-30d"
    )


# --------------------------------------------------------- feed coverage gate
def _daily_feed_coverage_map(journal, trailing_days: int) -> dict:
    """``{market_date_iso: fresh/scanned}`` over the trailing ``trailing_days``
    calendar days, from ``universe_days`` (EXP-0's own survivorship table --
    every shadow-tier symbol requested gets a row regardless of candidate
    status, so this is a true scanned-vs-fresh ratio, not just candidates)."""
    since = (timeutils.market_date() - timedelta(days=trailing_days)).isoformat()
    rows = journal.query(
        "SELECT market_date, "
        "SUM(CASE WHEN freshness_status = 'usable' THEN 1 ELSE 0 END) AS fresh, "
        "COUNT(*) AS scanned FROM universe_days WHERE market_date >= ? GROUP BY market_date",
        (since,),
    )
    return {
        r["market_date"]: (r["fresh"] / r["scanned"] if r["scanned"] else 0.0)
        for r in rows
    }


def check_feed_coverage_gate(journal, settings) -> tuple[bool, str]:
    """EXP-1 mechanism 8: labelling arms only while the trailing 14-day
    MEDIAN daily feed_coverage clears ``SHADOW_LABEL_MIN_FEED_COVERAGE`` --
    checked at RUN time on EVERY tick, never assumed at build/once. No
    history yet is treated as "not cleared" (fail toward not arming), never
    as a free pass."""
    daily = list(_daily_feed_coverage_map(journal, FEED_COVERAGE_TRAILING_DAYS).values())
    if not daily:
        return False, "no universe_days history yet -- feed coverage cannot be assessed, refusing to arm"
    median = statistics.median(daily)
    floor = settings.shadow_label_min_feed_coverage
    if median < floor:
        return False, f"trailing {FEED_COVERAGE_TRAILING_DAYS}-day median feed_coverage {median:.3f} < {floor} required to arm shadow labelling"
    return True, f"trailing {FEED_COVERAGE_TRAILING_DAYS}-day median feed_coverage {median:.3f} >= {floor}"


# -------------------------------------------------------------- auto-suspend
# SUSP-1 (docs/roadmap/alphaos-susp1-canary-aware-suspend-spec.md): the arm
# names returned by ``_canary_confirmation_latch`` below, used verbatim in
# both the suspend reason string and any future report-layer surfacing.
# ``CANARY_CONFIRMATION_STATUS_*`` themselves (the vocabulary CANARY-2's
# ``run_canary_confirmed`` writes) live in ``alphaos.constants`` -- see the
# import block above and that module's own comment (audit-fixup 2026-08,
# MUST FIX 1: previously spelled independently here, in canary/run.py, and
# in reports/canary_report.py; a mutation-tested audit finding proved a
# one-word rename in any single spelling left every existing test green
# while the safety behavior silently inverted).
CANARY_LATCH_ARM_IDENTITY = "identity"
CANARY_LATCH_ARM_CONFIRMED = "confirmed"
CANARY_LATCH_ARM_UNCONFIRMED = "unconfirmed"
CANARY_LATCH_ARM_LEGACY = "legacy-conservative"
# MUST FIX 2 (audit-fixup 2026-08, A HIGH-1 / B HIGH-2, convergent): a
# CROSS-CLASS confirmation -- a TIER_2/label-drift TRIGGER confirmed by a
# TIER_1-severity (identity or failsafe) same-day re-run -- names its own
# arm, distinct from same-tier ``confirmed``, so the reason string is
# legible about which case fired.
CANARY_LATCH_ARM_CONFIRMED_CROSS_CLASS = "confirmed-cross-class"
# ALSO FIX 4 (audit-fixup round 3, 2026-08, B NEW LOW, judged worth fixing):
# an unrecognized/future status value (parses fine, ``confirmation`` is a
# real dict, ``status`` is a real non-None string, but it matches none of
# the four known literals) latched correctly under ``CANARY_LATCH_ARM_LEGACY``
# before this fix -- the right fail direction, but a byte-identical reason
# to a GENUINELY legacy pre-CANARY-2 row. That equivalence is operationally
# dangerous: "legacy-conservative" tells the operator "old row, it ages out
# in ~SHADOW_SUSPEND_CANARY_WINDOW_DAYS days, sit tight" -- but an
# unrecognized status means CURRENT code is writing it every cycle, so the
# row will NEVER age out on its own. This arm exists so the reason string
# can say so explicitly, and name the offending value (the lockstep guard
# in tests/test_susp1_canary_suspend.py closes the SOURCE-level version of
# this gap; it cannot close the DATA-level version -- a row written by an
# older/newer deployment or a hand-edit -- which is what this arm defends).
CANARY_LATCH_ARM_UNRECOGNIZED_STATUS = "unrecognized-status"


def _parse_confirmation_annotation(drift_detail_json: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Best-effort parse of a TRIGGER row's own ``drift_detail_json ->
    confirmation`` sub-dict (written by CANARY-2's ``run_canary_confirmed``).
    Returns ``(status, confirming_drift_tier)`` -- both ``None`` on ANY
    malformed/absent shape (missing key, non-dict value, unparseable JSON,
    empty column). Callers treat a ``None`` status as the conservative-
    default (legacy) case, never as an error -- this function never raises."""
    if not drift_detail_json:
        return None, None
    try:
        detail = json.loads(drift_detail_json)
    except (TypeError, ValueError):
        return None, None
    confirmation = detail.get("confirmation") if isinstance(detail, dict) else None
    if not isinstance(confirmation, dict):
        return None, None
    return confirmation.get("status"), confirmation.get("confirming_drift_tier")


def _canary_confirmation_latch(trigger_tier: str, drift_detail_json: Optional[str]) -> tuple[bool, str]:
    """SUSP-1 latch semantics, clause 2 (+ MUST FIX 2's cross-class
    extension): does a TRIGGER row's own confirmation verdict latch the
    suspend? Returns ``(latches, arm)``.

    Fail direction (honest suspension): only the single explicit
    ``not_confirmed`` status releases a TIER_1 trigger. Every other case --
    ``identity_immediate``, ``confirmed``, ``unconfirmed_page`` (confirmation
    itself could not execute), a missing ``confirmation`` key, a non-dict
    value there, JSON that does not even parse, or a non-None status that
    matches none of the four known literals -- LATCHES. A malformed/
    unparseable ``drift_detail_json`` must never be skipped-because-broken;
    it is treated identically to a legacy pre-CANARY-2 row (no confirmation
    key at all), since both are cases where the system has no proof the trip
    was transient -- BUT a real, non-None, unrecognized status is deliberately
    NOT reported as legacy (``CANARY_LATCH_ARM_UNRECOGNIZED_STATUS``,
    ALSO FIX 4): a legacy row ages out of the recency window on its own; an
    unrecognized status means CURRENT code is writing it every cycle and
    will not.

    A TIER_2 trigger is narrower by design: it latches ONLY the cross-class
    case (MUST FIX 2) -- ``status == confirmed`` AND
    ``confirming_drift_tier == TIER_1`` -- the exact scenario CANARY-2's own
    MUST FIX 1 exists to catch (a label-drift trip confirmed by a same-day
    re-run that turns out to be a genuine identity/failsafe change). Every
    OTHER TIER_2 outcome (not_confirmed, unconfirmed_page, a same-tier
    TIER_2-confirmed, legacy/malformed/unrecognized) is deliberately
    UNCHANGED from pre-SUSP-1 `main` behavior -- the old query never read
    TIER_2 rows at all, and whether a same-tier confirmed TIER_2 should EVER
    suspend is an open operator policy question, recorded but not decided
    here (see the spec's own decision log -- NOT this round)."""
    status, confirming_tier = _parse_confirmation_annotation(drift_detail_json)

    if trigger_tier == DRIFT_TIER_1:
        if status == CANARY_CONFIRMATION_STATUS_NOT_CONFIRMED:
            return False, ""
        if status == CANARY_CONFIRMATION_STATUS_IDENTITY_IMMEDIATE:
            return True, CANARY_LATCH_ARM_IDENTITY
        if status == CANARY_CONFIRMATION_STATUS_CONFIRMED:
            return True, CANARY_LATCH_ARM_CONFIRMED
        if status == CANARY_CONFIRMATION_STATUS_UNCONFIRMED_PAGE:
            return True, CANARY_LATCH_ARM_UNCONFIRMED
        if status is None:
            # Key absent / non-dict confirmation / unparseable JSON / no
            # drift_detail_json at all: genuinely legacy pre-CANARY-2 shape
            # -- conservative default, never silently un-armed by code
            # (operator decision, D2, master reference §9 2026-08-05).
            return True, CANARY_LATCH_ARM_LEGACY
        # A real, non-None status that matches none of the four known
        # literals (ALSO FIX 4): still latches -- same fail direction -- but
        # NOT legacy. Distinguished from the branch above because the
        # operator-facing implication is opposite: legacy ages out, this
        # does not (see the constant's own comment above).
        return True, CANARY_LATCH_ARM_UNRECOGNIZED_STATUS

    # trigger_tier == DRIFT_TIER_2 (the only other tier the caller's query
    # selects): see docstring above -- cross-class-confirmed only.
    if status == CANARY_CONFIRMATION_STATUS_CONFIRMED and confirming_tier == DRIFT_TIER_1:
        return True, CANARY_LATCH_ARM_CONFIRMED_CROSS_CLASS
    return False, ""


def _row_within_window_or_unparseable(started_at_utc: Optional[str], since_dt) -> bool:
    """ALSO FIX (audit-fixup 2026-08, A L1): timestamp-hardening. A NULL/
    empty/unparseable ``started_at_utc`` must be treated as WITHIN the
    window (fail toward suspend), never silently dropped -- an approach that
    filtered rows via a SQL lexical ``started_at_utc >= ?`` compare would let
    a malformed value (``''``, a bare epoch-seconds string, a value with
    leading whitespace) sort as "older than the cutoff" by lexical accident
    and vanish from the result set entirely, violating the spec's own fail
    direction ("never skipped because broken"). Unreachable today
    (``started_at_utc`` is NOT NULL, single writer ``timeutils.stamp().utc``)
    -- hardened by construction, not by luck."""
    if not started_at_utc:
        return True
    parsed = timeutils.parse_iso(started_at_utc)
    if parsed is None:
        return True
    return parsed >= since_dt


def _canary_suspend_arm(journal, settings) -> tuple[bool, str]:
    """SUSP-1 (+ MUST FIX 2): the canary-aware replacement for the old
    unfiltered "ANY historical TIER_1 row latches forever" query. Selects
    recent TRIGGER rows (``confirmation_of IS NULL`` -- clause 1: a
    confirmation RUN row tripping any tier never latches independently, its
    verdict lives on its trigger row's own annotation instead) of EITHER
    TIER_1 or TIER_2 (the cross-class extension needs TIER_2 triggers
    visible too), window-filtered in Python (not SQL -- see
    ``_row_within_window_or_unparseable``), newest first, and returns on the
    first row whose own confirmation status latches (clause 2 /
    ``_canary_confirmation_latch``). A non-latching row does not stop the
    scan -- an OLDER row within the same window can still latch
    independently, since each row is its own event."""
    window_days = settings.shadow_suspend_canary_window_days
    since_dt = timeutils.now_utc() - timedelta(days=window_days)
    rows = journal.query(
        "SELECT run_id, drift_tier, drift_detail_json, started_at_utc FROM canary_runs "
        "WHERE drift_tier IN (?, ?) AND confirmation_of IS NULL "
        "ORDER BY started_at_utc DESC, id DESC",
        (DRIFT_TIER_1, DRIFT_TIER_2),
    )
    for row in rows:
        if not _row_within_window_or_unparseable(row["started_at_utc"], since_dt):
            continue
        latches, arm = _canary_confirmation_latch(row["drift_tier"], row["drift_detail_json"])
        if latches:
            if arm == CANARY_LATCH_ARM_CONFIRMED_CROSS_CLASS:
                tier_note = (
                    f"trigger tier {row['drift_tier']} confirmed by a TIER_1-severity "
                    "same-day re-run"
                )
            elif arm == CANARY_LATCH_ARM_UNRECOGNIZED_STATUS:
                status, _confirming_tier = _parse_confirmation_annotation(row["drift_detail_json"])
                tier_note = (
                    f"trigger tier {row['drift_tier']}, unrecognized confirmation.status={status!r} "
                    "-- NOT a legacy/malformed row (current code is writing this status every "
                    "cycle; unlike a genuinely legacy row, it will never age out of this window "
                    "on its own)"
                )
            else:
                tier_note = f"trigger tier {row['drift_tier']}"
            return True, (
                f"CANARY drift latch [{arm}]: run_id={row['run_id']!r} ({tier_note}), "
                f"started_at_utc={row['started_at_utc']!r}, within the "
                f"{window_days}-day recency window (SHADOW_SUSPEND_CANARY_WINDOW_DAYS) -- "
                "shadow labels flow through the exact same PlaybookClassifier CANARY watches"
            )
    return False, "no auto-suspend condition met"


def check_auto_suspend(journal, settings) -> tuple[bool, str]:
    """EXP-1 mechanism 13 (Autonomy-Ladder pattern: every entry criterion
    pairs with a rollback trigger). Returns (should_suspend, reason).
    Neither trigger self-heals -- the caller engages ``ShadowLabelSuspend
    Switch`` on a True return, which stays engaged until an operator clears
    it explicitly.

    The feed-coverage arm below is untouched by SUSP-1 (spec's own Non-
    goals). The canary arm (``_canary_suspend_arm``) is SUSP-1's own
    replacement for the old unfiltered "ANY historical TIER_1 row latches
    forever" query -- see that function and ``_canary_confirmation_latch``
    for the full latch semantics (docs/roadmap/alphaos-susp1-canary-aware-
    suspend-spec.md)."""
    daily_map = _daily_feed_coverage_map(
        journal, AUTO_SUSPEND_COVERAGE_CONSECUTIVE_DAYS + AUTO_SUSPEND_LOOKBACK_CALENDAR_PADDING_DAYS
    )
    last_n = sorted(daily_map.items())[-AUTO_SUSPEND_COVERAGE_CONSECUTIVE_DAYS:]
    floor = settings.shadow_label_min_feed_coverage
    if len(last_n) == AUTO_SUSPEND_COVERAGE_CONSECUTIVE_DAYS and all(cov < floor for _, cov in last_n):
        return True, (
            f"feed_coverage < {floor} for {AUTO_SUSPEND_COVERAGE_CONSECUTIVE_DAYS} consecutive "
            f"trading days: {last_n}"
        )

    return _canary_suspend_arm(journal, settings)


# -------------------------------------------------------------------- selection
def _tie_break_key(row: dict) -> tuple:
    """Deterministic ranking key: interest_score desc, rel_volume desc, then
    a sha256-of-symbol tiebreak (NOT Python's built-in hash() -- must be
    stable across reruns and PYTHONHASHSEED, per the spec's own test law)."""
    interest = row.get("interest_score") or 0.0
    rel_vol = row.get("unusual_volume") or 0.0
    symbol_hash = int(hashlib.sha256((row.get("symbol") or "").encode("utf-8")).hexdigest(), 16)
    return (interest, rel_vol, symbol_hash)


def select_shadow_shortlist(
    candidates: list[dict], settings, market_date: str, window_label: Optional[str],
) -> list[dict]:
    """EXP-1 mechanism 2: top-K + explore, versioned -- not pure top-K.

    ``candidates`` must already be the DEDUPED pool (symbols already
    labelled today excluded by the caller) for the current window --
    fewer-than-K selects all of them, zero selects zero (zero calls).
    Stamps ``selection_arm`` ('top_k'|'explore') onto each selected dict IN
    PLACE and returns the selected subset (never mutates ``candidates``
    itself beyond that stamp, never reorders the caller's list).
    """
    k = settings.shadow_label_top_k
    if not candidates:
        return []

    ranked = sorted(candidates, key=_tie_break_key, reverse=True)
    top = ranked[:k]
    for row in top:
        row["selection_arm"] = SHADOW_SELECTION_ARM_TOP_K

    below_cut = ranked[k:]
    selected = list(top)
    if below_cut:
        k_explore = max(1, round(settings.shadow_explore_fraction * k))
        # Deterministic seed: sha256("{market_date}:{window}:sel_shadow_v1")
        # (§H.1) -- reproducible across reruns and PYTHONHASHSEED, never
        # Python's own hash()/set ordering.
        seed_str = f"{market_date}:{window_label}:{SHADOW_SELECTION_VERSION_V1}"
        seed_int = int(hashlib.sha256(seed_str.encode("utf-8")).hexdigest(), 16) % (2 ** 32)
        rng = random.Random(seed_int)
        pool = list(below_cut)
        rng.shuffle(pool)
        explore = pool[:k_explore]
        for row in explore:
            row["selection_arm"] = SHADOW_SELECTION_ARM_EXPLORE
        selected = selected + explore

    return selected


def fetch_shadow_selection_pool(journal, market_date: str) -> list[dict]:
    """Shadow-tier candidates whose ``universe_days`` row is dated
    ``market_date`` (EXP-0's own authoritative "which trading day" stamp --
    never derived from created_at_utc, which can straddle a calendar-day
    boundary relative to ET), deduped to the LATEST row per symbol today
    (a persistent name gets a NEW candidates row every window; only the
    freshest is a labelling candidate), excluding symbols ALREADY labelled
    today (mechanism 2's own dedup law -- three windows never triple-pay for
    a persistent name; this is also what makes "backfill from rank K+1"
    happen for free -- exclusion happens before ranking, not after)."""
    already_labelled_today = {
        r["symbol"] for r in journal.query(
            "SELECT DISTINCT cl.symbol FROM candidate_labels cl "
            "JOIN candidates c ON c.candidate_id = cl.candidate_id "
            "JOIN universe_days u ON u.candidate_id = c.candidate_id "
            "WHERE cl.shadow_tier = 1 AND u.market_date = ?",
            (market_date,),
        )
    }
    rows = journal.query(
        "SELECT c.* FROM candidates c "
        "JOIN universe_days u ON u.candidate_id = c.candidate_id "
        "WHERE c.shadow_tier = 1 AND u.market_date = ? "
        "AND c.id = (SELECT MAX(c2.id) FROM candidates c2 "
        "JOIN universe_days u2 ON u2.candidate_id = c2.candidate_id "
        "WHERE c2.shadow_tier = 1 AND c2.symbol = c.symbol AND u2.market_date = ?)",
        (market_date, market_date),
    )
    return [r for r in rows if r["symbol"] not in already_labelled_today]


# ------------------------------------------------------------------ orchestration
def run_shadow_label(orch) -> dict:
    """The SHADOW_LABEL job's full domain logic (mechanisms 2,4,5,7,8,9,13).
    Never raises for any expected/handled condition -- only a genuinely
    unexpected exception propagates to JobRunner.run_job's own wrapper.
    """
    settings = orch.settings
    journal = orch.journal

    if not settings.shadow_labelling_enabled:
        return {"status": "skipped", "reason": "SHADOW_LABELLING_ENABLED is false", "shadow_calls": 0}

    # Mechanism 13: kill switch -> zero shadow calls (inherited, asserted by test).
    if orch.kill_switch.is_engaged():
        return {"status": "skipped", "reason": "kill switch engaged", "shadow_calls": 0}

    suspend_switch = ShadowLabelSuspendSwitch()
    if suspend_switch.is_engaged():
        return {
            "status": "skipped",
            "reason": f"shadow labelling auto-suspended: {suspend_switch.reason()}",
            "shadow_calls": 0,
        }

    should_suspend, suspend_reason = check_auto_suspend(journal, settings)
    if should_suspend:
        suspend_switch.engage(suspend_reason)
        journal.log_system_event(
            Severity.CRITICAL, "shadow_label",
            f"shadow labelling auto-suspended: {suspend_reason}", {"reason": suspend_reason},
        )
        try:
            alerts.send_alert(
                settings, title="AlphaOS: shadow labelling auto-suspended",
                message=suspend_reason, priority="high", journal=journal,
            )
        except Exception:  # noqa: BLE001 - alerting must never compound a suspend with a crash
            pass
        return {"status": "skipped", "reason": f"auto-suspend triggered: {suspend_reason}", "shadow_calls": 0}

    coverage_ok, coverage_detail = check_feed_coverage_gate(journal, settings)
    if not coverage_ok:
        return {"status": "skipped", "reason": coverage_detail, "shadow_calls": 0}

    market_date = timeutils.market_date().isoformat()
    from alphaos.scheduler.cadence import format_hhmm_et, market_now_et, scan_windows, window_containing

    window = window_containing(format_hhmm_et(market_now_et()), scan_windows(settings))
    window_label = f"{window[0]}-{window[1]}" if window else None

    pool = fetch_shadow_selection_pool(journal, market_date)
    if not pool:
        return {"status": "completed", "labelled": 0, "reason": "no unlabelled shadow-tier candidates this window", "shadow_calls": 0}

    selected = select_shadow_shortlist(pool, settings, market_date, window_label)
    if not selected:
        return {"status": "completed", "labelled": 0, "reason": "selection produced zero rows", "shadow_calls": 0}

    within_budget, budget_detail = check_shadow_budget(settings, journal, planned_calls=len(selected))
    if not within_budget:
        journal.log_system_event(
            Severity.WARNING, "shadow_label",
            f"shadow labelling skipped this window: {budget_detail}", {"planned_calls": len(selected)},
        )
        return {"status": "skipped", "reason": budget_detail, "shadow_calls": 0}

    feed_coverage_at_scan = _daily_feed_coverage_map(journal, 1).get(market_date)
    result = orch._label_shadow_shortlist(selected, orch_scan_batch_id(orch), feed_coverage_at_scan)
    return {
        "status": "completed",
        "labelled": result["labelled"],
        "skipped_stale": result["skipped_stale"],
        "errors": result["errors"],
        "selected": len(selected),
        "shadow_calls": result["labelled"],
        "budget_detail": budget_detail,
        "coverage_detail": coverage_detail,
    }


# ------------------------------------------------------------ preregistration
# EXP-1 mechanism 11: exactly TWO preregistrations rows, no framework -- via
# the EXISTING alphaos.stats.preregistration.register_hypothesis(), the same
# function PR12's seeded hypotheses use underneath propose_hypothesis().
H_INT_SHADOW_1_HYPOTHESIS = (
    "Shadow-tier interest-score top decile outperforms the median (twin of "
    "H-INT-1) -- if FALSE, the ranking top-K multiplies is noise and this "
    "feature's cost design collapses."
)
H_INT_SHADOW_1_METRIC = (
    "replay_r (top interest_score decile, shadow_tier=1, instrument_version="
    "'instr1' rows only, never pooled with core) centered against the "
    "shadow-tier population median replay_r"
)
H_AI_SHADOW_1_HYPOTHESIS = (
    "AI adds R at the shadow small/mid band (BASELINE-paired delta) -- "
    "sparse-news AI value could go either way; optional, evaluated only if "
    "resourced."
)
H_AI_SHADOW_1_METRIC = (
    "mean_ai_delta_r = mean(candidate_outcomes.replay_r - "
    "shadow_baseline_decisions.replay_r), shadow_tier=1, threshold_v1"
)
SHADOW_PREREG_FLOOR_EFFECTIVE_N = 20   # clusters (mechanism 11's own floor)
SHADOW_PREREG_FLOOR_SPAN_DAYS = 60


def seed_shadow_preregistrations(journal, now: Optional[object] = None) -> list[str]:
    """Idempotently ensure both EXP-1 preregistration rows exist (check-then-
    register by exact hypothesis+metric text match, mirroring ``alphaos.
    hypotheses.registry._find_baseline_prereg_id``'s own idiom). Returns the
    prereg_id(s) actually CREATED this call (empty if both already existed).
    H-INT-SHADOW-1 is required (evaluate FIRST -- it is self-referential per
    the spec); H-AI-SHADOW-1 is optional but seeded alongside it since
    BASELINE's own arms already cover shadow from day one."""
    from alphaos.stats.preregistration import register_hypothesis

    analysis_not_before = (timeutils.market_date() + timedelta(days=SHADOW_PREREG_FLOOR_SPAN_DAYS)).isoformat()
    created: list[str] = []
    for hypothesis, metric in (
        (H_INT_SHADOW_1_HYPOTHESIS, H_INT_SHADOW_1_METRIC),
        (H_AI_SHADOW_1_HYPOTHESIS, H_AI_SHADOW_1_METRIC),
    ):
        existing = journal.one(
            "SELECT prereg_id FROM preregistrations WHERE hypothesis = ? AND metric = ?",
            (hypothesis, metric),
        )
        if existing:
            continue
        prereg_id = register_hypothesis(
            journal, hypothesis=hypothesis, metric=metric,
            floor_effective_n=SHADOW_PREREG_FLOOR_EFFECTIVE_N,
            floor_span_days=SHADOW_PREREG_FLOOR_SPAN_DAYS,
            analysis_not_before=analysis_not_before,
            params={"instrument_version": "instr1", "shadow_tier": 1},
        )
        created.append(prereg_id)
    return created


def orch_scan_batch_id(orch) -> Optional[str]:
    """The most recent scan_batch_id today -- shadow labels are stamped
    against the batch they're scoped to for audit-trail consistency with
    every other candidate_labels row, even though SHADOW_LABEL is its own
    job type (mechanism 4) and does not create scan_batches rows itself."""
    row = orch.journal.one("SELECT scan_batch_id FROM scan_batches ORDER BY id DESC LIMIT 1")
    return row["scan_batch_id"] if row else None
