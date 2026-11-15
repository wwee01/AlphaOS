"""Read-only, bounded reconciliation with explicit uncertainty and lineage.

SQLite is captured in one read transaction; broker GETs are sequential and
non-atomic. Nothing repairs ledger rows, resets gates, or operates orders.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from hashlib import sha256
import json
import math
from typing import Any

from alphaos.monitoring.broker_reads import paper_guard
from alphaos.execution.protection_contract import verified_live_roles
from alphaos.scheduler.cadence import parse_windows
from alphaos.scheduler.monitor_health import monitor_health
from alphaos.util import timeutils
from alphaos.util.market_calendar import is_trading_day

VERSION = "paper_observer_v1"
ACTIVE = {"new", "accepted", "partially_filled"}
TERMINAL = {"filled", "cancelled", "rejected", "expired", "replaced"}


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, default=str, allow_nan=False).encode()).hexdigest()


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def same(a, b):
    a, b = number(a), number(b)
    return a is not None and b is not None and math.isclose(a, b, abs_tol=1e-6, rel_tol=1e-8)


def same_limit_price(intended, actual):
    """Compare the broker limit to the existing dispatch's cent-normalized intent.

    Never round the broker observation or substitute the execution fill price.
    Malformed/nonpositive prices cannot become matches through normalization.
    """
    if isinstance(intended, bool) or isinstance(actual, bool):
        return False
    intended, actual = number(intended), number(actual)
    if intended is None or actual is None or intended <= 0 or actual <= 0:
        return False
    normalized = round(intended, 2)
    return normalized > 0 and same(normalized, actual)


def stale_age(age, cap):
    return age is None or age < 0 or age > cap


def metadata(value):
    result = json.loads(value or "{}")
    if not isinstance(result, dict):
        raise ValueError("metadata_object_required")
    return result


def ledger_snapshot(journal, settings, now, history_days=7, limit=200):
    """Capture recent orders plus every unresolved intent/open origin, even old."""
    if journal.read_only is not True:
        raise ValueError("read_only_journal_required")
    cutoff = timeutils.to_iso(now - timedelta(days=history_days))
    journal.conn.execute("BEGIN")
    try:
        positions = journal.query("SELECT * FROM positions WHERE status='open' AND execution_source='alpaca_paper'")
        orders = journal.query(
            "SELECT o.* FROM paper_orders o WHERE o.execution_source='alpaca_paper' AND "
            "(o.state NOT IN ('filled','cancelled','rejected','expired','replaced') "
            "OR COALESCE(o.updated_at_utc,o.created_at_utc)>=? "
            "OR EXISTS (SELECT 1 FROM positions p WHERE p.order_id=o.order_id AND p.status='open') "
            "OR EXISTS (SELECT 1 FROM json_each(COALESCE(NULLIF(o.raw_request_json,''),'{}')) "
            "WHERE key='phase' AND value IN ('canceling','submitting','working','unknown'))) "
            "ORDER BY o.id DESC LIMIT ?", (cutoff, limit + 1),
        )
        if len(orders) > limit:
            raise ValueError("order_scope_exceeds_limit")
        fills: dict = {}
        chains: dict = {}
        for row in orders:
            fills[row["order_id"]] = journal.query("SELECT * FROM paper_fills WHERE order_id=?", (row["order_id"],))
            proposal = journal.one("SELECT * FROM trade_proposals WHERE proposal_id=?", (row.get("proposal_id"),))
            if not proposal:
                chains[row["order_id"]] = {}
                continue
            candidate = journal.one("SELECT * FROM candidates WHERE candidate_id=?", (proposal["candidate_id"],))
            chains[row["order_id"]] = {
                "proposal": proposal, "candidate": candidate,
                "evaluation": journal.one("SELECT * FROM openai_evaluations WHERE eval_id=?", (proposal.get("eval_id"),)),
                "risk": journal.one("SELECT * FROM risk_checks WHERE risk_check_id=?", (row.get("risk_check_id"),)),
                "approval": journal.one("SELECT * FROM approvals WHERE proposal_id=? AND approved=1 ORDER BY id DESC LIMIT 1", (proposal["proposal_id"],)),
                "snapshot": journal.one("SELECT * FROM price_snapshots WHERE snapshot_id=?", ((candidate or {}).get("price_snapshot_id"),)),
                "lineage": journal.one("SELECT * FROM lineage_snapshots WHERE lineage_id=?", (proposal.get("lineage_id"),)),
                "card": journal.one("SELECT * FROM setup_cards WHERE card_id=? AND version=?", (proposal.get("card_id"), proposal.get("card_version"))),
            }
        # Broker leg fills can be journaled under a synthetic exit order.
        broker_fills = journal.query("SELECT broker_order_id,qty,price,filled_at,fill_id FROM paper_fills WHERE execution_source='alpaca_paper' AND broker_order_id IS NOT NULL")
        scan = journal.one("SELECT * FROM scan_batches WHERE scan_type NOT LIKE '%shadow%' ORDER BY id DESC LIMIT 1")
        scan_data = journal.one("SELECT COUNT(*) n, SUM(CASE WHEN is_usable=1 THEN 1 ELSE 0 END) usable FROM price_snapshots WHERE created_at_utc>=? AND created_at_utc<=?", ((scan or {}).get("started_at_utc"), (scan or {}).get("completed_at_utc")))
        position_data = {p["position_id"]: journal.one("SELECT snapshot_id,quote_timestamp,bar_timestamp,received_at,created_at_utc,is_usable,is_mock FROM price_snapshots WHERE symbol=? ORDER BY id DESC LIMIT 1", (p["symbol"],)) for p in positions}
        jobs = journal.query("SELECT id,job_type,status,lock_key,started_at_utc,finished_at_utc,result_summary_json FROM job_runs WHERE job_type='scan' AND started_at_utc>=? ORDER BY id DESC", (timeutils.to_iso(now - timedelta(days=2)),))
        return {"orders": orders, "positions": positions, "fills": fills, "broker_fills": broker_fills,
                "chains": chains, "scan": scan, "scan_jobs": jobs, "scan_data": scan_data,
                "position_data": position_data,
                "risk_outcomes": journal.query("SELECT result,fail_reason,COUNT(*) n FROM risk_checks WHERE created_at_utc>=? GROUP BY result,fail_reason", (cutoff,)),
                "monitor_health": monitor_health(journal, settings, now),
                "captured_at_utc": timeutils.to_iso(now), "scope": {"history_days": history_days, "order_limit": limit, "order_count": len(orders), "recent_cutoff_utc": cutoff}}
    finally:
        journal.conn.rollback()


def observe(local, settings, broker=None, now=None):
    paper_guard(settings)
    now = now or timeutils.now_utc()
    findings: dict[str, dict] = {}
    def issue(code, entity, detail, evidence=None, severity="error"):
        key = f"{code}:{entity}"
        findings[key] = {"key": key, "code": code, "entity": str(entity), "severity": severity,
                         "detail": detail, "evidence": evidence or {}}

    health = local["monitor_health"]
    for code in health.get("issue_codes", []):
        issue(code, "execution_monitor", "Existing execution-monitor readiness is unhealthy.", health)
    result = {"version": VERSION, "observed_at_utc": timeutils.to_iso(now),
              "ledger_captured_at_utc": local["captured_at_utc"], "ledger_source_sha256": digest(local),
              "scope": {**local["scope"], "order_ids": [r["order_id"] for r in local["orders"]]}, "monitor_health": health,
              "risk_gate_outcomes": local["risk_outcomes"], "broker_verified": False,
              "external_observer_required": True, "broker_reads_non_atomic": True,
              "paper_results_live_equivalent": False}
    hours: dict[str, Any] = {"date": timeutils.to_et(now).date().isoformat(),
             "open": "09:30" if is_trading_day(timeutils.to_et(now).date()) else None,
             "close": "16:00" if is_trading_day(timeutils.to_et(now).date()) else None,
             "source": "local_calendar_full_closures_only"}
    exact: dict = {}
    open_orders, positions = [], []
    if broker is None:
        issue("broker_unverified", "account", "Ledger-only inspection cannot establish broker reconciliation.", severity="unknown")
    else:
        try:
            result["account"] = broker.account()
            positions = broker.positions()
            open_orders = broker.open_orders()
            hours = broker.session_hours(now)
            for row in local["orders"]:
                if not row.get("broker_order_id") and not row.get("client_order_id"):
                    exact[row["order_id"]] = None
                    continue
                norm = broker.exact_order(row.get("broker_order_id"), row.get("client_order_id"))
                if norm is None and row.get("broker_order_id") and row.get("client_order_id"):
                    norm = broker.exact_order(client_id=row["client_order_id"])
                exact[row["order_id"]] = norm
            result["broker_verified"] = True
        except Exception as exc:
            # Do not let API errors, authentication failures or partial reads
            # masquerade as missing orders/flat positions. No provider text.
            issue("broker_unavailable", "account", "Broker observation failed; reconciliation is unknown.", {"error_type": type(exc).__name__}, "unknown")
        result["broker_receipts"] = getattr(broker, "receipts", [])
        result["broker_read_budget"] = {"used": getattr(broker, "requests_sent", None),
                                       "limit": getattr(broker, "max_requests", None)}
    result["market_calendar"] = hours
    et = timeutils.to_et(now)
    hhmm = et.strftime("%H:%M")
    regular = bool(hours["open"] and hours["open"] <= hhmm < hours["close"])
    result["regular_session"] = regular
    _scan_health(local, settings, now, hours, issue, result)
    _lineage(local, settings, issue, result)
    if result["broker_verified"]:
        _reconcile(local, exact, open_orders, positions, issue, result)
        if regular:
            for pos in local["positions"]:
                # Historical decision timestamps are assessed at decision time
                # in _lineage, not aged against the current wall clock.
                snapshot = local["position_data"].get(pos["position_id"])
                if snapshot:
                    ages = {k: timeutils.age_seconds(snapshot.get(k), now) for k in ("quote_timestamp", "bar_timestamp")}
                    result.setdefault("position_data_evidence", []).append({"position_id": pos["position_id"], **snapshot, "source_ages_seconds": ages})
                    stale = snapshot.get("is_mock") or not snapshot.get("is_usable") or any(stale_age(ages[k], cap) for k, cap in (("quote_timestamp", settings.max_quote_age_seconds_rth), ("bar_timestamp", settings.max_bar_age_seconds_rth)))
                else:
                    stale = True
                if stale:
                    issue("position_data_stale", pos["position_id"], "Current regular-session position data is missing, stale or unusable; broker price has no verified source timestamp.", snapshot)
    result["findings"] = sorted(findings.values(), key=lambda v: v["key"])
    result["ok"] = not findings
    result["status"] = "healthy" if not findings else "attention_required"
    return result


def _scan_health(local, settings, now, hours, issue, result):
    et = timeutils.to_et(now)
    grace = local["monitor_health"].get("stale_after_minutes", 30)
    for start, end in parse_windows(settings.scheduler_scan_windows):
        # Truncate configured windows using the broker's actual early close.
        if not hours["open"] or start >= hours["close"]:
            continue
        end = min(end, hours["close"])
        deadline = et.replace(hour=int(end[:2]), minute=int(end[3:]), second=0, microsecond=0) + timedelta(minutes=grace)
        if et <= deadline:
            continue
        key = f"scan:{et.date().isoformat()}T{start}"
        rows = [r for r in local["scan_jobs"] if r.get("lock_key") == key]
        if not any(r["status"] == "completed" for r in rows):
            issue("scan_window_missing", f"{et.date()}:{start}", "Expected scan window has no completed receipt after its grace period.", {"lock_key": key, "deadline_et": deadline.isoformat(), "attempts": [{"id": r["id"], "status": r["status"]} for r in rows]})
    scan = local["scan"]
    result["activity"] = {"classification": "no_completed_scan_evidence"}
    if not scan:
        return
    evidence = {k: scan.get(k) for k in ("scan_batch_id", "started_at_utc", "completed_at_utc", "status", "universe_count", "candidates_found", "proposals_created", "errors_count", "blocked_count")}
    classification = "completed_with_candidates" if (scan.get("candidates_found") or 0) > 0 else "healthy_zero_candidates"
    if scan["status"] != "completed" or scan.get("errors_count"):
        issue("scan_failed", "latest_scan", "Latest scan did not complete cleanly.", evidence)
        classification = "scan_failure"
    elif not scan.get("universe_count"):
        issue("scan_empty_universe", "latest_scan", "Zero candidates with no observed universe is unverified inactivity.", evidence)
        classification = "unverified_inactivity"
    elif scan.get("blocked_count"):
        classification = "completed_with_blocks"
    sampled = local["scan_data"] or {}
    evidence["retained_snapshot_counts"] = sampled
    if classification == "healthy_zero_candidates" and (not sampled.get("n") or sampled.get("usable") != sampled["n"]):
        issue("zero_candidates_data_unverified", "latest_scan", "Zero candidates lacks a complete usable retained-data receipt.", evidence, "unknown")
        classification = "unverified_inactivity"
    matching = [r for r in local["scan_jobs"] if metadata(r.get("result_summary_json")).get("scan_summary", {}).get("scan_batch_id") == scan["scan_batch_id"]]
    if matching:
        summary = metadata(matching[0].get("result_summary_json")).get("scan_summary", {})
        if summary.get("shadow_tier_scanned"):
            coverage = number(summary.get("shadow_tier_feed_coverage"))
            evidence["shadow_feed_coverage"] = coverage
            if coverage is None or coverage < settings.shadow_label_min_feed_coverage:
                issue("scan_data_coverage", "shadow_tier", "Retained scan data coverage fails the existing shadow-label gate.", evidence, "warning")
                classification = "completed_with_data_blocks"
    result["activity"] = {"classification": classification, "evidence": evidence}


def _lineage(local, settings, issue, result):
    receipts = []
    for row in local["orders"]:
        # Exposure-reducing intents have their own execution receipt, rather
        # than an entry risk approval; never apply entry gates to exits.
        if row.get("order_class") != "bracket" and row.get("order_type") != "bracket":
            continue
        chain = local["chains"].get(row["order_id"], {})
        missing = [k for k in ("proposal", "candidate", "evaluation", "risk", "approval", "snapshot", "lineage", "card") if not chain.get(k)]
        evidence = {k: row.get(k) for k in ("order_id", "broker_order_id", "client_order_id", "candidate_id", "proposal_id", "risk_check_id", "submitted_at")}
        if missing:
            issue("lineage_missing", row["order_id"], "Entry evidence chain is incomplete.", {**evidence, "missing": missing})
        proposal, risk = chain.get("proposal") or {}, chain.get("risk") or {}
        candidate, evaluation = chain.get("candidate") or {}, chain.get("evaluation") or {}
        if risk and (risk.get("result") != "pass" or risk.get("proposal_id") != row.get("proposal_id")
                     or not same(risk.get("position_size"), row.get("qty"))
                     or not same(risk.get("entry_price"), proposal.get("entry"))
                     or not same(risk.get("stop_price"), proposal.get("stop"))
                     or not same(risk.get("target_price"), proposal.get("target"))):
            issue("risk_receipt_mismatch", row["order_id"], "Exact risk receipt does not approve the recorded entry terms.", {**evidence, "risk_result": risk.get("result")})
        if (candidate and (candidate.get("symbol") != row["symbol"] or row.get("candidate_id") != candidate["candidate_id"])) or (evaluation and (evaluation.get("candidate_id") != proposal.get("candidate_id") or evaluation.get("is_mock"))):
            issue("lineage_identity_mismatch", row["order_id"], "Candidate/evaluation identity or source is inconsistent.", evidence)
        for item in (candidate, evaluation):
            if item and item.get("lineage_id") != proposal.get("lineage_id"):
                issue("lineage_identity_mismatch", row["order_id"], "Candidate/evaluation/proposal lineage differs.", evidence)
        snapshot = chain.get("snapshot")
        if snapshot:
            reference = timeutils.parse_iso(snapshot.get("created_at_utc"))
            ages = {k: timeutils.age_seconds(snapshot.get(k), reference) if reference else None for k in ("quote_timestamp", "bar_timestamp")}
            session = snapshot.get("market_session")
            quote_limit = settings.max_quote_age_seconds_rth if session == "regular" else settings.max_quote_age_seconds_premarket
            bar_limit = settings.max_bar_age_seconds_rth if session == "regular" else settings.max_bar_age_seconds_premarket
            if snapshot.get("is_mock") or not snapshot.get("is_usable") or any(stale_age(ages[k], cap) for k, cap in (("quote_timestamp", quote_limit), ("bar_timestamp", bar_limit))):
                issue("entry_data_stale", row["order_id"], "Entry's recorded source data is stale, missing, unusable or mocked at its assessment timestamp.", {"snapshot_id": snapshot["snapshot_id"], "assessment_at_utc": timeutils.to_iso(reference) if reference else None, "source_ages_seconds": ages})
            notional = (number(row.get("qty")) or 0) * (number(row.get("intended_entry_price") or row.get("entry_price")) or 0)
            liquidity = number(snapshot.get("dollar_volume"))
            receipts.append({**evidence, "snapshot_id": snapshot["snapshot_id"], "quote_timestamp": snapshot.get("quote_timestamp"), "bar_timestamp": snapshot.get("bar_timestamp"), "lineage_id": proposal.get("lineage_id"), "source_sha256": digest(chain), "size_liquidity": {"notional": notional, "snapshot_dollar_volume": liquidity, "notional_to_snapshot_dollar_volume": notional / liquidity if liquidity and liquidity > 0 else None}})
            if liquidity is None or liquidity < settings.min_dollar_volume:
                issue("size_liquidity_warning", row["order_id"], "Liquidity is unknown or below the existing threshold; paper fills do not establish available live liquidity.", severity="warning")
    result["entry_evidence"] = receipts


def _reconcile(local, exact, open_orders, broker_positions, issue, result):
    known = set()
    by_id = {}
    for parent in open_orders + [r for r in exact.values() if r]:
        for item in [parent] + parent.get("legs", []):
            oid = item.get("broker_order_id")
            if oid:
                by_id[oid] = item
    checks = []
    for row in local["orders"]:
        norm = exact.get(row["order_id"])
        evidence = {k: row.get(k) for k in ("order_id", "broker_order_id", "client_order_id", "symbol", "qty", "state")}
        if not norm:
            issue("order_missing", row["order_id"], "Exact broker and client order lookups did not find the intended order.", evidence)
            continue
        known.add(norm["broker_order_id"])
        known.update(v["broker_order_id"] for v in norm.get("legs", []) if v.get("broker_order_id"))
        if (norm.get("broker_order_id") != row.get("broker_order_id") and row.get("broker_order_id")) or (row.get("client_order_id") and norm.get("client_order_id") != row["client_order_id"]):
            issue("order_identity_mismatch", row["order_id"], "Exact broker/client IDs disagree; symbol matching cannot substitute.", evidence)
        for field in ("symbol", "side", "time_in_force"):
            if row.get(field) and row[field] != norm.get(field):
                issue("order_terms_mismatch", row["order_id"], "Broker order terms differ from intent.", {**evidence, "field": field})
        if not same(row.get("qty"), norm.get("qty")):
            issue("order_terms_mismatch", row["order_id"], "Broker requested quantity differs from intent.", evidence)
        if row.get("limit_price") is not None and not same_limit_price(row["limit_price"], norm.get("limit_price")):
            issue("order_terms_mismatch", row["order_id"], "Broker limit price differs from intent.", {**evidence, "field": "limit_price"})
        if row.get("order_class") == "bracket":
            for role, field, broker_field in (("stop_loss", "stop_loss_price", "stop_price"), ("take_profit", "take_profit_price", "limit_price")):
                expected = row.get(field)
                if expected is not None:
                    legs = [leg for leg in norm.get("legs", []) if leg.get("role") == role]
                    if not legs or any(not same(expected, leg.get(broker_field)) for leg in legs):
                        issue("protective_terms_mismatch", row["order_id"] + ":" + role, "Exact bracket protective terms differ from the recorded exit plan.", {**evidence, "role": role, "expected_price": expected})
        if row.get("state") != norm.get("state"):
            issue("order_state_mismatch", row["order_id"], "Local lifecycle lags or disagrees with broker state.", {**evidence, "broker_state": norm.get("state"), "broker_updated_at": norm.get("updated_at")})
        qty = number(norm.get("filled_qty"))
        fills = local["fills"].get(row["order_id"], [])
        local_qty = sum(number(f.get("qty")) or 0 for f in fills)
        if qty is None or qty < 0 or not same(qty, local_qty):
            issue("fill_quantity_mismatch", row["order_id"], "Broker cumulative fill quantity disagrees with local fill receipts.", {**evidence, "broker_filled_qty": qty, "journal_filled_qty": local_qty, "broker_filled_at": norm.get("filled_at")})
        if qty and norm.get("status") == "partially_filled":
            issue("partial_fill", row["order_id"], "Partial exposure needs reconciliation and verified active protection; no order action taken.", {**evidence, "filled_qty": qty}, "warning")
        if qty and fills:
            average = sum((number(f.get("qty")) or 0) * (number(f.get("price")) or 0) for f in fills) / local_qty if local_qty else None
            if not same(average, norm.get("filled_avg_price")):
                issue("fill_price_mismatch", row["order_id"], "Broker cumulative average fill price differs from local evidence.", evidence)
        if qty and not timeutils.parse_iso(norm.get("filled_at")):
            issue("fill_timestamp_unverified", row["order_id"], "Filled quantity lacks a parseable broker source timestamp.", evidence)
        checks.append({**evidence, "broker_status": norm.get("status"), "broker_filled_qty": qty, "broker_filled_at": norm.get("filled_at"), "broker_updated_at": norm.get("updated_at")})
    for parent in open_orders:
        if parent.get("broker_order_id") not in known:
            issue("unattributed_open_order", parent.get("broker_order_id") or "unknown", "Open broker order has no exact local origin in the bounded evidence scope.", {"broker_order_id": parent.get("broker_order_id"), "client_order_id": parent.get("client_order_id"), "symbol": parent.get("symbol")})
    broker_fill_receipts = defaultdict(list)
    for fill in local["broker_fills"]:
        broker_fill_receipts[fill["broker_order_id"]].append(fill)
    parent_ids = {norm["broker_order_id"] for norm in exact.values() if norm}
    for oid, norm in by_id.items():
        if oid not in known or oid in parent_ids or not number(norm.get("filled_qty")):
            continue
        quantity = sum(number(f.get("qty")) or 0 for f in broker_fill_receipts[oid])
        if not same(quantity, norm.get("filled_qty")):
            issue("exit_fill_mismatch", oid, "Exact protective-leg cumulative fill does not reconcile to journal receipts.", {"broker_order_id": oid, "broker_filled_qty": norm.get("filled_qty"), "journal_filled_qty": quantity, "broker_filled_at": norm.get("filled_at")})
    local_qty = defaultdict(float)
    for pos in local["positions"]:
        qty = number(pos.get("qty"))
        if qty is None:
            issue("position_quantity_invalid", pos["position_id"], "Local position quantity is invalid.")
            continue
        local_qty[pos["symbol"]] += abs(qty) * (-1 if pos.get("direction") == "short" else 1)
    broker_qty = {}
    for pos in broker_positions:
        qty = number(pos.get("qty"))
        if qty is None or pos.get("symbol") in broker_qty:
            issue("broker_position_invalid", pos.get("symbol") or "unknown", "Broker positions are malformed or duplicated.")
            result["broker_verified"] = False
            continue
        broker_qty[pos["symbol"]] = -abs(qty) if pos.get("side") == "short" else qty
    for symbol in set(local_qty) | set(broker_qty):
        if not same(local_qty.get(symbol, 0), broker_qty.get(symbol, 0)):
            issue("position_quantity_mismatch", symbol, "Aggregate local origins do not reconcile to broker position quantity.", {"local_signed_qty": local_qty.get(symbol, 0), "broker_signed_qty": broker_qty.get(symbol, 0)})
    # Coverage is attributed to exact entry/exit-intent IDs. Each stop is
    # counted once per symbol, so concurrent origins cannot double-use it.
    protection: dict[tuple, dict[str, float]] = defaultdict(dict)
    targets: dict[tuple, dict[str, float]] = defaultdict(dict)
    for row in local["orders"]:
        norm = exact.get(row["order_id"])
        if not norm:
            continue
        for leg in [norm] + norm.get("legs", []):
            remaining = (number(leg.get("qty")) or 0) - (number(leg.get("filled_qty")) or 0)
            conditional_stop = (leg.get("status") == "held" and leg.get("role") == "stop_loss"
                and 'stop_loss' in verified_live_roles(norm, remaining, leg.get('symbol'), leg.get('side')))
            if leg.get("broker_order_id") in known and leg.get("stop_price") and (leg.get("status") in ACTIVE or conditional_stop):
                protection[(leg.get("symbol"), leg.get("side"))][leg["broker_order_id"]] = max(0, remaining)
            if leg.get("broker_order_id") in known and leg.get("role") == "take_profit" and leg.get("status") in ACTIVE:
                remaining = (number(leg.get("qty")) or 0) - (number(leg.get("filled_qty")) or 0)
                targets[(leg.get("symbol"), leg.get("side"))][leg["broker_order_id"]] = max(0, remaining)
    for symbol, qty in broker_qty.items():
        if not qty:
            continue
        stops = protection.get((symbol, "sell" if qty > 0 else "buy"), {})
        if sum(stops.values()) + 1e-6 < abs(qty):
            issue("missing_protective_order", symbol, "Exact active protective stop coverage is smaller than broker exposure.", {"broker_signed_qty": qty, "active_stop_ids": sorted(stops), "protected_qty": sum(stops.values())})
        expected_target_qty = sum(abs(number(pos.get("qty")) or 0) for pos in local["positions"] if pos["symbol"] == symbol and pos.get("target_price") is not None)
        profit_legs = targets.get((symbol, "sell" if qty > 0 else "buy"), {})
        if sum(profit_legs.values()) + 1e-6 < expected_target_qty:
            issue("missing_profit_order", symbol, "Exact active take-profit coverage is smaller than the local written exit plan.", {"expected_qty": expected_target_qty, "active_target_ids": sorted(profit_legs), "covered_qty": sum(profit_legs.values())})
    result["order_reconciliation"] = checks
    result["position_reconciliation"] = {"local_signed_qty": dict(local_qty), "broker_signed_qty": broker_qty}
