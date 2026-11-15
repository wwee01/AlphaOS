"""EXPLORE-1 routing only. Model opinions and all execution gates remain separate."""
from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
from dataclasses import replace

from alphaos.constants import ReasonCode
from alphaos.scheduler.cadence import parse_windows, window_containing
from alphaos.util import timeutils

VERSION = "explore_1"
ARMS = {"A", "B", "A+B", "C"}
_FAILURES = {ReasonCode.OPENAI_REJECT.value, ReasonCode.NO_ATR_DATA.value,
             ReasonCode.REWARD_RISK_TOO_LOW.value, ReasonCode.MAX_HOLDING_DAYS_OUT_OF_RANGE.value}


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def route(cand, evaluation, classification, settings, default_hold):
    """Return (arm or empty-string control eligibility, normalized plan), else None.

    No failed validation is rescued by defaults. A valid evaluator proposal
    does not need a classifier; B and C require a valid long classifier opinion.
    """
    ev = evaluation
    if (ev.candidate_id != cand["candidate_id"] or ev.symbol != cand["symbol"]
            or cand.get("shadow_tier") or cand.get("direction") == "short" or ev.direction != "long"
            or ev.validation_status != "passed" or ev.data_freshness_status != "usable"
            or ev.decision not in {"propose", "watch", "reject"} or _FAILURES.intersection(ev.risk_flags)):
        return None
    label_ok = (classification is not None and classification.validation_status == "passed"
                and classification.candidate_id == cand["candidate_id"] and classification.symbol == cand["symbol"]
                and classification.direction == "long"
                and classification.label_decision in {"propose", "watch", "reject"})
    a, b = ev.decision == "propose", label_ok and classification.label_decision == "propose"
    if not a and not label_ok:
        return None
    entry, stop, target = ev.entry, ev.stop, ev.target
    valid = (all(positive(v) for v in (entry, stop, target)) and stop < entry < target
             and (target - entry) / (entry - stop) >= settings.min_reward_risk)
    source = ev.stop_source
    if not valid:
        entry = (cand.snapshot or {}).get("last_price")
        if not positive(entry):
            return None
        stop = entry * (1 - settings.stop_loss_pct)
        target = entry + (entry - stop) * settings.target_reward_risk
        if not (0 < stop < entry < target and settings.target_reward_risk >= settings.min_reward_risk):
            return None
        source = "config"
    hold = ev.max_holding_days if ev.max_holding_days is not None else default_hold
    if type(default_hold) is not int or type(hold) is not int or not 1 <= hold <= default_hold:
        return None
    plan = replace(ev, entry=entry, stop=stop, target=target, stop_source=source,
                   expected_r=(target-entry)/(entry-stop), max_holding_days=hold)
    return ("A+B" if a and b else "A" if a else "B" if b else ""), plan


def freeze_draw(journal, settings, scan_batch_id, started_at, eligible_ids):
    """Unique window ownership is committed before routing, even for an empty pool.

    A blocked chosen candidate never causes a redraw. Off-window manual scans
    have no C draw. A later scan in the window cannot reuse another scan's C.
    """
    parsed = timeutils.parse_iso(started_at)
    if parsed is None or parsed.tzinfo is None:
        raise ValueError("campaign draw requires an aware scan start")
    at = timeutils.to_et(parsed)
    window = window_containing(at.strftime("%H:%M"), parse_windows(settings.scheduler_scan_windows))
    key = f"{VERSION}:{at.date()}:{window[0]}" if window else None
    previous = journal.one("SELECT explore_draw_json FROM scan_batches WHERE scan_batch_id=?", (scan_batch_id,))
    if previous is None:
        raise ValueError("campaign scan batch missing")
    if previous["explore_draw_json"] is not None:
        draw = json.loads(previous["explore_draw_json"])
        return draw["chosen_id"] if draw["scan_batch_id"] == scan_batch_id else None
    seed = hashlib.sha256((key or f"off-window:{scan_batch_id}").encode()).hexdigest()
    pool = sorted(eligible_ids)
    draw = {"policy_version": VERSION, "window_key": key, "scan_batch_id": scan_batch_id,
            "seed": seed, "eligible_ids": pool,
            "chosen_id": random.Random(seed).choice(pool) if key and pool else None}
    try:
        journal.conn.execute("UPDATE scan_batches SET explore_window_key=?, explore_draw_json=? "
                             "WHERE scan_batch_id=?", (key, json.dumps(draw, sort_keys=True), scan_batch_id))
        journal.conn.commit()
    except sqlite3.IntegrityError:
        journal.conn.rollback()
        previous = journal.one("SELECT explore_draw_json FROM scan_batches WHERE explore_window_key=?", (key,))
        if previous is None:
            raise
        draw = json.loads(previous["explore_draw_json"])
        journal.conn.execute("UPDATE scan_batches SET explore_draw_json=? WHERE scan_batch_id=?",
                             (json.dumps(draw, sort_keys=True), scan_batch_id))
        journal.conn.commit()
    return draw["chosen_id"] if draw["scan_batch_id"] == scan_batch_id else None


def entry_refusal(journal, settings, proposal):
    """Final, fresh local slot check under the existing entry submission lock."""
    if not settings.explore_1_enabled:
        return "EXPLORE-1 is inactive" if proposal.policy_version == VERSION else None
    saved = journal.proposal_by_id(proposal.proposal_id) or {}
    cand = journal.candidate_by_id(proposal.candidate_id) or {}
    if (not settings.is_paper or proposal.direction != "long" or cand.get("shadow_tier")
            or proposal.arm not in ARMS or proposal.policy_version != VERSION
            or any(saved.get(k) != getattr(proposal, k) for k in ("arm", "policy_version", "candidate_id", "direction"))
            or cand.get("arm") != proposal.arm or cand.get("policy_version") != VERSION):
        return "EXPLORE-1 requires a tagged paper-only long campaign proposal"
    holdings = journal.query(
        "SELECT arm FROM positions WHERE status='open' UNION ALL SELECT o.arm FROM paper_orders o "
        "WHERE o.side='buy' AND o.state IN ('submitted','accepted','partially_filled') AND NOT EXISTS "
        "(SELECT 1 FROM positions p WHERE p.order_id=o.order_id AND p.status='open')")
    pending = journal.query(
        "SELECT arm FROM trade_proposals WHERE policy_version=? AND direction='long' "
        "AND status IN ('pending_approval','proposed') AND proposal_id<>? "
        "AND proposal_expires_at_utc>?", (VERSION, proposal.proposal_id, timeutils.now_utc().isoformat()))
    counts = {arm: sum(arm in (row.get("arm") or "").split("+") for row in holdings)
              for arm in ("A", "B", "C")}
    waiting = {arm for row in pending for arm in (row.get("arm") or "").split("+") if arm in counts}
    for arm in proposal.arm.split("+"):
        if counts[arm] + 1 > settings.max_open_positions / 2 and waiting - {arm}:
            return f"EXPLORE-1 slot fairness: {arm} would exceed half the slots while another arm is pending"
    return None
