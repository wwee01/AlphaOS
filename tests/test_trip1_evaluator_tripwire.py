"""TRIP-1: Evaluator identity tripwire
(docs/roadmap/alphaos-evaluator-replay-and-coherence-specs.md, TRIP-1
section, refined 2026-07-27; dedupe + severity corrected 2026-07-28
audit-fixup after two independent Opus audits both returned REQUEST
CHANGES on the same root cause from different angles).

15 named tests map 1:1 onto the spec's own "Tests (hermetic, S.H.1
discipline)" list; a further block of audit-fixup tests below them
proves the 2026-07-28 corrections (MUST FIX 1/2, fixes 3-6):

1.  test_model_axis_mismatch_fires_one_event_and_one_alert
2.  test_prompt_axis_mismatch_alone_fires_model_axis_silent
3.  test_both_axes_mismatch_fires_two_events_one_alert
4.  test_match_on_both_axes_no_event_no_alert
5.  test_empty_ledger_is_a_silent_noop
6.  test_only_mock_rows_is_a_silent_noop
7.  test_null_prompt_template_version_model_axis_still_checked
8.  test_ordering_a_self_heal_fires_once_then_silent
9.  test_ordering_b_dedupe_suppresses_repeat_fire
10. test_flip_flop_fires_three_times
11. test_reference_ordering_uses_id_desc_not_created_at_utc
12. test_fail_open_on_reference_read_exception
13. test_alert_failure_isolation_event_still_persisted
14. test_tripwire_runs_before_first_openai_evaluations_insert_ast
15. test_zero_decision_surface

Audit-fixup tests (2026-07-28, mapped to the audit findings that
required them):
- test_fix1a_reapply_after_reference_advances_fires_again (A HIGH-1 / B H1)
- test_fix1b_undelivered_alert_is_not_suppressed_next_scan (B H1, real
  empty-NTFY_TOPIC branch, no monkeypatch)
- test_fix1_delivered_alert_still_suppresses_on_true_repeat (explicit
  "ordering (a)" the audits asked to be pinned)
- test_fix1c_mid_check_exception_orphans_only_the_failed_axis (B M3)
- test_fix2_error_severity_surfaces_in_daily_digest (MUST FIX 2)
- test_fix5_exact_prefix_dedupe_does_not_cross_match_a_similar_axis_name (A LOW-1 / B N1)
- test_fix6_behavioral_placement_real_scan_fires_when_identity_changed (A LOW-2 / B N2)

All offline, in-memory, hermetic. No real money, no network -- every
test that wants to observe an alert monkeypatches
``alphaos.tripwire.alerts.send_alert`` directly (the established
convention -- see tests/test_pr13_card_scoreboard.py,
tests/test_canary.py), EXCEPT test_fix1b, which deliberately uses the
REAL ``alerts.send_alert`` with an unset ``NTFY_TOPIC`` to prove the
undelivered-page fix against production code, not a stub.
"""

from __future__ import annotations

import json

from alphaos import tripwire as tripwire_module
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.safety import KillSwitch
from alphaos.scheduler.digest import build_daily_digest
from alphaos.tripwire import check_evaluator_identity
from alphaos.util.ids import new_id
from conftest import make_settings


# --------------------------------------------------------------- fixtures/helpers
def _journal():
    return JournalStore(":memory:")


def _seed_real_eval(journal, model="gpt-5.4-mini", prompt_version="v1", symbol="AAPL", **over):
    """Insert a REAL (``is_mock=0``) ``openai_evaluations`` row -- the
    detection reference this ticket reads. ``prompt_version=None`` seeds
    a legacy/hand-tampered NULL row (Test 7)."""
    cand_id = new_id("cand")
    journal.insert("candidates", {
        "candidate_id": cand_id, "symbol": symbol, "direction": "long", "strategy": "swing",
        "momentum_score": 0.7, "status": "watch", "armed_watch": 0,
        "scan_id": "scan_x", "scan_batch_id": "scanb_x", "playbook_name": "momentum",
    })
    eval_id = new_id("eval")
    row = {
        "eval_id": eval_id, "candidate_id": cand_id, "symbol": symbol, "model": model,
        "prompt_template_version": prompt_version, "is_mock": 0, "decision": "propose",
    }
    row.update(over)
    journal.insert("openai_evaluations", row)
    return eval_id


def _seed_mock_eval(journal, symbol="AAPL"):
    cand_id = new_id("cand")
    journal.insert("candidates", {
        "candidate_id": cand_id, "symbol": symbol, "direction": "long", "strategy": "swing",
        "momentum_score": 0.7, "status": "watch", "armed_watch": 0,
        "scan_id": "scan_x", "scan_batch_id": "scanb_x", "playbook_name": "momentum",
    })
    eval_id = new_id("eval")
    journal.insert("openai_evaluations", {
        "eval_id": eval_id, "candidate_id": cand_id, "symbol": symbol, "model": "mock",
        "prompt_template_version": "v1", "is_mock": 1, "decision": "propose",
    })
    return eval_id


