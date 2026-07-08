"""UI-PR-A dashboard tests: the annunciator strip, Tonight tab, Positions
tab, and the Approval Center / Candidate Flow enhancements. Hermetic -- mock
mode, no network, no real Streamlit process (reuses test_approval_execution's
_fake_st() to render the app headlessly, same as test_decision_override.py's
test_dashboard_readonly_with_adjustments already does -- one fixture, not a
second copy that can silently drift out of sync with the real `st` surface).

Two invariants matter most here: (1) the render path never writes to the
ledger (it's read-only, same as every other dashboard tab), and (2) the
annunciator's heartbeat check must never trigger JobRunner.heartbeat_check()'s
own alert side effect -- that method pages on staleness, which would fire on
every single dashboard page load if called from here.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from alphaos.dashboard import streamlit_app
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.util import timeutils
from alphaos.util.ids import new_id
from conftest import inject_pending_proposal, make_settings
from test_approval_execution import _fake_st


def _orch(**over):
    return Orchestrator(settings=make_settings(**over), journal=JournalStore(":memory:"))


def _open_position(journal, symbol="AAPL", entry=100.0, stop=97.0, target=106.0):
    position_id = new_id("pos")
    journal.insert("positions", {
        "position_id": position_id, "symbol": symbol, "direction": "long",
        "strategy": "swing", "qty": 10, "avg_entry_price": entry, "stop_price": stop,
        "target_price": target, "max_holding_days": 3,
        "opened_at": timeutils.to_iso(timeutils.now_utc()),
        "status": "open", "protection_status": "protected",
        "trade_id": new_id("trade"), "candidate_id": new_id("cand"), "proposal_id": new_id("prop"),
    })
    return position_id


def _open_incident(journal, position_id, symbol="AAPL"):
    journal.insert("protection_checks", {
        "check_id": new_id("chk"), "position_id": position_id, "symbol": symbol,
        "protection_status": "unprotected", "severity": "critical", "detail": "test incident",
    })


def _completed_job_run(journal, job_type="monitor", finished_at=None):
    finished_at = finished_at or timeutils.to_iso(timeutils.now_utc())
    journal.insert("job_runs", {
        "job_run_id": new_id("jr"), "job_type": job_type,
        "started_at_utc": finished_at, "started_at_sgt": finished_at,
        "finished_at_utc": finished_at, "finished_at_sgt": finished_at,
        "status": "completed",
    })


def _rejected_candidate_with_hindsight(journal, symbol="MSFT", delta_r=1.5):
    cand_id = new_id("cand")
    journal.insert("candidates", {
        "candidate_id": cand_id, "symbol": symbol, "direction": "long",
        "strategy": "swing", "status": "rejected",
    })
    journal.insert("rejected_candidates", {
        "rejection_id": new_id("rej"), "candidate_id": cand_id, "symbol": symbol,
        "stage": "openai", "reason_code": "reward_risk_too_low", "reason_detail": "test",
    })
    journal.insert("attribution_records", {
        "attribution_id": new_id("attr"), "attribution_type": "propose_user_rejected",
        "attribution_version": "v2", "agent": "system", "source_id": cand_id,
        "candidate_id": cand_id, "symbol": symbol, "resolved_status": "resolved",
        "delta_r": delta_r, "data_quality_status": "ok",
    })
    return cand_id


# ---------------------------------------------------------- pure functions
def test_hindsight_cell_pending_states_never_read_as_zero():
    assert streamlit_app._hindsight_cell(None) == "pending"
    assert streamlit_app._hindsight_cell({"resolved_status": "pending", "delta_r": None}) == "pending"
    # A malformed/incomplete row (resolved_status says resolved but delta_r
    # somehow missing) must still read as pending, never fabricate a "0.00R".
    assert streamlit_app._hindsight_cell({"resolved_status": "resolved", "delta_r": None}) == "pending"


def test_hindsight_cell_resolved_shows_signed_r():
    assert streamlit_app._hindsight_cell({"resolved_status": "resolved", "delta_r": 1.5}) == "+1.50R"
    assert streamlit_app._hindsight_cell({"resolved_status": "resolved", "delta_r": -0.75}) == "-0.75R"
    # A genuine measured zero IS shown (distinct from an unresolved "pending").
    assert streamlit_app._hindsight_cell({"resolved_status": "resolved", "delta_r": 0.0}) == "+0.00R"


def test_hindsight_cell_tags_mock_delta_r():
    """A simulated (is_mock) ΔR must never be styled identically to a real
    one -- UI/UX doc §1.4 evidence-state honesty. Production paper mode never
    produces is_mock=1 attribution rows, but mock-mode dev runs do."""
    real = streamlit_app._hindsight_cell({"resolved_status": "resolved", "delta_r": 1.2, "is_mock": 0})
    mock = streamlit_app._hindsight_cell({"resolved_status": "resolved", "delta_r": 1.2, "is_mock": 1})
    assert real == "+1.20R"
    assert mock == "+1.20R (mock)"


def test_format_age():
    assert streamlit_app._format_age(None) == "unknown"
    assert streamlit_app._format_age(30) == "30s"
    assert streamlit_app._format_age(125) == "2m"
    assert streamlit_app._format_age(7200) == "2.0h"


def test_heartbeat_age_seconds_no_runs_yet():
    j = JournalStore(":memory:")
    assert streamlit_app._heartbeat_age_seconds(j) is None


def test_heartbeat_age_seconds_reads_latest_completed_run():
    j = JournalStore(":memory:")
    finished = timeutils.to_iso(timeutils.now_utc() - timedelta(minutes=5))
    _completed_job_run(j, finished_at=finished)
    age = streamlit_app._heartbeat_age_seconds(j)
    assert age is not None
    assert 290 <= age <= 310  # ~5 minutes, generous tolerance for test wall-clock


# ----------------------------------------------------- read-only invariant
def test_heartbeat_check_never_called_from_dashboard_render(monkeypatch):
    """The exact bug this PR avoids: JobRunner.heartbeat_check() sends an
    alert on a stale heartbeat. If the annunciator ever called it directly
    instead of doing its own read-only query, a stale scheduler would cause
    every single dashboard page load to fire a duplicate alert."""
    from alphaos.util import alerts

    sent = []
    monkeypatch.setattr(alerts, "send_alert", lambda *a, **kw: sent.append((a, kw)))

    orch = _orch()
    # A very stale (long ago) completed job -- exactly the state that WOULD
    # trigger JobRunner.heartbeat_check()'s alert if that method were called.
    stale = timeutils.to_iso(timeutils.now_utc() - timedelta(hours=6))
    _completed_job_run(orch.journal, finished_at=stale)

    monkeypatch.setattr(streamlit_app, "st", _fake_st())
    streamlit_app.main(orch=orch)

    assert sent == [], f"dashboard render must never send an alert, got: {sent}"
    orch.close()


def test_dashboard_render_writes_nothing_with_populated_state(monkeypatch):
    """test_approval_execution.py's test_dashboard_render_writes_nothing
    proves this on an EMPTY journal. This proves the same invariant with
    every UI-PR-A code path actually exercised: an open position (annunciator
    open-R, Positions tab), an EXIT_REVIEW position (incident-driven), a
    pending proposal with a real invalidation_reason (Approval Center exit-
    plan block, TTL sort), and a rejected candidate with resolved attribution
    (Candidate Flow hindsight column)."""
    orch = _orch()
    j = orch.journal

    _open_position(j, symbol="AAPL")
    exit_review_pos_id = _open_position(j, symbol="TSLA")
    _open_incident(j, exit_review_pos_id, symbol="TSLA")  # -> BROKEN thesis, EXIT_REVIEW verdict
    inject_pending_proposal(orch, symbol="NVDA")  # has invalidation_reason via with_card=True
    _rejected_candidate_with_hindsight(j, symbol="MSFT", delta_r=1.5)
    _rejected_candidate_with_hindsight(j, symbol="AMD", delta_r=-0.8)

    # system_events is watched separately below: build_daily_brief() (called
    # by the new Tonight tab) legitimately logs a bounded, one-time-per-
    # MarketDataClient-instance "market data is mocked" WARNING the first
    # time it fetches a snapshot in mock mode -- pre-existing PR11 behavior
    # (daily_brief.py's own docstring already accepts the double-compute this
    # comes from), simply never exercised by a dashboard render before this
    # PR because no earlier tab called build_daily_brief()/assess_positions().
    watched = (
        "scan_batches", "scheduler_runs", "config_versions",
        "paper_orders", "paper_fills", "positions", "candidates", "trade_proposals",
        "rejected_candidates", "attribution_records", "job_runs", "protection_checks",
    )
    before = {t: j.count_rows(t) for t in watched}
    events_before_id = j.one("SELECT MAX(id) AS m FROM system_events")["m"] or 0

    monkeypatch.setattr(streamlit_app, "st", _fake_st())
    streamlit_app.main(orch=orch)   # one full render across every tab, zero user actions

    after = {t: j.count_rows(t) for t in watched}
    assert after == before, f"render wrote rows: before={before} after={after}"

    new_events = j.query("SELECT severity, category, message FROM system_events WHERE id > ?",
                         (events_before_id,))
    assert all(
        e["severity"] == "warning" and e["category"] == "market_data"
        and "mocked" in e["message"].lower()
        for e in new_events
    ), f"render logged an unexpected system_event: {new_events}"
    orch.close()


def test_exit_review_position_never_touches_orders_via_dashboard_render(monkeypatch):
    """EXIT_REVIEW is a human decision flag, never an auto-exit -- rendering
    the Positions/Tonight tabs with an EXIT_REVIEW position present must not
    create any order/fill/exit row (position_health.py's own invariant,
    re-verified here at the UI layer)."""
    orch = _orch()
    j = orch.journal
    pos_id = _open_position(j, symbol="TSLA")
    _open_incident(j, pos_id, symbol="TSLA")

    monkeypatch.setattr(streamlit_app, "st", _fake_st())
    streamlit_app.main(orch=orch)

    assert j.count_rows("paper_orders") == 0
    assert j.count_rows("paper_fills") == 0
    assert j.count_rows("positions", "status = 'closed'") == 0
    orch.close()


def test_invalidation_reason_surfaces_on_open_proposal_view():
    """The backend addition list_open_proposals() needed for the Approval
    Center's exit-plan block: invalidation_reason must round-trip from the
    real setup-card-stamped proposal, not just be present-but-None."""
    orch = _orch()
    pid, _ = inject_pending_proposal(orch, symbol="AAPL")
    views = orch.list_open_proposals()
    assert len(views) == 1
    assert views[0]["proposal_id"] == pid
    assert views[0]["invalidation_reason"]  # non-empty -- the default card sets a real rule
    orch.close()


# --------------------------------------------------------- OPS-A: loopback guard
def test_is_loopback_request_true_for_real_loopback(monkeypatch):
    fake = _fake_st()  # already configured as a genuine loopback connection
    monkeypatch.setattr(streamlit_app, "st", fake)
    assert streamlit_app._is_loopback_request() is True


def test_is_loopback_request_false_for_lan_ip(monkeypatch):
    """The primary signal: the ACTUAL connecting client's IP, regardless of
    what the server believes its own bind address is."""
    fake = _fake_st()
    fake.context.ip_address = "192.168.0.42"
    monkeypatch.setattr(streamlit_app, "st", fake)
    assert streamlit_app._is_loopback_request() is False


def test_is_loopback_request_false_for_non_loopback_bind_address(monkeypatch):
    """The secondary signal: even if a client somehow appears to connect from
    127.0.0.1 (e.g. through a misconfigured proxy), a non-loopback SERVER bind
    address alone is enough to refuse -- defense in depth, not a single point
    of failure."""
    fake = _fake_st()
    fake.get_option.return_value = "0.0.0.0"
    monkeypatch.setattr(streamlit_app, "st", fake)
    assert streamlit_app._is_loopback_request() is False


def test_is_loopback_request_false_when_ip_unknown(monkeypatch):
    """Unknown reads as NOT loopback -- unknown-never-safe, not unknown-means-fine."""
    fake = _fake_st()
    fake.context.ip_address = None
    monkeypatch.setattr(streamlit_app, "st", fake)
    assert streamlit_app._is_loopback_request() is False


def test_is_loopback_request_false_when_st_context_missing(monkeypatch):
    """Audit finding 2 / hardening: a Streamlit older than the pinned floor
    lacks st.context entirely. The guard must fail CLOSED (refuse) rather than
    raise AttributeError. Uses a spec'd mock with no `context` attribute."""
    from unittest.mock import MagicMock

    fake = MagicMock(spec=["get_option"])  # deliberately no `context` attribute
    fake.get_option.return_value = "127.0.0.1"
    monkeypatch.setattr(streamlit_app, "st", fake)
    assert streamlit_app._is_loopback_request() is False


