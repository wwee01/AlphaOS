"""Gated labeller decision override (Roadmap 2.6): the combinator, the arming
gate (inert while mock), real-driver detection, the audit trail, and the safety
floor (gates/approval intact, data-integrity rejects never upgraded). Hermetic."""

from __future__ import annotations

from types import SimpleNamespace

from alphaos.ai.openai_client import OpenAIEvaluation
from alphaos.ai.playbook_classifier import PlaybookClassification
from alphaos.constants import (
    CatalystStatus,
    Decision,
    DecisionAdjustment,
    EnrichmentSource,
    LABEL_VERSION_V1,
    Last30DaysProvider,
    Last30DaysStatus,
)
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator, ScanSummary
from alphaos.scanner.scan_context import ScanContext
from alphaos.util.ids import new_id
from conftest import make_settings

R, W, P = Decision.REJECT.value, Decision.WATCH.value, Decision.PROPOSE.value


def _orch(**over):
    base = {"LAST30DAYS_ENABLED": "true", "LAST30DAYS_PROVIDER": "mock"}
    base.update(over)
    return Orchestrator(settings=make_settings(**base), journal=JournalStore(":memory:"))


# --------------------------------------------------------------- combinator
def test_combinator_downgrade_only_when_not_armed():
    o = _orch()
    assert o._combine_decision(P, W, True, override_active=False) == W   # restrict
    assert o._combine_decision(W, P, True, override_active=False) == W   # NO upgrade
    assert o._combine_decision(P, R, True, override_active=False) == R
    o.close()


def test_combinator_symmetric_when_armed():
    o = _orch()
    assert o._combine_decision(W, P, True, override_active=True) == P    # UPGRADE
    assert o._combine_decision(P, W, True, override_active=True) == W    # downgrade
    assert o._combine_decision(R, P, True, override_active=True) == P    # upgrade w/ levels
    o.close()


def test_combinator_cannot_upgrade_without_valid_levels():
    o = _orch()
    # data-integrity reject (no levels) can NEVER be upgraded by narrative
    assert o._combine_decision(R, P, False, override_active=True) == R
    assert o._combine_decision(W, P, False, override_active=True) == W
    # downgrade is still allowed without levels
    assert o._combine_decision(P, R, False, override_active=True) == R
    o.close()


# ------------------------------------------------------------- arming gate
def test_override_not_armed_when_flag_off():
    o = _orch(LABELLER_DECISION_OVERRIDE_ENABLED="false")
    assert o._override_armed() is False
    o.close()


def test_override_inert_while_mock_even_if_enabled():
    o = _orch(LABELLER_DECISION_OVERRIDE_ENABLED="true")   # mock mode
    assert o._override_armed() is False
    assert o.system_health()["labeller_decision_override"] == "enabled_inert_while_mock"
    o.close()


def test_override_armed_only_with_real_ai_and_flag():
    o = Orchestrator(
        settings=make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="sk-test",
                               ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s",
                               LABELLER_DECISION_OVERRIDE_ENABLED="true"),
        journal=JournalStore(":memory:"),
    )
    assert o._override_armed() is True
    o.close()


# ------------------------------------------------------- real-driver gate
def _cat(status, ctype="analyst_upgrade", source=EnrichmentSource.ALPACA.value):
    return SimpleNamespace(catalyst_status=status, catalyst_type=ctype, enrichment_source=source)


def _l30(status=Last30DaysStatus.AVAILABLE.value, sentiment="bullish",
         provider=Last30DaysProvider.CLI.value):
    return SimpleNamespace(last30days_status=status, provider=provider, sentiment_label=sentiment)


def test_real_driver_accepts_live_positive_catalyst():
    o = _orch()
    has, driver, detail = o._real_decision_driver(_cat(CatalystStatus.CONFIRMED.value), None, "long")
    assert has is True and "catalyst:confirmed" in driver and "catalyst" in detail
    o.close()


def test_real_driver_accepts_live_supportive_sentiment():
    o = _orch()
    assert o._real_decision_driver(None, _l30(sentiment="bullish"), "long")[0] is True
    assert o._real_decision_driver(None, _l30(sentiment="bearish"), "short")[0] is True
    o.close()


