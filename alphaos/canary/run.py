"""CANARY: the model-drift canary. Replays the frozen golden corpus through
the CURRENT ``PlaybookClassifier`` -- the exact same production call EVAL-1
and the live labeller both use ("one replay engine, one truth") -- weekly,
comparing model-identity fields and label outputs against the ONE pinned
baseline run. Answers only "did the configured model change under us?", NOT
"is this prompt better?" (that's EVAL-1) -- but the two share corpus
machinery by design (see alphaos/canary/corpus.py). Zero decision surface:
never read by any gate/eval/labeller/risk/execution path.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from alphaos import lineage
from alphaos.canary.corpus import DEFAULT_CORPUS_DIR, load_corpus
from alphaos.constants import LabelSource, Severity
from alphaos.scanner.candidate_packet import reconstruct_from_stored
from alphaos.scheduler import cost_guard
from alphaos.util import alerts, timeutils
from alphaos.util.ids import new_id

DRIFT_TIER_1 = "TIER_1"
DRIFT_TIER_2 = "TIER_2"
DRIFT_TIER_3 = "TIER_3"
DRIFT_NONE = "none"


def _reconstruct_packet(fixture: dict):
    """Same shape assumption as EVAL-1's harness: a corpus fixture is already
    packet_id/candidate_id/interest_rank + the flattened packet_json fields
    at its top level, so the fixture dict doubles as the packet_json arg."""
    return reconstruct_from_stored(
        fixture["packet_id"], fixture["candidate_id"], fixture.get("interest_rank"), fixture,
    )


def get_baseline_run(journal) -> Optional[dict]:
    """The ONE pinned reference run, or None if nothing has been pinned yet
    (a fresh install, or the first post-merge run, has nothing to diff
    against -- run_canary reports 'no baseline pinned yet' rather than
    fabricating a drift verdict)."""
    return journal.one("SELECT * FROM canary_runs WHERE is_baseline = 1 ORDER BY id DESC LIMIT 1")


def pin_baseline(journal, run_id: str) -> dict:
    """Marks ``run_id`` as THE baseline every future run diffs against,
    demoting whichever run was previously pinned -- at most one baseline row
    at a time, enforced here (two UPDATEs in one transaction) rather than by
    a DB constraint. Never auto-called by run_canary itself: an operator
    decides when a run is clean enough to become the reference. Never
    raises; returns ``{"error": ...}`` if run_id doesn't exist."""
    existing = journal.one("SELECT run_id FROM canary_runs WHERE run_id = ?", (run_id,))
    if not existing:
        return {"error": f"no canary_runs row with run_id={run_id!r}"}
    journal.conn.execute("UPDATE canary_runs SET is_baseline = 0 WHERE is_baseline = 1")
    journal.conn.execute("UPDATE canary_runs SET is_baseline = 1 WHERE run_id = ?", (run_id,))
    journal.conn.commit()
    return {"pinned_run_id": run_id}


def _results_by_packet(journal, run_id: str) -> dict:
    rows = journal.query(
        "SELECT packet_id, primary_label, label_decision, label_confidence "
        "FROM canary_results WHERE run_id = ?",
        (run_id,),
    )
    return {r["packet_id"]: r for r in rows}


def _json_set(raw: Optional[str]) -> set:
    """Parses a JSON-array column (``response_models_json``/
    ``system_fingerprints_json``) into a set, tolerating every falsy shape
    (``None``, missing, empty string, or the literal JSON ``null``) as "no
    values recorded" rather than raising -- ``_compute_drift`` must never
    raise on a stored value, even a hand-tampered or pre-this-column DB
    row (audit NIT, 2026-07-10: ``json.loads("null")`` returns ``None``,
    and ``set(None)`` raises ``TypeError``)."""
    if not raw:
        return set()
    parsed = json.loads(raw)
    return set(parsed) if parsed else set()


