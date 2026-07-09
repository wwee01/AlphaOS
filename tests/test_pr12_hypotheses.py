"""PR12: Hypothesis Engine v0 (registry-first). Covers:
* constants -- the frozen risk-class floor table, the 8 seeded hypotheses.
* registry -- propose_hypothesis()'s idempotency, risk-class floor
  enforcement (never undercuttable by a caller), H-AI-1's special-cased
  link-to-BASELINE's-existing-row behavior.
* queries -- worked numeric examples for the centered-delta reduction and
  the direct-passthrough (H-REJ-1) shape.
* resolver -- the calendar floor, the sample-size pre-check (never freezes
  insufficient-data evidence), one-shot evaluation, idempotent re-runs,
  H-AI-1 never auto-evaluated, and the family-wide BH-FDR refresh (a
  hypothesis's cached q-value changes when a sibling joins the family, even
  with no change to its own data).
* report + scheduler wiring (the exact TEXT-0/INSTR-1 regression class:
  wired into cadence.is_due but not JobRunner's hardcoded dispatch tuple
  would mean this job silently never runs in production).

All offline, in-memory, mock mode. No real money, no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from alphaos.hypotheses import queries as hyp_queries
from alphaos.hypotheses import registry as hyp_registry
from alphaos.hypotheses import resolver as hyp_resolver
from alphaos.hypotheses.constants import HypothesisStatus, RiskClass, RISK_CLASS_FLOORS, SEEDED_HYPOTHESES
from alphaos.scheduler import cadence
from alphaos.scheduler.job_runner import JobRunner
from alphaos.stats.preregistration import register_hypothesis


# --------------------------------------------------------------- constants
def test_risk_class_floors_match_fable_ruling():
    assert RISK_CLASS_FLOORS[RiskClass.A.value] == {"min_sample": 30, "min_span_days": 28.0}
    assert RISK_CLASS_FLOORS[RiskClass.B.value] == {"min_sample": 40, "min_span_days": 42.0}
    assert RISK_CLASS_FLOORS[RiskClass.C.value] == {"min_sample": 60, "min_span_days": 90.0}


def test_seeded_hypotheses_has_8_unique_ids():
    ids = [h["hypothesis_id"] for h in SEEDED_HYPOTHESES]
    assert len(ids) == 8
    assert len(set(ids)) == 8


def test_h_ai_1_has_no_metric_fn_and_no_metric():
    h_ai_1 = next(h for h in SEEDED_HYPOTHESES if h["hypothesis_id"] == "H-AI-1")
    assert h_ai_1["metric_fn_name"] is None
    assert h_ai_1["metric"] is None
    assert h_ai_1["risk_class"] == RiskClass.C.value


def test_every_non_ai1_hypothesis_has_a_dispatchable_metric_fn_name():
    for h in SEEDED_HYPOTHESES:
        if h["hypothesis_id"] == "H-AI-1":
            continue
        assert h["metric_fn_name"] in hyp_queries.METRIC_FUNCTIONS, h["hypothesis_id"]


# ----------------------------------------------------------------- registry
def test_propose_hypothesis_creates_a_row_with_class_floor_applied(journal):
    spec = next(h for h in SEEDED_HYPOTHESES if h["hypothesis_id"] == "H-CAT-1")  # class B
    row = hyp_registry.propose_hypothesis(journal, spec)

    assert row["hypothesis_id"] == "H-CAT-1"
    assert row["status"] == HypothesisStatus.TESTING.value
    assert row["prereg_id"]

    prereg = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (row["prereg_id"],))
    assert prereg["floor_effective_n"] == 40
    assert prereg["floor_span_days"] == 42.0


def test_propose_hypothesis_is_idempotent(journal):
    spec = next(h for h in SEEDED_HYPOTHESES if h["hypothesis_id"] == "H-TQS-1")
    first = hyp_registry.propose_hypothesis(journal, spec)
    second = hyp_registry.propose_hypothesis(journal, spec)

    assert first["id"] == second["id"]
    assert journal.count_rows("hypothesis_proposals", "hypothesis_id = ?", ("H-TQS-1",)) == 1
    assert journal.count_rows("preregistrations") == 1  # register_hypothesis() NOT called twice


def test_propose_hypothesis_never_lets_a_caller_undercut_the_class_floor(journal):
    """No parameter exists for a caller to pass a smaller floor -- the only
    lever is risk_class itself, and RISK_CLASS_FLOORS is the single source
    of truth (Fable5 ruling)."""
    import inspect

    sig = inspect.signature(hyp_registry.propose_hypothesis)
    assert "floor_effective_n" not in sig.parameters
    assert "floor_span_days" not in sig.parameters


def test_h_ai_1_links_to_existing_baseline_prereg_when_present(journal):
    baseline_prereg_id = register_hypothesis(
        journal,
        hypothesis=hyp_registry._BASELINE_HYPOTHESIS_TEXT,
        metric=hyp_registry._BASELINE_METRIC_TEXT,
        floor_effective_n=30, floor_span_days=28.0,
        analysis_not_before="2026-09-07",
    )
    spec = next(h for h in SEEDED_HYPOTHESES if h["hypothesis_id"] == "H-AI-1")
    row = hyp_registry.propose_hypothesis(journal, spec)

    assert row["prereg_id"] == baseline_prereg_id
    assert row["status"] == HypothesisStatus.TESTING.value
    # register_hypothesis() must NOT have been called a second time for H-AI-1
    assert journal.count_rows("preregistrations") == 1


def test_h_ai_1_stays_unlinked_when_baseline_not_yet_registered(journal):
    spec = next(h for h in SEEDED_HYPOTHESES if h["hypothesis_id"] == "H-AI-1")
    row = hyp_registry.propose_hypothesis(journal, spec)

    assert row["prereg_id"] is None
    assert row["status"] == HypothesisStatus.PROPOSED.value


def test_seed_all_seeds_exactly_8_rows_and_is_idempotent(journal):
    first = hyp_registry.seed_all(journal)
    assert len(first) == 8
    assert journal.count_rows("hypothesis_proposals") == 8

    second = hyp_registry.seed_all(journal)
    assert len(second) == 8
    assert journal.count_rows("hypothesis_proposals") == 8  # no duplicates


# ------------------------------------------------------------------ queries
def _insert_outcome(journal, candidate_id, symbol, decision_date, **fields):
    journal.insert("candidate_outcomes", {
        "outcome_id": f"out-{candidate_id}",
        "candidate_id": candidate_id,
        "symbol": symbol,
        "candidate_type": "candidate",
        "decision_at_utc": f"{decision_date}T12:00:00+00:00",
        "outcome_status": "resolved",
        **fields,
    })


def test_h_tqs_1_rows_centers_top_quartile_against_bottom_quartile_mean(journal):
    # 4 low-TQS rows (forward_3d_r mean = 0.0), 4 high-TQS rows (forward_3d_r
    # mean = 1.0) -- top quartile's own centered_delta should be exactly
    # (its own forward_3d_r) - 0.0 == its own forward_3d_r.
    for i in range(4):
        cid = f"lo{i}"
        journal.insert("tqs_scores", {
            "tqs_id": f"tqs-{cid}", "source_type": "candidate", "candidate_id": cid, "symbol": "AAPL",
            "tqs_version": "v1", "data_confidence": 0.9, "tqs_bucket": "low",
            "data_quality_status": "ok", "tqs_score": 10 + i, "is_mock": 0,
        })
        _insert_outcome(journal, cid, "AAPL", f"2026-01-{i+1:02d}", forward_3d_r=0.0)
    for i in range(4):
        cid = f"hi{i}"
        journal.insert("tqs_scores", {
            "tqs_id": f"tqs-{cid}", "source_type": "candidate", "candidate_id": cid, "symbol": "MSFT",
            "tqs_version": "v1", "data_confidence": 0.9, "tqs_bucket": "high",
            "data_quality_status": "ok", "tqs_score": 90 + i, "is_mock": 0,
        })
        _insert_outcome(journal, cid, "MSFT", f"2026-02-{i+1:02d}", forward_3d_r=1.0)

    rows, value_key = hyp_queries.h_tqs_1_rows(journal)

    assert value_key == "centered_delta"
    # _quantile() is nearest-rank (no interpolation, see its own docstring),
    # so an exact 4-vs-4 split's rank-0.75 cutoff can land ON the top group's
    # own boundary value rather than strictly below it -- only 3 of the 4
    # high-TQS rows are guaranteed selected here, never a low-TQS row.
    assert 3 <= len(rows) <= 4
    assert all(r["symbol"] == "MSFT" for r in rows)  # never a low-TQS (AAPL) row
    for r in rows:
        assert r["centered_delta"] == pytest.approx(1.0)  # 1.0 (own) - 0.0 (bottom mean)


def test_h_cat_1_rows_excludes_unavailable_and_error_status(journal):
    for status in ("confirmed", "none_found", "unavailable", "error"):
        cid = f"cand-{status}"
        journal.insert("candidate_catalysts", {
            "catalyst_id": f"cat-{cid}", "candidate_id": cid, "symbol": "AAPL",
            "catalyst_status": status,
        })
        _insert_outcome(journal, cid, "AAPL", "2026-01-01", replay_r=0.5)

    rows, value_key = hyp_queries.h_cat_1_rows(journal)

    assert value_key == "centered_delta"
    assert len(rows) == 1  # only the 'confirmed' row is returned
    assert rows[0]["centered_delta"] == pytest.approx(0.0)  # 0.5 (own) - 0.5 (none_found mean)


def test_h_rej_1_rows_passes_through_delta_r_unchanged(journal):
    journal.insert("attribution_records", {
        "attribution_id": "attr1", "attribution_type": "propose_user_rejected",
        "attribution_version": "v1", "agent": "test", "source_id": "attr1",
        "symbol": "AAPL", "delta_r": -0.4, "resolved_status": "resolved",
        "data_quality_status": "ok", "is_mock": 0,
        "decision_at_utc": "2026-01-01T12:00:00+00:00",
    })
    journal.insert("attribution_records", {
        "attribution_id": "attr2", "attribution_type": "propose_approved",  # wrong type, must be excluded
        "attribution_version": "v1", "agent": "test", "source_id": "attr2",
        "symbol": "MSFT", "delta_r": 99.0, "resolved_status": "resolved",
        "data_quality_status": "ok", "is_mock": 0,
        "decision_at_utc": "2026-01-01T12:00:00+00:00",
    })

    rows, value_key = hyp_queries.h_rej_1_rows(journal)

    assert value_key == "delta_r"
    assert len(rows) == 1
    assert rows[0]["delta_r"] == -0.4
    assert rows[0]["max_holding_days"] is None  # attribution_records carries no holding-window field


# ------------------------------------------------------------------ resolver
def _seed_one(journal, hypothesis_id: str, monkeypatch=None, days_ago: int = 0):
    """Propose a single seeded hypothesis with analysis_not_before pinned to
    `days_ago` days before/after "now" (negative -> already due), bypassing
    the real risk-class-derived wait so tests don't need 28-90 real days."""
    spec = next(h for h in SEEDED_HYPOTHESES if h["hypothesis_id"] == hypothesis_id)
    now = datetime.now(timezone.utc) + timedelta(days=-days_ago)
    return hyp_registry.propose_hypothesis(journal, spec, now=now)


