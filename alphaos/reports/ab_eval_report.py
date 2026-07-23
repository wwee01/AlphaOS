"""AB-EVAL-1: the markdown side-by-side report -- the ticket's own
deliverable. Persistent state, shows the latest run regardless of when it
happened (AB-EVAL-1 has no cadence of its own -- same rationale as CANARY's
own report). Pure read; zero decision surface.
"""

from __future__ import annotations

import json
from typing import Optional


def _row_arm(row) -> str:
    """INSTR-2: the report's own grouping key. ``prompt_version`` is NULL on
    every pre-INSTR-2 row (the column didn't exist yet) -- those rows all
    ran under the only version that ever existed, "v1", but were never
    asked to record it, so NULL degrades to "v1" here rather than a
    literal "None" string leaking into the report."""
    return f"{row['model']}:{row['prompt_version'] or 'v1'}"


def build_ab_eval_report(journal, ab_run_id: Optional[str] = None) -> dict:
    """Report for one AB-EVAL-1 run -- defaults to the LATEST run when
    ``ab_run_id`` is omitted. ``{"status": "no_runs_yet"}`` is the honest,
    expected empty state, never an error.

    INSTR-2: groups by ARM (``"model:prompt_version"``), not bare model --
    a 4-arm proof-gate run replays the SAME model under both v1 and v2, and
    conflating those under one model key would silently average away
    exactly the distinction the gate exists to measure. ``models`` stays
    populated (distinct model names only, order-preserved from
    ``models_json``) for any pre-INSTR-2 reader of that specific field.
    """
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
    stored_arms = json.loads(run["arms_json"]) if run.get("arms_json") else None
    # Discover arms from the rows themselves when arms_json is absent (a
    # pre-INSTR-2 run) -- order-preserving over first-seen (model, version).
    arms = stored_arms if stored_arms is not None else list(dict.fromkeys(_row_arm(r) for r in rows))

    raw_decision_distribution: dict = {a: {} for a in arms}
    by_packet: dict = {}
    for r in rows:
        arm = _row_arm(r)
        dist = raw_decision_distribution.setdefault(arm, {})
        dist[r["raw_decision"]] = dist.get(r["raw_decision"], 0) + 1
        by_packet.setdefault(r["eval_id"], {})[arm] = r

    flipped_packets = []
    for eval_id, per_arm in by_packet.items():
        decisions = {a: per_arm[a]["raw_decision"] for a in arms if a in per_arm}
        if len(set(decisions.values())) > 1:
            flipped_packets.append({
                "eval_id": eval_id,
                "symbol": next(iter(per_arm.values()))["symbol"],
                "decisions": decisions,
                "reasoning": {a: per_arm[a]["reasoning_summary"] for a in arms if a in per_arm},
            })
    # Deterministic report order -- eval_id sorts stably regardless of
    # SQLite row-return order.
    flipped_packets.sort(key=lambda f: f["eval_id"])

    floor_autopsy: dict = {}
    for a in arms:
        a_rows = [r for r in rows if _row_arm(r) == a]
        raw_proposes = [r for r in a_rows if r["raw_decision"] == "propose"]
        rr_floor = [r for r in raw_proposes if r["downgrade_reason"] == "RR_FLOOR"]
        no_atr = [r for r in raw_proposes if r["downgrade_reason"] == "NO_ATR"]
        floor_autopsy[a] = {
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
        "arms": arms,
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
        f"arms: {', '.join(rep['arms'])}"
        f"{' (mock)' if rep['is_mock'] else ''}",
        "",
        "### Raw decision distribution (identical inputs, per arm)",
    ]
    for arm in rep["arms"]:
        dist = rep["raw_decision_distribution"].get(arm, {})
        dist_str = ", ".join(f"{k}={v}" for k, v in sorted(dist.items())) or "no results"
        lines.append(f"- **{arm}**: {dist_str}")

    lines += ["", "### RR_FLOOR / NO_ATR autopsy"]
    for arm in rep["arms"]:
        autopsy = rep["floor_autopsy"].get(arm, {})
        n_propose = autopsy.get("n_raw_propose", 0)
        if not n_propose:
            lines.append(f"- **{arm}**: 0 raw propose(s) -- nothing for the floor to downgrade.")
            continue
        rate = autopsy["rr_floor_rate"]
        rate_str = f"{rate * 100:.1f}%" if rate is not None else "n/a"
        lines.append(
            f"- **{arm}**: {n_propose} raw propose(s) -- "
            f"{autopsy['n_rr_floor']} downgraded RR_FLOOR ({rate_str}), "
            f"{autopsy['n_no_atr']} downgraded NO_ATR"
        )

    lines += ["", f"### Flipped packets ({len(rep['flipped_packets'])})"]
    if not rep["flipped_packets"]:
        lines.append("- None -- raw decisions agreed on every replayed packet.")
    for flip in rep["flipped_packets"][:50]:  # cap report length
        decisions_str = "; ".join(f"{a}={d}" for a, d in flip["decisions"].items())
        lines.append(f"- `{flip['eval_id']}` ({flip['symbol']}): {decisions_str}")
        for a, reasoning in flip["reasoning"].items():
            lines.append(f"    - {a}: {reasoning}")

    lines += [
        "",
        f"**Caveat:** n={rep['n_packets']} packets over a short trading-day span is "
        "descriptive only, not a significance claim -- no q-values computed or implied "
        "(FDR law).",
    ]
    return "\n".join(lines)
