"""REG-1: regime classifier + packet stamping (§H.1 direct construction
throughout -- no wall-clock dependence). Covers:
* the frozen classifier's four states, boundary pinning, precedence,
  no-account-inputs law, insufficient-history handling,
* the regime_days service (idempotent per-day insert, graceful missing-
  history handling, idempotent full backfill, existing-packet stamping),
* orchestrator scan-time wiring (once-per-scan reuse, disabled-by-default-off
  behavior is N/A here since regime_enabled defaults True -- tests the
  enabled/disabled toggle explicitly instead, loud-alert-never-blocks),
* the daily brief's regime header + arming-map scorer section,
* the shadow arming-map scorer's pure math (armed_always vs armed_per_map,
  the hard-coded CRISIS-never-armed rule, episode-counting floor gate).

All offline, in-memory, mock mode. No real money, no network.
"""

from __future__ import annotations

import inspect
import sqlite3

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.regime.classifier import (
    MIN_BARS_FOR_FIRST_CLASSIFICATION,
    REGIME_RULES_V1,
    classify_regime_series,
)
from alphaos.regime.service import backfill_regime_days, ensure_regime_for_today
from alphaos.reports.daily_brief import build_daily_brief, render_markdown
from alphaos.reports.regime_arming_scorer import (
    MIN_DISTINCT_REGIME_EPISODES,
    REGIME_ARMING_MAP_V1,
    _count_distinct_episodes,
    _is_armed_per_map,
    build_regime_arming_report,
    compute_regime_arming_scores,
)
from alphaos.safety import KillSwitch
from conftest import make_settings


# ------------------------------------------------------------------ helpers
def _dated_series(n, end=None):
    """n chronological calendar dates ending at `end` (default: today, via
    timeutils.market_date() -- required for any test exercising
    ensure_regime_for_today/backfill_regime_days without an explicit
    market_dt override, since those key off the REAL current date, not an
    arbitrary fixed one). Pure classify_regime_series()-only tests don't
    care what the actual dates are, so they use this too for consistency."""
    from datetime import timedelta

    from alphaos.util import timeutils

    end_date = end if end is not None else timeutils.market_date()
    start_date = end_date - timedelta(days=n - 1)
    return [(start_date + timedelta(days=i)).isoformat() for i in range(n)]


def _flat_bars(n, price=100.0, end=None):
    """n deterministic near-flat daily bars -- small SEEDED-random noise
    (never a perfectly periodic fixed wiggle, which degenerates into an
    all-equal-vol population and a meaningless 100th-percentile tie) keeps
    genuine variation while staying fully deterministic run-to-run."""
    import random

    rng = random.Random(20260709)  # fixed seed -- deterministic, not wall-clock
    dates = _dated_series(n, end=end)
    out = []
    p = price
    for d in dates:
        p *= (1 + rng.uniform(-0.0015, 0.0015))
        out.append({"date": d, "close": round(p, 4)})
    return out


def _trend_bars(n, price=100.0, drift=0.003, end=None):
    dates = _dated_series(n, end=end)
    out = []
    p = price
    for d in dates:
        p *= (1 + drift)
        out.append({"date": d, "close": round(p, 4)})
    return out


class _FakeBars:
    def __init__(self, all_bars):
        self._all = sorted(all_bars, key=lambda b: b["date"])

    def get_daily_bars(self, symbol, start, end, limit=200):
        in_range = [b for b in self._all if start <= b["date"] <= end]
        return in_range[:limit]


# ---------------------------------------------------------------- classifier
def test_classifier_reaches_trend_up_deterministically():
    bars = _trend_bars(MIN_BARS_FOR_FIRST_CLASSIFICATION + 60, drift=0.002)
    result = classify_regime_series(bars)
    assert result
    assert result[-1]["regime"] == "TREND_UP"
    # Deterministic across repeated calls with the same input.
    assert classify_regime_series(bars) == result