def _patch_alerts(monkeypatch, returns=True):
    sent = []
    monkeypatch.setattr(
        tripwire_module.alerts, "send_alert",
        lambda settings, title, message, priority="default", journal=None: (
            sent.append({"title": title, "message": message, "priority": priority}) or returns
        ),
    )
    return sent


def _tripwire_events(journal):
    return journal.query(
        "SELECT * FROM system_events WHERE category = 'tripwire' ORDER BY id ASC"
    )


def _detail(row):
    return json.loads(row["detail_json"]) if row.get("detail_json") else {}


# ---------------------------------------------------------------- Spec test 1
def test_model_axis_mismatch_fires_one_event_and_one_alert(monkeypatch):
    journal = _journal()
    sent = _patch_alerts(monkeypatch)
    ref_eval_id = _seed_real_eval(journal, model="gpt-5.4-mini", prompt_version="v1")
    settings = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna", OPENAI_PROMPT_VERSION="v1")

    result = check_evaluator_identity(journal, settings)

    events = _tripwire_events(journal)
    assert len(events) == 1
    # 2026-07-28 audit-fixup (MUST FIX 2): ERROR, not WARNING, so the daily
    # digest (severity IN error/critical) surfaces an undelivered page.
    assert events[0]["severity"] == "error"
    assert events[0]["category"] == "tripwire"
    assert events[0]["message"] == "TRIP-1 openai_primary_model: 'gpt-5.4-mini' -> 'gpt-5.6-luna'"
    assert result["fired"] == ["openai_primary_model"]

    detail = _detail(events[0])
    assert detail["reference_eval_id"] == ref_eval_id
    assert detail["alert_sent"] is True  # confirmed delivery -- send_alert returned True

    assert len(sent) == 1
    body = sent[0]["message"]
    assert sent[0]["title"] == "AlphaOS TRIP-1: evaluator identity changed"
    assert sent[0]["priority"] == "high"
    assert "openai_primary_model: gpt-5.4-mini -> gpt-5.6-luna" in body
    assert "alphaos ab_eval_run --arms gpt-5.4-mini:v1 gpt-5.6-luna:v1" in body
    assert "This page is the receipt" in body
    assert "Not deliberate? Revert .env" in body
    journal.close()


# ---------------------------------------------------------------- Spec test 2
def test_prompt_axis_mismatch_alone_fires_model_axis_silent(monkeypatch):
    journal = _journal()
    sent = _patch_alerts(monkeypatch)
    _seed_real_eval(journal, model="gpt-5.6-luna", prompt_version="v2")
    settings = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna", OPENAI_PROMPT_VERSION="v3")

    result = check_evaluator_identity(journal, settings)

    events = _tripwire_events(journal)
    assert len(events) == 1
    assert events[0]["severity"] == "error"
    assert events[0]["message"] == "TRIP-1 openai_prompt_version: 'v2' -> 'v3'"
    assert result["fired"] == ["openai_prompt_version"]
    assert "openai_primary_model" not in "".join(e["message"] for e in events)

    assert len(sent) == 1
    assert "alphaos ab_eval_run --arms gpt-5.6-luna:v2 gpt-5.6-luna:v3" in sent[0]["message"]
    journal.close()


# ---------------------------------------------------------------- Spec test 3
def test_both_axes_mismatch_fires_two_events_one_alert(monkeypatch):
    journal = _journal()
    sent = _patch_alerts(monkeypatch)
    _seed_real_eval(journal, model="gpt-5.4-mini", prompt_version="v2")
    settings = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna", OPENAI_PROMPT_VERSION="v3")

    result = check_evaluator_identity(journal, settings)

    events = _tripwire_events(journal)
    assert len(events) == 2
    assert all(e["severity"] == "error" for e in events)
    messages = {e["message"] for e in events}
    assert messages == {
        "TRIP-1 openai_primary_model: 'gpt-5.4-mini' -> 'gpt-5.6-luna'",
        "TRIP-1 openai_prompt_version: 'v2' -> 'v3'",
    }
    assert all(_detail(e)["alert_sent"] is True for e in events)
    assert set(result["fired"]) == {"openai_primary_model", "openai_prompt_version"}

    assert len(sent) == 1  # ONE alert covering both axes, not two
    body = sent[0]["message"]
    assert "openai_primary_model: gpt-5.4-mini -> gpt-5.6-luna" in body
    assert "openai_prompt_version: v2 -> v3" in body
    journal.close()


