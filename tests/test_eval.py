"""EVAL-1: the offline eval harness (§H.1 direct construction throughout --
no live network). Covers:
* corpus (select real/clean seed packets, additive write, idempotent
  re-write, load, ground-truth coverage),
* harness (replay through the REAL PlaybookClassifier, repeats, empty-corpus
  handling, every result stored including fail-safe, the live cost-cap
  refusal),
* report (parse rate, label agreement vs ground truth, categorical
  stability, the honest no-runs-yet empty state),
* daily brief integration,
* cost_guard now counts real eval replay calls too,
* the collect-only/no-decision-path law.

All offline, in-memory or tmp_path-backed. No real money, no network calls,
no writes under the real repo's data/eval/.
"""

from __future__ import annotations

import json
import os

import pytest

from alphaos.constants import LabelSource
from alphaos.eval.corpus import (
    CLEAN_SINCE_UTC, ground_truth_coverage, load_corpus, select_seed_packets, write_corpus,
)
from alphaos.eval.harness import run_eval
from alphaos.journal.journal_store import JournalStore
from alphaos.reports.eval_report import build_eval_report, render_markdown
from conftest import make_settings


@pytest.fixture
def journal():
    store = JournalStore(":memory:")
    yield store
    store.close()


def _seed_real_labelled_packet(journal, symbol="AAPL", primary_label="Momentum",
                               created_at_utc="2026-07-08T00:00:00+00:00"):
    """Directly constructs one real (is_mock=0), clean candidate_packets +
    candidate_labels row pair -- §H.1 direct construction, no scan needed."""
    from alphaos.util.ids import new_id

    packet_id = new_id("pkt")
    candidate_id = new_id("cand")
    packet_json = {
        "symbol": symbol, "last_price": 100.0, "direction": "long",
        "freshness_status": "usable", "spread_pct": 0.01, "liquidity_ok": True,
        "dollar_volume": 5_000_000.0, "change_pct": 2.0, "rel_volume": 1.5,
        "rel_strength_vs_spy": 0.5, "rel_strength_vs_qqq": 0.4,
        "near_day_high": True, "near_day_low": False, "gap_pct": 0.5,
        "structure_hint": "trend", "setup_hint": "breakout", "tradeable_volatility": True,
        "interest_score": 0.7, "shortlist_reason": "test", "momentum_score": 0.8,
        "missing_data_flags": [],
    }
    journal.conn.execute(
        "INSERT INTO candidate_packets (packet_id, candidate_id, symbol, interest_rank, "
        "packet_json, created_at_utc, created_at_sgt) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (packet_id, candidate_id, symbol, 1, json.dumps(packet_json), created_at_utc, created_at_utc),
    )
    journal.conn.execute(
        "INSERT INTO candidate_labels (label_id, candidate_id, packet_id, symbol, primary_label, "
        "label_decision, is_mock, created_at_utc, created_at_sgt) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (new_id("lbl"), candidate_id, packet_id, symbol, primary_label, "propose",
         created_at_utc, created_at_utc),
    )
    journal.conn.commit()
    return packet_id, candidate_id


# ================================================================== corpus
def test_select_seed_packets_excludes_mock_rows(journal):
    from alphaos.util.ids import new_id

    packet_id, candidate_id = new_id("pkt"), new_id("cand")
    journal.conn.execute(
        "INSERT INTO candidate_packets (packet_id, candidate_id, symbol, packet_json, "
        "created_at_utc, created_at_sgt) VALUES (?, ?, 'AAPL', '{}', ?, ?)",
        (packet_id, candidate_id, "2026-07-08T00:00:00+00:00", "2026-07-08T00:00:00+00:00"),
    )
    journal.conn.execute(
        "INSERT INTO candidate_labels (label_id, candidate_id, packet_id, symbol, primary_label, "
        "is_mock, created_at_utc, created_at_sgt) VALUES (?, ?, ?, 'AAPL', 'Momentum', 1, ?, ?)",
        (new_id("lbl"), candidate_id, packet_id, "2026-07-08T00:00:00+00:00", "2026-07-08T00:00:00+00:00"),
    )
    journal.conn.commit()

    seeds = select_seed_packets(journal)

    assert seeds == []