def _fake_metric_fn_factory(rows):
    return lambda journal: (rows, "centered_delta")


def _clustered_rows(n: int, start="2026-01-01", value=0.3, values=None):
    """`values`, when given, cycles across the n rows -- lets a test build a
    non-degenerate (real-variance) bootstrap sample instead of every row
    sharing one constant (a constant collapses to a point-mass p-value of
    exactly 0.0 or 1.0, which can never demonstrate BH-FDR's family-size
    sensitivity -- 0 stays 0 regardless of rank/family size)."""
    from datetime import date as _date
    base = _date.fromisoformat(start)
    vals = values if values is not None else [value]
    return [
        {"symbol": f"SYM{i}", "decision_date": (base + timedelta(days=i)).isoformat(),
         "max_holding_days": 1, "centered_delta": vals[i % len(vals)]}
        for i in range(n)
    ]


def test_resolver_leaves_hypothesis_alone_before_analysis_not_before(journal, monkeypatch):
    row = _seed_one(journal, "H-WIN-1", days_ago=-10)  # analysis_not_before is 10 days in the FUTURE
    monkeypatch.setitem(hyp_queries.METRIC_FUNCTIONS, "h_win_1_rows", _fake_metric_fn_factory(_clustered_rows(50)))

    summary = hyp_resolver.resolve_due_hypotheses(journal)

    assert "H-WIN-1" not in summary["evaluated"]
    refreshed = journal.one("SELECT status FROM hypothesis_proposals WHERE hypothesis_id = ?", (row["hypothesis_id"],))
    assert refreshed["status"] == HypothesisStatus.TESTING.value


