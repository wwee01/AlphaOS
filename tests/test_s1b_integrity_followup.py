"""SETUP-1 preregistration-integrity follow-up: dedicated test suite
covering every item in the follow-up's own Section 7 requirements -- exact
hash freezing, registration-time refusal on card/selector identity
problems, the ``analysis_not_before`` hard gate (its zero-write proof and
its controlled-future-clock success proof), non-mutation of the original
v1 pair, distinct identifiers for the corrected v2 pair, atomicity/
idempotency for the corrected pair specifically, and S1c's own preflight
diagnostic.

Some Section 7 items are already fully proven by other, more specific test
files and are not re-proven here, to avoid a second, weaker copy of an
existing test:
  - The production selector-hash mechanism itself (golden-fixture matrix,
    exact pinned value, drift detection): ``tests/test_card_selector.py``.
  - Atomic paired evaluation's core mechanics (partial-write prevention,
    one-shot re-entrancy) and the zero-production-PER-assignment / S1b
    import-isolation guarantees: ``tests/test_s1b_registration_integration.py``
    (that file's fixtures were updated by this same follow-up to freeze a
    valid identity, so its existing proofs now exercise the post-follow-up
    code path). This file adds ONE additional atomicity proof scoped to a
    v2-shaped pair (identity + analysis_not_before both present), since
    that exact combination did not exist before this follow-up, plus one
    additional isolation check scoped to this follow-up's own new symbols.

NOT WIRED INTO PRODUCTION: nothing in this file calls anything that
stamps a candidate, runs S1c, or writes to the real production journal --
every test here uses an isolated in-memory or fixture-orchestrator
journal.
"""

from __future__ import annotations

import json
import pathlib
import random
from datetime import date, timedelta

import pytest

import alphaos.orchestrator as orchestrator_mod
import alphaos.scanner.candidate_scanner as candidate_scanner_mod
from alphaos.cards import per_evidence
from alphaos.cards import registry as cards_registry_mod
from alphaos.cards.selector import GOLDEN_FIXTURE_SEMANTIC_HASH, PER_CARD_ID, SELECTOR_VERSION
from alphaos.journal.journal_store import JournalStore
from alphaos.stats.preregistration import (
    PreregistrationAlreadyEvaluatedError,
    evaluate_two_arm_hypothesis_pair,
    register_hypothesis,
)
from alphaos.util.ids import new_id


@pytest.fixture
def journal():
    store = JournalStore(":memory:")
    yield store
    store.close()


# --------------------------------------------------------------- fixture helpers
def _insert_cache_row(journal, symbol, report_date, fiscal_date_ending, timing="pre-market"):
    return journal.insert("earnings_calendar_cache", {
        "entry_id": new_id("ecc"), "symbol": symbol, "report_date": report_date,
        "fiscal_date_ending": fiscal_date_ending, "timing": timing, "source": "test",
    })


def _insert_per_candidate(journal, symbol, decision_date, cache_row_id, outcome_value):
    candidate_id = new_id("cand")
    journal.insert("candidates", {
        "candidate_id": candidate_id, "symbol": symbol, "shadow_tier": 0,
        "card_id": PER_CARD_ID, "card_version": 1, "card_assignment_status": "ok",
        "card_assignment_ref": str(cache_row_id), "card_selector_version": SELECTOR_VERSION,
    })
    journal.insert("candidate_outcomes", {
        "outcome_id": new_id("out"), "candidate_id": candidate_id, "symbol": symbol,
        "candidate_type": "candidate", "decision_at_utc": f"{decision_date}T14:30:00+00:00",
        "market_adjusted_return_5d_pct": outcome_value, "outcome_status": "complete",
    })
    return candidate_id