def test_select_seed_packets_excludes_pre_pr91_contaminated_rows(journal):
    _seed_real_labelled_packet(journal, symbol="OLD1", created_at_utc="2026-07-01T00:00:00+00:00")

    seeds = select_seed_packets(journal)

    assert seeds == []


def test_select_seed_packets_spreads_across_symbols_before_piling_on_one(journal):
    for i in range(3):
        _seed_real_labelled_packet(journal, symbol="AAPL", created_at_utc=f"2026-07-0{7+i}T00:00:00+00:00")
    _seed_real_labelled_packet(journal, symbol="MSFT", created_at_utc="2026-07-09T00:00:00+00:00")

    seeds = select_seed_packets(journal, limit=2)

    symbols = [s["symbol"] for s in seeds]
    assert "MSFT" in symbols  # the spread must win a slot over piling all 3 AAPL rows in first


def test_select_seed_packets_shapes_a_reconstructable_fixture(journal):
    _seed_real_labelled_packet(journal, symbol="AAPL")

    seeds = select_seed_packets(journal)

    assert len(seeds) == 1
    fixture = seeds[0]
    assert fixture["ground_truth_label"] is None
    assert fixture["provenance"]["historical_primary_label"] == "Momentum"
    assert fixture["last_price"] == 100.0  # a packet_json field flowed through
    assert "packet_id" in fixture and "candidate_id" in fixture


def test_select_seed_packets_never_selects_the_same_packet_twice(journal):
    """Regression guard: candidate_labels has no uniqueness constraint on
    packet_id -- if a packet ever gains a SECOND real label row (not
    reachable via today's write path, but not schema-prevented either), the
    naive join would return it twice, consuming two corpus slots. Pinned to
    the most recent real label per packet instead."""
    from alphaos.util.ids import new_id

    packet_id, candidate_id = new_id("pkt"), new_id("cand")
    packet_json = {"symbol": "AAPL", "last_price": 100.0}
    journal.conn.execute(
        "INSERT INTO candidate_packets (packet_id, candidate_id, symbol, packet_json, "
        "created_at_utc, created_at_sgt) VALUES (?, ?, 'AAPL', ?, ?, ?)",
        (packet_id, candidate_id, json.dumps(packet_json),
         "2026-07-08T00:00:00+00:00", "2026-07-08T00:00:00+00:00"),
    )
    # Two real label rows for the SAME packet_id (a hypothetical future
    # re-label/backfill scenario).
    for label, created_at in [("Momentum", "2026-07-08T00:00:00+00:00"),
                              ("Breakout", "2026-07-08T01:00:00+00:00")]:
        journal.conn.execute(
            "INSERT INTO candidate_labels (label_id, candidate_id, packet_id, symbol, "
            "primary_label, is_mock, created_at_utc, created_at_sgt) "
            "VALUES (?, ?, ?, 'AAPL', ?, 0, ?, ?)",
            (new_id("lbl"), candidate_id, packet_id, label, created_at, created_at),
        )
    journal.conn.commit()

    seeds = select_seed_packets(journal)

    assert len(seeds) == 1  # not 2
    assert seeds[0]["provenance"]["historical_primary_label"] == "Breakout"  # the most recent one


def test_write_corpus_is_additive_and_idempotent(tmp_path):
    corpus_dir = str(tmp_path / "corpus")
    packets = [
        {"packet_id": "pkt_a", "symbol": "AAPL", "ground_truth_label": None},
        {"packet_id": "pkt_b", "symbol": "MSFT", "ground_truth_label": None},
    ]

    manifest1, written1 = write_corpus(corpus_dir, packets, as_of_date="2026-07-09")
    assert len(written1) == 2
    assert manifest1["version"] == 1
    assert len(manifest1["packets"]) == 2

    # Re-write the SAME packets -- must not duplicate or bump version.
    manifest2, written2 = write_corpus(corpus_dir, packets, as_of_date="2026-07-10")
    assert written2 == []
    assert manifest2["version"] == 1
    assert len(manifest2["packets"]) == 2

    # Adding a genuinely NEW packet DOES bump version and appends.
    manifest3, written3 = write_corpus(
        corpus_dir, packets + [{"packet_id": "pkt_c", "symbol": "NVDA", "ground_truth_label": None}],
        as_of_date="2026-07-11",
    )
    assert written3 == ["pkt_c.json"]
    assert manifest3["version"] == 2
    assert len(manifest3["packets"]) == 3


