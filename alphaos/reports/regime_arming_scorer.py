"""REG-1: the shadow arming-map scorer -- the earn-its-existence instrument
for REG-2 (a separate, later, evidence-gated PR that would actually arm/
disarm cards by regime). Pure ledger math over EXISTING shadow rows
(``candidate_outcomes.replay_r`` joined to ``candidate_packets.regime`` by
``candidate_id``) -- nothing is armed or disarmed in reality here. No gate/
eval/risk/execution path may read this module's output; it exists purely to
accumulate the evidence REG-2 will later be judged on.

PRE-REGISTRATION BLOCK (paste into the PR description, per the spec):
  Hypothesis: per-map arming improves a card's expectancy by >= +0.1R over
    always-armed (paired comparison, same card, same underlying replay_r
    values -- just partitioned differently).
  Metric: paired replay ΔR per card = mean(replay_r | armed_per_map subset)
    - mean(replay_r | all resolved rows for that card).
  Floors: effective-N per regime per card -- minimum 2 DISTINCT regime
    episodes (contiguous same-regime day-runs, not just 2 rows) represented
    in the armed_per_map subset. Below floor, the card's row is still shown
    (counts only) but delta_r is withheld, matching this codebase's
    established floor-gating convention (attribution v2, TQS).
  Analysis-not-before date: 2026-09-07 (~60 days from REG-1's build date --
    a literal, non-sliding pre-registered checkpoint, not "60 days from
    whenever this runs"). Before that date the report renders a loud
    not-yet-for-decisions caveat regardless of what the numbers say.

THE ONE HARD-CODED RULE THAT SITS OUTSIDE THE MAP (per spec): CRISIS is
never armed for ANY card under armed_per_map, even if a future card's own
map entry claimed otherwise -- this is a risk rule protecting the account,
not something that waits for statistical proof (mirrors REG-2's own stub
spec: "the one hard-coded exception -- CRISIS => all cards stand down").
"""

from __future__ import annotations

from typing import Optional

# Pre-registered v1 candidate map (versioned like a card -- a change here is
# a new REGIME_ARMING_MAP_V2, never a silent edit to v1's meaning once real
# analysis has started). momentum cards -> TREND_UP only.
REGIME_ARMING_MAP_V1 = {
    "catalyst_momentum_v1": {"TREND_UP"},
}
REGIME_ARMING_MAP_VERSION = "regime_arming_map_v1"

MIN_DISTINCT_REGIME_EPISODES = 2
ANALYSIS_NOT_BEFORE_DATE = "2026-09-07"


def _mean(xs: list) -> Optional[float]:
    return (sum(xs) / len(xs)) if xs else None


def _is_armed_per_map(card_id: str, regime: Optional[str]) -> bool:
    if regime == "CRISIS":
        return False  # hard-coded, never map-overridable -- see module docstring
    if regime is None:
        return False  # unknown regime -- never treated as armed (unknown != safe)
    return regime in REGIME_ARMING_MAP_V1.get(card_id, set())


def _count_distinct_episodes(dates: list) -> int:
    """Count distinct contiguous-day episodes represented by ``dates`` (a
    list of "YYYY-MM-DD" strings, any order, possibly with duplicates).
    Two dates are the same episode only if EVERY calendar day between them
    (inclusive) is also present in ``dates`` -- a gap of even one day starts
    a new episode. This is a narrow, purpose-built floor for this report
    only; PORT-1's general effective_n() (clustering ANY same-regime
    consecutive days across the whole system) supersedes this once it
    lands -- see module docstring's floors."""
    from datetime import date as _date, timedelta as _timedelta

    unique_sorted = sorted({_date.fromisoformat(d) for d in dates if d})
    if not unique_sorted:
        return 0
    episodes = 1
    for i in range(1, len(unique_sorted)):
        if (unique_sorted[i] - unique_sorted[i - 1]) > _timedelta(days=1):
            episodes += 1
    return episodes


