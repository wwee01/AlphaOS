"""SETUP-1 S1c: candidate-level activation of the card selector.

Covers: the activation-preflight gate (alphaos.cards.activation), the
conditional card-assignment wiring in candidate_scanner.py/orchestrator.py,
core+shadow coverage, prospective-only/non-retroactive assignment,
decision-path isolation (S1c changes candidate evidence metadata ONLY),
and the daily-brief observability hook.

NOT covered here (already fully proven elsewhere, not duplicated):
  - select_card()'s own BMO/AMC/UNKNOWN/K=3/reschedule/NULL-fiscal timing
    rules and the strict point-in-time cache boundary: tests/
    test_card_selector.py (S1a) -- select_card()/build_selector_context()
    are UNCHANGED by S1c, only WIRED IN, so those proofs still hold
    verbatim.
  - s1c_activation_preflight()'s own identity/evaluated/evidence-null/
    analysis_not_before checks: tests/test_s1b_integrity_followup.py
    (S1b-integrity). This file adds ONLY the ONE additional layer S1c
    introduces: build_scan_card_activation() correctly turns a preflight
    verdict into an activation decision, and candidate_scanner.py/
    orchestrator.py correctly consume that decision.

Fixture-date discipline: every earnings-eligibility scenario builds its
OWN SelectorContext with an explicit, hand-chosen assignment_as_of_utc
(never the real wall clock) -- calling CandidateScanner.scan()/
scan_shadow_tier() directly rather than through Orchestrator.run_scan_once
(whose own assignment_as_of_utc is always real "now"). This keeps every
eligibility assertion deterministic regardless of what real calendar day
the suite happens to run on (house law: never hardcode a calendar date,
but also never let a real wall-clock read decide whether a test passes).
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from datetime import date, timedelta

import pytest

import alphaos.orchestrator as orchestrator_mod
import alphaos.scanner.candidate_scanner as candidate_scanner_mod
from alphaos.cards import per_evidence
from alphaos.cards.activation import (
    ACTIVATION_ERROR_STATUS,
    PREFLIGHT_FAILED_STATUS,
    ScanCardActivation,
    build_scan_card_activation,
)
from alphaos.cards.selector import CacheHealth, PER_CARD_ID, SELECTOR_VERSION, SelectorContext
from alphaos.config.settings import load_settings
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.scanner.candidate_scanner import DEFAULT_UNIVERSE, CandidateScanner
from alphaos.util.ids import new_id
from alphaos.util.market_calendar import is_trading_day, nth_trading_day_after


def make_settings(**overrides):
    env = {
        "ALPHAOS_MODE": "mock", "APPROVAL_MODE": "manual", "REAL_TRADING_ENABLED": "false",
        "ALPHAOS_DB_PATH": ":memory:", "MAX_AUTO_APPROVALS_PER_DAY": "1",
    }
    env.update({k: str(v) for k, v in overrides.items()})
    return load_settings(load_env_file=False, env=env)


# --------------------------------------------------------------- fixed "today"
# A deterministically-computed trading day, never a hardcoded literal --
# used ONLY as the anchor for hand-built SelectorContexts/assignment_as_of_utc
# strings in this file, never fed to the real wall clock.
def _trading_day_on_or_after(d: date) -> date:
    return d if is_trading_day(d) else nth_trading_day_after(d, 1)


ANCHOR_DAY = _trading_day_on_or_after(date(2026, 6, 1))  # a known, ordinary mid-week trading day
ANCHOR_AS_OF_UTC = f"{ANCHOR_DAY.isoformat()}T20:00:00+00:00"


@pytest.fixture
def journal():
    store = JournalStore(":memory:")
    yield store
    store.close()


@pytest.fixture
def orchestrator(journal):
    orch = Orchestrator(settings=make_settings(), journal=journal)
    yield orch


# --------------------------------------------------------------- fixture helpers
_VALID_CARD_ROW = {
    "card_id": PER_CARD_ID, "version": 1, "name": PER_CARD_ID, "state": "shadow",
    "content_hash": "test-content-hash-s1c",
    "content_json": {"requires_selector": SELECTOR_VERSION},
}


def _seed_valid_card_registry(journal, **overrides):
    journal.insert("setup_cards", {**_VALID_CARD_ROW, **overrides})


def _register_valid_v2_pair(journal, analysis_not_before="2020-01-01"):
    from alphaos.stats.preregistration import register_hypothesis

    _seed_valid_card_registry(journal)
    identity = per_evidence.fetch_active_per_card_identity(journal)
    pos = register_hypothesis(
        journal, hypothesis=per_evidence.PER_HYPOTHESIS_POS_V2, metric=per_evidence.PER_METRIC_POS_V2,
        floor_effective_n=20, floor_span_days=90.0,
        analysis_not_before=analysis_not_before, params={**identity, "direction": "positive"},
    )
    neg = register_hypothesis(
        journal, hypothesis=per_evidence.PER_HYPOTHESIS_NEG_V2, metric=per_evidence.PER_METRIC_NEG_V2,
        floor_effective_n=20, floor_span_days=90.0,
        analysis_not_before=analysis_not_before, params={**identity, "direction": "negative"},
    )
    return pos, neg, identity


def _insert_earnings_row(journal, symbol, report_date, timing, fiscal_date_ending=None, created_at_utc=None):
    row = {
        "entry_id": new_id("ecc"), "symbol": symbol, "report_date": report_date,
        "fiscal_date_ending": fiscal_date_ending, "timing": timing, "source": "test",
    }
    if created_at_utc is not None:
        row["created_at_utc"] = created_at_utc
    return journal.insert("earnings_calendar_cache", row)


def _insert_healthy_pull_run(journal, finished_at_utc, n_fetched=10):
    """Same convention as tests/test_card_selector.py's own
    _insert_pull_run(): compute_cache_health() reads job_runs, NOT just
    the presence of earnings_calendar_cache rows -- a cache row without a
    recent, usable job_runs entry still degrades to STALE/CACHE_EMPTY, so
    every test that needs cache_health == 'ok' must seed this too."""
    result_summary = {"status": "completed", "earnings_calendar_result": {
        "market_date": finished_at_utc[:10], "n_fetched": n_fetched, "n_written": n_fetched, "warnings": [],
    }}
    journal.insert("job_runs", {
        "job_run_id": new_id("jr"), "job_type": "earnings_calendar_pull",
        "started_at_utc": finished_at_utc, "started_at_sgt": finished_at_utc,
        "finished_at_utc": finished_at_utc, "finished_at_sgt": finished_at_utc,
        "status": "completed", "result_summary_json": json.dumps(result_summary),
    })


def _force_momentum(monkeypatch, market_client):
    """Same technique as tests/test_exp1_shadow_labelling.py's own
    _force_momentum_candidates(): MockDataProvider seeds its RNG per
    (symbol, real calendar day), so whether a symbol clears the momentum
    gate is otherwise a function of the wall-clock date, not just the
    symbol. Forcing change_pct past the 2% gate removes that flakiness
    without faking any other snapshot field."""
    orig_get_snapshot = market_client.provider.get_snapshot

    def _forced(symbol):
        snap = orig_get_snapshot(symbol)
        snap["change_pct"] = 0.05
        return snap

    monkeypatch.setattr(market_client.provider, "get_snapshot", _forced)

    orig_get_snapshots = market_client.provider.get_snapshots

    def _forced_batch(symbols):
        snaps = orig_get_snapshots(symbols)
        for s in snaps:
            s["change_pct"] = 0.05
        return snaps

    monkeypatch.setattr(market_client.provider, "get_snapshots", _forced_batch)


# ============================================================ Section 1/9:
# activation preflight
def test_activation_active_when_corrected_pair_is_ready(journal):
    _register_valid_v2_pair(journal)
    activation = build_scan_card_activation(journal, ANCHOR_AS_OF_UTC, DEFAULT_UNIVERSE)
    assert activation.active is True
    assert activation.reason is None
    assert isinstance(activation.context, SelectorContext)
    assert activation.context.assignment_as_of_utc == ANCHOR_AS_OF_UTC


def test_activation_inactive_when_corrected_pair_not_registered(journal):
    activation = build_scan_card_activation(journal, ANCHOR_AS_OF_UTC, DEFAULT_UNIVERSE)
    assert activation.active is False
    assert activation.context is None
    assert activation.reason == "corrected_pair_not_registered"


def test_activation_inactive_when_only_the_original_incomplete_pair_exists(journal):
    from alphaos.stats.preregistration import register_hypothesis

    register_hypothesis(
        journal,
        hypothesis=(
            "H-PER-1P: post_earnings_reaction_v1 candidates have a POSITIVE mean "
            "5-trading-day market-adjusted excess outcome over contemporaneous "
            "date x tier default-card candidates"
        ),
        metric="per_excess_market_adjusted_5d, smooth_weight_joint_bootstrap_v1",
        floor_effective_n=20, floor_span_days=90.0, analysis_not_before="2020-01-01",
    )
    register_hypothesis(
        journal,
        hypothesis=(
            "H-PER-1N: post_earnings_reaction_v1 candidates have a NEGATIVE mean "
            "5-trading-day market-adjusted excess outcome over contemporaneous "
            "date x tier default-card candidates"
        ),
        metric="per_excess_market_adjusted_5d_negated, smooth_weight_joint_bootstrap_v1",
        floor_effective_n=20, floor_span_days=90.0, analysis_not_before="2020-01-01",
    )
    activation = build_scan_card_activation(journal, ANCHOR_AS_OF_UTC, DEFAULT_UNIVERSE)
    assert activation.active is False
    assert activation.reason == "corrected_pair_not_registered"


def test_activation_inactive_when_live_card_hash_drifts_after_registration(journal):
    _register_valid_v2_pair(journal)
    journal.conn.execute(
        "UPDATE setup_cards SET content_hash = ? WHERE card_id = ?", ("drifted-hash", PER_CARD_ID),
    )
    journal.conn.commit()
    activation = build_scan_card_activation(journal, ANCHOR_AS_OF_UTC, DEFAULT_UNIVERSE)
    assert activation.active is False
    assert activation.reason == "identity_drifted_from_live_state"


def test_activation_inactive_when_selector_semantic_hash_drifts(journal, monkeypatch):
    from alphaos.cards import selector as selector_mod

    _register_valid_v2_pair(journal)
    monkeypatch.setattr(selector_mod, "GOLDEN_FIXTURE_SEMANTIC_HASH", "deliberately-wrong-hash")
    activation = build_scan_card_activation(journal, ANCHOR_AS_OF_UTC, DEFAULT_UNIVERSE)
    assert activation.active is False
    assert activation.reason == "live_identity_unavailable"


def test_activation_inactive_once_the_pair_is_evaluated(journal):
    pos, neg, _ = _register_valid_v2_pair(journal)
    journal.conn.execute("UPDATE preregistrations SET evaluated_at_utc = ? WHERE prereg_id = ?",
                         ("2020-06-01T00:00:00+00:00", pos))
    journal.conn.commit()
    activation = build_scan_card_activation(journal, ANCHOR_AS_OF_UTC, DEFAULT_UNIVERSE)
    assert activation.active is False
    assert activation.reason == "already_evaluated"


def test_activation_inactive_when_evidence_fields_are_populated(journal):
    pos, neg, _ = _register_valid_v2_pair(journal)
    journal.conn.execute("UPDATE preregistrations SET point_estimate = ? WHERE prereg_id = ?", (1.0, pos))
    journal.conn.commit()
    activation = build_scan_card_activation(journal, ANCHOR_AS_OF_UTC, DEFAULT_UNIVERSE)
    assert activation.active is False
    assert activation.reason == "evidence_fields_not_null"


def test_activation_never_raises_on_corrupted_params_json(journal):
    """Audit finding: s1c_activation_preflight() makes no actual never-
    raises promise (its json.loads(params_json) is unguarded) --
    build_scan_card_activation() must be the one place that actually
    holds that line, degrading to active=False with ACTIVATION_ERROR_STATUS
    rather than propagating json.JSONDecodeError and crashing the scan."""
    pos, _, _ = _register_valid_v2_pair(journal)
    journal.conn.execute(
        "UPDATE preregistrations SET params_json = ? WHERE prereg_id = ?", ("{not valid json", pos),
    )
    journal.conn.commit()
    activation = build_scan_card_activation(journal, ANCHOR_AS_OF_UTC, DEFAULT_UNIVERSE)
    assert activation.active is False
    assert activation.context is None
    assert activation.reason == ACTIVATION_ERROR_STATUS


def test_resolve_card_assignment_never_crashes_on_malformed_cache_row(journal, monkeypatch):
    """Audit finding: select_card() makes no never-raises promise either
    (a hand-corrupted earnings_calendar_cache row -- e.g. an unparseable
    report_date -- would raise inside it). The per-candidate call in
    candidate_scanner.py must degrade THAT ONE candidate to the default
    card rather than crashing the whole scan over one bad row."""
    symbol = DEFAULT_UNIVERSE[15]
    _insert_earnings_row(
        journal, symbol, "not-a-real-date", timing="pre-market",
        created_at_utc=f"{(ANCHOR_DAY - timedelta(days=1)).isoformat()}T00:00:00+00:00",
    )
    _insert_healthy_pull_run(journal, f"{(ANCHOR_DAY - timedelta(days=1)).isoformat()}T01:00:00+00:00")
    _register_valid_v2_pair(journal)
    activation = build_scan_card_activation(journal, ANCHOR_AS_OF_UTC, DEFAULT_UNIVERSE)
    assert activation.active is True

    scanner = _scanner_with_forced_momentum(journal, monkeypatch)
    result = scanner.scan(symbols=[symbol], scan_batch_id="batch1", card_activation=activation)
    assert len(result.candidates) == 1, "one bad cache row must not crash the whole scan"
    cand = result.candidates[0].row
    assert cand["card_id"] != PER_CARD_ID
    assert cand["card_assignment_status"] == ACTIVATION_ERROR_STATUS


def test_preflight_failure_emits_warning_and_assigns_zero_per_candidates(orchestrator):
    """Full run_scan_once() with no corrected pair registered -- the
    fail-closed contract end to end: a WARNING system event names the
    failure, and zero candidates ever carry card_id=PER_CARD_ID."""
    summary = orchestrator.run_scan_once()
    assert summary.candidates >= 0  # scan completed without crashing
    events = orchestrator.journal.query(
        "SELECT * FROM system_events WHERE category = 'cards' AND severity = 'warning'"
    )
    assert any("preflight" in (e["message"] or "").lower() or "unavailable" in (e["message"] or "").lower()
               for e in events), "expected a WARNING system event naming the preflight failure"
    per_count = orchestrator.journal.scalar(
        "SELECT COUNT(*) FROM candidates WHERE card_id = ?", (PER_CARD_ID,),
    )
    assert per_count == 0


# ============================================================ Section 3/9:
# candidate assignment wiring
def _scanner_with_forced_momentum(journal, monkeypatch, settings=None):
    scanner = CandidateScanner(settings or make_settings(), journal)
    _force_momentum(monkeypatch, scanner.market)
    return scanner


def test_core_per_assignment_for_a_qualifying_bmo_event(journal, monkeypatch):
    """A BMO earnings report_date == ANCHOR_DAY makes ANCHOR_DAY itself
    inside the PER window (BMO opens ON the report date) -- select_card()
    must tag the candidate post_earnings_reaction with card_assignment_ref
    pointing at the exact cache row used."""
    symbol = DEFAULT_UNIVERSE[4]  # a real core-universe symbol
    row_id = _insert_earnings_row(
        journal, symbol, ANCHOR_DAY.isoformat(), timing="pre-market",
        created_at_utc=f"{(ANCHOR_DAY - timedelta(days=1)).isoformat()}T00:00:00+00:00",
    )
    _insert_healthy_pull_run(journal, f"{(ANCHOR_DAY - timedelta(days=1)).isoformat()}T01:00:00+00:00")
    _register_valid_v2_pair(journal)
    activation = build_scan_card_activation(journal, ANCHOR_AS_OF_UTC, DEFAULT_UNIVERSE)
    assert activation.active is True
    assert activation.context is not None
    assert activation.context.cache_health == CacheHealth.OK

    scanner = _scanner_with_forced_momentum(journal, monkeypatch)
    result = scanner.scan(symbols=[symbol], scan_batch_id="batch1", card_activation=activation)
    assert len(result.candidates) == 1
    cand = result.candidates[0].row
    assert cand["card_id"] == PER_CARD_ID
    assert cand["card_version"] == 1
    assert str(cand["card_assignment_ref"]) == str(row_id)
    assert cand["card_assignment_status"] == "ok"
    assert cand["card_selector_version"] == SELECTOR_VERSION
    assert cand["shadow_tier"] == 0


def test_shadow_per_assignment_for_a_qualifying_event(journal, monkeypatch):
    """The SAME activation/context, reused verbatim for a shadow-tier
    candidate -- core and shadow share identical selector semantics."""
    symbol = "SHADOWSYM1"
    row_id = _insert_earnings_row(
        journal, symbol, ANCHOR_DAY.isoformat(), timing="pre-market",
        created_at_utc=f"{(ANCHOR_DAY - timedelta(days=1)).isoformat()}T00:00:00+00:00",
    )
    _insert_healthy_pull_run(journal, f"{(ANCHOR_DAY - timedelta(days=1)).isoformat()}T01:00:00+00:00")
    _register_valid_v2_pair(journal)
    activation = build_scan_card_activation(journal, ANCHOR_AS_OF_UTC, [symbol])
    assert activation.active is True

    scanner = _scanner_with_forced_momentum(journal, monkeypatch)
    result = scanner.scan_shadow_tier([symbol], scan_batch_id="batch1", card_activation=activation)
    assert len(result.candidates) == 1
    cand = result.candidates[0].row
    assert cand["card_id"] == PER_CARD_ID
    assert str(cand["card_assignment_ref"]) == str(row_id)
    assert cand["card_assignment_status"] == "ok"
    assert cand["shadow_tier"] == 1


def test_healthy_default_assignment_when_no_eligible_event(journal, monkeypatch):
    symbol = DEFAULT_UNIVERSE[5]
    # A cache row for a DIFFERENT symbol (so cache_health is 'ok' and this
    # scan's own symbol is genuinely evaluated, not just cache_empty).
    _insert_earnings_row(
        journal, "SOMEOTHERSYM", ANCHOR_DAY.isoformat(), timing="pre-market",
        created_at_utc=f"{(ANCHOR_DAY - timedelta(days=1)).isoformat()}T00:00:00+00:00",
    )
    _insert_healthy_pull_run(journal, f"{(ANCHOR_DAY - timedelta(days=1)).isoformat()}T01:00:00+00:00")
    _register_valid_v2_pair(journal)
    activation = build_scan_card_activation(journal, ANCHOR_AS_OF_UTC, DEFAULT_UNIVERSE)
    assert activation.active is True
    assert activation.context is not None
    assert activation.context.cache_health == CacheHealth.OK

    scanner = _scanner_with_forced_momentum(journal, monkeypatch)
    result = scanner.scan(symbols=[symbol], scan_batch_id="batch1", card_activation=activation)
    assert len(result.candidates) == 1
    cand = result.candidates[0].row
    assert cand["card_id"] != PER_CARD_ID
    assert cand["card_assignment_status"] == "ok"
    assert cand["card_assignment_ref"] is None
    assert cand["card_selector_version"] == SELECTOR_VERSION


@pytest.mark.parametrize("degraded_status", [
    CacheHealth.REFRESH_FAILED_RECENT, CacheHealth.STALE, CacheHealth.CACHE_EMPTY, CacheHealth.UNKNOWN,
])
def test_degraded_cache_health_always_forces_default_card(journal, monkeypatch, degraded_status):
    """Bypasses build_selector_context()'s own cache-health computation
    (already exhaustively tested in test_card_selector.py) and instead
    hand-builds a SelectorContext with the degraded status directly --
    this test's own job is only to prove candidate_scanner.py correctly
    SURFACES whatever cache_health the context carries onto the row."""
    from alphaos.cards.registry import get_default_card

    context = SelectorContext(
        assignment_as_of_utc=ANCHOR_AS_OF_UTC, cache_health=degraded_status,
        default_card=get_default_card(), current_belief_by_symbol={},
    )
    activation = ScanCardActivation(active=True, context=context, reason=None)
    symbol = DEFAULT_UNIVERSE[6]
    scanner = _scanner_with_forced_momentum(journal, monkeypatch)
    result = scanner.scan(symbols=[symbol], scan_batch_id="batch1", card_activation=activation)
    assert len(result.candidates) == 1
    cand = result.candidates[0].row
    assert cand["card_id"] != PER_CARD_ID
    assert cand["card_assignment_status"] == degraded_status
    assert cand["card_assignment_ref"] is None


def test_preflight_failed_assignment_status_is_distinct_from_cache_health(journal, monkeypatch):
    activation = ScanCardActivation(active=False, context=None, reason="corrected_pair_not_registered")
    symbol = DEFAULT_UNIVERSE[7]
    scanner = _scanner_with_forced_momentum(journal, monkeypatch)
    result = scanner.scan(symbols=[symbol], scan_batch_id="batch1", card_activation=activation)
    cand = result.candidates[0].row
    assert cand["card_assignment_status"] == PREFLIGHT_FAILED_STATUS
    assert PREFLIGHT_FAILED_STATUS not in vars(CacheHealth).values()
    assert cand["card_id"] != PER_CARD_ID


def test_no_activation_supplied_is_byte_identical_to_pre_s1c_behavior(journal, monkeypatch):
    """card_activation=None (the default) -- a caller outside
    Orchestrator.run_scan_once, e.g. a future direct CandidateScanner use
    -- must leave the three S1c-only fields exactly None, matching every
    candidate row created before this slice existed."""
    symbol = DEFAULT_UNIVERSE[8]
    scanner = _scanner_with_forced_momentum(journal, monkeypatch)
    result = scanner.scan(symbols=[symbol], scan_batch_id="batch1")
    cand = result.candidates[0].row
    assert cand["card_assignment_status"] is None
    assert cand["card_assignment_ref"] is None
    assert cand["card_selector_version"] is None


def test_exactly_one_card_per_candidate(journal, monkeypatch):
    symbol = DEFAULT_UNIVERSE[9]
    _insert_earnings_row(
        journal, symbol, ANCHOR_DAY.isoformat(), timing="pre-market",
        created_at_utc=f"{(ANCHOR_DAY - timedelta(days=1)).isoformat()}T00:00:00+00:00",
    )
    _insert_healthy_pull_run(journal, f"{(ANCHOR_DAY - timedelta(days=1)).isoformat()}T01:00:00+00:00")
    _register_valid_v2_pair(journal)
    activation = build_scan_card_activation(journal, ANCHOR_AS_OF_UTC, DEFAULT_UNIVERSE)
    scanner = _scanner_with_forced_momentum(journal, monkeypatch)
    result = scanner.scan(symbols=[symbol], scan_batch_id="batch1", card_activation=activation)
    row = journal.one("SELECT card_id, card_version FROM candidates WHERE candidate_id = ?",
                      (result.candidates[0].row["candidate_id"],))
    assert row["card_id"] is not None and row["card_version"] is not None


# ============================================================ Section 2/9:
# one frozen context per scan + mid-scan immutability
def test_selector_context_loaded_exactly_once_per_scan_batch(journal, monkeypatch):
    """Instruments build_selector_context itself (module-level function,
    called from within build_scan_card_activation) -- proves the loader
    runs once per scan, not once per candidate, regardless of how many
    symbols the scan covers."""
    _register_valid_v2_pair(journal)
    calls = []
    import alphaos.cards.activation as activation_mod
    original = activation_mod.build_selector_context

    def _counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(activation_mod, "build_selector_context", _counting)
    activation = activation_mod.build_scan_card_activation(journal, ANCHOR_AS_OF_UTC, DEFAULT_UNIVERSE)
    assert len(calls) == 1

    scanner = _scanner_with_forced_momentum(journal, monkeypatch)
    scanner.scan(symbols=DEFAULT_UNIVERSE[:5], scan_batch_id="batch1", card_activation=activation)
    # Scanning 5 symbols must not trigger any additional context load --
    # the SAME already-built activation object is reused verbatim.
    assert len(calls) == 1


def test_mid_scan_cache_insertion_cannot_alter_an_already_frozen_batch(journal, monkeypatch):
    """Builds the activation/context ONCE (as a real scan would, before
    any candidate exists), THEN inserts a new qualifying earnings row,
    THEN proves select_card() -- consulted via the SAME frozen context --
    still does not see it: the row simply postdates the context's own
    strict point-in-time boundary."""
    symbol = DEFAULT_UNIVERSE[10]
    # A cache row for a DIFFERENT symbol establishes cache_health='ok'
    # BEFORE the context is built, so the frozen context's own cache_health
    # is genuinely 'ok' (not cache_empty) when the mid-scan row lands.
    _insert_earnings_row(
        journal, "PRECEDINGSYM", ANCHOR_DAY.isoformat(), timing="pre-market",
        created_at_utc=f"{(ANCHOR_DAY - timedelta(days=1)).isoformat()}T00:00:00+00:00",
    )
    _insert_healthy_pull_run(journal, f"{(ANCHOR_DAY - timedelta(days=1)).isoformat()}T01:00:00+00:00")
    _register_valid_v2_pair(journal)
    activation = build_scan_card_activation(journal, ANCHOR_AS_OF_UTC, DEFAULT_UNIVERSE)
    assert activation.active is True
    assert activation.context is not None
    assert activation.context.cache_health == CacheHealth.OK

    # A row inserted AFTER the context was built, dated as though it were
    # legitimately eligible -- but created_at_utc is unconstrained here
    # (mid-scan real time), simulating a concurrent cache refresh mid-batch.
    _insert_earnings_row(journal, symbol, ANCHOR_DAY.isoformat(), timing="pre-market")

    scanner = _scanner_with_forced_momentum(journal, monkeypatch)
    result = scanner.scan(symbols=[symbol], scan_batch_id="batch1", card_activation=activation)
    cand = result.candidates[0].row
    assert cand["card_id"] != PER_CARD_ID, (
        "a cache row inserted after the context was frozen must never alter this batch's assignment"
    )


# ============================================================ Section 4/9:
# prospective-only / immutable assignment
def test_original_candidate_untouched_by_a_later_reschedule_and_more_scans(journal, monkeypatch):
    symbol = DEFAULT_UNIVERSE[11]
    row1_created = f"{(ANCHOR_DAY - timedelta(days=2)).isoformat()}T00:00:00+00:00"
    row1 = _insert_earnings_row(
        journal, symbol, ANCHOR_DAY.isoformat(), timing="pre-market",
        fiscal_date_ending="2026-fiscal-Q1", created_at_utc=row1_created,
    )
    # Within 48h of ANCHOR_AS_OF_UTC (row1_created itself is 2 days --
    # 68h -- before it, too stale on its own).
    _insert_healthy_pull_run(journal, f"{(ANCHOR_DAY - timedelta(days=1)).isoformat()}T01:00:00+00:00")
    _register_valid_v2_pair(journal)
    activation1 = build_scan_card_activation(journal, ANCHOR_AS_OF_UTC, DEFAULT_UNIVERSE)
    scanner = _scanner_with_forced_momentum(journal, monkeypatch)
    result1 = scanner.scan(symbols=[symbol], scan_batch_id="batch1", card_activation=activation1)
    original = dict(result1.candidates[0].row)
    assert original["card_id"] == PER_CARD_ID
    assert str(original["card_assignment_ref"]) == str(row1)

    # Reschedule: a NEWER cache row for the SAME fiscal event, moving the
    # report_date far into the future -- superseding row1 as current belief.
    future_date = (ANCHOR_DAY + timedelta(days=60)).isoformat()
    _insert_earnings_row(
        journal, symbol, future_date, timing="pre-market",
        fiscal_date_ending="2026-fiscal-Q1",
        created_at_utc=f"{ANCHOR_DAY.isoformat()}T23:59:00+00:00",
    )

    # Run more scans (a later context, a later day) -- the ORIGINAL row
    # must never be updated. A fresh pull run keeps cache_health 'ok' at
    # this later as-of instant (the first pull run is now >48h stale).
    later_as_of = f"{(ANCHOR_DAY + timedelta(days=1)).isoformat()}T20:00:00+00:00"
    _insert_healthy_pull_run(journal, f"{(ANCHOR_DAY + timedelta(days=1)).isoformat()}T01:00:00+00:00")
    activation2 = build_scan_card_activation(journal, later_as_of, DEFAULT_UNIVERSE)
    scanner2 = _scanner_with_forced_momentum(journal, monkeypatch)
    scanner2.scan(symbols=[symbol], scan_batch_id="batch2", card_activation=activation2)

    reread = journal.one(
        "SELECT card_id, card_version, card_assignment_ref, card_assignment_status, "
        "card_selector_version FROM candidates WHERE candidate_id = ?",
        (original["candidate_id"],),
    )
    assert reread["card_id"] == original["card_id"]
    assert reread["card_assignment_ref"] == original["card_assignment_ref"] or \
        str(reread["card_assignment_ref"]) == str(original["card_assignment_ref"])
    assert reread["card_assignment_status"] == original["card_assignment_status"]


def test_no_update_statement_ever_targets_candidates_card_columns():
    """Structural guard, supplemental to the runtime proof above: neither
    candidate_scanner.py nor orchestrator.py may contain an UPDATE
    statement whose SET clause touches any card_* column -- assignment
    happens ONLY at INSERT time. Scoped to the SET clause specifically
    (not "no UPDATE candidates at all") since orchestrator.py legitimately
    has an unrelated `UPDATE candidates SET news_status = ...` -- this
    guard's job is only the 5 card columns, not every column ever."""
    import re

    card_columns = (
        "card_id", "card_version", "card_assignment_status", "card_assignment_ref", "card_selector_version",
    )
    for mod in (candidate_scanner_mod, orchestrator_mod):
        text = pathlib.Path(str(mod.__file__)).read_text(encoding="utf-8")
        for match in re.finditer(r'UPDATE\s+candidates\s+SET\s+([^"]*)', text):
            set_clause = match.group(1)
            for col in card_columns:
                assert col not in set_clause, (
                    f"{mod.__file__} contains an UPDATE candidates statement whose SET clause "
                    f"touches {col!r}: {set_clause!r}"
                )


