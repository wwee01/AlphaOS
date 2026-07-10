"""PR14: Red-Team Debate v0 -- shadow-only, bear-only adversarial voting.
Scored for PROPOSE-decision proposals only (never WATCH/REJECT), computed
strictly AFTER decisions are committed so it cannot influence them, gated by
a daily sub-cap nested inside the shared 30-day AI cost cap, fail-safe
(per-item errors never fail a scan), idempotent (a second pass inserts zero
new rows). Hermetic -- mock mode, no network."""

from __future__ import annotations

import pathlib

import alphaos.debate as debate_pkg
from alphaos.debate.batch import score_debate_batch
from alphaos.ai.bear_debater import BearDebater
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.scheduler.cost_guard import check_debate_budget, debate_calls_today
from conftest import inject_pending_proposal, make_settings


def _orch(**over):
    base = {"LABELLING_ENABLED": "true"}
    base.update(over)
    return Orchestrator(settings=make_settings(**base), journal=JournalStore(":memory:"))


def _tag_batch(journal, proposal_id, scan_batch_id):
    journal.conn.execute(
        "UPDATE trade_proposals SET scan_batch_id = ? WHERE proposal_id = ?",
        (scan_batch_id, proposal_id),
    )
    journal.conn.commit()


# ------------------------------------------------------------- default posture
def test_disabled_by_default():
    o = _orch()
    assert o.settings.debate_shadow_enabled is False
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    _tag_batch(o.journal, pid, "batch1")
    summ = o.run_scan_once()
    assert summ.debate_voted == 0
    assert o.journal.count_rows("agent_votes") == 0
    o.close()


def test_enabled_votes_on_a_deterministically_injected_proposal():
    o = _orch(DEBATE_SHADOW_ENABLED="true")
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    _tag_batch(o.journal, pid, "batch1")
    result = score_debate_batch(o.journal, o.settings, "batch1")
    assert result == {"voted": 1, "skipped": 0, "budget_exhausted": False}
    row = o.journal.one("SELECT * FROM agent_votes WHERE proposal_id = ?", (pid,))
    assert row is not None
    assert row["agent_role"] == "bear"
    assert row["stance"] in ("oppose", "neutral", "support")
    assert 0.0 <= row["conviction"] <= 1.0
    assert row["is_mock"] == 1
    o.close()


# --------------------------------------------------------------- scope: PROPOSE only
def test_watch_and_rejected_candidates_get_no_vote():
    """The exact bug class this test guards: trade_proposals.status never
    persists 'proposed' for a real propose decision (it is overwritten to
    pending_approval/blocked before insert), so scope must be identified via
    candidates.status, not trade_proposals.status. A WATCH/REJECTED
    candidate must never accumulate a vote even if a trade_proposals row
    happens to exist for it (the risk-blocked path does insert one, status
    'blocked')."""
    from alphaos.util.ids import new_id

    o = _orch(DEBATE_SHADOW_ENABLED="true")
    watch_cand = new_id("cand")
    o.journal.insert("candidates", {
        "candidate_id": watch_cand, "symbol": "MSFT", "direction": "long",
        "strategy": "swing", "status": "watch",
    })
    rejected_cand = new_id("cand")
    o.journal.insert("candidates", {
        "candidate_id": rejected_cand, "symbol": "TSLA", "direction": "long",
        "strategy": "swing", "status": "rejected",
    })
    # a risk-blocked trade_proposals row: status='blocked', paired with a
    # REJECTED candidate -- the exact ambiguous case that broke a naive
    # trade_proposals.status='proposed' filter (there is no such row) and
    # would ALSO break a naive "status != 'proposed'" or blanket
    # "status='blocked' means risk-blocked" filter (blocked is overloaded).
    from conftest import make_proposal

    blocked_prop = make_proposal(symbol="TSLA", candidate_id=rejected_cand)
    blocked_prop.status = "blocked"
    blocked_prop.scan_batch_id = "batch1"
    o.journal.insert("trade_proposals", blocked_prop.to_row())

    result = score_debate_batch(o.journal, o.settings, "batch1")
    assert result == {"voted": 0, "skipped": 0, "budget_exhausted": False}
    assert o.journal.count_rows("agent_votes") == 0
    o.close()


