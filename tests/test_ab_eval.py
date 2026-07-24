"""AB-EVAL-1: primary-evaluator A/B replay harness
(docs/roadmap/alphaos-evaluator-replay-and-coherence-specs.md, "## AB-EVAL-1
-- Primary-evaluator A/B replay harness (shadow, read-only)"). Replays
IDENTICAL frozen openai_evaluations snapshots through two models via the
production OpenAIClient call path. HERMETIC throughout -- mock mode only
(or direct construction against a real journal for the ATR/floor tests),
no real network calls, no wall-clock dependence.
"""

from __future__ import annotations

import json
import os

import pytest

from alphaos.ab_eval.corpus import (
    CANDIDATE_CREATION_FIELDS, CANDIDATE_CREATION_SHADOW_FIELDS,
    CANDIDATE_IN_MEMORY_EXTRA_FIELDS, CorpusTamperedError, KILL_ZONE_DATES,
    load_corpus, select_default_corpus, write_corpus,
)
from alphaos.ab_eval.replay import NO_ATR, RR_FLOOR, UNKNOWN, ReplayResult, replay_packet
from alphaos.ab_eval.run import run_ab_eval
from alphaos.ai.openai_client import ATR_RULES_V1, OpenAIClient, OpenAIEvaluation
from alphaos.constants import Decision, ReasonCode, TradeDirection
from alphaos.reports.ab_eval_report import build_ab_eval_report, render_markdown
from alphaos.scheduler import cost_guard
from alphaos.util.ids import new_id
from conftest import make_settings

_FIXTURE = {
    "eval_id": "eval_abevaltest01",
    "candidate_id": "cand_abevaltest01",
    "symbol": "AAPL",
    "candidate": {
        "candidate_id": "cand_abevaltest01", "symbol": "AAPL", "direction": "long",
        "momentum_score": 0.7, "rel_strength": 0.5, "unusual_volume": 1.2,
    },
    "snapshot": {"last_price": 100.0, "volume": 1_000_000},
    "freshness_status": "usable",
    "provenance": {
        "original_model": "gpt-5.4-mini", "original_decision": "propose",
        "original_created_at_utc": "2026-07-09T15:00:00+00:00",
    },
}


# ------------------------------------------------------------------- corpus
def test_load_corpus_empty_when_never_built(tmp_path):
    manifest, fixtures = load_corpus(str(tmp_path / "does_not_exist"))
    assert manifest is None
    assert fixtures == []


def test_write_corpus_is_additive_never_overwrites(tmp_path):
    corpus_dir = str(tmp_path / "corpus")
    manifest1, written1 = write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-20")
    assert written1 == ["eval_abevaltest01.json"]
    assert manifest1["version"] == 1

    mutated = {**_FIXTURE, "symbol": "MUTATED"}
    manifest2, written2 = write_corpus(corpus_dir, [mutated], as_of_date="2026-07-21")
    assert written2 == []  # same eval_id -- never overwritten
    _, fixtures = load_corpus(corpus_dir)
    assert fixtures[0]["symbol"] == "AAPL"  # original content survives


def test_write_corpus_manifest_sha256_matches_file_content(tmp_path):
    corpus_dir = str(tmp_path / "corpus")
    manifest, _ = write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-20")
    import hashlib
    with open(f"{corpus_dir}/eval_abevaltest01.json", "rb") as f:
        content = f.read()
    expected_sha = hashlib.sha256(content).hexdigest()
    entry = next(e for e in manifest["evaluations"] if e["file"] == "eval_abevaltest01.json")
    assert entry["sha256"] == expected_sha


def test_write_corpus_refuses_malformed_eval_id(tmp_path):
    with pytest.raises(ValueError):
        write_corpus(str(tmp_path / "corpus"), [{**_FIXTURE, "eval_id": "../../etc/passwd"}],
                     as_of_date="2026-07-20")


def test_load_corpus_detects_a_tampered_fixture(tmp_path):
    """Spec's own required test: 'frozen manifest tamper -> loud failure'.
    A fixture file modified on disk AFTER write_corpus() froze its
    MANIFEST sha256 must be caught, never silently replayed."""
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-20")

    tampered = {**_FIXTURE, "symbol": "TAMPERED"}
    with open(os.path.join(corpus_dir, "eval_abevaltest01.json"), "w", encoding="utf-8") as f:
        json.dump(tampered, f)

    with pytest.raises(CorpusTamperedError, match="sha256"):
        load_corpus(corpus_dir)


def test_load_corpus_untampered_fixtures_load_normally(tmp_path):
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-20")

    manifest, fixtures = load_corpus(corpus_dir)

    assert manifest is not None
    assert len(fixtures) == 1


def test_run_ab_eval_propagates_corpus_tampered_error_uncaught(tmp_path, journal):
    """The tamper error must NOT be swallowed into a returned {"error": ...}
    dict -- only an uncaught exception reaching a job runner's own handler
    marks a run fuse-eligible-failed (same law as CANARY)."""
    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-20")
    with open(os.path.join(corpus_dir, "eval_abevaltest01.json"), "w", encoding="utf-8") as f:
        json.dump({**_FIXTURE, "symbol": "TAMPERED"}, f)

    with pytest.raises(CorpusTamperedError):
        run_ab_eval(journal, settings, ["gpt-5.4-mini", "gpt-5.6-luna"], corpus_dir=corpus_dir)


def _seed_eval_row(journal, eval_id, candidate_id, symbol, created_at_utc,
                   decision="reject", direction="long", momentum_score=0.3):
    journal.insert("candidates", {
        "candidate_id": candidate_id, "symbol": symbol, "direction": direction,
        "momentum_score": momentum_score,
    })
    journal.insert("openai_evaluations", {
        "eval_id": eval_id, "candidate_id": candidate_id, "symbol": symbol,
        "model": "gpt-5.4-mini", "decision": decision, "is_mock": 0,
        "snapshot_json": json.dumps({"last_price": 50.0}),
        "data_freshness_status": "usable",
        "created_at_utc": created_at_utc, "created_at_sgt": created_at_utc,
    })


def test_select_default_corpus_includes_all_kill_zone_rows_unconditionally(journal):
    for i, date in enumerate(KILL_ZONE_DATES):
        _seed_eval_row(journal, f"eval_kz{i:02d}", f"cand_kz{i:02d}", "AAPL",
                       f"{date}T15:00:00+00:00", decision="reject")
    fixtures = select_default_corpus(journal, total=1)  # total smaller than kill-zone count
    assert len(fixtures) == 2  # both kill-zone rows included regardless of `total`
    assert {f["eval_id"] for f in fixtures} == {"eval_kz00", "eval_kz01"}


def test_select_default_corpus_excludes_rows_without_snapshot_or_mock(journal):
    _seed_eval_row(journal, "eval_kz00", "cand_kz00", "AAPL", "2026-07-09T15:00:00+00:00")
    journal.insert("candidates", {"candidate_id": "cand_nosnap", "symbol": "MSFT"})
    journal.insert("openai_evaluations", {
        "eval_id": "eval_nosnap", "candidate_id": "cand_nosnap", "symbol": "MSFT",
        "decision": "reject", "is_mock": 0, "snapshot_json": None,
        "created_at_utc": "2026-07-09T16:00:00+00:00", "created_at_sgt": "2026-07-09T16:00:00+00:00",
    })
    journal.insert("candidates", {"candidate_id": "cand_mock", "symbol": "NVDA"})
    journal.insert("openai_evaluations", {
        "eval_id": "eval_mock", "candidate_id": "cand_mock", "symbol": "NVDA",
        "decision": "reject", "is_mock": 1, "snapshot_json": json.dumps({"last_price": 1.0}),
        "created_at_utc": "2026-07-09T17:00:00+00:00", "created_at_sgt": "2026-07-09T17:00:00+00:00",
    })

    fixtures = select_default_corpus(journal, total=60)
    assert {f["eval_id"] for f in fixtures} == {"eval_kz00"}