def test_write_corpus_rejects_a_path_traversal_packet_id(tmp_path):
    """Regression guard (scope/safety audit F-1, reproduced): a malformed
    packet_id must never be allowed to shape a filesystem path, even though
    every WIRED caller today only ever supplies internally-generated,
    always-well-formed ids -- defense in depth, matching the standard
    TEXT-0 already established for external-input-into-a-path (accession_no)."""
    corpus_dir = str(tmp_path / "corpus")
    evil = "../../../../../../../../tmp/eval_corpus_escape/PWNED"

    with pytest.raises(ValueError, match="malformed packet_id"):
        write_corpus(corpus_dir, [{"packet_id": evil, "symbol": "AAPL", "ground_truth_label": None}],
                     as_of_date="2026-07-09")

    assert not os.path.exists("/tmp/eval_corpus_escape")


def test_write_corpus_never_overwrites_an_operator_adjudicated_fixture(tmp_path):
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [{"packet_id": "pkt_a", "symbol": "AAPL", "ground_truth_label": None}],
                 as_of_date="2026-07-09")

    # Simulate the operator hand-adjudicating it.
    path = os.path.join(corpus_dir, "pkt_a.json")
    with open(path, encoding="utf-8") as f:
        fixture = json.load(f)
    fixture["ground_truth_label"] = "Momentum"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fixture, f)

    # A later build offering the SAME packet_id (e.g. re-selected from the
    # journal) must never clobber the operator's adjudication.
    write_corpus(corpus_dir, [{"packet_id": "pkt_a", "symbol": "AAPL", "ground_truth_label": None}],
                 as_of_date="2026-07-10")

    with open(path, encoding="utf-8") as f:
        after = json.load(f)
    assert after["ground_truth_label"] == "Momentum"


def test_load_corpus_missing_returns_none_and_empty(tmp_path):
    manifest, packets = load_corpus(str(tmp_path / "does_not_exist"))
    assert manifest is None
    assert packets == []


def test_load_corpus_round_trips_write_corpus(tmp_path):
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [{"packet_id": "pkt_a", "symbol": "AAPL", "ground_truth_label": None}],
                 as_of_date="2026-07-09")

    manifest, packets = load_corpus(corpus_dir)

    assert manifest["version"] == 1
    assert len(packets) == 1
    assert packets[0]["symbol"] == "AAPL"


def test_ground_truth_coverage_counts_only_truthy_labels():
    packets = [
        {"ground_truth_label": "Momentum"},
        {"ground_truth_label": None},
        {"ground_truth_label": ""},
    ]
    assert ground_truth_coverage(packets) == {"total": 3, "labeled": 1}


# ============================================================ harness
def test_run_eval_with_a_missing_corpus_is_a_safe_noop_with_an_error(tmp_path, journal):
    settings = make_settings()
    result = run_eval(journal, settings, corpus_dir=str(tmp_path / "nope"))

    assert "error" in result
    assert result["n_results"] == 0
    assert journal.count_rows("eval_runs") == 0


def test_run_eval_isolates_a_malformed_fixture_and_processes_the_rest(tmp_path, journal):
    """Regression guard, self-caught during audit prep: a corpus fixture
    missing a required CandidatePacket field (an operator hand-edit typo)
    must count as one n_corpus_errors entry and let the run continue to the
    remaining packets, never crash the whole run."""
    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    _seed_real_labelled_packet(journal, symbol="AAPL")
    _seed_real_labelled_packet(journal, symbol="MSFT")
    seeds = select_seed_packets(journal)
    write_corpus(corpus_dir, seeds, as_of_date="2026-07-09")

    # Corrupt one fixture in place: drop a required field.
    broken_file = os.path.join(corpus_dir, f"{seeds[0]['packet_id']}.json")
    with open(broken_file, encoding="utf-8") as f:
        broken = json.load(f)
    del broken["last_price"]  # a required positional CandidatePacket field
    with open(broken_file, "w", encoding="utf-8") as f:
        json.dump(broken, f)

    result = run_eval(journal, settings, corpus_dir=corpus_dir, repeats=1)

    assert "error" not in result  # the run itself must not crash/error out
    assert result["n_corpus_errors"] == 1
    assert result["n_results"] == 1  # the second, well-formed packet still processed
    run_row = journal.one("SELECT * FROM eval_runs WHERE run_id = ?", (result["run_id"],))
    assert run_row["finished_at_utc"] is not None


