"""S1b: tests for alphaos.cards.per_evidence -- the DB-facing evidence-query
construction half of the S1b statistical machinery. Unit-level tests exercise
the pure ladder/gate/exclusion logic directly against hand-built dicts (fast,
precise, one gate at a time); integration-level tests build a realistic
in-memory journal and exercise build_primary_evidence()/build_placebo_evidence()
end to end.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from alphaos.cards import per_evidence as pe
from alphaos.cards.selector import PER_CARD_ID, SELECTOR_VERSION
from alphaos.journal.journal_store import JournalStore
from alphaos.stats.two_arm import two_arm_bootstrap
from alphaos.util.ids import new_id


@pytest.fixture
def journal():
    store = JournalStore(":memory:")
    yield store
    store.close()


# ------------------------------------------------------------- DB fixture helpers
def _insert_cache_row(journal, symbol, report_date, fiscal_date_ending=None, timing="pre-market"):
    return journal.insert("earnings_calendar_cache", {
        "entry_id": new_id("ecc"), "symbol": symbol, "report_date": report_date,
        "fiscal_date_ending": fiscal_date_ending, "timing": timing, "source": "test",
    })


def _insert_per_candidate(journal, symbol, decision_date, cache_row_id, outcome_value, shadow_tier=0):
    candidate_id = new_id("cand")
    journal.insert("candidates", {
        "candidate_id": candidate_id, "symbol": symbol, "shadow_tier": shadow_tier,
        "card_id": PER_CARD_ID, "card_version": 1, "card_assignment_status": "ok",
        "card_assignment_ref": str(cache_row_id), "card_selector_version": SELECTOR_VERSION,
    })
    journal.insert("candidate_outcomes", {
        "outcome_id": new_id("out"), "candidate_id": candidate_id, "symbol": symbol,
        "candidate_type": "candidate", "decision_at_utc": f"{decision_date}T14:30:00+00:00",
        "market_adjusted_return_5d_pct": outcome_value, "outcome_status": "complete",
    })
    return candidate_id


def _insert_control_candidate(journal, symbol, decision_date, outcome_value, shadow_tier=0,
                               card_id="catalyst_momentum_v2", status="ok"):
    candidate_id = new_id("cand")
    journal.insert("candidates", {
        "candidate_id": candidate_id, "symbol": symbol, "shadow_tier": shadow_tier,
        "card_id": card_id, "card_version": 1, "card_assignment_status": status,
    })
    journal.insert("candidate_outcomes", {
        "outcome_id": new_id("out"), "candidate_id": candidate_id, "symbol": symbol,
        "candidate_type": "candidate", "decision_at_utc": f"{decision_date}T14:30:00+00:00",
        "market_adjusted_return_5d_pct": outcome_value, "outcome_status": "complete",
    })
    return candidate_id


def _trading_dates_from(start: date, n: int) -> list[str]:
    """n trading-day-ish calendar dates (skips weekends only -- fixtures
    don't need real holiday accuracy) starting at `start`, spaced 7
    calendar days apart (safely non-overlapping under a 5-trading-day
    outcome window) to guarantee no accidental clustering in the fixture."""
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=7)
    return out


# ------------------------------------------------------------------ pure helpers
def test_event_key_prefers_fiscal_date():
    assert pe._event_key("AAPL", "2026-03-31", "2026-05-01") == "AAPL|fiscal:2026-03-31"
    assert pe._event_key("AAPL", None, "2026-05-01") == "AAPL|report_date:2026-05-01"


def test_event_key_scoped_per_symbol():
    """Two different symbols sharing the same fiscal_date_ending (e.g. two
    calendar-Q1 filers) must NOT collide into the same dedup group."""
    assert pe._event_key("AAPL", "2026-03-31", "x") != pe._event_key("MSFT", "2026-03-31", "x")


def test_trading_day_distance_symmetric():
    d1 = date(2026, 5, 1)
    d2 = date(2026, 5, 6)
    assert pe._trading_day_distance(d1, d2) == pe._trading_day_distance(d2, d1)
    assert pe._trading_day_distance(d1, d1) == 0


def test_nth_trading_day_before_is_inverse_of_after():
    from alphaos.util.market_calendar import nth_trading_day_after
    d = date(2026, 5, 15)
    after = nth_trading_day_after(d, 10)
    assert pe._nth_trading_day_before(after, 10) == d


# ---------------------------------------------------------- ladder (pure, no DB)
def _ev(symbol, market_date, shadow_tier=0, value=1.0, candidate_id=None, event_key=None):
    return {
        "candidate_id": candidate_id or new_id("cand"), "symbol": symbol, "shadow_tier": shadow_tier,
        "_market_date": market_date, "outcome_value": value, "_event_key": event_key or f"report_date:{market_date}",
    }


def _ctl(symbol, market_date, tier="core", value=0.0, candidate_id=None):
    return {
        "candidate_id": candidate_id or new_id("cand"), "symbol": symbol, "shadow_tier": 0 if tier == "core" else 1,
        "_market_date": market_date, "_tier": tier, "outcome_value": value,
    }


def test_ladder_rung1_used_when_enough_same_date_tier_controls():
    events = [_ev("AAPL", "2026-05-01")]
    controls = [_ctl("X1", "2026-05-01") for _ in range(pe.RUNG1_MIN_CONTROLS)]
    valid, excluded = pe._apply_ladder(events, controls)
    assert len(valid) == 1 and not excluded
    assert valid[0]["control_fallback"] == "rung1"
    assert valid[0]["stratum_key"] == ("dt", "2026-05-01", "core")


def test_ladder_falls_back_to_rung2_pooled_tier():
    events = [_ev("AAPL", "2026-05-01")]
    # Only 4 same-date controls (below rung-1 minimum of 5) but 30 pooled tier controls elsewhere.
    controls = [_ctl("X1", "2026-05-01") for _ in range(4)]
    controls += [_ctl(f"X{i}", "2026-06-01") for i in range(30)]
    valid, excluded = pe._apply_ladder(events, controls)
    assert len(valid) == 1 and not excluded
    assert valid[0]["control_fallback"] == "rung2"
    assert valid[0]["stratum_key"] == ("tier", "core")


def test_ladder_excludes_when_neither_rung_clears():
    events = [_ev("AAPL", "2026-05-01")]
    controls = [_ctl("X1", "2026-05-01") for _ in range(4)] + [_ctl(f"X{i}", "2026-06-01") for i in range(10)]
    valid, excluded = pe._apply_ladder(events, controls)
    assert not valid and len(excluded) == 1
    assert excluded[0]["excluded_reason"] == "no_rung_cleared"


def test_ladder_never_falls_back_cross_tier():
    """A shadow-tier event with plenty of CORE controls (but zero shadow
    controls) must be excluded, never quietly matched against core."""
    events = [_ev("AAPL", "2026-05-01", shadow_tier=1)]
    controls = [_ctl(f"X{i}", "2026-05-01", tier="core") for i in range(50)]
    valid, excluded = pe._apply_ladder(events, controls)
    assert not valid and len(excluded) == 1


# --------------------------------------------------------------- gates (pure)
def test_gate_fails_below_raw_n_floor():
    events = [{**_ev(f"S{i}", f"2026-0{1+i%6}-{1+i:02d}"), "control_fallback": "rung1"} for i in range(10)]
    gate = pe._check_population_gates(events, [], [_ctl("X", "2026-05-01") for _ in range(10)])
    assert not gate.ok and gate.reason == "per_raw_n_below_floor"


def test_gate_fails_symbol_concentration():
    events = []
    dates = _trading_dates_from(date(2026, 1, 5), 30)
    for i, d in enumerate(dates):
        symbol = "AAPL" if i < 10 else f"SYM{i}"  # AAPL = 10/30 = 33% > 20% ceiling
        events.append({**_ev(symbol, d), "control_fallback": "rung1"})
    gate = pe._check_population_gates(events, [], [_ctl("X", d) for d in dates for _ in range(10)])
    assert not gate.ok and gate.reason == "symbol_concentration_above_ceiling"


def test_gate_fails_span_below_floor():
    # A short span by using consecutive weekdays within one narrow window
    # (the standard 7-day-spaced _trading_dates_from() fixture is
    # deliberately >=90 days for the "clean population" fixtures, so a
    # short-span test needs its own tighter date generator).
    short_dates = []
    d = date(2026, 5, 4)
    while len(short_dates) < 26:
        if d.weekday() < 5:
            short_dates.append(d.isoformat())
        d += timedelta(days=1)
    events = [{**_ev(f"SYM{i}", short_dates[i]), "control_fallback": "rung1"} for i in range(26)]
    gate = pe._check_population_gates(events, [], [_ctl("X", d) for d in short_dates for _ in range(10)])
    assert not gate.ok and gate.reason == "span_below_floor"


def test_gate_fails_pooled_fallback_share_ceiling():
    dates = _trading_dates_from(date(2026, 1, 5), 30)
    events = []
    for i, d in enumerate(dates):
        fallback = "rung2" if i < 10 else "rung1"  # 10/30 = 33% > 20% ceiling
        events.append({**_ev(f"SYM{i}", d), "control_fallback": fallback})
    gate = pe._check_population_gates(events, [], [_ctl("X", d) for d in dates for _ in range(10)])
    assert not gate.ok and gate.reason == "pooled_fallback_share_above_ceiling"


def test_gate_ok_on_a_clean_population():
    dates = _trading_dates_from(date(2026, 1, 5), 30)
    events = [{**_ev(f"SYM{i}", dates[i]), "control_fallback": "rung1"} for i in range(30)]
    controls = [_ctl(f"CTL{j}", d) for d in dates for j in range(10)]
    gate = pe._check_population_gates(events, [], controls)
    assert gate.ok, gate.reason


# ------------------------------------------------------------ selector binding
def test_validate_card_selector_binding_noop_when_absent():
    pe.validate_card_selector_binding({"card_id": "x"})  # must not raise


def test_validate_card_selector_binding_ok_when_matching():
    pe.validate_card_selector_binding({"card_id": "post_earnings_reaction_v1", "requires_selector": SELECTOR_VERSION})


def test_validate_card_selector_binding_raises_on_mismatch():
    with pytest.raises(ValueError):
        pe.validate_card_selector_binding({"card_id": "x", "requires_selector": "card_selector_v0_stale"})


# ------------------------------------------------------------ integration (DB)
def _seed_clean_primary_population(journal, n_events=30, controls_per_date=10):
    """30 PER events, one per distinct symbol, spread 7 calendar days apart
    starting 2026-01-05 (>=90-day span, >=3 distinct months, 0% symbol
    concentration by construction), each date backed by
    ``controls_per_date`` default-card controls (>= RUNG1_MIN_CONTROLS)."""
    dates = _trading_dates_from(date(2026, 1, 5), n_events)
    for i, d in enumerate(dates):
        symbol = f"SYM{i}"
        cache_id = _insert_cache_row(journal, symbol, d, fiscal_date_ending=f"fiscal-{i}", timing="pre-market")
        _insert_per_candidate(journal, symbol, d, cache_id, outcome_value=0.5 + 0.01 * i)
        for j in range(controls_per_date):
            _insert_control_candidate(journal, f"CTL{i}_{j}", d, outcome_value=0.1 * (j % 3))
    return dates


def test_build_primary_evidence_ok_on_clean_population(journal):
    _seed_clean_primary_population(journal)
    result = pe.build_primary_evidence(journal, "2027-01-01T00:00:00+00:00")
    assert result.status == "ok", result.reason
    assert len(result.per_clusters) >= 1
    assert len(result.control_clusters) >= 1
    per_event_rows = [r for r in result.snapshot_rows if r["arm"] == "per_event"]
    assert len(per_event_rows) == 30


def test_build_primary_evidence_defers_when_raw_n_too_low(journal):
    _seed_clean_primary_population(journal, n_events=10)
    result = pe.build_primary_evidence(journal, "2027-01-01T00:00:00+00:00")
    assert result.status == "insufficient_data"
    assert result.reason == "per_raw_n_below_floor"


def test_cross_arm_exclusion_removes_overlapping_controls(journal):
    """A control on the SAME symbol as a PER event, dated inside the
    exclusion zone, must never appear in the primary control pool -- even
    though it would otherwise be a perfectly good rung-1 control."""
    dates = _seed_clean_primary_population(journal, n_events=30)
    # Add a same-symbol, same-date "control" for SYM0's own PER event --
    # this should be excluded, not counted.
    _insert_control_candidate(journal, "SYM0", dates[0], outcome_value=99.0)
    result = pe.build_primary_evidence(journal, "2027-01-01T00:00:00+00:00")
    assert result.status == "ok"
    control_symbols_dates = {(r["symbol"], r["market_date"]) for r in result.snapshot_rows if r["arm"] == "control"}
    assert ("SYM0", dates[0]) not in control_symbols_dates


def test_swap_missing_primary_overlap_exclusion_leaks_a_same_symbol_control():
    """Directly compares the real (exclusion-applying) control filter
    against a broken variant that skips the exclusion entirely -- proving
    the real one is what actually removes the same-symbol, same-window
    control the naive/broken variant would wrongly keep."""
    controls = [
        {"candidate_id": "c1", "symbol": "SYM0", "shadow_tier": 0, "_market_date": "2026-05-01",
         "_tier": "core", "outcome_value": 99.0},
        {"candidate_id": "c2", "symbol": "OTHER", "shadow_tier": 0, "_market_date": "2026-05-01",
         "_tier": "core", "outcome_value": 1.0},
    ]
    exclusion_zones = [("SYM0", "2026-05-01")]
    real_filtered = pe._filter_controls(controls, exclusion_zones)
    broken_filtered = controls  # the naive "no filtering at all" the real function replaces
    assert len(real_filtered) == 1 and real_filtered[0]["candidate_id"] == "c2"
    assert len(broken_filtered) == 2, "the broken (unfiltered) variant wrongly retains the same-symbol control"
    assert real_filtered != broken_filtered


def test_degraded_cache_health_candidates_excluded_from_control_pool(journal):
    """A default-card candidate whose card_assignment_status is a degraded
    health state (not 'ok') never actually got EVALUATED against PER
    eligibility -- must never count as a control."""
    _seed_clean_primary_population(journal, n_events=30)
    _insert_control_candidate(journal, "DEGRADED1", "2026-01-05", outcome_value=1.0, status="stale")
    result = pe.build_primary_evidence(journal, "2027-01-01T00:00:00+00:00")
    assert result.status == "ok"
    assert not any(r["symbol"] == "DEGRADED1" for r in result.snapshot_rows if r["arm"] == "control")


# ------------------------------------------------------- primary/placebo independence
def test_changing_placebo_definition_leaves_primary_byte_identical(journal, monkeypatch):
    """The regression proof the spec explicitly requires: changing ONLY
    the placebo shift constant must leave build_primary_evidence()'s
    result (and therefore the primary's own snapshot hash) byte-identical.
    """
    _seed_clean_primary_population(journal)
    as_of = "2027-01-01T00:00:00+00:00"

    result_a = pe.build_primary_evidence(journal, as_of)
    rows_a = pe.canonical_snapshot_rows(result_a, None)
    hash_a = pe.canonical_snapshot_hash(rows_a)

    monkeypatch.setattr(pe, "PLACEBO_SHIFT_TRADING_DAYS", 20)
    result_b = pe.build_primary_evidence(journal, as_of)
    rows_b = pe.canonical_snapshot_rows(result_b, None)
    hash_b = pe.canonical_snapshot_hash(rows_b)

    assert hash_a == hash_b
    assert result_a.per_clusters == result_b.per_clusters
    assert result_a.control_clusters == result_b.control_clusters


def test_placebo_never_shares_a_control_with_its_own_event(journal):
    _seed_clean_primary_population(journal)
    as_of = "2027-01-01T00:00:00+00:00"
    pe.build_primary_evidence(journal, as_of)
    # Reconstruct minimal primary_events shape build_placebo_evidence expects.
    raw = pe._fetch_per_candidate_rows(journal, as_of)
    events = pe._dedupe_to_one_per_event(raw)
    placebo = pe.build_placebo_evidence(journal, as_of, events)
    placebo_event_ids = {r["candidate_id"] for r in placebo.snapshot_rows if r["arm"] == "placebo_event"}
    placebo_control_ids = {r["candidate_id"] for r in placebo.snapshot_rows if r["arm"] == "placebo_control"}
    assert placebo_event_ids.isdisjoint(placebo_control_ids)


# ============================================================ integrated calibration
def _seed_integrated_null_population(journal, seed=42):
    """A single zero-true-effect population carrying EVERY property the
    approved spec's calibration DGP requires, all at once, built through
    real DB rows (not synthetic dicts):

      * skewed, heavy-tailed outcomes (lognormal noise, matching
        _skewed_zero_effect_fixture's own convention) on BOTH arms, so the
        true effect is exactly zero by construction;
      * a REPEATED symbol ('REPEATSYM') with two distinct, non-overlapping
        earnings events (different fiscal quarters, ~3 months apart);
      * unequal date x tier control counts (8 per date is typical; a few
        dates get exactly the rung-1 minimum; one date is deliberately
        starved to force a rung-2 pooled fallback);
      * a minimum-support same-date stratum (exactly RUNG1_MIN_CONTROLS=5
        controls on 3 separate dates);
      * a pooled same-tier fallback (one event's own date has only 3
        controls -- below the rung-1 minimum -- so it must resolve via
        rung-2 against the >=30-control tier pool);
      * a planted same-symbol, same-window control (on SYM5's own event
        date) that a correct primary control pool must exclude.

    Returns the list of PER event dates (for span/exclusion assertions).
    """
    rng = random.Random(seed)
    dates = _trading_dates_from(date(2026, 1, 5), 28)
    for i, d in enumerate(dates):
        symbol = f"SYM{i}"
        cache_id = _insert_cache_row(journal, symbol, d, fiscal_date_ending=f"fiscal-{i}")
        _insert_per_candidate(journal, symbol, d, cache_id, outcome_value=rng.lognormvariate(0, 0.6) - 1.2)
        if i == 0:
            n_ctl = 3  # below the rung-1 minimum -> forces a rung-2 pooled fallback
        elif i in (1, 2, 3):
            n_ctl = 5  # exactly the rung-1 minimum -- minimum-support strata
        else:
            n_ctl = 8  # typical -- deliberately UNEQUAL vs the above two cases
        for k in range(n_ctl):
            _insert_control_candidate(journal, f"CTL{i}_{k}", d, outcome_value=rng.lognormvariate(0, 0.6) - 1.2)

    # Repeated symbol: two distinct earnings events, well-separated dates,
    # distinct fiscal quarters (so they dedup as two SEPARATE events, not one).
    rep_dates = [date(2026, 2, 2).isoformat(), date(2026, 5, 4).isoformat()]
    for qi, d in enumerate(rep_dates):
        cache_id = _insert_cache_row(journal, "REPEATSYM", d, fiscal_date_ending=f"repfiscal-{qi}")
        _insert_per_candidate(journal, "REPEATSYM", d, cache_id, outcome_value=rng.lognormvariate(0, 0.6) - 1.2)
        for k in range(8):
            _insert_control_candidate(journal, f"REPCTL{qi}_{k}", d, outcome_value=rng.lognormvariate(0, 0.6) - 1.2)

    # Planted same-symbol, same-window control: must be excluded from the
    # primary control pool (SYM5's PER event sits at dates[5]).
    _insert_control_candidate(journal, "SYM5", dates[5], outcome_value=999.0)

    return dates + rep_dates


def _redraw_values(clusters, rng):
    """Deep-copies a cluster list with every 'value' field redrawn from a
    fresh zero-mean skewed (lognormal) null draw, preserving cluster
    membership / stratum_key / stratum_keys exactly. Used to re-run the
    SAME real, DB-derived cluster STRUCTURE (unequal counts, thin/thick
    strata, the repeated-symbol cluster, post-exclusion control pool)
    through many independent null draws for a calibration-rate check,
    without paying the cost of rebuilding the DB fixture each time."""
    out = []
    for cluster in clusters:
        out.append([{**row, "value": rng.lognormvariate(0, 0.6) - 1.2} for row in cluster])
    return out


def test_integrated_skewed_null_calibration_through_the_real_evidence_and_bootstrap_path(journal):
    """The integration-level calibration test the conformance audit
    required: unlike every other calibration test in this suite (which
    hand-builds synthetic cluster dicts and calls
    ``two_arm.two_arm_bootstrap`` directly), this one goes through the
    REAL DB-facing construction path -- ``per_evidence.build_primary_evidence()``
    -- against a population carrying every property the approved DGP
    specifies at once (skew, heavy tails, a repeated symbol, unequal
    date x tier control counts, a minimum-support stratum, a pooled
    rung-2 fallback, and a planted cross-arm exclusion), then feeds the
    REAL resulting clusters into the REAL bootstrap engine.
    """
    as_of = "2027-01-01T00:00:00+00:00"
    _seed_integrated_null_population(journal)

    # ---- structural properties, verified once against the real construction ----
    result_a = pe.build_primary_evidence(journal, as_of)
    assert result_a.status == "ok", result_a.reason
    detail = result_a.detail
    assert detail["n_per_raw"] >= 25
    assert detail["span_days"] >= 90.0
    assert detail["n_distinct_months"] >= 3
    assert detail["max_symbol_share"] <= 0.20
    assert detail["n_control_raw"] >= 100
    assert 0.0 < detail["pooled_fallback_share"] <= 0.20, (
        "the deliberately-starved date must have forced a real rung-2 fallback"
    )
    assert detail["exclusion_share"] <= 0.10

    per_event_rows = [r for r in result_a.snapshot_rows if r["arm"] == "per_event"]
    assert sum(1 for r in per_event_rows if r["symbol"] == "REPEATSYM") == 2, (
        "the repeated symbol must contribute exactly its two distinct earnings events to E*"
    )
    assert any(r["control_fallback"] == "rung2" for r in per_event_rows), (
        "at least one event must have resolved through the pooled rung-2 fallback"
    )
    control_rows = [r for r in result_a.snapshot_rows if r["arm"] == "control"]
    assert not any(r["symbol"] == "SYM5" for r in control_rows), (
        "the planted same-symbol, same-window control must be excluded from the primary pool"
    )
    # A genuine minimum-support stratum exists (exactly RUNG1_MIN_CONTROLS on 3 dates).
    from collections import Counter
    date_counts = Counter((r["market_date"]) for r in control_rows)
    assert any(n == pe.RUNG1_MIN_CONTROLS for n in date_counts.values()), (
        "expected at least one date stratum with EXACTLY the rung-1 minimum control count"
    )

    # ---- fixed E* / reproducibility: an independent rebuild must be IDENTICAL ----
    result_b = pe.build_primary_evidence(journal, as_of)
    assert result_a.per_clusters == result_b.per_clusters
    assert result_a.control_clusters == result_b.control_clusters
    hash_a = pe.canonical_snapshot_hash(pe.canonical_snapshot_rows(result_a, None))
    hash_b = pe.canonical_snapshot_hash(pe.canonical_snapshot_rows(result_b, None))
    assert hash_a == hash_b, "the evidence snapshot hash must be reproducible given identical input data"

    # ---- primary independence from the placebo, on THIS realistic fixture ----
    import unittest.mock
    with unittest.mock.patch.object(pe, "PLACEBO_SHIFT_TRADING_DAYS", 20):
        result_c = pe.build_primary_evidence(journal, as_of)
    hash_c = pe.canonical_snapshot_hash(pe.canonical_snapshot_rows(result_c, None))
    assert hash_c == hash_a, "changing the placebo definition must never alter the primary result"

    # ---- calibration rate: reuse the REAL cluster structure across many null draws ----
    # This is what makes the rate check "integrated" rather than synthetic: the
    # cluster membership, stratum assignment, unequal control counts, and
    # post-exclusion control pool all come from the ACTUAL per_evidence.py
    # construction above -- only the outcome VALUES are redrawn per rep (a
    # fresh DB rebuild per rep would test the same thing far more slowly).
    n_sims = 100
    rejections_pos = rejections_neg = 0
    for sim_seed in range(n_sims):
        rng = random.Random(5000 + sim_seed)
        per_clusters = _redraw_values(result_a.per_clusters, rng)
        control_clusters = _redraw_values(result_a.control_clusters, rng)
        res = two_arm_bootstrap(per_clusters, control_clusters, n_resamples=300, seed=sim_seed)
        assert res["status"] == "ok"
        if res["p_pos"] < 0.05:
            rejections_pos += 1
        if res["p_neg"] < 0.05:
            rejections_neg += 1
    rate_pos, rate_neg = rejections_pos / n_sims, rejections_neg / n_sims
    # Documented tolerance: same [0, 0.11] skewed-null band as the low-level
    # calibration test -- justified identically (skew + a single-draw PER
    # arm vs a multi-draw control average is asymmetric by construction, so
    # the lower bound stays at 0; the upper bound is the real anti-inflation
    # control). 100 sims here, same as the low-level test, for the same band.
    assert 0.0 <= rate_pos <= 0.11, f"integrated H-PER-1P false-positive rate {rate_pos} outside tolerance"
    assert 0.0 <= rate_neg <= 0.11, f"integrated H-PER-1N false-positive rate {rate_neg} outside tolerance"