def _compute_drift(
    current_agg: dict, current_by_packet: dict, baseline_run: Optional[dict],
    baseline_by_packet: dict, label_diff_pct: float, confidence_shift_band: float,
) -> tuple:
    """Returns ``(drift_tier, detail_dict)``. Checked in severity order --
    Tier 1 (identity/failsafe) short-circuits before Tier 2/3 are even
    computed, since a changed model identity already explains any downstream
    label movement (mirrors the spec's own D4 lineage-joint rationale: a
    silent model shift that also moves behavior must be attributed to the
    model, never double-counted as a separate label-drift finding)."""
    if baseline_run is None:
        return DRIFT_NONE, {"reason": "no baseline pinned yet"}

    detail: dict[str, Any] = {}
    baseline_models = _json_set(baseline_run.get("response_models_json"))
    current_models = _json_set(current_agg["response_models_json"])
    baseline_fps = _json_set(baseline_run.get("system_fingerprints_json"))
    current_fps = _json_set(current_agg["system_fingerprints_json"])
    # Only compare when BOTH sides actually observed a value -- an absent
    # system_fingerprint on either side (a model that never sends it) must
    # never be mistaken for "changed to nothing". Known, accepted
    # consequence (audit LOW, 2026-07-10): a baseline pinned during the
    # mock era (empty identity sets) vs the first real live run also
    # reads as "no comparison possible" rather than Tier 1 -- re-pin the
    # baseline once live to get a real identity reference.
    identity_changed = (
        bool(baseline_models and current_models and baseline_models != current_models)
        or bool(baseline_fps and current_fps and baseline_fps != current_fps)
    )
    if identity_changed:
        detail["identity_change"] = {
            "baseline_response_models": sorted(baseline_models), "current_response_models": sorted(current_models),
            "baseline_system_fingerprints": sorted(baseline_fps), "current_system_fingerprints": sorted(current_fps),
        }

    baseline_n = baseline_run.get("n_prompts") or 0
    baseline_rate = (baseline_run.get("n_parse_or_failsafe") or 0) / baseline_n if baseline_n else 0.0
    current_n = current_agg["n_prompts"]
    current_rate = (current_agg["n_parse_or_failsafe"] / current_n) if current_n else 0.0
    failsafe_appeared = baseline_rate == 0.0 and current_rate > 0.0
    if failsafe_appeared:
        detail["failsafe_rate_change"] = {"baseline_rate": baseline_rate, "current_rate": round(current_rate, 4)}

    if identity_changed or failsafe_appeared:
        return DRIFT_TIER_1, detail

    compared = label_mismatches = decision_mismatches = 0
    for packet_id, cur in current_by_packet.items():
        base = baseline_by_packet.get(packet_id)
        if base is None:  # corpus grew/shrank since baseline -- compare only the intersection
            continue
        compared += 1
        if cur["primary_label"] != base["primary_label"]:
            label_mismatches += 1
        if cur["label_decision"] != base["label_decision"]:
            decision_mismatches += 1
    if compared > 0:
        label_rate = label_mismatches / compared
        decision_rate = decision_mismatches / compared
        if label_rate >= label_diff_pct or decision_rate >= label_diff_pct:
            detail["label_drift"] = {
                "compared": compared, "label_mismatches": label_mismatches,
                "label_mismatch_rate": round(label_rate, 3), "decision_mismatches": decision_mismatches,
                "decision_mismatch_rate": round(decision_rate, 3), "threshold": label_diff_pct,
            }
            return DRIFT_TIER_2, detail

    baseline_conf = baseline_run.get("mean_confidence")
    current_conf = current_agg.get("mean_confidence")
    if baseline_conf is not None and current_conf is not None:
        shift = abs(current_conf - baseline_conf)
        if shift > confidence_shift_band:
            detail["confidence_shift"] = {
                "baseline_mean_confidence": baseline_conf, "current_mean_confidence": current_conf,
                "shift": round(shift, 4), "band": confidence_shift_band,
            }
            return DRIFT_TIER_3, detail

    return DRIFT_NONE, detail


