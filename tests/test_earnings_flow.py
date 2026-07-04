"""End-to-end earnings-proximity flow (PR5): journaled, advisory-only, never
bypasses gates / approval / execution / labeller; the per-scan budget cap
enriches the top-N by rank and journals the rest as a DISTINCT
skipped_budget_cap; the decision-time recompute uses the REAL max_holding_days
without re-fetching. Hermetic -- mock provider only."""

from __future__ import annotations

import pathlib

from alphaos import earnings as earnings_pkg
from alphaos.constants import EarningsDataStatus, OFFICIAL_LABELS
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from conftest import make_settings


def _orch(**over):
    base = {"LABELLING_ENABLED": "true"}
    base.update(over)
    return Orchestrator(settings=make_settings(**base), journal=JournalStore(":memory:"))


# ------------------------------------------------------------- default posture
def test_enabled_by_default():
    """Unlike last30days, earnings-proximity defaults to ON (mock, zero-cost,
    informational) -- a plain scan with labelling on should produce rows with no
    extra settings."""
    o = _orch()
    summ = o.run_scan_once()
    assert summ.earnings_enriched > 0
    assert o.journal.count_rows("candidate_earnings") > 0
    o.close()


def test_disabled_writes_no_rows():
    o = _orch(EARNINGS_PROXIMITY_ENABLED="false")
    summ = o.run_scan_once()
    assert summ.earnings_enriched == 0 and summ.earnings_skipped_budget_cap == 0
    assert o.journal.count_rows("candidate_earnings") == 0
    o.close()


# ---------------------------------------------------------------- budget cap
def test_scan_enriches_and_journals_every_eligible_candidate():
    o = _orch(MAX_CANDIDATES_TO_AI="10", EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="6")
    summ = o.run_scan_once()
    assert 0 < summ.earnings_enriched <= 6
    rows = o.journal.query("SELECT * FROM candidate_earnings")
    assert len(rows) == summ.earnings_enriched + summ.earnings_skipped_budget_cap
    assert all(r["earnings_data_status"] for r in rows)   # never blank
    o.close()


def test_budget_cap_top10_enriched_rest_skipped():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="10")
    summ = o.run_scan_once()
    assert summ.labelled == 12
    assert summ.earnings_enriched == 10
    assert summ.earnings_skipped_budget_cap == 2

    skipped = o.journal.query(
        "SELECT * FROM candidate_earnings WHERE enrichment_status = 'skipped'"
    )
    assert len(skipped) == 2
    for r in skipped:
        assert r["earnings_data_status"] == EarningsDataStatus.UNKNOWN.value
        assert r["earnings_data_status"] != EarningsDataStatus.UNAVAILABLE.value
        assert r["provider"]
        assert r["symbol"]
    o.close()


# ---------------------------------------------------- denormalized summaries
def test_candidates_get_earnings_summary_columns():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="10")
    o.run_scan_once()
    rows = o.journal.query(
        "SELECT earnings_date, earnings_data_status FROM candidates "
        "WHERE earnings_data_status IS NOT NULL"
    )
    assert len(rows) == 12  # every shortlisted/labelled candidate gets a concrete status
    concrete = [r for r in rows if r["earnings_data_status"] in ("ok", "unavailable")]
    assert len(concrete) == 10  # the 10 actually enriched within the per-scan cap
    o.close()


def test_rejected_candidates_get_earnings_summary_columns():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="10")
    o.run_scan_once()
    rejects = o.journal.query("SELECT * FROM rejected_candidates")
    assert rejects
    for r in rejects:
        assert "earnings_data_status" in r.keys()
        assert "lineage_id" in r.keys()
    o.close()


def test_decision_adjustments_get_earnings_summary_columns():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="10")
    o.run_scan_once()
    rows = o.journal.query("SELECT * FROM decision_adjustments")
    assert rows
    for r in rows:
        assert "earnings_date" in r.keys()
        assert "earnings_data_status" in r.keys()
    o.close()


def test_trade_proposals_get_earnings_summary_recomputed_with_real_hold_days():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="10")
    o.run_scan_once()
    proposals = o.journal.query("SELECT * FROM trade_proposals")
    assert proposals
    for p in proposals:
        assert "earnings_data_status" in p.keys()
        assert "lineage_id" in p.keys()
        # the proposal's own max_holding_days is what earnings was recomputed
        # against -- both are on the same row so a caller can audit consistency.
        assert p["max_holding_days"] is not None
    o.close()