# Rule 1 — mock sources must not arm an upgrade
def test_rule1_mock_catalyst_cannot_upgrade():
    o = _orch()
    cat = _cat(CatalystStatus.CONFIRMED.value, source=EnrichmentSource.MOCK.value)
    l30 = _l30(provider=Last30DaysProvider.MOCK.value)
    assert o._real_decision_driver(cat, l30, "long")[0] is False
    o.close()


# Rule 2 — conflicting (and stale/none/unavailable/error) catalyst alone cannot upgrade
def test_rule2_conflicting_or_weak_catalyst_alone_cannot_upgrade():
    o = _orch()
    for status in (CatalystStatus.CONFLICTING.value, CatalystStatus.STALE.value,
                   CatalystStatus.NONE_FOUND.value, CatalystStatus.UNAVAILABLE.value,
                   CatalystStatus.ERROR.value):
        assert o._real_decision_driver(_cat(status), None, "long")[0] is False, status
    o.close()


# Rule 3 — last30days none_found / unknown sentiment cannot upgrade
def test_rule3_last30days_none_or_unknown_cannot_upgrade():
    o = _orch()
    assert o._real_decision_driver(None, _l30(status=Last30DaysStatus.NONE_FOUND.value), "long")[0] is False
    assert o._real_decision_driver(None, _l30(sentiment="unknown"), "long")[0] is False
    assert o._real_decision_driver(None, _l30(sentiment="neutral"), "long")[0] is False
    o.close()


# Opposing real signals must not upgrade (supportive only)
def test_opposing_signals_cannot_upgrade():
    o = _orch()
    # live bearish sentiment cannot upgrade a LONG (but supports a short)
    assert o._real_decision_driver(None, _l30(sentiment="bearish"), "long")[0] is False
    # live confirmed analyst_downgrade cannot upgrade a LONG
    assert o._real_decision_driver(_cat(CatalystStatus.CONFIRMED.value, "analyst_downgrade"), None, "long")[0] is False
    # ...but it does support a SHORT
    assert o._real_decision_driver(_cat(CatalystStatus.CONFIRMED.value, "analyst_downgrade"), None, "short")[0] is True
    o.close()


# Rule 4 — a mixed driver upgrades only if at least one real positive driver exists
def test_rule4_mixed_driver_requires_a_real_positive_driver():
    o = _orch()
    # mock catalyst + unknown sentiment -> NO positive driver
    weak = o._real_decision_driver(_cat(CatalystStatus.CONFIRMED.value, source=EnrichmentSource.MOCK.value),
                                   _l30(sentiment="unknown"), "long")
    assert weak[0] is False
    # live confirmed catalyst + live bullish sentiment -> mixed, qualifies
    has, _, detail = o._real_decision_driver(
        _cat(CatalystStatus.CONFIRMED.value, "product_launch"), _l30(sentiment="bullish"), "long")
    assert has is True and set(detail.keys()) == {"catalyst", "last30days"}
    # live bullish sentiment alone (catalyst mock) still qualifies via the one real driver
    assert o._real_decision_driver(_cat(CatalystStatus.CONFIRMED.value, source=EnrichmentSource.MOCK.value),
                                   _l30(sentiment="bullish"), "long")[0] is True
    o.close()


