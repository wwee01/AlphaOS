"""Opt-in producer observations of selected loaded code and effective settings.

No source-file/HEAD attestation, no full settings serialization, no activation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import logging
import marshal
import math
import types
import uuid

VERSION = "decision_runtime_identity_v1"
FIELDS = {
    "labeller_decision_override_enabled": bool, "label_min_confidence_to_propose": float,
    "min_reward_risk": float, "labelling_enabled": bool, "openai_primary_model": str,
    "openai_prompt_version": str, "label_model": str, "label_propose_threshold": float,
    "label_max_output_tokens": int, "active_card_id": str, "last30days_enabled": bool,
    "last30days_polarity_enabled": bool, "news_enrichment_enabled": bool,
    "earnings_proximity_enabled": bool, "max_candidates_to_ai": int,
    "stop_loss_pct": float, "target_reward_risk": float, "is_mock": bool,
    "has_openai_key": bool,
}
LIMITS = ["Selected Python callable code only; transitive globals, provider internals, defaults and closures unverified.",
          "Pre/post settings observations cannot detect mutations restored between observations.",
          "Local producer records are not authenticated model execution or complete runtime attestation."]
TABLE_IDS = {"candidates": "candidate_id", "openai_evaluations": "eval_id",
             "candidate_labels": "label_id", "decision_adjustments": "adjustment_id"}
CANDIDATE_FIELDS = ("candidate_id", "scan_batch_id", "symbol", "price_snapshot_id", "scan_id", "created_at_utc")
ROW_FIELDS = {
    "candidates": ",".join(CANDIDATE_FIELDS),
    "openai_evaluations": "eval_id,candidate_id,symbol,decision,model,model_provider,prompt_template_version,schema_version,prompt_hash,system_prompt_hash,lineage_id,snapshot_json,created_at_utc",
    "candidate_labels": "label_id,candidate_id,scan_batch_id,packet_id,symbol,label_decision,label_confidence,label_version,label_source,validation_status,model,is_mock,label_frozen_at_utc,created_at_utc",
    "decision_adjustments": "adjustment_id,candidate_id,scan_batch_id,packet_id,symbol,eval_decision,label_decision,final_decision,override_armed,override_enabled,evidence_json,lineage_id,ai_lineage_json,created_at_utc",
}
CALLABLE_NAMES = {"label_candidate", "classify", "evaluate", "resolve_decision", "combine_decision", "override_armed", "real_decision_driver",
                  "evaluator.raw_evaluate", "evaluator.post_process", "evaluator._apply_atr_stop", "evaluator._enforce_min_reward_risk"}


def _time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("aware_runtime_time_required")
    return result


def _validate_runtime(value):
    """Validate declared narrow observation; return unavailable scope names."""
    if set(value) != {"settings", "callables", "effective_clients", "limitations", "full_runtime_attested"} or value["limitations"] != LIMITS or value["full_runtime_attested"] is not False:
        raise ValueError("exact_runtime_scope_required")
    missing: list[str] = []
    for group, names, fields in (("settings", {"orchestrator", "evaluator", "classifier"}, FIELDS),
                                ("effective_clients", {"evaluator", "classifier"}, {"model": str, "use_mock": bool})):
        if set(value[group]) != names:
            raise ValueError("runtime_settings_scope_mismatch")
        for name, section in value[group].items():
            if set(section) != {"values", "missing_or_invalid", "full_settings_verified"} or section["full_settings_verified"] is not False:
                raise ValueError("runtime_settings_shape_mismatch")
            absent = section["missing_or_invalid"]
            if not isinstance(absent, list) or len(set(absent)) != len(absent) or set(section["values"]) & set(absent) or set(section["values"]) | set(absent) != set(fields):
                raise ValueError("runtime_settings_field_mismatch")
            for key, val in section["values"].items():
                wanted = fields[key]
                if not (type(val) is wanted or wanted is float and type(val) is int) or type(val) is float and not math.isfinite(val) or type(val) is str and len(val) > 128:
                    raise ValueError("runtime_settings_type_mismatch")
            missing.extend(f"{group}.{name}.{key}" for key in absent)
    if set(value["callables"]) != CALLABLE_NAMES:
        raise ValueError("runtime_callable_scope_mismatch")
    for name, item in value["callables"].items():
        if set(item) != {"module", "qualname", "loaded_code_sha256", "bound_class", "bound_instance_token", "closure_present_unverified", "defaults_present_unverified"}:
            raise ValueError("runtime_callable_shape_mismatch")
        code = item["loaded_code_sha256"]
        if code is None:
            missing.append(f"callable.{name}")
        elif not isinstance(code, str) or len(code) != 64 or any(c not in "0123456789abcdef" for c in code):
            raise ValueError("runtime_callable_hash_mismatch")
        for key in ("closure_present_unverified", "defaults_present_unverified"):
            if type(item[key]) is not bool:
                raise ValueError("runtime_callable_flag_mismatch")
        for key in ("module", "qualname", "bound_class", "bound_instance_token"):
            if item[key] is not None and (type(item[key]) is not str or len(item[key]) > 512):
                raise ValueError("runtime_callable_name_mismatch")
    return missing


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def settings_slice(settings, fields=None):
    """Read only reviewed named scalar attributes, never enumerate Settings."""
    values, missing = {}, []
    for key, wanted in (FIELDS if fields is None else fields).items():
        value = getattr(settings, key, None)
        if isinstance(value, Enum):
            value = value.value
        valid = type(value) is wanted or (wanted is float and type(value) is int)
        if not valid or (type(value) is float and not math.isfinite(value)) or (
            type(value) is str and len(value) > 128
        ):
            missing.append(key)
        else:
            values[key] = value
    return {"values": values, "missing_or_invalid": missing, "full_settings_verified": False}


def callable_identity(fn):
    bound = getattr(fn, "__self__", None)
    function = getattr(fn, "__func__", fn)
    code = getattr(function, "__code__", None)
    return {
        "module": getattr(function, "__module__", None),
        "qualname": getattr(function, "__qualname__", None),
        "loaded_code_sha256": hashlib.sha256(marshal.dumps(code)).hexdigest()
        if isinstance(code, types.CodeType) else None,
        "bound_class": f"{type(bound).__module__}.{type(bound).__qualname__}" if bound is not None else None,
        "bound_instance_token": str(id(bound)) if bound is not None else None,
        "closure_present_unverified": bool(getattr(function, "__closure__", None)),
        "defaults_present_unverified": bool(getattr(function, "__defaults__", None) or getattr(function, "__kwdefaults__", None)),
    }


def observe_runtime(orch):
    callables = {"label_candidate": orch._label_candidate, "classify": orch.labeller.classify,
                 "evaluate": orch.openai.evaluate, "resolve_decision": orch._resolve_decision,
                 "combine_decision": orch._combine_decision, "override_armed": orch._override_armed,
                 "real_decision_driver": orch._real_decision_driver}
    for name in ("raw_evaluate", "post_process", "_apply_atr_stop", "_enforce_min_reward_risk"):
        callables[f"evaluator.{name}"] = getattr(orch.openai, name, None)
    return {"settings": {"orchestrator": settings_slice(orch.settings),
                          "evaluator": settings_slice(getattr(orch.openai, "settings", None)),
                          "classifier": settings_slice(getattr(orch.labeller, "settings", None))},
            "callables": {name: callable_identity(fn) for name, fn in callables.items()},
            "effective_clients": {name: settings_slice(client, {"model": str, "use_mock": bool})
                                  for name, client in (("evaluator", orch.openai), ("classifier", orch.labeller))},
            "limitations": LIMITS, "full_runtime_attested": False}


def _emit(orch, payload):
    try:
        acknowledgment = orch.runtime_identity_sink.append_runtime_identity(payload)
        if not isinstance(acknowledgment, str) or len(acknowledgment) != 64 or any(c not in "0123456789abcdef" for c in acknowledgment):
            raise ValueError("runtime_identity_receipt_not_acknowledged")
    except Exception:
        orch.runtime_identity_failures += 1
        try:
            logging.getLogger(__name__).error("runtime_identity_audit_unavailable")
        except Exception:
            pass


def _base(candidate, batch, phase):
    return {"version": VERSION, "observation_id": uuid.uuid4().hex,
            "candidate_id": candidate["candidate_id"], "scan_batch_id": batch,
            "phase": phase, "recorded_at_utc": _now(), "full_runtime_attested": False}


def begin(orch, candidate, batch, *, skipped=False):
    """Producer-only call, before label/evaluation. Errors never gate trading."""
    try:
        before = observe_runtime(orch)
        highwater = orch.journal.one("SELECT COALESCE(MAX(id),0) AS n FROM decision_adjustments")["n"]
        payload = {**_base(candidate, batch, "skipped" if skipped else "started"),
                   "before": before, "gaps": ["ai_degraded"] if skipped else []}
        _emit(orch, payload)
        return {"payload": payload, "adjustment_highwater": highwater}
    except Exception:
        _emit(orch, {**_base(candidate, batch, "skipped"), "gaps": ["runtime_identity_start_failed"]})
        return None


def _row_identity(table, row):
    value = {k: row.get(k) for k in CANDIDATE_FIELDS} if table == "candidates" else dict(row)
    return {"id": row[TABLE_IDS[table]], "row_sha256": _sha(value)}


def finish(orch, token, candidate, batch, evaluation, classification, final_decision):
    try:
        after = observe_runtime(orch)
        rows = {"candidates": orch.journal.query(f"SELECT {ROW_FIELDS['candidates']} FROM candidates WHERE candidate_id=?", (candidate["candidate_id"],)),
                "openai_evaluations": orch.journal.query(f"SELECT {ROW_FIELDS['openai_evaluations']} FROM openai_evaluations WHERE eval_id=?", (evaluation.eval_id,)),
                "candidate_labels": [], "decision_adjustments": []}
        if classification is not None:
            rows["candidate_labels"] = orch.journal.query(f"SELECT {ROW_FIELDS['candidate_labels']} FROM candidate_labels WHERE label_id=?", (classification.label_id,))
            if token is not None:
                rows["decision_adjustments"] = orch.journal.query(
                    f"SELECT {ROW_FIELDS['decision_adjustments']} FROM decision_adjustments WHERE candidate_id=? AND scan_batch_id=? AND id>?",
                    (candidate["candidate_id"], batch, token["adjustment_highwater"]))
        gaps = [] if token else ["missing_start"]
        if token and token["payload"]["before"] != after:
            gaps.append("runtime_changed_during_decision")
        for table, found in rows.items():
            expected = 1 if table in ("candidates", "openai_evaluations") or classification is not None else 0
            if len(found) != expected:
                gaps.append(f"ambiguous_{table}")
            if any(r.get("candidate_id") != candidate["candidate_id"] for r in found):
                gaps.append(f"wrong_candidate_{table}")
        payload = {**_base(candidate, batch, "finished"),
                   "start_observation_id": token["payload"]["observation_id"] if token else None,
                   "before": token["payload"]["before"] if token else None, "after": after,
                   "lineage": {t: [_row_identity(t, r) for r in found] for t, found in rows.items()},
                   "label_expected": classification is not None, "final_decision": final_decision, "gaps": gaps}
        _emit(orch, payload)
    except Exception:
        _emit(orch, {**_base(candidate, batch, "skipped"), "gaps": ["runtime_identity_finish_failed"]})


def audit_runtime_identity(snapshot, candidate_ids, receipts):
    """Read-only exact lineage check. Missing evidence remains in full census."""
    results = []
    for candidate_id in candidate_ids:
        events = [p for p in receipts if p.get("candidate_id") == candidate_id]
        starts = [p for p in events if p.get("phase") == "started"]
        ends = [p for p in events if p.get("phase") == "finished"]
        status, gaps = "consistent", []
        if not starts or not ends:
            status, gaps = "missing", ["producer_start_or_finish_missing"]
        elif len(starts) != 1 or len(ends) != 1:
            status, gaps = "contradictory", ["ambiguous_producer_observations"]
        else:
            a, b = starts[0], ends[0]
            if (a.get("version") != VERSION or b.get("version") != VERSION
                or b.get("start_observation_id") != a.get("observation_id")
                or a.get("before") != b.get("before") or b.get("before") != b.get("after")
                or a.get("scan_batch_id") != b.get("scan_batch_id") or b.get("gaps")
                or a.get("full_runtime_attested") is not False or b.get("full_runtime_attested") is not False):
                gaps.append("runtime_identity_or_link_mismatch")
            try:
                unavailable = _validate_runtime(b["before"]) + _validate_runtime(b["after"])
                start_time, end_time = _time(a["recorded_at_utc"]), _time(b["recorded_at_utc"])
                if not start_time <= end_time <= datetime.now(timezone.utc):
                    gaps.append("runtime_time_order_invalid")
                observed_rows = {}
                for table, key in TABLE_IDS.items():
                    refs = b["lineage"][table]
                    expected = 1 if table in ("candidates", "openai_evaluations") or b["label_expected"] else 0
                    if len(refs) != expected:
                        gaps.append(f"ambiguous_{table}")
                    for ref in refs:
                        rows = snapshot.conn.execute(f"SELECT {ROW_FIELDS[table]} FROM {table} WHERE {key}=?", (ref["id"],)).fetchall()
                        if len(rows) != 1 or dict(rows[0]).get("candidate_id") != candidate_id or _row_identity(table, dict(rows[0])) != ref:
                            gaps.append(f"saved_row_mismatch_{table}")
                        else:
                            saved = dict(rows[0])
                            observed_rows[table] = saved
                            row_time = _time(saved["created_at_utc"])
                            if not (row_time <= start_time if table == "candidates" else start_time <= row_time <= end_time):
                                gaps.append(f"saved_time_mismatch_{table}")
                            if table != "openai_evaluations" and saved.get("scan_batch_id") != b["scan_batch_id"]:
                                gaps.append(f"saved_batch_mismatch_{table}")
                final_row = observed_rows.get("decision_adjustments") if b["label_expected"] else observed_rows.get("openai_evaluations")
                if not final_row or final_row.get("final_decision" if b["label_expected"] else "decision") != b["final_decision"]:
                    gaps.append("saved_final_decision_mismatch")
            except (KeyError, TypeError, ValueError, AttributeError):
                gaps.append("malformed_runtime_identity")
            if gaps:
                status = "contradictory"
            elif unavailable:
                status, gaps = "missing", ["unavailable_selected_runtime_scope", *unavailable]
        results.append({"candidate_id": candidate_id, "status": status, "gaps": gaps})
    return {"version": VERSION, "candidates": results, "eligible_count": len(results),
            "complete_count": sum(r["status"] == "consistent" for r in results),
            "full_runtime_attested": False, "limitations": LIMITS}
