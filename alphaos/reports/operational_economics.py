"""Bounded scheduled cash-basis observations; append-only, never a trading input.

Raw normalized receipts stay in the journal. Reports recompute the latest
attempt, never select a favourable earlier result or promote provisional NAV.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from fractions import Fraction
import json
from typing import Any

from alphaos.reports.account_economics import (
    METHOD as ECONOMIC_METHOD,
    endpoint,
    measure_account_economics,
)
from alphaos.reports.close_equity import _calendar_time
from alphaos.reports.common_valuation import collect_common_valuation, evidence_hash
from alphaos.reports.economic_capture import EconomicSource, collect_economic_inputs
from alphaos.util import timeutils
from alphaos.util.ids import new_id

VERSION = "operational_cash_basis_economics_v1"
CATEGORY = "operational_economics_receipt"
MAX_BYTES = 8 * 1024 * 1024
MAX_RECEIPTS = 512
MAX_CHAIN_BYTES = 64 * 1024 * 1024
_OBSERVATION_CLOCKS = {"requested_at_utc", "received_at_utc", "captured_at_utc"}


class EvidenceError(ValueError):
    """Stable code-only failure; source bodies and credentials are not errors."""


class CaptureYielded(Exception):
    pass


def _dt(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and "T" in value:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise EvidenceError("aware_timestamp_required")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise EvidenceError("aware_timestamp_required")
    return dt.astimezone(timezone.utc)


def _owned(value):
    raw = json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))
    if len(raw.encode()) > MAX_BYTES:
        raise EvidenceError("receipt_size_limit")
    return json.loads(raw)


def _times(value, upper, depth=0):
    if depth > 25:
        raise EvidenceError("receipt_depth_limit")
    if isinstance(value, dict):
        for k, v in value.items():
            if k in _OBSERVATION_CLOCKS and _dt(v) > upper:
                raise EvidenceError("source_observation_after_recording")
            _times(v, upper, depth + 1)
    elif isinstance(value, list):
        for v in value:
            _times(v, upper, depth + 1)


def _contract(settings):
    return {
        "version": VERSION,
        "feed": settings.market_data_feed,
        "source": "EconomicSource",
        "max_get_calls": EconomicSource.MAX_CALLS,
        "valuation": "common_final_regular_session_minute_raw",
        "economic_method": ECONOMIC_METHOD,
        "subscription_convention": "150_usd_monthly_times_12_div_52_per_seven_days",
    }


def _latest(journal):
    return journal.one(
        "SELECT * FROM system_events WHERE category=? ORDER BY id DESC LIMIT 1",
        (CATEGORY,),
    )


def _read(row, now):
    if row is None:
        return None
    raw = row["detail_json"]
    if not isinstance(raw, str) or len(raw.encode()) > MAX_BYTES:
        raise EvidenceError("receipt_size_limit")
    receipt = json.loads(raw)
    e = receipt["envelope"]
    if receipt["sha256"] != evidence_hash(e) or e["version"] != VERSION:
        raise EvidenceError("receipt_pin_or_version_invalid")
    if (
        e["event_id"] != row["event_id"]
        or row["category"] != CATEGORY
        or row["message"] != VERSION
    ):
        raise EvidenceError("receipt_row_identity_invalid")
    if (
        e["capture_state"]
        not in {"captured", "yielded", "unavailable", "not_new_endpoint"}
        or type(e["anchor_is_self"]) is not bool
    ):
        raise EvidenceError("receipt_state_invalid")
    if (
        type(e["actual_get_attempts"]) is not int
        or not 0 <= e["actual_get_attempts"] <= EconomicSource.MAX_CALLS
    ):
        raise EvidenceError("receipt_request_count_invalid")
    start, recorded = _dt(e["attempt_started_at_utc"]), _dt(e["recorded_at_utc"])
    if not start <= recorded <= now or _dt(row["created_at_utc"]) != recorded:
        raise EvidenceError("receipt_recording_clock_invalid")
    if e["attempt_market_date"] != timeutils.market_date(start).isoformat():
        raise EvidenceError("receipt_attempt_date_invalid")
    _times(e, recorded)
    if e["capture"] is not None:
        capture = e["capture"]
        if evidence_hash(capture["raw_evidence"]) != capture["input_sha256"]:
            raise EvidenceError("endpoint_pin_invalid")
        if not start <= _dt(capture["raw_evidence"]["captured_at_utc"]) <= recorded:
            raise EvidenceError("endpoint_capture_clock_invalid")
        if capture["raw_evidence"]["configured_feed"] != e["contract"]["feed"]:
            raise EvidenceError("endpoint_feed_identity_invalid")
    return receipt


def _anchor(journal, receipt, now):
    """Last chronological archived endpoint, including an invalid one; no best-result search."""
    if receipt is None:
        return None
    e = receipt["envelope"]
    if e["anchor_is_self"]:
        if e["capture"] is None:
            raise EvidenceError("anchor_capture_missing")
        return {
            "event_id": e["event_id"],
            "sha256": receipt["sha256"],
            "capture": e["capture"],
            "recorded_at_utc": e["recorded_at_utc"],
        }
    ref = e["opening_reference"]
    if ref is None:
        return None
    row = journal.one(
        "SELECT * FROM system_events WHERE category=? AND event_id=?",
        (CATEGORY, ref["event_id"]),
    )
    previous = _read(row, now)
    if (
        previous is None
        or previous["sha256"] != ref["sha256"]
        or not previous["envelope"]["anchor_is_self"]
    ):
        raise EvidenceError("opening_reference_invalid")
    if _dt(previous["envelope"]["recorded_at_utc"]) > _dt(e["attempt_started_at_utc"]):
        raise EvidenceError("opening_reference_chronology_invalid")
    return {
        **ref,
        "capture": previous["envelope"]["capture"],
        "recorded_at_utc": previous["envelope"]["recorded_at_utc"],
    }


def _derive(e, opening):
    """Recompute all reportable values, including on reads of stored receipts."""
    result: dict[str, Any] = {
        "version": VERSION,
        "status": e["capture_state"],
        "comparison_eligible": False,
        "all_in_net_gain_usd": None,
        "weekly_target_verdict": None,
        "research_activation": False,
        "other_operating_expenses": "unknown; actual bills and shared allocation not supplied",
        "subscription_monthly_usd": "150",
        "subscription_weekly_usd_exact": {"numerator": 450, "denominator": 13},
        "subscription_allocation_usd": None,
        "subscription_only_adjusted_gain_usd": None,
        "subscription_equity_inclusion": "unreconciled; no deduction from account cash",
        "blockers": [],
    }
    result.update(
        activity_coverage_start_utc=e["activity_coverage_start_utc"],
        activity_history_policy="retain_complete_opening_creation_time_query; lookback_grows; rollover_not_implemented",
        max_activity_pages=EconomicSource.MAX_PAGES,
        activity_page_size=EconomicSource.PAGE_SIZE,
        max_get_calls=EconomicSource.MAX_CALLS,
        activity_lookback_days_at_attempt=(
            (
                _dt(e["attempt_started_at_utc"]) - _dt(e["activity_coverage_start_utc"])
            ).total_seconds()
            / 86400
            if e["activity_coverage_start_utc"]
            else None
        ),
    )
    capture = e["capture"]
    if capture is None:
        result["blockers"] = [e["failure_code"] or "capture_incomplete"]
        return result
    try:
        end = endpoint(capture)
        result.update(
            endpoint_status="validated_asof",
            endpoint_cutoff_utc=end["cutoff"].isoformat(),
            endpoint_nav_usd=str(end["nav"]),
            endpoint_source_sha256=end["sha256"],
            common_capture_status=capture["status"],
        )
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        result.update(
            status="blocked",
            endpoint_status="blocked",
            blockers=["endpoint_validation_failed"],
            validation_error_type=type(exc).__name__,
        )
        return result
    if e["capture_state"] == "not_new_endpoint":
        result["blockers"] = ["same_or_older_cutoff_not_replaced"]
        return result
    if opening is None:
        result.update(
            status="waiting_for_second_endpoint",
            blockers=["independently_archived_opening_endpoint_missing"],
        )
        return result
    if _dt(opening["recorded_at_utc"]) >= end["cutoff"]:
        result.update(
            status="blocked",
            blockers=["opening_receipt_not_archived_before_ending_cutoff"],
        )
        return result
    inputs = e["economic_inputs"]
    if inputs is None:
        result.update(
            status=e["capture_state"],
            blockers=[e["failure_code"] or "interval_evidence_incomplete"],
        )
        return result
    if (
        inputs["start_capture"] != opening["capture"]
        or inputs["end_capture"] != capture
        or inputs.get("operating_cost") is not None
    ):
        raise EvidenceError("interval_endpoint_or_cost_binding_invalid")
    economic = measure_account_economics(inputs)
    result.update(
        status=economic["status"],
        comparison_eligible=economic["comparison_eligible"],
        economic=economic,
        blockers=list(economic["blockers"]),
    )
    if economic["status"] == "reconciled_asof":
        elapsed = _dt(economic["end_at_utc"]) - _dt(economic["start_at_utc"])
        micros = (
            elapsed.days * 86400 + elapsed.seconds
        ) * 1000000 + elapsed.microseconds
        allocated = Fraction(450, 13) * Fraction(micros, 7 * 86400 * 1000000)
        result["subscription_allocation_usd"] = {
            "numerator": allocated.numerator,
            "denominator": allocated.denominator,
            "display": str(float(allocated)),
        }
        # No assumed external/embedded allocation; subtracting this could double-count a fee.
        result["subscription_allocation_basis"] = (
            "Known convention over exact interval; not a payment receipt or all-in deduction."
        )
    return result


def _validate_latest(journal, now):
    # Validate every chronological anchor transition, including failures. A
    # later valid-looking receipt cannot conceal a missing or skipped ancestor.
    previous = None
    anchor = None
    latest_row = latest = latest_opening = None
    consumed = 0
    cursor = journal.conn.execute(
        "SELECT * FROM system_events WHERE category=? ORDER BY id ASC LIMIT ?",
        (CATEGORY, MAX_RECEIPTS + 1),
    )
    for count, raw_row in enumerate(cursor, 1):
        if count > MAX_RECEIPTS:
            raise EvidenceError("receipt_chain_count_limit")
        row = dict(raw_row)
        receipt = _read(row, now)
        consumed += len(row["detail_json"].encode())
        if consumed > MAX_CHAIN_BYTES:
            raise EvidenceError("receipt_chain_size_limit")
        e = receipt["envelope"]
        if e["previous_receipt_sha256"] != (previous["sha256"] if previous else None):
            raise EvidenceError("previous_receipt_pin_invalid")
        if previous and _dt(previous["envelope"]["recorded_at_utc"]) > _dt(
            e["attempt_started_at_utc"]
        ):
            raise EvidenceError("previous_receipt_chronology_invalid")
        expected_ref = (
            {k: anchor[k] for k in ("event_id", "sha256")} if anchor else None
        )
        if e["opening_reference"] != expected_ref:
            raise EvidenceError("opening_reference_not_consecutive")
        latest_opening = anchor
        capture = e["capture"]
        advances = bool(
            capture
            and (
                anchor is None
                or _calendar_time(capture["raw_evidence"]["session"], "close")
                > _calendar_time(anchor["capture"]["raw_evidence"]["session"], "close")
            )
        )
        if e["anchor_is_self"] != advances:
            raise EvidenceError("endpoint_anchor_transition_invalid")
        if advances:
            anchor = {
                "event_id": e["event_id"],
                "sha256": receipt["sha256"],
                "capture": capture,
                "recorded_at_utc": e["recorded_at_utc"],
            }
        previous = latest = receipt
        latest_row = row
    if (
        latest is not None
        and _derive(latest["envelope"], latest_opening) != latest["envelope"]["derived"]
    ):
        raise EvidenceError("derived_receipt_changed")
    return latest_row, latest


def _append(journal, expected_row, envelope):
    conn = journal.conn
    if conn.in_transaction:
        raise EvidenceError("caller_transaction_not_supported_for_capture")
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _latest(journal)
        if current != expected_row:
            raise EvidenceError("concurrent_capture_receipt_changed")
        now = _dt(timeutils.now_utc())
        _validate_latest(journal, now)
        if now < _dt(envelope["attempt_started_at_utc"]):
            raise EvidenceError("recording_clock_regressed")
        envelope["recorded_at_utc"] = now.isoformat()
        _times(envelope, now)
        receipt = _owned({"envelope": envelope, "sha256": evidence_hash(envelope)})
        encoded = json.dumps(
            receipt, sort_keys=True, allow_nan=False, separators=(",", ":")
        )
        totals = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(length(CAST(detail_json AS BLOB))),0) FROM system_events WHERE category=?",
            (CATEGORY,),
        ).fetchone()
        if (
            totals[0] >= MAX_RECEIPTS
            or totals[1] + len(encoded.encode()) > MAX_CHAIN_BYTES
        ):
            raise EvidenceError("receipt_chain_capacity_reached")
        stamp = timeutils.stamp(now)
        conn.execute(
            "INSERT INTO system_events (event_id,severity,category,message,detail_json,created_at_utc,created_at_sgt) VALUES (?,?,?,?,?,?,?)",
            (
                envelope["event_id"],
                "info"
                if envelope["derived"]["status"]
                in {"reconciled_asof", "waiting_for_second_endpoint"}
                else "warning",
                CATEGORY,
                VERSION,
                encoded,
                stamp.utc,
                stamp.local_sgt,
            ),
        )
        conn.commit()
        return receipt
    except BaseException:
        conn.rollback()
        raise


def capture_operational_economics(
    journal, settings, *, should_yield=None, source_factory=None
):
    """One bounded existing-source attempt. No catch-up loop or hidden retries.

    source_factory is a test injection accepting settings and activity_start.
    The production factory is always the redirect-refusing EconomicSource.
    """
    if settings.is_mock or settings.offline_mode or not settings.has_alpaca_keys:
        return {
            "version": VERSION,
            "status": "unavailable",
            "reason": "live_paper_source_not_configured",
            "written": 0,
        }
    if journal.conn.in_transaction:
        raise EvidenceError("caller_transaction_not_supported_for_capture")
    now = _dt(timeutils.now_utc())
    previous_row, previous = _validate_latest(journal, now)
    contract = _contract(settings)
    if previous and previous["envelope"]["contract"] != contract:
        raise EvidenceError("configured_source_contract_changed")
    day = timeutils.market_date(now).isoformat()
    if (
        previous
        and previous["envelope"]["attempt_market_date"] == day
        and (
            previous["envelope"]["capture_state"] != "yielded"
            or (
                previous["envelope"]["capture"] is not None
                and previous["envelope"]["failure_code"] is None
            )
        )
    ):
        return {
            "version": VERSION,
            "status": "already_recorded",
            "written": 0,
            "event_id": previous["envelope"]["event_id"],
            "measurement_status": previous["envelope"]["derived"]["status"],
        }
    if should_yield and should_yield():
        return {
            "version": VERSION,
            "status": "yielded",
            "written": 0,
            "reason": "scan_window_reserved",
        }
    opening = _anchor(journal, previous, now)
    ref = {k: opening[k] for k in ("event_id", "sha256")} if opening else None
    activity_start = (
        opening["capture"]["raw_evidence"]["activities_after"]["request"]["after"]
        if opening
        else now.isoformat()
    )
    source = None
    calls = 0
    e: dict[str, Any] = {
        "version": VERSION,
        "event_id": new_id("opnav"),
        "contract": contract,
        "previous_receipt_sha256": previous["sha256"] if previous else None,
        "attempt_started_at_utc": now.isoformat(),
        "attempt_market_date": day,
        "recorded_at_utc": None,
        "opening_reference": ref,
        "capture": None,
        "economic_inputs": None,
        "anchor_is_self": False,
        "capture_state": "unavailable",
        "failure_code": None,
        "actual_get_attempts": 0,
        "activity_coverage_start_utc": activity_start if opening else None,
    }
    try:
        source = (source_factory or EconomicSource)(
            settings, activity_start=activity_start
        )
        original_transport = source.transport

        def guarded_transport(base, path, params):
            nonlocal calls
            if should_yield and should_yield():
                raise CaptureYielded()
            calls += 1
            return original_transport(base, path, params)

        source.transport = guarded_transport
        if opening:
            inputs = collect_economic_inputs(source, deepcopy(opening["capture"]))
            e["capture"] = inputs["end_capture"]
            old_cutoff = _calendar_time(
                opening["capture"]["raw_evidence"]["session"], "close"
            )
            new_cutoff = _calendar_time(
                e["capture"]["raw_evidence"]["session"], "close"
            )
            if new_cutoff <= old_cutoff:
                e["capture_state"] = "not_new_endpoint"
            else:
                e.update(
                    economic_inputs=inputs,
                    anchor_is_self=True,
                    capture_state="captured",
                )
        else:
            e.update(
                capture=collect_common_valuation(source, now=now),
                anchor_is_self=True,
                capture_state="captured",
            )
        if should_yield and should_yield():
            # Completed bounded receipts can be retained; status still truthfully yields the job.
            e["capture_state"] = "yielded"
    except CaptureYielded:
        e.update(capture_state="yielded", failure_code="scan_window_reserved")
    except Exception as exc:
        known = {
            "account activity pagination cap reached; coverage unknown": "activity_pagination_cap_reached",
            "economic capture request cap reached": "request_cap_reached",
            "manual valuation request cap reached": "request_cap_reached",
            "corporate-action pagination cap reached": "corporate_action_pagination_cap_reached",
            "common bar pagination cap reached; coverage unknown": "common_bar_pagination_cap_reached",
        }
        e.update(
            capture_state="unavailable",
            failure_code=known.get(str(exc), "source_capture_incomplete"),
            failure_type=type(exc).__name__,
        )
    e["actual_get_attempts"] = calls
    e["completed_requests"] = (
        source.partial.get("completed_requests", []) if source else []
    )
    if e["capture"] is not None:
        e["activity_coverage_start_utc"] = e["capture"]["raw_evidence"][
            "activities_after"
        ]["request"]["after"]
    e = _owned(e)
    e["derived"] = _derive(e, opening)
    saved = _append(journal, previous_row, e)
    return {
        "version": VERSION,
        "status": "yielded"
        if e["capture_state"] == "yielded"
        else e["derived"]["status"],
        "written": 1,
        "event_id": e["event_id"],
        "receipt_sha256": saved["sha256"],
        "actual_get_attempts": calls,
        "measurement": e["derived"],
    }


def _newer_job_gap(journal, receipt):
    """A failed receipt write must not leave an older success looking current."""
    row = journal.one(
        "SELECT * FROM job_runs WHERE job_type='benchmark_spine' ORDER BY id DESC LIMIT 1"
    )
    if not row:
        return None
    if receipt and _dt(row["started_at_utc"]) <= _dt(
        receipt["envelope"]["recorded_at_utc"]
    ):
        return None
    state = row["status"]
    summary = json.loads(row.get("result_summary_json") or "{}")
    if not isinstance(summary, dict) or not isinstance(
        summary.get("benchmark_spine_result", {}), dict
    ):
        raise EvidenceError("job_summary_shape_invalid")
    capture = summary.get("benchmark_spine_result", {}).get("operational_economics")
    if capture is not None and not isinstance(capture, dict):
        raise EvidenceError("job_capture_shape_invalid")
    if state == "started":
        reason = "newer_benchmark_attempt_in_progress"
    elif state == "failed":
        reason = "newer_benchmark_attempt_failed_before_receipt"
    elif capture is None and receipt is None:
        return None  # no new-contract receipt exists; older releases had no capture contract
    elif (
        capture is not None
        and capture.get("status") == "already_recorded"
        and receipt
        and capture.get("event_id") == receipt["envelope"]["event_id"]
    ):
        return None
    else:
        reason = "newer_benchmark_attempt_has_no_valid_receipt"
    return {
        "version": VERSION,
        "status": "unavailable",
        "comparison_eligible": False,
        "all_in_net_gain_usd": None,
        "weekly_target_verdict": None,
        "blockers": [reason],
        "job_run_id": row["job_run_id"],
        "job_status": state,
        "prior_receipt_event_id": receipt["envelope"]["event_id"] if receipt else None,
    }


def build_operational_economics_report(journal, settings):
    """Read-only coherent view of the newest attempt; no source calls or fallback."""
    owns = not journal.conn.in_transaction
    if owns:
        journal.conn.execute("BEGIN")
    try:
        _, receipt = _validate_latest(journal, _dt(timeutils.now_utc()))
        gap = _newer_job_gap(journal, receipt)
        if gap:
            return gap
        if receipt is None:
            return {
                "version": VERSION,
                "status": "not_collected",
                "comparison_eligible": False,
                "all_in_net_gain_usd": None,
                "weekly_target_verdict": None,
            }
        e = receipt["envelope"]
        if e["contract"] != _contract(settings):
            raise EvidenceError("configured_source_contract_changed")
        # Source preimages remain private in the ledger. No raw account/activity narrative in reports.
        result = deepcopy(e["derived"])
        result.update(
            event_id=e["event_id"],
            receipt_sha256=receipt["sha256"],
            recorded_at_utc=e["recorded_at_utc"],
            actual_get_attempts=e["actual_get_attempts"],
            capture_state=e["capture_state"],
        )
        if "economic" in result:
            allowed = {
                "status",
                "basis",
                "start_at_utc",
                "end_at_utc",
                "start_nav",
                "end_nav",
                "account_gain_usd",
                "account_return",
                "benchmark_return",
                "benchmark_gain_usd",
                "excess_gain_usd",
                "excess_return",
                "cash_residual",
                "quantity_residuals",
                "reported_broker_fee_cash",
                "broker_fee_completeness",
                "modeled_trade_cost_deduction",
                "operating_cost_status",
                "comparison_eligible",
                "blockers",
                "percentage_comparison_blockers",
                "dividend_convention",
                "source_limitations",
                "input_sha256",
            }
            result["economic"] = {
                k: v for k, v in result["economic"].items() if k in allowed
            }
        return result
    except (KeyError, TypeError, ValueError, ArithmeticError, RecursionError):
        return {
            "version": VERSION,
            "status": "evidence_unreadable",
            "comparison_eligible": False,
            "all_in_net_gain_usd": None,
            "weekly_target_verdict": None,
            "blockers": ["latest_receipt_validation_failed"],
        }
    finally:
        if owns:
            journal.conn.rollback()
