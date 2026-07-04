"""Journal-aware orchestration for the counterfactual outcome ledger.

Two phases, both idempotent and both safe to call repeatedly / on a schedule:

* ``seed_pending_outcomes`` — finds candidates/proposals/rejects/armed-watch
  rows and user-decision-overrides that don't have a ``candidate_outcomes``
  row yet, and creates one each (status ``pending``). PURE READ of existing
  decision tables + one INSERT per new row; never touches the decision tables
  themselves, never influences scanning/eval/labelling/risk/execution.
* ``update_pending_outcomes`` — for rows still ``pending``/``partial``, fetches
  bars observed AFTER the decision and computes forward 1/3/5-day returns +
  bracket replay (see ``outcomes_engine``). Write-only to
  ``candidate_outcomes``; never reads back into any trading decision.

Both use SQL ``NOT EXISTS`` / status filters to only touch un-worked rows, so
re-running converges rather than reprocessing.
"""

from __future__ import annotations

from typing import Optional

from alphaos.learning.outcomes_engine import forward_window_stats, replay_bracket
from alphaos.util import timeutils
from alphaos.util.ids import new_id

# AlphaOS-side classification a candidate can resolve to (the "primary" row,
# one per candidate_id). 'user_override' is seeded separately, in parallel.
_ALPHAOS_SIDE_TYPES = ("proposal", "blocked", "armed_watch", "reject", "candidate")

# If we still have zero forward bars this many calendar days after a decision
# was recorded, treat it as genuinely unavailable (not a transient gap) so the
# row converges instead of being retried forever.
UNAVAILABLE_AFTER_DAYS = 15.0


# --------------------------------------------------------------------- seed
#
# candidate_type is a SNAPSHOT frozen at first seed, not a live view: if a
# 'candidate' or 'reject' later grows a proposal (e.g. via a user override),
# the ORIGINAL row keeps its original type — a separate 'user_override' row
# captures the new path in parallel. This is deliberate (each row is a fixed
# counterfactual observation), not a bug.
def _classify_candidate(journal, cand: dict) -> dict:
    """AlphaOS-side classification + level/decision sourcing for one candidate.
    Priority: proposal(blocked) > proposal > armed_watch > reject > candidate.
    ``decision_at_utc`` is the SOURCE row's own timestamp (proposal/reject/
    decision_adjustments/candidate) — the actual moment AlphaOS decided —
    which is what forward outcomes must anchor on, NOT when this
    candidate_outcomes row happens to get seeded (that can lag, e.g. when
    catching up on a backlog)."""
    candidate_id = cand["candidate_id"]
    ev = journal.evaluation_for_candidate(candidate_id) or {}
    adj = journal.one(
        "SELECT * FROM decision_adjustments WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
        (candidate_id,)) or {}
    proposal = journal.one(
        "SELECT * FROM trade_proposals WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
        (candidate_id,))
    reject = journal.one(
        "SELECT * FROM rejected_candidates WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
        (candidate_id,))

    if proposal:
        candidate_type = "blocked" if proposal.get("status") == "blocked" else "proposal"
        entry, stop, target = proposal.get("entry"), proposal.get("stop"), proposal.get("target")
        direction = proposal.get("direction") or ev.get("direction") or cand.get("direction")
        playbook = proposal.get("playbook_name") or cand.get("playbook_name")
        decision_at_utc = proposal.get("created_at_utc") or cand.get("created_at_utc")
        lineage_id = proposal.get("lineage_id") or cand.get("lineage_id")
    elif cand.get("armed_watch"):
        candidate_type = "armed_watch"
        entry, stop, target = ev.get("entry"), ev.get("stop"), ev.get("target")
        direction = ev.get("direction") or cand.get("direction")
        playbook = cand.get("playbook_name")
        decision_at_utc = adj.get("created_at_utc") or cand.get("created_at_utc")
        lineage_id = adj.get("lineage_id") or cand.get("lineage_id")
    elif reject:
        candidate_type = "reject"
        if ev.get("entry") is not None:
            entry, stop, target = ev.get("entry"), ev.get("stop"), ev.get("target")
        else:
            entry, stop, target = reject.get("would_be_entry"), reject.get("would_be_stop"), None
        direction = reject.get("direction") or ev.get("direction") or cand.get("direction")
        playbook = cand.get("playbook_name")
        decision_at_utc = reject.get("created_at_utc") or cand.get("created_at_utc")
        lineage_id = reject.get("lineage_id") or cand.get("lineage_id")
    else:
        candidate_type = "candidate"
        entry, stop, target = ev.get("entry"), ev.get("stop"), ev.get("target")
        direction = ev.get("direction") or cand.get("direction")
        playbook = cand.get("playbook_name")
        decision_at_utc = cand.get("created_at_utc")
        lineage_id = cand.get("lineage_id")

    final_decision = adj.get("final_decision") or cand.get("label_decision") or cand.get("status")
    return {
        "candidate_type": candidate_type,
        "eval_decision": ev.get("decision"),
        "label_decision": cand.get("label_decision"),
        "final_decision": final_decision,
        # Frozen at seed time — AlphaOS's original call, for counterfactual
        # comparison against whatever final_decision later becomes.
        "original_decision": final_decision,
        "entry_reference_price": entry, "stop_price": stop, "target_price": target,
        "direction_hint": direction, "playbook_id": playbook,
        "decision_at_utc": decision_at_utc,
        # PR4: preserve the SOURCE decision's lineage_id (same anchor-on-source,
        # not anchor-on-seed-time, principle as decision_at_utc above) rather
        # than computing a fresh "current" snapshot -- an outcome row measures
        # the original decision, so it must carry that decision's own lineage,
        # not whatever code/config happens to be running when this row is seeded.
        "lineage_id": lineage_id,
    }


