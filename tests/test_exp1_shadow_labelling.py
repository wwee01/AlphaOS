"""EXP-1: shadow-tier AI labelling.

Covers (mechanism-by-mechanism):
* selection: top-K + explore, deterministic seed, dedup/backfill, fewer-than-K
  labels all, zero labels none (mechanism 2),
* the shadow AI-cost sub-cap pre-flight: whole-window refusal with zero
  client invocations, core untouched (mechanism 7),
* the feed-coverage arming gate + per-packet stale skip (mechanism 8),
* operator-surface exclusion -- the single biggest named build-session risk
  (mechanism 9): the scan-loop guard can't be bypassed by the new loop
  (behavior probe), approve_proposal refuses a forced shadow_tier=1
  proposal, recent_candidates/digest/brief never name a shadow symbol,
  relabel/eval_corpus_build default-exclude shadow,
* idempotent rerun spends zero extra calls (mechanism 4),
* kill switch -> zero shadow calls; auto-suspend triggers (mechanism 13),
* the degrade-law fix: a global-cap breach keeps deterministic capture alive
  (mechanism 13),
* additive-only schema migration on an old DB.

All offline, in-memory, mock mode. No real money, no network.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from datetime import timedelta

from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.safety import KillSwitch, ShadowLabelSuspendSwitch
from alphaos.scheduler import shadow_label
from alphaos.scheduler.digest import build_daily_digest
from alphaos.scheduler.job_runner import JobRunner
from alphaos.util import timeutils
from alphaos.util.ids import new_id
from conftest import make_settings


# --------------------------------------------------------------------- fakes
def _universe_doc(symbols, version=1):
    return {
        "version": version, "as_of_date": "2026-07-01", "sha256": "test",
        "screen_params": {}, "symbols": symbols,
    }


def _orch_with_shadow_universe(tmp_path, symbols, **extra_env):
    path = str(tmp_path / "shadow_universe.json")
    with open(path, "w") as f:
        json.dump(_universe_doc(symbols), f)
    env = {
        "SHADOW_TIER_ENABLED": "true", "SHADOW_TIER_UNIVERSE_FILE": path,
        "SHADOW_LABELLING_ENABLED": "true", "LABELLING_ENABLED": "true",
    }
    env.update(extra_env)
    settings = make_settings(**env)
    journal = JournalStore(":memory:")
    return Orchestrator(settings=settings, journal=journal), path


def _seed_symbols(n, prefix="ZZ"):
    return [{"symbol": f"{prefix}{i}", "adv_20d_usd": 10_000_000.0, "exchange": "NYSE"} for i in range(n)]


def _days_ago(n: int) -> str:
    """§H.1 law: never hardcode a calendar date -- every date-dependent
    fixture here is relative to the real market_date() at test-run time, so
    a trailing-N-day window (feed coverage / auto-suspend) always sees
    exactly the fixture rows it's meant to, regardless of what day the
    suite happens to run on."""
    return (timeutils.market_date() - timedelta(days=n)).isoformat()


def _seed_universe_days(journal, market_date, n_scanned=10, n_fresh=10):
    """Directly seed universe_days rows (bypassing a real scan) so the feed-
    coverage gate has trailing history to compute a median from -- mirrors
    this codebase's own §H.1 direct-construction-throughout law."""
    for i in range(n_scanned):
        journal.insert("universe_days", {
            "universe_day_id": new_id("univday"),
            "market_date": market_date,
            "symbol": f"SEED{i}",
            "tier": "watchlist",
            "freshness_status": "usable" if i < n_fresh else "stale",
            "candidate_found": 0,
        })


# ---------------------------------------------------------- selection (mech 2)
def test_select_shadow_shortlist_top_k_plus_explore_deterministic():
    candidates = [
        {"symbol": f"S{i}", "candidate_id": f"c{i}", "interest_score": 1.0 - i * 0.05, "unusual_volume": 1.0}
        for i in range(10)
    ]
    settings = make_settings(SHADOW_LABEL_TOP_K="3", SHADOW_EXPLORE_FRACTION="0.2")
    sel1 = shadow_label.select_shadow_shortlist(
        [dict(c) for c in candidates], settings, "2026-07-15", "09:35-09:50",
    )
    sel2 = shadow_label.select_shadow_shortlist(
        [dict(c) for c in candidates], settings, "2026-07-15", "09:35-09:50",
    )
    assert len(sel1) == 4  # K=3 + max(1, round(0.2*3))=1
    assert {c["symbol"] for c in sel1} == {c["symbol"] for c in sel2}  # deterministic across reruns
    top_k_symbols = {c["symbol"] for c in sel1 if c["selection_arm"] == "top_k"}
    assert top_k_symbols == {"S0", "S1", "S2"}  # literal top-3 by interest_score
    explore_symbols = {c["symbol"] for c in sel1 if c["selection_arm"] == "explore"}
    assert explore_symbols and explore_symbols.isdisjoint(top_k_symbols)


def test_select_shadow_shortlist_deterministic_across_pythonhashseed(monkeypatch):
    """Same seed_str -> same explore draw regardless of PYTHONHASHSEED (the
    seed is sha256-derived, never Python's built-in hash())."""
    candidates = [
        {"symbol": f"S{i}", "candidate_id": f"c{i}", "interest_score": 1.0 - i * 0.01, "unusual_volume": 1.0}
        for i in range(20)
    ]
    settings = make_settings(SHADOW_LABEL_TOP_K="3", SHADOW_EXPLORE_FRACTION="0.2")
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    sel_a = shadow_label.select_shadow_shortlist([dict(c) for c in candidates], settings, "2026-07-15", "w1")
    monkeypatch.setenv("PYTHONHASHSEED", "12345")
    sel_b = shadow_label.select_shadow_shortlist([dict(c) for c in candidates], settings, "2026-07-15", "w1")
    assert {c["symbol"] for c in sel_a} == {c["symbol"] for c in sel_b}