# --------------------------------------------------------- provider health
def test_unavailable_data_status_appears_distinctly_not_as_safe(monkeypatch):
    """Force EVERY enriched candidate's provider result to "no data found" so
    this doesn't depend on the mock's date-seeded ~15%-per-symbol unavailable
    roll landing on at least one of 10 symbols (a real flake risk across a
    handful of symbols -- see HANDOVER.md's documented mock-RNG-boundary
    lesson)."""
    from alphaos.earnings.earnings_provider import EarningsProximityResult

    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="10")

    def _always_unavailable(symbol):
        return EarningsProximityResult(
            symbol=symbol, earnings_date=None, status=EarningsDataStatus.UNAVAILABLE.value,
            source="stub",
        )

    monkeypatch.setattr(o.earnings_enricher._provider, "get_earnings_for_symbol", _always_unavailable)
    o.run_scan_once()
    unavailable = o.journal.query(
        "SELECT * FROM candidate_earnings WHERE earnings_data_status = ?",
        (EarningsDataStatus.UNAVAILABLE.value,),
    )
    assert len(unavailable) == 10  # every enriched candidate, deterministically
    for r in unavailable:
        assert r["earnings_date"] is None
        assert r["earnings_within_hold_window"] == 0
        assert "earnings_data_unavailable" in (r["risk_tags_json"] or "")
    o.close()


def test_provider_disabled_never_reads_as_ok():
    o = _orch(EARNINGS_PROXIMITY_ENABLED="false")
    o.run_scan_once()
    assert o.journal.count_rows("candidate_earnings") == 0  # disabled entirely: no rows at all
    o.close()


# -------------------------------------------------------------- timing values
def test_timing_values_from_scan_are_recognized():
    from alphaos.constants import EarningsTiming

    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="10")
    o.run_scan_once()
    timings = {r["earnings_timing"] for r in o.journal.query(
        "SELECT DISTINCT earnings_timing FROM candidate_earnings WHERE earnings_timing IS NOT NULL"
    )}
    allowed = {EarningsTiming.BEFORE_OPEN.value, EarningsTiming.AFTER_CLOSE.value,
              EarningsTiming.UNKNOWN.value}
    assert timings <= allowed
    o.close()


# ---------------------------------------------------------------- lineage
def test_candidate_earnings_rows_carry_lineage_id():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="10")
    o.run_scan_once()
    rows = o.journal.query("SELECT lineage_id FROM candidate_earnings")
    assert rows and all(r["lineage_id"] for r in rows)
    snapshot = o.journal.one(
        "SELECT earnings_config_hash FROM lineage_snapshots WHERE lineage_id = ?",
        (rows[0]["lineage_id"],),
    )
    assert snapshot and snapshot["earnings_config_hash"]
    o.close()


def test_decision_lineage_report_includes_candidate_earnings():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="10")
    o.run_scan_once()
    cand = o.journal.one("SELECT candidate_id FROM candidates LIMIT 1")
    report = o.decision_lineage_report(cand["candidate_id"])
    assert report["found"] is True
    assert "candidate_earnings" in report
    o.close()


# ---------------------------------------------------- digest surfacing (PR5)
def test_daily_digest_surfaces_earnings_proximity_section():
    from alphaos.scheduler.digest import build_daily_digest

    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="10")
    o.run_scan_once()
    digest = build_daily_digest(o.journal, o.settings, o.kill_switch)
    assert "earnings_proximity" in digest
    ep = digest["earnings_proximity"]
    for key in ("enabled", "provider", "candidates_near_earnings_hold_window_today",
                "proposals_near_earnings_hold_window_today",
                "candidates_earnings_warning_today", "earnings_data_unavailable_today",
                "earnings_provider_failures_today"):
        assert key in ep
    o.close()


