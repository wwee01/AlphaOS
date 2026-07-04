"""End-to-end proposal TTL / stale-approval guard (PR6): every proposal is
TTL-stamped at creation (scan, scheduler, user-override, and demo-seed paths
alike), an expired proposal is rejected BEFORE any broker submission and the
expiry is persisted for audit, a fresh proposal supersedes any other open
proposal for the same symbol, and every existing safety invariant (kill
switch, protection, manual approval, real-money-unreachable) is unaffected.
Hermetic -- mock mode, no network."""

from __future__ import annotations

import pathlib

from alphaos import proposals as proposal_ttl_pkg
from alphaos.constants import EarningsDataStatus, ProposalStatus, ReasonCode
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.safety import KillSwitch
from alphaos.util import timeutils
from alphaos.util.ids import new_id
from conftest import inject_pending_proposal, make_settings


def _orch(**over):
    return Orchestrator(settings=make_settings(**over), journal=JournalStore(":memory:"))


# --------------------------------------------------------------- creation paths
def test_scan_created_proposal_gets_ttl_stamped():
    o = _orch(LABELLING_ENABLED="true", INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    o.run_scan_once()
    rows = o.journal.query(
        "SELECT proposal_ttl_seconds, proposal_expires_at_utc FROM trade_proposals"
    )
    assert rows
    for r in rows:
        assert r["proposal_ttl_seconds"] is not None
        assert r["proposal_expires_at_utc"] is not None
    o.close()


def test_scheduled_scan_proposal_gets_ttl_stamped():
    from alphaos.scheduler import JobRunner

    o = _orch(LABELLING_ENABLED="true", INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    o.startup()
    result = JobRunner(o).run_job("scan")
    if result["status"] != "completed":
        o.close()
        return  # kill switch/cost cap skipped this pass -- nothing to assert
    rows = o.journal.query("SELECT proposal_ttl_seconds FROM trade_proposals")
    if rows:  # a scan can complete with zero proposals depending on mock data
        assert all(r["proposal_ttl_seconds"] is not None for r in rows)
    o.close()


def test_seed_demo_proposal_gets_ttl_and_approves_within_window():
    o = _orch()
    demo = o.seed_demo()
    assert demo["approved"] is True
    row = o.journal.proposal_by_id(demo["proposal_id"])
    assert row["proposal_ttl_seconds"] == o.settings.proposal_ttl_rth_seconds
    assert row["proposal_expires_at_utc"] is not None
    o.close()


def test_inject_pending_proposal_fixture_is_fresh_by_construction():
    """The shared test fixture itself must produce an approvable (non-expired)
    proposal by default -- proving the general approval-flow test suite isn't
    silently broken by TTL."""
    o = _orch()
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    row = o.journal.proposal_by_id(pid)
    assert row["proposal_ttl_seconds"] is not None
    assert not proposal_ttl_pkg.is_expired(row["proposal_expires_at_utc"])
    o.close()


# --------------------------------------------------------- approval-time guard
def test_fresh_proposal_within_ttl_proceeds_to_existing_checks():
    o = _orch()
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    ok, msg = o.approve_proposal(pid, approver="test")
    assert ok is True
    assert o.journal.count_rows("paper_fills") == 1
    o.close()


def test_expired_proposal_rejected_before_submission():
    o = _orch()
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    o.journal.conn.execute(
        "UPDATE trade_proposals SET proposal_expires_at_utc = ? WHERE proposal_id = ?",
        (timeutils.to_iso(timeutils.now_utc() - __import__("datetime").timedelta(hours=1)), pid),
    )
    o.journal.conn.commit()

    ok, msg = o.approve_proposal(pid, approver="test")
    assert ok is False
    assert "expired" in msg.lower()
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_rows("paper_fills") == 0
    assert o.journal.count_open_positions() == 0
    o.close()


def test_expired_proposal_creates_no_execution_artifacts():
    o = _orch()
    pid, _ = inject_pending_proposal(o, symbol="MSFT")
    o.journal.conn.execute(
        "UPDATE trade_proposals SET proposal_expires_at_utc = '2000-01-01T00:00:00+00:00' "
        "WHERE proposal_id = ?", (pid,),
    )
    o.journal.conn.commit()
    o.approve_proposal(pid, approver="test")
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_rows("paper_fills") == 0
    assert o.journal.count_rows("positions") == 0
    assert o.journal.count_rows("approvals") == 0  # never even reached the approvals record
    o.close()


def test_auto_approval_path_never_executes_an_expired_proposal():
    """AUTO approval mode does NOT flow through approve_proposal()'s stale guard
    -- it goes _handle_proposal -> consider() -> _execute() directly. A
    proposal born already-expired (e.g. the TTL=0 closed-session bucket) must
    STILL never auto-execute. Regression for the formal-audit finding that the
    guard originally lived only on the manual path. Forces born-expired TTL
    deterministically (mock snapshots are always REGULAR->1800s, so a natural
    scan can't produce an expired proposal)."""
    from datetime import timedelta

    o = _orch(APPROVAL_MODE="auto", REQUIRE_MANUAL_APPROVAL="false",
              MAX_AUTO_APPROVALS_PER_DAY="50", LABELLING_ENABLED="true",
              INTEREST_SCAN_TOP_N="6", MAX_CANDIDATES_TO_AI="6")
    assert o.settings.effective_approval_mode.value == "auto"

    def _born_expired(proposal, snapshot=None):
        proposal.proposal_ttl_seconds = 60
        proposal.proposal_expires_at_utc = timeutils.to_iso(
            timeutils.now_utc() - timedelta(hours=1))

    o._stamp_proposal_ttl = _born_expired
    summ = o.run_scan_once()

    assert summ.auto_submitted == 0
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_rows("paper_fills") == 0
    assert o.journal.count_open_positions() == 0
    # the born-expired proposals are cleanly marked 'expired' (not silently dropped)
    expired = o.journal.query(
        "SELECT * FROM trade_proposals WHERE status = 'expired'")
    assert expired
    for r in expired:
        assert r["expired_reason"] == ReasonCode.PROPOSAL_EXPIRED.value
        assert r["expired_at_utc"] is not None
    o.close()


def test_execute_chokepoint_blocks_expired_proposal_directly():
    """_execute() is the single funnel every submission route passes through;
    it carries the TTL guard as an unconditional backstop, so even a
    hypothetical FUTURE caller that skipped the approval-path checks cannot
    submit a stale proposal."""
    from datetime import timedelta

    from alphaos.strategy.proposal import TradeProposal

    o = _orch()
    prop = TradeProposal(
        symbol="AAPL", direction="long", strategy="swing", entry=100.0, stop=97.0,
        target=106.0, max_holding_days=3, qty=10, risk_per_share=3.0, dollar_risk=30.0,
        expected_r=2.0, same_day_exit_eligible=True, status="approved",
    )
    prop.proposal_expires_at_utc = timeutils.to_iso(timeutils.now_utc() - timedelta(hours=1))

    result = o._execute(prop)

    assert result.blocked is True
    assert result.block_reason == ReasonCode.PROPOSAL_EXPIRED.value
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_rows("paper_fills") == 0
    o.close()


def test_execute_chokepoint_lets_fresh_proposal_through():
    """The chokepoint guard must be a no-op for a fresh proposal -- prove it
    doesn't over-block the normal manual path."""
    o = _orch()
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    ok, msg = o.approve_proposal(pid, approver="test")
    assert ok is True
    assert o.journal.count_rows("paper_fills") == 1
    o.close()


def test_expired_proposal_remains_auditable():
    o = _orch()
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    before = o.journal.proposal_by_id(pid)
    o.journal.conn.execute(
        "UPDATE trade_proposals SET proposal_expires_at_utc = '2000-01-01T00:00:00+00:00' "
        "WHERE proposal_id = ?", (pid,),
    )
    o.journal.conn.commit()
    o.approve_proposal(pid, approver="test")

    after = o.journal.proposal_by_id(pid)
    assert after is not None                          # never deleted
    assert after["status"] == ProposalStatus.EXPIRED.value
    assert after["expired_reason"] == ReasonCode.PROPOSAL_EXPIRED.value
    assert after["expired_at_utc"] is not None
    # core trade fields untouched -- only lifecycle/audit fields changed
    assert after["entry"] == before["entry"]
    assert after["stop"] == before["stop"]
    assert after["target"] == before["target"]
    assert after["proposal_id"] == before["proposal_id"]
    o.close()


def test_legacy_null_expiry_proposal_treated_as_expired():
    """A proposal row from before PR6 existed (NULL TTL fields) must fail safe
    -- never be treated as fresh just because it predates this guard."""
    o = _orch()
    cand_id = new_id("cand")
    o.journal.insert("candidates", {
        "candidate_id": cand_id, "symbol": "AAPL", "direction": "long",
        "strategy": "swing", "status": "proposed",
    })
    snap = o.market.get_snapshot("AAPL")
    entry = float(snap["last_price"])
    from alphaos.strategy.proposal import TradeProposal

    prop = TradeProposal(
        symbol="AAPL", direction="long", strategy="swing", entry=entry,
        stop=round(entry * 0.97, 2), target=round(entry * 1.06, 2), max_holding_days=3,
        qty=10, risk_per_share=entry * 0.03, dollar_risk=entry * 0.03 * 10,
        expected_r=2.0, same_day_exit_eligible=True, candidate_id=cand_id,
        status="pending_approval",
    )
    # Deliberately do NOT stamp TTL -- simulates a pre-PR6 row.
    o.journal.insert("trade_proposals", prop.to_row())
    assert o.journal.proposal_by_id(prop.proposal_id)["proposal_expires_at_utc"] is None

    ok, msg = o.approve_proposal(prop.proposal_id, approver="test")
    assert ok is False
    assert "expired" in msg.lower()
    o.close()


def test_old_db_gets_ttl_columns_added_nullable(tmp_path):
    """A ledger written before PR6 (no TTL columns on trade_proposals) must open
    cleanly: the 6 new columns are ALTER-added as nullable, an existing pre-PR6
    proposal row survives with NULL TTL fields, and SCHEMA_VERSION doesn't move
    (purely additive). Complements the behavioral fail-safe test above with the
    schema-migration half of legacy-row safety."""
    import sqlite3

    from alphaos.journal.schema import SCHEMA_VERSION

    db = str(tmp_path / "pre_pr6.db")
    raw = sqlite3.connect(db)
    # Minimal pre-PR6 trade_proposals table (no proposal_ttl_* / superseded_* cols).
    raw.execute(
        "CREATE TABLE trade_proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "proposal_id TEXT, candidate_id TEXT, symbol TEXT, status TEXT)"
    )
    raw.execute(
        "INSERT INTO trade_proposals (proposal_id, candidate_id, symbol, status) "
        "VALUES ('old_prop', 'old_cand', 'AAPL', 'pending_approval')"
    )
    raw.execute("PRAGMA user_version = 0")
    raw.commit()
    raw.close()

    j = JournalStore(db)
    try:
        cols = {r["name"] for r in j.conn.execute("PRAGMA table_info(trade_proposals)")}
        for c in ("proposal_ttl_seconds", "proposal_expires_at_utc", "expired_reason",
                  "expired_at_utc", "superseded_by_proposal_id", "superseded_at_utc"):
            assert c in cols, f"missing additive column {c}"
        old = j.one("SELECT * FROM trade_proposals WHERE proposal_id = 'old_prop'")
        assert old["symbol"] == "AAPL"
        assert old["proposal_expires_at_utc"] is None   # legacy row: NULL TTL
        assert j.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert SCHEMA_VERSION == 3
    finally:
        j.close()


# ---------------------------------------------------------------- supersession
def test_supersession_blocks_approval_of_old_proposal():
    o = _orch()
    old_pid, _ = inject_pending_proposal(o, symbol="AAPL")
    o._supersede_open_proposals("AAPL", "prop_fresh_fake")

    ok, msg = o.approve_proposal(old_pid, approver="test")
    assert ok is False
    assert "not approvable" in msg
    assert "superseded" in msg
    row = o.journal.proposal_by_id(old_pid)
    assert row["status"] == ProposalStatus.SUPERSEDED.value
    assert row["superseded_by_proposal_id"] == "prop_fresh_fake"
    assert row["superseded_at_utc"] is not None
    o.close()


def test_supersession_does_not_delete_or_mutate_core_trade_fields():
    o = _orch()
    old_pid, _ = inject_pending_proposal(o, symbol="AAPL")
    before = o.journal.proposal_by_id(old_pid)
    o._supersede_open_proposals("AAPL", "prop_fresh_fake")
    after = o.journal.proposal_by_id(old_pid)
    assert after is not None
    assert after["entry"] == before["entry"]
    assert after["stop"] == before["stop"]
    assert after["target"] == before["target"]
    assert after["qty"] == before["qty"]
    o.close()


def test_supersession_only_affects_same_symbol():
    o = _orch()
    aapl_pid, _ = inject_pending_proposal(o, symbol="AAPL")
    msft_pid, _ = inject_pending_proposal(o, symbol="MSFT")
    o._supersede_open_proposals("AAPL", "prop_fresh_fake")
    assert o.journal.proposal_by_id(aapl_pid)["status"] == ProposalStatus.SUPERSEDED.value
    assert o.journal.proposal_by_id(msft_pid)["status"] == "pending_approval"
    o.close()


def test_supersession_excludes_the_new_proposal_itself():
    """The just-created proposal must never supersede itself."""
    o = _orch()
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    o._supersede_open_proposals("AAPL", pid)
    assert o.journal.proposal_by_id(pid)["status"] == "pending_approval"
    o.close()


def _propose_evaluation(o, symbol="AAPL"):
    """A hand-built PROPOSE-decision evaluation for ``symbol``, so
    _handle_proposal can be exercised deterministically for a KNOWN symbol --
    never depending on which symbols the mock scanner/evaluator happen to
    propose on a given run (that set varies with the date-seeded mock RNG)."""
    from alphaos.ai.openai_client import OpenAIEvaluation
    from alphaos.constants import Decision
    from alphaos.orchestrator import ScanSummary
    from alphaos.util.ids import new_id

    snap = o.market.get_snapshot(symbol)
    entry = float(snap["last_price"])
    cand_id = new_id("cand")
    o.journal.insert("candidates", {
        "candidate_id": cand_id, "symbol": symbol, "direction": "long",
        "strategy": "swing", "status": "detected",
    })
    cand = {"candidate_id": cand_id, "symbol": symbol, "_snapshot": snap}
    evaluation = OpenAIEvaluation(
        eval_id=new_id("eval"), candidate_id=cand_id, symbol=symbol, model="mock",
        direction="long", entry=entry, stop=round(entry * 0.97, 2), target=round(entry * 1.06, 2),
        max_holding_days=3, expected_r=2.0, confidence=0.8, decision=Decision.PROPOSE.value,
        reasoning_summary="test",
    )
    summary = ScanSummary(scan_id="test_scan")
    return cand, evaluation, summary


def test_end_to_end_fresh_proposal_supersedes_old_one_via_handle_proposal():
    """The REAL creation path (_handle_proposal) supersedes an existing open
    proposal for the same symbol -- not just the standalone helper. Uses a
    deterministic hand-built PROPOSE evaluation for AAPL specifically (not a
    natural scan, whose proposed symbol set varies with the mock RNG)."""
    o = _orch()
    old_pid, _ = inject_pending_proposal(o, symbol="AAPL")
    cand, evaluation, summary = _propose_evaluation(o, symbol="AAPL")

    handled = o._handle_proposal(cand, evaluation, summary)

    assert handled is True
    new_row = o.journal.one(
        "SELECT * FROM trade_proposals WHERE symbol = 'AAPL' AND proposal_id != ? "
        "ORDER BY id DESC LIMIT 1", (old_pid,),
    )
    assert new_row is not None
    assert new_row["status"] == "pending_approval"
    old_row = o.journal.proposal_by_id(old_pid)
    assert old_row["status"] == ProposalStatus.SUPERSEDED.value
    assert old_row["superseded_by_proposal_id"] == new_row["proposal_id"]
    o.close()


def test_risk_blocked_reevaluation_does_not_supersede_existing_open_proposal():
    """A later scan that risk-BLOCKS a fresh AAPL evaluation must NOT
    invalidate the existing, still-good open AAPL proposal -- only an
    APPROVABLE fresh proposal supersedes. Directly exercises the risk-blocked
    branch of _handle_proposal, for the SAME symbol as the old proposal (the
    weakness a natural-scan-based test can't guarantee)."""
    from unittest.mock import MagicMock

    from alphaos.risk.risk_engine import RiskDecision

    o = _orch()
    old_pid, _ = inject_pending_proposal(o, symbol="AAPL")
    cand, evaluation, summary = _propose_evaluation(o, symbol="AAPL")

    o.risk.assess = MagicMock(return_value=RiskDecision(
        approved=False, sizing=None,
        block_reasons=[{"code": ReasonCode.RISK_OVERSIZED.value}],
    ))
    handled = o._handle_proposal(cand, evaluation, summary)

    assert handled is False
    assert summary.risk_blocked == 1
    blocked_row = o.journal.one(
        "SELECT * FROM trade_proposals WHERE symbol = 'AAPL' AND proposal_id != ? "
        "ORDER BY id DESC LIMIT 1", (old_pid,),
    )
    assert blocked_row is not None and blocked_row["status"] == "blocked"
    old_row = o.journal.proposal_by_id(old_pid)
    assert old_row["status"] == "pending_approval"  # untouched -- never superseded by a blocked eval
    o.close()


# -------------------------------------------------------- digest / reporting
def test_digest_shows_active_and_expired_proposals():
    from alphaos.scheduler.digest import build_daily_digest

    o = _orch()
    active_pid, _ = inject_pending_proposal(o, symbol="AAPL")
    expired_pid, _ = inject_pending_proposal(o, symbol="MSFT")
    o.journal.conn.execute(
        "UPDATE trade_proposals SET proposal_expires_at_utc = '2000-01-01T00:00:00+00:00' "
        "WHERE proposal_id = ?", (expired_pid,),
    )
    o.journal.conn.commit()
    o.approve_proposal(expired_pid, approver="test")  # discover + persist the expiry

    digest = build_daily_digest(o.journal, o.settings, o.kill_switch)
    pl = digest["proposal_lifecycle"]
    assert active_pid in {r["proposal_id"] for r in pl["active_proposals_today"]}
    assert expired_pid in {r["proposal_id"] for r in pl["expired_proposals_today"]}
    o.close()


def test_digest_shows_stale_unmarked_proposal_before_approval_attempted():
    from alphaos.scheduler.digest import build_daily_digest

    o = _orch()
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    o.journal.conn.execute(
        "UPDATE trade_proposals SET proposal_expires_at_utc = '2000-01-01T00:00:00+00:00' "
        "WHERE proposal_id = ?", (pid,),
    )
    o.journal.conn.commit()
    # NO approval attempt -- status is still 'pending_approval' in the DB.

    digest = build_daily_digest(o.journal, o.settings, o.kill_switch)
    pl = digest["proposal_lifecycle"]
    assert pid in {r["proposal_id"] for r in pl["stale_unmarked_proposals_today"]}
    assert pid not in {r["proposal_id"] for r in pl["expired_proposals_today"]}
    o.close()


def test_digest_shows_superseded_proposal():
    from alphaos.scheduler.digest import build_daily_digest

    o = _orch()
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    o._supersede_open_proposals("AAPL", "prop_fresh_fake")

    digest = build_daily_digest(o.journal, o.settings, o.kill_switch)
    assert pid in {r["proposal_id"] for r in digest["proposal_lifecycle"]["superseded_proposals_today"]}
    o.close()


def test_list_open_proposals_shows_ttl_fields():
    o = _orch()
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    views = o.list_open_proposals()
    v = next(v for v in views if v["proposal_id"] == pid)
    assert v["proposal_ttl_seconds"] is not None
    assert v["proposal_expires_at_utc"] is not None
    assert v["proposal_seconds_remaining"] > 0
    assert v["proposal_is_stale"] is False
    o.close()


# -------------------------------------------------------------------- lineage
def test_proposal_ttl_config_hash_changes_with_settings():
    import dataclasses

    from alphaos.lineage.config_snapshot import build_config_hashes

    s1 = make_settings()
    s2 = dataclasses.replace(s1, proposal_ttl_rth_seconds=900)
    h1 = build_config_hashes(s1)
    h2 = build_config_hashes(s2)
    assert h1["proposal_ttl_config_hash"] != h2["proposal_ttl_config_hash"]
    assert h1["scanner_config_hash"] == h2["scanner_config_hash"]  # unrelated category untouched


def test_scan_proposal_lineage_includes_proposal_ttl_config_hash():
    o = _orch(LABELLING_ENABLED="true", INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    o.run_scan_once()
    row = o.journal.one(
        "SELECT lineage_id FROM trade_proposals WHERE lineage_id IS NOT NULL LIMIT 1"
    )
    assert row
    snap = o.journal.one(
        "SELECT proposal_ttl_config_hash FROM lineage_snapshots WHERE lineage_id = ?",
        (row["lineage_id"],),
    )
    assert snap and snap["proposal_ttl_config_hash"]
    o.close()


# ---------------------------------------------------- session-bucket selection
def test_premarket_snapshot_session_gets_extended_hours_ttl():
    o = _orch(PROPOSAL_TTL_EXTENDED_HOURS_SECONDS="300")
    from alphaos.strategy.proposal import TradeProposal

    prop = TradeProposal(
        symbol="AAPL", direction="long", strategy="swing", entry=100.0, stop=97.0,
        target=106.0, max_holding_days=3, qty=10, risk_per_share=3.0, dollar_risk=30.0,
        expected_r=2.0, same_day_exit_eligible=True,
    )
    o._stamp_proposal_ttl(prop, {"market_session": "premarket"})
    assert prop.proposal_ttl_seconds == 300
    o.close()


def test_closed_snapshot_session_gets_shortest_ttl():
    o = _orch(PROPOSAL_TTL_CLOSED_SESSION_SECONDS="0")
    from alphaos.strategy.proposal import TradeProposal

    prop = TradeProposal(
        symbol="AAPL", direction="long", strategy="swing", entry=100.0, stop=97.0,
        target=106.0, max_holding_days=3, qty=10, risk_per_share=3.0, dollar_risk=30.0,
        expected_r=2.0, same_day_exit_eligible=True,
    )
    o._stamp_proposal_ttl(prop, {"market_session": "closed"})
    assert prop.proposal_ttl_seconds == 0
    assert proposal_ttl_pkg.is_expired(prop.proposal_expires_at_utc)  # born already-stale
    o.close()


def test_missing_snapshot_session_falls_back_to_live_market_session():
    o = _orch()
    from alphaos.strategy.proposal import TradeProposal

    prop = TradeProposal(
        symbol="AAPL", direction="long", strategy="swing", entry=100.0, stop=97.0,
        target=106.0, max_holding_days=3, qty=10, risk_per_share=3.0, dollar_risk=30.0,
        expected_r=2.0, same_day_exit_eligible=True,
    )
    o._stamp_proposal_ttl(prop, {})  # no market_session key at all
    live_session = timeutils.market_session().value
    from alphaos.proposals.ttl import ttl_seconds_for_session
    assert prop.proposal_ttl_seconds == ttl_seconds_for_session(o.settings, live_session)
    o.close()


# ------------------------------------------------------------ safety invariants
def test_kill_switch_still_blocks_a_fresh_proposal(tmp_path):
    o = _orch()
    ks_path = tmp_path / "KILL_SWITCH"
    o.kill_switch = KillSwitch(str(ks_path))
    o.kill_switch.engage("test")
    pid, _ = inject_pending_proposal(o, symbol="AAPL")

    ok, msg = o.approve_proposal(pid, approver="test")
    assert ok is False
    assert "kill switch" in msg
    o.close()


def test_protection_incident_still_blocks_a_fresh_proposal():
    o = _orch()
    o.journal.insert("protection_checks", {
        "check_id": new_id("pcheck"), "position_id": "pos_fake", "symbol": "META",
        "protection_status": "unprotected", "severity": "critical",
        "detail": "test-injected incident",
    })
    pid, _ = inject_pending_proposal(o, symbol="AAPL")

    ok, msg = o.approve_proposal(pid, approver="test")
    assert ok is False
    assert "protection incident" in msg
    o.close()


def test_earnings_fields_remain_visible_on_a_fresh_ttl_guarded_proposal():
    """TTL must not hide or clear any other advisory context on the row --
    earnings summary fields (if present) survive untouched alongside TTL."""
    o = _orch()
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    o.journal.conn.execute(
        "UPDATE trade_proposals SET earnings_within_hold_window = 1, "
        "earnings_data_status = ? WHERE proposal_id = ?",
        (EarningsDataStatus.OK.value, pid),
    )
    o.journal.conn.commit()
    row = o.journal.proposal_by_id(pid)
    assert row["earnings_within_hold_window"] == 1
    assert not proposal_ttl_pkg.is_expired(row["proposal_expires_at_utc"])

    ok, msg = o.approve_proposal(pid, approver="test")
    assert ok is True  # earnings is advisory-only; does not block approval
    after = o.journal.proposal_by_id(pid)
    assert after["earnings_within_hold_window"] == 1  # untouched by the approval path
    o.close()


def test_real_money_unreachable_with_ttl_guard():
    o = _orch()
    assert o.system_health()["real_money_trading"] == "unreachable"
    o.close()


def test_manual_approval_still_required_by_default():
    o = _orch(LABELLING_ENABLED="true", INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    o.run_scan_once()
    approved_or_filled = o.journal.query(
        "SELECT * FROM trade_proposals WHERE status IN ('approved', 'filled')"
    )
    assert approved_or_filled == []
    o.close()


def test_ttl_does_not_change_decision_distribution():
    """Toggling the TTL settings must not change which candidates get
    proposed/watched/rejected -- TTL is stamped strictly AFTER the decision is
    already made, never influences it."""
    base = {"INTEREST_SCAN_TOP_N": "12", "MAX_CANDIDATES_TO_AI": "12", "LABELLING_ENABLED": "true"}
    short_ttl = Orchestrator(settings=make_settings(PROPOSAL_TTL_RTH_SECONDS="60", **base),
                             journal=JournalStore(":memory:"))
    summ_short = short_ttl.run_scan_once()
    short_ttl.close()

    long_ttl = Orchestrator(settings=make_settings(PROPOSAL_TTL_RTH_SECONDS="7200", **base),
                            journal=JournalStore(":memory:"))
    summ_long = long_ttl.run_scan_once()
    long_ttl.close()

    assert summ_short.proposed == summ_long.proposed
    assert summ_short.watch == summ_long.watch
    assert summ_short.rejected == summ_long.rejected


# --------------------------------------------------------- structural safety
def test_no_orders_approvals_fills_positions_created_by_proposals_ttl_code():
    """The proposals/ttl package must never touch order/approval/position
    state -- same structural grep-based check used for the scheduler/lineage/
    earnings packages."""
    ttl_dir = pathlib.Path(proposal_ttl_pkg.__file__).parent
    banned = ("execute_proposal", "approve_proposal", "close_position",
              "submit_bracket", "submit_order", "place_order")
    for py_file in ttl_dir.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{py_file.name} references {token!r}"


def test_proposals_package_never_references_alpaca_client():
    ttl_dir = pathlib.Path(proposal_ttl_pkg.__file__).parent
    for py_file in ttl_dir.glob("*.py"):
        assert "alpaca_client" not in py_file.read_text(encoding="utf-8")


def test_double_approval_of_expired_proposal_is_idempotent():
    """Attempting to approve an already-expired (and already-marked) proposal
    a second time must stay blocked, never crash, never double-submit."""
    o = _orch()
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    o.journal.conn.execute(
        "UPDATE trade_proposals SET proposal_expires_at_utc = '2000-01-01T00:00:00+00:00' "
        "WHERE proposal_id = ?", (pid,),
    )
    o.journal.conn.commit()
    ok1, msg1 = o.approve_proposal(pid, approver="test")
    ok2, msg2 = o.approve_proposal(pid, approver="test")
    assert ok1 is False and ok2 is False
    assert o.journal.count_rows("paper_fills") == 0
    o.close()