# ---------------------------------------------------------------- Spec test 4
def test_match_on_both_axes_no_event_no_alert(monkeypatch):
    journal = _journal()
    sent = _patch_alerts(monkeypatch)
    _seed_real_eval(journal, model="gpt-5.6-luna", prompt_version="v3")
    settings = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna", OPENAI_PROMPT_VERSION="v3")

    result = check_evaluator_identity(journal, settings)

    assert _tripwire_events(journal) == []
    assert sent == []
    assert result["fired"] == []
    assert set(result["checked"]) == {"openai_primary_model", "openai_prompt_version"}
    journal.close()


# ---------------------------------------------------------------- Spec test 5
def test_empty_ledger_is_a_silent_noop(monkeypatch):
    journal = _journal()
    sent = _patch_alerts(monkeypatch)
    settings = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna")

    result = check_evaluator_identity(journal, settings)

    assert _tripwire_events(journal) == []
    assert sent == []
    assert result == {"checked": [], "fired": [], "suppressed": [], "reference_eval_id": None}
    journal.close()


# ---------------------------------------------------------------- Spec test 6
def test_only_mock_rows_is_a_silent_noop(monkeypatch):
    journal = _journal()
    sent = _patch_alerts(monkeypatch)
    _seed_mock_eval(journal)
    settings = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna")

    result = check_evaluator_identity(journal, settings)

    assert _tripwire_events(journal) == []
    assert sent == []
    assert result["reference_eval_id"] is None  # never a 'mock' -> real page
    journal.close()


# ---------------------------------------------------------------- Spec test 7
def test_null_prompt_template_version_model_axis_still_checked(monkeypatch):
    journal = _journal()
    sent = _patch_alerts(monkeypatch)
    _seed_real_eval(journal, model="gpt-5.4-mini", prompt_version=None)
    settings = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna", OPENAI_PROMPT_VERSION="v2")

    result = check_evaluator_identity(journal, settings)  # must not raise

    assert result["checked"] == ["openai_primary_model"]
    assert result["fired"] == ["openai_primary_model"]
    events = _tripwire_events(journal)
    assert len(events) == 1
    assert events[0]["message"] == "TRIP-1 openai_primary_model: 'gpt-5.4-mini' -> 'gpt-5.6-luna'"
    assert len(sent) == 1
    journal.close()


# ---------------------------------------------------------------- Spec test 8
def test_ordering_a_self_heal_fires_once_then_silent(monkeypatch):
    journal = _journal()
    sent = _patch_alerts(monkeypatch)
    _seed_real_eval(journal, model="gpt-5.4-mini", prompt_version="v1")
    settings = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna", OPENAI_PROMPT_VERSION="v1")

    result1 = check_evaluator_identity(journal, settings)
    assert result1["fired"] == ["openai_primary_model"]
    assert len(_tripwire_events(journal)) == 1
    assert len(sent) == 1

    # scan N's own candidate loop inserts a real row under the NEW identity
    _seed_real_eval(journal, model="gpt-5.6-luna", prompt_version="v1")

    result2 = check_evaluator_identity(journal, settings)
    assert result2["fired"] == []
    assert result2["suppressed"] == []
    assert len(_tripwire_events(journal)) == 1  # no second event
    assert len(sent) == 1  # no second alert
    journal.close()


# ---------------------------------------------------------------- Spec test 9
def test_ordering_b_dedupe_suppresses_repeat_fire(monkeypatch):
    journal = _journal()
    sent = _patch_alerts(monkeypatch)
    _seed_real_eval(journal, model="gpt-5.4-mini", prompt_version="v1")
    settings = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna", OPENAI_PROMPT_VERSION="v1")

    result1 = check_evaluator_identity(journal, settings)
    assert result1["fired"] == ["openai_primary_model"]
    assert len(_tripwire_events(journal)) == 1
    assert len(sent) == 1

    # ai_degraded / empty shortlist / mock day: NO new real row lands, so the
    # reference_eval_id is UNCHANGED and the prior page was confirmed
    # delivered -- all three of the corrected dedupe condition hold.
    result2 = check_evaluator_identity(journal, settings)
    assert result2["fired"] == []
    assert result2["suppressed"] == ["openai_primary_model"]
    assert len(_tripwire_events(journal)) == 1  # still just the one event
    assert len(sent) == 1  # still just the one alert
    journal.close()


