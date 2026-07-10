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
from alphaos.scheduler import JobRunner


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


def cmd_backfill_regime_days(orch: Orchestrator) -> int:
    """REG-1 one-off: extend SPY history, classify the full series into
    regime_days, and stamp pre-existing candidate_packets rows still missing
    a regime. Idempotent; measurement only -- no decision/execution change."""
    res = orch.backfill_regime_days()
    _print({"backfill_regime_days": res})
    return 0 if "error" not in res else 1


def cmd_outcomes_update(orch: Orchestrator) -> int:
    """Counterfactual outcome tracker: seed + resolve candidate_outcomes rows
    (candidates/proposals/rejects/armed-watch/user-overrides) with 1/3/5-day
    forward returns + bracket replay. PURE MEASUREMENT — no execution/approval
    change; idempotent."""
    res = orch.outcomes_update()
    _print({"outcomes_update": res})
    return 0


def cmd_regime_arming_report(orch: Orchestrator) -> int:
    """REG-1: the shadow arming-map scorer report. PURE READ -- pure ledger
    math over existing shadow rows; nothing armed/disarmed for real."""
    from alphaos.reports.regime_arming_scorer import render_markdown

    rep = orch.regime_arming_report()
    print(render_markdown(rep))
    print()
    _print({"regime_arming_report": rep})
    return 0


def cmd_baseline_report(orch: Orchestrator) -> int:
    """BASELINE: does the AI add R report. PURE READ -- pure ledger math
    over existing shadow rows; nothing gated for real."""
    from alphaos.reports.baseline_report import render_markdown

    rep = orch.baseline_report()
    print(render_markdown(rep))
    print()
    _print({"baseline_report": rep})
    return 0


def cmd_baseline_register(orch: Orchestrator) -> int:
    """BASELINE spec item 6: register the pre-registration block (=
    preregistrations row #1, per Prime Directive #4) -- one-off, operator-
    invoked, idempotent (refuses a duplicate rather than creating a second
    row for the same hypothesis, since register_hypothesis() itself is NOT
    idempotent -- every call creates a new row)."""
    from alphaos.reports.baseline_report import FLOOR_DAY_BLOCKS, FLOOR_SPAN_DAYS
    from alphaos.stats.preregistration import register_hypothesis

    hypothesis = (
        "AI adds >= +0.05R mean paired ai_delta_r over threshold_v1 on "
        "proposed candidates, conditional on labeller reach"
    )
    metric = "mean_ai_delta_r = mean(candidate_outcomes.replay_r - shadow_baseline_decisions.replay_r), threshold_v1"
    existing = orch.journal.one(
        "SELECT prereg_id, registered_at_utc FROM preregistrations WHERE hypothesis = ? AND metric = ?",
        (hypothesis, metric),
    )
    if existing:
        print(f"Already registered: {existing['prereg_id']} (at {existing['registered_at_utc']}) -- no-op.")
        _print({"baseline_register": {"status": "already_registered", **existing}})
        return 0

    prereg_id = register_hypothesis(
        orch.journal, hypothesis, metric,
        # register_hypothesis()'s own parameter is named floor_effective_n
        # (PORT-1's generic vocabulary for this bar, regardless of counting
        # axis) -- BASELINE_report's own constant is FLOOR_DAY_BLOCKS since
        # it counts day-blocks, not PORT-1's symbol-clustered effective_n.
        floor_effective_n=FLOOR_DAY_BLOCKS, floor_span_days=FLOOR_SPAN_DAYS,
        analysis_not_before="2026-09-07",
        params={"rule_version": "threshold_v1", "target_delta_r": 0.05},
    )
    print(f"Registered: {prereg_id}")
    _print({"baseline_register": {"status": "registered", "prereg_id": prereg_id}})
    return 0


def cmd_eval_corpus_build(orch: Orchestrator, corpus_dir: str, limit: int) -> int:
    """EVAL-1 one-off: select real, clean (post-PR9.1) candidate_packets rows
    into the frozen golden corpus (additive; never overwrites an existing
    fixture). Does NOT adjudicate ground truth -- the operator reviews the
    written fixture files and fills in ground_truth_label by hand, then
    commits the corpus directory like a card."""
    res = orch.eval_corpus_build(corpus_dir=corpus_dir, limit=limit)
    _print({"eval_corpus_build": res})
    print(
        f"\n{res['packets_written']} new packet(s) written to {res['corpus_dir']} "
        f"(corpus now {res['corpus_size']} packet(s), version {res['corpus_version']}). "
        "Review the fixtures, fill in ground_truth_label by hand where you can, then "
        "git add/commit the corpus directory -- it is never auto-committed."
    )
    return 0


