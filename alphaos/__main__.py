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


def cmd_attribution_report(orch: Orchestrator) -> int:
    """User-override attribution learning report (heuristic; never a significance
    claim). PURE READ — no execution, no ledger writes."""
    from alphaos.reports.attribution import render_markdown

    rep = orch.attribution_report()
    print(render_markdown(rep))
    print()
    _print({"attribution_report": rep})
    return 0


def cmd_backfill_mfe_mae(orch: Orchestrator) -> int:
    """Backfill MFE/MAE on closed trades recorded before intra-trade excursion
    tracking existed. Idempotent; write-only to trade_outcomes.mfe/.mae/
    .mfe_mae_source; no exit/order/execution behavior change."""
    res = orch.backfill_mfe_mae()
    _print({"backfill_mfe_mae": res})
    return 0


def cmd_outcomes_update(orch: Orchestrator) -> int:
    """Counterfactual outcome tracker: seed + resolve candidate_outcomes rows
    (candidates/proposals/rejects/armed-watch/user-overrides) with 1/3/5-day
    forward returns + bracket replay. PURE MEASUREMENT — no execution/approval
    change; idempotent."""
    res = orch.outcomes_update()
    _print({"outcomes_update": res})
    return 0


def cmd_outcomes_report(orch: Orchestrator) -> int:
    """Measurement-visibility summary over candidate_outcomes. No statistical
    claims — always surfaces a small-sample caveat."""
    from alphaos.reports.outcomes_summary import render_markdown

    rep = orch.outcomes_report()
    print(render_markdown(rep))
    print()
    _print({"outcomes_report": rep})
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


def cmd_last30days_probe(orch: Orchestrator, symbol: str) -> int:
    """READ-ONLY: run last30days narrative enrichment for ONE symbol and print the
    context. Writes nothing to the ledger; never proposes or executes. Uses the
    configured provider (mock by default; set LAST30DAYS_PROVIDER=cli to test the
    live keyless skill)."""
    _print({"last30days_probe": orch.last30days_probe(symbol)})
    return 0


def cmd_armed_watch(orch: Orchestrator) -> int:
    """List ARMED WATCH (near-action) candidates: override armed but stayed watch."""
    rows = orch.journal.armed_watches(100)
    view = [{k: r.get(k) for k in (
        "symbol", "eval_decision", "label_decision", "final_decision", "arming_classification",
        "armed_watch_reason", "sentiment_label", "label_confidence", "source_coverage_json",
        "proposal_readiness", "labeller_reason",
    )} for r in rows]
    _print({"armed_watch_summary": orch.journal.armed_watch_summary(), "armed_watches": view,
            "count": len(view)})
    return 0


def cmd_override(orch: Orchestrator, args) -> int:
    """Record a USER OVERRIDE of AlphaOS's recommendation (separate decision layer).
    Without --yes this previews AlphaOS's decision; with --yes it records the
    override (safety gates + manual approval still apply; never auto-executes)."""
    if not args.yes:
        cand = orch.journal.one("SELECT * FROM candidates WHERE candidate_id = ?", (args.candidate_id,))
        if not cand:
            _print({"override_preview": f"candidate {args.candidate_id} not found"})
            return 1
        adj = orch.journal.one(
            "SELECT eval_decision, label_decision, final_decision, armed_watch, arming_classification "
            "FROM decision_adjustments WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
            (args.candidate_id,)) or {}
        _print({"override_preview": {
            "symbol": cand.get("symbol"), "requested_action": args.action,
            "alphaos_final_decision": adj.get("final_decision") or cand.get("label_decision"),
            "armed_watch": bool(adj.get("armed_watch")),
            "arming_classification": adj.get("arming_classification"),
            "high_risk_warning": (adj.get("arming_classification") == "high_risk_narrative"),
            "note": "re-run with --yes to record. Safety gates + manual approval still apply; "
                    "a watch_to_trade only creates a pending_approval proposal (you must `approve` it).",
        }})
        return 0
    res = orch.create_user_override(
        args.candidate_id, args.action, reason_code=args.reason, note=args.note,
        direction=args.direction, size=args.size)
    _print({"override": res})
    return 0 if res.get("ok") else 1


def cmd_overrides(orch: Orchestrator) -> int:
    """List user overrides + attribution summary."""
    _print({"user_override_summary": orch.journal.user_override_summary(),
            "recent_overrides": orch.journal.recent_user_overrides(50)})
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
    l30 = sub.add_parser("last30days_probe",
                         help="READ-ONLY: print last30days narrative context for one symbol (no ledger writes)")
    l30.add_argument("symbol")
    sub.add_parser("armed_watch", help="list ARMED WATCH (near-action) candidates: armed but stayed watch")
    ov = sub.add_parser("override", help="record a USER OVERRIDE of AlphaOS's recommendation (gated; manual approval still required)")
    ov.add_argument("--candidate-id", required=True)
    ov.add_argument("--action", required=True,
                    help="watch_to_trade | propose_to_reject | manual_exit | manual_hold | reject_to_trade | ...")
    ov.add_argument("--reason", default=None, help="reason code (e.g. strong_conviction, disagrees_with_ai)")
    ov.add_argument("--note", default=None, help="free-text note")
    ov.add_argument("--direction", default=None, help="override direction (long|short), if changing")
    ov.add_argument("--size", type=float, default=None, help="size override, if applicable")
    ov.add_argument("--yes", action="store_true", help="confirm + record the override (otherwise preview only)")
    sub.add_parser("overrides", help="list user overrides + attribution summary")
    sub.add_parser("attribution_report",
                   help="user-override attribution learning report (AlphaOS vs user; heuristic)")
    sub.add_parser("backfill_mfe_mae",
                   help="backfill MFE/MAE on closed trades from before excursion tracking existed (idempotent)")
    sub.add_parser("outcomes_update",
                   help="counterfactual outcome tracker: seed + resolve candidate_outcomes "
                        "(1/3/5-day forward returns + bracket replay; measurement only)")
    sub.add_parser("outcomes_report",
                   help="measurement-visibility summary over candidate_outcomes (no statistical claims)")
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
        if args.command == "last30days_probe":
            return cmd_last30days_probe(orch, args.symbol)
        if args.command == "armed_watch":
            return cmd_armed_watch(orch)
        if args.command == "override":
            return cmd_override(orch, args)
        if args.command == "overrides":
            return cmd_overrides(orch)
        if args.command == "attribution_report":
            return cmd_attribution_report(orch)
        if args.command == "backfill_mfe_mae":
            return cmd_backfill_mfe_mae(orch)
        if args.command == "outcomes_update":
            return cmd_outcomes_update(orch)
        if args.command == "outcomes_report":
            return cmd_outcomes_report(orch)
        if args.command == "dashboard":
            return cmd_dashboard(orch)
        if args.command == "kill":
            return cmd_kill(orch, args.action)
        return 1
    finally:
        orch.close()


if __name__ == "__main__":
    raise SystemExit(main())
