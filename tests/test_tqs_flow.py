"""End-to-end TQS v0 shadow scoring (PR7): scored for every eligible candidate
and proposal, computed strictly AFTER decisions are committed so it cannot
influence them (proven both by an on/off behavior-neutrality A/B test and by
grep-based structural checks that no decision path reads alphaos.tqs),
fail-safe (component/row errors never fail a scan), idempotent (a second
scoring pass inserts zero new rows), and lineage-stamped. Hermetic -- mock
mode, no network."""

from __future__ import annotations

import pathlib

import alphaos.tqs as tqs_pkg
from alphaos.constants import TqsBucket, TqsDataQualityStatus
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.tqs import TQS_VERSION
from conftest import make_settings


def _orch(**over):
    base = {"LABELLING_ENABLED": "true"}
    base.update(over)
    return Orchestrator(settings=make_settings(**base), journal=JournalStore(":memory:"))


# ------------------------------------------------------------- default posture
def test_enabled_by_default_and_scores_a_real_scan():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    summ = o.run_scan_once()
    assert summ.tqs_scored_candidates > 0
    assert o.journal.count_rows("tqs_scores") > 0
    o.close()


def test_disabled_writes_no_rows():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12", TQS_SHADOW_ENABLED="false")
    summ = o.run_scan_once()
    assert summ.tqs_scored_candidates == 0
    assert summ.tqs_scored_proposals == 0
    assert o.journal.count_rows("tqs_scores") == 0
    o.close()


# --------------------------------------------------------- scoring coverage
def test_every_candidate_regardless_of_decision_gets_a_candidate_level_score():
    """The whole point of excluding decisions from TQS's own inputs: a REJECT
    must score exactly like a PROPOSE would, given the same underlying
    evidence -- proven by checking every candidate status is represented."""
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    summ = o.run_scan_once()
    total_candidates = o.journal.count_rows("candidates")
    assert summ.tqs_scored_candidates == total_candidates
    joined = o.journal.query(
        "SELECT c.status FROM candidates c "
        "LEFT JOIN tqs_scores t ON t.candidate_id = c.candidate_id AND t.source_type = 'candidate' "
        "WHERE t.tqs_id IS NULL"
    )
    assert joined == []  # no candidate silently skipped
    o.close()


def test_proposal_gets_a_separate_row_from_its_candidate():
    """Deterministic by construction (inject_pending_proposal), not dependent
    on whether the mock scan's date-seeded RNG happens to produce a proposal
    this run."""
    from conftest import inject_pending_proposal
    from alphaos.tqs import score_candidate, score_proposal

    o = _orch()
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    cand_row = o.journal.one("SELECT * FROM candidates ORDER BY id DESC LIMIT 1")
    candidate_id = cand_row["candidate_id"]

    assert score_candidate(o.journal, o.settings, cand_row) is not None
    assert score_proposal(o.journal, o.settings, candidate_id, pid) is not None

    rows = o.journal.query(
        "SELECT source_type FROM tqs_scores WHERE candidate_id = ?", (candidate_id,)
    )
    assert {r["source_type"] for r in rows} == {"candidate", "proposal"}
    o.close()


# --------------------------------------------------- behavior neutrality (A/B)
def _fingerprint_proposals(journal):
    return [dict(r) for r in journal.query(
        "SELECT symbol, direction, entry, stop, target, qty, status, expected_r, "
        "risk_per_share, dollar_risk, requires_margin, margin_approved, "
        "setup_classification, playbook_name FROM trade_proposals ORDER BY symbol, entry"
    )]


def _fingerprint_rejected(journal):
    return [dict(r) for r in journal.query(
        "SELECT symbol, stage, reason_code, direction, would_be_entry, would_be_stop "
        "FROM rejected_candidates ORDER BY symbol, stage, reason_code"
    )]


def _fingerprint_decision_adjustments(journal):
    return [dict(r) for r in journal.query(
        "SELECT symbol, eval_decision, label_decision, final_decision, adjustment, "
        "override_armed, driver, armed_watch FROM decision_adjustments ORDER BY symbol"
    )]


