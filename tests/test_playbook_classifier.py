"""AI Category/Playbook Classifier (Roadmap 2.3) — mock determinism, official-label
enforcement, fail-safe, no-auto-propose rules."""

from __future__ import annotations

from alphaos.ai.label_validation import coerce_and_validate
from alphaos.ai.playbook_classifier import PlaybookClassifier
from alphaos.config.settings import load_settings
from alphaos.constants import Decision, LABEL_OTHER, OFFICIAL_LABELS
from alphaos.scanner.candidate_packet import CandidatePacket


def _s(**over):
    return load_settings(load_env_file=False, env={"ALPHAOS_MODE": "mock", **over})


def _pkt(momentum=0.7, structure="trend", direction="long"):
    return CandidatePacket(
        packet_id="p", candidate_id="c", symbol="AAPL", last_price=100.0, direction=direction,
        freshness_status="usable", spread_pct=0.001, liquidity_ok=True, dollar_volume=5e9,
        change_pct=0.04, rel_volume=1.8, rel_strength_vs_spy=0.03, rel_strength_vs_qqq=0.02,
        near_day_high=False, near_day_low=False, gap_pct=0.01, structure_hint=structure,
        setup_hint="x", tradeable_volatility=True, interest_score=0.7, interest_rank=1,
        shortlist_reason="r", momentum_score=momentum, missing_data_flags=[],
    )


# ----------------------------------------------------------------- mock engine
def test_mock_is_deterministic():
    c = PlaybookClassifier(_s())
    a, b = c.classify(_pkt()), c.classify(_pkt())
    assert (a.primary_label, a.label_decision, a.confidence) == (b.primary_label, b.label_decision, b.confidence)


def test_mock_momentum_proposes_with_official_label():
    out = PlaybookClassifier(_s()).classify(_pkt(momentum=0.7, structure="trend"))
    assert out.primary_label == "Momentum" and out.primary_label in OFFICIAL_LABELS
    assert out.label_decision == Decision.PROPOSE.value
    assert out.label_source == "mock" and out.is_mock is True


def test_mock_weak_is_other_and_watch():
    out = PlaybookClassifier(_s()).classify(_pkt(momentum=0.1, structure="range"))
    assert out.primary_label == LABEL_OTHER
    assert out.label_decision == Decision.WATCH.value


# ------------------------------------------------------- validation / coercion
def test_invalid_primary_label_coerced_to_other_watch():
    clean, status = coerce_and_validate(
        {"primary_label": "SuperBreakout", "decision": "propose", "confidence": 0.9}, _s()
    )
    assert clean["primary_label"] == LABEL_OTHER and clean["decision"] == Decision.WATCH.value
    assert status == "invalid_label"
    assert "SuperBreakout" in clean["candidate_tags"]  # kept UNOFFICIAL


def test_other_unclassified_never_auto_proposes():
    clean, status = coerce_and_validate(
        {"primary_label": LABEL_OTHER, "decision": "propose", "confidence": 0.99}, _s()
    )
    assert clean["decision"] == Decision.WATCH.value and status == "other_downgraded"


def test_low_confidence_cannot_propose():
    clean, status = coerce_and_validate(
        {"primary_label": "Momentum", "decision": "propose", "confidence": 0.5},
        _s(LABEL_MIN_CONFIDENCE_TO_PROPOSE="0.8"),
    )
    assert clean["decision"] == Decision.WATCH.value and status == "low_confidence"


def test_nonofficial_secondary_labels_become_unofficial_tags():
    clean, _ = coerce_and_validate(
        {"primary_label": "Momentum", "secondary_labels": ["Breakout", "MyTag"],
         "decision": "watch", "confidence": 0.5}, _s()
    )
    assert "Breakout" in clean["secondary_labels"] and "MyTag" not in clean["secondary_labels"]
    assert "MyTag" in clean["candidate_tags"]


def test_malformed_decision_defaults_to_watch():
    clean, status = coerce_and_validate(
        {"primary_label": "Momentum", "decision": "garbage", "confidence": 0.9}, _s()
    )
    assert clean["decision"] == Decision.WATCH.value and status == "missing_decision"


def test_live_exception_fails_safe_to_reject(monkeypatch):
    c = PlaybookClassifier(_s())
    c.use_mock = False  # force the live branch

    def boom(_packet):
        raise RuntimeError("api down")

    monkeypatch.setattr(c, "_live_classify", boom)
    out = c.classify(_pkt(momentum=0.9, structure="breakout"))
    assert out.primary_label == LABEL_OTHER
    assert out.label_decision == Decision.REJECT.value   # never PROPOSE on failure
    assert out.label_source == "fail_safe"
