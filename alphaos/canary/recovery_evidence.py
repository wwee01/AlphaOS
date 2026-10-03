"""Read-only, producer-compatible canary evidence for manual shadow recovery.

This is a stricter *recovery proof*, not a change to runtime drift/suspension
policy. It reads the current corpus and one consistent database snapshot; no
provider calls, startup/migration, rows, markers or settings are changed.

A proof binds recorded evidence, not an authenticated model call. Historical
runs do not store a full manifest digest or per-result token usage. Current
fixture content is checked against its manifest, and its exact production
prompt projection against both runs. Missing provider fingerprints are allowed
when absent on both sides; that absence remains explicit.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from alphaos.ai import prompt_templates
from alphaos.ai.label_validation import coerce_and_validate
from alphaos.canary.corpus import DEFAULT_CORPUS_DIR
from alphaos.canary.run import _compute_drift, _reconstruct_packet
from alphaos.constants import Decision, DRIFT_NONE, OFFICIAL_LABELS
from alphaos.lineage.hashing import stable_hash
from alphaos.util import timeutils

VERSION = "canary_recovery_evidence_v1"
_MAX_PACKETS = 512
_MAX_FILE_BYTES = 1_048_576
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PACKET_FILE = re.compile(r"^[A-Za-z0-9_]+\.json$")


class _Invalid(ValueError):
    pass


def _json(raw: Any, kind: type) -> Any:
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise _Invalid("duplicate_json_key")
            out[key] = value
        return out

    def nonfinite(_):
        raise _Invalid("nonfinite_json")

    if not isinstance(raw, (str, bytes)):
        raise _Invalid("json_shape")
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
    if type(value) is not kind:
        raise _Invalid("json_shape")
    return value


def _time(raw: Any) -> datetime:
    if not isinstance(raw, (str, datetime)):
        raise _Invalid("timestamp")
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00")) if isinstance(raw, str) else raw
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise _Invalid("timestamp_timezone")
    return dt.astimezone(timezone.utc)


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _text(value: Any) -> bool:
    return type(value) is str and bool(value.strip())


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
        raise _Invalid("corpus_file")
    with path.open("rb") as handle:
        value = handle.read(_MAX_FILE_BYTES + 1)
    if len(value) > _MAX_FILE_BYTES:
        raise _Invalid("corpus_file_size")
    return value


def _corpus(path: Path) -> dict:
    """Own bytes before parsing/hashing; refuse loader's missing/extra loopholes."""
    manifest_bytes = _bytes(path / "MANIFEST.json")
    manifest = _json(manifest_bytes, dict)
    if type(manifest.get("version")) is not int or manifest["version"] < 1:
        raise _Invalid("corpus_version")
    entries = manifest.get("packets")
    if type(entries) is not list or not 0 < len(entries) <= _MAX_PACKETS:
        raise _Invalid("corpus_membership")
    expected: dict[str, str] = {}
    for entry in entries:
        if type(entry) is not dict:
            raise _Invalid("corpus_entry")
        name, digest = entry.get("file"), entry.get("sha256")
        if (not isinstance(name, str) or not _PACKET_FILE.fullmatch(name)
                or name == "MANIFEST.json" or name in expected
                or not isinstance(digest, str) or not _HEX64.fullmatch(digest)):
            raise _Invalid("corpus_entry")
        expected[name] = digest
    names = {p.name for p in path.glob("*.json") if p.name != "MANIFEST.json"}
    if names != set(expected):
        raise _Invalid("corpus_files_missing_or_extra")
    packets = {}
    owned = {}
    for name, expected_hash in sorted(expected.items()):
        content = _bytes(path / name)
        owned[name] = content
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise _Invalid("corpus_content_hash")
        fixture = _json(content, dict)
        pid = fixture.get("packet_id")
        if (not _text(pid) or name != f"{pid}.json" or pid in packets
                or not _text(fixture.get("candidate_id")) or not _text(fixture.get("symbol"))):
            raise _Invalid("corpus_packet_identity")
        packet = _reconstruct_packet(fixture)
        prompt = prompt_templates.build_label_user_prompt(packet.to_prompt_dict(), sorted(OFFICIAL_LABELS))
        packets[pid] = {"packet_id": pid, "candidate_id": fixture["candidate_id"],
                        "symbol": fixture["symbol"], "file_sha256": expected_hash,
                        "prompt_hash": stable_hash(prompt),
                        "system_prompt_hash": stable_hash(prompt_templates.LABEL_SYSTEM_PROMPT)}
    # This detects observed change during the read, not continuous filesystem
    # attestation. The recovery command must re-read this proof at apply.
    if (_bytes(path / "MANIFEST.json") != manifest_bytes
            or names != {p.name for p in path.glob("*.json") if p.name != "MANIFEST.json"}
            or any(_bytes(path / name) != value for name, value in owned.items())):
        raise _Invalid("corpus_changed_during_read")
    return {"manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "version": manifest["version"], "packets": packets}


def _rows(conn, sql, params=()) -> list[dict]:
    cursor = conn.execute(sql, params)
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _read(journal) -> dict:
    conn = journal.conn
    # SAVEPOINT starts a consistent read snapshot without committing or rolling
    # back any caller work. Only SELECTs occur inside; query_only is supported.
    conn.execute("SAVEPOINT canary_recovery_evidence")
    try:
        latest = _rows(conn, "SELECT * FROM canary_runs WHERE "
                       "(is_mock IS NULL OR is_mock != 1) AND "
                       "(confirmation_of IS NULL OR confirmation_of = '') ORDER BY id DESC LIMIT 1")
        baseline = _rows(conn, "SELECT * FROM canary_runs WHERE is_baseline = 1 ORDER BY id")
        result: dict[str, Any] = {"latest": latest, "baselines": baseline, "results": {}}
        for run in latest + baseline:
            result["results"][run["run_id"]] = _rows(
                conn, "SELECT * FROM canary_results WHERE run_id = ? ORDER BY id", (run["run_id"],))
        return result
    finally:
        conn.execute("RELEASE SAVEPOINT canary_recovery_evidence")


def _strings(raw) -> set[str]:
    values = _json(raw, list)
    if not all(_text(v) for v in values) or len(set(values)) != len(values):
        raise _Invalid("identity_aggregate")
    return set(values)


def _validate_run(run, rows, corpus, scope, corpus_path, now, prefix, reasons) -> dict:
    def fail(code):
        reasons.append(f"{prefix}.{code}")

    expected = corpus["packets"]
    if run.get("is_mock") != 0 or type(run.get("is_mock")) is not int:
        fail("not_real")
    if run.get("confirmation_of") is not None:
        fail("not_original_run")
    if run.get("configured_model") != scope["label_model"]:
        fail("configured_model_mismatch")
    try:
        if not _text(run.get("corpus_dir")) or Path(run["corpus_dir"]).resolve() != corpus_path.resolve():
            fail("corpus_path_mismatch")
    except (ValueError, OSError):
        fail("corpus_path_mismatch")
    if type(run.get("corpus_version")) is not int or run["corpus_version"] != corpus["version"]:
        fail("corpus_version_mismatch")
    if type(run.get("n_prompts")) is not int or run["n_prompts"] != len(expected):
        fail("prompt_count_mismatch")
    if type(run.get("n_parse_or_failsafe")) is not int or run["n_parse_or_failsafe"] != 0:
        fail("failsafe_aggregate")
    start = finish = None
    try:
        start, finish = _time(run.get("started_at_utc")), _time(run.get("finished_at_utc"))
        created = _time(run.get("created_at_utc"))
        if not start <= created <= finish <= now:
            fail("run_chronology")
    except (ValueError, TypeError):
        fail("run_chronology")
    counts = [run.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")]
    if (not all(type(v) is int and v >= 0 for v in counts)
            or counts[0] <= 0 or counts[2] <= 0 or counts[0] + counts[1] != counts[2]):
        fail("token_aggregate")
    pids = [r.get("packet_id") for r in rows]
    if (len(rows) != len(expected) or not all(isinstance(p, str) for p in pids)
            or len(set(pids)) != len(pids) or set(pids) != set(expected)):
        fail("result_membership")
    ids = [r.get("result_id") for r in rows]
    if not all(_text(i) for i in ids) or len(set(ids)) != len(ids):
        fail("result_identity")
    confidences, models, fingerprints = [], set(), set()
    by_packet = {}
    for row in rows:
        pid = row.get("packet_id")
        if pid not in expected:
            continue
        spec = expected[pid]
        if row.get("run_id") != run["run_id"] or row.get("symbol") != spec["symbol"]:
            fail("result_identity")
        if row.get("validation_status") != "passed" or type(row.get("is_failsafe")) is not int or row["is_failsafe"] != 0:
            fail("result_not_clean")
        if row.get("primary_label") not in OFFICIAL_LABELS or row.get("label_decision") not in {d.value for d in Decision}:
            fail("result_output")
        confidence = row.get("label_confidence")
        if not _number(confidence) or not 0 <= confidence <= 1:
            fail("result_confidence")
        else:
            confidences.append(confidence)
        try:
            created = _time(row.get("created_at_utc"))
            if start is None or finish is None or not start <= created <= finish <= now:
                fail("result_chronology")
        except (ValueError, TypeError):
            fail("result_chronology")
        if any(row.get(key) != spec[key] for key in ("prompt_hash", "system_prompt_hash")):
            fail("prompt_signature_mismatch")
        if not _text(row.get("response_model")):
            fail("response_model_missing")
        else:
            models.add(row["response_model"])
        fingerprint = row.get("system_fingerprint")
        if fingerprint is not None:
            if not _text(fingerprint):
                fail("fingerprint_malformed")
            else:
                fingerprints.add(fingerprint)
        try:
            raw = _json(row.get("raw_json"), dict)
            if (raw.get("symbol") != spec["symbol"] or raw.get("fail_safe")
                    or not _number(raw.get("confidence")) or not 0 <= raw["confidence"] <= 1):
                fail("raw_result")
            clean, status = coerce_and_validate(raw, SimpleNamespace(**scope))
            if status != "passed" or (clean["primary_label"], clean["decision"], clean["confidence"]) != (
                    row.get("primary_label"), row.get("label_decision"), confidence):
                fail("raw_result_mismatch")
        except (ValueError, TypeError, KeyError):
            fail("raw_result")
        by_packet[pid] = row
    try:
        if not models or _strings(run.get("response_models_json")) != models:
            fail("model_aggregate")
        if _strings(run.get("system_fingerprints_json")) != fingerprints:
            fail("fingerprint_aggregate")
    except (ValueError, TypeError):
        fail("identity_aggregate")
    mean = round(sum(confidences) / len(confidences), 4) if confidences else None
    if not _number(run.get("mean_confidence")) or run["mean_confidence"] != mean:
        fail("confidence_aggregate")
    return {"start": start, "finish": finish, "by_packet": by_packet,
            "models": sorted(models), "fingerprints": sorted(fingerprints)}


def validate_canary_recovery(journal, settings, *, as_of_utc=None, corpus_dir=None) -> dict:
    """Return a fresh gate and a stable fingerprint of its underlying inputs.

    ``journal.conn`` must be an existing SQLite connection; this function never
    opens a database. An optional aware datetime/string bounds an offline audit
    and cannot exceed actual project UTC. Default corpus is the producer's
    ``data/canary`` relative to the caller's application working directory.

    The fingerprint excludes observation time, age and verdict. Always rerun
    the gate at apply; a stable fingerprint is NOT permission to reuse an old
    ``ok`` after expiry. Configuration/model aliases are compared as configured;
    reported response identities are compared to their recorded baseline, not
    incorrectly forced to equal a possibly unversioned request alias.
    """
    from alphaos.scheduler.jobs import PREFLIGHT_CANARY_STALENESS_DAYS

    reasons: list[str] = []
    inputs: dict[str, Any] = {"version": VERSION}
    result: dict[str, Any] = {"version": VERSION, "ok": False, "reasons": reasons,
                             "run_id": None, "evidence_sha256": None, "inputs": {},
                             "observed_at_utc": None, "age_seconds": None,
                             "limitations": ["Recorded metadata is not provider-call authentication.",
                                             "Historical runs do not pin a full corpus manifest or per-result token usage.",
                                             "Current corpus prompt projection, not historical unprompted metadata, is verified."]}
    try:
        actual = _time(timeutils.now_utc())
        now = _time(as_of_utc) if as_of_utc is not None else actual
        if now > actual:
            raise _Invalid("future_observation")
        result["observed_at_utc"] = now.isoformat()
        scope = {k: getattr(settings, k) for k in (
            "label_model", "label_min_confidence_to_propose", "canary_tier2_label_diff_pct",
            "canary_tier3_confidence_shift_band")}
        scope["staleness_days"] = PREFLIGHT_CANARY_STALENESS_DAYS
        inputs["settings"] = scope
        if (not _text(scope["label_model"])
                or not _number(scope["label_min_confidence_to_propose"])
                or not 0 <= scope["label_min_confidence_to_propose"] <= 1
                or not _number(scope["canary_tier2_label_diff_pct"])
                or not 0 < scope["canary_tier2_label_diff_pct"] <= 1
                or not _number(scope["canary_tier3_confidence_shift_band"])
                or not 0 < scope["canary_tier3_confidence_shift_band"]):
            raise _Invalid("settings_scope")
        path = Path(corpus_dir if corpus_dir is not None else DEFAULT_CORPUS_DIR)
        inputs["corpus"] = corpus = _corpus(path)
        inputs["database"] = data = _read(journal)
        if not data["latest"]:
            reasons.append("latest.missing")
        if len(data["baselines"]) != 1:
            reasons.append("baseline.not_unique")
        if not reasons:
            latest, baseline = data["latest"][0], data["baselines"][0]
            result["run_id"] = latest["run_id"]
            current = _validate_run(latest, data["results"][latest["run_id"]], corpus, scope, path,
                                    now, "latest", reasons)
            reference = _validate_run(baseline, data["results"][baseline["run_id"]], corpus, scope, path,
                                      now, "baseline", reasons)
            if current["start"] is not None:
                result["age_seconds"] = age = (now - current["start"]).total_seconds()
                if not 0 <= age < PREFLIGHT_CANARY_STALENESS_DAYS * 86400:
                    reasons.append("latest.stale_or_future")
            if (reference["finish"] is None or current["start"] is None
                    or reference["finish"] > current["start"] or baseline["run_id"] == latest["run_id"]):
                reasons.append("baseline.not_prior_reference")
            if current["fingerprints"] != reference["fingerprints"]:
                reasons.append("fingerprint_scope_changed_or_missing")
            if not current["fingerprints"]:
                result["limitations"].append("The model supplied no system fingerprint; none is authenticated or inferred.")
            if latest.get("drift_tier") != DRIFT_NONE:
                reasons.append("latest.not_clean_drift")
            if not reasons:
                tier, detail = _compute_drift(
                    latest, current["by_packet"], baseline, reference["by_packet"],
                    scope["canary_tier2_label_diff_pct"], scope["canary_tier3_confidence_shift_band"])
                saved_detail = _json(latest.get("drift_detail_json"), dict)
                if tier != DRIFT_NONE:
                    reasons.append("latest.recomputed_drift_not_clean")
                if {k: v for k, v in saved_detail.items() if k != "confirmation"} != detail:
                    reasons.append("latest.drift_detail_mismatch")
        result["evidence_sha256"] = _hash(inputs)
    except (ValueError, TypeError, KeyError, AttributeError, OSError, sqlite3.Error, RecursionError) as exc:
        # No arbitrary raw payload, filesystem error or SQL is emitted. Both
        # callers must refuse when this diagnostic is unavailable.
        reasons.append(str(exc) if isinstance(exc, _Invalid) else f"evidence_unavailable:{type(exc).__name__}")
    # Public diagnostics/receipts carry compact bindings, never raw model
    # narratives. The full owned rows remain only in the internal hash preimage.
    compact: dict[str, Any] = {k: inputs[k] for k in ("version", "settings", "corpus") if k in inputs}
    saved_data = inputs.get("database")
    if saved_data is not None:
        try:
            compact["database"] = {
                "rows_sha256": _hash(saved_data),
                "latest_run_count": len(saved_data["latest"]),
                "baseline_run_count": len(saved_data["baselines"]),
                "result_counts": {key: len(rows) for key, rows in saved_data["results"].items()},
            }
        except (ValueError, TypeError):
            reasons.append("row_fingerprint_unavailable")
    result["inputs"] = compact
    result["reasons"] = sorted(set(reasons))
    result["ok"] = not result["reasons"]
    return result