def test_select_shadow_shortlist_fewer_than_k_selects_all():
    candidates = [{"symbol": "ONLY", "candidate_id": "c1", "interest_score": 0.5, "unusual_volume": 1.0}]
    settings = make_settings(SHADOW_LABEL_TOP_K="5")
    sel = shadow_label.select_shadow_shortlist(candidates, settings, "2026-07-15", "w1")
    assert len(sel) == 1
    assert sel[0]["selection_arm"] == "top_k"


def test_select_shadow_shortlist_zero_candidates_zero_calls():
    settings = make_settings(SHADOW_LABEL_TOP_K="5")
    assert shadow_label.select_shadow_shortlist([], settings, "2026-07-15", "w1") == []


def test_fetch_shadow_selection_pool_dedups_already_labelled_today(tmp_path):
    """Mechanism 2's own dedup law: a symbol already labelled today (any
    window) is excluded from the pool, so a persistent name is never
    triple-paid across the day's 3 windows."""
    orch, _ = _orch_with_shadow_universe(tmp_path, _seed_symbols(4))
    orch.run_scan_once()
    market_date = timeutils.market_date().isoformat()

    pool_before = shadow_label.fetch_shadow_selection_pool(orch.journal, market_date)
    assert len(pool_before) == 4

    # Label ONE symbol directly (simulating a prior window's labelling).
    row = pool_before[0]
    from alphaos.scanner.scan_context import ScanContext

    ctx = ScanContext(row=dict(row))
    ctx.snapshot = orch.market.get_snapshot(row["symbol"])
    classification = orch._label_candidate(ctx, ctx.snapshot, None, enrich=False, l30_mode=None, earnings_mode=None)
    orch.journal.conn.execute(
        "UPDATE candidate_labels SET shadow_tier = 1 WHERE label_id = ?", (classification.label_id,),
    )
    orch.journal.conn.commit()

    pool_after = shadow_label.fetch_shadow_selection_pool(orch.journal, market_date)
    assert row["symbol"] not in {r["symbol"] for r in pool_after}
    assert len(pool_after) == 3
    orch.journal.close()


# -------------------------------------------------------- budget (mechanism 7)
def test_check_shadow_budget_subcap_breach_refuses_whole_window_zero_calls(journal):
    settings = make_settings(SHADOW_AI_CAP_CALLS_PER_30D="5")
    for _ in range(5):
        journal.insert("candidate_labels", {
            "label_id": new_id("lbl"),
            "candidate_id": "c1", "symbol": "ZZ1", "shadow_tier": 1, "is_mock": 0,
            "label_decision": "watch",
        })
    within, detail = shadow_label.check_shadow_budget(settings, journal, planned_calls=1)
    assert within is False
    assert "sub-cap" in detail


def test_check_shadow_budget_daily_cap_breach_refuses(journal):
    settings = make_settings(SHADOW_AI_CAP_CALLS_PER_DAY="2")
    for _ in range(2):
        journal.insert("candidate_labels", {
            "label_id": new_id("lbl"),
            "candidate_id": "c1", "symbol": "ZZ1", "shadow_tier": 1, "is_mock": 0,
            "label_decision": "watch",
        })
    within, detail = shadow_label.check_shadow_budget(settings, journal, planned_calls=1)
    assert within is False
    assert "daily cap" in detail


def test_check_shadow_budget_global_cap_breach_refuses(journal):
    """Mechanism 7(c): a would-breach of the GLOBAL 30-day cap also refuses
    the shadow window, even if the shadow sub-cap itself has room."""
    settings = make_settings(SCHEDULER_AI_COST_CAP_CALLS_PER_30D="50", SHADOW_AI_CAP_CALLS_PER_30D="12")
    for _ in range(50):
        journal.insert("openai_evaluations", {
            "eval_id": new_id("eval"),
            "candidate_id": "c1", "symbol": "AAPL", "model": "mock", "direction": "long",
            "entry": 100.0, "stop": 97.0, "target": 106.0, "max_holding_days": 3,
            "expected_r": 2.0, "confidence": 0.8, "decision": "propose", "reasoning_summary": "t",
            "is_mock": 0,
        })
    within, detail = shadow_label.check_shadow_budget(settings, journal, planned_calls=1)
    assert within is False
    assert "global" in detail


def test_shadow_budget_breach_skips_shadow_core_untouched(tmp_path):
    """Mechanism 7's own acceptance test: sub-cap breach -> shadow skipped
    with ZERO calls, core labelling (if any ran this scan) is untouched."""
    orch, _ = _orch_with_shadow_universe(tmp_path, _seed_symbols(4), SHADOW_AI_CAP_CALLS_PER_30D="0")
    orch.run_scan_once()
    _seed_universe_days(orch.journal, timeutils.market_date().isoformat())
    core_labels_before = orch.journal.count_rows("candidate_labels", "shadow_tier = 0")

    result = shadow_label.run_shadow_label(orch)

    assert result["status"] == "skipped"
    assert result["shadow_calls"] == 0
    assert orch.journal.count_rows("candidate_labels", "shadow_tier = 1") == 0
    assert orch.journal.count_rows("candidate_labels", "shadow_tier = 0") == core_labels_before
    orch.journal.close()


