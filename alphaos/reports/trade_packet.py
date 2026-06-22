"""Trade Packet v1 assembler.

Given any stable anchor (candidate_id / trade_id / position_id / proposal_id),
walk the journal's single-hop lookups and assemble ONE read-only dict that ties
the full lifecycle together:

    scan_batch -> candidate -> openai_evaluation -> (claude_review) ->
    risk_check -> proposal -> approval -> orders -> fills ->
    monitoring_snapshots -> position -> exit -> outcome -> baseline -> rejections

The packet is pure-read: it never writes. Missing links resolve to ``None`` (or
an empty list for collections) so a partial trade still produces a valid packet.
The central correlation key is ``trade_id``, minted at proposal birth and carried
through every downstream row.
"""

from __future__ import annotations

from typing import Optional


def assemble_trade_packet(
    journal,
    *,
    candidate_id: Optional[str] = None,
    trade_id: Optional[str] = None,
    position_id: Optional[str] = None,
    proposal_id: Optional[str] = None,
) -> dict:
    """Assemble the Trade Packet for a single trade lifecycle.

    Any one anchor is enough — the assembler resolves the rest by walking the
    links. Resolution order:
      * position_id -> position -> order -> proposal -> candidate
      * trade_id     -> position (then as above), else proposal
      * proposal_id  -> proposal -> candidate
      * candidate_id -> candidate (then forward via its proposal)
    """
    position = None
    proposal = None
    candidate = None

    # ---- Resolve the anchor down to a candidate (walking backwards as needed).
    if position_id:
        position = journal.position_by_id(position_id)
    if position is None and trade_id:
        position = journal.position_for_trade(trade_id)

    if position is not None:
        if not proposal_id:
            proposal_id = position.get("proposal_id")
        if not candidate_id:
            candidate_id = position.get("candidate_id")
        if not trade_id:
            trade_id = position.get("trade_id")
        # If the position did not carry the proposal/candidate ids, walk via order.
        if (not proposal_id or not candidate_id) and position.get("order_id"):
            order = journal.order_by_id(position["order_id"])
            if order:
                proposal_id = proposal_id or order.get("proposal_id")
                candidate_id = candidate_id or order.get("candidate_id")
                trade_id = trade_id or order.get("trade_id")

    if proposal is None and proposal_id:
        proposal = journal.proposal_by_id(proposal_id)
    if proposal is None and trade_id:
        # A proposal may exist (blocked) without a position.
        proposal = journal.one(
            "SELECT * FROM trade_proposals WHERE trade_id = ? ORDER BY id DESC LIMIT 1",
            (trade_id,),
        )

    if proposal is not None:
        proposal_id = proposal_id or proposal.get("proposal_id")
        candidate_id = candidate_id or proposal.get("candidate_id")
        trade_id = trade_id or proposal.get("trade_id")

    if candidate_id:
        candidate = journal.candidate_by_id(candidate_id)

    # ---- Forward resolution: candidate -> eval -> proposal (if not anchored).
    openai_evaluation = (
        journal.evaluation_for_candidate(candidate_id) if candidate_id else None
    )
    claude_review = (
        journal.claude_review_for_candidate(candidate_id) if candidate_id else None
    )

    if proposal is None and candidate_id:
        proposal = journal.one(
            "SELECT * FROM trade_proposals WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
            (candidate_id,),
        )
        if proposal is not None:
            proposal_id = proposal_id or proposal.get("proposal_id")
            trade_id = trade_id or proposal.get("trade_id")

    risk_check = journal.risk_check_for_proposal(proposal_id) if proposal_id else None
    approval = journal.approval_for_proposal(proposal_id) if proposal_id else None

    # ---- Orders / fills (orders link to the proposal; exit orders do not, so
    #      we collect entry orders here and walk fills off each).
    orders = journal.orders_for_proposal(proposal_id) if proposal_id else []
    fills: list[dict] = []
    for order in orders:
        fills.extend(journal.fills_for_order(order["order_id"]))

    # ---- Resolve the position forward from the entry order if still unknown.
    if position is None:
        for order in orders:
            pos = journal.one(
                "SELECT * FROM positions WHERE order_id = ? ORDER BY id DESC LIMIT 1",
                (order["order_id"],),
            )
            if pos is not None:
                position = pos
                break
    if position is None and trade_id:
        position = journal.position_for_trade(trade_id)

    if position is not None and not position_id:
        position_id = position.get("position_id")

    monitoring_snapshots = (
        journal.monitoring_snapshots_for_position(position_id) if position_id else []
    )
    position_exits = journal.exits_for_position(position_id) if position_id else []
    exit_row = position_exits[-1] if position_exits else None
    outcome = journal.outcome_for_position(position_id) if position_id else None
    baseline = journal.baseline_for_candidate(candidate_id) if candidate_id else None
    rejections = journal.rejections_for_candidate(candidate_id) if candidate_id else []

    # ---- Stable ids found anywhere along the chain.
    ids = {
        "scan_batch_id": (candidate or {}).get("scan_batch_id")
        or (proposal or {}).get("scan_batch_id"),
        "candidate_id": candidate_id,
        "eval_id": (openai_evaluation or {}).get("eval_id"),
        "claude_review_id": (claude_review or {}).get("review_id"),
        "risk_check_id": (risk_check or {}).get("risk_check_id")
        or (proposal or {}).get("risk_check_id"),
        "proposal_id": proposal_id,
        "approval_id": (approval or {}).get("approval_id"),
        "trade_id": trade_id,
        "internal_order_id": orders[0]["order_id"] if orders else None,
        "broker_order_id": orders[0].get("broker_order_id") if orders else None,
        "fill_id": fills[0]["fill_id"] if fills else None,
        "position_id": position_id,
        "exit_id": (exit_row or {}).get("exit_id"),
        "outcome_id": (outcome or {}).get("outcome_id"),
        "baseline_outcome_id": (baseline or {}).get("baseline_id"),
        "daily_report_id": None,
    }

    scan_batch = (
        journal.scan_batch_by_id(ids["scan_batch_id"]) if ids["scan_batch_id"] else None
    )

    return {
        "ids": ids,
        "scan_batch": scan_batch,
        "candidate": candidate,
        "openai_evaluation": openai_evaluation,
        "claude_review": claude_review,
        "risk_check": risk_check,
        "proposal": proposal,
        "approval": approval,
        "orders": orders,
        "fills": fills,
        "monitoring_snapshots": monitoring_snapshots,
        "position": position,
        "exit": exit_row,
        "outcome": outcome,
        "baseline": baseline,
        "rejections": rejections,
    }
