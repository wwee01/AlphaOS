"""Cross-family turns survive external interruption and real ledger reopening."""
from datetime import date, datetime, timezone

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.learning import outcomes_tracker as tracker
from alphaos.util import timeutils
from test_outcome_processing_fairness import bars, row, seed

FORWARD = "forward_replay_v1"
HOLD = "hold1_10d_v1"


class PartialProvider:
    def __init__(self, journal):
        self.journal = journal
        self.calls = []

    def get_daily_bars(self, symbol, start, end):
        assert not self.journal.conn.in_transaction
        self.calls.append(symbol)
        return bars(date.fromisoformat(start), 7 if symbol.startswith("HOLD") else 4)


def dispatch(j):
    return j.one("SELECT * FROM outcome_family_dispatch WHERE dispatcher='candidate_outcomes'")


def prime_first(j, first):
    if first == HOLD:
        # A genuine successful empty call rotates the first family. No manual
        # metadata rewrite is needed to exercise the reverse ordering.
        tracker.update_pending_outcomes(j, PartialProvider(j), limit=1)


def seed_both(j, n=1):
    for i in range(n):
        seed(j, f"FWD_{i}")
        seed(j, f"HOLD_{i}", family="hold10", decision="2026-08-31T15:00:00Z")


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(timeutils, "now_utc", lambda: datetime(2026, 9, 23, 22, tzinfo=timezone.utc))


def test_interrupted_forward_reopen_gives_hold_a_first_turn(tmp_path):
    path = str(tmp_path / "interrupted.db")
    j = JournalStore(path)
    seed(j, "HOLD_READY", family="hold10", decision="2026-08-31T15:00:00Z")
    for i in range(4):
        seed(j, f"FWD_{i}")

    class Interrupted:
        def __init__(self, journal):
            self.journal = journal
            self.calls = []

        def get_daily_bars(self, symbol, start, end):
            assert not self.journal.conn.in_transaction
            self.calls.append(symbol)
            if len(self.calls) == 2:
                raise KeyboardInterrupt("synthetic external interruption")
            return bars(date.fromisoformat(start), 10 if symbol == "HOLD_READY" else 4)

    first = Interrupted(j)
    with pytest.raises(KeyboardInterrupt):
        tracker.update_pending_outcomes(j, first, limit=500)
    assert first.calls == ["FWD_0", "FWD_1"]
    j.close()

    j = JournalStore(path)
    second = Interrupted(j)
    with pytest.raises(KeyboardInterrupt):
        tracker.update_pending_outcomes(j, second, limit=500)
    assert second.calls[0] == "HOLD_READY"
    assert row(j, "HOLD_READY")["outcome_status_10d"] == "complete"
    j.close()


@pytest.mark.parametrize("first", [FORWARD, HOLD])
@pytest.mark.parametrize("interrupted_block", [0, 1])
def test_either_interrupted_block_leaves_other_family_first_after_reopen(tmp_path, monkeypatch, first, interrupted_block):
    path = str(tmp_path / "symmetric.db")
    j = JournalStore(path)
    prime_first(j, first)
    seed_both(j)
    order = [first, HOLD if first == FORWARD else FORWARD]
    failed_family = order[interrupted_block]
    expected_next = HOLD if failed_family == FORWARD else FORWARD
    prefix = "HOLD" if failed_family == HOLD else "FWD"

    class Interrupted(PartialProvider):
        def get_daily_bars(self, symbol, start, end):
            assert not self.journal.conn.in_transaction
            # Durable next-family state must precede any provider work.
            expected = FORWARD if symbol.startswith("HOLD") else HOLD
            assert dispatch(self.journal)["next_family"] == expected
            if symbol.startswith(prefix):
                raise KeyboardInterrupt("synthetic block interruption")
            return super().get_daily_bars(symbol, start, end)

    with pytest.raises(KeyboardInterrupt):
        tracker.update_pending_outcomes(j, Interrupted(j), limit=1)
    assert dispatch(j)["next_family"] == expected_next
    j.close()

    j = JournalStore(path)
    provider = PartialProvider(j)
    blocks = []
    original = tracker._fair_outcome_rows

    def record_blocks(journal, family, *args):
        blocks.append(family)
        yield from original(journal, family, *args)

    monkeypatch.setattr(tracker, "_fair_outcome_rows", record_blocks)
    tracker.update_pending_outcomes(j, provider, limit=1)
    # A first block can finish the end of its prior frozen cycle without a
    # provider call. The scheduled block, not first nonempty fetch, is the turn.
    assert blocks[0] == expected_next
    j.close()