# --------------------------------------------------------- feed coverage (mech 8)
def test_feed_coverage_gate_blocks_below_threshold(journal):
    settings = make_settings(SHADOW_LABEL_MIN_FEED_COVERAGE="0.80")
    _seed_universe_days(journal, _days_ago(1), n_scanned=10, n_fresh=3)  # 0.30 coverage
    ok, detail = shadow_label.check_feed_coverage_gate(journal, settings)
    assert ok is False
    assert "0.3" in detail or "coverage" in detail


def test_feed_coverage_gate_passes_above_threshold(journal):
    settings = make_settings(SHADOW_LABEL_MIN_FEED_COVERAGE="0.80")
    _seed_universe_days(journal, _days_ago(1), n_scanned=10, n_fresh=9)  # 0.90 coverage
    ok, _ = shadow_label.check_feed_coverage_gate(journal, settings)
    assert ok is True


def test_feed_coverage_gate_no_history_refuses(journal):
    settings = make_settings()
    ok, detail = shadow_label.check_feed_coverage_gate(journal, settings)
    assert ok is False
    assert "no universe_days history" in detail


def test_stale_snapshot_skips_no_api_call_stamps_reason(tmp_path, monkeypatch):
    """Mechanism 8's structural law: a stale/degraded CURRENT snapshot at
    label time gets NO API call ever -- the row keeps deterministic capture
    plus label_skipped_reason='stale', never a fabricated-confidence label."""
    orch, _ = _orch_with_shadow_universe(tmp_path, _seed_symbols(2))
    orch.run_scan_once()
    row = orch.journal.query("SELECT * FROM candidates WHERE shadow_tier = 1")[0]

    class _StaleReport:
        is_usable = False
        freshness_status = "stale"
        block_reason = "STALE_QUOTE"

    monkeypatch.setattr(orch.freshness, "assess", lambda snap: _StaleReport())
    labels_before = orch.journal.count_rows("candidate_labels", "shadow_tier = 1")

    result = orch._label_shadow_shortlist(
        [dict(row, selection_arm="top_k")], scan_batch_id=None, feed_coverage_at_scan=0.9,
    )

    assert result["skipped_stale"] == 1
    assert result["labelled"] == 0
    assert orch.journal.count_rows("candidate_labels", "shadow_tier = 1") == labels_before
    stamped = orch.journal.one("SELECT label_skipped_reason FROM candidates WHERE candidate_id = ?", (row["candidate_id"],))
    assert stamped["label_skipped_reason"] == "stale"
    orch.journal.close()


def test_shadow_candidate_stamps_interest_score_version(tmp_path):
    """Audit-fixup regression (correctness LOW): mechanism 3's own constant
    named this row's interest/momentum formula version, but nothing stamped
    it -- unlike selection_version, a future v1->v2 recalibration of
    SHADOW_V1_* would have been invisible in the archive (code-convention-
    only versioning, no data trail). Mirrors liquidity_instrumentation_
    version's own already-tested pattern immediately above it in
    candidate_scanner.py."""
    from alphaos.constants import SHADOW_INTEREST_SCORE_VERSION_V1

    orch, _ = _orch_with_shadow_universe(tmp_path, _seed_symbols(2))
    orch.run_scan_once()
    row = orch.journal.query("SELECT * FROM candidates WHERE shadow_tier = 1")[0]
    assert row["interest_score_version"] == SHADOW_INTEREST_SCORE_VERSION_V1
    orch.journal.close()


# --------------------------------------------------- operator-surface exclusion (mech 9)
def test_shadow_labelling_loop_does_not_bypass_the_scan_loop_guard(tmp_path):
    """Behavior probe (not just a grep): run shadow labelling end-to-end with
    a real AI label produced, then directly query openai_evaluations/
    trade_proposals for any shadow-tagged candidate_id -- the same probe
    EXP-0's own test uses, now exercised WITH the new labelling loop wired
    in and actually producing real labels (the scenario EXP-0's tests
    necessarily pre-dated)."""
    orch, _ = _orch_with_shadow_universe(tmp_path, _seed_symbols(6))
    orch.run_scan_once()
    _seed_universe_days(orch.journal, timeutils.market_date().isoformat())
    result = shadow_label.run_shadow_label(orch)
    assert result["labelled"] > 0, "expected at least one real shadow label for this probe to be meaningful"

    shadow_ids = [r["candidate_id"] for r in orch.journal.query(
        "SELECT candidate_id FROM candidates WHERE shadow_tier = 1"
    )]
    placeholders = ",".join("?" * len(shadow_ids))
    evals = orch.journal.query(f"SELECT * FROM openai_evaluations WHERE candidate_id IN ({placeholders})", shadow_ids)
    proposals = orch.journal.query(f"SELECT * FROM trade_proposals WHERE candidate_id IN ({placeholders})", shadow_ids)
    assert evals == []
    assert proposals == []
    orch.journal.close()


def test_shadow_label_loop_source_never_bypasses_guard():
    """Source-inspection guard-presence check (mirrors EXP-0's own
    test_shadow_tier_guard_present_at_ai_evaluation_chokepoint): the new
    shadow labelling loop must never construct a ScanContext that reaches
    _handle_proposal/_resolve_decision -- it only ever calls
    _label_candidate directly."""
    source = inspect.getsource(Orchestrator._label_shadow_shortlist)
    assert "_label_candidate" in source
    assert "_handle_proposal" not in source
    assert "_resolve_decision" not in source
    assert "openai.evaluate" not in source


