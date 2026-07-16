"""EVID-1: per-setup-version evidence report. Covers:
* all_registered_setups includes shadow/demoted versions (unlike
  scoreboard.py's live_eligible_cards -- a full-family report never hides a
  non-live setup),
* compute_setup_metric_stats generalizes scoreboard.py's own floor/CI/
  effective-N machinery to any metric (replay_r or the new
  market_adjusted_return_*d_pct columns) and any population,
  unknown-metric guard,
* population_breakdown separates populations rather than blending them
  (the Edge Lab audit's own "did rejects outperform approvals?" question),
* build_setup_evidence_report applies BH-FDR only to setups that clear
  their own floor, and includes the full family (not just live_eligible).

All offline, in-memory, mock mode. No real money, no network.
"""

from __future__ import annotations

from datetime import date as _date
from datetime import timedelta

from alphaos.cards import setup_evidence


def _insert_card(journal, card_id: str, version: int, state: str = "live_eligible"):
    journal.insert("setup_cards", {
        "card_id": card_id, "version": version, "state": state,
        "content_hash": f"hash-{card_id}-v{version}",
    })


def _insert_candidate_with_outcome(
    journal, candidate_id: str, card_id: str, card_version: int, symbol: str,
    decision_date: str, candidate_type: str = "proposal", **metric_values,
):
    journal.insert("candidates", {
        "candidate_id": candidate_id, "symbol": symbol,
        "card_id": card_id, "card_version": card_version,
    })
    journal.insert("candidate_outcomes", {
        "outcome_id": f"out-{candidate_id}", "candidate_id": candidate_id, "symbol": symbol,
        "candidate_type": candidate_type, "decision_at_utc": f"{decision_date}T12:00:00+00:00",
        "outcome_status": "complete", **metric_values,
    })


def _seed_clustered(journal, card_id, card_version, n, values, metric_key, start="2026-01-01", **fixed):
    """n candidates for (card_id, card_version), one per day starting at
    `start`, cycling through `values` for `metric_key` -- enough to clear
    the 30-sample/28-day floor when n>=30 and values has real variance.
    candidate_id includes `start` so two calls seeding different populations
    (e.g. proposal vs reject) for the SAME card never collide."""
    base = _date.fromisoformat(start)
    for i in range(n):
        _insert_candidate_with_outcome(
            journal, f"{card_id}-{start}-cand-{i}", card_id, card_version, f"SYM{i}",
            (base + timedelta(days=i)).isoformat(),
            **{metric_key: values[i % len(values)]}, **fixed,
        )


# --------------------------------------------------------- all_registered_setups
def test_all_registered_setups_includes_shadow_and_demoted(journal):
    _insert_card(journal, "card_a", 1, state="live_eligible")
    _insert_card(journal, "card_b", 1, state="shadow")
    _insert_card(journal, "card_c", 1, state="live_eligible")
    journal.insert("card_demotions", {
        "demotion_id": "demo1", "card_id": "card_c", "card_version": 1,
        "reason": "test", "triggering_snapshot_id_1": "s1", "triggering_snapshot_id_2": "s2",
        "alert_sent": True, "demoted_at_utc": "2026-01-01T00:00:00+00:00",
        "demoted_at_sgt": "2026-01-01T08:00:00+08:00",
    })

    setups = setup_evidence.all_registered_setups(journal)

    assert {(s["card_id"], s["card_version"]) for s in setups} == {
        ("card_a", 1), ("card_b", 1), ("card_c", 1),
    }


# ------------------------------------------------------ compute_setup_metric_stats
def test_compute_setup_metric_stats_below_floor(journal):
    _seed_clustered(journal, "card_a", 1, n=5, values=[-1.0], metric_key="replay_r")

    stats = setup_evidence.compute_setup_metric_stats(journal, "card_a", 1, "replay_r")

    assert stats["clears_floor"] is False


def test_compute_setup_metric_stats_clears_floor_and_computes_p_value(journal):
    _seed_clustered(journal, "card_a", 1, n=35, values=[0.1, -0.6], metric_key="replay_r")

    stats = setup_evidence.compute_setup_metric_stats(journal, "card_a", 1, "replay_r")

    assert stats["clears_floor"] is True
    assert stats["effective_n"] >= 30
    assert stats["span_days"] >= 28.0
    assert stats["point_estimate"] < 0
    assert stats["ci_high"] < 0
    assert stats["one_sided_p_below_zero"] is not None


def test_compute_setup_metric_stats_generalizes_to_market_adjusted_return(journal):
    """The whole point of this module vs scoreboard.py: it must bootstrap
    market_adjusted_return_5d_pct just as readily as replay_r -- a
    candidate_outcomes column scoreboard.py's own hardcoded query never
    reads at all."""
    _seed_clustered(
        journal, "card_a", 1, n=35, values=[0.02, -0.01],
        metric_key="market_adjusted_return_5d_pct",
    )

    stats = setup_evidence.compute_setup_metric_stats(
        journal, "card_a", 1, "market_adjusted_return_5d_pct",
    )

    assert stats["metric"] == "market_adjusted_return_5d_pct"
    assert stats["clears_floor"] is True
    assert stats["point_estimate"] is not None


