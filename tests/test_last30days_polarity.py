"""LLM-derived last30days narrative polarity (Roadmap 2.7).

Covers the deterministic arming safety logic, the mock keyword classifier, the
fail-safe paths, and end-to-end integration: aligned high-confidence polarity can
arm an upgrade (normal_driver), hype/meme/squeeze arms only as high_risk_narrative
(manual-only + warned), and nothing bypasses gates / approval / execution.
Hermetic — no real API (the live path is monkeypatched / mock).
"""

from __future__ import annotations

import pathlib

from alphaos.ai.last30days_polarity import (
    Last30DaysPolarityClassifier,
    PolarityEvidence,
    PolarityResult,
)
from alphaos.ai.playbook_classifier import PlaybookClassification
from alphaos.constants import (
    LABEL_VERSION_V1,
    ArmingClassification,
    Decision,
    DirectionAlignment,
    POLARITY_PROMPT_VERSION,
    PolarityParseStatus,
)
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.research.last30days_provider import Last30DaysResult
from conftest import make_settings


# --------------------------------------------------------------- helpers
def _settings(**over):
    base = {"LAST30DAYS_ENABLED": "true", "LAST30DAYS_PROVIDER": "mock",
            "LAST30DAYS_POLARITY_ENABLED": "true", "LAST30DAYS_POLARITY_ARMING_ALLOWED": "true"}
    base.update(over)
    return make_settings(**base)


def _parsed(**over):
    d = {"sentiment_label": "bullish", "sentiment_score": 0.6, "confidence": 0.8,
         "source_coverage_quality": "high", "narrative_driver_type": "fundamental",
         "hype_or_manipulation_risk": "none", "official_catalyst_conflict": False}
    d.update(over)
    return d


def _clf(**over):
    return Last30DaysPolarityClassifier(_settings(**over))


def _ev(symbol="AAPL", direction="long", titles=None):
    titles = ["AAPL strong quarter"] if titles is None else titles
    return PolarityEvidence(
        candidate_id="c1", symbol=symbol, direction=direction, structure_hint="trend",
        provider="cli", cluster_titles=titles, cluster_summaries=["s"],
        source_coverage=["reddit", "hackernews"], source_coverage_count=2,
        catalyst_summary=None, eval_decision=None, label_decision="watch",
    )


# ============================================================ deterministic arming
def test_alignment_mapping():
    c = _clf()
    assert c._alignment("bullish", "long") == DirectionAlignment.ALIGNED.value
    assert c._alignment("bullish", "short") == DirectionAlignment.CONFLICTING.value
    assert c._alignment("bearish", "short") == DirectionAlignment.ALIGNED.value
    assert c._alignment("bearish", "long") == DirectionAlignment.CONFLICTING.value
    assert c._alignment("neutral", "long") == DirectionAlignment.NEUTRAL.value
    assert c._alignment("unclear", "long") == DirectionAlignment.UNCLEAR.value
    assert c._alignment("bullish", "unknown") == DirectionAlignment.UNCLEAR.value


def test_bullish_aligned_arms_normal_driver():
    align, arm, cls = _clf()._decide_arming(_parsed(), "long")
    assert align == DirectionAlignment.ALIGNED.value
    assert arm is True
    assert cls == ArmingClassification.NORMAL_DRIVER.value


def test_bearish_aligned_arms_for_short():
    align, arm, cls = _clf()._decide_arming(_parsed(sentiment_label="bearish"), "short")
    assert arm is True and cls == ArmingClassification.NORMAL_DRIVER.value


def test_conflicting_does_not_arm():
    _, arm, cls = _clf()._decide_arming(_parsed(sentiment_label="bearish"), "long")
    assert arm is False and cls == ArmingClassification.NON_ARMING.value


def test_neutral_and_unclear_do_not_arm():
    for s in ("neutral", "unclear"):
        _, arm, cls = _clf()._decide_arming(_parsed(sentiment_label=s), "long")
        assert arm is False and cls == ArmingClassification.NON_ARMING.value


def test_low_confidence_does_not_arm():
    _, arm, _ = _clf(LAST30DAYS_POLARITY_MIN_CONFIDENCE="0.65")._decide_arming(_parsed(confidence=0.5), "long")
    assert arm is False


def test_low_source_coverage_does_not_arm():
    _, arm, _ = _clf()._decide_arming(_parsed(source_coverage_quality="low"), "long")
    assert arm is False


def test_official_catalyst_conflict_prevents_arming():
    _, arm, _ = _clf()._decide_arming(_parsed(official_catalyst_conflict=True), "long")
    assert arm is False


def test_hype_meme_squeeze_arms_only_as_high_risk():
    # social/meme/squeeze driver -> still arms (aligned) but classified HIGH-RISK
    for drv in ("social_momentum", "meme_hype", "squeeze_risk"):
        _, arm, cls = _clf()._decide_arming(_parsed(narrative_driver_type=drv), "long")
        assert arm is True and cls == ArmingClassification.HIGH_RISK_NARRATIVE.value
    # high hype risk on an otherwise-fundamental driver also -> high-risk
    _, arm, cls = _clf()._decide_arming(_parsed(hype_or_manipulation_risk="high"), "long")
    assert arm is True and cls == ArmingClassification.HIGH_RISK_NARRATIVE.value