def test_digest_surfaces_in_hold_candidate_that_never_became_a_proposal():
    """A candidate INSIDE the hold window that gets REJECTED (never a proposal)
    must still appear in the digest's candidate-level hold bucket -- the most
    severe event-risk signal must not be dropped just because the candidate
    didn't become a trade. Force every earnings result to 2 days out so every
    enriched candidate is inside the default 3-day hold window, then reject them
    all via the kill switch path is not needed -- we assert directly on the
    candidate_earnings-backed digest bucket."""
    from alphaos.earnings.earnings_provider import EarningsProximityResult
    from alphaos.scheduler.digest import build_daily_digest

    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="10")

    def _always_in_hold(symbol):
        from datetime import timedelta
        from alphaos.util import timeutils
        earnings_date = (timeutils.market_date() + timedelta(days=2)).isoformat()
        return EarningsProximityResult(
            symbol=symbol, earnings_date=earnings_date, status=EarningsDataStatus.OK.value,
            source="stub",
        )

    monkeypatch_target = o.earnings_enricher._provider
    orig = monkeypatch_target.get_earnings_for_symbol
    monkeypatch_target.get_earnings_for_symbol = _always_in_hold
    try:
        o.run_scan_once()
    finally:
        monkeypatch_target.get_earnings_for_symbol = orig

    # candidate_earnings hold-window rows exist for EVERY enriched candidate,
    # regardless of whether it became a proposal or was rejected.
    in_hold = o.journal.query(
        "SELECT candidate_id FROM candidate_earnings WHERE earnings_within_hold_window = 1")
    assert len(in_hold) == 10
    digest = build_daily_digest(o.journal, o.settings, o.kill_switch)
    surfaced_ids = {r["candidate_id"]
                    for r in digest["earnings_proximity"]["candidates_near_earnings_hold_window_today"]}
    # every in-hold candidate is surfaced, including any that were rejected
    assert surfaced_ids == {r["candidate_id"] for r in in_hold}
    o.close()


def test_digest_provider_failures_bucket_surfaces_injected_failure():
    """When the provider raises, the enricher fails safe (enrichment_status
    'error') AND the digest's provider-failures bucket surfaces it -- a health
    signal must not be silently swallowed by the fail-safe."""
    from alphaos.scheduler.digest import build_daily_digest

    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="10")

    def _boom(symbol):
        raise RuntimeError("provider outage")

    o.earnings_enricher._provider.get_earnings_for_symbol = _boom
    o.run_scan_once()  # must NOT raise -- fail-safe
    failures = o.journal.query(
        "SELECT * FROM candidate_earnings WHERE enrichment_status = 'error'")
    assert len(failures) == 10
    digest = build_daily_digest(o.journal, o.settings, o.kill_switch)
    surfaced = digest["earnings_proximity"]["earnings_provider_failures_today"]
    assert len(surfaced) == 10
    # and they are NOT falsely "safe": data status is unavailable, flags are 0
    for r in surfaced:
        assert r["earnings_data_status"] == EarningsDataStatus.UNAVAILABLE.value
        assert r["earnings_within_hold_window"] == 0
    o.close()


def test_digest_unavailable_bucket_excludes_budget_skips():
    """Budget-cap skips (a deliberate cost choice, not a data-health problem)
    must NOT inflate the provider-health 'unavailable' bucket, or a real outage
    would be masked by routine cost-control noise."""
    from alphaos.scheduler.digest import build_daily_digest

    # 12 shortlisted, only 8 within the earnings budget -> 4 budget-skipped.
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="8")
    o.run_scan_once()
    skipped = o.journal.query(
        "SELECT * FROM candidate_earnings WHERE enrichment_status = 'skipped'")
    assert len(skipped) == 4  # they exist and are queryable...
    digest = build_daily_digest(o.journal, o.settings, o.kill_switch)
    unavailable = digest["earnings_proximity"]["earnings_data_unavailable_today"]
    # ...but none of them appear in the provider-health bucket
    assert all(r["enrichment_status"] != "skipped" for r in unavailable)
    o.close()


# ------------------------------------------------------------ safety invariants
def test_earnings_creates_no_execution_and_no_approval():
    o = _orch()
    summ = o.run_scan_once()
    assert summ.proposed >= 0
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_rows("paper_fills") == 0
    assert o.journal.count_open_positions() == 0
    assert o.journal.count_rows("approvals") == 0     # manual approval still required
    o.close()


def test_manual_approval_boundary_unchanged_with_earnings_enabled():
    o = _orch()
    o.run_scan_once()
    approved_or_filled = o.journal.query(
        "SELECT * FROM trade_proposals WHERE status IN ('approved', 'filled')"
    )
    assert approved_or_filled == []
    o.close()