def _insert_control_candidate(journal, symbol, decision_date, outcome_value):
    candidate_id = new_id("cand")
    journal.insert("candidates", {
        "candidate_id": candidate_id, "symbol": symbol, "shadow_tier": 0,
        "card_id": "catalyst_momentum_v2", "card_version": 1, "card_assignment_status": "ok",
    })
    journal.insert("candidate_outcomes", {
        "outcome_id": new_id("out"), "candidate_id": candidate_id, "symbol": symbol,
        "candidate_type": "candidate", "decision_at_utc": f"{decision_date}T14:30:00+00:00",
        "market_adjusted_return_5d_pct": outcome_value, "outcome_status": "complete",
    })
    return candidate_id


def _trading_dates_from(start: date, n: int) -> list[str]:
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=7)
    return out


def _seed_population(journal, n_events=30, effect=2.0, controls_per_date=10, symbol_offset=0, seed=42):
    rng = random.Random(seed)
    dates = _trading_dates_from(date(2026, 1, 5) + timedelta(days=7 * symbol_offset), n_events)
    for i, d in enumerate(dates):
        symbol = f"SYM{symbol_offset + i}"
        cache_id = _insert_cache_row(journal, symbol, d, fiscal_date_ending=f"fiscal-{symbol_offset + i}")
        _insert_per_candidate(journal, symbol, d, cache_id, outcome_value=rng.gauss(effect, 0.5))
        for j in range(controls_per_date):
            _insert_control_candidate(journal, f"CTL{symbol_offset + i}_{j}", d, outcome_value=rng.gauss(0.0, 0.5))
    return dates


_VALID_CARD_ROW = {
    "card_id": PER_CARD_ID, "version": 1, "name": PER_CARD_ID, "state": "shadow",
    "content_hash": "test-content-hash-abc123",
    "content_json": {"requires_selector": SELECTOR_VERSION},
}


def _seed_valid_card_registry(journal, **overrides):
    journal.insert("setup_cards", {**_VALID_CARD_ROW, **overrides})


def _register_valid_v2_pair(journal, analysis_not_before, floor_effective_n=20, floor_span_days=90.0):
    _seed_valid_card_registry(journal)
    identity = per_evidence.fetch_active_per_card_identity(journal)
    pos = register_hypothesis(
        journal, hypothesis=per_evidence.PER_HYPOTHESIS_POS_V2, metric=per_evidence.PER_METRIC_POS_V2,
        floor_effective_n=floor_effective_n, floor_span_days=floor_span_days,
        analysis_not_before=analysis_not_before, params={**identity, "direction": "positive"},
    )
    neg = register_hypothesis(
        journal, hypothesis=per_evidence.PER_HYPOTHESIS_NEG_V2, metric=per_evidence.PER_METRIC_NEG_V2,
        floor_effective_n=floor_effective_n, floor_span_days=floor_span_days,
        analysis_not_before=analysis_not_before, params={**identity, "direction": "negative"},
    )
    return pos, neg, identity


# ============================================================ Section 1/2/7:
# exact hash freezing
def test_registration_freezes_exact_card_content_hash_and_selector_semantic_hash(journal):
    """Section 2/Section 7: the corrected registration path must freeze the
    EXACT card_content_hash read from setup_cards (never recomputed or
    approximated) and the EXACT live selector semantic hash -- both
    obtained via fetch_active_per_card_identity(), never hand-typed."""
    _seed_valid_card_registry(journal, content_hash="exact-hash-0xdeadbeef")
    identity = per_evidence.fetch_active_per_card_identity(journal)
    assert identity["card_content_hash"] == "exact-hash-0xdeadbeef"
    assert identity["selector_semantic_hash"] == GOLDEN_FIXTURE_SEMANTIC_HASH

    prereg_pos = register_hypothesis(
        journal, hypothesis="H-TEST-1P", metric="test-metric-pos",
        floor_effective_n=1, floor_span_days=1.0, analysis_not_before="2020-01-01",
        params={**identity, "direction": "positive"},
    )
    row = journal.one("SELECT params_json FROM preregistrations WHERE prereg_id = ?", (prereg_pos,))
    frozen = json.loads(row["params_json"])
    assert frozen["card_content_hash"] == "exact-hash-0xdeadbeef"
    assert frozen["selector_semantic_hash"] == GOLDEN_FIXTURE_SEMANTIC_HASH
    assert frozen["card_id"] == PER_CARD_ID
    assert frozen["card_version"] == 1
    assert frozen["selector_version"] == SELECTOR_VERSION


