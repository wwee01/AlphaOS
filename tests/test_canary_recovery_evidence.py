"""Recovery proof, exercised through actual run_canary with an offline classifier."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from alphaos.ai import prompt_templates
from alphaos.ai.playbook_classifier import PlaybookClassifier
from alphaos.canary.corpus import write_corpus
from alphaos.canary.recovery_evidence import validate_canary_recovery
from alphaos.canary.run import pin_baseline, run_canary
from alphaos.constants import OFFICIAL_LABELS
from alphaos.journal.journal_store import JournalStore
from alphaos.lineage.hashing import stable_hash
from alphaos.util import timeutils
from conftest import make_settings

NOW = datetime(2026, 10, 3, 5, 33, 28, tzinfo=timezone.utc)


@pytest.fixture
def proof(tmp_path, monkeypatch):
    journal = JournalStore(str(tmp_path / "test.db"))
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="offline-placeholder",
                             LABEL_MODEL="configured-alias")
    corpus = tmp_path / "corpus"
    fixtures = [{"packet_id": f"pkt_{i}", "candidate_id": f"cand_{i}", "symbol": symbol,
                 "last_price": 100, "direction": "long", "freshness_status": "usable",
                 "interest_rank": i, "missing_data_flags": [], "spread_pct": .001,
                 "liquidity_ok": True, "dollar_volume": 1000000, "change_pct": .02,
                 "rel_volume": 1.5, "rel_strength_vs_spy": .01, "rel_strength_vs_qqq": .01,
                 "near_day_high": True, "near_day_low": False, "gap_pct": 0,
                 "structure_hint": "trend", "setup_hint": "trend_continuation",
                 "tradeable_volatility": True, "interest_score": .7,
                 "shortlist_reason": "offline", "momentum_score": .6}
                for i, symbol in enumerate(("AAA", "BBB"))]
    write_corpus(str(corpus), fixtures, "2026-10-01")
    clock = [NOW - timedelta(days=2)]
    monkeypatch.setattr(timeutils, "now_utc", lambda: clock[0])
    monkeypatch.setattr("alphaos.canary.run.lineage.get_or_create_lineage_id", lambda *a: "offline-lineage")

    def classify(_self, packet):
        prompt = prompt_templates.build_label_user_prompt(packet.to_prompt_dict(), sorted(OFFICIAL_LABELS))
        return SimpleNamespace(primary_label="Momentum", label_decision="watch", confidence=.8,
                               validation_status="passed", label_source="openai", raw={
                                   "symbol": packet.symbol, "primary_label": "Momentum",
                                   "decision": "WATCH", "confidence": .8},
                               response_model="resolved-model-2026", system_fingerprint=None,
                               prompt_hash=stable_hash(prompt),
                               system_prompt_hash=stable_hash(prompt_templates.LABEL_SYSTEM_PROMPT),
                               prompt_tokens=10, completion_tokens=5, total_tokens=15)

    monkeypatch.setattr(PlaybookClassifier, "classify", classify)
    baseline_result = run_canary(journal, settings, str(corpus))
    assert baseline_result["n_results"] == 2 and baseline_result["n_corpus_errors"] == 0
    baseline = baseline_result["run_id"]
    pin_baseline(journal, baseline)
    clock[0] = NOW - timedelta(days=1)
    latest_result = run_canary(journal, settings, str(corpus))
    assert latest_result["n_results"] == 2 and latest_result["n_corpus_errors"] == 0
    latest = latest_result["run_id"]
    clock[0] = NOW
    yield SimpleNamespace(journal=journal, settings=settings, corpus=corpus, baseline=baseline,
                          latest=latest, clock=clock, classify=classify)
    journal.conn.close()


def check(p, **kw):
    return validate_canary_recovery(p.journal, p.settings, corpus_dir=p.corpus, **kw)


def run_update(p, column, value, run=None):
    p.journal.conn.execute(f"UPDATE canary_runs SET {column}=? WHERE run_id=?", (value, run or p.latest))
    p.journal.conn.commit()


def row_update(p, column, value):
    p.journal.conn.execute(f"UPDATE canary_results SET {column}=? WHERE run_id=? AND packet_id='pkt_0'",
                           (value, p.latest))
    p.journal.conn.commit()


def test_actual_producer_clean_alias_model_and_missing_optional_fingerprint(proof):
    result = check(proof)
    assert result["ok"], result
    assert result["run_id"] == proof.latest
    assert len(result["evidence_sha256"]) == 64
    assert any("no system fingerprint" in text for text in result["limitations"])
    assert "raw_json" not in json.dumps(result)
    assert "baseline" not in result["inputs"]["database"]
    assert result["inputs"]["database"]["result_counts"][proof.latest] == 2


def test_actual_producer_partial_exception_is_finished_but_not_complete(proof, monkeypatch):
    def classify(self, packet):
        if packet.symbol == "BBB":
            raise ValueError("offline fixture exception")
        return proof.classify(self, packet)
    monkeypatch.setattr(PlaybookClassifier, "classify", classify)
    partial = run_canary(proof.journal, proof.settings, str(proof.corpus))
    row = proof.journal.one("SELECT * FROM canary_runs WHERE run_id=?", (partial["run_id"],))
    assert partial["n_corpus_errors"] == 1 and row["finished_at_utc"]
    assert row["n_prompts"] == 2 and row["n_parse_or_failsafe"] == 0 and row["total_tokens"] > 0
    assert "latest.result_membership" in check(proof)["reasons"]


@pytest.mark.parametrize("mode", ["zero", "missing", "duplicate", "foreign"])
def test_exact_packet_multiset_not_counts_alone(proof, mode):
    conn = proof.journal.conn
    if mode in ("zero", "missing"):
        conn.execute("DELETE FROM canary_results WHERE run_id=?" + (" AND packet_id='pkt_0'" if mode == "missing" else ""), (proof.latest,))
    elif mode == "foreign":
        row_update(proof, "packet_id", "foreign")
    else:
        row = proof.journal.one("SELECT * FROM canary_results WHERE run_id=? LIMIT 1", (proof.latest,))
        del row["id"]
        row["result_id"] = "duplicate-other-id"
        proof.journal.insert("canary_results", row)
    conn.commit()
    assert "latest.result_membership" in check(proof)["reasons"]


@pytest.mark.parametrize("column,value", [
    ("validation_status", "invalid_label"), ("is_failsafe", 1),
    ("prompt_hash", "0"*16), ("system_prompt_hash", "f"*16),
    ("response_model", None), ("response_model", "foreign-model"),
    ("system_fingerprint", "new-fingerprint"), ("system_fingerprint", ""),
    ("symbol", "FOREIGN"), ("primary_label", "invented"), ("label_decision", "propose"),
    ("label_confidence", 2), ("label_confidence", -.1), ("label_confidence", None),
    ("created_at_utc", "garbage"), ("created_at_utc", "2026-10-02T05:33:28"),
    ("created_at_utc", (NOW + timedelta(seconds=1)).isoformat()),
    ("created_at_utc", (NOW - timedelta(days=5)).isoformat()),
    ("raw_json", "[]"), ("raw_json", "null"),
    ("raw_json", '{"symbol":"AAA","symbol":"BBB"}'),
    ("raw_json", '{"symbol":"AAA","primary_label":"Momentum","decision":"WATCH","confidence":NaN}'),
    ("raw_json", '{"symbol":"FOREIGN","primary_label":"Momentum","decision":"WATCH","confidence":0.8}'),
])
def test_row_identity_validation_and_chronology(proof, column, value):
    row_update(proof, column, value)
    assert not check(proof)["ok"]


@pytest.mark.parametrize("column,value", [
    ("n_prompts", 0), ("n_prompts", 3), ("n_prompts", 1.5), ("n_parse_or_failsafe", 1),
    ("prompt_tokens", None), ("total_tokens", 0), ("completion_tokens", -1), ("total_tokens", 31),
    ("mean_confidence", .7), ("mean_confidence", None),
    ("response_models_json", "[]"), ("response_models_json", "null"),
    ("response_models_json", '["resolved-model-2026","resolved-model-2026"]'),
    ("system_fingerprints_json", '["phantom"]'),
    ("configured_model", "different"), ("corpus_version", 2), ("corpus_dir", "foreign/path"),
    ("finished_at_utc", None), ("finished_at_utc", "invalid"),
    ("finished_at_utc", "2026-10-02T05:33:28"),
    ("finished_at_utc", (NOW + timedelta(seconds=1)).isoformat()),
    ("finished_at_utc", (NOW - timedelta(days=5)).isoformat()),
    ("created_at_utc", (NOW - timedelta(days=5)).isoformat()),
    ("drift_tier", "tier_2"), ("drift_detail_json", "{}"),
    ("drift_detail_json", '{"reason":"no baseline pinned yet"}'),
])
def test_run_aggregate_and_scope(proof, column, value):
    run_update(proof, column, value)
    assert not check(proof)["ok"]


@pytest.mark.parametrize("mode", ["none", "duplicate", "mock", "missing_packet", "foreign_prompt", "new_model", "newer"])
def test_baseline_must_be_one_complete_real_prior_compatible_reference(proof, mode):
    if mode == "none":
        run_update(proof, "is_baseline", 0, proof.baseline)
    elif mode == "duplicate":
        run_update(proof, "is_baseline", 1)
    elif mode == "mock":
        run_update(proof, "is_mock", 1, proof.baseline)
    elif mode == "newer":
        run_update(proof, "finished_at_utc", NOW.isoformat(), proof.baseline)
    elif mode == "new_model":
        proof.journal.conn.execute("UPDATE canary_results SET response_model='different' WHERE run_id=?", (proof.baseline,))
        run_update(proof, "response_models_json", '["different"]', proof.baseline)
    elif mode == "missing_packet":
        proof.journal.conn.execute("DELETE FROM canary_results WHERE run_id=? AND packet_id='pkt_0'", (proof.baseline,))
    else:
        proof.journal.conn.execute("UPDATE canary_results SET prompt_hash='foreign' WHERE run_id=?", (proof.baseline,))
    proof.journal.conn.commit()
    assert not check(proof)["ok"]


def test_latest_original_not_older_complete_or_newer_confirmation(proof):
    run_update(proof, "finished_at_utc", None)
    row = proof.journal.one("SELECT * FROM canary_runs WHERE run_id=?", (proof.latest,))
    del row["id"]
    row.update(run_id="confirmation", confirmation_of=proof.latest, finished_at_utc=NOW.isoformat())
    proof.journal.insert("canary_runs", row)
    result = check(proof)
    assert result["run_id"] == proof.latest and not result["ok"]


def test_newest_original_unfinished_blocks_older_complete(proof):
    row = proof.journal.one("SELECT * FROM canary_runs WHERE run_id=?", (proof.latest,))
    del row["id"]
    row.update(run_id="new-unfinished", started_at_utc=NOW.isoformat(), created_at_utc=NOW.isoformat(), finished_at_utc=None)
    proof.journal.insert("canary_runs", row)
    result = check(proof)
    assert result["run_id"] == "new-unfinished" and not result["ok"]


@pytest.mark.parametrize("mode", ["missing_manifest", "missing_file", "extra_file", "changed_bytes", "duplicate_entry", "empty", "foreign_packet", "path_escape", "symlink"])
def test_stricter_manifest_membership_than_existing_loader(proof, mode):
    path = proof.corpus
    manifest = json.loads((path / "MANIFEST.json").read_text())
    if mode == "missing_manifest":
        (path / "MANIFEST.json").unlink()
    elif mode == "missing_file":
        (path / "pkt_0.json").unlink()
    elif mode == "extra_file":
        (path / "extra.json").write_text("{}")
    elif mode == "changed_bytes":
        with (path / "pkt_0.json").open("a") as f:
            f.write(" ")
    elif mode == "symlink":
        (path / "pkt_0.json").unlink()
        (path / "pkt_0.json").symlink_to(path / "pkt_1.json")
    else:
        if mode == "duplicate_entry":
            manifest["packets"].append(manifest["packets"][0])
        elif mode == "empty":
            manifest["packets"] = []
        elif mode == "path_escape":
            manifest["packets"][0]["file"] = "../outside.json"
        else:
            import hashlib
            fixture = json.loads((path / "pkt_0.json").read_text())
            fixture["packet_id"] = "foreign"
            (path / "pkt_0.json").write_text(json.dumps(fixture))
            manifest["packets"][0]["sha256"] = hashlib.sha256((path / "pkt_0.json").read_bytes()).hexdigest()
        (path / "MANIFEST.json").write_text(json.dumps(manifest))
    assert not check(proof)["ok"]


def test_stable_evidence_recomputed_age_expiry_not_authority_to_reuse_bool(proof):
    first = check(proof)
    proof.clock[0] += timedelta(seconds=10)
    second = check(proof)
    assert first["ok"] and second["ok"] and first["evidence_sha256"] == second["evidence_sha256"]
    assert second["age_seconds"] == first["age_seconds"] + 10
    proof.clock[0] = NOW + timedelta(days=9)
    expired = check(proof)
    assert not expired["ok"] and first["evidence_sha256"] == expired["evidence_sha256"]
    assert "latest.stale_or_future" in expired["reasons"]


def test_asof_cannot_be_naive_or_future(proof):
    assert not check(proof, as_of_utc=NOW.replace(tzinfo=None))["ok"]
    assert not check(proof, as_of_utc=NOW + timedelta(seconds=1))["ok"]


def test_hash_binds_raw_without_disclosing_it(proof):
    before = check(proof)
    row = proof.journal.one("SELECT raw_json FROM canary_results WHERE run_id=? AND packet_id='pkt_0'", (proof.latest,))
    raw = json.loads(row["raw_json"])
    raw["reason_for_label"] = "PRIVATE-NARRATIVE-DO-NOT-EMIT"
    row_update(proof, "raw_json", json.dumps(raw))
    after = check(proof)
    assert before["ok"] and after["ok"]
    assert before["evidence_sha256"] != after["evidence_sha256"]
    assert "PRIVATE-NARRATIVE" not in json.dumps(after)


def test_readonly_and_caller_transaction_remains_uncommitted(proof):
    conn = proof.journal.conn
    conn.execute("UPDATE canary_runs SET latency_ms_total=55 WHERE run_id=?", (proof.latest,))
    assert conn.in_transaction
    result = check(proof)
    assert result["ok"] and conn.in_transaction
    conn.rollback()
    assert proof.journal.scalar("SELECT latency_ms_total FROM canary_runs WHERE run_id=?", (proof.latest,)) is None
    conn.execute("PRAGMA query_only=ON")
    before = conn.total_changes
    assert check(proof)["ok"]
    assert conn.total_changes == before and not conn.in_transaction


def test_missing_schema_returns_unavailable_without_creating_tables(proof):
    conn = sqlite3.connect(":memory:")
    result = validate_canary_recovery(SimpleNamespace(conn=conn), proof.settings, corpus_dir=proof.corpus)
    assert not result["ok"] and result["reasons"] == ["evidence_unavailable:OperationalError"]
    assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("identity", ["fingerprint_missing_one", "models_swapped"])
def test_per_packet_identity_cannot_hide_behind_equal_aggregate_sets(proof, identity):
    conn = proof.journal.conn
    if identity == "fingerprint_missing_one":
        conn.execute("UPDATE canary_results SET system_fingerprint='fp-shared'")
        conn.execute("UPDATE canary_runs SET system_fingerprints_json='[\"fp-shared\"]'")
        conn.execute("UPDATE canary_results SET system_fingerprint=NULL WHERE run_id=? AND packet_id='pkt_0'", (proof.latest,))
    else:
        conn.execute("UPDATE canary_results SET response_model=CASE WHEN packet_id='pkt_0' THEN 'model-a' ELSE 'model-b' END")
        conn.execute("UPDATE canary_runs SET response_models_json='[\"model-a\",\"model-b\"]'")
        conn.execute("UPDATE canary_results SET response_model=CASE WHEN packet_id='pkt_0' THEN 'model-b' ELSE 'model-a' END WHERE run_id=?", (proof.latest,))
    conn.commit()
    assert not check(proof)["ok"]


def test_matching_per_packet_fingerprint_absence_remains_explicit(proof):
    conn = proof.journal.conn
    conn.execute("UPDATE canary_results SET system_fingerprint='fp-shared' WHERE packet_id='pkt_1'")
    conn.execute("UPDATE canary_runs SET system_fingerprints_json='[\"fp-shared\"]'")
    conn.commit()
    result = check(proof)
    assert result["ok"] and any("Some packets" in value for value in result["limitations"])
