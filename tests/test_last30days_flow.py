"""End-to-end last30days flow (Roadmap 2.5): journaled, advisory-only, never
bypasses gates / approval / execution; the no-news eval is unaffected; the
per-scan budget cap enriches the top-N by rank and journals the rest as a
DISTINCT skipped_budget_cap (not none_found). Hermetic — mock provider only."""

from __future__ import annotations

import json

from alphaos.constants import NEWS_STATUS_DISABLED_V1, OFFICIAL_LABELS, Last30DaysStatus
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from conftest import make_settings


def _orch(**over):
    base = {"LAST30DAYS_ENABLED": "true", "LAST30DAYS_PROVIDER": "mock"}
    base.update(over)
    return Orchestrator(settings=make_settings(**base), journal=JournalStore(":memory:"))


def test_scan_enriches_and_journals_every_eligible_candidate():
    o = _orch(MAX_CANDIDATES_TO_AI="10", LAST30DAYS_MAX_SYMBOLS_PER_SCAN="6")
    summ = o.run_scan_once()
    assert 0 < summ.last30days_enriched <= 6                  # cost cap respected
    rows = o.journal.query("SELECT * FROM candidate_last30days")
    # every enriched + every budget-skipped eligible candidate is journaled
    assert len(rows) == summ.last30days_enriched + summ.last30days_skipped_budget_cap
    assert all(r["last30days_status"] for r in rows)
    o.close()


def test_budget_cap_top10_enriched_rest_skipped():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              LAST30DAYS_MAX_SYMBOLS_PER_SCAN="10")
    summ = o.run_scan_once()
    assert summ.labelled == 12                                # 12 eligible candidates
    assert summ.last30days_enriched == 10                     # top 10 by interest rank
    assert summ.last30days_skipped_budget_cap == 2            # ranks 11 & 12

    skipped = o.journal.query(
        "SELECT * FROM candidate_last30days WHERE last30days_status = ?",
        (Last30DaysStatus.SKIPPED_BUDGET_CAP.value,),
    )
    assert len(skipped) == 2
    for r in skipped:
        assert r["enrichment_status"] == "skipped"
        assert r["enrichment_error"] is None
        assert r["reason"]                                    # records WHY
        assert r["interest_rank"] is not None
        assert r["provider"]                                  # configured provider recorded
        assert r["symbol"]
    # the skipped candidates are exactly ranks 11 and 12 (top-N by rank enriched)
    assert sorted(r["interest_rank"] for r in skipped) == [11, 12]
    o.close()


def test_skipped_budget_cap_is_not_none_found():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              LAST30DAYS_MAX_SYMBOLS_PER_SCAN="10")
    o.run_scan_once()
    skipped = o.journal.query("SELECT * FROM candidate_last30days WHERE enrichment_status = 'skipped'")
    assert skipped
    for r in skipped:
        assert r["last30days_status"] == Last30DaysStatus.SKIPPED_BUDGET_CAP.value
        assert r["last30days_status"] != Last30DaysStatus.NONE_FOUND.value
    o.close()


def test_packet_carries_last30days_when_fed_to_labeller():
    o = _orch(LAST30DAYS_FEED_TO_LABELLER="true")
    o.run_scan_once()
    row = o.journal.one(
        "SELECT packet_id FROM candidate_last30days WHERE last30days_status = ? LIMIT 1",
        (Last30DaysStatus.AVAILABLE.value,),
    )
    assert row, "expected at least one 'available' enriched candidate"
    pj = json.loads(o.journal.one(
        "SELECT packet_json FROM candidate_packets WHERE packet_id = ?", (row["packet_id"],),
    )["packet_json"])
    assert pj["last30days_context"] != "unavailable"
    assert "sentiment_context" in pj
    o.close()


def test_feed_to_labeller_false_keeps_packet_unavailable_but_still_journals():
    o = _orch(LAST30DAYS_FEED_TO_LABELLER="false")
    o.run_scan_once()
    assert o.journal.count_rows("candidate_last30days") > 0   # still journaled + dashboard-visible
    pkts = o.journal.query("SELECT packet_json FROM candidate_packets")
    assert pkts
    assert all(json.loads(p["packet_json"])["last30days_context"] == "unavailable" for p in pkts)
    o.close()