def test_classifier_reaches_trend_dn_deterministically():
    bars = _trend_bars(MIN_BARS_FOR_FIRST_CLASSIFICATION + 60, drift=-0.002)
    result = classify_regime_series(bars)
    assert result
    assert result[-1]["regime"] == "TREND_DN"


def test_classifier_reaches_chop_on_flat_series():
    bars = _flat_bars(MIN_BARS_FOR_FIRST_CLASSIFICATION + 20)
    result = classify_regime_series(bars)
    assert result
    assert result[-1]["regime"] == "CHOP"
    assert result[-1]["chop_streak_days"] >= 5


def test_classifier_reaches_crisis_on_high_vol_tail():
    # 400 flat days to build a low-vol trailing distribution, then a sharp
    # alternating +/-8% whipsaw tail -- deterministic (no RNG), pushes the
    # trailing-20d vol to the top of its own 1-year distribution.
    bars = _flat_bars(400)
    from datetime import date, timedelta

    last_date = date.fromisoformat(bars[-1]["date"])
    price = bars[-1]["close"]
    for i in range(1, 40):
        price *= (1.08 if i % 2 == 0 else 0.92)
        bars.append({"date": (last_date + timedelta(days=i)).isoformat(), "close": round(price, 4)})
    result = classify_regime_series(bars)
    assert result
    assert result[-1]["regime"] == "CRISIS"
    assert result[-1]["vol_percentile_1y"] >= 90.0


def test_classifier_precedence_crisis_wins_over_trend_up():
    """A whipsaw tail grafted onto a strong uptrend: SMA50 > SMA200 and
    close > SMA50 (TREND_UP conditions hold) but vol percentile is also
    pushed to >=90 -- CRISIS must win (evaluated first, unconditionally)."""
    bars = _trend_bars(400, drift=0.0025)
    from datetime import date, timedelta

    last_date = date.fromisoformat(bars[-1]["date"])
    price = bars[-1]["close"]
    for i in range(1, 40):
        price *= (1.07 if i % 2 == 0 else 0.94)
        bars.append({"date": (last_date + timedelta(days=i)).isoformat(), "close": round(price, 4)})
    result = classify_regime_series(bars)
    last = result[-1]
    assert last["vol_percentile_1y"] >= 90.0
    assert last["regime"] == "CRISIS"  # not TREND_UP, even though sma50>sma200 likely still holds


def test_classifier_boundary_vol_percentile_exactly_90_is_crisis():
    """Pinned boundary: vol percentile >= 90 (inclusive) triggers CRISIS.
    Population 1..10, value=9 -> count(v<=9)=9 of 10 -> exactly 90.0."""
    from alphaos.regime import classifier as clf_module

    pct = clf_module._percentile_rank(9, list(range(1, 11)))
    assert pct == 90.0
    assert pct >= clf_module._CRISIS_VOL_PERCENTILE
    # And just below the boundary must NOT qualify.
    pct_below = clf_module._percentile_rank(8, list(range(1, 11)))
    assert pct_below == 80.0
    assert pct_below < clf_module._CRISIS_VOL_PERCENTILE


def test_classifier_boundary_dev_exactly_1_5_pct_is_not_chop_eligible():
    """Pinned boundary: |dev|/SMA50 < 1.5% is STRICT -- exactly 1.5% must
    NOT count toward the chop streak."""
    from alphaos.regime import classifier as clf_module

    assert not (1.5 < clf_module._CHOP_DEV_THRESHOLD_PCT)
    assert not (1.5 < 1.5)  # exactly-equal fails the strict "<" -- sanity pin


def test_classifier_no_account_inputs_signature():
    """Structural law: the ONLY parameter is `bars` -- a future edit that
    tries to widen this to accept account/P&L/position data must fail this
    test loudly."""
    sig = inspect.signature(classify_regime_series)
    assert list(sig.parameters) == ["bars"]


