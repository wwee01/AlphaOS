"""CANARY: the model-drift canary. Replays the frozen golden corpus through
the CURRENT ``PlaybookClassifier`` -- the exact same production call EVAL-1
and the live labeller both use ("one replay engine, one truth") -- weekly,
comparing model-identity fields and label outputs against the ONE pinned
baseline run. Answers only "did the configured model change under us?", NOT
"is this prompt better?" (that's EVAL-1) -- but the two share corpus
machinery by design (see alphaos/canary/corpus.py). Zero decision surface on
the LIVE trading path: never read by any gate/eval/labeller/risk/execution
path. Correction (audit finding, 2026-08-03): this is narrower than "never
read by any code" -- ``alphaos/scheduler/shadow_label.py``'s
``check_auto_suspend`` (EXP-1, itself a shadow/measurement-layer mechanism,
not a live decision path) DOES read ``canary_runs.drift_tier`` to decide
whether to suspend shadow labelling. That is the one deliberate, named
exception; see that function's own docstring for the semantics.
**SUSP-1 (docs/roadmap/alphaos-susp1-canary-aware-suspend-spec.md, merged
2026-08) resolved the semantics gap this note used to carry forward**: the
consumer now reads ``confirmation_of`` (trigger rows only) and this module's
own ``drift_detail_json -> confirmation.status`` annotation (written by
``run_canary_confirmed`` below) within a recency window, instead of latching
on any historical TIER_1 row forever.

CANARY-2 (docs/roadmap/alphaos-canary2-drift-confirmation-spec.md) adds a
caller-layer confirmation policy on top of the above, in ``run_canary_confirmed``:
a confirmable trip (TIER_1/failsafe-only or TIER_2/label-drift -- both
sampling-sensitive) is re-run once, same-day, same process, before paging;
an identity change (TIER_1/identity, deterministic) still pages immediately.
The ONE fail direction that shapes every branch: any failure in the
confirmation machinery degrades to paging the ORIGINAL trip immediately,
marked UNCONFIRMED -- never to silence. ``run_canary`` itself keeps its
original, unconditional "page on any TIER_1/TIER_2 trip" behavior when
called directly (unchanged contract, still exercised by CANARY's own direct
tests and by manual `alphaos canary_run`) -- ``run_canary_confirmed`` calls
it with ``suppress_alert=True`` and owns all paging decisions itself, so the
weekly scheduled job (the only caller of ``run_canary_confirmed`` -- see
``alphaos/scheduler/jobs.py::run_canary_run_job``) never double-pages.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from alphaos import lineage
from alphaos.canary.corpus import CorpusTamperedError, DEFAULT_CORPUS_DIR, load_corpus
from alphaos.constants import LabelSource, Severity
from alphaos.scanner.candidate_packet import reconstruct_from_stored
from alphaos.scheduler import cost_guard
from alphaos.util import alerts, timeutils
from alphaos.util.ids import new_id

DRIFT_TIER_1 = "TIER_1"
DRIFT_TIER_2 = "TIER_2"
DRIFT_TIER_3 = "TIER_3"
DRIFT_NONE = "none"

# CANARY-2 Design 1: the two TIER_1 trigger classes. Identity is deterministic
# (no sampling involved -- the model literally reports a different name/
# fingerprint) and pages immediately; a fail-safe appearance CAN be a sampling
# phenomenon (proven 2026-08-02: COST's verbosity tail crossed the token
# ceiling once in nine runs) and must be confirmed first, same as TIER_2.
CANARY_TRIGGER_IDENTITY = "identity"
CANARY_TRIGGER_FAILSAFE = "failsafe"

# CANARY-2 Design 3: the stable/boundary split, "canary_stability_v1", frozen
# 2026-08-03 from the full 9-run characterization vs baseline
# canaryrun_de5814877cc1 (see the spec's own "Motivating evidence" section).
# A packet is STABLE iff its primary_label never differed from baseline in
# any of the 9 non-baseline runs. Membership is FROZEN -- changing it means
# a new version (canary_stability_v2), a new registration, never an in-place
# edit (same versioning law as regime_rules_v1/trend_rules_v1). The 13
# stable ids are copied verbatim from the spec (its literal registration).
CANARY_STABILITY_VERSION = "canary_stability_v1"

CANARY_STABLE_PACKETS_V1 = frozenset({
    "pkt_077c103ae10b", "pkt_1eeb01e36b6d", "pkt_25ad8d71eedf", "pkt_31bc21eccf10",
    "pkt_33cc39aec8c7", "pkt_4b424842d943", "pkt_a7c1bcae7175", "pkt_af6a10c6ea2b",
    "pkt_d27d1d035916", "pkt_d74e3608adc2", "pkt_d9bc552ddc8e", "pkt_f182ebbce06b",
    "pkt_fe03feceab9c",
})

# The other 7 packets of the frozen 20-packet v1 corpus (data/canary/,
# MANIFEST-pinned) -- derived here (corpus packet ids minus
# CANARY_STABLE_PACKETS_V1), computed 2026-08-03, and frozen the same way:
# these are the ids that WERE in the corpus at v1-registration time and are
# NOT stable. A packet absent from BOTH this set and the stable set (i.e.
# added to the corpus after this registration) buckets as "unclassified_new"
# (Design 3) -- never silently folded into either bucket.
CANARY_BOUNDARY_PACKETS_V1 = frozenset({
    "pkt_5f60f021b855", "pkt_660af7b8b2c0", "pkt_789ce514f70a", "pkt_7948511988dc",
    "pkt_ab841c53a1f3", "pkt_c089156ab4e0", "pkt_e91598e03a4b",
})


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


def _stability_bucket(packet_id: str) -> str:
    """CANARY-2 Design 3: classify one packet id against the frozen
    ``canary_stability_v1`` registration. Any id absent from BOTH frozen sets
    (added to the corpus after the v1 registration) is ``unclassified_new`` --
    never silently folded into stable or boundary (test 9)."""
    if packet_id in CANARY_STABLE_PACKETS_V1:
        return "stable"
    if packet_id in CANARY_BOUNDARY_PACKETS_V1:
        return "boundary"
    return "unclassified_new"


def _label_comparison(current_by_packet: dict, baseline_by_packet: dict, label_diff_pct: float) -> Optional[dict]:
    """CANARY-2 Design 3: computed "wherever mismatches are computed (all
    tiers, including clean runs -- the split is cheap and the history is
    useful)" -- i.e. unconditionally whenever there is at least one
    comparable (packet present in both current and baseline) packet, not
    only when the TIER_2 threshold is crossed. Returns ``None`` when nothing
    is comparable (empty intersection), same as the pre-CANARY-2 code's own
    ``compared == 0`` no-op."""
    compared = label_mismatches = decision_mismatches = 0
    stable_total = boundary_total = unclassified_new_total = 0
    stable_flips = boundary_flips = unclassified_new_flips = 0
    for packet_id, cur in current_by_packet.items():
        base = baseline_by_packet.get(packet_id)
        if base is None:  # corpus grew/shrank since baseline -- compare only the intersection
            continue
        compared += 1
        bucket = _stability_bucket(packet_id)
        if bucket == "stable":
            stable_total += 1
        elif bucket == "boundary":
            boundary_total += 1
        else:
            unclassified_new_total += 1

        flipped = cur["primary_label"] != base["primary_label"]
        if flipped:
            label_mismatches += 1
            if bucket == "stable":
                stable_flips += 1
            elif bucket == "boundary":
                boundary_flips += 1
            else:
                unclassified_new_flips += 1
        if cur["label_decision"] != base["label_decision"]:
            decision_mismatches += 1

    if compared == 0:
        return None
    label_rate = label_mismatches / compared
    decision_rate = decision_mismatches / compared
    return {
        "compared": compared, "label_mismatches": label_mismatches,
        "label_mismatch_rate": round(label_rate, 3), "decision_mismatches": decision_mismatches,
        "decision_mismatch_rate": round(decision_rate, 3), "threshold": label_diff_pct,
        "stable_flips": stable_flips, "stable_total": stable_total,
        "boundary_flips": boundary_flips, "boundary_total": boundary_total,
        "unclassified_new": unclassified_new_total, "unclassified_new_flips": unclassified_new_flips,
        "tripped": bool(label_rate >= label_diff_pct or decision_rate >= label_diff_pct),
    }


def _compute_drift(
    current_agg: dict, current_by_packet: dict, baseline_run: Optional[dict],
    baseline_by_packet: dict, label_diff_pct: float, confidence_shift_band: float,
) -> tuple:
    """Returns ``(drift_tier, detail_dict)``. Checked in severity order --
    Tier 1 (identity/failsafe) short-circuits before Tier 2/3 are even
    computed, since a changed model identity already explains any downstream
    label movement (mirrors the spec's own D4 lineage-joint rationale: a
    silent model shift that also moves behavior must be attributed to the
    model, never double-counted as a separate label-drift finding). CANARY-2
    preserves this exact short-circuit RETURN order -- it only ADDS a
    ``detail["trigger_class"]`` tag (identity/failsafe) to the Tier 1 branch,
    for the caller layer's confirmation-vs-immediate-page policy. Audit fix
    (2026-08-03, both reviewers/ALSO FIX 4): the stable/boundary split
    (Design 3 -- "computed wherever mismatches are computed ... including
    clean runs") is now computed BEFORE the Tier-1 check, so it lands in
    ``detail`` on every tier including TIER_1 -- the split's own COMPUTATION
    is unconditional, only the TIER DECISION still short-circuits at Tier 1
    exactly as before (a label_drift block being present, even "tripped",
    never itself promotes a TIER_1 result to TIER_2)."""
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

    # ALSO FIX 4: computed here, BEFORE the Tier-1 return, so it lands in
    # drift_detail on every tier -- the decision below still only acts on it
    # when Tier 1 did NOT already fire, preserving the exact prior severity
    # order for TIER DECISIONS.
    label_comparison = _label_comparison(current_by_packet, baseline_by_packet, label_diff_pct)
    if label_comparison is not None:
        detail["label_drift"] = label_comparison

    if identity_changed or failsafe_appeared:
        # Identity takes priority when both fire together: it's the more
        # certain, deterministic signal, and per policy pages immediately
        # regardless of whether a failsafe also appeared this run.
        detail["trigger_class"] = CANARY_TRIGGER_IDENTITY if identity_changed else CANARY_TRIGGER_FAILSAFE
        return DRIFT_TIER_1, detail

    if label_comparison is not None and label_comparison["tripped"]:
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


def run_canary(
    journal, settings, corpus_dir: Optional[str] = None,
    confirmation_of: Optional[str] = None, suppress_alert: bool = False,
) -> dict:
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
    key would be swallowed into a 'completed' job_runs row instead).

    CANARY-2 additions, both opt-in and both default to the pre-CANARY-2
    behavior so every existing direct call/test of this function is
    unaffected:

    - ``confirmation_of``: when set, this run IS a same-day confirmation
      replay of the named trigger run_id -- stamped onto the new
      ``canary_runs.confirmation_of`` column for lineage/query, and (the
      structural half of the loop guard) this function never itself reads
      or acts on the column, so a confirmation run cannot cascade into
      spawning another confirmation -- only ``run_canary_confirmed`` (never
      called with ``confirmation_of`` set) initiates confirmation replays.
    - ``suppress_alert``: when True, ``run_canary`` computes and stores
      drift exactly as always but never calls ``alerts.send_alert`` itself
      -- the caller (``run_canary_confirmed``) owns 100% of the paging
      decision instead. Defaults False so a direct call (manual
      `alphaos canary_run`, or any pre-CANARY-2 test) keeps its original
      "page immediately on Tier 1/2" contract unchanged."""
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
        "confirmation_of": confirmation_of,
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

        if drift_tier in (DRIFT_TIER_1, DRIFT_TIER_2) and not suppress_alert:
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


# ------------------------------------------------------ CANARY-2 confirmation

# Severity order for "did the confirmation trip the same class or worse?"
# (spec Design 2) -- lower rank is more severe. TIER_1 (identity/failsafe)
# outranks TIER_2 (label drift), which outranks TIER_3, which outranks a
# clean run.
_TIER_RANK = {DRIFT_TIER_1: 1, DRIFT_TIER_2: 2, DRIFT_TIER_3: 3, DRIFT_NONE: 4}


def _update_drift_detail(journal, run_id: str, detail: dict) -> None:
    """Annotates a TRIGGER run's own ``drift_detail_json`` with its eventual
    confirmation outcome (a ``"confirmation"`` sub-dict), after the fact.
    This is why ``canary_status``/the daily brief can read the confirmed
    state directly off the trigger row (Design 5) without any special-casing
    in the report layer -- the trigger row IS the confirmed state once this
    runs."""
    journal.conn.execute(
        "UPDATE canary_runs SET drift_detail_json = ? WHERE run_id = ?",
        (json.dumps(detail), run_id),
    )
    journal.conn.commit()


def _split_summary_text(detail: dict) -> str:
    """The spec's own fixed interpretive sentence (Design 3, verbatim --
    neutrality-disciplined: states a measured fact, prescribes nothing).
    ALSO FIX 7: renders the unclassified_new bucket too, when nonzero --
    spec says "rendered", not just carried in the JSON dump."""
    label_drift = detail.get("label_drift") or {}
    segments = [
        f"{label_drift.get('stable_flips', 0)}/{label_drift.get('stable_total', 0)} stable",
        f"{label_drift.get('boundary_flips', 0)}/{label_drift.get('boundary_total', 0)} boundary",
    ]
    unclassified_total = label_drift.get("unclassified_new", 0)
    if unclassified_total:
        segments.append(f"{label_drift.get('unclassified_new_flips', 0)}/{unclassified_total} new-unclassified")
    return (
        "flips: " + ", ".join(segments) + ". "
        "Stable-packet flips are high-signal (never flipped in the 9-run characterization); "
        "boundary flips are the labeller's known decision-boundary wobble."
    )


def _tier_headline(tier: str, detail: dict) -> str:
    """One self-describing sentence for whichever concrete signal a (tier,
    detail) pair actually carries. Branches on the TIER itself, not on
    inferred detail shape -- ALSO FIX 4 means ``label_drift`` is now present
    on every tier's detail, so "label_drift present" alone no longer implies
    TIER_2. Used for BOTH the trigger side and the confirmation side of a
    confirmed page (MUST FIX 3, audit 2026-08-03): a TIER_2 trigger confirmed
    by a TIER_1/identity re-run must never render a literal
    "confirmation=None" for the identity diff -- the single most actionable
    fact -- it must fall back to THIS, the confirmation run's own headline
    fact, read from its own detail."""
    if tier == DRIFT_TIER_1:
        identity = detail.get("identity_change")
        if identity:
            return (
                f"{tier}/identity: response_model {identity.get('baseline_response_models')} -> "
                f"{identity.get('current_response_models')}, system_fingerprint "
                f"{identity.get('baseline_system_fingerprints')} -> {identity.get('current_system_fingerprints')}"
            )
        failsafe = detail.get("failsafe_rate_change")
        if failsafe:
            return (
                f"{tier}/failsafe: parse/fail-safe rate {failsafe.get('baseline_rate')} -> "
                f"{failsafe.get('current_rate')}"
            )
        return f"{tier}: (no identity/failsafe detail recorded on this run)"
    if tier == DRIFT_TIER_2:
        label_drift = detail.get("label_drift") or {}
        return (
            f"{tier}/label drift: {label_drift.get('label_mismatches')}/{label_drift.get('compared')} "
            f"mismatches ({label_drift.get('label_mismatch_rate')})"
        )
    return f"{tier}"


def _worse_tier(tier_a: str, tier_b: str) -> str:
    """Lower _TIER_RANK is more severe (worse)."""
    return tier_a if _TIER_RANK[tier_a] <= _TIER_RANK[tier_b] else tier_b


def _confirmed_message_body(drift_tier: str, detail: dict, confirm_tier: str, confirm_detail: dict) -> str:
    """MUST FIX 1 + 3 (audit 2026-08-03): a confirmed page's body must never
    assume the confirmation shares the trigger's own signal shape -- the
    rank-only "confirmed" check that used to gate this function's caller
    already let a TIER_1/failsafe trigger get cross-confirmed by a TIER_2
    label-drift re-run (or vice versa), and the OLD renderer here silently
    produced "confirmation=None" for that case because it only ever looked
    at ``detail["label_drift"]``/``detail["failsafe_rate_change"]`` (the
    TRIGGER's own shape), never the confirmation's.

    Same-class case (both TIER_2) keeps the spec's own literal Design-3
    format verbatim (test 4's own literal assertions pin this). Every other
    combination (TIER_1+TIER_1, TIER_1+TIER_2, TIER_2+TIER_1) states BOTH
    tiers/classes explicitly, each read from its OWN detail -- never a
    borrowed or absent one -- plus the stable/boundary split from whichever
    side is TIER_2, if either is."""
    if drift_tier == DRIFT_TIER_2 and confirm_tier == DRIFT_TIER_2:
        trigger_label_drift = detail.get("label_drift") or {}
        confirm_label_drift = confirm_detail.get("label_drift") or {}
        return (
            f"mismatch counts: trigger={trigger_label_drift.get('label_mismatches')}, "
            f"confirmation={confirm_label_drift.get('label_mismatches')}. {_split_summary_text(detail)}"
        )
    parts = [
        f"trigger {_tier_headline(drift_tier, detail)}; "
        f"confirmation {_tier_headline(confirm_tier, confirm_detail)}."
    ]
    if drift_tier == DRIFT_TIER_2:
        parts.append(_split_summary_text(detail))
    elif confirm_tier == DRIFT_TIER_2:
        parts.append(_split_summary_text(confirm_detail))
    return " ".join(parts)


def _log_canary_event_best_effort(
    journal, severity: Severity, message: str, detail: Optional[dict] = None,
) -> None:
    """MUST FIX 2 (audit 2026-08-03, both reviewers): mirrors
    ``JobRunner._log_failure_best_effort``'s own established try/except
    pattern (job_runner.py) -- a momentarily-locked DB (a live failure mode)
    must never crash the confirmation flow on top of, or instead of, the
    page it exists to protect. Audit-proven: without this wrap, a locked-DB
    write here escaped ``run_canary_confirmed`` entirely and turned the
    highest-stakes page in the system (the drift page itself, including the
    deterministic identity-immediate path) into a content-free generic
    "AlphaOS job failed: canary_run" alert. Audit logging is best-effort and
    must never gate/mask the actual alert send."""
    try:
        journal.log_system_event(severity, "canary", message, detail)
    except Exception:  # noqa: BLE001 -- best-effort, see docstring
        pass


def _update_drift_detail_best_effort(journal, run_id: str, detail: dict) -> None:
    """Same best-effort law as ``_log_canary_event_best_effort``, applied to
    the lineage annotation UPDATE -- and (see call sites below) always
    invoked AFTER the page has already been sent, never before, so a
    locked-DB failure here can degrade the report/brief layer's lineage
    accuracy but can never suppress a page that was otherwise ready to
    send."""
    try:
        _update_drift_detail(journal, run_id, detail)
    except Exception:  # noqa: BLE001 -- best-effort, see docstring
        pass


def _send_drift_alert(journal, settings, title: str, message: str) -> None:
    """CANARY-2's own event-before-send discipline, reusing
    ``cards/demotion.py``'s established pattern: the durable system_events
    audit row for a page is journaled BEFORE the network call, so the record
    survives even if ``alerts.send_alert`` itself fails or is unreachable
    (test 12 -- "the confirmed-drift system_event persists"). MUST FIX 2:
    the journal write itself is now best-effort -- it must record the page
    when it can, but a DB hiccup here must never prevent the send that
    follows it (the PAGE is the protected asset, not the audit row)."""
    _log_canary_event_best_effort(journal, Severity.WARNING, message, {"title": title})
    alerts.send_alert(settings, title=title, message=message, priority="high", journal=journal)


def run_canary_confirmed(journal, settings, corpus_dir: Optional[str] = None) -> dict:
    """CANARY-2's caller-layer confirmation policy (spec Design 1/2), wrapping
    ``run_canary``. This is the ONLY intended entry point for the weekly
    scheduled job (``alphaos/scheduler/jobs.py::run_canary_run_job``) -- the
    confirmation happens inside this SAME process/caller layer, never as a
    second scheduler job type or lock key. Manual `alphaos canary_run` keeps
    calling ``run_canary`` directly (unconfirmed, unchanged) via
    ``Orchestrator.canary_run`` -- see that CLI command's own help text for
    the explicit "no confirmation policy" callout (ALSO FIX 9).

    Policy (spec's own table):
      TIER_1 / identity   -> page immediately, no confirmation run.
      TIER_1 / failsafe-only -> confirm first (or unconfirmed-page on failure).
      TIER_2 / label drift    -> confirm first (or unconfirmed-page on failure).
      TIER_3 / none            -> never paged (unchanged).

    The ONE law that shapes every branch below (spec's "fail direction"):
    any failure in the confirmation machinery -- cost preflight refusal,
    exception, corpus tamper, anything -- pages the ORIGINAL trip
    immediately, marked UNCONFIRMED. Never silence. MUST FIX 5 (audit
    2026-08-03): a ``CorpusTamperedError`` on the confirmation run is paged
    UNCONFIRMED first, exactly like every other confirmation failure, but
    THEN re-raised -- ``run_canary``'s own loud-failure/fuse-eligible
    contract for a tampered corpus (spec non-goals: unchanged) must survive
    even when the tamper is discovered on a confirmation replay rather than
    the first run of the week; a returned ``"error"`` key here would let
    ``JobRunner`` mark the job merely 'completed', exactly the swallow
    ``tests/test_canary.py``'s own
    ``test_run_canary_propagates_corpus_tampered_error_uncaught`` exists to
    prevent for the single-run case.

    MUST FIX 1 (audit 2026-08-03, CONVERGENT, both reviewers): "confirmed"
    means the confirmation run tripped ANY pageable tier (TIER_1 or TIER_2),
    not "the same or a numerically worse tier than the trigger". The old
    rank-only rule treated TIER_1/failsafe and TIER_2/label-drift as one
    ordered ladder -- a TIER_1/failsafe trigger confirmed by a TIER_2
    re-run (two consecutive genuinely pageable trips) fell through to "not
    confirmed", got journaled as "transient wobble" (factually false: the
    re-run DID trip), and produced zero pages. Auditor B's correlation
    argument: a real model swap that pushes verbosity past the token ceiling
    (failsafe) is the same swap that moves labels (TIER_2) -- this was
    preferentially open in exactly the scenario CANARY exists to catch.
    TIER_2 -> TIER_3 and TIER_2 -> clean remain correctly "not confirmed"
    (neither is pageable).

    The loop guard is structural, not conventional: the confirmation replay
    is always a raw ``run_canary(confirmation_of=...)`` call, never a
    recursive ``run_canary_confirmed`` call -- a confirmation run therefore
    cannot itself spawn a confirmation, independent of anything this
    function computes (exactly one confirmation per trigger run, ever)."""
    result = run_canary(journal, settings, corpus_dir=corpus_dir, suppress_alert=True)
    result["paged"] = False
    result["confirmation_run_id"] = None
    if "error" in result:
        # The FIRST run itself couldn't execute (empty corpus / cost-cap
        # refusal) -- pre-existing behavior, unrelated to CANARY-2's
        # confirmation policy (which only concerns a CONFIRMATION run's own
        # failure). Nothing tripped, nothing to confirm, nothing to page.
        return result

    drift_tier = result["drift_tier"]
    detail = result.get("drift_detail") or {}
    trigger_run_id = result["run_id"]
    baseline_run = get_baseline_run(journal)
    baseline_run_id = baseline_run["run_id"] if baseline_run else None

    if drift_tier == DRIFT_TIER_1 and detail.get("trigger_class") == CANARY_TRIGGER_IDENTITY:
        title = f"AlphaOS CANARY: model drift detected ({drift_tier})"
        message = (
            f"Weekly canary run {trigger_run_id} flagged {drift_tier}/identity drift vs the pinned "
            f"baseline ({baseline_run_id or '?'}) -- deterministic model-identity change, no "
            f"confirmation needed. {json.dumps(detail)}"
        )
        _send_drift_alert(journal, settings, title, message)
        result["paged"] = True
        # ALSO FIX 6: lineage-annotate even the immediate-page path, so a
        # trigger row is never indistinguishable from a raw pre-CANARY-2
        # trip -- best-effort, AFTER the send (never gates it).
        _update_drift_detail_best_effort(
            journal, trigger_run_id, {**detail, "confirmation": {"status": "identity_immediate"}},
        )
        return result

    if drift_tier not in (DRIFT_TIER_1, DRIFT_TIER_2):
        return result  # TIER_3 / none -- never paged, unchanged

    trigger_class = detail.get("trigger_class", "label_drift")
    _log_canary_event_best_effort(
        journal, Severity.WARNING,
        f"drift trip pending confirmation: {drift_tier}/{trigger_class}, run {trigger_run_id}",
        {"run_id": trigger_run_id, "drift_tier": drift_tier, "trigger_class": trigger_class},
    )

    confirm_result: Optional[dict] = None
    confirm_error: Optional[str] = None
    confirm_exc: Optional[BaseException] = None
    try:
        confirm_result = run_canary(
            journal, settings, corpus_dir=corpus_dir,
            confirmation_of=trigger_run_id, suppress_alert=True,
        )
        if confirm_result.get("error"):
            confirm_error = confirm_result["error"]
    except Exception as exc:  # noqa: BLE001 -- fail TOWARD paging (the one law); never swallow
        confirm_error = f"{type(exc).__name__}: {exc}"
        confirm_exc = exc

    if confirm_error is not None:
        _log_canary_event_best_effort(
            journal, Severity.ERROR,
            f"canary confirmation run for {trigger_run_id} could not execute: {confirm_error} "
            "-- paging the original trip UNCONFIRMED.",
            {"run_id": trigger_run_id, "reason": confirm_error},
        )
        title = f"AlphaOS CANARY: model drift detected, UNCONFIRMED ({drift_tier})"
        message = (
            f"Weekly canary run {trigger_run_id} flagged {drift_tier} drift vs the pinned baseline "
            f"({baseline_run_id or '?'}). UNCONFIRMED -- confirmation run could not execute: "
            f"{confirm_error}. {json.dumps(detail)}"
        )
        _send_drift_alert(journal, settings, title, message)
        result["paged"] = True
        result["unconfirmed"] = True
        _update_drift_detail_best_effort(
            journal, trigger_run_id,
            {**detail, "confirmation": {"status": "unconfirmed_page", "reason": confirm_error}},
        )
        if isinstance(confirm_exc, CorpusTamperedError):
            # MUST FIX 5: page first (done above), THEN re-raise -- order
            # matters. The UNCONFIRMED page is durable and sent regardless;
            # this re-raise is what makes JobRunner mark the job
            # failed/fuse-eligible, matching run_canary's own frozen
            # loud-failure contract for a tampered corpus.
            raise confirm_exc
        return result

    if confirm_result is None:
        # Structurally unreachable: confirm_error is None here only when the
        # try block above completed without raising AND without an "error"
        # key, which is the one path that assigns confirm_result. Made an
        # explicit loud failure rather than a bare `assert` (ALSO FIX 8 --
        # asserts strip under -O, and this is a safety-relevant path where
        # silently falling through would be worse than a clear crash).
        raise RuntimeError(
            "run_canary_confirmed: unreachable state -- confirm_error is None but "
            "confirm_result is None (structural invariant violated)"
        )

    confirm_tier = confirm_result["drift_tier"]
    confirm_detail = confirm_result.get("drift_detail") or {}
    confirm_run_id = confirm_result["run_id"]
    result["confirmation_run_id"] = confirm_run_id
    # MUST FIX 1: "confirmed" = the confirmation tripped ANY pageable tier,
    # not "the same-or-worse tier on one ladder" (see the function docstring
    # for the full cross-class-hole rationale).
    confirmed = confirm_tier in (DRIFT_TIER_1, DRIFT_TIER_2)

    if confirmed:
        worse = _worse_tier(drift_tier, confirm_tier)
        title = f"AlphaOS CANARY: model drift CONFIRMED ({worse})"
        message = (
            f"Weekly canary run {trigger_run_id} (trigger {drift_tier}) flagged drift vs the pinned "
            f"baseline ({baseline_run_id or '?'}), confirmed by same-day re-run {confirm_run_id} "
            f"({confirm_tier}). {_confirmed_message_body(drift_tier, detail, confirm_tier, confirm_detail)} "
            f"{json.dumps({'trigger_detail': detail, 'confirmation_detail': confirm_detail})}"
        )
        _send_drift_alert(journal, settings, title, message)
        result["paged"] = True
        result["confirmed"] = True
        _update_drift_detail_best_effort(journal, trigger_run_id, {**detail, "confirmation": {
            "status": "confirmed", "confirming_run_id": confirm_run_id, "confirming_drift_tier": confirm_tier,
        }})
    else:
        # Only reachable when confirm_tier is TIER_3 or none -- genuinely
        # non-pageable, so "transient wobble / not confirmed" stays accurate
        # (MUST FIX 1's own requirement: never emit this language when the
        # confirmation run's own drift_tier IS pageable).
        _log_canary_event_best_effort(
            journal, Severity.WARNING,
            f"transient wobble -- trip not confirmed: run {trigger_run_id} tripped {drift_tier}, "
            f"confirmation run {confirm_run_id} did not ({confirm_tier})",
            {"run_id": trigger_run_id, "confirming_run_id": confirm_run_id,
             "original_tier": drift_tier, "confirming_tier": confirm_tier},
        )
        result["paged"] = False
        result["confirmed"] = False
        _update_drift_detail_best_effort(journal, trigger_run_id, {**detail, "confirmation": {
            "status": "not_confirmed", "confirming_run_id": confirm_run_id, "confirming_drift_tier": confirm_tier,
        }})

    return result
