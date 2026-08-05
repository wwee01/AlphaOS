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

Audit-fixup round (2026-08, two independent Opus audits, both REQUEST
CHANGES, convergent findings) adds:

12. MUST FIX 1 -- confirmation-status vocabulary lockstep (AST-harvested,
    producer vs consumer) + a cold-import regression (the circular-import
    class that shipped in round 1, invisible to every OTHER test in this
    repo because conftest.py always warms alphaos.scheduler first).
13. MUST FIX 2 -- a cross-class confirmation (TIER_2 trigger confirmed by a
    TIER_1-severity same-day re-run) must latch at TIER_1 severity, driven
    through the REAL CANARY-2 producer (run_canary_confirmed), not a
    hand-built row -- proves the wiring, not just the helper.
14. The "NOT this round" policy boundary: a same-tier TIER_2-confirmed-by-
    TIER_2 trigger does NOT latch (pre-existing on `main`, deliberately left
    alone -- an operator policy question, not a builder fix).

Audit-fixup round 3 (2026-08, both audits upgraded to APPROVE WITH NOTES;
this round's items are the residue) adds:

15. ALSO FIX 3 -- the vocabulary lockstep guard (item 12) now also covers
    reports/canary_report.py, the third real consumer (rendering, not a
    suspend decision) -- previously only shadow_label.py was checked.
16. ALSO FIX 4 -- a fifth arm, unrecognized-status: a real, non-None status
    matching none of the four known literals still latches (right fail
    direction) but is no longer reported byte-identical to a genuinely
    legacy row (an unrecognized status means CURRENT code is writing it
    every cycle -- it will never age out the way a stale legacy row does).

All offline, in-memory, mock mode. No real money, no network.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from alphaos.config.settings import SettingsError
from alphaos.safety import ShadowLabelSuspendSwitch
from alphaos.scheduler import shadow_label
from alphaos.util import alerts, timeutils
from conftest import make_settings
from test_canary import _identity_detail, _label_drift_detail, _seed_pinned_baseline
from test_exp1_shadow_labelling import _orch_with_shadow_universe, _seed_symbols, _seed_universe_days


# --------------------------------------------------------------------- fakes
def _insert_tier1_row(
    journal,
    run_id: str,
    *,
    days_ago: float = 1,
    drift_tier: str = "TIER_1",
    confirmation_of=None,
    drift_detail: dict | None = None,
    drift_detail_json_raw: str | None = None,
) -> None:
    """Directly construct a ``canary_runs`` row shaped like a real weekly
    canary run (§H.1 direct-construction law, same idiom as
    ``test_auto_suspend_triggers_on_canary_tier1``). Exactly one of
    ``drift_detail``/``drift_detail_json_raw`` may be given; neither given
    means a legacy pre-CANARY-2 row (no ``drift_detail_json`` at all).
    ``days_ago`` accepts fractional days for boundary-precision tests.
    ``drift_tier`` defaults to TIER_1 but accepts TIER_2 for MUST FIX 2's
    cross-class coverage (the name ``_insert_tier1_row`` predates that
    generalization; kept to avoid an unnecessary rename of every existing
    call site)."""
    import json as _json

    assert drift_detail is None or drift_detail_json_raw is None
    if drift_detail is not None:
        detail_json = _json.dumps(drift_detail)
    else:
        detail_json = drift_detail_json_raw

    started = timeutils.stamp(timeutils.now_utc() - timedelta(days=days_ago))
    journal.insert("canary_runs", {
        "run_id": run_id, "corpus_dir": "data/canary", "n_prompts": 5,
        "drift_tier": drift_tier, "drift_detail_json": detail_json,
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
    assert "[identity]" in reason
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
    # Bracketed exact token, not a loose substring (audit NIT, both
    # reviewers): "confirmed" is a substring of BOTH "unconfirmed" and
    # "confirmed-cross-class" -- a substring assert would still pass even
    # if an arm mixup bug returned the wrong arm entirely.
    assert "[confirmed]" in reason
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
    assert "[unconfirmed]" in reason
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
    assert "[legacy-conservative]" in reason
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
    assert "[legacy-conservative]" in reason
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
    assert "[legacy-conservative]" in reason


def test_susp1_unrecognized_status_latches_and_names_offending_value(journal):
    """ALSO FIX 4 (audit-fixup round 3, B NEW LOW, judged worth fixing): a
    real, non-None, well-formed status that matches none of the four known
    literals (e.g. a value a NEWER deployment or a hand-edit wrote) latches
    -- correct fail direction -- but must NOT render byte-identical to a
    genuinely legacy row. Distinguished arm, and the offending value is
    named in the reason string so the operator can tell "this ages out on
    its own" (legacy-conservative) apart from "this will not, because
    current code is writing it every cycle" (unrecognized-status)."""
    settings = make_settings()
    _insert_tier1_row(
        journal, "canaryrun_future_status", days_ago=1,
        drift_detail={"confirmation": {"status": "some_future_status_v2"}},
    )
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is True
    assert "[unrecognized-status]" in reason
    assert "[legacy-conservative]" not in reason
    assert "canaryrun_future_status" in reason
    assert "some_future_status_v2" in reason  # the offending value itself, not just the arm name


def test_susp1_unrecognized_status_distinct_from_genuinely_legacy_row(journal):
    """Same shape (in-window TIER_1 trigger, both latch True) but the
    reason strings must differ in their ARM, proving the two cases are not
    conflated -- a genuinely legacy row (no confirmation key at all) still
    reports legacy-conservative, never unrecognized-status."""
    settings = make_settings()
    _insert_tier1_row(journal, "canaryrun_truly_legacy", days_ago=1, drift_detail=None)
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is True
    assert "[legacy-conservative]" in reason
    assert "[unrecognized-status]" not in reason


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


def test_susp1_malformed_timestamp_latches_never_silently_dropped(journal, monkeypatch):
    """ALSO FIX (audit-fixup 2026-08, A L1): fail-direction timestamp
    hardening. A garbage ``started_at_utc`` (here: bare epoch-seconds text,
    the exact shape flagged) must NOT be silently excluded by a lexical
    window compare -- it must be treated as within the window (fail toward
    suspend) and evaluated normally. Unreachable via any real write path
    today (single writer, NOT NULL column) but must hold by construction."""
    fixed_now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(shadow_label.timeutils, "now_utc", lambda: fixed_now)
    settings = make_settings()
    journal.insert("canary_runs", {
        "run_id": "canaryrun_garbage_timestamp", "corpus_dir": "data/canary", "n_prompts": 5,
        "drift_tier": "TIER_1", "drift_detail_json": None, "confirmation_of": None,
        "started_at_utc": "1785936000", "started_at_sgt": "1785936000",
    })
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is True
    assert "canaryrun_garbage_timestamp" in reason
    assert "[legacy-conservative]" in reason


def test_susp1_empty_string_timestamp_latches_never_silently_dropped(journal, monkeypatch):
    """Same fail-direction guard, empty-string shape."""
    fixed_now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(shadow_label.timeutils, "now_utc", lambda: fixed_now)
    settings = make_settings()
    journal.insert("canary_runs", {
        "run_id": "canaryrun_empty_timestamp", "corpus_dir": "data/canary", "n_prompts": 5,
        "drift_tier": "TIER_1", "drift_detail_json": None, "confirmation_of": None,
        "started_at_utc": "", "started_at_sgt": "",
    })
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is True
    assert "canaryrun_empty_timestamp" in reason


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
    assert "[legacy-conservative]" in reason


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
def test_susp1_e2e_not_confirmed_proceeds_to_labelling_via_run_shadow_label(tmp_path, monkeypatch):
    """Spec test 11 (not-confirmed half): driving the PRODUCTION entry
    point ``run_shadow_label``, not just the ``check_auto_suspend`` helper
    -- a prior ticket's audit caught exactly this gap (HOLD-1 lesson). A
    not_confirmed trigger row must not stop shadow labelling.

    ALSO FIX (audit-fixup 2026-08, B LOW): the suspend switch is pinned to
    a tmp_path-scoped file, same as its sibling e2e test below -- otherwise
    this reads/writes the REAL ``data/SHADOW_LABEL_SUSPENDED`` relative to
    cwd, and a stray marker file from an unrelated drill would fail this
    test for a reason that has nothing to do with SUSP-1."""
    orch, _ = _orch_with_shadow_universe(tmp_path, _seed_symbols(6))
    orch.run_scan_once()
    _seed_universe_days(orch.journal, timeutils.market_date().isoformat())
    _insert_tier1_row(
        orch.journal, "canaryrun_e2e_notconfirmed", days_ago=1,
        drift_detail={"confirmation": {"status": "not_confirmed", "confirming_run_id": "canaryrun_e2e_x"}},
    )

    switch = ShadowLabelSuspendSwitch(path=str(tmp_path / "SHADOW_LABEL_SUSPENDED"))
    monkeypatch.setattr(shadow_label, "ShadowLabelSuspendSwitch", lambda: switch)

    try:
        result = shadow_label.run_shadow_label(orch)

        assert result["status"] == "completed"
        assert result["labelled"] > 0
        assert orch.journal.count_rows("candidate_labels", "shadow_tier = 1") > 0
    finally:
        switch.release()
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
    # ALSO FIX (audit-fixup 2026-08, B NIT): monkeypatch.setattr, not a raw
    # module-attribute reassignment + manual try/finally restore -- pytest
    # reverts this automatically even if an assertion below raises.
    monkeypatch.setattr(shadow_label, "ShadowLabelSuspendSwitch", lambda: switch)

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
        assert "[confirmed]" in switch.reason()
        assert "canaryrun_e2e_confirmed" in switch.reason()
        assert paged.get("title") == "AlphaOS: shadow labelling auto-suspended"
        assert "canaryrun_e2e_confirmed" in paged.get("message", "")
        assert orch.journal.count_rows("candidate_labels", "shadow_tier = 1") == 0
        events = orch.journal.query(
            "SELECT * FROM system_events WHERE category = 'shadow_label' AND severity = 'critical'"
        )
        assert len(events) == 1
    finally:
        switch.release()
    orch.journal.close()


# ================================================== audit-fixup round (2026-08)
# ---------------------------------------------- MUST FIX 2: cross-class latch
def test_susp1_cross_class_tier2_confirmed_by_tier1_latches_via_real_producer(tmp_path, journal, monkeypatch):
    """MUST FIX 2 (A HIGH-1 / B HIGH-2, CONVERGENT): drives the REAL
    CANARY-2 producer (``run_canary_confirmed``), not a hand-built row -- a
    TIER_2/label-drift trigger confirmed by a TIER_1/identity same-day
    re-run (a genuine model swap, e.g. gpt-5.1->gpt-5.2) must LATCH the
    auto-suspend at TIER_1 severity even though the TRIGGER row itself
    never carried TIER_1. Round-1 SUSP-1 code excluded ALL TIER_2 trigger
    rows from the query, so CANARY-2 itself paged "model drift CONFIRMED
    (TIER_1)" while ``check_auto_suspend`` returned False -- a regression
    vs `main`'s original (unfiltered, TIER_1-only-literal-but-catch-all)
    query. Mirrors ``tests/test_canary.py::
    test_canary2_auditfix_cross_class_tier2_confirmed_by_identity_keeps_diff``'s
    own real-producer probe exactly."""
    settings = make_settings(NTFY_TOPIC="test-topic")
    corpus_dir = str(tmp_path / "corpus")
    _seed_pinned_baseline(journal, settings, corpus_dir)

    import alphaos.canary.run as run_module
    from alphaos.canary.run import run_canary_confirmed
    from alphaos.constants import DRIFT_TIER_1, DRIFT_TIER_2

    responses = iter([
        (DRIFT_TIER_2, _label_drift_detail(4, 1, 3)),
        (DRIFT_TIER_1, _identity_detail(["gpt-5.1"], ["gpt-5.2"])),
    ])
    monkeypatch.setattr(run_module, "_compute_drift", lambda *a, **kw: next(responses))
    monkeypatch.setattr(run_module.alerts, "send_alert", lambda *a, **kw: True)

    result = run_canary_confirmed(journal, settings, corpus_dir=corpus_dir)
    assert result["confirmed"] is True  # sanity: CANARY-2 itself paged this (pre-existing law)
    assert result["paged"] is True

    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is True
    assert "[confirmed-cross-class]" in reason
    assert result["run_id"] in reason


def test_susp1_same_tier_tier2_confirmed_does_not_latch_policy_boundary(journal):
    """The "NOT this round" boundary, encoded as a test: a same-tier
    TIER_2-trigger confirmed by a TIER_2-severity re-run must NOT latch.
    This is deliberately pre-existing `main` behavior (the old query never
    read TIER_2 rows at all) -- whether a same-tier confirmed TIER_2 should
    EVER suspend is an open operator policy question the spec's own
    decision log records but does not decide; MUST FIX 2 only ever expands
    coverage for the cross-class-to-TIER_1 case, never plain TIER_2."""
    settings = make_settings()
    _insert_tier1_row(
        journal, "canaryrun_same_tier2_confirmed", days_ago=1, drift_tier="TIER_2",
        drift_detail={
            "confirmation": {
                "status": "confirmed", "confirming_run_id": "canaryrun_x", "confirming_drift_tier": "TIER_2",
            },
        },
    )
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is False
    assert reason == "no auto-suspend condition met"


def test_susp1_tier2_trigger_not_confirmed_does_not_latch(journal):
    """Adjacent boundary case: a TIER_2 trigger's OWN not_confirmed/
    unconfirmed_page/legacy outcomes stay exactly pre-SUSP-1 (unlatched) --
    only the cross-class-confirmed-to-TIER_1 path is new."""
    settings = make_settings()
    _insert_tier1_row(
        journal, "canaryrun_tier2_notconfirmed", days_ago=1, drift_tier="TIER_2",
        drift_detail={"confirmation": {"status": "not_confirmed", "confirming_drift_tier": "TIER_2"}},
    )
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is False


def test_susp1_tier2_trigger_legacy_shape_does_not_latch(journal):
    """A TIER_2 trigger with NO confirmation annotation at all (legacy
    shape) does not latch -- TIER_2's conservative default is "don't latch,"
    unlike TIER_1's "latch," precisely because pre-SUSP-1 `main` never read
    TIER_2 rows in the first place; there is no regression to guard against
    here in the other direction."""
    settings = make_settings()
    _insert_tier1_row(journal, "canaryrun_tier2_legacy", days_ago=1, drift_tier="TIER_2", drift_detail=None)
    should_suspend, reason = shadow_label.check_auto_suspend(journal, settings)
    assert should_suspend is False


# ------------------------------------------- MUST FIX 1: vocabulary lockstep
def _resolve_ast_literal(node: ast.AST, module) -> object:
    """Resolve an AST node to its underlying Python value: a raw string
    literal resolves directly; a bare ``Name`` resolves via ``getattr`` on
    the module that contains it (this is how both ``canary/run.py`` and
    ``shadow_label.py`` now reference the shared ``CANARY_CONFIRMATION_
    STATUS_*`` constants imported from ``alphaos.constants`` -- as plain
    names in their own namespace, via ``from alphaos.constants import
    X``). Raises loudly on anything else -- an unresolvable node means the
    harvester itself needs updating, not a silent skip."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return getattr(module, node.id)
    raise AssertionError(
        f"lockstep harvester: unresolvable AST node {ast.dump(node)} in {module.__name__}"
    )


def _harvest_confirmation_status_producer_values(module) -> set:
    """AST-harvest every value written to a ``"status"`` key inside a dict
    literal in ``module``'s own source -- the PRODUCER side of the
    confirmation-status vocabulary (CANARY-2's ``run_canary_confirmed``,
    ``alphaos/canary/run.py``). Source, not bytecode -- ``ast.walk``
    recurses into nested/starred dict literals (``{**detail,
    "confirmation": {"status": X}}``) automatically."""
    tree = ast.parse(inspect.getsource(module))
    values = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key_node, value_node in zip(node.keys, node.values):
                if isinstance(key_node, ast.Constant) and key_node.value == "status":
                    values.add(_resolve_ast_literal(value_node, module))
    return values


def _harvest_confirmation_status_consumer_values(module, compared_name: str = "status") -> set:
    """AST-harvest every value compared against a local named
    ``compared_name`` via ``==`` in ``module``'s own source -- the CONSUMER
    side (``shadow_label._canary_confirmation_latch``)."""
    tree = ast.parse(inspect.getsource(module))
    values = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
            left, right = node.left, node.comparators[0]
            if isinstance(left, ast.Name) and left.id == compared_name:
                values.add(_resolve_ast_literal(right, module))
            elif isinstance(right, ast.Name) and right.id == compared_name:
                values.add(_resolve_ast_literal(left, module))
    return values


_EXPECTED_CONFIRMATION_STATUS_VOCABULARY = {
    "identity_immediate", "confirmed", "unconfirmed_page", "not_confirmed",
}


def test_susp1_confirmation_status_vocabulary_lockstep_producer_consumer():
    """MUST FIX 1(b) (B HIGH-1 / A HIGH-2, CONVERGENT), extended (audit-fixup
    round 3, ALSO FIX 3, B NEW LOW): the four confirmation-status literals
    are centralized in ``alphaos.constants``, but THIS test independently
    proves the PRODUCER (``canary/run.py``'s ``run_canary_confirmed``, which
    WRITES ``{"status": ...}``) and BOTH real consumers -- ``shadow_label.py``'s
    ``_canary_confirmation_latch`` (COMPARES ``status == ...`` to decide
    whether to suspend) and ``reports/canary_report.py``'s ``render_markdown``
    (COMPARES ``status == ...`` to render the operator-facing drift line) --
    still spell the exact same four-value vocabulary. Harvested from source
    via AST, never hand-copied into this test. House precedent:
    ``test_ab_eval.py::test_candidate_whitelist_matches_scanner_creation_
    insert``'s own AST-introspected lockstep pattern (INSTR-3 spec).

    round 3's own finding (B's mutation F): drifting ``canary_report.py``
    alone to a bare literal previously left the whole repo green -- the
    harvester already handled that module correctly once pointed at it, it
    just wasn't being asked. Every consumer this repo has for this
    vocabulary is now covered; a fourth consumer, if one is ever added,
    would need a fourth line here (there is no way to enumerate "every
    consumer" fully automatically without a repo-wide AST sweep, which is
    out of scope for this ticket's own blast radius).

    Mutation-tested by hand before landing (see the audit-fixup commit
    message / build report for the exact before/after): temporarily
    hardcoding a raw ``"unconfirmed-page-typo"`` string in place of the
    imported ``CANARY_CONFIRMATION_STATUS_UNCONFIRMED_PAGE`` constant in one
    of ``_canary_confirmation_latch``'s comparisons turns this test RED
    (consumer-only value ``{"unconfirmed-page-typo"}``, producer-only value
    ``{"unconfirmed_page"}``) while every other test in this file (including
    the ``unconfirmed_page`` behavioral test above) still independently
    exercises the ACTUAL runtime behavior -- proving this test catches a
    vocabulary drift that a purely behavioral suite would not. Separately
    re-verified for this round's extension: hardcoding a raw literal in
    ``canary_report.py``'s own comparison turns this test RED too (see the
    build report for the exact before/after)."""
    from alphaos.canary import run as canary_run_module
    from alphaos.reports import canary_report as canary_report_module
    from alphaos.scheduler import shadow_label as shadow_label_module

    producer_values = _harvest_confirmation_status_producer_values(canary_run_module)
    consumer_values = _harvest_confirmation_status_consumer_values(shadow_label_module)
    report_values = _harvest_confirmation_status_consumer_values(canary_report_module)

    for consumer_name, consumer_values_set in (
        ("shadow_label.py", consumer_values), ("reports/canary_report.py", report_values),
    ):
        producer_only = producer_values - consumer_values_set
        consumer_only = consumer_values_set - producer_values
        assert not producer_only, (
            f"canary/run.py writes a confirmation status {consumer_name} never checks: {producer_only}"
        )
        assert not consumer_only, (
            f"{consumer_name} checks a confirmation status canary/run.py never writes: {consumer_only}"
        )
        assert consumer_values_set == _EXPECTED_CONFIRMATION_STATUS_VOCABULARY

    assert producer_values == _EXPECTED_CONFIRMATION_STATUS_VOCABULARY


def test_susp1_lockstep_harvester_reports_a_deliberately_broken_vocabulary():
    """Proves the harvester itself has teeth (in-suite, no source
    mutation required): fed a deliberately mismatched pair of fake modules
    -- one "producer" that writes a status the other "consumer" never
    checks, and vice versa -- the harvester must surface BOTH one-sided
    sets, not silently intersect them away. This is what the hand
    source-mutation test described in the sibling test's own docstring
    would trip in the real modules; this version proves it deterministically
    without touching real source files."""
    import types

    producer_src = (
        "def build():\n"
        "    return {'confirmation': {'status': 'identity_immediate'}}\n"
        "def build2():\n"
        "    return {'confirmation': {'status': 'producer_only_value'}}\n"
    )
    consumer_src = (
        "def check(status):\n"
        "    if status == 'identity_immediate':\n"
        "        return True\n"
        "    if status == 'consumer_only_value':\n"
        "        return True\n"
        "    return False\n"
    )
    fake_producer = types.ModuleType("fake_producer")
    fake_producer.__source_override__ = producer_src
    fake_consumer = types.ModuleType("fake_consumer")
    fake_consumer.__source_override__ = consumer_src

    import inspect as _inspect
    orig_getsource = _inspect.getsource

    def _fake_getsource(obj):
        override = getattr(obj, "__source_override__", None)
        return override if override is not None else orig_getsource(obj)

    import unittest.mock as mock
    with mock.patch.object(inspect, "getsource", side_effect=_fake_getsource):
        producer_values = _harvest_confirmation_status_producer_values(fake_producer)
        consumer_values = _harvest_confirmation_status_consumer_values(fake_consumer, compared_name="status")

    assert producer_values == {"identity_immediate", "producer_only_value"}
    assert consumer_values == {"identity_immediate", "consumer_only_value"}
    assert producer_values - consumer_values == {"producer_only_value"}
    assert consumer_values - producer_values == {"consumer_only_value"}


# ------------------------------------------------- MUST FIX 1: cold import
def test_susp1_cold_import_canary_modules_no_circular_import():
    """MUST FIX 1(a) (B BLOCKER / B HIGH-1 / A HIGH-2, CONVERGENT): every
    module in the canary/shadow-label dependency chain must cold-import
    cleanly in a GENUINELY FRESH interpreter. Round-1 SUSP-1 code created a
    real circular import: ``alphaos.canary`` -> ``canary.run`` ->
    ``alphaos.scheduler`` (via ``cost_guard``) -> ``scheduler.digest`` ->
    ``scheduler.shadow_label`` -> back into the still-initializing
    ``canary.run`` (which ``shadow_label.py`` imported ``DRIFT_TIER_1``
    from directly). Invisible to every OTHER test in this repo because
    ``conftest.py`` always warms ``alphaos.scheduler`` first; invisible in
    production only because ``__main__.py`` happens to import
    ``alphaos.scheduler`` before ``alphaos.canary`` (an untested ordering
    accident, per the audit). A subprocess with a genuinely fresh
    ``sys.modules`` is the only way this repo can catch this bug class --
    there is currently no other import-hygiene test anywhere in ``tests/``.

    Fixed by promoting the shared ``DRIFT_TIER_*``/confirmation-status
    vocabulary to ``alphaos.constants`` (a leaf module, zero ``alphaos.*``
    imports of its own) so ``shadow_label.py`` no longer needs to import
    anything from ``alphaos.canary.run`` at all."""
    for module_name in (
        "alphaos.canary",
        "alphaos.canary.run",
        "alphaos.canary.corpus",
        "alphaos.reports.canary_report",
        "alphaos.scheduler.shadow_label",
    ):
        result = subprocess.run(
            [sys.executable, "-c", f"import {module_name}"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"cold import of {module_name!r} failed in a fresh interpreter:\n{result.stderr}"
        )