# ============================================================ Section 2/7:
# registration-time refusal
def test_per_register_v2_refuses_on_card_hash_mismatch_card_absent(orchestrator, capsys):
    """'card-hash mismatch' umbrella: if the card cannot be trusted to have
    a real, current content_hash -- absent from the registry entirely --
    registration must refuse and write nothing. Startup is run FIRST (real
    sync_registry(), real card present) so the DELETE below is what
    actually produces the absence, not a coincidence of never having
    synced at all. Asserts the SPECIFIC refusal reason (via captured
    stdout), not just the exit code -- otherwise this test would stay
    green even if the refusal fired for the wrong underlying cause
    (architecture-audit finding)."""
    from alphaos.__main__ import cmd_per_register_v2

    orchestrator._ensure_startup()
    orchestrator.journal.conn.execute("DELETE FROM setup_cards WHERE card_id = ?", (PER_CARD_ID,))
    orchestrator.journal.conn.commit()
    assert cmd_per_register_v2(orchestrator) == 1
    assert "is not present in setup_cards" in capsys.readouterr().out
    rows = orchestrator.journal.query(
        "SELECT prereg_id FROM preregistrations WHERE hypothesis LIKE 'H-PER-1P-v2%'"
    )
    assert rows == []


def test_per_register_v2_refuses_on_card_hash_mismatch_wrong_version(orchestrator, capsys):
    from alphaos.__main__ import cmd_per_register_v2

    orchestrator._ensure_startup()
    orchestrator.journal.conn.execute("UPDATE setup_cards SET version = 2 WHERE card_id = ?", (PER_CARD_ID,))
    orchestrator.journal.conn.commit()
    assert cmd_per_register_v2(orchestrator) == 1
    assert "expected exactly" in capsys.readouterr().out
    rows = orchestrator.journal.query(
        "SELECT prereg_id FROM preregistrations WHERE hypothesis LIKE 'H-PER-1P-v2%'"
    )
    assert rows == []


def test_per_register_v2_refuses_on_card_hash_mismatch_wrong_state(orchestrator, capsys):
    from alphaos.__main__ import cmd_per_register_v2

    orchestrator._ensure_startup()
    orchestrator.journal.conn.execute(
        "UPDATE setup_cards SET state = 'live_eligible' WHERE card_id = ?", (PER_CARD_ID,),
    )
    orchestrator.journal.conn.commit()
    assert cmd_per_register_v2(orchestrator) == 1
    assert "expected 'shadow'" in capsys.readouterr().out
    rows = orchestrator.journal.query(
        "SELECT prereg_id FROM preregistrations WHERE hypothesis LIKE 'H-PER-1P-v2%'"
    )
    assert rows == []


def test_per_register_v2_refuses_on_selector_version_mismatch(orchestrator, capsys):
    """Section 2: the card's own declared requires_selector must match the
    live SELECTOR_VERSION -- simulated by rewriting the card's stored
    content_json to declare a stale selector version."""
    from alphaos.__main__ import cmd_per_register_v2

    orchestrator._ensure_startup()
    row = orchestrator.journal.one("SELECT content_json FROM setup_cards WHERE card_id = ?", (PER_CARD_ID,))
    content = json.loads(row["content_json"])
    content["requires_selector"] = "card_selector_v999_stale"
    orchestrator.journal.conn.execute(
        "UPDATE setup_cards SET content_json = ? WHERE card_id = ?", (json.dumps(content), PER_CARD_ID),
    )
    orchestrator.journal.conn.commit()
    assert cmd_per_register_v2(orchestrator) == 1
    assert "no longer matches the live SELECTOR_VERSION" in capsys.readouterr().out
    rows = orchestrator.journal.query(
        "SELECT prereg_id FROM preregistrations WHERE hypothesis LIKE 'H-PER-1P-v2%'"
    )
    assert rows == []


