"""Unattended close-window auto-approval (operator request, 2026-07-11).
NOT PR15/L3 -- a narrower, time-scoped door into the EXISTING, already-
shipped APPROVAL_MODE=auto engine (alphaos/approval.py), added because the
operator is asleep during the market-close scan window and cannot manually
approve within a proposal's ~30-minute TTL. Covers:

* ApprovalEngine.consider(unattended=True) -- approves within both the
  unattended-only cap AND the shared auto-approval cap; every existing
  gate (risk/freshness/daytrade/margin) still applies identically; manual
  mode with unattended=False is unaffected.
* journal_store's count_auto_approvals_today() now counts BOTH
  AUTO_APPROVED and UNATTENDED_APPROVED (one shared budget);
  count_unattended_approvals_today() counts only the latter.
* Orchestrator.run_scan_once()'s eligibility wiring: SCHEDULER trigger +
  wall-clock inside a configured window -> unattended=True threaded through
  to consider(); a manually-triggered scan at the exact same wall-clock
  time is NOT eligible (a human is already looking, no unattended door
  needed); a scheduler-triggered scan OUTSIDE any window is not eligible.
* The PR6 auto-path TTL guard (born-expired -> zero fills) holds
  identically on the unattended path.
* settings.py's parse/validation (empty=inert, malformed raises, the
  ET-vs-SGT misconfiguration WARNING).

All offline, in-memory, mock/paper mode. No real network/broker calls.
"""

from __future__ import annotations

from datetime import timedelta

from alphaos.approval import ApprovalEngine
from alphaos.constants import ApprovalLabel, ReasonCode
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.util import timeutils
from conftest import make_proposal, make_settings


def _orch(**over):
    return Orchestrator(settings=make_settings(**over), journal=JournalStore(":memory:"))


# --------------------------------------------------- ApprovalEngine.consider()
def test_unattended_approves_within_both_caps(journal):
    s = make_settings(
        APPROVAL_MODE="manual", MAX_AUTO_APPROVALS_PER_DAY="5", MAX_UNATTENDED_APPROVALS_PER_DAY="5",
    )
    eng = ApprovalEngine(s, journal)

    outcome = eng.consider(make_proposal(symbol="NVDA"), risk_ok=True, freshness_ok=True, unattended=True)

    assert outcome.approved is True
    assert outcome.label == ApprovalLabel.UNATTENDED_APPROVED.value
    assert journal.count_unattended_approvals_today() == 1
    assert journal.count_auto_approvals_today() == 1  # shared counter sees it too


def test_manual_mode_without_unattended_flag_still_pends(journal):
    """The whole point: APPROVAL_MODE stays 'manual' -- only unattended=True
    (computed once at scan start, never a global mode flip) opens the door."""
    s = make_settings(APPROVAL_MODE="manual")
    eng = ApprovalEngine(s, journal)

    outcome = eng.consider(make_proposal(), risk_ok=True, freshness_ok=True, unattended=False)

    assert outcome.approved is False
    assert outcome.status == "pending_manual"
    assert outcome.reason == ReasonCode.APPROVAL_REQUIRED.value


def test_unattended_still_denies_on_risk_or_freshness(journal):
    s = make_settings(APPROVAL_MODE="manual", MAX_UNATTENDED_APPROVALS_PER_DAY="5")
    eng = ApprovalEngine(s, journal)

    r = eng.consider(make_proposal(), risk_ok=False, freshness_ok=True, unattended=True)
    assert r.approved is False and r.reason == ReasonCode.RISK_OVERSIZED.value

    f = eng.consider(make_proposal(), risk_ok=True, freshness_ok=False, unattended=True)
    assert f.approved is False and f.reason == ReasonCode.STALE_DATA.value
    assert journal.count_unattended_approvals_today() == 0


