"""SUSP-1: canary-aware shadow-label auto-suspend
(docs/roadmap/alphaos-susp1-canary-aware-suspend-spec.md).

Replaces the old unfiltered "ANY historical TIER_1 row latches forever"
canary arm of ``shadow_label.check_auto_suspend`` with one that reads only
recent TRIGGER rows (``confirmation_of IS NULL``) within a recency window,
latching on every confirmation status except an explicit ``not_confirmed``
(the fail direction: honest suspension -- when evidence is ambiguous,
suspend). Covers, per the spec's own numbered test list:

1. identity_immediate -> latches, reason names identity
2. confirmed -> latches
3. unconfirmed_page -> latches (fail direction)
4. not_confirmed -> does NOT latch (the headline fix)
5. a confirmation RUN row (confirmation_of set) tripping TIER_1 -> never
   latches independently
6. legacy row (no confirmation key) -> latches inside the window, releases
   outside it
7. malformed drift_detail_json -> latches (never skipped-because-broken)
8. window boundary (>= vs >), pinned
9. a row shaped exactly like the real production artifact
   canaryrun_f607c73a2589 -> latches at N < window, releases at N > window
10. the feed-coverage arm + settings validation bounds
11. end-to-end through run_shadow_label (production entry point), not just
    the helper

All offline, in-memory, mock mode. No real money, no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from alphaos.config.settings import SettingsError
from alphaos.safety import ShadowLabelSuspendSwitch
from alphaos.scheduler import shadow_label
from alphaos.util import alerts, timeutils
from conftest import make_settings
from test_exp1_shadow_labelling import _orch_with_shadow_universe, _seed_symbols, _seed_universe_days


# --------------------------------------------------------------------- fakes
def _insert_tier1_row(
    journal,
    run_id: str,
    *,
    days_ago: float = 1,
    confirmation_of=None,
    drift_detail: dict | None = None,
    drift_detail_json_raw: str | None = None,
) -> None:
    """Directly construct a ``canary_runs`` row shaped like a real weekly
    canary run (§H.1 direct-construction law, same idiom as
    ``test_auto_suspend_triggers_on_canary_tier1``). Exactly one of
    ``drift_detail``/``drift_detail_json_raw`` may be given; neither given
    means a legacy pre-CANARY-2 row (no ``drift_detail_json`` at all).
    ``days_ago`` accepts fractional days for boundary-precision tests."""
    import json as _json

    assert drift_detail is None or drift_detail_json_raw is None
    if drift_detail is not None:
        detail_json = _json.dumps(drift_detail)
    else:
        detail_json = drift_detail_json_raw

    started = timeutils.stamp(timeutils.now_utc() - timedelta(days=days_ago))
    journal.insert("canary_runs", {
        "run_id": run_id, "corpus_dir": "data/canary", "n_prompts": 5,
        "drift_tier": "TIER_1", "drift_detail_json": detail_json,
        "confirmation_of": confirmation_of,
        "started_at_utc": started.utc, "started_at_sgt": started.local_sgt,
    })


# ------------------------------------------------------------- latch semantics
def test_susp1_identity_immediate_within_window_latches_names_identity(journal):
    """Spec test 1."""
    settings = make_settings()
    _insert_tier1_row(
        journal, "canaryrun_identity", days_ago=1,
        drift_detail={"confirmation": {"status": "identity_immediate"}},
    )
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is True
    assert "identity" in reason
    assert "canaryrun_identity" in reason


def test_susp1_confirmed_within_window_latches(journal):
    """Spec test 2."""
    settings = make_settings()
    _insert_tier1_row(
        journal, "canaryrun_confirmed", days_ago=1,
        drift_detail={"confirmation": {"status": "confirmed", "confirming_run_id": "canaryrun_x"}},
    )
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is True
    assert "confirmed" in reason
    assert "canaryrun_confirmed" in reason


def test_susp1_unconfirmed_page_within_window_latches_fail_direction(journal):
    """Spec test 3: the confirmation run could not execute -- fail TOWARD
    suspension, never toward silence."""
    settings = make_settings()
    _insert_tier1_row(
        journal, "canaryrun_unconfirmed", days_ago=1,
        drift_detail={"confirmation": {"status": "unconfirmed_page", "reason": "cost cap"}},
    )
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is True
    assert "unconfirmed" in reason
    assert "canaryrun_unconfirmed" in reason


def test_susp1_not_confirmed_within_window_does_not_latch_headline_fix(journal):
    """Spec test 4 -- THE headline fix: the exact both-auditor probe
    scenario (failsafe trip, clean same-day confirmation, no page sent)
    must let shadow labelling keep running, not re-latch through the
    auto-suspend side door."""
    settings = make_settings()
    _insert_tier1_row(
        journal, "canaryrun_notconfirmed", days_ago=1,
        drift_detail={"confirmation": {"status": "not_confirmed", "confirming_run_id": "canaryrun_y"}},
    )
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is False
    assert reason == "no auto-suspend condition met"


def test_susp1_confirmation_run_row_never_latches_independently(journal):
    """Spec test 5: a same-day CONFIRMATION replay (confirmation_of set)
    that itself tripped TIER_1 must never latch on its own -- its verdict
    lives on its trigger row's own annotation, not on this row."""
    settings = make_settings()
    _insert_tier1_row(
        journal, "canaryrun_confirmation_replay", days_ago=0.1,
        confirmation_of="canaryrun_some_trigger",
        drift_detail=None,  # confirmation runs never get a drift_detail_json.confirmation annotation
    )
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is False
    assert reason == "no auto-suspend condition met"


