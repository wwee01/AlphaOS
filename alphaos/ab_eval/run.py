"""AB-EVAL-1: orchestrates one A/B replay run -- reads the frozen corpus
(``alphaos.ab_eval.corpus``), replays every (packet, model) pair through the
production evaluate core (``alphaos.ab_eval.replay``), and journals BOTH
the raw and pipeline-final verdict per pair into ``ab_eval_results``.
Shadow, read-only, zero decision surface: nothing here is ever read by the
live scan/gate/risk/execution path -- same law as CANARY/EVAL-1.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from alphaos import lineage
from alphaos.ab_eval.corpus import DEFAULT_CORPUS_DIR, load_corpus
from alphaos.ab_eval.replay import replay_packet
from alphaos.constants import Severity
from alphaos.scheduler import cost_guard
from alphaos.util import timeutils
from alphaos.util.ids import new_id


def run_ab_eval(journal: Any, settings: Any, models: list, corpus_dir: Optional[str] = None) -> dict:
    """Replays the frozen AB-EVAL-1 corpus through every model in
    ``models`` (order preserved -- typically ``[current_primary,
    comparison_model]``). Returns a result dict (``"error"`` key on
    refusal/empty-corpus, same convention as ``run_canary`` -- an operator
    hasn't built the corpus yet, or the cost cap has no room, is a safe
    no-op, never a hard crash).

    Deliberately propagates ``CorpusTamperedError`` uncaught -- see
    ``alphaos/ab_eval/corpus.py``'s own ``CorpusTamperedError`` docstring
    (a loud, fuse-eligible failure; a returned ``"error"`` key would be
    swallowed into a 'completed' job_runs row instead, same law as CANARY).
    """
    corpus_dir = corpus_dir or DEFAULT_CORPUS_DIR
    ab_run_id = new_id("abrun")
    # Dedupe while preserving order (audit NIT, 2026-07-20): a repeated
    # --models entry would double every packet's call count (and the cost
    # preflight's planned_calls) for zero comparative information.
    models = list(dict.fromkeys(models or []))
    result: dict[str, Any] = {
        "ab_run_id": ab_run_id, "n_packets": 0, "n_results": 0,
        "n_corpus_errors": 0, "models": list(models),
    }

    if len(models) < 2:
        result["error"] = "AB-EVAL-1 needs at least 2 distinct models to compare -- refusing to start"
        return result

    manifest, fixtures = load_corpus(corpus_dir)
    result["n_packets"] = len(fixtures)
    if not fixtures:
        result["error"] = f"corpus at {corpus_dir!r} is empty or missing -- run ab_eval_corpus_build first"
        return result

    is_mock = bool(settings.is_mock or not settings.has_openai_key)
    if not is_mock:
        within_budget, detail = cost_guard.check_scan_budget(settings, journal)
        if not within_budget:
            result["error"] = f"AI cost cap reached, refusing to start a live AB-EVAL run: {detail}"
            return result
        # Same pre-flight magnitude check as CANARY/EVAL-1's own run --
        # this run's overshoot potential is n_packets * n_models (both
        # small, operator-tunable), not a scan's naturally-bounded
        # shortlist.
        planned_calls = len(fixtures) * len(models)
        used = cost_guard.calls_in_last_30_days(journal)
        cap = settings.scheduler_ai_cost_cap_calls_per_30d
        if used + planned_calls > cap:
            result["error"] = (
                f"this run would make {planned_calls} real AI calls, pushing trailing-30-day usage "
                f"to {used + planned_calls} over the {cap} cap ({used} already used) -- refusing to start"
            )
            return result

    lineage_id = lineage.get_or_create_lineage_id(journal, settings)
    started = timeutils.stamp()
    journal.insert("ab_eval_runs", {
        "ab_run_id": ab_run_id, "corpus_dir": corpus_dir,
        "manifest_version": (manifest or {}).get("version"),
        "models_json": json.dumps(list(models)), "is_mock": 1 if is_mock else 0,
        "n_packets": len(fixtures), "lineage_id": lineage_id,
        "started_at_utc": started.utc, "started_at_sgt": started.local_sgt,
    })

    try:
        for fixture in fixtures:
            # Per-packet isolation (same law as CANARY/EVAL-1): one bad
            # snapshot row must never abort the whole run. Both models'
            # rows for a packet are collected locally first and only
            # inserted together -- if EITHER model's replay raises, NEITHER
            # row for this packet is journaled (no partial/contaminated
            # packet rows), and the packet is counted as a corpus error.
            try:
                pending_rows = []
                for model in models:
                    replayed = replay_packet(fixture, model, settings, journal)
                    pending_rows.append({
                        "ab_result_id": new_id("abres"),
                        "ab_run_id": ab_run_id,
                        "eval_id": fixture["eval_id"],
                        "symbol": fixture.get("symbol"),
                        "model": model,
                        "raw_decision": replayed.raw.decision,
                        "raw_confidence": replayed.raw.confidence,
                        "raw_entry": replayed.raw.entry,
                        "raw_stop": replayed.raw.stop,
                        "raw_target": replayed.raw.target,
                        "raw_expected_r": replayed.raw.expected_r,
                        "pipeline_decision": replayed.final.decision,
                        "pipeline_expected_r": replayed.final.expected_r,
                        "downgrade_reason": replayed.downgrade_reason,
                        "reasoning_summary": replayed.raw.reasoning_summary,
                        "prompt_tokens": replayed.raw.prompt_tokens,
                        "completion_tokens": replayed.raw.completion_tokens,
                        "total_tokens": replayed.raw.total_tokens,
                        "lineage_id": lineage_id,
                    })
                for row in pending_rows:
                    journal.insert("ab_eval_results", row)
                    result["n_results"] += 1
            except Exception as exc:  # noqa: BLE001 - one bad fixture must never abort the whole run
                result["n_corpus_errors"] += 1
                journal.log_system_event(
                    Severity.ERROR, "ab_eval",
                    f"could not replay packet {fixture.get('eval_id', '?')!r} "
                    f"from its corpus fixture: {exc} -- skipped.",
                )
                continue
    finally:
        finished = timeutils.stamp()
        journal.conn.execute(
            "UPDATE ab_eval_runs SET finished_at_utc = ?, finished_at_sgt = ?, "
            "n_results = ?, n_corpus_errors = ? WHERE ab_run_id = ?",
            (finished.utc, finished.local_sgt, result["n_results"], result["n_corpus_errors"], ab_run_id),
        )
        journal.conn.commit()

    return result
