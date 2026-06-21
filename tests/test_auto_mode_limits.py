"""Auto mode respects the max daily auto-approval cap and never bypasses gates (test #9)."""

from __future__ import annotations

from alphaos.approval import ApprovalEngine
from alphaos.constants import ApprovalLabel, ReasonCode
from conftest import make_settings, make_proposal


def test_auto_approves_up_to_cap_then_denies(journal):
    s = make_settings(APPROVAL_MODE="auto", REQUIRE_MANUAL_APPROVAL="false", MAX_AUTO_APPROVALS_PER_DAY="1")
    eng = ApprovalEngine(s, journal)

    first = eng.consider(make_proposal(symbol="AAPL"), risk_ok=True, freshness_ok=True)
    assert first.approved is True
    assert first.label == ApprovalLabel.AUTO_APPROVED.value

    second = eng.consider(make_proposal(symbol="MSFT"), risk_ok=True, freshness_ok=True)
    assert second.approved is False
    assert second.reason == ReasonCode.AUTO_APPROVAL_LIMIT.value
    assert journal.count_auto_approvals_today() == 1


def test_auto_never_bypasses_risk_or_freshness(journal):
    s = make_settings(APPROVAL_MODE="auto", REQUIRE_MANUAL_APPROVAL="false", MAX_AUTO_APPROVALS_PER_DAY="5")
    eng = ApprovalEngine(s, journal)

    r = eng.consider(make_proposal(), risk_ok=False, freshness_ok=True)
    assert r.approved is False and r.reason == ReasonCode.RISK_OVERSIZED.value

    f = eng.consider(make_proposal(), risk_ok=True, freshness_ok=False)
    assert f.approved is False and f.reason == ReasonCode.STALE_DATA.value
    # Nothing was approved.
    assert journal.count_auto_approvals_today() == 0


def test_auto_cannot_enable_margin_short(journal):
    s = make_settings(APPROVAL_MODE="auto", REQUIRE_MANUAL_APPROVAL="false", MAX_AUTO_APPROVALS_PER_DAY="5")
    eng = ApprovalEngine(s, journal)
    prop = make_proposal(direction="short", entry=100.0, stop=103.0, target=94.0, requires_margin=True)
    out = eng.consider(prop, risk_ok=True, freshness_ok=True)
    assert out.approved is False
    assert out.reason == ReasonCode.MARGIN_APPROVAL_REQUIRED.value
