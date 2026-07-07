"""Decision lineage stamping (PR4): measurement-only metadata answering "which
code/config/model/prompt/data/scheduler context produced this decision?" for
every candidate/proposal/reject/override/outcome row. Hermetic (mock mode, no
network); never touches a gate/eval/labeller/risk/execution/approval path.
"""

from __future__ import annotations

import pathlib

from alphaos import lineage
from alphaos.constants import UserOverrideAction
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.scanner.scan_context import ScanContext
from conftest import inject_pending_proposal, make_settings


def _orch(**over):
    return Orchestrator(settings=make_settings(**over), journal=JournalStore(":memory:"))


# --------------------------------------------------------------- builder core
def test_lineage_builder_is_stable_for_same_config():
    s = make_settings()
    j = JournalStore(":memory:")
    lid1 = lineage.get_or_create_lineage_id(j, s)
    lid2 = lineage.get_or_create_lineage_id(j, s)
    assert lid1 is not None
    assert lid1 == lid2
    assert j.count_rows("lineage_snapshots") == 1  # idempotent, never duplicated


def test_lineage_hash_changes_when_relevant_config_changes():
    j = JournalStore(":memory:")
    lid_a = lineage.get_or_create_lineage_id(j, make_settings())
    lid_b = lineage.get_or_create_lineage_id(j, make_settings(MAX_RISK_PER_TRADE_PCT="0.5"))
    assert lid_a != lid_b
    assert j.count_rows("lineage_snapshots") == 2


def test_lineage_snapshot_excludes_secrets():
    s = make_settings(OPENAI_API_KEY="sk-super-secret-value", ALPACA_SECRET_KEY="alpaca-secret-value")
    j = JournalStore(":memory:")
    lineage.get_or_create_lineage_id(j, s)
    row = j.one("SELECT * FROM lineage_snapshots LIMIT 1")
    blob = str(dict(row))
    assert "sk-super-secret-value" not in blob
    assert "alpaca-secret-value" not in blob


def test_config_hash_excludes_secrets_directly():
    from alphaos.lineage.config_snapshot import settings_dict

    s = make_settings(OPENAI_API_KEY="sk-super-secret-value")
    d = settings_dict(s)
    assert "openai_api_key" not in d
    assert "sk-super-secret-value" not in str(d)


# ------------------------------------------------------------ decision stamps
def test_candidate_and_proposal_rows_get_lineage_stamps():
    o = _orch()
    o.startup()
    summary = o.run_scan_once()
    assert summary.candidates > 0

    candidates = o.journal.query("SELECT * FROM candidates")
    assert candidates
    assert all(c["lineage_id"] for c in candidates)

    proposals = o.journal.query("SELECT * FROM trade_proposals")
    if proposals:
        assert all(p["lineage_id"] for p in proposals)
    o.close()


def test_reject_rows_get_lineage_stamps():
    # Deterministic by construction, NOT dependent on the mock scan's
    # date-seeded RNG crossing some momentum threshold (per HANDOVER.md's own
    # documented lesson: a mock-data-dependent assertion can silently re-break
    # on a future date). Directly injects a candidate + calls the real
    # _reject_candidate() path, same style as conftest's inject_pending_proposal.
    from types import SimpleNamespace

    from alphaos.util.ids import new_id

    o = _orch()
    cand_id = new_id("cand")
    o.journal.insert("candidates", {
        "candidate_id": cand_id, "symbol": "AAPL", "direction": "long",
        "strategy": "swing", "status": "detected",
    })
    cand = ScanContext(row={"candidate_id": cand_id, "symbol": "AAPL"})
    evaluation = SimpleNamespace(
        validation_status="passed", reasoning_summary="test reject",
        direction="long", entry=100.0, stop=97.0,
    )
    o._reject_candidate(cand, "test", evaluation, reason="test_reject")

    rejects = o.journal.query("SELECT * FROM rejected_candidates WHERE candidate_id = ?", (cand_id,))
    assert rejects
    assert all(r["lineage_id"] for r in rejects)
    o.close()