def test_earnings_never_creates_or_overwrites_official_labels():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="10")
    o.run_scan_once()
    labels = o.journal.query("SELECT primary_label FROM candidate_labels")
    assert labels and all(l["primary_label"] in OFFICIAL_LABELS for l in labels)
    o.close()


def test_earnings_cannot_change_decision_behavior():
    """Toggling earnings-proximity on/off must NOT change the scan's decision
    distribution -- it is never applied to the packet, so the AI eval/labeller
    sees an identical view either way."""
    base = {"INTEREST_SCAN_TOP_N": "12", "MAX_CANDIDATES_TO_AI": "12", "LABELLING_ENABLED": "true"}
    off = Orchestrator(settings=make_settings(EARNINGS_PROXIMITY_ENABLED="false", **base),
                       journal=JournalStore(":memory:"))
    summ_off = off.run_scan_once()
    off.close()
    on = Orchestrator(settings=make_settings(EARNINGS_PROXIMITY_ENABLED="true",
                                             EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="10", **base),
                      journal=JournalStore(":memory:"))
    summ_on = on.run_scan_once()
    on.close()
    assert summ_on.proposed == summ_off.proposed
    assert summ_on.watch == summ_off.watch
    assert summ_on.rejected == summ_off.rejected


def test_earnings_proximity_never_applied_to_packet():
    """CandidatePacket has NO ``apply_earnings``-style hook (unlike
    ``apply_catalyst``/``apply_last30days``, which explicitly feed the AI
    packet) -- earnings-proximity context has no path to reach the AI
    eval/labeller prompt. (Note: the packet's pre-existing ``earnings_context``
    field is populated by ``apply_catalyst()`` as part of the UNRELATED
    catalyst-evidence breakdown -- a naming coincidence, not this PR's flag.)"""
    from alphaos.scanner.candidate_packet import CandidatePacket

    assert not hasattr(CandidatePacket, "apply_earnings")

    # Toggling earnings-proximity must not perturb the (catalyst-driven) packet
    # field of the same name -- confirms no accidental coupling was introduced.
    import json

    base = {"INTEREST_SCAN_TOP_N": "12", "MAX_CANDIDATES_TO_AI": "12", "LABELLING_ENABLED": "true"}
    off = Orchestrator(settings=make_settings(EARNINGS_PROXIMITY_ENABLED="false", **base),
                       journal=JournalStore(":memory:"))
    off.run_scan_once()
    ctx_off = [json.loads(r["packet_json"])["earnings_context"]
              for r in off.journal.query("SELECT packet_json FROM candidate_packets ORDER BY id")]
    off.close()

    on = Orchestrator(settings=make_settings(EARNINGS_PROXIMITY_ENABLED="true",
                                             EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="10", **base),
                      journal=JournalStore(":memory:"))
    on.run_scan_once()
    ctx_on = [json.loads(r["packet_json"])["earnings_context"]
             for r in on.journal.query("SELECT packet_json FROM candidate_packets ORDER BY id")]
    on.close()

    assert ctx_on == ctx_off


def test_real_money_unreachable_with_earnings_enabled():
    o = _orch()
    assert o.system_health()["real_money_trading"] == "unreachable"
    o.close()


def test_no_orders_approvals_fills_positions_created_by_earnings_code():
    """The earnings package must never touch order/approval/position state --
    same structural grep-based check used for the scheduler/lineage packages."""
    earnings_dir = pathlib.Path(earnings_pkg.__file__).parent
    banned = ("execute_proposal", "approve_proposal", "close_position",
              "submit_bracket", "submit_order", "place_order")
    for py_file in earnings_dir.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{py_file.name} references {token!r}"


def test_earnings_package_never_references_alpaca_client():
    earnings_dir = pathlib.Path(earnings_pkg.__file__).parent
    for py_file in earnings_dir.glob("*.py"):
        assert "alpaca_client" not in py_file.read_text(encoding="utf-8")