def test_select_default_corpus_stratifies_later_rows_by_decision_and_date(journal):
    for i, date in enumerate(KILL_ZONE_DATES):
        _seed_eval_row(journal, f"eval_kz{i:02d}", f"cand_kz{i:02d}", "AAPL",
                       f"{date}T15:00:00+00:00", decision="reject")
    # 6 later rows across 2 dates x 3 decisions
    n = 0
    for date in ("2026-07-13", "2026-07-14"):
        for decision in ("propose", "watch", "reject"):
            n += 1
            _seed_eval_row(journal, f"eval_later{n:02d}", f"cand_later{n:02d}", "MSFT",
                           f"{date}T15:00:00+00:00", decision=decision)

    fixtures = select_default_corpus(journal, total=4)  # 2 kill-zone + 2 later
    assert len(fixtures) == 4
    kill_zone_ids = {f["eval_id"] for f in fixtures if f["eval_id"].startswith("eval_kz")}
    later_ids = {f["eval_id"] for f in fixtures if f["eval_id"].startswith("eval_later")}
    assert kill_zone_ids == {"eval_kz00", "eval_kz01"}
    assert len(later_ids) == 2


def test_row_to_fixture_freezes_candidate_and_snapshot(journal):
    _seed_eval_row(journal, "eval_kz00", "cand_kz00", "AAPL", "2026-07-09T15:00:00+00:00",
                   momentum_score=0.9)
    fixtures = select_default_corpus(journal, total=1)
    assert fixtures[0]["candidate"]["momentum_score"] == 0.9
    assert fixtures[0]["candidate"]["symbol"] == "AAPL"
    assert fixtures[0]["snapshot"] == {"last_price": 50.0}
    assert "id" not in fixtures[0]["candidate"]  # DB bookkeeping column dropped


# All downstream pipeline-outcome columns an attacker/late pipeline stage
# could have written onto the candidates row by corpus-build time. Values
# are distinctive sentinels so the leak assertion can't pass by accident.
_OUTCOME_COLUMN_SENTINELS = {
    "status": "LEAKME_STATUS",
    "status_reason": "LEAKME_STATUS_REASON",
    "reject_reason": "LEAKME_REJECT_REASON",
    "block_reason": "LEAKME_BLOCK_REASON",
    "watch_reason": "LEAKME_WATCH_REASON",
    "decision_adjustment": "LEAKME_DECISION_ADJUSTMENT",
    "decision_adjustment_reason": "LEAKME_ADJUSTMENT_REASON",
    "card_assignment_status": "LEAKME_CARD_ASSIGNMENT_STATUS",
    "card_assignment_ref": "LEAKME_CARD_ASSIGNMENT_REF",
    "primary_label": "LEAKME_PRIMARY_LABEL",
    "label_decision": "LEAKME_LABEL_DECISION",
    "label_confidence": 0.987654,
    "sentiment_label": "LEAKME_SENTIMENT",
    "polarity_label": "LEAKME_POLARITY",
    "polarity_alignment": "LEAKME_POLARITY_ALIGNMENT",
    "narrative_driver_type": "LEAKME_NARRATIVE_DRIVER",
    "arming_classification": "LEAKME_ARMING",
    "shortlist_reason": "momentum+relvol",  # creation field -- must SURVIVE (control)
}


def test_frozen_candidate_never_leaks_pipeline_outcomes_into_prompt(journal):
    """Audit HIGH (2026-07-20): the frozen candidate dict is serialized into
    the REAL no-news prompt on a live replay (prompt_templates._public keeps
    every non-underscore key), so a fixture built from a candidates row that
    the pipeline has since stamped with its own verdicts must NOT carry any
    of those values into the prompt text -- same failure class as the logged
    2026-07-06 _public prompt-leak incident, invisible to mock-only tests
    because mock never builds a prompt. This test runs the REAL production
    prompt builder on the frozen candidate and asserts every sentinel is
    absent."""
    from alphaos.ai.prompt_templates import build_no_news_user_prompt

    journal.insert("candidates", {
        "candidate_id": "cand_leak01", "symbol": "AAPL", "direction": "long",
        "momentum_score": 0.7, **_OUTCOME_COLUMN_SENTINELS,
    })
    journal.insert("openai_evaluations", {
        "eval_id": "eval_leak01", "candidate_id": "cand_leak01", "symbol": "AAPL",
        "model": "gpt-5.4-mini", "decision": "reject", "is_mock": 0,
        "snapshot_json": json.dumps({"last_price": 50.0}),
        "data_freshness_status": "usable",
        "created_at_utc": "2026-07-09T15:00:00+00:00",
        "created_at_sgt": "2026-07-09T23:00:00+08:00",
    })

    fixtures = select_default_corpus(journal, total=1)
    assert len(fixtures) == 1
    frozen = fixtures[0]["candidate"]

    prompt = build_no_news_user_prompt(frozen, fixtures[0]["snapshot"], "usable")

    assert "LEAKME" not in prompt          # every text outcome sentinel absent
    assert "0.987654" not in prompt        # the numeric one too (label_confidence)
    # Post-evaluation status mutations pinned back to the creation constant:
    assert frozen["status"] == "detected"
    assert frozen["status_reason"] == "detected"
    # Control: a genuine creation-time field with the same provenance as the
    # sentinels (same row, same freeze pass) DID survive into the prompt --
    # proving the assertion above isn't vacuous.
    assert "momentum+relvol" in prompt
    assert frozen["momentum_score"] == 0.7


def test_candidate_whitelist_matches_scanner_creation_insert():
    """Lockstep law for the whitelist (audit HIGH, 2026-07-20): the frozen
    candidate field set must equal the scanner's OWN creation-insert column
    set, introspected from the source (AST), never hand-copied -- so a
    future scan-time column can't silently drop out of fixtures, and a
    future post-hoc column can't silently leak in. Also pins the two
    documented in-memory extras to their real origins: the scanner's
    ``cand["last_price"] = ...`` post-insert stamp and the orchestrator's
    ``["interest_rank"] = ...`` pre-evaluation assignment."""
    import ast
    import inspect

    from alphaos import orchestrator as orchestrator_module
    from alphaos.scanner import candidate_scanner

    tree = ast.parse(inspect.getsource(candidate_scanner))
    core_dicts, update_dicts, subscript_assigns = [], [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Name) and target.id == "cand"
                        and isinstance(node.value, ast.Dict)):
                    core_dicts.append(
                        [k.value for k in node.value.keys if isinstance(k, ast.Constant)])
                if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                        and target.value.id == "cand" and isinstance(target.slice, ast.Constant)):
                    subscript_assigns.append(target.slice.value)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "cand" and node.args
                and isinstance(node.args[0], ast.Dict)):
            update_dicts.append(
                [k.value for k in node.args[0].keys if isinstance(k, ast.Constant)])

    assert len(core_dicts) == 1, "expected exactly ONE `cand = {...}` creation dict in the scanner"
    assert len(update_dicts) == 1, "expected exactly ONE `cand.update({...})` shadow extension"
    assert set(CANDIDATE_CREATION_FIELDS) == set(core_dicts[0])
    assert set(CANDIDATE_CREATION_SHADOW_FIELDS) == set(update_dicts[0])

    # The two in-memory extras exist at their claimed origins:
    assert "last_price" in subscript_assigns  # scanner's post-insert stamp
    orch_tree = ast.parse(inspect.getsource(orchestrator_module))
    orch_subscript_keys = [
        node.slice.value
        for assign in ast.walk(orch_tree) if isinstance(assign, ast.Assign)
        for node in assign.targets
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
    ]
    assert "interest_rank" in orch_subscript_keys  # orchestrator's pre-eval rank assignment
    assert set(CANDIDATE_IN_MEMORY_EXTRA_FIELDS) == {"last_price", "interest_rank"}