# Rule 5 — every upgrade is still just a PROPOSAL: gates + manual approval apply
def test_rule5_upgrades_still_require_gates_and_manual_approval(monkeypatch):
    """Direct construction (§H.1): a previous version ran a full mock scan and
    asserted `summ.decision_upgraded > 0`, which silently depends on the
    date-seeded organic scan producing at least one WATCH-eval candidate that
    day — the third bite of the same flake class (broke on the 2026-07-06
    seed). Build the WATCH candidate by hand instead; the upgrade is then
    decided ONLY by the override mechanism under test."""
    o = _orch()
    cand, evaluation, classification = _watch_ready_candidate(o)
    monkeypatch.setattr(o, "_override_armed", lambda: True)
    monkeypatch.setattr(o, "_real_decision_driver",
                        lambda catalyst, last30, direction, polarity=None:
                        (True, "last30days:bullish", {"last30days": {}}))

    summary = ScanSummary(scan_id="rule5-test")
    final = o._resolve_decision(cand, evaluation, classification, "rule5-batch", summary)
    assert final == Decision.PROPOSE.value                  # the upgrade happened
    assert summary.decision_upgraded > 0
    o._handle_proposal(cand, evaluation, summary, scan_batch_id="rule5-batch")
    # ...and it became exactly a PENDING proposal, nothing more:
    rows = o.journal.query("SELECT status FROM trade_proposals")
    assert rows and all(r["status"] == "pending_approval" for r in rows)
    assert o.journal.count_rows("paper_orders") == 0        # nothing executed
    assert o.journal.count_rows("paper_fills") == 0
    assert o.journal.count_open_positions() == 0
    assert o.journal.count_rows("approvals") == 0           # manual approval still required
    assert o.system_health()["manual_approval"] == "required"
    o.close()


# ------------------------------------------------------------- flow / audit
def test_scan_records_one_adjustment_per_labelled_candidate():
    o = _orch()
    summ = o.run_scan_once()
    rows = o.journal.query("SELECT * FROM decision_adjustments")
    assert len(rows) == summ.labelled
    for r in rows:
        assert r["eval_decision"] and r["final_decision"] and r["adjustment"]
        assert r["override_enabled"] == 0          # default off
    o.close()


def test_adjustment_rows_store_full_source_level_evidence():
    """Every adjustment row must let a later analyst answer 'which exact catalyst/
    sentiment evidence drove this move?' — not just a generic driver string."""
    import json

    # caps >= shortlist so every labelled candidate gets both enrichments
    o = _orch(INTEREST_SCAN_TOP_N="6", MAX_CANDIDATES_TO_AI="6",
              NEWS_MAX_SYMBOLS_PER_SCAN="10", LAST30DAYS_MAX_SYMBOLS_PER_SCAN="10")
    o.run_scan_once()
    rows = o.journal.query("SELECT * FROM decision_adjustments")
    assert rows
    for r in rows:
        # decision columns
        for k in ("eval_decision", "label_decision", "final_decision", "adjustment"):
            assert r[k]
        assert r["driver_source"] in ("catalyst", "last30days", "mixed", "none")
        # catalyst evidence (ran in mock -> non-null status + recorded source)
        assert r["catalyst_status"] is not None
        assert r["catalyst_source"] == "mock"
        # last30days / sentiment evidence
        assert r["last30days_status"] is not None
        assert r["sentiment_label"] is not None
        # complete reconstructable snapshot
        ev = json.loads(r["evidence_json"])
        assert "catalyst" in ev and "last30days" in ev
        assert "source" in ev["catalyst"] and "timestamp_utc" in ev["catalyst"]
        assert "sentiment_label" in ev["last30days"] and "source_coverage" in ev["last30days"]
    o.close()


def test_enabling_override_in_mock_changes_nothing():
    """Inert-while-mock: turning the flag on must not add a single proposal."""
    off = _orch(LABELLER_DECISION_OVERRIDE_ENABLED="false").run_scan_once().proposed
    on = _orch(LABELLER_DECISION_OVERRIDE_ENABLED="true").run_scan_once().proposed
    assert on == off


def _propose_label(packet):
    from alphaos.util.ids import new_id
    return PlaybookClassification(
        label_id=new_id("lbl"), candidate_id=packet.candidate_id, symbol=packet.symbol,
        primary_label="Momentum", secondary_labels=[], candidate_tags=[], risk_tags=[],
        direction="long", label_decision=Decision.PROPOSE.value, confidence=0.9,
        reason_for_label="forced propose", thesis_stub="", invalidation="", main_risk="",
        missing_context=[], suggested_new_tags=[], label_version=LABEL_VERSION_V1,
        label_source="mock", validation_status="passed", model="mock", is_mock=True, raw={},
    )