def cmd_eval(orch: Orchestrator, corpus_dir: str, repeats: int) -> int:
    """EVAL-1: replay the frozen golden corpus through the CURRENT playbook
    classifier (the exact production call, never a reimplementation).
    Stores every result including fail-safe ones. Zero decision surface."""
    res = orch.run_eval(corpus_dir=corpus_dir, repeats=repeats)
    _print({"eval_run": res})
    return 0 if "error" not in res else 1


def cmd_eval_report(orch: Orchestrator) -> int:
    """EVAL-1: the latest eval run's report -- parse rate, label agreement
    vs ground truth, categorical stability across repeats. PURE READ."""
    from alphaos.reports.eval_report import render_markdown

    rep = orch.eval_report()
    print(render_markdown(rep))
    print()
    _print({"eval_report": rep})
    return 0


def cmd_relabel(orch: Orchestrator, date_from: str, date_to: str, dry_run: bool) -> int:
    """TASK-R one-off: replay stored packet_json for candidate_packets rows
    in [date_from, date_to] through the CURRENT labeller. --dry-run prints
    composed prompts with zero network calls; the live run persists new
    candidate_labels rows (relabel_of set, originals never touched) and
    prints an old-vs-new label diff table."""
    res = orch.relabel_candidates(date_from, date_to, dry_run=dry_run)
    if "error" in res:
        _print({"relabel": res})
        return 1

    if dry_run:
        print(f"DRY RUN -- {res['n_packets']} packet(s) in [{date_from}, {date_to}], zero network calls:\n")
        for p in res["prompts"]:
            print(f"--- {p['symbol']} ({p['packet_id']}) ---")
            print(p["prompt"])
            print()
    else:
        print(f"Relabelled {res['n_relabelled']}/{res['n_packets']} packet(s) in [{date_from}, {date_to}]:\n")
        print(f"{'symbol':<8} {'old label':<20} {'new label':<20} {'old dec':<10} {'new dec':<10}")
        for d in res["diffs"]:
            print(f"{d['symbol']:<8} {str(d['old_label']):<20} {str(d['new_label']):<20} "
                  f"{str(d['old_decision']):<10} {str(d['new_decision']):<10}")
        print()
    _print({"relabel": {k: v for k, v in res.items() if k not in ("prompts",)}})
    return 0


def cmd_canary_corpus_build(orch: Orchestrator, corpus_dir: str, limit: int) -> int:
    """CANARY one-off: select real, clean (post-PR9.1) candidate_packets rows
    -- preferring TASK-R relabels -- into the frozen golden corpus (additive;
    never overwrites an existing fixture). Review the fixtures, then git
    add/commit the corpus directory -- it is never auto-committed."""
    res = orch.canary_corpus_build(corpus_dir=corpus_dir, limit=limit)
    _print({"canary_corpus_build": res})
    print(
        f"\n{res['packets_written']} new packet(s) written to {res['corpus_dir']} "
        f"(corpus now {res['corpus_size']} packet(s), version {res['corpus_version']}). "
        "Review the fixtures, then git add/commit the corpus directory -- it is never auto-committed. "
        "Set CANARY_ENABLED=true once you're ready for the weekly job to run."
    )
    return 0


def cmd_canary_run(orch: Orchestrator, corpus_dir: str) -> int:
    """CANARY: replay the frozen golden corpus through the CURRENT playbook
    classifier and compare against the pinned baseline run. Zero decision
    surface."""
    res = orch.canary_run(corpus_dir=corpus_dir)
    _print({"canary_run": res})
    return 0 if "error" not in res else 1


def cmd_canary_status(orch: Orchestrator) -> int:
    """CANARY: the latest run's report -- PURE READ."""
    from alphaos.reports.canary_report import render_markdown

    rep = orch.canary_status()
    print(render_markdown(rep))
    print()
    _print({"canary_status": rep})
    return 0


def cmd_canary_pin_baseline(orch: Orchestrator, run_id: str) -> int:
    """CANARY: mark run_id as THE reference run every future run diffs
    against. Never automatic -- an operator decides when a run is clean
    enough to trust."""
    res = orch.canary_pin_baseline(run_id)
    _print({"canary_pin_baseline": res})
    return 0 if "error" not in res else 1


