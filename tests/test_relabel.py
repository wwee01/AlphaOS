"""TASK-R: retro-relabel of the contaminated 2026-07-01 baseline (§H.1
direct construction throughout -- no live network). Covers:
* date-range packet selection (inclusive boundaries, excludes outside range),
* --dry-run makes ZERO network calls (client never constructed) and
  persists nothing,
* a live run persists NEW candidate_labels rows via the real
  PlaybookClassifier, relabel_of set to the most recent original label
  (or NULL if none existed), originals byte-identical before/after,
* one system_event per relabelled packet,
* the live cost cap is respected,
* the shared packet-reconstruction helper this shares with EVAL-1.

All offline, in-memory. No real money, no network.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.relabel import relabel_candidates
from alphaos.util.ids import new_id
from conftest import make_settings


@pytest.fixture
def journal():
    store = JournalStore(":memory:")
    yield store
    store.close()


def _seed_packet(journal, symbol="AAPL", sgt_date="2026-07-01", with_label=True,
                 primary_label="Momentum", label_decision="propose"):
    packet_id, candidate_id = new_id("pkt"), new_id("cand")
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
    ts = f"{sgt_date}T12:00:00+08:00"
    journal.conn.execute(
        "INSERT INTO candidate_packets (packet_id, candidate_id, symbol, scan_batch_id, "
        "packet_json, created_at_utc, created_at_sgt) VALUES (?, ?, ?, 'batch_test', ?, ?, ?)",
        (packet_id, candidate_id, symbol, json.dumps(packet_json), ts, ts),
    )
    if with_label:
        journal.conn.execute(
            "INSERT INTO candidate_labels (label_id, candidate_id, packet_id, symbol, "
            "primary_label, label_decision, is_mock, created_at_utc, created_at_sgt) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (new_id("lbl"), candidate_id, packet_id, symbol, primary_label, label_decision, ts, ts),
        )
    journal.conn.commit()
    return packet_id, candidate_id


# ==================================================================== range
def test_packets_in_range_includes_boundaries_excludes_outside(journal):
    _seed_packet(journal, symbol="IN_START", sgt_date="2026-07-01")
    _seed_packet(journal, symbol="IN_END", sgt_date="2026-07-03")
    _seed_packet(journal, symbol="BEFORE", sgt_date="2026-06-30")
    _seed_packet(journal, symbol="AFTER", sgt_date="2026-07-04")

    settings = make_settings()
    result = relabel_candidates(journal, settings, "2026-07-01", "2026-07-03", dry_run=True)

    symbols = {p["symbol"] for p in result["prompts"]}
    assert symbols == {"IN_START", "IN_END"}
    assert result["n_packets"] == 2


def test_empty_range_is_a_safe_noop(journal):
    settings = make_settings()
    result = relabel_candidates(journal, settings, "2099-01-01", "2099-01-01", dry_run=True)

    assert "error" not in result
    assert result["n_packets"] == 0
    assert result["prompts"] == []


def test_latest_label_for_packet_picks_the_most_recent_of_several(journal):
    """Regression guard (audit-flagged coverage gap): a packet with MULTIPLE
    prior labels must relabel against the most recently inserted one, not
    an arbitrary/earliest one."""
    from alphaos.relabel import _latest_label_for_packet
    from alphaos.util.ids import new_id

    packet_id, candidate_id = new_id("pkt"), new_id("cand")
    label_ids = []
    for i, label in enumerate(["Other/Unclassified", "Breakout", "Momentum"]):
        lbl_id = new_id("lbl")
        label_ids.append(lbl_id)
        journal.conn.execute(
            "INSERT INTO candidate_labels (label_id, candidate_id, packet_id, symbol, "
            "primary_label, is_mock, created_at_utc, created_at_sgt) "
            "VALUES (?, ?, ?, 'AAPL', ?, 0, ?, ?)",
            (lbl_id, candidate_id, packet_id, label, f"2026-07-0{i+1}T00:00:00+00:00",
             f"2026-07-0{i+1}T08:00:00+08:00"),
        )
    journal.conn.commit()

    latest = _latest_label_for_packet(journal, packet_id)

    assert latest["label_id"] == label_ids[-1]  # the LAST-inserted row (Momentum), not the first
    assert latest["primary_label"] == "Momentum"


def test_live_run_relabels_against_the_most_recent_of_several_originals(journal):
    """End-to-end version of the above, through the real relabel_of wiring."""
    from alphaos.util.ids import new_id

    packet_id, candidate_id = _seed_packet(journal, symbol="AAPL", primary_label="Other/Unclassified")
    for label in ["Breakout", "Momentum"]:
        journal.conn.execute(
            "INSERT INTO candidate_labels (label_id, candidate_id, packet_id, symbol, "
            "primary_label, is_mock, created_at_utc, created_at_sgt) "
            "VALUES (?, ?, ?, 'AAPL', ?, 0, ?, ?)",
            (new_id("lbl"), candidate_id, packet_id, label,
             "2026-07-01T00:00:00+00:00", "2026-07-01T08:00:00+08:00"),
        )
    journal.conn.commit()
    most_recent = journal.one(
        "SELECT * FROM candidate_labels WHERE packet_id = ? ORDER BY id DESC LIMIT 1", (packet_id,),
    )
    settings = make_settings()

    result = relabel_candidates(journal, settings, "2026-07-01", "2026-07-01", dry_run=False)

    assert result["n_relabelled"] == 1
    new_row = journal.one("SELECT * FROM candidate_labels WHERE relabel_of IS NOT NULL")
    assert new_row["relabel_of"] == most_recent["label_id"]


# ============================================================ isolation
def test_relabel_isolates_a_malformed_packet_json_and_processes_the_rest(journal):
    """Regression guard (audit-flagged MEDIUM-severity-shaped gap, LOW in
    practice since production packet_json is always complete): a row with a
    truncated/malformed packet_json must count as one n_corpus_errors entry
    and let the run continue to the remaining packets -- never abort the
    whole run and silently lose already-committed results' siblings,
    contradicting this function's own "Never raises" docstring. Mirrors
    EVAL-1's harness, which shares this exact reconstruction helper."""
    _seed_packet(journal, symbol="GOOD1")
    packet_id_bad, candidate_id_bad = new_id("pkt"), new_id("cand")
    ts = "2026-07-01T12:00:00+08:00"
    journal.conn.execute(
        "INSERT INTO candidate_packets (packet_id, candidate_id, symbol, scan_batch_id, "
        "packet_json, created_at_utc, created_at_sgt) VALUES (?, ?, 'BAD', 'batch_test', ?, ?, ?)",
        (packet_id_bad, candidate_id_bad, json.dumps({"symbol": "BAD"}), ts, ts),  # missing required fields
    )
    journal.conn.commit()
    _seed_packet(journal, symbol="GOOD2")
    settings = make_settings()

    result = relabel_candidates(journal, settings, "2026-07-01", "2026-07-01", dry_run=False)

    assert "error" not in result  # the run itself must not crash/error out
    assert result["n_corpus_errors"] == 1
    assert result["n_relabelled"] == 2  # both good packets still processed


