"""D1 corrected-behaviour regressions on 742312f (original trigger: legacy partial 10d row)."""
import sys, os
sys.path.insert(0, os.path.join(os.environ["ALPHAOS_REPO"], "tests"))
from datetime import datetime, timezone
import pytest
from alphaos.journal.journal_store import JournalStore
from alphaos.learning.outcomes_engine import REPLAY_METHOD_VERSION
from alphaos.learning.outcomes_tracker import update_pending_outcomes
from alphaos.reports.baseline_report import _pair_exclusion_reason
from alphaos.util import timeutils
from test_replay_maturity_repair import Bars, bars


def seed_legacy_partial(j, **over):
    j.insert("candidates", {"candidate_id": "c1", "symbol": "AAPL", "direction": "long", "card_id": "catalyst_momentum_v3"})
    j.insert("candidate_outcomes", {**{
        "outcome_id": "o1", "candidate_id": "c1", "symbol": "AAPL", "candidate_type": "candidate",
        "decision_at_utc": "2026-01-05T16:00:00+00:00", "entry_reference_price": 100.0,
        "stop_price": 90.0, "target_price": 120.0, "direction_hint": "long", "outcome_status": "partial",
        "replay_result": "neither", "replay_r": 0.1, "replay_exit_reason": "window_exhausted",
        "replay_window_days": 10, "replay_status": None, "replay_method_version": None,
        "data_quality_status": "ok"}, **over})


@pytest.fixture
def j(monkeypatch):
    j = JournalStore(":memory:")
    monkeypatch.setattr(timeutils, "now_utc", lambda: datetime(2026, 2, 1, tzinfo=timezone.utc))
    yield j
    j.close()


def test_d1a_legacy_partial_continues_to_day_ten_and_keeps_null_method_stamp(j):
    seed_legacy_partial(j)
    src = Bars(bars(5)); update_pending_outcomes(j, src)
    d5 = j.one("SELECT * FROM candidate_outcomes")
    assert d5["outcome_status"] == "complete" and d5["replay_status"] == "pending" and d5["replay_r"] is None
    assert d5["replay_method_version"] is None
    f5 = d5["forward_5d_r"]
    src.rows = bars(10); r = update_pending_outcomes(j, src)
    d10 = j.one("SELECT * FROM candidate_outcomes")
    assert r["updated"] == 1 and r["completed"] == 0
    assert d10["replay_status"] == "complete" and d10["replay_result"] == "neither" and d10["replay_r"] is not None
    assert d10["replay_bars_used"] == 10
    assert d10["replay_method_version"] is None          # provenance retained: NOT promoted to v2
    assert d10["forward_5d_r"] == f5                      # completed 5d family untouched
    assert update_pending_outcomes(j, src)["updated"] == 0  # idempotent
    # and it is excluded from the prospective cohort
    assert _pair_exclusion_reason(d10, {"decision": "propose", "replay_window_days": 10, "replay_r": 0.0}) == "legacy_or_unverified_ai_replay"


def test_d1b_day_six_barrier_is_captured_on_a_legacy_row(j):
    seed_legacy_partial(j)
    src = Bars(bars(5)); update_pending_outcomes(j, src)
    src.rows = bars(6); src.rows[-1]["high"] = 121.0
    update_pending_outcomes(j, src)
    d = j.one("SELECT * FROM candidate_outcomes")
    assert d["replay_status"] == "complete" and d["replay_result"] == "target_hit" and d["replay_r"] == 2.0
    assert d["replay_method_version"] is None


def test_d1c_already_stranded_row_recovers(j):
    """Row shaped exactly like the 35 production rows AFTER their 5d family completes under cd1cb2c."""
    seed_legacy_partial(j, outcome_status="complete", replay_status="pending", replay_result=None,
                        replay_r=None, replay_exit_reason="window_incomplete", replay_bars_used=4, forward_5d_r=0.02)
    src = Bars(bars(10)); r = update_pending_outcomes(j, src)
    d = j.one("SELECT * FROM candidate_outcomes")
    assert r["updated"] == 1 and d["replay_status"] == "complete" and d["replay_r"] is not None
    assert d["forward_5d_r"] == 0.02 and d["replay_method_version"] is None


def test_d1d_legacy_complete_with_final_replay_is_never_revisited(j):
    seed_legacy_partial(j, outcome_status="complete", replay_status=None, replay_result="neither", replay_r=0.1)
    src = Bars(bars(10)); r = update_pending_outcomes(j, src)
    d = j.one("SELECT * FROM candidate_outcomes")
    assert r["total"] == 0 and d["replay_r"] == 0.1 and d["replay_status"] is None


def test_d1e_pending_replay_with_no_bars_goes_unavailable_without_touching_5d_family(j, monkeypatch):
    seed_legacy_partial(j, outcome_status="complete", replay_status="pending", replay_result=None, replay_r=None,
                        decision_at_utc="2025-11-01T16:00:00+00:00", forward_5d_r=0.02)
    r = update_pending_outcomes(j, Bars([]))
    d = j.one("SELECT * FROM candidate_outcomes")
    assert r["unavailable"] == 1 and d["replay_status"] == "unavailable"
    assert d["outcome_status"] == "complete" and d["forward_5d_r"] == 0.02


def test_d1f_new_rows_are_stamped_v2_and_enter_cohort(j):
    seed_legacy_partial(j, outcome_status="pending", replay_status="pending", replay_result=None, replay_r=None,
                        replay_exit_reason=None, replay_method_version=REPLAY_METHOD_VERSION)
    src = Bars(bars(10)); update_pending_outcomes(j, src); update_pending_outcomes(j, src)
    d = j.one("SELECT * FROM candidate_outcomes")
    assert d["replay_method_version"] == REPLAY_METHOD_VERSION and d["replay_status"] == "complete"
    assert _pair_exclusion_reason(d, {"decision": "propose", "replay_window_days": 10, "replay_r": 0.0}) is None
