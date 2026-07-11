"""PR-UI-B2: the Learning tab (TQS / Attribution / Hypotheses / Journal).

Hermetic -- mock mode, in-memory SQLite, no network, reuses
test_approval_execution._fake_st() for the one full-render test, same
pattern test_dashboard.py already uses. Covers the new report-layer helpers
directly (build_tqs_report, build_journal_feed, hypothesis_report's new
progress field, scoreboard.promotion_history) plus the dashboard's own pure
formatting functions, then one full-render, writes-nothing invariant test.
"""

from __future__ import annotations

from alphaos.cards.scoreboard import demoted_cards, promotion_history
from alphaos.dashboard import streamlit_app
from alphaos.hypotheses import accept_draft, reject_draft, seed_all
from alphaos.hypotheses.proposer import intake_draft
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.reports.journal_feed import build_journal_feed
from alphaos.reports.tqs_report import build_tqs_report
from alphaos.util.ids import new_id
from conftest import make_settings
from test_approval_execution import _fake_st


def _orch(**over):
    return Orchestrator(settings=make_settings(**over), journal=JournalStore(":memory:"))


def _tqs_row(journal, symbol="AAPL", bucket="strong", confidence=0.8, is_mock=0,
            components=None, missing=None):
    journal.insert("tqs_scores", {
        "tqs_id": new_id("tqs"), "source_type": "candidate", "candidate_id": new_id("cand"),
        "symbol": symbol, "tqs_version": "0.1.0", "raw_score": 70, "data_confidence": confidence,
        "tqs_score": round(70 * confidence), "tqs_bucket": bucket,
        "components_json": components or {}, "missing_components_json": missing or {},
        "data_quality_status": "mock" if is_mock else "ok", "is_mock": is_mock,
    })


def _resolved_attribution(journal, symbol="MSFT", delta_r=1.0, is_mock=0,
                          atype="propose_user_rejected", resolved_at="2026-06-01T00:00:00Z"):
    journal.insert("attribution_records", {
        "attribution_id": new_id("attr"), "attribution_type": atype, "attribution_version": "v2",
        "agent": "user", "source_id": new_id("src"), "candidate_id": new_id("cand"), "symbol": symbol,
        "resolved_status": "resolved", "delta_r": delta_r, "is_mock": is_mock,
        "resolved_at_utc": resolved_at, "decision_at_utc": resolved_at,
        "data_quality_status": "ok",
    })


def _promotion_decision(journal, card_id="catalyst_momentum_v2", direction="promote", decided_at="2026-06-01T00:00:00Z"):
    journal.insert("promotion_decisions", {
        "decision_id": new_id("promo"), "card_id": card_id, "card_version": 1,
        "from_state": "shadow", "to_state": "live_eligible", "direction": direction,
        "trigger": "manual", "hypothesis_id": "H-CAT-1", "decided_by": "operator",
        "decided_at_utc": decided_at, "decided_at_sgt": decided_at,
    })


def _card_demotion(journal, card_id="catalyst_momentum_v2", demoted_at="2026-06-02T00:00:00Z"):
    journal.insert("card_demotions", {
        "demotion_id": new_id("demo"), "card_id": card_id, "card_version": 1,
        "reason": "test breach", "triggering_snapshot_id_1": new_id("snap"),
        "triggering_snapshot_id_2": new_id("snap"), "demoted_at_utc": demoted_at,
        "demoted_at_sgt": demoted_at,
    })


def _valid_candidate(**over) -> dict:
    # metric_fn_name=h_win_1_rows / direction=positive deliberately does NOT
    # collide with any seeded hypothesis's own metric+direction pair (the
    # seeded H-WIN-1 itself is direction='either', not 'positive' --
    # check_duplicate()'s exact-match rule treats those as distinct) -- a
    # test using h_tqs_1_rows/positive here would be silently hard-blocked
    # as a duplicate of the seeded H-TQS-1 the moment seed_all() has run.
    base = {
        "title": "Morning-window candidates show a modest positive delta",
        "claim_text": "Morning-window candidates show a modest positive delta vs afternoon",
        "metric_fn_name": "h_win_1_rows",
        "proposed_risk_class": "A",
        "direction": "positive",
    }
    base.update(over)
    return base


# --------------------------------------------------------------- TQS report
def test_build_tqs_report_excludes_mock_from_histogram_and_confidence():
    j = JournalStore(":memory:")
    _tqs_row(j, bucket="strong", confidence=0.9, is_mock=0)
    _tqs_row(j, bucket="strong", confidence=0.8, is_mock=0)
    _tqs_row(j, bucket="weak", confidence=0.5, is_mock=1)  # mock -- excluded

    rep = build_tqs_report(j)
    assert rep["scored_count"] == 2
    assert rep["mock_excluded_count"] == 1
    assert rep["bucket_histogram"] == {"strong": 2}
    assert rep["mean_data_confidence"] == 0.85