def compute_regime_arming_scores(rows: list) -> dict:
    """Pure function -- no I/O. ``rows``: list of ``{"card_id", "regime",
    "replay_r", "market_date"}`` (already resolved, replay_r not null,
    regime not null -- callers filter before calling). Returns
    ``{"cards": [{"card_id", "n_all", "mean_r_armed_always", "n_armed_per_map",
    "mean_r_armed_per_map", "delta_r", "distinct_episodes_armed",
    "floor_met"}], "arming_map_version", "analysis_not_before",
    "analysis_ready"}``.
    """
    by_card: dict = {}
    for r in rows:
        by_card.setdefault(r["card_id"], []).append(r)

    cards = []
    for card_id, card_rows in sorted(by_card.items()):
        all_r = [r["replay_r"] for r in card_rows]
        armed_rows = [r for r in card_rows if _is_armed_per_map(card_id, r["regime"])]
        armed_r = [r["replay_r"] for r in armed_rows]
        episodes_by_regime: dict = {}
        for r in armed_rows:
            episodes_by_regime.setdefault(r["regime"], []).append(r["market_date"])
        distinct_episodes = {
            regime: _count_distinct_episodes(dates) for regime, dates in episodes_by_regime.items()
        }
        floor_met = bool(distinct_episodes) and all(
            n >= MIN_DISTINCT_REGIME_EPISODES for n in distinct_episodes.values()
        )
        mean_always = _mean(all_r)
        mean_armed = _mean(armed_r)
        cards.append({
            "card_id": card_id,
            "n_all": len(all_r),
            "mean_r_armed_always": mean_always,
            "n_armed_per_map": len(armed_r),
            "mean_r_armed_per_map": mean_armed,
            "delta_r": (
                (mean_armed - mean_always)
                if (floor_met and mean_always is not None and mean_armed is not None)
                else None
            ),
            "distinct_episodes_by_regime": distinct_episodes,
            "floor_met": floor_met,
        })

    return {
        "arming_map_version": REGIME_ARMING_MAP_VERSION,
        "analysis_not_before": ANALYSIS_NOT_BEFORE_DATE,
        "cards": cards,
    }


def build_regime_arming_report(journal, settings, limit: int = 2000) -> dict:
    """Journal-facing entry point. Joins resolved candidate_outcomes ->
    candidate_packets (by candidate_id, for the regime stamp) -> candidates
    (by candidate_id, for card_id). PURE READ. Never called from any gate/
    eval/risk/execution path."""
    from alphaos.util import timeutils

    rows = journal.query(
        "SELECT c.card_id, p.regime, o.replay_r, p.created_at_utc AS packet_created_at_utc "
        "FROM candidate_outcomes o "
        "JOIN candidate_packets p ON p.candidate_id = o.candidate_id "
        "JOIN candidates c ON c.candidate_id = o.candidate_id "
        "WHERE o.replay_r IS NOT NULL AND o.outcome_status = 'resolved' "
        "AND p.regime IS NOT NULL AND c.card_id IS NOT NULL "
        "ORDER BY o.id DESC LIMIT ?",
        (limit,),
    )
    # market_date for episode-counting: derive from the packet's own
    # created_at_utc via the SAME timeutils.market_date() every other date
    # derivation in this codebase uses (never a naive UTC-date truncation).
    prepared = []
    for r in rows:
        dt = timeutils.parse_iso(r["packet_created_at_utc"])
        market_date = timeutils.market_date(dt).isoformat() if dt else None
        prepared.append({
            "card_id": r["card_id"], "regime": r["regime"],
            "replay_r": r["replay_r"], "market_date": market_date,
        })

    result = compute_regime_arming_scores(prepared)
    today = timeutils.market_date().isoformat()
    result["analysis_ready"] = today >= ANALYSIS_NOT_BEFORE_DATE
    return result


def render_markdown(rep: dict) -> str:
    lines = [
        "## Shadow arming-map scorer (REG-2 evidence instrument -- nothing armed for real)",
        f"Map: `{rep['arming_map_version']}` -- analysis not before "
        f"`{rep['analysis_not_before']}`"
        + ("" if rep.get("analysis_ready") else " (NOT YET REACHED -- descriptive only)"),
        "",
    ]
    if not rep["cards"]:
        lines.append("- (no resolved, regime-stamped shadow rows yet)")
        return "\n".join(lines)
    for c in rep["cards"]:
        if c["delta_r"] is not None:
            lines.append(
                f"- {c['card_id']}: armed_always={c['mean_r_armed_always']:.3f}R "
                f"(n={c['n_all']}) vs armed_per_map={c['mean_r_armed_per_map']:.3f}R "
                f"(n={c['n_armed_per_map']}) -> ΔR={c['delta_r']:+.3f}"
            )
        else:
            lines.append(
                f"- {c['card_id']}: below floor ({MIN_DISTINCT_REGIME_EPISODES}+ distinct "
                f"regime episodes needed per armed state) -- counts only: "
                f"n_all={c['n_all']}, n_armed_per_map={c['n_armed_per_map']}, "
                f"episodes={c['distinct_episodes_by_regime']}"
            )
    return "\n".join(lines)