def run_canary(journal, settings, corpus_dir: Optional[str] = None) -> dict:
    """Replays every corpus packet once through the current playbook
    classifier, storing every result (including fail-safe), then compares
    against the pinned baseline run (if any) and alerts on Tier 1/2 drift.
    Returns a result dict (with an ``"error"`` key on failure -- an empty/
    missing corpus is a safe no-op, same as EVAL-1's empty-corpus handling,
    not a hard failure: an operator hasn't populated ``data/canary/`` yet,
    an expected state until they do).

    Deliberately propagates ``CorpusTamperedError`` uncaught (the ONE
    exception to an otherwise never-raises contract) if ``load_corpus``
    finds a fixture whose content no longer matches its own frozen
    MANIFEST sha256 -- per spec this must be a loud, fuse-eligible job
    failure, which only an uncaught exception reaching
    ``JobRunner.run_job``'s own handler produces (a returned ``"error"``
    key would be swallowed into a 'completed' job_runs row instead)."""
    from alphaos.ai.playbook_classifier import PlaybookClassifier

    corpus_dir = corpus_dir or DEFAULT_CORPUS_DIR
    run_id = new_id("canaryrun")
    result: dict[str, Any] = {
        "run_id": run_id, "n_packets": 0, "n_results": 0,
        "n_fail_safe": 0, "n_corpus_errors": 0, "drift_tier": DRIFT_NONE,
    }

    manifest, packets = load_corpus(corpus_dir)
    result["n_packets"] = len(packets)
    if not packets:
        result["error"] = f"corpus at {corpus_dir!r} is empty or missing -- run canary_corpus_build first"
        return result

    is_mock = bool(settings.is_mock or not settings.has_openai_key)
    if not is_mock:
        within_budget, detail = cost_guard.check_scan_budget(settings, journal)
        if not within_budget:
            result["error"] = f"AI cost cap reached, refusing to start a live canary run: {detail}"
            return result
        # Same pre-flight magnitude check as EVAL-1's run_eval -- this run's
        # overshoot potential is the corpus size (small, ~12-20, but still
        # operator-tunable), not a scan's naturally-bounded shortlist.
        planned_calls = len(packets)
        used = cost_guard.calls_in_last_30_days(journal)
        cap = settings.scheduler_ai_cost_cap_calls_per_30d
        if used + planned_calls > cap:
            result["error"] = (
                f"this run would make {planned_calls} real AI calls, pushing trailing-30-day usage "
                f"to {used + planned_calls} over the {cap} cap ({used} already used) -- refusing to start"
            )
            return result

    classifier = PlaybookClassifier(settings, journal)
    lineage_id = lineage.get_or_create_lineage_id(journal, settings)
    started = timeutils.stamp()
    journal.insert("canary_runs", {
        "run_id": run_id, "corpus_dir": corpus_dir,
        "corpus_version": (manifest or {}).get("version"),
        "configured_model": settings.label_model, "is_mock": 1 if is_mock else 0,
        "n_prompts": len(packets), "lineage_id": lineage_id,
        "started_at_utc": started.utc, "started_at_sgt": started.local_sgt,
    })

    response_models: set = set()
    system_fingerprints: set = set()
    confidences: list = []
    prompt_tokens = completion_tokens = total_tokens = 0

    try:
        for fixture in packets:
            # Same per-packet isolation as EVAL-1's run_eval, same rationale
            # (a hand-edited fixture with a wrong-TYPE field can still raise
            # inside classify()'s mock path even though reconstruction
            # itself succeeded) -- one bad fixture must never abort the
            # whole weekly run.
            try:
                packet = _reconstruct_packet(fixture)
                classification = classifier.classify(packet)
                journal.insert("canary_results", {
                    "result_id": new_id("canaryres"),
                    "run_id": run_id,
                    "packet_id": fixture["packet_id"],
                    "symbol": fixture.get("symbol"),
                    "primary_label": classification.primary_label,
                    "label_decision": classification.label_decision,
                    "label_confidence": classification.confidence,
                    "validation_status": classification.validation_status,
                    "is_failsafe": 1 if classification.label_source == LabelSource.FAIL_SAFE.value else 0,
                    "raw_json": classification.raw or {},
                    "response_model": classification.response_model,
                    "system_fingerprint": classification.system_fingerprint,
                    "prompt_hash": classification.prompt_hash,
                    "system_prompt_hash": classification.system_prompt_hash,
                })
                result["n_results"] += 1
                if classification.label_source == LabelSource.FAIL_SAFE.value:
                    result["n_fail_safe"] += 1
                if classification.response_model:
                    response_models.add(classification.response_model)
                if classification.system_fingerprint:
                    system_fingerprints.add(classification.system_fingerprint)
                confidences.append(classification.confidence)
                prompt_tokens += classification.prompt_tokens or 0
                completion_tokens += classification.completion_tokens or 0
                total_tokens += classification.total_tokens or 0
            except Exception as exc:  # noqa: BLE001 - one bad fixture must never abort the whole run
                result["n_corpus_errors"] += 1
                journal.log_system_event(
                    Severity.ERROR, "canary",
                    f"could not process packet {fixture.get('packet_id', '?')!r} "
                    f"from its corpus fixture: {exc} -- skipped.",
                )
                continue

        current_agg = {
            # audit LOW (2026-07-10): must match canary_runs.n_prompts' OWN
            # meaning (full corpus size, stamped once before this loop even
            # starts -- see the INSERT above) -- NOT result["n_results"]
            # (successful-classifications-only), or the failsafe-rate
            # denominators on the two sides of _compute_drift silently
            # stop meaning the same thing.
            "n_prompts": len(packets),
            "n_parse_or_failsafe": result["n_fail_safe"],
            "response_models_json": json.dumps(sorted(response_models)),
            "system_fingerprints_json": json.dumps(sorted(system_fingerprints)),
            "mean_confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
        }
        current_by_packet = _results_by_packet(journal, run_id)
        baseline_run = get_baseline_run(journal)
        baseline_by_packet = _results_by_packet(journal, baseline_run["run_id"]) if baseline_run else {}
        drift_tier, drift_detail = _compute_drift(
            current_agg, current_by_packet, baseline_run, baseline_by_packet,
            settings.canary_tier2_label_diff_pct, settings.canary_tier3_confidence_shift_band,
        )
        result["drift_tier"] = drift_tier
        result["drift_detail"] = drift_detail

        journal.conn.execute(
            "UPDATE canary_runs SET n_parse_or_failsafe = ?, response_models_json = ?, "
            "system_fingerprints_json = ?, mean_confidence = ?, prompt_tokens = ?, "
            "completion_tokens = ?, total_tokens = ?, drift_tier = ?, drift_detail_json = ? "
            "WHERE run_id = ?",
            (
                result["n_fail_safe"], json.dumps(sorted(response_models)),
                json.dumps(sorted(system_fingerprints)), current_agg["mean_confidence"],
                prompt_tokens, completion_tokens, total_tokens, drift_tier,
                json.dumps(drift_detail), run_id,
            ),
        )
        journal.conn.commit()

        if drift_tier in (DRIFT_TIER_1, DRIFT_TIER_2):
            alerts.send_alert(
                settings,
                title=f"AlphaOS CANARY: model drift detected ({drift_tier})",
                message=(
                    f"Weekly canary run {run_id} flagged {drift_tier} drift vs the pinned baseline "
                    f"({baseline_run['run_id'] if baseline_run else '?'}). {json.dumps(drift_detail)}"
                ),
                priority="high",
                journal=journal,
            )
    finally:
        finished = timeutils.stamp()
        journal.conn.execute(
            "UPDATE canary_runs SET finished_at_utc = ?, finished_at_sgt = ? WHERE run_id = ?",
            (finished.utc, finished.local_sgt, run_id),
        )
        journal.conn.commit()

    return result
