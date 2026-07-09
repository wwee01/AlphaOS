"""PR10 Setup Cards v1: the versioned join key for the whole learning loop.

Covers: card loading/validation, idempotent registry sync, the mutated-
content-refuses-to-start invariant, 100%-coverage stamping of every new
candidate/proposal, the exit-first invariant enforced at ``_execute()``
(both a missing-invalidation-reason and a missing-target variant), a
migration test (old DB gains the table + columns), and behavior neutrality
(card stamping changes nothing else about a decision).
"""

from __future__ import annotations

import os
import textwrap

import pytest

from alphaos.cards import registry as cards
from alphaos.config.settings import SettingsError
from alphaos.constants import PLAYBOOK_V1, ReasonCode
from alphaos.journal.journal_store import JournalStore
from conftest import inject_pending_proposal, make_proposal


def _write_card(directory, filename, card_id="test_card", version=1,
                 invalidation_rule="original rule", state="shadow", name="Test"):
    path = os.path.join(directory, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(f"""\
            card_id: {card_id}
            version: {version}
            name: {name}
            state: {state}
            invalidation_rule: {invalidation_rule}
        """))
    return path


# ------------------------------------------------------------ load/validate
def test_load_card_files_parses_the_real_default_card():
    """The actual shipped card, not a fixture -- confirms the real YAML file
    is well-formed and matches the spec's required shape."""
    loaded = cards.load_card_files()
    ids = {c["card_id"] for c in loaded}
    assert cards.DEFAULT_CARD_ID in ids
    default = next(c for c in loaded if c["card_id"] == cards.DEFAULT_CARD_ID)
    assert default["version"] == 1
    assert default["state"] == "live_eligible"
    assert default["invalidation_rule"]


def test_load_card_files_raises_on_missing_required_field(tmp_path):
    _write_card(tmp_path, "bad.yaml", invalidation_rule="")  # empty -> falsy -> missing
    with pytest.raises(SettingsError, match="missing required field"):
        cards.load_card_files(tmp_path)


def test_load_card_files_raises_on_invalid_version(tmp_path):
    # -1 (not 0) so it's truthy -- exercises the dedicated version-range check,
    # not the "missing field" falsy check (0 would be caught by that one first).
    path = tmp_path / "bad.yaml"
    path.write_text("card_id: x\nversion: -1\nname: X\nstate: shadow\ninvalidation_rule: r\n")
    with pytest.raises(SettingsError, match="invalid version"):
        cards.load_card_files(tmp_path)


def test_get_default_card_returns_whichever_card_id_is_current():
    card = cards.get_default_card()
    assert card["card_id"] == cards.DEFAULT_CARD_ID


def test_get_default_card_raises_if_not_found(tmp_path):
    _write_card(tmp_path, "other.yaml", card_id="not_the_default")
    with pytest.raises(SettingsError, match="not found"):
        cards.get_default_card(tmp_path)


# --------------------------------------------------------------- registry sync
def test_sync_registry_inserts_new_card_and_is_idempotent(journal, settings, tmp_path):
    _write_card(tmp_path, "test_card.yaml")

    first = cards.sync_registry(journal, settings, cards_dir=tmp_path)
    second = cards.sync_registry(journal, settings, cards_dir=tmp_path)

    assert first == ["test_card:v1"]
    assert second == []  # idempotent -- no dupes on a second sync
    assert journal.count_rows("setup_cards") == 1


def test_sync_registry_refuses_on_mutated_content_same_version(journal, settings, tmp_path):
    _write_card(tmp_path, "test_card.yaml", invalidation_rule="original rule")
    cards.sync_registry(journal, settings, cards_dir=tmp_path)

    _write_card(tmp_path, "test_card.yaml", invalidation_rule="MUTATED rule")  # same v1, different content
    with pytest.raises(SettingsError, match="content changed without a version bump"):
        cards.sync_registry(journal, settings, cards_dir=tmp_path)

    assert journal.count_rows("setup_cards") == 1  # the bad sync never got to insert anything