def test_per_register_v2_refuses_on_selector_semantic_hash_mismatch(orchestrator, monkeypatch, capsys):
    """Section 2: if the live selector's golden-fixture hash has drifted
    from its own pinned constant, registration must refuse -- a genuine
    swap test that reintroduces the exact drift condition (same technique
    as test_card_selector.py's own drift swap test)."""
    from alphaos.__main__ import cmd_per_register_v2
    from alphaos.cards import selector as selector_mod

    orchestrator._ensure_startup()
    monkeypatch.setattr(selector_mod, "GOLDEN_FIXTURE_SEMANTIC_HASH", "deliberately-wrong-hash")
    assert cmd_per_register_v2(orchestrator) == 1
    assert "selector semantic drift detected" in capsys.readouterr().out
    rows = orchestrator.journal.query(
        "SELECT prereg_id FROM preregistrations WHERE hypothesis LIKE 'H-PER-1P-v2%'"
    )
    assert rows == []


# ============================================================ Section 4/7:
# analysis_not_before hard gate
def test_evaluation_refuses_before_analysis_not_before_with_zero_writes(journal):
    _seed_population(journal, n_events=30, effect=2.0)
    pos, neg, _ = _register_valid_v2_pair(journal, analysis_not_before="2030-06-01")
    result = evaluate_two_arm_hypothesis_pair(
        journal, pos, neg, as_of_utc="2030-01-01T00:00:00+00:00", n_resamples=200, seed=1,
    )
    assert result["outcome"] == "deferred"
    assert result["reason"] == "before_analysis_not_before"
    pos_row = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (pos,))
    neg_row = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (neg,))
    assert pos_row["evaluated_at_utc"] is None
    assert neg_row["evaluated_at_utc"] is None
    assert pos_row["point_estimate"] is None
    assert journal.count_rows("per_evidence_snapshots") == 0

    # one-shot not consumed -- the SAME pair can still succeed once the
    # caller-supplied clock legitimately reaches analysis_not_before.
    later = evaluate_two_arm_hypothesis_pair(
        journal, pos, neg, as_of_utc="2030-06-01T00:00:00+00:00", n_resamples=300, seed=1,
    )
    assert later["outcome"] == "evaluated"


def test_evaluation_proceeds_past_date_gate_with_controlled_future_clock(journal):
    """Section 4: an injectable as_of_utc, never a wall-clock sleep or
    monkeypatched global time, is what lets evaluation legitimately
    proceed once analysis_not_before has (per the caller-supplied clock)
    arrived -- subject to every other existing gate (population, floor,
    identity)."""
    _seed_population(journal, n_events=30, effect=2.0)
    pos, neg, identity = _register_valid_v2_pair(journal, analysis_not_before="2030-01-01")
    result = evaluate_two_arm_hypothesis_pair(
        journal, pos, neg, as_of_utc="2030-01-02T00:00:00+00:00", n_resamples=500, seed=1,
    )
    assert result["outcome"] == "evaluated"
    assert result["pos"]["p_value"] < 0.05
    pos_row = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (pos,))
    assert pos_row["evaluated_at_utc"] is not None
    frozen = json.loads(pos_row["params_json"])
    assert {k: frozen.get(k) for k in identity} == identity


