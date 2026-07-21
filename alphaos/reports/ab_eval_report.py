"""AB-EVAL-1: the markdown side-by-side report -- the ticket's own
deliverable. Persistent state, shows the latest run regardless of when it
happened (AB-EVAL-1 has no cadence of its own -- same rationale as CANARY's
own report). Pure read; zero decision surface.
"""

from __future__ import annotations

import json
from typing import Optional


def build_ab_eval_report(journal, ab_run_id: Optional[str] = None) -> dict:
    """Report for one AB-EVAL-1 run -- defaults to the LATEST run when
    ``ab_run_id`` is omitted. ``{"status": "no_runs_yet"}`` is the honest,
    expected empty state, never an error."""
    run = (
        journal.one("SELECT * FROM ab_eval_runs WHERE ab_run_id = ?", (ab_run_id,))
        if ab_run_id else
        journal.one("SELECT * FROM ab_eval_runs ORDER BY id DESC LIMIT 1")
    )
    if not run:
        return {"status": "no_runs_yet"}

    rows = journal.query(
        "SELECT * FROM ab_eval_results WHERE ab_run_id = ?", (run["ab_run_id"],),
    )
    models = json.loads(run["models_json"]) if run["models_json"] else []

    raw_decision_distribution: dict = {m: {} for m in models}
    by_packet: dict = {}
    for r in rows:
        dist = raw_decision_distribution.setdefault(r["model"], {})
        dist[r["raw_decision"]] = dist.get(r["raw_decision"], 0) + 1
        by_packet.setdefault(r["eval_id"], {})[r["model"]] = r

    flipped_packets = []
    for eval_id, per_model in by_packet.items():
        decisions = {m: per_model[m]["raw_decision"] for m in models if m in per_model}
        if len(set(decisions.values())) > 1:
            flipped_packets.append({
                "eval_id": eval_id,
                "symbol": next(iter(per_model.values()))["symbol"],
                "decisions": decisions,
                "reasoning": {m: per_model[m]["reasoning_summary"] for m in models if m in per_model},
            })
    # Deterministic report order -- eval_id sorts stably regardless of
    # SQLite row-return order.
    flipped_packets.sort(key=lambda f: f["eval_id"])

    floor_autopsy: dict = {}
    for m in models:
        m_rows = [r for r in rows if r["model"] == m]
        raw_proposes = [r for r in m_rows if r["raw_decision"] == "propose"]
        rr_floor = [r for r in raw_proposes if r["downgrade_reason"] == "RR_FLOOR"]
        no_atr = [r for r in raw_proposes if r["downgrade_reason"] == "NO_ATR"]
        floor_autopsy[m] = {
            "n_raw_propose": len(raw_proposes),
            "n_rr_floor": len(rr_floor),
            "n_no_atr": len(no_atr),
            "rr_floor_rate": round(len(rr_floor) / len(raw_proposes), 3) if raw_proposes else None,
            "raw_expected_r_on_rr_floor": [r["raw_expected_r"] for r in rr_floor],
        }

    return {
        "status": "ok",
        "ab_run_id": run["ab_run_id"],
        "started_at_sgt": run["started_at_sgt"],
        "is_mock": bool(run["is_mock"]),
        "n_packets": run["n_packets"],
        "n_results": run["n_results"],
        "n_corpus_errors": run["n_corpus_errors"],
        "models": models,
        "raw_decision_distribution": raw_decision_distribution,
        "flipped_packets": flipped_packets,
        "floor_autopsy": floor_autopsy,
    }


def render_markdown(rep: dict) -> str:
    if rep["status"] == "no_runs_yet":
        return (
            "## AB-EVAL-1 (primary-evaluator A/B replay)\n"
            "- No AB-EVAL-1 runs yet -- `python -m alphaos ab_eval_corpus_build` then "
            "`python -m alphaos ab_eval_run --models <a> <b>`."
        )

    lines = [
        "## AB-EVAL-1 (primary-evaluator A/B replay)",
        f"- Run `{rep['ab_run_id']}` · {rep['started_at_sgt']} SGT · "
        f"{rep['n_packets']} packet(s), {rep['n_corpus_errors']} skipped · "
        f"models: {', '.join(rep['models'])}"
        f"{' (mock)' if rep['is_mock'] else ''}",
        "",
        "### Raw decision distribution (identical inputs, per model)",
    ]
    for model in rep["models"]:
        dist = rep["raw_decision_distribution"].get(model, {})
        dist_str = ", ".join(f"{k}={v}" for k, v in sorted(dist.items())) or "no results"
        lines.append(f"- **{model}**: {dist_str}")

    lines += ["", "### RR_FLOOR / NO_ATR autopsy"]
    for model in rep["models"]:
        autopsy = rep["floor_autopsy"].get(model, {})
        n_propose = autopsy.get("n_raw_propose", 0)
        if not n_propose:
            lines.append(f"- **{model}**: 0 raw propose(s) -- nothing for the floor to downgrade.")
            continue
        rate = autopsy["rr_floor_rate"]
        rate_str = f"{rate * 100:.1f}%" if rate is not None else "n/a"
        lines.append(
            f"- **{model}**: {n_propose} raw propose(s) -- "
            f"{autopsy['n_rr_floor']} downgraded RR_FLOOR ({rate_str}), "
            f"{autopsy['n_no_atr']} downgraded NO_ATR"
        )

    lines += ["", f"### Flipped packets ({len(rep['flipped_packets'])})"]
    if not rep["flipped_packets"]:
        lines.append("- None -- raw decisions agreed on every replayed packet.")
    for flip in rep["flipped_packets"][:50]:  # cap report length
        decisions_str = "; ".join(f"{m}={d}" for m, d in flip["decisions"].items())
        lines.append(f"- `{flip['eval_id']}` ({flip['symbol']}): {decisions_str}")
        for m, reasoning in flip["reasoning"].items():
            lines.append(f"    - {m}: {reasoning}")

    lines += [
        "",
        f"**Caveat:** n={rep['n_packets']} packets over a short trading-day span is "
        "descriptive only, not a significance claim -- no q-values computed or implied "
        "(FDR law).",
    ]
    return "\n".join(lines)