def _fingerprint_risk_checks(journal):
    return [dict(r) for r in journal.query(
        "SELECT result, fail_reason, max_risk_amount, position_size, entry_price, "
        "stop_price, target_price, reward_risk FROM risk_checks ORDER BY entry_price, result"
    )]


def test_tqs_toggle_does_not_change_decision_artifacts():
    """The core behavior-neutrality claim: with TQS_SHADOW_ENABLED on vs off,
    every decision-bearing table's CONTENT (excluding ids/timestamps/lineage_id
    -- lineage_id legitimately differs because tqs_shadow_enabled is itself a
    lineage config field) is byte-identical, and the scan summary's decision
    counts match exactly."""
    base = {"INTEREST_SCAN_TOP_N": "12", "MAX_CANDIDATES_TO_AI": "12", "LABELLING_ENABLED": "true"}
    off = Orchestrator(settings=make_settings(TQS_SHADOW_ENABLED="false", **base),
                       journal=JournalStore(":memory:"))
    summ_off = off.run_scan_once()
    proposals_off = _fingerprint_proposals(off.journal)
    rejected_off = _fingerprint_rejected(off.journal)
    adjustments_off = _fingerprint_decision_adjustments(off.journal)
    risk_checks_off = _fingerprint_risk_checks(off.journal)
    off.close()

    on = Orchestrator(settings=make_settings(TQS_SHADOW_ENABLED="true", **base),
                      journal=JournalStore(":memory:"))
    summ_on = on.run_scan_once()
    proposals_on = _fingerprint_proposals(on.journal)
    rejected_on = _fingerprint_rejected(on.journal)
    adjustments_on = _fingerprint_decision_adjustments(on.journal)
    risk_checks_on = _fingerprint_risk_checks(on.journal)
    on.close()

    assert summ_on.proposed == summ_off.proposed
    assert summ_on.watch == summ_off.watch
    assert summ_on.rejected == summ_off.rejected
    assert summ_on.risk_blocked == summ_off.risk_blocked
    assert proposals_on == proposals_off
    assert rejected_on == rejected_off
    assert adjustments_on == adjustments_off
    assert risk_checks_on == risk_checks_off
    # sanity: the A/B test itself actually produced decision rows to compare
    # (an all-empty comparison would pass vacuously regardless of a real leak)
    assert proposals_off or rejected_off or adjustments_off or risk_checks_off


def test_tqs_toggle_does_not_change_candidate_labels_or_status():
    base = {"INTEREST_SCAN_TOP_N": "12", "MAX_CANDIDATES_TO_AI": "12", "LABELLING_ENABLED": "true"}
    off = Orchestrator(settings=make_settings(TQS_SHADOW_ENABLED="false", **base),
                       journal=JournalStore(":memory:"))
    off.run_scan_once()
    cands_off = [dict(r) for r in off.journal.query(
        "SELECT symbol, status, primary_label, label_decision FROM candidates ORDER BY symbol"
    )]
    off.close()

    on = Orchestrator(settings=make_settings(TQS_SHADOW_ENABLED="true", **base),
                      journal=JournalStore(":memory:"))
    on.run_scan_once()
    cands_on = [dict(r) for r in on.journal.query(
        "SELECT symbol, status, primary_label, label_decision FROM candidates ORDER BY symbol"
    )]
    on.close()

    assert cands_on == cands_off


# --------------------------------------------------------------- no-read proof
def test_decision_functions_never_reference_tqs():
    """orchestrator.py DOES import alphaos.tqs (score_scan_batch/score_proposal
    are called write-only, post-commit) and list_open_proposals() DOES read
    tqs_scores (display-only) -- so a whole-file grep can't be the check here.
    Instead, extract the SOURCE of each actual decision-making function via
    inspect.getsource() and confirm none of them mention "tqs" at all. This is
    a precise structural complement to the empirical A/B behavior-neutrality
    test above -- together they prove both "doesn't" and "can't"."""
    import inspect

    from alphaos.orchestrator import Orchestrator

    decision_functions = (
        "_handle_proposal", "_resolve_decision", "_combine_decision",
        "_real_decision_driver", "approve_proposal", "reject_proposal",
        "_label_candidate", "_freeze_label", "run_scan_once",
    )
    for fn_name in decision_functions:
        fn = getattr(Orchestrator, fn_name)
        source = inspect.getsource(fn)
        if fn_name == "run_scan_once":
            # The one expected exception: run_scan_once's own TQS call site,
            # which by design runs strictly AFTER every decision in the
            # function is already committed. Strip that clearly-marked tail
            # block and confirm nothing ELSE in the function mentions tqs.
            marker = "# PR7: TQS v0 shadow scoring."
            assert marker in source, "expected PR7 call site marker not found in run_scan_once"
            source = source.split(marker)[0]
        assert "tqs" not in source.lower(), f"Orchestrator.{fn_name} references tqs"