def _source_from_override(journal, ov: dict) -> dict:
    """Level/decision sourcing for a user-override counterfactual row. Unlike
    the AlphaOS-side row, ``final_decision`` here is the USER's final decision
    and ``original_decision`` is AlphaOS's original (frozen) call — the pair a
    future ΔR comparison needs. ``decision_at_utc`` is the override's OWN
    timestamp (when the user actually made their call) — that is the decision
    whose forward outcome this row measures, not the original candidate scan."""
    entry = stop = target = None
    if ov.get("proposal_id"):
        prop = journal.proposal_by_id(ov["proposal_id"])
        if prop:
            entry, stop, target = prop.get("entry"), prop.get("stop"), prop.get("target")
    if entry is None:
        ev = journal.evaluation_for_candidate(ov.get("candidate_id")) or {}
        entry, stop, target = ev.get("entry"), ev.get("stop"), ev.get("target")
    direction = ov.get("user_direction") or ov.get("alphaos_direction")
    return {
        "candidate_type": "user_override",
        "eval_decision": ov.get("alphaos_eval_decision"),
        "label_decision": ov.get("alphaos_label_decision"),
        "final_decision": ov.get("user_final_decision"),
        "original_decision": ov.get("alphaos_final_decision"),
        "entry_reference_price": entry, "stop_price": stop, "target_price": target,
        "direction_hint": direction, "playbook_id": None,
        "decision_at_utc": ov.get("created_at_utc"),
        # PR4: the override row's own lineage (the environment/config in
        # effect when the USER made this override) -- not the original
        # AlphaOS decision's lineage, since this row measures the override.
        "lineage_id": ov.get("lineage_id"),
    }


def _insert_outcome_row(journal, *, candidate_id: str, symbol: Optional[str],
                        scan_id: Optional[str], scan_batch_id: Optional[str],
                        armed_watch: bool, info: dict, override_flag: bool) -> None:
    journal.insert("candidate_outcomes", {
        "outcome_id": new_id("cout"),
        "scan_id": scan_id,
        "scan_batch_id": scan_batch_id,
        "candidate_id": candidate_id,
        "symbol": symbol,
        "candidate_type": info["candidate_type"],
        "decision_at_utc": info.get("decision_at_utc"),
        "original_decision": info["original_decision"],
        "eval_decision": info["eval_decision"],
        "label_decision": info["label_decision"],
        "final_decision": info["final_decision"],
        "armed_watch": 1 if armed_watch else 0,
        "user_override": 1 if override_flag else 0,
        "playbook_id": info.get("playbook_id"),
        "entry_reference_price": info.get("entry_reference_price"),
        "stop_price": info.get("stop_price"),
        "target_price": info.get("target_price"),
        "direction_hint": info.get("direction_hint"),
        "outcome_status": "pending",
        "lineage_id": info.get("lineage_id"),
    })