# --------------------------------------------------------------------- run
def test_run_ab_eval_empty_corpus_is_a_safe_no_op(tmp_path, journal):
    settings = make_settings()
    result = run_ab_eval(journal, settings, ["gpt-5.4-mini", "gpt-5.6-luna"], corpus_dir=str(tmp_path / "empty"))
    assert result["n_packets"] == 0
    assert "error" in result
    assert journal.count_rows("ab_eval_runs", "1=1") == 0


def test_run_ab_eval_needs_at_least_two_models(tmp_path, journal):
    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-20")
    result = run_ab_eval(journal, settings, ["gpt-5.4-mini"], corpus_dir=corpus_dir)
    assert "error" in result
    assert journal.count_rows("ab_eval_runs", "1=1") == 0


def test_run_ab_eval_mock_happy_path_writes_run_and_both_models_results(tmp_path, journal):
    settings = make_settings()  # ALPHAOS_MODE=mock by default
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-20")

    result = run_ab_eval(journal, settings, ["gpt-5.4-mini", "gpt-5.6-luna"], corpus_dir=corpus_dir)

    assert result["n_packets"] == 1
    assert result["n_results"] == 2  # 1 packet x 2 models
    assert result["n_corpus_errors"] == 0
    run_row = journal.one("SELECT * FROM ab_eval_runs WHERE ab_run_id = ?", (result["ab_run_id"],))
    assert run_row is not None
    assert run_row["is_mock"] == 1
    assert run_row["finished_at_utc"] is not None
    assert json.loads(run_row["models_json"]) == ["gpt-5.4-mini", "gpt-5.6-luna"]
    result_rows = journal.query("SELECT * FROM ab_eval_results WHERE ab_run_id = ?", (result["ab_run_id"],))
    assert {r["model"] for r in result_rows} == {"gpt-5.4-mini", "gpt-5.6-luna"}
    assert all(r["eval_id"] == "eval_abevaltest01" for r in result_rows)


def test_run_ab_eval_isolates_one_bad_fixture(tmp_path, journal):
    """Spec's own required test: 'one bad snapshot row skips, never aborts
    the run' -- same per-packet isolation law as CANARY/EVAL-1."""
    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    good = _FIXTURE
    bad = {**_FIXTURE, "eval_id": "eval_abevaltest02", "candidate_id": "cand_abevaltest02",
           "candidate": {**_FIXTURE["candidate"], "candidate_id": "cand_abevaltest02",
                        "momentum_score": "not_a_number"}}  # wrong TYPE -- breaks mock eval's float()
    write_corpus(corpus_dir, [good, bad], as_of_date="2026-07-20")

    result = run_ab_eval(journal, settings, ["gpt-5.4-mini", "gpt-5.6-luna"], corpus_dir=corpus_dir)

    assert result["n_packets"] == 2
    assert result["n_results"] == 2  # only the good packet's 2 model rows
    assert result["n_corpus_errors"] == 1
    result_rows = journal.query("SELECT * FROM ab_eval_results WHERE ab_run_id = ?", (result["ab_run_id"],))
    assert {r["eval_id"] for r in result_rows} == {"eval_abevaltest01"}  # the bad one never partially wrote
    event = journal.one("SELECT * FROM system_events WHERE category = 'ab_eval'")
    assert event is not None
    assert "eval_abevaltest02" in event["message"]


def test_run_ab_eval_refuses_when_cost_cap_reached(tmp_path, journal, monkeypatch):
    """Spec's own required test: 'cost-guard refusal over cap'."""
    settings = make_settings(
        OPENAI_API_KEY="sk-test", ALPHAOS_MODE="paper",
        SCHEDULER_AI_COST_CAP_CALLS_PER_30D="50",
        SHADOW_AI_CAP_CALLS_PER_30D="12",
    )
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-20")
    monkeypatch.setattr(cost_guard, "calls_in_last_30_days", lambda journal: 50)

    result = run_ab_eval(journal, settings, ["gpt-5.4-mini", "gpt-5.6-luna"], corpus_dir=corpus_dir)

    assert "error" in result
    assert "cost cap" in result["error"]
    assert journal.count_rows("ab_eval_runs", "1=1") == 0


def test_run_ab_eval_refuses_when_planned_calls_would_exceed_cap(tmp_path, journal, monkeypatch):
    """Distinct from the pool-already-full case above: the pool has SOME
    room, but this run's own n_packets * n_models would push usage over the
    cap -- same pre-flight magnitude check as CANARY's own run."""
    settings = make_settings(
        OPENAI_API_KEY="sk-test", ALPHAOS_MODE="paper",
        SCHEDULER_AI_COST_CAP_CALLS_PER_30D="50",
        SHADOW_AI_CAP_CALLS_PER_30D="12",  # EXP-1's own joint-validation must clear this cap too
    )
    corpus_dir = str(tmp_path / "corpus")
    fixture_2 = {**_FIXTURE, "eval_id": "eval_abevaltest02", "candidate_id": "cand_abevaltest02",
                "candidate": {**_FIXTURE["candidate"], "candidate_id": "cand_abevaltest02"}}
    fixture_3 = {**_FIXTURE, "eval_id": "eval_abevaltest03", "candidate_id": "cand_abevaltest03",
                "candidate": {**_FIXTURE["candidate"], "candidate_id": "cand_abevaltest03"}}
    write_corpus(corpus_dir, [_FIXTURE, fixture_2, fixture_3], as_of_date="2026-07-20")
    # Pool has room (48/50) but this run's own 3 packets x 2 models = 6
    # planned calls would push usage to 54, over the 50 cap.
    monkeypatch.setattr(cost_guard, "calls_in_last_30_days", lambda journal: 48)

    result = run_ab_eval(journal, settings, ["gpt-5.4-mini", "gpt-5.6-luna"], corpus_dir=corpus_dir)

    assert "error" in result
    assert "6 real AI calls" in result["error"]
    assert journal.count_rows("ab_eval_runs", "1=1") == 0