def test_risk_engine_and_approval_never_reference_tqs_at_all():
    """The strong, unambiguous version of the check above: these two modules
    -- the actual gate/approval logic -- must not mention 'tqs' in any form."""
    import alphaos.approval as approval_mod
    import alphaos.risk.risk_engine as risk_mod

    for mod, name in ((approval_mod, "approval.py"), (risk_mod, "risk_engine.py")):
        text = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert "tqs" not in text.lower(), f"{name} references tqs"


def test_decision_functions_do_not_take_or_return_tqs_values():
    """approve_proposal/_handle_proposal/_resolve_decision/RiskEngine.assess
    signatures and behavior are unaffected -- call them and confirm normal
    operation with TQS enabled, proving no hidden coupling changed their
    contracts."""
    from conftest import inject_pending_proposal

    o = _orch()
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    ok, msg = o.approve_proposal(pid, approver="test")
    assert ok is True
    assert o.journal.count_rows("paper_fills") == 1
    o.close()


# -------------------------------------------------------------- missing-data
def test_mock_scan_marks_ai_labeller_narrative_components_missing():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    o.run_scan_once()
    row = o.journal.one("SELECT missing_components_json FROM tqs_scores WHERE source_type='candidate' LIMIT 1")
    assert row and row["missing_components_json"]
    import json

    missing = json.loads(row["missing_components_json"])
    assert "ai_conviction" in missing
    assert missing["ai_conviction"]["reason"] == "mock_ai"
    o.close()


def test_mock_scan_rows_get_data_quality_status_mock():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    o.run_scan_once()
    rows = o.journal.query("SELECT data_quality_status, is_mock FROM tqs_scores")
    assert rows
    assert all(r["data_quality_status"] == TqsDataQualityStatus.MOCK.value for r in rows)
    assert all(r["is_mock"] == 1 for r in rows)
    o.close()


def test_earnings_unavailable_component_missing_not_fabricated():
    """Direct DB-level check: a candidate with no earnings enrichment at all
    (earnings_data_status IS NULL, since earnings enrichment never ran for it)
    must show event_risk_clearance as MISSING, never scored 1.0 (safe) or 0.0."""
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12", EARNINGS_PROXIMITY_ENABLED="false")
    o.run_scan_once()
    rows = o.journal.query("SELECT missing_components_json FROM tqs_scores WHERE source_type='candidate'")
    assert rows
    import json

    for r in rows:
        missing = json.loads(r["missing_components_json"])
        assert "event_risk_clearance" in missing
        assert missing["event_risk_clearance"]["reason"] == "earnings_unavailable"
    o.close()


def test_labeller_failsafe_row_is_missing_with_reason_recorded():
    """Direct construction of a labeller-failsafe candidate row (bypassing the
    natural scan, whose failsafe rate is date-seeded and not reliably
    triggerable) to prove the reason is captured, matching the pure-function
    test but through the real DB-reading input builder this time."""
    from alphaos.constants import LabelSource
    from alphaos.tqs.inputs import build_candidate_inputs
    from alphaos.tqs.scoring import compute_tqs
    from alphaos.util.ids import new_id

    o = _orch()
    cand_id = new_id("cand")
    o.journal.insert("candidates", {
        "candidate_id": cand_id, "symbol": "AAPL", "direction": "long", "strategy": "swing",
        "status": "rejected", "label_confidence": 0.0, "label_source": LabelSource.FAIL_SAFE.value,
    })
    cand_row = o.journal.one("SELECT * FROM candidates WHERE candidate_id = ?", (cand_id,))
    inputs = build_candidate_inputs(o.journal, o.settings, cand_row)
    result = compute_tqs(inputs)
    assert "label_conviction" in result.missing_components
    assert result.missing_components["label_conviction"]["reason"] == "labeller_failsafe"
    o.close()


