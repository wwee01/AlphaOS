"""TASK-R: retro-relabel of the contaminated 2026-07-01 baseline.

One-off CLI task (kept as standing code; not a scheduler job, never a
generalized "relabeling framework" -- operator-passed date ranges only,
per the spec's own explicit non-goal). Replays each stored ``packet_json``
for candidate_packets rows in an operator-given date range through the
CURRENT playbook classifier -- never a re-scan, never a second replay
engine ("one replay engine, one truth", the same law PR8's attribution
ledger and EVAL-1's harness both already follow -- this reuses the exact
same reconstruction helper EVAL-1 does).

Never modifies/overwrites an original evaluation (append-only law); never
touches any decision/outcome/attribution row. A new row's ``relabel_of``
points back at the label it cleanly replays; the original is left exactly
as it was.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from alphaos.ai import prompt_templates as pt
from alphaos.constants import OFFICIAL_LABELS, Severity
from alphaos.scanner.candidate_packet import reconstruct_from_stored
from alphaos.scheduler import cost_guard
from alphaos.util import timeutils


def _packets_in_range(journal, date_from: str, date_to: str) -> list:
    """candidate_packets rows whose SGT calendar date falls within
    [date_from, date_to] inclusive -- matches this codebase's own
    "which day did this happen" convention (market_date()/created_at_sgt),
    not raw UTC, which can straddle a calendar day either side of SGT."""
    return journal.query(
        "SELECT packet_id, candidate_id, interest_rank, packet_json, symbol, scan_batch_id "
        "FROM candidate_packets WHERE substr(created_at_sgt, 1, 10) BETWEEN ? AND ? "
        "ORDER BY created_at_utc ASC",
        (date_from, date_to),
    )


def _latest_label_for_packet(journal, packet_id: str) -> Optional[dict]:
    return journal.one(
        "SELECT * FROM candidate_labels WHERE packet_id = ? ORDER BY id DESC LIMIT 1", (packet_id,),
    )


def relabel_candidates(
    journal, settings, date_from: str, date_to: str, dry_run: bool = False,
) -> dict:
    """Retro-relabel every candidate_packets row in [date_from, date_to].
    ``dry_run=True`` composes and returns the prompts with ZERO network
    calls (the client is never constructed). ``dry_run=False`` persists
    NEW candidate_labels rows via the real ``PlaybookClassifier`` -- the
    standard client path, so cost_guard counts these calls exactly the
    way it counts any other real labeller call. Never raises; returns a
    result dict (with an ``"error"`` key on failure)."""
    import json

    result: dict[str, Any] = {
        "date_from": date_from, "date_to": date_to, "dry_run": dry_run,
        "n_packets": 0, "n_relabelled": 0, "n_corpus_errors": 0, "prompts": [], "diffs": [],
    }

    rows = _packets_in_range(journal, date_from, date_to)
    result["n_packets"] = len(rows)
    if not rows:
        return result

    is_mock = bool(settings.is_mock or not settings.has_openai_key)
    if not dry_run and not is_mock:
        within_budget, detail = cost_guard.check_scan_budget(settings, journal)
        if not within_budget:
            result["error"] = f"AI cost cap reached, refusing to start a live relabel run: {detail}"
            return result

    classifier = None
    if not dry_run:
        from alphaos.ai.playbook_classifier import PlaybookClassifier

        classifier = PlaybookClassifier(settings, journal)

    for row in rows:
        # Isolated per-packet: a malformed/truncated packet_json (never
        # produced by the system itself -- to_prompt_dict() always emits
        # every required field -- but this table is queried, not
        # hand-edited like EVAL-1's corpus fixtures, so this is genuinely
        # defensive rather than an expected path) must count as ONE error
        # and move on to the next packet, never abort the whole run and
        # lose every remaining packet's results. Mirrors EVAL-1's harness,
        # which shares this exact reconstruction helper and already learned
        # this lesson the hard way (see its own docstring).
        try:
            raw_packet_json = row["packet_json"]
            packet_json = (
                json.loads(raw_packet_json) if isinstance(raw_packet_json, str) else (raw_packet_json or {})
            )
            packet = reconstruct_from_stored(
                row["packet_id"], row["candidate_id"], row["interest_rank"], packet_json,
            )
            original = _latest_label_for_packet(journal, row["packet_id"])

            if dry_run:
                prompt = pt.build_label_user_prompt(packet.to_prompt_dict(), sorted(OFFICIAL_LABELS))
                result["prompts"].append(
                    {"packet_id": row["packet_id"], "symbol": row["symbol"], "prompt": prompt}
                )
                continue

            assert classifier is not None  # only None when dry_run, and that branch always continues above
            classification = classifier.classify(packet)
            frozen_at = timeutils.stamp().utc
            new_row = classification.to_row(row["packet_id"], row["scan_batch_id"], frozen_at)
            new_row["relabel_of"] = original["label_id"] if original else None
            journal.insert("candidate_labels", new_row)
            result["n_relabelled"] += 1

            prompt_sha256 = hashlib.sha256(
                pt.build_label_user_prompt(packet.to_prompt_dict(), sorted(OFFICIAL_LABELS)).encode("utf-8")
            ).hexdigest()
            journal.log_system_event(
                Severity.INFO, "relabel",
                f"relabelled {row['symbol']} (packet {row['packet_id']}): "
                f"{(original or {}).get('primary_label')!r} -> {classification.primary_label!r}",
                {
                    "original_id": (original or {}).get("label_id"),
                    "new_id": new_row["label_id"],
                    "prompt_sha256": prompt_sha256,
                },
            )
            result["diffs"].append({
                "symbol": row["symbol"],
                "old_label": (original or {}).get("primary_label"),
                "new_label": classification.primary_label,
                "old_decision": (original or {}).get("label_decision"),
                "new_decision": classification.label_decision,
            })
        except Exception as exc:  # noqa: BLE001 - one bad row must never abort the whole run
            result["n_corpus_errors"] += 1
            journal.log_system_event(
                Severity.ERROR, "relabel",
                f"could not relabel packet {row.get('packet_id', '?')!r}: {exc} -- skipped.",
            )
            continue

    return result
