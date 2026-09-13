"""Read-only production evidence for N4 (2026-09-14): does the round-four attribution accept the ACTUAL holdings?

Runs `verify_entry_absence` for a synthetic failed add-on intent inserted into a SCRATCH COPY of the
production ledger, against the real paper broker using read-only calls only (list_positions, get_order,
list_open_orders). Never writes production. Result on 2026-09-14 ~00:30 SGT at 208544b:

  META ATTRIBUTION OK -> attributed pos_b4026419eeee qty 27, protection bae2cf1c…, verified ids [stop 4216c401…, tp b1480270…, parent]
  AVGO ATTRIBUTION OK -> attributed pos_996e6b0d29a1 qty 43, protection da49fa35…, verified ids [tp 109bee77…, parent, stop fb6f3f9b…]

Usage (only ever against a copy):  ALPHAOS_SCRATCH_DB=/path/to/copy.db python this_file.py
"""
import json, os, sys

from alphaos.broker.alpaca_client import AlpacaClient
from alphaos.config.settings import load_settings
from alphaos.execution.entry_recovery_exposure import verify_entry_absence
from alphaos.journal.journal_store import JournalStore
from alphaos.util import timeutils

db = os.environ.get("ALPHAOS_SCRATCH_DB", "")
assert db and "scratch" in db.lower() and "alphaos.db" not in os.path.basename(db), "refusing: not a scratch copy"
broker = AlpacaClient(load_settings())
j = JournalStore(db)
for sym, qty, entry, stop, target in (("META", 5, 610.0, 580.0, 660.0), ("AVGO", 5, 370.0, 350.0, 400.0)):
    oid = f"ord_probe_{sym.lower()}"
    j.insert("paper_orders", {
        "order_id": oid, "client_order_id": f"prop_probe_{sym.lower()}", "symbol": sym, "direction": "long",
        "side": "buy", "qty": qty, "order_type": "bracket", "order_class": "bracket", "state": "submitted",
        "execution_source": "alpaca_paper", "execution_provider": "alpaca_paper", "execution_mode": "alpaca_paper",
        "limit_price": entry, "entry_price": entry, "take_profit_price": target, "stop_loss_price": stop,
        "time_in_force": "gtc", "proposal_id": f"prop_probe_{sym.lower()}", "submitted_at": timeutils.stamp().utc,
        "raw_request_json": {"purpose": "paper_entry", "proposal_id": f"prop_probe_{sym.lower()}", "phase": "unknown",
                             "dispatch_version": 1, "dispatch_started_at": timeutils.stamp().utc},
    })
    try:
        ev = verify_entry_absence(j, broker, j.order_by_id(oid))
        print(sym, "ATTRIBUTION OK ->", json.dumps({k: ev[k] for k in ("attributed_positions", "broker_position", "verified_protection_ids")}, default=str))
    except Exception as exc:
        print(sym, "REFUSED ->", type(exc).__name__, exc)
j.close()
