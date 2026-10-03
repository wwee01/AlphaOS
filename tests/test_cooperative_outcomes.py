"""Real outcome rows, durable restarts and tiny cooperative slices, no network."""

from datetime import date, datetime, timezone
import json
from types import SimpleNamespace

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.learning import cooperative_outcomes as work
from alphaos.learning import outcomes_tracker as tracker
from alphaos.learning import work_continuation as turns
from alphaos.scheduler import cadence
from alphaos.util import timeutils
from conftest import make_settings
from test_outcome_processing_fairness import seed, bars

NOW = datetime(2026, 10, 2, 21, tzinfo=timezone.utc)


def baseline(j, symbol, rule="threshold_v1"):
    j.insert(
        "shadow_baseline_decisions",
        {
            "baseline_decision_id": "b_" + symbol,
            "candidate_id": "c_" + symbol,
            "symbol": symbol,
            "rule_version": rule,
            "decision": "propose",
            "direction": "long",
            "entry": 100,
            "stop": 90,
            "target": 120,
            "input_sha": "unchanged",
            "decision_at_utc": "2026-09-29T15:00:00+00:00",
            "replay_status": "pending",
        },
    )


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(timeutils, "now_utc", lambda: NOW)
    j = JournalStore(str(tmp_path / "ledger.db"))
    s = make_settings(
        ALPHAOS_DB_PATH=j.db_path, BASELINE_ENABLED=True, ATTRIBUTION_ENABLED=True
    )
    calls = []

    class Provider:
        def get_daily_bars(self, symbol, start, end):
            assert not j.conn.in_transaction
            calls.append(symbol)
            return bars(date.fromisoformat(start), 1)

    monkeypatch.setattr(
        "alphaos.data.providers.alpaca_bars.make_bars_provider", lambda *a: Provider()
    )
    state = SimpleNamespace(
        j=j, s=s, calls=calls, orch=SimpleNamespace(journal=j, settings=s)
    )
    yield state
    state.j.close()


def test_stop_before_any_work_preserves_all_rows_cursors_and_turns(c):
    seed(c.j, "FWD", decision="2026-09-29T15:00:00Z")
    before = c.j.query("SELECT * FROM candidate_outcomes")
    result = work.run_cooperative_outcomes(c.orch, limit=500, should_yield=lambda: True)
    assert result["yielded"] and result["stages"] == {}
    assert c.j.query("SELECT * FROM candidate_outcomes") == before
    assert c.j.count_rows("outcome_processing_cursors") == 0
    assert c.j.count_rows("outcome_family_dispatch") == 0
    assert c.j.count_rows("system_events", "category LIKE 'outcomes_work_turn_%'") == 0


def test_one_fetch_slices_reach_all_families_and_newer_baseline_rows_after_reopen(c):
    for i in range(4):
        seed(c.j, f"FWD{i}", decision="2026-09-29T15:00:00Z")
        seed(c.j, f"HOLD{i}", family="hold10", decision="2026-09-29T15:00:00Z")
        baseline(c.j, f"BASE{i}")
        baseline(c.j, f"BASEH{i}", "threshold_v1_hold10")
    for _ in range(16):
        before = len(c.calls)
        result = work.run_cooperative_outcomes(
            c.orch, limit=500, should_yield=lambda: len(c.calls) > before
        )
        assert result["yielded"]
        # Reopen after every slice, keeping the provider's journal cell current.
        path = c.j.db_path
        old = c.j
        reopened = JournalStore(path)
        old.conn.close()
        old.conn = reopened.conn
        c.orch.journal = old
    assert set(c.calls) == {
        f"{prefix}{i}" for prefix in ("FWD", "HOLD", "BASE", "BASEH") for i in range(4)
    }
    assert len(c.calls) == 16
    assert c.j.count_rows("shadow_baseline_decisions", "replay_status='pending'") == 8
    assert c.j.count_rows("candidate_outcomes", "outcome_status_10d='partial'") == 4
    for family in ("baseline_v1_v1", "baseline_hold10_v1"):
        assert (
            c.j.one(
                "SELECT * FROM outcome_processing_cursors WHERE family=?", (family,)
            )["last_row_id"]
            > 0
        )


