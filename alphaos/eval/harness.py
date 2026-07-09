"""EVAL-1: the replay harness. Reconstructs a real ``CandidatePacket`` from
each frozen corpus fixture and replays it through the CURRENT
``PlaybookClassifier`` -- the exact same production call the labeller uses
at scan time, never a reimplementation ("one replay engine, one truth" --
the same law PR8's attribution ledger follows). Every result is stored,
including fail-safe ones (a discarded fail-safe completion is precisely the
example the harness needs most). Zero decision surface: never read by any
gate/eval/labeller/risk/execution path.
"""

from __future__ import annotations

from typing import Any, Optional

from alphaos import lineage
from alphaos.constants import LabelSource, Severity
from alphaos.eval.corpus import DEFAULT_CORPUS_DIR, load_corpus
from alphaos.scanner.candidate_packet import reconstruct_from_stored
from alphaos.scheduler import cost_guard
from alphaos.util import timeutils
from alphaos.util.ids import new_id


def _reconstruct_packet(fixture: dict):
    """A corpus fixture is already shaped as packet_id/candidate_id/
    interest_rank + the flattened packet_json fields at its top level, so
    the fixture dict itself doubles as the ``packet_json`` argument."""
    return reconstruct_from_stored(
        fixture["packet_id"], fixture["candidate_id"], fixture.get("interest_rank"), fixture,
    )


def run_eval(journal, settings, corpus_dir: Optional[str] = None, repeats: int = 1) -> dict:
    """Replays every corpus packet through the current playbook classifier,
    ``repeats`` times each, storing every result (including fail-safe).
    Never raises; returns a result dict (with an ``"error"`` key on
    failure). A live (non-mock) run checks the SAME trailing-30-day AI cost
    cap the scheduled scan job does, since this reuses the identical real
    API call -- refuses to start (not partial-run) once the cap is reached."""
    from alphaos.ai.playbook_classifier import PlaybookClassifier
    from alphaos.constants import LABEL_VERSION_V1

    corpus_dir = corpus_dir or DEFAULT_CORPUS_DIR
    run_id = new_id("evalrun")
    result: dict[str, Any] = {
        "run_id": run_id, "n_packets": 0, "repeats": repeats,
        "n_results": 0, "n_fail_safe": 0, "n_corpus_errors": 0,
    }

    if repeats < 1:
        result["error"] = f"repeats must be >= 1, got {repeats!r}"
        return result

    manifest, packets = load_corpus(corpus_dir)
    result["n_packets"] = len(packets)
    if not packets:
        result["error"] = f"corpus at {corpus_dir!r} is empty or missing -- run eval_corpus_build first"
        return result

    is_mock = bool(settings.is_mock or not settings.has_openai_key)
    if not is_mock:
        within_budget, detail = cost_guard.check_scan_budget(settings, journal)
        if not within_budget:
            result["error"] = f"AI cost cap reached, refusing to start a live eval run: {detail}"
            return result
        # A scheduled scan's own single-invocation overshoot is bounded by
        # its natural shortlist size; this replay's is packets x repeats,
        # directly operator-tunable into the thousands -- a pre-flight
        # magnitude check (not just "any room at all") keeps one `eval`
        # call from blowing far past the cap in a single run, the way a
        # small scan's check-once-per-scan precedent never has to worry
        # about at this scale.
        planned_calls = len(packets) * repeats
        used = cost_guard.calls_in_last_30_days(journal)
        cap = settings.scheduler_ai_cost_cap_calls_per_30d
        if used + planned_calls > cap:
            result["error"] = (
                f"this run would make {planned_calls} real AI calls ({len(packets)} packets x "
                f"{repeats} repeats), which would push trailing-30-day usage to {used + planned_calls} "
                f"over the {cap} cap ({used} already used) -- refusing to start; lower --repeats or "
                "the corpus size, or wait for cap headroom"
            )
            return result

    classifier = PlaybookClassifier(settings, journal)
    lineage_id = lineage.get_or_create_lineage_id(journal, settings)
    started = timeutils.stamp()
    journal.insert("eval_runs", {
        "run_id": run_id, "corpus_dir": corpus_dir,
        "corpus_version": (manifest or {}).get("version"),
        "label_version": LABEL_VERSION_V1,
        "model": settings.label_model, "is_mock": 1 if is_mock else 0,
        "repeats": repeats, "n_packets": len(packets), "lineage_id": lineage_id,
        "started_at_utc": started.utc, "started_at_sgt": started.local_sgt,
    })

    try:
        for fixture in packets:
            # Isolated per-packet: a malformed/hand-edited fixture (e.g. an
            # operator typo that drops a required field, or changes a
            # field's TYPE rather than removing it) must count as ONE
            # error and move on to the next packet, never abort the whole
            # run and lose every remaining packet's results -- same
            # isolation principle as every other per-item loop in this
            # codebase (TEXT-0's fetch loop, etc). Deliberately wraps BOTH
            # reconstruction AND classify(): PlaybookClassifier.classify()
            # is documented "never raises", but its MOCK path's own
            # unguarded float()/list() coercions can still raise on a
            # wrong-TYPE field that reconstructs fine (Python dataclasses
            # don't enforce field types at construction) and only fails
            # once classify() tries to use it -- audit-caught (correctness
            # pass) as an escape that defeated an earlier, narrower version
            # of this same guard.
            try:
                packet = _reconstruct_packet(fixture)
                for repeat_index in range(repeats):
                    classification = classifier.classify(packet)
                    journal.insert("eval_results", {
                        "result_id": new_id("evalres"),
                        "run_id": run_id,
                        "packet_id": fixture["packet_id"],
                        "symbol": fixture.get("symbol"),
                        "repeat_index": repeat_index,
                        "primary_label": classification.primary_label,
                        "label_decision": classification.label_decision,
                        "label_confidence": classification.confidence,
                        "validation_status": classification.validation_status,
                        "label_source": classification.label_source,
                        "raw_json": classification.raw or {},
                        "model": classification.model,
                        "is_mock": 1 if classification.is_mock else 0,
                        "model_provider": classification.model_provider,
                        "prompt_hash": classification.prompt_hash,
                        "system_prompt_hash": classification.system_prompt_hash,
                    })
                    result["n_results"] += 1
                    if classification.label_source == LabelSource.FAIL_SAFE.value:
                        result["n_fail_safe"] += 1
            except Exception as exc:  # noqa: BLE001 - one bad fixture must never abort the whole run
                result["n_corpus_errors"] += 1
                journal.log_system_event(
                    Severity.ERROR, "eval",
                    f"could not process packet {fixture.get('packet_id', '?')!r} "
                    f"from its corpus fixture: {exc} -- skipped.",
                )
                continue
    finally:
        finished = timeutils.stamp()
        journal.conn.execute(
            "UPDATE eval_runs SET finished_at_utc = ?, finished_at_sgt = ? WHERE run_id = ?",
            (finished.utc, finished.local_sgt, run_id),
        )
        journal.conn.commit()

    return result