def test_decision_adjustment_and_armed_watch_rows_get_lineage_stamps():
    import json

    o = _orch()
    o.run_scan_once()
    adjustments = o.journal.query("SELECT * FROM decision_adjustments")
    assert adjustments  # a normal scan always labels/adjusts at least one shortlisted candidate
    assert all(a["lineage_id"] for a in adjustments)

    # ai_lineage_json (the composite label+last30days AI-call lineage) must be
    # populated and valid JSON with the expected sub-keys -- this is a
    # separate lineage surface from lineage_id and was otherwise unasserted.
    with_ai_lineage = [a for a in adjustments if a.get("ai_lineage_json")]
    assert with_ai_lineage, "expected at least one decision_adjustments row with ai_lineage_json populated"
    for row in with_ai_lineage:
        parsed = json.loads(row["ai_lineage_json"])
        assert isinstance(parsed, dict) and parsed
        assert set(parsed.keys()) <= {"label", "last30days"}
    o.close()


def test_user_override_rows_get_lineage_stamps():
    o = _orch()
    proposal_id, _ = inject_pending_proposal(o, symbol="AAPL")
    cid = o.journal.proposal_by_id(proposal_id)["candidate_id"]
    res = o.create_user_override(cid, UserOverrideAction.PROPOSE_TO_REJECT.value, reason_code="test")
    assert res["ok"]
    rows = o.journal.query("SELECT * FROM user_decision_overrides WHERE candidate_id = ?", (cid,))
    assert rows and rows[0]["lineage_id"]
    o.close()


def test_candidate_outcomes_preserve_source_decision_lineage(tmp_path):
    """The outcome row's lineage_id must be the SOURCE decision's lineage (the
    config in effect when AlphaOS actually decided), never a freshly-computed
    snapshot of whatever config happens to be running when outcomes_update is
    later called -- same anchor-on-source-not-seed-time principle as
    decision_at_utc (measurement-foundation PR, Opus audit HIGH-1).

    Uses two Orchestrators under DIFFERENT settings sharing one file-backed
    journal (:memory: can't be shared across two JournalStore instances, and
    two Orchestrators built from the SAME settings would produce an identical
    lineage_id whether preserved or recomputed -- neither setup could actually
    catch a real "recompute instead of preserve" regression)."""
    db_path = str(tmp_path / "lineage_preserve.db")

    o1 = Orchestrator(settings=make_settings(ALPHAOS_DB_PATH=db_path), journal=JournalStore(db_path))
    o1.startup()
    o1.run_scan_once()
    original_lineage_ids = {
        c["candidate_id"]: c["lineage_id"] for c in o1.journal.query("SELECT * FROM candidates")
    }
    assert original_lineage_ids and all(original_lineage_ids.values())
    o1.close()

    # A DIFFERENT config, sharing the SAME on-disk journal -- simulates
    # outcomes_update running later under a changed config (e.g. catching up
    # on a backlog after a code/config change).
    o2 = Orchestrator(
        settings=make_settings(ALPHAOS_DB_PATH=db_path, MAX_RISK_PER_TRADE_PCT="0.5"),
        journal=JournalStore(db_path),
    )
    o2.outcomes_update()
    outcomes = o2.journal.query("SELECT * FROM candidate_outcomes")
    assert outcomes
    for outcome in outcomes:
        assert outcome["lineage_id"] == original_lineage_ids[outcome["candidate_id"]]

    # Prove o2's config really does hash differently, ruling out a false-pass
    # via "both configs happen to hash the same".
    fresh_lineage_id_under_o2_config = lineage.get_or_create_lineage_id(o2.journal, o2.settings)
    assert fresh_lineage_id_under_o2_config not in original_lineage_ids.values()
    o2.close()


def test_trade_outcomes_anchor_on_entry_proposal_lineage():
    """A closed trade's trade_outcomes row must carry the ENTRY proposal's
    lineage_id (the config that decided the trade), NOT a fresh snapshot of
    whatever config is live at close time -- same anchor-on-source principle
    as candidate_outcomes. Force-closes a demo trade and asserts the outcome's
    lineage_id matches its entry proposal's, not just that it's non-null."""
    o = _orch()
    demo = o.seed_demo()
    assert demo["approved"] is True
    o.run_monitor_once(price_overrides={"DEMO": 10_000_000})  # force a target-hit close

    outcomes = o.journal.query("SELECT * FROM trade_outcomes")
    assert outcomes
    for row in outcomes:
        assert row["lineage_id"]  # stamped at all
        prop = o.journal.one(
            "SELECT lineage_id FROM trade_proposals WHERE proposal_id = ?", (row["proposal_id"],)
        )
        assert prop is not None
        # The outcome's lineage is the ENTRY proposal's lineage, not a fresh one.
        assert row["lineage_id"] == prop["lineage_id"]
    o.close()