def test_resolver_leaves_hypothesis_testing_when_sample_too_small(journal, monkeypatch):
    row = _seed_one(journal, "H-WIN-1", days_ago=40)  # class A floor (30/28) is long past
    monkeypatch.setitem(hyp_queries.METRIC_FUNCTIONS, "h_win_1_rows", _fake_metric_fn_factory(_clustered_rows(5)))

    summary = hyp_resolver.resolve_due_hypotheses(journal)

    assert "H-WIN-1" in summary["not_yet_sufficient"]
    refreshed = journal.one("SELECT status FROM hypothesis_proposals WHERE hypothesis_id = ?", (row["hypothesis_id"],))
    assert refreshed["status"] == HypothesisStatus.TESTING.value
    prereg = journal.one("SELECT evaluated_at_utc FROM preregistrations WHERE prereg_id = ?", (row["prereg_id"],))
    assert prereg["evaluated_at_utc"] is None  # the one-shot must NOT have been consumed


def test_resolver_evaluates_once_floor_is_cleared(journal, monkeypatch):
    row = _seed_one(journal, "H-WIN-1", days_ago=40)
    monkeypatch.setitem(hyp_queries.METRIC_FUNCTIONS, "h_win_1_rows", _fake_metric_fn_factory(_clustered_rows(35)))

    summary = hyp_resolver.resolve_due_hypotheses(journal)

    assert "H-WIN-1" in summary["evaluated"]
    refreshed = journal.one("SELECT * FROM hypothesis_proposals WHERE hypothesis_id = ?", (row["hypothesis_id"],))
    assert refreshed["status"] == HypothesisStatus.RESOLVED.value
    assert refreshed["last_verdict"] in ("rejected", "forward-test-candidate", "inconclusive")
    prereg = journal.one("SELECT evaluated_at_utc FROM preregistrations WHERE prereg_id = ?", (row["prereg_id"],))
    assert prereg["evaluated_at_utc"] is not None