# ============================================================ Section 5/9:
# core + shadow coverage
def test_core_and_shadow_share_identical_selector_version_and_context():
    _the_same_activation = build_scan_card_activation
    # Structural: scan() and scan_shadow_tier() both accept card_activation
    # and both route it into the SAME _resolve_card_assignment/select_card
    # call -- verified behaviorally by the core/shadow assignment tests
    # above using the identical activation object; this test only pins
    # that no separate PER_CARD_ID/SELECTOR_VERSION constant exists for
    # either tier.
    assert PER_CARD_ID == "post_earnings_reaction"
    assert SELECTOR_VERSION == "card_selector_v1"


# ============================================================ Section 7/9:
# decision-path isolation
def test_baseline_vs_s1c_decisions_identical_apart_from_card_metadata(journal, monkeypatch):
    """Two scans, identical input snapshot, one with NO activation (the
    pre-S1c baseline shape: card_activation=None) and one with a healthy
    but non-eligible activation (post-S1c, real code path, default card
    assigned via the selector) -- every field outside the 5 card columns
    must be identical."""
    symbol = DEFAULT_UNIVERSE[12]
    _register_valid_v2_pair(journal)

    scanner_a = _scanner_with_forced_momentum(journal, monkeypatch)
    result_a = scanner_a.scan(symbols=[symbol], scan_batch_id="baseline", card_activation=None)

    journal2 = JournalStore(":memory:")
    _register_valid_v2_pair(journal2)
    scanner_b = CandidateScanner(make_settings(), journal2)
    _force_momentum(monkeypatch, scanner_b.market)
    activation_b = build_scan_card_activation(journal2, ANCHOR_AS_OF_UTC, DEFAULT_UNIVERSE)
    result_b = scanner_b.scan(symbols=[symbol], scan_batch_id="s1c", card_activation=activation_b)

    a, b = dict(result_a.candidates[0].row), dict(result_b.candidates[0].row)
    # card_* columns are the ONLY decision-relevant fields S1c may change.
    # The rest are non-decision identity/lineage fields that legitimately
    # differ between two independent scans (fresh ids, fresh snapshot rows)
    # even with byte-identical mock input -- excluded from the comparison
    # for that reason, not because they're card-related.
    excluded_fields = {
        "card_id", "card_version", "card_assignment_status", "card_assignment_ref",
        "card_selector_version", "candidate_id", "scan_id", "scan_batch_id", "lineage_id",
        "price_snapshot_id",
    }
    for key in a:
        if key in excluded_fields:
            continue
        assert a[key] == b[key], f"non-card field {key!r} diverged between baseline and S1c: {a[key]!r} vs {b[key]!r}"
    # And the default card identity itself (id/version) matches -- only
    # the S1c-only diagnostic fields differ (None vs a stamped status).
    assert a["card_id"] == b["card_id"]
    assert a["card_version"] == b["card_version"]
    journal2.close()