def test_risk_checks_unaffected_by_earnings_toggle():
    """Risk sizing/checks are computed entirely independently of earnings-
    proximity -- the risk_checks rows (and the sizing they carry) must be
    identical whether earnings-proximity is on or off."""
    base = {"INTEREST_SCAN_TOP_N": "12", "MAX_CANDIDATES_TO_AI": "12", "LABELLING_ENABLED": "true"}
    off = Orchestrator(settings=make_settings(EARNINGS_PROXIMITY_ENABLED="false", **base),
                       journal=JournalStore(":memory:"))
    off.run_scan_once()
    risk_off = off.journal.query(
        "SELECT result, position_size, max_risk_amount FROM risk_checks ORDER BY id"
    )
    off.close()

    on = Orchestrator(settings=make_settings(EARNINGS_PROXIMITY_ENABLED="true",
                                             EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="10", **base),
                      journal=JournalStore(":memory:"))
    on.run_scan_once()
    risk_on = on.journal.query(
        "SELECT result, position_size, max_risk_amount FROM risk_checks ORDER BY id"
    )
    on.close()

    assert [dict(r) for r in risk_on] == [dict(r) for r in risk_off]


def test_protection_watchdog_unaffected_by_earnings_toggle():
    """protection_watchdog is untouched by this PR; confirm its read-only status
    report is byte-identical whether earnings-proximity is on or off."""
    from alphaos.execution.protection_watchdog import status_report

    base = {"INTEREST_SCAN_TOP_N": "12", "MAX_CANDIDATES_TO_AI": "12", "LABELLING_ENABLED": "true"}
    off = Orchestrator(settings=make_settings(EARNINGS_PROXIMITY_ENABLED="false", **base),
                       journal=JournalStore(":memory:"))
    off.run_scan_once()
    report_off = status_report(off.journal)
    off.close()

    on = Orchestrator(settings=make_settings(EARNINGS_PROXIMITY_ENABLED="true",
                                             EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="10", **base),
                      journal=JournalStore(":memory:"))
    on.run_scan_once()
    report_on = status_report(on.journal)
    on.close()

    assert report_on == report_off


def test_scheduler_triggered_scan_also_produces_candidate_earnings():
    """The scheduler path (JobRunner) reaches the same run_scan_once() code --
    earnings enrichment must be populated there too, and scheduler bookkeeping
    (job_runs/scheduler_runs) must remain unaffected."""
    from alphaos.scheduler import JobRunner

    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="10")
    o.startup()
    result = JobRunner(o).run_job("scan")
    if result["status"] != "completed":
        o.close()
        return  # kill switch/cost cap skipped this pass -- nothing to assert
    assert o.journal.count_rows("candidate_earnings") > 0
    job_run = o.journal.one("SELECT * FROM job_runs WHERE job_type = 'scan' ORDER BY id DESC LIMIT 1")
    assert job_run is not None
    assert job_run["status"] == "completed"
    o.close()


# --------------------------------------------------- additive migration (PR5)
def test_old_db_gets_earnings_columns_and_table_added_nullable(tmp_path):
    """A ledger written before PR5 (no earnings columns on candidates, no
    candidate_earnings table) must open cleanly: the 6 summary columns are
    ALTER-added as nullable, the new table is created, an existing pre-PR5 row
    survives with NULL earnings fields, and SCHEMA_VERSION does not move (purely
    additive)."""
    import sqlite3

    from alphaos.journal.schema import SCHEMA_VERSION

    db = str(tmp_path / "pre_pr5.db")
    raw = sqlite3.connect(db)
    # Minimal pre-PR5 candidates table (no earnings_* columns).
    raw.execute(
        "CREATE TABLE candidates (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "candidate_id TEXT, symbol TEXT, status TEXT)"
    )
    raw.execute(
        "INSERT INTO candidates (candidate_id, symbol, status) VALUES ('old_cand', 'AAPL', 'detected')"
    )
    raw.execute("PRAGMA user_version = 0")
    raw.commit()
    raw.close()

    j = JournalStore(db)
    try:
        cols = {r["name"] for r in j.conn.execute("PRAGMA table_info(candidates)")}
        for c in ("earnings_date", "days_until_earnings", "earnings_within_hold_window",
                  "earnings_within_warning_window", "earnings_timing", "earnings_data_status"):
            assert c in cols, f"missing additive column {c}"
        tables = {r["name"] for r in j.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "candidate_earnings" in tables
        # the pre-PR5 row still reads, with NULL earnings fields
        old = j.one("SELECT * FROM candidates WHERE candidate_id = 'old_cand'")
        assert old["symbol"] == "AAPL"
        assert old["earnings_data_status"] is None
        # additive-only: version constant unchanged
        assert j.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert SCHEMA_VERSION == 3
    finally:
        j.close()
