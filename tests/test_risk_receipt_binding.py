"""Prospective approval/final-account evidence bindings; real paths, fake broker."""

from datetime import datetime, timezone
import json

import pytest

from alphaos.approval import ApprovalEngine
from alphaos.broker.alpaca_client import AlpacaClient
from alphaos.execution.order_manager import OrderManager
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator, _eval_view_from_proposal
from alphaos.risk.risk_engine import RiskEngine
from alphaos.strategy.proposal import TradeProposal
from alphaos.util import timeutils, alerts
from conftest import make_settings, inject_pending_proposal
from test_entry_submission_recovery import RecoverableBroker
from test_alpaca_paper_execution import _paper_om

NOW = datetime(2026, 10, 1, 17, tzinfo=timezone.utc)


def setup_path(tmp_path, monkeypatch, mode="manual"):
    monkeypatch.setattr(timeutils, "now_utc", lambda: NOW)
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **kw: True)
    fake = RecoverableBroker()
    settings, memory, _ = _paper_om(
        fake,
        APPROVAL_MODE=mode,
        REQUIRE_MANUAL_APPROVAL="false",
        MAX_AUTO_APPROVALS_PER_DAY=5,
        MAX_UNATTENDED_APPROVALS_PER_DAY=1,
    )
    memory.close()
    journal = JournalStore(str(tmp_path / "risk-binding.db"))
    manager = OrderManager(
        settings, journal, alpaca=AlpacaClient(settings, journal, trading_client=fake)
    )
    orch = Orchestrator(settings=make_settings(), journal=journal)
    # Keep offline mock market data; execution uses the actual paper manager
    # with an injected SDK-shaped broker and real risk/approval components.
    orch.settings = settings
    orch.orders = manager
    orch.risk = RiskEngine(settings)
    orch.approvals = ApprovalEngine(settings, journal)
    orch._ensure_startup = lambda: None
    journal.insert(
        "job_runs",
        {
            "job_run_id": "healthy",
            "job_type": "monitor",
            "lock_key": "healthy",
            "status": "completed",
            "started_at_utc": NOW.isoformat(),
            "started_at_sgt": timeutils.stamp(NOW).local_sgt,
            "finished_at_utc": NOW.isoformat(),
        },
    )
    pid, _ = inject_pending_proposal(orch)
    proposal = TradeProposal.from_row(journal.proposal_by_id(pid))
    initial = orch.risk.assess(
        direction=proposal.direction,
        entry=proposal.entry,
        stop=proposal.stop,
        equity=100000,
    )
    initial_id = orch._record_risk_check(
        proposal, _eval_view_from_proposal(proposal), initial
    )
    proposal.risk_check_id = initial_id
    journal.conn.execute(
        "UPDATE trade_proposals SET risk_check_id=? WHERE proposal_id=?",
        (initial_id, pid),
    )
    journal.conn.commit()
    snapshots = []

    def account():
        equity = 95000 if not snapshots else 94000
        snapshots.append(equity)
        return {"equity": equity, "last_equity": equity, "trading_blocked": False}

    fake.get_account = account
    return fake, orch, proposal, initial_id, snapshots


def assert_final_binding(orch, proposal, initial_id, equity):
    journal = orch.journal
    row = journal.orders_for_proposal(proposal.proposal_id)[0]
    stored = journal.proposal_by_id(proposal.proposal_id)
    assert row["risk_check_id"] == stored["risk_check_id"] != initial_id
    risk = journal.one(
        "SELECT * FROM risk_checks WHERE risk_check_id=?", (row["risk_check_id"],)
    )
    assert (
        risk["result"] == "pass"
        and risk["max_risk_amount"] == equity * orch.settings.max_risk_per_trade_pct
    )
    assert risk["proposal_id"] == proposal.proposal_id
    events = [
        json.loads(r["detail_json"])
        for r in journal.query(
            "SELECT * FROM system_events WHERE category='entry_risk_snapshot'"
        )
    ]
    matching = [e for e in events if e.get("risk_check_id") == risk["risk_check_id"]]
    assert len(matching) == 1
    assert matching[0]["stage"] == "final_account_submission"
    assert matching[0]["proposal_qty"] == proposal.qty
    assert matching[0]["risk"]["account_context"]["equity"] == equity
    assert row["qty"] == proposal.qty and row["limit_price"] == proposal.entry
    return row


def test_manual_paper_approval_anchors_order_to_final_account_check(
    tmp_path, monkeypatch
):
    fake, orch, proposal, initial_id, snapshots = setup_path(tmp_path, monkeypatch)
    ok, message = orch.approve_proposal(proposal.proposal_id)
    assert ok, message
    assert snapshots == [95000, 94000]
    assert_final_binding(orch, proposal, initial_id, 94000)
    assert fake.submits == 1
    assert [r["label"] for r in orch.journal.query("SELECT * FROM approvals")] == [
        "MANUAL_APPROVED"
    ]


