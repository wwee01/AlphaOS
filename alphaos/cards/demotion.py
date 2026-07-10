"""PR13 slice 1: the daily card-demotion mechanism -- writes ONE
``card_scoreboard_snapshots`` row per (card_id, version) per SGT calendar
day, then demotes (writes a ``card_demotions`` row + fires an alert) once a
card has clocked >= 2 CONSECUTIVE breach snapshots (audit A2 -- a
sequential-test crumb against single-night noise, never a single bad day).

The "consecutive" streak is computed by re-querying
``card_scoreboard_snapshots`` history, most-recent-first -- the SAME "no
separate counter column, count the streak from history" idiom
``alphaos.scheduler.cadence.is_fused()`` already uses for ``job_runs``.

Demotion is TERMINAL (anti-double-jeopardy law, spec audit B3): once a
``card_demotions`` row exists for a (card_id, version), that exact version
is never re-scored or re-demoted again -- ``scoreboard.live_eligible_cards()``
already excludes it. Slice 2 (promotion, PR13/PR13.5) is out of scope here;
this module only ever demotes, never promotes, un-demotes, or writes to
``setup_cards``/card YAML (Prime Directive 7 -- only an operator-committed
version bump changes a card's own behavior).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from alphaos.cards.scoreboard import compute_card_scoreboard, live_eligible_cards
from alphaos.util import alerts, timeutils
from alphaos.util.ids import new_id

# Audit A2: a breach must persist this many CONSECUTIVE daily snapshots
# before it demotes anything -- one bad day is noise, not evidence.
MIN_CONSECUTIVE_BREACHES_TO_DEMOTE = 2


def _write_snapshot(journal, card_id: str, card_version: int, state: str, score: dict, evaluation_date: str) -> Optional[str]:
    """Idempotent insert -- same "attempt every tick, let the DB reject a
    same-day repeat" idiom as idx_universe_days_symbol_date. Returns the new
    snapshot_id, or None if today's snapshot for this (card, version)
    already exists (a re-run within the same SGT day is a no-op, not an
    error)."""
    existing = journal.one(
        "SELECT snapshot_id FROM card_scoreboard_snapshots "
        "WHERE card_id = ? AND card_version = ? AND evaluation_date = ?",
        (card_id, card_version, evaluation_date),
    )
    if existing:
        return None
    snapshot_id = new_id("cardsnap")
    journal.insert("card_scoreboard_snapshots", {
        "snapshot_id": snapshot_id,
        "card_id": card_id,
        "card_version": card_version,
        "evaluation_date": evaluation_date,
        "state": state,
        "expectancy_r": score["expectancy_r"],
        "ci_low": score["ci_low"],
        "ci_high": score["ci_high"],
        "effective_n": score["effective_n"],
        "n_raw": score["n_raw"],
        "span_days": score["span_days"],
        "clears_floor": score["clears_floor"],
        "breach": score["breach"],
    })
    return snapshot_id


def _consecutive_breach_streak(journal, card_id: str, card_version: int, limit: int) -> list[dict]:
    """The most recent `limit` snapshots for (card_id, version), most-recent
    first -- callers count the leading breach=1 streak themselves (mirrors
    cadence.is_fused()'s own query-then-count-in-Python shape exactly).

    Ordering by `id DESC` (not `evaluation_date DESC`) assumes snapshot ids
    are monotonic with evaluation_date -- true today because both real call
    sites (the scheduler job and the orchestrator method) always evaluate
    at the real current time, and no backfill/re-insert path exists. A
    future feature that ever inserts a snapshot for a PAST date would need
    to order by `evaluation_date DESC` instead, or this streak check would
    silently read the wrong 2 rows."""
    return journal.query(
        "SELECT snapshot_id, breach FROM card_scoreboard_snapshots "
        "WHERE card_id = ? AND card_version = ? ORDER BY id DESC LIMIT ?",
        (card_id, card_version, limit),
    )


def _maybe_demote(journal, settings, card_id: str, card_version: int) -> Optional[dict]:
    """Checks the last MIN_CONSECUTIVE_BREACHES_TO_DEMOTE snapshots; demotes
    (writes card_demotions + fires an alert) iff ALL of them breached AND
    this (card_id, version) has never been demoted before. Returns the new
    demotion row, or None if no demotion happened this call."""
    recent = _consecutive_breach_streak(journal, card_id, card_version, MIN_CONSECUTIVE_BREACHES_TO_DEMOTE)
    if len(recent) < MIN_CONSECUTIVE_BREACHES_TO_DEMOTE:
        return None
    if not all(r["breach"] for r in recent):
        return None

    already_demoted = journal.one(
        "SELECT 1 FROM card_demotions WHERE card_id = ? AND card_version = ?",
        (card_id, card_version),
    )
    if already_demoted:
        return None

    reason = (
        f"{MIN_CONSECUTIVE_BREACHES_TO_DEMOTE} consecutive daily scoreboard "
        f"snapshots with a reliably-negative expectancy (CI fully below zero)"
    )
    now = timeutils.stamp()
    demotion_id = new_id("carddemo")
    try:
        journal.insert("card_demotions", {
            "demotion_id": demotion_id,
            "card_id": card_id,
            "card_version": card_version,
            "reason": reason,
            "triggering_snapshot_id_1": recent[0]["snapshot_id"],
            "triggering_snapshot_id_2": recent[1]["snapshot_id"],
            "alert_sent": False,
            "demoted_at_utc": now.utc,
            "demoted_at_sgt": now.local_sgt,
        })
    except Exception:
        # Lost a race against a concurrent evaluator for this (card_id,
        # version) -- idx_card_demotions_card_version already caught it and
        # someone else's demotion row is authoritative; never demote twice.
        return None

    # Correctness-audit LOW: alert AFTER the insert wins, never before --
    # a concurrent evaluator (e.g. a manual CLI run overlapping the daily
    # scheduler job, which bypasses the job_runs lock) could otherwise both
    # pass the already_demoted check and both page, even though only one
    # insert can ever win. Only the row that actually landed pages, matching
    # job_runner.py's own _alert_job_failure ordering (record durably, then
    # alert -- never the reverse).
    sent = alerts.send_alert(
        settings,
        title=f"AlphaOS card demoted: {card_id} v{card_version}",
        message=f"{card_id} v{card_version} demoted -- {reason}.",
        priority="high",
        journal=journal,
    )
    if sent:
        journal.conn.execute(
            "UPDATE card_demotions SET alert_sent = ? WHERE demotion_id = ?", (True, demotion_id),
        )
        journal.conn.commit()
    return journal.one("SELECT * FROM card_demotions WHERE demotion_id = ?", (demotion_id,))


def run_daily_card_evaluation(journal, settings, now: Optional[datetime] = None) -> dict:
    """One daily pass: snapshot every live_eligible, not-yet-demoted card,
    then check each for a fresh demotion. Never raises -- a bug scoring one
    card must never block the others (per-item failure isolation, matching
    every other scheduler job in this codebase)."""
    evaluation_date = timeutils.stamp(now).local_sgt[:10]
    summary: dict = {"snapshotted": [], "already_snapshotted_today": [], "demoted": [], "errors": []}

    for card in live_eligible_cards(journal):
        card_id, card_version = card["card_id"], card["card_version"]
        try:
            score = compute_card_scoreboard(journal, card_id, card_version)
            snapshot_id = _write_snapshot(
                journal, card_id, card_version, card["state"], score, evaluation_date,
            )
            if snapshot_id is None:
                summary["already_snapshotted_today"].append(card_id)
                continue
            summary["snapshotted"].append(card_id)

            demotion = _maybe_demote(journal, settings, card_id, card_version)
            if demotion is not None:
                summary["demoted"].append({"card_id": card_id, "card_version": card_version})
        except Exception as exc:  # never let one card's bug block the others
            summary["errors"].append({"card_id": card_id, "error": str(exc)})

    return summary
