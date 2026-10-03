"""Explicit preview-first shadow recovery. No provider, broker, startup or schema work."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from alphaos import shadow_marker as marker
from alphaos.journal.journal_store import JournalStore
from alphaos.safety import KillSwitch, ShadowLabelSuspendSwitch
from alphaos.scheduler import cost_guard, shadow_label
from alphaos.scheduler.ownership import current_ownership, worker_ownership
from alphaos.util import timeutils

VERSION = "shadow_recovery_v1"
PREVIEW_TTL_SECONDS = 300
CATEGORY = "shadow_recovery"


class RecoveryRefused(RuntimeError):
    pass


def _bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str).encode()


def digest(value) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _now():
    value = timeutils.now_utc()
    if value.tzinfo is None or value.utcoffset() is None:
        raise RecoveryRefused("aware recovery clock required")
    return value


def _aware(value):
    if not isinstance(value, str) or "T" not in value:
        raise RecoveryRefused("explicitly aware recovery timestamp required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecoveryRefused("invalid recovery timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RecoveryRefused("explicitly aware recovery timestamp required")
    return parsed


def _ledger_identity(journal) -> dict:
    captured = getattr(journal, "_shadow_recovery_ledger_identity", None)
    if not isinstance(captured, dict):
        raise RecoveryRefused("recovery requires an identity-bound existing_journal connection")
    path = Path(journal.db_path).resolve(strict=True)
    info = path.stat()
    current = {"path": str(path), "device": info.st_dev, "inode": info.st_ino}
    if current != captured:
        raise RecoveryRefused("opened recovery ledger identity changed")
    return dict(captured)


def _config(settings) -> str:
    # Only a digest is persisted; raw configuration/credentials never leave this function.
    return digest(dataclasses.asdict(settings))


def _source_identity() -> dict:
    root = Path(__file__).resolve().parents[1]
    names = ("safety.py", "shadow_marker.py", "scheduler/shadow_recovery.py",
             "canary/recovery_evidence.py", "canary/run.py", "canary/corpus.py",
             "ai/label_validation.py", "ai/prompt_templates.py", "config/settings.py", "util/timeutils.py",
             "scheduler/ownership.py", "scheduler/shadow_label.py", "scheduler/cost_guard.py")
    return {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}


def _canary(journal, settings, now, corpus_dir):
    from alphaos.canary.recovery_evidence import validate_canary_recovery
    return validate_canary_recovery(journal, settings, as_of_utc=now, corpus_dir=corpus_dir)


def recovery_checks(journal, settings, *, kill_switch=None, corpus_dir=None) -> dict:
    """Caller owns the read/write transaction; all checks are read-only."""
    _ledger_identity(journal)
    now = _now()
    kill = kill_switch or KillSwitch()
    canary = _canary(journal, settings, now, corpus_dir)
    coverage_ok, coverage_reason = shadow_label.check_feed_coverage_gate(journal, settings)
    auto, auto_reason = shadow_label.check_auto_suspend(journal, settings)
    planned = settings.shadow_label_top_k + max(1, round(settings.shadow_explore_fraction * settings.shadow_label_top_k))
    budget_ok, budget_reason = shadow_label.check_shadow_budget(settings, journal, planned)
    daily = shadow_label._daily_feed_coverage_map(journal, shadow_label.FEED_COVERAGE_TRAILING_DAYS)
    used = {"shadow_30d": shadow_label.shadow_calls_in_last_30_days(journal),
            "shadow_today": shadow_label.shadow_calls_today(journal),
            "shared_30d": cost_guard.calls_in_last_30_days(journal)}
    canary_rows = journal.query(
        "SELECT * FROM canary_runs WHERE confirmation_of IS NULL ORDER BY id")
    coverage_rows = journal.query(
        "SELECT id,market_date,symbol,freshness_status FROM universe_days WHERE market_date>=? ORDER BY id",
        ((timeutils.market_date()-timedelta(days=shadow_label.FEED_COVERAGE_TRAILING_DAYS)).isoformat(),))
    enabled = bool(settings.shadow_labelling_enabled and settings.shadow_tier_enabled)
    killed = kill.is_engaged()
    inputs = {"version": VERSION, "config_sha256": _config(settings), "selected_source_sha256": _source_identity(),
              "canary_sha256": canary["evidence_sha256"], "coverage_rows_sha256": digest(coverage_rows),
              "auto_suspend_rows_sha256": digest(canary_rows), "market_date": timeutils.market_date().isoformat(),
              "daily_coverage": daily, "budget_counts": used, "planned_max_calls": planned,
              "enabled": enabled, "kill_engaged": killed}
    return {"version": VERSION, "observed_at_utc": now.isoformat(),
            "ok": bool(enabled and not killed and canary["ok"] and coverage_ok and not auto and budget_ok),
            "canary": {k: canary.get(k) for k in ("ok", "reasons", "run_id", "evidence_sha256", "limitations")},
            "coverage_ok": coverage_ok, "coverage_reason": coverage_reason,
            "auto_suspend_triggered": auto, "auto_suspend_reason": auto_reason,
            "budget_ok": budget_ok, "budget_reason": budget_reason, "inputs": inputs,
            "evidence_sha256": digest(inputs)}


@contextmanager
def existing_journal(path, *, writable=False):
    """Open an existing ledger without JournalStore initialization/migration."""
    absolute = Path(path).resolve(strict=True)
    before = absolute.stat()
    captured = {"path": str(absolute), "device": before.st_dev, "inode": before.st_ino}
    journal = JournalStore.__new__(JournalStore)
    journal.db_path, journal.jsonl_mirror, journal.read_only = str(absolute), False, not writable
    journal._columns = {}
    journal.conn = sqlite3.connect(absolute.as_uri()+("?mode=rw" if writable else "?mode=ro"),
                                   uri=True, timeout=1.0)
    journal.conn.row_factory = sqlite3.Row
    try:
        journal.conn.execute("PRAGMA synchronous=FULL" if writable else "PRAGMA query_only=ON")
        setattr(journal, "_shadow_recovery_ledger_identity", captured)
        _ledger_identity(journal)  # Refuse replacement across connection open.
        yield journal
    finally:
        journal.close()


def _state(path) -> dict:
    original, original_raw = marker.fingerprint(path)
    if original is not None:
        try:
            text = (original_raw or b"").decode("utf-8")
            if text.lstrip().startswith("{"):
                value = json.loads(text)
                if (set(value) != {"version", "generation", "reason"}
                        or value["version"] != "shadow_suspend_marker_v1"
                        or not re.fullmatch(r"[a-f0-9]{32}", value.get("generation", ""))
                        or not isinstance(value.get("reason"), str)):
                    raise ValueError("marker shape")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise RecoveryRefused("suspension marker is malformed") from exc
    pending, raw = marker.fingerprint(marker.pending_path(path))
    pending_value = None
    if pending is not None:
        try:
            pending_value = json.loads((raw or b"").decode())
        except (ValueError, UnicodeError) as exc:
            raise RecoveryRefused("pending recovery is malformed") from exc
        if (not isinstance(pending_value, dict) or pending_value.get("version") != VERSION
                or set(pending_value) != {"version", "operation_id", "prepared_event_id", "prepared_sha256"}
                or not re.fullmatch(r"[a-f0-9]{32}", str(pending_value.get("operation_id")))
                or not re.fullmatch(r"[a-f0-9]{64}", str(pending_value.get("prepared_sha256")))
                or not re.fullmatch(r"shadow_recovery_[a-f0-9]{32}", str(pending_value.get("prepared_event_id")))):
            raise RecoveryRefused("pending recovery is unrecognized")
    return {"path": str(marker.canonical(path)), "marker": original,
            "pending": pending, "pending_value": pending_value}


def _receipt(journal, pending) -> dict:
    rows = journal.query("SELECT * FROM system_events WHERE event_id=?", (pending["prepared_event_id"],))
    if len(rows) != 1:
        raise RecoveryRefused("prepared recovery audit missing")
    row = rows[0]
    try:
        value = json.loads(row["detail_json"])
    except (TypeError, ValueError) as exc:
        raise RecoveryRefused("prepared recovery audit malformed") from exc
    if (not isinstance(value, dict)
            or set(value) != {"version", "phase", "operation_id", "ledger", "marker", "config_sha256",
                             "evidence_sha256", "preview_sha256", "note", "observed_at_utc"}
            or row["category"] != CATEGORY or row["message"] != "Shadow recovery prepared."
            or value.get("version") != VERSION or value.get("phase") != "prepared"
            or value.get("operation_id") != pending["operation_id"]
            or digest(value) != pending["prepared_sha256"]):
        raise RecoveryRefused("prepared recovery audit mismatch")
    at = _aware(row["created_at_utc"])
    observed = _aware(value["observed_at_utc"])
    if (at is None or at.tzinfo is None or at > _now() or observed is None
            or observed.tzinfo is None or observed > at):
        raise RecoveryRefused("prepared recovery audit time invalid")
    return value


def _validate_state(journal, state, checks):
    if state["pending"] is not None:
        receipt = _receipt(journal, state["pending_value"])
        if receipt["ledger"] != _ledger_identity(journal):
            raise RecoveryRefused("pending recovery belongs to another ledger")
        if not isinstance(receipt["marker"], dict) or receipt["marker"].get("path") != state["path"]:
            raise RecoveryRefused("pending recovery belongs to another marker")
        if receipt["config_sha256"] != checks["inputs"]["config_sha256"]:
            raise RecoveryRefused("configuration changed since preparation")
        if state["marker"] is not None and state["marker"] != receipt["marker"]:
            raise RecoveryRefused("new suspension during pending recovery")
        return receipt
    if state["marker"] is None:
        raise RecoveryRefused("no suspension to recover")
    return None


def preview(journal, settings, *, suspend_switch=None, kill_switch=None, corpus_dir=None) -> dict:
    """No application writes/locks; SQLite's normal read coordination still applies."""
    if journal.conn.in_transaction:
        raise RecoveryRefused("preview requires its own read snapshot")
    _ledger_identity(journal)
    switch = suspend_switch or ShadowLabelSuspendSwitch()
    state = _state(switch.path)
    journal.conn.execute("BEGIN")
    try:
        checks = recovery_checks(journal, settings, kill_switch=kill_switch, corpus_dir=corpus_dir)
        _validate_state(journal, state, checks)
        result = {"version": VERSION, "observed_at_utc": _now().isoformat(),
                  "ledger": _ledger_identity(journal), "marker_path": str(marker.canonical(switch.path)),
                  "state": state, "checks": checks, "eligible": checks["ok"],
                  "action": "resume" if state["pending"] else "release"}
        if _state(switch.path) != state:
            raise RecoveryRefused("marker changed during preview")
    finally:
        journal.conn.rollback()
    result["preview_sha256"] = digest(result)
    return result