def test_susp1_legacy_row_within_window_latches_conservative(journal):
    """Spec test 6 (in-window half): a legacy pre-CANARY-2 row (no
    confirmation key in drift_detail at all) latches conservatively."""
    settings = make_settings()
    _insert_tier1_row(journal, "canaryrun_legacy_recent", days_ago=5, drift_detail=None)
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is True
    assert "legacy-conservative" in reason
    assert "canaryrun_legacy_recent" in reason


def test_susp1_legacy_row_outside_window_does_not_latch(journal):
    """Spec test 6 (outside-window half): the same shape of row, but old
    enough to have aged out of the default 14-day window, releases with NO
    bespoke un-arm mechanism -- it simply falls outside the query."""
    settings = make_settings()
    _insert_tier1_row(journal, "canaryrun_legacy_stale", days_ago=20, drift_detail=None)
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is False
    assert reason == "no auto-suspend condition met"


def test_susp1_malformed_drift_detail_json_latches(journal):
    """Spec test 7: unparseable JSON on an in-window TIER_1 trigger must
    LATCH, never be skipped-because-broken."""
    settings = make_settings()
    _insert_tier1_row(
        journal, "canaryrun_malformed", days_ago=1,
        drift_detail_json_raw="{this is not valid json,,,",
    )
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is True
    assert "legacy-conservative" in reason
    assert "canaryrun_malformed" in reason


def test_susp1_malformed_confirmation_value_not_a_dict_latches(journal):
    """Adjacent malformed-shape case: ``drift_detail_json`` parses fine but
    its own ``confirmation`` key is not a dict (e.g. hand-edited/corrupted)
    -- must also latch, same conservative-default rule."""
    settings = make_settings()
    _insert_tier1_row(
        journal, "canaryrun_bad_confirmation_shape", days_ago=1,
        drift_detail={"confirmation": "not-a-dict"},
    )
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is True
    assert "legacy-conservative" in reason


def test_susp1_window_boundary_exact_edge_is_inclusive(journal, monkeypatch):
    """Spec test 8: pin the operator's own comparison. A row whose
    started_at_utc lands EXACTLY at the window cutoff (now - window_days)
    is INCLUDED (>=, not >) -- matches this codebase's existing window-gate
    idiom (``_daily_feed_coverage_map``'s own ``market_date >= since``,
    ``build_daily_digest``'s ``started_at_utc >= ?``). "Now" is pinned via
    monkeypatch so the fixture's timestamp and the code's own cutoff
    computation land on the exact same instant -- a live clock would race
    the two ``now_utc()`` calls by microseconds and make this boundary
    untestable."""
    fixed_now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(shadow_label.timeutils, "now_utc", lambda: fixed_now)
    settings = make_settings(SHADOW_SUSPEND_CANARY_WINDOW_DAYS="7")
    edge = timeutils.to_iso(fixed_now - timedelta(days=7))
    journal.insert("canary_runs", {
        "run_id": "canaryrun_exact_edge", "corpus_dir": "data/canary", "n_prompts": 5,
        "drift_tier": "TIER_1", "drift_detail_json": None, "confirmation_of": None,
        "started_at_utc": edge, "started_at_sgt": edge,
    })
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is True
    assert "canaryrun_exact_edge" in reason