def test_only_batch_scoped_proposals_are_voted_on():
    o = _orch(DEBATE_SHADOW_ENABLED="true")
    pid1, _ = inject_pending_proposal(o, symbol="AAPL")
    _tag_batch(o.journal, pid1, "batch1")
    pid2, _ = inject_pending_proposal(o, symbol="MSFT")
    _tag_batch(o.journal, pid2, "batch2")  # a DIFFERENT batch

    result = score_debate_batch(o.journal, o.settings, "batch1")
    assert result["voted"] == 1
    assert o.journal.one("SELECT 1 FROM agent_votes WHERE proposal_id = ?", (pid1,))
    assert o.journal.one("SELECT 1 FROM agent_votes WHERE proposal_id = ?", (pid2,)) is None
    o.close()


def test_no_scan_batch_id_is_a_no_op():
    o = _orch(DEBATE_SHADOW_ENABLED="true")
    assert score_debate_batch(o.journal, o.settings, None) == {
        "voted": 0, "skipped": 0, "budget_exhausted": False,
    }
    o.close()


# ------------------------------------------------------ determinism/idempotency
def test_scoring_same_batch_twice_inserts_zero_new_rows():
    o = _orch(DEBATE_SHADOW_ENABLED="true")
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    _tag_batch(o.journal, pid, "batch1")
    result1 = score_debate_batch(o.journal, o.settings, "batch1")
    assert result1["voted"] == 1
    before = o.journal.count_rows("agent_votes")

    result2 = score_debate_batch(o.journal, o.settings, "batch1")
    after = o.journal.count_rows("agent_votes")
    assert after == before
    assert result2 == {"voted": 0, "skipped": 1, "budget_exhausted": False}
    o.close()


def test_agent_votes_unique_index_rejects_duplicate_proposal_role():
    import sqlite3

    j = JournalStore(":memory:")
    try:
        row = {
            "vote_id": "v1", "proposal_id": "p1", "candidate_id": "c1", "agent_role": "bear",
            "stance": "oppose", "conviction": 0.5, "is_mock": 1,
            "created_at_utc": "x", "created_at_sgt": "x",
        }
        j.insert("agent_votes", row)
        raised = False
        try:
            j.insert("agent_votes", {**row, "vote_id": "v2"})
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "duplicate (proposal_id, agent_role) row was NOT rejected"
    finally:
        j.close()


def test_bear_debater_mock_vote_is_deterministic_for_the_same_proposal():
    o = _orch()
    debater = BearDebater(o.settings, o.journal)
    candidate = {"candidate_id": "c1", "symbol": "AAPL"}
    proposal = {"proposal_id": "p1", "is_demo": False}
    v1 = debater.debate(candidate, proposal, "batch1")
    v2 = debater.debate(candidate, proposal, "batch1")
    assert v1.stance == v2.stance
    assert v1.conviction == v2.conviction
    o.close()


# ---------------------------------------------------------------------- lineage
def test_agent_votes_rows_carry_lineage_id():
    """Audit fix (both correctness NIT and scope/safety MEDIUM, independently
    flagged): every other AI-producing table in this codebase
    (openai_evaluations, claude_reviews, last30days_polarity, tqs_scores)
    stamps lineage_id -- agent_votes silently didn't. Mirrors
    test_tqs_flow.py's own test_tqs_scores_rows_carry_lineage_id."""
    o = _orch(DEBATE_SHADOW_ENABLED="true")
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    _tag_batch(o.journal, pid, "batch1")
    score_debate_batch(o.journal, o.settings, "batch1")
    row = o.journal.one("SELECT lineage_id FROM agent_votes WHERE proposal_id = ?", (pid,))
    assert row and row["lineage_id"]
    snap = o.journal.one(
        "SELECT 1 FROM lineage_snapshots WHERE lineage_id = ?", (row["lineage_id"],)
    )
    assert snap is not None
    o.close()