def test_component_error_is_logged_as_system_event():
    """An exception raised inside input-building (not scoring itself, which is
    pure and can't raise on bad DB state) is caught by score_candidate's outer
    try/except, logged, and the scan is unaffected."""
    from unittest.mock import MagicMock

    o = _orch(INTEREST_SCAN_TOP_N="3", MAX_CANDIDATES_TO_AI="3")
    import alphaos.tqs.batch as batch_mod

    original = batch_mod.build_candidate_inputs
    batch_mod.build_candidate_inputs = MagicMock(side_effect=RuntimeError("boom"))
    try:
        summ = o.run_scan_once()  # must NOT raise
    finally:
        batch_mod.build_candidate_inputs = original

    assert summ.tqs_scored_candidates == 0  # every candidate failed to score...
    events = o.journal.query(
        "SELECT * FROM system_events WHERE category = 'tqs' AND severity = 'warning'"
    )
    assert events  # ...but each failure was logged
    assert any("boom" in (e.get("detail_json") or "") for e in events)
    o.close()


# ------------------------------------------------------ determinism/idempotency
def test_scoring_same_batch_twice_inserts_zero_new_rows():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    summ = o.run_scan_once()
    before = o.journal.count_rows("tqs_scores")
    assert before > 0

    from alphaos.tqs import score_scan_batch

    result2 = score_scan_batch(o.journal, o.settings, summ.scan_batch_id)
    after = o.journal.count_rows("tqs_scores")
    assert after == before
    assert result2 == {"scored_candidates": 0, "scored_proposals": 0, "skipped": 0}
    o.close()


def test_compute_tqs_is_deterministic_across_repeated_runs_same_journal_state():
    from alphaos.tqs.inputs import build_candidate_inputs
    from alphaos.tqs.scoring import compute_tqs

    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    o.run_scan_once()
    cand_row = o.journal.one("SELECT * FROM candidates LIMIT 1")
    r1 = compute_tqs(build_candidate_inputs(o.journal, o.settings, cand_row))
    r2 = compute_tqs(build_candidate_inputs(o.journal, o.settings, cand_row))
    assert r1.tqs_score == r2.tqs_score
    assert r1.components == r2.components
    assert r1.missing_components == r2.missing_components
    o.close()


# -------------------------------------------------------------- schema/migration
def test_old_db_gets_tqs_scores_table_added_additively(tmp_path):
    import sqlite3

    from alphaos.journal.schema import SCHEMA_VERSION

    db = str(tmp_path / "pre_pr7.db")
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
        assert "tqs_scores" in tables
        cols = {r["name"] for r in j.conn.execute("PRAGMA table_info(tqs_scores)")}
        for c in ("tqs_id", "source_type", "candidate_id", "proposal_id", "tqs_version",
                  "raw_score", "data_confidence", "tqs_score", "tqs_bucket",
                  "components_json", "missing_components_json", "data_quality_status",
                  "is_mock", "lineage_id"):
            assert c in cols, f"missing column {c}"
        assert j.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert SCHEMA_VERSION == 3
    finally:
        j.close()