# =============================================================== dry run
def test_dry_run_makes_zero_network_calls(journal, monkeypatch):
    """Belt-and-suspenders: force PlaybookClassifier construction to raise
    if reached at all during a dry run."""
    _seed_packet(journal, symbol="AAPL")
    settings = make_settings()

    def _boom(*a, **k):
        raise AssertionError("PlaybookClassifier must never be constructed during --dry-run")

    monkeypatch.setattr("alphaos.ai.playbook_classifier.PlaybookClassifier", _boom)

    result = relabel_candidates(journal, settings, "2026-07-01", "2026-07-01", dry_run=True)

    assert "error" not in result
    assert len(result["prompts"]) == 1


def test_dry_run_persists_nothing(journal):
    _seed_packet(journal, symbol="AAPL")
    before = journal.count_rows("candidate_labels")
    settings = make_settings()

    relabel_candidates(journal, settings, "2026-07-01", "2026-07-01", dry_run=True)

    assert journal.count_rows("candidate_labels") == before


def test_dry_run_prompt_carries_no_underscore_keys(journal):
    """Belt-and-suspenders per the spec's own explicit test requirement --
    structurally guaranteed by to_prompt_dict()'s whitelist, but the
    composed prompt itself is the artifact an operator actually eyeballs."""
    _seed_packet(journal, symbol="AAPL")
    settings = make_settings()

    result = relabel_candidates(journal, settings, "2026-07-01", "2026-07-01", dry_run=True)

    prompt = result["prompts"][0]["prompt"]
    assert '"_' not in prompt


# =================================================================== live run
def test_live_run_persists_new_rows_with_relabel_of_set(journal):
    packet_id, _ = _seed_packet(journal, symbol="AAPL", primary_label="Breakout")
    original = journal.one("SELECT * FROM candidate_labels WHERE packet_id = ?", (packet_id,))
    settings = make_settings()

    result = relabel_candidates(journal, settings, "2026-07-01", "2026-07-01", dry_run=False)

    assert "error" not in result
    assert result["n_relabelled"] == 1
    new_rows = journal.query(
        "SELECT * FROM candidate_labels WHERE packet_id = ? AND relabel_of IS NOT NULL", (packet_id,)
    )
    assert len(new_rows) == 1
    assert new_rows[0]["relabel_of"] == original["label_id"]
    assert new_rows[0]["scan_batch_id"] == "batch_test"  # threaded from the original packet


def test_live_run_sets_relabel_of_null_when_no_original_label_existed(journal):
    _seed_packet(journal, symbol="AAPL", with_label=False)
    settings = make_settings()

    result = relabel_candidates(journal, settings, "2026-07-01", "2026-07-01", dry_run=False)

    assert result["n_relabelled"] == 1
    row = journal.one("SELECT * FROM candidate_labels WHERE relabel_of IS NULL AND symbol = 'AAPL'")
    assert row is not None


