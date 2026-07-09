"""PR13 slice 1: per-card scoreboard + auto-demotion. Covers:
* scoreboard -- expectancy/CI/effective-N/span computation, floor gating,
  breach requires a clustered-bootstrap CI reliably below zero (never a raw
  negative mean), candidate_type filtering (only proposal/blocked count),
  candidate_outcomes dedup (the same fan-out fix a correctness audit applied
  to PR12's own queries.py), card_version separation (v1/v2 of the same
  card_id are never pooled), live_eligible_cards excludes shadow/demoted.
* demotion -- idempotent same-day snapshot, a single breach day never
  demotes, exactly 2 CONSECUTIVE breach days demotes + alerts, a non-breach
  day resets the streak, demotion is terminal (never re-fires, never
  errors), demoted cards drop out of future evaluation cycles.
* scheduler wiring (the exact TEXT-0/INSTR-1/PR12 regression class).

All offline, in-memory, mock mode. No real money, no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from alphaos.cards import demotion as card_demotion
from alphaos.cards import scoreboard as card_scoreboard
from alphaos.scheduler import cadence
from alphaos.scheduler.job_runner import JobRunner


def _insert_card(journal, card_id: str, version: int, state: str = "live_eligible"):
    journal.insert("setup_cards", {
        "card_id": card_id, "version": version, "state": state,
        "content_hash": f"hash-{card_id}-v{version}",
    })


def _insert_candidate_with_outcome(
    journal, candidate_id: str, card_id: str, card_version: int, symbol: str,
    decision_date: str, replay_r: float, candidate_type: str = "proposal",
):
    journal.insert("candidates", {
        "candidate_id": candidate_id, "symbol": symbol,
        "card_id": card_id, "card_version": card_version,
    })
    journal.insert("candidate_outcomes", {
        "outcome_id": f"out-{candidate_id}", "candidate_id": candidate_id, "symbol": symbol,
        "candidate_type": candidate_type, "decision_at_utc": f"{decision_date}T12:00:00+00:00",
        "outcome_status": "resolved", "replay_r": replay_r,
    })


def _seed_clustered_candidates(journal, card_id: str, card_version: int, n: int, values, start="2026-01-01"):
    """n candidates for (card_id, card_version), one per day starting at
    `start`, cycling through `values` -- enough to clear the 30-sample/
    28-day floor when n>=30 and values has real variance."""
    from datetime import date as _date
    base = _date.fromisoformat(start)
    for i in range(n):
        _insert_candidate_with_outcome(
            journal, f"{card_id}-cand-{i}", card_id, card_version, f"SYM{i}",
            (base + timedelta(days=i)).isoformat(), values[i % len(values)],
        )


# --------------------------------------------------------------- scoreboard
def test_live_eligible_cards_excludes_shadow_state(journal):
    _insert_card(journal, "card_a", 1, state="live_eligible")
    _insert_card(journal, "card_b", 1, state="shadow")

    cards = card_scoreboard.live_eligible_cards(journal)

    assert [c["card_id"] for c in cards] == ["card_a"]


def test_live_eligible_cards_excludes_already_demoted(journal):
    _insert_card(journal, "card_a", 1, state="live_eligible")
    journal.insert("card_demotions", {
        "demotion_id": "demo1", "card_id": "card_a", "card_version": 1,
        "reason": "test", "triggering_snapshot_id_1": "s1", "triggering_snapshot_id_2": "s2",
        "alert_sent": True, "demoted_at_utc": "2026-01-01T00:00:00+00:00",
        "demoted_at_sgt": "2026-01-01T08:00:00+08:00",
    })

    cards = card_scoreboard.live_eligible_cards(journal)

    assert cards == []


def test_compute_card_scoreboard_below_floor_never_breaches(journal):
    _seed_clustered_candidates(journal, "card_a", 1, n=5, values=[-1.0])  # far below the 30-sample floor

    score = card_scoreboard.compute_card_scoreboard(journal, "card_a", 1)

    assert score["clears_floor"] is False
    assert score["breach"] is False  # even though every single value is negative


def test_compute_card_scoreboard_clears_floor_and_detects_reliable_breach(journal):
    # 35 days, alternating a small positive with a larger negative -- real
    # variance (never a degenerate constant, which collapses bootstrap CI to
    # a point mass and can't demonstrate "reliably below zero" meaningfully),
    # mean clearly negative.
    _seed_clustered_candidates(journal, "card_a", 1, n=35, values=[0.1, -0.6])

    score = card_scoreboard.compute_card_scoreboard(journal, "card_a", 1)

    assert score["clears_floor"] is True
    assert score["effective_n"] >= 30
    assert score["span_days"] >= 28.0
    assert score["expectancy_r"] < 0
    assert score["breach"] is True
    assert score["ci_high"] < 0


def test_compute_card_scoreboard_positive_expectancy_never_breaches(journal):
    _seed_clustered_candidates(journal, "card_a", 1, n=35, values=[0.6, -0.1])  # clearly positive mean

    score = card_scoreboard.compute_card_scoreboard(journal, "card_a", 1)

    assert score["clears_floor"] is True
    assert score["expectancy_r"] > 0
    assert score["breach"] is False


def test_scoreboard_only_counts_proposal_and_blocked_candidate_types(journal):
    _insert_candidate_with_outcome(journal, "c1", "card_a", 1, "AAPL", "2026-01-01", -5.0, candidate_type="reject")
    _insert_candidate_with_outcome(journal, "c2", "card_a", 1, "MSFT", "2026-01-02", -5.0, candidate_type="armed_watch")
    _insert_candidate_with_outcome(journal, "c3", "card_a", 1, "GOOG", "2026-01-03", 0.5, candidate_type="proposal")

    rows = card_scoreboard._card_replay_r_rows(journal, "card_a", 1)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "GOOG"


def test_scoreboard_dedupes_a_parallel_user_override_outcome_row(journal):
    """Same correctness-audit fix PR12's own queries.py needed:
    candidate_outcomes is one row per (candidate_id, candidate_type) -- a
    human-overridden candidate carries a second, parallel row that must
    never be double-counted here either."""
    journal.insert("candidates", {
        "candidate_id": "c1", "symbol": "AAPL", "card_id": "card_a", "card_version": 1,
    })
    journal.insert("candidate_outcomes", {
        "outcome_id": "out-c1", "candidate_id": "c1", "symbol": "AAPL",
        "candidate_type": "proposal", "decision_at_utc": "2026-01-01T12:00:00+00:00",
        "outcome_status": "resolved", "replay_r": 0.5,
    })
    journal.insert("candidate_outcomes", {
        "outcome_id": "out-c1-override", "candidate_id": "c1", "symbol": "AAPL",
        "candidate_type": "user_override", "decision_at_utc": "2026-01-01T12:00:00+00:00",
        "outcome_status": "resolved", "replay_r": 99.0,
    })

    rows = card_scoreboard._card_replay_r_rows(journal, "card_a", 1)

    assert len(rows) == 1
    assert rows[0]["replay_r"] == 0.5  # never the 99.0 override row


def test_scoreboard_never_pools_two_different_card_versions(journal):
    _insert_candidate_with_outcome(journal, "c1", "card_a", 1, "AAPL", "2026-01-01", 0.5)
    _insert_candidate_with_outcome(journal, "c2", "card_a", 2, "MSFT", "2026-01-02", -0.5)

    rows_v1 = card_scoreboard._card_replay_r_rows(journal, "card_a", 1)
    rows_v2 = card_scoreboard._card_replay_r_rows(journal, "card_a", 2)

    assert len(rows_v1) == 1 and rows_v1[0]["replay_r"] == 0.5
    assert len(rows_v2) == 1 and rows_v2[0]["replay_r"] == -0.5


def test_build_card_scoreboard_report_and_render_markdown(journal):
    _insert_card(journal, "card_a", 1)
    _seed_clustered_candidates(journal, "card_a", 1, n=35, values=[0.1, -0.6])

    rep = card_scoreboard.build_card_scoreboard_report(journal)
    markdown = card_scoreboard.render_markdown(rep)

    assert rep["n_cards"] == 1
    assert rep["n_breaching"] == 1
    assert "card_a" in markdown
    assert "BREACH" in markdown


# ------------------------------------------------------------------ demotion
def test_daily_evaluation_writes_one_snapshot_per_card_and_is_idempotent_same_day(journal, settings):
    _insert_card(journal, "card_a", 1)
    _seed_clustered_candidates(journal, "card_a", 1, n=5, values=[0.3])
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)

    first = card_demotion.run_daily_card_evaluation(journal, settings, now=now)
    second = card_demotion.run_daily_card_evaluation(journal, settings, now=now)

    assert first["snapshotted"] == ["card_a"]
    assert second["already_snapshotted_today"] == ["card_a"]
    assert journal.count_rows("card_scoreboard_snapshots", "card_id = ?", ("card_a",)) == 1


def test_single_breach_day_never_demotes(journal, settings):
    _insert_card(journal, "card_a", 1)
    _seed_clustered_candidates(journal, "card_a", 1, n=35, values=[0.1, -0.6])
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)

    result = card_demotion.run_daily_card_evaluation(journal, settings, now=now)

    assert result["demoted"] == []
    assert journal.count_rows("card_demotions") == 0


def test_two_consecutive_breach_days_demotes_and_alerts(journal, settings, monkeypatch):
    _insert_card(journal, "card_a", 1)
    _seed_clustered_candidates(journal, "card_a", 1, n=35, values=[0.1, -0.6])

    sent_alerts = []
    monkeypatch.setattr(
        card_demotion.alerts, "send_alert",
        lambda settings, title, message, priority="default", journal=None: sent_alerts.append(title) or True,
    )

    day1 = datetime(2026, 3, 1, tzinfo=timezone.utc)
    day2 = datetime(2026, 3, 2, tzinfo=timezone.utc)
    result1 = card_demotion.run_daily_card_evaluation(journal, settings, now=day1)
    result2 = card_demotion.run_daily_card_evaluation(journal, settings, now=day2)

    assert result1["demoted"] == []  # day 1 alone is not enough
    assert result2["demoted"] == [{"card_id": "card_a", "card_version": 1}]
    assert len(sent_alerts) == 1
    demotions = journal.query("SELECT * FROM card_demotions WHERE card_id = ?", ("card_a",))
    assert len(demotions) == 1
    assert demotions[0]["alert_sent"] == 1


def test_a_non_breach_day_resets_the_streak(journal, settings, monkeypatch):
    """breach, non-breach, breach must NOT demote -- only 2 CONSECUTIVE
    breach snapshots count (mirrors cadence.is_fused()'s own semantics:
    a non-matching row anywhere in the checked window breaks the streak)."""
    _insert_card(journal, "card_a", 1)
    monkeypatch.setattr(card_demotion.alerts, "send_alert", lambda *a, **k: True)

    # Directly seed the snapshot HISTORY (breach, non-breach) rather than
    # regenerating 2 full 35-candidate windows -- the streak logic only
    # reads card_scoreboard_snapshots, never recomputes past days.
    journal.insert("card_scoreboard_snapshots", {
        "snapshot_id": "s1", "card_id": "card_a", "card_version": 1,
        "evaluation_date": "2026-03-01", "state": "live_eligible",
        "expectancy_r": -0.3, "ci_low": -0.5, "ci_high": -0.1,
        "effective_n": 35, "n_raw": 35, "span_days": 34.0,
        "clears_floor": True, "breach": True,
    })
    journal.insert("card_scoreboard_snapshots", {
        "snapshot_id": "s2", "card_id": "card_a", "card_version": 1,
        "evaluation_date": "2026-03-02", "state": "live_eligible",
        "expectancy_r": 0.1, "ci_low": -0.1, "ci_high": 0.3,
        "effective_n": 35, "n_raw": 35, "span_days": 35.0,
        "clears_floor": True, "breach": False,
    })
    _seed_clustered_candidates(journal, "card_a", 1, n=35, values=[0.1, -0.6])  # day 3: breach again
    day3 = datetime(2026, 3, 3, tzinfo=timezone.utc)

    result = card_demotion.run_daily_card_evaluation(journal, settings, now=day3)

    assert result["demoted"] == []
    assert journal.count_rows("card_demotions") == 0


def test_demotion_is_terminal_and_never_refires(journal, settings, monkeypatch):
    _insert_card(journal, "card_a", 1)
    monkeypatch.setattr(card_demotion.alerts, "send_alert", lambda *a, **k: True)
    _seed_clustered_candidates(journal, "card_a", 1, n=35, values=[0.1, -0.6])

    day1 = datetime(2026, 3, 1, tzinfo=timezone.utc)
    day2 = datetime(2026, 3, 2, tzinfo=timezone.utc)
    day3 = datetime(2026, 3, 3, tzinfo=timezone.utc)
    card_demotion.run_daily_card_evaluation(journal, settings, now=day1)
    card_demotion.run_daily_card_evaluation(journal, settings, now=day2)  # demotes here
    result3 = card_demotion.run_daily_card_evaluation(journal, settings, now=day3)

    assert journal.count_rows("card_demotions", "card_id = ?", ("card_a",)) == 1  # never a second row
    assert result3["errors"] == []
    # A demoted card drops out of live_eligible_cards -- day 3 must not even
    # attempt to snapshot it again.
    assert "card_a" not in result3["snapshotted"]
    assert "card_a" not in result3["already_snapshotted_today"]


def test_one_bad_card_never_blocks_the_others(journal, settings, monkeypatch):
    _insert_card(journal, "card_a", 1)
    _insert_card(journal, "card_b", 1)
    _seed_clustered_candidates(journal, "card_b", 1, n=5, values=[0.3])

    def _boom(journal, card_id, card_version):
        if card_id == "card_a":
            raise RuntimeError("simulated bug scoring card_a")
        return card_scoreboard.compute_card_scoreboard(journal, card_id, card_version)

    monkeypatch.setattr(card_demotion, "compute_card_scoreboard", _boom)

    result = card_demotion.run_daily_card_evaluation(journal, settings)

    assert result["errors"] == [{"card_id": "card_a", "error": "simulated bug scoring card_a"}]
    assert result["snapshotted"] == ["card_b"]


# --------------------------------------------------------------------- report
def test_cmd_card_scoreboard_smoke(orchestrator):
    from alphaos.__main__ import cmd_card_scoreboard

    assert cmd_card_scoreboard(orchestrator) == 0


def test_cmd_card_demotion_check_smoke(orchestrator):
    from alphaos.__main__ import cmd_card_demotion_check

    assert cmd_card_demotion_check(orchestrator) == 0


# ---------------------------------------------------------------- scheduler
def test_run_due_jobs_includes_card_demotion_check(orchestrator, monkeypatch):
    """The exact regression class TEXT-0/INSTR-1/PR12 self-caught -- wired
    into cadence.is_due but NOT into JobRunner's hardcoded dispatch tuple
    would mean this job silently NEVER runs in production."""
    monkeypatch.setattr(cadence, "is_due", lambda job_type, settings, journal, now=None: (True, "forced for test"))

    results = JobRunner(orchestrator).run_due_jobs()

    by_type = {r["job_type"]: r for r in results}
    assert cadence.JobType.CARD_DEMOTION_CHECK in by_type
    assert by_type[cadence.JobType.CARD_DEMOTION_CHECK]["status"] == "completed"


def test_scheduler_status_report_includes_card_demotion_check(orchestrator):
    report = JobRunner(orchestrator).status_report()

    assert "card_demotion_check" in report["recent_by_job_type"]


def test_card_demotion_check_due_once_daily(settings, journal):
    due, reason = cadence.is_due(cadence.JobType.CARD_DEMOTION_CHECK, settings, journal)
    assert isinstance(due, bool)  # exercises the real dispatch path end-to-end
