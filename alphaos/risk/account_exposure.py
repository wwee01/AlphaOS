"""Fresh paper-account bounds. Unknown state never becomes free buying capacity."""
from __future__ import annotations

import math
from contextlib import contextmanager
from pathlib import Path

from alphaos.execution.protection_contract import verified_live_roles
from alphaos.util import timeutils


class ExposureUnavailable(ValueError):
    pass


class ExposureUnreconciled(ExposureUnavailable):
    """A snapshot awaits reconciliation; entry remains forbidden until rechecked."""

    def __init__(self, message, *, verified_fill=None):
        super().__init__(message)
        self.verified_fill = verified_fill


class EntrySubmissionBusy(ExposureUnavailable):
    """Another local entry owns submission; no broker attempt was made."""


def _number(value, label, *, positive=False):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ExposureUnavailable(f"missing {label}") from None
    if not math.isfinite(value) or (positive and value <= 0):
        raise ExposureUnavailable(f"invalid {label}")
    return value


def summarize_exposure(account: dict, positions: list[dict], orders: list[dict], known_entries: dict, known_reductions: set | None = None) -> dict:
    known_reductions = known_reductions or set()
    equity = _number(account.get("equity"), "account equity", positive=True)
    last_equity = _number(account.get("last_equity"), "previous closing equity", positive=True)
    if account.get("trading_blocked") is not False:
        raise ExposureUnavailable("account trading permission is blocked or unknown")
    by_symbol = {}
    gross = 0.0
    for pos in positions:
        symbol = pos.get("symbol")
        qty = abs(_number(pos.get("qty"), "position quantity"))
        if not symbol or not qty or symbol in by_symbol:
            raise ExposureUnavailable("invalid or duplicate broker position")
        if pos.get("side") not in {"long", "short"}:
            raise ExposureUnavailable("unknown position side")
        value = abs(_number(pos.get("market_value"), "position market value"))
        if value <= 0:
            raise ExposureUnavailable("nonzero position has no market value")
        gross += value
        by_symbol[symbol] = pos
    pending_slots = 0
    occupied_symbols = set(by_symbol)
    pending = 0.0
    seen = set()
    for order in orders:
        oid = order.get("broker_order_id")
        if not oid or oid in seen:
            raise ExposureUnavailable("missing or duplicate open order identity")
        seen.add(oid)
        symbol = order.get("symbol")
        qty = _number(order.get("qty"), "open order quantity", positive=True)
        filled = _number(order.get("filled_qty"), "open order filled quantity")
        if not 0 <= filled <= qty:
            raise ExposureUnavailable("invalid cumulative fill quantity")
        remaining = qty - filled
        if not remaining:
            continue
        if oid in known_entries:
            row = known_entries[oid]
            if symbol != row["symbol"]:
                raise ExposureUnavailable("entry order symbol differs from ledger")
            occupied_symbols.add(symbol)
            # Limit entry remains the worst authorized notional per share.
            price = _number(order.get("limit_price"), "pending entry limit", positive=True)
            pending += remaining * price
            if filled == 0 or symbol not in by_symbol:
                pending_slots += 1
        else:
            # Only verified, reducing stop/target orders consume no new exposure.
            held = by_symbol.get(symbol)
            expected_side = "sell" if held and held.get("side") == "long" else "buy"
            if (oid not in known_reductions or not held or order.get("side") != expected_side
                    or remaining > abs(float(held["qty"]))):
                raise ExposureUnavailable("unrecognized open order could increase exposure")
            if not (order.get("stop_price") or order.get("limit_price")):
                raise ExposureUnavailable("unreconciled independent close order")
    return {"equity": equity, "gross_committed_notional": gross + pending,
            "open_positions": len(by_symbol) + pending_slots,
            "broker_position_slots": len(by_symbol), "pending_entry_slots": pending_slots,
            "occupied_symbols": sorted(occupied_symbols),
            "account_pnl_today": equity - last_equity,
            "pending_entry_notional": pending, "observed_at_utc": timeutils.stamp().utc}


def _unreconciled_entry(journal, symbol, local):
    """Find one acknowledged origin; also reused inside the booking transaction."""
    from alphaos.execution.ledger_ops import metadata
    rows = journal.query(
        "SELECT o.* FROM paper_orders o WHERE o.execution_source='alpaca_paper' "
        "AND o.order_class='bracket' AND o.symbol=? AND o.proposal_id IS NOT NULL "
        "AND o.broker_order_id IS NOT NULL AND o.state IN ('accepted','partially_filled','filled') "
        "AND NOT EXISTS (SELECT 1 FROM positions p WHERE p.order_id=o.order_id AND p.status='closed')",
        (symbol,),
    )
    eligible = [r for r in rows if metadata(r).get("purpose") == "paper_entry"
                and metadata(r).get("phase") == "acknowledged"
                and r.get("client_order_id") == r.get("proposal_id")]
    if len(eligible) != 1:
        raise ValueError("no unique acknowledged entry")
    row = eligible[0]
    if local and local.get("order_id") != row["order_id"]:
        raise ValueError("local origin differs from entry")
    return row