def test_susp1_window_boundary_just_past_edge_is_excluded(journal, monkeypatch):
    """Spec test 8 continued: one second older than the cutoff falls
    outside the window. Same pinned-clock rationale as the inclusive case
    above."""
    fixed_now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(shadow_label.timeutils, "now_utc", lambda: fixed_now)
    settings = make_settings(SHADOW_SUSPEND_CANARY_WINDOW_DAYS="7")
    just_past = timeutils.to_iso(
        fixed_now - timedelta(days=7) - timedelta(seconds=1)
    )
    journal.insert("canary_runs", {
        "run_id": "canaryrun_just_past_edge", "corpus_dir": "data/canary", "n_prompts": 5,
        "drift_tier": "TIER_1", "drift_detail_json": None, "confirmation_of": None,
        "started_at_utc": just_past, "started_at_sgt": just_past,
    })
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is False


def test_susp1_older_row_still_latches_after_newer_not_confirmed_row(journal):
    """Neither the spec's numbered list nor the design section stops the
    scan at the first non-latching row -- a NEWER not_confirmed row (system-
    proven transient) must not mask an OLDER, still-in-window row that DOES
    latch. Each canary event is independent evidence."""
    settings = make_settings()
    _insert_tier1_row(
        journal, "canaryrun_older_confirmed", days_ago=6,
        drift_detail={"confirmation": {"status": "confirmed", "confirming_run_id": "canaryrun_z"}},
    )
    _insert_tier1_row(
        journal, "canaryrun_newer_notconfirmed", days_ago=1,
        drift_detail={"confirmation": {"status": "not_confirmed", "confirming_run_id": "canaryrun_w"}},
    )
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is True
    assert "canaryrun_older_confirmed" in reason


# --------------------------------------------------- real-ledger shape regression
def test_susp1_canaryrun_f607c73a2589_shape_latches_at_3_days(journal):
    """Spec test 9: a row shaped exactly like the real production artifact
    ``canaryrun_f607c73a2589`` (TIER_1, legacy -- pre-CANARY-2, no
    confirmation key at all) at 3 days old -- well inside the default
    14-day window -- latches."""
    settings = make_settings()
    _insert_tier1_row(journal, "canaryrun_f607c73a2589", days_ago=3, drift_detail=None)
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is True
    assert "canaryrun_f607c73a2589" in reason
    assert "legacy-conservative" in reason


def test_susp1_canaryrun_f607c73a2589_shape_releases_at_20_days(journal):
    """Spec test 9 continued: the SAME row shape at 20 days old -- past the
    default 14-day window (real-world: this is the ~2026-08-16 age-out the
    operator's D2 ruling names) -- releases. No code deleted or dismissed
    the row; it simply aged out."""
    settings = make_settings()
    _insert_tier1_row(journal, "canaryrun_f607c73a2589", days_ago=20, drift_detail=None)
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is False
    assert reason == "no auto-suspend condition met"


# ------------------------------------------------- feed-coverage arm + settings
def test_susp1_feed_coverage_arm_still_fires_alongside_canary_arm(journal):
    """Spec test 10 (part 1): the feed-coverage arm is untouched -- it must
    still fire on its own terms even with zero canary rows present."""
    from datetime import timedelta as _td

    settings = make_settings(SHADOW_LABEL_MIN_FEED_COVERAGE="0.80")
    for i in range(3):
        day = (timeutils.market_date() - _td(days=i)).isoformat()
        _seed_universe_days(journal, day, n_scanned=10, n_fresh=2)  # 0.20 coverage each day
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is True
    assert "consecutive trading days" in reason