# --------------------------------------------------------------- Spec test 10
def test_flip_flop_fires_three_times(monkeypatch):
    journal = _journal()
    sent = _patch_alerts(monkeypatch)
    settings_b = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna", OPENAI_PROMPT_VERSION="v1")
    settings_a = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.4-mini", OPENAI_PROMPT_VERSION="v1")

    # Reference starts at A ("gpt-5.4-mini"); settings already at B -> A->B fires.
    _seed_real_eval(journal, model="gpt-5.4-mini", prompt_version="v1")
    r1 = check_evaluator_identity(journal, settings_b)
    assert r1["fired"] == ["openai_primary_model"]

    # A real row lands under B; operator reverts to A -> B->A fires.
    _seed_real_eval(journal, model="gpt-5.6-luna", prompt_version="v1")
    r2 = check_evaluator_identity(journal, settings_a)
    assert r2["fired"] == ["openai_primary_model"]

    # A real row lands under A; operator flips back to B -> A->B fires AGAIN
    # (message identical to r1's, but the LATEST tripwire event for this axis
    # is now the B->A one from r2, so it is not a dedupe hit -- and even by
    # message+reference alone this is a NEW reference_eval_id, so the
    # corrected 3-way dedupe would refire even if it weren't).
    _seed_real_eval(journal, model="gpt-5.4-mini", prompt_version="v1")
    r3 = check_evaluator_identity(journal, settings_b)
    assert r3["fired"] == ["openai_primary_model"]

    assert len(_tripwire_events(journal)) == 3
    assert len(sent) == 3
    journal.close()


# --------------------------------------------------------------- Spec test 11
def test_reference_ordering_uses_id_desc_not_created_at_utc(monkeypatch):
    journal = _journal()
    sent = _patch_alerts(monkeypatch)
    same_ts = "2026-07-27T00:00:00.000000+00:00"
    _seed_real_eval(journal, model="gpt-5.4-mini", prompt_version="v1", created_at_utc=same_ts)
    newer_eval_id = _seed_real_eval(
        journal, model="gpt-5.6-luna", prompt_version="v1", created_at_utc=same_ts,
    )
    settings = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna", OPENAI_PROMPT_VERSION="v1")

    result = check_evaluator_identity(journal, settings)

    # The higher-id row (inserted second) carries the currently-configured
    # identity -> no mismatch. A created_at_utc-based ORDER BY could pick
    # either tied row and would flake between fire/no-fire.
    assert result["fired"] == []
    assert result["reference_eval_id"] == newer_eval_id
    assert sent == []
    journal.close()


# --------------------------------------------------------------- Spec test 12
def test_fail_open_on_reference_read_exception(monkeypatch):
    journal = _journal()
    orig_one = journal.one

    def _raising_one(sql, params=()):
        if "FROM openai_evaluations WHERE is_mock" in sql:
            raise RuntimeError("simulated reference-read failure")
        return orig_one(sql, params)

    monkeypatch.setattr(journal, "one", _raising_one)
    settings = make_settings()
    orch = Orchestrator(settings=settings, journal=journal)

    summary = orch.run_scan_once()  # must complete normally, no exception

    assert summary is not None
    errors = journal.query(
        "SELECT * FROM system_events WHERE category = 'tripwire' AND severity = 'error'"
    )
    assert len(errors) == 1
    assert errors[0]["message"].startswith("TRIP-1 check failed:")
    assert "scan continues" in errors[0]["message"]
    journal.close()


# --------------------------------------------------------------- Spec test 13
def test_alert_failure_isolation_event_still_persisted(monkeypatch):
    journal = _journal()

    def _raising_send_alert(*a, **k):
        raise RuntimeError("simulated ntfy failure")

    monkeypatch.setattr(tripwire_module.alerts, "send_alert", _raising_send_alert)
    _seed_real_eval(journal, model="gpt-5.4-mini", prompt_version="v1")
    settings = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna", OPENAI_PROMPT_VERSION="v1")

    result = check_evaluator_identity(journal, settings)  # must not raise

    assert result is not None
    mismatch_events = [
        e for e in _tripwire_events(journal)
        if e["message"].startswith("TRIP-1 openai_primary_model:")
    ]
    # The per-axis event was inserted BEFORE the alert send, so it survives
    # send_alert raising -- but since the send never confirmed, alert_sent
    # stays False (2026-07-28 audit-fixup: this is exactly what makes the
    # next scan retry rather than silently treating an undelivered page as
    # handled -- see test_fix1b below for the end-to-end proof of that).
    assert len(mismatch_events) == 1
    assert mismatch_events[0]["severity"] == "error"
    assert mismatch_events[0]["message"] == "TRIP-1 openai_primary_model: 'gpt-5.4-mini' -> 'gpt-5.6-luna'"
    assert _detail(mismatch_events[0])["alert_sent"] is False
    journal.close()