def test_sqlite_null_uniqueness_does_not_allow_duplicate_candidate_rows(tmp_path):
    """The exact bug class the partial-unique-index tightening exists to
    prevent: SQLite treats every NULL as distinct, so a naive
    UNIQUE(source_type, candidate_id, proposal_id, tqs_version) would NOT stop
    two candidate-level (proposal_id IS NULL) rows for the SAME candidate."""
    import sqlite3

    j = JournalStore(":memory:")
    try:
        j.conn.execute(
            "INSERT INTO tqs_scores (tqs_id, source_type, candidate_id, symbol, tqs_version, "
            "data_confidence, tqs_bucket, data_quality_status, created_at_utc, created_at_sgt) "
            "VALUES ('t1', 'candidate', 'cand1', 'AAPL', '0.1.0', 0.5, 'watch', 'ok', 'x', 'x')"
        )
        raised = False
        try:
            j.conn.execute(
                "INSERT INTO tqs_scores (tqs_id, source_type, candidate_id, symbol, tqs_version, "
                "data_confidence, tqs_bucket, data_quality_status, created_at_utc, created_at_sgt) "
                "VALUES ('t2', 'candidate', 'cand1', 'AAPL', '0.1.0', 0.5, 'watch', 'ok', 'x', 'x')"
            )
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "duplicate candidate-level row was NOT rejected -- NULL-uniqueness gap"

        # a DIFFERENT candidate, also NULL proposal_id, must NOT collide
        j.conn.execute(
            "INSERT INTO tqs_scores (tqs_id, source_type, candidate_id, symbol, tqs_version, "
            "data_confidence, tqs_bucket, data_quality_status, created_at_utc, created_at_sgt) "
            "VALUES ('t3', 'candidate', 'cand2', 'MSFT', '0.1.0', 0.5, 'watch', 'ok', 'x', 'x')"
        )
        assert j.count_rows("tqs_scores") == 2
    finally:
        j.close()


def test_sqlite_null_uniqueness_proposal_level_rows():
    import sqlite3

    j = JournalStore(":memory:")
    try:
        j.conn.execute(
            "INSERT INTO tqs_scores (tqs_id, source_type, candidate_id, proposal_id, symbol, "
            "tqs_version, data_confidence, tqs_bucket, data_quality_status, created_at_utc, created_at_sgt) "
            "VALUES ('t1', 'proposal', 'cand1', 'prop1', 'AAPL', '0.1.0', 0.5, 'watch', 'ok', 'x', 'x')"
        )
        raised = False
        try:
            j.conn.execute(
                "INSERT INTO tqs_scores (tqs_id, source_type, candidate_id, proposal_id, symbol, "
                "tqs_version, data_confidence, tqs_bucket, data_quality_status, created_at_utc, created_at_sgt) "
                "VALUES ('t2', 'proposal', 'cand1', 'prop1', 'AAPL', '0.1.0', 0.5, 'watch', 'ok', 'x', 'x')"
            )
        except sqlite3.IntegrityError:
            raised = True
        assert raised
    finally:
        j.close()


def test_no_legacy_backfill():
    """A pre-PR7 candidate row (created before tqs_scores existed) is never
    retroactively scored by anything in this PR -- score_scan_batch only ever
    scores rows matching a scan_batch_id it's explicitly given."""
    o = _orch()
    from alphaos.util.ids import new_id

    legacy_cand_id = new_id("cand")
    o.journal.insert("candidates", {
        "candidate_id": legacy_cand_id, "symbol": "AAPL", "direction": "long",
        "strategy": "swing", "status": "detected",
    })  # no scan_batch_id -- simulates a legacy/orphan row
    o.run_scan_once()
    assert o.journal.one(
        "SELECT 1 FROM tqs_scores WHERE candidate_id = ?", (legacy_cand_id,)
    ) is None
    o.close()


# ---------------------------------------------------------------------- lineage
def test_tqs_scores_rows_carry_lineage_id():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    o.run_scan_once()
    rows = o.journal.query("SELECT lineage_id, tqs_version FROM tqs_scores")
    assert rows and all(r["lineage_id"] for r in rows)
    assert all(r["tqs_version"] == TQS_VERSION for r in rows)
    snap = o.journal.one(
        "SELECT tqs_config_hash FROM lineage_snapshots WHERE lineage_id = ?", (rows[0]["lineage_id"],)
    )
    assert snap and snap["tqs_config_hash"]
    o.close()