def test_unattended_still_denies_daytrade_and_margin(journal):
    s = make_settings(APPROVAL_MODE="manual", MAX_UNATTENDED_APPROVALS_PER_DAY="5")
    eng = ApprovalEngine(s, journal)

    daytrade = make_proposal(strategy="daytrade_experiment")
    r1 = eng.consider(daytrade, risk_ok=True, freshness_ok=True, unattended=True)
    assert r1.approved is False and r1.reason == ReasonCode.DAYTRADE_GATED.value

    margin = make_proposal(direction="short", entry=100.0, stop=103.0, target=94.0, requires_margin=True)
    r2 = eng.consider(margin, risk_ok=True, freshness_ok=True, unattended=True)
    assert r2.approved is False and r2.reason == ReasonCode.MARGIN_APPROVAL_REQUIRED.value


def test_unattended_own_cap_denies_before_shared_cap_is_even_checked(journal):
    """MAX_UNATTENDED_APPROVALS_PER_DAY=1, MAX_AUTO_APPROVALS_PER_DAY=99 --
    the OWN cap trips first even though the shared cap has plenty of room."""
    s = make_settings(
        APPROVAL_MODE="manual", MAX_UNATTENDED_APPROVALS_PER_DAY="1", MAX_AUTO_APPROVALS_PER_DAY="99",
    )
    eng = ApprovalEngine(s, journal)

    first = eng.consider(make_proposal(symbol="AAPL"), risk_ok=True, freshness_ok=True, unattended=True)
    assert first.approved is True

    second = eng.consider(make_proposal(symbol="MSFT"), risk_ok=True, freshness_ok=True, unattended=True)
    assert second.approved is False
    assert second.reason == ReasonCode.AUTO_APPROVAL_LIMIT.value
    assert journal.count_unattended_approvals_today() == 1


def test_shared_cap_intersects_auto_and_unattended_labels(journal):
    """The SHARED cap (max_auto_approvals_per_day) counts AUTO_APPROVED and
    UNATTENDED_APPROVED TOGETHER -- an operator running global auto mode
    (however rare in practice) and the unattended window on the same day
    must not silently get 2x the intended daily budget."""
    s = make_settings(
        APPROVAL_MODE="auto", REQUIRE_MANUAL_APPROVAL="false",
        MAX_AUTO_APPROVALS_PER_DAY="1", MAX_UNATTENDED_APPROVALS_PER_DAY="5",
    )
    eng = ApprovalEngine(s, journal)

    first = eng.consider(make_proposal(symbol="AAPL"), risk_ok=True, freshness_ok=True, unattended=False)
    assert first.approved is True
    assert first.label == ApprovalLabel.AUTO_APPROVED.value

    second = eng.consider(make_proposal(symbol="MSFT"), risk_ok=True, freshness_ok=True, unattended=True)
    assert second.approved is False
    assert second.reason == ReasonCode.AUTO_APPROVAL_LIMIT.value  # shared cap, not the own cap


# --------------------------------------------------------- journal_store counters
def test_count_auto_approvals_today_counts_both_labels(journal):
    s = make_settings(APPROVAL_MODE="manual", MAX_UNATTENDED_APPROVALS_PER_DAY="5")
    eng = ApprovalEngine(s, journal)
    eng.consider(make_proposal(symbol="AAA"), risk_ok=True, freshness_ok=True, unattended=True)

    s2 = make_settings(APPROVAL_MODE="auto", REQUIRE_MANUAL_APPROVAL="false", MAX_AUTO_APPROVALS_PER_DAY="5")
    eng2 = ApprovalEngine(s2, journal)
    eng2.consider(make_proposal(symbol="BBB"), risk_ok=True, freshness_ok=True, unattended=False)

    assert journal.count_auto_approvals_today() == 2
    assert journal.count_unattended_approvals_today() == 1


