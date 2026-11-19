"""Pure EXPLORE-1 event identity and reference-session mapping.

Callers supply copied source rows and independently verified source commits.
No timestamps are inferred from outcome rows, processing time or later retries.
Unavailable records remain in the returned census; this is not an evaluator.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Iterable, Mapping

from alphaos.learning.close_ref_engine import (
    CalendarPending, Window, pinned_calendar, resolve_window, window_from_dict, window_to_dict,
)

CAMPAIGN_START = datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc)


def _instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp_unavailable")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp_unavailable")
    return parsed.astimezone(timezone.utc)


def _row_id(row: Mapping) -> int:
    value = row.get("id")
    if type(value) is not int or value < 1:
        raise ValueError("source_row_id_unavailable")
    return value


def _live(row: Mapping) -> bool:
    return type(row.get("shadow_tier")) is int and row["shadow_tier"] == 0


def _commit(value: str | None) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


@dataclass(frozen=True)
class Decision:
    population: str
    arm: str
    candidate_id: str
    symbol: str
    scan_batch_id: str
    source_table: str
    source_id: str
    basis: str
    status: str
    reason: str | None
    decision_at_utc: str | None
    lower_bound_utc: str | None
    upper_bound_utc: str | None
    window: Window | None
    calendar_version: str
    source_commit: str | None
    scan_row_id: int
    candidate_row_id: int | None
    ranking_eligible: bool = False
    ranking_exclusion_reason: str | None = None

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        return self.population, self.arm, self.candidate_id, self.source_table, self.source_id

    @property
    def key(self) -> tuple[str, str, str, str] | None:
        if self.window is None:
            return None
        return self.population, self.arm, self.symbol, self.window.reference.date


def decision_to_dict(record: Decision) -> dict:
    body = asdict(record)
    body["window"] = window_to_dict(record.window) if record.window else None
    return body


def decision_from_dict(body: dict) -> Decision:
    owned = dict(body)
    owned["window"] = window_from_dict(body["window"]) if body["window"] is not None else None
    record = Decision(**owned)
    if record.calendar_version != pinned_calendar().sha256:
        raise ValueError("calendar_version_mismatch")
    return record


def _record(population: str, arm: str, candidate: Mapping, batch: Mapping,
            source_table: str, source_id: str, basis: str, source_commit: str | None,
            *, exact: object = None, lower: object = None, upper: object = None,
            reason: str | None = None) -> Decision:
    calendar = pinned_calendar()
    record = Decision(population, arm, str(candidate.get("candidate_id", "")),
                      str(candidate.get("symbol", "")), str(batch.get("scan_batch_id", "")),
                      source_table, source_id, basis, "unavailable", reason,
                      None, None, None, None, calendar.sha256, source_commit,
                      _row_id(batch), _row_id(candidate) if candidate.get("id") is not None else None)
    try:
        stamp = _instant(exact) if exact is not None else None
        if stamp is not None and stamp < CAMPAIGN_START:
            return replace(record, decision_at_utc=stamp.isoformat(),
                           status="out_of_scope", reason="before_campaign")
        lo = _instant(lower) if lower is not None else None
        hi = _instant(upper) if upper is not None else None
        record = replace(record, decision_at_utc=stamp.isoformat() if stamp else None,
                         lower_bound_utc=lo.isoformat() if lo else None,
                         upper_bound_utc=hi.isoformat() if hi else None)
        if stamp is None and hi is not None and hi < CAMPAIGN_START:
            return replace(record, status="out_of_scope", reason="before_campaign")
        if reason:
            return record
        if stamp is not None:
            if lo is not None and hi is not None and not lo <= stamp <= hi:
                return replace(record, reason="identity_mismatch")
            if stamp < CAMPAIGN_START:
                return replace(record, status="out_of_scope", reason="before_campaign")
            window = resolve_window(stamp, calendar)
        else:
            if lo is None or hi is None:
                return replace(record, reason="timestamp_unavailable")
            if lo > hi:
                return replace(record, reason="session_unproven")
            if hi < CAMPAIGN_START:
                return replace(record, status="out_of_scope", reason="before_campaign")
            if lo < CAMPAIGN_START:
                return replace(record, reason="boundary_unproven")
            first, last = resolve_window(lo, calendar), resolve_window(hi, calendar)
            if first.reference != last.reference:
                return replace(record, reason="session_unproven")
            window = replace(first, decision_at_utc=None)
        return replace(record, status="ready", reason=None, window=window)
    except CalendarPending as exc:
        return replace(record, reason=str(exc))
    except (ValueError, TypeError, OverflowError):
        return replace(record, reason=reason or "timestamp_unavailable")


def _routing_bounds(batch: Mapping, candidates: list[Mapping], evaluations: Iterable[Mapping],
                    proposals: Iterable[Mapping], source_commit: str | None) -> tuple:
    """Only source-proven same-batch opinions/completion/proposals bound routing."""
    if not _commit(source_commit):
        return None, None, "ordering_source_unproven"
    batch_id = batch["scan_batch_id"]
    ids = {row["candidate_id"] for row in candidates
           if row.get("scan_batch_id") == batch_id and _live(row)}
    opinions = [row for row in evaluations if row.get("candidate_id") in ids]
    try:
        if not opinions:
            return None, None, "timestamp_unavailable"
        lower = max(_instant(row.get("created_at_utc")) for row in opinions)
        uppers = ([_instant(batch["completed_at_utc"])]
                  if batch.get("completed_at_utc") is not None else [])
        uppers.extend(_instant(row.get("created_at_utc")) for row in proposals
                      if row.get("scan_batch_id") == batch_id
                      and row.get("policy_version") == "explore_1")
        if not uppers:
            return lower.isoformat(), None, "timestamp_unavailable"
        upper = min(uppers)
        return lower.isoformat(), upper.isoformat(), None
    except (ValueError, TypeError, OverflowError):
        return None, None, "timestamp_unavailable"


def routing_records(batch: Mapping, candidates: Iterable[Mapping], evaluations: Iterable[Mapping],
                    proposals: Iterable[Mapping], *, source_commit: str | None = None) -> list[Decision]:
    """All arm records and owning in-window pool members, including refusals.

    A valid source_commit means the caller proved the producer ordering at that
    commit; this pure function does not claim that the deployed bytes were clean.
    """
    candidates = list(candidates)
    batch_id = batch["scan_batch_id"]
    own = {row["candidate_id"]: row for row in candidates if row.get("scan_batch_id") == batch_id}
    lower, upper, bounds_reason = _routing_bounds(batch, candidates, evaluations, proposals, source_commit)
    exact = batch.get("explore_routed_at_utc")
    basis = "exact_routing_v1" if exact is not None else "bounded_routing_v1"
    # Exact stamps need no legacy proof. A supplied, established interval is an
    # additional consistency constraint, never a replacement for an invalid stamp.
    common = dict(exact=exact, lower=lower, upper=upper,
                  reason=bounds_reason if exact is None else None)
    draw = batch.get("explore_draw_json")
    try:
        draw = json.loads(draw) if isinstance(draw, str) else draw
        if not isinstance(draw, dict):
            draw = {}
    except (ValueError, TypeError):
        draw = {}
    owner = draw.get("scan_batch_id") == batch_id and draw.get("window_key") is not None
    eligible = draw.get("eligible_ids", []) if owner else []
    valid_ids = isinstance(eligible, list) and all(isinstance(item, str) for item in eligible)
    if not valid_ids:
        eligible = []
    output = []
    for candidate in own.values():
        arm = candidate.get("arm")
        if candidate.get("policy_version") != "explore_1" or arm not in ("A", "B", "A+B", "C"):
            continue
        error = None
        if not _live(candidate):
            error = "identity_mismatch"
        if arm == "C" and (not owner or not valid_ids or
                           candidate["candidate_id"] != draw.get("chosen_id") or
                           candidate["candidate_id"] not in eligible):
            error = "identity_mismatch"
        for single in ("A", "B") if arm == "A+B" else (arm,):
            args = dict(common)
            if error:
                args["reason"] = error
            output.append(_record("arm", single, candidate, batch, "scan_batches", batch_id,
                                  basis, source_commit, **args))
    for candidate_id in dict.fromkeys(eligible):
        pool_candidate = own.get(candidate_id)
        if pool_candidate is None:
            # Preserve a malformed pool reference even when no candidate supplies
            # symbol/tie-break metadata; it can never enter a valid decision key.
            pool_candidate = {"candidate_id": candidate_id, "symbol": ""}
        args = dict(common)
        if (candidate_id not in own or not _live(pool_candidate) or
                eligible.count(candidate_id) != 1):
            args["reason"] = "identity_mismatch"
        output.append(_record("pool", "", pool_candidate, batch, "scan_batches", batch_id,
                              basis, source_commit, **args))
    return output


def _original(rows: list[Mapping], timestamp: str) -> tuple[Mapping, str | None]:
    """A malformed timestamp cannot be silently discarded in favour of a later row."""
    try:
        return min(rows, key=lambda row: (_instant(row.get(timestamp)), _row_id(row))), None
    except (ValueError, TypeError, OverflowError):
        return min(rows, key=_row_id), "timestamp_unavailable"


def _ranking_reason(candidate: Mapping, opinion: Mapping, non_degraded: bool) -> str | None:
    if not non_degraded:
        return "non_degraded_scan_unproven"
    if candidate.get("direction") != "long" or opinion.get("direction") != "long":
        return "not_long"
    if opinion.get("validation_status") != "passed":
        return "evaluation_not_passed"
    if type(opinion.get("is_mock")) is not int or opinion["is_mock"] != 0:
        return "evaluation_not_nonmock"
    if opinion.get("decision") not in ("reject", "watch", "propose"):
        return "invalid_opinion"
    confidence = opinion.get("confidence")
    try:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence):
            return "nonfinite_confidence"
    except OverflowError:
        return "nonfinite_confidence"
    return None


def evaluator_records(candidates: Iterable[Mapping], batches: Iterable[Mapping],
                      evaluations: Iterable[Mapping], *, non_degraded_scan_ids: Iterable[str] = (),
                      source_commits: Mapping[str, str] | None = None) -> list[Decision]:
    """Measure all original live evaluations; qualify ranking before symbol dedupe."""
    batch_map = {row["scan_batch_id"]: row for row in batches}
    originals: dict[str, list[Mapping]] = {}
    for row in evaluations:
        originals.setdefault(row["candidate_id"], []).append(row)
    non_degraded = set(non_degraded_scan_ids)
    output = []
    for candidate in candidates:
        rows = originals.get(candidate["candidate_id"], [])
        if not _live(candidate) or not rows:
            continue
        batch = batch_map[candidate["scan_batch_id"]]
        opinion, reason = _original(rows, "created_at_utc")
        if opinion.get("symbol") != candidate.get("symbol"):
            reason = "identity_mismatch"
        record = _record("evaluator", "", candidate, batch, "openai_evaluations", opinion["eval_id"],
                         "exact_evaluation_v1", (source_commits or {}).get(batch["scan_batch_id"]),
                         exact=opinion.get("created_at_utc"), reason=reason)
        excluded = _ranking_reason(candidate, opinion, batch["scan_batch_id"] in non_degraded)
        output.append(replace(record, ranking_eligible=excluded is None and record.status == "ready",
                              ranking_exclusion_reason=excluded or record.reason))
    return output


def shadow_records(candidates: Iterable[Mapping], batches: Iterable[Mapping], labels: Iterable[Mapping],
                   *, source_commits: Mapping[str, str] | None = None) -> list[Decision]:
    batch_map = {row["scan_batch_id"]: row for row in batches}
    originals: dict[str, list[Mapping]] = {}
    for label in labels:
        if label.get("relabel_of") is None and type(label.get("shadow_tier")) is int and label["shadow_tier"] == 1:
            originals.setdefault(label["candidate_id"], []).append(label)
    output = []
    for candidate in candidates:
        rows = originals.get(candidate["candidate_id"], [])
        if type(candidate.get("shadow_tier")) is not int or candidate["shadow_tier"] != 1 or not rows:
            continue
        batch = batch_map[candidate["scan_batch_id"]]
        label, reason = _original(rows, "label_frozen_at_utc")
        try:
            if (_instant(label.get("label_frozen_at_utc")) != _instant(candidate.get("label_frozen_at_utc")) or
                    label.get("scan_batch_id") != batch["scan_batch_id"] or
                    label.get("symbol") != candidate.get("symbol")):
                reason = "identity_mismatch"
        except (ValueError, TypeError, OverflowError):
            reason = "identity_mismatch"
        output.append(_record("shadow", "", candidate, batch, "candidate_labels", label["label_id"],
                              "exact_label_v1", (source_commits or {}).get(batch["scan_batch_id"]),
                              exact=label.get("label_frozen_at_utc"), reason=reason))
    return output


def _bounds(record: Decision) -> tuple[datetime, datetime]:
    if record.decision_at_utc is not None:
        at = _instant(record.decision_at_utc)
        return at, at
    return _instant(record.lower_bound_utc), _instant(record.upper_bound_utc)


def deduplicate(records: Iterable[Decision]) -> list[Decision]:
    """Reconcile new + already enrolled records without reviving unavailable rows.

    Callers must pass the entire competing census, not just the current scan.
    Nonqualifying evaluator rows remain measurable but do not consume ranking
    keys. The original records and their immutable windows are never mutated.
    """
    unique: dict[tuple, Decision] = {}
    for record in records:
        previous = unique.get(record.identity)
        if previous is not None:
            # A previously unavailable identity never becomes eligible on retry.
            if previous.status == "unavailable":
                continue
            if record.status == "unavailable":
                unique[record.identity] = record
                continue
            if replace(previous, status="ready", reason=None) != replace(record, status="ready", reason=None):
                raise ValueError("decision_identity_changed")
            continue
        unique[record.identity] = record
    groups: dict[tuple, list[Decision]] = {}
    for record in unique.values():
        if record.key is None or (record.population == "evaluator" and not record.ranking_eligible):
            continue
        if record.status in ("ready", "duplicate_in_session") or record.reason == "order_unproven":
            groups.setdefault(record.key, []).append(record)
    for group in groups.values():
        unproven = any(record.reason == "order_unproven" for record in group)
        for i, first in enumerate(group):
            for second in group[i + 1:]:
                if first.decision_at_utc is not None and second.decision_at_utc is not None:
                    continue  # Exact ties use the prescribed row-id tie break.
                same_routing = (first.scan_batch_id == second.scan_batch_id and
                                first.basis.endswith("routing_v1") and second.basis.endswith("routing_v1"))
                if same_routing:
                    continue
                first_lo, first_hi = _bounds(first)
                second_lo, second_hi = _bounds(second)
                if not (first_hi < second_lo or second_hi < first_lo):
                    unproven = True
        if unproven:
            for record in group:
                unique[record.identity] = replace(record, status="unavailable", reason="order_unproven")
        else:
            ordered = sorted(group, key=lambda item: (_bounds(item)[0], item.scan_row_id, item.candidate_row_id))
            for index, record in enumerate(ordered):
                unique[record.identity] = replace(record, status="ready" if index == 0 else "duplicate_in_session",
                                                  reason=None if index == 0 else "duplicate_in_session")
    return list(unique.values())
