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

import json
from dataclasses import dataclass, field
from typing import Optional

from alphaos.ai.claude_reviewer import ClaudeReviewer, ClaudeUnavailable
from alphaos.ai.openai_client import OpenAIClient, OpenAIEvaluation
from alphaos.approval import ApprovalEngine
from alphaos.config.settings import Settings, load_settings
from alphaos.constants import (
    ArmedWatchReason,
    ArmingClassification,
    AttributionResult,
    HIGH_RISK_NARRATIVE_WARNING,
    OverrideAggressiveness,
    OverrideBlockedReason,
    OverrideOutcomeStatus,
    UserOverrideAction,
    BASELINE_MOMENTUM_NO_NEWS_V1,
    BaselineType,
    CATALYST_NOT_AVAILABLE_V1,
    CatalystStatus,
    CatalystType,
    DecisionAdjustment,
    Decision,
    EnrichmentSource,
    Last30DaysProvider,
    Last30DaysStatus,
    SentimentLabel,
    ExecutionProvider,
    NEWS_STATUS_DISABLED_V1,
    NewsStatus,
    OrderState,
    PLAYBOOK_V1,
    ReasonCode,
    RunStatus,
    ScanType,
    SchedulerRunType,
    Severity,
    Strategy,
    TargetProfile,
    TargetSource,
    TradeDirection,
    TriggerSource,
)
from alphaos.scanner.candidate_scanner import DEFAULT_UNIVERSE
from alphaos.util import timeutils
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
from alphaos.scanner.candidate_packet import build_packet
from alphaos.ai.playbook_classifier import PlaybookClassifier
from alphaos.ai.last30days_polarity import Last30DaysPolarityClassifier, PolarityEvidence
from alphaos.news.catalyst_enricher import CatalystEnricher
from alphaos.research.last30days_enricher import Last30DaysEnricher
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
    labelled: int = 0
    shortlisted: int = 0
    catalyst_enriched: int = 0
    last30days_enriched: int = 0
    last30days_skipped_budget_cap: int = 0
    polarity_classified: int = 0
    high_risk_narrative: int = 0
    decision_upgraded: int = 0
    decision_downgraded: int = 0
    armed_watch: int = 0
    scan_batch_id: Optional[str] = None
    scheduler_run_id: Optional[str] = None
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
        self.labeller = PlaybookClassifier(self.settings, self.journal)  # Roadmap 2.3: AI category labelling
        self.enricher = CatalystEnricher(self.settings, self.journal)    # Roadmap 2.4: official catalyst context
        self.l30_enricher = Last30DaysEnricher(self.settings, self.journal)  # Roadmap 2.5: last30days narrative context
        self.polarity = Last30DaysPolarityClassifier(self.settings, self.journal)  # Roadmap 2.7: narrative polarity
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

        # --- Mint a scan batch + a scheduler run (records exist even though v1
        #     has no real scheduler; trigger is manual CLI). ------------------
        scan_batch_id = new_id("scan")
        scheduler_run_id = new_id("schr")
        st = timeutils.stamp()
        session = timeutils.market_session()
        if session.value == "regular":
            scan_type = ScanType.POST_OPEN.value
        elif session.value == "premarket":
            scan_type = ScanType.PREMARKET.value
        else:
            scan_type = ScanType.MANUAL.value
        self.journal.insert(
            "scan_batches",
            {
                "scan_batch_id": scan_batch_id,
                "scheduler_run_id": scheduler_run_id,
                "scan_type": scan_type,
                "source": "cli",
                "started_at_utc": st.utc,
                "started_at_sgt": st.local_sgt,
                "status": RunStatus.STARTED.value,
                "market_session": session.value,
                "universe_count": len(DEFAULT_UNIVERSE),
            },
        )
        self.journal.insert(
            "scheduler_runs",
            {
                "scheduler_run_id": scheduler_run_id,
                "run_type": SchedulerRunType.SCAN.value,
                "trigger_source": TriggerSource.MANUAL_CLI.value,
                "started_at_utc": st.utc,
                "started_at_sgt": st.local_sgt,
                "status": RunStatus.STARTED.value,
                "scan_batch_id": scan_batch_id,
            },
        )

        scan = self.scanner.scan(scan_batch_id=scan_batch_id)
        summary = ScanSummary(
            scan_id=scan.scan_id, candidates=len(scan.candidates),
            scan_batch_id=scan_batch_id, scheduler_run_id=scheduler_run_id,
        )

        if self.kill_switch.is_engaged():
            self.journal.log_system_event(
                Severity.WARNING, "scan", "Kill switch engaged: no proposals will be executed."
            )

        # --- Roadmap 2.3: rank candidates by deterministic interest, then label
        #     only the top-N shortlist (cost cap). Labelling is ADVISORY — it can
        #     only DOWNGRADE the trade decision, never create a PROPOSE. With
        #     labelling disabled this is the exact legacy momentum path. ---
        labelling = self.settings.labelling_enabled
        shortlist: set = set()
        if labelling:
            self._rank_candidates(scan.candidates)
            # The AI-labelling shortlist is the top interest-ranked candidates,
            # bounded by BOTH the shortlist size and the hard AI cost cap.
            cap = min(self.settings.interest_scan_top_n, self.settings.max_candidates_to_ai)
            shortlist = {
                c["candidate_id"] for c in scan.candidates
                if (c.get("interest_rank") or 10 ** 9) <= cap
            }
            summary.shortlisted = len(shortlist)

        # Catalyst enrichment is separately cost-capped per scan (Roadmap 2.4).
        enrich_budget = (self.settings.news_max_symbols_per_scan
                         if (labelling and self.settings.news_enrichment_enabled) else 0)

        # last30days narrative enrichment is a SEPARATE per-scan budget (Roadmap
        # 2.5): the top-N shortlisted candidates BY INTEREST RANK are enriched;
        # eligible candidates outside the cap are explicitly journaled as
        # 'skipped_budget_cap' (never silently dropped). Selecting by rank (not loop
        # order) guarantees the highest-interest candidates get the budget.
        l30_enabled = labelling and self.settings.last30days_enabled
        l30_cap = (min(cap, self.settings.last30days_max_symbols_per_scan)
                   if l30_enabled else 0)
        l30_set = {
            c["candidate_id"] for c in scan.candidates
            if l30_enabled and (c.get("interest_rank") or 10 ** 9) <= l30_cap
        }

        for cand in scan.candidates:
            snapshot = cand.get("_snapshot", {})
            # v1 NO-NEWS mode for the EVAL: it never sees catalyst context.
            self._update_candidate_news(cand["candidate_id"], NEWS_STATUS_DISABLED_V1)

            # AI category/playbook label for the shortlist only (advisory, journaled,
            # cost-capped). It never executes anything and is applied downgrade-only.
            classification = None
            if labelling and cand["candidate_id"] in shortlist:
                do_enrich = enrich_budget > 0
                # last30days mode: enrich (within cap) | skipped_budget_cap (eligible
                # but outside cap) | None (last30days disabled). Context only.
                l30_mode = None
                if l30_enabled:
                    l30_mode = ("enrich" if cand["candidate_id"] in l30_set
                                else "skipped_budget_cap")
                classification = self._label_candidate(
                    cand, snapshot, scan_batch_id, enrich=do_enrich, l30_mode=l30_mode)
                summary.labelled += 1
                if do_enrich:
                    enrich_budget -= 1
                    summary.catalyst_enriched += 1
                if l30_mode == "enrich":
                    summary.last30days_enriched += 1
                elif l30_mode == "skipped_budget_cap":
                    summary.last30days_skipped_budget_cap += 1
                if cand.get("_polarity") is not None:
                    summary.polarity_classified += 1

            evaluation = self.openai.evaluate(
                cand, snapshot, freshness_status="usable",  # scanner only keeps usable snapshots
            )
            self.journal.insert("openai_evaluations", evaluation.to_row())
            self._record_baselines(cand, evaluation)

            decision = evaluation.decision
            if classification is not None:
                decision = self._resolve_decision(cand, evaluation, classification, scan_batch_id, summary)

            if decision == Decision.REJECT.value:
                if (classification is not None
                        and classification.label_decision == Decision.REJECT.value
                        and evaluation.decision != Decision.REJECT.value):
                    # Label-driven reject (e.g. fail-safe / Other-Unclassified).
                    self._reject_candidate(cand, "ai_label", evaluation,
                                           reason=ReasonCode.LABEL_UNCLASSIFIED.value)
                else:
                    self._reject_candidate(cand, "openai", evaluation)
                summary.rejected += 1
                continue
            if decision == Decision.WATCH.value:
                self._set_candidate_status(cand["candidate_id"], "watch")
                summary.watch += 1
                continue

            # decision == propose
            handled = self._handle_proposal(cand, evaluation, summary, scan_batch_id=scan_batch_id)
            if not handled:
                summary.rejected += 1

        # --- Close out the batch + scheduler run (raw UPDATE, like
        #     _set_proposal_status). -----------------------------------------
        done = timeutils.stamp()
        self.journal.conn.execute(
            "UPDATE scan_batches SET status = ?, completed_at_utc = ?, completed_at_sgt = ?, "
            "candidates_found = ?, proposals_created = ?, watch_count = ?, rejected_count = ?, "
            "blocked_count = ?, errors_count = ? WHERE scan_batch_id = ?",
            (
                RunStatus.COMPLETED.value, done.utc, done.local_sgt,
                summary.candidates, summary.proposed, summary.watch, summary.rejected,
                summary.risk_blocked, 0, scan_batch_id,
            ),
        )
        self.journal.conn.execute(
            "UPDATE scheduler_runs SET status = ?, completed_at_utc = ?, completed_at_sgt = ?, "
            "candidates_found = ?, proposals_created = ?, error_count = ? WHERE scheduler_run_id = ?",
            (
                RunStatus.COMPLETED.value, done.utc, done.local_sgt,
                summary.candidates, summary.proposed, 0, scheduler_run_id,
            ),
        )
        self.journal.conn.commit()

        self.journal.log_system_event(
            Severity.INFO, "scan",
            f"scan_once complete: {summary.proposed} proposed, {summary.watch} watch, "
            f"{summary.rejected} rejected, {summary.risk_blocked} risk-blocked, "
            f"{summary.auto_submitted} auto-submitted, {summary.pending_manual} pending.",
        )
        return summary

    def _handle_proposal(self, cand, evaluation, summary: ScanSummary, scan_batch_id=None) -> bool:
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
            self._tag_target_profile(proposal, from_config=evaluation.is_mock)
            proposal.scan_batch_id = scan_batch_id
            proposal.playbook_name = PLAYBOOK_V1
            proposal.setup_classification = "momentum_continuation"
            proposal.expected_hold_days = evaluation.max_holding_days
            # Persist the risk check (does NOT change whether the trade proceeds).
            rc_id = self._record_risk_check(proposal, evaluation, risk)
            proposal.risk_check_id = rc_id
            proposal.status = "blocked"
            self.journal.insert("trade_proposals", proposal.to_row())
            self._reject_candidate(cand, "risk", evaluation, reason=risk.primary_reason)
            summary.risk_blocked += 1
            return False

        proposal = self.swing.build_proposal(evaluation, risk.sizing)
        self._tag_target_profile(proposal, from_config=evaluation.is_mock)
        proposal.scan_batch_id = scan_batch_id
        proposal.playbook_name = PLAYBOOK_V1
        proposal.setup_classification = "momentum_continuation"
        proposal.expected_hold_days = evaluation.max_holding_days
        rc_id = self._record_risk_check(proposal, evaluation, risk)
        proposal.risk_check_id = rc_id
        proposal.status = "pending_approval"
        self.journal.insert("trade_proposals", proposal.to_row())
        # Roadmap 2.7: surface the polarity arming classification + high-risk
        # narrative warning on the proposal (advisory; never changes levels/sizing).
        arming_cls = cand.get("_arming_classification")
        warning = cand.get("_narrative_warning")
        if arming_cls or warning:
            self.journal.conn.execute(
                "UPDATE trade_proposals SET arming_classification = ?, narrative_warning = ? "
                "WHERE proposal_id = ?",
                (arming_cls, warning, proposal.proposal_id),
            )
        # Link the pre-trade baseline for this candidate to the trade's id.
        self.journal.conn.execute(
            "UPDATE baseline_outcomes SET trade_id = ? WHERE candidate_id = ? AND trade_id IS NULL",
            (proposal.trade_id, cand["candidate_id"]),
        )
        self.journal.conn.commit()
        self._set_candidate_status(cand["candidate_id"], "proposed")
        summary.proposed += 1

        # HIGH-RISK narrative (hype/meme/squeeze) is MANUAL-ONLY: never auto-approve,
        # regardless of approval mode. It still went through every risk/freshness gate.
        if (arming_cls == ArmingClassification.HIGH_RISK_NARRATIVE.value
                and self.settings.last30days_high_risk_narrative_manual_only):
            summary.pending_manual += 1
            return True

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

        # Idempotency (belt-and-suspenders on top of the status guard): if a live
        # entry order already exists for this proposal, never create a duplicate.
        # Exit orders carry proposal_id=NULL, so only the entry order matches here.
        _dead = (
            OrderState.REJECTED.value, OrderState.CANCELLED.value,
            OrderState.EXPIRED.value, OrderState.FAILED.value,
        )
        existing = [o for o in self.journal.orders_for_proposal(proposal_id) if o.get("state") not in _dead]
        if existing:
            return False, f"already approved/executed (order {existing[0]['order_id']})"

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
        # Persist the manual-approval re-check (audit only; the decision above
        # already determined whether the trade proceeds) and keep the proposal's
        # denormalized risk_check_id pointing at the approval-time check.
        rc_id = self._record_risk_check(proposal, _eval_view_from_proposal(proposal), risk)
        if rc_id:
            self.journal.conn.execute(
                "UPDATE trade_proposals SET risk_check_id = ? WHERE proposal_id = ?", (rc_id, proposal_id)
            )
            self.journal.conn.commit()
        if not risk.approved:
            # Journal the block explicitly (uniform with the freshness/drift
            # blocks above); the risk_check row recorded just above carries the
            # per-gate detail.
            self.journal.log_system_event(
                Severity.WARNING, "approval",
                f"Approval blocked for {proposal.symbol}: risk {risk.primary_reason}.",
                {"proposal_id": proposal_id, "reason_code": risk.primary_reason},
            )
            self._set_proposal_status(proposal_id, "blocked")
            return False, f"risk blocked: {risk.primary_reason}"

        self.approvals.approve_manually(proposal, approver=approver, freshness_ok=True, risk_ok=True)
        result = self._execute(proposal, fill_price=cur)
        if result.blocked:
            self._set_proposal_status(proposal_id, "blocked")
            return False, f"execution blocked: {result.block_reason}"
        # Status lifecycle: a real broker order may be ACCEPTED but not yet filled
        # (it fills later via reconcile). Only mark 'filled' once the fill is
        # confirmed; otherwise mark 'submitted'. Simulated fills are immediate.
        if result.state == OrderState.FILLED.value:
            self._set_proposal_status(proposal_id, "filled")
            verb = "filled"
        else:
            self._set_proposal_status(proposal_id, "submitted")
            verb = "submitted"
        # Cost-model calibration capture (best-effort, AFTER the order — never gates
        # execution). Records the approval-time market context + modeled assumptions.
        self._record_execution_calibration(proposal, snap, result)
        return True, f"approved + {verb} ({result.protection_path})"

    def reject_proposal(self, proposal_id: str, approver: str = "user", reason: str = "user rejected"):
        row = self.journal.proposal_by_id(proposal_id)
        if not row:
            return False, "proposal not found"
        proposal = TradeProposal.from_row(row)
        self.approvals.reject_manually(proposal, approver=approver, reason=reason)
        self._set_proposal_status(proposal_id, "rejected")
        return True, "rejected"

    # ----------------------------------------------- Roadmap 2.8: User Override
    _OVERRIDE_AGGRESSIVENESS = {
        UserOverrideAction.WATCH_TO_TRADE.value: OverrideAggressiveness.MORE_AGGRESSIVE.value,
        UserOverrideAction.REJECT_TO_TRADE.value: OverrideAggressiveness.MORE_AGGRESSIVE.value,
        UserOverrideAction.REJECT_TO_WATCH.value: OverrideAggressiveness.MORE_AGGRESSIVE.value,
        UserOverrideAction.INCREASE_SIZE.value: OverrideAggressiveness.MORE_AGGRESSIVE.value,
        UserOverrideAction.NORMAL_TO_HIGH_CONVICTION.value: OverrideAggressiveness.MORE_AGGRESSIVE.value,
        UserOverrideAction.PROPOSE_TO_REJECT.value: OverrideAggressiveness.MORE_CONSERVATIVE.value,
        UserOverrideAction.REDUCE_SIZE.value: OverrideAggressiveness.MORE_CONSERVATIVE.value,
        UserOverrideAction.LONG_TO_SHORT.value: OverrideAggressiveness.DIRECTION_CHANGE.value,
        UserOverrideAction.SHORT_TO_LONG.value: OverrideAggressiveness.DIRECTION_CHANGE.value,
        UserOverrideAction.MANUAL_EXIT.value: OverrideAggressiveness.EXIT_OVERRIDE.value,
        UserOverrideAction.MANUAL_HOLD.value: OverrideAggressiveness.HOLD_OVERRIDE.value,
    }

    @staticmethod
    def _eval_from_row(row: dict) -> OpenAIEvaluation:
        return OpenAIEvaluation(
            eval_id=row.get("eval_id") or new_id("eval"), candidate_id=row.get("candidate_id"),
            symbol=row.get("symbol"), model=row.get("model") or "unknown",
            direction=row.get("direction") or TradeDirection.LONG.value,
            entry=row.get("entry"), stop=row.get("stop"), target=row.get("target"),
            max_holding_days=row.get("max_holding_days"), expected_r=row.get("expected_r"),
            confidence=row.get("confidence"), decision=row.get("decision") or Decision.WATCH.value,
            reasoning_summary=row.get("reasoning_summary") or "", is_mock=bool(row.get("is_mock")),
        )

    @staticmethod
    def _nightdesk_flag(action, arming_cls, a_final):
        if arming_cls == ArmingClassification.HIGH_RISK_NARRATIVE.value:
            return True, "high_risk_narrative_override"
        if action in (UserOverrideAction.WATCH_TO_TRADE.value, UserOverrideAction.REJECT_TO_TRADE.value) \
                and a_final != Decision.PROPOSE.value:
            return True, "user_traded_against_alphaos_recommendation"
        if action == UserOverrideAction.PROPOSE_TO_REJECT.value:
            return True, "user_rejected_alphaos_proposal"
        return False, None

    def create_user_override(self, candidate_id, action, reason_code=None, note=None,
                             direction=None, size=None, approver="user") -> dict:
        """Record a USER OVERRIDE as a SEPARATE decision layer. It NEVER rewrites
        AlphaOS's original recommendation (both are stored), NEVER bypasses the
        risk/freshness/spread/liquidity gates, manual approval, or the real-money
        guard. `watch_to_trade` only ever creates a PENDING_APPROVAL proposal —
        the user must still `approve` it. Returns {ok, message, override}."""
        self._ensure_startup()
        action = str(action)
        cand = self.journal.one("SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,))
        if not cand:
            return {"ok": False, "message": f"candidate {candidate_id} not found"}
        symbol = cand.get("symbol")
        adj = self.journal.one(
            "SELECT * FROM decision_adjustments WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
            (candidate_id,)) or {}
        ev = self.journal.evaluation_for_candidate(candidate_id) or {}
        a_final = adj.get("final_decision") or cand.get("label_decision") or ev.get("decision")
        arming_cls = adj.get("arming_classification") or cand.get("arming_classification")
        armed_watch = bool(cand.get("armed_watch") or adj.get("armed_watch"))

        rec = {
            "override_id": new_id("ovr"), "candidate_id": candidate_id, "proposal_id": None,
            "symbol": symbol,
            "alphaos_eval_decision": adj.get("eval_decision") or ev.get("decision"),
            "alphaos_label_decision": adj.get("label_decision") or cand.get("label_decision"),
            "alphaos_final_decision": a_final,
            "alphaos_direction": ev.get("direction") or cand.get("direction"),
            "alphaos_confidence": ev.get("confidence"),
            "alphaos_reasoning_summary": ev.get("reasoning_summary"),
            "armed_watch": 1 if armed_watch else 0, "arming_classification": arming_cls,
            "user_override_action": action, "user_final_decision": None,
            "user_direction": direction or (ev.get("direction") or cand.get("direction")),
            "user_size_override": size, "user_reason_code": reason_code, "user_reason_text": note,
            "override_aggressiveness": self._OVERRIDE_AGGRESSIVENESS.get(action),
            "execution_allowed": 0, "blocked_reason": None, "execution_result": None,
            "linked_order_id": None, "linked_trade_id": None,
            "outcome_r": None, "outcome_pnl": None,
            "outcome_status": OverrideOutcomeStatus.PENDING.value,
            "alphaos_would_have_traded": 1 if a_final == Decision.PROPOSE.value else 0,
            "user_did_trade": 0, "attribution_result": AttributionResult.PENDING.value,
        }

        if action in (UserOverrideAction.WATCH_TO_TRADE.value, UserOverrideAction.REJECT_TO_TRADE.value):
            rec["user_final_decision"] = Decision.PROPOSE.value
            msg = self._override_open_trade(cand, ev, rec, approver)
        elif action == UserOverrideAction.PROPOSE_TO_REJECT.value:
            rec["user_final_decision"] = Decision.REJECT.value
            prop = self.journal.one(
                "SELECT * FROM trade_proposals WHERE candidate_id = ? "
                "AND status IN ('pending_approval','proposed') ORDER BY id DESC LIMIT 1", (candidate_id,))
            if not prop:
                rec["blocked_reason"] = OverrideBlockedReason.NO_PROPOSAL.value
                msg = "no open proposal to reject"
            else:
                self.reject_proposal(prop["proposal_id"], approver=approver,
                                     reason=f"user_override: {reason_code or note or 'rejected'}")
                rec["proposal_id"] = prop["proposal_id"]
                msg = f"AlphaOS proposal {prop['proposal_id']} rejected by user override"
        elif action == UserOverrideAction.MANUAL_EXIT.value:
            rec["user_final_decision"] = "exit"
            pos = self.journal.one(
                "SELECT * FROM positions WHERE symbol = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
                (symbol,))
            if not pos:
                rec["blocked_reason"] = OverrideBlockedReason.NO_OPEN_POSITION.value
                msg = "no open position to exit"
            else:
                rec["linked_trade_id"] = pos.get("trade_id")
                msg = "manual exit recorded (use `alphaos flatten` to close — PAPER-only, manual)"
        elif action == UserOverrideAction.MANUAL_HOLD.value:
            rec["user_final_decision"] = "hold"
            msg = "manual hold recorded"
        else:
            rec["user_final_decision"] = action
            msg = f"override '{action}' recorded (no automatic action wired in v1)"

        nd, nd_reason = self._nightdesk_flag(action, arming_cls, a_final)
        rec["nightdesk_research_candidate"] = 1 if nd else 0
        rec["nightdesk_research_reason"] = nd_reason

        self.journal.insert("user_decision_overrides", rec)
        self.journal.log_system_event(
            Severity.INFO, "user_override",
            f"user override {action} for {symbol}: {msg}",
            {"override_id": rec["override_id"], "candidate_id": candidate_id},
        )
        return {"ok": True, "message": msg, "override": rec}

    def _override_open_trade(self, cand, ev_row, rec, approver) -> str:
        """watch/reject -> trade: build a proposal from the stored eval, re-run the
        SAME freshness + risk gates, and (only if they pass) create a PENDING_APPROVAL
        proposal tagged as a user override. NEVER executes here — manual approval is
        still required. On any gate failure the override is recorded with
        execution_allowed=0 + a blocked_reason."""
        symbol = cand.get("symbol")
        if not ev_row or ev_row.get("entry") is None or ev_row.get("stop") is None or ev_row.get("target") is None:
            rec["blocked_reason"] = OverrideBlockedReason.OTHER.value
            rec["execution_result"] = "no usable eval levels for this candidate"
            return "blocked: no usable eval levels (cannot build a trade)"
        evaluation = self._eval_from_row(ev_row)
        if rec.get("user_direction"):
            evaluation.direction = rec["user_direction"]
        direction = evaluation.direction or TradeDirection.LONG.value
        requires_margin = direction == TradeDirection.SHORT.value

        # Freshness re-check (mandatory before any order path).
        snap = self.market.get_snapshot(symbol)
        report = self.freshness.assess(snap)
        if not report.is_usable:
            rec["blocked_reason"] = OverrideBlockedReason.STALE_DATA.value
            rec["execution_result"] = f"{report.freshness_status}/{report.block_reason}"
            return f"blocked: data not usable ({report.freshness_status})"

        # Risk re-check (sizing, spread, liquidity, exposure, daily cap, R:R...).
        risk = self.risk.assess(
            direction=direction, entry=evaluation.entry, stop=evaluation.stop, snapshot=snap,
            open_positions=self.journal.count_open_positions(),
            trades_today=self.journal.count_paper_orders_today(),
            realized_pnl_today=self.journal.realized_pnl_today(),
            requires_margin=requires_margin, margin_approved=False,
        )
        if not risk.approved or risk.sizing is None:
            codes = {b.get("code") for b in (risk.block_reasons or [])}
            if ReasonCode.WIDE_SPREAD.value in codes:
                br = OverrideBlockedReason.WIDE_SPREAD.value
            elif ReasonCode.LOW_LIQUIDITY.value in codes:
                br = OverrideBlockedReason.LOW_LIQUIDITY.value
            else:
                br = OverrideBlockedReason.RISK_GATE_FAILED.value
            rec["blocked_reason"] = br
            rec["execution_result"] = risk.primary_reason
            return f"blocked: risk gate ({risk.primary_reason})"

        # Gates passed -> create a PENDING_APPROVAL proposal (NOT executed).
        proposal = self.swing.build_proposal(evaluation, risk.sizing)
        self._tag_target_profile(proposal, from_config=evaluation.is_mock)
        proposal.playbook_name = PLAYBOOK_V1
        proposal.setup_classification = "user_override"
        proposal.expected_hold_days = evaluation.max_holding_days
        proposal.proposal_reason = f"user_override:{rec['user_override_action']}"
        proposal.status = "pending_approval"
        rc_id = self._record_risk_check(proposal, evaluation, risk)
        proposal.risk_check_id = rc_id
        self.journal.insert("trade_proposals", proposal.to_row())
        warning = (HIGH_RISK_NARRATIVE_WARNING
                   if rec.get("arming_classification") == ArmingClassification.HIGH_RISK_NARRATIVE.value else None)
        self.journal.conn.execute(
            "UPDATE trade_proposals SET arming_classification = ?, narrative_warning = ? WHERE proposal_id = ?",
            (rec.get("arming_classification"), warning, proposal.proposal_id),
        )
        self.journal.conn.commit()
        rec["proposal_id"] = proposal.proposal_id
        rec["linked_trade_id"] = proposal.trade_id
        rec["execution_allowed"] = 1
        return (f"gates passed -> PENDING_APPROVAL proposal {proposal.proposal_id} created "
                f"(MANUAL approval still required: `alphaos approve {proposal.proposal_id}`)")

    def resolve_user_override(self, override_id, outcome_r=None, outcome_pnl=None,
                              outcome_status=None, did_trade=None) -> dict:
        """Record the outcome of a user override after close + compute a preliminary
        attribution (user vs AlphaOS). Heuristic only — not statistically significant
        on small samples."""
        row = self.journal.one("SELECT * FROM user_decision_overrides WHERE override_id = ?", (override_id,))
        if not row:
            return {"ok": False, "message": "override not found"}
        status = outcome_status or row.get("outcome_status")
        alphaos_traded = bool(row.get("alphaos_would_have_traded"))
        attribution = AttributionResult.INCONCLUSIVE.value
        if status == OverrideOutcomeStatus.WON.value:
            attribution = (AttributionResult.USER_OUTPERFORMED.value if not alphaos_traded
                           else AttributionResult.INCONCLUSIVE.value)
        elif status == OverrideOutcomeStatus.LOST.value:
            attribution = (AttributionResult.ALPHAOS_OUTPERFORMED.value if not alphaos_traded
                           else AttributionResult.INCONCLUSIVE.value)
        st = timeutils.stamp()
        self.journal.conn.execute(
            "UPDATE user_decision_overrides SET outcome_r = ?, outcome_pnl = ?, outcome_status = ?, "
            "user_did_trade = ?, attribution_result = ?, resolved_at_utc = ?, resolved_at_sgt = ? "
            "WHERE override_id = ?",
            (outcome_r, outcome_pnl, status,
             1 if (did_trade if did_trade is not None else row.get("user_did_trade")) else 0,
             attribution, st.utc, st.local_sgt, override_id),
        )
        self.journal.conn.commit()
        return {"ok": True, "override_id": override_id, "attribution_result": attribution}

    # ----------------------------------------------------- approval center view
    def list_open_proposals(self) -> list[dict]:
        """Read-only Approval Center view: the actionable proposal queue enriched
        with derived decision-support fields. PURE READS — never writes, so it is
        safe to call on every dashboard render. Live freshness/spread/risk are
        re-checked at approval time, not here (that would fetch + persist data)."""
        views: list[dict] = []
        for row in self.journal.open_proposals():
            direction = row.get("direction") or TradeDirection.LONG.value
            fresh = self.journal.latest_freshness_for_symbol(row["symbol"]) or {}
            views.append(
                {
                    "proposal_id": row.get("proposal_id"),
                    "trade_id": row.get("trade_id"),
                    "candidate_id": row.get("candidate_id"),
                    "symbol": row.get("symbol"),
                    "direction": direction,
                    "side": "sell_short" if direction == TradeDirection.SHORT.value else "buy",
                    "entry": row.get("entry"),
                    "stop": row.get("stop"),
                    "target": row.get("target"),
                    "qty": row.get("qty"),
                    "risk_per_share": row.get("risk_per_share"),
                    "risk_amount": row.get("dollar_risk"),
                    "expected_r": row.get("expected_r"),
                    "reward_risk": _reward_risk(direction, row.get("entry"), row.get("stop"), row.get("target")),
                    "requires_margin": bool(row.get("requires_margin")),
                    "status": row.get("status"),
                    "generated_at_utc": row.get("created_at_utc"),
                    "generated_at_sgt": row.get("created_at_sgt"),
                    "last_known_freshness": fresh.get("freshness_status") or "n/a (re-checked at approval)",
                }
            )
        return views

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
        # Record a scheduler run for this monitor pass (records exist even though
        # v1 has no real scheduler). Keep the monitor behavior itself unchanged.
        scheduler_run_id = new_id("schr")
        st = timeutils.stamp()
        positions_seen = self.journal.count_open_positions()
        self.journal.insert(
            "scheduler_runs",
            {
                "scheduler_run_id": scheduler_run_id,
                "run_type": SchedulerRunType.MONITOR.value,
                "trigger_source": TriggerSource.MANUAL_CLI.value,
                "started_at_utc": st.utc,
                "started_at_sgt": st.local_sgt,
                "status": RunStatus.STARTED.value,
                "positions_touched": positions_seen,
            },
        )
        # Reconcile real Alpaca paper orders first (broker-managed bracket OCO):
        # opens positions on entry fills, closes them on TP/SL leg fills.
        recon = self.orders.reconcile()
        # Local watchdog handles only simulated_internal positions.
        exits = self.positions.monitor(price_overrides=price_overrides)
        all_exits = list(recon.get("exits", [])) + exits
        done = timeutils.stamp()
        self.journal.conn.execute(
            "UPDATE scheduler_runs SET status = ?, completed_at_utc = ?, completed_at_sgt = ?, "
            "positions_touched = ?, error_count = ? WHERE scheduler_run_id = ?",
            (RunStatus.COMPLETED.value, done.utc, done.local_sgt, positions_seen, 0, scheduler_run_id),
        )
        self.journal.conn.commit()
        self.journal.log_system_event(
            Severity.INFO, "monitor",
            f"monitor_once complete: {len(all_exits)} exit(s); "
            f"reconciled {recon.get('reconciled', 0)} alpaca_paper order(s), "
            f"opened {len(recon.get('opened', []))}.",
        )
        return {
            "exits": all_exits,
            "reconciled": recon.get("reconciled", 0),
            "opened": recon.get("opened", []),
            "open_positions": self.journal.count_open_positions(),
            "scheduler_run_id": scheduler_run_id,
        }

    # --------------------------------------------------------------- report
    def generate_daily_report(self) -> dict:
        self._ensure_startup()
        return self.recon.generate()

    # ------------------------------------------- cost calibration / broker hygiene
    def calibration_report(self) -> dict:
        """Cost-model calibration: modeled vs actual Alpaca paper execution."""
        from alphaos.reports.cost_calibration import build_calibration_report

        return build_calibration_report(self.journal, self.settings)

    def attribution_report(self, limit: int = 1000) -> dict:
        """User-override attribution: AlphaOS recommendation vs the user's final
        decision, who outperformed, win rate/expectancy. PURE READ — heuristic /
        descriptive only, never a significance claim on a small sample."""
        from alphaos.reports.attribution import build_attribution_report

        return build_attribution_report(self.journal, self.settings, limit=limit)

    def flatten_paper_account(self) -> dict:
        """Paper-ONLY: cancel all open Alpaca paper orders + close all open Alpaca
        paper positions. Refuses unless the paper-only guardrails hold; the broker
        connector is hard-wired paper=True, so this can never touch real money."""
        self._ensure_startup()
        alpaca = getattr(self.orders, "alpaca", None)
        if not (self.settings.is_paper and self.settings.has_alpaca_keys and alpaca):
            return {"ok": False, "reason": "alpaca paper not connected (need paper mode + Alpaca creds)"}
        try:
            alpaca.preflight()
        except Exception as exc:
            return {"ok": False, "reason": f"paper-only preflight failed: {exc}"}
        summary = alpaca.flatten_paper()
        self.journal.log_system_event(
            Severity.WARNING, "broker",
            f"FLATTEN paper account: cancelled {summary.get('cancelled_orders')} order(s), "
            f"closed {summary.get('closed_positions')} position(s).",
            summary,
        )
        return {"ok": True, **summary}

    def broker_ledger_report(self) -> dict:
        """Broker-vs-ledger reconciliation: detect mismatches, orphan broker
        orders/positions, and orphan ledger positions. Read-only."""
        from alphaos.reports.broker_recon import build_broker_ledger_report

        return build_broker_ledger_report(self.journal, getattr(self.orders, "alpaca", None))

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
        self._tag_target_profile(proposal, from_config=True)
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

    def _record_execution_calibration(self, proposal, snap, result) -> None:
        """Persist a cost-calibration row for an approved order (best-effort audit
        write AFTER execution; never raises into the approval/execution path)."""
        try:
            if not result or result.blocked or not result.order:
                return
            from alphaos.reports.cost_calibration import build_calibration_row

            row = build_calibration_row(self.settings, proposal, snap or {}, result.order)
            self.journal.insert("execution_calibration", row)
        except Exception as exc:  # pragma: no cover - defensive (audit-only)
            try:
                self.journal.log_system_event(
                    Severity.WARNING, "calibration",
                    f"calibration capture failed for {getattr(proposal, 'proposal_id', None)}; "
                    "order unaffected.",
                    {"error": str(exc)},
                )
            except Exception:
                pass

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

    # -------------------------------------------- 2.3 interest ranking + labels
    def _rank_candidates(self, candidates: list) -> None:
        """Assign interest_rank (1-based; highest interest first) and persist it.
        Rank is metadata for the AI-labelling shortlist + dashboard; it does not
        reorder the scan or change any trade decision."""
        ordered = sorted(
            candidates,
            key=lambda c: (c.get("interest_score") or 0.0, c.get("momentum_score") or 0.0),
            reverse=True,
        )
        for rank, c in enumerate(ordered, start=1):
            c["interest_rank"] = rank
            self.journal.conn.execute(
                "UPDATE candidates SET interest_rank = ? WHERE candidate_id = ?",
                (rank, c["candidate_id"]),
            )
        self.journal.conn.commit()

    def _label_candidate(self, cand: dict, snapshot: dict, scan_batch_id, enrich: bool = True,
                         l30_mode: Optional[str] = None):
        """Build the compact packet, (optionally) enrich it with catalyst +
        last30days context, journal it, AI-classify it, and freeze the label +
        catalyst + last30days view onto the candidate. Returns the
        PlaybookClassification (advisory). ``l30_mode`` is one of: 'enrich'
        (within the per-scan cap), 'skipped_budget_cap' (eligible but outside it),
        or None (last30days disabled)."""
        signals = cand.get("_interest")
        if signals is None:  # defensive: recompute if the scanner didn't attach it
            from alphaos.scanner.interest_scanner import InterestScanner

            signals = InterestScanner(self.settings).score(snapshot)
        packet = build_packet(cand, snapshot, signals, cand.get("interest_rank"))
        # Roadmap 2.4: official catalyst enrichment BEFORE labelling, so the AI can
        # use catalyst context for thesis/risk. Fail-safe, context only — never
        # forces a decision, bypasses a gate, or executes. Cost-capped via `enrich`.
        catalyst = None
        if enrich:
            catalyst = self.enricher.enrich(packet)
            packet.apply_catalyst(catalyst)
        # Roadmap 2.5: last30days narrative context, AFTER catalyst, BEFORE labelling.
        # Context only — fail-safe; never forces a decision, bypasses a gate, affects
        # sizing, overwrites a label, or executes. Only fed to the labeller when
        # LAST30DAYS_FEED_TO_LABELLER is on; always journaled either way.
        last30 = None
        if l30_mode == "enrich":
            last30 = self.l30_enricher.enrich(
                packet, rank=cand.get("interest_rank"), interest_score=signals.interest_score)
            if self.settings.last30days_feed_to_labeller:
                packet.apply_last30days(last30)
        elif l30_mode == "skipped_budget_cap":
            last30 = self.l30_enricher.skipped_budget_cap(
                packet, rank=cand.get("interest_rank"), interest_score=signals.interest_score)
        self.journal.insert("candidate_packets", packet.to_row(scan_batch_id))
        classification = self.labeller.classify(packet)
        if catalyst is not None:
            # Advisory label-review: if the catalyst implies a different OFFICIAL
            # label than the frozen one, flag for review — NEVER overwrite primary_label.
            catalyst.label_review_required = bool(
                catalyst.catalyst_suggested_label
                and catalyst.catalyst_suggested_label != classification.primary_label
            )
            self.journal.insert(
                "candidate_catalysts",
                catalyst.to_row(cand["candidate_id"], packet.packet_id, scan_batch_id),
            )
        if last30 is not None:
            self.journal.insert(
                "candidate_last30days",
                last30.to_row(cand["candidate_id"], packet.packet_id, scan_batch_id),
            )
        # Roadmap 2.7: LLM narrative polarity over the live last30days clusters.
        # Only when enrichment found a real narrative (status 'available'); SEPARATE
        # evidence; fail-safe. It can ARM an override upgrade (gated, deterministic)
        # but never trades, bypasses a gate, or skips approval.
        polarity = None
        if (last30 is not None and self.settings.last30days_polarity_enabled
                and last30.last30days_status == Last30DaysStatus.AVAILABLE.value):
            ev = PolarityEvidence(
                candidate_id=cand["candidate_id"], symbol=packet.symbol,
                direction=cand.get("direction") or getattr(signals, "direction_hint", "long"),
                structure_hint=getattr(signals, "structure_hint", None),
                provider=getattr(last30, "provider", None),
                cluster_titles=list(getattr(last30, "top_themes", []) or []),
                cluster_summaries=([last30.summary] if getattr(last30, "summary", None) else []),
                source_coverage=list(getattr(last30, "source_coverage", []) or []),
                source_coverage_count=len(getattr(last30, "source_coverage", []) or []),
                catalyst_summary=getattr(catalyst, "catalyst_summary", None),
                eval_decision=None, label_decision=classification.label_decision,
            )
            polarity = self.polarity.classify(ev)
            self.journal.insert("last30days_polarity", polarity.to_row(scan_batch_id, packet.packet_id))
        self._freeze_label(cand, packet, classification, scan_batch_id, catalyst, last30, polarity)
        # Stash the advisory context so _resolve_decision can (a) decide whether a
        # real driver justifies a symmetric override and (b) record the driver.
        cand["_catalyst"] = catalyst
        cand["_last30"] = last30
        cand["_polarity"] = polarity
        cand["_packet_id"] = packet.packet_id
        return classification

    def _freeze_label(self, cand, packet, classification, scan_batch_id, catalyst=None,
                      last30=None, polarity=None) -> None:
        """Persist the label (append-only history) + freeze the current view onto
        the candidate, including the advisory catalyst (2.4) + last30days (2.5) +
        polarity (2.7) views. History is never rewritten; none of catalyst /
        last30days / polarity EVER overwrites primary_label — only advisory fields."""
        frozen_at = timeutils.now_utc().isoformat()
        self.journal.insert(
            "candidate_labels",
            classification.to_row(packet.packet_id, scan_batch_id, frozen_at),
        )
        cs = catalyst.catalyst_status if catalyst else None
        ct = catalyst.catalyst_type if catalyst else None
        csl = catalyst.catalyst_suggested_label if catalyst else None
        lrr = 1 if ((catalyst and catalyst.label_review_required)
                    or (last30 and last30.label_review_required)) else 0
        l30s = last30.last30days_status if last30 else None
        sentl = last30.sentiment_label if last30 else None
        pol_label = polarity.sentiment_label if polarity else None
        pol_align = polarity.direction_alignment if polarity else None
        pol_driver = polarity.narrative_driver_type if polarity else None
        pol_arming = polarity.arming_classification if polarity else None
        self.journal.conn.execute(
            "UPDATE candidates SET primary_label = ?, secondary_labels_json = ?, "
            "candidate_tags_json = ?, risk_tags_json = ?, label_confidence = ?, "
            "label_decision = ?, label_version = ?, label_source = ?, label_frozen_at_utc = ?, "
            "catalyst_status = ?, catalyst_type = ?, catalyst_suggested_label = ?, label_review_required = ?, "
            "last30days_status = ?, sentiment_label = ?, "
            "polarity_label = ?, polarity_alignment = ?, narrative_driver_type = ?, arming_classification = ? "
            "WHERE candidate_id = ?",
            (
                classification.primary_label,
                json.dumps(classification.secondary_labels),
                json.dumps(classification.candidate_tags),
                json.dumps(classification.risk_tags),
                classification.confidence,
                classification.label_decision,
                classification.label_version,
                classification.label_source,
                frozen_at,
                cs, ct, csl, lrr,
                l30s, sentl,
                pol_label, pol_align, pol_driver, pol_arming,
                cand["candidate_id"],
            ),
        )
        self.journal.conn.commit()

    _DECISION_ORDER = {Decision.REJECT.value: 0, Decision.WATCH.value: 1, Decision.PROPOSE.value: 2}
    _DECISION_INV = {0: Decision.REJECT.value, 1: Decision.WATCH.value, 2: Decision.PROPOSE.value}

    @staticmethod
    def _apply_label_floor(base_decision: str, label_decision: str) -> str:
        """Downgrade-only: the AI label can RESTRICT the trade decision but never
        expand it. Returns the more restrictive of the two."""
        order = Orchestrator._DECISION_ORDER
        return Orchestrator._DECISION_INV[min(order.get(base_decision, 0), order.get(label_decision, 0))]

    # ------------------------------------------- Roadmap 2.6: gated override
    def _override_armed(self) -> bool:
        """Globally ARMED only when the operator opted in AND the AI is real (a key
        is present and we're not in mock mode). While mock, this is always False, so
        the label stays strictly downgrade-only — the override is inert until the
        signals driving it are real."""
        return bool(
            self.settings.labeller_decision_override_enabled
            and self.settings.has_openai_key
            and not self.settings.is_mock
        )

    # Catalyst types that clearly OPPOSE a direction — never a positive upgrade
    # driver for it (an analyst downgrade / legal-regulatory hit can't upgrade a
    # long; an upgrade / launch / partnership can't upgrade a short).
    _BEARISH_CATALYSTS = frozenset({CatalystType.ANALYST_DOWNGRADE.value,
                                    CatalystType.LEGAL_REGULATORY.value})
    _BULLISH_CATALYSTS = frozenset({CatalystType.ANALYST_UPGRADE.value,
                                    CatalystType.PRODUCT_LAUNCH.value,
                                    CatalystType.PARTNERSHIP.value})

    @staticmethod
    def _real_decision_driver(catalyst, last30, direction, polarity=None) -> tuple:
        """Return (has_real_positive_driver, driver_str, detail) — whether a real,
        LIVE, POSITIVE driver exists to ARM an upgrade aligned with the trade
        direction. ONLY these qualify:

        * a LIVE catalyst (source not mock/disabled/none) that is **confirmed or
          possible** AND whose type does not clearly oppose the direction; or
        * a last30days POLARITY (2.7) whose DETERMINISTIC arming decision is
          ``should_arm_override`` (aligned + high-confidence + covered + no catalyst
          conflict + arming enabled). The arming_classification (normal_driver /
          high_risk_narrative) is carried through in the detail.

        If polarity is absent (disabled), it falls back to the pre-2.7 raw-sentiment
        check (which never arms for keyless 'unknown' sentiment). Everything else is
        rejected. Downgrades never need a driver — they are always the safe direction.
        """
        is_long = (direction or TradeDirection.LONG.value) != TradeDirection.SHORT.value
        drivers, detail = [], {}

        cs = getattr(catalyst, "catalyst_status", None)
        csrc = getattr(catalyst, "enrichment_source", None)
        ctype = getattr(catalyst, "catalyst_type", None)
        if (csrc not in (None, EnrichmentSource.MOCK.value, EnrichmentSource.DISABLED.value,
                         EnrichmentSource.NONE.value)
                and cs in (CatalystStatus.CONFIRMED.value, CatalystStatus.POSSIBLE.value)):
            opposing = (ctype in Orchestrator._BEARISH_CATALYSTS) if is_long \
                else (ctype in Orchestrator._BULLISH_CATALYSTS)
            if not opposing:
                drivers.append(f"catalyst:{cs}:{ctype}")
                detail["catalyst"] = {"status": cs, "type": ctype, "source": csrc}

        if polarity is not None:
            # 2.7: the AlphaOS-side deterministic arming decision already enforced
            # alignment + confidence + coverage + no-conflict + config. Trust it here.
            if getattr(polarity, "should_arm_override", False):
                drivers.append(f"last30days:{polarity.sentiment_label}:{polarity.arming_classification}")
                detail["last30days"] = {
                    "sentiment": polarity.sentiment_label,
                    "alignment": polarity.direction_alignment,
                    "driver_type": polarity.narrative_driver_type,
                    "arming_classification": polarity.arming_classification,
                    "confidence": polarity.confidence,
                    "provider": polarity.provider,
                }
        else:
            # pre-2.7 fallback: raw supportive sentiment from a live cli provider.
            ls = getattr(last30, "last30days_status", None)
            lsrc = getattr(last30, "provider", None)
            sent = getattr(last30, "sentiment_label", None)
            supportive = (sent == SentimentLabel.BULLISH.value) if is_long \
                else (sent == SentimentLabel.BEARISH.value)
            if (lsrc == Last30DaysProvider.CLI.value and ls == Last30DaysStatus.AVAILABLE.value
                    and supportive):
                drivers.append(f"last30days:{sent}")
                detail["last30days"] = {"status": ls, "sentiment": sent, "provider": lsrc}

        return (bool(drivers), "; ".join(drivers), detail)

    def _combine_decision(self, base: str, label: str, eval_levels_ok: bool,
                          override_active: bool) -> str:
        """Combine the no-news eval decision with the advisory label decision.

        * Not armed -> downgrade-only (the legacy, always-safe floor).
        * Armed -> the label is authoritative and may move the call UP or DOWN,
          EXCEPT it can never UPGRADE a non-tradeable eval (no valid levels /
          unusable freshness — a data-integrity reject). Narrative never overrides
          a data-quality block.
        """
        order = self._DECISION_ORDER
        if not override_active:
            return self._apply_label_floor(base, label)
        if order.get(label, 0) > order.get(base, 0) and not eval_levels_ok:
            return base
        return label

    def _resolve_decision(self, cand, evaluation, classification, scan_batch_id, summary) -> str:
        """Compute the final trade decision from the eval + label, applying the
        gated symmetric override, and ALWAYS record how/why it moved (audit for
        learning). Returns the final decision; downstream gates + manual approval
        are unchanged and still authoritative."""
        base = evaluation.decision
        label = classification.label_decision
        catalyst = cand.get("_catalyst")
        last30 = cand.get("_last30")
        polarity = cand.get("_polarity")
        has_driver, driver_str, driver_detail = self._real_decision_driver(
            catalyst, last30, evaluation.direction, polarity)
        override_active = self._override_armed() and has_driver
        eval_levels_ok = (
            evaluation.entry is not None and evaluation.stop is not None
            and evaluation.target is not None
            and getattr(evaluation, "data_freshness_status", "usable") == "usable"
        )
        final = self._combine_decision(base, label, eval_levels_ok, override_active)

        order = self._DECISION_ORDER
        if order.get(final, 0) > order.get(base, 0):
            adjustment = DecisionAdjustment.UPGRADED.value
            summary.decision_upgraded += 1
        elif order.get(final, 0) < order.get(base, 0):
            adjustment = DecisionAdjustment.DOWNGRADED.value
            summary.decision_downgraded += 1
        else:
            adjustment = DecisionAdjustment.UNCHANGED.value

        # If a high-risk narrative (hype/meme/squeeze) drove an UPGRADE, tag the
        # candidate so the proposal carries the warning + is forced manual-only.
        l30_detail = (driver_detail or {}).get("last30days") or {}
        arming_cls = (l30_detail.get("arming_classification")
                      or getattr(polarity, "arming_classification", None))
        if (adjustment == DecisionAdjustment.UPGRADED.value
                and arming_cls == ArmingClassification.HIGH_RISK_NARRATIVE.value):
            cand["_arming_classification"] = ArmingClassification.HIGH_RISK_NARRATIVE.value
            cand["_narrative_warning"] = getattr(polarity, "warning_message", "") or ""
            summary.high_risk_narrative += 1
        elif adjustment == DecisionAdjustment.UPGRADED.value and arming_cls:
            cand["_arming_classification"] = arming_cls

        # Roadmap 2.8 (Part A) — ARMED WATCH: the override armed a real driver but
        # the decision stayed WATCH (no upgrade) because eval/labeller produced no
        # higher actionable decision. A near-action watchlist item, NOT a reject.
        armed_watch = (override_active and final == Decision.WATCH.value
                       and adjustment == DecisionAdjustment.UNCHANGED.value)
        armed_watch_reason = None
        if armed_watch:
            armed_watch_reason = (ArmedWatchReason.LABELLER_DID_NOT_UPGRADE.value
                                  if label != Decision.PROPOSE.value
                                  else ArmedWatchReason.EVAL_NOT_TRADEABLE.value)
            summary.armed_watch += 1

        self._record_decision_adjustment(
            cand, evaluation, classification, base, final, adjustment,
            override_active, driver_str, driver_detail, catalyst, last30, scan_batch_id,
            armed_watch=armed_watch, armed_watch_reason=armed_watch_reason, arming_cls=arming_cls,
        )
        return final

    @staticmethod
    def _driver_source(driver_detail: dict) -> str:
        """Categorize which real signal(s) drove an armed move: catalyst / last30days
        / mixed / none. Derived from the qualified-driver detail (mock/'unknown'
        signals never qualify, so they read as 'none')."""
        keys = set((driver_detail or {}).keys())
        if {"catalyst", "last30days"} <= keys:
            return "mixed"
        if keys == {"catalyst"}:
            return "catalyst"
        if keys == {"last30days"}:
            return "last30days"
        return "none"

    @staticmethod
    def _evidence_snapshot(catalyst, last30) -> dict:
        """A COMPLETE source-level evidence snapshot captured at decision time, so a
        later analyst can answer exactly which catalyst/sentiment evidence drove an
        upgrade/downgrade — independent of which fields were promoted to columns."""
        ev: dict = {}
        if catalyst is not None:
            ev["catalyst"] = {
                "status": getattr(catalyst, "catalyst_status", None),
                "type": getattr(catalyst, "catalyst_type", None),
                "summary": getattr(catalyst, "catalyst_summary", None),
                "confidence": getattr(catalyst, "catalyst_confidence", None),
                "source": getattr(catalyst, "enrichment_source", None),
                "timestamp_utc": getattr(catalyst, "catalyst_timestamp_utc", None),
                "age_minutes": getattr(catalyst, "catalyst_age_minutes", None),
                "sources": getattr(catalyst, "catalyst_sources", None),
                "risk_tags": getattr(catalyst, "catalyst_risk_tags", None),
                "enrichment_status": getattr(catalyst, "enrichment_status", None),
            }
        if last30 is not None:
            ev["last30days"] = {
                "status": getattr(last30, "last30days_status", None),
                "sentiment_label": getattr(last30, "sentiment_label", None),
                "sentiment_score": getattr(last30, "sentiment_score", None),
                "summary": getattr(last30, "summary", None),
                "top_themes": getattr(last30, "top_themes", None),
                "source_coverage": getattr(last30, "source_coverage", None),
                "cluster_count": getattr(last30, "cluster_count", None),
                "item_count": getattr(last30, "item_count", None),
                "newest_age_hours": getattr(last30, "newest_age_hours", None),
                "provider": getattr(last30, "provider", None),
                "enrichment_status": getattr(last30, "enrichment_status", None),
            }
        return ev

    def _record_decision_adjustment(self, cand, evaluation, classification, base, final,
                                    adjustment, override_active, driver_str, driver_detail,
                                    catalyst, last30, scan_batch_id, armed_watch=False,
                                    armed_watch_reason=None, arming_cls=None) -> None:
        """Append-only audit + a denormalized tag on the candidate. Stores enough
        source-level evidence (catalyst + last30days, columns AND a full
        ``evidence_json`` snapshot) to answer later: which exact catalyst/sentiment
        evidence caused this candidate to be upgraded/downgraded? Best-effort: it
        records a decision; it never gates or executes anything."""
        reason = (f"{adjustment} (label={classification.label_decision} vs eval={base})"
                  + (f" — driver: {driver_str}" if driver_str else ""))
        self.journal.insert("decision_adjustments", {
            "adjustment_id": new_id("dadj"),
            "candidate_id": cand["candidate_id"],
            "packet_id": cand.get("_packet_id"),
            "scan_batch_id": scan_batch_id,
            "symbol": cand.get("symbol"),
            "eval_decision": base,
            "label_decision": classification.label_decision,
            "final_decision": final,
            "adjustment": adjustment,
            "override_armed": 1 if override_active else 0,
            "override_enabled": 1 if self.settings.labeller_decision_override_enabled else 0,
            "driver": driver_str or None,
            "driver_source": self._driver_source(driver_detail),
            "driver_detail_json": driver_detail,
            "evidence_json": self._evidence_snapshot(catalyst, last30),
            # --- catalyst evidence ---
            "catalyst_status": getattr(catalyst, "catalyst_status", None),
            "catalyst_type": getattr(catalyst, "catalyst_type", None),
            "catalyst_summary": getattr(catalyst, "catalyst_summary", None),
            "catalyst_source": getattr(catalyst, "enrichment_source", None),
            "catalyst_confidence": getattr(catalyst, "catalyst_confidence", None),
            "catalyst_timestamp_utc": getattr(catalyst, "catalyst_timestamp_utc", None),
            "catalyst_age_minutes": getattr(catalyst, "catalyst_age_minutes", None),
            # --- last30days / sentiment evidence ---
            "last30days_status": getattr(last30, "last30days_status", None),
            "last30days_provider": getattr(last30, "provider", None),
            "sentiment_label": getattr(last30, "sentiment_label", None),
            "sentiment_score": getattr(last30, "sentiment_score", None),
            "last30days_summary": getattr(last30, "summary", None),
            "top_themes_json": getattr(last30, "top_themes", None),
            "source_coverage_json": getattr(last30, "source_coverage", None),
            "label_confidence": getattr(classification, "confidence", None),
            # --- Roadmap 2.8 (Part A) armed-watch + labeller reasoning ---
            "arming_classification": arming_cls,
            "armed_watch": 1 if armed_watch else 0,
            "armed_watch_reason": armed_watch_reason,
            "proposal_readiness": getattr(classification, "proposal_readiness", None),
            "labeller_reason": getattr(classification, "reason_for_label", None),
            "labeller_missing_conditions_json": getattr(classification, "missing_conditions", None),
            "labeller_upgrade_blockers_json": getattr(classification, "upgrade_blockers", None),
        })
        self.journal.conn.execute(
            "UPDATE candidates SET decision_adjustment = ?, decision_adjustment_reason = ?, armed_watch = ? "
            "WHERE candidate_id = ?",
            (adjustment, reason, 1 if armed_watch else 0, cand["candidate_id"]),
        )
        self.journal.conn.commit()

    def _record_risk_check(self, proposal, evaluation, risk) -> str:
        """Persist a risk_checks row for the proposal. Pure audit: it never
        changes whether the trade proceeds (the RiskDecision already decided)."""
        risk_check_id = new_id("rchk")
        codes = {b.get("code") for b in (risk.block_reasons or [])}

        def gate(*reason_codes) -> str:
            return "fail" if codes.intersection(reason_codes) else "pass"

        requires_margin = getattr(proposal, "requires_margin", False)
        margin_approved = getattr(proposal, "margin_approved", False)
        is_short = proposal.direction == TradeDirection.SHORT.value
        sizing = risk.sizing
        self.journal.insert(
            "risk_checks",
            {
                "risk_check_id": risk_check_id,
                "proposal_id": proposal.proposal_id,
                "candidate_id": proposal.candidate_id,
                "trade_id": proposal.trade_id,
                "result": "pass" if risk.approved else "fail",
                "fail_reason": risk.primary_reason,
                "max_risk_amount": (sizing.risk_budget if sizing else None),
                "max_risk_pct": self.settings.max_risk_per_trade_pct,
                "position_size": (sizing.shares if sizing else None),
                "entry_price": evaluation.entry,
                "stop_price": evaluation.stop,
                "target_price": evaluation.target,
                "reward_risk": evaluation.expected_r,
                "min_reward_risk": self.settings.min_reward_risk,
                "stop_loss_pct": self.settings.stop_loss_pct,
                "target_reward_risk": self.settings.target_reward_risk,
                "target_profile": TargetProfile.CONFIGURED_STANDARD.value,
                "liquidity_check_result": gate(ReasonCode.LOW_LIQUIDITY.value),
                "spread_check_result": gate(
                    ReasonCode.WIDE_SPREAD.value, ReasonCode.CROSSED_QUOTE.value
                ),
                "daily_loss_check_result": gate(ReasonCode.DAILY_LOSS_LIMIT.value),
                "max_trades_check_result": gate(ReasonCode.DAILY_TRADE_LIMIT.value),
                "max_open_positions_check_result": gate(ReasonCode.TOO_MANY_POSITIONS.value),
                "short_margin_assumption": ("short_requires_margin" if is_short else None),
                "margin_or_leverage_required": 1 if requires_margin else 0,
                "user_approval_required_for_margin_or_leverage": (
                    1 if (requires_margin and not margin_approved) else 0
                ),
                "block_reasons_json": risk.block_reasons,
            },
        )
        return risk_check_id

    def _tag_target_profile(self, proposal, *, from_config: bool) -> None:
        """Record target-profile evidence on a proposal. Tracking only: it does
        not change the stop/target levels or any behavior. Every system-generated
        trade uses configured_standard; the source reflects config (mock baseline)
        vs the live OpenAI engine."""
        proposal.target_profile = TargetProfile.CONFIGURED_STANDARD.value
        proposal.target_reward_risk = self.settings.target_reward_risk
        proposal.min_reward_risk = self.settings.min_reward_risk
        proposal.stop_loss_pct = self.settings.stop_loss_pct
        src = TargetSource.CONFIG.value if from_config else TargetSource.OPENAI.value
        proposal.target_price_source = src
        proposal.stop_price_source = src

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
                # The persisted value stays the v1 no-news baseline constant
                # (BaselineType.NO_NEWS is the semantic equivalent). trade_id is
                # filled later once a trade exists; hypothetical_* left nullable.
                "baseline_type": BASELINE_MOMENTUM_NO_NEWS_V1,
                "trade_id": None,
                "target_profile": TargetProfile.CONFIGURED_STANDARD.value,
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
    def last30days_probe(self, symbol: str) -> dict:
        """READ-ONLY manual probe: run last30days enrichment for ONE symbol and
        return the narrative context WITHOUT writing to the ledger. Forces the
        configured provider even if scan-time enrichment is disabled (an explicit
        operator action), so the live CLI path can be verified before enabling it
        in scans. Never proposes, sizes, executes, or journals a candidate row."""
        from alphaos.research.last30days_provider import make_last30days_provider
        from alphaos.scanner.interest_scanner import InterestScanner

        symbol = (symbol or "").upper().strip()
        snapshot = self.market.get_snapshot(symbol)
        signals = InterestScanner(self.settings).score(snapshot)
        cand = {"candidate_id": "probe", "symbol": symbol,
                "direction": getattr(signals, "direction_hint", "long"), "momentum_score": None}
        packet = build_packet(cand, snapshot, signals, None)
        provider = make_last30days_provider(self.settings, force=True)
        enricher = Last30DaysEnricher(self.settings, self.journal, provider=provider)
        ctx = enricher.enrich(packet)
        return {
            "symbol": symbol,
            "provider": getattr(provider, "name", "disabled"),
            "python": self.settings.last30days_python,
            "repo_path": self.settings.last30days_repo_path or "(auto-resolve)",
            "sources": self.settings.last30days_sources,
            "feed_to_labeller": self.settings.last30days_feed_to_labeller,
            "last30days_status": ctx.last30days_status,
            "summary": ctx.summary,
            "top_themes": ctx.top_themes,
            "source_coverage": ctx.source_coverage,
            "sentiment_label": ctx.sentiment_label,
            "risk_tags": ctx.risk_tags,
            "last30days_context": ctx.last30days_context,
            "sentiment_context": ctx.sentiment_context,
            "enrichment_status": ctx.enrichment_status,
            "enrichment_error": ctx.enrichment_error,
        }

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
            "last30days_research": (
                "disabled_v1" if not s.last30days_enabled
                else f"{s.last30days_provider} (keyless, context-only)"
            ),
            "labeller_decision_override": (
                "downgrade_only" if not s.labeller_decision_override_enabled
                else ("armed (symmetric)" if (s.has_openai_key and not s.is_mock)
                      else "enabled_inert_while_mock")
            ),
            "last30days_polarity": (
                "disabled_v1" if not s.last30days_polarity_enabled
                else (f"{s.last30days_polarity_model} (arming "
                      f"{'on' if s.last30days_polarity_arming_allowed else 'off'})"
                      + ("" if (s.has_openai_key and not s.is_mock) else ", mock"))
            ),
            "execution_provider": s.execution_provider,     # simulated_internal | alpaca_paper
            "real_alpaca_paper_execution": "enabled" if s.real_paper_execution else "not_enabled_v1",
            "real_money_trading": "unreachable",
            "manual_approval": "required" if s.effective_approval_mode.value == "manual" else "auto (capped)",
            "kill_switch": "ENGAGED" if self.kill_switch.is_engaged() else "off",
            "broker_connected": self.orders.broker_connected,
            "open_positions": self.journal.count_open_positions(),
            "labeller_failsafe": self._labeller_failsafe_health(),
        }

    def _labeller_failsafe_health(self) -> dict:
        """VISIBILITY into the labeller fail-safe rate over recent labels. A failing
        labeller silently rejects (looks conservative), so a spike here is the
        alarm. PURE READ; advisory only — never changes any decision/gate/approval."""
        from alphaos.ai.labeller_health import evaluate_failsafe_health

        s = self.settings
        summary = self.journal.labeller_source_summary(limit=50)
        health = evaluate_failsafe_health(
            summary, s.labeller_failsafe_warn_rate, s.labeller_failsafe_critical_rate,
            s.labeller_failsafe_min_sample)
        return {
            "level": health["level"],
            "message": health["message"],
            "total": summary["total"],
            "openai": summary["openai"],
            "mock": summary["mock"],
            "fail_safe": summary["fail_safe"],
            "fail_safe_rate": summary["fail_safe_rate"],
            "by_source": summary["by_source"],
            "by_failsafe_reason": summary["by_failsafe_reason"],
            "top_reason": health.get("top_reason"),
        }

    def close(self) -> None:
        self.journal.close()


def _reward_risk(direction, entry, stop, target):
    """Reward:risk from levels (read-only display helper). None if undefined."""
    try:
        entry, stop, target = float(entry), float(stop), float(target)
    except (TypeError, ValueError):
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    reward = (entry - target) if direction == TradeDirection.SHORT.value else (target - entry)
    return round(reward / risk, 2)


def _zero_sizing(evaluation):
    from alphaos.risk.risk_engine import PositionSizing

    rps = abs((evaluation.entry or 0) - (evaluation.stop or 0))
    return PositionSizing(shares=0, risk_per_share=rps, dollar_risk=0.0, position_value=0.0, risk_budget=0.0)


@dataclass
class _EvalView:
    """Minimal evaluation-shaped view used to re-record a risk_check from a
    rebuilt proposal during manual approval (entry/stop/target/expected_r)."""

    entry: Optional[float]
    stop: Optional[float]
    target: Optional[float]
    expected_r: Optional[float]
    max_holding_days: Optional[int] = None


def _eval_view_from_proposal(proposal) -> _EvalView:
    return _EvalView(
        entry=proposal.entry,
        stop=proposal.stop,
        target=proposal.target,
        expected_r=proposal.expected_r,
        max_holding_days=proposal.max_holding_days,
    )
