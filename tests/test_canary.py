"""CANARY: the model-drift canary (docs/roadmap/alphaos-pr-implementation-specs.md,
"## CANARY — Model-Drift Canary"). Weekly replay of a frozen prompt set
through the CURRENT PlaybookClassifier to detect silent upstream OpenAI model
changes -- distinct from EVAL-1 ("is this prompt better?"); CANARY only
answers "did the configured model change under us?". Zero decision surface.
HERMETIC throughout -- mock mode only, no real network calls.
"""

from __future__ import annotations

import json

import pytest

from alphaos.canary.corpus import load_corpus, select_seed_packets, write_corpus
from alphaos.canary.run import (
    DRIFT_NONE, DRIFT_TIER_1, DRIFT_TIER_2, DRIFT_TIER_3,
    _compute_drift, get_baseline_run, pin_baseline, run_canary,
)
from alphaos.journal.journal_store import JournalStore
from alphaos.scheduler import cadence, cost_guard
from alphaos.scheduler.job_runner import JobRunner, _JOB_FUNCS
from alphaos.scheduler.jobs import run_canary_run_job
from alphaos.util.ids import new_id
from conftest import make_settings

_FIXTURE = {
    "packet_id": "pkt_canarytest01", "candidate_id": "cand_canarytest01", "interest_rank": 1,
    "symbol": "AAPL", "last_price": 100.0, "direction": "long", "freshness_status": "usable",
    "spread_pct": 0.001, "liquidity_ok": True, "dollar_volume": 1_000_000.0, "change_pct": 0.02,
    "rel_volume": 1.5, "rel_strength_vs_spy": 0.01, "rel_strength_vs_qqq": 0.01,
    "near_day_high": True, "near_day_low": False, "gap_pct": 0.0, "structure_hint": "breakout",
    "setup_hint": "x", "tradeable_volatility": True, "interest_score": 0.7,
    "shortlist_reason": "x", "momentum_score": 0.6, "missing_data_flags": [],
}


# ------------------------------------------------------------------- corpus
def test_load_corpus_empty_when_never_built(tmp_path):
    manifest, packets = load_corpus(str(tmp_path / "does_not_exist"))
    assert manifest is None
    assert packets == []


def test_write_corpus_is_additive_never_overwrites(tmp_path):
    corpus_dir = str(tmp_path / "corpus")
    manifest1, written1 = write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-10")
    assert written1 == ["pkt_canarytest01.json"]
    assert manifest1["version"] == 1

    mutated = {**_FIXTURE, "symbol": "MUTATED"}
    manifest2, written2 = write_corpus(corpus_dir, [mutated], as_of_date="2026-07-11")
    assert written2 == []  # same packet_id -- never overwritten
    _, packets = load_corpus(corpus_dir)
    assert packets[0]["symbol"] == "AAPL"  # original content survives


def test_write_corpus_manifest_sha256_matches_file_content(tmp_path):
    corpus_dir = str(tmp_path / "corpus")
    manifest, _ = write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-10")
    import hashlib
    with open(f"{corpus_dir}/pkt_canarytest01.json", "rb") as f:
        content = f.read()
    expected_sha = hashlib.sha256(content).hexdigest()
    entry = next(e for e in manifest["packets"] if e["file"] == "pkt_canarytest01.json")
    assert entry["sha256"] == expected_sha


def test_write_corpus_refuses_malformed_packet_id(tmp_path):
    with pytest.raises(ValueError):
        write_corpus(str(tmp_path / "corpus"), [{**_FIXTURE, "packet_id": "../../etc/passwd"}],
                     as_of_date="2026-07-10")


def test_select_seed_packets_prefers_task_r_relabels(journal):
    """spec: 'prefer TASK-R's relabelled seven plus a spread across symbols'."""
    since = "2026-07-06T15:00:00+00:00"
    for i, (symbol, is_relabel) in enumerate([("AAPL", False), ("MSFT", True), ("AMD", False)]):
        packet_id = f"pkt_seed{i:02d}"
        journal.insert("candidate_packets", {
            "packet_id": packet_id, "candidate_id": f"cand_seed{i:02d}",
            "interest_rank": 1, "symbol": symbol,
            "packet_json": json.dumps({"symbol": symbol}),
            "created_at_utc": since, "created_at_sgt": since,
        })
        journal.insert("candidate_labels", {
            "label_id": new_id("lbl"), "packet_id": packet_id, "candidate_id": f"cand_seed{i:02d}",
            "symbol": symbol, "primary_label": "Momentum", "label_decision": "watch",
            "is_mock": 0, "relabel_of": (new_id("orig") if is_relabel else None),
            "created_at_utc": since, "created_at_sgt": since,
        })

    seeds = select_seed_packets(journal, limit=3)
    assert seeds[0]["symbol"] == "MSFT"  # the relabelled one sorts first
    assert seeds[0]["provenance"]["is_task_r_relabel"] is True
    assert {s["symbol"] for s in seeds} == {"AAPL", "MSFT", "AMD"}