def test_armed_upgrade_promotes_watch_to_propose_with_audit(monkeypatch):
    """With arming + a real driver forced, a PROPOSE label upgrades a WATCH eval —
    and the move is journaled as 'upgraded' with its driver. Still no execution.

    Direct construction (§H.1): a previous version ran a full mock scan and
    asserted the organic shortlist contained an upgradeable WATCH candidate —
    date-flaky (broke on the 2026-07-06 seed, third bite of this class). The
    candidate is now hand-built; every audit-trail assertion is unchanged.
    (The old organic-scan version also asserted candidate_labels stay official
    — meaningless under direct construction since the labeller never runs, and
    already covered by test_scan_records_one_adjustment_per_labelled_candidate's
    organic scan plus the classifier's own whitelist coercion tests.)"""
    o = _orch()
    cand, evaluation, classification = _watch_ready_candidate(o)
    # Stash a real-shaped last30days enrichment on the candidate: evidence_json
    # is snapshotted from the ACTUAL cand.last30 object (never from the
    # driver tuple), so the audit-trail assertion below stays meaningful.
    cand.last30 = _l30(sentiment="bullish")
    monkeypatch.setattr(o, "_override_armed", lambda: True)            # arm
    monkeypatch.setattr(o, "_real_decision_driver",
                        lambda catalyst, last30, direction, polarity=None:
                        (True, "last30days:bullish", {"last30days": {"sentiment": "bullish"}}))
    summary = ScanSummary(scan_id="audit-test")
    final = o._resolve_decision(cand, evaluation, classification, "audit-batch", summary)

    assert final == Decision.PROPOSE.value
    assert summary.decision_upgraded > 0
    ups = o.journal.query("SELECT * FROM decision_adjustments WHERE adjustment = ?",
                          (DecisionAdjustment.UPGRADED.value,))
    assert ups
    import json
    for r in ups:
        assert r["eval_decision"] == Decision.WATCH.value
        assert r["final_decision"] == Decision.PROPOSE.value
        assert r["override_armed"] == 1
        assert r["driver"] == "last30days:bullish"          # driver recorded for learning
        assert r["driver_source"] == "last30days"           # categorical source recorded
        ev = json.loads(r["evidence_json"])                 # evidence snapshot present + parseable
        assert ev and "last30days" in ev
    tagged = o.journal.query("SELECT * FROM candidates WHERE decision_adjustment = ?",
                             (DecisionAdjustment.UPGRADED.value,))
    assert tagged and all(t["decision_adjustment_reason"] for t in tagged)

    # SAFETY: the upgraded decision becomes at most a pending proposal; it
    # never auto-executes and never bypasses approval.
    o._handle_proposal(cand, evaluation, summary, scan_batch_id="audit-batch")
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_rows("paper_fills") == 0
    assert o.journal.count_open_positions() == 0
    assert o.journal.count_rows("approvals") == 0
    o.close()


def _watch_ready_candidate(orch, symbol="ZWATCH"):
    """A single hand-built candidate whose eval decision is WATCH, with FIXED
    (not mock-scanner-random) entry/stop/target -- schema-valid and guaranteed
    to clear risk sizing regardless of the mock market data's date seed.
    Whether it ends up upgraded is then decided ONLY by the override
    mechanism under test, not by whether today's mock scan happens to
    independently produce a WATCH candidate at all.

    ``ScanContext.snapshot`` carries ``market_session='regular'`` exactly like
    every real MockDataProvider snapshot does: ``_stamp_proposal_ttl`` reads
    the session from the snapshot, and an empty snapshot would fall back to
    the REAL wall-clock session -- outside US market hours that is CLOSED,
    whose TTL is 0, so the created proposal would be born-expired and any
    status assertion would depend on WHEN the test suite runs (§H.1)."""
    cand_id = new_id("cand")
    orch.journal.insert("candidates", {
        "candidate_id": cand_id, "symbol": symbol, "direction": "long", "strategy": "swing",
        "momentum_score": 0.5, "news_status": "available", "status": "watch",
    })
    evaluation = OpenAIEvaluation(
        eval_id=new_id("eval"), candidate_id=cand_id, symbol=symbol, model="mock",
        direction="long", entry=100.0, stop=97.0, target=106.0, max_holding_days=3,
        expected_r=2.0, confidence=0.6, decision=Decision.WATCH.value,
        reasoning_summary="test fixture", data_freshness_status="usable", is_mock=True,
    )
    classification = _propose_label(SimpleNamespace(candidate_id=cand_id, symbol=symbol))
    cand = ScanContext(row={"candidate_id": cand_id, "symbol": symbol, "last_price": 100.0})
    cand.snapshot = {"symbol": symbol, "last_price": 100.0, "market_session": "regular"}
    return cand, evaluation, classification