def test_evaluate_two_arm_hypothesis_pair_never_invoked_by_a_production_scan(orchestrator, monkeypatch):
    """A spy on the dangerous evaluation entrypoint -- if a production
    scan ever called it (it must not), this test fails loudly rather
    than relying only on a static grep."""
    called = []
    import alphaos.stats.preregistration as prereg_mod
    original = prereg_mod.evaluate_two_arm_hypothesis_pair

    def _spy(*args, **kwargs):
        called.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(prereg_mod, "evaluate_two_arm_hypothesis_pair", _spy)
    orchestrator.run_scan_once()
    assert called == []


def test_no_evidence_snapshot_created_by_a_production_scan(orchestrator):
    orchestrator.run_scan_once()
    assert orchestrator.journal.count_rows("per_evidence_snapshots") == 0


# ============================================================ isolation
# (replaces the 3 now-stale negative assertions in
# tests/test_s1b_registration_integration.py -- see that file's own
# updated comments for why they had to change)
PRODUCTION_FILES = [
    (orchestrator_mod, "orchestrator.py"),
    (candidate_scanner_mod, "candidate_scanner.py"),
]


@pytest.mark.parametrize("mod,name", PRODUCTION_FILES)
def test_selector_and_per_evidence_now_legitimately_referenced(mod, name):
    text = pathlib.Path(str(mod.__file__)).read_text(encoding="utf-8")
    assert "cards.activation" in text, f"{name} must import alphaos.cards.activation (S1c wiring)"