def test_sync_registry_allows_a_version_bump_with_different_content(journal, settings, tmp_path):
    _write_card(tmp_path, "test_card.yaml", version=1, invalidation_rule="v1 rule")
    cards.sync_registry(journal, settings, cards_dir=tmp_path)

    _write_card(tmp_path, "test_card.yaml", version=2, invalidation_rule="v2 rule, changed")
    synced = cards.sync_registry(journal, settings, cards_dir=tmp_path)

    assert synced == ["test_card:v2"]
    assert journal.count_rows("setup_cards") == 2  # both versions coexist, append-only


def test_sync_registry_persists_content_as_json(journal, settings, tmp_path):
    _write_card(tmp_path, "test_card.yaml", name="My Card")
    cards.sync_registry(journal, settings, cards_dir=tmp_path)

    row = journal.one("SELECT * FROM setup_cards WHERE card_id = 'test_card'")
    assert row["name"] == "My Card"
    assert row["state"] == "shadow"
    assert "invalidation_rule" in row["content_json"]  # stored as JSON text


# ------------------------------------------------------------- startup wiring
def test_orchestrator_startup_syncs_the_real_default_card(orchestrator):
    orchestrator.startup()
    row = orchestrator.journal.one(
        "SELECT * FROM setup_cards WHERE card_id = ?", (cards.DEFAULT_CARD_ID,)
    )
    assert row is not None
    assert row["version"] == 1


def test_orchestrator_startup_is_safe_to_call_twice(orchestrator):
    orchestrator.startup()
    orchestrator.startup()  # must not raise, must not duplicate
    assert orchestrator.journal.count_rows(
        "setup_cards", "card_id = ?", (cards.DEFAULT_CARD_ID,)
    ) == 1


# ------------------------------------------------------- 100% stamping coverage
def test_every_scan_candidate_is_stamped_with_a_card(orchestrator):
    orchestrator.run_scan_once()
    rows = orchestrator.journal.query("SELECT card_id, card_version FROM candidates")
    assert rows  # non-vacuity guard: a scan producing nothing would make this test meaningless
    for row in rows:
        assert row["card_id"] == cards.DEFAULT_CARD_ID
        assert row["card_version"] == 1


def test_every_proposal_pending_and_risk_blocked_is_stamped_with_a_card(orchestrator):
    orchestrator.run_scan_once()
    rows = orchestrator.journal.query(
        "SELECT card_id, card_version, invalidation_reason, status FROM trade_proposals"
    )
    assert rows
    assert any(r["status"] == "pending_approval" for r in rows)
    assert any(r["status"] == "blocked" for r in rows)  # risk-blocked path also covered
    for row in rows:
        assert row["card_id"] == cards.DEFAULT_CARD_ID
        assert row["card_version"] == 1
        assert row["invalidation_reason"]  # non-empty on every single row, both branches


def test_override_created_proposal_is_stamped_with_the_same_card(orchestrator):
    """The override path (_override_open_trade) stamps setup_classification=
    'user_override' but the SAME default card -- per spec §10.3."""
    symbol = "AAPL"
    snap = orchestrator.market.get_snapshot(symbol)
    cand_id = "cand_override_test"
    orchestrator.journal.insert("candidates", {
        "candidate_id": cand_id, "symbol": symbol, "direction": "long", "strategy": "swing",
        "status": "watch",
    })
    orchestrator.journal.insert("openai_evaluations", {
        "eval_id": "ev_override_test", "candidate_id": cand_id, "symbol": symbol, "model": "mock",
        "direction": "long", "entry": float(snap["last_price"]), "stop": float(snap["last_price"]) * 0.97,
        "target": float(snap["last_price"]) * 1.06, "max_holding_days": 3, "expected_r": 2.0,
        "confidence": 0.8, "decision": "watch", "reasoning_summary": "test", "is_mock": 1,
    })
    cand = {"symbol": symbol, "candidate_id": cand_id}
    rec = {"user_override_action": "watch_to_trade", "arming_classification": None}
    ev_row = orchestrator.journal.one(
        "SELECT * FROM openai_evaluations WHERE eval_id = 'ev_override_test'"
    )
    msg = orchestrator._override_open_trade(cand, ev_row, rec, approver="tester")
    assert "PENDING_APPROVAL" in msg, msg

    row = orchestrator.journal.one(
        "SELECT * FROM trade_proposals WHERE proposal_id = ?", (rec["proposal_id"],)
    )
    assert row["card_id"] == cards.DEFAULT_CARD_ID
    assert row["card_version"] == 1
    assert row["invalidation_reason"]
    assert row["setup_classification"] == "user_override"  # unchanged by PR10, per spec