# ------------------------------------------------------- idempotent race handling
def test_concurrent_duplicate_vote_is_a_silent_no_op_not_a_warning():
    """Audit fix (correctness NIT): a genuine concurrent double-vote on the
    same (proposal_id, agent_role) -- e.g. _already_voted's own pre-check
    window raced -- must be a silent idempotent no-op (mirroring
    tqs/batch.py's _insert_result -> sqlite3.IntegrityError -> None), not a
    WARNING-level system event (which the broad except Exception fallback
    would have produced before this fix)."""
    from alphaos.debate.batch import vote_on_proposal

    o = _orch(DEBATE_SHADOW_ENABLED="true")
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    cand_row = o.journal.one(
        "SELECT * FROM candidates WHERE candidate_id = "
        "(SELECT candidate_id FROM trade_proposals WHERE proposal_id = ?)", (pid,),
    )
    prop_row = o.journal.one("SELECT * FROM trade_proposals WHERE proposal_id = ?", (pid,))
    debater = BearDebater(o.settings, o.journal)

    vote_on_proposal(o.journal, o.settings, debater, cand_row, prop_row, "batch1")
    assert o.journal.count_rows("agent_votes") == 1

    # bypass the _already_voted pre-check to force a genuine DB-level race
    import alphaos.debate.batch as batch_mod
    original = batch_mod._already_voted
    batch_mod._already_voted = lambda *a, **k: False
    try:
        result = vote_on_proposal(o.journal, o.settings, debater, cand_row, prop_row, "batch1")
    finally:
        batch_mod._already_voted = original

    assert result is None
    assert o.journal.count_rows("agent_votes") == 1  # still exactly one row
    warnings = o.journal.query(
        "SELECT * FROM system_events WHERE category = 'debate' AND severity = 'warning'"
    )
    assert warnings == []  # silent no-op, NOT logged as a warning
    o.close()


# --------------------------------------------------------------- cost budget
def test_daily_cap_stops_mid_batch_and_journals_the_shortfall():
    o = _orch(DEBATE_SHADOW_ENABLED="true", DEBATE_MAX_CALLS_PER_DAY="1")
    pid1, _ = inject_pending_proposal(o, symbol="AAPL")
    pid2, _ = inject_pending_proposal(o, symbol="MSFT")
    _tag_batch(o.journal, pid1, "batch1")
    _tag_batch(o.journal, pid2, "batch1")

    result = score_debate_batch(o.journal, o.settings, "batch1")
    assert result["voted"] == 1
    assert result["skipped"] == 1
    assert result["budget_exhausted"] is True
    assert o.journal.count_rows("agent_votes") == 1
    events = o.journal.query(
        "SELECT * FROM system_events WHERE category = 'debate' AND message LIKE '%exhausted mid-batch%'"
    )
    assert events
    o.close()


def test_daily_cap_already_reached_sits_out_the_whole_batch():
    """A mock vote (is_mock=1) never counts toward the daily cap (proven by
    test_mock_votes_never_count_against_either_cap), so "already reached" can
    only be simulated by seeding a REAL (is_mock=0) vote directly -- as if an
    earlier real-mode scan today already spent the day's one call."""
    from alphaos.util import timeutils

    o = _orch(DEBATE_SHADOW_ENABLED="true", DEBATE_MAX_CALLS_PER_DAY="1")
    o.journal.insert("agent_votes", {
        "vote_id": "v_seed", "proposal_id": "p_seed", "candidate_id": "c_seed",
        "agent_role": "bear", "stance": "oppose", "conviction": 0.8, "is_mock": 0,
        "created_at_utc": timeutils.to_iso(timeutils.now_utc()),
    })

    pid2, _ = inject_pending_proposal(o, symbol="MSFT")
    _tag_batch(o.journal, pid2, "batch2")
    r2 = score_debate_batch(o.journal, o.settings, "batch2")
    assert r2 == {"voted": 0, "skipped": 0, "budget_exhausted": True}
    o.close()


def test_shared_30day_cap_exhausted_sits_out_entirely():
    o = _orch(DEBATE_SHADOW_ENABLED="true", SCHEDULER_AI_COST_CAP_CALLS_PER_30D="50")
    from alphaos.util import timeutils
    from alphaos.util.ids import new_id

    # fill the SHARED cap via a different call site (openai_evaluations) --
    # proves the nested-cap layering (debate sits out even though ITS OWN
    # daily sub-cap has plenty of room).
    for _ in range(50):
        o.journal.insert("openai_evaluations", {
            "eval_id": new_id("eval"), "candidate_id": new_id("cand"), "symbol": "AAPL",
            "model": "real", "direction": "long", "decision": "reject", "is_mock": 0,
            "created_at_utc": timeutils.to_iso(timeutils.now_utc()),
        })
    pid, _ = inject_pending_proposal(o, symbol="MSFT")
    _tag_batch(o.journal, pid, "batch1")

    result = score_debate_batch(o.journal, o.settings, "batch1")
    assert result == {"voted": 0, "skipped": 0, "budget_exhausted": True}
    assert o.journal.count_rows("agent_votes") == 0
    o.close()


