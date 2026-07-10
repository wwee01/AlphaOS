"""PR13 slice 1: the per-card scoreboard -- PURE READ over already-journaled
``candidate_outcomes``/``candidates``/``setup_cards``. Never writes anything
(that is ``alphaos/cards/demotion.py``'s job); never read by any gate/eval/
labeller/risk/execution path -- purely descriptive, exactly like
``alphaos/reports/baseline_report.py``/``regime_arming_scorer.py``.

DELIBERATELY does NOT go through ``alphaos.stats.preregistration``'s
``register_hypothesis()``/``evaluate_hypothesis()``: those exist to freeze
evidence EXACTLY ONCE for a formal, cited hypothesis (PORT-1's anti-
optional-stopping law). A card's own scoreboard is the opposite shape -- an
OPERATIONAL health monitor that re-evaluates every day, on purpose, the
same way a dashboard metric or a fuse check does. It calls
``alphaos.stats.effective_n``/``alphaos.stats.bootstrap`` directly (the same
underlying primitives, reused, never a second implementation), the same way
``baseline_report.py`` calls ``day_block_bootstrap()`` directly for its own
still-descriptive report.

Scope for slice 1: only ``state='live_eligible'`` cards are scored (a
``shadow`` card cannot be "demoted" -- it was never live to begin with;
promotion-readiness for shadow cards is slice 2's own, differently-floored
concern, per PR13.5). Only ``candidate_type IN ('proposal', 'blocked')``
candidate_outcomes rows count -- rows where the card's own entry rule said
"trade this," not diluted by 'reject'/'armed_watch'/'candidate' rows the
card merely noticed but never acted on.
"""

from __future__ import annotations

from alphaos.reports.attribution import (
    MIN_RESOLVED_FOR_V2_AGGREGATE,
    MIN_SPAN_DAYS_FOR_V2_AGGREGATE,
)
from alphaos.stats.bootstrap import clustered_bootstrap
from alphaos.stats.effective_n import effective_n

# Reused verbatim from attribution.py (one-floor law) -- the SAME bar PR12's
# own Class A risk floor already reuses for the identical reason.
MIN_RESOLVED = MIN_RESOLVED_FOR_V2_AGGREGATE
MIN_SPAN_DAYS = MIN_SPAN_DAYS_FOR_V2_AGGREGATE


def live_eligible_cards(journal) -> list[dict]:
    """Every (card_id, version) currently registered with state='live_eligible'
    that has NOT already been terminally demoted (a demoted version is never
    re-scored -- re-entry requires a brand new version, per the anti-double-
    jeopardy law; scoring it again would be pointless and could look like a
    second, contradictory verdict on the same terminal fact)."""
    return journal.query(
        "SELECT sc.card_id, sc.version AS card_version, sc.state "
        "FROM setup_cards sc "
        "WHERE sc.state = 'live_eligible' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM card_demotions cd "
        "  WHERE cd.card_id = sc.card_id AND cd.card_version = sc.version"
        ")"
    )


def _card_replay_r_rows(journal, card_id: str, card_version: int) -> list[dict]:
    """One row per candidate this card produced a proposal/blocked decision
    for, shaped for effective_n()/clustered_bootstrap() (symbol/decision_date/
    max_holding_days/replay_r). Dedupes candidate_outcomes to the single most
    recent non-user_override row per candidate_id -- the SAME fan-out fix a
    correctness audit applied to PR12's own queries.py (candidate_outcomes is
    one row per (candidate_id, candidate_type); a human-overridden candidate
    carries a second, parallel row that must never be double-counted here
    either)."""
    return journal.query(
        "SELECT c.symbol, co.replay_r, co.decision_at_utc, tp.max_holding_days "
        "FROM candidates c "
        "JOIN candidate_outcomes co ON co.id = ("
        "  SELECT co2.id FROM candidate_outcomes co2 "
        "  WHERE co2.candidate_id = c.candidate_id AND co2.candidate_type != 'user_override' "
        "  ORDER BY co2.id DESC LIMIT 1"
        ") "
        "LEFT JOIN trade_proposals tp ON tp.id = ("
        "  SELECT tp2.id FROM trade_proposals tp2 "
        "  WHERE tp2.candidate_id = c.candidate_id ORDER BY tp2.id DESC LIMIT 1"
        ") "
        "WHERE c.card_id = ? AND c.card_version = ? "
        "AND co.candidate_type IN ('proposal', 'blocked') "
        "AND co.replay_r IS NOT NULL",
        (card_id, card_version),
    )