def test_cost_guard_counts_ab_eval_results_from_non_mock_runs_only(journal):
    journal.insert("ab_eval_runs", {
        "ab_run_id": "abrun_live", "corpus_dir": "data/ab_eval", "is_mock": 0,
        "n_packets": 1, "started_at_utc": "2026-07-20T00:00:00+00:00", "started_at_sgt": "2026-07-20T08:00:00+08:00",
    })
    journal.insert("ab_eval_runs", {
        "ab_run_id": "abrun_mock", "corpus_dir": "data/ab_eval", "is_mock": 1,
        "n_packets": 1, "started_at_utc": "2026-07-20T00:00:00+00:00", "started_at_sgt": "2026-07-20T08:00:00+08:00",
    })
    for run_id, model in (("abrun_live", "gpt-5.4-mini"), ("abrun_live", "gpt-5.6-luna"),
                          ("abrun_mock", "gpt-5.4-mini")):
        journal.insert("ab_eval_results", {
            "ab_result_id": new_id("abres"), "ab_run_id": run_id, "eval_id": "eval_x",
            "symbol": "AAPL", "model": model,
        })

    count = cost_guard.calls_in_last_30_days(journal)
    assert count == 2  # only the 2 rows from the non-mock run


# ---------------------------------------------------------- downgrade_reason
def _seed_atr(journal, symbol, atr_14, market_date="2026-07-08"):
    journal.insert("atr_history", {
        "atr_id": f"atr_{symbol}_{market_date}", "symbol": symbol, "market_date": market_date,
        "atr_14": atr_14, "rules_version": ATR_RULES_V1, "n_bars_fetched": 15,
    })


def test_downgrade_reason_rr_floor_when_raw_propose_fails_the_floor(journal):
    """Spec's own required test: inject a raw propose whose ATR R:R < 1.2
    and assert downgrade_reason='RR_FLOOR' with raw_decision='propose'
    preserved."""
    _seed_atr(journal, "AAPL", atr_14=5.0)  # wide ATR -> wide stop -> low R:R
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test",
                             MIN_REWARD_RISK="1.2")
    client = OpenAIClient(settings, journal)
    raw = OpenAIEvaluation(
        eval_id="ev1", candidate_id="c1", symbol="AAPL", model="gpt-5.4-mini",
        direction=TradeDirection.LONG.value, entry=100.0, stop=97.0, target=104.0,
        max_holding_days=3, expected_r=2.33, confidence=0.8,
        decision=Decision.PROPOSE.value, reasoning_summary="raw propose", is_mock=False,
    )

    final = client.post_process(raw, {"symbol": "AAPL"})

    # k=2.0 * ATR=5.0 = 10.0 stop distance -> new stop 90.0; R:R = |104-100|/10 = 0.4 < 1.2 floor
    assert final.decision == Decision.REJECT.value
    assert raw.decision == Decision.PROPOSE.value  # the RAW object itself is untouched
    from alphaos.ab_eval.replay import _downgrade_reason
    assert _downgrade_reason(raw, final) == RR_FLOOR


def test_downgrade_reason_no_atr_when_symbol_has_no_atr_history(journal):
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test")
    client = OpenAIClient(settings, journal)
    raw = OpenAIEvaluation(
        eval_id="ev1", candidate_id="c1", symbol="ZZZZ", model="gpt-5.4-mini",
        direction=TradeDirection.LONG.value, entry=100.0, stop=97.0, target=110.0,
        max_holding_days=3, expected_r=3.33, confidence=0.8,
        decision=Decision.PROPOSE.value, reasoning_summary="raw propose", is_mock=False,
    )

    final = client.post_process(raw, {"symbol": "ZZZZ"})

    from alphaos.ab_eval.replay import _downgrade_reason
    assert final.decision == Decision.REJECT.value
    assert _downgrade_reason(raw, final) == NO_ATR


def test_downgrade_reason_none_when_raw_propose_clears_the_pipeline(journal):
    _seed_atr(journal, "AAPL", atr_14=1.0)
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test",
                             MIN_REWARD_RISK="1.2")
    client = OpenAIClient(settings, journal)
    raw = OpenAIEvaluation(
        eval_id="ev1", candidate_id="c1", symbol="AAPL", model="gpt-5.4-mini",
        direction=TradeDirection.LONG.value, entry=100.0, stop=97.0, target=110.0,
        max_holding_days=3, expected_r=3.33, confidence=0.8,
        decision=Decision.PROPOSE.value, reasoning_summary="raw propose", is_mock=False,
    )

    final = client.post_process(raw, {"symbol": "AAPL"})

    from alphaos.ab_eval.replay import _downgrade_reason
    assert final.decision == Decision.PROPOSE.value
    assert _downgrade_reason(raw, final) is None


def test_downgrade_reason_unknown_when_downgrade_has_no_known_reason_code(journal):
    """Audit NIT (2026-07-20): a raw propose that became a final non-propose
    WITHOUT either known reason code stamped is a third downgrade path this
    module doesn't know about -- it must surface as the UNKNOWN sentinel in
    the autopsy, never hide inside NULL ('no downgrade happened')."""
    from alphaos.ab_eval.replay import _downgrade_reason

    raw = OpenAIEvaluation(
        eval_id="ev1", candidate_id="c1", symbol="AAPL", model="gpt-5.4-mini",
        direction=TradeDirection.LONG.value, entry=100.0, stop=97.0, target=110.0,
        max_holding_days=3, expected_r=3.33, confidence=0.8,
        decision=Decision.PROPOSE.value, reasoning_summary="raw propose",
    )
    final = OpenAIEvaluation(
        eval_id="ev2", candidate_id="c1", symbol="AAPL", model="gpt-5.4-mini",
        direction=TradeDirection.LONG.value, entry=None, stop=None, target=None,
        max_holding_days=None, expected_r=None, confidence=0.0,
        decision=Decision.REJECT.value, reasoning_summary="mystery downgrade",
        risk_flags=["SOME_FUTURE_REASON_CODE"],
    )
    assert _downgrade_reason(raw, final) == UNKNOWN


def test_downgrade_reason_none_when_raw_decision_was_not_propose(journal):
    raw = OpenAIEvaluation(
        eval_id="ev1", candidate_id="c1", symbol="AAPL", model="gpt-5.4-mini",
        direction=TradeDirection.LONG.value, entry=None, stop=None, target=None,
        max_holding_days=None, expected_r=None, confidence=0.0,
        decision=Decision.REJECT.value, reasoning_summary="raw reject", risk_flags=["SOME_OTHER_REASON"],
    )
    from alphaos.ab_eval.replay import _downgrade_reason
    assert _downgrade_reason(raw, raw) is None