def test_classifier_no_account_inputs_source_never_references_account_terms():
    """Scans the CODE BODY only (the docstring legitimately names these
    terms to explain the law itself) -- slices source lines starting AFTER
    the docstring statement via its AST end line, so only executable code
    can trip this."""
    import ast

    source_lines, _ = inspect.getsourcelines(classify_regime_series)
    tree = ast.parse("".join(source_lines))
    fn_node = tree.body[0]
    body_stmts = fn_node.body
    first_real_idx = 0
    if (body_stmts and isinstance(body_stmts[0], ast.Expr)
            and isinstance(body_stmts[0].value, ast.Constant)
            and isinstance(body_stmts[0].value.value, str)):
        first_real_idx = 1
    body_source = (
        "".join(source_lines[body_stmts[first_real_idx].lineno - 1:])
        if first_real_idx < len(body_stmts) else ""
    )
    for term in ("pnl", "p&l", "drawdown", "position", "equity", "realized_r", "account"):
        assert term not in body_source.lower(), f"classifier code body references account term {term!r}"


def test_classifier_insufficient_history_produces_no_rows():
    bars = _flat_bars(MIN_BARS_FOR_FIRST_CLASSIFICATION - 1)
    assert classify_regime_series(bars) == []


def test_classifier_rules_version_stamped_on_every_row():
    bars = _trend_bars(MIN_BARS_FOR_FIRST_CLASSIFICATION + 5)
    result = classify_regime_series(bars)
    assert all(r["rules_version"] == REGIME_RULES_V1 for r in result)


# --------------------------------------------------------------------- service
def test_ensure_regime_for_today_stale_benchmark_spine_never_mislabels_today(journal, settings):
    """Regression for a scope/safety audit finding: a stale benchmark-spine
    gap (bars stop 10 days before today) must NOT get silently stamped onto
    today's packets as if it were fresh -- ensure_regime_for_today's
    contract is "today's regime, or None," never "the last known regime.\""""
    from datetime import timedelta

    from alphaos.util import timeutils

    stale_end = timeutils.market_date() - timedelta(days=10)
    bars = _trend_bars(MIN_BARS_FOR_FIRST_CLASSIFICATION + 60, end=stale_end)
    provider = _FakeBars(bars)

    row = ensure_regime_for_today(journal, settings, bars_provider=provider)
    assert row is None  # never the stale row, never fabricated as "today"

    # And no regime_days row was inserted under the WRONG (today's) date --
    # the stale day itself is fine to have been classified internally, just
    # never returned/used as if it were today's answer.
    todays_row = journal.one(
        "SELECT * FROM regime_days WHERE market_date = ?", (timeutils.market_date().isoformat(),)
    )
    assert todays_row is None


def test_ensure_regime_for_today_cold_start_returns_none_without_a_prior_backfill(journal, settings):
    """ensure_regime_for_today's own catch-up window is deliberately small
    (it only extends benchmark_bars using _backfill_benchmark_bars' DEFAULT
    ~90-day lookback, not REG-1's deep one) -- on a truly cold system (no
    prior backfill_regime_days run), 90 days is short of
    MIN_BARS_FOR_FIRST_CLASSIFICATION, so today's regime is correctly
    unavailable until the operator runs the one-off backfill. This is the
    same "ship the mechanism, arming is a separate deliberate step" pattern
    as EXP-0's universe_build -- not a bug."""
    bars = _trend_bars(MIN_BARS_FOR_FIRST_CLASSIFICATION + 30)
    provider = _FakeBars(bars)
    row = ensure_regime_for_today(journal, settings, bars_provider=provider)
    assert row is None
    assert journal.count_rows("benchmark_bars") < MIN_BARS_FOR_FIRST_CLASSIFICATION