def test_stop_before_candidate_row_claim_does_not_skip_id(c):
    seed(c.j, "A", decision="2026-09-29T15:00:00Z")
    seed(c.j, "B", decision="2026-09-29T15:00:00Z")
    tracker.update_pending_outcomes(
        c.j,
        type("P", (), {"get_daily_bars": lambda *a: c.calls.append(a[1]) or []})(),
        should_yield=lambda: bool(c.calls),
    )
    cursor = c.j.one(
        "SELECT * FROM outcome_processing_cursors WHERE family='forward_replay_v1'"
    )
    assert cursor["last_row_id"] == 1
    assert (
        c.j.one("SELECT * FROM outcome_family_dispatch")["next_family"]
        == "hold1_10d_v1"
    )
    assert (
        c.j.one("SELECT * FROM candidate_outcomes WHERE symbol='B'")["updated_at_utc"]
        is None
    )


def test_unknown_baseline_arms_stay_pending_and_warning_remains(c):
    baseline(c.j, "UNKNOWN", "unrecognized_future_rule")
    before = c.j.query("SELECT * FROM shadow_baseline_decisions")
    result = work.run_cooperative_outcomes(c.orch, limit=5, should_yield=lambda: False)
    assert not result["yielded"]
    assert c.j.query("SELECT * FROM shadow_baseline_decisions") == before
    assert (
        c.j.count_rows("system_events", "category='baseline' AND severity='warning'")
        == 1
    )


@pytest.mark.parametrize(
    "bad", ["version", "domain", "next_stage", "revision", "future"]
)
def test_malformed_latest_turn_never_silently_restarts(c, bad):
    turns.claim_turn(c.j, "outer", "candidate", None)
    row = c.j.one(
        "SELECT * FROM system_events WHERE category='outcomes_work_turn_outer'"
    )
    data = json.loads(row["detail_json"])
    if bad == "future":
        c.j.conn.execute(
            "UPDATE system_events SET created_at_utc=? WHERE id=?",
            ("2099-01-01T00:00:00Z", row["id"]),
        )
    else:
        data[bad] = {
            "version": "future",
            "domain": "baseline",
            "next_stage": "foreign",
            "revision": True,
        }[bad]
        c.j.conn.execute(
            "UPDATE system_events SET detail_json=? WHERE id=?",
            (json.dumps(data), row["id"]),
        )
    c.j.conn.commit()
    with pytest.raises(ValueError):
        turns.turn(c.j, "outer")
    assert c.calls == []


def test_turn_compare_and_swap_refuses_stale_continuation(c):
    next_stage, old = turns.claim_turn(c.j, "outer", "candidate", None)
    turns.claim_turn(c.j, "outer", next_stage, old)
    with pytest.raises(RuntimeError, match="changed"):
        turns.claim_turn(c.j, "outer", next_stage, old)
    assert c.j.count_rows("system_events", "category='outcomes_work_turn_outer'") == 2


def test_yielded_does_not_erase_failure_fuse(c):
    for i, status in enumerate(("failed", "yielded", "failed", "yielded", "failed")):
        c.j.insert(
            "job_runs",
            {
                "job_run_id": str(i),
                "job_type": "outcomes_update",
                "status": status,
                "lock_key": str(i),
                "started_at_utc": NOW.isoformat(),
                "started_at_sgt": NOW.isoformat(),
            },
        )
    assert cadence.is_fused("outcomes_update", 3, c.j)[0]
    assert cadence.is_due("outcomes_update", c.s, c.j, NOW)[0]


def test_baseline_table_cursor_cannot_accept_unknown_or_candidate_family(c):
    from alphaos.baseline import tracker as base

    with pytest.raises(ValueError, match="cursor"):
        base._resolve_pending_rows_for_arm_set(
            c.j,
            object(),
            1,
            base.BASELINE_RULE_VERSIONS,
            3,
            should_yield=lambda: False,
            cursor_family="forward_replay_v1",
        )
    with pytest.raises(ValueError, match="family"):
        tracker._claim_outcome(c.j, "candidate_outcomes;drop", "1=1", (), None)
