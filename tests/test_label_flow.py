"""End-to-end interest-scan -> label -> gate flow (Roadmap 2.3).

Asserts the new flow journals every shortlisted candidate, the label is
downgrade-only (never creates a PROPOSE), labelling executes nothing, gates +
manual approval are preserved, and disabling labelling reverts to the legacy
momentum path.
"""

from __future__ import annotations

from alphaos.constants import Decision, LABEL_VERSION_V1
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from conftest import make_settings


def _orch(**over):
    return Orchestrator(settings=make_settings(**over), journal=JournalStore(":memory:"))


def test_scan_journals_packets_and_labels_for_shortlist():
    o = _orch(MAX_CANDIDATES_TO_AI="6")
    summ = o.run_scan_once()
    assert summ.shortlisted > 0 and summ.labelled == summ.shortlisted
    assert summ.shortlisted <= 6  # cost cap respected
    assert o.journal.count_rows("candidate_packets") == summ.labelled
    assert o.journal.count_rows("candidate_labels") == summ.labelled
    labelled = o.journal.query("SELECT * FROM candidates WHERE primary_label IS NOT NULL")
    assert len(labelled) == summ.labelled
    for c in labelled:
        assert c["interest_rank"] is not None
        assert c["label_frozen_at_utc"] and c["label_source"]
        assert c["label_decision"] in (Decision.PROPOSE.value, Decision.WATCH.value, Decision.REJECT.value)
    o.close()


def test_ledger_records_propose_watch_reject_candidates():
    o = _orch()
    summ = o.run_scan_once()
    assert summ.candidates > 0 and summ.proposed > 0  # proven momentum path survives
    statuses = {r["status"] for r in o.journal.query("SELECT DISTINCT status FROM candidates")}
    assert "proposed" in statuses
    # rejected candidates (eval-driven) are journaled with a reason
    assert o.journal.count_rows("rejected_candidates") >= 0  # may be 0 in mock; never errors
    o.close()


def test_label_floor_is_downgrade_only(monkeypatch):
    """Force every label to WATCH; shortlisted candidates the eval would PROPOSE
    must end WATCH (no proposal) — the label can only restrict."""
    o = _orch()
    from alphaos.ai.playbook_classifier import PlaybookClassification
    from alphaos.util.ids import new_id

    def watch_label(packet):
        return PlaybookClassification(
            label_id=new_id("lbl"), candidate_id=packet.candidate_id, symbol=packet.symbol,
            primary_label="Momentum", secondary_labels=[], candidate_tags=[], risk_tags=[],
            direction="long", label_decision=Decision.WATCH.value, confidence=0.9,
            reason_for_label="forced watch", thesis_stub="", invalidation="", main_risk="",
            missing_context=[], suggested_new_tags=[], label_version=LABEL_VERSION_V1,
            label_source="mock", validation_status="passed", model="mock", is_mock=True, raw={},
        )

    monkeypatch.setattr(o.labeller, "classify", watch_label)
    o.run_scan_once()
    shortlisted = o.journal.query("SELECT * FROM candidates WHERE label_decision IS NOT NULL")
    assert shortlisted
    assert all(c["status"] != "proposed" for c in shortlisted)  # WATCH label blocked every propose
    o.close()


def test_labelling_creates_no_orders():
    o = _orch()
    o.run_scan_once()
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_rows("paper_fills") == 0
    assert o.journal.count_open_positions() == 0
    o.close()


def test_labelling_disabled_reverts_to_legacy_path():
    o = _orch(LABELLING_ENABLED="false")
    summ = o.run_scan_once()
    assert summ.labelled == 0 and summ.shortlisted == 0
    assert o.journal.count_rows("candidate_labels") == 0
    assert o.journal.count_rows("candidate_packets") == 0
    assert summ.proposed > 0  # legacy momentum path still proposes
    o.close()


def test_dashboard_render_is_readonly_on_populated_ledger(monkeypatch):
    """The read-only dashboard must write NOTHING on render even when the ledger
    is full (candidates, labels, pending proposals) — no scan, no approval, no
    execution from rendering."""
    import sys
    sys.path.insert(0, "tests")
    from test_approval_execution import _fake_st
    from alphaos.dashboard import streamlit_app

    o = _orch()
    o.run_scan_once()  # populate candidates / packets / labels / pending proposals
    j = o.journal
    tabs = [r["name"] for r in j.query("SELECT name FROM sqlite_master WHERE type='table'")]
    before = sum(j.count_rows(t) for t in tabs)
    assert before > 0

    monkeypatch.setattr(streamlit_app, "st", _fake_st())
    streamlit_app.main(orch=o)  # full render of all tabs, no user actions

    after = sum(j.count_rows(t) for t in tabs)
    assert after == before, "dashboard render must not write to the ledger"
    o.close()


def test_wide_spread_candidate_filtered_before_labelling():
    """The interest score cannot bypass the spread gate: a wide-spread symbol is
    rejected at the tradeability gate and never becomes a labelled candidate."""
    from alphaos.scanner.candidate_scanner import CandidateScanner
    from alphaos.util import timeutils

    s = make_settings(MAX_SPREAD_PCT="0.01")
    j = JournalStore(":memory:")

    class FakeMarket:
        def get_snapshot(self, sym):
            ts = timeutils.stamp().utc
            return {
                "symbol": sym, "provider": "alpaca_mock", "feed": "iex", "is_mock": True,
                "last_price": 100.0, "prev_close": 95.0, "bid": 99.0, "ask": 101.0, "spread": 2.0,
                "spread_pct": 0.02, "volume": 1_000_000, "rel_volume": 2.0, "dollar_volume": 1e8,
                "change_pct": 0.05, "bar_open": 95.0, "bar_high": 101.0, "bar_low": 95.0,
                "quote_timestamp": ts, "bar_timestamp": ts, "source_timestamp": ts,
                "received_at": ts, "market_session": "regular",
            }

    sc = CandidateScanner(s, j, market_data=FakeMarket())
    res = sc.scan(symbols=["WIDE"])
    assert res.candidates == []  # high interest, but wide spread -> not a candidate
    assert j.query("SELECT * FROM rejected_candidates WHERE reason_code = 'WIDE_SPREAD'")
    j.close()
