"""S1c: operational observability for the PER-card selector -- the
smallest visibility the daily brief needs into what
``alphaos.cards.activation`` actually decided each scan, split by outcome
and tier.

PURE READ over ``candidates`` rows -- writes nothing, computes nothing
that feeds back into any decision. Zero/low PER counts outside earnings
season are a normal, expected state and are never themselves an error
(see ``render_markdown()``'s own framing below) -- this module reports
COUNTS only, never a verdict or a performance conclusion. The formal
H-PER-1P/H-PER-1N verdict is a separate, operator-invoked, one-shot
evaluation (``alphaos.stats.preregistration.evaluate_two_arm_hypothesis_
pair()``); nothing here anticipates, previews, or substitutes for it.
"""

from __future__ import annotations

from alphaos.cards.activation import ACTIVATION_ERROR_STATUS, PREFLIGHT_FAILED_STATUS
from alphaos.cards.selector import CacheHealth, PER_CARD_ID

# Every degraded card_assignment_status value this report distinguishes --
# the selector's own CacheHealth values plus the activation layer's own
# preflight-failure and unexpected-error statuses. An explicit, ordered
# tuple (not `SELECT DISTINCT`) so a status this report has never seen
# still surfaces loudly (via n_total's own reconciliation) rather than
# silently vanishing.
_KNOWN_DEGRADED_STATUSES = (
    CacheHealth.REFRESH_FAILED_RECENT, CacheHealth.STALE,
    CacheHealth.CACHE_EMPTY, CacheHealth.UNKNOWN, PREFLIGHT_FAILED_STATUS,
    ACTIVATION_ERROR_STATUS,
)


def _count(journal, since_utc: str, extra_where: str, extra_params: tuple = ()) -> int:
    return journal.count_rows(
        "candidates", f"created_at_utc >= ? AND {extra_where}", (since_utc, *extra_params),
    )


def build_per_selector_report(journal, since_utc: str) -> dict:
    """S1c assignment counts since ``since_utc`` -- the caller passes the
    same trading-day boundary ``_todays_activity`` (daily_brief.py) uses,
    so this reads as "today", consistent with the rest of the brief."""
    per_ok = _count(journal, since_utc, "card_id = ? AND card_assignment_status = 'ok'", (PER_CARD_ID,))
    core_per = _count(
        journal, since_utc,
        "card_id = ? AND card_assignment_status = 'ok' AND COALESCE(shadow_tier, 0) = 0", (PER_CARD_ID,),
    )
    shadow_per = _count(
        journal, since_utc,
        "card_id = ? AND card_assignment_status = 'ok' AND COALESCE(shadow_tier, 0) = 1", (PER_CARD_ID,),
    )
    default_ok = _count(journal, since_utc, "card_id != ? AND card_assignment_status = 'ok'", (PER_CARD_ID,))
    degraded_by_status = {
        status: _count(journal, since_utc, "card_assignment_status = ?", (status,))
        for status in _KNOWN_DEGRADED_STATUSES
    }
    # Rows with no card_assignment_status at all -- pre-S1c-activation
    # candidates, or a candidate created through a call path that doesn't
    # pass a ScanCardActivation (e.g. seed_demo). Kept for reconciliation,
    # not surfaced in the markdown (not actionable on its own).
    unstamped = _count(journal, since_utc, "card_assignment_status IS NULL")
    # Shadow-tier rows carry their own scan_window label (core rows leave
    # it NULL) -- already-supported breakdown per the operator's own
    # observability requirement; kept in the report dict for anyone
    # querying it programmatically, not rendered in the terse markdown
    # below (no new dashboard).
    window_rows = journal.query(
        "SELECT scan_window, COUNT(*) AS n FROM candidates "
        "WHERE created_at_utc >= ? AND card_id = ? AND card_assignment_status = 'ok' "
        "AND scan_window IS NOT NULL GROUP BY scan_window",
        (since_utc, PER_CARD_ID),
    )
    per_by_scan_window = {r["scan_window"]: r["n"] for r in window_rows}

    return {
        "since_utc": since_utc,
        "per_assignments": per_ok,
        "core_per_assignments": core_per,
        "shadow_per_assignments": shadow_per,
        "default_healthy": default_ok,
        "degraded_by_status": degraded_by_status,
        "per_by_scan_window": per_by_scan_window,
        "unstamped": unstamped,
        "n_total": per_ok + default_ok + sum(degraded_by_status.values()) + unstamped,
    }


def render_markdown(rep: dict) -> str:
    lines = [
        "## SETUP-1 S1c -- PER-card selector activity (evidence metadata only; "
        "zero/low counts outside earnings season are expected, never an error)",
        f"- PER-tagged today: **{rep['per_assignments']}** "
        f"(core={rep['core_per_assignments']}, shadow={rep['shadow_per_assignments']})",
        f"- Default (checked, not eligible): {rep['default_healthy']}",
    ]
    degraded_total = sum(rep["degraded_by_status"].values())
    if degraded_total:
        parts = ", ".join(f"{k}={v}" for k, v in rep["degraded_by_status"].items() if v)
        lines.append(f"- ⚠️ Degraded (selector could not evaluate): {degraded_total} ({parts})")
    return "\n".join(lines)