def test_evaluation_refuses_when_analysis_not_before_mismatched_between_rows(journal):
    _seed_population(journal, n_events=30, effect=2.0)
    pos, neg, _ = _register_valid_v2_pair(journal, analysis_not_before="2030-01-01")
    journal.conn.execute(
        "UPDATE preregistrations SET analysis_not_before = ? WHERE prereg_id = ?", ("2030-06-01", neg),
    )
    journal.conn.commit()
    result = evaluate_two_arm_hypothesis_pair(
        journal, pos, neg, as_of_utc="2031-01-01T00:00:00+00:00", n_resamples=200, seed=1,
    )
    assert result["outcome"] == "deferred"
    assert result["reason"] == "analysis_not_before_missing_or_mismatched"
    assert journal.count_rows("per_evidence_snapshots") == 0


# ============================================================ Section 5/7:
# non-mutation + distinct identifiers
def test_existing_v1_style_rows_are_never_mutated_by_v2_registration(orchestrator):
    """Section 5: the corrected pair must be a brand-new row pair, never a
    mutation of the original. Registers the ORIGINAL pair first (the real
    cmd_per_register(), unmodified by this follow-up), snapshots it
    byte-for-byte, then runs cmd_per_register_v2(), then re-reads the
    original rows and asserts nothing changed."""
    from alphaos.__main__ import cmd_per_register, cmd_per_register_v2

    orchestrator._ensure_startup()
    assert cmd_per_register(orchestrator) == 0
    before = orchestrator.journal.query("SELECT * FROM preregistrations WHERE hypothesis LIKE 'H-PER-1%'")
    assert len(before) == 2

    assert cmd_per_register_v2(orchestrator) == 0

    prereg_ids = [r["prereg_id"] for r in before]
    placeholders = ",".join("?" for _ in prereg_ids)
    after_rows = orchestrator.journal.query(
        f"SELECT * FROM preregistrations WHERE prereg_id IN ({placeholders})", prereg_ids,
    )
    after = {r["prereg_id"]: r for r in after_rows}
    for row in before:
        assert after[row["prereg_id"]] == row, "original v1 row mutated by v2 registration"


def test_corrected_pair_uses_distinct_identifiers_from_the_original_pair(orchestrator):
    from alphaos.__main__ import cmd_per_register, cmd_per_register_v2

    orchestrator._ensure_startup()
    assert cmd_per_register(orchestrator) == 0
    assert cmd_per_register_v2(orchestrator) == 0

    rows = orchestrator.journal.query("SELECT prereg_id, hypothesis, metric FROM preregistrations")
    assert len(rows) == 4
    prereg_ids = [r["prereg_id"] for r in rows]
    assert len(set(prereg_ids)) == 4, "all four rows must have distinct prereg_ids"

    v1_pairs = {(r["hypothesis"], r["metric"]) for r in rows if "-v2" not in r["hypothesis"]}
    v2_pairs = {(r["hypothesis"], r["metric"]) for r in rows if "-v2" in r["hypothesis"]}
    assert len(v1_pairs) == 2
    assert len(v2_pairs) == 2
    assert v1_pairs.isdisjoint(v2_pairs)
    # Never a silent reuse of the original text, even partially.
    for hyp, metric in v1_pairs:
        assert (hyp, metric) not in v2_pairs


# ============================================================ Section 7:
# atomicity + idempotency for the corrected pair specifically
def test_atomic_evaluation_intact_for_a_v2_shaped_pair(journal):
    """Proves the pre-existing atomicity guarantee (partial writes
    impossible; a race caught mid-flight rolls back both rows) still holds
    for a pair that carries BOTH the new identity fields and a reached
    analysis_not_before -- the exact shape the corrected pair will have
    once it clears every new gate."""
    _seed_population(journal, n_events=30, effect=1.0)
    pos, neg, _ = _register_valid_v2_pair(journal, analysis_not_before="2030-01-01")
    journal.conn.execute(
        "UPDATE preregistrations SET evaluated_at_utc = ? WHERE prereg_id = ?",
        ("2030-01-02T00:00:00+00:00", neg),
    )
    journal.conn.commit()
    with pytest.raises(PreregistrationAlreadyEvaluatedError):
        evaluate_two_arm_hypothesis_pair(
            journal, pos, neg, as_of_utc="2030-01-02T00:00:00+00:00", n_resamples=200, seed=1,
        )
    pos_row = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (pos,))
    assert pos_row["evaluated_at_utc"] is None, "pos must remain unevaluated -- no partial write"
    assert journal.count_rows("per_evidence_snapshots") == 0