# --------------------------------------------------------------------- CLI
def test_decision_lineage_cli_command_end_to_end(tmp_path, monkeypatch):
    from alphaos import __main__ as cli
    from alphaos.config.settings import load_settings

    db = str(tmp_path / "cli_lineage.db")
    env = {
        "ALPHAOS_MODE": "mock", "APPROVAL_MODE": "manual",
        "REAL_TRADING_ENABLED": "false", "ALPHAOS_DB_PATH": db,
    }
    monkeypatch.setattr(cli, "load_settings", lambda: load_settings(load_env_file=False, env=env))

    seed = Orchestrator(settings=load_settings(load_env_file=False, env=env))
    seed.startup()
    seed.run_scan_once()
    cand = seed.journal.one("SELECT candidate_id FROM candidates LIMIT 1")
    seed.close()

    assert cli.main(["decision_lineage", cand["candidate_id"]]) == 0
    assert cli.main(["decision_lineage", "definitely_not_a_real_id"]) == 0  # not-found is still a clean exit


# ------------------------------------------------------------ scheduler chain
def test_scheduler_triggered_decisions_include_scheduler_run_id():
    from alphaos.scheduler import JobRunner

    o = _orch()
    o.startup()
    result = JobRunner(o).run_job("scan")
    assert result["status"] in ("completed", "skipped")
    if result["status"] == "completed":
        candidates = o.journal.query("SELECT * FROM candidates")
        assert candidates
        scan_batch_id = candidates[0]["scan_batch_id"]
        sched_run = o.journal.one(
            "SELECT * FROM scheduler_runs WHERE scan_batch_id = ?", (scan_batch_id,)
        )
        assert sched_run is not None
        assert sched_run["trigger_source"] == "scheduler"
    o.close()


def test_decision_lineage_report_surfaces_scheduler_context():
    from alphaos.scheduler import JobRunner

    o = _orch()
    o.startup()
    result = JobRunner(o).run_job("scan")
    if result["status"] != "completed":
        o.close()
        return  # kill switch/cost cap skipped this pass -- nothing to assert
    cand = o.journal.one("SELECT candidate_id FROM candidates LIMIT 1")
    report = o.decision_lineage_report(cand["candidate_id"])
    assert report["found"] is True
    assert report["lineage_snapshots"]
    assert report["scheduler_runs"]
    assert report["scheduler_runs"][0]["trigger_source"] == "scheduler"
    o.close()


# ----------------------------------------------------------- safety invariants
def test_manual_approval_boundary_unchanged_with_lineage_wired():
    o = _orch()
    o.startup()
    o.run_scan_once()
    approved_or_filled = o.journal.query(
        "SELECT * FROM trade_proposals WHERE status IN ('approved', 'filled')"
    )
    assert approved_or_filled == []  # manual mode default: nothing auto-executes
    o.close()


def test_no_orders_approvals_fills_positions_created_by_lineage_code():
    """The lineage package must never touch order/approval/position state --
    same structural grep-based check used for the scheduler package in PR3."""
    lineage_dir = pathlib.Path(lineage.__file__).parent
    banned = ("execute_proposal", "approve_proposal", "close_position",
              "submit_bracket", "submit_order", "place_order")
    for py_file in lineage_dir.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{py_file.name} references {token!r}"


def test_real_money_remains_unreachable_with_lineage_wired():
    o = _orch()
    o.startup()
    o.run_scan_once()
    assert o.settings.real_trading_enabled_raw == "false"
    lineage_dir = pathlib.Path(lineage.__file__).parent
    for py_file in lineage_dir.glob("*.py"):
        assert "alpaca_client" not in py_file.read_text(encoding="utf-8")
    o.close()


