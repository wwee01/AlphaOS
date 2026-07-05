"""Attribution v2 -- discovery + resolution orchestration (PR8).

Two idempotent phases, called from Orchestrator.outcomes_update() right after
the existing seed_pending_outcomes()/update_pending_outcomes() calls -- the
SAME hook point, no new call site elsewhere:

* ``discover_events`` -- finds source rows (trade_proposals /
  user_decision_overrides) newly eligible for one of the 5 attribution types
  and inserts a PENDING attribution_records row for each. PURE READ of the
  decision tables + one INSERT per new row; never touches trade_proposals /
  user_decision_overrides / candidate_outcomes / trade_outcomes.
* ``resolve_pending`` -- for rows still pending/partial, re-derives
  alphaos_path_r/actual_path_r/delta_r from whatever candidate_outcomes /
  trade_outcomes now show. Once a row reaches resolved/unresolvable it is
  NEVER revisited again (matches update_pending_outcomes' own convention:
  only pending/partial candidate_outcomes rows are ever touched twice).

Fail-safe throughout: any exception discovering or resolving one row is
caught, logged as a system_event, and skipped -- never fails the whole batch,
never touches the real pipeline.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from alphaos.attribution import discovery
from alphaos.attribution.resolve import (
    ATTRIBUTION_VERSION,
    compute_data_quality,
    resolve_propose_approved_executed,
    resolve_user_override_trade,
    resolve_zero_vs_replay,
)
from alphaos.constants import AttributionAgent, AttributionResolvedStatus, AttributionType, Severity
from alphaos.util import timeutils
from alphaos.util.ids import new_id

# attribution_type -> agent for the 4 proposal-anchored types (shared
# resolve_zero_vs_replay formula; only agent + which verbatim source field to
# copy differ between them).
_PROPOSAL_EVENT_AGENTS = {
    AttributionType.PROPOSE_USER_REJECTED.value: AttributionAgent.USER.value,
    AttributionType.PROPOSE_APPROVED_EXECUTED.value: AttributionAgent.EXECUTION.value,
    AttributionType.PROPOSE_EXPIRED.value: AttributionAgent.OPERATIONAL.value,
    AttributionType.PROPOSE_BLOCKED.value: AttributionAgent.GATE.value,
}


def _is_mock_row(journal, settings, candidate_id: Optional[str]) -> bool:
    """Same rule as the PR7 audit fix: mock-mode INTENT, not just the row's
    own eval flag -- a candidate/proposal/override scored with no
    openai_evaluations row at all would otherwise read as real data even
    though the whole run is ALPHAOS_MODE=mock."""
    ev = journal.evaluation_for_candidate(candidate_id) if candidate_id else None
    return bool(settings.is_mock) or bool((ev or {}).get("is_mock"))


def _log_error(journal, where: str, detail: dict) -> None:
    journal.log_system_event(
        Severity.WARNING, "attribution", f"Attribution {where} failed; skipped.", detail,
    )


def _insert_pending(journal, *, attribution_type: str, agent: str, source_id: str,
                    candidate_id: Optional[str], proposal_id: Optional[str],
                    override_id: Optional[str], symbol: Optional[str], direction: Optional[str],
                    decision_at_utc: Optional[str], lineage_id: Optional[str], is_mock: bool,
                    extra: Optional[dict] = None) -> Optional[str]:
    attribution_id = new_id("attr")
    row = {
        "attribution_id": attribution_id,
        "attribution_type": attribution_type,
        "attribution_version": ATTRIBUTION_VERSION,
        "agent": agent,
        "source_id": source_id,
        "candidate_id": candidate_id,
        "proposal_id": proposal_id,
        "override_id": override_id,
        "symbol": symbol,
        "direction": direction,
        "decision_at_utc": decision_at_utc,
        "resolved_status": AttributionResolvedStatus.PENDING.value,
        "data_quality_status": compute_data_quality(is_mock, AttributionResolvedStatus.PENDING.value),
        "is_mock": is_mock,
        "lineage_id": lineage_id,
        **(extra or {}),
    }
    try:
        journal.insert("attribution_records", row)
    except sqlite3.IntegrityError:
        return None  # another caller already discovered this exact row -- idempotent no-op
    return attribution_id


# ------------------------------------------------------------------ discover
def _discover_proposal_event(journal, settings, prop: dict, attribution_type: str,
                             extra: Optional[dict] = None) -> bool:
    try:
        if prop.get("is_demo"):
            return False
        candidate_id = prop.get("candidate_id")
        result = _insert_pending(
            journal, attribution_type=attribution_type, agent=_PROPOSAL_EVENT_AGENTS[attribution_type],
            source_id=prop["proposal_id"], candidate_id=candidate_id, proposal_id=prop["proposal_id"],
            override_id=None, symbol=prop.get("symbol"), direction=prop.get("direction"),
            decision_at_utc=prop.get("created_at_utc"), lineage_id=prop.get("lineage_id"),
            is_mock=_is_mock_row(journal, settings, candidate_id), extra=extra,
        )
        return result is not None
    except Exception as exc:  # fail-safe: discovery never touches the real pipeline
        _log_error(journal, f"discovery:{attribution_type}",
                  {"error": str(exc), "proposal_id": prop.get("proposal_id")})
        return False


def _discover_override_event(journal, settings, ov: dict) -> bool:
    try:
        candidate_id = ov.get("candidate_id")
        cand = journal.candidate_by_id(candidate_id) if candidate_id else None
        if cand and cand.get("status") == "demo":
            return False
        result = _insert_pending(
            journal, attribution_type=AttributionType.USER_OVERRIDE_TRADE.value,
            agent=AttributionAgent.USER.value, source_id=ov["override_id"], candidate_id=candidate_id,
            proposal_id=ov.get("proposal_id"), override_id=ov["override_id"], symbol=ov.get("symbol"),
            direction=ov.get("user_direction") or ov.get("alphaos_direction"),
            decision_at_utc=ov.get("created_at_utc"), lineage_id=ov.get("lineage_id"),
            is_mock=_is_mock_row(journal, settings, candidate_id),
        )
        return result is not None
    except Exception as exc:  # fail-safe: discovery never touches the real pipeline
        _log_error(journal, "discovery:user_override_trade",
                  {"error": str(exc), "override_id": ov.get("override_id")})
        return False


def discover_events(journal, settings, limit: int = 200) -> dict:
    """Create PENDING attribution_records rows for newly-eligible source rows
    across all 5 types. Never raises -- a total discovery failure is logged
    and returns zero counts rather than propagating."""
    counts = {t.value: 0 for t in AttributionType}
    try:
        for prop in discovery.find_propose_user_rejected(journal, ATTRIBUTION_VERSION, limit):
            if _discover_proposal_event(journal, settings, prop, AttributionType.PROPOSE_USER_REJECTED.value):
                counts[AttributionType.PROPOSE_USER_REJECTED.value] += 1
        for prop in discovery.find_propose_approved_executed(journal, ATTRIBUTION_VERSION, limit):
            if _discover_proposal_event(journal, settings, prop, AttributionType.PROPOSE_APPROVED_EXECUTED.value):
                counts[AttributionType.PROPOSE_APPROVED_EXECUTED.value] += 1
        for prop in discovery.find_propose_expired(journal, ATTRIBUTION_VERSION, limit):
            if _discover_proposal_event(journal, settings, prop, AttributionType.PROPOSE_EXPIRED.value,
                                        extra={"expired_reason": prop.get("expired_reason")}):
                counts[AttributionType.PROPOSE_EXPIRED.value] += 1
        for prop in discovery.find_propose_blocked(journal, ATTRIBUTION_VERSION, limit):
            if _discover_proposal_event(journal, settings, prop, AttributionType.PROPOSE_BLOCKED.value,
                                        extra={"blocked_reason_code": prop.get("proposal_reason")}):
                counts[AttributionType.PROPOSE_BLOCKED.value] += 1
        for ov in discovery.find_user_override_trade(journal, ATTRIBUTION_VERSION, limit):
            if _discover_override_event(journal, settings, ov):
                counts[AttributionType.USER_OVERRIDE_TRADE.value] += 1
    except Exception as exc:  # fail-safe: a total discovery failure never fails outcomes_update
        _log_error(journal, "discovery", {"error": str(exc)})
    counts["total"] = sum(counts[t.value] for t in AttributionType)
    return counts


# ------------------------------------------------------------------- resolve
def _update_resolution(journal, attribution_id: str, fields: dict) -> None:
    st = timeutils.stamp()
    fields = dict(fields)
    if fields.get("resolved_status") in (
        AttributionResolvedStatus.RESOLVED.value, AttributionResolvedStatus.UNRESOLVABLE.value,
    ):
        fields.setdefault("resolved_at_utc", st.utc)
    for key in ("missing_data_json", "notes_json"):
        if key in fields and fields[key] is not None and not isinstance(fields[key], str):
            import json

            fields[key] = json.dumps(fields[key], default=str)
    cols = ", ".join(f"{k} = ?" for k in fields)
    journal.conn.execute(
        f"UPDATE attribution_records SET {cols} WHERE attribution_id = ?",
        (*fields.values(), attribution_id),
    )
    journal.conn.commit()


def _resolve_one(journal, row: dict) -> Optional[str]:
    """Returns the new resolved_status, or None if still pending (nothing new
    to persist this pass -- the row is simply left untouched, not rewritten)."""
    attribution_type = row["attribution_type"]
    candidate_id = row.get("candidate_id")

    if attribution_type == AttributionType.PROPOSE_APPROVED_EXECUTED.value:
        trade_outcome = discovery.trade_outcome_for_proposal(journal, row.get("proposal_id"))
        co = discovery.candidate_outcome_for_proposal(journal, candidate_id)
        result = resolve_propose_approved_executed(trade_outcome, co)
        extra_ids = {"trade_outcome_id": (trade_outcome or {}).get("outcome_id"),
                    "candidate_outcome_id": (co or {}).get("outcome_id")}
    elif attribution_type == AttributionType.USER_OVERRIDE_TRADE.value:
        trade_outcome = discovery.trade_outcome_for_proposal(journal, row.get("proposal_id"))
        co = discovery.candidate_outcome_for_override(journal, candidate_id)
        result = resolve_user_override_trade(trade_outcome, co)
        extra_ids = {"trade_outcome_id": (trade_outcome or {}).get("outcome_id"),
                    "candidate_outcome_id": (co or {}).get("outcome_id")}
    else:  # propose_user_rejected / propose_expired / propose_blocked
        co = discovery.candidate_outcome_for_proposal(journal, candidate_id)
        result = resolve_zero_vs_replay(co)
        extra_ids = {"candidate_outcome_id": (co or {}).get("outcome_id"), "trade_outcome_id": None}

    if result["resolved_status"] == AttributionResolvedStatus.PENDING.value:
        return None  # nothing new learned this pass; leave the row untouched

    is_mock = bool(row.get("is_mock"))
    degraded = row.get("lineage_id") is None
    missing_reason = result.get("missing_reason")
    fields = {
        **extra_ids,
        "alphaos_path_r": result["alphaos_path_r"],
        "actual_path_r": result["actual_path_r"],
        "delta_r": result["delta_r"],
        "execution_delta_r": result["execution_delta_r"],
        "r_basis": result["r_basis"],
        "replay_status": result["replay_status"],
        "resolved_status": result["resolved_status"],
        "data_quality_status": compute_data_quality(is_mock, result["resolved_status"], degraded=degraded),
        "missing_data_json": {"reason": missing_reason} if missing_reason else None,
    }
    _update_resolution(journal, row["attribution_id"], fields)
    return result["resolved_status"]


def resolve_pending(journal, settings, limit: int = 200) -> dict:
    """Resolve pending/partial attribution_records rows. Idempotent: rows
    already resolved/unresolvable are NEVER revisited. Never raises -- a total
    resolution failure is logged and returns zero counts; a single row's
    failure is logged and the rest of the batch continues."""
    counts = {"total": 0, "resolved": 0, "partial": 0, "unresolvable": 0, "still_pending": 0}
    try:
        rows = journal.query(
            "SELECT * FROM attribution_records WHERE resolved_status IN (?, ?) ORDER BY id ASC LIMIT ?",
            (AttributionResolvedStatus.PENDING.value, AttributionResolvedStatus.PARTIAL.value, limit),
        )
    except Exception as exc:  # fail-safe: a read failure never fails outcomes_update
        _log_error(journal, "resolve:read", {"error": str(exc)})
        return counts

    for row in rows:
        counts["total"] += 1
        try:
            new_status = _resolve_one(journal, row)
        except Exception as exc:  # fail-safe: one row's failure never blocks the rest
            _log_error(journal, "resolve:row",
                      {"error": str(exc), "attribution_id": row.get("attribution_id")})
            continue
        if new_status is None:
            counts["still_pending"] += 1
        else:
            counts[new_status] = counts.get(new_status, 0) + 1
    return counts