def test_run_eval_isolates_a_wrong_type_field_that_only_fails_inside_classify(tmp_path, journal):
    """Regression guard for a MEDIUM the correctness audit reproduced: a
    fixture with a WRONG-TYPE field (not a missing one) reconstructs a
    CandidatePacket FINE -- Python dataclasses don't enforce field types at
    construction -- and only raises once PlaybookClassifier's own mock path
    tries to coerce it (float(momentum_score)). An earlier, narrower version
    of the isolation guard wrapped only _reconstruct_packet and let this
    escape, aborting the whole run. Must now be isolated the same as a
    missing-field fixture."""
    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    _seed_real_labelled_packet(journal, symbol="AAPL")
    _seed_real_labelled_packet(journal, symbol="MSFT")
    seeds = select_seed_packets(journal)
    write_corpus(corpus_dir, seeds, as_of_date="2026-07-09")

    broken_file = os.path.join(corpus_dir, f"{seeds[0]['packet_id']}.json")
    with open(broken_file, encoding="utf-8") as f:
        broken = json.load(f)
    broken["momentum_score"] = "not-a-number"  # wrong TYPE, not missing -- reconstructs fine
    with open(broken_file, "w", encoding="utf-8") as f:
        json.dump(broken, f)

    result = run_eval(journal, settings, corpus_dir=corpus_dir, repeats=1)

    assert "error" not in result  # the run itself must not crash/error out
    assert result["n_corpus_errors"] == 1
    assert result["n_results"] == 1  # the second, well-formed packet still processed
    run_row = journal.one("SELECT * FROM eval_runs WHERE run_id = ?", (result["run_id"],))
    assert run_row["finished_at_utc"] is not None


def test_run_eval_rejects_non_positive_repeats(tmp_path, journal):
    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    _seed_real_labelled_packet(journal, symbol="AAPL")
    seeds = select_seed_packets(journal)
    write_corpus(corpus_dir, seeds, as_of_date="2026-07-09")

    for bad_repeats in (0, -1, -5):
        result = run_eval(journal, settings, corpus_dir=corpus_dir, repeats=bad_repeats)
        assert "error" in result
        assert "repeats" in result["error"].lower()
        assert result["n_results"] == 0


def test_run_eval_refuses_when_planned_calls_would_overshoot_the_cap(tmp_path, journal):
    """Regression guard: the cap is checked once at the START, so a large
    corpus x repeats could otherwise blow far past the cap in a single run
    even with a little headroom left. A pre-flight magnitude check must
    refuse the whole run up front rather than partially overshoot."""
    from alphaos.util.ids import new_id

    settings = make_settings(
        ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-test-key-not-real",
        SCHEDULER_AI_COST_CAP_CALLS_PER_30D="50",
        # EXP-1: SHADOW_AI_CAP_CALLS_PER_30D's own joint-validation (<=25% of
        # the shared pool) must clear THIS lowered global cap too -- its
        # default of 500 only clears the default global cap of 2000.
        SHADOW_AI_CAP_CALLS_PER_30D="12",
    )
    corpus_dir = str(tmp_path / "corpus")
    for i in range(3):
        _seed_real_labelled_packet(journal, symbol=f"SYM{i}")
    seeds = select_seed_packets(journal)
    write_corpus(corpus_dir, seeds, as_of_date="2026-07-09")

    # 43 already used -- only 7 of headroom left under the cap of 50.
    for _ in range(43):
        journal.conn.execute(
            "INSERT INTO openai_evaluations (eval_id, candidate_id, symbol, model, direction, "
            "decision, reasoning_summary, is_mock, created_at_utc, created_at_sgt) "
            "VALUES (?, ?, 'AAPL', 'gpt-4o-mini', 'long', 'reject', 'x', 0, ?, ?)",
            (new_id("eval"), new_id("cand"), "2026-07-09T00:00:00+00:00", "2026-07-09T00:00:00+00:00"),
        )
    journal.conn.commit()

    # 3 packets x repeats=50 = 150 planned calls -- way over the 7 remaining.
    result = run_eval(journal, settings, corpus_dir=corpus_dir, repeats=50)

    assert "error" in result
    assert "150" in result["error"]
    assert journal.count_rows("eval_runs") == 0
    assert journal.count_rows("eval_results") == 0


