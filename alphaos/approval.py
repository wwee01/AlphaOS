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
    def consider(self, proposal, risk_ok: bool, freshness_ok: bool) -> ApprovalOutcome:
        """Called by the scan pipeline after risk + freshness are known.

        Auto applies only when APPROVAL_MODE=auto AND REQUIRE_MANUAL_APPROVAL is
        off (effective_approval_mode); otherwise the proposal awaits manual
        approval.
        """
        if self.settings.effective_approval_mode != ApprovalMode.AUTO:
            # Manual mode: never auto-submit. Await explicit user approval.
            return ApprovalOutcome(False, "pending_manual", reason=ReasonCode.APPROVAL_REQUIRED.value)

        # --- AUTO mode: enforce every guardrail before approving. -----------
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

        used = self.journal.count_auto_approvals_today()
        if used >= self.settings.max_auto_approvals_per_day:
            return self._deny(proposal, ReasonCode.AUTO_APPROVAL_LIMIT.value,
                              f"auto-approval cap reached ({used}/{self.settings.max_auto_approvals_per_day})")

        approval_id = self._record(
            proposal, ApprovalLabel.AUTO_APPROVED, approved=True,
            approver="auto", reason="passed risk+freshness within auto cap",
            freshness_ok=freshness_ok, risk_ok=risk_ok,
        )
        self.journal.log_system_event(
            Severity.INFO, "approval",
            f"AUTO_APPROVED {proposal.symbol} ({used + 1}/{self.settings.max_auto_approvals_per_day}).",
            {"proposal_id": proposal.proposal_id},
        )
        return ApprovalOutcome(True, "auto_approved", ApprovalLabel.AUTO_APPROVED.value,
                               "auto approved", approval_id)

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