# ------------------------------------------------------- exit-first invariant
def test_execute_blocks_a_proposal_missing_invalidation_reason(orchestrator):
    pid, _ = inject_pending_proposal(orchestrator)
    # inject_pending_proposal defaults to a fully-stamped proposal (conftest's
    # make_proposal with_card=True) -- explicitly strip it back to simulate a
    # legacy pre-PR10 row reaching approval.
    orchestrator.journal.conn.execute(
        "UPDATE trade_proposals SET invalidation_reason = NULL WHERE proposal_id = ?", (pid,)
    )
    orchestrator.journal.conn.commit()

    ok, msg = orchestrator.approve_proposal(pid, approver="tester")

    assert not ok
    assert ReasonCode.EXIT_PLAN_INCOMPLETE.value in msg
    assert orchestrator.journal.proposal_by_id(pid)["status"] != "filled"
    assert orchestrator.journal.count_rows("paper_orders") == 0


def test_execute_blocks_a_proposal_missing_target(orchestrator):
    pid, _ = inject_pending_proposal(orchestrator)
    orchestrator.journal.conn.execute(
        "UPDATE trade_proposals SET target = NULL WHERE proposal_id = ?", (pid,)
    )
    orchestrator.journal.conn.commit()

    ok, msg = orchestrator.approve_proposal(pid, approver="tester")

    assert not ok
    assert ReasonCode.EXIT_PLAN_INCOMPLETE.value in msg
    assert orchestrator.journal.count_rows("paper_orders") == 0


def test_execute_allows_a_fully_stamped_proposal_through(orchestrator):
    """The positive case, paired with the two blocking tests above: a proposal
    with every exit-plan field present must execute normally -- this PR must
    not have tightened anything beyond the specifically-named 5 fields."""
    pid, _ = inject_pending_proposal(orchestrator)

    ok, msg = orchestrator.approve_proposal(pid, approver="tester")

    assert ok, msg
    assert orchestrator.journal.proposal_by_id(pid)["status"] == "filled"


def test_direct_execute_call_reports_all_missing_fields_at_once(orchestrator):
    """A proposal built with with_card=False (the deliberately-legacy shape)
    should report every missing field in one shot, not just the first."""
    from datetime import timedelta

    from alphaos.util import timeutils

    proposal = make_proposal(with_card=False)
    proposal.target = None
    # Give it a valid, non-expired TTL -- is_expired(None) treats a missing
    # timestamp as expired (fail-safe), which would short-circuit before ever
    # reaching the exit-plan check this test is actually exercising.
    proposal.proposal_expires_at_utc = timeutils.to_iso(timeutils.now_utc() + timedelta(minutes=30))

    result = orchestrator._execute(proposal)

    assert result.blocked is True
    assert result.block_reason == ReasonCode.EXIT_PLAN_INCOMPLETE.value
    assert "invalidation_reason" in result.detail
    assert "target" in result.detail