class _StreamlitStopped(Exception):
    """Stand-in for Streamlit's real StopException -- st.stop() is a no-op
    MagicMock call by default, which would silently let execution fall
    through past the refusal branch. Raising here and asserting the raise is
    how we prove main() genuinely halts rather than merely calling st.error()
    and continuing."""


def test_non_loopback_request_renders_nothing_and_writes_nothing(monkeypatch):
    """The actual OPS-A acceptance bar: a non-loopback connection gets the
    refusal message and NOTHING else -- no sidebar, no tabs, no orchestrator
    construction, and (the part that matters) zero ledger writes even though
    the seeded state below includes a pending proposal a real Approval Center
    render would otherwise show Approve/Reject buttons for."""
    fake = _fake_st()
    fake.context.ip_address = "203.0.113.7"  # TEST-NET-3, definitely not loopback
    fake.stop.side_effect = _StreamlitStopped
    monkeypatch.setattr(streamlit_app, "st", fake)

    orch = _orch()
    inject_pending_proposal(orch, symbol="AAPL")
    before = orch.journal.count_rows("trade_proposals")

    with pytest.raises(_StreamlitStopped):
        streamlit_app.main(orch=orch)

    # Refusal fires before ANYTHING else -- get_orchestrator/render_sidebar/
    # the title/tabs never run for this connection.
    fake.sidebar.title.assert_not_called()
    fake.tabs.assert_not_called()
    fake.title.assert_not_called()
    assert fake.error.call_count == 1
    assert "REFUSED" in fake.error.call_args[0][0]

    after = orch.journal.count_rows("trade_proposals")
    assert after == before
    orch.close()