def test_debate_calls_today_and_check_debate_budget():
    o = _orch(DEBATE_MAX_CALLS_PER_DAY="2")
    assert debate_calls_today(o.journal) == 0
    ok, detail = check_debate_budget(o.settings, o.journal)
    assert ok is True
    assert "0/2" in detail
    o.close()


def test_mock_votes_never_count_against_either_cap():
    """Mock votes (is_mock=1) must not consume the daily sub-cap or the
    shared 30-day cap -- mirrors cost_guard's own is_mock=0 filter for every
    other call-site term."""
    o = _orch(DEBATE_SHADOW_ENABLED="true", DEBATE_MAX_CALLS_PER_DAY="1")
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    _tag_batch(o.journal, pid, "batch1")
    score_debate_batch(o.journal, o.settings, "batch1")
    assert debate_calls_today(o.journal) == 0  # the vote written was_mock=1
    from alphaos.scheduler.cost_guard import calls_in_last_30_days
    assert calls_in_last_30_days(o.journal) == 0
    o.close()


# -------------------------------------------------------------- fail-safe
def test_component_error_is_logged_as_system_event_and_does_not_raise():
    from unittest.mock import MagicMock

    o = _orch(DEBATE_SHADOW_ENABLED="true")
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    _tag_batch(o.journal, pid, "batch1")

    import alphaos.debate.batch as batch_mod
    original = batch_mod.BearDebater.debate
    batch_mod.BearDebater.debate = MagicMock(side_effect=RuntimeError("boom"))
    try:
        result = score_debate_batch(o.journal, o.settings, "batch1")  # must NOT raise
    finally:
        batch_mod.BearDebater.debate = original

    assert result["voted"] == 0
    assert result["skipped"] == 1
    events = o.journal.query(
        "SELECT * FROM system_events WHERE category = 'debate' AND severity = 'warning'"
    )
    assert events
    assert any("boom" in (e.get("detail_json") or "") for e in events)
    o.close()


def test_orphaned_proposal_with_no_matching_candidate_is_silently_excluded_not_fatal():
    """candidates rows are never deleted in this codebase (CandidateStatus's
    own docstring), so this is a defense-in-depth case, not a real-world
    path: the scope-defining JOIN itself requires a matching candidates row,
    so a proposal with none is excluded from consideration entirely (never
    reaches the loop's own defensive per-item candidate lookup) -- proven
    here to not crash and not vote, matching TQS's own belt-and-suspenders
    posture (score_proposal has an equivalent, similarly unreachable guard)."""
    o = _orch(DEBATE_SHADOW_ENABLED="true")
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    _tag_batch(o.journal, pid, "batch1")
    cand_id = o.journal.one("SELECT candidate_id FROM trade_proposals WHERE proposal_id = ?", (pid,))["candidate_id"]
    o.journal.conn.execute("DELETE FROM candidates WHERE candidate_id = ?", (cand_id,))
    o.journal.conn.commit()

    result = score_debate_batch(o.journal, o.settings, "batch1")  # must NOT raise
    assert result == {"voted": 0, "skipped": 0, "budget_exhausted": False}
    assert o.journal.count_rows("agent_votes") == 0
    o.close()


def test_demo_proposal_never_gets_a_vote():
    o = _orch(DEBATE_SHADOW_ENABLED="true")
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    o.journal.conn.execute("UPDATE trade_proposals SET is_demo = 1 WHERE proposal_id = ?", (pid,))
    _tag_batch(o.journal, pid, "batch1")
    result = score_debate_batch(o.journal, o.settings, "batch1")
    assert result == {"voted": 0, "skipped": 1, "budget_exhausted": False}
    o.close()