def test_live_run_never_modifies_the_original_row(journal):
    packet_id, _ = _seed_packet(journal, symbol="AAPL")
    before = journal.query("SELECT * FROM candidate_labels ORDER BY id")
    before_hash = hashlib.sha256(json.dumps(before, sort_keys=True, default=str).encode()).hexdigest()
    settings = make_settings()

    relabel_candidates(journal, settings, "2026-07-01", "2026-07-01", dry_run=False)

    original_ids = [r["id"] for r in before]
    after = journal.query(
        "SELECT * FROM candidate_labels WHERE id IN ({})".format(",".join(map(str, original_ids)))
    )
    after_hash = hashlib.sha256(json.dumps(after, sort_keys=True, default=str).encode()).hexdigest()
    assert before_hash == after_hash


def test_live_run_logs_one_system_event_per_packet(journal):
    _seed_packet(journal, symbol="AAPL")
    _seed_packet(journal, symbol="MSFT")
    settings = make_settings()

    relabel_candidates(journal, settings, "2026-07-01", "2026-07-01", dry_run=False)

    events = journal.query("SELECT * FROM system_events WHERE category = 'relabel'")
    assert len(events) == 2
    for e in events:
        detail = json.loads(e["detail_json"])
        assert "original_id" in detail and "new_id" in detail and "prompt_sha256" in detail


def test_live_run_produces_a_diff_entry_per_packet(journal):
    _seed_packet(journal, symbol="AAPL", primary_label="Breakout", label_decision="watch")
    settings = make_settings()

    result = relabel_candidates(journal, settings, "2026-07-01", "2026-07-01", dry_run=False)

    assert len(result["diffs"]) == 1
    diff = result["diffs"][0]
    assert diff["symbol"] == "AAPL"
    assert diff["old_label"] == "Breakout"
    assert diff["old_decision"] == "watch"
    assert "new_label" in diff and "new_decision" in diff


def test_live_run_refuses_when_cost_cap_is_reached(journal):
    settings = make_settings(
        ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-test-key-not-real",
        SCHEDULER_AI_COST_CAP_CALLS_PER_30D="50",
        # EXP-1: SHADOW_AI_CAP_CALLS_PER_30D's own joint-validation (<=25% of
        # the shared pool) must clear THIS lowered global cap too -- its
        # default of 500 only clears the default global cap of 2000.
        SHADOW_AI_CAP_CALLS_PER_30D="12",
    )
    _seed_packet(journal, symbol="AAPL")
    for _ in range(50):
        journal.conn.execute(
            "INSERT INTO openai_evaluations (eval_id, candidate_id, symbol, model, direction, "
            "decision, reasoning_summary, is_mock, created_at_utc, created_at_sgt) "
            "VALUES (?, ?, 'AAPL', 'gpt-4o-mini', 'long', 'reject', 'x', 0, ?, ?)",
            (new_id("eval"), new_id("cand"), "2026-07-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),
        )
    journal.conn.commit()

    result = relabel_candidates(journal, settings, "2026-07-01", "2026-07-01", dry_run=False)

    assert "error" in result
    assert "cost cap" in result["error"].lower()
    assert journal.count_rows(
        "candidate_labels", "relabel_of IS NOT NULL",
    ) == 0


def test_live_run_in_mock_mode_never_checks_the_cost_cap(journal, monkeypatch):
    """Mock mode makes no real calls, so there is nothing to guard against
    -- the cap check must not even run (would needlessly refuse a
    perfectly-safe mock relabel if it did)."""
    _seed_packet(journal, symbol="AAPL")
    settings = make_settings()  # mock mode

    def _boom(*a, **k):
        raise AssertionError("cost cap must not be checked in mock mode")

    monkeypatch.setattr("alphaos.scheduler.cost_guard.check_scan_budget", _boom)

    result = relabel_candidates(journal, settings, "2026-07-01", "2026-07-01", dry_run=False)

    assert "error" not in result
    assert result["n_relabelled"] == 1


# ============================================================= CLI wiring
def test_cli_relabel_is_a_valid_command():
    from alphaos.__main__ import build_parser

    args = build_parser().parse_args(["relabel", "--from", "2026-07-01", "--to", "2026-07-01", "--dry-run"])
    assert args.command == "relabel"
    assert args.date_from == "2026-07-01"
    assert args.date_to == "2026-07-01"
    assert args.dry_run is True


def test_cli_relabel_requires_from_and_to():
    from alphaos.__main__ import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["relabel"])


# =============================================== no-decision-path grep guard
def test_relabel_module_never_touches_the_order_submission_surface():
    import pathlib

    banned = ("execute_proposal", "approve_proposal", "close_position",
              "submit_bracket", "submit_order", "place_order")
    path = pathlib.Path(__file__).resolve().parents[1] / "alphaos" / "relabel.py"
    text = path.read_text(encoding="utf-8")
    for token in banned:
        assert token not in text, f"relabel.py references {token!r}"