def test_approve_proposal_refuses_forced_shadow_tier_proposal(tmp_path):
    """Mechanism 9(c): defense in depth. Force a trade_proposals row to
    exist for a shadow_tier=1 candidate (bypassing every creation-time
    guard directly at the DB level, simulating "some future path somehow
    lets one through") and confirm approve_proposal refuses it anyway."""
    orch, _ = _orch_with_shadow_universe(tmp_path, _seed_symbols(2))
    orch.run_scan_once()
    shadow = orch.journal.one("SELECT * FROM candidates WHERE shadow_tier = 1 LIMIT 1")
    assert shadow

    proposal_id = new_id("prop")
    orch.journal.insert("trade_proposals", {
        "proposal_id": proposal_id, "candidate_id": shadow["candidate_id"], "symbol": shadow["symbol"],
        "direction": "long", "strategy": "swing", "entry": 20.0, "stop": 19.0, "target": 22.0,
        "max_holding_days": 3, "qty": 10, "risk_per_share": 1.0, "dollar_risk": 10.0, "expected_r": 2.0,
        "status": "pending_approval",
    })

    ok, msg = orch.approve_proposal(proposal_id)

    assert ok is False
    assert "shadow" in msg.lower()
    row = orch.journal.proposal_by_id(proposal_id)
    assert row["status"] == "pending_approval"  # never transitioned toward filled/submitted
    orders = orch.journal.orders_for_proposal(proposal_id)
    assert orders == []
    orch.journal.close()


def test_recent_candidates_excludes_shadow_by_default(tmp_path):
    orch, _ = _orch_with_shadow_universe(tmp_path, _seed_symbols(2))
    orch.run_scan_once()
    cands = orch.journal.recent_candidates(200)
    assert all(not c.get("shadow_tier") for c in cands)
    cands_incl = orch.journal.recent_candidates(200, include_shadow=True)
    assert any(c.get("shadow_tier") for c in cands_incl)
    orch.journal.close()


def test_digest_never_names_a_shadow_symbol(tmp_path):
    """Grep-style assertion over rendered output: after a real shadow label
    (including one that scores 'propose'), the symbol must never appear
    anywhere in the digest's JSON-serialized form outside the exact
    core-tier sections it legitimately belongs to."""
    orch, _ = _orch_with_shadow_universe(tmp_path, _seed_symbols(6))
    orch.run_scan_once()
    _seed_universe_days(orch.journal, timeutils.market_date().isoformat())
    shadow_label.run_shadow_label(orch)

    shadow_symbols = {
        r["symbol"] for r in orch.journal.query("SELECT symbol FROM candidates WHERE shadow_tier = 1")
    }
    assert shadow_symbols, "need at least one shadow symbol for this assertion to be meaningful"

    digest = build_daily_digest(orch.journal, orch.settings, orch.kill_switch)
    blob = json.dumps(digest, default=str)
    for sym in shadow_symbols:
        assert sym not in blob, f"shadow symbol {sym!r} leaked into the digest"
    assert digest["shadow_labelling"]["labelled_today"] >= 1
    orch.journal.close()


def test_daily_brief_never_names_a_shadow_symbol(tmp_path):
    from alphaos.reports.daily_brief import build_daily_brief

    orch, _ = _orch_with_shadow_universe(tmp_path, _seed_symbols(6))
    orch.run_scan_once()
    _seed_universe_days(orch.journal, timeutils.market_date().isoformat())
    shadow_label.run_shadow_label(orch)

    shadow_symbols = {
        r["symbol"] for r in orch.journal.query("SELECT symbol FROM candidates WHERE shadow_tier = 1")
    }
    assert shadow_symbols

    brief = build_daily_brief(orch.journal, orch.settings, orch.kill_switch)
    blob = json.dumps(brief, default=str)
    for sym in shadow_symbols:
        assert sym not in blob, f"shadow symbol {sym!r} leaked into the daily brief"
    orch.journal.close()


def test_relabel_defaults_exclude_shadow_packets(tmp_path):
    from alphaos.relabel import relabel_candidates

    orch, _ = _orch_with_shadow_universe(tmp_path, _seed_symbols(4))
    orch.run_scan_once()
    _seed_universe_days(orch.journal, timeutils.market_date().isoformat())
    shadow_label.run_shadow_label(orch)  # produces real shadow candidate_packets rows
    today = timeutils.stamp().local_sgt[:10]

    res = relabel_candidates(orch.journal, orch.settings, today, today, dry_run=True)
    shadow_symbols = {
        r["symbol"] for r in orch.journal.query("SELECT symbol FROM candidates WHERE shadow_tier = 1")
    }
    prompted_symbols = {p["symbol"] for p in res["prompts"]}
    assert prompted_symbols.isdisjoint(shadow_symbols)

    res_incl = relabel_candidates(orch.journal, orch.settings, today, today, dry_run=True, include_shadow=True)
    prompted_incl = {p["symbol"] for p in res_incl["prompts"]}
    assert shadow_symbols & prompted_incl
    orch.journal.close()


