"""Approval engine — wires APPROVAL_MODE (manual | auto) through the pipeline.

manual (default): every paper trade requires explicit per-candidate user
approval before submission. The pipeline leaves proposals pending; the
dashboard/CLI calls ``approve_manually``.

auto: a proposal may be approved without per-candidate input ONLY if it passes
risk + freshness, is not a day-trade experiment, does not need unapproved
margin/leverage, and is within the daily auto-approval cap. Auto is paper/mock
only and every auto approval is labelled AUTO_APPROVED and logged.

Auto must never bypass risk or freshness gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from alphaos.constants import (
    ApprovalLabel,
    ApprovalMode,
    ReasonCode,
    Severity,
    Strategy,
)
from alphaos.util.ids import new_id


@dataclass
class ApprovalOutcome:
    approved: bool
    status: str                 # auto_approved | pending_manual | auto_denied | rejected | manual_approved
    label: Optional[str] = None
    reason: Optional[str] = None
    approval_id: Optional[str] = None


class ApprovalEngine:
    def __init__(self, settings, journal):
        self.settings = settings
        self.journal = journal

    # ------------------------------------------------------------- pipeline
    def consider(self, proposal, risk_ok: bool, freshness_ok: bool, unattended: bool = False) -> ApprovalOutcome:
        """Called by the scan pipeline after risk + freshness are known.

        Auto applies when EITHER APPROVAL_MODE=auto AND REQUIRE_MANUAL_APPROVAL
        is off (effective_approval_mode), OR ``unattended`` is True -- a SECOND
        DOOR into this exact same gate stack, added 2026-07-11 for the
        market-close scan window the operator is asleep for (see
        ``Settings.unattended_approve_windows``'s own docstring; this is NOT
        PR15/L3 -- that remains separately gated). ``unattended`` is computed
        ONCE at scan start (never per-proposal at consider-time -- a slow AI
        evaluation call can legitimately span the window's own end boundary,
        and re-checking wall-clock here would silently deny exactly the late
        candidates the mechanism exists to catch) and threaded down through
        ``Orchestrator._handle_proposal``. Neither door skips a single gate
        below -- risk/freshness/daytrade/margin checks apply identically
        either way, and the unattended door additionally passes its OWN cap
        before the shared cap both doors share.
        """
        if self.settings.effective_approval_mode != ApprovalMode.AUTO and not unattended:
            # Manual mode, no unattended window active: never auto-submit.
            return ApprovalOutcome(False, "pending_manual", reason=ReasonCode.APPROVAL_REQUIRED.value)

        # --- AUTO/unattended: enforce every guardrail before approving. -----
        if not risk_ok:
            return self._deny(proposal, ReasonCode.RISK_OVERSIZED.value, "auto denied: risk gate not passed")
        if not freshness_ok:
            return self._deny(proposal, ReasonCode.STALE_DATA.value, "auto denied: data not fresh")
        if proposal.strategy == Strategy.DAYTRADE_EXPERIMENT.value:
            return self._deny(proposal, ReasonCode.DAYTRADE_GATED.value,
                              "auto cannot approve day-trade experiment trades")
        if proposal.requires_margin and not proposal.margin_approved:
            return self._deny(proposal, ReasonCode.MARGIN_APPROVAL_REQUIRED.value,
                              "auto cannot enable margin/leverage or a short path needing margin")

        # Count-then-insert below assumes the single-threaded, synchronous
        # scan loop this codebase runs today (one candidate handled fully,
        # including the journal insert, before the next is considered) --
        # there is no DB-level UNIQUE backstop on either cap. If concurrent
        # scan execution is ever introduced, this needs one.
        if unattended:
            # Own cap FIRST (an intersection of two caps, not two parallel
            # additive budgets -- see Settings.max_unattended_approvals_per_day).
            unattended_used = self.journal.count_unattended_approvals_today()
            if unattended_used >= self.settings.max_unattended_approvals_per_day:
                return self._deny(
                    proposal, ReasonCode.AUTO_APPROVAL_LIMIT.value,
                    f"unattended-approval cap reached "
                    f"({unattended_used}/{self.settings.max_unattended_approvals_per_day})",
                )

        used = self.journal.count_auto_approvals_today()
        if used >= self.settings.max_auto_approvals_per_day:
            return self._deny(proposal, ReasonCode.AUTO_APPROVAL_LIMIT.value,
                              f"auto-approval cap reached ({used}/{self.settings.max_auto_approvals_per_day})")

        label = ApprovalLabel.UNATTENDED_APPROVED if unattended else ApprovalLabel.AUTO_APPROVED
        approver = "window_auto" if unattended else "auto"
        reason = (
            "passed risk+freshness within the unattended-window + shared auto cap"
            if unattended else "passed risk+freshness within auto cap"
        )
        approval_id = self._record(
            proposal, label, approved=True, approver=approver, reason=reason,
            freshness_ok=freshness_ok, risk_ok=risk_ok,
        )
        # Log progress against the cap that actually gated THIS approval --
        # the own cap for the unattended door, the shared cap otherwise.
        if unattended:
            progress = f"{unattended_used + 1}/{self.settings.max_unattended_approvals_per_day}"
        else:
            progress = f"{used + 1}/{self.settings.max_auto_approvals_per_day}"
        self.journal.log_system_event(
            Severity.INFO, "approval",
            f"{label.value} {proposal.symbol} ({progress}).",
            {"proposal_id": proposal.proposal_id},
        )
        return ApprovalOutcome(True, "auto_approved", label.value, reason, approval_id)

    # ---------------------------------------------------------- manual API
    def approve_manually(self, proposal, approver: str = "user",
                         freshness_ok: bool = True, risk_ok: bool = True) -> ApprovalOutcome:
        approval_id = self._record(
            proposal, ApprovalLabel.MANUAL_APPROVED, approved=True, approver=approver,
            reason="manual user approval", freshness_ok=freshness_ok, risk_ok=risk_ok,
        )
        self.journal.log_system_event(
            Severity.INFO, "approval", f"MANUAL_APPROVED {proposal.symbol}.",
            {"proposal_id": proposal.proposal_id, "approver": approver},
        )
        return ApprovalOutcome(True, "manual_approved", ApprovalLabel.MANUAL_APPROVED.value,
                               "manual approval", approval_id)

    def reject_manually(self, proposal, approver: str = "user", reason: str = "user rejected") -> ApprovalOutcome:
        approval_id = self._record(
            proposal, ApprovalLabel.REJECTED, approved=False, approver=approver, reason=reason,
        )
        self.journal.log_system_event(
            Severity.INFO, "approval", f"REJECTED {proposal.symbol} by {approver}.",
            {"proposal_id": proposal.proposal_id},
        )
        return ApprovalOutcome(False, "rejected", ApprovalLabel.REJECTED.value, reason, approval_id)

    # --------------------------------------------------------------- helpers
    def _deny(self, proposal, code, detail) -> ApprovalOutcome:
        self.journal.log_system_event(
            Severity.INFO, "approval", f"Auto-approval denied for {proposal.symbol}: {code}",
            {"proposal_id": proposal.proposal_id, "detail": detail},
        )
        return ApprovalOutcome(False, "auto_denied", reason=code)

    def _record(self, proposal, label: ApprovalLabel, approved: bool, approver: str,
                reason: str, freshness_ok: bool = True, risk_ok: bool = True) -> str:
        approval_id = new_id("apr")
        self.journal.insert(
            "approvals",
            {
                "approval_id": approval_id,
                "proposal_id": proposal.proposal_id,
                "candidate_id": proposal.candidate_id,
                "symbol": proposal.symbol,
                "approval_mode": self.settings.approval_mode.value,
                "label": label.value,
                "approved": 1 if approved else 0,
                "approver": approver,
                "reason": reason,
                "freshness_ok": 1 if freshness_ok else 0,
                "risk_ok": 1 if risk_ok else 0,
            },
        )
        return approval_id
