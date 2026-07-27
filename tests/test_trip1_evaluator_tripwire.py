"""TRIP-1: Evaluator identity tripwire
(docs/roadmap/alphaos-evaluator-replay-and-coherence-specs.md, TRIP-1
section, refined 2026-07-27).

15 named tests below map 1:1 onto the spec's own "Tests (hermetic,
S.H.1 discipline)" list:

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

All offline, in-memory, hermetic. No real money, no network -- every
test that wants to observe an alert monkeypatches
``alphaos.tripwire.alerts.send_alert`` directly (the established
convention -- see tests/test_pr13_card_scoreboard.py,
tests/test_canary.py), so no test needs a real NTFY_TOPIC.
"""

from __future__ import annotations

from alphaos import tripwire as tripwire_module
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
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


def _patch_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(
        tripwire_module.alerts, "send_alert",
        lambda settings, title, message, priority="default", journal=None: (
            sent.append({"title": title, "message": message, "priority": priority}) or True
        ),
    )
    return sent


def _tripwire_events(journal):
    return journal.query(
        "SELECT * FROM system_events WHERE category = 'tripwire' ORDER BY id ASC"
    )


# ---------------------------------------------------------------- Spec test 1
def test_model_axis_mismatch_fires_one_event_and_one_alert(monkeypatch):
    journal = _journal()
    sent = _patch_alerts(monkeypatch)
    _seed_real_eval(journal, model="gpt-5.4-mini", prompt_version="v1")
    settings = make_settings(OPENAI_PRIMARY_MODEL="gpt-5.6-luna", OPENAI_PROMPT_VERSION="v1")

    result = check_evaluator_identity(journal, settings)

    events = _tripwire_events(journal)
    assert len(events) == 1
    assert events[0]["severity"] == "warning"
    assert events[0]["category"] == "tripwire"
    assert events[0]["message"] == "TRIP-1 openai_primary_model: 'gpt-5.4-mini' -> 'gpt-5.6-luna'"
    assert result["fired"] == ["openai_primary_model"]

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
    messages = {e["message"] for e in events}
    assert messages == {
        "TRIP-1 openai_primary_model: 'gpt-5.4-mini' -> 'gpt-5.6-luna'",
        "TRIP-1 openai_prompt_version: 'v2' -> 'v3'",
    }
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

    # ai_degraded / empty shortlist / mock day: NO new real row lands
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
    # is now the B->A one from r2, so it is not a dedupe hit).
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
            raise sqlite3_error()
        return orig_one(sql, params)

    def sqlite3_error():
        return RuntimeError("simulated reference-read failure")

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
    warnings = journal.query(
        "SELECT * FROM system_events WHERE category = 'tripwire' AND severity = 'warning'"
    )
    # The per-axis WARNING event was inserted BEFORE the alert send, so it
    # survives the send raising.
    assert len(warnings) == 1
    assert warnings[0]["message"] == "TRIP-1 openai_primary_model: 'gpt-5.4-mini' -> 'gpt-5.6-luna'"
    journal.close()


# --------------------------------------------------------------- Spec test 14
def test_tripwire_runs_before_first_openai_evaluations_insert_ast():
    """Placement structural test (house AST call-order pattern, same shape
    as tests/test_ab_eval.py's own '_ast' checks): in
    Orchestrator.run_scan_once, check_evaluator_identity must be called
    strictly before the scan's own first openai_evaluations insert -- the
    single production insert site. If it ran after, that insert would move
    the detection reference before it was ever compared (Design 1/4)."""
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
_DECISION_SURFACE_PACKAGES = ("risk", "strategy", "execution", "tqs", "scanner", "ai")
_DECISION_SURFACE_ROOT_FILES = ("approval.py", "safety.py")
_DECISION_SURFACE_EXTRA_FILES = ("hypotheses/proposer.py", "scheduler/shadow_label.py")


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

    (a) STRUCTURAL: no file anywhere under the decision-surface packages
        (risk/, strategy/, execution/, tqs/, scanner/, ai/), nor
        approval.py/safety.py, nor the hypotheses/shadow-label modules
        that read candidate_outcomes from outside those packages, may
        reference 'tripwire' or 'check_evaluator_identity' in any form
        (source-text-based, so it also catches a stray import/comment/
        string, not just a live read).
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