def test_arming_disabled_when_flag_off():
    _, arm, _ = _clf(LAST30DAYS_POLARITY_ARMING_ALLOWED="false")._decide_arming(_parsed(), "long")
    assert arm is False


# ============================================================ classify() guards
def test_classify_skipped_when_disabled():
    r = _clf(LAST30DAYS_POLARITY_ENABLED="false").classify(_ev())
    assert r.parse_status == PolarityParseStatus.SKIPPED.value
    assert r.should_arm_override is False


def test_classify_skipped_when_no_evidence():
    r = _clf().classify(_ev(titles=[]))
    assert r.parse_status == PolarityParseStatus.SKIPPED.value
    assert r.should_arm_override is False


def test_classify_fail_safe_on_model_error(monkeypatch):
    c = _clf()
    c.use_mock = False  # force the live path...

    def boom(ev):
        raise RuntimeError("api down")

    monkeypatch.setattr(c, "_live_classify", boom)
    r = c.classify(_ev())               # must NOT raise
    assert r.parse_status == PolarityParseStatus.MODEL_ERROR.value
    assert r.sentiment_label == "unclear" and r.should_arm_override is False


def test_coerce_clamps_garbage_to_safe_unclear():
    parsed = _clf()._coerce({"sentiment_label": "MOON", "confidence": 9.9,
                             "source_coverage_quality": "lol", "narrative_driver_type": "??",
                             "hype_or_manipulation_risk": "??"})
    assert parsed["sentiment_label"] == "unclear"
    assert parsed["confidence"] == 1.0                 # clamped to [0,1]
    assert parsed["source_coverage_quality"] == "low"
    assert parsed["narrative_driver_type"] == "unclear"


# ============================================================ mock classifier
def test_mock_classifier_detects_bullish_and_hype():
    r = _clf().classify(_ev(titles=["AAPL 🚀 to the moon squeeze", "AAPL undervalued buy"]))
    assert r.sentiment_label == "bullish"
    assert r.arming_classification == ArmingClassification.HIGH_RISK_NARRATIVE.value  # hype detected


def test_mock_classifier_detects_bearish():
    r = _clf().classify(_ev(symbol="XYZ", direction="short",
                            titles=["XYZ lawsuit and downgrade", "XYZ crash fears, sell"]))
    assert r.sentiment_label == "bearish"
    assert r.direction_alignment == DirectionAlignment.ALIGNED.value


# ============================================================ source-level safety
def test_real_ai_calls_use_max_completion_tokens_not_max_tokens():
    for f in ("alphaos/ai/playbook_classifier.py", "alphaos/ai/last30days_polarity.py",
              "alphaos/ai/openai_client.py"):
        txt = pathlib.Path(f).read_text(encoding="utf-8")
        assert "max_tokens=" not in txt, f"{f} must not pass max_tokens to OpenAI"
    assert "max_completion_tokens" in pathlib.Path("alphaos/ai/playbook_classifier.py").read_text()
    assert "max_completion_tokens" in pathlib.Path("alphaos/ai/last30days_polarity.py").read_text()


# ============================================================ integration
def _polarity_for(ev, should_arm, arming, sentiment="bullish", warning=""):
    return PolarityResult(
        candidate_id=ev.candidate_id, symbol=ev.symbol, provider="cli", model="gpt-5.4-mini",
        prompt_template_version=POLARITY_PROMPT_VERSION, sentiment_label=sentiment,
        sentiment_score=0.7, confidence=0.85, direction_alignment=DirectionAlignment.ALIGNED.value,
        source_coverage_quality="high", narrative_driver_type="fundamental",
        hype_or_manipulation_risk="none", requires_user_attention=bool(warning),
        official_catalyst_conflict=False, should_arm_override=should_arm,
        arming_classification=arming, warning_message=warning, reasoning_summary="test",
        evidence_items=[], raw_json={"mock": True}, parse_status=PolarityParseStatus.PARSED.value,
    )


def _always_available_l30():
    class _P:
        name = "cli"

        def get_research_for_symbol(self, symbol, query):
            return Last30DaysResult(
                symbol=symbol, query=query,
                clusters=[{"title": f"{symbol} bullish", "score": 40.0, "sources": ["reddit", "hackernews"]}],
                item_count=6, sources_used=["hackernews", "reddit"], newest_age_hours=12.0,
                sentiment_hint="bullish", provider="cli")

    from alphaos.research.last30days_enricher import Last30DaysEnricher
    return _P, Last30DaysEnricher