def test_non_loopback_refusal_halts_even_if_st_stop_does_not_raise(monkeypatch):
    """Audit finding 1 / hardening: st.stop() does NOT raise unconditionally in
    a real Streamlit run (returns normally without a live ScriptRunContext).
    The explicit `return` after it must still halt main() so the action surface
    never renders even when st.stop() is a plain no-op -- this is the belt to
    st.stop()'s suspenders. Here st.stop() is left as a no-op (does NOT raise);
    main() must still return cleanly having rendered nothing past the refusal."""
    fake = _fake_st()
    fake.context.ip_address = "203.0.113.7"  # not loopback
    # NOTE: fake.stop has no side_effect here -- it's a silent no-op, the
    # worst case the return backstop exists to cover.
    monkeypatch.setattr(streamlit_app, "st", fake)

    orch = _orch()
    inject_pending_proposal(orch, symbol="AAPL")
    before = orch.journal.count_rows("trade_proposals")

    result = streamlit_app.main(orch=orch)  # must NOT raise, must NOT render on

    assert result is None
    fake.stop.assert_called_once()
    fake.sidebar.title.assert_not_called()
    fake.tabs.assert_not_called()
    fake.title.assert_not_called()
    assert orch.journal.count_rows("trade_proposals") == before
    orch.close()