def test_susp1_window_setting_default_is_14(journal):
    settings = make_settings()
    assert settings.shadow_suspend_canary_window_days == 14


def test_susp1_window_setting_rejects_below_7():
    with pytest.raises(SettingsError):
        make_settings(SHADOW_SUSPEND_CANARY_WINDOW_DAYS="6")


def test_susp1_window_setting_rejects_above_90():
    with pytest.raises(SettingsError):
        make_settings(SHADOW_SUSPEND_CANARY_WINDOW_DAYS="91")


def test_susp1_window_setting_accepts_bounds():
    assert make_settings(SHADOW_SUSPEND_CANARY_WINDOW_DAYS="7").shadow_suspend_canary_window_days == 7
    assert make_settings(SHADOW_SUSPEND_CANARY_WINDOW_DAYS="90").shadow_suspend_canary_window_days == 90


# --------------------------------------------------------- end-to-end (mech 13)
def test_susp1_e2e_not_confirmed_proceeds_to_labelling_via_run_shadow_label(tmp_path):
    """Spec test 11 (not-confirmed half): driving the PRODUCTION entry
    point ``run_shadow_label``, not just the ``check_auto_suspend`` helper
    -- a prior ticket's audit caught exactly this gap (HOLD-1 lesson). A
    not_confirmed trigger row must not stop shadow labelling."""
    orch, _ = _orch_with_shadow_universe(tmp_path, _seed_symbols(6))
    orch.run_scan_once()
    _seed_universe_days(orch.journal, timeutils.market_date().isoformat())
    _insert_tier1_row(
        orch.journal, "canaryrun_e2e_notconfirmed", days_ago=1,
        drift_detail={"confirmation": {"status": "not_confirmed", "confirming_run_id": "canaryrun_e2e_x"}},
    )

    result = shadow_label.run_shadow_label(orch)

    assert result["status"] == "completed"
    assert result["labelled"] > 0
    assert orch.journal.count_rows("candidate_labels", "shadow_tier = 1") > 0
    orch.journal.close()


def test_susp1_e2e_confirmed_suspends_engages_switch_and_pages_via_run_shadow_label(tmp_path, monkeypatch):
    """Spec test 11 (confirmed half): the confirmed scenario suspends,
    engages ``ShadowLabelSuspendSwitch``, and pages -- proving the wiring,
    not just the helper."""
    orch, _ = _orch_with_shadow_universe(tmp_path, _seed_symbols(6))
    orch.run_scan_once()
    _seed_universe_days(orch.journal, timeutils.market_date().isoformat())
    _insert_tier1_row(
        orch.journal, "canaryrun_e2e_confirmed", days_ago=1,
        drift_detail={"confirmation": {"status": "confirmed", "confirming_run_id": "canaryrun_e2e_y"}},
    )

    switch = ShadowLabelSuspendSwitch(path=str(tmp_path / "SHADOW_LABEL_SUSPENDED"))
    import alphaos.scheduler.shadow_label as sl_module
    orig_switch_cls = sl_module.ShadowLabelSuspendSwitch
    sl_module.ShadowLabelSuspendSwitch = lambda: switch

    paged = {}

    def _capture_alert(settings, title, message, **kwargs):
        paged["title"] = title
        paged["message"] = message
        return True

    monkeypatch.setattr(alerts, "send_alert", _capture_alert)

    try:
        result = shadow_label.run_shadow_label(orch)

        assert result["status"] == "skipped"
        assert "auto-suspend" in result["reason"]
        assert switch.is_engaged()
        assert "confirmed" in switch.reason()
        assert "canaryrun_e2e_confirmed" in switch.reason()
        assert paged.get("title") == "AlphaOS: shadow labelling auto-suspended"
        assert "canaryrun_e2e_confirmed" in paged.get("message", "")
        assert orch.journal.count_rows("candidate_labels", "shadow_tier = 1") == 0
        events = orch.journal.query(
            "SELECT * FROM system_events WHERE category = 'shadow_label' AND severity = 'critical'"
        )
        assert len(events) == 1
    finally:
        sl_module.ShadowLabelSuspendSwitch = orig_switch_cls
        switch.release()
    orch.journal.close()
