"""TRIP-1 -- Evaluator identity tripwire
(docs/roadmap/alphaos-evaluator-replay-and-coherence-specs.md, TRIP-1
section, refined 2026-07-27; dedupe + severity corrected 2026-07-28
audit-fixup, see the spec's own dated Design 4 correction).

Makes the NEXT silent evaluator-identity change loud: at scan start,
compares the two axes that determine "which model judges, with what
prompt" -- ``settings.openai_primary_model`` / ``settings.openai_prompt_
version`` -- against the identity actually stamped on the most recent
REAL ``openai_evaluations`` row (``model`` / ``prompt_template_
version``). On a mismatch it journals one ERROR ``system_events`` row
per mismatched axis (category ``"tripwire"``) and sends exactly one
alert per scan covering every axis that fired this scan. Never blocks,
never gates, never aborts a scan (Non-goals, frozen).

Severity is ERROR, not WARNING (2026-07-28 audit-fixup, MUST FIX 2):
the daily digest (``scheduler/digest.py``) only surfaces ``system_events``
rows at severity ERROR/CRITICAL, so a WARNING-only mismatch event was
invisible everywhere except a direct ntfy push -- a single point of
failure. Verified before raising it: the scheduler's only self-halt
mechanism, ``scheduler/cadence.py::is_fused``, counts consecutive
``job_runs.status == 'failed'`` rows -- it never reads ``system_events``
at all, so this can never trip a fuse or halt anything. The only other
severity-filtered consumer, ``reports/daily_recon.py``, filters on
``category = 'execution'`` and never sees a ``category = 'tripwire'``
row. A legitimate identity change pages loudly; it never halts the
system.

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
reference is expected and normal. **Corrected dedupe condition
(2026-07-28 audit-fixup, MUST FIX 1)**: the ORIGINAL dedupe (message
string alone) had two real gaps found by independent adversarial
review, both reproduced end-to-end:

1. A message string can recur verbatim across two genuinely different
   REFERENCE ROWS -- e.g. an operator trials a new model on a day that
   happens to produce zero real eval rows (mock trial / ai_degraded /
   empty shortlist / cost cap), gets paged, reverts; weeks later,
   after real luna evaluations have kept landing and a proper A/B +
   decision-log ships the same model for real, the transition message
   is byte-identical to the first page even though the underlying
   reference eval row has moved on -- the ORIGINAL dedupe would have
   suppressed this SECOND, real transition forever.
2. ``alerts.send_alert`` returns a bool and never raises; it returns
   False silently (no log at all) when ``NTFY_TOPIC`` is unset, and
   False + a WARNING log on any network/HTTP failure. The original
   code discarded that return value entirely and journaled the dedupe
   row unconditionally -- one ntfy blip on the single scan a
   transition occurs would burn the transition's only page forever,
   with zero record it was ever undelivered.

Fire iff no prior tripwire event for this axis has ALL THREE of: (a)
the identical candidate message, (b) the identical
``reference_eval_id``, and (c) ``alert_sent is True`` in its
``detail_json``. Any one of those differing means fire again. This
also fixes a third failure mode (B M3): per-axis events are journaled
INSIDE the mismatch loop, before the single shared alert send: if an
exception unwinds between inserting axis 1's event and reaching the
send (e.g. while building axis 2), axis 1's row is left with its
initial ``alert_sent: False`` (the field is only ever flipped to True
in the follow-up UPDATE issued after a confirmed-successful send,
mirroring ``cards/demotion.py``'s own insert-then-update-on-success
pattern) -- so the next scan retries axis 1 rather than treating an
orphaned, never-alerted event as done.

No acknowledgment mechanism of any kind (Design 5, decided): an ack
channel a session can operate is a tripwire a session can disarm. TRIP-1
fires identically on deliberate and accidental changes, capped at
exactly one CONFIRMED-DELIVERED page per distinct transition -- the
alert text itself does the deliberate-vs-accidental disambiguation.

Fail-open (Design 6): the ENTIRE check body -- reference read, dedupe
reads, event inserts, alert send -- is wrapped in ONE try/except. Any
exception is journaled as an ERROR (best-effort; a failure while
journaling the failure is also swallowed) and never propagates. A
tripwire that aborts a scan is worse than no tripwire.
"""

from __future__ import annotations

import json

from alphaos.constants import Severity
from alphaos.util import alerts

_REFERENCE_SQL = (
    "SELECT eval_id, model, prompt_template_version, created_at_utc "
    "FROM openai_evaluations WHERE is_mock = 0 ORDER BY id DESC LIMIT 1"
)

# (axis name == settings field name, reference-row column it is stamped as)
_AXES = (
    ("openai_primary_model", "model"),
    ("openai_prompt_version", "prompt_template_version"),
)