@pytest.mark.parametrize("first", [FORWARD, HOLD])
def test_turn_is_durable_even_when_interrupted_before_row_claim(tmp_path, monkeypatch, first):
    path = str(tmp_path / "beforeclaim.db")
    j = JournalStore(path)
    prime_first(j, first)
    seed_both(j)
    original = j.query("SELECT * FROM candidate_outcomes ORDER BY id")

    def stop_before_claim(*args, **kwargs):
        assert not j.conn.in_transaction
        raise KeyboardInterrupt("before first row claim")

    with monkeypatch.context() as scoped:
        scoped.setattr(tracker, "_claim_outcome", stop_before_claim)
        with pytest.raises(KeyboardInterrupt):
            tracker.update_pending_outcomes(j, PartialProvider(j), limit=1)
    assert j.query("SELECT * FROM candidate_outcomes ORDER BY id") == original
    j.close()

    j = JournalStore(path)
    provider = PartialProvider(j)
    tracker.update_pending_outcomes(j, provider, limit=1)
    assert provider.calls[0].startswith("HOLD" if first == FORWARD else "FWD")
    j.close()


@pytest.mark.parametrize("first", [FORWARD, HOLD])
def test_interrupt_before_second_block_claim_leaves_first_family_due(tmp_path, monkeypatch, first):
    path = str(tmp_path / "secondclaim.db")
    j = JournalStore(path)
    prime_first(j, first)
    seed_both(j, n=2)
    second = HOLD if first == FORWARD else FORWARD
    original = tracker._claim_outcome

    def interrupt_second(journal, family, *args):
        if family == second:
            assert dispatch(journal)["next_family"] == first
            assert not journal.conn.in_transaction
            raise KeyboardInterrupt("before second block row claim")
        return original(journal, family, *args)

    with monkeypatch.context() as scoped:
        scoped.setattr(tracker, "_claim_outcome", interrupt_second)
        with pytest.raises(KeyboardInterrupt):
            tracker.update_pending_outcomes(j, PartialProvider(j), limit=1)
    assert row(j, "HOLD_0" if second == HOLD else "FWD_0")["updated_at_utc"] is None
    j.close()

    j = JournalStore(path)
    provider = PartialProvider(j)
    tracker.update_pending_outcomes(j, provider, limit=1)
    assert provider.calls[0].startswith("HOLD" if first == HOLD else "FWD")
    j.close()

def test_successful_reopened_passes_alternate_first_and_preserve_both_caps(tmp_path):
    path = str(tmp_path / "caps.db")
    j = JournalStore(path)
    seed_both(j, n=3)
    j.close()
    all_calls = []
    for pass_number in range(4):
        j = JournalStore(path)
        provider = PartialProvider(j)
        result = tracker.update_pending_outcomes(j, provider, limit=2)
        expected_prefix = "FWD" if pass_number % 2 == 0 else "HOLD"
        assert provider.calls[0].startswith(expected_prefix)
        assert sum(s.startswith("FWD") for s in provider.calls) <= 2
        assert sum(s.startswith("HOLD") for s in provider.calls) <= 2
        assert result["total"] == result["updated"] == sum(s.startswith("FWD") for s in provider.calls)
        assert result["hold1_10d"]["updated"] == sum(s.startswith("HOLD") for s in provider.calls)
        assert set(result) == {"total", "updated", "completed", "skipped", "unavailable", "hold1_10d", "session_admission"}
        assert result["session_admission"]["method_version"] == "daily_session_admission_v1"
        all_calls.extend(provider.calls)
        j.close()
    assert set(all_calls) == {f"{prefix}_{i}" for prefix in ("FWD", "HOLD") for i in range(3)}


@pytest.mark.parametrize("empty_family", [FORWARD, HOLD, "both"])
def test_empty_families_still_get_one_bounded_turn(empty_family):
    j = JournalStore(":memory:")
    if empty_family != "both":
        seed(j, "HOLD_0", family="hold10") if empty_family == FORWARD else seed(j, "FWD_0")
    for expected in (HOLD, FORWARD):
        provider = PartialProvider(j)
        result = tracker.update_pending_outcomes(j, provider, limit=1)
        assert dispatch(j)["next_family"] == expected
        assert len(provider.calls) == (0 if empty_family == "both" else 1)
        assert result["completed"] == result["hold1_10d"]["completed"] == 0
    j.close()


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_nonprocessing_limits_never_create_or_advance_dispatch(limit):
    j = JournalStore(":memory:")
    seed_both(j)
    if limit == 0:
        assert tracker.update_pending_outcomes(j, PartialProvider(j), limit=limit)["total"] == 0
    else:
        with pytest.raises(ValueError, match="nonnegative integer"):
            tracker.update_pending_outcomes(j, PartialProvider(j), limit=limit)
    assert dispatch(j) is None
    assert j.count_rows("outcome_processing_cursors") == 0
    j.close()