# --------------------------------------------------------------------- run
def test_run_canary_empty_corpus_is_a_safe_no_op(tmp_path, journal):
    settings = make_settings()
    result = run_canary(journal, settings, corpus_dir=str(tmp_path / "empty"))
    assert result["n_packets"] == 0
    assert "error" in result
    assert journal.count_rows("canary_runs", "1=1") == 0


def test_run_canary_mock_happy_path_writes_run_and_results(tmp_path, journal):
    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-10")

    result = run_canary(journal, settings, corpus_dir=corpus_dir)

    assert result["n_packets"] == 1
    assert result["n_results"] == 1
    assert result["n_corpus_errors"] == 0
    assert result["drift_tier"] == DRIFT_NONE  # no baseline pinned yet
    run_row = journal.one("SELECT * FROM canary_runs WHERE run_id = ?", (result["run_id"],))
    assert run_row is not None
    assert run_row["is_mock"] == 1
    assert run_row["finished_at_utc"] is not None
    result_row = journal.one("SELECT * FROM canary_results WHERE run_id = ?", (result["run_id"],))
    assert result_row["packet_id"] == "pkt_canarytest01"


def test_run_canary_isolates_one_bad_fixture(tmp_path, journal):
    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    good = _FIXTURE
    bad = {**_FIXTURE, "packet_id": "pkt_canarytest02", "candidate_id": "cand_canarytest02",
           "momentum_score": "not_a_number"}  # wrong TYPE -- reconstructs fine, breaks mock classify's float()
    write_corpus(corpus_dir, [good, bad], as_of_date="2026-07-10")

    result = run_canary(journal, settings, corpus_dir=corpus_dir)

    assert result["n_packets"] == 2
    assert result["n_results"] == 1
    assert result["n_corpus_errors"] == 1


def test_run_canary_refuses_when_cost_cap_reached(tmp_path, journal, monkeypatch):
    settings = make_settings(
        OPENAI_API_KEY="sk-test", ALPHAOS_MODE="paper",
        SCHEDULER_AI_COST_CAP_CALLS_PER_30D="50",
    )
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-10")
    monkeypatch.setattr(cost_guard, "calls_in_last_30_days", lambda journal: 50)

    result = run_canary(journal, settings, corpus_dir=corpus_dir)

    assert "error" in result
    assert "cost cap" in result["error"]
    assert journal.count_rows("canary_runs", "1=1") == 0


# ---------------------------------------------------------------- baseline
def test_pin_baseline_marks_run_and_demotes_previous(tmp_path, journal):
    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-10")

    run1 = run_canary(journal, settings, corpus_dir=corpus_dir)
    run2 = run_canary(journal, settings, corpus_dir=corpus_dir)

    pin_baseline(journal, run1["run_id"])
    assert get_baseline_run(journal)["run_id"] == run1["run_id"]

    pin_baseline(journal, run2["run_id"])  # re-pin demotes the old one
    baseline = get_baseline_run(journal)
    assert baseline["run_id"] == run2["run_id"]
    assert journal.count_rows("canary_runs", "is_baseline = 1") == 1


def test_pin_baseline_unknown_run_id_returns_error(journal):
    result = pin_baseline(journal, "nonexistent_run_id")
    assert "error" in result


def test_run_canary_against_pinned_baseline_reports_no_drift_when_identical(tmp_path, journal):
    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-10")

    run1 = run_canary(journal, settings, corpus_dir=corpus_dir)
    pin_baseline(journal, run1["run_id"])
    run2 = run_canary(journal, settings, corpus_dir=corpus_dir)

    assert run2["drift_tier"] == DRIFT_NONE
    assert run2["drift_detail"] == {}