# ------------------------------------------------------- run_scan_once wiring
def test_scheduler_scan_inside_window_is_unattended_eligible(monkeypatch):
    """SCHEDULER trigger + wall-clock inside a configured window ->
    unattended=True threaded all the way to an actual auto-submitted fill."""
    from datetime import datetime as _dt

    monkeypatch.setattr(
        "alphaos.scheduler.cadence.market_now_et", lambda now=None: _dt(2026, 7, 10, 15, 50),
    )
    o = _orch(
        UNATTENDED_APPROVE_WINDOWS="15:45-16:00", MAX_UNATTENDED_APPROVALS_PER_DAY="50",
        LABELLING_ENABLED="true", INTEREST_SCAN_TOP_N="6", MAX_CANDIDATES_TO_AI="6",
    )
    from alphaos.constants import TriggerSource

    summ = o.run_scan_once(trigger_source=TriggerSource.SCHEDULER.value)

    assert summ.auto_submitted > 0
    rows = o.journal.query("SELECT label FROM approvals WHERE label = 'UNATTENDED_APPROVED'")
    assert rows
    o.close()


def test_manual_cli_scan_at_the_same_wall_clock_is_not_unattended_eligible(monkeypatch):
    """The SAME wall-clock moment, but trigger_source defaults to
    manual_cli -- a human-triggered scan never gets the unattended door,
    since a human is already looking at the screen."""
    from datetime import datetime as _dt

    monkeypatch.setattr(
        "alphaos.scheduler.cadence.market_now_et", lambda now=None: _dt(2026, 7, 10, 15, 50),
    )
    o = _orch(
        UNATTENDED_APPROVE_WINDOWS="15:45-16:00", MAX_UNATTENDED_APPROVALS_PER_DAY="50",
        LABELLING_ENABLED="true", INTEREST_SCAN_TOP_N="6", MAX_CANDIDATES_TO_AI="6",
    )
    summ = o.run_scan_once()  # default trigger_source = manual_cli

    assert summ.auto_submitted == 0
    assert summ.pending_manual > 0
    assert o.journal.query("SELECT * FROM approvals WHERE label = 'UNATTENDED_APPROVED'") == []
    o.close()


def test_scheduler_scan_outside_any_window_is_not_unattended_eligible(monkeypatch):
    from datetime import datetime as _dt

    monkeypatch.setattr(
        "alphaos.scheduler.cadence.market_now_et", lambda now=None: _dt(2026, 7, 10, 11, 0),
    )
    o = _orch(
        UNATTENDED_APPROVE_WINDOWS="15:45-16:00", MAX_UNATTENDED_APPROVALS_PER_DAY="50",
        LABELLING_ENABLED="true", INTEREST_SCAN_TOP_N="6", MAX_CANDIDATES_TO_AI="6",
    )
    from alphaos.constants import TriggerSource

    summ = o.run_scan_once(trigger_source=TriggerSource.SCHEDULER.value)

    assert summ.auto_submitted == 0
    assert o.journal.query("SELECT * FROM approvals WHERE label = 'UNATTENDED_APPROVED'") == []
    o.close()


def test_no_unattended_windows_configured_is_never_eligible(monkeypatch):
    """The default (empty) config -- feature inert regardless of trigger
    source or wall-clock."""
    from datetime import datetime as _dt

    monkeypatch.setattr(
        "alphaos.scheduler.cadence.market_now_et", lambda now=None: _dt(2026, 7, 10, 15, 50),
    )
    o = _orch(LABELLING_ENABLED="true", INTEREST_SCAN_TOP_N="6", MAX_CANDIDATES_TO_AI="6")
    from alphaos.constants import TriggerSource

    summ = o.run_scan_once(trigger_source=TriggerSource.SCHEDULER.value)

    assert summ.auto_submitted == 0
    o.close()


