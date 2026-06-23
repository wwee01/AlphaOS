"""AlphaOS one-shot CLI runners.

    python -m alphaos scan_once
    python -m alphaos monitor_once
    python -m alphaos generate_daily_report

Plus helpers: status, seed_demo, kill (engage/release), dashboard (hint).
These are one-shot commands, not a daemon (no scheduler in v1).
"""

from __future__ import annotations

import argparse
import json
import sys

from alphaos.config.settings import SettingsError, load_settings
from alphaos.orchestrator import Orchestrator
from alphaos.safety import KillSwitch


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_scan_once(orch: Orchestrator) -> int:
    summary = orch.run_scan_once()
    _print({"scan_once": summary.as_dict()})
    return 0


def cmd_monitor_once(orch: Orchestrator) -> int:
    _print({"monitor_once": orch.run_monitor_once()})
    return 0


def cmd_generate_daily_report(orch: Orchestrator) -> int:
    rep = orch.generate_daily_report()
    print(rep["content_md"])
    return 0


def cmd_interest_scan(orch: Orchestrator) -> int:
    """Roadmap 2.3: interest scan -> candidate packets -> AI category labels ->
    existing gates -> proposals (manual approval still required; no auto-exec)."""
    _print({"interest_scan": orch.run_scan_once().as_dict()})
    return 0


def cmd_proposals(orch: Orchestrator) -> int:
    views = orch.list_open_proposals()
    _print({"open_proposals": views, "count": len(views)})
    return 0


def cmd_approve(orch: Orchestrator, proposal_id: str, approve_margin: bool) -> int:
    ok, msg = orch.approve_proposal(proposal_id, approver="cli", approve_margin=approve_margin)
    _print({"approve": {"proposal_id": proposal_id, "ok": ok, "message": msg}})
    return 0 if ok else 1


def cmd_reject(orch: Orchestrator, proposal_id: str, reason: str) -> int:
    ok, msg = orch.reject_proposal(proposal_id, approver="cli", reason=reason)
    _print({"reject": {"proposal_id": proposal_id, "ok": ok, "message": msg}})
    return 0 if ok else 1


def cmd_calibration_report(orch: Orchestrator) -> int:
    from alphaos.reports.cost_calibration import render_markdown

    rep = orch.calibration_report()
    print(render_markdown(rep))
    print()
    _print({"calibration": rep["summary"], "recommended_model": rep["recommended_model"]})
    return 0


def cmd_flatten(orch: Orchestrator) -> int:
    res = orch.flatten_paper_account()
    _print({"flatten": res})
    return 0 if res.get("ok") else 1


def cmd_reconcile_report(orch: Orchestrator) -> int:
    _print({"broker_ledger_reconciliation": orch.broker_ledger_report()})
    return 0


def cmd_status(orch: Orchestrator) -> int:
    checks = orch.startup()
    _print(
        {
            "mode": orch.settings.mode.value,
            "real_trading_enabled_raw": orch.settings.real_trading_enabled_raw,
            "real_trading_value_ok": orch.settings.real_trading_value_ok,
            "system_health": orch.system_health(),
            "startup_checks": [c.as_dict() for c in checks],
        }
    )
    return 0


def cmd_seed_demo(orch: Orchestrator) -> int:
    _print({"seed_demo": orch.seed_demo()})
    return 0


def cmd_kill(orch: Orchestrator, action: str) -> int:
    ks = KillSwitch()
    if action == "engage":
        ks.engage("cli")
        orch.journal.log_system_event("critical", "kill_switch", "Kill switch ENGAGED via CLI.")
    elif action == "release":
        ks.release()
        orch.journal.log_system_event("warning", "kill_switch", "Kill switch RELEASED via CLI.")
    _print({"kill_switch_engaged": ks.is_engaged()})
    return 0


def cmd_dashboard(_: Orchestrator) -> int:
    print("Run the dashboard with:\n  streamlit run alphaos/dashboard/streamlit_app.py")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m alphaos", description="AlphaOS v1 CLI")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("scan_once", help="run one scan/propose pass (legacy alias of interest_scan)")
    sub.add_parser("interest_scan", help="interest scan -> packet -> AI label -> propose (manual approval)")
    sub.add_parser("monitor_once", help="run one watchdog/exit pass")
    sub.add_parser("generate_daily_report", help="generate today's learning report")
    sub.add_parser("status", help="show mode/safety/startup status")
    sub.add_parser("proposals", help="list open proposals awaiting approval")
    ap = sub.add_parser("approve", help="approve + submit a proposal (paper); re-checks safety/risk/freshness")
    ap.add_argument("proposal_id")
    ap.add_argument("--margin", action="store_true", help="explicitly approve margin/borrow for a short")
    rj = sub.add_parser("reject", help="reject a proposal (removes it from the actionable queue)")
    rj.add_argument("proposal_id")
    rj.add_argument("--reason", default="cli rejected")
    sub.add_parser("calibration_report", help="cost-model calibration: modeled vs actual paper execution")
    sub.add_parser("flatten", help="PAPER-ONLY: cancel open Alpaca paper orders + close paper positions")
    sub.add_parser("reconcile_report", help="broker-vs-ledger reconciliation (detect orphans/mismatches)")
    sub.add_parser("seed_demo", help="create a labelled demo trade (exec/journal/dashboard demo)")
    sub.add_parser("dashboard", help="how to launch the Streamlit dashboard")
    kill = sub.add_parser("kill", help="engage/release the kill switch")
    kill.add_argument("action", choices=["engage", "release"])
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    orch = Orchestrator(settings=settings)
    try:
        if args.command == "scan_once":
            return cmd_scan_once(orch)
        if args.command == "interest_scan":
            return cmd_interest_scan(orch)
        if args.command == "monitor_once":
            return cmd_monitor_once(orch)
        if args.command == "generate_daily_report":
            return cmd_generate_daily_report(orch)
        if args.command == "status":
            return cmd_status(orch)
        if args.command == "proposals":
            return cmd_proposals(orch)
        if args.command == "approve":
            return cmd_approve(orch, args.proposal_id, args.margin)
        if args.command == "reject":
            return cmd_reject(orch, args.proposal_id, args.reason)
        if args.command == "calibration_report":
            return cmd_calibration_report(orch)
        if args.command == "flatten":
            return cmd_flatten(orch)
        if args.command == "reconcile_report":
            return cmd_reconcile_report(orch)
        if args.command == "seed_demo":
            return cmd_seed_demo(orch)
        if args.command == "dashboard":
            return cmd_dashboard(orch)
        if args.command == "kill":
            return cmd_kill(orch, args.action)
        return 1
    finally:
        orch.close()


if __name__ == "__main__":
    raise SystemExit(main())