def test_armed_upgrade_increases_proposals_vs_downgrade_only(monkeypatch):
    """Concrete proof the upgrade path can CREATE a proposal the downgrade-only
    path would not — the capability the user asked for.

    Directly constructs a WATCH candidate (see _watch_ready_candidate) and
    calls the orchestrator's own decision + proposal-creation methods on it,
    exactly as run_scan_once()'s loop would, instead of hoping a full mock
    scan happens to (a) produce a WATCH-decision candidate that day at all,
    and (b) have the risk engine also approve its date-seeded sizing. Both
    were unrelated, date-seeded coin flips this test previously depended on
    without pinning down: it broke outright on a date where every scan-
    produced WATCH candidate was risk-blocked, and fuzzing across simulated
    dates surfaced a second, rarer failure mode -- a date where the labelled
    shortlist contained zero WATCH candidates at all, so there was nothing
    for either version of the assertion to upgrade.
    """
    base = _orch()
    cand, evaluation, classification = _watch_ready_candidate(base)
    summary_floor = ScanSummary(scan_id="floor-test")
    final_floor = base._resolve_decision(cand, evaluation, classification, "floor-batch", summary_floor)
    assert final_floor == Decision.WATCH.value     # downgrade-only: PROPOSE label can't lift WATCH
    # final_floor == WATCH, so run_scan_once()'s loop would `continue` here --
    # never reaching _handle_proposal. No proposal row for this candidate.
    proposals_floor = base.journal.count_rows("trade_proposals")
    base.close()

    armed = _orch()
    cand2, evaluation2, classification2 = _watch_ready_candidate(armed)
    monkeypatch.setattr(armed, "_override_armed", lambda: True)
    monkeypatch.setattr(armed, "_real_decision_driver",
                        lambda catalyst, last30, direction, polarity=None:
                        (True, "catalyst:confirmed:product_launch", {"catalyst": {"status": "confirmed"}}))
    summary_armed = ScanSummary(scan_id="armed-test")
    final_armed = armed._resolve_decision(cand2, evaluation2, classification2, "armed-batch", summary_armed)
    assert final_armed == Decision.PROPOSE.value   # armed: the override upgraded it
    armed._handle_proposal(cand2, evaluation2, summary_armed, scan_batch_id="armed-batch")
    proposals_armed = armed.journal.count_rows("trade_proposals")
    armed.close()

    assert proposals_armed > proposals_floor


def test_real_money_unreachable_with_override_enabled():
    o = _orch(LABELLER_DECISION_OVERRIDE_ENABLED="true")
    assert o.system_health()["real_money_trading"] == "unreachable"
    o.close()


def test_dashboard_readonly_with_adjustments(monkeypatch):
    import sys

    sys.path.insert(0, "tests")
    from test_approval_execution import _fake_st

    from alphaos.dashboard import streamlit_app

    o = _orch()
    monkeypatch.setattr(o.labeller, "classify", _propose_label)
    monkeypatch.setattr(o, "_override_armed", lambda: True)
    monkeypatch.setattr(o, "_real_decision_driver",
                        lambda catalyst, last30, direction, polarity=None: (True, "forced", {}))
    o.run_scan_once()
    j = o.journal
    assert j.count_rows("decision_adjustments") > 0
    tabs = [r["name"] for r in j.query("SELECT name FROM sqlite_master WHERE type='table'")]
    before = sum(j.count_rows(t) for t in tabs)
    monkeypatch.setattr(streamlit_app, "st", _fake_st())
    streamlit_app.main(orch=o)
    after = sum(j.count_rows(t) for t in tabs)
    assert after == before
    o.close()
