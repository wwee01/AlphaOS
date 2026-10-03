"""Real-shaped, offline evidence for collection and retained-registry reporting."""

from datetime import timedelta

from alphaos.journal.journal_store import JournalStore
from alphaos.reports.daily_brief import _one_action
from alphaos.reports.research_status import build_collection_status, build_experiment_registry_report
from alphaos.safety import KillSwitch, ShadowLabelSuspendSwitch
from alphaos.stats.preregistration import register_hypothesis
from alphaos.util import timeutils
from conftest import make_settings


def test_all_retained_experiments_preserve_negative_evidence_and_authoritative_date(tmp_path):
    path = str(tmp_path / "ledger.db")
    journal = JournalStore(path)
    tested = register_hypothesis(journal, "Top TQS predicts +0.3R", "centered_delta", 40, 42, "2026-08-21")
    baseline = register_hypothesis(journal, "AI value", "paired delta", 30, 28, "2026-09-07",
                                   params={"rule_version": "threshold_v1"})
    old = register_hypothesis(journal, "H-PER-1P: positive", "per", 20, 90, "2026-10-15")
    corrected = register_hypothesis(journal, "H-PER-1P-v2: positive", "per_v2", 20, 90, "2026-10-15",
                                   params={"card_content_hash": "frozen"})
    journal.conn.execute(
        "UPDATE preregistrations SET evaluated_at_utc=?,point_estimate=-.4,ci_low=-.7,ci_high=-.1,"
        "ci_level=.9,evidence_status='ok',effective_n=137,n_raw=210,span_days=49 WHERE prereg_id=?",
        (timeutils.stamp().utc, tested),
    )
    journal.conn.commit()
    before = journal.query("SELECT * FROM preregistrations")
    journal.close()
    ro = JournalStore(path, read_only=True)
    report = build_experiment_registry_report(ro, {"hypotheses": [
        {"prereg_id": tested, "hypothesis_id": "H-TQS-1", "metric_fn_name": "h_tqs_1_rows", "status": "resolved", "analysis_not_before": "2026-08-21"},
        {"prereg_id": baseline, "hypothesis_id": "H-AI-1", "status": "testing", "analysis_not_before": "2026-10-08"},
    ]})
    rows = {r["prereg_id"]: r for r in report["experiments"]}
    assert report["registered_count"] == 4
    assert report["evaluated_count"] == report["superseded_count"] == 1
    assert rows[tested]["verdict"] == "rejected"
    assert rows[tested]["ci_high"] == -.1
    assert "reference-arm uncertainty" in rows[tested]["validity_context"]
    assert rows[old]["superseded_by"] == corrected
    assert rows[baseline]["analysis_not_before"] == "2026-09-07"
    assert rows[baseline]["registry_date_mismatch"] is True
    assert rows[baseline]["verdict"] is None
    assert rows[baseline]["point_estimate"] is None
    assert ro.query("SELECT * FROM preregistrations") == before
    ro.close()


def _collection_fixture(tmp_path):
    journal = JournalStore(str(tmp_path / "collection.db"))
    settings = make_settings(SHADOW_TIER_ENABLED="true", SHADOW_LABELLING_ENABLED="true",
                             LAST30DAYS_ENABLED="true")
    stamp = timeutils.stamp()
    journal.insert("universe_days", {"universe_day_id": "u1", "symbol": "AAA", "tier": "watchlist",
                                    "market_date": timeutils.market_date().isoformat(), "freshness_status": "usable"})
    journal.insert("canary_runs", {"run_id": "clean", "corpus_dir": "fixture", "is_mock": 0,
                                  "n_prompts": 20, "n_parse_or_failsafe": 0, "total_tokens": 100,
                                  "drift_tier": "none", "started_at_utc": stamp.utc,
                                  "started_at_sgt": stamp.local_sgt, "finished_at_utc": stamp.utc})
    return journal, settings, ShadowLabelSuspendSwitch(str(tmp_path / "suspend")), KillSwitch(str(tmp_path / "kill"))