@pytest.mark.parametrize("unattended", [False, True])
def test_automatic_approval_anchors_order_without_changing_cap_or_size(
    tmp_path, monkeypatch, unattended
):
    fake, orch, proposal, initial_id, snapshots = setup_path(
        tmp_path, monkeypatch, mode="auto"
    )
    preview = orch.approvals.consider(
        proposal, risk_ok=True, freshness_ok=True, unattended=unattended, record=False
    )
    assert preview.approved
    result = orch._execute(
        proposal,
        approval=lambda: orch.approvals.consider(
            proposal, risk_ok=True, freshness_ok=True, unattended=unattended
        ),
    )
    assert not result.blocked and not result.deferred
    assert snapshots == [95000]
    assert_final_binding(orch, proposal, initial_id, 95000)
    assert fake.submits == 1
    assert orch.journal.count_auto_approvals_today() == 1
    assert orch.journal.count_unattended_approvals_today() == int(unattended)
    assert proposal.risk_check_id == result.order["risk_check_id"]


def test_final_receipt_is_durable_before_post_and_existing_intent_retry_is_read_only(
    tmp_path, monkeypatch
):
    fake, orch, proposal, initial_id, _ = setup_path(tmp_path, monkeypatch, mode="auto")
    initial_row = orch.journal.one(
        "SELECT * FROM risk_checks WHERE risk_check_id=?", (initial_id,)
    )
    inspected = []

    def before_post(spec):
        with_journal = JournalStore(orch.journal.db_path, read_only=True)
        try:
            row = with_journal.orders_for_proposal(proposal.proposal_id)[0]
            final_id = row["risk_check_id"]
            assert final_id == proposal.risk_check_id != initial_id
            assert (
                with_journal.proposal_by_id(proposal.proposal_id)["risk_check_id"]
                == final_id
            )
            event = with_journal.one(
                "SELECT detail_json FROM system_events WHERE category='entry_risk_snapshot'"
            )
            assert json.loads(event["detail_json"])["risk_check_id"] == final_id
            inspected.append(spec.copy())
        finally:
            with_journal.close()

    fake.before_post = before_post
    result = orch._execute(
        proposal,
        approval=lambda: orch.approvals.consider(
            proposal, risk_ok=True, freshness_ok=True
        ),
    )
    assert not result.blocked and len(inspected) == 1
    preserved_tables = (
        "risk_checks",
        "trade_proposals",
        "paper_orders",
        "approvals",
        "system_events",
    )
    before = {t: orch.journal.query(f"SELECT * FROM {t}") for t in preserved_tables}
    monkeypatch.setattr(
        orch,
        "_assess_current_risk",
        lambda **kw: pytest.fail("retry repeated final check"),
    )
    again = orch._execute(
        proposal, approval=lambda: pytest.fail("retry consumed approval")
    )
    assert again.order["order_id"] == result.order["order_id"]
    assert {
        t: orch.journal.query(f"SELECT * FROM {t}") for t in preserved_tables
    } == before
    assert (
        orch.journal.one(
            "SELECT * FROM risk_checks WHERE risk_check_id=?", (initial_id,)
        )
        == initial_row
    )
    assert fake.submits == 1


def test_d5_smaller_final_capacity_blocks_without_resizing_or_new_anchor(
    tmp_path, monkeypatch
):
    fake, orch, proposal, initial_id, _ = setup_path(tmp_path, monkeypatch, mode="auto")
    fake.get_account = lambda: {
        "equity": 4000,
        "last_equity": 4000,
        "trading_blocked": False,
    }
    original_size = proposal.qty
    result = orch._execute(
        proposal, approval=lambda: pytest.fail("approval consumed on D5 refusal")
    )
    assert result.blocked and result.block_reason == "RISK_OVERSIZED"
    assert proposal.qty == original_size and proposal.risk_check_id == initial_id
    assert (
        orch.journal.proposal_by_id(proposal.proposal_id)["risk_check_id"] == initial_id
    )
    assert orch.journal.count_rows("risk_checks") == 1
    assert fake.submits == orch.journal.count_rows("approvals") == 0