def _propose_label(p):
    from alphaos.util.ids import new_id
    return PlaybookClassification(
        label_id=new_id("lbl"), candidate_id=p.candidate_id, symbol=p.symbol, primary_label="Momentum",
        secondary_labels=[], candidate_tags=[], risk_tags=[], direction="long",
        label_decision=Decision.PROPOSE.value, confidence=0.9, reason_for_label="x", thesis_stub="",
        invalidation="", main_risk="", missing_context=[], suggested_new_tags=[],
        label_version=LABEL_VERSION_V1, label_source="mock", validation_status="passed",
        model="mock", is_mock=True, raw={})


def _armed_orch(monkeypatch, arming, warning="", min_conf="0.6"):
    """Orchestrator wired so EVERY shortlisted long candidate gets an available
    last30days + a chosen polarity result + a PROPOSE label + a WATCH eval, with the
    override armed — so polarity drives a visible upgrade."""
    s = make_settings(LAST30DAYS_ENABLED="true", LAST30DAYS_PROVIDER="mock",
                      LAST30DAYS_POLARITY_ENABLED="true", LAST30DAYS_POLARITY_ARMING_ALLOWED="true",
                      LABELLER_DECISION_OVERRIDE_ENABLED="true",
                      INTEREST_SCAN_TOP_N="6", MAX_CANDIDATES_TO_AI="6",
                      LAST30DAYS_MAX_SYMBOLS_PER_SCAN="6")
    o = Orchestrator(settings=s, journal=JournalStore(":memory:"))
    _P, Enricher = _always_available_l30()
    o.l30_enricher = Enricher(s, o.journal, provider=_P())
    monkeypatch.setattr(o.polarity, "classify",
                        lambda ev: _polarity_for(ev, should_arm=(arming != "non_arming"),
                                                 arming=arming, warning=warning))
    monkeypatch.setattr(o.labeller, "classify", _propose_label)
    monkeypatch.setattr(o, "_override_armed", lambda: True)
    _orig = o.openai.evaluate

    def watch_long(cand, snap, freshness_status="usable"):
        ev = _orig(cand, snap, freshness_status)
        if (ev.direction or "long") != "short":
            ev.decision = Decision.WATCH.value
        return ev

    monkeypatch.setattr(o.openai, "evaluate", watch_long)
    return o


def test_aligned_normal_polarity_arms_upgrade(monkeypatch):
    o = _armed_orch(monkeypatch, ArmingClassification.NORMAL_DRIVER.value)
    summ = o.run_scan_once()
    assert summ.decision_upgraded > 0
    assert o.journal.count_rows("last30days_polarity") > 0
    ups = o.journal.query("SELECT * FROM decision_adjustments WHERE adjustment='upgraded'")
    assert ups and all(r["final_decision"] == Decision.PROPOSE.value for r in ups)
    o.close()


def test_high_risk_narrative_arms_but_is_manual_only_and_warned(monkeypatch):
    o = _armed_orch(monkeypatch, ArmingClassification.HIGH_RISK_NARRATIVE.value,
                    warning="hype/squeeze risk")
    summ = o.run_scan_once()
    assert summ.decision_upgraded > 0
    assert summ.high_risk_narrative > 0
    # high-risk proposals are manual-only: never auto-submitted/approved/executed
    assert summ.auto_submitted == 0
    assert o.journal.count_rows("approvals") == 0
    assert o.journal.count_rows("paper_orders") == 0
    # the proposal carries the classification + warning for the user
    props = o.journal.query(
        "SELECT * FROM trade_proposals WHERE arming_classification = ?",
        (ArmingClassification.HIGH_RISK_NARRATIVE.value,))
    assert props and all(p["narrative_warning"] for p in props)
    o.close()


def test_non_arming_polarity_leaves_decision_unchanged(monkeypatch):
    o = _armed_orch(monkeypatch, ArmingClassification.NON_ARMING.value)
    summ = o.run_scan_once()
    assert summ.decision_upgraded == 0          # polarity present but not arming
    assert o.journal.count_rows("last30days_polarity") > 0
    o.close()


def test_polarity_never_executes_or_approves(monkeypatch):
    o = _armed_orch(monkeypatch, ArmingClassification.NORMAL_DRIVER.value)
    o.run_scan_once()
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_rows("paper_fills") == 0
    assert o.journal.count_open_positions() == 0
    assert o.journal.count_rows("approvals") == 0        # manual approval still required
    assert o.system_health()["manual_approval"] == "required"
    assert o.system_health()["real_money_trading"] == "unreachable"
    o.close()


def test_polarity_disabled_by_default_writes_no_rows():
    o = Orchestrator(settings=make_settings(LAST30DAYS_ENABLED="true"), journal=JournalStore(":memory:"))
    o.run_scan_once()
    assert o.journal.count_rows("last30days_polarity") == 0   # disabled by default
    o.close()


def test_polarity_skipped_when_last30days_not_available(monkeypatch):
    # last30days disabled -> no available context -> polarity never runs
    o = Orchestrator(settings=make_settings(LAST30DAYS_ENABLED="false",
                                            LAST30DAYS_POLARITY_ENABLED="true"),
                     journal=JournalStore(":memory:"))
    o.run_scan_once()
    assert o.journal.count_rows("last30days_polarity") == 0
    o.close()