# ------------------------------------------------------------ drift tiers
def _agg(n_prompts=10, n_failsafe=0, models=("gpt-5.1",), fps=("fp_abc",), mean_conf=0.7):
    return {
        "n_prompts": n_prompts, "n_parse_or_failsafe": n_failsafe,
        "response_models_json": json.dumps(sorted(models)),
        "system_fingerprints_json": json.dumps(sorted(fps)),
        "mean_confidence": mean_conf,
    }


def _baseline_row(**overrides):
    row = {"run_id": "baserun", "n_prompts": 10, "n_parse_or_failsafe": 0,
           "response_models_json": json.dumps(["gpt-5.1"]),
           "system_fingerprints_json": json.dumps(["fp_abc"]), "mean_confidence": 0.7}
    row.update(overrides)
    return row


def test_compute_drift_none_when_no_baseline_pinned():
    tier, detail = _compute_drift(_agg(), {}, None, {}, 0.20, 0.15)
    assert tier == DRIFT_NONE
    assert "no baseline pinned" in detail["reason"]


def test_compute_drift_tier1_on_response_model_change():
    current = _agg(models=("gpt-5.2",))
    tier, detail = _compute_drift(current, {}, _baseline_row(), {}, 0.20, 0.15)
    assert tier == DRIFT_TIER_1
    assert "identity_change" in detail


def test_compute_drift_tier1_on_system_fingerprint_change():
    current = _agg(fps=("fp_xyz",))
    tier, detail = _compute_drift(current, {}, _baseline_row(), {}, 0.20, 0.15)
    assert tier == DRIFT_TIER_1
    assert "identity_change" in detail


def test_compute_drift_tier1_on_failsafe_rate_appearing():
    current = _agg(n_failsafe=2)  # baseline had 0, current has 2/10 = nonzero
    tier, detail = _compute_drift(current, {}, _baseline_row(n_parse_or_failsafe=0), {}, 0.20, 0.15)
    assert tier == DRIFT_TIER_1
    assert "failsafe_rate_change" in detail


def test_compute_drift_never_flags_absent_fingerprint_as_changed():
    """A model that never sends system_fingerprint on either side must not
    be mistaken for 'changed to nothing'."""
    current = _agg(fps=())
    tier, _ = _compute_drift(current, {}, _baseline_row(system_fingerprints_json=json.dumps([])), {}, 0.20, 0.15)
    assert tier != DRIFT_TIER_1


def test_compute_drift_tier2_on_label_mismatch_at_or_above_threshold():
    current_by_packet = {
        "p1": {"primary_label": "Breakout", "label_decision": "watch", "label_confidence": 0.7},
        "p2": {"primary_label": "Momentum", "label_decision": "watch", "label_confidence": 0.7},
        "p3": {"primary_label": "Breakout", "label_decision": "watch", "label_confidence": 0.7},
    }
    baseline_by_packet = {
        "p1": {"primary_label": "Momentum", "label_decision": "watch", "label_confidence": 0.7},  # mismatch
        "p2": {"primary_label": "Momentum", "label_decision": "watch", "label_confidence": 0.7},
        "p3": {"primary_label": "Momentum", "label_decision": "watch", "label_confidence": 0.7},  # mismatch
    }
    current = _agg(n_prompts=3)
    tier, detail = _compute_drift(current, current_by_packet, _baseline_row(n_prompts=3),
                                  baseline_by_packet, 0.5, 0.15)
    assert tier == DRIFT_TIER_2
    assert detail["label_drift"]["label_mismatches"] == 2


def test_compute_drift_tier2_below_threshold_falls_through_to_none_or_tier3():
    current_by_packet = {
        "p1": {"primary_label": "Breakout", "label_decision": "watch", "label_confidence": 0.7},
        "p2": {"primary_label": "Momentum", "label_decision": "watch", "label_confidence": 0.7},
    }
    baseline_by_packet = {
        "p1": {"primary_label": "Momentum", "label_decision": "watch", "label_confidence": 0.7},  # 1/2 = 50%
        "p2": {"primary_label": "Momentum", "label_decision": "watch", "label_confidence": 0.7},
    }
    current = _agg(n_prompts=2, mean_conf=0.7)
    tier, _ = _compute_drift(current, current_by_packet, _baseline_row(n_prompts=2, mean_confidence=0.7),
                             baseline_by_packet, 0.6, 0.15)  # 50% < 60% threshold
    assert tier != DRIFT_TIER_2