@pytest.mark.parametrize("mod,name", PRODUCTION_FILES)
def test_formal_evaluation_module_still_never_referenced(mod, name):
    """The ONE boundary S1c must NOT move: alphaos.stats.preregistration
    (home of evaluate_two_arm_hypothesis_pair, the dangerous one-shot
    formal-evaluation entrypoint) must still never be imported by
    production code, even though cards.selector/cards.per_evidence now
    legitimately are."""
    text = pathlib.Path(str(mod.__file__)).read_text(encoding="utf-8")
    for forbidden in ("stats.preregistration", "evaluate_two_arm_hypothesis_pair", "two_arm"):
        assert forbidden not in text, f"{name} references {forbidden!r} -- formal evaluation must stay unwired"


def test_production_import_graph_now_loads_selector_and_per_evidence():
    """The real, load-bearing guarantee (subprocess, not substring grep):
    importing the full production stack now legitimately loads
    alphaos.cards.selector/alphaos.cards.per_evidence/alphaos.cards.
    activation -- S1c's own point. (alphaos.stats.preregistration is
    NOT part of this proof: it was ALREADY part of the production import
    graph before S1c, via alphaos.hypotheses.resolver's own use of the
    one-arm register_hypothesis()/evaluate_hypothesis() for every OTHER
    hypothesis family -- the S1b isolation suite's own forbidden-module
    list never included it. The real remaining boundary --
    evaluate_two_arm_hypothesis_pair() is never CALLED and no evidence
    snapshot is ever created by a scan -- is proven behaviorally above by
    test_evaluate_two_arm_hypothesis_pair_never_invoked_by_a_production_scan
    and test_no_evidence_snapshot_created_by_a_production_scan.)"""
    script = (
        "import sys\n"
        "import alphaos.hypotheses.resolver\n"
        "import alphaos.orchestrator\n"
        "import alphaos.scanner.candidate_scanner\n"
        "import alphaos.cards.registry\n"
        "loaded = [m for m in "
        "('alphaos.cards.selector', 'alphaos.cards.per_evidence', 'alphaos.cards.activation') "
        "if m in sys.modules]\n"
        "print(','.join(loaded))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    loaded = [m for m in result.stdout.strip().split(",") if m]
    assert set(loaded) == {"alphaos.cards.selector", "alphaos.cards.per_evidence", "alphaos.cards.activation"}, (
        f"expected S1c's own modules to load via the production stack, got {loaded}"
    )


def test_selector_never_referenced_by_risk_approval_sizing_execution_paths():
    """Extends S1c's own isolation to the DANGEROUS files specifically --
    even though the selector is now wired into scanning, it must never
    reach risk/approval/sizing/broker/execution code."""
    import alphaos.approval as approval_mod
    import alphaos.execution.order_manager as order_manager_mod
    import alphaos.execution.position_manager as position_manager_mod
    import alphaos.risk.risk_engine as risk_engine_mod

    for mod, name in (
        (approval_mod, "approval.py"), (risk_engine_mod, "risk_engine.py"),
        (order_manager_mod, "order_manager.py"), (position_manager_mod, "position_manager.py"),
    ):
        text = pathlib.Path(str(mod.__file__)).read_text(encoding="utf-8")
        for forbidden in ("cards.selector", "select_card", "cards.activation", "cards.per_evidence"):
            assert forbidden not in text, f"{name} references {forbidden!r} -- S1c must never reach this path"


def test_production_scan_can_produce_a_per_assignment_when_eligible_never_otherwise(orchestrator):
    """Replaces test_s1b_registration_integration.py's old
    test_production_scan_produces_zero_per_assignments: without a
    corrected pair registered (the orchestrator fixture's default state),
    a real scan must still produce ZERO PER assignments -- this remains
    true post-S1c, just for a DIFFERENT reason now (preflight fails, not
    "the selector is unwired")."""
    orchestrator.run_scan_once()
    total = orchestrator.journal.scalar("SELECT COUNT(*) FROM candidates")
    assert total > 0, "scan produced no candidates at all -- this test can no longer prove anything"
    per_count = orchestrator.journal.scalar(
        "SELECT COUNT(*) FROM candidates WHERE card_id = ?", (PER_CARD_ID,),
    )
    assert per_count == 0


# ============================================================ observability
def test_per_selector_report_reconciles_with_candidate_rows(journal, monkeypatch):
    from alphaos.cards.selector_health import build_per_selector_report

    symbol_per = DEFAULT_UNIVERSE[13]
    symbol_default = DEFAULT_UNIVERSE[14]
    _insert_earnings_row(
        journal, symbol_per, ANCHOR_DAY.isoformat(), timing="pre-market",
        created_at_utc=f"{(ANCHOR_DAY - timedelta(days=1)).isoformat()}T00:00:00+00:00",
    )
    _insert_healthy_pull_run(journal, f"{(ANCHOR_DAY - timedelta(days=1)).isoformat()}T01:00:00+00:00")
    _register_valid_v2_pair(journal)
    activation = build_scan_card_activation(journal, ANCHOR_AS_OF_UTC, DEFAULT_UNIVERSE)
    scanner = _scanner_with_forced_momentum(journal, monkeypatch)
    scanner.scan(symbols=[symbol_per, symbol_default], scan_batch_id="batch1", card_activation=activation)

    since_utc = "2000-01-01T00:00:00+00:00"
    rep = build_per_selector_report(journal, since_utc)
    assert rep["per_assignments"] == 1
    assert rep["core_per_assignments"] == 1
    assert rep["shadow_per_assignments"] == 0
    assert rep["default_healthy"] == 1
    assert rep["n_total"] == 2


def test_zero_per_count_is_not_an_error_and_report_is_omitted_when_truly_empty(journal):
    from alphaos.cards.selector_health import build_per_selector_report

    rep = build_per_selector_report(journal, "2000-01-01T00:00:00+00:00")
    assert rep["per_assignments"] == 0
    assert rep["n_total"] == 0  # no candidates stamped at all yet -- daily_brief's
    # own _per_selector_health() omits the section in exactly this case (see
    # tests/test_daily_brief.py-style "omit, don't fabricate" convention);
    # zero PER count alone (n_total>0 but per_assignments==0) is NOT this case
    # and must still show, per the operator's own "low/zero PER outside
    # earnings season is normal" requirement.


def test_render_markdown_never_alerts_on_a_healthy_zero_per_count():
    from alphaos.cards.selector_health import render_markdown

    rep = {
        "since_utc": "x", "per_assignments": 0, "core_per_assignments": 0, "shadow_per_assignments": 0,
        "default_healthy": 5, "degraded_by_status": {s: 0 for s in (
            CacheHealth.REFRESH_FAILED_RECENT, CacheHealth.STALE, CacheHealth.CACHE_EMPTY,
            CacheHealth.UNKNOWN, PREFLIGHT_FAILED_STATUS, ACTIVATION_ERROR_STATUS,
        )}, "per_by_scan_window": {}, "unstamped": 0, "n_total": 5,
    }
    md = render_markdown(rep)
    assert "⚠️" not in md
    assert "PER-tagged today: **0**" in md