def test_per_register_v2_is_idempotent(orchestrator):
    from alphaos.__main__ import cmd_per_register_v2

    orchestrator._ensure_startup()
    assert cmd_per_register_v2(orchestrator) == 0
    assert cmd_per_register_v2(orchestrator) == 0
    pos_rows = orchestrator.journal.query(
        "SELECT prereg_id FROM preregistrations WHERE hypothesis LIKE 'H-PER-1P-v2%'"
    )
    neg_rows = orchestrator.journal.query(
        "SELECT prereg_id FROM preregistrations WHERE hypothesis LIKE 'H-PER-1N-v2%'"
    )
    assert len(pos_rows) == 1
    assert len(neg_rows) == 1


# ============================================================ Section 6/7:
# S1c preflight diagnostic
def test_s1c_preflight_false_when_corrected_pair_not_registered(journal):
    result = per_evidence.s1c_activation_preflight(journal)
    assert result == {
        "ready": False,
        "reason": "corrected_pair_not_registered",
        "detail": {"pos_found": False, "neg_found": False},
    }


def test_s1c_preflight_ignores_the_original_incomplete_pair(journal):
    """The 'no older incomplete pair is selected as the active pair'
    guarantee: registering ONLY the original v1-shaped H-PER-1P/H-PER-1N
    pair (no identity fields, exact real production hypothesis/metric
    text) must NOT satisfy the preflight -- it looks the corrected pair up
    by ITS OWN exact identity, never a loose LIKE 'H-PER-1%' match."""
    register_hypothesis(
        journal,
        hypothesis=(
            "H-PER-1P: post_earnings_reaction_v1 candidates have a POSITIVE mean "
            "5-trading-day market-adjusted excess outcome over contemporaneous "
            "date x tier default-card candidates"
        ),
        metric="per_excess_market_adjusted_5d, smooth_weight_joint_bootstrap_v1",
        floor_effective_n=20, floor_span_days=90.0, analysis_not_before="2026-10-15",
    )
    register_hypothesis(
        journal,
        hypothesis=(
            "H-PER-1N: post_earnings_reaction_v1 candidates have a NEGATIVE mean "
            "5-trading-day market-adjusted excess outcome over contemporaneous "
            "date x tier default-card candidates"
        ),
        metric="per_excess_market_adjusted_5d_negated, smooth_weight_joint_bootstrap_v1",
        floor_effective_n=20, floor_span_days=90.0, analysis_not_before="2026-10-15",
    )
    result = per_evidence.s1c_activation_preflight(journal)
    assert result["ready"] is False
    assert result["reason"] == "corrected_pair_not_registered"


def test_s1c_preflight_ready_true_for_a_correctly_registered_pair(journal):
    _, _, identity = _register_valid_v2_pair(journal, analysis_not_before="2030-01-01")
    result = per_evidence.s1c_activation_preflight(journal)
    assert result["ready"] is True
    assert result["reason"] is None
    assert result["detail"]["identity"] == identity


def test_s1c_preflight_false_once_evaluated(journal):
    _seed_population(journal, n_events=30, effect=2.0)
    pos, neg, _ = _register_valid_v2_pair(journal, analysis_not_before="2026-01-01")
    result = evaluate_two_arm_hypothesis_pair(
        journal, pos, neg, as_of_utc="2027-01-01T00:00:00+00:00", n_resamples=300, seed=1,
    )
    assert result["outcome"] == "evaluated"
    preflight = per_evidence.s1c_activation_preflight(journal)
    assert preflight["ready"] is False
    assert preflight["reason"] == "already_evaluated"