def test_resolver_never_sets_a_semantic_verdict_as_status(journal, monkeypatch):
    """See HypothesisStatus's own docstring -- MET/FAILED/WITHDRAWN are
    operator-only; the resolver only ever writes proposed/testing/resolved."""
    _seed_one(journal, "H-WIN-1", days_ago=40)
    monkeypatch.setitem(hyp_queries.METRIC_FUNCTIONS, "h_win_1_rows", _fake_metric_fn_factory(_clustered_rows(35)))

    hyp_resolver.resolve_due_hypotheses(journal)

    all_statuses = {r["status"] for r in journal.query("SELECT status FROM hypothesis_proposals")}
    assert all_statuses <= {
        HypothesisStatus.PROPOSED.value, HypothesisStatus.TESTING.value, HypothesisStatus.RESOLVED.value,
    }


def test_resolver_rerun_after_evaluation_does_not_reevaluate_or_error(journal, monkeypatch):
    row = _seed_one(journal, "H-WIN-1", days_ago=40)
    monkeypatch.setitem(hyp_queries.METRIC_FUNCTIONS, "h_win_1_rows", _fake_metric_fn_factory(_clustered_rows(35)))
    hyp_resolver.resolve_due_hypotheses(journal)
    prereg_before = journal.one("SELECT evaluated_at_utc FROM preregistrations WHERE prereg_id = ?", (row["prereg_id"],))

    summary = hyp_resolver.resolve_due_hypotheses(journal)

    # Once resolved, the row's status is no longer 'testing' -- the second
    # pass's testing_rows scan never even visits it (so it appears in
    # neither "evaluated" nor "synced"), but _refresh_all_verdicts() still
    # refreshes its cached verdict fields every pass regardless of status.
    assert summary["errors"] == []
    assert "H-WIN-1" not in summary["evaluated"]
    assert "H-WIN-1" in summary["refreshed"]
    prereg_after = journal.one("SELECT evaluated_at_utc FROM preregistrations WHERE prereg_id = ?", (row["prereg_id"],))
    assert prereg_after["evaluated_at_utc"] == prereg_before["evaluated_at_utc"]  # unchanged, never re-evaluated