def _seed_real_labelled_shadow_packet(journal, symbol="SHDW1", created_at_utc="2026-07-08T00:00:00+00:00"):
    """Direct construction (§H.1) of one REAL (is_mock=0) shadow-tier
    candidate + packet + label triple -- select_seed_packets requires
    is_mock=0, which mock-mode orchestrator runs never produce."""
    import json as _json
    candidate_id = new_id("cand")
    packet_id = new_id("pkt")
    journal.conn.execute(
        "INSERT INTO candidates (candidate_id, symbol, shadow_tier, created_at_utc, created_at_sgt) "
        "VALUES (?, ?, 1, ?, ?)",
        (candidate_id, symbol, created_at_utc, created_at_utc),
    )
    packet_json = {
        "symbol": symbol, "last_price": 20.0, "direction": "long", "freshness_status": "usable",
        "spread_pct": 0.01, "liquidity_ok": True, "dollar_volume": 5_000_000.0, "change_pct": 0.02,
        "rel_volume": 1.5, "rel_strength_vs_spy": 0.1, "rel_strength_vs_qqq": 0.1,
        "near_day_high": True, "near_day_low": False, "gap_pct": 0.01, "structure_hint": "trend",
        "setup_hint": "breakout", "tradeable_volatility": True, "interest_score": 0.6,
        "shortlist_reason": "test", "momentum_score": 0.6, "missing_data_flags": [],
    }
    journal.conn.execute(
        "INSERT INTO candidate_packets (packet_id, candidate_id, symbol, interest_rank, packet_json, "
        "created_at_utc, created_at_sgt) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (packet_id, candidate_id, symbol, 1, _json.dumps(packet_json), created_at_utc, created_at_utc),
    )
    journal.conn.execute(
        "INSERT INTO candidate_labels (label_id, candidate_id, packet_id, symbol, primary_label, "
        "label_decision, shadow_tier, is_mock, created_at_utc, created_at_sgt) "
        "VALUES (?, ?, ?, ?, 'Momentum', 'watch', 1, 0, ?, ?)",
        (new_id("lbl"), candidate_id, packet_id, symbol, created_at_utc, created_at_utc),
    )
    journal.conn.commit()
    return candidate_id, packet_id


def test_eval_corpus_build_defaults_exclude_shadow_packets(journal):
    from alphaos.eval.corpus import select_seed_packets

    _, _ = _seed_real_labelled_shadow_packet(journal, symbol="SHDW1")

    seeds = select_seed_packets(journal, limit=100)
    assert {s["symbol"] for s in seeds} == set()  # the only real-labelled packet is shadow-tier

    seeds_incl = select_seed_packets(journal, limit=100, include_shadow=True)
    assert {s["symbol"] for s in seeds_incl} == {"SHDW1"}


# ------------------------------------------------------------ idempotency (mech 4)
def test_shadow_label_job_rerun_same_window_spends_zero_extra_calls(tmp_path):
    orch, _ = _orch_with_shadow_universe(tmp_path, _seed_symbols(6))
    orch.run_scan_once()
    _seed_universe_days(orch.journal, timeutils.market_date().isoformat())
    runner = JobRunner(orch)

    r1 = runner.run_job("shadow_label")
    labels_after_first = orch.journal.count_rows("candidate_labels", "shadow_tier = 1")
    r2 = runner.run_job("shadow_label")

    assert r1["status"] == "completed"
    assert r2["status"] == "skipped"
    assert r2["reason"] == "duplicate_lock"
    assert orch.journal.count_rows("candidate_labels", "shadow_tier = 1") == labels_after_first
    orch.journal.close()


# ------------------------------------------------------------- kill switch (mech 13)
def test_kill_switch_engaged_zero_shadow_calls(tmp_path):
    orch, _ = _orch_with_shadow_universe(tmp_path, _seed_symbols(4))
    orch.run_scan_once()
    _seed_universe_days(orch.journal, timeutils.market_date().isoformat())
    ks = KillSwitch(path=str(tmp_path / "KILL_SWITCH"))
    orch.kill_switch = ks
    ks.engage("test")

    result = shadow_label.run_shadow_label(orch)

    assert result["status"] == "skipped"
    assert result["shadow_calls"] == 0
    assert orch.journal.count_rows("candidate_labels", "shadow_tier = 1") == 0
    orch.journal.close()


# --------------------------------------------------------------- auto-suspend
def test_auto_suspend_triggers_on_three_consecutive_bad_coverage_days(tmp_path, journal):
    settings = make_settings(SHADOW_LABEL_MIN_FEED_COVERAGE="0.80")
    for i in range(3):
        _seed_universe_days(journal, _days_ago(i), n_scanned=10, n_fresh=2)  # 0.20 coverage each day
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is True
    assert "consecutive trading days" in reason


def test_auto_suspend_does_not_trigger_with_only_two_bad_days(journal):
    settings = make_settings(SHADOW_LABEL_MIN_FEED_COVERAGE="0.80")
    for i in range(2):
        _seed_universe_days(journal, _days_ago(i), n_scanned=10, n_fresh=2)
    should_suspend, _ = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is False


def test_auto_suspend_triggers_across_a_holiday_cluster_gap(journal):
    """Audit-fixup regression (correctness LOW): 3 bad-coverage TRADING days
    spread across a 6-calendar-day span (simulating a weekend + a holiday
    cluster between them) must still trigger -- the original +2 calendar
    padding on top of the 3-day requirement only covered an ordinary
    weekend (a 5-calendar-day window), so day offset -6 would have fallen
    outside it and this exact case would have silently under-triggered."""
    settings = make_settings(SHADOW_LABEL_MIN_FEED_COVERAGE="0.80")
    for offset in (0, 3, 6):
        _seed_universe_days(journal, _days_ago(offset), n_scanned=10, n_fresh=2)  # 0.20 coverage
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is True
    assert "consecutive trading days" in reason