def test_s1c_preflight_false_when_live_identity_drifts(journal):
    _register_valid_v2_pair(journal, analysis_not_before="2030-01-01")
    journal.conn.execute(
        "UPDATE setup_cards SET content_hash = ? WHERE card_id = ?", ("changed-hash", PER_CARD_ID),
    )
    journal.conn.commit()
    result = per_evidence.s1c_activation_preflight(journal)
    assert result["ready"] is False
    assert result["reason"] == "identity_drifted_from_live_state"


def test_s1c_preflight_false_when_analysis_not_before_mismatched(journal):
    _, neg, _ = _register_valid_v2_pair(journal, analysis_not_before="2030-01-01")
    journal.conn.execute(
        "UPDATE preregistrations SET analysis_not_before = ? WHERE prereg_id = ?", ("2031-01-01", neg),
    )
    journal.conn.commit()
    result = per_evidence.s1c_activation_preflight(journal)
    assert result["ready"] is False
    assert result["reason"] == "analysis_not_before_missing_or_mismatched"


def test_s1c_preflight_never_wired_into_any_cli_command():
    """s1c_activation_preflight() is a pure diagnostic -- confirms it is
    not dispatched by any subcommand (would be an accidental activation
    trigger, which this follow-up explicitly forbids)."""
    from alphaos.__main__ import build_parser

    parser = build_parser()
    sub_action = next(a for a in parser._subparsers._group_actions if hasattr(a, "choices"))
    command_names = set(sub_action.choices.keys())
    assert not any("s1c" in name.lower() for name in command_names)
    assert not any("preflight" in name.lower() for name in command_names)
    assert "per_register_v2" in command_names
    assert "per_evaluate_v2" not in command_names  # evaluation stays generic, no v2-specific CLI


# ============================================================ Section 7:
# isolation -- this follow-up's own new symbols never reach production
PRODUCTION_FILES = [
    (orchestrator_mod, "orchestrator.py"),
    (candidate_scanner_mod, "candidate_scanner.py"),
    (cards_registry_mod, "cards/registry.py"),
]


@pytest.mark.parametrize("mod,name", PRODUCTION_FILES)
def test_new_integrity_symbols_never_referenced_by_production_files(mod, name):
    """Extends the existing S1b isolation guarantee to this follow-up's OWN
    new names: none of orchestrator.py, candidate_scanner.py, or
    cards/registry.py may reference the v2 registration command, the
    identity-freeze mechanism, or the S1c preflight diagnostic."""
    text = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    forbidden = (
        "per_register_v2", "fetch_active_per_card_identity", "CardIdentityError",
        "s1c_activation_preflight", "PER_HYPOTHESIS_POS_V2", "verify_selector_semantic_identity",
    )
    for token in forbidden:
        assert token not in text, f"{name} references {token!r} -- S1c must remain unwired"


def test_production_scan_after_v2_registration_still_zero_per_assignments(orchestrator):
    """Section 7: a full production scan must still produce zero
    post_earnings_reaction assignments even after the corrected pair has
    been registered (registering evidence machinery must never itself
    wire the selector)."""
    from alphaos.__main__ import cmd_per_register_v2

    orchestrator._ensure_startup()
    assert cmd_per_register_v2(orchestrator) == 0
    orchestrator.run_scan_once()
    total_candidates = orchestrator.journal.scalar("SELECT COUNT(*) FROM candidates")
    assert total_candidates > 0, "scan produced no candidates at all -- this test can no longer prove anything"
    count = orchestrator.journal.scalar(
        "SELECT COUNT(*) FROM candidates WHERE card_id = ?", (PER_CARD_ID,),
    )
    assert count == 0