# --------------------------------------------------------------- Spec test 14
def test_tripwire_runs_before_first_openai_evaluations_insert_ast():
    """Placement structural test (house AST call-order pattern, same shape
    as tests/test_ab_eval.py's own '_ast' checks): in
    Orchestrator.run_scan_once, check_evaluator_identity must be called
    strictly before the scan's own first openai_evaluations insert -- the
    single production insert site. If it ran after, that insert would move
    the detection reference before it was ever compared (Design 1/4).

    This is a SOURCE-ORDER check (``node.lineno``), not an execution-order
    proof -- see test_fix6_behavioral_placement_real_scan_fires_when_
    identity_changed below for the behavioral companion (2026-07-28
    audit-fixup, A LOW-2 / B N2): wrapping the call in a never-taken
    conditional would keep THIS test green while making the tripwire
    conditionally blind at runtime."""
    import ast
    import inspect
    import textwrap

    from alphaos import orchestrator as orch_module

    source = inspect.getsource(orch_module.Orchestrator.run_scan_once)
    tree = ast.parse(textwrap.dedent(source))

    tripwire_call_lines = []
    eval_insert_lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "check_evaluator_identity":
            tripwire_call_lines.append(node.lineno)
        if isinstance(func, ast.Attribute) and func.attr == "insert":
            if node.args and isinstance(node.args[0], ast.Constant) \
                    and node.args[0].value == "openai_evaluations":
                eval_insert_lines.append(node.lineno)

    assert tripwire_call_lines, "check_evaluator_identity is not called in run_scan_once"
    assert eval_insert_lines, "no openai_evaluations insert found in run_scan_once"
    assert min(tripwire_call_lines) < min(eval_insert_lines), (
        "check_evaluator_identity must run BEFORE the scan's first "
        "openai_evaluations insert -- placement is load-bearing (Design 1/4)."
    )


# --------------------------------------------------------------- Spec test 15
# 2026-07-28 audit-fixup (B M1): B walked straight through the original
# 6-package list by adding a real import+call to alphaos/proposals/ttl.py
# (proposal expiry -- decision-adjacent) and this test stayed GREEN. Widened
# to also cover proposals/, broker/, regime/, cards/, learning/.
_DECISION_SURFACE_PACKAGES = (
    "risk", "strategy", "execution", "tqs", "scanner", "ai",
    "proposals", "broker", "regime", "cards", "learning",
)
_DECISION_SURFACE_ROOT_FILES = ("approval.py", "safety.py")
_DECISION_SURFACE_EXTRA_FILES = ("hypotheses/proposer.py", "scheduler/shadow_label.py")
# alphaos/orchestrator.py is DELIBERATELY EXCLUDED from this sweep, not
# silently omitted: it is the one file that LEGITIMATELY imports and calls
# check_evaluator_identity (Design 1's own hook point) -- see
# test_tripwire_runs_before_first_openai_evaluations_insert_ast and
# test_fix6_behavioral_placement_real_scan_fires_when_identity_changed
# above/below for orchestrator.py's own placement proofs instead.


def _decision_surface_files():
    import pathlib

    import alphaos

    root = pathlib.Path(alphaos.__file__).parent
    files = []
    for pkg in _DECISION_SURFACE_PACKAGES:
        files += sorted((root / pkg).rglob("*.py"))
    for fname in _DECISION_SURFACE_ROOT_FILES:
        files.append(root / fname)
    for rel in _DECISION_SURFACE_EXTRA_FILES:
        files.append(root / rel)
    files = [f for f in files if "__pycache__" not in f.parts]
    assert files, "decision-surface glob found nothing -- package layout changed, fix the glob"
    return files