# ------------------------------------------------------------ no-read proof
def test_decision_functions_never_reference_debate():
    import inspect

    decision_functions = (
        "_handle_proposal", "_resolve_decision", "_combine_decision",
        "_real_decision_driver", "approve_proposal", "reject_proposal",
        "_label_candidate", "_freeze_label", "run_scan_once",
    )
    for fn_name in decision_functions:
        fn = getattr(Orchestrator, fn_name)
        source = inspect.getsource(fn)
        if fn_name == "run_scan_once":
            marker = "# PR14: Red-Team Debate v0 shadow bear-agent voting."
            assert marker in source, "expected PR14 call site marker not found in run_scan_once"
            source = source.split(marker)[0]
        assert "debate" not in source.lower(), f"Orchestrator.{fn_name} references debate"


def test_risk_engine_and_approval_never_reference_debate_at_all():
    import alphaos.approval as approval_mod
    import alphaos.risk.risk_engine as risk_mod

    for mod, name in ((approval_mod, "approval.py"), (risk_mod, "risk_engine.py")):
        text = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert "debate" not in text.lower(), f"{name} references debate"


def test_no_orders_approvals_fills_positions_created_by_debate_code():
    debate_dir = pathlib.Path(debate_pkg.__file__).parent
    banned = ("execute_proposal", "approve_proposal", "close_position",
              "submit_bracket", "submit_order", "place_order")
    for py_file in debate_dir.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{py_file.name} references {token!r}"


# ------------------------------------------------------------- safety invariants
def test_debate_creates_no_orders_approvals_fills_positions():
    o = _orch(DEBATE_SHADOW_ENABLED="true")
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    _tag_batch(o.journal, pid, "batch1")
    score_debate_batch(o.journal, o.settings, "batch1")
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_rows("paper_fills") == 0
    assert o.journal.count_open_positions() == 0
    o.close()


def test_debate_toggle_does_not_change_decision_artifacts():
    """Mirrors test_tqs_flow.py's own behavior-neutrality A/B test: with
    DEBATE_SHADOW_ENABLED on vs off, a normal scan's own decision artifacts
    are byte-identical -- proving the toggle cannot influence what a real
    scan decides, only whether a shadow vote gets written alongside it."""
    def _fingerprint(journal):
        return [dict(r) for r in journal.query(
            "SELECT symbol, direction, entry, stop, target, qty, status, expected_r "
            "FROM trade_proposals ORDER BY symbol, entry"
        )]

    base = {"INTEREST_SCAN_TOP_N": "12", "MAX_CANDIDATES_TO_AI": "12", "LABELLING_ENABLED": "true"}
    off = _orch(DEBATE_SHADOW_ENABLED="false", **base)
    off.run_scan_once()
    fp_off = _fingerprint(off.journal)
    off.close()

    on = _orch(DEBATE_SHADOW_ENABLED="true", **base)
    on.run_scan_once()
    fp_on = _fingerprint(on.journal)
    on.close()

    assert fp_on == fp_off


# ----------------------------------------------------------- schema/migration
def test_old_db_gets_agent_votes_table_added_additively(tmp_path):
    import sqlite3

    db = str(tmp_path / "pre_pr14.db")
    raw = sqlite3.connect(db)
    raw.execute(
        "CREATE TABLE candidates (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "candidate_id TEXT, symbol TEXT, status TEXT)"
    )
    raw.execute("PRAGMA user_version = 0")
    raw.commit()
    raw.close()

    j = JournalStore(db)
    try:
        tables = {r["name"] for r in j.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "agent_votes" in tables
        cols = {r["name"] for r in j.conn.execute("PRAGMA table_info(agent_votes)")}
        for c in ("vote_id", "proposal_id", "candidate_id", "agent_role", "stance",
                  "conviction", "is_mock", "model_provider", "prompt_hash"):
            assert c in cols, f"missing column {c}"
    finally:
        j.close()


# --------------------------------------------------------------------- CLI
def test_cmd_debate_register_is_idempotent():
    from alphaos.__main__ import cmd_debate_register

    o = _orch()
    assert cmd_debate_register(o) == 0
    assert o.journal.count_rows("preregistrations") == 1
    assert cmd_debate_register(o) == 0  # second call: no-op, not a second row
    assert o.journal.count_rows("preregistrations") == 1
    row = o.journal.one("SELECT * FROM preregistrations LIMIT 1")
    assert row["floor_effective_n"] == 30
    assert row["floor_span_days"] == 28.0
    o.close()
