"""Bounded replay work remains fair across sessions, failures and restarts.

All ledgers and providers are synthetic. No broker, network or production files.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
import json
import sqlite3

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.config.settings import SettingsError
from alphaos.learning import outcomes_tracker as tracker
from alphaos.learning.outcomes_engine import REPLAY_METHOD_VERSION
from alphaos.util import timeutils
from alphaos.util.market_calendar import nth_trading_day_after


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(timeutils, "now_utc", lambda: datetime(2026, 9, 23, 22, tzinfo=timezone.utc))


def seed(j, symbol, *, family="forward", legacy=False, decision="2026-09-14T15:00:00Z",
         no_levels=False, final=False):
    return j.insert("candidate_outcomes", {
        "outcome_id": "out_" + symbol, "candidate_id": "cand_" + symbol,
        "symbol": symbol, "candidate_type": "candidate",
        "decision_at_utc": decision, "direction_hint": "long",
        "entry_reference_price": 100, "stop_price": 90,
        "target_price": None if no_levels else 120, "replay_window_days": 10,
        "replay_method_version": None if legacy else REPLAY_METHOD_VERSION,
        "outcome_status": "complete" if family == "hold10" or final else "pending",
        "replay_status": "complete" if family == "hold10" or final else "pending",
        "replay_result": "neither" if family == "hold10" or final else None,
        "replay_r": 0.1 if family == "hold10" or final else None,
        "forward_5d_r": 0.1 if family == "hold10" or final else None,
        "outcome_status_10d": None if family == "hold10" and not final else "complete",
    })


def bars(decision, n):
    return [{"date": nth_trading_day_after(decision, day).isoformat(),
             "open": 100, "high": 102, "low": 98, "close": 101}
            for day in range(1, n + 1)]


class Provider:
    def __init__(self, journal, *, family="forward"):
        self.journal = journal
        self.family = family
        self.calls = []
        self.recovered = False

    def get_daily_bars(self, symbol, start, end):
        # Cursor commits must never enclose a provider call.
        assert not self.journal.conn.in_transaction
        self.calls.append(symbol)
        if symbol.startswith("ERR") and not self.recovered:
            raise TimeoutError("SECRET_PROVIDER_PAYLOAD must not enter job summaries")
        if symbol.startswith("EMPTY"):
            return []
        n = 10 if symbol.startswith("NEW") or self.recovered else (7 if self.family == "hold10" else 4)
        return bars(date.fromisoformat(start), n)


def row(j, symbol):
    return j.one("SELECT * FROM candidate_outcomes WHERE symbol=?", (symbol,))


def family_result(result, family):
    return result if family == "forward" else result["hold1_10d"]


@pytest.mark.parametrize("family", ["forward", "hold10"])
def test_more_than_500_persistent_rows_do_not_starve_newer_rows_after_restart(tmp_path, monkeypatch, family):
    path = str(tmp_path / "queue.db")
    j = JournalStore(path)
    old = []
    for i in range(513):
        kind = ("IMM", "EMPTY", "ERR", "NOLEVEL", "BADTIME")[i % 5]
        symbol = f"{kind}_{i}"
        old.append(symbol)
        seed(j, symbol, family=family, no_levels=kind == "NOLEVEL",
             decision="bad date" if kind == "BADTIME" else "2026-09-14T15:00:00Z",
             legacy=i % 2 == 0)
    for i in range(7):
        seed(j, f"NEW_{i}", family=family, decision="2026-08-31T15:00:00Z")
    original = {symbol: row(j, symbol) for symbol in old}
    source = Provider(j, family=family)
    first = tracker.update_pending_outcomes(j, source, limit=500)
    result = family_result(first, family)
    assert result["errors"] == 100
    assert result["provider_error_types"] == {"TimeoutError": 100}
    assert len(result["provider_error_samples"]) == 5
    assert "SECRET" not in json.dumps(first)
    assert not any(symbol.startswith("NEW") for symbol in source.calls)
    cursor_family = "forward_replay_v1" if family == "forward" else "hold1_10d_v1"
    assert j.one("SELECT * FROM outcome_processing_cursors WHERE family=?", (cursor_family,))["last_row_id"] == 500
    j.close()

    # Reopening is a genuine durable restart, not reusing an in-memory index.
    j = JournalStore(path)
    source = Provider(j, family=family)
    second = family_result(tracker.update_pending_outcomes(j, source, limit=500), family)
    assert second["completed"] == 7
    assert all(row(j, f"NEW_{i}")["outcome_status" if family == "forward" else "outcome_status_10d"]
               == "complete" for i in range(7))
    assert all(row(j, symbol)["replay_method_version"] == original[symbol]["replay_method_version"]
               for symbol in old)
    for symbol in old:
        current = row(j, symbol)
        if symbol.startswith(("ERR", "EMPTY", "BADTIME")):
            assert current == original[symbol]  # scheduling is not evidence
        elif family == "forward":
            assert current["outcome_status"] == "partial"
            assert current["replay_status"] == ("unavailable" if symbol.startswith("NOLEVEL") else "pending")
            assert current["replay_r"] is None
        else:
            assert current["outcome_status_10d"] == "partial"
            for key in ("forward_5d_r", "replay_result", "replay_r", "outcome_status"):
                assert current[key] == original[symbol][key]

    # A later completed session supplies ten real session dates. The next
    # cycle retries old work; errors have not made those observations terminal.
    frozen_new = [row(j, f"NEW_{i}") for i in range(7)]
    monkeypatch.setattr(timeutils, "now_utc", lambda: datetime(2026, 9, 29, 22, tzinfo=timezone.utc))
    source.recovered = True
    tracker.update_pending_outcomes(j, source, limit=500)
    assert row(j, "ERR_2")["outcome_status" if family == "forward" else "outcome_status_10d"] == "complete"
    assert [row(j, f"NEW_{i}") for i in range(7)] == frozen_new
    j.close()


@pytest.mark.parametrize("family", ["forward", "hold10"])
def test_frozen_cycle_high_water_retries_old_work_before_unbounded_new_arrivals(family):
    j = JournalStore(":memory:")
    for i in range(3):
        seed(j, f"IMM_{i}", family=family)
    source = Provider(j, family=family)
    tracker.update_pending_outcomes(j, source, limit=2)
    seed(j, "IMM_3", family=family)
    tracker.update_pending_outcomes(j, source, limit=2)
    # The new ID cannot extend the existing cycle past its frozen high-water.
    assert source.calls == ["IMM_0", "IMM_1", "IMM_2"]
    seed(j, "IMM_4", family=family)
    tracker.update_pending_outcomes(j, source, limit=2)
    assert source.calls[-2:] == ["IMM_0", "IMM_1"]
    tracker.update_pending_outcomes(j, source, limit=2)
    tracker.update_pending_outcomes(j, source, limit=2)
    assert Counter(source.calls) == Counter({"IMM_0": 2, "IMM_1": 2, "IMM_2": 2, "IMM_3": 1, "IMM_4": 1})
    j.close()


@pytest.mark.parametrize("family", ["forward", "hold10"])
def test_crash_after_claim_survives_restart_and_retries_next_cycle(tmp_path, family):
    path = str(tmp_path / "crash.db")
    j = JournalStore(path)
    seed(j, "IMM_0", family=family)
    seed(j, "IMM_1", family=family)
    originals = j.query("SELECT * FROM candidate_outcomes")

    class Crashing:
        def get_daily_bars(self, symbol, start, end):
            assert not j.conn.in_transaction
            raise KeyboardInterrupt("simulated process exit after durable claim")

    with pytest.raises(KeyboardInterrupt):
        tracker.update_pending_outcomes(j, Crashing(), limit=1)
    assert j.query("SELECT * FROM candidate_outcomes") == originals
    j.close()

    j = JournalStore(path)
    source = Provider(j, family=family)
    tracker.update_pending_outcomes(j, source, limit=1)
    assert source.calls == ["IMM_1"]
    tracker.update_pending_outcomes(j, source, limit=1)
    assert source.calls == ["IMM_1", "IMM_0"]
    j.close()


@pytest.mark.parametrize("family", ["forward", "hold10"])
def test_single_failed_row_once_per_call_then_retry_next_cycle(family):
    j = JournalStore(":memory:")
    seed(j, "ERR_0", family=family)
    source = Provider(j, family=family)
    original = row(j, "ERR_0")
    for _ in range(2):
        result = family_result(tracker.update_pending_outcomes(j, source, limit=500), family)
        assert result["errors"] == 1
        assert result["completed"] == 0
        assert row(j, "ERR_0") == original
    assert source.calls == ["ERR_0", "ERR_0"]
    source.recovered = True
    assert family_result(tracker.update_pending_outcomes(j, source, limit=500), family)["completed"] == 1
    j.close()


def test_absent_provider_and_zero_limit_do_not_advance_cursors():
    j = JournalStore(":memory:")
    seed(j, "IMM_0")
    assert tracker.update_pending_outcomes(j, None, limit=500)["skipped"] == 1
    assert tracker.update_pending_outcomes(j, Provider(j), limit=0)["total"] == 0
    assert j.count_rows("outcome_processing_cursors") == 0
    assert row(j, "IMM_0")["updated_at_utc"] is None
    j.close()


def test_empty_and_ineligible_ledger_preserve_frozen_rows():
    j = JournalStore(":memory:")
    assert tracker.update_pending_outcomes(j, Provider(j))["total"] == 0
    seed(j, "FROZEN", legacy=True, final=True)
    original = row(j, "FROZEN")
    source = Provider(j)
    for _ in range(3):
        assert tracker.update_pending_outcomes(j, source)["total"] == 0
    assert row(j, "FROZEN") == original
    assert source.calls == []
    j.close()


@pytest.mark.parametrize("limit", [-1, True, 1.5])
def test_unbounded_or_invalid_limits_are_rejected(limit):
    j = JournalStore(":memory:")
    with pytest.raises(ValueError, match="nonnegative integer"):
        tracker.update_pending_outcomes(j, Provider(j), limit=limit)
    assert j.count_rows("outcome_processing_cursors") == 0
    j.close()


def test_cursor_claims_are_serialized_across_connections_and_old_cycle_stops(tmp_path):
    path = str(tmp_path / "claims.db")
    a, b = JournalStore(path), JournalStore(path)
    for i in range(3):
        seed(a, f"IMM_{i}")
    def claim(j, cycle=None):
        return tracker._claim_outcome(j, "forward_replay_v1",
                                      tracker._FORWARD_ELIGIBILITY, tracker._FORWARD_PARAMS, cycle)
    r1, cycle = claim(a)
    r2, other_cycle = claim(b)
    r3, _ = claim(a, cycle)
    assert [r1["symbol"], r2["symbol"], r3["symbol"]] == ["IMM_0", "IMM_1", "IMM_2"]
    assert cycle == other_cycle
    r4, newer_cycle = claim(b)
    assert r4["symbol"] == "IMM_0" and newer_cycle != cycle
    assert claim(a, cycle)[0] is None
    a.close()
    b.close()


def test_additive_cursor_schema_on_restored_old_ledger_is_idempotent(tmp_path):
    source = JournalStore(str(tmp_path / "old.db"))
    seed(source, "FROZEN", legacy=True, final=True)
    seed(source, "PENDING", legacy=True)
    original = source.query("SELECT * FROM candidate_outcomes ORDER BY id")
    source.conn.execute("DROP TABLE outcome_processing_cursors")
    source.conn.commit()
    restored = str(tmp_path / "restored.db")
    raw = sqlite3.connect(restored)
    source.conn.backup(raw)
    raw.close()
    source.close()
    for _ in range(2):
        j = JournalStore(restored)
        assert j.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert j.query("SELECT * FROM candidate_outcomes ORDER BY id") == original
        assert j.count_rows("outcome_processing_cursors") == 0
        assert j.conn.execute("PRAGMA user_version").fetchone()[0] == 3
        j.close()


@pytest.mark.parametrize("family", ["forward", "hold10"])
def test_malformed_bars_and_levels_are_counted_without_blocking_valid_rows_or_other_family(family):
    j = JournalStore(":memory:")
    seed(j, "BADBAR", family=family)
    seed(j, "BADSTOP", family=family)
    j.conn.execute("UPDATE candidate_outcomes SET stop_price='invalid stop payload' WHERE symbol='BADSTOP'")
    j.conn.commit()
    seed(j, "NEW_VALID", family=family, decision="2026-08-31T15:00:00Z")
    seed(j, "NEW_OTHER", family="hold10" if family == "forward" else "forward",
         decision="2026-08-31T15:00:00Z")
    originals = [row(j, name) for name in ("BADBAR", "BADSTOP")]

    class BadBars(Provider):
        def get_daily_bars(self, symbol, start, end):
            result = super().get_daily_bars(symbol, start, end)
            if symbol == "BADBAR" and not self.recovered:
                for bar in result:
                    bar["close"] = "invalid close payload"
            return result

    source = BadBars(j, family=family)
    result = tracker.update_pending_outcomes(j, source)
    errors = family_result(result, family)
    assert errors["errors"] == errors["calculation_errors"] == 2
    assert errors["calculation_error_types"] == {"TypeError": 1, "ValueError": 1}
    assert "invalid close payload" not in json.dumps(result)
    assert "invalid stop payload" not in json.dumps(result)
    assert [row(j, name) for name in ("BADBAR", "BADSTOP")] == originals
    assert result["completed"] == result["hold1_10d"]["completed"] == 1
    # A later cycle retries the malformed provider input after source recovery.
    source.recovered = True
    retry = family_result(tracker.update_pending_outcomes(j, source), family)
    assert retry["completed"] == 1 and retry["calculation_errors"] == 1
    assert row(j, "BADSTOP") == originals[1]
    j.close()


@pytest.mark.parametrize("family", ["forward", "hold10"])
def test_missing_calculation_key_is_explicit_and_retried(monkeypatch, family):
    j = JournalStore(":memory:")
    seed(j, "IMM_0", family=family)
    original = row(j, "IMM_0")
    real_stats = tracker.forward_window_stats
    monkeypatch.setattr(tracker, "forward_window_stats", lambda *args: {})
    result = family_result(tracker.update_pending_outcomes(j, Provider(j, family=family)), family)
    assert result["calculation_errors"] == 1
    assert result["calculation_error_types"] == {"KeyError": 1}
    assert row(j, "IMM_0") == original
    monkeypatch.setattr(tracker, "forward_window_stats", real_stats)
    assert family_result(tracker.update_pending_outcomes(j, Provider(j, family=family)), family)["updated"] == 1
    j.close()


@pytest.mark.parametrize("exception", [RuntimeError, SettingsError, sqlite3.OperationalError])
def test_unexpected_programming_and_database_failures_propagate_after_claim(monkeypatch, exception):
    j = JournalStore(":memory:")
    seed(j, "IMM_0")
    seed(j, "IMM_1")
    originals = j.query("SELECT * FROM candidate_outcomes")
    real_stats = tracker.forward_window_stats

    def broken(*args):
        raise exception("unexpected failure")

    monkeypatch.setattr(tracker, "forward_window_stats", broken)
    with pytest.raises(exception, match="unexpected failure"):
        tracker.update_pending_outcomes(j, Provider(j), limit=1)
    assert j.query("SELECT * FROM candidate_outcomes") == originals
    monkeypatch.setattr(tracker, "forward_window_stats", real_stats)
    source = Provider(j)
    tracker.update_pending_outcomes(j, source, limit=1)
    assert source.calls == ["IMM_1"]
    j.close()


@pytest.mark.parametrize("exception", [RuntimeError, SettingsError, sqlite3.OperationalError])
def test_unknown_provider_or_configuration_failure_is_not_swallowed(exception):
    j = JournalStore(":memory:")
    seed(j, "IMM_0")
    original = row(j, "IMM_0")

    class BrokenProvider:
        def get_daily_bars(self, symbol, start, end):
            raise exception("unexpected provider/configuration failure")

    with pytest.raises(exception):
        tracker.update_pending_outcomes(j, BrokenProvider())
    assert row(j, "IMM_0") == original
    assert j.one("SELECT * FROM outcome_processing_cursors WHERE family='forward_replay_v1'")["last_row_id"] == 1
    j.close()
