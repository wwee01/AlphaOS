"""Counterfactual-outcome measurement report (Fable 5 review PR2, Part D).

Pure visibility over ``candidate_outcomes``: how much has been tracked, how
much has resolved, and a purely DESCRIPTIVE breakdown by candidate type and
bracket-replay result. No statistical claims are made anywhere in this module
— the whole point of the counterfactual ledger is to eventually have enough
data for that, and this report exists to show how close (or far) that is.
"""

from __future__ import annotations

from typing import Optional

from alphaos.reports.metrics import MIN_MEANINGFUL_SAMPLE
from alphaos.util import timeutils

_TRACKED_TYPES = ("candidate", "proposal", "reject", "armed_watch", "blocked", "user_override")

MEASUREMENT_CAVEAT = (
    "Measurement visibility only — no statistical claims. Forward outcomes and "
    "bracket replay are descriptive counterfactual data, not a backtest and not "
    "evidence of edge; treat any sample below the meaningful-sample threshold as "
    "anecdotal."
)


def _num(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _mean(xs: list[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 4) if xs else None


def _count_by(rows: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        v = r.get(key)
        if v is None or v == "":
            continue
        out[str(v)] = out.get(str(v), 0) + 1
    return out


def _type_stats(rows: list[dict], candidate_type: str) -> dict:
    subset = [r for r in rows if r.get("candidate_type") == candidate_type]
    complete = [r for r in subset if r.get("outcome_status") == "complete"]
    f1 = [_num(r.get("forward_1d_r")) for r in subset if _num(r.get("forward_1d_r")) is not None]
    f3 = [_num(r.get("forward_3d_r")) for r in subset if _num(r.get("forward_3d_r")) is not None]
    f5 = [_num(r.get("forward_5d_r")) for r in subset if _num(r.get("forward_5d_r")) is not None]
    return {
        "tracked": len(subset),
        "complete": len(complete),
        "pending": sum(1 for r in subset if r.get("outcome_status") == "pending"),
        "mean_forward_1d_r": _mean(f1),
        "mean_forward_3d_r": _mean(f3),
        "mean_forward_5d_r": _mean(f5),
        "small_sample": len(complete) < MIN_MEANINGFUL_SAMPLE,
    }


def compute_outcomes_summary(rows: list[dict]) -> dict:
    """Pure aggregation over candidate_outcomes rows. Never raises on missing
    fields; JSON-safe."""
    total = len(rows)
    by_status = _count_by(rows, "outcome_status")
    by_type = _count_by(rows, "candidate_type")
    by_replay = _count_by(rows, "replay_result")
    complete_n = by_status.get("complete", 0)

    return {
        "total_tracked": total,
        "pending": by_status.get("pending", 0),
        "partial": by_status.get("partial", 0),
        "complete": complete_n,
        "unavailable": by_status.get("unavailable", 0),
        "by_candidate_type": by_type,
        "by_type_forward_outcomes": {t: _type_stats(rows, t) for t in _TRACKED_TYPES},
        "bracket_replay_results": by_replay,
        "small_sample": complete_n < MIN_MEANINGFUL_SAMPLE,
        "note": (
            f"complete={complete_n} (< {MIN_MEANINGFUL_SAMPLE}); descriptive only, "
            "not statistically significant" if complete_n < MIN_MEANINGFUL_SAMPLE
            else f"complete={complete_n}"
        ),
        "caveat": MEASUREMENT_CAVEAT,
    }


def build_outcomes_report(journal, settings, limit: int = 2000) -> dict:
    """Read candidate_outcomes and aggregate. PURE READ — safe to call any
    time; never writes, never touches gates/execution."""
    rows = journal.query("SELECT * FROM candidate_outcomes ORDER BY id DESC LIMIT ?", (limit,))
    rep = compute_outcomes_summary(rows)
    rep["as_of"] = timeutils.market_date().isoformat()
    rep["mode"] = settings.mode.value
    return rep


def render_markdown(rep: dict) -> str:
    lines = [
        f"# Counterfactual Outcome Report — {rep.get('as_of', '')}",
        f"_mode: {rep.get('mode')}_",
        "",
        f"- Total tracked: **{rep['total_tracked']}**",
        f"- Pending: **{rep['pending']}**  ·  Partial: **{rep['partial']}**  ·  "
        f"Complete: **{rep['complete']}**  ·  Unavailable: **{rep['unavailable']}**",
        "",
        "## By candidate type",
    ]
    lines += [f"- {k}: {v}" for k, v in rep["by_candidate_type"].items()] or ["- (none)"]
    lines += ["", "## Forward outcomes by type (descriptive only)"]
    for t, s in rep["by_type_forward_outcomes"].items():
        if s["tracked"] == 0:
            continue
        lines.append(
            f"- **{t}**: tracked={s['tracked']} complete={s['complete']} pending={s['pending']} "
            f"· mean R (1d/3d/5d) = {s['mean_forward_1d_r']}/{s['mean_forward_3d_r']}/{s['mean_forward_5d_r']}"
            f"{'  (small sample)' if s['small_sample'] else ''}"
        )
    lines += ["", "## Bracket replay results"]
    lines += [f"- {k}: {v}" for k, v in rep["bracket_replay_results"].items()] or ["- (none replayed yet)"]
    lines += [
        "",
        f"- {rep['note']}",
        "",
        f"> ⚠️ {rep['caveat']}",
    ]
    return "\n".join(lines)