def _audit(journal, phase, value) -> str:
    st = timeutils.stamp()
    event_id = "shadow_recovery_" + uuid4().hex
    journal.conn.execute(
        "INSERT INTO system_events(event_id,severity,category,message,detail_json,created_at_utc,created_at_sgt,created_at_market) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (event_id, "WARNING", CATEGORY, f"Shadow recovery {phase}.", _bytes(value).decode(),
         st.utc, st.local_sgt, st.market_et))
    return event_id


def _create_pending(path, value) -> None:
    marker.require_locked(path)
    marker.write_exclusive(marker.pending_path(path), _bytes(value))


def _check_preview(value, expected_sha256):
    owned = json.loads(_bytes(value))
    stated = owned.pop("preview_sha256", None)
    if not isinstance(expected_sha256, str) or digest(owned) != expected_sha256 or stated != expected_sha256:
        raise RecoveryRefused("preview hash mismatch")
    if owned.get("version") != VERSION:
        raise RecoveryRefused("unsupported preview")
    at = _aware(owned.get("observed_at_utc", ""))
    now = _now()
    if at is None or at.tzinfo is None or not 0 <= (now-at).total_seconds() <= PREVIEW_TTL_SECONDS:
        raise RecoveryRefused("preview expired or invalid")
    return owned


