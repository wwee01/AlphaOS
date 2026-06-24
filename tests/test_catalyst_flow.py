"""End-to-end catalyst enrichment flow (Roadmap 2.4): journaled, advisory-only,
never bypasses gates / approval / execution; AI labelling unaffected."""

from __future__ import annotations

import json

from alphaos.constants import OFFICIAL_LABELS
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from conftest import make_settings


def _orch(**over):
    return Orchestrator(settings=make_settings(**over), journal=JournalStore(":memory:"))


def test_scan_enriches_and_journals_every_enriched_candidate():
    o = _orch(MAX_CANDIDATES_TO_AI="10", NEWS_MAX_SYMBOLS_PER_SCAN="6")
    summ = o.run_scan_once()
    assert 0 < summ.catalyst_enriched <= 6                      # cost cap respected
    rows = o.journal.query("SELECT * FROM candidate_catalysts")
    assert len(rows) == summ.catalyst_enriched
    assert all(r["catalyst_status"] for r in rows)             # every enriched candidate journaled
    o.close()


def test_packet_includes_catalyst_fields():
    o = _orch()
    o.run_scan_once()
    pd = json.loads(o.journal.one("SELECT packet_json FROM candidate_packets LIMIT 1")["packet_json"])
    for k in ("catalyst_status", "catalyst_type", "catalyst_summary", "catalyst_risk_tags",
              "official_news_context", "sector_context"):
        assert k in pd
    o.close()


def test_catalyst_cannot_create_official_labels():
    o = _orch()
    o.run_scan_once()
    labels = o.journal.query("SELECT primary_label FROM candidate_labels")
    assert labels and all(l["primary_label"] in OFFICIAL_LABELS for l in labels)
    o.close()


def test_catalyst_does_not_overwrite_frozen_label():
    o = _orch()
    o.run_scan_once()
    joined = o.journal.query(
        "SELECT c.primary_label AS pl, cat.catalyst_suggested_label AS sl, cat.label_review_required AS rev "
        "FROM candidates c JOIN candidate_catalysts cat ON c.candidate_id = cat.candidate_id"
    )
    assert joined
    for r in joined:
        assert r["pl"] in OFFICIAL_LABELS                       # frozen label stays official
        if r["rev"]:                                            # flagged for review...
            assert r["pl"] != r["sl"]                           # ...but NOT overwritten
    o.close()


def test_catalyst_causes_no_execution_and_no_approval():
    o = _orch()
    summ = o.run_scan_once()
    assert summ.proposed > 0
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_rows("paper_fills") == 0
    assert o.journal.count_open_positions() == 0
    assert o.journal.count_rows("approvals") == 0              # manual approval still required
    o.close()


def test_labelling_works_without_enrichment():
    o = _orch(NEWS_ENRICHMENT_ENABLED="false")
    summ = o.run_scan_once()
    assert summ.labelled > 0 and summ.catalyst_enriched == 0
    assert o.journal.count_rows("candidate_catalysts") == 0
    pd = json.loads(o.journal.one("SELECT packet_json FROM candidate_packets LIMIT 1")["packet_json"])
    assert pd["catalyst_status"] == "unavailable"
    o.close()