def test_disabled_by_default_writes_no_rows():
    o = Orchestrator(settings=make_settings(), journal=JournalStore(":memory:"))  # default: disabled
    summ = o.run_scan_once()
    assert summ.last30days_enriched == 0 and summ.last30days_skipped_budget_cap == 0
    assert o.journal.count_rows("candidate_last30days") == 0
    pj = json.loads(o.journal.one("SELECT packet_json FROM candidate_packets LIMIT 1")["packet_json"])
    assert pj["last30days_context"] == "unavailable"
    o.close()


def test_last30days_creates_no_execution_and_no_approval():
    o = _orch()
    summ = o.run_scan_once()
    assert summ.proposed >= 0
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_rows("paper_fills") == 0
    assert o.journal.count_open_positions() == 0
    assert o.journal.count_rows("approvals") == 0             # manual approval still required
    o.close()


def test_last30days_never_creates_or_overwrites_official_labels():
    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              LAST30DAYS_MAX_SYMBOLS_PER_SCAN="10")
    o.run_scan_once()
    labels = o.journal.query("SELECT primary_label FROM candidate_labels")
    assert labels and all(label["primary_label"] in OFFICIAL_LABELS for label in labels)
    # frozen primary_label on candidates stays official even for skipped candidates
    joined = o.journal.query(
        "SELECT c.primary_label AS pl, l.last30days_status AS ls "
        "FROM candidates c JOIN candidate_last30days l ON c.candidate_id = l.candidate_id"
    )
    assert joined
    assert all(r["pl"] in OFFICIAL_LABELS for r in joined)
    o.close()


def test_last30days_cannot_force_propose():
    """Enabling last30days must NEVER create a proposal the no-news path didn't
    already make: the eval is no-news (identical both ways) and the label floor is
    downgrade-only, so the proposed count can only stay the same or shrink."""
    base = {"INTEREST_SCAN_TOP_N": "12", "MAX_CANDIDATES_TO_AI": "12"}
    off = Orchestrator(settings=make_settings(LAST30DAYS_ENABLED="false", **base),
                       journal=JournalStore(":memory:"))
    proposed_off = off.run_scan_once().proposed
    off.close()
    on = _orch(LAST30DAYS_MAX_SYMBOLS_PER_SCAN="10", **base)
    proposed_on = on.run_scan_once().proposed
    on.close()
    assert proposed_on <= proposed_off          # last30days never ADDS a proposal


def test_eval_stays_no_news_with_last30days_enabled():
    o = _orch()
    o.run_scan_once()
    assert o.journal.query("SELECT 1 FROM openai_evaluations LIMIT 1")
    cands = o.journal.query("SELECT news_status FROM candidates")
    assert cands and all(c["news_status"] == NEWS_STATUS_DISABLED_V1 for c in cands)
    o.close()


def test_real_money_unreachable_with_last30days_enabled():
    o = _orch()
    assert o.system_health()["real_money_trading"] == "unreachable"
    assert "keyless" in o.system_health()["last30days_research"]
    o.close()


def test_dashboard_render_is_readonly_with_skipped_rows(monkeypatch):
    """Read-only dashboard writes NOTHING on render even with skipped_budget_cap
    rows present (the new last30days summary/detail sections are SELECT-only)."""
    import sys

    sys.path.insert(0, "tests")
    from test_approval_execution import _fake_st

    from alphaos.dashboard import streamlit_app

    o = _orch(INTEREST_SCAN_TOP_N="12", MAX_CANDIDATES_TO_AI="12",
              LAST30DAYS_MAX_SYMBOLS_PER_SCAN="10")
    o.run_scan_once()
    j = o.journal
    statuses = {r["last30days_status"] for r in j.query("SELECT last30days_status FROM candidate_last30days")}
    assert Last30DaysStatus.SKIPPED_BUDGET_CAP.value in statuses

    tabs = [r["name"] for r in j.query("SELECT name FROM sqlite_master WHERE type='table'")]
    before = sum(j.count_rows(t) for t in tabs)
    assert before > 0

    monkeypatch.setattr(streamlit_app, "st", _fake_st())
    streamlit_app.main(orch=o)                                # full render, no user actions

    after = sum(j.count_rows(t) for t in tabs)
    assert after == before, "dashboard render must not write to the ledger"
    o.close()


def test_last30days_probe_is_readonly():
    o = _orch()
    before = o.journal.count_rows("candidate_last30days")
    res = o.last30days_probe("AAPL")
    assert res["symbol"] == "AAPL"
    assert res["provider"] == "mock"
    assert res["last30days_status"]                            # ran the (forced) provider
    assert o.journal.count_rows("candidate_last30days") == before  # wrote nothing
    o.close()