def apply_recovery(journal, settings, preview_value, *, expected_preview_sha256, note,
                   suspend_switch=None, kill_switch=None, corpus_dir=None) -> dict:
    """Never wait for SQLite while holding the marker lock; restore caller timeout."""
    if journal.conn.in_transaction:
        raise RecoveryRefused("apply requires an independent transaction")
    timeout = journal.conn.execute("PRAGMA busy_timeout").fetchone()[0]
    journal.conn.execute("PRAGMA busy_timeout=0")
    try:
        return _apply_recovery(journal, settings, preview_value,
                               expected_preview_sha256=expected_preview_sha256, note=note,
                               suspend_switch=suspend_switch, kill_switch=kill_switch, corpus_dir=corpus_dir)
    finally:
        journal.conn.execute(f"PRAGMA busy_timeout={int(timeout)}")


def _apply_recovery(journal, settings, preview_value, *, expected_preview_sha256, note,
                    suspend_switch=None, kill_switch=None, corpus_dir=None) -> dict:
    """Explicit manual apply. A pending barrier survives every interrupted stage."""
    request = _check_preview(preview_value, expected_preview_sha256)
    if not isinstance(note, str) or not note.strip() or len(note) > 512 or any(ord(c)<32 for c in note):
        raise RecoveryRefused("a bounded single-line operator note is required")
    if journal.read_only or journal.conn.in_transaction:
        raise RecoveryRefused("apply requires an independent writable connection")
    if current_ownership(journal, "research") is not None:
        raise RecoveryRefused("nested research recovery is refused")
    switch = suspend_switch or ShadowLabelSuspendSwitch()
    kill = kill_switch or KillSwitch()
    path = marker.canonical(switch.path)
    if request["ledger"] != _ledger_identity(journal) or request["marker_path"] != str(path):
        raise RecoveryRefused("preview target mismatch")
    with worker_ownership(journal, "research") as acquired:
        if not acquired:
            raise RecoveryRefused("research worker busy")
        with marker.locked(path):
            if _state(path) != request["state"]:
                raise RecoveryRefused("marker changed since preview")
            journal.conn.execute("BEGIN IMMEDIATE")
            try:
                checks = recovery_checks(journal, settings, kill_switch=kill, corpus_dir=corpus_dir)
                if not checks["ok"] or checks["evidence_sha256"] != request["checks"]["evidence_sha256"]:
                    raise RecoveryRefused("current gates or proof differ from preview")
                state = _state(path)
                prepared = _validate_state(journal, state, checks)
                if prepared is None:
                    prepared = {"version": VERSION, "phase": "prepared", "operation_id": uuid4().hex,
                                "ledger": request["ledger"], "marker": state["marker"],
                                "config_sha256": checks["inputs"]["config_sha256"],
                                "evidence_sha256": checks["evidence_sha256"], "preview_sha256": expected_preview_sha256,
                                "note": note.strip(), "observed_at_utc": checks["observed_at_utc"]}
                    prepared_id = _audit(journal, "prepared", prepared)
                    pending_value = {"version": VERSION, "operation_id": prepared["operation_id"],
                                     "prepared_event_id": prepared_id, "prepared_sha256": digest(prepared)}
                else:
                    pending_value = state["pending_value"]
                journal.conn.commit()
            except BaseException:
                journal.conn.rollback()
                raise

            if state["pending"] is None:
                _create_pending(path, pending_value)
            pending_identity, _ = marker.fingerprint(marker.pending_path(path))
            if pending_identity is None:
                raise RecoveryRefused("pending guard missing")
            # Recheck immediately before filesystem mutation and freeze DB writers.
            journal.conn.execute("BEGIN IMMEDIATE")
            try:
                checks = recovery_checks(journal, settings, kill_switch=kill, corpus_dir=corpus_dir)
                state = _state(path)
                if (not checks["ok"] or checks["evidence_sha256"] != request["checks"]["evidence_sha256"]
                        or state["pending"] != pending_identity or state["pending_value"] != pending_value):
                    raise RecoveryRefused("recovery inputs changed before release")
                _validate_state(journal, state, checks)
                if current_ownership(journal, "research") is None:
                    raise RecoveryRefused("research ownership lost")
                if state["marker"] is not None:
                    marker.remove_matching(path, prepared["marker"], marker_path=path)
                completed = {"version": VERSION, "phase": "completed", "operation_id": prepared["operation_id"],
                             "prepared_event_id": pending_value["prepared_event_id"],
                             "prepared_sha256": pending_value["prepared_sha256"],
                             "pending_identity": pending_identity, "ledger": request["ledger"],
                             "config_sha256": checks["inputs"]["config_sha256"],
                             "evidence_sha256": checks["evidence_sha256"], "preview_sha256": expected_preview_sha256,
                             "note": note.strip(), "observed_at_utc": checks["observed_at_utc"],
                             "meaning": "gates verified and original marker removed; pending barrier may remain"}
                completed_id = _audit(journal, "completed", completed)
                journal.conn.commit()
            except BaseException:
                journal.conn.rollback()
                raise
            # Completion is durable before clearing the final fail-closed barrier.
            journal.conn.execute("BEGIN IMMEDIATE")
            try:
                marker.require_locked(path)
                if current_ownership(journal, "research") is None:
                    raise RecoveryRefused("research ownership lost")
                final_checks = recovery_checks(journal, settings, kill_switch=kill, corpus_dir=corpus_dir)
                if (_ledger_identity(journal) != request["ledger"]
                        or not final_checks["ok"] or final_checks["evidence_sha256"] != completed["evidence_sha256"]
                        or marker.fingerprint(path)[0] is not None):
                    raise RecoveryRefused("gates or suspension changed before final release")
                if current_ownership(journal, "research") is None:
                    raise RecoveryRefused("research ownership lost")
                marker.remove_matching(marker.pending_path(path), pending_identity, marker_path=path)
            finally:
                journal.conn.rollback()
            return {"version": VERSION, "status": "released", "operation_id": prepared["operation_id"],
                    "completed_event_id": completed_id, "released_at_utc": _now().isoformat(),
                    "suspended": switch.is_engaged(),
                    "limitation": "No atomicity with future independent kill/gate changes; runtime guards remain active."}