def cmd_universe_build(orch: Orchestrator) -> int:
    """EXP-0: screen the tradable universe down to the shadow-tier ADV/price
    band and write the result to the committed universe file (NOT git-add'd
    or committed by this command — reviewing the symbol list and committing
    it is a deliberate operator step, per the spec's own acceptance gate).
    One-off / quarterly refresh; never a scheduler job. Requires live Alpaca
    credentials (mock/offline mode has nothing to screen against)."""
    from alphaos.universe.builder import build_shadow_universe, write_universe_file

    screened = build_shadow_universe(orch.settings, orch.journal)
    if not screened["symbols"] and screened["screened"] == 0:
        _print({
            "universe_build": screened,
            "note": "no live Alpaca screen available (mock/offline mode, or missing credentials) "
                    "-- nothing written",
        })
        return 1
    doc = write_universe_file(screened, orch.settings.shadow_tier_universe_file)
    _print({
        "universe_build": {
            "path": orch.settings.shadow_tier_universe_file,
            "version": doc["version"],
            "sha256": doc["sha256"],
            "as_of_date": doc["as_of_date"],
            "screened": doc["screened"],
            "passed": doc["passed"],
            "skipped_count": len(doc["skipped"]),
            "skipped_reasons": sorted({s["reason"] for s in doc["skipped"]}),
        },
        "next_step": "Review the symbol list, then `git add` + commit the file yourself "
                     "before setting SHADOW_TIER_ENABLED=true.",
    })
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


def cmd_relative_performance_report(orch: Orchestrator) -> int:
    """PR9.5: paper-equity vs S&P 500 measurement. No statistical claims —
    floor-gated exactly like every other report; PURE READ."""
    from alphaos.reports.relative_performance import render_markdown

    rep = orch.relative_performance_report()
    print(render_markdown(rep))
    print()
    _print({"relative_performance_report": rep})
    return 0


def cmd_brief(orch: Orchestrator) -> int:
    """PR11: the daily human interface. PURE READ."""
    from alphaos.reports.daily_brief import render_markdown

    brief = orch.daily_brief_report()
    print(render_markdown(brief))
    print()
    _print({"daily_brief": brief})
    return 0


def cmd_decision_lineage(orch: Orchestrator, decision_id: str) -> int:
    """READ-ONLY: which code/config/model/prompt/data/scheduler context
    produced this decision. Accepts a candidate_id, proposal_id,
    rejection_id, adjustment_id, override_id, outcome_id, eval_id, review_id,
    or polarity_id."""
    _print({"decision_lineage": orch.decision_lineage_report(decision_id)})
    return 0


def cmd_flatten(orch: Orchestrator) -> int:
    res = orch.flatten_paper_account()
    _print({"flatten": res})
    return 0 if res.get("ok") else 1


def cmd_reconcile_report(orch: Orchestrator) -> int:
    _print({"broker_ledger_reconciliation": orch.broker_ledger_report()})
    return 0


def cmd_protection_status(orch: Orchestrator) -> int:
    """READ-ONLY: broker protection watchdog status -- unprotected/mismatched
    positions, open incidents, and whether new entries are currently blocked."""
    _print({"protection_status": orch.protection_status_report()})
    return 0


def cmd_protection_resolve(orch: Orchestrator, incident_id: str, exit_price: float, note: str) -> int:
    """Human-confirmed resolution of a local-open/broker-closed protection
    incident: calls close_position() with a confirmed exit price -- never raw SQL."""
    res = orch.protection_resolve(incident_id, exit_price=exit_price, note=note, resolved_by="cli")
    _print({"protection_resolve": res})
    return 0 if res.get("ok") else 1


def cmd_protection_ack(orch: Orchestrator, incident_id: str, note: str) -> int:
    """Acknowledge an unprotected/degraded protection incident WITHOUT closing
    the position (lifts the new-entry block once protection is confirmed restored,
    or the risk is explicitly accepted)."""
    res = orch.protection_ack(incident_id, note=note, resolved_by="cli")
    _print({"protection_ack": res})
    return 0 if res.get("ok") else 1


def cmd_scheduler_status(orch: Orchestrator) -> int:
    """READ-ONLY: scheduler job history, lock state, protection/kill-switch/cost-cap summary."""
    _print({"scheduler_status": JobRunner(orch).status_report()})
    return 0


def cmd_scheduler_run_once(orch: Orchestrator) -> int:
    """Run every scheduled job that is currently due (respects cadence windows,
    kill switch, cost cap)."""
    _print({"scheduler_run_once": JobRunner(orch).run_due_jobs()})
    return 0


def cmd_scheduler_run_job(orch: Orchestrator, job_type: str) -> int:
    """Force-run one scheduler job now, bypassing cadence timing (still respects
    kill switch / protection / cost cap / locking)."""
    _print({"scheduler_run_job": JobRunner(orch).run_job(job_type)})
    return 0


