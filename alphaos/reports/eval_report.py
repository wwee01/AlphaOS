"""EVAL-1: the offline eval harness's report -- parse rate, label agreement
vs the operator-adjudicated ground truth, categorical stability across
repeated samples. Pure read; zero decision surface.
"""

from __future__ import annotations

from typing import Optional

from alphaos.constants import LabelSource
from alphaos.eval.corpus import ground_truth_coverage, load_corpus


def build_eval_report(journal, run_id: Optional[str] = None, corpus_dir: Optional[str] = None) -> dict:
    """Report for one eval run -- defaults to the LATEST run when ``run_id``
    is omitted. ``{"status": "no_runs_yet"}`` is the honest, expected empty
    state (no operator has run ``alphaos eval`` yet), never an error."""
    run = (
        journal.one("SELECT * FROM eval_runs WHERE run_id = ?", (run_id,))
        if run_id else
        journal.one("SELECT * FROM eval_runs ORDER BY id DESC LIMIT 1")
    )
    if not run:
        return {"status": "no_runs_yet"}

    results = journal.query("SELECT * FROM eval_results WHERE run_id = ?", (run["run_id"],))
    n_results = len(results)
    n_parsed = sum(1 for r in results if r["label_source"] != LabelSource.FAIL_SAFE.value)
    parse_rate = round(n_parsed / n_results, 4) if n_results else None

    _, packets = load_corpus(corpus_dir or run["corpus_dir"])
    coverage = ground_truth_coverage(packets)
    gt_by_packet = {
        p["packet_id"]: p["ground_truth_label"] for p in packets if p.get("ground_truth_label")
    }

    agreement_n, agreement_matches = 0, 0
    for r in results:
        gt = gt_by_packet.get(r["packet_id"])
        if gt:
            agreement_n += 1
            if r["primary_label"] == gt:
                agreement_matches += 1
    label_agreement = round(agreement_matches / agreement_n, 4) if agreement_n else None

    # Categorical stability across repeats: for each packet, what fraction
    # of its repeats agree with that packet's OWN most-common label? Only
    # meaningful with repeats > 1 -- a single pass has nothing to compare.
    stability = None
    if run["repeats"] and run["repeats"] > 1 and results:
        by_packet: dict = {}
        for r in results:
            by_packet.setdefault(r["packet_id"], []).append(r["primary_label"])
        agree_fractions = [
            labels.count(max(set(labels), key=labels.count)) / len(labels)
            for labels in by_packet.values() if labels
        ]
        if agree_fractions:
            stability = round(sum(agree_fractions) / len(agree_fractions), 4)

    return {
        "status": "ok",
        "run_id": run["run_id"],
        "started_at_sgt": run["started_at_sgt"],
        "model": run["model"],
        "is_mock": bool(run["is_mock"]),
        "repeats": run["repeats"],
        "n_packets": run["n_packets"],
        "n_results": n_results,
        "parse_rate": parse_rate,
        "ground_truth_coverage": coverage,
        "label_agreement": label_agreement,
        "label_agreement_n": agreement_n,
        "categorical_stability": stability,
    }


def render_markdown(rep: dict) -> str:
    if rep["status"] == "no_runs_yet":
        return (
            "## Eval harness\n"
            "- No eval runs yet -- `python -m alphaos eval_corpus_build` then `python -m alphaos eval`."
        )
    lines = [
        "## Eval harness",
        f"- Last run: {rep['started_at_sgt']} SGT · model={rep['model']}"
        f"{' (mock)' if rep['is_mock'] else ''} · {rep['n_packets']} packets x "
        f"{rep['repeats']} repeat(s) = {rep['n_results']} results",
    ]
    lines.append(
        f"- Parse rate: {rep['parse_rate'] * 100:.1f}%" if rep["parse_rate"] is not None
        else "- Parse rate: n/a (no results)"
    )
    gt = rep["ground_truth_coverage"]
    if rep["label_agreement"] is not None:
        lines.append(
            f"- Label agreement vs ground truth: {rep['label_agreement'] * 100:.1f}% "
            f"(n={rep['label_agreement_n']}, {gt['labeled']}/{gt['total']} corpus packets adjudicated)"
        )
    else:
        lines.append(
            f"- Label agreement vs ground truth: N/A -- {gt['labeled']}/{gt['total']} "
            "corpus packets adjudicated yet"
        )
    if rep["categorical_stability"] is not None:
        lines.append(
            f"- Categorical stability across {rep['repeats']} repeats: "
            f"{rep['categorical_stability'] * 100:.1f}%"
        )
    return "\n".join(lines)