def test_absent_provider_and_zero_limit_preserve_existing_dispatch_and_cursors():
    j = JournalStore(":memory:")
    seed_both(j)
    tracker.update_pending_outcomes(j, PartialProvider(j), limit=1)
    state, cursors = dispatch(j), j.query("SELECT * FROM outcome_processing_cursors ORDER BY family")
    assert tracker.update_pending_outcomes(j, None, limit=1)["skipped"] == 1
    assert tracker.update_pending_outcomes(j, PartialProvider(j), limit=0)["total"] == 0
    assert dispatch(j) == state
    assert j.query("SELECT * FROM outcome_processing_cursors ORDER BY family") == cursors
    j.close()


@pytest.mark.parametrize("first", [FORWARD, HOLD])
def test_newly_eligible_hold_work_waits_only_when_hold_ran_first(tmp_path, first):
    path = str(tmp_path / "neweligibility.db")
    j = JournalStore(path)
    prime_first(j, first)
    seed(j, "NEW_READY", decision="2026-08-31T15:00:00Z")
    j.conn.execute("UPDATE candidate_outcomes SET outcome_status_10d=NULL")
    j.conn.commit()

    class Mature(PartialProvider):
        def get_daily_bars(self, symbol, start, end):
            assert not self.journal.conn.in_transaction
            self.calls.append(symbol)
            return bars(date.fromisoformat(start), 10)

    provider = Mature(j)
    result = tracker.update_pending_outcomes(j, provider, limit=1)
    assert result["completed"] == 1
    assert result["hold1_10d"]["completed"] == (1 if first == FORWARD else 0)
    assert len(provider.calls) == (2 if first == FORWARD else 1)
    completed_forward = row(j, "NEW_READY")
    j.close()

    j = JournalStore(path)
    provider = Mature(j)
    result = tracker.update_pending_outcomes(j, provider, limit=1)
    assert result["completed"] == 0
    assert result["hold1_10d"]["completed"] == (0 if first == FORWARD else 1)
    assert row(j, "NEW_READY")["outcome_status_10d"] == "complete"
    for key in ("forward_5d_r", "outcome_status", "replay_r", "replay_result", "replay_status", "replay_method_version"):
        assert row(j, "NEW_READY")[key] == completed_forward[key]
    j.close()


@pytest.mark.parametrize("failing_family", [FORWARD, HOLD])
def test_caught_provider_errors_keep_family_counters_and_continue(failing_family):
    j = JournalStore(":memory:")
    prime_first(j, HOLD)
    seed_both(j)

    class Unavailable(PartialProvider):
        def get_daily_bars(self, symbol, start, end):
            failed = symbol.startswith("HOLD") if failing_family == HOLD else symbol.startswith("FWD")
            if failed:
                assert not self.journal.conn.in_transaction
                self.calls.append(symbol)
                raise TimeoutError("private provider details")
            return super().get_daily_bars(symbol, start, end)

    provider = Unavailable(j)
    result = tracker.update_pending_outcomes(j, provider, limit=1)
    assert provider.calls == ["HOLD_0", "FWD_0"]
    failed_result = result if failing_family == FORWARD else result["hold1_10d"]
    other = result["hold1_10d"] if failing_family == FORWARD else result
    assert failed_result["provider_errors"] == 1
    assert failed_result["updated"] == 0
    assert other["updated"] == 1
    assert "private" not in str(result)
    j.close()


def test_stale_dispatcher_cannot_overwrite_new_turn(tmp_path):
    path = str(tmp_path / "stale.db")
    a, b = JournalStore(path), JournalStore(path)
    first, revision = tracker._advance_dispatch_turn(a)
    tracker._advance_dispatch_turn(b)
    fresh = dispatch(b)
    with pytest.raises(RuntimeError, match="dispatch changed"):
        tracker._advance_dispatch_turn(a, first, revision)
    assert dispatch(a) == fresh
    assert not a.conn.in_transaction
    a.close()
    b.close()