def test_run_eval_stores_one_result_per_packet_per_repeat(tmp_path, journal):
    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    _seed_real_labelled_packet(journal, symbol="AAPL")
    seeds = select_seed_packets(journal)
    write_corpus(corpus_dir, seeds, as_of_date="2026-07-09")

    result = run_eval(journal, settings, corpus_dir=corpus_dir, repeats=3)

    assert "error" not in result
    assert result["n_packets"] == 1
    assert result["n_results"] == 3
    assert journal.count_rows("eval_results", "run_id = ?", (result["run_id"],)) == 3
    run_row = journal.one("SELECT * FROM eval_runs WHERE run_id = ?", (result["run_id"],))
    assert run_row["finished_at_utc"] is not None
    assert run_row["n_packets"] == 1
    assert run_row["repeats"] == 3


def test_run_eval_stores_fail_safe_results_never_discards_them(tmp_path, journal, monkeypatch):
    """The spec's own words: 'raw={"fail_safe": reason} rows are precisely
    the examples the harness needs most -- retention starts here.' Force a
    fail-safe classification and confirm it lands in eval_results intact."""
    from alphaos.ai.playbook_classifier import PlaybookClassification

    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    _seed_real_labelled_packet(journal, symbol="AAPL")
    seeds = select_seed_packets(journal)
    write_corpus(corpus_dir, seeds, as_of_date="2026-07-09")

    fail_safe_result = PlaybookClassification(
        label_id="lbl_test", candidate_id="cand_test", symbol="AAPL",
        primary_label="Other/Unclassified", secondary_labels=[], candidate_tags=[],
        risk_tags=["label_unavailable"], direction="long", label_decision="reject",
        confidence=0.0, reason_for_label="AI label unavailable (timeout); failed safe to reject.",
        thesis_stub="", invalidation="", main_risk="no AI classification available",
        missing_context=["ai_label"], suggested_new_tags=[], label_version="v1",
        label_source=LabelSource.FAIL_SAFE.value, validation_status="timeout",
        model="mock", is_mock=True, raw={"fail_safe": "timeout"},
    )
    monkeypatch.setattr(
        "alphaos.ai.playbook_classifier.PlaybookClassifier.classify",
        lambda self, packet: fail_safe_result,
    )

    result = run_eval(journal, settings, corpus_dir=corpus_dir, repeats=1)

    assert result["n_fail_safe"] == 1
    row = journal.one("SELECT * FROM eval_results WHERE run_id = ?", (result["run_id"],))
    assert row["label_source"] == LabelSource.FAIL_SAFE.value
    assert json.loads(row["raw_json"]) == {"fail_safe": "timeout"}


def test_run_eval_refuses_a_live_run_once_the_cost_cap_is_reached(tmp_path, journal):
    """A live (non-mock) replay reuses the SAME real API call the labeller
    makes at scan time -- it must respect the same trailing-30-day cap, not
    silently spend past it because it's a different call SITE."""
    from alphaos.util.ids import new_id

    settings = make_settings(
        ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-test-key-not-real",
        SCHEDULER_AI_COST_CAP_CALLS_PER_30D="50",
        # EXP-1: SHADOW_AI_CAP_CALLS_PER_30D's own joint-validation (<=25% of
        # the shared pool) must clear THIS lowered global cap too -- its
        # default of 500 only clears the default global cap of 2000.
        SHADOW_AI_CAP_CALLS_PER_30D="12",
    )
    corpus_dir = str(tmp_path / "corpus")
    _seed_real_labelled_packet(journal, symbol="AAPL")
    seeds = select_seed_packets(journal)
    write_corpus(corpus_dir, seeds, as_of_date="2026-07-09")

    for _ in range(50):
        journal.conn.execute(
            "INSERT INTO openai_evaluations (eval_id, candidate_id, symbol, model, direction, "
            "decision, reasoning_summary, is_mock, created_at_utc, created_at_sgt) "
            "VALUES (?, ?, 'AAPL', 'gpt-4o-mini', 'long', 'reject', 'x', 0, ?, ?)",
            (new_id("eval"), new_id("cand"), "2026-07-09T00:00:00+00:00", "2026-07-09T00:00:00+00:00"),
        )
    journal.conn.commit()

    result = run_eval(journal, settings, corpus_dir=corpus_dir, repeats=1)

    assert "error" in result
    assert "cost cap" in result["error"].lower()
    assert result["n_results"] == 0
    assert journal.count_rows("eval_runs") == 0  # refused before even starting a run row