def cmd_scheduler_health(orch: Orchestrator) -> int:
    """PR9 dead-man's-switch: exit 0 if a job_runs row completed recently
    enough during market hours (else exit 1 + one alert). Meant to be driven
    by its OWN separate LaunchAgent, not the scheduler's own tick."""
    result = JobRunner(orch).heartbeat_check()
    _print({"scheduler_health": result})
    return 0 if result["ok"] else 1


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
    sub.add_parser("protection_status",
                   help="broker protection watchdog status: unprotected/mismatched positions, open incidents")
    pr = sub.add_parser("protection_resolve",
                        help="human-confirmed resolution of a local-open/broker-closed protection incident "
                             "(calls close_position with a confirmed exit price; never raw SQL)")
    pr.add_argument("incident_id")
    pr.add_argument("--exit-price", type=float, required=True)
    pr.add_argument("--note", default="", help="required context for the audit trail")
    pa = sub.add_parser("protection_ack",
                        help="acknowledge an unprotected/degraded protection incident without closing the "
                             "position (lifts the new-entry block)")
    pa.add_argument("incident_id")
    pa.add_argument("--note", default="")
    sub.add_parser("scheduler_status",
                   help="READ-ONLY: scheduler job history, lock state, protection/kill-switch/cost-cap summary")
    sub.add_parser("scheduler_run_once",
                   help="run every scheduled job that is currently due (respects cadence windows, kill switch, "
                        "cost cap)")
    srj = sub.add_parser("scheduler_run_job",
                         help="force-run one scheduler job now, bypassing cadence timing (still respects kill "
                              "switch / protection / cost cap / locking)")
    srj.add_argument("job_type", choices=[
        "scan", "monitor", "outcomes_update", "daily_digest", "benchmark_spine", "text_archive_pull",
        "atr_update", "canary_run",
    ])
    sub.add_parser("scheduler_health",
                   help="dead-man's-switch check: exit 0 if a job completed recently enough during "
                        "market hours, else exit 1 + one alert (run from its own separate LaunchAgent)")
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
    sub.add_parser("brief", help="the daily human interface: needs-you, portfolio health, one action (PR11)")
    sub.add_parser("backfill_mfe_mae",
                   help="backfill MFE/MAE on closed trades from before excursion tracking existed (idempotent)")
    sub.add_parser("outcomes_update",
                   help="counterfactual outcome tracker: seed + resolve candidate_outcomes "
                        "(1/3/5-day forward returns + bracket replay; measurement only)")
    sub.add_parser("outcomes_report",
                   help="measurement-visibility summary over candidate_outcomes (no statistical claims)")
    sub.add_parser("relative_performance_report",
                   help="paper-equity vs S&P 500 measurement (no statistical claims; PR9.5)")
    sub.add_parser("universe_build",
                   help="EXP-0: screen + write the shadow-tier universe file ($5-50M ADV band); "
                        "one-off/quarterly, requires live Alpaca creds, never auto-commits")
    sub.add_parser("backfill_regime_days",
                   help="REG-1: backfill regime_days from SPY history + stamp existing packets "
                        "(idempotent, measurement only)")
    sub.add_parser("regime_arming_report",
                   help="REG-1: shadow arming-map scorer (armed_always vs armed_per_map paired "
                        "replay ΔR per card; nothing armed for real)")
    sub.add_parser("baseline_report",
                   help="BASELINE: does the AI add R over threshold_v1/propose_all_v1 (paired "
                        "ai_delta_r, day-block bootstrap CI; nothing gated for real)")
    sub.add_parser("baseline_register",
                   help="BASELINE: one-off, idempotent -- register the pre-registration block "
                        "(preregistrations row #1)")
    ecb = sub.add_parser("eval_corpus_build",
                         help="EVAL-1: select real, clean candidate_packets rows into the frozen "
                              "golden corpus (additive; ground_truth_label starts null, never "
                              "auto-committed)")
    ecb.add_argument("--corpus-dir", default=None, help="defaults to data/eval")
    ecb.add_argument("--limit", type=int, default=30, help="max NEW packets to select (default 30)")
    ev = sub.add_parser("eval",
                        help="EVAL-1: replay the frozen golden corpus through the current playbook "
                             "classifier; stores every result incl. fail-safe ones")
    ev.add_argument("--corpus-dir", default=None, help="defaults to data/eval")
    ev.add_argument("--repeats", type=int, default=1,
                    help="replay each packet this many times, for categorical-stability measurement")
    sub.add_parser("eval_report",
                   help="EVAL-1: the latest eval run's report (parse rate, label agreement vs "
                        "ground truth, categorical stability)")
    rl = sub.add_parser("relabel",
                        help="TASK-R one-off: retro-relabel candidate_packets in a date range through "
                             "the current labeller; never touches an original row")
    rl.add_argument("--from", dest="date_from", required=True, help="SGT calendar date, YYYY-MM-DD")
    rl.add_argument("--to", dest="date_to", required=True, help="SGT calendar date, YYYY-MM-DD")
    rl.add_argument("--dry-run", action="store_true", help="print composed prompts, zero network calls")
    ccb = sub.add_parser("canary_corpus_build",
                         help="CANARY: select real, clean candidate_packets rows (preferring TASK-R "
                              "relabels) into the frozen golden corpus (additive; never auto-committed)")
    ccb.add_argument("--corpus-dir", default=None, help="defaults to data/canary")
    ccb.add_argument("--limit", type=int, default=20, help="max NEW packets to select (default 20)")
    cr = sub.add_parser("canary_run",
                        help="CANARY: replay the frozen golden corpus through the current playbook "
                             "classifier and compare against the pinned baseline run")
    cr.add_argument("--corpus-dir", default=None, help="defaults to data/canary")
    sub.add_parser("canary_status",
                   help="CANARY: the latest canary run's report (drift tier, parse/fail-safe rate)")
    cpb = sub.add_parser("canary_pin_baseline",
                         help="CANARY: mark a run as THE baseline every future run diffs against "
                              "(never automatic -- an operator decides when a run is trustworthy)")
    cpb.add_argument("run_id")
    dl = sub.add_parser("decision_lineage",
                        help="READ-ONLY: which code/config/model/prompt/data/scheduler context produced "
                             "one decision (accepts a candidate_id, proposal_id, rejection_id, "
                             "adjustment_id, override_id, outcome_id, eval_id, review_id, or polarity_id)")
    dl.add_argument("decision_id")
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
        if args.command == "protection_status":
            return cmd_protection_status(orch)
        if args.command == "protection_resolve":
            return cmd_protection_resolve(orch, args.incident_id, args.exit_price, args.note)
        if args.command == "protection_ack":
            return cmd_protection_ack(orch, args.incident_id, args.note)
        if args.command == "scheduler_status":
            return cmd_scheduler_status(orch)
        if args.command == "scheduler_run_once":
            return cmd_scheduler_run_once(orch)
        if args.command == "scheduler_run_job":
            return cmd_scheduler_run_job(orch, args.job_type)
        if args.command == "scheduler_health":
            return cmd_scheduler_health(orch)
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
        if args.command == "brief":
            return cmd_brief(orch)
        if args.command == "backfill_mfe_mae":
            return cmd_backfill_mfe_mae(orch)
        if args.command == "outcomes_update":
            return cmd_outcomes_update(orch)
        if args.command == "outcomes_report":
            return cmd_outcomes_report(orch)
        if args.command == "relative_performance_report":
            return cmd_relative_performance_report(orch)
        if args.command == "universe_build":
            return cmd_universe_build(orch)
        if args.command == "backfill_regime_days":
            return cmd_backfill_regime_days(orch)
        if args.command == "regime_arming_report":
            return cmd_regime_arming_report(orch)
        if args.command == "baseline_report":
            return cmd_baseline_report(orch)
        if args.command == "baseline_register":
            return cmd_baseline_register(orch)
        if args.command == "eval_corpus_build":
            return cmd_eval_corpus_build(orch, args.corpus_dir, args.limit)
        if args.command == "eval":
            return cmd_eval(orch, args.corpus_dir, args.repeats)
        if args.command == "eval_report":
            return cmd_eval_report(orch)
        if args.command == "relabel":
            return cmd_relabel(orch, args.date_from, args.date_to, args.dry_run)
        if args.command == "canary_corpus_build":
            return cmd_canary_corpus_build(orch, args.corpus_dir, args.limit)
        if args.command == "canary_run":
            return cmd_canary_run(orch, args.corpus_dir)
        if args.command == "canary_status":
            return cmd_canary_status(orch)
        if args.command == "canary_pin_baseline":
            return cmd_canary_pin_baseline(orch, args.run_id)
        if args.command == "decision_lineage":
            return cmd_decision_lineage(orch, args.decision_id)
        if args.command == "dashboard":
            return cmd_dashboard(orch)
        if args.command == "kill":
            return cmd_kill(orch, args.action)
        return 1
    finally:
        orch.close()


if __name__ == "__main__":
    raise SystemExit(main())
