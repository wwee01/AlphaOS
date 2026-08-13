"""CANARY: the model-drift canary (docs/roadmap/alphaos-pr-implementation-specs.md,
"## CANARY — Model-Drift Canary"). Weekly replay of a frozen prompt set
through the CURRENT PlaybookClassifier to detect silent upstream OpenAI model
changes -- distinct from EVAL-1 ("is this prompt better?"); CANARY only
answers "did the configured model change under us?". Zero decision surface.
HERMETIC throughout -- mock mode only, no real network calls.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest

from alphaos.canary.corpus import (
    CorpusTamperedError, load_corpus, select_seed_packets, write_corpus,
)
from alphaos.canary.run import (
    CANARY_BOUNDARY_PACKETS_V1, CANARY_STABLE_PACKETS_V1, CANARY_TRIGGER_FAILSAFE,
    CANARY_TRIGGER_IDENTITY, DRIFT_NONE, DRIFT_TIER_1, DRIFT_TIER_2, DRIFT_TIER_3,
    _compute_drift, _json_set, _label_comparison, get_baseline_run, pin_baseline,
    run_canary, run_canary_confirmed,
)
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.scheduler import cadence, cost_guard
from alphaos.scheduler.job_runner import JobRunner, _JOB_FUNCS
from alphaos.scheduler.jobs import run_canary_run_job
from alphaos.util import timeutils
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


def test_load_corpus_detects_a_tampered_fixture(tmp_path):
    """audit MEDIUM (correctness, 2026-07-10) -- the spec's own test list
    explicitly requires this: 'corpus tamper (sha mismatch) -> loud job
    failure, fuse-eligible'. A fixture file modified on disk AFTER
    write_corpus() froze its MANIFEST sha256 must be caught, never silently
    replayed as if nothing changed."""
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-10")

    tampered = {**_FIXTURE, "symbol": "TAMPERED"}
    with open(os.path.join(corpus_dir, "pkt_canarytest01.json"), "w", encoding="utf-8") as f:
        json.dump(tampered, f)

    with pytest.raises(CorpusTamperedError, match="sha256"):
        load_corpus(corpus_dir)


def test_load_corpus_untampered_fixtures_load_normally(tmp_path):
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-10")

    manifest, packets = load_corpus(corpus_dir)

    assert manifest is not None
    assert len(packets) == 1


def test_run_canary_propagates_corpus_tampered_error_uncaught(tmp_path, journal):
    """The tamper error must NOT be swallowed into a returned {"error": ...}
    dict -- only an uncaught exception reaching JobRunner.run_job's own
    handler marks job_runs 'failed' (fuse-eligible); a returned error key
    would be wrapped as 'completed' instead (see run_canary_run_job)."""
    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-10")
    with open(os.path.join(corpus_dir, "pkt_canarytest01.json"), "w", encoding="utf-8") as f:
        json.dump({**_FIXTURE, "symbol": "TAMPERED"}, f)

    with pytest.raises(CorpusTamperedError):
        run_canary(journal, settings, corpus_dir=corpus_dir)


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

    # audit LOW (2026-07-10): canary_runs.n_prompts must be the FULL corpus
    # size (2), not just the successful-classification count (1) -- the
    # failsafe-rate denominator on the baseline side of a future
    # _compute_drift call reads this same column, and it must mean the
    # same thing there as it does here.
    run_row = journal.one("SELECT n_prompts FROM canary_runs WHERE run_id = ?", (result["run_id"],))
    assert run_row["n_prompts"] == 2


def test_run_canary_refuses_when_cost_cap_reached(tmp_path, journal, monkeypatch):
    settings = make_settings(
        OPENAI_API_KEY="sk-test", ALPHAOS_MODE="paper",
        SCHEDULER_AI_COST_CAP_CALLS_PER_30D="50",
        SHADOW_AI_CAP_CALLS_PER_30D="12",  # EXP-1's own joint-validation must clear this cap too
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
    # CANARY-2 Design 3: the stable/boundary split is computed "wherever
    # mismatches are computed ... including clean runs" -- so drift_detail is
    # no longer empty on an identical-to-baseline run, it carries a
    # zero-mismatch label_drift block instead.
    assert run2["drift_detail"]["label_drift"]["label_mismatches"] == 0
    assert run2["drift_detail"]["label_drift"]["tripped"] is False


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


def test_json_set_never_raises_on_null_or_missing_values():
    """audit NIT (correctness, 2026-07-10): json.loads("null") returns None,
    and set(None) raises TypeError -- _json_set must tolerate every falsy
    shape a stored column could hold (not reachable via any current write
    path, but a DB row should never be able to crash a read)."""
    assert _json_set(None) == set()
    assert _json_set("") == set()
    assert _json_set("null") == set()
    assert _json_set("[]") == set()
    assert _json_set('["a", "b"]') == {"a", "b"}


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


# ---------------------------------------------------- CANARY-2: confirmation
# docs/roadmap/alphaos-canary2-drift-confirmation-spec.md's 14 numbered tests,
# one alphaos_test function per spec test (multiple functions for a couple
# that cover two scenarios/assertions the spec bundles into one bullet).

def _seed_pinned_baseline(journal, settings, corpus_dir):
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-10")
    run1 = run_canary(journal, settings, corpus_dir=corpus_dir)
    pin_baseline(journal, run1["run_id"])
    return run1


def _label_drift_detail(label_mismatches, stable_flips, boundary_flips, stable_total=13, boundary_total=7):
    compared = stable_total + boundary_total
    return {
        "label_drift": {
            "compared": compared, "label_mismatches": label_mismatches,
            "label_mismatch_rate": round(label_mismatches / compared, 3) if compared else 0.0,
            "decision_mismatches": 0, "decision_mismatch_rate": 0.0, "threshold": 0.2,
            "stable_flips": stable_flips, "stable_total": stable_total,
            "boundary_flips": boundary_flips, "boundary_total": boundary_total,
            "unclassified_new": 0, "unclassified_new_flips": 0, "tripped": True,
        },
    }


def test_canary2_identity_tier1_pages_immediately_no_confirmation(tmp_path, journal, monkeypatch):
    """Spec test 1: identity-triggered TIER_1 pages immediately; NO
    confirmation run executed; alert body carries the identity diff."""
    settings = make_settings(NTFY_TOPIC="test-topic")
    corpus_dir = str(tmp_path / "corpus")
    _seed_pinned_baseline(journal, settings, corpus_dir)

    import alphaos.canary.run as run_module
    forced_detail = {
        "trigger_class": CANARY_TRIGGER_IDENTITY,
        "identity_change": {"baseline_response_models": ["gpt-5.1"], "current_response_models": ["gpt-5.2"]},
    }
    monkeypatch.setattr(run_module, "_compute_drift", lambda *a, **kw: (DRIFT_TIER_1, forced_detail))
    sent = []
    monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: sent.append(kw) or True)

    result = run_canary_confirmed(journal, settings, corpus_dir=corpus_dir)

    assert result["paged"] is True
    assert result["confirmation_run_id"] is None
    assert len(sent) == 1
    assert sent[0]["priority"] == "high"
    assert "gpt-5.2" in sent[0]["message"]
    # No confirmation run was spawned: only the baseline-seed row + this trigger row exist.
    assert journal.count_rows("canary_runs", "1=1") == 2
    assert journal.count_rows("canary_runs", "confirmation_of IS NOT NULL") == 0


def test_canary2_failsafe_tier1_confirms_before_paging(tmp_path, journal, monkeypatch):
    """Spec test 2: failsafe-only TIER_1 -- no separate page fires at the
    trip moment; exactly one confirmation run is executed; confirmation_of
    is stamped on the second row."""
    settings = make_settings(NTFY_TOPIC="test-topic")
    corpus_dir = str(tmp_path / "corpus")
    _seed_pinned_baseline(journal, settings, corpus_dir)

    import alphaos.canary.run as run_module
    forced_detail = {
        "trigger_class": CANARY_TRIGGER_FAILSAFE,
        "failsafe_rate_change": {"baseline_rate": 0.0, "current_rate": 0.1},
    }
    monkeypatch.setattr(run_module, "_compute_drift", lambda *a, **kw: (DRIFT_TIER_1, forced_detail))
    sent = []
    monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: sent.append(kw) or True)

    result = run_canary_confirmed(journal, settings, corpus_dir=corpus_dir)

    confirmation_rows = journal.query(
        "SELECT * FROM canary_runs WHERE confirmation_of = ?", (result["run_id"],)
    )
    assert len(confirmation_rows) == 1
    assert result["confirmation_run_id"] == confirmation_rows[0]["run_id"]
    # exactly ONE page overall (the confirmed verdict) -- not one at the trip and one at confirmation.
    assert len(sent) == 1


def test_canary2_tier2_trip_clean_confirmation_no_alert(tmp_path, journal, monkeypatch):
    """Spec test 3: TIER_2 trip + clean confirmation -> zero alerts; a
    "not confirmed" event is journaled naming both run ids; both runs
    persist in canary_runs (nothing is deleted)."""
    settings = make_settings(NTFY_TOPIC="test-topic")
    corpus_dir = str(tmp_path / "corpus")
    _seed_pinned_baseline(journal, settings, corpus_dir)

    import alphaos.canary.run as run_module
    responses = iter([(DRIFT_TIER_2, _label_drift_detail(4, 0, 4)), (DRIFT_NONE, {})])
    monkeypatch.setattr(run_module, "_compute_drift", lambda *a, **kw: next(responses))
    sent = []
    monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: sent.append(kw) or True)

    result = run_canary_confirmed(journal, settings, corpus_dir=corpus_dir)

    assert sent == []
    assert result["paged"] is False
    assert result["confirmed"] is False
    confirm_row = journal.one("SELECT * FROM canary_runs WHERE confirmation_of = ?", (result["run_id"],))
    assert confirm_row is not None
    events = journal.query(
        "SELECT * FROM system_events WHERE category = 'canary' AND message LIKE '%not confirmed%'"
    )
    assert len(events) == 1
    assert result["run_id"] in events[0]["message"]
    assert confirm_row["run_id"] in events[0]["message"]
    assert journal.count_rows("canary_runs", "1=1") == 3  # baseline seed + trigger + confirmation


def test_canary2_tier2_confirmed_pages_once_with_both_run_ids_counts_and_split(tmp_path, journal, monkeypatch):
    """Spec test 4: TIER_2 trip + confirming trip -> exactly ONE alert;
    body contains both run ids, both mismatch counts, the stable/boundary
    split, and "confirmed by same-day re-run"."""
    settings = make_settings(NTFY_TOPIC="test-topic")
    corpus_dir = str(tmp_path / "corpus")
    _seed_pinned_baseline(journal, settings, corpus_dir)

    import alphaos.canary.run as run_module
    responses = iter([
        (DRIFT_TIER_2, _label_drift_detail(4, 1, 3)),
        (DRIFT_TIER_2, _label_drift_detail(5, 2, 3)),
    ])
    monkeypatch.setattr(run_module, "_compute_drift", lambda *a, **kw: next(responses))
    sent = []
    monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: sent.append(kw) or True)

    result = run_canary_confirmed(journal, settings, corpus_dir=corpus_dir)

    assert len(sent) == 1
    body = sent[0]["message"]
    assert result["run_id"] in body
    assert result["confirmation_run_id"] in body
    assert "mismatch counts: trigger=4, confirmation=5" in body
    assert "1/13 stable" in body
    assert "3/7 boundary" in body
    assert "confirmed by same-day re-run" in body
    assert "high-signal" in body  # spec Design 3's fixed interpretive sentence, verbatim
    assert result["confirmed"] is True
    assert result["paged"] is True


def test_canary2_loop_guard_confirmation_never_spawns_second_confirmation(tmp_path, journal, monkeypatch):
    """Spec test 5: loop guard -- a confirmation run that itself trips does
    NOT spawn a second confirmation. Structural assert on call count (how
    many times ``run_canary`` itself was invoked), not on convention."""
    settings = make_settings(NTFY_TOPIC="test-topic")
    corpus_dir = str(tmp_path / "corpus")
    _seed_pinned_baseline(journal, settings, corpus_dir)

    import alphaos.canary.run as run_module
    monkeypatch.setattr(
        run_module, "_compute_drift", lambda *a, **kw: (DRIFT_TIER_2, _label_drift_detail(4, 1, 3))
    )
    monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: True)

    real_run_canary = run_module.run_canary
    calls = {"n": 0}

    def counting_run_canary(*a, **kw):
        calls["n"] += 1
        return real_run_canary(*a, **kw)

    monkeypatch.setattr(run_module, "run_canary", counting_run_canary)

    run_canary_confirmed(journal, settings, corpus_dir=corpus_dir)

    assert calls["n"] == 2  # the trigger + exactly ONE confirmation, never a second
    assert journal.count_rows("canary_runs", "confirmation_of IS NOT NULL") == 1


def test_canary2_cost_preflight_refusal_pages_original_unconfirmed(tmp_path, journal, monkeypatch):
    """Spec test 6: cost preflight refuses the confirmation -> the original
    trip pages immediately with the UNCONFIRMED marker (fail-toward-paging,
    the spec's one law). Needs run_canary's own is_mock=False to exercise
    the real cost-guard preflight -- PlaybookClassifier.classify is
    monkeypatched to its own deterministic mock path so this stays hermetic
    (zero real network) despite is_mock=False."""
    settings = make_settings(
        NTFY_TOPIC="test-topic", OPENAI_API_KEY="sk-test", ALPHAOS_MODE="paper",
        SCHEDULER_AI_COST_CAP_CALLS_PER_30D="50",
        SHADOW_AI_CAP_CALLS_PER_30D="12",  # EXP-1's own joint-validation must clear this cap too
    )
    corpus_dir = str(tmp_path / "corpus")

    import alphaos.ai.playbook_classifier as pc_module
    import alphaos.canary.run as run_module
    monkeypatch.setattr(pc_module.PlaybookClassifier, "classify", lambda self, packet: self._mock_classify(packet))

    _seed_pinned_baseline(journal, settings, corpus_dir)

    monkeypatch.setattr(
        run_module, "_compute_drift", lambda *a, **kw: (DRIFT_TIER_2, _label_drift_detail(1, 0, 0, 0, 0))
    )
    sent = []
    monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: sent.append(kw) or True)

    calls = {"n": 0}

    def fake_calls_in_last_30_days(_journal):
        calls["n"] += 1
        return 0 if calls["n"] <= 2 else 999  # trigger's two checks pass; confirmation's checks fail

    monkeypatch.setattr(cost_guard, "calls_in_last_30_days", fake_calls_in_last_30_days)

    result = run_canary_confirmed(journal, settings, corpus_dir=corpus_dir)

    assert result["paged"] is True
    assert result["unconfirmed"] is True
    assert len(sent) == 1
    assert "UNCONFIRMED" in sent[0]["title"]
    assert "confirmation run could not execute" in sent[0]["message"]
    # the refused confirmation attempt never created its own canary_runs row (refused pre-insert,
    # same as run_canary's own pre-existing cost-cap-refusal contract).
    assert journal.count_rows("canary_runs", "confirmation_of IS NOT NULL") == 0
    events = journal.query("SELECT * FROM system_events WHERE category = 'canary' AND severity = 'error'")
    assert any("could not execute" in e["message"] for e in events)


def test_canary2_confirmation_exception_pages_unconfirmed_and_job_completes(tmp_path, monkeypatch):
    """Spec test 7: confirmation run raises mid-flight -> same fail
    direction as test 6 (unconfirmed page), error journaled, nothing
    swallowed, AND the weekly job itself still completes (the exception
    never escapes ``run_canary_run_job`` -- it must not fuse the scheduler)."""
    settings = make_settings(CANARY_ENABLED="true", NTFY_TOPIC="test-topic")
    journal = JournalStore(":memory:")
    orch = Orchestrator(settings=settings, journal=journal)
    try:
        import alphaos.canary.run as run_module
        corpus_dir = str(tmp_path / "corpus")
        monkeypatch.setattr(run_module, "DEFAULT_CORPUS_DIR", corpus_dir)
        _seed_pinned_baseline(journal, settings, corpus_dir)

        call_count = {"n": 0}

        def flaky_compute_drift(*a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return (DRIFT_TIER_2, _label_drift_detail(1, 0, 0, 0, 0))
            raise RuntimeError("simulated failure mid-confirmation")

        monkeypatch.setattr(run_module, "_compute_drift", flaky_compute_drift)
        sent = []
        monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: sent.append(kw) or True)

        job_result = run_canary_run_job(orch, JobRunner(orch))

        assert job_result["status"] == "completed"
        result = job_result["canary_result"]
        assert result["paged"] is True
        assert result["unconfirmed"] is True
        assert len(sent) == 1
        assert "could not execute" in sent[0]["message"]
        events = journal.query("SELECT * FROM system_events WHERE category = 'canary' AND severity = 'error'")
        assert any("simulated failure mid-confirmation" in e["message"] for e in events)
    finally:
        journal.close()


def test_canary2_stable_packets_v1_is_the_frozen_registration():
    """Spec test 8 (part 1): CANARY_STABLE_PACKETS_V1 is exactly the 13
    frozen ids named in the spec -- this literal assert IS the
    registration."""
    assert CANARY_STABLE_PACKETS_V1 == frozenset({
        "pkt_077c103ae10b", "pkt_1eeb01e36b6d", "pkt_25ad8d71eedf", "pkt_31bc21eccf10",
        "pkt_33cc39aec8c7", "pkt_4b424842d943", "pkt_a7c1bcae7175", "pkt_af6a10c6ea2b",
        "pkt_d27d1d035916", "pkt_d74e3608adc2", "pkt_d9bc552ddc8e", "pkt_f182ebbce06b",
        "pkt_fe03feceab9c",
    })
    assert len(CANARY_STABLE_PACKETS_V1) == 13
    assert CANARY_STABLE_PACKETS_V1.isdisjoint(CANARY_BOUNDARY_PACKETS_V1)


def test_canary2_split_counts_sum_to_compared():
    """Spec test 8 (part 2): split counts in drift_detail sum correctly --
    stable_total + boundary_total + unclassified_new == compared."""
    stable_id = sorted(CANARY_STABLE_PACKETS_V1)[0]
    boundary_id = sorted(CANARY_BOUNDARY_PACKETS_V1)[0]
    new_id_ = "pkt_brandnew00000"
    current_by_packet = {
        stable_id: {"primary_label": "Momentum", "label_decision": "watch"},
        boundary_id: {"primary_label": "Breakout", "label_decision": "watch"},
        new_id_: {"primary_label": "Momentum", "label_decision": "watch"},
    }
    baseline_by_packet = {
        stable_id: {"primary_label": "Momentum", "label_decision": "watch"},
        boundary_id: {"primary_label": "Momentum", "label_decision": "watch"},  # flip
        new_id_: {"primary_label": "Breakout", "label_decision": "watch"},  # flip
    }
    result = _label_comparison(current_by_packet, baseline_by_packet, 0.2)
    assert result["compared"] == 3
    assert result["stable_total"] + result["boundary_total"] + result["unclassified_new"] == result["compared"]


def test_canary2_packet_absent_from_both_sets_renders_unclassified_new():
    """Spec test 9: a corpus packet absent from both the stable and
    boundary sets renders as unclassified_new -- never silently bucketed
    into either."""
    new_id_ = "pkt_totallynew0000"
    current_by_packet = {new_id_: {"primary_label": "Momentum", "label_decision": "watch"}}
    baseline_by_packet = {new_id_: {"primary_label": "Breakout", "label_decision": "watch"}}

    result = _label_comparison(current_by_packet, baseline_by_packet, 0.2)

    assert result["unclassified_new"] == 1
    assert result["unclassified_new_flips"] == 1
    assert result["stable_flips"] == 0
    assert result["boundary_flips"] == 0


def test_canary2_confirmation_of_column_additive_migration(tmp_path):
    """Spec test 10: additive migration -- ``confirmation_of`` materializes
    on a pre-CANARY-2 DB; SCHEMA_VERSION stays 3; old rows read back NULL."""
    from alphaos.journal.schema import SCHEMA_VERSION

    db_path = tmp_path / "pre_canary2.db"
    j1 = JournalStore(str(db_path))
    j1.conn.execute("DROP TABLE IF EXISTS canary_runs")
    j1.conn.execute(
        """
        CREATE TABLE canary_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            corpus_dir TEXT NOT NULL,
            corpus_version INTEGER,
            configured_model TEXT,
            is_mock INTEGER DEFAULT 0,
            n_prompts INTEGER NOT NULL DEFAULT 0,
            n_parse_or_failsafe INTEGER NOT NULL DEFAULT 0,
            response_models_json TEXT,
            system_fingerprints_json TEXT,
            mean_confidence REAL,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            latency_ms_total INTEGER,
            is_baseline INTEGER NOT NULL DEFAULT 0,
            drift_tier TEXT,
            drift_detail_json TEXT,
            lineage_id TEXT,
            started_at_utc TEXT NOT NULL,
            started_at_sgt TEXT NOT NULL,
            finished_at_utc TEXT,
            finished_at_sgt TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """
    )
    j1.conn.execute(
        "INSERT INTO canary_runs (run_id, corpus_dir, n_prompts, started_at_utc, started_at_sgt, "
        "created_at_utc, created_at_sgt) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("run_pre_canary2", "data/canary", 1, "2026-07-01T00:00:00+00:00", "2026-07-01T08:00:00+08:00",
         "2026-07-01T00:00:00+00:00", "2026-07-01T08:00:00+08:00"),
    )
    j1.conn.commit()
    j1.close()

    j2 = JournalStore(str(db_path))  # re-opening must additively add confirmation_of
    cols = j2._cols("canary_runs")
    assert "confirmation_of" in cols
    row = j2.one("SELECT confirmation_of FROM canary_runs WHERE run_id = ?", ("run_pre_canary2",))
    assert row["confirmation_of"] is None
    user_version = j2.conn.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == SCHEMA_VERSION == 3
    j2.close()


def test_canary2_scheduler_job_end_to_end_confirmed_page(tmp_path, monkeypatch):
    """Spec test 11 (confirmed-page scenario): drive the WEEKLY JOB entry
    point (``run_canary_run_job``) end-to-end, not just ``run_canary_confirmed``
    directly -- the HOLD-1 audit lesson (prove the production wiring, not
    just the library function)."""
    settings = make_settings(CANARY_ENABLED="true", NTFY_TOPIC="test-topic")
    journal = JournalStore(":memory:")
    orch = Orchestrator(settings=settings, journal=journal)
    try:
        import alphaos.canary.run as run_module
        corpus_dir = str(tmp_path / "corpus")
        monkeypatch.setattr(run_module, "DEFAULT_CORPUS_DIR", corpus_dir)
        _seed_pinned_baseline(journal, settings, corpus_dir)

        monkeypatch.setattr(
            run_module, "_compute_drift", lambda *a, **kw: (DRIFT_TIER_2, _label_drift_detail(1, 0, 0, 0, 0))
        )
        sent = []
        monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: sent.append(kw) or True)

        job_result = run_canary_run_job(orch, JobRunner(orch))

        assert job_result["status"] == "completed"
        result = job_result["canary_result"]
        assert result["confirmed"] is True
        assert result["paged"] is True
        assert len(sent) == 1
        assert "confirmed by same-day re-run" in sent[0]["message"]
    finally:
        journal.close()


def test_canary2_scheduler_job_end_to_end_not_confirmed(tmp_path, monkeypatch):
    """Spec test 11 (not-confirmed scenario), same end-to-end scheduler-path
    proof as above."""
    settings = make_settings(CANARY_ENABLED="true", NTFY_TOPIC="test-topic")
    journal = JournalStore(":memory:")
    orch = Orchestrator(settings=settings, journal=journal)
    try:
        import alphaos.canary.run as run_module
        corpus_dir = str(tmp_path / "corpus")
        monkeypatch.setattr(run_module, "DEFAULT_CORPUS_DIR", corpus_dir)
        _seed_pinned_baseline(journal, settings, corpus_dir)

        responses = iter([
            (DRIFT_TIER_2, _label_drift_detail(1, 0, 0, 0, 0)),
            (DRIFT_NONE, {}),
        ])
        monkeypatch.setattr(run_module, "_compute_drift", lambda *a, **kw: next(responses))
        sent = []
        monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: sent.append(kw) or True)

        job_result = run_canary_run_job(orch, JobRunner(orch))

        assert job_result["status"] == "completed"
        result = job_result["canary_result"]
        assert result["confirmed"] is False
        assert result["paged"] is False
        assert sent == []
    finally:
        journal.close()


def test_canary2_alert_send_failure_confirmed_event_persists(tmp_path, journal, monkeypatch):
    """Spec test 12: alert-send failure on a confirmed page -- the
    confirmed-drift system_event persists (event-before-send, the existing
    ``cards/demotion.py`` durability law: journal first, network second)."""
    settings = make_settings(NTFY_TOPIC="test-topic")
    corpus_dir = str(tmp_path / "corpus")
    _seed_pinned_baseline(journal, settings, corpus_dir)

    import alphaos.canary.run as run_module
    responses = iter([
        (DRIFT_TIER_2, _label_drift_detail(1, 0, 1, 0, 1)),
        (DRIFT_TIER_2, _label_drift_detail(1, 0, 1, 0, 1)),
    ])
    monkeypatch.setattr(run_module, "_compute_drift", lambda *a, **kw: next(responses))

    captured = {}

    def check_event_already_written(*a, **kw):
        rows = journal.query(
            "SELECT * FROM system_events WHERE category = 'canary' AND message LIKE '%CONFIRMED%'"
        )
        captured["existing_before_send"] = len(rows)
        return False  # simulates a send failure -- matches send_alert's real "never raises" contract

    monkeypatch.setattr(run_module.alerts, "send_alert", check_event_already_written)

    result = run_canary_confirmed(journal, settings, corpus_dir=corpus_dir)

    assert captured["existing_before_send"] == 1  # the durable audit row existed BEFORE the send attempt
    assert result["paged"] is True  # the page was attempted regardless of send outcome
    events = journal.query(
        "SELECT * FROM system_events WHERE category = 'canary' AND message LIKE '%CONFIRMED%'"
    )
    assert len(events) == 1


def test_canary2_mock_mode_fully_deterministic_zero_network(tmp_path, journal, monkeypatch):
    """Spec test 13: mock mode, whole flow deterministic, zero network.
    NTFY_TOPIC is deliberately left UNSET (not just mocked) -- ``send_alert``
    genuinely no-ops rather than being intercepted, which is a stronger
    "zero network" proof than mocking it away would be; the autouse
    ``_block_real_network_calls`` fixture would raise on any real attempt
    regardless."""
    settings = make_settings()  # ALPHAOS_MODE=mock by default; no NTFY_TOPIC
    corpus_dir = str(tmp_path / "corpus")
    _seed_pinned_baseline(journal, settings, corpus_dir)

    import alphaos.canary.run as run_module
    monkeypatch.setattr(
        run_module, "_compute_drift", lambda *a, **kw: (DRIFT_TIER_2, _label_drift_detail(1, 0, 1, 0, 1))
    )

    result1 = run_canary_confirmed(journal, settings, corpus_dir=corpus_dir)
    result2 = run_canary_confirmed(journal, settings, corpus_dir=corpus_dir)

    assert result1["confirmed"] is True and result2["confirmed"] is True
    assert result1["paged"] is True and result2["paged"] is True


def test_canary2_canary_status_renders_confirmation_lineage_confirmed(tmp_path, journal, monkeypatch):
    """Spec test 14 (confirmed outcome): canary_status renders confirmation
    lineage."""
    from alphaos.reports.canary_report import build_canary_report, render_markdown

    settings = make_settings(NTFY_TOPIC="test-topic")
    corpus_dir = str(tmp_path / "corpus")
    _seed_pinned_baseline(journal, settings, corpus_dir)

    import alphaos.canary.run as run_module
    monkeypatch.setattr(
        run_module, "_compute_drift", lambda *a, **kw: (DRIFT_TIER_2, _label_drift_detail(1, 0, 1, 0, 1))
    )
    monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: True)

    result = run_canary_confirmed(journal, settings, corpus_dir=corpus_dir)

    rep = build_canary_report(journal)
    assert rep["run_id"] == result["run_id"]
    assert rep["confirmation"]["status"] == "confirmed"
    assert rep["confirmation"]["confirming_run_id"] == result["confirmation_run_id"]
    md = render_markdown(rep)
    assert "CONFIRMED" in md
    assert result["confirmation_run_id"] in md


def test_canary2_canary_status_renders_confirmation_lineage_not_confirmed(tmp_path, journal, monkeypatch):
    """Spec test 14 (not-confirmed outcome): canary_status renders
    confirmation lineage."""
    from alphaos.reports.canary_report import build_canary_report, render_markdown

    settings = make_settings(NTFY_TOPIC="test-topic")
    corpus_dir = str(tmp_path / "corpus")
    _seed_pinned_baseline(journal, settings, corpus_dir)

    import alphaos.canary.run as run_module
    responses = iter([
        (DRIFT_TIER_2, _label_drift_detail(1, 0, 1, 0, 1)),
        (DRIFT_NONE, {}),
    ])
    monkeypatch.setattr(run_module, "_compute_drift", lambda *a, **kw: next(responses))
    monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: True)

    result = run_canary_confirmed(journal, settings, corpus_dir=corpus_dir)

    rep = build_canary_report(journal)
    assert rep["run_id"] == result["run_id"]
    assert rep["confirmation"]["status"] == "not_confirmed"
    assert rep["confirmation"]["confirming_run_id"] == result["confirmation_run_id"]
    md = render_markdown(rep)
    assert "NOT confirmed" in md


# ---------------------------------------------- CANARY-2: audit-fixup regressions
# Both Opus post-build audits (2026-08-03) independently converged on MUST FIX
# 1 (the rank rule ate a page across trigger classes) and flagged several
# other real gaps -- see docs/roadmap/alphaos-canary2-drift-confirmation-spec.md's
# own dated correction notes for the full adjudication. One test per named
# finding that specifically needs a NEW regression (existing tests already
# covered the rest, per the audit's own "core held" verdict).

def _identity_detail(baseline_models, current_models):
    return {
        "trigger_class": CANARY_TRIGGER_IDENTITY,
        "identity_change": {
            "baseline_response_models": baseline_models, "current_response_models": current_models,
            "baseline_system_fingerprints": [], "current_system_fingerprints": [],
        },
    }


def _failsafe_detail(baseline_rate=0.0, current_rate=0.15):
    return {
        "trigger_class": CANARY_TRIGGER_FAILSAFE,
        "failsafe_rate_change": {"baseline_rate": baseline_rate, "current_rate": current_rate},
    }


def test_canary2_auditfix_cross_class_failsafe_trigger_confirmed_by_tier2(tmp_path, journal, monkeypatch):
    """MUST FIX 1 (both reviewers, CONVERGENT HIGH): a TIER_1/failsafe
    trigger confirmed by a TIER_2/label-drift re-run -- two consecutive
    genuinely pageable trips -- must page: the old rank-only rule
    (confirmed = rank(confirm) <= rank(trigger)) fell through to "not
    confirmed"/"transient wobble" here (TIER_2 outranks TIER_1 numerically),
    which was factually false -- the re-run DID trip."""
    settings = make_settings(NTFY_TOPIC="test-topic")
    corpus_dir = str(tmp_path / "corpus")
    _seed_pinned_baseline(journal, settings, corpus_dir)

    import alphaos.canary.run as run_module
    responses = iter([(DRIFT_TIER_1, _failsafe_detail()), (DRIFT_TIER_2, _label_drift_detail(4, 1, 3))])
    monkeypatch.setattr(run_module, "_compute_drift", lambda *a, **kw: next(responses))
    sent = []
    monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: sent.append(kw) or True)

    result = run_canary_confirmed(journal, settings, corpus_dir=corpus_dir)

    assert result["confirmed"] is True
    assert result["paged"] is True
    assert len(sent) == 1
    body = sent[0]["message"]
    assert "TIER_1" in body and "TIER_2" in body
    assert "failsafe" in body and "label drift" in body
    assert "TIER_1" in sent[0]["title"]  # title = the WORSE tier (TIER_1 outranks TIER_2)
    events = journal.query("SELECT * FROM system_events WHERE category = 'canary'")
    assert not any("transient wobble" in e["message"] for e in events)  # never for a pageable confirmation


def test_canary2_auditfix_cross_class_tier2_confirmed_by_identity_keeps_diff(tmp_path, journal, monkeypatch):
    """MUST FIX 3 (both reviewers, CONVERGENT MEDIUM): a TIER_2 trigger
    confirmed by a TIER_1/identity re-run must render the identity diff --
    the single most actionable fact -- never a literal "confirmation=None"."""
    settings = make_settings(NTFY_TOPIC="test-topic")
    corpus_dir = str(tmp_path / "corpus")
    _seed_pinned_baseline(journal, settings, corpus_dir)

    import alphaos.canary.run as run_module
    responses = iter([
        (DRIFT_TIER_2, _label_drift_detail(4, 1, 3)),
        (DRIFT_TIER_1, _identity_detail(["gpt-5.6-luna"], ["gpt-9.9-SWAPPED"])),
    ])
    monkeypatch.setattr(run_module, "_compute_drift", lambda *a, **kw: next(responses))
    sent = []
    monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: sent.append(kw) or True)

    result = run_canary_confirmed(journal, settings, corpus_dir=corpus_dir)

    assert result["confirmed"] is True
    assert result["paged"] is True
    assert len(sent) == 1
    body = sent[0]["message"]
    assert "gpt-9.9-SWAPPED" in body  # the identity diff -- never dropped
    assert "confirmation=None" not in body
    assert "TIER_1" in sent[0]["title"]  # TIER_1/identity outranks TIER_2/label-drift


def test_canary2_auditfix_locked_db_on_pending_event_still_pages(tmp_path, journal, monkeypatch):
    """MUST FIX 2 (Auditor A HIGH): a momentarily-locked DB on the
    "drift trip pending confirmation" journal write (run.py's own
    pre-confirmation event) must not crash the flow -- the eventual page
    must still happen."""
    settings = make_settings(NTFY_TOPIC="test-topic")
    corpus_dir = str(tmp_path / "corpus")
    _seed_pinned_baseline(journal, settings, corpus_dir)

    import sqlite3

    import alphaos.canary.run as run_module
    responses = iter([
        (DRIFT_TIER_2, _label_drift_detail(4, 1, 3)),
        (DRIFT_TIER_2, _label_drift_detail(5, 2, 3)),
    ])
    monkeypatch.setattr(run_module, "_compute_drift", lambda *a, **kw: next(responses))
    sent = []
    monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: sent.append(kw) or True)

    real_log_system_event = journal.log_system_event
    call_count = {"n": 0}

    def flaky_log_system_event(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:  # the FIRST call is the pending-confirmation event
            raise sqlite3.OperationalError("database is locked")
        return real_log_system_event(*a, **kw)

    monkeypatch.setattr(journal, "log_system_event", flaky_log_system_event)

    result = run_canary_confirmed(journal, settings, corpus_dir=corpus_dir)

    assert result["paged"] is True
    assert result["confirmed"] is True
    assert len(sent) == 1


def test_canary2_auditfix_locked_db_on_send_path_event_still_pages(tmp_path, journal, monkeypatch):
    """MUST FIX 2 (Auditor A HIGH): a locked DB on the event write INSIDE
    _send_drift_alert (the send-path event, distinct from the
    pending-confirmation event above) must not prevent the actual send --
    proven here with EVERY log_system_event call failing, on the
    identity-immediate path (the highest-stakes page in the system, per
    Auditor A's own probe)."""
    settings = make_settings(NTFY_TOPIC="test-topic")
    corpus_dir = str(tmp_path / "corpus")
    _seed_pinned_baseline(journal, settings, corpus_dir)

    import sqlite3

    import alphaos.canary.run as run_module
    monkeypatch.setattr(
        run_module, "_compute_drift",
        lambda *a, **kw: (DRIFT_TIER_1, _identity_detail(["gpt-5.1"], ["gpt-5.2"])),
    )
    sent = []
    monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: sent.append(kw) or True)

    def always_locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(journal, "log_system_event", always_locked)

    result = run_canary_confirmed(journal, settings, corpus_dir=corpus_dir)

    assert result["paged"] is True
    assert len(sent) == 1  # the page still went out despite every log_system_event call failing


def test_canary2_auditfix_corpus_tampered_on_confirmation_pages_then_reraises(tmp_path, monkeypatch):
    """MUST FIX 5 (Auditor A HIGH): a CorpusTamperedError discovered during
    the CONFIRMATION run pages UNCONFIRMED first, THEN re-raises, so the
    weekly job still lands 'failed'/fuse-eligible -- run_canary's own
    loud-failure contract for a tampered corpus (spec non-goals: unchanged)
    must survive even when the tamper is found on a confirmation replay,
    not just the first run of the week. Driven through JobRunner.run_job
    (the real production entry point that catches/records job failures),
    not the raw run_canary_run_job wrapper, so the "job lands failed" claim
    is actually proven, not assumed."""
    settings = make_settings(CANARY_ENABLED="true", NTFY_TOPIC="test-topic")
    journal = JournalStore(":memory:")
    orch = Orchestrator(settings=settings, journal=journal)
    try:
        import alphaos.canary.run as run_module
        corpus_dir = str(tmp_path / "corpus")
        monkeypatch.setattr(run_module, "DEFAULT_CORPUS_DIR", corpus_dir)
        _seed_pinned_baseline(journal, settings, corpus_dir)

        call_count = {"n": 0}

        def flaky_compute_drift(*a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return (DRIFT_TIER_2, _label_drift_detail(1, 0, 0, 0, 0))
            raise CorpusTamperedError("pkt_canarytest01.json content no longer matches its frozen sha256")

        monkeypatch.setattr(run_module, "_compute_drift", flaky_compute_drift)
        sent = []
        monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: sent.append(kw) or True)

        job_result = JobRunner(orch).run_job(cadence.JobType.CANARY_RUN)

        # The exception WAS re-raised out of run_canary_confirmed -- JobRunner's
        # own try/except around job_func() catches it and marks the job_runs
        # row 'failed' (fuse-eligible), never silently 'completed'.
        assert job_result["status"] == "failed"
        # Order proven, not assumed: the UNCONFIRMED page (sent[0]) went out
        # BEFORE the raise; JobRunner's own generic job-failure alert
        # (sent[1], its pre-existing contract, unrelated to CANARY-2) follows.
        assert len(sent) == 2
        assert "UNCONFIRMED" in sent[0]["title"]
        assert "canary_run" in sent[1]["title"]
    finally:
        journal.close()


def test_canary2_auditfix_tier1_detail_includes_label_drift_split():
    """ALSO FIX 4: the stable/boundary split is computed on TIER_1 runs too
    (Design 3: "all tiers, including clean runs") -- WITHOUT promoting the
    tier decision (the Tier-1-before-Tier-2 short-circuit RETURN order is
    otherwise unchanged)."""
    baseline_row = {
        "run_id": "baserun", "n_prompts": 1, "n_parse_or_failsafe": 0,
        "response_models_json": json.dumps(["gpt-5.1"]), "system_fingerprints_json": json.dumps(["fp_a"]),
        "mean_confidence": 0.7,
    }
    current_agg = {
        "n_prompts": 1, "n_parse_or_failsafe": 0,
        "response_models_json": json.dumps(["gpt-5.2"]),  # identity changed -> TIER_1
        "system_fingerprints_json": json.dumps(["fp_a"]), "mean_confidence": 0.7,
    }
    stable_id = sorted(CANARY_STABLE_PACKETS_V1)[0]
    current_by_packet = {stable_id: {"primary_label": "Breakout", "label_decision": "watch"}}
    baseline_by_packet = {stable_id: {"primary_label": "Momentum", "label_decision": "watch"}}  # also flips

    tier, detail = _compute_drift(current_agg, current_by_packet, baseline_row, baseline_by_packet, 0.2, 0.15)

    assert tier == DRIFT_TIER_1  # the short-circuit DECISION is unchanged
    assert "label_drift" in detail  # but the split's COMPUTATION is now unconditional
    assert detail["label_drift"]["stable_flips"] == 1


def test_canary2_auditfix_unclassified_new_rendered_in_flips_text():
    """ALSO FIX 7: the human-readable flips line names the unclassified_new
    bucket when nonzero -- spec says "rendered", not just carried in the
    JSON dump."""
    from alphaos.canary.run import _split_summary_text

    detail = {"label_drift": {
        "stable_flips": 1, "stable_total": 13, "boundary_flips": 3, "boundary_total": 7,
        "unclassified_new": 2, "unclassified_new_flips": 1,
    }}

    text = _split_summary_text(detail)

    assert "1/2 new-unclassified" in text


def test_canary2_auditfix_unclassified_new_omitted_when_zero():
    """Companion to the above: the segment stays silent (not "0/0") on the
    common case where the corpus hasn't grown since v1."""
    from alphaos.canary.run import _split_summary_text

    detail = {"label_drift": {
        "stable_flips": 1, "stable_total": 13, "boundary_flips": 3, "boundary_total": 7,
        "unclassified_new": 0, "unclassified_new_flips": 0,
    }}

    text = _split_summary_text(detail)

    assert "new-unclassified" not in text


def test_canary2_auditfix_identity_immediate_lineage_annotated(tmp_path, journal, monkeypatch):
    """ALSO FIX 6: the identity-immediate trigger row gets a lineage
    annotation too, so it's never indistinguishable from a raw
    pre-CANARY-2 trip when read back through canary_status."""
    from alphaos.reports.canary_report import build_canary_report

    settings = make_settings(NTFY_TOPIC="test-topic")
    corpus_dir = str(tmp_path / "corpus")
    _seed_pinned_baseline(journal, settings, corpus_dir)

    import alphaos.canary.run as run_module
    monkeypatch.setattr(
        run_module, "_compute_drift",
        lambda *a, **kw: (DRIFT_TIER_1, _identity_detail(["gpt-5.1"], ["gpt-5.2"])),
    )
    monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: True)

    result = run_canary_confirmed(journal, settings, corpus_dir=corpus_dir)

    rep = build_canary_report(journal)
    assert rep["run_id"] == result["run_id"]
    assert rep["confirmation"]["status"] == "identity_immediate"


def test_canary2_auditfix_cli_help_names_confirmation_policy_gap():
    """ALSO FIX 9: the manual `canary_run` CLI command's help text names the
    confirmation-policy gap explicitly (behavior unchanged -- Design 5's
    split is defensible -- just made discoverable), pointing an operator at
    `scheduler_run_job canary_run` for the confirmed path."""
    from alphaos.__main__ import build_parser

    help_text = build_parser().format_help()

    assert "scheduler_run_job canary_run" in help_text


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
    # GREEN-1 (date rot, §H.1 4th occurrence): was hardcoded "2026-07-09" --
    # cost_guard's own window is a TRAILING 30 days from timeutils.now_utc(),
    # so a literal date ages out of the window and this assertion goes
    # permanently red the day it does (it did, on 2026-08-08). Stamped
    # relative to now instead, same house pattern as cd057ce/233c74b/6ffbc2d.
    # Mutation-tested: `--clock-shift-days=90` / `-90` both stay green (see
    # tests/_dateshift.py).
    since = timeutils.to_iso(timeutils.now_utc() - timedelta(days=1))
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
