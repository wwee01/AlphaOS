"""One bounded paper-observer poll; no scheduler, orders or AI construction."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from alphaos.config.settings import load_settings
from alphaos.journal.journal_store import JournalStore
from alphaos.monitoring.broker_reads import PaperReads, paper_guard
from alphaos.monitoring.incidents import record_transitions
from alphaos.monitoring.observer import ledger_snapshot, observe
from alphaos.util import timeutils


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger-only", action="store_true", help="Offline local evidence; never claims broker verification")
    parser.add_argument("--expected-account-id", help="Independent pin from the existing Alpaca paper account; never learned automatically")
    parser.add_argument("--state-path", help="Optional observer-only incident JSON; absence makes this poll stateless")
    parser.add_argument("--notify", action="store_true", help="Explicitly send transitions through existing ntfy; requires --state-path")
    args = parser.parse_args(argv)
    if not args.ledger_only and not args.expected_account_id:
        parser.error("Broker polling requires --expected-account-id")
    if args.notify and not args.state_path:
        parser.error("--notify requires --state-path")
    broker = None
    try:
        settings = load_settings()
        paper_guard(settings)
        if args.state_path:
            ledger_path = Path(settings.db_path).resolve()
            state_path = Path(args.state_path).resolve()
            # Never overwrite SQLite/WAL, env, strategy or existing monitor state.
            if not state_path.name.endswith(".paper-observer.json") or state_path == ledger_path:
                raise ValueError("state_path_must_end_in_paper_observer_json")
        now = timeutils.now_utc()
        journal = JournalStore(settings.db_path, read_only=True)
        try:
            local = ledger_snapshot(journal, settings, now)
        finally:
            journal.close()
        if not args.ledger_only:
            broker = PaperReads(settings, args.expected_account_id)
        report = observe(local, settings, broker, now)
        report["completed_at_utc"] = timeutils.to_iso(timeutils.now_utc())
        if args.state_path:
            transition = record_transitions(report, args.state_path, settings, args.notify)
            report["incident_transitions"] = transition
            if transition["active_count"] or transition["pending_count"]:
                report.update(ok=False, status="attention_required")
        print(json.dumps(report, sort_keys=True, allow_nan=False))
        return 0 if report["ok"] else 1
    except Exception as exc:
        print(json.dumps({"status": "observation_failed", "ok": False, "error_type": type(exc).__name__}))
        return 2
    finally:
        if broker:
            broker.close()


if __name__ == "__main__":
    raise SystemExit(main())