def test_compute_drift_tier3_on_confidence_shift_beyond_band():
    current = _agg(mean_conf=0.3)  # baseline 0.7, shift = 0.4 > band 0.15
    tier, detail = _compute_drift(current, {}, _baseline_row(mean_confidence=0.7), {}, 0.20, 0.15)
    assert tier == DRIFT_TIER_3
    assert detail["confidence_shift"]["shift"] == pytest.approx(0.4)


def test_compute_drift_none_when_everything_within_band():
    current = _agg(mean_conf=0.72)  # baseline 0.7, shift = 0.02 < band 0.15
    tier, _ = _compute_drift(current, {}, _baseline_row(mean_confidence=0.7), {}, 0.20, 0.15)
    assert tier == DRIFT_NONE


def test_compute_drift_corpus_growth_only_compares_intersection():
    """A packet present in the current run but not the baseline (corpus grew
    since baseline was pinned) must be skipped, never treated as a mismatch."""
    current_by_packet = {
        "p1": {"primary_label": "Breakout", "label_decision": "watch", "label_confidence": 0.7},
        "p_new": {"primary_label": "Momentum", "label_decision": "watch", "label_confidence": 0.7},
    }
    baseline_by_packet = {
        "p1": {"primary_label": "Breakout", "label_decision": "watch", "label_confidence": 0.7},
    }
    current = _agg(n_prompts=2)
    tier, _ = _compute_drift(current, current_by_packet, _baseline_row(n_prompts=1),
                             baseline_by_packet, 0.20, 0.15)
    assert tier == DRIFT_NONE


# ---------------------------------------------------------- alerting/report
def test_run_canary_sends_high_priority_alert_on_tier1_drift(tmp_path, journal, monkeypatch):
    """Mock-mode classify() never produces a real response_model/
    system_fingerprint (there's no live call to observe), so organic drift
    can't be produced through the mock path -- this test instead verifies
    the ALERT-WIRING itself: given _compute_drift says Tier 1/2, does
    run_canary actually call send_alert with priority=high? (The drift-
    computation logic itself is unit-tested directly against
    _compute_drift, above, independent of the mock/live classifier path.)"""
    settings = make_settings(NTFY_TOPIC="test-topic")
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-10")
    run1 = run_canary(journal, settings, corpus_dir=corpus_dir)
    pin_baseline(journal, run1["run_id"])

    sent = []
    import alphaos.canary.run as run_module
    monkeypatch.setattr(run_module, "_compute_drift", lambda *a, **kw: (DRIFT_TIER_1, {"forced": "for test"}))
    monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: sent.append(kw) or True)

    run_canary(journal, settings, corpus_dir=corpus_dir)

    assert len(sent) == 1
    assert sent[0]["priority"] == "high"


def test_canary_report_no_runs_yet(journal):
    from alphaos.reports.canary_report import build_canary_report

    rep = build_canary_report(journal)
    assert rep["status"] == "no_runs_yet"


def test_canary_report_reflects_latest_run(tmp_path, journal):
    from alphaos.reports.canary_report import build_canary_report

    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-10")
    result = run_canary(journal, settings, corpus_dir=corpus_dir)

    rep = build_canary_report(journal)
    assert rep["status"] == "ok"
    assert rep["run_id"] == result["run_id"]
    assert rep["baseline_pinned"] is False


# -------------------------------------------------------------- daily brief
def test_canary_health_none_when_no_runs(journal):
    from alphaos.reports.daily_brief import _canary_health

    assert _canary_health(journal) is None


def test_canary_health_populated_after_a_run(tmp_path, journal):
    from alphaos.reports.daily_brief import _canary_health

    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-10")
    run_canary(journal, settings, corpus_dir=corpus_dir)

    health = _canary_health(journal)
    assert health is not None
    assert health["status"] == "ok"


def test_render_markdown_includes_canary_section_when_present(tmp_path, orchestrator):
    from alphaos.reports.daily_brief import build_daily_brief, render_markdown

    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-10")
    run_canary(orchestrator.journal, orchestrator.settings, corpus_dir=corpus_dir)

    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    md = render_markdown(brief)

    assert "## Canary (model-drift)" in md


# ------------------------------------------------------------ scheduler wiring
def test_canary_run_in_default_lock_key_once_weekly_group(settings):
    key = cadence.default_lock_key(cadence.JobType.CANARY_RUN, settings)
    assert key.startswith("canary_run:")