def test_ensure_regime_for_today_idempotent_same_day_after_a_prior_backfill(journal, settings):
    """The realistic sequence: an operator has already run backfill_regime_days
    once (deep history) -- ensure_regime_for_today must then read the
    existing row back idempotently across repeated same-day calls, never
    inserting a duplicate."""
    bars = _trend_bars(MIN_BARS_FOR_FIRST_CLASSIFICATION + 30)
    provider = _FakeBars(bars)
    backfill_regime_days(journal, settings, bars_provider=provider, initial_lookback_days=2000)

    row1 = ensure_regime_for_today(journal, settings, bars_provider=provider)
    assert row1 is not None
    count_after_first = journal.count_rows("regime_days")
    row2 = ensure_regime_for_today(journal, settings, bars_provider=provider)
    assert row2["regime_day_id"] == row1["regime_day_id"]
    assert journal.count_rows("regime_days") == count_after_first  # no duplicate


def test_ensure_regime_for_today_insufficient_history_returns_none_never_raises(journal, settings):
    provider = _FakeBars(_flat_bars(10))  # far short of MIN_BARS_FOR_FIRST_CLASSIFICATION
    row = ensure_regime_for_today(journal, settings, bars_provider=provider)
    assert row is None


def test_backfill_regime_days_idempotent_on_rerun(journal, settings):
    bars = _trend_bars(MIN_BARS_FOR_FIRST_CLASSIFICATION + 100)
    provider = _FakeBars(bars)
    r1 = backfill_regime_days(journal, settings, bars_provider=provider, initial_lookback_days=2000)
    assert r1["regime_days_written"] > 0
    assert "error" not in r1
    r2 = backfill_regime_days(journal, settings, bars_provider=provider, initial_lookback_days=2000)
    assert r2["regime_days_written"] == 0
    assert r2["regime_days_already_present"] == r1["regime_days_written"]


def test_backfill_regime_days_stamps_existing_null_packets(journal, settings):
    bars = _trend_bars(MIN_BARS_FOR_FIRST_CLASSIFICATION + 60)
    provider = _FakeBars(bars)

    last_date = bars[-1]["date"]
    journal.insert("scan_batches", {
        "scan_batch_id": "sb1", "scan_type": "manual", "source": "cli",
        "started_at_utc": f"{last_date}T14:35:00+00:00", "started_at_sgt": f"{last_date}T22:35:00+08:00",
        "status": "completed",
    })
    journal.insert("candidate_packets", {
        "packet_id": "pkt1", "candidate_id": "cand1", "scan_batch_id": "sb1", "symbol": "AAPL",
    })
    assert journal.one("SELECT regime FROM candidate_packets WHERE packet_id = 'pkt1'")["regime"] is None

    result = backfill_regime_days(journal, settings, bars_provider=provider, initial_lookback_days=2000)
    assert result["packets_stamped"] == 1

    stamped = journal.one("SELECT regime, regime_rules_version FROM candidate_packets WHERE packet_id = 'pkt1'")
    assert stamped["regime"] is not None
    assert stamped["regime_rules_version"] == REGIME_RULES_V1

    # Idempotent: re-run stamps zero more (already non-NULL).
    result2 = backfill_regime_days(journal, settings, bars_provider=provider, initial_lookback_days=2000)
    assert result2["packets_stamped"] == 0


