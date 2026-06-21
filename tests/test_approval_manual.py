"""Paper approval is required in manual mode (test #2)."""

from __future__ import annotations

from alphaos.approval import ApprovalEngine
from conftest import make_settings, make_proposal, inject_pending_proposal
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator


def test_manual_mode_does_not_auto_approve(journal):
    s = make_settings(APPROVAL_MODE="manual")
    eng = ApprovalEngine(s, journal)
    outcome = eng.consider(make_proposal(), risk_ok=True, freshness_ok=True)
    assert outcome.approved is False
    assert outcome.status == "pending_manual"
    assert journal.count_rows("approvals") == 0


def test_scan_in_manual_mode_places_no_orders(orchestrator):
    orchestrator.run_scan_once()
    # No fills may exist without explicit approval.
    assert orchestrator.journal.count_rows("paper_fills") == 0
    assert orchestrator.journal.count_open_positions() == 0


def test_manual_approval_then_executes():
    s = make_settings(APPROVAL_MODE="manual")
    journal = JournalStore(":memory:")
    orch = Orchestrator(settings=s, journal=journal)
    proposal_id, _ = inject_pending_proposal(orch)

    # Before approval: nothing executed.
    assert journal.count_rows("paper_fills") == 0

    ok, msg = orch.approve_proposal(proposal_id, approver="tester")
    assert ok, msg

    # A MANUAL_APPROVED record and a fill now exist.
    approvals = journal.query("SELECT * FROM approvals WHERE label='MANUAL_APPROVED'")
    assert len(approvals) == 1
    assert journal.count_rows("paper_fills") >= 1
    assert journal.count_open_positions() == 1
    orch.close()