def _verify_unreconciled_fill(client, journal, held, local):
    """Return read-only evidence; no fill is booked and no exposure is waived."""
    from alphaos.execution.entry_submission import EntrySubmission

    try:
        row = _unreconciled_entry(journal, held.get("symbol"), local)
        proposal = journal.proposal_by_id(row["proposal_id"])
        parent = client.get_order(row["broker_order_id"])
        EntrySubmission.validate(row, parent, nested=True)
        qty = abs(_number(held.get("qty"), "held quantity"))
        if (_number(parent.get("filled_qty"), "filled quantity", positive=True) != qty
                or held.get("side") != ("short" if row["direction"] == "short" else "long")):
            raise ValueError("entry fill does not explain holding")
        side = "buy" if row["direction"] == "short" else "sell"
        if verified_live_roles(parent, qty, held["symbol"], side) != {"stop_loss", "take_profit"}:
            raise ValueError("entry protection is not verified")
        return {"order": row, "parent": parent, "position": local, "proposal": proposal}
    except Exception as exc:
        raise ExposureUnavailable("unreconciled position lacks verified entry and protection evidence") from exc


def read_exposure(client, journal) -> dict:
    if client is None:
        raise ExposureUnavailable("paper broker is unavailable")
    # The process may have died after writing its intent but before writing
    # an incident or receiving the POST response. Absence of a broker order
    # from a subsequent open-order list cannot prove it was never accepted.
    from alphaos.execution.entry_submission import pending_entry_intent
    try:
        unresolved = pending_entry_intent(journal)
    except Exception as exc:
        raise ExposureUnavailable("paper entry intent state cannot be verified") from exc
    if unresolved:
        raise ExposureUnavailable("paper entry submission remains unresolved; reconcile its client ID first")
    account = client.get_account()
    positions = client.list_positions()
    orders = client.list_open_orders()
    refreshed_positions = client.list_positions()
    def quantities(rows):
        return sorted((r.get("symbol"), r.get("side"), r.get("qty")) for r in rows)
    if quantities(positions) != quantities(refreshed_positions):
        raise ExposureUnreconciled("broker position changed during exposure check; retry after reconciliation")
    positions = refreshed_positions
    local_positions = journal.open_positions()
    local_by_symbol: dict[str, list[dict]] = {}
    for pos in local_positions:
        local_by_symbol.setdefault(pos["symbol"], []).append(pos)
    known_reductions = set()
    for held in positions:
        origins = local_by_symbol.get(held.get("symbol"), [])
        if len(origins) > 1:
            from alphaos.execution.position_attribution import verify_symbol_attribution
            try:
                proof = verify_symbol_attribution(journal, client, held["symbol"], allow_pending_entries=True)
                if abs(proof["broker_position"]["qty"] - abs(float(held["qty"]))) > 1e-6:
                    raise ValueError("aggregate changed during exposure check")
            except Exception as exc:
                raise ExposureUnavailable(f"origin attribution unavailable: {exc}") from exc
            known_reductions.update(proof["verified_protection_ids"])
            continue
        local = origins[0] if origins else None
        if not local or abs(float(local["qty"])) != abs(float(held["qty"])):
            proof = _verify_unreconciled_fill(client, journal, held, local)
            raise ExposureUnreconciled("verified entry fill awaits local position reconciliation",
                                      verified_fill=proof)
        if not local.get("broker_order_id"):
            raise ExposureUnavailable("position lacks broker protection lineage")
        parent = client.get_order(local["broker_order_id"])
        expected_side = "sell" if held["side"] == "long" else "buy"
        if parent.get("order_class") == "oco":
            known_reductions.add(parent["broker_order_id"])
        for leg in parent.get("legs", []):
            if leg.get("role") in {"stop_loss", "take_profit"}:
                known_reductions.add(leg["broker_order_id"])
        live_roles = verified_live_roles(parent, abs(float(held["qty"])), held["symbol"], expected_side)
        if not {"stop_loss", "take_profit"} <= live_roles:
            raise ExposureUnavailable("current matching stop and target protection is not verified")
    known = {r["broker_order_id"]: r for r in journal.query(
        "SELECT broker_order_id, symbol, state FROM paper_orders WHERE proposal_id IS NOT NULL "
        "AND broker_order_id IS NOT NULL AND order_class = 'bracket'"
    )}
    result = summarize_exposure(account, positions, orders, known, known_reductions)
    # A filled but not yet reconciled position does not erase a slot; broker
    # reality covers missing local fills, local records cover unresolved exits.
    result["open_positions"] = max(result["broker_position_slots"], journal.count_open_positions()) + result["pending_entry_slots"]
    # A local accepted entry remains reserved until reconciliation makes it
    # terminal, even if an intermediate broker open-order list omits it.
    terminal = {"filled", "rejected", "cancelled", "canceled", "expired", "failed"}
    pending_symbols = {r["symbol"] for r in known.values() if r.get("state") not in terminal}
    result["occupied_symbols"] = sorted(set(result["occupied_symbols"]) | set(local_by_symbol) | pending_symbols)
    return result


@contextmanager
def entry_submission_lock(journal):
    """Serialize the final fresh check and submission across local writers.

    A file lock releases on crash. Do not hold a SQLite write lock during a
    broker request. Only new-entry callers use this, never protective exits.
    """
    import fcntl
    if journal.db_path in {":memory:", ""}:
        raise ExposureUnavailable("paper entry submission requires a durable ledger")
    path = Path(journal.db_path).resolve().with_suffix(".entry.lock")
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise EntrySubmissionBusy("another entry submission is in progress") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
