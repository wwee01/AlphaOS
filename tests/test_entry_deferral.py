"""Monitor contention preserves pending proposals, approval authority and caps."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import threading
import time

import pytest

from alphaos.constants import OrderState, ReasonCode
from alphaos.execution.order_manager import OrderResult
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.scheduler.ownership import monitor_ownership
from alphaos.util import timeutils
from conftest import inject_pending_proposal, make_settings
from test_proposal_ttl_flow import _propose_evaluation


@contextmanager
def held_monitor(journal, *, duration=None, before_release=None, during_hold=None):
    acquired, release, done = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def run():
        try:
            with monitor_ownership(journal) as owned:
                assert owned
                acquired.set()
                if during_hold:
                    during_hold()
                release.wait(15 if duration is None else duration)
                if before_release:
                    before_release()
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=run)
    thread.start()
    assert acquired.wait(3)
    try:
        yield done
    finally:
        release.set()
        thread.join(3)
        assert not thread.is_alive()
        assert not errors, errors


@pytest.fixture
def paper_entry(tmp_path, monkeypatch):
    clock = [datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(timeutils, 'now_utc', lambda: clock[0])
    settings = make_settings(APPROVAL_MODE='auto', REQUIRE_MANUAL_APPROVAL='false',
                             MAX_AUTO_APPROVALS_PER_DAY=1, MAX_UNATTENDED_APPROVALS_PER_DAY=1)
    journal = JournalStore(str(tmp_path / 'entry.db'))
    orch = Orchestrator(settings=settings, journal=journal)
    orch.startup()
    exposure = dict(equity=100000, gross_committed_notional=0, open_positions=0, account_pnl_today=0)
    monkeypatch.setattr('alphaos.risk.account_exposure.read_exposure', lambda *args: dict(exposure))
    original_execute = orch.orders.execute_proposal
    attempted = []

    def broker_free_execute(proposal, **kwargs):
        # Exercise the caller and durable receipt/status logic, using the
        # existing simulator for the placement boundary (never a broker).
        attempted.append(proposal.proposal_id)
        assert journal.scalar('SELECT COUNT(*) FROM approvals WHERE approved=1') == 1
        orch.orders.real_paper = False
        try:
            return original_execute(proposal, **kwargs)
        finally:
            orch.orders.real_paper = True

    orch.orders.real_paper = True
    monkeypatch.setattr(orch.orders, 'execute_proposal', broker_free_execute)
    journal.insert('job_runs', {
        'job_run_id': 'monitor-ready', 'job_type': 'monitor', 'lock_key': 'initial',
        'status': 'completed', 'started_at_utc': clock[0].isoformat(),
        'started_at_sgt': timeutils.stamp(clock[0]).local_sgt,
        'finished_at_utc': clock[0].isoformat(), 'result_summary_json': {'worker_role': 'monitor'},
    })
    yield orch, clock, exposure, attempted
    orch.close()


def test_four_second_monitor_pass_waits_then_manually_submits_once(paper_entry):
    orch, _, _, attempted = paper_entry
    pid, _ = inject_pending_proposal(orch)
    start = time.monotonic()
    with held_monitor(orch.journal, duration=4) as done:
        ok, message = orch.approve_proposal(pid, approver='explicit user')
        assert done.is_set()
    assert ok, message
    assert 3.8 <= time.monotonic() - start < 10
    assert attempted == [pid]
    assert orch.journal.proposal_by_id(pid)['status'] == 'filled'
    assert orch.journal.count_rows('approvals') == 1
    assert orch.journal.count_auto_approvals_today() == 0
    assert not orch.approve_proposal(pid)[0]
    assert attempted == [pid]


def test_manual_timeout_is_pending_without_receipt_and_requires_explicit_retry(paper_entry, monkeypatch):
    orch, _, _, attempted = paper_entry
    monkeypatch.setattr('alphaos.orchestrator.ENTRY_MONITOR_WAIT_SECONDS', 0.1)
    pid, _ = inject_pending_proposal(orch)
    with held_monitor(orch.journal):
        ok, message = orch.approve_proposal(pid)
    assert not ok and 'execution deferred' in message and 'fresh approval retry' in message
    assert orch.journal.proposal_by_id(pid)['status'] == 'pending_approval'
    assert orch.journal.count_rows('approvals') == 0
    assert attempted == []
    # Releasing a lock grants no standing manual approval or background order.
    assert orch.approve_proposal(pid)[0]
    assert attempted == [pid]
    assert orch.journal.count_rows('approvals') == 1


@pytest.mark.parametrize('unattended', [False, True])
def test_auto_timeout_does_not_consume_either_cap_and_next_eligible_proposal_can_submit(paper_entry, monkeypatch, unattended):
    orch, _, _, attempted = paper_entry
    monkeypatch.setattr('alphaos.orchestrator.ENTRY_MONITOR_WAIT_SECONDS', 0.1)
    cand, evaluation, summary = _propose_evaluation(orch)
    with held_monitor(orch.journal):
        assert orch._handle_proposal(cand, evaluation, summary, unattended=unattended)
    assert summary.entry_deferred == 1 and summary.pending_manual == 1
    assert summary.auto_submitted == 0
    assert orch.journal.one('SELECT * FROM trade_proposals WHERE candidate_id=?', (cand['candidate_id'],))['status'] == 'pending_approval'
    assert orch.journal.count_auto_approvals_today() == 0
    assert orch.journal.count_unattended_approvals_today() == 0
    assert orch.journal.count_rows('approvals') == 0 and attempted == []
    next_cand, next_eval, next_summary = _propose_evaluation(orch, symbol='MSFT')
    assert orch._handle_proposal(next_cand, next_eval, next_summary, unattended=unattended)
    assert next_summary.auto_submitted == 1
    assert len(attempted) == 1
    assert orch.journal.count_auto_approvals_today() == 1
    assert orch.journal.count_unattended_approvals_today() == int(unattended)


def test_auto_pass_after_monitor_wait_records_approval_only_at_submission(paper_entry):
    orch, _, _, attempted = paper_entry
    cand, evaluation, summary = _propose_evaluation(orch)
    def verify_no_early_receipt():
        other = JournalStore(orch.journal.db_path, read_only=True)
        try:
            assert other.count_rows('approvals') == 0
        finally:
            other.close()
    with held_monitor(orch.journal, duration=0.15, before_release=verify_no_early_receipt):
        orch._handle_proposal(cand, evaluation, summary)
    assert summary.auto_submitted == 1 and summary.entry_deferred == 0
    assert len(attempted) == 1 and orch.journal.count_auto_approvals_today() == 1


def test_expiry_during_wait_is_terminal_expired_without_consuming_manual_approval(paper_entry):
    orch, clock, _, attempted = paper_entry
    pid, _ = inject_pending_proposal(orch)
    expiry = clock[0] + timedelta(seconds=1)
    orch.journal.conn.execute('UPDATE trade_proposals SET proposal_expires_at_utc=? WHERE proposal_id=?',
                              (expiry.isoformat(), pid))
    orch.journal.conn.commit()
    def advance():
        clock[0] += timedelta(seconds=2)
    with held_monitor(orch.journal, duration=0.15, before_release=advance):
        ok, message = orch.approve_proposal(pid)
    assert not ok and ReasonCode.PROPOSAL_EXPIRED.value in message
    assert orch.journal.proposal_by_id(pid)['status'] == 'expired'
    assert orch.journal.count_rows('approvals') == 0 and attempted == []


@pytest.mark.parametrize('change', ['exposure', 'stale_quote', 'price_drift', 'monitor_failed'])
def test_changed_conditions_during_wait_are_rechecked_before_approval(paper_entry, change, monkeypatch):
    orch, clock, exposure, attempted = paper_entry
    pid, _ = inject_pending_proposal(orch)
    original_snapshot = orch.market.get_snapshot
    changed = [False]
    def snapshot(symbol):
        data = dict(original_snapshot(symbol))
        if changed[0] and change == 'stale_quote':
            data.update(quote_timestamp='2020-01-01T00:00:00+00:00', source_timestamp='2020-01-01T00:00:00+00:00')
        if changed[0] and change == 'price_drift':
            data['last_price'] *= 1.2
        return data
    monkeypatch.setattr(orch.market, 'get_snapshot', snapshot)
    def alter():
        changed[0] = True
        if change == 'exposure':
            exposure['gross_committed_notional'] = 99999
        if change == 'monitor_failed':
            other = JournalStore(orch.journal.db_path)
            try:
                other.insert('job_runs', {
                    'job_run_id': 'monitor-failed', 'job_type': 'monitor', 'lock_key': 'later',
                    'status': 'failed', 'started_at_utc': (clock[0] + timedelta(seconds=1)).isoformat(),
                    'finished_at_utc': (clock[0] + timedelta(seconds=1)).isoformat(),
                    'started_at_sgt': timeutils.stamp(clock[0]).local_sgt,
                })
            finally:
                other.close()
    with held_monitor(orch.journal, duration=0.15, before_release=alter):
        ok, _ = orch.approve_proposal(pid)
    assert not ok
    assert orch.journal.count_rows('approvals') == 0 and attempted == []
    expected_status = 'pending_approval' if change in {'stale_quote', 'price_drift'} else 'blocked'
    assert orch.journal.proposal_by_id(pid)['status'] == expected_status


def test_rejection_during_wait_is_preserved_and_not_approved(paper_entry):
    orch, _, _, attempted = paper_entry
    pid, _ = inject_pending_proposal(orch)
    def reject():
        other = JournalStore(orch.journal.db_path)
        try:
            other.conn.execute("UPDATE trade_proposals SET status='rejected' WHERE proposal_id=?", (pid,))
            other.conn.commit()
        finally:
            other.close()
    with held_monitor(orch.journal, duration=0.15, before_release=reject):
        ok, _ = orch.approve_proposal(pid)
    assert not ok
    assert orch.journal.proposal_by_id(pid)['status'] == 'rejected'
    assert orch.journal.count_rows('approvals') == 0 and attempted == []


@pytest.mark.parametrize('outcome', ['uncertain', 'rejected'])
def test_actual_submission_attempt_preserves_approval_and_daily_cap(paper_entry, monkeypatch, outcome):
    orch, _, _, _ = paper_entry
    cand, evaluation, summary = _propose_evaluation(orch)
    attempted = []
    def submit(proposal, **kwargs):
        assert orch.journal.count_auto_approvals_today() == 1
        attempted.append(proposal.proposal_id)
        return OrderResult(blocked=outcome == 'rejected', state=OrderState.REJECTED.value if outcome == 'rejected' else OrderState.ACCEPTED.value,
                           submission_uncertain=outcome == 'uncertain', block_reason='broker_rejected' if outcome == 'rejected' else None)
    monkeypatch.setattr(orch.orders, 'execute_proposal', submit)
    orch._handle_proposal(cand, evaluation, summary)
    assert len(attempted) == 1
    assert orch.journal.count_auto_approvals_today() == 1
    assert orch.journal.count_rows('approvals') == 1
    row = orch.journal.one('SELECT * FROM trade_proposals WHERE candidate_id=?', (cand['candidate_id'],))
    assert row['status'] == ('blocked' if outcome == 'rejected' else 'submitted')


def test_automatic_cap_is_rechecked_after_wait_and_never_double_consumed(paper_entry):
    orch, _, _, attempted = paper_entry
    cand, evaluation, summary = _propose_evaluation(orch)
    def consume_cap_elsewhere():
        other = JournalStore(orch.journal.db_path)
        try:
            other.insert('approvals', {
                'approval_id': 'other-approval', 'proposal_id': 'other-proposal',
                'candidate_id': 'other-candidate', 'symbol': 'MSFT',
                'approval_mode': 'auto', 'label': 'AUTO_APPROVED', 'approved': 1,
                'approver': 'auto', 'reason': 'concurrent prior approval',
            })
        finally:
            other.close()
    with held_monitor(orch.journal, duration=0.15, before_release=consume_cap_elsewhere):
        orch._handle_proposal(cand, evaluation, summary)
    assert summary.entry_deferred == 1 and summary.auto_submitted == 0
    assert orch.journal.count_auto_approvals_today() == 1
    assert orch.journal.count_rows('approvals') == 1 and attempted == []
    row = orch.journal.one('SELECT * FROM trade_proposals WHERE candidate_id=?', (cand['candidate_id'],))
    assert row['status'] == 'pending_approval'


def test_automatic_expiry_during_wait_marks_expired_without_receipt(paper_entry, monkeypatch):
    orch, clock, _, attempted = paper_entry
    cand, evaluation, summary = _propose_evaluation(orch)
    original_stamp = orch._stamp_proposal_ttl
    def brief_ttl(proposal, snapshot):
        original_stamp(proposal, snapshot)
        proposal.proposal_expires_at_utc = (clock[0] + timedelta(seconds=1)).isoformat()
    monkeypatch.setattr(orch, '_stamp_proposal_ttl', brief_ttl)
    def advance():
        clock[0] += timedelta(seconds=2)
    with held_monitor(orch.journal, duration=0.15, before_release=advance):
        orch._handle_proposal(cand, evaluation, summary)
    assert summary.auto_submitted == 0 and summary.entry_deferred == 0
    assert orch.journal.count_rows('approvals') == 0 and attempted == []
    row = orch.journal.one('SELECT * FROM trade_proposals WHERE candidate_id=?', (cand['candidate_id'],))
    assert row['status'] == 'expired'


def test_margin_authorization_is_not_inferred_from_a_deferred_manual_attempt(paper_entry, monkeypatch):
    orch, _, _, attempted = paper_entry
    monkeypatch.setattr('alphaos.orchestrator.ENTRY_MONITOR_WAIT_SECONDS', 0.1)
    pid, _ = inject_pending_proposal(orch)
    orch.journal.conn.execute('UPDATE trade_proposals SET requires_margin=1 WHERE proposal_id=?', (pid,))
    orch.journal.conn.commit()
    with held_monitor(orch.journal):
        ok, message = orch.approve_proposal(pid, approve_margin=True)
    assert not ok and 'deferred' in message
    assert orch.journal.count_rows('approvals') == 0
    ok, message = orch.approve_proposal(pid, approve_margin=False)
    assert not ok and 'explicit margin approval' in message
    assert attempted == []


def test_competing_entry_ownership_defers_without_consuming_manual_approval(paper_entry):
    from alphaos.risk.account_exposure import entry_submission_lock, EntrySubmissionBusy
    orch, _, _, attempted = paper_entry
    pid, _ = inject_pending_proposal(orch)
    with entry_submission_lock(orch.journal):
        with pytest.raises(EntrySubmissionBusy):
            with entry_submission_lock(orch.journal):
                pytest.fail('second entry acquired the same account')
        ok, message = orch.approve_proposal(pid)
    assert not ok and 'deferred' in message
    assert orch.journal.proposal_by_id(pid)['status'] == 'pending_approval'
    assert orch.journal.count_rows('approvals') == 0 and attempted == []
    assert orch.approve_proposal(pid)[0]
    assert attempted == [pid]


@pytest.mark.parametrize('withdrawal', ['rejected', 'superseded'])
def test_timeout_after_withdrawal_preserves_status_and_does_not_claim_pending(paper_entry, monkeypatch, withdrawal):
    orch, _, _, attempted = paper_entry
    monkeypatch.setattr('alphaos.orchestrator.ENTRY_MONITOR_WAIT_SECONDS', 0.1)
    pid, _ = inject_pending_proposal(orch)
    checked, withdrawn = threading.Event(), threading.Event()
    original_risk = orch._assess_current_risk
    def checked_risk(**kwargs):
        result = original_risk(**kwargs)
        checked.set()
        assert withdrawn.wait(3)
        return result
    monkeypatch.setattr(orch, '_assess_current_risk', checked_risk)
    def withdraw_while_monitor_remains_busy():
        assert checked.wait(3)
        other = JournalStore(orch.journal.db_path)
        try:
            other.conn.execute('UPDATE trade_proposals SET status=? WHERE proposal_id=?', (withdrawal, pid))
            other.conn.commit()
        finally:
            other.close()
        withdrawn.set()
    with held_monitor(orch.journal, during_hold=withdraw_while_monitor_remains_busy):
        ok, message = orch.approve_proposal(pid)
    assert not ok and ReasonCode.APPROVAL_REQUIRED.value in message
    assert 'deferred' not in message and 'remains pending' not in message
    assert orch.journal.proposal_by_id(pid)['status'] == withdrawal
    assert orch.journal.count_rows('approvals') == 0 and attempted == []
