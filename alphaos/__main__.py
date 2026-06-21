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
    sub.add_parser("scan_once", help="run one scan/propose pass")
    sub.add_parser("monitor_once", help="run one watchdog/exit pass")
    sub.add_parser("generate_daily_report", help="generate today's learning report")
    sub.add_parser("status", help="show mode/safety/startup status")
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
        if args.command == "monitor_once":
            return cmd_monitor_once(orch)
        if args.command == "generate_daily_report":
            return cmd_generate_daily_report(orch)
        if args.command == "status":
            return cmd_status(orch)
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
