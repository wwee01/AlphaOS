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
import random
import statistics
from datetime import timedelta
from typing import Optional

from alphaos.constants import (
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
def check_auto_suspend(journal, settings) -> tuple[bool, str]:
    """EXP-1 mechanism 13 (Autonomy-Ladder pattern: every entry criterion
    pairs with a rollback trigger). Returns (should_suspend, reason).
    Neither trigger self-heals -- the caller engages ``ShadowLabelSuspend
    Switch`` on a True return, which stays engaged until an operator clears
    it explicitly."""
    daily_map = _daily_feed_coverage_map(journal, AUTO_SUSPEND_COVERAGE_CONSECUTIVE_DAYS + 2)
    last_n = sorted(daily_map.items())[-AUTO_SUSPEND_COVERAGE_CONSECUTIVE_DAYS:]
    floor = settings.shadow_label_min_feed_coverage
    if len(last_n) == AUTO_SUSPEND_COVERAGE_CONSECUTIVE_DAYS and all(cov < floor for _, cov in last_n):
        return True, (
            f"feed_coverage < {floor} for {AUTO_SUSPEND_COVERAGE_CONSECUTIVE_DAYS} consecutive "
            f"trading days: {last_n}"
        )

    tier1 = journal.one("SELECT run_id FROM canary_runs WHERE drift_tier = 'TIER_1' ORDER BY id DESC LIMIT 1")
    if tier1:
        return True, (
            f"CANARY Tier-1 drift event detected (run_id={tier1['run_id']!r}) -- shadow labels "
            "flow through the exact same PlaybookClassifier CANARY watches"
        )

    return False, "no auto-suspend condition met"


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