def seed_pending_outcomes(journal, limit: int = 500) -> dict:
    """Create missing candidate_outcomes rows. Returns counts by type + total.
    NEVER writes to candidates/proposals/rejects/overrides — read-only there."""
    counts = {t: 0 for t in (*_ALPHAOS_SIDE_TYPES, "user_override")}

    candidates = journal.query(
        "SELECT c.* FROM candidates c WHERE NOT EXISTS ("
        "  SELECT 1 FROM candidate_outcomes co WHERE co.candidate_id = c.candidate_id "
        "  AND co.candidate_type IN ('proposal','blocked','armed_watch','reject','candidate')"
        ") ORDER BY c.id ASC LIMIT ?", (limit,))
    for cand in candidates:
        info = _classify_candidate(journal, cand)
        _insert_outcome_row(
            journal, candidate_id=cand["candidate_id"], symbol=cand.get("symbol"),
            scan_id=cand.get("scan_id"), scan_batch_id=cand.get("scan_batch_id"),
            armed_watch=bool(cand.get("armed_watch")), info=info, override_flag=False)
        counts[info["candidate_type"]] += 1

    overrides = journal.query(
        "SELECT o.* FROM user_decision_overrides o WHERE o.candidate_id IS NOT NULL AND NOT EXISTS ("
        "  SELECT 1 FROM candidate_outcomes co WHERE co.candidate_id = o.candidate_id "
        "  AND co.candidate_type = 'user_override'"
        ") ORDER BY o.id ASC LIMIT ?", (limit,))
    for ov in overrides:
        info = _source_from_override(journal, ov)
        cand = journal.candidate_by_id(ov["candidate_id"])
        _insert_outcome_row(
            journal, candidate_id=ov["candidate_id"], symbol=ov.get("symbol") or (cand or {}).get("symbol"),
            scan_id=(cand or {}).get("scan_id"), scan_batch_id=(cand or {}).get("scan_batch_id"),
            armed_watch=bool((cand or {}).get("armed_watch")), info=info, override_flag=True)
        counts["user_override"] += 1

    counts["total"] = sum(counts.values())
    return counts


# ------------------------------------------------------------------- update
def _update_row(journal, outcome_id: str, fields: dict) -> None:
    st = timeutils.stamp()
    fields = dict(fields)
    fields["updated_at_utc"] = st.utc
    fields["updated_at_sgt"] = st.local_sgt
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    journal.conn.execute(
        f"UPDATE candidate_outcomes SET {set_clause} WHERE outcome_id = ?",
        (*fields.values(), outcome_id),
    )
    journal.conn.commit()


def _lookup_decision_timestamp(journal, candidate_id: str, candidate_type: str) -> Optional[str]:
    """Re-derive a row's original decision timestamp from its (already-decided)
    candidate_type's source table — the same mapping ``_classify_candidate``/
    ``_source_from_override`` use at seed time. Used only to REPAIR legacy rows
    seeded before ``decision_at_utc`` existed; never re-classifies the type."""
    if candidate_type in ("proposal", "blocked"):
        row = journal.one(
            "SELECT created_at_utc FROM trade_proposals WHERE candidate_id = ? "
            "ORDER BY id DESC LIMIT 1", (candidate_id,))
    elif candidate_type == "armed_watch":
        row = journal.one(
            "SELECT created_at_utc FROM decision_adjustments WHERE candidate_id = ? "
            "ORDER BY id DESC LIMIT 1", (candidate_id,))
    elif candidate_type == "reject":
        row = journal.one(
            "SELECT created_at_utc FROM rejected_candidates WHERE candidate_id = ? "
            "ORDER BY id DESC LIMIT 1", (candidate_id,))
    elif candidate_type == "user_override":
        row = journal.one(
            "SELECT created_at_utc FROM user_decision_overrides WHERE candidate_id = ? "
            "ORDER BY id DESC LIMIT 1", (candidate_id,))
    else:  # 'candidate' catch-all
        row = None
    if row and row.get("created_at_utc"):
        return row["created_at_utc"]
    # Last resort for every type (including 'candidate', and any type whose
    # specific source row is gone): the candidates table itself.
    cand = journal.candidate_by_id(candidate_id)
    return (cand or {}).get("created_at_utc")


def _repair_missing_decision_at_utc(journal, rows: list[dict]) -> list[dict]:
    """Backfill decision_at_utc on legacy rows (seeded before this column
    existed) by re-deriving it from the linked source row. Additive, idempotent
    — only rows with decision_at_utc IS NULL are touched. When the source row
    can no longer be found, falls back to created_at_utc (so the row can still
    resolve) but marks data_quality_status so seed-time is never silently
    mistaken for decision-time. Returns ``rows`` with decision_at_utc filled in
    (in-memory) so the caller's SAME pass anchors correctly."""
    repaired = []
    for row in rows:
        if row.get("decision_at_utc"):
            repaired.append(row)
            continue
        ts = _lookup_decision_timestamp(journal, row["candidate_id"], row["candidate_type"])
        if ts:
            _update_row(journal, row["outcome_id"], {"decision_at_utc": ts})
            row = dict(row)
            row["decision_at_utc"] = ts
        else:
            # Source row is gone too — created_at_utc is the only timestamp
            # left. Use it so the row isn't stuck forever, but flag it: this is
            # NOT a reliable decision timestamp, unlike the normal case.
            fallback = row.get("created_at_utc")
            _update_row(journal, row["outcome_id"], {
                "decision_at_utc": fallback, "data_quality_status": "decision_time_unrecoverable"})
            row = dict(row)
            row["decision_at_utc"] = fallback
            row["data_quality_status"] = "decision_time_unrecoverable"
        repaired.append(row)
    return repaired