def test_build_tqs_report_component_availability_rates():
    j = JournalStore(":memory:")
    _tqs_row(j, components={"ai_conviction": {"score": 1.0, "weight": 20}}, missing={})
    _tqs_row(j, components={}, missing={"ai_conviction": {"weight": 20, "reason": "mock_ai"}})

    rep = build_tqs_report(j)
    avail = rep["component_availability"]["ai_conviction"]
    assert avail == {"available": 1, "missing": 1, "availability_rate": 0.5}
    # A component never mentioned in either json blob for any row: 0/0 -> None rate, never fabricated.
    assert rep["component_availability"]["event_risk_clearance"]["availability_rate"] is None


def test_build_tqs_report_empty_journal():
    j = JournalStore(":memory:")
    rep = build_tqs_report(j)
    assert rep["scored_count"] == 0
    assert rep["mean_data_confidence"] is None
    assert rep["bucket_histogram"] == {}


# ------------------------------------------------------- hypothesis progress
def test_hypothesis_progress_none_for_non_testing_and_h_ai_1():
    j = JournalStore(":memory:")
    seed_all(j)
    from alphaos.reports.hypothesis_report import build_hypothesis_report

    rep = build_hypothesis_report(j)
    by_id = {h["hypothesis_id"]: h for h in rep["hypotheses"]}
    # H-AI-1 has metric_fn_name=None and no BASELINE prereg registered yet in
    # this fixture -> status stays 'proposed', progress must be None either way.
    assert by_id["H-AI-1"]["progress"] is None


def test_hypothesis_progress_below_floor_for_thin_sample():
    j = JournalStore(":memory:")
    seed_all(j)
    # H-REJ-1 (Class C, floor 60/90d) has no reference arm -- simplest to
    # drive with a handful of resolved rejection rows, well below its floor.
    _resolved_attribution(j, symbol="AAA", delta_r=0.5, atype="propose_user_rejected",
                          resolved_at="2026-06-01T00:00:00Z")
    _resolved_attribution(j, symbol="BBB", delta_r=-0.2, atype="propose_user_rejected",
                          resolved_at="2026-06-02T00:00:00Z")

    from alphaos.reports.hypothesis_report import build_hypothesis_report

    rep = build_hypothesis_report(j)
    by_id = {h["hypothesis_id"]: h for h in rep["hypotheses"]}
    progress = by_id["H-REJ-1"]["progress"]
    assert progress is not None
    assert progress["clears_floor"] is False
    assert progress["effective_n"] <= 2
    assert progress["floor_effective_n"] == 60


# ------------------------------------------------------------- scoreboard
def test_promotion_history_and_demoted_cards_newest_first():
    j = JournalStore(":memory:")
    _promotion_decision(j, card_id="card_a", direction="promote", decided_at="2026-06-01T00:00:00Z")
    _promotion_decision(j, card_id="card_b", direction="demote", decided_at="2026-06-03T00:00:00Z")
    _card_demotion(j, card_id="card_c", demoted_at="2026-06-02T00:00:00Z")

    promos = promotion_history(j)
    assert [p["card_id"] for p in promos] == ["card_b", "card_a"]  # id desc = insertion order desc
    demoted = demoted_cards(j)
    assert demoted[0]["card_id"] == "card_c"


# --------------------------------------------------------------- journal feed
def test_journal_feed_includes_all_three_entry_kinds_sorted_newest_first():
    j = JournalStore(":memory:")
    seed_all(j)
    _resolved_attribution(j, symbol="MSFT", delta_r=1.5, resolved_at="2026-06-01T00:00:00Z")
    _promotion_decision(j, card_id="card_x", decided_at="2026-06-05T00:00:00Z")
    _card_demotion(j, card_id="card_y", demoted_at="2026-06-04T00:00:00Z")

    feed = build_journal_feed(j)
    kinds = {e["kind"] for e in feed["entries"]}
    assert {"resolved_event", "hypothesis_lifecycle", "promotion_demotion"} <= kinds

    timestamps = [e["timestamp"] for e in feed["entries"]]
    assert timestamps == sorted(timestamps, reverse=True)

    resolved = [e for e in feed["entries"] if e["kind"] == "resolved_event"][0]
    assert "MSFT" in resolved["text"] and "+1.50R" in resolved["text"]
    assert resolved["provenance"]["attribution_id"]


