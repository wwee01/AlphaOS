"""Research-validity probes against cd1cb2c (offline, in-memory)."""
import sys, os
sys.path.insert(0, os.path.join(os.environ["ALPHAOS_REPO"], "tests"))
from datetime import date, datetime, timezone
from alphaos.journal.journal_store import JournalStore
from alphaos.learning.outcomes_tracker import update_pending_outcomes
from alphaos.util import timeutils
from test_replay_maturity_repair import Bars, bars


def seed_legacy_partial(j):
    """A row that existed BEFORE the deploy: replay_method_version NULL,
    outcome_status 'partial' (5d family not yet resolved), 10-day window."""
    j.insert("candidates", {"candidate_id": "c1", "symbol": "AAPL", "direction": "long",
                             "card_id": "catalyst_momentum_v3"})
    j.insert("candidate_outcomes", {
        "outcome_id": "o1", "candidate_id": "c1", "symbol": "AAPL",
        "candidate_type": "candidate", "decision_at_utc": "2026-01-05T16:00:00+00:00",
        "entry_reference_price": 100.0, "stop_price": 90.0, "target_price": 120.0,
        "direction_hint": "long", "outcome_status": "partial",
        "replay_result": "neither", "replay_r": 0.1, "replay_exit_reason": "window_exhausted",
        "replay_window_days": 10, "replay_status": None, "replay_method_version": None,
        "data_quality_status": "ok",
    })


def test_p6_legacy_partial_row_with_10d_window_is_stranded_pending_forever(monkeypatch):
    j = JournalStore(":memory:")
    monkeypatch.setattr(timeutils, "now_utc", lambda: datetime(2026, 2, 1, tzinfo=timezone.utc))
    seed_legacy_partial(j)
    src = Bars(bars(5))
    update_pending_outcomes(j, src)
    d5 = j.one("SELECT * FROM candidate_outcomes WHERE outcome_id='o1'")
    assert d5["outcome_status"] == "complete"
    assert d5["replay_status"] == "pending" and d5["replay_r"] is None
    assert d5["replay_method_version"] is None          # <-- never stamped on a legacy row
    # ten usable bars are now available; a v2-stamped row would complete here
    src.rows = bars(10)
    r = update_pending_outcomes(j, src)
    d10 = j.one("SELECT * FROM candidate_outcomes WHERE outcome_id='o1'")
    assert r["updated"] == 0                             # row is no longer selected at all
    assert d10["replay_status"] == "pending" and d10["replay_r"] is None   # stranded
    j.close()


def test_p6b_same_row_stamped_v2_completes_normally(monkeypatch):
    """Control: identical row but with replay_method_version stamped -> completes at day 10."""
    from alphaos.learning.outcomes_engine import REPLAY_METHOD_VERSION
    j = JournalStore(":memory:")
    monkeypatch.setattr(timeutils, "now_utc", lambda: datetime(2026, 2, 1, tzinfo=timezone.utc))
    seed_legacy_partial(j)
    j.conn.execute("UPDATE candidate_outcomes SET replay_method_version=? WHERE outcome_id='o1'", (REPLAY_METHOD_VERSION,))
    j.conn.commit()
    src = Bars(bars(5)); update_pending_outcomes(j, src)
    src.rows = bars(10); update_pending_outcomes(j, src)
    d10 = j.one("SELECT * FROM candidate_outcomes WHERE outcome_id='o1'")
    assert d10["replay_status"] == "complete" and d10["replay_r"] is not None
    j.close()