def compute_card_scoreboard(journal, card_id: str, card_version: int) -> dict:
    """One card's current scoreboard, computed fresh. Returns:
    ``{"card_id", "card_version", "expectancy_r", "ci_low", "ci_high",
    "effective_n", "n_raw", "span_days", "clears_floor", "breach"}``.
    ``breach`` is True only when the card clears ITS OWN floor (never
    computed on an untrustworthy sample) AND the clustered-bootstrap CI is
    RELIABLY below zero (``ci_high < 0``) -- a raw negative point estimate
    alone is not enough; that would flag noise, not signal."""
    rows = _card_replay_r_rows(journal, card_id, card_version)
    en = effective_n(
        [{**r, "decision_date": (r["decision_at_utc"] or "")[:10]} for r in rows],
    )
    boot = clustered_bootstrap(en["clusters"], "replay_r")
    clears_floor = en["effective_n"] >= MIN_RESOLVED and (en["span_days"] or 0) >= MIN_SPAN_DAYS
    breach = bool(clears_floor and boot["ci_high"] is not None and boot["ci_high"] < 0)
    return {
        "card_id": card_id,
        "card_version": card_version,
        "expectancy_r": boot["point_estimate"],
        "ci_low": boot["ci_low"],
        "ci_high": boot["ci_high"],
        "effective_n": en["effective_n"],
        "n_raw": en["n_raw"],
        "span_days": en["span_days"],
        "clears_floor": clears_floor,
        "breach": breach,
    }


def demoted_cards(journal) -> list[dict]:
    """Every (card_id, version) ever demoted, most recent first -- scope/
    safety-audit MEDIUM: `live_eligible_cards()` permanently excludes a
    demoted version, so without this a demoted card would silently vanish
    from the scoreboard with no standing surface reflecting it (the alert
    is a one-time push notification, easy to miss). Mirrors
    ``alphaos.reports.daily_brief.SURVIVORSHIP_DENOMINATOR_CAVEAT``'s own
    law: "any report claiming system-level edge must print the FULL
    preregistration family (promoted + demoted + withdrawn), never just a
    promoted subset -- otherwise a reader sees only the survivors and
    mistakes selection for edge." Same principle, applied to cards."""
    return journal.query(
        "SELECT card_id, card_version, reason, demoted_at_utc FROM card_demotions "
        "ORDER BY id DESC"
    )


def build_card_scoreboard_report(journal) -> dict:
    """Every live_eligible, not-yet-demoted card's current scoreboard, PURE
    READ (does not consult or write ``card_scoreboard_snapshots`` -- that
    table is the demotion mechanism's own persisted history; this report
    always recomputes from the live ledger, same "always fresh" posture as
    every other report in this codebase). Also surfaces the full historical
    demoted roster (see ``demoted_cards()``'s own docstring) -- a demoted
    card is never just silently absent from this report."""
    cards = live_eligible_cards(journal)
    scored = [compute_card_scoreboard(journal, c["card_id"], c["card_version"]) for c in cards]
    demoted = demoted_cards(journal)
    return {
        "n_cards": len(scored),
        "n_breaching": sum(1 for s in scored if s["breach"]),
        "cards": scored,
        "demoted": demoted,
        "n_demoted": len(demoted),
        "floor_effective_n": MIN_RESOLVED,
        "floor_span_days": MIN_SPAN_DAYS,
    }


def render_markdown(rep: dict) -> str:
    lines = [
        "## PR13 -- per-card scoreboard (shadow measurement; demotion is the "
        "only automated action, never promotion)",
        f"{rep['n_cards']} live_eligible card(s) scored, {rep['n_breaching']} "
        f"currently breaching (floor: {rep['floor_effective_n']}+ effective_n, "
        f"{rep['floor_span_days']}+ day span), {rep['n_demoted']} demoted historically",
        "",
    ]
    for s in rep["cards"]:
        if not s["clears_floor"]:
            lines.append(
                f"- {s['card_id']} v{s['card_version']}: below floor -- "
                f"effective_n={s['effective_n']}, span_days={s['span_days']} "
                "(counts only, no expectancy shown)"
            )
            continue
        marker = " ⚠️ BREACH" if s["breach"] else ""
        lines.append(
            f"- {s['card_id']} v{s['card_version']}: expectancy={s['expectancy_r']:+.4f}R "
            f"[{s['ci_low']:+.4f}, {s['ci_high']:+.4f}] "
            f"(effective_n={s['effective_n']}, span={s['span_days']}d){marker}"
        )
    if rep["demoted"]:
        lines += ["", "### Demoted (terminal -- only a new version can re-enter live_eligible)"]
        for d in rep["demoted"]:
            lines.append(f"- {d['card_id']} v{d['card_version']}: demoted {d['demoted_at_utc']} -- {d['reason']}")
    return "\n".join(lines)
