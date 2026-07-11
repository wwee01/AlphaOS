"""PR-UI-B2: the Learning tab's Journal panel feed -- PURE READ assembly of
three entry types only (UI/UX doc §9: "a feed, newest first, three entry
types only: resolved events, hypothesis lifecycle (proposed -> testing n/N ->
met/failed), promotions/demotions with evidence links"):

1. Resolved attribution events (``attribution_records``, resolved_status=
   'resolved') -- reuses the SAME per-event ΔR + '(mock)' tagging convention
   ``alphaos.dashboard.streamlit_app._hindsight_cell()`` already established
   for the Candidate Flow tab's hindsight column (never a second, drifting
   sentence format for the same fact). Unlike ``daily_brief.py``'s own
   ``_learned_sentence()`` (which deliberately OMITS the per-event ΔR on the
   Tonight headline, audit C4), this feed is the detailed audit trail the
   Tonight headline points an operator AT -- showing the individual signed
   ΔR here is the whole point of a journal, not a contradiction of C4's rule
   (which is scoped to the aggregate-only Tonight summary).
2. Hypothesis lifecycle -- 'proposed' (hypothesis_proposals.created_at_utc)
   and 'resolved' (resolved_at_utc, with the raw verdict/q-value -- see
   hypothesis_report.py's own module docstring for why MET/FAILED/WITHDRAWN
   are never auto-derived here either) transitions, PLUS HGEN-1 draft
   accept/reject decisions (system_events, category='hypothesis_drafts' --
   the message text proposer.py's own accept_draft()/reject_draft() already
   wrote is reused verbatim, never re-derived, so this feed can never say
   something different from what was actually logged at the time).
3. Promotions/demotions -- ``alphaos.cards.scoreboard.promotion_history()``
   (manual promote/demote via ``promotion_decisions``) UNIONed with
   ``alphaos.cards.scoreboard.demoted_cards()`` (automatic-trigger-only
   ``card_demotions``) -- the two tables are never merged at the DB level
   (schema.py's own comment), so this report-level union is exactly what
   that comment anticipates.

Every entry carries a ``provenance`` dict of plain-text ids (attribution_id/
candidate_id, hypothesis_id/draft_id/event_id, decision_id/demotion_id) --
UI/UX doc's "every entry links into rung-5 provenance," satisfied here as
plain ids with no new drill-down UI required this PR.

Nothing here writes, and nothing here is read by any gate/eval/labeller/
risk/execution path -- a report module exactly like its siblings
(hypothesis_report.py, attribution.py, daily_brief.py).
"""

from __future__ import annotations

import json


def _parse_detail(value) -> dict:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _resolved_attribution_entries(journal, limit: int) -> list[dict]:
    rows = journal.query(
        "SELECT attribution_id, attribution_type, symbol, delta_r, is_mock, "
        "resolved_at_utc, candidate_id FROM attribution_records "
        "WHERE resolved_status = 'resolved' AND resolved_at_utc IS NOT NULL "
        "AND delta_r IS NOT NULL ORDER BY resolved_at_utc DESC LIMIT ?",
        (limit,),
    )
    out = []
    for r in rows:
        kind = (r.get("attribution_type") or "decision").replace("_", " ")
        suffix = " (mock)" if r.get("is_mock") else ""
        out.append({
            "kind": "resolved_event",
            "timestamp": r["resolved_at_utc"],
            "text": f"{r.get('symbol', '?')}: {kind} → {r['delta_r']:+.2f}R{suffix} [resolved]",
            "provenance": {
                "attribution_id": r.get("attribution_id"),
                "candidate_id": r.get("candidate_id"),
            },
        })
    return out


def _hypothesis_lifecycle_entries(journal, limit: int) -> list[dict]:
    rows = journal.query(
        "SELECT hypothesis_id, claim, risk_class, created_at_utc, resolved_at_utc, "
        "last_verdict, last_q_value FROM hypothesis_proposals ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    out = []
    for r in rows:
        if r.get("created_at_utc"):
            out.append({
                "kind": "hypothesis_lifecycle",
                "timestamp": r["created_at_utc"],
                "text": f"{r['hypothesis_id']}: proposed (risk class {r['risk_class']}) — {r['claim']}",
                "provenance": {"hypothesis_id": r["hypothesis_id"]},
            })
        if r.get("resolved_at_utc"):
            q = r.get("last_q_value")
            q_str = f"{q:.4f}" if q is not None else "n/a"
            out.append({
                "kind": "hypothesis_lifecycle",
                "timestamp": r["resolved_at_utc"],
                "text": (
                    f"{r['hypothesis_id']}: resolved — raw verdict "
                    f"{r.get('last_verdict') or 'n/a'} (q={q_str}); MET/FAILED/WITHDRAWN "
                    "is an operator ruling, not set here"
                ),
                "provenance": {"hypothesis_id": r["hypothesis_id"]},
            })

    draft_events = journal.query(
        "SELECT event_id, message, created_at_utc, detail_json FROM system_events "
        "WHERE category = 'hypothesis_drafts' ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    for e in draft_events:
        detail = _parse_detail(e.get("detail_json"))
        out.append({
            "kind": "hypothesis_lifecycle",
            "timestamp": e["created_at_utc"],
            "text": e["message"],
            "provenance": {
                "draft_id": detail.get("draft_id"),
                "hypothesis_id": detail.get("hypothesis_id"),
                "event_id": e.get("event_id"),
            },
        })
    return out


def _promotion_demotion_entries(journal, limit: int) -> list[dict]:
    from alphaos.cards.scoreboard import demoted_cards, promotion_history

    out = []
    for p in promotion_history(journal, limit=limit):
        out.append({
            "kind": "promotion_demotion",
            "timestamp": p.get("decided_at_utc"),
            "text": (
                f"{p['card_id']} v{p['card_version']}: {p['from_state']} → {p['to_state']} "
                f"({p['direction']}, decided by {p.get('decided_by') or 'unknown'})"
            ),
            "provenance": {
                "decision_id": p.get("decision_id"),
                "hypothesis_id": p.get("hypothesis_id"),
            },
        })
    for d in demoted_cards(journal)[:limit]:
        out.append({
            "kind": "promotion_demotion",
            "timestamp": d.get("demoted_at_utc"),
            "text": f"{d['card_id']} v{d['card_version']}: auto-demoted — {d.get('reason') or 'n/a'}",
            "provenance": {"card_id": d.get("card_id"), "card_version": d.get("card_version")},
        })
    return out


def build_journal_feed(journal, limit: int = 50) -> dict:
    """Combine all three entry kinds, newest first, truncated to ``limit``.
    A per-source over-fetch (``limit`` on each query) keeps this cheap even
    when the combined, sorted result is trimmed further -- avoids silently
    starving one entry kind (e.g. hypothesis lifecycle, inherently rare)
    behind a flood of another (resolved attribution events, the highest-
    volume kind) purely due to fetch order."""
    entries = (
        _resolved_attribution_entries(journal, limit)
        + _hypothesis_lifecycle_entries(journal, limit)
        + _promotion_demotion_entries(journal, limit)
    )
    # A missing/unparseable timestamp is uncountable, not "oldest" or
    # "newest" -- excluded from the sorted feed rather than fabricating a
    # sort position (unknown-never-zero's same posture, applied to ordering).
    entries = [e for e in entries if e.get("timestamp")]
    entries.sort(key=lambda e: e["timestamp"], reverse=True)
    return {"entries": entries[:limit], "total_matched": len(entries)}
