"""Orchestrator — wires the daily workflow together.

scan_once:   universe -> snapshots(+freshness) -> candidates -> news -> OpenAI
             eval -> (propose) risk-size -> proposal -> approval (manual leaves
             pending; auto may submit within guardrails) -> simulated paper fill.
monitor_once: watchdog over open positions (stop/target/time) -> exits.
report:      daily learning report.

Manual approval (approve_proposal) and the manual Claude review live here too, so
the dashboard and CLI share one code path. Every step is journaled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from alphaos.ai.claude_reviewer import ClaudeReviewer, ClaudeUnavailable
from alphaos.ai.openai_client import OpenAIClient
from alphaos.approval import ApprovalEngine
from alphaos.config.settings import Settings, load_settings
from alphaos.constants import (
    BASELINE_MOMENTUM_NO_NEWS_V1,
    CATALYST_NOT_AVAILABLE_V1,
    Decision,
    ExecutionProvider,
    NEWS_STATUS_DISABLED_V1,
    NewsStatus,
    PLAYBOOK_V1,
    ReasonCode,
    Severity,
    Strategy,
    TradeDirection,
)
from alphaos.data.freshness_guard import FreshnessGuard
from alphaos.data.market_data import MarketDataClient
from alphaos.execution.order_manager import OrderManager
from alphaos.execution.position_manager import PositionManager
from alphaos.journal.journal_store import JournalStore
from alphaos.news.news_service import NewsService
from alphaos.risk.risk_engine import RiskEngine
from alphaos.reports.daily_recon import DailyRecon
from alphaos.safety import KillSwitch
from alphaos.scanner.candidate_scanner import CandidateScanner
from alphaos.strategy.proposal import TradeProposal
from alphaos.strategy.swing_strategy import SwingStrategy
from alphaos.strategy.daytrade_experiment import DaytradeExperiment
from alphaos.util.ids import new_id

# Price-drift is gated by MAX_PRICE_DRIFT_BPS_SINCE_PROPOSAL via the freshness guard.


@dataclass
class ScanSummary:
    scan_id: str
    candidates: int = 0
    proposed: int = 0
    watch: int = 0
    rejected: int = 0
    risk_blocked: int = 0
    auto_submitted: int = 0
    pending_manual: int = 0
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__


class Orchestrator:
    def __init__(self, settings: Optional[Settings] = None, journal: Optional[JournalStore] = None):
        self.settings = settings or load_settings()
        self.journal = journal or JournalStore(self.settings.db_path, self.settings.jsonl_mirror)

        self.kill_switch = KillSwitch()
        self.market = MarketDataClient(self.settings, self.journal)
        self.freshness = FreshnessGuard.from_settings(self.settings)
        self.scanner = CandidateScanner(self.settings, self.journal, self.market)
        self.news = NewsService(self.settings, self.journal)  # v1: no-news mode
        self.openai = OpenAIClient(self.settings, self.journal)
        self.claude = ClaudeReviewer(self.settings, self.journal)
        self.risk = RiskEngine(self.settings)
        self.swing = SwingStrategy()
        self.daytrade = DaytradeExperiment(enabled=False)
        self.approvals = ApprovalEngine(self.settings, self.journal)
        self.positions = PositionManager(self.settings, self.journal, self.market)
        self.orders = OrderManager(
            self.settings, self.journal, position_manager=self.positions, kill_switch=self.kill_switch
        )
        self.recon = DailyRecon(self.settings, self.journal)
        self._startup_logged = False

    # ----------------------------------------------------------- lifecycle
    def startup(self) -> list:
        """Record config + run startup-safety checks, logging each to events."""
        self.journal.record_config_version(self.settings)
        checks = self.settings.validate_startup()
        for c in checks:
            sev = c.severity if c.ok else c.severity
            level = Severity.INFO if c.ok else c.severity
            self.journal.log_system_event(
                level, "startup", f"[{'OK' if c.ok else 'FAIL'}] {c.name}: {c.detail}"
            )
        # In paper mode, refuse paper execution if critical checks fail.
        if self.settings.is_paper:
            ok, failing = self.settings.paper_execution_allowed()
            if not ok:
                self.journal.log_system_event(
                    Severity.CRITICAL, "startup",
                    "Paper execution refused: Alpaca paper safety checks failed.",
                    {"failing": [f.name for f in failing]},
                )
        self._startup_logged = True
        return checks

    def _ensure_startup(self) -> None:
        if not self._startup_logged:
            self.startup()

    # ------------------------------------------------------------- scan_once
    def run_scan_once(self) -> ScanSummary:
        self._ensure_startup()
        scan = self.scanner.scan()
        summary = ScanSummary(scan_id=scan.scan_id, candidates=len(scan.candidates))

        if self.kill_switch.is_engaged():
            self.journal.log_system_event(
                Severity.WARNING, "scan", "Kill switch engaged: no proposals will be executed."
            )

        for cand in scan.candidates:
            snapshot = cand.get("_snapshot", {})
            # v1 NO-NEWS mode: no news fetch; evaluate on price/volume/structure.
            self._update_candidate_news(cand["candidate_id"], NEWS_STATUS_DISABLED_V1)

            evaluation = self.openai.evaluate(
                cand, snapshot, freshness_status="usable",  # scanner only keeps usable snapshots
            )
            self.journal.insert("openai_evaluations", evaluation.to_row())
            self._record_baselines(cand, evaluation)

            decision = evaluation.decision
            if decision == Decision.REJECT.value:
                self._reject_candidate(cand, "openai", evaluation)
                summary.rejected += 1
                continue
            if decision == Decision.WATCH.value:
                self._set_candidate_status(cand["candidate_id"], "watch")
                summary.watch += 1
                continue

            # decision == propose
            handled = self._handle_proposal(cand, evaluation, summary)
            if not handled:
                summary.rejected += 1

        self.journal.log_system_event(
            Severity.INFO, "scan",
            f"scan_once complete: {summary.proposed} proposed, {summary.watch} watch, "
            f"{summary.rejected} rejected, {summary.risk_blocked} risk-blocked, "
            f"{summary.auto_submitted} auto-submitted, {summary.pending_manual} pending.",
        )
        return summary

    def _handle_proposal(self, cand, evaluation, summary: ScanSummary) -> bool:
        direction = evaluation.direction or TradeDirection.LONG.value
        requires_margin = direction == TradeDirection.SHORT.value
        snapshot = cand.get("_snapshot", {})

        risk = self.risk.assess(
            direction=direction,
            entry=evaluation.entry,
            stop=evaluation.stop,
            snapshot=snapshot,
            open_positions=self.journal.count_open_positions(),
            trades_today=self.journal.count_paper_orders_today(),
            realized_pnl_today=self.journal.realized_pnl_today(),
            requires_margin=requires_margin,
            margin_approved=False,
        )
        if not risk.approved or risk.sizing is None:
            proposal = self.swing.build_proposal(evaluation, risk.sizing or _zero_sizing(evaluation))
            proposal.status = "blocked"
            self.journal.insert("trade_proposals", proposal.to_row())
            self._reject_candidate(cand, "risk", evaluation, reason=risk.primary_reason)
            summary.risk_blocked += 1
            return False

        proposal = self.swing.build_proposal(evaluation, risk.sizing)
        proposal.status = "pending_approval"
        self.journal.insert("trade_proposals", proposal.to_row())
        self._set_candidate_status(cand["candidate_id"], "proposed")
        summary.proposed += 1

        outcome = self.approvals.consider(proposal, risk_ok=True, freshness_ok=True)
        if outcome.approved:
            result = self._execute(proposal)
            if result.blocked:
                self._set_proposal_status(proposal.proposal_id, "blocked")
            else:
                self._set_proposal_status(proposal.proposal_id, "filled")
                summary.auto_submitted += 1
        else:
            summary.pending_manual += 1
        return True

    # --------------------------------------------------- manual approval API
    def approve_proposal(
        self, proposal_id: str, approver: str = "user", approve_margin: bool = False
    ):
        """Manual approval path (dashboard/CLI). Re-validates freshness + risk
        before executing. Returns (ok, message)."""
        self._ensure_startup()
        row = self.journal.proposal_by_id(proposal_id)
        if not row:
            return False, "proposal not found"
        if row["status"] not in ("pending_approval", "proposed"):
            return False, f"proposal not approvable (status={row['status']})"
        proposal = TradeProposal.from_row(row)

        if self.kill_switch.is_engaged():
            return False, "kill switch engaged"

        # Explicit margin/short capability approval (only via this flag).
        if proposal.requires_margin:
            if not approve_margin:
                self.journal.log_system_event(
                    Severity.WARNING, "approval",
                    f"{proposal.symbol} needs margin/borrow; surface case and require explicit approval.",
                    {"proposal_id": proposal_id},
                )
                return False, "this trade requires explicit margin approval (approve_margin=True)"
            proposal.margin_approved = True
            self.journal.conn.execute(
                "UPDATE trade_proposals SET margin_approved = 1 WHERE proposal_id = ?", (proposal_id,)
            )
            self.journal.conn.commit()

        # Freshness re-check (mandatory before any order). Closed session,
        # stale/missing quote or bar all surface here and block.
        snap = self.market.get_snapshot(proposal.symbol)
        report = self.freshness.assess(snap)
        if not report.is_usable:
            self.journal.log_system_event(
                Severity.WARNING, "approval",
                f"Approval blocked for {proposal.symbol}: {report.freshness_status} "
                f"({report.block_reason}).",
            )
            return False, f"data not usable ({report.freshness_status}/{report.block_reason})"

        # Material price drift since proposal => do not trade on a stale entry.
        cur = snap.get("last_price")
        drift_ok, drift_bps = self.freshness.check_price_drift(proposal.entry, cur)
        if not drift_ok:
            self.journal.log_system_event(
                Severity.WARNING, "approval",
                f"Approval blocked for {proposal.symbol}: price drift {drift_bps} bps "
                f"> {self.settings.max_price_drift_bps_since_proposal} bps "
                f"({proposal.entry}->{cur}).",
                {"reason_code": ReasonCode.PRICE_DRIFT.value},
            )
            return False, f"price drift {drift_bps} bps exceeds limit since proposal"

        # Risk re-check.
        risk = self.risk.assess(
            direction=proposal.direction,
            entry=proposal.entry,
            stop=proposal.stop,
            snapshot=snap,
            open_positions=self.journal.count_open_positions(),
            trades_today=self.journal.count_paper_orders_today(),
            realized_pnl_today=self.journal.realized_pnl_today(),
            requires_margin=proposal.requires_margin,
            margin_approved=proposal.margin_approved,
        )
        if not risk.approved:
            self._set_proposal_status(proposal_id, "blocked")
            return False, f"risk blocked: {risk.primary_reason}"

        self.approvals.approve_manually(proposal, approver=approver, freshness_ok=True, risk_ok=True)
        result = self._execute(proposal, fill_price=cur)
        if result.blocked:
            self._set_proposal_status(proposal_id, "blocked")
            return False, f"execution blocked: {result.block_reason}"
        self._set_proposal_status(proposal_id, "filled")
        return True, f"approved + filled ({result.protection_path})"

    def reject_proposal(self, proposal_id: str, approver: str = "user", reason: str = "user rejected"):
        row = self.journal.proposal_by_id(proposal_id)
        if not row:
            return False, "proposal not found"
        proposal = TradeProposal.from_row(row)
        self.approvals.reject_manually(proposal, approver=approver, reason=reason)
        self._set_proposal_status(proposal_id, "rejected")
        return True, "rejected"

    # ------------------------------------------------------- claude review
    def request_claude_review(self, candidate_id: str, triggered_by: str = "user"):
        """Manual-only Claude second opinion. Raises ClaudeUnavailable without a key.
        Stored in its own table; never overwrites the OpenAI evaluation."""
        if not self.claude.available:
            raise ClaudeUnavailable("Claude review requires ANTHROPIC_API_KEY (button disabled).")
        cand = self.journal.one("SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,))
        ev = self.journal.evaluation_for_candidate(candidate_id)
        if not cand or not ev:
            raise ValueError("candidate or evaluation not found")
        review = self.claude.review(cand, ev, triggered_by=triggered_by)
        self.journal.insert("claude_reviews", review.to_row())
        self.journal.log_system_event(
            Severity.INFO, "claude", f"Claude review stored for {cand['symbol']} (verdict={review.verdict})."
        )
        return review

    # ------------------------------------------------------------- monitor
    def run_monitor_once(self, price_overrides: Optional[dict] = None) -> dict:
        self._ensure_startup()
        exits = self.positions.monitor(price_overrides=price_overrides)
        self.journal.log_system_event(
            Severity.INFO, "monitor", f"monitor_once complete: {len(exits)} exit(s)."
        )
        return {"exits": exits, "open_positions": self.journal.count_open_positions()}

    # --------------------------------------------------------------- report
    def generate_daily_report(self) -> dict:
        self._ensure_startup()
        return self.recon.generate()

    # ----------------------------------------------------------- demo seed
    def seed_demo(self) -> dict:
        """Create a clearly-labelled DEMO proposal that exercises the execution +
        journal + dashboard layers end-to-end WITHOUT touching the news pipeline.

        This is not the runtime scan and never fabricates news. It is gated to
        non-real modes and logged as DEMO_SEED.
        """
        self._ensure_startup()
        symbol = "DEMO"
        # Price from the same symbol the approval path will re-fetch, so the
        # mandatory freshness/material-move re-check is consistent.
        snap = self.market.get_snapshot(symbol)
        entry = float(snap.get("last_price") or 100.0)
        stop = round(entry * 0.97, 2)
        target = round(entry * 1.06, 2)
        cand_id = new_id("cand")
        self.journal.insert(
            "candidates",
            {
                "candidate_id": cand_id, "symbol": symbol, "direction": TradeDirection.LONG.value,
                "strategy": Strategy.SWING.value, "momentum_score": 0.7,
                "news_status": NEWS_STATUS_DISABLED_V1, "status": "demo", "notes_json": {"demo": True},
            },
        )
        risk = self.risk.assess(direction="long", entry=entry, stop=stop, snapshot=snap)
        proposal = TradeProposal(
            symbol=symbol, direction="long", strategy=Strategy.SWING.value,
            entry=entry, stop=stop, target=target, max_holding_days=3,
            qty=(risk.sizing.shares if risk.sizing else 1),
            risk_per_share=(risk.sizing.risk_per_share if risk.sizing else (entry - stop)),
            dollar_risk=(risk.sizing.dollar_risk if risk.sizing else (entry - stop)),
            expected_r=2.0, same_day_exit_eligible=True, candidate_id=cand_id,
            eval_id="demo", is_demo=True, status="pending_approval",
        )
        self.journal.insert("trade_proposals", proposal.to_row())
        self.journal.log_system_event(
            Severity.WARNING, "demo",
            "DEMO_SEED proposal created (bypasses news pipeline; clearly labelled).",
            {"proposal_id": proposal.proposal_id},
        )
        ok, msg = self.approve_proposal(proposal.proposal_id, approver="demo")
        return {"proposal_id": proposal.proposal_id, "approved": ok, "message": msg}

    # --------------------------------------------------------------- helpers
    def _execute(self, proposal: TradeProposal, fill_price: Optional[float] = None):
        self._set_proposal_status(proposal.proposal_id, "approved")
        return self.orders.execute_proposal(proposal, fill_price=fill_price)

    def _update_candidate_news(self, candidate_id: str, news_status) -> None:
        status = news_status.value if isinstance(news_status, NewsStatus) else str(news_status)
        self.journal.conn.execute(
            "UPDATE candidates SET news_status = ? WHERE candidate_id = ?", (status, candidate_id)
        )
        self.journal.conn.commit()

    def _set_candidate_status(self, candidate_id: str, status: str) -> None:
        self.journal.conn.execute(
            "UPDATE candidates SET status = ? WHERE candidate_id = ?", (status, candidate_id)
        )
        self.journal.conn.commit()

    def _set_proposal_status(self, proposal_id: str, status: str) -> None:
        self.journal.conn.execute(
            "UPDATE trade_proposals SET status = ? WHERE proposal_id = ?", (status, proposal_id)
        )
        self.journal.conn.commit()

    def _reject_candidate(self, cand, stage, evaluation, reason: Optional[str] = None) -> None:
        if reason is None:
            # No-news mode: rejections come from data/validation/risk, not "no news".
            if evaluation.validation_status not in (None, "", "passed"):
                reason = ReasonCode.INVENTED_CATALYST.value
            else:
                reason = ReasonCode.OPENAI_REJECT.value
        self.journal.insert(
            "rejected_candidates",
            {
                "rejection_id": new_id("rej"),
                "candidate_id": cand["candidate_id"],
                "symbol": cand["symbol"],
                "stage": stage,
                "reason_code": reason,
                "reason_detail": evaluation.reasoning_summary,
                "direction": evaluation.direction,
                "would_be_entry": evaluation.entry,
                "would_be_stop": evaluation.stop,
            },
        )
        self._set_candidate_status(cand["candidate_id"], "rejected")

    def _record_baselines(self, cand, evaluation) -> None:
        """Record the v1 no-news baseline (the live measurement path).

        News-dependent fields are written as NULL so the news layer can populate
        them later without a migration.
        """
        ref_price = cand.get("last_price")
        self.journal.insert(
            "baseline_outcomes",
            {
                "baseline_id": new_id("base"),
                "candidate_id": cand["candidate_id"],
                "symbol": cand["symbol"],
                "baseline_type": BASELINE_MOMENTUM_NO_NEWS_V1,
                "direction": evaluation.direction,
                "reference_price": ref_price,
                "ref_timestamp": cand.get("_snapshot", {}).get("quote_timestamp"),
                "ai_decision": evaluation.decision,
                "claude_consulted": 0,
                "news_status": NEWS_STATUS_DISABLED_V1,
                "catalyst": CATALYST_NOT_AVAILABLE_V1,
                "no_news_baseline": 1,
                # news-dependent fields left NULL for the future news layer:
                "news_confirmed_subset": None,
                "news_provider": None,
                "news_sources": None,
                "catalyst_type": None,
                "catalyst_confidence": None,
                "notes_json": {"confidence": evaluation.confidence, "momentum": cand.get("momentum_score")},
            },
        )

    # --------------------------------------------------------- system health
    def system_health(self) -> dict:
        """Structured health for the dashboard/CLI: mocked/deferred/disabled/live
        layers are all explicitly labelled."""
        s = self.settings
        last_snap = self.journal.one(
            "SELECT freshness_status, market_session FROM price_snapshots ORDER BY id DESC LIMIT 1"
        )
        freshness = (last_snap or {}).get("freshness_status") or "n/a"
        return {
            "playbook": PLAYBOOK_V1,
            "ai_primary": f"openai / {'configured' if s.has_openai_key else 'missing key (mock)'}",
            "ai_reviewer": f"anthropic / optional / {'configured' if s.has_anthropic_key else 'missing key'}",
            "market_data_provider": s.data_provider,
            "market_data_feed": s.market_data_feed,
            "market_data_mode": s.market_data_mode,         # live / mock
            "market_data_limited": "free/IEX — limited-market data",
            "market_data_freshness": freshness,
            "news_provider": "disabled_v1",
            "benzinga": "deferred_v1",
            "web_scraper": "disabled_v1",
            "massive": "deferred_v1",
            "execution_provider": s.execution_provider,     # simulated_internal
            "real_alpaca_paper_execution": "not_enabled_v1",
            "real_money_trading": "unreachable",
            "manual_approval": "required" if s.effective_approval_mode.value == "manual" else "auto (capped)",
            "kill_switch": "ENGAGED" if self.kill_switch.is_engaged() else "off",
            "broker_connected": self.orders.broker_connected,
            "open_positions": self.journal.count_open_positions(),
        }

    def close(self) -> None:
        self.journal.close()


def _zero_sizing(evaluation):
    from alphaos.risk.risk_engine import PositionSizing

    rps = abs((evaluation.entry or 0) - (evaluation.stop or 0))
    return PositionSizing(shares=0, risk_per_share=rps, dollar_risk=0.0, position_value=0.0, risk_budget=0.0)