def test_auto_suspend_triggers_on_canary_tier1(journal):
    settings = make_settings()
    journal.insert("canary_runs", {
        "run_id": "canaryrun_test1", "corpus_dir": "data/canary", "n_prompts": 5,
        "drift_tier": "TIER_1",
        "started_at_utc": timeutils.stamp().utc, "started_at_sgt": timeutils.stamp().local_sgt,
    })
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is True
    assert "Tier-1" in reason or "TIER_1" in reason


def test_auto_suspend_engages_switch_and_stays_engaged(tmp_path):
    """Once auto-suspended, the switch stays engaged for subsequent runs
    (does not self-heal the moment the metric recovers for one tick)."""
    orch, _ = _orch_with_shadow_universe(tmp_path, _seed_symbols(2), SHADOW_LABEL_MIN_FEED_COVERAGE="0.80")
    switch = ShadowLabelSuspendSwitch(path=str(tmp_path / "SHADOW_LABEL_SUSPENDED"))
    for i in range(1, 4):
        _seed_universe_days(orch.journal, _days_ago(i), n_scanned=10, n_fresh=1)

    import alphaos.scheduler.shadow_label as sl_module
    orig = sl_module.ShadowLabelSuspendSwitch
    sl_module.ShadowLabelSuspendSwitch = lambda: switch
    try:
        result = shadow_label.run_shadow_label(orch)
        assert result["status"] == "skipped"
        assert switch.is_engaged()
        # A good day right after must NOT self-heal.
        _seed_universe_days(orch.journal, _days_ago(0), n_scanned=10, n_fresh=10)
        result2 = shadow_label.run_shadow_label(orch)
        assert result2["status"] == "skipped"
        assert "auto-suspended" in result2["reason"]
    finally:
        sl_module.ShadowLabelSuspendSwitch = orig
        switch.release()
    orch.journal.close()


# -------------------------------------------------------- degrade law (mech 13)
def test_global_cap_breach_universe_days_writes_continue(tmp_path):
    """Survivorship regression: run_scan_job's degrade-law fix must keep
    shadow-tier universe_days rows flowing even when the global AI cost cap
    is breached -- the pre-existing bug this fix targets was a full-skip
    that silently stopped ALL capture (core AND shadow) on a cap-breach
    day."""
    from alphaos.scheduler.jobs import run_scan_job
    orch, _ = _orch_with_shadow_universe(
        tmp_path, _seed_symbols(4), SCHEDULER_AI_COST_CAP_CALLS_PER_30D="50",
        SHADOW_AI_CAP_CALLS_PER_30D="12",
    )
    for _ in range(51):
        orch.journal.insert("openai_evaluations", {
            "eval_id": new_id("eval"), "candidate_id": new_id("cand"), "symbol": "AAPL",
            "model": "mock", "direction": "long", "entry": 100.0, "stop": 97.0, "target": 106.0,
            "max_holding_days": 3, "expected_r": 2.0, "confidence": 0.8, "decision": "propose",
            "reasoning_summary": "test", "is_mock": 0,
        })
    universe_days_before = orch.journal.count_rows("universe_days")
    proposals_before = orch.journal.count_rows("trade_proposals")

    result = run_scan_job(orch, runner=None)

    assert result["cost_cap_exceeded"] is True
    assert result["degraded"] is True
    assert orch.journal.count_rows("universe_days") > universe_days_before
    assert orch.journal.count_rows("trade_proposals") == proposals_before  # no AI eval ran
    orch.journal.close()