@pytest.mark.parametrize("point", ["risk_insert", "proposal_update", "event_insert"])
def test_capture_failure_is_atomic_and_precedes_approval_and_post(
    tmp_path, monkeypatch, point
):
    fake, orch, proposal, initial_id, _ = setup_path(tmp_path, monkeypatch, mode="auto")
    before_risks = orch.journal.query("SELECT * FROM risk_checks")
    before_proposals = orch.journal.query("SELECT * FROM trade_proposals")
    if point == "proposal_update":
        orch.journal.conn.execute(
            "CREATE TEMP TRIGGER refuse_risk_link BEFORE UPDATE OF risk_check_id ON trade_proposals BEGIN SELECT RAISE(ABORT, 'injected link fault'); END"
        )
        orch.journal.conn.commit()
    else:
        insert = JournalStore.insert

        def fail_insert(self, table, data, **kw):
            if (point == "risk_insert" and table == "risk_checks") or (
                point == "event_insert"
                and table == "system_events"
                and data.get("category") == "entry_risk_snapshot"
            ):
                raise RuntimeError("injected evidence write fault")
            return insert(self, table, data, **kw)

        monkeypatch.setattr(JournalStore, "insert", fail_insert)
    with pytest.raises(Exception, match="injected"):
        orch._execute(
            proposal,
            approval=lambda: pytest.fail("approval consumed before durable evidence"),
        )
    assert orch.journal.query("SELECT * FROM risk_checks") == before_risks
    assert orch.journal.query("SELECT * FROM trade_proposals") == before_proposals
    assert not orch.journal.query(
        "SELECT * FROM system_events WHERE category='entry_risk_snapshot'"
    )
    assert proposal.risk_check_id == initial_id
    assert (
        fake.submits
        == orch.journal.count_rows("approvals")
        == orch.journal.count_rows("paper_orders")
        == 0
    )


def test_cap_race_after_preview_still_denies_without_order(tmp_path, monkeypatch):
    fake, orch, proposal, _, _ = setup_path(tmp_path, monkeypatch, mode="auto")
    object.__setattr__(orch.settings, "max_auto_approvals_per_day", 1)
    assert orch.approvals.consider(
        proposal, risk_ok=True, freshness_ok=True, record=False
    ).approved
    # Another eligible action consumes the shared cap before final approval.
    from conftest import make_proposal

    assert orch.approvals.consider(
        make_proposal(symbol="OTHER"), risk_ok=True, freshness_ok=True
    ).approved
    result = orch._execute(
        proposal,
        approval=lambda: orch.approvals.consider(
            proposal, risk_ok=True, freshness_ok=True
        ),
    )
    assert result.deferred and not result.blocked
    assert fake.submits == orch.journal.count_rows("paper_orders") == 0
    assert orch.journal.count_auto_approvals_today() == 1
    assert (
        orch.journal.proposal_by_id(proposal.proposal_id)["status"]
        == "pending_approval"
    )


def test_simulated_manual_path_uses_its_fresh_approval_risk_id_without_final_account_claim(
    monkeypatch,
):
    monkeypatch.setattr(timeutils, "now_utc", lambda: NOW)
    orch = Orchestrator(settings=make_settings())
    pid, _ = inject_pending_proposal(orch)
    ok, message = orch.approve_proposal(pid)
    assert ok, message
    row = orch.journal.orders_for_proposal(pid)[0]
    assert row["risk_check_id"] == orch.journal.proposal_by_id(pid)["risk_check_id"]
    assert row["risk_check_id"]
    assert not orch.journal.query(
        "SELECT * FROM system_events WHERE category='entry_risk_snapshot'"
    )
    orch.close()


@pytest.mark.parametrize("unattended", [False, True])
def test_scan_proposal_auto_path_carries_final_risk_anchor(
    tmp_path, monkeypatch, unattended
):
    from alphaos.orchestrator import ScanSummary
    from alphaos.scanner.scan_context import ScanContext

    fake, orch, seed, _, _ = setup_path(tmp_path, monkeypatch, mode="auto")
    fake.get_account = lambda: {
        "equity": 95000,
        "last_equity": 95000,
        "trading_blocked": False,
    }
    candidate = orch.journal.one(
        "SELECT * FROM candidates WHERE candidate_id=?", (seed.candidate_id,)
    )
    evaluation = orch._eval_from_row(
        orch.journal.evaluation_for_candidate(seed.candidate_id)
    )
    context = ScanContext(candidate, snapshot=orch.market.get_snapshot(seed.symbol))
    summary = ScanSummary(scan_id="offline-binding-scan")
    assert orch._handle_proposal(context, evaluation, summary, unattended=unattended)
    assert summary.auto_submitted == 1 and not summary.entry_deferred
    order = orch.journal.one("SELECT * FROM paper_orders")
    proposal = TradeProposal.from_row(orch.journal.proposal_by_id(order["proposal_id"]))
    checks = orch.journal.query(
        "SELECT * FROM risk_checks WHERE proposal_id=? ORDER BY id",
        (proposal.proposal_id,),
    )
    assert len(checks) == 2
    assert_final_binding(orch, proposal, checks[0]["risk_check_id"], 95000)
    assert fake.submits == 1 and orch.journal.count_auto_approvals_today() == 1
    assert orch.journal.count_unattended_approvals_today() == int(unattended)
