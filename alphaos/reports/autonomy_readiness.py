"""PR13 slice 2: the autonomy-readiness report -- for every hypothesis that
gates a card promotion, the full precondition checklist from
``alphaos.cards.promotion.check_promotion_preconditions()``, rendered so an
operator can see at a glance what (if anything) is still blocking a
graduation. PURE READ; never gated for real, never read by any
gate/eval/labeller/risk/execution path.
"""

from __future__ import annotations

from alphaos.cards.promotion import check_promotion_preconditions


def build_autonomy_readiness_report(journal) -> dict:
    hypotheses = journal.query(
        "SELECT hypothesis_id FROM hypothesis_proposals WHERE card_id IS NOT NULL ORDER BY hypothesis_id"
    )
    checks = [
        {"hypothesis_id": h["hypothesis_id"], **check_promotion_preconditions(journal, h["hypothesis_id"])}
        for h in hypotheses
    ]
    return {
        "n_checked": len(checks),
        "n_eligible": sum(1 for c in checks if c["eligible"]),
        "checks": checks,
    }


def render_markdown(rep: dict) -> str:
    lines = [
        "## PR13 slice 2 -- autonomy readiness (card-gating hypotheses only; "
        "promotion is never automatic -- this is a checklist, not a trigger)",
        f"{rep['n_checked']} card-gating hypothesis(es) checked, {rep['n_eligible']} currently eligible",
        "",
    ]
    if rep["n_checked"] == 0:
        lines.append("- (no seeded hypothesis currently names a card)")
        return "\n".join(lines)
    for c in rep["checks"]:
        marker = "✅ READY" if c["eligible"] else f"⏳ {c['reason_code']}"
        lines.append(f"- {c['hypothesis_id']} ({c.get('card_id') or 'no card'}): {marker}")
        if not c["eligible"]:
            lines.append(f"  {c['detail']}")
    return "\n".join(lines)