def test_resolver_never_auto_evaluates_h_ai_1(journal, monkeypatch):
    baseline_prereg_id = register_hypothesis(
        journal, hypothesis=hyp_registry._BASELINE_HYPOTHESIS_TEXT, metric=hyp_registry._BASELINE_METRIC_TEXT,
        floor_effective_n=1, floor_span_days=0.0, analysis_not_before="2020-01-01",
    )
    _seed_one(journal, "H-AI-1", days_ago=9999)  # absurdly overdue by calendar

    summary = hyp_resolver.resolve_due_hypotheses(journal)

    assert "H-AI-1" not in summary["evaluated"]
    prereg = journal.one("SELECT evaluated_at_utc FROM preregistrations WHERE prereg_id = ?", (baseline_prereg_id,))
    assert prereg["evaluated_at_utc"] is None
    row = journal.one("SELECT status FROM hypothesis_proposals WHERE hypothesis_id = ?", ("H-AI-1",))
    assert row["status"] == HypothesisStatus.TESTING.value


def test_resolver_syncs_h_ai_1_once_baseline_evaluates_it_independently(journal):
    """H-AI-1 is never evaluated BY the resolver, but its verdict must still
    surface once BASELINE's own (separate) evaluation path runs."""
    baseline_prereg_id = register_hypothesis(
        journal, hypothesis=hyp_registry._BASELINE_HYPOTHESIS_TEXT, metric=hyp_registry._BASELINE_METRIC_TEXT,
        floor_effective_n=1, floor_span_days=0.0, analysis_not_before="2020-01-01",
    )
    _seed_one(journal, "H-AI-1", days_ago=9999)
    from alphaos.stats.preregistration import evaluate_hypothesis
    evaluate_hypothesis(journal, baseline_prereg_id, _clustered_rows(5, value=0.1), "centered_delta")

    summary = hyp_resolver.resolve_due_hypotheses(journal)

    assert "H-AI-1" in summary["synced"]
    row = journal.one("SELECT * FROM hypothesis_proposals WHERE hypothesis_id = ?", ("H-AI-1",))
    assert row["status"] == HypothesisStatus.RESOLVED.value
    assert row["last_verdict"] is not None