_ALERT_TITLE = "AlphaOS TRIP-1: evaluator identity changed"


def _parse_detail(value) -> dict:
    """Best-effort JSON decode of a ``detail_json`` column value -- same
    tolerant shape as ``alphaos/reports/journal_feed.py``'s own
    ``_parse_detail`` (not imported directly: this is a measurement
    module and should not depend on a report module). Never raises;
    anything unparseable/non-dict decodes to ``{}``."""
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _latest_tripwire_event_for_axis(journal, axis: str) -> "dict | None":
    """The latest ``system_events`` row whose message is for THIS axis
    specifically. Exact-prefix match via ``substr(message, 1, N) = ?``,
    deliberately NOT a SQL ``LIKE 'TRIP-1 {axis}:%'`` pattern
    (2026-07-28 audit-fixup, A LOW-1 / B N1): ``_`` is a single-char
    LIKE wildcard and both current axis names contain underscores, so a
    naive LIKE pattern is fire-biased today (harmless -- an extra page,
    never a missed one -- since neither current axis name can
    off-by-one collide with the other) but would silently mismatch the
    day a third, similarly-named axis joins per the spec's own
    maintenance-trigger rule. ``substr`` equality has no such wildcard
    semantics at all."""
    prefix = f"TRIP-1 {axis}: "
    return journal.one(
        "SELECT event_id, message, detail_json FROM system_events "
        "WHERE category = 'tripwire' AND substr(message, 1, ?) = ? "
        "ORDER BY id DESC LIMIT 1",
        (len(prefix), prefix),
    )


def check_evaluator_identity(journal, settings) -> dict:
    """Compare the two evaluator-identity axes against the last real
    ``openai_evaluations`` row; journal + alert on mismatch, deduped per
    distinct transition AND per confirmed-delivered page (see module
    docstring's "Corrected dedupe condition"). Returns ``{"checked",
    "fired", "suppressed", "reference_eval_id"}`` for tests only -- the
    production caller (``Orchestrator.run_scan_once``) discards this.
    Never raises."""
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

        fired_event_ids: dict = {}  # axis -> event_id, for the post-send UPDATE
        fired_lines = []
        for axis, (old, new) in mismatched.items():
            candidate_message = f"TRIP-1 {axis}: '{old}' -> '{new}'"
            last = _latest_tripwire_event_for_axis(journal, axis)
            if last is not None:
                last_detail = _parse_detail(last.get("detail_json"))
                if (
                    last["message"] == candidate_message
                    and last_detail.get("reference_eval_id") == ref["eval_id"]
                    and last_detail.get("alert_sent") is True
                ):
                    # Same transition, same reference row, AND that page was
                    # actually delivered -- genuinely already handled.
                    result["suppressed"].append(axis)
                    continue
            event_id = journal.log_system_event(
                Severity.ERROR, "tripwire", candidate_message,
                {
                    "axis": axis, "old": old, "new": new,
                    "reference_eval_id": ref["eval_id"],
                    "reference_created_at_utc": ref["created_at_utc"],
                    "alert_sent": False,  # flipped to True below iff the send confirms
                },
            )
            result["fired"].append(axis)
            fired_event_ids[axis] = event_id
            fired_lines.append(
                f"{axis}: {old} -> {new}   (last real eval under old identity: "
                f"{ref['eval_id']} @ {ref['created_at_utc']})"
            )

        if result["fired"]:
            # Unchanged axis uses its current (settings) value on both
            # arms of the --arms instruction; a fired axis uses its real
            # old/new pair. Event inserts above happen strictly BEFORE
            # this alert send, so a failed/undelivered send never loses
            # the audit row -- only the alert_sent flag (below) reflects it.
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
            # cards/demotion.py's own pattern: insert first (done above),
            # capture send_alert's real return value, THEN update the
            # dedupe-relevant flag only on confirmed delivery. Never
            # reorder to send-before-insert (Design 3 audit durability).
            sent = alerts.send_alert(settings, _ALERT_TITLE, body, priority="high", journal=journal)
            if sent:
                for axis, event_id in fired_event_ids.items():
                    old, new = mismatched[axis]
                    detail = {
                        "axis": axis, "old": old, "new": new,
                        "reference_eval_id": ref["eval_id"],
                        "reference_created_at_utc": ref["created_at_utc"],
                        "alert_sent": True,
                    }
                    journal.conn.execute(
                        "UPDATE system_events SET detail_json = ? WHERE event_id = ?",
                        (json.dumps(detail, default=str), event_id),
                    )
                journal.conn.commit()
        return result
    except Exception as exc:  # fail-open (Design 6): never abort the scan
        try:
            journal.log_system_event(
                Severity.ERROR, "tripwire", f"TRIP-1 check failed: {exc} -- scan continues",
            )
        except Exception:
            pass
        return result