def test_canary_run_is_due_dispatch_wired(journal, settings):
    due, reason = cadence.is_due(cadence.JobType.CANARY_RUN, settings, journal)
    assert isinstance(due, bool)
    assert isinstance(reason, str)


def test_canary_run_in_job_funcs_dispatch_table():
    assert cadence.JobType.CANARY_RUN in _JOB_FUNCS
    assert _JOB_FUNCS[cadence.JobType.CANARY_RUN] is run_canary_run_job


def test_canary_run_in_run_due_jobs_and_status_report(orchestrator, monkeypatch):
    monkeypatch.setattr(cadence, "is_due", lambda job_type, settings, journal, now=None: (True, "forced for test"))

    results = JobRunner(orchestrator).run_due_jobs()

    by_type = {r["job_type"]: r for r in results}
    assert cadence.JobType.CANARY_RUN in by_type
    # CANARY_ENABLED defaults false -- dispatched, but the job itself no-ops.
    assert by_type[cadence.JobType.CANARY_RUN]["status"] == "skipped"

    report = JobRunner(orchestrator).status_report()
    assert "canary_run" in report["recent_by_job_type"]


def test_run_canary_run_job_skips_when_disabled(orchestrator):
    assert orchestrator.settings.canary_enabled is False
    result = run_canary_run_job(orchestrator, JobRunner(orchestrator))
    assert result["status"] == "skipped"


def test_only_weekday_and_at_or_after_time_is_due(journal):
    """A Monday (weekday=0) with Sunday (weekday=6) configured must not be due,
    regardless of time of day."""
    settings = make_settings(SCHEDULER_CANARY_RUN_WEEKDAY="6", SCHEDULER_CANARY_RUN_TIME="10:00")
    # 2026-07-06 is a Monday (weekday=0).
    from alphaos.util import timeutils
    monday_noon_sgt = timeutils.parse_iso("2026-07-06T12:00:00+08:00")
    due, reason = cadence.is_due(cadence.JobType.CANARY_RUN, settings, journal, now=monday_noon_sgt)
    assert due is False
    assert "weekday" in reason


def test_due_on_configured_weekday_at_or_after_time(journal):
    settings = make_settings(SCHEDULER_CANARY_RUN_WEEKDAY="6", SCHEDULER_CANARY_RUN_TIME="10:00")
    # 2026-07-05 is a Sunday (weekday=6).
    from alphaos.util import timeutils
    sunday_1030_sgt = timeutils.parse_iso("2026-07-05T10:30:00+08:00")
    due, reason = cadence.is_due(cadence.JobType.CANARY_RUN, settings, journal, now=sunday_1030_sgt)
    assert due is True


def test_due_on_configured_weekday_before_time_is_not_due(journal):
    settings = make_settings(SCHEDULER_CANARY_RUN_WEEKDAY="6", SCHEDULER_CANARY_RUN_TIME="10:00")
    from alphaos.util import timeutils
    sunday_early_sgt = timeutils.parse_iso("2026-07-05T09:00:00+08:00")
    due, reason = cadence.is_due(cadence.JobType.CANARY_RUN, settings, journal, now=sunday_early_sgt)
    assert due is False


def test_canary_run_only_fires_once_per_week(journal):
    settings = make_settings(SCHEDULER_CANARY_RUN_WEEKDAY="6", SCHEDULER_CANARY_RUN_TIME="10:00")
    from alphaos.util import timeutils
    sunday_1030_sgt = timeutils.parse_iso("2026-07-05T10:30:00+08:00")
    lock_key = cadence.default_lock_key(cadence.JobType.CANARY_RUN, settings, now=sunday_1030_sgt)
    journal.insert("job_runs", {
        "job_run_id": new_id("jr"), "job_type": cadence.JobType.CANARY_RUN, "lock_key": lock_key,
        "status": "completed", "trigger_source": "scheduler",
        "started_at_utc": "2026-07-05T02:30:00+00:00", "started_at_sgt": "2026-07-05T10:30:00+08:00",
    })
    due, reason = cadence.is_due(cadence.JobType.CANARY_RUN, settings, journal, now=sunday_1030_sgt)
    assert due is False
    assert "already completed this week" in reason


# ------------------------------------------------------------ settings/config
def test_canary_enabled_defaults_false(settings):
    assert settings.canary_enabled is False