# ------------------------------------------------------------- migration test
def test_setup_cards_table_and_new_columns_added_to_a_pre_pr10_db(tmp_path):
    """An old ledger written before PR10 (no setup_cards table, no card_id/
    card_version/invalidation_reason columns) must gain all of it additively
    on open -- SCHEMA_VERSION stays 3 (purely additive), matching every other
    post-hoc addition in this codebase's history."""
    import sqlite3

    db = str(tmp_path / "pre_pr10.db")
    raw = sqlite3.connect(db)
    raw.execute(
        "CREATE TABLE candidates (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "candidate_id TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL, "
        "created_at_utc TEXT NOT NULL, created_at_sgt TEXT NOT NULL)"
    )
    raw.execute(
        "CREATE TABLE trade_proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "proposal_id TEXT NOT NULL UNIQUE, candidate_id TEXT NOT NULL, symbol TEXT NOT NULL, "
        "created_at_utc TEXT NOT NULL, created_at_sgt TEXT NOT NULL)"
    )
    raw.execute(
        "CREATE TABLE system_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_id TEXT NOT NULL UNIQUE, severity TEXT NOT NULL, category TEXT NOT NULL, "
        "message TEXT NOT NULL, detail_json TEXT, created_at_utc TEXT NOT NULL, "
        "created_at_sgt TEXT NOT NULL, created_at_market TEXT)"
    )
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    from alphaos.journal.schema import SCHEMA_VERSION

    j = JournalStore(db)
    try:
        tables = {r["name"] for r in j.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        assert "setup_cards" in tables
        assert j.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert SCHEMA_VERSION == 3

        for table in ("candidates", "trade_proposals"):
            cols = {r["name"] for r in j.conn.execute(f"PRAGMA table_info({table})")}
            assert "card_id" in cols
            assert "card_version" in cols
        proposal_cols = {r["name"] for r in j.conn.execute("PRAGMA table_info(trade_proposals)")}
        assert "invalidation_reason" in proposal_cols

        # And immediately usable, not just present.
        j.insert("setup_cards", {
            "card_id": "x", "version": 1, "state": "shadow", "content_hash": "abc",
        })
        assert j.count_rows("setup_cards") == 1
    finally:
        j.close()


def test_setup_cards_unique_index_enforces_one_row_per_card_and_version(journal):
    journal.insert("setup_cards", {
        "card_id": "x", "version": 1, "state": "shadow", "content_hash": "hash1",
    })
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        journal.insert("setup_cards", {
            "card_id": "x", "version": 1, "state": "shadow", "content_hash": "hash2",
        })
    # A different version for the same card_id is fine (append-only).
    journal.insert("setup_cards", {
        "card_id": "x", "version": 2, "state": "shadow", "content_hash": "hash2",
    })
    assert journal.count_rows("setup_cards") == 2


# --------------------------------------------------------- behavior neutrality
def test_card_stamping_changes_nothing_else_about_a_decision(orchestrator):
    """Behavior-neutrality proof (house pattern §H.6): every pre-existing
    decision-relevant field is exactly what swing_strategy/risk_engine always
    produced -- card_id/card_version/invalidation_reason are purely additive,
    never altering entry/stop/target/qty/status/playbook_name/setup_classification."""
    orchestrator.run_scan_once()
    rows = orchestrator.journal.query("SELECT * FROM trade_proposals")
    assert rows  # non-vacuity guard
    for row in rows:
        assert row["entry"] is not None and row["entry"] > 0
        assert row["stop"] is not None and row["stop"] > 0
        assert row["target"] is not None
        assert row["qty"] is not None and row["qty"] > 0
        assert row["playbook_name"] == PLAYBOOK_V1
        assert row["setup_classification"] == "momentum_continuation"
        assert row["status"] in ("pending_approval", "blocked")


def test_benchmark_capture_and_relative_performance_modules_never_reference_cards():
    """No-read grep, mirroring PR9.5's own isolation proof: the measurement-only
    shadow layers must never join on or read setup-card data (no reason they
    would need to; this just confirms no accidental coupling crept in)."""
    import pathlib

    import alphaos.reports.benchmark_capture as bc_mod
    import alphaos.reports.relative_performance as rp_mod

    for mod, name in ((bc_mod, "benchmark_capture.py"), (rp_mod, "relative_performance.py")):
        text = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert "setup_cards" not in text and "card_id" not in text, \
            f"{name} unexpectedly references setup cards"


# ------------------------------------------------------------------- by_card
def test_attribution_by_card_slice_present_and_floor_gated(journal, settings):
    from datetime import date, timedelta

    from alphaos.reports.attribution import (
        MIN_RESOLVED_FOR_V2_SUBSLICE,
        MIN_SPAN_DAYS_FOR_V2_AGGREGATE,
        compute_attribution_v2,
    )

    # Below the floor: a handful of resolved rows for one card -- must show
    # below_sample_floor, never a mean/sum computed on a too-small sample.
    few = [
        {"attribution_type": "propose_blocked", "agent": "alphaos", "resolved_status": "resolved",
         "delta_r": 1.0, "is_mock": 0, "card_id": "catalyst_momentum_v1"}
        for _ in range(MIN_RESOLVED_FOR_V2_SUBSLICE - 1)
    ]
    rep = compute_attribution_v2(few)
    assert rep["aggregate_delta_r_by_card"]["catalyst_momentum_v1"]["status"] == "below_sample_floor"

    # At/above the floor: real mean/sum now appear. _floor_gated_v2_aggregate
    # enforces MIN_SPAN_DAYS_FOR_V2_AGGREGATE unconditionally (regardless of
    # which min_resolved override is passed), so the count floor alone isn't
    # enough -- the resolved_at_utc spread must ALSO clear the span floor.
    base = date(2026, 1, 1)
    # Each row gets its OWN symbol (PORT-1) so all MIN_RESOLVED_FOR_V2_SUBSLICE
    # rows also land in that many separate effective_n clusters -- a real
    # independent-observation fixture, not just N rows on one symbol.
    plenty = [
        {"attribution_type": "propose_blocked", "agent": "alphaos", "resolved_status": "resolved",
         "delta_r": 1.0, "is_mock": 0, "card_id": "catalyst_momentum_v1",
         "resolved_at_utc": (base + timedelta(days=i * 2)).isoformat() + "T00:00:00+00:00",
         "symbol": f"SYM{i:02d}",
         "decision_at_utc": (base + timedelta(days=i * 2)).isoformat() + "T00:00:00+00:00"}
        for i in range(MIN_RESOLVED_FOR_V2_SUBSLICE)
    ]
    assert (MIN_RESOLVED_FOR_V2_SUBSLICE - 1) * 2 >= MIN_SPAN_DAYS_FOR_V2_AGGREGATE  # sanity on the fixture itself
    rep2 = compute_attribution_v2(plenty)
    slice_ = rep2["aggregate_delta_r_by_card"]["catalyst_momentum_v1"]
    assert slice_["status"] == "ok"
    assert slice_["mean_delta_r"] == 1.0
    assert slice_["resolved_count"] == MIN_RESOLVED_FOR_V2_SUBSLICE
    assert slice_["effective_n"] == MIN_RESOLVED_FOR_V2_SUBSLICE


def test_attribution_by_card_excludes_mock_and_unresolved_rows(journal, settings):
    from alphaos.reports.attribution import compute_attribution_v2

    rows = [
        {"attribution_type": "propose_blocked", "agent": "alphaos", "resolved_status": "resolved",
         "delta_r": 1.0, "is_mock": 1, "card_id": "catalyst_momentum_v1"},  # mock -> excluded
        {"attribution_type": "propose_blocked", "agent": "alphaos", "resolved_status": "pending",
         "delta_r": 1.0, "is_mock": 0, "card_id": "catalyst_momentum_v1"},  # unresolved -> excluded
        {"attribution_type": "propose_blocked", "agent": "alphaos", "resolved_status": "resolved",
         "delta_r": 1.0, "is_mock": 0, "card_id": None},  # no card -> not sliced at all
    ]
    rep = compute_attribution_v2(rows)
    assert rep["aggregate_delta_r_by_card"] == {}


def test_digest_tqs_shadow_includes_bucket_histogram_by_card(orchestrator):
    from alphaos.scheduler.digest import build_daily_digest

    orchestrator.run_scan_once()
    digest = build_daily_digest(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)

    by_card = digest["tqs_shadow"]["bucket_histogram_by_card_today"]
    assert cards.DEFAULT_CARD_ID in by_card
    assert sum(by_card[cards.DEFAULT_CARD_ID].values()) > 0
