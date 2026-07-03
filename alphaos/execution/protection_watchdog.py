"""Broker protection watchdog (docs/roadmap/protection-watchdog.md).

For every open, broker-managed (``execution_source=alpaca_paper``) position,
verifies:
  1. the position still exists at the broker (vs. the local ledger believing
     it's open),
  2. its stop-loss leg is live (working/open) at the broker,
  3. its take-profit leg is live (working/open) at the broker,
  4. the recorded protective time_in_force is appropriate for the position's
     intended holding period.

Missing STOP -> ``protection_status=unprotected``, CRITICAL, blocks all new
entries. Missing TARGET only (stop still live) -> ``degraded``, WARNING, does
NOT block. A position open locally but absent at the broker (closed via some
path other than a bracket-leg fill -- the exact gap ``OrderManager.reconcile()``
has) -> ``closed_mismatch``, CRITICAL, blocks all new entries.

Pure detection + recording. This module NEVER calls ``close_position()``,
re-submits, or cancels a broker order on its own -- only the explicitly
human-triggered ``resolve_incident()``/``acknowledge_incident()`` touch
anything, and only ``resolve_incident()`` ever closes a position (via the
same ``PositionManager.close_position()`` every other exit already uses, with
an operator-confirmed price -- never raw SQL, never a guessed price).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from alphaos.constants import ExecutionSource, OrderState, ProtectionStatus, Severity
from alphaos.util import timeutils
from alphaos.util.ids import new_id

_LIVE_LEG_STATES = {OrderState.ACCEPTED.value, OrderState.SUBMITTED.value, OrderState.PARTIALLY_FILLED.value}
_INCIDENT_STATUSES = (ProtectionStatus.UNPROTECTED.value, ProtectionStatus.CLOSED_MISMATCH.value)


@dataclass
class ProtectionCheckResult:
    position_id: str
    symbol: str
    protection_status: str
    severity: str
    detail: str
    stop_live: Optional[bool] = None
    target_live: Optional[bool] = None
    broker_position_exists: Optional[bool] = None
    qty_match: Optional[bool] = None
    broker_qty: Optional[float] = None
    time_in_force: Optional[str] = None
    tif_appropriate: Optional[bool] = None
    incident_id: Optional[str] = None  # set by _record_check only when a NEW incident opened this pass


def _recorded_tif(journal, order_id: Optional[str]) -> Optional[str]:
    if not order_id:
        return None
    row = journal.one("SELECT time_in_force FROM paper_orders WHERE order_id = ?", (order_id,))
    return (row or {}).get("time_in_force")


def check_position(journal, alpaca_client, position: dict,
                   broker_positions_by_symbol: Optional[dict], settings) -> ProtectionCheckResult:
    """Check ONE open, broker-managed position. Never raises -- any broker-lookup
    failure yields ``check_error`` (logged WARNING elsewhere), never a false
    ``unprotected`` verdict from a mere network hiccup."""
    symbol = position["symbol"]
    position_id = position["position_id"]

    if broker_positions_by_symbol is None:
        return ProtectionCheckResult(
            position_id=position_id, symbol=symbol, protection_status=ProtectionStatus.CHECK_ERROR.value,
            severity=Severity.WARNING.value,
            detail=f"{symbol}: could not fetch broker positions this pass; protection status unknown.",
        )

    broker_pos = broker_positions_by_symbol.get(symbol)
    if broker_pos is None:
        return ProtectionCheckResult(
            position_id=position_id, symbol=symbol, protection_status=ProtectionStatus.CLOSED_MISMATCH.value,
            severity=Severity.CRITICAL.value, broker_position_exists=False,
            detail=(f"{symbol}: local ledger shows this position OPEN, but the broker has no matching "
                    f"position -- it closed via a path this system didn't observe (manual flatten, "
                    f"external close, etc). Resolve via `alphaos protection_resolve`."),
        )

    broker_qty = broker_pos.get("qty")
    local_qty = position.get("qty")
    qty_match = (
        abs(float(local_qty) - float(broker_qty)) < 1e-6
        if (local_qty is not None and broker_qty is not None) else None
    )

    try:
        norm = alpaca_client.get_order(position.get("broker_order_id"))
    except Exception as exc:  # noqa: BLE001 - broker/network failure must never propagate
        return ProtectionCheckResult(
            position_id=position_id, symbol=symbol, protection_status=ProtectionStatus.CHECK_ERROR.value,
            severity=Severity.WARNING.value, broker_position_exists=True, qty_match=qty_match,
            broker_qty=broker_qty, detail=f"{symbol}: broker order lookup failed ({exc}).",
        )

    legs = norm.get("legs") or []
    stop_leg = next((l for l in legs if l.get("role") == "stop_loss"), None)
    target_leg = next((l for l in legs if l.get("role") == "take_profit"), None)
    stop_live = bool(stop_leg) and stop_leg.get("state") in _LIVE_LEG_STATES
    target_live = bool(target_leg) and target_leg.get("state") in _LIVE_LEG_STATES

    observed_tif = (
        norm.get("time_in_force")
        or (stop_leg or {}).get("time_in_force")
        or (target_leg or {}).get("time_in_force")
        or _recorded_tif(journal, position.get("order_id"))
    )
    tif_appropriate = None
    if observed_tif is not None:
        is_multiday = (position.get("max_holding_days") or 0) > 1
        tif_appropriate = not (
            is_multiday and observed_tif == "day" and not settings.allow_day_tif_for_multiday_positions
        )

    if not stop_live:
        status, severity = ProtectionStatus.UNPROTECTED.value, Severity.CRITICAL.value
        detail = (f"{symbol}: stop-loss protective order is MISSING at the broker "
                  f"(state={stop_leg.get('state') if stop_leg else 'no leg found'}) -- "
                  f"position has NO downside protection.")
    elif not target_live:
        status, severity = ProtectionStatus.DEGRADED.value, Severity.WARNING.value
        detail = (f"{symbol}: take-profit protective order is missing at the broker "
                  f"(state={target_leg.get('state') if target_leg else 'no leg found'}); "
                  f"stop is still live, downside remains protected.")
    else:
        status, severity = ProtectionStatus.PROTECTED.value, Severity.INFO.value
        detail = f"{symbol}: stop and target both live at the broker."

    return ProtectionCheckResult(
        position_id=position_id, symbol=symbol, protection_status=status, severity=severity, detail=detail,
        stop_live=stop_live, target_live=target_live, broker_position_exists=True,
        qty_match=qty_match, broker_qty=broker_qty, time_in_force=observed_tif, tif_appropriate=tif_appropriate,
    )


def _record_check(journal, position: dict, result: ProtectionCheckResult, scheduler_run_id: Optional[str]) -> None:
    """Insert this pass's audit row, update positions.protection_status, and
    open/auto-resolve incidents as needed. A 'new' incident (CRITICAL log +
    counted) is only raised on a transition INTO unprotected/closed_mismatch --
    if an unresolved incident already exists for this position, the audit row
    is still written but no duplicate alert fires. A DEGRADED transition gets
    the same one-time (not every pass) WARNING treatment -- 'loud, never
    silent' per Part B, without spamming an alert on every subsequent pass
    while it stays degraded."""
    prior = journal.one(
        "SELECT protection_status FROM protection_checks WHERE position_id = ? ORDER BY id DESC LIMIT 1",
        (position["position_id"],),
    )
    is_incident_type = result.protection_status in _INCIDENT_STATUSES
    existing_open = None
    if is_incident_type:
        existing_open = journal.one(
            "SELECT check_id FROM protection_checks WHERE position_id = ? "
            "AND protection_status IN (?, ?) AND resolved_at_utc IS NULL ORDER BY id DESC LIMIT 1",
            (position["position_id"], *_INCIDENT_STATUSES),
        )
    is_new_incident = is_incident_type and existing_open is None
    is_new_degraded = (
        result.protection_status == ProtectionStatus.DEGRADED.value
        and (prior is None or prior.get("protection_status") != ProtectionStatus.DEGRADED.value)
    )

    check_id = new_id("pcheck")
    journal.insert("protection_checks", {
        "check_id": check_id,
        "position_id": result.position_id,
        "symbol": result.symbol,
        "trade_id": position.get("trade_id"),
        "protection_status": result.protection_status,
        "broker_position_exists": result.broker_position_exists,
        "local_qty": position.get("qty"),
        "broker_qty": result.broker_qty,
        "qty_match": result.qty_match,
        "stop_live": result.stop_live,
        "target_live": result.target_live,
        "time_in_force": result.time_in_force,
        "tif_appropriate": result.tif_appropriate,
        "dangling_orders_json": [],
        "severity": result.severity,
        "detail": result.detail,
        "scheduler_run_id": scheduler_run_id,
    })

    if is_new_incident:
        result.incident_id = check_id
        journal.log_system_event(
            result.severity, "protection_watchdog", result.detail,
            {"check_id": check_id, "position_id": result.position_id, "symbol": result.symbol},
        )
    elif is_new_degraded:
        journal.log_system_event(
            result.severity, "protection_watchdog", result.detail,
            {"check_id": check_id, "position_id": result.position_id, "symbol": result.symbol},
        )

    journal.conn.execute(
        "UPDATE positions SET protection_status = ? WHERE position_id = ?",
        (result.protection_status, result.position_id),
    )
    journal.conn.commit()

    # Self-healing: PROTECTED or DEGRADED both mean the blocking condition (a
    # missing STOP) is gone -- auto-resolve any still-open incident. Deliberately
    # an allowlist (not "not unprotected/closed_mismatch") so a transient
    # check_error can never accidentally auto-clear a real incident.
    if result.protection_status in (ProtectionStatus.PROTECTED.value, ProtectionStatus.DEGRADED.value):
        st = timeutils.stamp()
        journal.conn.execute(
            "UPDATE protection_checks SET resolved_at_utc = ?, resolved_by = ?, resolution_note = ? "
            "WHERE position_id = ? AND protection_status IN (?, ?) AND resolved_at_utc IS NULL",
            (st.utc, "watchdog_reconfirmed", "protection confirmed restored on a later watchdog pass",
             result.position_id, *_INCIDENT_STATUSES),
        )
        journal.conn.commit()


def _open_incident_count(journal) -> int:
    row = journal.one(
        "SELECT COUNT(*) AS n FROM protection_checks WHERE protection_status IN (?, ?) "
        "AND resolved_at_utc IS NULL",
        _INCIDENT_STATUSES,
    )
    return int((row or {}).get("n") or 0)


def run_watchdog_pass(journal, alpaca_client, settings, scheduler_run_id: Optional[str] = None) -> dict:
    """Iterate every open, broker-managed position; check + record each. No-ops
    (all-zero summary) when there's no real paper broker connected, mirroring
    OrderManager.reconcile()'s own guard -- same condition, since alpaca_client
    is only ever constructed when broker_connected."""
    counts = {"checked": 0, "protected": 0, "degraded": 0, "unprotected": 0,
             "closed_mismatch": 0, "check_error": 0, "new_incidents": [], "dangling_orders": []}
    if not (settings.real_paper_execution and alpaca_client is not None):
        counts["open_incident_count"] = _open_incident_count(journal)
        return counts

    open_positions = [
        p for p in journal.open_positions() if p.get("execution_source") == ExecutionSource.ALPACA_PAPER.value
    ]
    if not open_positions:
        counts["open_incident_count"] = _open_incident_count(journal)
        return counts

    try:
        broker_positions = alpaca_client.list_positions()
        broker_by_symbol = {p["symbol"]: p for p in broker_positions}
    except Exception as exc:  # noqa: BLE001 - broker/network failure must never propagate
        journal.log_system_event(
            Severity.WARNING, "protection_watchdog", f"list_positions failed this pass: {exc}."
        )
        broker_by_symbol = None

    for pos in open_positions:
        result = check_position(journal, alpaca_client, pos, broker_by_symbol, settings)
        _record_check(journal, pos, result, scheduler_run_id)
        counts["checked"] += 1
        counts[result.protection_status] += 1
        if result.incident_id:
            counts["new_incidents"].append(result.incident_id)

    try:
        broker_orders = alpaca_client.list_open_orders()
        local_symbols = {p["symbol"] for p in open_positions}
        dangling = [o for o in broker_orders if o.get("symbol") not in local_symbols]
        if dangling:
            journal.log_system_event(
                Severity.WARNING, "protection_watchdog",
                f"{len(dangling)} dangling broker order(s) with no matching open local position.",
                {"dangling": dangling},
            )
        counts["dangling_orders"] = dangling
    except Exception as exc:  # noqa: BLE001 - broker/network failure must never propagate
        journal.log_system_event(
            Severity.WARNING, "protection_watchdog", f"list_open_orders failed this pass: {exc}."
        )

    counts["open_incident_count"] = _open_incident_count(journal)
    return counts


def has_blocking_incident(journal) -> Optional[dict]:
    """Cheap, targeted, single-row read: the most relevant OPEN incident, or
    None. Used on the hot path (every proposal execution) -- mirrors
    KillSwitch.is_engaged()'s role, but DB-backed since it needs structured
    detail (which position, why), not just a boolean."""
    return journal.one(
        "SELECT * FROM protection_checks WHERE protection_status IN (?, ?) AND resolved_at_utc IS NULL "
        "ORDER BY id DESC LIMIT 1",
        _INCIDENT_STATUSES,
    )


def resolve_incident(journal, position_manager, incident_id: str, exit_price: float,
                     note: str, resolved_by: str = "user") -> dict:
    """ONLY for a closed_mismatch incident (local open / broker closed). Calls
    position_manager.close_position() -- the SAME path every other exit uses,
    never raw SQL -- with an operator-confirmed price. Never guesses a price."""
    row = journal.one("SELECT * FROM protection_checks WHERE check_id = ?", (incident_id,))
    if not row:
        return {"ok": False, "message": f"incident {incident_id} not found"}
    if row.get("resolved_at_utc"):
        return {"ok": False, "message": f"incident {incident_id} already resolved"}
    if row["protection_status"] != ProtectionStatus.CLOSED_MISMATCH.value:
        return {"ok": False, "message": (
            f"incident {incident_id} is '{row['protection_status']}', not a local-open/broker-closed "
            f"mismatch -- use protection_ack instead (no position close needed)."
        )}
    pos = journal.one("SELECT * FROM positions WHERE position_id = ?", (row["position_id"],))
    if not pos or pos.get("status") != "open":
        return {"ok": False, "message": "position is not open locally -- nothing to resolve"}

    exit_row = position_manager.close_position(
        row["position_id"], float(exit_price), "broker_protection_incident_manual_resolve",
        triggered_by=resolved_by, execution_source=ExecutionSource.ALPACA_PAPER.value,
    )
    if exit_row is None:
        return {"ok": False, "message": "close_position() declined (position no longer open) -- re-check protection_status"}

    st = timeutils.stamp()
    journal.conn.execute(
        "UPDATE protection_checks SET resolved_at_utc = ?, resolved_by = ?, resolution_note = ?, "
        "resolution_exit_id = ? WHERE check_id = ?",
        (st.utc, resolved_by, note, exit_row.get("exit_id"), incident_id),
    )
    journal.conn.commit()
    journal.log_system_event(
        Severity.WARNING, "protection_watchdog",
        f"Protection incident {incident_id} resolved by {resolved_by}: {row['symbol']} closed @ {exit_price}.",
        {"incident_id": incident_id, "exit": exit_row},
    )
    return {"ok": True, "message": f"resolved; position closed @ {exit_price}", "exit": exit_row}


def acknowledge_incident(journal, incident_id: str, note: str, resolved_by: str = "user") -> dict:
    """ONLY for an unprotected/degraded incident. Marks it resolved WITHOUT
    touching the position -- for when a human has manually restored protection
    directly at the broker, or explicitly chooses to accept the risk and unblock
    other trades. Never calls close_position(). This is the explicit
    'require user decision' path; the watchdog also self-heals these
    automatically once it reconfirms protection on a later pass (see
    _record_check) -- this is for when a human wants to unblock sooner."""
    row = journal.one("SELECT * FROM protection_checks WHERE check_id = ?", (incident_id,))
    if not row:
        return {"ok": False, "message": f"incident {incident_id} not found"}
    if row.get("resolved_at_utc"):
        return {"ok": False, "message": f"incident {incident_id} already resolved"}
    if row["protection_status"] not in (ProtectionStatus.UNPROTECTED.value, ProtectionStatus.DEGRADED.value):
        return {"ok": False, "message": (
            f"incident {incident_id} is '{row['protection_status']}' -- use protection_resolve for a "
            f"local-open/broker-closed mismatch (it requires closing the position with a confirmed price)."
        )}
    st = timeutils.stamp()
    journal.conn.execute(
        "UPDATE protection_checks SET resolved_at_utc = ?, resolved_by = ?, resolution_note = ? WHERE check_id = ?",
        (st.utc, resolved_by, note, incident_id),
    )
    journal.conn.commit()
    journal.log_system_event(
        Severity.WARNING, "protection_watchdog",
        f"Protection incident {incident_id} acknowledged by {resolved_by} (position NOT closed): {note}",
        {"incident_id": incident_id},
    )
    return {"ok": True, "message": "acknowledged; new-entry block lifted for this incident"}


def status_report(journal) -> dict:
    """Read-only summary for BOTH system_health()'s dashboard/CLI surface and
    the protection_status CLI command."""
    open_incidents = journal.query(
        "SELECT * FROM protection_checks WHERE protection_status IN (?, ?) AND resolved_at_utc IS NULL "
        "ORDER BY id DESC",
        _INCIDENT_STATUSES,
    )
    # Latest check per currently-open broker-managed position.
    latest = journal.query(
        "SELECT pc.* FROM protection_checks pc "
        "JOIN (SELECT position_id, MAX(id) AS max_id FROM protection_checks GROUP BY position_id) m "
        "ON pc.position_id = m.position_id AND pc.id = m.max_id "
        "JOIN positions p ON p.position_id = pc.position_id "
        "WHERE p.status = 'open'"
    )
    counts = {"protected": 0, "degraded": 0, "unprotected": 0, "closed_mismatch": 0, "check_error": 0}
    for row in latest:
        s = row.get("protection_status")
        if s in counts:
            counts[s] += 1

    blocking = len(open_incidents) > 0
    checked = sum(counts.values())
    if blocking:
        summary_label = f"BLOCKED -- {len(open_incidents)} open incident(s)"
        blocking_detail = open_incidents[0]["detail"]
    elif counts["degraded"] > 0:
        summary_label = f"{counts['degraded']} degraded (not blocking)"
        blocking_detail = None
    elif checked > 0:
        summary_label = "all protected"
        blocking_detail = None
    else:
        summary_label = "no broker-managed positions"
        blocking_detail = None

    return {
        "checked": checked,
        **counts,
        "open_incidents": open_incidents,
        "open_incident_count": len(open_incidents),
        "blocking": blocking,
        "blocking_detail": blocking_detail,
        "summary_label": summary_label,
    }