def test_unattended_born_expired_proposal_never_fills(monkeypatch):
    """PR6's own auto-path TTL guard, re-verified on the unattended door:
    a proposal born already-expired must never auto-execute here either."""
    from datetime import datetime as _dt

    monkeypatch.setattr(
        "alphaos.scheduler.cadence.market_now_et", lambda now=None: _dt(2026, 7, 10, 15, 50),
    )
    o = _orch(
        UNATTENDED_APPROVE_WINDOWS="15:45-16:00", MAX_UNATTENDED_APPROVALS_PER_DAY="50",
        LABELLING_ENABLED="true", INTEREST_SCAN_TOP_N="6", MAX_CANDIDATES_TO_AI="6",
    )
    from alphaos.constants import TriggerSource

    def _born_expired(proposal, snapshot=None):
        proposal.proposal_ttl_seconds = 60
        proposal.proposal_expires_at_utc = timeutils.to_iso(timeutils.now_utc() - timedelta(hours=1))

    o._stamp_proposal_ttl = _born_expired
    summ = o.run_scan_once(trigger_source=TriggerSource.SCHEDULER.value)

    assert summ.auto_submitted == 0
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_open_positions() == 0
    expired = o.journal.query("SELECT * FROM trade_proposals WHERE status = 'expired'")
    assert expired
    o.close()


# --------------------------------------------- high-risk-narrative ordering
def test_high_risk_narrative_check_precedes_consider_in_source():
    """Structural proof (a full live high-risk-narrative scenario needs
    last30days polarity/arming fixtures well beyond this feature's own
    scope to set up): the HIGH_RISK_NARRATIVE manual-only check must sit
    BEFORE the consider() call in _handle_proposal's own source, so it
    binds the unattended door for free, with zero new code -- confirmed by
    source position, not just re-reading the (unmodified) surrounding
    comment."""
    import inspect

    from alphaos.orchestrator import Orchestrator

    source = inspect.getsource(Orchestrator._handle_proposal)
    narrative_pos = source.lower().index("high-risk narrative")
    consider_pos = source.index("self.approvals.consider(")
    assert narrative_pos < consider_pos


# ------------------------------------------------------------- settings.py
def test_unattended_windows_default_empty_and_inert():
    s = make_settings()
    assert s.unattended_approve_windows == ""
    assert all(c.ok for c in s.validate_startup() if c.name == "unattended_approve_windows_aligned")


def test_unattended_windows_malformed_raises():
    import pytest

    from alphaos.config.settings import SettingsError

    with pytest.raises(SettingsError):
        make_settings(UNATTENDED_APPROVE_WINDOWS="not-a-window")


def test_unattended_windows_misaligned_with_scan_windows_warns_not_raises():
    """The ET-vs-SGT misconfiguration trap (Fable5 review): a window that
    matches no scan-window start must WARN, never raise/block startup."""
    s = make_settings(UNATTENDED_APPROVE_WINDOWS="03:45-04:00")  # looks like an SGT time, not ET
    checks = [c for c in s.validate_startup() if c.name == "unattended_approve_windows_aligned"]
    assert len(checks) == 1
    assert checks[0].ok is False
    assert checks[0].severity.value == "warning"
    assert s.startup_ok() is True  # a WARNING must never fail startup_ok()


def test_unattended_windows_aligned_with_a_real_scan_window_passes():
    s = make_settings(UNATTENDED_APPROVE_WINDOWS="15:45-16:00")
    checks = [c for c in s.validate_startup() if c.name == "unattended_approve_windows_aligned"]
    assert checks[0].ok is True


# -------------------------------------------------------------- daily brief
def test_daily_brief_surfaces_unattended_approvals(journal):
    from alphaos.reports.daily_brief import _unattended_approvals_today

    s = make_settings(APPROVAL_MODE="manual", MAX_UNATTENDED_APPROVALS_PER_DAY="5")
    eng = ApprovalEngine(s, journal)
    eng.consider(make_proposal(symbol="NVDA"), risk_ok=True, freshness_ok=True, unattended=True)

    result = _unattended_approvals_today(journal, "2020-01-01T00:00:00+00:00")

    assert result == {"count": 1, "symbols": ["NVDA"]}


def test_daily_brief_omits_unattended_line_on_a_quiet_day(journal):
    from alphaos.reports.daily_brief import _unattended_approvals_today

    assert _unattended_approvals_today(journal, "2020-01-01T00:00:00+00:00") is None
