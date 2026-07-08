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
from alphaos.constants import LabelSource
from alphaos.eval.corpus import DEFAULT_CORPUS_DIR, load_corpus
from alphaos.scanner.candidate_packet import PROMPT_KEYS, CandidatePacket
from alphaos.scheduler import cost_guard
from alphaos.util import timeutils
from alphaos.util.ids import new_id


def _reconstruct_packet(fixture: dict) -> CandidatePacket:
    """Rebuilds a CandidatePacket from a frozen corpus fixture -- the exact
    inverse of ``CandidatePacket.to_row()``'s ``packet_json`` (all
    ``PROMPT_KEYS`` fields) plus the ``packet_id``/``candidate_id``/
    ``interest_rank`` columns stored alongside it."""
    kwargs = {k: fixture[k] for k in PROMPT_KEYS if k in fixture}
    return CandidatePacket(
        packet_id=fixture["packet_id"],
        candidate_id=fixture["candidate_id"],
        interest_rank=fixture.get("interest_rank"),
        **kwargs,
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
        "n_results": 0, "n_fail_safe": 0,
    }

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
    finally:
        finished = timeutils.stamp()
        journal.conn.execute(
            "UPDATE eval_runs SET finished_at_utc = ?, finished_at_sgt = ? WHERE run_id = ?",
            (finished.utc, finished.local_sgt, run_id),
        )
        journal.conn.commit()

    return result