def test_journal_feed_surfaces_draft_accept_reject_events():
    j = JournalStore(":memory:")
    seed_all(j)
    accepted = intake_draft(j, _valid_candidate(title="accepted one"), source="manual")
    accept_draft(j, accepted["draft_id"], decided_by="operator")
    rejected = intake_draft(
        j,
        _valid_candidate(
            title="rejected one", claim_text="a distinct claim text",
            metric_fn_name="h_ttl_1_rows", direction="positive",
        ),
        source="manual",
    )
    reject_draft(j, rejected["draft_id"], decided_by="operator", reason="not compelling")

    feed = build_journal_feed(j)
    texts = " ".join(e["text"] for e in feed["entries"])
    assert accepted["draft_id"] in texts
    assert rejected["draft_id"] in texts
    assert "operator" in texts


def test_journal_feed_never_backfills_missing_timestamp_into_sort_order():
    j = JournalStore(":memory:")
    _resolved_attribution(j, resolved_at=None)  # unparseable/missing -> excluded, not sorted as oldest/newest
    feed = build_journal_feed(j)
    assert feed["entries"] == []


# ------------------------------------------------------- dashboard pure fns
def test_hypothesis_status_label_tags_operator_ruling():
    assert streamlit_app._hypothesis_status_label({"status": "testing"}) == "testing"
    assert streamlit_app._hypothesis_status_label({"status": "met"}) == "met (operator ruling)"
    assert streamlit_app._hypothesis_status_label({"status": "failed"}) == "failed (operator ruling)"
    assert streamlit_app._hypothesis_status_label({"status": "withdrawn"}) == "withdrawn (operator ruling)"


def test_hypothesis_progress_label_formats_or_dashes():
    assert streamlit_app._hypothesis_progress_label(None) == "—"
    label = streamlit_app._hypothesis_progress_label({
        "effective_n": 5, "floor_effective_n": 60, "span_days": 10.0, "floor_span_days": 90.0,
        "clears_floor": False,
    })
    assert "5/60" in label and "below floor" in label


def test_attribution_v2_agg_row_below_floor_shows_counts_only():
    row = streamlit_app._attribution_v2_agg_row(
        "propose_blocked / gate", {"status": "below_sample_floor", "effective_n": 3, "span_days": 2.0},
        floor_n=30, floor_span=28,
    )
    assert row["mean_ΔR"] is None and row["sum_ΔR"] is None
    assert "below floor" in row["status"]


def test_attribution_v2_agg_row_ok_shows_mean_and_sum():
    row = streamlit_app._attribution_v2_agg_row(
        "propose_blocked / gate",
        {"status": "ok", "effective_n": 31, "span_days": 40.0, "mean_delta_r": 0.21, "sum_delta_r": 6.5},
        floor_n=30, floor_span=28,
    )
    assert row["mean_ΔR"] == 0.21 and row["sum_ΔR"] == 6.5
    assert row["status"] == "✓ ok"


# --------------------------------------------------------------- full render
def test_learning_tab_renders_read_only_with_populated_state(monkeypatch):
    """Full-render, writes-nothing invariant (same posture as
    test_dashboard.py's own test_dashboard_render_writes_nothing_with_
    populated_state), extended to cover every new data source this tab
    reads: seeded hypotheses, a pending HGEN-1 draft, resolved attribution,
    a promotion decision, and an automatic card demotion."""
    orch = _orch()
    j = orch.journal
    seed_all(j)
    intake_draft(j, _valid_candidate(), source="manual")
    _tqs_row(j)
    _resolved_attribution(j)
    _promotion_decision(j)
    _card_demotion(j)

    watched = (
        "scan_batches", "scheduler_runs", "config_versions",
        "paper_orders", "paper_fills", "positions", "candidates", "trade_proposals",
        "hypothesis_proposals", "hypothesis_drafts", "preregistrations",
        "promotion_decisions", "card_demotions", "attribution_records", "tqs_scores",
    )
    before = {t: j.count_rows(t) for t in watched}

    fake = _fake_st()
    monkeypatch.setattr(streamlit_app, "st", fake)
    streamlit_app.main(orch=orch)

    after = {t: j.count_rows(t) for t in watched}
    assert after == before, f"Learning tab render wrote rows: before={before} after={after}"

    # No accept/reject button surfaces for the pending draft -- read-only in
    # this PR by design (the CLI ceremony is the authored path).
    button_calls = [c.args[0] for c in fake.button.call_args_list if c.args]
    assert not any("accept" in str(a).lower() or "reject" in str(a).lower() for a in button_calls)

    # The required anti-self-modification banner rendered.
    warning_texts = " ".join(str(c.args[0]) for c in fake.warning.call_args_list if c.args)
    assert "operator" in warning_texts.lower()
    assert "never adjusts its own weights" in warning_texts.lower()
    orch.close()