def test_zero_decision_surface(monkeypatch):
    """Two independent proofs, mirroring HOLD-1's own widened glob-based
    decision-surface sweep (test_no_live_scan_eval_risk_execution_module_
    reads_the_new_columns_ast, spec test 5's audit-round widening) rather
    than a narrow hand-listed module set:

    (a) A SOURCE-TEXT sweep (2026-07-28 audit-fixup, B M2 -- named
        honestly, not oversold as "structural"/AST-based): no file
        anywhere under the widened decision-surface packages (risk/,
        strategy/, execution/, tqs/, scanner/, ai/, proposals/, broker/,
        regime/, cards/, learning/), nor approval.py/safety.py, nor the
        hypotheses/shadow-label modules that read candidate_outcomes from
        outside those packages, may contain the substring 'tripwire' or
        'check_evaluator_identity'. This is plain substring matching over
        each file's raw text -- it catches the realistic accidental case
        (a stray import, a live Call, a docstring/comment mention) and
        NOTHING adversarial: B defeated an earlier draft of this exact
        style of check elsewhere in the house pattern with a trivially
        split string literal (``"trip" "wire"``), and the same defeat
        works here. It is a regression tripwire for accidental coupling,
        not a security boundary against a deliberately evasive change.
    (b) BEHAVIORAL: scan output (ScanSummary fields) is byte-identical
        whether the tripwire fires this scan or stays silent -- same
        direct-comparison proof REG-1's scope audit used.
    """
    for path in _decision_surface_files():
        text = path.read_text(encoding="utf-8")
        assert "tripwire" not in text.lower(), f"{path} references the tripwire"
        assert "check_evaluator_identity" not in text, f"{path} references check_evaluator_identity"

    _patch_alerts(monkeypatch)  # defense-in-depth: never a real ntfy.sh call here

    # Silent run: no real eval rows at all -> tripwire is a no-op.
    journal_silent = _journal()
    orch_silent = Orchestrator(settings=make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna"), journal=journal_silent)
    summary_silent = orch_silent.run_scan_once()

    # Firing run: identical settings/scan, but a mismatched real reference
    # row is seeded first so the tripwire actually fires this scan.
    journal_firing = _journal()
    _seed_real_eval(journal_firing, model="gpt-5.4-mini", prompt_version="v1")
    orch_firing = Orchestrator(settings=make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna"), journal=journal_firing)
    summary_firing = orch_firing.run_scan_once()

    assert len(_tripwire_events(journal_firing)) >= 1  # sanity: it really fired
    assert _tripwire_events(journal_silent) == []      # sanity: it really stayed silent

    fields = [f.name for f in summary_silent.__dataclass_fields__.values()]
    for field in fields:
        if field == "scan_id" or field.endswith("_id"):
            continue  # ids are randomly minted per run, not a decision-surface signal
        assert getattr(summary_silent, field) == getattr(summary_firing, field), (
            f"ScanSummary.{field} differs between tripwire-silent and tripwire-firing runs"
        )

    journal_silent.close()
    journal_firing.close()


# =================================================================
# 2026-07-28 audit-fixup tests -- two independent Opus audits, both
# REQUEST CHANGES on the same convergent root cause (dedupe honesty).
# =================================================================

# ------------------------------------------------------------- MUST FIX 1a
def test_fix1a_reapply_after_reference_advances_fires_again(monkeypatch):
    """A HIGH-1 / B H1, manifestation 1 -- 'careful operator' re-apply.

    Day 1: operator trials a new model on a day that produces ZERO real
    eval rows (mock trial / ai_degraded / empty shortlist / cost cap) --
    the tripwire still fires against the last real (pre-trial) reference,
    pages once, operator reverts. Weeks later, after real evaluations
    keep landing under the OLD identity and a proper A/B + decision-log
    ships the SAME new model for real, the transition message is
    byte-identical to the first page -- but the reference row has moved
    on. The ORIGINAL (message-only) dedupe would suppress this SECOND,
    real transition forever. The corrected dedupe (message AND
    reference_eval_id AND alert_sent) must not."""
    journal = _journal()
    sent = _patch_alerts(monkeypatch)
    settings_trial = make_settings(OPENAI_PRIMARY_MODEL="gpt-6.0-x", OPENAI_PROMPT_VERSION="v1")
    settings_luna = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna", OPENAI_PROMPT_VERSION="v1")

    # Day 1: last real row is luna; operator has switched settings to X for
    # a mock/ai_degraded trial that lands no new real rows. Fires once.
    old_ref_eval_id = _seed_real_eval(journal, model="gpt-5.6-luna", prompt_version="v1")
    r1 = check_evaluator_identity(journal, settings_trial)
    assert r1["fired"] == ["openai_primary_model"]
    assert len(sent) == 1

    # Operator reverts to luna -- settings match the still-unmoved
    # reference again, so the revert itself is silent (accepted quiet case).
    r_revert = check_evaluator_identity(journal, settings_luna)
    assert r_revert["fired"] == []
    assert len(sent) == 1

    # Weeks pass; real luna evaluations keep landing normally (33 of them,
    # per the audit's own reproduction), advancing the reference row.
    for _ in range(33):
        new_ref_eval_id = _seed_real_eval(journal, model="gpt-5.6-luna", prompt_version="v1")
    assert new_ref_eval_id != old_ref_eval_id

    # Operator now ships X for real, after a proper A/B + decision log.
    r2 = check_evaluator_identity(journal, settings_trial)
    assert r2["fired"] == ["openai_primary_model"], (
        "re-apply after the reference advanced must fire again, not be "
        "suppressed by a stale message-only dedupe match"
    )
    assert len(sent) == 2  # the real page landed
    events = [e for e in _tripwire_events(journal) if e["message"].startswith("TRIP-1 openai_primary_model:")]
    assert len(events) == 2
    assert _detail(events[0])["reference_eval_id"] == old_ref_eval_id
    assert _detail(events[1])["reference_eval_id"] == new_ref_eval_id
    journal.close()


# ------------------------------------------------------------- MUST FIX 1b
def test_fix1b_undelivered_alert_is_not_suppressed_next_scan():
    """B H1, manifestation 2 -- undelivered page burned forever.

    Deliberately does NOT monkeypatch alerts.send_alert: NTFY_TOPIC is
    unset (make_settings' own default), so the REAL alerts.send_alert
    returns False silently, with no network call and no log of its own
    (per alerts.py's own documented contract). The ORIGINAL code
    discarded that return value and journaled the dedupe row
    unconditionally, burning the transition's only page forever. The
    corrected code must retry on the next scan."""
    journal = _journal()
    _seed_real_eval(journal, model="gpt-5.4-mini", prompt_version="v1")
    settings = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna", OPENAI_PROMPT_VERSION="v1")
    assert settings.ntfy_topic == ""  # the real undelivered branch, no monkeypatch

    result1 = check_evaluator_identity(journal, settings)
    assert result1["fired"] == ["openai_primary_model"]
    events = _tripwire_events(journal)
    assert len(events) == 1
    assert _detail(events[0])["alert_sent"] is False  # never confirmed delivered

    # Next scan, still no real network topic configured, still no new real
    # rows landed -- must retry, not silently treat this as handled.
    result2 = check_evaluator_identity(journal, settings)
    assert result2["fired"] == ["openai_primary_model"], (
        "an undelivered page must fire again next scan, never be silently "
        "burned by an unconditional dedupe write"
    )
    assert result2["suppressed"] == []
    events = _tripwire_events(journal)
    assert len(events) == 2
    assert all(_detail(e)["alert_sent"] is False for e in events)
    journal.close()


# ------------------------------------------------------------- MUST FIX 1 (a)
def test_fix1_delivered_alert_still_suppresses_on_true_repeat(monkeypatch):
    """Explicit pin for corrected-dedupe ordering (a), which both audits
    asked to be verified directly: same message, same reference_eval_id,
    AND a CONFIRMED-delivered alert -- must still suppress. (test 9 above
    already exercises this path end-to-end; this test additionally reads
    the stored detail_json directly to prove all three fields line up,
    rather than only observing the black-box suppressed/fired outcome.)"""
    journal = _journal()
    sent = _patch_alerts(monkeypatch, returns=True)
    ref_eval_id = _seed_real_eval(journal, model="gpt-5.4-mini", prompt_version="v1")
    settings = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna", OPENAI_PROMPT_VERSION="v1")

    r1 = check_evaluator_identity(journal, settings)
    assert r1["fired"] == ["openai_primary_model"]
    row = _tripwire_events(journal)[0]
    detail = _detail(row)
    assert detail["reference_eval_id"] == ref_eval_id
    assert detail["alert_sent"] is True

    r2 = check_evaluator_identity(journal, settings)  # nothing changed at all
    assert r2["suppressed"] == ["openai_primary_model"]
    assert r2["fired"] == []
    assert len(_tripwire_events(journal)) == 1
    assert len(sent) == 1
    journal.close()


# ------------------------------------------------------------- MUST FIX 1c
def test_fix1c_mid_check_exception_orphans_only_the_failed_axis(monkeypatch):
    """B M3 -- a mid-loop exception must not silently mark an axis as
    'handled'. Both axes mismatch; log_system_event is patched to raise on
    its SECOND call this check (i.e. axis 1's -- openai_primary_model,
    per _AXES order -- event commits fine, axis 2's -- openai_prompt_
    version -- insert raises). The outer fail-open wrapper catches it and
    journals its own 'TRIP-1 check failed' ERROR (itself a THIRD
    log_system_event call, which must not re-trigger the injected raise).
    Axis 1's row must persist with alert_sent still False (the send was
    never reached), so the NEXT scan retries axis 1 -- and axis 2, which
    never got an event row at all, is simply a fresh mismatch."""
    journal = _journal()
    sent = _patch_alerts(monkeypatch)
    _seed_real_eval(journal, model="gpt-5.4-mini", prompt_version="v2")
    settings = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna", OPENAI_PROMPT_VERSION="v3")

    orig_log = journal.log_system_event
    calls = []

    def _flaky_log(*a, **k):
        calls.append(a)
        if len(calls) == 2:
            raise RuntimeError("simulated failure inserting axis 2's event")
        return orig_log(*a, **k)

    monkeypatch.setattr(journal, "log_system_event", _flaky_log)

    result = check_evaluator_identity(journal, settings)  # must not raise
    assert result is not None

    axis1_events = [e for e in _tripwire_events(journal) if e["message"].startswith("TRIP-1 openai_primary_model:")]
    axis2_events = [e for e in _tripwire_events(journal) if e["message"].startswith("TRIP-1 openai_prompt_version:")]
    check_failed_events = [e for e in _tripwire_events(journal) if e["message"].startswith("TRIP-1 check failed:")]
    assert len(axis1_events) == 1
    assert _detail(axis1_events[0])["alert_sent"] is False  # send never reached
    assert axis2_events == []  # never even inserted -- the raise happened here
    assert len(check_failed_events) == 1  # fail-open wrapper's own ERROR event

    assert sent == []  # the raise happened before the shared alert send was ever reached

    # Next scan (no monkeypatch, no new real rows): axis 1 retries (its
    # dedupe row has alert_sent=False) and axis 2 fires fresh.
    result2 = check_evaluator_identity(journal, settings)
    assert set(result2["fired"]) == {"openai_primary_model", "openai_prompt_version"}
    assert len(sent) == 1  # ONE alert covering both axes this time
    journal.close()


# -------------------------------------------------------------- MUST FIX 2
def test_fix2_error_severity_surfaces_in_daily_digest(monkeypatch):
    """MUST FIX 2 -- scheduler/digest.py:141 only surfaces system_events
    at severity IN ('error', 'critical'); the mismatch event must be ERROR
    so an undelivered ntfy push is not the tripwire's only channel.
    Verified before making this change (see alphaos/tripwire.py's own
    module docstring) that scheduler/cadence.py::is_fused -- the only
    self-halt mechanism in this codebase -- counts consecutive
    job_runs.status=='failed' rows and never reads system_events at all,
    so this cannot trip any fuse or halt anything."""
    journal = _journal()
    _patch_alerts(monkeypatch)
    _seed_real_eval(journal, model="gpt-5.4-mini", prompt_version="v1")
    settings = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna", OPENAI_PROMPT_VERSION="v1")

    result = check_evaluator_identity(journal, settings)
    assert result["fired"] == ["openai_primary_model"]

    digest = build_daily_digest(journal, settings, KillSwitch())
    digest_events = digest["errors_and_failures"]["system_events"]
    tripwire_rows = [e for e in digest_events if e["category"] == "tripwire"]
    assert len(tripwire_rows) == 1
    assert tripwire_rows[0]["message"] == "TRIP-1 openai_primary_model: 'gpt-5.4-mini' -> 'gpt-5.6-luna'"
    journal.close()


# -------------------------------------------------------------- Fix 5 (A LOW-1 / B N1)
def test_fix5_exact_prefix_dedupe_does_not_cross_match_a_similar_axis_name(monkeypatch):
    """A LOW-1 / B N1 -- the dedupe lookup uses substr-equality (exact
    prefix), not a SQL LIKE pattern, so an axis name containing
    underscores can never wildcard-cross-match a different, similarly-
    shaped axis name. Proven directly against the internal helper: a
    tripwire event for a hand-crafted, deliberately-similar axis name must
    NOT be picked up as the latest event for 'openai_primary_model'."""
    journal = _journal()
    # A message that a naive `LIKE 'TRIP-1 openai_primary_model:%'` pattern
    # would NOT actually cross with (since it's a different literal string),
    # but which demonstrates the helper's exactness: an axis name that
    # differs from the real one only by substituting a single underscore
    # for another character -- the exact class of string LIKE's `_`
    # wildcard is defined to match.
    journal.log_system_event(
        tripwire_module.Severity.ERROR, "tripwire",
        "TRIP-1 openaiXprimary_model: 'a' -> 'b'",  # NOT 'openai_primary_model'
        {"axis": "openaiXprimary_model", "old": "a", "new": "b", "reference_eval_id": "eval_x",
         "alert_sent": True},
    )

    last = tripwire_module._latest_tripwire_event_for_axis(journal, "openai_primary_model")
    assert last is None, "the exact-prefix lookup must not cross-match a similarly-shaped axis name"
    journal.close()


# -------------------------------------------------------------- Fix 6 (A LOW-2 / B N2)
def test_fix6_behavioral_placement_real_scan_fires_when_identity_changed(monkeypatch):
    """A LOW-2 / B N2 -- behavioral companion to the AST placement test
    (test_tripwire_runs_before_first_openai_evaluations_insert_ast above).
    That test only proves SOURCE order; it would stay green even if the
    call were wrapped in a never-taken conditional, silently defeating the
    tripwire at runtime. This test seeds a ledger under an OLD identity,
    runs a REAL run_scan_once that (via the mock evaluator) will itself
    insert fresh openai_evaluations rows under the CURRENT (new)
    settings identity, and asserts the tripwire still fired -- which is
    only possible if it read the reference BEFORE this scan's own inserts
    could move it, i.e. the call genuinely executes before the insert at
    runtime, not merely earlier in source text."""
    sent = _patch_alerts(monkeypatch)
    journal = _journal()
    _seed_real_eval(journal, model="gpt-5.4-mini", prompt_version="v1")
    settings = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna", OPENAI_PROMPT_VERSION="v1")
    orch = Orchestrator(settings=settings, journal=journal)

    orch.run_scan_once()

    events = [e for e in _tripwire_events(journal) if e["message"].startswith("TRIP-1 openai_primary_model:")]
    assert len(events) == 1
    assert events[0]["message"] == "TRIP-1 openai_primary_model: 'gpt-5.4-mini' -> 'gpt-5.6-luna'"
    assert len(sent) == 1
    journal.close()