# --------------------------------------------------------------- schema/migration
def test_additive_shadow_columns_migrate_onto_an_old_db(tmp_path):
    """An old DB (pre-EXP-1 shape: candidates/candidate_labels without the
    new columns) must still open and be reconciled additively -- old DBs
    must always open (house law)."""
    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE candidates (id INTEGER PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE, "
        "symbol TEXT NOT NULL, shadow_tier INTEGER DEFAULT 0, "
        "created_at_utc TEXT NOT NULL, created_at_sgt TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE candidate_labels (id INTEGER PRIMARY KEY, label_id TEXT NOT NULL UNIQUE, "
        "candidate_id TEXT NOT NULL, symbol TEXT NOT NULL, "
        "created_at_utc TEXT NOT NULL, created_at_sgt TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    store = JournalStore(db_path)  # must not raise
    info = store.conn.execute("PRAGMA table_info(candidates)").fetchall()
    cols = {r[1] for r in info}
    for expected in (
        "bid_size", "ask_size", "quote_age_seconds", "spread_pct_mid", "adv_20d_dollar",
        "volume_today_pct_of_adv", "scan_window", "data_feed", "crossed_or_locked_quote",
        "core_gate_verdict", "liquidity_instrumentation_version", "interest_score_version", "selection_arm",
        "selection_version", "feed_coverage_at_scan", "label_skipped_reason", "sector_cluster_key",
    ):
        assert expected in cols, f"{expected!r} missing from migrated candidates table"
    label_cols = {r[1] for r in store.conn.execute("PRAGMA table_info(candidate_labels)").fetchall()}
    assert "shadow_tier" in label_cols
    store.close()


# ---------------------------------------------------------------- flags/defaults
def test_shadow_labelling_disabled_by_default():
    settings = make_settings()
    assert settings.shadow_labelling_enabled is False


def test_shadow_labelling_enabled_false_zero_side_effects(tmp_path):
    orch, _ = _orch_with_shadow_universe(tmp_path, _seed_symbols(4), SHADOW_LABELLING_ENABLED="false")
    orch.run_scan_once()
    result = shadow_label.run_shadow_label(orch)
    assert result["status"] == "skipped"
    assert result["shadow_calls"] == 0
    assert orch.journal.count_rows("candidate_labels", "shadow_tier = 1") == 0
    orch.journal.close()


# --------------------------------------------------------------------------- CLI
def test_cli_scheduler_run_job_accepts_shadow_label(tmp_path):
    """Mechanism 4: `alphaos scheduler_run_job shadow_label` must be a valid
    CLI invocation (the manual-rerun idempotency acceptance criterion)."""
    from alphaos import __main__ as cli

    parser = cli.build_parser()
    args = parser.parse_args(["scheduler_run_job", "shadow_label"])
    assert args.job_type == "shadow_label"


def test_canary_corpus_build_defaults_exclude_shadow_packets(journal):
    """Mechanism 9(f)/12: CANARY's own select_seed_packets (a SEPARATE
    function from EVAL-1's, alphaos/canary/corpus.py) needed the identical
    fix -- CANARY's golden corpus stays megacap-weighted for now."""
    from alphaos.canary.corpus import select_seed_packets

    _seed_real_labelled_shadow_packet(journal, symbol="SHDW2")

    seeds = select_seed_packets(journal, limit=100)
    assert {s["symbol"] for s in seeds} == set()

    seeds_incl = select_seed_packets(journal, limit=100, include_shadow=True)
    assert {s["symbol"] for s in seeds_incl} == {"SHDW2"}


# ------------------------------------------------- discovered contamination risk
def test_h_int_1_never_pools_shadow_tier_rows(journal):
    """Discovered while building EXP-1 (not one of the spec's 13 numbered
    mechanisms, but squarely covered by its own 'Never' law: never pool
    across core/shadow). h_int_1_rows drives directly off `candidates`;
    without a shadow_tier filter a shadow-tier row with a candidate_outcomes
    replay_r would silently pool into H-INT-1's core evidence."""
    from alphaos.hypotheses.queries import h_int_1_rows

    for i, (shadow, score, r) in enumerate([
        (0, 0.9, 1.0), (0, 0.1, -0.5), (0, 0.5, 0.0), (0, 0.4, 0.1),
        (0, 0.6, 0.2), (0, 0.3, -0.1), (0, 0.7, 0.3), (0, 0.2, -0.2),
        (0, 0.8, 0.4), (0, 0.45, 0.05),
        (1, 0.99, 5.0),  # shadow-tier outlier -- must never enter H-INT-1's population
    ]):
        cid = new_id("cand")
        journal.conn.execute(
            "INSERT INTO candidates (candidate_id, symbol, interest_score, shadow_tier, "
            "created_at_utc, created_at_sgt) VALUES (?, ?, ?, ?, '2026-07-01T00:00:00+00:00', "
            "'2026-07-01T00:00:00+00:00')",
            (cid, f"H{i}", score, shadow),
        )
        journal.conn.execute(
            "INSERT INTO candidate_outcomes (outcome_id, candidate_id, symbol, candidate_type, replay_r, "
            "created_at_utc, created_at_sgt) VALUES (?, ?, ?, 'candidate', ?, "
            "'2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00')",
            (new_id("out"), cid, f"H{i}", r),
        )
    journal.conn.commit()

    rows, _, reference = h_int_1_rows(journal)
    all_symbols = {r["symbol"] for r in rows} | {r["symbol"] for r in (reference or [])}
    assert "H10" not in all_symbols, "shadow-tier row leaked into H-INT-1's evidence population"


def test_h_win_1_never_pools_shadow_tier_rows(journal):
    """Audit-fixup (correctness LOW): h_win_1_rows carries the identical
    `c.shadow_tier = 0` filter as h_int_1_rows, but only H-INT-1 had a
    dedicated regression test -- this is H-WIN-1's twin, closing the gap
    the correctness audit named."""
    from alphaos.hypotheses.queries import h_win_1_rows

    sbid = new_id("scan")
    journal.conn.execute(
        "INSERT INTO scan_batches (scan_batch_id, market_session, started_at_sgt, "
        "created_at_utc, created_at_sgt) VALUES (?, 'regular', '2026-07-01T09:40:00+08:00', "
        "'2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00')",
        (sbid,),
    )
    for i, (shadow, r) in enumerate([
        (0, 1.0), (0, -0.5), (0, 0.0), (0, 0.1), (0, 0.2),
        (0, -0.1), (0, 0.3), (0, -0.2), (0, 0.4), (0, 0.05),
        (1, 5.0),  # shadow-tier outlier -- must never enter H-WIN-1's population
    ]):
        cid = new_id("cand")
        journal.conn.execute(
            "INSERT INTO candidates (candidate_id, symbol, scan_batch_id, shadow_tier, "
            "created_at_utc, created_at_sgt) VALUES (?, ?, ?, ?, '2026-07-01T00:00:00+00:00', "
            "'2026-07-01T00:00:00+00:00')",
            (cid, f"W{i}", sbid, shadow),
        )
        journal.conn.execute(
            "INSERT INTO candidate_outcomes (outcome_id, candidate_id, symbol, candidate_type, replay_r, "
            "created_at_utc, created_at_sgt) VALUES (?, ?, ?, 'candidate', ?, "
            "'2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00')",
            (new_id("out"), cid, f"W{i}", r),
        )
    journal.conn.commit()

    rows, _, reference = h_win_1_rows(journal)
    all_symbols = {r["symbol"] for r in rows} | {r["symbol"] for r in (reference or [])}
    assert "W10" not in all_symbols, "shadow-tier row leaked into H-WIN-1's evidence population"


def test_proposal_display_surfaces_never_leak_a_shadow_tier_proposal(tmp_path, journal):
    """Audit-fixup regression (scope/safety, the audit's single most
    consequential finding): plant a `trade_proposals` row pointing at a
    shadow-tier candidate directly -- bypassing every creation-time guard,
    exactly as the auditor did to demonstrate the gap -- and confirm
    open_proposals()/build_daily_digest()/build_daily_brief() all exclude
    it. The two adjacent symbol-leak tests above (test_digest_never_names_a_
    shadow_symbol / test_daily_brief_never_names_a_shadow_symbol) structurally
    can't catch this: per the auditor, neither of them ever creates a
    proposal at all, so a display surface relying solely on the (today,
    unreachable) creation-time chokepoint would sail through both."""
    journal.insert("candidates", {
        "candidate_id": "cand-shadow-1", "symbol": "SHDWPROP", "shadow_tier": 1,
    })
    journal.insert("trade_proposals", {
        "proposal_id": "prop-shadow-1", "candidate_id": "cand-shadow-1",
        "symbol": "SHDWPROP", "status": "pending_approval",
    })

    open_props = journal.open_proposals()
    assert "SHDWPROP" not in {p["symbol"] for p in open_props}, (
        "shadow-tier proposal leaked into open_proposals()"
    )

    settings = make_settings()
    kill_switch = KillSwitch(path=str(tmp_path / "KILL_SWITCH"))

    digest = build_daily_digest(journal, settings, kill_switch)
    assert "SHDWPROP" not in json.dumps(digest, default=str), (
        "shadow-tier proposal leaked into the digest"
    )

    from alphaos.reports.daily_brief import build_daily_brief

    brief = build_daily_brief(journal, settings, kill_switch)
    assert "SHDWPROP" not in json.dumps(brief, default=str), (
        "shadow-tier proposal leaked into the daily brief"
    )


def test_todays_activity_excludes_shadow_tier_proposals_from_counts(tmp_path, journal):
    """Audit-fixup regression (scope/safety LOW, self-discovered while
    writing the test above): unlike open_proposals()/digest.py's four
    buckets, daily_brief.py's _todays_activity() counted `trade_proposals`
    directly with no shadow_tier join at all -- a symbol-grep test can't
    catch this since it's a COUNT, not a name, so this asserts on the
    number itself. A shadow-tier proposal must not inflate either the
    'proposed today' or 'blocked today' counters an operator reads as real
    activity."""
    from alphaos.reports.daily_brief import build_daily_brief

    journal.insert("candidates", {
        "candidate_id": "cand-shadow-2", "symbol": "SHDWCOUNT", "shadow_tier": 1,
    })
    journal.insert("trade_proposals", {
        "proposal_id": "prop-shadow-2a", "candidate_id": "cand-shadow-2",
        "symbol": "SHDWCOUNT", "status": "pending_approval",
    })
    journal.insert("trade_proposals", {
        "proposal_id": "prop-shadow-2b", "candidate_id": "cand-shadow-2",
        "symbol": "SHDWCOUNT", "status": "blocked",
    })

    settings = make_settings()
    kill_switch = KillSwitch(path=str(tmp_path / "KILL_SWITCH"))

    brief = build_daily_brief(journal, settings, kill_switch)
    ta = brief["todays_activity"]
    assert ta["proposed_today"] == 0, "shadow-tier proposal inflated the proposed_today count"
    assert ta["blocked_today"] == 0, "shadow-tier proposal inflated the blocked_today count"


def test_tqs_score_scan_batch_excludes_shadow_tier(tmp_path):
    from alphaos.tqs.batch import score_scan_batch

    orch, _ = _orch_with_shadow_universe(tmp_path, _seed_symbols(4), TQS_SHADOW_ENABLED="true")
    summary = orch.run_scan_once()
    shadow_ids = {
        r["candidate_id"] for r in orch.journal.query("SELECT candidate_id FROM candidates WHERE shadow_tier = 1")
    }
    assert shadow_ids

    score_scan_batch(orch.journal, orch.settings, summary.scan_batch_id)

    scored_ids = {r["candidate_id"] for r in orch.journal.query("SELECT candidate_id FROM tqs_scores")}
    assert scored_ids.isdisjoint(shadow_ids)
    orch.journal.close()


def test_label_summary_and_recent_labels_exclude_shadow(journal):
    _seed_real_labelled_shadow_packet(journal, symbol="SHDW3")
    journal.conn.execute(
        "INSERT INTO candidates (candidate_id, symbol, shadow_tier, created_at_utc, created_at_sgt) "
        "VALUES ('core1', 'COREX', 0, '2026-07-08T00:00:00+00:00', '2026-07-08T00:00:00+00:00')"
    )
    journal.conn.execute(
        "INSERT INTO candidate_labels (label_id, candidate_id, symbol, primary_label, label_decision, "
        "shadow_tier, is_mock, created_at_utc, created_at_sgt) VALUES "
        "('lbl_core1', 'core1', 'COREX', 'Momentum', 'watch', 0, 0, "
        "'2026-07-08T00:00:00+00:00', '2026-07-08T00:00:00+00:00')"
    )
    journal.conn.commit()

    summary = journal.label_summary()
    total_by_label = sum(r["n"] for r in summary["by_primary_label"])
    assert total_by_label == 1  # only the core row counted, not the shadow one

    recent = journal.recent_candidate_labels(200)
    assert all(not r.get("shadow_tier") for r in recent)
    recent_incl = journal.recent_candidate_labels(200, include_shadow=True)
    assert any(r.get("shadow_tier") for r in recent_incl)