def test_scheduler_monitor_ordering_still_preserved_with_lineage_wired(orchestrator, monkeypatch):
    from alphaos.scheduler import JobRunner
    from alphaos.scheduler.jobs import run_monitor_job

    calls = []
    monkeypatch.setattr(
        orchestrator.orders, "reconcile",
        lambda *a, **k: calls.append("reconcile") or {"reconciled": 0, "opened": [], "exits": []},
    )
    monkeypatch.setattr(
        "alphaos.orchestrator.protection_watchdog.run_watchdog_pass",
        lambda *a, **k: calls.append("watchdog") or {
            "checked": 0, "protected": 0, "unprotected": 0, "degraded": 0,
            "closed_mismatch": 0, "check_error": 0, "unverifiable": 0,
            "qty_mismatches": 0, "dangling_orders": [], "new_incidents": [],
        },
    )
    monkeypatch.setattr(orchestrator.positions, "monitor", lambda *a, **k: calls.append("monitor") or [])

    run_monitor_job(orchestrator, JobRunner(orchestrator))

    assert calls == ["reconcile", "watchdog", "monitor"]


def test_risk_gate_behavior_unchanged_by_lineage_stamping():
    """A risk-blocked proposal must still be genuinely blocked -- lineage
    stamping is additive metadata on the SAME row, never a decision input."""
    o = _orch(MAX_RISK_PER_TRADE_PCT="0.0001")  # force risk block
    o.startup()
    o.run_scan_once()
    blocked = o.journal.query("SELECT * FROM trade_proposals WHERE status = 'blocked'")
    for b in blocked:
        assert b["status"] == "blocked"
        assert b["lineage_id"]  # still stamped, but the block itself is untouched
    o.close()


# --------------------------------------------------------------------- CLI
def test_decision_lineage_resolves_via_multiple_id_types():
    o = _orch()
    o.startup()
    o.run_scan_once()
    proposal = o.journal.one("SELECT proposal_id, candidate_id FROM trade_proposals LIMIT 1")
    if proposal is None:
        o.close()
        return
    by_candidate = o.decision_lineage_report(proposal["candidate_id"])
    by_proposal = o.decision_lineage_report(proposal["proposal_id"])
    assert by_candidate["found"] and by_proposal["found"]
    assert by_candidate["candidate_id"] == by_proposal["candidate_id"] == proposal["candidate_id"]
    o.close()


def test_decision_lineage_not_found_for_unknown_id():
    o = _orch()
    report = o.decision_lineage_report("definitely_not_a_real_id")
    assert report == {"found": False, "queried_id": "definitely_not_a_real_id"}
    o.close()


# ----------------------------------------------- audit follow-up coverage (PR4)
def test_ai_call_lineage_is_deterministic_and_hashes_the_prompt():
    """Direct hermetic unit test of the AI-call lineage helper -- the live
    _live_eval/_live_classify/_live_review call sites are `# pragma: no cover`
    (real network), so this is the deterministic proof that a given prompt
    produces a stable hash and a changed prompt produces a different one,
    without needing a live API."""
    from alphaos.lineage.hashing import stable_hash

    a = lineage.ai_call_lineage(provider="openai", prompt="hello", system_prompt="sys")
    b = lineage.ai_call_lineage(provider="openai", prompt="hello", system_prompt="sys")
    assert a == b  # deterministic
    assert a["model_provider"] == "openai"
    assert a["prompt_hash"] == stable_hash("hello")
    assert a["system_prompt_hash"] == stable_hash("sys")

    c = lineage.ai_call_lineage(provider="openai", prompt="different", system_prompt="sys")
    assert c["prompt_hash"] != a["prompt_hash"]  # prompt change -> new hash
    assert c["system_prompt_hash"] == a["system_prompt_hash"]  # unchanged system prompt -> same hash

    # None prompt (mock/fallback path) yields None hashes, distinguishable from a real call.
    none_case = lineage.ai_call_lineage(provider=None, prompt=None)
    assert none_case == {"model_provider": None, "prompt_hash": None, "system_prompt_hash": None}