def test_stored_raw_fields_survive_pipeline_mutation_end_to_end(tmp_path, journal, monkeypatch):
    """Regression (caught empirically on the first RR_FLOOR demo run):
    _apply_atr_stop mutates its evaluation argument IN PLACE
    (stop/expected_r/stop_source), so replaying without isolating the raw
    object stored the ATR-overridden stop (90.0) as 'raw_stop' where the
    model returned 97.0 -- exactly the raw-verdict clobbering this harness
    exists to prevent. The spec's two-layer law: raw_* columns hold the
    model's own values AS RETURNED; pipeline_* columns hold the post-chain
    verdict; both must be visible on the same row when the floor trips."""
    _seed_atr(journal, "AAPL", atr_14=5.0)  # wide ATR -> stop 90.0 -> R:R 0.4 < 1.2 floor
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test",
                             MIN_REWARD_RISK="1.2",
                             SHADOW_AI_CAP_CALLS_PER_30D="12")
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-20")

    def _fake_raw_evaluate(self, candidate, snapshot, freshness_status="usable"):
        return OpenAIEvaluation(
            eval_id="ev1", candidate_id="c1", symbol="AAPL", model=self.model,
            direction=TradeDirection.LONG.value, entry=100.0, stop=97.0, target=104.0,
            max_holding_days=3, expected_r=1.33, confidence=0.72,
            decision=Decision.PROPOSE.value, reasoning_summary="raw propose", is_mock=False,
        )

    monkeypatch.setattr(OpenAIClient, "raw_evaluate", _fake_raw_evaluate)
    monkeypatch.setattr(cost_guard, "calls_in_last_30_days", lambda journal: 0)

    result = run_ab_eval(journal, settings, ["gpt-5.4-mini", "gpt-5.6-luna"], corpus_dir=corpus_dir)

    rows = journal.query("SELECT * FROM ab_eval_results WHERE ab_run_id = ?", (result["ab_run_id"],))
    assert len(rows) == 2
    for row in rows:
        assert row["raw_decision"] == "propose"      # spec: raw_decision='propose' preserved
        assert row["raw_stop"] == 97.0               # the model's OWN stop, not the ATR-overridden 90.0
        assert row["raw_expected_r"] == 1.33         # the model's OWN R:R, not the post-ATR 0.4
        assert row["pipeline_decision"] == "reject"  # spec: pipeline_decision='reject'
        assert row["downgrade_reason"] == "RR_FLOOR"  # spec: downgrade_reason='RR_FLOOR'


# ------------------------------------------------------------- replay_packet
def test_replay_packet_parameterizes_only_model_and_prompt_version(journal, monkeypatch):
    """The replay client's settings clone differs from the original ONLY in
    openai_primary_model/openai_prompt_version (INSTR-2's second arm axis)
    -- proof that no other config axis is silently forked for the replay
    path."""
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test",
                             OPENAI_PRIMARY_MODEL="gpt-5.4-mini", OPENAI_PROMPT_VERSION="v1")
    captured = {}

    def _fake_raw_evaluate(self, candidate, snapshot, freshness_status="usable"):
        captured["model"] = self.model
        captured["prompt_version"] = self.settings.openai_prompt_version
        return OpenAIEvaluation(
            eval_id="ev1", candidate_id="c1", symbol="AAPL", model=self.model,
            direction="long", entry=100.0, stop=97.0, target=104.0, max_holding_days=3,
            expected_r=1.33, confidence=0.5, decision=Decision.WATCH.value, reasoning_summary="x",
        )

    monkeypatch.setattr(OpenAIClient, "raw_evaluate", _fake_raw_evaluate)

    result = replay_packet(_FIXTURE, ("gpt-5.6-luna", "v2"), settings, journal)

    assert isinstance(result, ReplayResult)
    assert result.model == "gpt-5.6-luna"
    assert result.prompt_version == "v2"
    assert captured["model"] == "gpt-5.6-luna"  # parameterized
    assert captured["prompt_version"] == "v2"  # parameterized
    assert settings.openai_primary_model == "gpt-5.4-mini"  # original settings untouched
    assert settings.openai_prompt_version == "v1"  # original settings untouched


def test_replay_packet_read_only_journal_never_writes_system_events(journal):
    """Shadow/read-only law: a replay call must never write an
    indistinguishable-from-live INFO row into system_events, even when the
    pipeline downgrades (which would normally log one via
    _apply_atr_stop/_enforce_min_reward_risk)."""
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test")
    before = journal.count_rows("system_events", "1=1")

    # No ATR history seeded -> _apply_atr_stop would normally log an INFO
    # event on the real journal if it weren't wrapped read-only.
    fixture = {**_FIXTURE, "candidate": {**_FIXTURE["candidate"], "momentum_score": 0.9}}
    replay_packet(fixture, ("gpt-5.4-mini", "v1"), settings, journal)

    after = journal.count_rows("system_events", "1=1")
    assert after == before


def test_replay_packet_v1_and_v2_arms_replaying_same_fixture_are_uncontaminated(journal):
    """Test 13's own order-independence check: a v1 arm and a v2 arm
    replaying the SAME fixture object (the normal multi-arm run shape --
    run.py shares one ``fixture`` dict across every arm in its per-packet
    loop) must never contaminate each other's view of it, regardless of
    which arm runs first. _augment_snapshot_for_prompt's copy-on-write
    (v2) / unchanged-passthrough (v1) design is what makes this true."""
    _seed_atr(journal, "AAPL", atr_14=2.0)
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test")
    shared_fixture = {**_FIXTURE, "candidate": {**_FIXTURE["candidate"], "momentum_score": 0.9}}
    original_snapshot = shared_fixture["snapshot"]

    v1_first = replay_packet(shared_fixture, ("gpt-5.4-mini", "v1"), settings, journal)
    v2_second = replay_packet(shared_fixture, ("gpt-5.4-mini", "v2"), settings, journal)
    assert shared_fixture["snapshot"] is original_snapshot  # never mutated in place
    assert "atr_policy" not in shared_fixture["snapshot"]  # v2's copy never leaked back

    # Order reversed -- same fixture object, opposite arm order.
    v2_first = replay_packet(shared_fixture, ("gpt-5.4-mini", "v2"), settings, journal)
    v1_second = replay_packet(shared_fixture, ("gpt-5.4-mini", "v1"), settings, journal)
    assert shared_fixture["snapshot"] is original_snapshot
    assert "atr_policy" not in shared_fixture["snapshot"]

    # Both v1 replays saw the unaugmented snapshot; both v2 replays saw the
    # ATR block -- independent of call order.
    assert v1_first.raw.snapshot is None and v1_second.raw.snapshot is None  # replay never stamps it
    for v2_result in (v2_second, v2_first):
        assert v2_result.prompt_version == "v2"


def test_replay_packet_v1_arm_never_leaks_a_stale_atr_policy_into_the_live_prompt(journal, monkeypatch):
    """Audit fixup (MEDIUM, 2026-07-23): the original naive
    ``atr_policy=snapshot.get("atr_policy")`` gate in ``_live_eval`` would
    leak a stale v2-era ``atr_policy`` block into a supposedly
    byte-identical v1 prompt whenever a fixture's OWN stored snapshot
    already carried one -- exactly the shape a future AB-EVAL-1 corpus
    built from post-cutover ``snapshot_json`` will have (``_augment_
    snapshot_for_prompt``'s own v1/mock no-op deliberately returns the
    snapshot UNCHANGED, never stripping a pre-existing key). Drives the
    REAL production chain -- ``replay_packet`` -> ``raw_evaluate`` ->
    ``_live_eval`` -> ``build_no_news_user_prompt`` -- with ONLY the OpenAI
    SDK class itself faked (never ``_live_eval``), so the actual
    prompt-building code under test really executes; mirrors the auditor's
    own probe."""
    import json as _json

    import openai

    captured: dict = {}

    class _FakeMessage:
        def __init__(self, content):
            self.content = content

    class _FakeChoice:
        def __init__(self, content):
            self.message = _FakeMessage(content)

    class _FakeResponse:
        def __init__(self, content):
            self.choices = [_FakeChoice(content)]
            self.usage = None

    class _FakeCompletions:
        def create(self, model, response_format, messages, timeout):
            captured["messages"] = messages
            payload = {
                "symbol": "AAPL", "direction": "long", "entry": 100.0, "stop": 97.0,
                "target": 104.0, "max_holding_days": 3, "expected_r": 1.0, "confidence": 0.5,
                "decision": "reject", "reasoning_summary": "x",
                "catalyst": "not_available_v1", "news_status": "disabled_v1",
                "news_sources": [], "data_freshness_status": "usable", "risk_flags": [],
            }
            return _FakeResponse(_json.dumps(payload))

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeOpenAI:
        def __init__(self, api_key=None):
            self.chat = _FakeChat()

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)

    stale_snapshot = {
        "last_price": 100.0,
        "atr_policy": {
            "atr_14": 999.0, "stop_multiplier": 42.0, "risk_per_share": 999.0,
            "min_reward_risk": 1.2, "min_target_distance": 999.0,
            "rules_version": "should_never_leak",
        },
    }
    fixture = {**_FIXTURE, "snapshot": stale_snapshot}
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test")

    replay_packet(fixture, ("gpt-5.4-mini", "v1"), settings, journal)

    assert "messages" in captured  # sanity: the real _live_eval genuinely ran
    user_prompt = captured["messages"][1]["content"]
    assert "ATR_STOP_POLICY" not in user_prompt
    assert "999.0" not in user_prompt
    assert "should_never_leak" not in user_prompt