def test_compute_setup_metric_stats_unknown_metric_raises(journal):
    try:
        setup_evidence.compute_setup_metric_stats(journal, "card_a", 1, "not_a_real_column")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_compute_setup_metric_stats_dedupes_user_override_row(journal):
    """Same fan-out fix as scoreboard.py's own _card_replay_r_rows()."""
    journal.insert("candidates", {
        "candidate_id": "c1", "symbol": "AAPL", "card_id": "card_a", "card_version": 1,
    })
    journal.insert("candidate_outcomes", {
        "outcome_id": "out-c1", "candidate_id": "c1", "symbol": "AAPL",
        "candidate_type": "proposal", "decision_at_utc": "2026-01-01T12:00:00+00:00",
        "outcome_status": "complete", "replay_r": 0.5,
    })
    journal.insert("candidate_outcomes", {
        "outcome_id": "out-c1-override", "candidate_id": "c1", "symbol": "AAPL",
        "candidate_type": "user_override", "decision_at_utc": "2026-01-01T12:00:00+00:00",
        "outcome_status": "complete", "replay_r": 99.0,
    })

    rows = setup_evidence._rows_for_setup(journal, "card_a", 1, ("proposal", "blocked"))

    assert len(rows) == 1
    assert rows[0]["replay_r"] == 0.5   # never the 99.0 override row


# ------------------------------------------------------------- population_breakdown
def test_population_breakdown_separates_populations_not_blended(journal):
    """The Edge Lab audit's own Stage-7 question: did rejects outperform
    approvals? Must be answerable directly, never hidden inside one blended
    number."""
    _seed_clustered(
        journal, "card_a", 1, n=35, values=[1.0], metric_key="replay_r",
        candidate_type="proposal",
    )
    _seed_clustered(
        journal, "card_a", 1, n=35, values=[-1.0], metric_key="replay_r",
        start="2026-03-01", candidate_type="reject",
    )

    rep = setup_evidence.population_breakdown(journal, "card_a", 1, "replay_r")

    assert rep["by_population"]["proposal"]["point_estimate"] > 0
    assert rep["by_population"]["reject"]["point_estimate"] < 0


# ---------------------------------------------------- build_setup_evidence_report
def test_build_setup_evidence_report_bh_fdr_only_applies_to_testable_setups(journal):
    _insert_card(journal, "card_strong", 1)
    _insert_card(journal, "card_thin", 1)
    _seed_clustered(
        journal, "card_strong", 1, n=35, values=[0.03, -0.01],
        metric_key="market_adjusted_return_5d_pct",
    )
    # Far below the floor -- must never enter BH-FDR at all.
    _seed_clustered(
        journal, "card_thin", 1, n=3, values=[0.03],
        metric_key="market_adjusted_return_5d_pct", start="2026-06-01",
    )

    rep = setup_evidence.build_setup_evidence_report(journal, metric_key="market_adjusted_return_5d_pct")

    assert rep["n_setups_registered"] == 2
    assert rep["n_setups_testable"] == 1
    by_card = {s["card_id"]: s for s in rep["setups"]}
    assert by_card["card_strong"]["clears_floor"] is True
    assert by_card["card_strong"]["q_value"] is not None
    assert by_card["card_thin"]["clears_floor"] is False
    assert by_card["card_thin"]["q_value"] is None
    assert by_card["card_thin"]["bh_discovery"] is False


def test_build_setup_evidence_report_includes_full_family_not_just_live(journal):
    """A shadow (never-live) card's setup must still show up -- the Edge Lab
    review is a full-family report, not scoreboard.py's live-only demotion
    monitor."""
    _insert_card(journal, "card_shadow", 1, state="shadow")
    _seed_clustered(
        journal, "card_shadow", 1, n=5, values=[0.01],
        metric_key="market_adjusted_return_5d_pct",
    )

    rep = setup_evidence.build_setup_evidence_report(journal, metric_key="market_adjusted_return_5d_pct")

    assert rep["n_setups_registered"] == 1
    assert rep["setups"][0]["card_id"] == "card_shadow"


def test_build_setup_evidence_report_zero_registered_setups_is_safe(journal):
    rep = setup_evidence.build_setup_evidence_report(journal)
    assert rep["n_setups_registered"] == 0
    assert rep["n_setups_testable"] == 0
    assert rep["setups"] == []


# ------------------------------------------------------------------ render_markdown
def test_render_markdown_does_not_raise_on_mixed_floor_setups(journal):
    _insert_card(journal, "card_strong", 1)
    _insert_card(journal, "card_thin", 1)
    _seed_clustered(
        journal, "card_strong", 1, n=35, values=[0.03, -0.01],
        metric_key="market_adjusted_return_5d_pct",
    )
    _seed_clustered(
        journal, "card_thin", 1, n=3, values=[0.03],
        metric_key="market_adjusted_return_5d_pct", start="2026-06-01",
    )
    rep = setup_evidence.build_setup_evidence_report(journal, metric_key="market_adjusted_return_5d_pct")

    md = setup_evidence.render_markdown(rep)

    assert "card_strong" in md and "card_thin" in md