# ============================================================= report
def test_eval_report_no_runs_yet(journal):
    rep = build_eval_report(journal)
    assert rep == {"status": "no_runs_yet"}
    assert "No eval runs yet" in render_markdown(rep)


def test_eval_report_parse_rate_and_label_agreement(tmp_path, journal):
    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    _seed_real_labelled_packet(journal, symbol="AAPL")
    seeds = select_seed_packets(journal)
    write_corpus(corpus_dir, seeds, as_of_date="2026-07-09")
    result = run_eval(journal, settings, corpus_dir=corpus_dir, repeats=1)

    rep = build_eval_report(journal, run_id=result["run_id"], corpus_dir=corpus_dir)

    assert rep["status"] == "ok"
    assert rep["parse_rate"] == 1.0
    assert rep["ground_truth_coverage"] == {"total": 1, "labeled": 0}
    assert rep["label_agreement"] is None  # no ground truth adjudicated yet
    assert rep["categorical_stability"] is None  # repeats=1, nothing to compare
    md = render_markdown(rep)
    assert "Parse rate: 100.0%" in md
    assert "N/A" in md  # label agreement section


def test_eval_report_label_agreement_once_ground_truth_is_adjudicated(tmp_path, journal):
    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    _seed_real_labelled_packet(journal, symbol="AAPL")
    seeds = select_seed_packets(journal)
    write_corpus(corpus_dir, seeds, as_of_date="2026-07-09")
    result = run_eval(journal, settings, corpus_dir=corpus_dir, repeats=1)

    row = journal.one("SELECT * FROM eval_results WHERE run_id = ?", (result["run_id"],))
    packet_path = os.path.join(corpus_dir, f"{row['packet_id']}.json")
    with open(packet_path, encoding="utf-8") as f:
        fixture = json.load(f)
    fixture["ground_truth_label"] = row["primary_label"]  # simulate a MATCHING adjudication
    with open(packet_path, "w", encoding="utf-8") as f:
        json.dump(fixture, f)

    rep = build_eval_report(journal, run_id=result["run_id"], corpus_dir=corpus_dir)

    assert rep["label_agreement"] == 1.0
    assert rep["label_agreement_n"] == 1
    assert rep["ground_truth_coverage"]["labeled"] == 1


def test_eval_report_categorical_stability_with_a_real_disagreement(tmp_path, journal, monkeypatch):
    """Direct construction of a KNOWN disagreement: 3 of 5 repeats return
    the majority label -- stability must compute to exactly 0.6.
    Deliberately chosen so neither the FIRST ('Breakout') nor the LAST
    ('Dip Buy') repeat is the true mode ('Momentum') -- audit-caught: an
    earlier sequence's mode happened to equal BOTH the correct answer AND
    a naive "just count matches to the first/last repeat" shortcut, so it
    would have passed even against a wrong implementation. This sequence
    gives 0.6 correctly and 0.2 under either positional-shortcut bug."""
    from alphaos.ai.playbook_classifier import PlaybookClassification

    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    _seed_real_labelled_packet(journal, symbol="AAPL")
    seeds = select_seed_packets(journal)
    write_corpus(corpus_dir, seeds, as_of_date="2026-07-09")

    labels_in_order = ["Breakout", "Momentum", "Momentum", "Momentum", "Dip Buy"]
    call_count = {"n": 0}

    def _fake_classify(self, packet):
        label = labels_in_order[call_count["n"] % len(labels_in_order)]
        call_count["n"] += 1
        return PlaybookClassification(
            label_id="lbl", candidate_id=packet.candidate_id, symbol=packet.symbol,
            primary_label=label, secondary_labels=[], candidate_tags=[], risk_tags=[],
            direction="long", label_decision="watch", confidence=0.5, reason_for_label="x",
            thesis_stub="", invalidation="", main_risk="", missing_context=[],
            suggested_new_tags=[], label_version="v1", label_source=LabelSource.MOCK.value,
            validation_status="passed", model="mock", is_mock=True, raw={"mock": True},
        )

    monkeypatch.setattr(
        "alphaos.ai.playbook_classifier.PlaybookClassifier.classify", _fake_classify,
    )

    result = run_eval(journal, settings, corpus_dir=corpus_dir, repeats=5)
    rep = build_eval_report(journal, run_id=result["run_id"], corpus_dir=corpus_dir)

    assert rep["categorical_stability"] == 0.6  # 3/5 agree with the true mode ("Momentum")


