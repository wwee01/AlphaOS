"""TRIP-1 -- Evaluator identity tripwire
(docs/roadmap/alphaos-evaluator-replay-and-coherence-specs.md, TRIP-1
section, refined 2026-07-27).

Makes the NEXT silent evaluator-identity change loud: at scan start,
compares the two axes that determine "which model judges, with what
prompt" -- ``settings.openai_primary_model`` / ``settings.openai_prompt_
version`` -- against the identity actually stamped on the most recent
REAL ``openai_evaluations`` row (``model`` / ``prompt_template_
version``). On a mismatch it journals one WARNING ``system_events`` row
per mismatched axis (category ``"tripwire"``) and sends exactly one
alert per scan covering every axis that fired this scan. Never blocks,
never gates, never aborts a scan (Non-goals, frozen).

Zero decision surface (house law, same as CANARY/AB-EVAL-1): this
module is measurement-only. Nothing in the scan/eval/gate/risk/
execution path may import or read anything from here -- the one
production caller (``Orchestrator.run_scan_once``, called immediately
after ``self._ensure_startup()``, BEFORE this scan's own first
``openai_evaluations`` insert -- placement is load-bearing, see Design
1) discards the return value entirely. The returned dict exists only so
tests can assert on the outcome of a single call.

Detection reference = the last REAL ``openai_evaluations`` row
(``is_mock = 0``), ordered by ``id DESC`` (AUTOINCREMENT insertion
order -- NEVER ``created_at_utc``, which is ISO text stamped by
``JournalStore.insert`` and can tie within the same instant; Test 11
pins this), NOT ``config_versions``. ``config_versions`` is a
per-process-startup forensic RECORD that accumulates silently and
nothing diffs; the ledger's own per-row-stamped identity is the ground
truth of "what the verdicts we actually have were produced under," and
that is exactly what this ticket protects (Design 2 -- do not
re-litigate building on ``config_versions`` instead).

Idempotency is via ``system_events`` dedupe (Design 4), NOT via "the
reference row moves": an ``ai_degraded`` scan, an empty shortlist, or a
mock-mode day can all produce zero real eval rows, so a stale-but-true
reference is expected and normal. The only thing preventing a page every
scan thereafter is the dedupe check against the latest tripwire event
for that axis -- fire iff no such event exists yet or its message
differs from the candidate message.

No acknowledgment mechanism of any kind (Design 5, decided): an ack
channel a session can operate is a tripwire a session can disarm. TRIP-1
fires identically on deliberate and accidental changes, capped at
exactly one page per distinct transition -- the alert text itself does
the deliberate-vs-accidental disambiguation.

Fail-open (Design 6): the ENTIRE check body -- reference read, dedupe
reads, event inserts, alert send -- is wrapped in ONE try/except. Any
exception is journaled as an ERROR (best-effort; a failure while
journaling the failure is also swallowed) and never propagates. A
tripwire that aborts a scan is worse than no tripwire.
"""

from __future__ import annotations

from alphaos.constants import Severity
from alphaos.util import alerts

_REFERENCE_SQL = (
    "SELECT eval_id, model, prompt_template_version, created_at_utc "
    "FROM openai_evaluations WHERE is_mock = 0 ORDER BY id DESC LIMIT 1"
)

_DEDUPE_SQL = (
    "SELECT message FROM system_events WHERE category = 'tripwire' "
    "AND message LIKE ? ORDER BY id DESC LIMIT 1"
)

# (axis name == settings field name, reference-row column it is stamped as)
_AXES = (
    ("openai_primary_model", "model"),
    ("openai_prompt_version", "prompt_template_version"),
)

_ALERT_TITLE = "AlphaOS TRIP-1: evaluator identity changed"


def check_evaluator_identity(journal, settings) -> dict:
    """Compare the two evaluator-identity axes against the last real
    ``openai_evaluations`` row; journal + alert on mismatch, deduped per
    distinct transition (never twice for the same old->new pair on the
    same axis). Returns ``{"checked", "fired", "suppressed",
    "reference_eval_id"}`` for tests only -- the production caller
    (``Orchestrator.run_scan_once``) discards this. Never raises."""
    result: dict = {"checked": [], "fired": [], "suppressed": [], "reference_eval_id": None}
    try:
        ref = journal.one(_REFERENCE_SQL)
        if ref is None:
            # Empty ledger / fresh install / pure-mock install: nothing to
            # protect yet -- same stance as CANARY's "no baseline pinned".
            return result
        result["reference_eval_id"] = ref["eval_id"]

        mismatched: dict = {}  # axis -> (old, new)
        for axis, ref_col in _AXES:
            ref_val = ref[ref_col]
            if ref_val is None:
                # NULL on the reference row (legacy/hand-tampered row):
                # no-op for THIS axis only -- never a crash, never an
                # alert on an unknowable old value.
                continue
            result["checked"].append(axis)
            new_val = getattr(settings, axis)
            if ref_val != new_val:
                mismatched[axis] = (ref_val, new_val)

        fired_lines = []
        for axis, (old, new) in mismatched.items():
            candidate_message = f"TRIP-1 {axis}: '{old}' -> '{new}'"
            last = journal.one(_DEDUPE_SQL, (f"TRIP-1 {axis}:%",))
            if last is not None and last["message"] == candidate_message:
                # Ordering B: no real evals landed since the last fire, so
                # settings still mismatch the (stale-but-true) reference --
                # but this exact transition already has its one page.
                result["suppressed"].append(axis)
                continue
            journal.log_system_event(
                Severity.WARNING, "tripwire", candidate_message,
                {
                    "axis": axis, "old": old, "new": new,
                    "reference_eval_id": ref["eval_id"],
                    "reference_created_at_utc": ref["created_at_utc"],
                },
            )
            result["fired"].append(axis)
            fired_lines.append(
                f"{axis}: {old} -> {new}   (last real eval under old identity: "
                f"{ref['eval_id']} @ {ref['created_at_utc']})"
            )

        if result["fired"]:
            # Unchanged axis uses its current (settings) value on both
            # arms of the --arms instruction; a fired axis uses its real
            # old/new pair. Event inserts above happen strictly BEFORE
            # this alert send, so a failed send never loses the audit row.
            model_old, model_new = mismatched.get(
                "openai_primary_model",
                (settings.openai_primary_model, settings.openai_primary_model),
            )
            version_old, version_new = mismatched.get(
                "openai_prompt_version",
                (settings.openai_prompt_version, settings.openai_prompt_version),
            )
            body = "\n".join(
                fired_lines
                + [
                    "A model change is a strategy change (S9 ruling, 2026-07-26). "
                    "Before trusting any new verdicts:",
                    "1) replay the frozen corpus through old and new identities:",
                    f"   alphaos ab_eval_run --arms {model_old}:{version_old} "
                    f"{model_new}:{version_new}",
                    "2) log the keep/revert decision in the S9 decision log.",
                    "Deliberate change? This page is the receipt -- confirm the "
                    "decision row exists.",
                    "Not deliberate? Revert .env, restart, and investigate what "
                    "changed it.",
                ]
            )
            alerts.send_alert(settings, _ALERT_TITLE, body, priority="high", journal=journal)
        return result
    except Exception as exc:  # fail-open (Design 6): never abort the scan
        try:
            journal.log_system_event(
                Severity.ERROR, "tripwire", f"TRIP-1 check failed: {exc} -- scan continues",
            )
        except Exception:
            pass
        return result