def test_canary_run_weekday_out_of_range_rejected():
    with pytest.raises(Exception):
        make_settings(SCHEDULER_CANARY_RUN_WEEKDAY="7")


def test_canary_tier2_label_diff_pct_out_of_range_rejected():
    with pytest.raises(Exception):
        make_settings(CANARY_TIER2_LABEL_DIFF_PCT="0")


def test_canary_config_hash_changes_with_its_own_settings_but_not_others():
    from alphaos.lineage.config_snapshot import build_config_hashes

    base = make_settings()
    changed = make_settings(CANARY_TIER2_LABEL_DIFF_PCT="0.5")

    h_base = build_config_hashes(base)
    h_changed = build_config_hashes(changed)
    assert h_base["canary_config_hash"] != h_changed["canary_config_hash"]
    assert h_base["scanner_config_hash"] == h_changed["scanner_config_hash"]
    assert h_base["risk_config_hash"] == h_changed["risk_config_hash"]


# ----------------------------------------------------------- cost accounting
def test_cost_guard_counts_canary_results_from_non_mock_runs_only(journal):
    since = "2026-07-09T00:00:00+00:00"
    journal.insert("canary_runs", {
        "run_id": "run_live", "corpus_dir": "data/canary", "is_mock": 0, "n_prompts": 1,
        "started_at_utc": since, "started_at_sgt": since,
    })
    journal.insert("canary_runs", {
        "run_id": "run_mock", "corpus_dir": "data/canary", "is_mock": 1, "n_prompts": 1,
        "started_at_utc": since, "started_at_sgt": since,
    })
    journal.insert("canary_results", {
        "result_id": new_id("cres"), "run_id": "run_live", "packet_id": "p1",
        "created_at_utc": since, "created_at_sgt": since,
    })
    journal.insert("canary_results", {
        "result_id": new_id("cres"), "run_id": "run_mock", "packet_id": "p2",
        "created_at_utc": since, "created_at_sgt": since,
    })

    count = cost_guard.calls_in_last_30_days(journal)
    assert count == 1  # only the non-mock run's result counts


# -------------------------------------------------------- playbook extension
def test_playbook_classification_response_meta_none_in_mock_mode(settings):
    from alphaos.ai.playbook_classifier import PlaybookClassifier
    from alphaos.scanner.candidate_packet import reconstruct_from_stored

    packet = reconstruct_from_stored("pkt_x", "cand_x", 1, _FIXTURE)
    classification = PlaybookClassifier(settings, journal=None).classify(packet)
    assert classification.response_model is None
    assert classification.system_fingerprint is None


# -------------------------------------------------------------- schema/lineage
def test_old_db_gets_canary_tables_added_additively(tmp_path):
    db_path = tmp_path / "pre_canary.db"
    j1 = JournalStore(str(db_path))
    j1.conn.execute("DROP TABLE IF EXISTS canary_runs")
    j1.conn.execute("DROP TABLE IF EXISTS canary_results")
    j1.conn.execute("DROP INDEX IF EXISTS idx_canary_runs_started")
    j1.conn.commit()
    j1.close()

    j2 = JournalStore(str(db_path))  # re-opening must additively recreate them
    cols = j2._cols("canary_runs")
    for expected in ("run_id", "configured_model", "is_baseline", "drift_tier", "n_prompts"):
        assert expected in cols, f"missing column {expected}"
    result_cols = j2._cols("canary_results")
    for expected in ("result_id", "run_id", "packet_id", "response_model", "system_fingerprint"):
        assert expected in result_cols, f"missing column {expected}"
    idx = {r["name"] for r in j2.query(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='canary_runs'")}
    assert "idx_canary_runs_started" in idx
    j2.close()


def test_old_db_gets_canary_config_hash_column_added_additively(tmp_path):
    db_path = tmp_path / "pre_canary_lineage.db"
    j1 = JournalStore(str(db_path))
    j1.close()
    raw = __import__("sqlite3").connect(str(db_path))
    raw.execute("ALTER TABLE lineage_snapshots RENAME TO lineage_snapshots_old")
    raw.execute(
        "CREATE TABLE lineage_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "lineage_id TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, created_at_sgt TEXT NOT NULL)"
    )
    raw.commit()
    raw.close()

    j2 = JournalStore(str(db_path))
    cols = j2._cols("lineage_snapshots")
    assert "canary_config_hash" in cols
    j2.close()