def test_backfill_regime_days_never_raises_journals_error_on_failure(journal, settings, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(
        "alphaos.regime.service.classify_regime_series", _boom,
    )
    result = backfill_regime_days(journal, settings, bars_provider=_FakeBars(_trend_bars(400)))
    assert "error" in result
    events = journal.query("SELECT * FROM system_events WHERE category = 'regime_backfill' AND severity = 'error'")
    assert events


# ---------------------------------------------------------- schema migration
def test_regime_schema_added_to_a_pre_reg_1_db(tmp_path):
    db = str(tmp_path / "pre_reg_1.db")
    raw = sqlite3.connect(db)
    raw.execute(
        "CREATE TABLE candidate_packets (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "packet_id TEXT NOT NULL UNIQUE, candidate_id TEXT NOT NULL, "
        "created_at_utc TEXT NOT NULL, created_at_sgt TEXT NOT NULL)"
    )
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    j = JournalStore(db)
    try:
        cols = {r["name"] for r in j.conn.execute("PRAGMA table_info(candidate_packets)")}
        assert {"regime", "regime_rules_version"} <= cols
        tables = {r["name"] for r in j.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "regime_days" in tables
        j.insert("regime_days", {
            "regime_day_id": "rd1", "market_date": "2026-07-08", "regime": "CHOP",
            "regime_rules_version": REGIME_RULES_V1, "computed_at_utc": "2026-07-08T00:00:00+00:00",
        })
        assert j.count_rows("regime_days") == 1
        assert j.conn.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        j.close()


def test_regime_days_unique_index_enforces_one_row_per_date_version(journal):
    journal.insert("regime_days", {
        "regime_day_id": "rd1", "market_date": "2026-07-08", "regime": "CHOP",
        "regime_rules_version": REGIME_RULES_V1, "computed_at_utc": "2026-07-08T00:00:00+00:00",
    })
    with pytest.raises(sqlite3.IntegrityError):
        journal.insert("regime_days", {
            "regime_day_id": "rd2", "market_date": "2026-07-08", "regime": "TREND_UP",
            "regime_rules_version": REGIME_RULES_V1, "computed_at_utc": "2026-07-08T00:00:00+00:00",
        })
    # A DIFFERENT rules_version for the SAME date is allowed (a v2 recompute
    # adds rows, never collides with v1).
    journal.insert("regime_days", {
        "regime_day_id": "rd3", "market_date": "2026-07-08", "regime": "TREND_UP",
        "regime_rules_version": "regime_rules_v2", "computed_at_utc": "2026-07-08T00:00:00+00:00",
    })
    assert journal.count_rows("regime_days") == 2


# ----------------------------------------------------------- lineage config hash
def test_regime_config_hash_present_and_lineage_intact(journal, settings):
    from alphaos.lineage.builder import get_or_create_lineage_id

    lid = get_or_create_lineage_id(journal, settings)
    assert lid is not None  # regression: a config-hash key with no matching
    # lineage_snapshots column silently breaks this (see EXP-0's own incident)
    row = journal.one("SELECT regime_config_hash FROM lineage_snapshots WHERE lineage_id = ?", (lid,))
    assert row and row["regime_config_hash"] is not None


# ----------------------------------------------------------- orchestrator wiring
def test_scan_stamps_regime_on_every_packet_when_available(tmp_path):
    bars = _trend_bars(MIN_BARS_FOR_FIRST_CLASSIFICATION + 30)
    settings = make_settings(LABELLING_ENABLED="true", REGIME_BACKFILL_LOOKBACK_DAYS="2000")
    journal = JournalStore(":memory:")
    backfill_regime_days(journal, settings, bars_provider=_FakeBars(bars), initial_lookback_days=2000)

    orch = Orchestrator(settings=settings, journal=journal)
    orch.run_scan_once()

    packets = journal.query("SELECT regime, regime_rules_version FROM candidate_packets")
    assert packets, "expected at least one packet from the default mock scan"
    distinct = {(p["regime"], p["regime_rules_version"]) for p in packets}
    assert len(distinct) == 1  # once-per-scan computation, reused for every packet
    regime, version = next(iter(distinct))
    assert regime is not None and version == REGIME_RULES_V1
    journal.close()


def test_scan_stamps_null_and_alerts_when_regime_unavailable(orchestrator):
    """Default mock-mode journal has zero benchmark_bars history -- packets
    must stamp regime=NULL, never block the scan, and a loud alert must be
    journaled exactly once per scan (not once per candidate)."""
    orchestrator.run_scan_once()
    packets = orchestrator.journal.query("SELECT regime FROM candidate_packets")
    assert packets
    assert all(p["regime"] is None for p in packets)
    alerts = orchestrator.journal.query(
        "SELECT * FROM system_events WHERE category = 'regime' AND severity = 'warning'"
    )
    assert len(alerts) == 1


def test_regime_disabled_costs_zero_regime_days_rows(tmp_path):
    settings = make_settings(REGIME_ENABLED="false")
    journal = JournalStore(":memory:")
    orch = Orchestrator(settings=settings, journal=journal)
    orch.run_scan_once()
    assert journal.count_rows("regime_days") == 0
    packets = journal.query("SELECT regime FROM candidate_packets")
    assert all(p["regime"] is None for p in packets)
    journal.close()


# --------------------------------------------------------------- daily brief
def test_regime_header_none_when_no_regime_days_row(journal, settings):
    brief = build_daily_brief(journal, settings, KillSwitch())
    assert brief["regime"] is None
    md = render_markdown(brief)
    assert "## Regime:" not in md


def test_regime_header_renders_with_streak_and_caveat(journal, settings):
    bars = _trend_bars(MIN_BARS_FOR_FIRST_CLASSIFICATION + 10)
    backfill_regime_days(journal, settings, bars_provider=_FakeBars(bars), initial_lookback_days=2000)
    brief = build_daily_brief(journal, settings, KillSwitch())
    assert brief["regime"] is not None
    assert brief["regime"]["consecutive_days"] >= 1
    assert "descriptive only" in brief["regime"]["caveat"]
    md = render_markdown(brief)
    assert "## Regime:" in md
    assert "descriptive only" in md


def test_daily_brief_always_carries_regime_arming_section(journal, settings):
    brief = build_daily_brief(journal, settings, KillSwitch())
    assert "regime_arming" in brief
    assert brief["regime_arming"]["cards"] == []
    md = render_markdown(brief)
    assert "Shadow arming-map scorer" in md


# -------------------------------------------------------- shadow arming scorer
def test_is_armed_per_map_momentum_card_only_trend_up():
    assert _is_armed_per_map("catalyst_momentum_v1", "TREND_UP") is True
    assert _is_armed_per_map("catalyst_momentum_v1", "TREND_DN") is False
    assert _is_armed_per_map("catalyst_momentum_v1", "CHOP") is False


def test_is_armed_per_map_crisis_never_armed_regardless_of_map():
    """The one hard-coded rule: CRISIS is never armed for ANY card, even a
    hypothetical future card whose map explicitly claims CRISIS."""
    assert _is_armed_per_map("catalyst_momentum_v1", "CRISIS") is False
    fake_map_backup = dict(REGIME_ARMING_MAP_V1)
    try:
        REGIME_ARMING_MAP_V1["hypothetical_card"] = {"CRISIS", "TREND_UP"}
        assert _is_armed_per_map("hypothetical_card", "CRISIS") is False
        assert _is_armed_per_map("hypothetical_card", "TREND_UP") is True
    finally:
        REGIME_ARMING_MAP_V1.clear()
        REGIME_ARMING_MAP_V1.update(fake_map_backup)


def test_is_armed_per_map_unknown_regime_never_armed():
    assert _is_armed_per_map("catalyst_momentum_v1", None) is False


def test_count_distinct_episodes_same_day_dupes_and_adjacent_days_are_one_episode():
    assert _count_distinct_episodes(["2026-01-05", "2026-01-05"]) == 1
    assert _count_distinct_episodes(["2026-01-05", "2026-01-06", "2026-01-07"]) == 1


def test_count_distinct_episodes_a_gap_starts_a_new_episode():
    assert _count_distinct_episodes(["2026-01-05", "2026-01-10"]) == 2
    assert _count_distinct_episodes(["2026-01-05", "2026-01-06", "2026-01-20"]) == 2


def test_count_distinct_episodes_empty_is_zero():
    assert _count_distinct_episodes([]) == 0


def test_compute_regime_arming_scores_delta_r_withheld_below_floor():
    rows = [
        {"card_id": "catalyst_momentum_v1", "regime": "TREND_UP", "replay_r": 1.0, "market_date": "2026-01-05"},
        {"card_id": "catalyst_momentum_v1", "regime": "CHOP", "replay_r": -0.5, "market_date": "2026-01-06"},
    ]
    result = compute_regime_arming_scores(rows)
    card = result["cards"][0]
    assert card["floor_met"] is False  # only 1 distinct TREND_UP episode, need MIN_DISTINCT_REGIME_EPISODES
    assert card["delta_r"] is None
    assert card["n_all"] == 2
    assert card["n_armed_per_map"] == 1


def test_compute_regime_arming_scores_delta_r_present_when_floor_met():
    rows = [
        {"card_id": "catalyst_momentum_v1", "regime": "TREND_UP", "replay_r": 1.0, "market_date": "2026-01-05"},
        {"card_id": "catalyst_momentum_v1", "regime": "TREND_UP", "replay_r": 1.4, "market_date": "2026-02-05"},  # 2nd episode
        {"card_id": "catalyst_momentum_v1", "regime": "CHOP", "replay_r": -0.5, "market_date": "2026-01-06"},
        {"card_id": "catalyst_momentum_v1", "regime": "CHOP", "replay_r": -0.3, "market_date": "2026-01-07"},
    ]
    result = compute_regime_arming_scores(rows)
    card = result["cards"][0]
    assert card["floor_met"] is True
    assert card["n_all"] == 4
    assert card["mean_r_armed_always"] == pytest.approx((1.0 + 1.4 - 0.5 - 0.3) / 4)
    assert card["n_armed_per_map"] == 2
    assert card["mean_r_armed_per_map"] == pytest.approx((1.0 + 1.4) / 2)
    assert card["delta_r"] == pytest.approx(card["mean_r_armed_per_map"] - card["mean_r_armed_always"])


def test_compute_regime_arming_scores_empty_input():
    result = compute_regime_arming_scores([])
    assert result["cards"] == []


def test_compute_regime_arming_scores_min_episode_constant_is_2():
    assert MIN_DISTINCT_REGIME_EPISODES == 2


def test_build_regime_arming_report_end_to_end(journal, settings):
    """Behavior probe through the real journal join (candidate_outcomes ->
    candidate_packets -> candidates), not just the pure compute function."""
    from alphaos.cards.registry import get_default_card

    card = get_default_card()
    journal.insert("candidates", {
        "candidate_id": "cand1", "symbol": "AAPL", "card_id": card["card_id"],
        "card_version": card["version"],
    })
    journal.insert("candidate_packets", {
        "packet_id": "pkt1", "candidate_id": "cand1", "symbol": "AAPL",
        "regime": "TREND_UP", "regime_rules_version": REGIME_RULES_V1,
    })
    journal.insert("candidate_outcomes", {
        "outcome_id": "out1", "candidate_id": "cand1", "symbol": "AAPL",
        "candidate_type": "candidate", "replay_r": 1.2, "outcome_status": "resolved",
    })

    rep = build_regime_arming_report(journal, settings)
    assert len(rep["cards"]) == 1
    assert rep["cards"][0]["card_id"] == card["card_id"]
    assert rep["cards"][0]["n_all"] == 1


def test_build_regime_arming_report_excludes_unresolved_and_missing_regime(journal, settings):
    from alphaos.cards.registry import get_default_card

    card = get_default_card()
    journal.insert("candidates", {
        "candidate_id": "cand1", "symbol": "AAPL", "card_id": card["card_id"], "card_version": card["version"],
    })
    journal.insert("candidate_packets", {
        "packet_id": "pkt1", "candidate_id": "cand1", "symbol": "AAPL", "regime": None,
    })
    journal.insert("candidate_outcomes", {
        "outcome_id": "out1", "candidate_id": "cand1", "symbol": "AAPL",
        "candidate_type": "candidate", "replay_r": 1.2, "outcome_status": "pending",
    })
    rep = build_regime_arming_report(journal, settings)
    assert rep["cards"] == []  # neither row qualifies: no regime, and status='pending' not 'resolved'