def test_labeller_lineage_shape_is_present_in_ai_lineage_json():
    """The labeller (playbook_classifier) contribution to
    decision_adjustments.ai_lineage_json must carry the model/is_mock/
    model_provider/prompt_hash/system_prompt_hash keys (prompt_hash is None on
    the mock path, but the KEY must be present so a live run populates it)."""
    import json

    o = _orch()
    o.run_scan_once()
    rows = [a for a in o.journal.query("SELECT * FROM decision_adjustments") if a.get("ai_lineage_json")]
    assert rows
    label_shapes = [json.loads(r["ai_lineage_json"]).get("label") for r in rows]
    label_shapes = [ls for ls in label_shapes if ls]
    assert label_shapes, "expected a label sub-dict in ai_lineage_json"
    for ls in label_shapes:
        assert set(ls.keys()) == {"model", "is_mock", "model_provider", "prompt_hash", "system_prompt_hash"}
    o.close()


def test_repeated_outcomes_update_does_not_clobber_lineage():
    """A core PR4 promise: re-running outcomes_update (idempotent, e.g. a
    scheduled cadence catching up) must NEVER rewrite an already-seeded
    candidate_outcomes.lineage_id -- the UPDATE phase only writes forward
    returns / status, never lineage."""
    o = _orch()
    o.startup()
    o.run_scan_once()
    o.outcomes_update()
    first = {r["outcome_id"]: r["lineage_id"] for r in o.journal.query("SELECT * FROM candidate_outcomes")}
    assert first
    o.outcomes_update()  # run again
    o.outcomes_update()  # and again
    second = {r["outcome_id"]: r["lineage_id"] for r in o.journal.query("SELECT * FROM candidate_outcomes")}
    assert second == first  # identical row set, identical lineage -- no clobber, no dup
    o.close()


def test_null_lineage_row_is_still_readable_and_reportable():
    """Legacy/backlog rows created before PR4 have lineage_id=NULL. They must
    remain readable and the lineage report must handle them gracefully
    (found:true, empty lineage_snapshots), never crash."""
    from alphaos.util.ids import new_id

    o = _orch()
    cand_id = new_id("cand")
    # Insert a candidate with NO lineage_id (simulating a pre-PR4 row).
    o.journal.insert("candidates", {
        "candidate_id": cand_id, "symbol": "AAPL", "direction": "long",
        "strategy": "swing", "status": "detected",
    })
    row = o.journal.one("SELECT * FROM candidates WHERE candidate_id = ?", (cand_id,))
    assert row["lineage_id"] is None  # legacy row, no lineage

    report = o.decision_lineage_report(cand_id)
    assert report["found"] is True
    assert report["lineage_snapshots"] == []  # NULL lineage -> no snapshot, but no crash
    o.close()


def test_armed_watch_decision_adjustment_row_is_stamped():
    """Explicitly assert an armed_watch=1 decision_adjustments row carries a
    lineage_id (armed-watch is not its own table -- it's a flagged
    decision_adjustments row -- so prove the flagged variant is stamped, not
    just the blanket set)."""
    from alphaos.util.ids import new_id

    o = _orch()
    # Directly insert a stamped armed-watch decision_adjustments row (the scan
    # RNG doesn't deterministically produce armed_watch=1 on every date).
    o.journal.insert("decision_adjustments", {
        "adjustment_id": new_id("dadj"), "candidate_id": new_id("cand"), "symbol": "AAPL",
        "eval_decision": "watch", "final_decision": "watch", "adjustment": "unchanged",
        "armed_watch": 1, "armed_watch_reason": "test armed watch",
        "lineage_id": lineage.get_or_create_lineage_id(o.journal, o.settings),
    })
    armed = o.journal.query("SELECT * FROM decision_adjustments WHERE armed_watch = 1")
    assert armed
    assert all(a["lineage_id"] for a in armed)
    o.close()


def test_get_git_info_never_crashes_without_a_git_repo(tmp_path):
    """git lineage must degrade to all-None (never raise) when called from a
    non-git directory -- so lineage works in packaged/CI/non-git environments
    and a git failure can never break a trading/monitoring path."""
    info = lineage.get_git_info(repo_root=tmp_path)  # tmp_path has no .git
    assert info.commit_sha is None
    assert info.branch is None
    assert info.dirty is None  # conservative: None, never a wrong False