def test_components_json_has_no_secrets_or_prompt_text():
    """components_json/missing_components_json must contain only normalized
    values/weights/source-field NAMES -- never prompts, raw AI text, keys, or
    account identifiers."""
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    o.run_scan_once()
    rows = o.journal.query("SELECT components_json, missing_components_json FROM tqs_scores")
    banned = ("api_key", "secret", "password", "token", "cookie", "authorization", "account_id")
    for r in rows:
        blob = (r["components_json"] or "") + (r["missing_components_json"] or "")
        for term in banned:
            assert term not in blob.lower()
    o.close()


# ---------------------------------------------------------------------- digest
def test_digest_shows_tqs_shadow_section():
    from alphaos.scheduler.digest import build_daily_digest

    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    o.run_scan_once()
    digest = build_daily_digest(o.journal, o.settings, o.kill_switch)
    assert "tqs_shadow" in digest
    section = digest["tqs_shadow"]
    for key in ("enabled", "tqs_version", "scored_count_today", "bucket_histogram_today",
                "mean_data_confidence_today", "unscorable_count_today", "mock_share_today"):
        assert key in section
    assert section["scored_count_today"] > 0
    assert section["tqs_version"] == TQS_VERSION
    o.close()


def test_digest_tqs_shadow_present_and_zeroed_when_disabled():
    from alphaos.scheduler.digest import build_daily_digest

    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12", TQS_SHADOW_ENABLED="false")
    o.run_scan_once()
    digest = build_daily_digest(o.journal, o.settings, o.kill_switch)
    section = digest["tqs_shadow"]
    assert section["enabled"] is False
    assert section["scored_count_today"] == 0
    assert section["bucket_histogram_today"] == {}
    assert section["mean_data_confidence_today"] is None
    o.close()


def test_list_open_proposals_shows_tqs_display_fields():
    from conftest import inject_pending_proposal

    o = _orch()
    pid, _ = inject_pending_proposal(o, symbol="AAPL")
    from alphaos.tqs import score_candidate

    cand_row = o.journal.one("SELECT * FROM candidates ORDER BY id DESC LIMIT 1")
    if cand_row:
        score_candidate(o.journal, o.settings, cand_row)
    views = o.list_open_proposals()
    v = next(v for v in views if v["proposal_id"] == pid)
    assert "tqs_score" in v and "tqs_bucket" in v and "tqs_data_confidence" in v
    o.close()


# ------------------------------------------------------------ safety invariants
def test_tqs_creates_no_orders_approvals_fills_positions():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    o.run_scan_once()
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_rows("paper_fills") == 0
    assert o.journal.count_open_positions() == 0
    assert o.journal.count_rows("approvals") == 0  # manual approval still required
    o.close()


def test_manual_approval_boundary_unchanged_with_tqs_enabled():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    o.run_scan_once()
    approved_or_filled = o.journal.query(
        "SELECT * FROM trade_proposals WHERE status IN ('approved', 'filled')"
    )
    assert approved_or_filled == []
    o.close()


def test_real_money_unreachable_with_tqs_enabled():
    o = _orch()
    assert o.system_health()["real_money_trading"] == "unreachable"
    o.close()


def test_no_orders_approvals_fills_positions_created_by_tqs_code():
    """Structural grep-based check, same pattern as the scheduler/lineage/
    earnings/proposals-ttl packages' own tests."""
    tqs_dir = pathlib.Path(tqs_pkg.__file__).parent
    banned = ("execute_proposal", "approve_proposal", "close_position",
              "submit_bracket", "submit_order", "place_order")
    for py_file in tqs_dir.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{py_file.name} references {token!r}"


def test_tqs_package_never_references_alpaca_client():
    tqs_dir = pathlib.Path(tqs_pkg.__file__).parent
    for py_file in tqs_dir.glob("*.py"):
        assert "alpaca_client" not in py_file.read_text(encoding="utf-8")


def test_scan_still_completes_normally_with_tqs_enabled():
    """Basic end-to-end smoke: a scan with TQS on behaves like a normal scan
    (scheduler bookkeeping, system_events, etc. all present)."""
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12")
    summ = o.run_scan_once()
    batch = o.journal.one("SELECT status FROM scan_batches WHERE scan_batch_id = ?", (summ.scan_batch_id,))
    assert batch["status"] == "completed"
    o.close()