# --------------------------------------------------------------- AST/structural
def test_replay_routes_through_production_raw_evaluate_and_post_process_ast():
    """AST test pinning the replay engine to the production evaluate core --
    same pattern as PR13's promote_card() call-node check (a docstring
    mention would show up as a Constant/Expr node, never a Call, so this
    can only pass if replay_packet() genuinely invokes the production
    methods). INSTR-2: pins the three-step mirror of evaluate() --
    _augment_snapshot_for_prompt -> raw_evaluate -> post_process, in that
    order. Structural proof for the spec's own 'no forked second prompt'
    law: the module also defines NO function whose name contains 'prompt'
    (beyond the mirrored _augment_snapshot_for_prompt CALL, which is a
    production OpenAIClient method, not a def in this module) and imports
    no prompt-building module directly -- the only route to a prompt is
    through OpenAIClient's own raw_evaluate -> _live_eval ->
    pt.build_no_news_user_prompt chain."""
    import ast
    import inspect
    import textwrap

    from alphaos.ab_eval import replay as replay_module

    replay_fn_source = inspect.getsource(replay_module.replay_packet)
    replay_tree = ast.parse(textwrap.dedent(replay_fn_source))
    attr_calls = [
        node.func.attr for node in ast.walk(replay_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "raw_evaluate" in attr_calls
    assert "post_process" in attr_calls
    assert "_augment_snapshot_for_prompt" in attr_calls
    assert (
        attr_calls.index("_augment_snapshot_for_prompt")
        < attr_calls.index("raw_evaluate")
        < attr_calls.index("post_process")
    )

    module_source = inspect.getsource(replay_module)
    module_tree = ast.parse(textwrap.dedent(module_source))
    func_defs = [
        node.name for node in ast.walk(module_tree) if isinstance(node, ast.FunctionDef)
    ]
    assert not any("prompt" in name.lower() for name in func_defs), (
        f"ab_eval/replay.py defines a prompt-shaped function {func_defs!r} -- "
        "this would be a forked second prompt path, forbidden by spec."
    )

    imported_names = []
    for node in ast.walk(module_tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.extend(a.name for a in node.names)
            if node.module:
                imported_names.append(node.module)
        if isinstance(node, ast.Import):
            imported_names.extend(a.name for a in node.names)
    assert not any("prompt_templates" in n for n in imported_names), (
        "ab_eval/replay.py imports prompt_templates directly -- the ONLY route to a "
        "prompt must be through OpenAIClient's own production call path."
    )


def test_live_eval_still_calls_the_one_production_prompt_builder_ast():
    """Companion structural check: the refactor of evaluate() must not have
    forked _live_eval's own prompt-build call -- it still routes through
    pt.build_no_news_user_prompt, the ONE production prompt path both the
    live evaluator and (transitively, via raw_evaluate) the replay harness
    share."""
    import ast
    import inspect
    import textwrap

    from alphaos.ai import openai_client as oc_module

    source = inspect.getsource(oc_module.OpenAIClient._live_eval)
    tree = ast.parse(textwrap.dedent(source))
    attr_calls = [
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "build_no_news_user_prompt" in attr_calls


def test_evaluate_calls_raw_evaluate_then_post_process_in_order_ast():
    """evaluate() itself must be a thin wrapper calling
    _augment_snapshot_for_prompt(), then raw_evaluate(), then
    post_process(), in that order (INSTR-2's three-step mirror) -- the
    single source of truth the live path and (indirectly, via the same
    three steps) the replay path share."""
    import ast
    import inspect
    import textwrap

    from alphaos.ai import openai_client as oc_module

    source = inspect.getsource(oc_module.OpenAIClient.evaluate)
    tree = ast.parse(textwrap.dedent(source))
    attr_calls = [
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert (
        attr_calls.index("_augment_snapshot_for_prompt")
        < attr_calls.index("raw_evaluate")
        < attr_calls.index("post_process")
    )


def test_run_ab_eval_dedupes_repeated_models(tmp_path, journal):
    """Audit NIT (2026-07-20): a repeated --models entry would double every
    packet's call count (and the cost preflight's planned_calls) for zero
    comparative information -- deduped order-preserving; two copies of ONE
    model is still a one-model run and refuses."""
    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-20")

    same_twice = run_ab_eval(journal, settings, ["gpt-5.4-mini", "gpt-5.4-mini"], corpus_dir=corpus_dir)
    assert "error" in same_twice
    assert journal.count_rows("ab_eval_runs", "1=1") == 0

    result = run_ab_eval(journal, settings, ["gpt-5.4-mini", "gpt-5.4-mini", "gpt-5.6-luna"],
                         corpus_dir=corpus_dir)
    assert result["models"] == ["gpt-5.4-mini", "gpt-5.6-luna"]
    assert result["n_results"] == 2  # 1 packet x 2 DISTINCT models, never 3 calls


# ------------------------------------------------------ INSTR-2: harness arms
def test_cli_ab_eval_run_arms_flag_is_mutually_exclusive_with_models():
    """--arms parsing: MODEL:VERSION tokens, mutually exclusive with the
    existing --models."""
    from alphaos.__main__ import build_parser

    args = build_parser().parse_args([
        "ab_eval_run", "--arms", "gpt-5.4-mini:v1", "gpt-5.6-luna:v1",
        "gpt-5.4-mini:v2", "gpt-5.6-luna:v2",
    ])
    assert args.command == "ab_eval_run"
    assert args.arms == ["gpt-5.4-mini:v1", "gpt-5.6-luna:v1", "gpt-5.4-mini:v2", "gpt-5.6-luna:v2"]
    assert args.models is None

    args2 = build_parser().parse_args(["ab_eval_run", "--models", "gpt-5.4-mini", "gpt-5.6-luna"])
    assert args2.models == ["gpt-5.4-mini", "gpt-5.6-luna"]
    assert args2.arms is None

    with pytest.raises(SystemExit):
        build_parser().parse_args([
            "ab_eval_run", "--models", "a", "--arms", "b:v1", "c:v1",
        ])

    with pytest.raises(SystemExit):
        build_parser().parse_args(["ab_eval_run"])  # one of --models/--arms required


def test_cmd_ab_eval_run_parses_and_validates_arm_tokens(tmp_path, journal):
    """cmd_ab_eval_run's own MODEL:VERSION token parser -- malformed tokens
    (missing ':', or a version outside {v1, v2}) refuse loudly rather than
    silently constructing a bogus arm."""
    from alphaos.__main__ import cmd_ab_eval_run
    from alphaos.orchestrator import Orchestrator

    orch = Orchestrator(settings=make_settings(), journal=journal)
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-20")

    rc = cmd_ab_eval_run(orch, None, ["gpt-5.4-mini-no-colon"], corpus_dir)
    assert rc == 1
    assert journal.count_rows("ab_eval_runs", "1=1") == 0

    rc = cmd_ab_eval_run(orch, None, ["gpt-5.4-mini:v9"], corpus_dir)
    assert rc == 1
    assert journal.count_rows("ab_eval_runs", "1=1") == 0

    # Audit NIT fixup (2026-07-23): ":v1" partitions to an EMPTY model name
    # with a valid version -- must still refuse, not silently construct a
    # bogus arm with model="".
    rc = cmd_ab_eval_run(orch, None, [":v1", "gpt-5.6-luna:v2"], corpus_dir)
    assert rc == 1
    assert journal.count_rows("ab_eval_runs", "1=1") == 0

    rc = cmd_ab_eval_run(orch, None, ["gpt-5.4-mini:v1", "gpt-5.6-luna:v2"], corpus_dir)
    assert rc == 0
    run_row = journal.one("SELECT * FROM ab_eval_runs ORDER BY id DESC LIMIT 1")
    assert json.loads(run_row["arms_json"]) == ["gpt-5.4-mini:v1", "gpt-5.6-luna:v2"]


def test_run_ab_eval_arms_dedupe_refuse_below_two_and_populate_prompt_version(tmp_path, journal):
    """(model, version) dedupe (a repeated arm token is one arm, not two);
    refusal below 2 distinct arms; ab_eval_results.prompt_version populated
    per-row from the arm that produced it."""
    settings = make_settings()  # mock mode
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-20")

    one_arm = run_ab_eval(journal, settings, corpus_dir=corpus_dir,
                          arms=[("gpt-5.4-mini", "v1"), ("gpt-5.4-mini", "v1")])
    assert "error" in one_arm
    assert journal.count_rows("ab_eval_runs", "1=1") == 0

    result = run_ab_eval(journal, settings, corpus_dir=corpus_dir, arms=[
        ("gpt-5.4-mini", "v1"), ("gpt-5.4-mini", "v1"), ("gpt-5.4-mini", "v2"),
    ])
    assert result["arms"] == ["gpt-5.4-mini:v1", "gpt-5.4-mini:v2"]  # deduped, order preserved
    assert result["models"] == ["gpt-5.4-mini"]  # one distinct model, two prompt versions
    assert result["n_results"] == 2  # 1 packet x 2 DISTINCT arms

    rows = journal.query(
        "SELECT * FROM ab_eval_results WHERE ab_run_id = ?", (result["ab_run_id"],),
    )
    versions = {r["prompt_version"] for r in rows}
    assert versions == {"v1", "v2"}
    run_row = journal.one("SELECT * FROM ab_eval_runs WHERE ab_run_id = ?", (result["ab_run_id"],))
    assert json.loads(run_row["arms_json"]) == ["gpt-5.4-mini:v1", "gpt-5.4-mini:v2"]
    assert json.loads(run_row["models_json"]) == ["gpt-5.4-mini"]  # old-reader field intact


def test_run_ab_eval_arms_planned_calls_is_packets_times_arms(tmp_path, journal, monkeypatch):
    """planned_calls = n_packets x n_arms in the cost pre-flight -- 4 arms
    over 3 packets must refuse at exactly 12 planned calls, not 12/n_models
    or any other miscount."""
    settings = make_settings(
        OPENAI_API_KEY="sk-test", ALPHAOS_MODE="paper",
        SCHEDULER_AI_COST_CAP_CALLS_PER_30D="50", SHADOW_AI_CAP_CALLS_PER_30D="12",
    )
    corpus_dir = str(tmp_path / "corpus")
    fixture_2 = {**_FIXTURE, "eval_id": "eval_abevaltest02", "candidate_id": "cand_abevaltest02",
                "candidate": {**_FIXTURE["candidate"], "candidate_id": "cand_abevaltest02"}}
    fixture_3 = {**_FIXTURE, "eval_id": "eval_abevaltest03", "candidate_id": "cand_abevaltest03",
                "candidate": {**_FIXTURE["candidate"], "candidate_id": "cand_abevaltest03"}}
    write_corpus(corpus_dir, [_FIXTURE, fixture_2, fixture_3], as_of_date="2026-07-20")
    monkeypatch.setattr(cost_guard, "calls_in_last_30_days", lambda journal: 40)  # room for 10 more

    result = run_ab_eval(journal, settings, corpus_dir=corpus_dir, arms=[
        ("gpt-5.4-mini", "v1"), ("gpt-5.6-luna", "v1"), ("gpt-5.4-mini", "v2"), ("gpt-5.6-luna", "v2"),
    ])

    assert "error" in result
    assert "12 real AI calls" in result["error"]  # 3 packets x 4 arms, never 3x2
    assert journal.count_rows("ab_eval_runs", "1=1") == 0


def test_run_ab_eval_models_sugar_still_works_unchanged(tmp_path, journal):
    """--models remains sugar for MODEL:<configured openai_prompt_version>
    -- every pre-INSTR-2 caller of run_ab_eval(models=[...]) keeps working
    completely unchanged (result shape, journaled rows)."""
    settings = make_settings()  # mock, openai_prompt_version defaults "v1"
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-20")

    result = run_ab_eval(journal, settings, ["gpt-5.4-mini", "gpt-5.6-luna"], corpus_dir=corpus_dir)

    assert result["arms"] == ["gpt-5.4-mini:v1", "gpt-5.6-luna:v1"]
    rows = journal.query(
        "SELECT * FROM ab_eval_results WHERE ab_run_id = ?", (result["ab_run_id"],),
    )
    assert {r["prompt_version"] for r in rows} == {"v1"}


# -------------------------------------------------------- refactor-preserving
def test_evaluate_contains_atr_read_exception_as_safe_reject(journal):
    """Audit HIGH regression (2026-07-20): pre-refactor, _apply_atr_stop ran
    INSIDE evaluate()'s live try/except -- a true exception (e.g. a transient
    SQLite error on the atr_history read) was contained to a journaled ERROR
    + safe OPENAI_REJECT rejection, and the scan loop continued. The
    post_process() factoring briefly moved it outside that containment, so
    one flaky read would have aborted the ENTIRE nightly scan mid-loop.
    evaluate() must RETURN a reject, never raise."""
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test")
    client = OpenAIClient(settings, journal)

    def _fake_live_eval(self, candidate, snapshot, freshness_status):
        return OpenAIEvaluation(
            eval_id="ev1", candidate_id="c1", symbol="AAPL", model=self.model,
            direction=TradeDirection.LONG.value, entry=100.0, stop=97.0, target=110.0,
            max_holding_days=3, expected_r=3.33, confidence=0.8,
            decision=Decision.PROPOSE.value, reasoning_summary="x", is_mock=False,
        )

    def _raising_scalar(sql, params=()):
        raise Exception("simulated transient SQLite error on atr_history read")

    import types
    client._live_eval = types.MethodType(_fake_live_eval, client)
    journal.scalar = _raising_scalar  # the read _apply_atr_stop performs

    result = client.evaluate({"symbol": "AAPL"}, {"last_price": 100.0}, freshness_status="usable")

    assert result.decision == Decision.REJECT.value          # returned, not raised
    assert ReasonCode.OPENAI_REJECT.value in result.risk_flags
    assert result.snapshot == {"last_price": 100.0}          # snapshot still stamped LAST
    event = journal.one(
        "SELECT * FROM system_events WHERE category = 'openai' AND severity = 'error'")
    assert event is not None
    assert "rejecting" in event["message"]


def test_evaluate_mock_path_unchanged_after_refactor(journal):
    """The evaluate() refactor (raw_evaluate + post_process) must be
    byte-identical in behavior to the pre-refactor inline sequence for the
    mock path -- same decision/entry/stop/target as always."""
    settings = make_settings()  # mock mode
    client = OpenAIClient(settings, journal)
    candidate = {"candidate_id": "c1", "symbol": "AAPL", "direction": "long", "momentum_score": 0.9}
    snapshot = {"last_price": 100.0}

    result = client.evaluate(candidate, snapshot, freshness_status="usable")

    assert result.decision == Decision.PROPOSE.value
    assert result.is_mock is True
    assert result.snapshot == snapshot


def test_evaluate_live_path_unchanged_after_refactor_atr_applies(journal):
    _seed_atr(journal, "AAPL", atr_14=1.0)
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test",
                             MIN_REWARD_RISK="1.2")
    client = OpenAIClient(settings, journal)

    def _fake_live_eval(self, candidate, snapshot, freshness_status):
        return OpenAIEvaluation(
            eval_id="ev1", candidate_id="c1", symbol="AAPL", model=self.model,
            direction="long", entry=100.0, stop=97.0, target=110.0, max_holding_days=3,
            expected_r=3.33, confidence=0.8, decision=Decision.PROPOSE.value,
            reasoning_summary="x", is_mock=False,
        )

    import types
    client._live_eval = types.MethodType(_fake_live_eval, client)

    result = client.evaluate({"symbol": "AAPL"}, {"last_price": 100.0}, freshness_status="usable")

    # k=2.0 * ATR=1.0 = 2.0 stop distance -> new stop 98.0; R:R = 10/2 = 5.0 -- clears the floor
    assert result.stop == 98.0
    assert result.decision == Decision.PROPOSE.value
    assert result.snapshot == {"last_price": 100.0}


# ------------------------------------------------------------------- report
def test_ab_eval_report_no_runs_yet(journal):
    rep = build_ab_eval_report(journal)
    assert rep["status"] == "no_runs_yet"
    assert "ab_eval_corpus_build" in render_markdown(rep)


def test_ab_eval_report_reflects_latest_run_with_flip_and_autopsy(tmp_path, journal):
    """INSTR-2: the report groups by ARM ("model:prompt_version"), not bare
    model (Design item 4) -- this run used the --models sugar path, so
    every arm is its model at the configured (default "v1") prompt
    version. models stays populated too (distinct model names only) for
    any pre-INSTR-2 reader of that specific field."""
    settings = make_settings()
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [_FIXTURE], as_of_date="2026-07-20")
    result = run_ab_eval(journal, settings, ["gpt-5.4-mini", "gpt-5.6-luna"], corpus_dir=corpus_dir)

    rep = build_ab_eval_report(journal, ab_run_id=result["ab_run_id"])

    assert rep["status"] == "ok"
    assert rep["models"] == ["gpt-5.4-mini", "gpt-5.6-luna"]
    assert rep["arms"] == ["gpt-5.4-mini:v1", "gpt-5.6-luna:v1"]
    assert set(rep["raw_decision_distribution"].keys()) == {"gpt-5.4-mini:v1", "gpt-5.6-luna:v1"}
    md = render_markdown(rep)
    assert "Raw decision distribution" in md
    assert "RR_FLOOR / NO_ATR autopsy" in md
    assert "Flipped packets" in md
    assert "descriptive only" in md


def test_ab_eval_report_flips_detected_across_arms_same_model_two_versions(journal):
    """INSTR-2: the arm-grouped flip detection distinguishes v1 from v2 for
    the SAME model -- exactly the proof-gate's own 4-arm shape, where
    conflating gpt-5.4-mini:v1 and gpt-5.4-mini:v2 under one 'gpt-5.4-mini'
    key would silently hide the very effect the gate measures."""
    journal.insert("ab_eval_runs", {
        "ab_run_id": "abrun_flip2", "corpus_dir": "data/ab_eval", "is_mock": 1,
        "n_packets": 1, "n_results": 2, "models_json": json.dumps(["mini"]),
        "arms_json": json.dumps(["mini:v1", "mini:v2"]),
        "started_at_utc": "2026-07-20T00:00:00+00:00", "started_at_sgt": "2026-07-20T08:00:00+08:00",
    })
    journal.insert("ab_eval_results", {
        "ab_result_id": new_id("abres"), "ab_run_id": "abrun_flip2", "eval_id": "eval_x",
        "symbol": "AAPL", "model": "mini", "prompt_version": "v1", "raw_decision": "reject",
        "reasoning_summary": "mini v1 says reject",
    })
    journal.insert("ab_eval_results", {
        "ab_result_id": new_id("abres"), "ab_run_id": "abrun_flip2", "eval_id": "eval_x",
        "symbol": "AAPL", "model": "mini", "prompt_version": "v2", "raw_decision": "propose",
        "reasoning_summary": "mini v2 says propose",
    })

    rep = build_ab_eval_report(journal, ab_run_id="abrun_flip2")

    assert len(rep["flipped_packets"]) == 1
    assert rep["flipped_packets"][0]["decisions"] == {"mini:v1": "reject", "mini:v2": "propose"}
    md = render_markdown(rep)
    assert "mini v2 says propose" in md


def test_ab_eval_report_flips_detected_across_models(journal):
    """A pre-INSTR-2 run (no arms_json, no per-row prompt_version) still
    renders correctly -- NULL prompt_version degrades to "v1" so the arm
    key is just "model:v1", matching this codebase's own historical rows
    (every one of which really did run under the only version that ever
    existed)."""
    journal.insert("ab_eval_runs", {
        "ab_run_id": "abrun_flip", "corpus_dir": "data/ab_eval", "is_mock": 1,
        "n_packets": 1, "n_results": 2, "models_json": json.dumps(["mini", "luna"]),
        "started_at_utc": "2026-07-20T00:00:00+00:00", "started_at_sgt": "2026-07-20T08:00:00+08:00",
    })
    journal.insert("ab_eval_results", {
        "ab_result_id": new_id("abres"), "ab_run_id": "abrun_flip", "eval_id": "eval_x",
        "symbol": "AAPL", "model": "mini", "raw_decision": "propose",
        "reasoning_summary": "mini says propose",
    })
    journal.insert("ab_eval_results", {
        "ab_result_id": new_id("abres"), "ab_run_id": "abrun_flip", "eval_id": "eval_x",
        "symbol": "AAPL", "model": "luna", "raw_decision": "reject",
        "reasoning_summary": "luna says reject",
    })

    rep = build_ab_eval_report(journal, ab_run_id="abrun_flip")

    assert len(rep["flipped_packets"]) == 1
    assert rep["flipped_packets"][0]["decisions"] == {"mini:v1": "propose", "luna:v1": "reject"}
    md = render_markdown(rep)
    assert "mini says propose" in md
    assert "luna says reject" in md