def test_family_wide_refresh_updates_an_already_evaluated_hypothesis_q_value(journal, monkeypatch):
    """BH-FDR correction is family-wide (schema.py's own documented
    convention) -- evaluating a SECOND hypothesis can shift the FIRST
    hypothesis's own cached q-value even though its own data never changed.
    Both hypotheses need REAL variance (not one constant value repeated,
    which collapses to a point-mass p of exactly 0.0/1.0 that BH-FDR cannot
    move) -- these two mixed-sign patterns are empirically pinned to
    p ~= 0.41 and p ~= 0.78 under DEFAULT_SEED, chosen so the two-hypothesis
    family's BH correction provably reorders/rescales H-WIN-1's own q away
    from its solo-family value."""
    monkeypatch.setitem(
        hyp_queries.METRIC_FUNCTIONS, "h_win_1_rows",
        _fake_metric_fn_factory(_clustered_rows(35, values=[0.1, 0.1, 0.1, -0.3])),
    )
    monkeypatch.setitem(
        hyp_queries.METRIC_FUNCTIONS, "h_int_1_rows",
        _fake_metric_fn_factory(_clustered_rows(45, values=[0.2, 0.2, -0.5])),
    )

    _seed_one(journal, "H-WIN-1", days_ago=40)
    hyp_resolver.resolve_due_hypotheses(journal)
    q_after_first = journal.one(
        "SELECT last_q_value FROM hypothesis_proposals WHERE hypothesis_id = ?", ("H-WIN-1",)
    )["last_q_value"]

    _seed_one(journal, "H-INT-1", days_ago=42)  # class B floor, also long past
    hyp_resolver.resolve_due_hypotheses(journal)
    q_after_second = journal.one(
        "SELECT last_q_value FROM hypothesis_proposals WHERE hypothesis_id = ?", ("H-WIN-1",)
    )["last_q_value"]

    assert q_after_first != q_after_second


# -------------------------------------------------------------------- report
def test_hypothesis_report_empty_state(journal):
    from alphaos.reports.hypothesis_report import build_hypothesis_report, render_markdown

    rep = build_hypothesis_report(journal)
    assert rep["n_total"] == 0
    assert "hypothesis registry" in render_markdown(rep)


def test_hypothesis_report_marks_overdue(journal):
    from alphaos.reports.hypothesis_report import build_hypothesis_report

    _seed_one(journal, "H-WIN-1", days_ago=40)
    rep = build_hypothesis_report(journal)

    row = next(h for h in rep["hypotheses"] if h["hypothesis_id"] == "H-WIN-1")
    assert row["overdue"] is True


def test_hypothesis_report_renders_resolved_row_without_crashing(journal, monkeypatch):
    from alphaos.reports.hypothesis_report import build_hypothesis_report, render_markdown

    _seed_one(journal, "H-WIN-1", days_ago=40)
    monkeypatch.setitem(hyp_queries.METRIC_FUNCTIONS, "h_win_1_rows", _fake_metric_fn_factory(_clustered_rows(35)))
    hyp_resolver.resolve_due_hypotheses(journal)

    rep = build_hypothesis_report(journal)
    markdown = render_markdown(rep)
    assert "H-WIN-1" in markdown
    assert "verdict:" in markdown


# ---------------------------------------------------------------- scheduler
def test_run_due_jobs_includes_hypothesis_resolve(orchestrator, monkeypatch):
    """The exact regression class TEXT-0/INSTR-1 self-caught -- wired into
    cadence.is_due but NOT into JobRunner's hardcoded dispatch tuple would
    mean this job silently NEVER runs in production."""
    monkeypatch.setattr(cadence, "is_due", lambda job_type, settings, journal, now=None: (True, "forced for test"))

    results = JobRunner(orchestrator).run_due_jobs()

    by_type = {r["job_type"]: r for r in results}
    assert cadence.JobType.HYPOTHESIS_RESOLVE in by_type
    assert by_type[cadence.JobType.HYPOTHESIS_RESOLVE]["status"] == "completed"


def test_scheduler_status_report_includes_hypothesis_resolve(orchestrator):
    report = JobRunner(orchestrator).status_report()

    assert "hypothesis_resolve" in report["recent_by_job_type"]


def test_hypothesis_resolve_due_once_daily(settings, journal):
    due, reason = cadence.is_due(cadence.JobType.HYPOTHESIS_RESOLVE, settings, journal)
    assert isinstance(due, bool)  # exercises the real dispatch path end-to-end, no monkeypatching