# ================================================== daily brief integration
def test_daily_brief_eval_health_none_when_no_runs(orchestrator):
    from alphaos.reports.daily_brief import build_daily_brief, render_markdown as render_brief

    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    md = render_brief(brief)

    assert brief["eval_health"] is None
    assert "## Eval harness" not in md


def test_daily_brief_eval_health_populated_after_a_run(tmp_path, orchestrator):
    from alphaos.reports.daily_brief import build_daily_brief, render_markdown as render_brief

    _seed_real_labelled_packet(orchestrator.journal, symbol="AAPL")
    corpus_dir = str(tmp_path / "corpus")
    orchestrator.eval_corpus_build(corpus_dir=corpus_dir, limit=10)
    orchestrator.run_eval(corpus_dir=corpus_dir, repeats=1)

    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    md = render_brief(brief)

    assert brief["eval_health"] is not None
    assert "## Eval harness" in md


# ===================================================================== cost guard
def test_cost_guard_counts_real_eval_results(journal):
    from alphaos.scheduler import cost_guard
    from alphaos.util.ids import new_id

    before = cost_guard.calls_in_last_30_days(journal)
    journal.insert("eval_results", {
        "result_id": new_id("evalres"), "run_id": new_id("evalrun"), "packet_id": new_id("pkt"),
        "is_mock": 0,
    })
    after = cost_guard.calls_in_last_30_days(journal)

    assert after == before + 1


def test_cost_guard_excludes_mock_eval_results(journal):
    from alphaos.scheduler import cost_guard
    from alphaos.util.ids import new_id

    before = cost_guard.calls_in_last_30_days(journal)
    journal.insert("eval_results", {
        "result_id": new_id("evalres"), "run_id": new_id("evalrun"), "packet_id": new_id("pkt"),
        "is_mock": 1,
    })
    after = cost_guard.calls_in_last_30_days(journal)

    assert after == before


# =============================================== no-decision-path grep guard
def test_eval_module_never_touches_the_order_submission_surface():
    import pathlib

    banned = ("execute_proposal", "approve_proposal", "close_position",
              "submit_bracket", "submit_order", "place_order")
    eval_dir = pathlib.Path(__file__).resolve().parents[1] / "alphaos" / "eval"
    for py_file in eval_dir.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{py_file.name} references {token!r}"


def test_eval_module_never_imports_gate_or_execution_paths():
    import pathlib

    banned = ("alphaos.execution", "alphaos.gates", "from alphaos.risk")
    eval_dir = pathlib.Path(__file__).resolve().parents[1] / "alphaos" / "eval"
    for py_file in eval_dir.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{py_file.name} references {token!r}"


# ============================================================= CLI wiring
def test_cli_eval_commands_are_valid_choices():
    from alphaos.__main__ import build_parser

    p = build_parser()
    args = p.parse_args(["eval_corpus_build", "--limit", "10"])
    assert args.command == "eval_corpus_build" and args.limit == 10
    args = p.parse_args(["eval", "--repeats", "3"])
    assert args.command == "eval" and args.repeats == 3
    args = p.parse_args(["eval_report"])
    assert args.command == "eval_report"


def test_clean_since_utc_matches_the_pr91_merge_instant():
    """Documents the exact contamination boundary this module relies on --
    if PR9.1's merge time is ever misremembered/changed, this constant is
    the one place that would need updating, and this test would flag drift
    against the docstring's own stated commit."""
    assert CLEAN_SINCE_UTC == "2026-07-06T14:45:00+00:00"