def test_incomplete_canary_never_counts_as_recovery_or_clears_persistent_latch(tmp_path):
    journal, settings, suspend, kill = _collection_fixture(tmp_path)
    suspend.engage("historical drift needs acknowledgment")
    before = journal.conn.total_changes
    report = build_collection_status(journal, settings, suspend, kill)
    shadow = report["shadow"]
    assert shadow["state"] == "suspended"
    assert shadow["successful_labels"] == 0
    assert shadow["last_successful_label_at_utc"] is None
    assert not shadow["current_checks"]["auto_suspend_triggered"]
    assert not shadow["current_checks"]["latest_canary_recent_and_complete"]
    assert shadow["current_checks"]["canary_recovery_evidence"]["reasons"]
    assert shadow["current_checks"]["coverage_ok"]
    assert shadow["current_checks"]["budget_ok"]
    assert suspend.is_engaged()
    assert suspend.reason() == "historical drift needs acknowledgment"
    assert journal.conn.total_changes == before
    journal.close()



def test_verified_canary_is_reported_without_clearing_latch(tmp_path, monkeypatch):
    proof = {"ok": True, "age_seconds": 86400, "reasons": [], "evidence_sha256": "proof"}
    monkeypatch.setattr("alphaos.reports.research_status.validate_canary_recovery", lambda *args: proof)
    journal, settings, suspend, kill = _collection_fixture(tmp_path)
    suspend.engage("operator review required")
    before = journal.conn.total_changes
    report = build_collection_status(journal, settings, suspend, kill)["shadow"]
    assert report["state"] == "suspended"
    assert report["current_checks"]["canary_recovery_evidence"] == proof
    assert report["current_checks"]["latest_canary_recent_and_complete"] is True
    assert report["current_checks"]["canary_age_days"] == 1
    assert journal.conn.total_changes == before
    assert suspend.is_engaged()
    journal.close()

def test_only_real_passed_labels_count_and_stale_evidence_is_visible(tmp_path, monkeypatch):
    # Isolate collection-age reporting from the separately tested canary proof.
    monkeypatch.setattr("alphaos.reports.research_status.validate_canary_recovery",
                        lambda *args: {"ok": True, "age_seconds": 0, "reasons": []})
    journal, settings, suspend, kill = _collection_fixture(tmp_path)
    old = timeutils.to_iso(timeutils.now_utc() - timedelta(days=10))
    for label_id, mock, validation, when in [("real", 0, "passed", old),
                                            ("mock", 1, "passed", timeutils.stamp().utc),
                                            ("failed", 0, "live_exception", timeutils.stamp().utc)]:
        journal.insert("candidate_labels", {"label_id": label_id, "candidate_id": label_id,
                                            "symbol": "AAA", "shadow_tier": 1, "is_mock": mock,
                                            "validation_status": validation, "created_at_utc": when})
    journal.insert("universe_days", {"universe_day_id": "u2", "symbol": "AAA", "tier": "watchlist",
                                    "market_date": (timeutils.market_date() - timedelta(days=3)).isoformat(),
                                    "freshness_status": "usable"})
    result = build_collection_status(journal, settings, suspend, kill)
    assert result["shadow"]["successful_labels"] == 1
    assert result["shadow"]["last_successful_label_at_utc"] == old
    assert result["shadow"]["state"] == "label collection stale"
    assert result["text_archive"] == {"enabled": False, "documents": 0, "last_seen_at_utc": None}
    assert result["regime"]["days"] == 0
    journal.close()


def test_current_conditions_diagnostic_error_fails_closed(tmp_path, monkeypatch):
    journal, settings, suspend, kill = _collection_fixture(tmp_path)
    from alphaos.scheduler import shadow_label

    def fail(*args):
        raise ValueError("secret provider details")
    monkeypatch.setattr(shadow_label, "check_auto_suspend", fail)
    result = build_collection_status(journal, settings, suspend, kill)
    assert result["shadow"]["state"] == "checks unavailable"
    assert "secret" not in str(result)
    journal.close()


def test_suspension_cannot_be_hidden_by_below_floor_headline():
    needs = {"open_incident_count": 0, "fused_jobs": [], "pending_approvals": []}
    collection = {"shadow": {"enabled": True, "state": "suspended"}}
    result = _one_action(needs, [], {"status": "below_sample_floor", "data_progress": "1/30"},
                         collection_status=collection)
    assert "suspended" in result
    needs["open_incident_count"] = 1
    assert "protection incident" in _one_action(needs, [], {}, collection_status=collection)