def update_pending_outcomes(journal, bars_provider=None, limit: int = 200) -> dict:
    """Resolve pending/partial candidate_outcomes rows with forward 1/3/5-day
    returns + bracket replay. Idempotent: only rows still pending/partial are
    touched; ``complete``/``unavailable`` rows are never revisited. Missing
    bars are handled safely — no provider or a transient empty fetch just
    leaves the row pending (retried next call); only after
    ``UNAVAILABLE_AFTER_DAYS`` with zero bars does a row convert to
    ``unavailable``. Forward windows anchor on ``decision_at_utc`` (the
    original decision time), NOT ``created_at_utc`` (when this row was
    seeded) — seeding can lag the decision by days/weeks when catching up on
    a backlog, and anchoring on seed time would mislabel a multi-week-old
    candidate's next bar as a "1-day" return."""
    rows = journal.query(
        "SELECT * FROM candidate_outcomes WHERE outcome_status IN ('pending','partial') "
        "ORDER BY id ASC LIMIT ?", (limit,))
    counts = {"total": len(rows), "updated": 0, "completed": 0, "skipped": 0, "unavailable": 0}
    if bars_provider is None:
        counts["skipped"] = len(rows)
        return counts

    rows = _repair_missing_decision_at_utc(journal, rows)
    now = timeutils.now_utc()
    for row in rows:
        decision_at = timeutils.parse_iso(row.get("decision_at_utc"))
        if decision_at is None:
            counts["skipped"] += 1
            continue
        age_days = (now - decision_at).total_seconds() / 86400.0
        decision_date = decision_at.date().isoformat()
        bars = bars_provider.get_daily_bars(row["symbol"], decision_date, now.date().isoformat()) or []
        # Bars strictly AFTER the decision day — never let the decision's own
        # day count as "forward" (no lookahead leakage into the replay).
        forward_bars = [b for b in bars if b.get("date") and b["date"] > decision_date]

        if not forward_bars:
            if age_days > UNAVAILABLE_AFTER_DAYS:
                # Don't clobber an already-flagged unrecoverable decision_at_utc
                # (repair may have just set it this same pass) — that provenance
                # signal matters more than the more-obvious no-bars reason.
                dq = row.get("data_quality_status") or "no_bars_after_window"
                _update_row(journal, row["outcome_id"], {
                    "outcome_status": "unavailable", "data_quality_status": dq})
                counts["unavailable"] += 1
            else:
                counts["skipped"] += 1
            continue

        ref, stop, direction = row.get("entry_reference_price"), row.get("stop_price"), row.get("direction_hint")
        f1 = forward_window_stats(ref, stop, direction, forward_bars, 1)
        f3 = forward_window_stats(ref, stop, direction, forward_bars, 3)
        f5 = forward_window_stats(ref, stop, direction, forward_bars, 5)

        update = {
            "forward_1d_return_pct": f1["return_pct"], "forward_1d_r": f1["r"],
            "max_favorable_1d_r": f1["max_favorable_r"], "max_adverse_1d_r": f1["max_adverse_r"],
            "forward_3d_return_pct": f3["return_pct"], "forward_3d_r": f3["r"],
            "max_favorable_3d_r": f3["max_favorable_r"], "max_adverse_3d_r": f3["max_adverse_r"],
            "forward_5d_return_pct": f5["return_pct"], "forward_5d_r": f5["r"],
            "max_favorable_5d_r": f5["max_favorable_r"], "max_adverse_5d_r": f5["max_adverse_r"],
        }

        target = row.get("target_price")
        if ref is not None and stop and target:
            replay = replay_bracket(ref, stop, target, direction, forward_bars)
            update["replay_result"] = replay["result"]
            update["replay_r"] = replay["replay_r"]
            update["replay_exit_reason"] = replay["replay_exit_reason"]

        resolved = f5["bars_used"] >= 5
        update["outcome_status"] = "complete" if resolved else "partial"
        # Don't clobber an unrecoverable-decision_at_utc flag from repair just
        # because the forward-return math itself succeeded — the anchor being
        # a fallback (not the true decision time) is still worth knowing.
        update["data_quality_status"] = row.get("data_quality_status") or "ok"
        _update_row(journal, row["outcome_id"], update)
        counts["updated"] += 1
        if resolved:
            counts["completed"] += 1

    return counts
