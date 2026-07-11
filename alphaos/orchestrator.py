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
import sqlite3
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
    BEARISH_CATALYST_TYPES,
    BULLISH_CATALYST_TYPES,
    CandidateStatus,
    CATALYST_NOT_AVAILABLE_V1,
    CatalystStatus,
    DecisionAdjustment,
    Decision,
    EnrichmentSource,
    Last30DaysProvider,
    Last30DaysStatus,
    SentimentLabel,
    NEWS_STATUS_DISABLED_V1,
    NewsStatus,
    OrderState,
    PLAYBOOK_V1,
    ProposalStatus,
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
    UniverseTier,
)
from alphaos.regime.service import ensure_regime_for_today
from alphaos.scanner.candidate_scanner import CURRENT_INSTRUMENT_VERSION, DEFAULT_UNIVERSE
from alphaos.universe.builder import load_universe_file
from alphaos.cards import registry as cards
from alphaos.util import timeutils
from alphaos.data.freshness_guard import FreshnessGuard
from alphaos.data.market_data import MarketDataClient
from alphaos.execution.order_manager import OrderManager, OrderResult
from alphaos.execution.position_manager import PositionManager
from alphaos.execution import protection_watchdog
from alphaos import lineage
from alphaos import proposals as proposal_ttl
from alphaos import tqs
from alphaos import debate
from alphaos.journal.journal_store import JournalStore
from alphaos.news.news_service import NewsService
from alphaos.risk.risk_engine import RiskEngine
from alphaos.reports.daily_recon import DailyRecon
from alphaos.safety import KillSwitch
from alphaos.scanner.candidate_scanner import CandidateScanner
from alphaos.scanner.candidate_packet import build_packet
from alphaos.scanner.scan_context import ScanContext
from alphaos.ai.playbook_classifier import PlaybookClassifier
from alphaos.ai.last30days_polarity import Last30DaysPolarityClassifier, PolarityEvidence
from alphaos.news.catalyst_enricher import CatalystEnricher
from alphaos.research.last30days_enricher import Last30DaysEnricher
from alphaos.earnings.earnings_enricher import EarningsProximityEnricher, recompute_with_hold_days
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
    earnings_enriched: int = 0
    earnings_skipped_budget_cap: int = 0
    tqs_scored_candidates: int = 0
    tqs_scored_proposals: int = 0
    debate_voted: int = 0
    debate_skipped: int = 0
    polarity_classified: int = 0
    high_risk_narrative: int = 0
    decision_upgraded: int = 0
    decision_downgraded: int = 0
    armed_watch: int = 0
    scan_batch_id: Optional[str] = None
    scheduler_run_id: Optional[str] = None
    notes: list = field(default_factory=list)
    # --- EXP-0: shadow-tier deterministic universe capture ---
    # Zero by definition when shadow_tier_enabled is False or no universe
    # file has been committed yet -- see Orchestrator.run_scan_once.
    shadow_tier_scanned: int = 0
    shadow_tier_fresh: int = 0
    shadow_tier_stale: int = 0
    shadow_tier_candidates: int = 0
    shadow_tier_top_decile: int = 0
    shadow_tier_feed_coverage: Optional[float] = None

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
        self.earnings_enricher = EarningsProximityEnricher(self.settings, self.journal)  # PR5: earnings-proximity context
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
        # PR10: sync the setup-card registry BEFORE anything else can run --
        # unlike the checks below (which log-and-continue), a mutated card
        # (SettingsError) must stop startup cold, not just warn. See
        # alphaos/cards/registry.py's module docstring.
        synced = cards.sync_registry(self.journal, self.settings)
        if synced:
            self.journal.log_system_event(
                Severity.INFO, "startup", f"Setup cards registry synced: {', '.join(synced)}",
            )
        checks = self.settings.validate_startup()
        for c in checks:
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
    def run_scan_once(self, trigger_source: str = TriggerSource.MANUAL_CLI.value) -> ScanSummary:
        self._ensure_startup()

        # --- Mint a scan batch + a scheduler run (records exist even though v1
        #     has no real scheduler; trigger defaults to manual CLI). ---------
        scan_batch_id = new_id("scan")
        scheduler_run_id = new_id("schr")
        st = timeutils.stamp()

        # Unattended close-window auto-approval (2026-07-11): eligibility is
        # computed ONCE, HERE, at scan start -- never per-proposal later in
        # _handle_proposal/consider() (Fable5 review: a slow AI-evaluation
        # call can legitimately span the window's own end boundary; a
        # consider-time wall-clock check would silently deny exactly the
        # late candidates this mechanism exists to catch, and would also
        # leak auto-approval to a manual test scan that happens to straddle
        # the window). Requires BOTH: this run was SCHEDULER-triggered (a
        # human-triggered scan, via CLI or the dashboard, means a human is
        # already looking at the screen and can just click approve -- no
        # unattended door needed) AND the scan-start wall-clock falls inside
        # a configured UNATTENDED_APPROVE_WINDOWS window.
        unattended = False
        if trigger_source == TriggerSource.SCHEDULER.value and self.settings.unattended_approve_windows.strip():
            from alphaos.scheduler.cadence import format_hhmm_et, market_now_et, parse_windows, window_containing

            hhmm = format_hhmm_et(market_now_et())
            unattended = window_containing(hhmm, parse_windows(self.settings.unattended_approve_windows)) is not None
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
                "trigger_source": trigger_source,
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

        # --- REG-1: regime classifier + packet stamping (shadow/measurement
        # only -- no arming, no gating, no allocation changes). Computed (or
        # looked up, if already computed earlier today) ONCE per scan here,
        # then stamped onto every packet built below via self._today_regime
        # -- mirrors candidate_scanner.py's own self._spy/self._qqq
        # once-per-scan pattern. A missing/unavailable regime NEVER blocks
        # the scan -- packets simply stamp NULL and a loud alert is journaled
        # (see the stamping call site in _label_candidate). ---
        self._today_regime = None
        if self.settings.regime_enabled:
            self._today_regime = ensure_regime_for_today(self.journal, self.settings)
            if self._today_regime is None:
                self.journal.log_system_event(
                    Severity.WARNING, "regime",
                    "No regime_days row available for today (insufficient trailing SPY "
                    "history, or a benchmark-spine gap) -- packets will stamp regime=NULL "
                    "this scan. Never blocks the scan itself.",
                )

        # --- EXP-0: shadow-tier deterministic universe capture. Rides this
        # same scan job (no new scheduler cadence). `shadow_result` is NEVER
        # merged into `scan` -- the `for cand in scan.candidates:` loop below
        # (AI evaluation -> proposal creation) only ever sees core-tier
        # candidates; there is no code path from here that hands a
        # shadow-tier candidate to that loop. Zero AI calls, zero enrichment:
        # this block calls only the scanner + universe_days journaling. ---
        if self.settings.shadow_tier_enabled:
            universe_doc = load_universe_file(self.settings.shadow_tier_universe_file)
            if universe_doc and universe_doc.get("symbols"):
                shadow_symbols = [s["symbol"] for s in universe_doc["symbols"] if s.get("symbol")]
                shadow_result = self.scanner.scan_shadow_tier(
                    shadow_symbols, scan_batch_id=scan_batch_id,
                    universe_file_version=universe_doc.get("version"),
                )
                self._record_universe_days(shadow_result, universe_doc)
                summary.shadow_tier_scanned = shadow_result.snapshots
                summary.shadow_tier_stale = shadow_result.blocked_stale
                summary.shadow_tier_fresh = shadow_result.snapshots - shadow_result.blocked_stale
                summary.shadow_tier_candidates = len(shadow_result.candidates)
                summary.shadow_tier_top_decile = self._count_top_decile_interest(shadow_result.candidates)
                summary.shadow_tier_feed_coverage = (
                    round(summary.shadow_tier_fresh / summary.shadow_tier_scanned, 4)
                    if summary.shadow_tier_scanned else None
                )
            else:
                self.journal.log_system_event(
                    Severity.WARNING, "scanner",
                    "SHADOW_TIER_ENABLED is true but no shadow-universe file found at "
                    f"{self.settings.shadow_tier_universe_file!r} -- run `alphaos universe_build`, "
                    "review, and commit the result first.",
                )

        if self.kill_switch.is_engaged():
            self.journal.log_system_event(
                Severity.WARNING, "scan", "Kill switch engaged: no proposals will be executed."
            )

        if protection_watchdog.has_blocking_incident(self.journal):
            self.journal.log_system_event(
                Severity.WARNING, "scan",
                "Protection incident unresolved: no proposals will be auto-executed this scan."
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

        # Earnings-proximity enrichment is a SEPARATE per-scan budget (PR5),
        # mirroring last30days exactly: top-N shortlisted candidates BY INTEREST
        # RANK are enriched; eligible candidates outside the cap are explicitly
        # journaled as 'skipped_budget_cap' (never silently dropped). Advisory
        # only -- never fed to the labeller/AI eval, never gates a decision.
        earnings_enabled = labelling and self.settings.earnings_proximity_enabled
        earnings_cap = (min(cap, self.settings.earnings_proximity_max_symbols_per_scan)
                        if earnings_enabled else 0)
        earnings_set = {
            c["candidate_id"] for c in scan.candidates
            if earnings_enabled and (c.get("interest_rank") or 10 ** 9) <= earnings_cap
        }

        for cand in scan.candidates:
            # EXP-0 backstop: structurally this can never be true (the shadow-
            # tier pass above returns its own separate ScanResult, never
            # merged into `scan`) -- a loud, unambiguous failure here beats a
            # silent AI-labelling cost leak if a future refactor ever does
            # merge the two paths. See also the twin guard in
            # _handle_proposal, the actual proposal-creation chokepoint.
            if cand.get("shadow_tier"):
                raise RuntimeError(
                    f"shadow_tier candidate {cand.get('candidate_id')!r} reached the core-tier "
                    "AI-evaluation loop -- this must be structurally impossible (EXP-0)."
                )
            snapshot = cand.snapshot or {}
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
                earnings_mode = None
                if earnings_enabled:
                    earnings_mode = ("enrich" if cand["candidate_id"] in earnings_set
                                     else "skipped_budget_cap")
                classification = self._label_candidate(
                    cand, snapshot, scan_batch_id, enrich=do_enrich, l30_mode=l30_mode,
                    earnings_mode=earnings_mode)
                summary.labelled += 1
                if do_enrich:
                    enrich_budget -= 1
                    summary.catalyst_enriched += 1
                if l30_mode == "enrich":
                    summary.last30days_enriched += 1
                elif l30_mode == "skipped_budget_cap":
                    summary.last30days_skipped_budget_cap += 1
                if earnings_mode == "enrich":
                    summary.earnings_enriched += 1
                elif earnings_mode == "skipped_budget_cap":
                    summary.earnings_skipped_budget_cap += 1
                if cand.polarity is not None:
                    summary.polarity_classified += 1

            evaluation = self.openai.evaluate(
                cand, snapshot, freshness_status="usable",  # scanner only keeps usable snapshots
            )
            eval_lineage_id = lineage.get_or_create_lineage_id(self.journal, self.settings)
            self.journal.insert("openai_evaluations", {
                **evaluation.to_row(),
                "lineage_id": eval_lineage_id,
            })
            self._record_baselines(cand, evaluation)

            decision = evaluation.decision
            if classification is not None:
                decision = self._resolve_decision(cand, evaluation, classification, scan_batch_id, summary)

            # BASELINE: the deterministic shadow baseline (the "does the AI
            # add R?" instrument) -- written strictly AFTER the live decision
            # fully resolves (never influences it, shadow law), for EVERY
            # candidate that reached the primary AI evaluator (the SAME
            # population openai_evaluations is written for, above -- 2:1
            # shadow_baseline_decisions rows per evaluation, acceptance
            # criterion). NOT the legacy `_record_baselines`/`baseline_outcomes`
            # call two lines up -- that is the old no-news hypothetical-P&L
            # tracker, an unrelated mechanism; never conflate the two.
            if self.settings.baseline_enabled:
                from alphaos.baseline.tracker import record_shadow_baseline_decisions

                record_shadow_baseline_decisions(
                    self.journal, self.settings, cand,
                    scan_batch_id=scan_batch_id, lineage_id=eval_lineage_id,
                )

            if decision == Decision.REJECT.value:
                if (classification is not None
                        and classification.label_decision == Decision.REJECT.value
                        and evaluation.decision != Decision.REJECT.value):
                    # Label-driven reject (e.g. fail-safe / Other-Unclassified).
                    self._reject_candidate(cand, "ai_label", evaluation,
                                           reason=ReasonCode.LABEL_UNCLASSIFIED.value)
                elif ReasonCode.NO_ATR_DATA.value in (evaluation.risk_flags or []):
                    # INSTR-1 (scope/safety audit finding): _reject_candidate's own
                    # no-reason default always falls back to the generic
                    # OPENAI_REJECT code, which would make a persistent per-symbol
                    # ATR gap indistinguishable from an ordinary model rejection in
                    # every reason-code-bucketed report -- an operator needs to be
                    # able to tell "ATR data is missing for this symbol" apart from
                    # "the model itself rejected it."
                    self._reject_candidate(cand, "openai", evaluation, reason=ReasonCode.NO_ATR_DATA.value)
                else:
                    self._reject_candidate(cand, "openai", evaluation)
                summary.rejected += 1
                continue
            if decision == Decision.WATCH.value:
                self._set_candidate_status(cand["candidate_id"], CandidateStatus.WATCH.value)
                summary.watch += 1
                continue

            # decision == propose
            handled = self._handle_proposal(
                cand, evaluation, summary, scan_batch_id=scan_batch_id, unattended=unattended,
            )
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

        # PR7: TQS v0 shadow scoring. MUST run last, strictly AFTER every
        # decision above has already been committed -- this is what makes "TQS
        # cannot influence this scan's decisions" true by construction, not
        # merely by discipline. Measurement-only: nothing downstream reads
        # tqs_scores. score_scan_batch() never raises regardless, but the
        # tqs_shadow_enabled check still lives here (not inside
        # score_scan_batch) so a disabled shadow costs zero queries, not just
        # zero writes.
        if self.settings.tqs_shadow_enabled:
            tqs_result = tqs.score_scan_batch(self.journal, self.settings, scan_batch_id)
            summary.tqs_scored_candidates = tqs_result["scored_candidates"]
            summary.tqs_scored_proposals = tqs_result["scored_proposals"]

        # PR14: Red-Team Debate v0 shadow bear-agent voting. Same ordering
        # guarantee as TQS above -- MUST run last, strictly AFTER every
        # decision has already been committed, so "the bear agent cannot
        # influence this scan's decisions" is true by construction. Unlike
        # TQS, this is a genuinely paid LLM call, so the settings check
        # lives here (not inside score_debate_batch) for the same reason as
        # TQS's own: a disabled shadow costs zero queries, not just zero
        # writes/spend.
        if self.settings.debate_shadow_enabled:
            debate_result = debate.score_debate_batch(self.journal, self.settings, scan_batch_id)
            summary.debate_voted = debate_result["voted"]
            summary.debate_skipped = debate_result["skipped"]

        return summary

    def _handle_proposal(self, cand: "ScanContext", evaluation, summary: ScanSummary,
                         scan_batch_id=None, unattended: bool = False) -> bool:
        # EXP-0: guards the scan loop's two proposal-creation branches.
        # CORRECTION (scope/safety audit, F-1): this is NOT the one true
        # proposal-creation chokepoint -- _override_open_trade builds and
        # inserts its own trade_proposals row independently and never calls
        # this method; it has its own dedicated shadow_tier guard instead
        # (a graceful blocked_reason, not a RuntimeError, since that path is
        # reachable by an ordinary user action, not just an internal-logic
        # bug). seed_demo hardcodes shadow_tier=0 and needs no guard.
        # Structurally unreachable today (shadow-tier candidates never enter
        # scan.candidates, and no other path constructs a ScanContext with
        # shadow_tier=1) -- a loud failure beats a silent leak.
        if cand.get("shadow_tier"):
            raise RuntimeError(
                f"shadow_tier candidate {cand.get('candidate_id')!r} reached "
                "_handle_proposal -- this must be structurally impossible (EXP-0)."
            )
        direction = evaluation.direction or TradeDirection.LONG.value
        requires_margin = direction == TradeDirection.SHORT.value
        snapshot = cand.snapshot or {}
        card = cards.get_default_card()

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
            self._tag_target_profile(proposal, from_config=evaluation.is_mock, evaluation=evaluation)
            proposal.scan_batch_id = scan_batch_id
            proposal.playbook_name = PLAYBOOK_V1
            proposal.setup_classification = "momentum_continuation"
            proposal.card_id = card["card_id"]
            proposal.card_version = card["version"]
            proposal.invalidation_reason = card["invalidation_rule"]
            proposal.expected_hold_days = evaluation.max_holding_days
            self._stamp_proposal_ttl(proposal, snapshot)
            # Persist the risk check (does NOT change whether the trade proceeds).
            rc_id = self._record_risk_check(proposal, evaluation, risk)
            proposal.risk_check_id = rc_id
            proposal.status = ProposalStatus.BLOCKED.value
            self.journal.insert("trade_proposals", {
                **proposal.to_row(),
                **self._earnings_fields_for(cand.earnings, proposal.max_holding_days),
                "lineage_id": lineage.get_or_create_lineage_id(self.journal, self.settings),
            })
            self._reject_candidate(cand, "risk", evaluation, reason=risk.primary_reason)
            summary.risk_blocked += 1
            return False

        proposal = self.swing.build_proposal(evaluation, risk.sizing)
        self._tag_target_profile(proposal, from_config=evaluation.is_mock, evaluation=evaluation)
        proposal.scan_batch_id = scan_batch_id
        proposal.playbook_name = PLAYBOOK_V1
        proposal.setup_classification = "momentum_continuation"
        proposal.card_id = card["card_id"]
        proposal.card_version = card["version"]
        proposal.invalidation_reason = card["invalidation_rule"]
        proposal.expected_hold_days = evaluation.max_holding_days
        self._stamp_proposal_ttl(proposal, snapshot)
        rc_id = self._record_risk_check(proposal, evaluation, risk)
        proposal.risk_check_id = rc_id
        proposal.status = ProposalStatus.PENDING_APPROVAL.value
        self.journal.insert("trade_proposals", {
            **proposal.to_row(),
            **self._earnings_fields_for(cand.earnings, proposal.max_holding_days),
            "lineage_id": lineage.get_or_create_lineage_id(self.journal, self.settings),
        })
        # PR6: a fresh approvable proposal for this symbol supersedes any OTHER
        # still-open (pending_approval/proposed) proposal for the same symbol --
        # never deletes it, just marks it so approving the stale one is blocked
        # by the same status guard approve_proposal() already enforces.
        self._supersede_open_proposals(cand["symbol"], proposal.proposal_id)
        # Roadmap 2.7: surface the polarity arming classification + high-risk
        # narrative warning on the proposal (advisory; never changes levels/sizing).
        arming_cls = cand.arming_classification
        warning = cand.narrative_warning
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
        self._set_candidate_status(cand["candidate_id"], CandidateStatus.PROPOSED.value)
        summary.proposed += 1

        # HIGH-RISK narrative (hype/meme/squeeze) is MANUAL-ONLY: never auto-approve,
        # regardless of approval mode. It still went through every risk/freshness gate.
        if (arming_cls == ArmingClassification.HIGH_RISK_NARRATIVE.value
                and self.settings.last30days_high_risk_narrative_manual_only):
            summary.pending_manual += 1
            return True

        # PR6: the AUTO-approval path does NOT flow through approve_proposal()'s
        # stale guard, so re-check TTL here before considering auto-submission.
        # A proposal born already-expired (e.g. the TTL=0 closed-session bucket)
        # must never auto-execute; mark it expired cleanly and stop. The
        # _execute() chokepoint below is a further backstop, but handling it here
        # yields a clean 'expired' status (not a generic 'blocked') for audit.
        if proposal_ttl.is_expired(proposal.proposal_expires_at_utc):
            self._mark_proposal_expired(proposal.proposal_id, ReasonCode.PROPOSAL_EXPIRED.value)
            return True

        outcome = self.approvals.consider(proposal, risk_ok=True, freshness_ok=True, unattended=unattended)
        if outcome.approved:
            result = self._execute(proposal)
            if result.blocked:
                self._set_proposal_status(proposal.proposal_id, ProposalStatus.BLOCKED.value)
            else:
                self._set_proposal_status(proposal.proposal_id, ProposalStatus.FILLED.value)
                summary.auto_submitted += 1
        else:
            summary.pending_manual += 1
        return True

    def _stamp_proposal_ttl(self, proposal, snapshot: Optional[dict] = None) -> None:
        """PR6: freeze the TTL + expiry instant onto the proposal at CREATION
        time, from the market session active right now. Never recomputed
        later against the session active at approval time -- once set, a
        proposal's expiry is fixed (anchor-on-source, like PR4 lineage
        snapshots and PR5's earnings recompute).

        Session is read from ``snapshot["market_session"]`` when given --
        EXACTLY the same source FreshnessGuard.assess() prefers -- rather than
        calling the real wall-clock timeutils.market_session() directly: the
        mock data provider fixes every snapshot's session to REGULAR
        specifically so tests are deterministic regardless of the real
        weekday/time, and TTL must respect that same fiction, not bypass it."""
        session = (snapshot or {}).get("market_session")
        expiry = proposal_ttl.compute_expiry(
            self.settings, timeutils.to_iso(timeutils.now_utc()), session=session)
        proposal.proposal_ttl_seconds = expiry["proposal_ttl_seconds"]
        proposal.proposal_expires_at_utc = expiry["proposal_expires_at_utc"]

    def _supersede_open_proposals(self, symbol: str, new_proposal_id: str) -> None:
        """PR6: mark any OTHER still-open (pending_approval/proposed) proposal
        for ``symbol`` as superseded by the fresh one just created. NEVER
        deletes a row -- the old proposal stays fully auditable. Approving a
        superseded proposal is blocked by the same status guard
        ``approve_proposal`` already enforces (superseded is not in the
        approvable-status tuple), so no separate check is needed there.

        Deliberately only called from the risk-APPROVED branch of
        _handle_proposal: a later scan that risk-BLOCKS a fresh evaluation
        does not invalidate an existing, still-good open proposal for the
        same symbol -- that would remove user optionality for reasons
        unrelated to the old proposal's own validity."""
        now = timeutils.to_iso(timeutils.now_utc())
        self.journal.conn.execute(
            "UPDATE trade_proposals SET status = ?, superseded_by_proposal_id = ?, "
            "superseded_at_utc = ? WHERE symbol = ? AND status IN (?, ?) AND proposal_id != ?",
            (
                ProposalStatus.SUPERSEDED.value, new_proposal_id, now,
                symbol, ProposalStatus.PENDING_APPROVAL.value, ProposalStatus.PROPOSED.value,
                new_proposal_id,
            ),
        )
        self.journal.conn.commit()

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
        if row["status"] not in ProposalStatus.approvable():
            return False, f"proposal not approvable (status={row['status']})"
        proposal = TradeProposal.from_row(row)

        # PR6: stale-approval guard. Checked BEFORE broker submission (and
        # before every other gate below) so an expired proposal always gets a
        # clear, specific PROPOSAL_EXPIRED reason regardless of what else might
        # also be true (kill switch, protection, risk...). A missing/unparseable
        # expiry (e.g. a proposal row from before this PR existed) is treated
        # as expired -- fail safe, never "still fresh". Discovering expiry here
        # PERSISTS it (status='expired', audit fields set) so the row's history
        # remains fully reconstructable; it is never deleted or silently mutated.
        if proposal_ttl.is_expired(proposal.proposal_expires_at_utc):
            self._mark_proposal_expired(proposal_id, ReasonCode.PROPOSAL_EXPIRED.value)
            return False, (
                "proposal expired (TTL exceeded) — request a fresh scan/evaluation; "
                f"expired at {proposal.proposal_expires_at_utc}"
            )

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

        blocking = protection_watchdog.has_blocking_incident(self.journal)
        if blocking:
            return False, f"blocked: unresolved protection incident ({blocking['check_id']}) — {blocking['detail']}"

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
            eval_id=row.get("eval_id") or new_id("eval"),
            candidate_id=row.get("candidate_id"),  # type: ignore[arg-type]  # pre-existing: row is always DB-populated
            symbol=row.get("symbol"), model=row.get("model") or "unknown",  # type: ignore[arg-type]
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
                "AND status IN (?, ?) ORDER BY id DESC LIMIT 1",
                (candidate_id, *ProposalStatus.approvable()))
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
        rec["lineage_id"] = lineage.get_or_create_lineage_id(self.journal, self.settings)

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
        # EXP-0: unlike the scan loop's two RuntimeError backstops (genuinely
        # unreachable today -- shadow candidates never enter scan.candidates
        # at all), THIS path is reachable by an ordinary user action: nothing
        # stops an operator from calling `alphaos override` against a
        # shadow-tier candidate_id they noticed in the digest. It's currently
        # harmless only because no code path plants an openai_evaluations row
        # for a shadow candidate yet (EXP-1 adds exactly that) -- a graceful,
        # journaled refusal here, not a crash, matching this function's own
        # blocked_reason convention for every other refusal below.
        if cand.get("shadow_tier"):
            rec["blocked_reason"] = OverrideBlockedReason.SHADOW_TIER_EXCLUDED.value
            rec["execution_result"] = "candidate is shadow_tier=1 -- no proposal creation permitted"
            return "blocked: shadow-tier candidates are measurement-only, never tradeable via override"
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
        self._tag_target_profile(proposal, from_config=evaluation.is_mock, evaluation=evaluation)
        proposal.playbook_name = PLAYBOOK_V1
        proposal.setup_classification = "user_override"
        card = cards.get_default_card()
        proposal.card_id = card["card_id"]
        proposal.card_version = card["version"]
        proposal.invalidation_reason = card["invalidation_rule"]
        proposal.expected_hold_days = evaluation.max_holding_days
        proposal.proposal_reason = f"user_override:{rec['user_override_action']}"
        proposal.status = "pending_approval"
        self._stamp_proposal_ttl(proposal, snap)
        rc_id = self._record_risk_check(proposal, evaluation, risk)
        proposal.risk_check_id = rc_id
        self.journal.insert("trade_proposals", {
            **proposal.to_row(),
            "lineage_id": lineage.get_or_create_lineage_id(self.journal, self.settings),
        })
        self._supersede_open_proposals(symbol, proposal.proposal_id)
        warning = (HIGH_RISK_NARRATIVE_WARNING
                   if rec.get("arming_classification") == ArmingClassification.HIGH_RISK_NARRATIVE.value else None)
        self.journal.conn.execute(
            "UPDATE trade_proposals SET arming_classification = ?, narrative_warning = ? WHERE proposal_id = ?",
            (rec.get("arming_classification"), warning, proposal.proposal_id),
        )
        self.journal.conn.commit()
        # PR7: TQS v0 shadow scoring, strictly AFTER this proposal is committed
        # (same ordering guarantee as the main scan path). Measurement-only.
        if self.settings.tqs_shadow_enabled:
            tqs.score_proposal(self.journal, self.settings, cand["candidate_id"], proposal.proposal_id)
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
            # PR6: TTL/expiry visibility -- computed fresh on every render (never
            # persisted here; this is a PURE READ), so an operator sees staleness
            # BEFORE clicking Approve, even though the DB status itself is only
            # lazily flipped to 'expired' the moment an approval is attempted.
            expires_at = row.get("proposal_expires_at_utc")
            remaining = proposal_ttl.seconds_remaining(expires_at)
            # PR7: TQS v0 -- DISPLAY ONLY. This is a shadow measurement signal
            # (see alphaos/tqs/ module docstring): it must never be read by
            # approval/risk/execution logic, only shown to a human operator
            # alongside everything else here. A missing tqs_row (shadow
            # disabled, or scoring hasn't run for this proposal) shows as None
            # -- never a fabricated score.
            tqs_row = self.journal.one(
                "SELECT tqs_score, tqs_bucket, data_confidence FROM tqs_scores "
                "WHERE proposal_id = ? AND source_type = 'proposal' ORDER BY id DESC LIMIT 1",
                (row.get("proposal_id"),),
            ) or {}
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
                    "proposal_ttl_seconds": row.get("proposal_ttl_seconds"),
                    "proposal_expires_at_utc": expires_at,
                    "proposal_seconds_remaining": remaining,
                    "proposal_is_stale": proposal_ttl.is_expired(expires_at),
                    "tqs_score": tqs_row.get("tqs_score"),
                    "tqs_bucket": tqs_row.get("tqs_bucket"),
                    "tqs_data_confidence": tqs_row.get("data_confidence"),
                    # UI-PR-A: the setup card's invalidation rule, stamped verbatim onto
                    # the proposal at creation time (see cards/registry.py) -- never
                    # LLM-derived (PR10 non-goal). None on pre-PR10 rows or a
                    # demo-seeded proposal that bypassed the cards path.
                    "invalidation_reason": row.get("invalidation_reason"),
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
        self.journal.insert("claude_reviews", {
            **review.to_row(),
            "lineage_id": lineage.get_or_create_lineage_id(self.journal, self.settings),
        })
        self.journal.log_system_event(
            Severity.INFO, "claude", f"Claude review stored for {cand['symbol']} (verdict={review.verdict})."
        )
        return review

    # ------------------------------------------------------------- monitor
    def run_monitor_once(
        self, price_overrides: Optional[dict] = None,
        trigger_source: str = TriggerSource.MANUAL_CLI.value,
    ) -> dict:
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
                "trigger_source": trigger_source,
                "started_at_utc": st.utc,
                "started_at_sgt": st.local_sgt,
                "status": RunStatus.STARTED.value,
                "positions_touched": positions_seen,
            },
        )
        # Reconcile real Alpaca paper orders first (broker-managed bracket OCO):
        # opens positions on entry fills, closes them on TP/SL leg fills. The
        # protection watchdog runs NEXT, not before -- it must see this pass's
        # legitimate leg-fill closes already applied to positions.status, or a
        # normal, healthy close would misfire as a false-positive closed_mismatch.
        recon = self.orders.reconcile()
        protection = protection_watchdog.run_watchdog_pass(
            self.journal, self.orders.alpaca, self.settings, scheduler_run_id=scheduler_run_id
        )
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
            f"opened {len(recon.get('opened', []))}; "
            f"protection: {protection.get('unprotected', 0)} unprotected, "
            f"{protection.get('closed_mismatch', 0)} mismatched.",
        )
        return {
            "exits": all_exits,
            "reconciled": recon.get("reconciled", 0),
            "opened": recon.get("opened", []),
            "protection": protection,
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

    # ------------------------------------------------- measurement foundation
    def backfill_mfe_mae(self, limit: int = 500) -> dict:
        """Backfill MFE/MAE on closed trades recorded before intra-trade
        excursion tracking existed. Idempotent (only rows with
        mfe_mae_source IS NULL); never changes exit/stop/target/order behavior
        — write-only to trade_outcomes.mfe/.mae/.mfe_mae_source."""
        from alphaos.data.providers.alpaca_bars import make_bars_provider
        from alphaos.execution.mfe_mae_backfill import backfill_mfe_mae

        provider = make_bars_provider(self.settings, self.journal)
        return backfill_mfe_mae(self.journal, bars_provider=provider, limit=limit)

    def regime_arming_report(self, limit: int = 2000) -> dict:
        """REG-1: the shadow arming-map scorer -- armed_always vs
        armed_per_map paired replay ΔR per card. PURE READ, pure ledger math
        over existing shadow rows; nothing armed/disarmed for real. See
        alphaos/reports/regime_arming_scorer.py's pre-registration block."""
        from alphaos.reports.regime_arming_scorer import build_regime_arming_report

        return build_regime_arming_report(self.journal, self.settings, limit=limit)

    def baseline_report(self, limit: int = 5000) -> dict:
        """BASELINE: does the AI add R over either frozen deterministic
        rule, given a candidate reached the labeller? PURE READ, pure ledger
        math over existing shadow rows; nothing gated for real. See
        alphaos/reports/baseline_report.py's pre-registration block."""
        from alphaos.reports.baseline_report import build_baseline_report

        return build_baseline_report(self.journal, self.settings, limit=limit)

    def hypothesis_seed(self) -> list:
        """PR12: idempotently register every SEEDED_HYPOTHESES entry. Safe
        to call repeatedly (a no-op past the first call per hypothesis_id)."""
        from alphaos.hypotheses import seed_all

        return seed_all(self.journal)

    def hypothesis_resolve(self) -> dict:
        """PR12: one resolver pass -- evaluate any hypothesis_proposals row
        that has cleared its own calendar + sample-size floor, then refresh
        last_verdict/last_q_value for the whole evaluated family. PURE
        WRITE-ONLY to hypothesis_proposals/preregistrations; never read by
        any gate/eval/labeller/risk/execution path."""
        from alphaos.hypotheses import resolve_due_hypotheses

        return resolve_due_hypotheses(self.journal)

    def hypothesis_report(self) -> dict:
        """PR12: the registry status report -- every seeded hypothesis's
        risk class, claim, mechanical status, and (once evaluated) fresh
        verdict/q-value. PURE READ. See alphaos/reports/hypothesis_report.py."""
        from alphaos.reports.hypothesis_report import build_hypothesis_report

        return build_hypothesis_report(self.journal)

    def card_scoreboard_report(self) -> dict:
        """PR13 slice 1: every live_eligible, not-yet-demoted card's current
        scoreboard. PURE READ -- never consults or writes
        card_scoreboard_snapshots; always recomputed fresh from the ledger."""
        from alphaos.cards.scoreboard import build_card_scoreboard_report

        return build_card_scoreboard_report(self.journal)

    def card_demotion_check(self) -> dict:
        """PR13 slice 1: one daily pass -- snapshot every live_eligible card's
        scoreboard, demote (+ alert) any card with >= 2 consecutive breach
        snapshots. Writes only card_scoreboard_snapshots/card_demotions;
        never touches setup_cards or card YAML (Prime Directive 7)."""
        from alphaos.cards.demotion import run_daily_card_evaluation

        return run_daily_card_evaluation(self.journal, self.settings)

    def hypothesis_mark_status(self, hypothesis_id: str, new_status: str, decided_by: str) -> dict:
        """PR13 slice 2: the ONLY writer of MET/FAILED/WITHDRAWN. Operator-
        only by construction (raises if decided_by='system'); requires the
        hypothesis to already be 'resolved'. See
        alphaos/hypotheses/registry.py's mark_hypothesis_status()."""
        from alphaos.hypotheses import mark_hypothesis_status

        return mark_hypothesis_status(self.journal, hypothesis_id, new_status, decided_by)

    def hypothesis_drafts_list(self, status: Optional[str] = None) -> list:
        """HGEN-1: list drafts (optionally filtered by status). PURE READ."""
        from alphaos.hypotheses import list_drafts

        return list_drafts(self.journal, status=status)

    def hypothesis_draft_accept(self, draft_id: str, decided_by: str) -> dict:
        """HGEN-1: the authorship act -- the ONLY path from a quarantined
        draft to the real registry. See
        alphaos/hypotheses/proposer.py's accept_draft()."""
        from alphaos.hypotheses import accept_draft

        return accept_draft(self.journal, draft_id, decided_by)

    def hypothesis_draft_reject(self, draft_id: str, decided_by: str, reason: str) -> dict:
        """HGEN-1: record an operator rejection of a pending draft."""
        from alphaos.hypotheses import reject_draft

        return reject_draft(self.journal, draft_id, decided_by, reason)

    def hypothesis_generate(self) -> dict:
        """HGEN-1: one hypothesis-generation pass (operator-triggered only --
        no scheduler job wires this). Default-off (HYPOTHESIS_GEN_SHADOW_
        ENABLED), re-checks the G1 runtime gate every call, and refuses over
        the unreviewed-draft ceiling / cost caps. See
        alphaos/hypotheses/generator.py's run_hypothesis_generate()."""
        from alphaos.hypotheses import run_hypothesis_generate

        return run_hypothesis_generate(self.journal, self.settings)

    def autonomy_readiness_report(self) -> dict:
        """PR13 slice 2: every card-gating hypothesis's promotion precondition
        checklist. PURE READ. See alphaos/reports/autonomy_readiness.py."""
        from alphaos.reports.autonomy_readiness import build_autonomy_readiness_report

        return build_autonomy_readiness_report(self.journal)

    def card_promote(self, hypothesis_id: str, decided_by: str, research_ref: Optional[str] = None) -> dict:
        """PR13 slice 2: graduate the card named by hypothesis_id from
        shadow to live_eligible (content unchanged, no new version minted --
        see alphaos/cards/promotion.py's own "graduation vs mutation"
        module docstring). Raises ValueError with a reason_code if any
        precondition is unmet. Never touches setup_cards or card YAML."""
        from alphaos.cards.promotion import promote_card

        return promote_card(self.journal, hypothesis_id, decided_by, research_ref)

    def card_demote_manual(self, card_id: str, card_version: int, decided_by: str, reason: str) -> dict:
        """PR13 slice 2: a manual override demotion -- an operator's own
        judgment call, not evidence-gated. See
        alphaos/cards/promotion.py's demote_card()."""
        from alphaos.cards.promotion import demote_card

        return demote_card(self.journal, card_id, card_version, decided_by, reason)

    def card_materialize_prepare(self, hypothesis_id: str) -> dict:
        """PR13.5: write a proposed next-version scaffold + evidence packet
        to the staging dir for the operator to inspect and author. PURE
        READ w.r.t. cards/ -- see alphaos/cards/materialize.py's own module
        docstring for the full graduation-vs-materialization distinction."""
        from alphaos.cards.materialize import prepare_materialization

        return prepare_materialization(self.journal, hypothesis_id, self.settings.card_promotion_staging_dir)

    def card_materialize_confirm(self, hypothesis_id: str, decided_by: str) -> dict:
        """PR13.5: verify the operator has authored + committed the new
        version's YAML, then register it and journal the decision. NEVER
        writes to cards/ itself. See alphaos/cards/materialize.py."""
        from alphaos.cards.materialize import confirm_materialization

        return confirm_materialization(self.journal, self.settings, hypothesis_id, decided_by)

    def backfill_regime_days(self) -> dict:
        """REG-1 one-off: extend benchmark_bars SPY history, classify the
        full available series into regime_days, and stamp any pre-existing
        candidate_packets rows still missing a regime. Idempotent; a
        derivation from stored/vendor daily bars, never a mutation of
        already-stamped evidence -- see alphaos/regime/service.py."""
        from alphaos.data.providers.alpaca_bars import make_bars_provider
        from alphaos.regime.service import backfill_regime_days

        provider = make_bars_provider(self.settings, self.journal)
        return backfill_regime_days(self.journal, self.settings, bars_provider=provider)

    def eval_corpus_build(self, corpus_dir: Optional[str] = None, limit: int = 30) -> dict:
        """EVAL-1 one-off: select up to ``limit`` real, clean (post-PR9.1)
        candidate_packets rows and write them into the frozen golden corpus
        (additive -- never overwrites an existing fixture). Does NOT
        adjudicate ground truth; every newly-written packet's
        ``ground_truth_label`` starts null until an operator hand-edits the
        fixture file. See alphaos/eval/corpus.py."""
        from alphaos.eval.corpus import DEFAULT_CORPUS_DIR, select_seed_packets, write_corpus

        root = corpus_dir or DEFAULT_CORPUS_DIR
        seeds = select_seed_packets(self.journal, limit=limit)
        manifest, written = write_corpus(root, seeds, as_of_date=timeutils.market_date().isoformat())
        return {
            "corpus_dir": root, "candidates_considered": len(seeds),
            "packets_written": len(written), "corpus_version": manifest["version"],
            "corpus_size": len(manifest["packets"]),
        }

    def run_eval(self, corpus_dir: Optional[str] = None, repeats: int = 1) -> dict:
        """EVAL-1: replay the frozen golden corpus through the CURRENT
        playbook classifier -- the exact same production call the labeller
        uses at scan time, never a reimplementation. Stores every result
        including fail-safe ones. Zero decision surface. See
        alphaos/eval/harness.py."""
        from alphaos.eval.harness import run_eval

        return run_eval(self.journal, self.settings, corpus_dir=corpus_dir, repeats=repeats)

    def eval_report(self, run_id: Optional[str] = None) -> dict:
        """EVAL-1: the latest (or a specific) eval run's report -- parse
        rate, label agreement vs ground truth, categorical stability across
        repeats. PURE READ."""
        from alphaos.reports.eval_report import build_eval_report

        return build_eval_report(self.journal, run_id=run_id)

    def relabel_candidates(self, date_from: str, date_to: str, dry_run: bool = False) -> dict:
        """TASK-R one-off: retro-relabel candidate_packets rows in
        [date_from, date_to] through the CURRENT labeller. Never touches
        an original row; see alphaos/relabel.py."""
        from alphaos.relabel import relabel_candidates

        return relabel_candidates(self.journal, self.settings, date_from, date_to, dry_run=dry_run)

    def canary_corpus_build(self, corpus_dir: Optional[str] = None, limit: int = 20) -> dict:
        """CANARY one-off: select up to ``limit`` real, clean (post-PR9.1)
        candidate_packets rows -- preferring TASK-R relabels -- and write
        them into the frozen golden corpus (additive -- never overwrites an
        existing fixture). See alphaos/canary/corpus.py."""
        from alphaos.canary.corpus import DEFAULT_CORPUS_DIR, select_seed_packets, write_corpus

        root = corpus_dir or DEFAULT_CORPUS_DIR
        seeds = select_seed_packets(self.journal, limit=limit)
        manifest, written = write_corpus(root, seeds, as_of_date=timeutils.market_date().isoformat())
        return {
            "corpus_dir": root, "candidates_considered": len(seeds),
            "packets_written": len(written), "corpus_version": manifest["version"],
            "corpus_size": len(manifest["packets"]),
        }

    def canary_run(self, corpus_dir: Optional[str] = None) -> dict:
        """CANARY: replay the frozen golden corpus through the CURRENT
        playbook classifier and compare against the pinned baseline run.
        Zero decision surface. See alphaos/canary/run.py."""
        from alphaos.canary.run import run_canary

        return run_canary(self.journal, self.settings, corpus_dir=corpus_dir)

    def canary_pin_baseline(self, run_id: str) -> dict:
        """CANARY: mark ``run_id`` as THE reference run every future run
        diffs against. Never automatic -- an operator decides when a run is
        clean enough to trust as the baseline."""
        from alphaos.canary.run import pin_baseline

        return pin_baseline(self.journal, run_id)

    def canary_status(self) -> dict:
        """CANARY: the latest run's report -- PURE READ. See
        alphaos/reports/canary_report.py."""
        from alphaos.reports.canary_report import build_canary_report

        return build_canary_report(self.journal)

    def outcomes_update(self, limit: int = 500) -> dict:
        """Counterfactual outcome tracker (Fable 5 review PR2): seed
        candidate_outcomes rows for candidates/proposals/rejects/armed-watch/
        user-overrides not yet tracked, then resolve pending rows with 1/3/5-day
        forward returns + bracket replay from historical bars. PURE MEASUREMENT
        — never read by any gate/eval/labeller/risk/execution path; idempotent;
        makes no order/approval/execution changes."""
        from alphaos.data.providers.alpaca_bars import make_bars_provider
        from alphaos.learning.outcomes_tracker import seed_pending_outcomes, update_pending_outcomes

        seeded = seed_pending_outcomes(self.journal, limit=limit)
        provider = make_bars_provider(self.settings, self.journal)
        updated = update_pending_outcomes(self.journal, bars_provider=provider, limit=limit)
        result = {"seeded": seeded, "updated": updated}

        # PR8: Attribution v2. MUST run strictly AFTER the outcome ledger above
        # -- attribution only ever READS candidate_outcomes/trade_outcomes as
        # they already stand, never recomputes a replay itself. Same
        # zero-cost-when-disabled posture as PR7's tqs_shadow_enabled check.
        if self.settings.attribution_enabled:
            from alphaos import attribution

            discovered = attribution.discover_events(self.journal, self.settings, limit=limit)
            resolved = attribution.resolve_pending(self.journal, self.settings, limit=limit)
            result["attribution"] = {"discovered": discovered, "resolved": resolved}

        # BASELINE: extends this SAME counterfactual outcomes job (spec item
        # 4) -- reuses the SAME bars_provider already constructed above
        # (zero new provider/client code) and the ONE replay engine
        # (alphaos.learning.outcomes_engine.replay_bracket). Order relative
        # to attribution doesn't matter for correctness (independent
        # tables), but this reads more naturally grouped with the outcome-
        # ledger resolution it shares a provider with.
        if self.settings.baseline_enabled:
            from alphaos.baseline.tracker import resolve_pending_baseline_decisions

            result["baseline"] = resolve_pending_baseline_decisions(
                self.journal, bars_provider=provider, limit=limit)
        return result

    def outcomes_report(self, limit: int = 2000) -> dict:
        """Measurement-visibility summary over candidate_outcomes. PURE READ —
        no statistical claims; always surfaces a small-sample caveat."""
        from alphaos.reports.outcomes_summary import build_outcomes_report

        return build_outcomes_report(self.journal, self.settings, limit=limit)

    def relative_performance_report(self, limit: int = 3650) -> dict:
        """PR9.5: paper-equity vs S&P 500 measurement. PURE READ — no
        statistical claims; floor-gated exactly like every other report."""
        from alphaos.reports.relative_performance import build_relative_performance_report

        return build_relative_performance_report(self.journal, self.settings, limit=limit)

    def decision_lineage_report(self, decision_id: str) -> dict:
        """READ-ONLY: full lineage reconstruction for one decision (accepts a
        candidate_id, proposal_id, rejection_id, adjustment_id, override_id,
        outcome_id, eval_id, review_id, or polarity_id)."""
        from alphaos.reports.decision_lineage import build_decision_lineage_report

        return build_decision_lineage_report(self.journal, decision_id)

    def daily_brief_report(self) -> dict:
        """PR11: the daily human interface. PURE READ — needs-you, portfolio
        health, today's activity, best candidate, what-learned, moonshot gap,
        one action. Never auto-exits anything; EXIT_REVIEW is a human flag."""
        from alphaos.reports.daily_brief import build_daily_brief

        return build_daily_brief(self.journal, self.settings, self.kill_switch)

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

    # ------------------------------------------------- protection watchdog
    def protection_status_report(self) -> dict:
        """Read-only: current per-position protection status + open incidents."""
        return protection_watchdog.status_report(self.journal)

    def protection_resolve(self, incident_id: str, exit_price: float, note: str,
                           resolved_by: str = "user") -> dict:
        """Human-confirmed resolution of a local-open/broker-closed incident: calls
        close_position() with an operator-supplied price -- never raw SQL, never
        a guessed price."""
        self._ensure_startup()
        return protection_watchdog.resolve_incident(
            self.journal, self.positions, incident_id, exit_price, note, resolved_by
        )

    def protection_ack(self, incident_id: str, note: str, resolved_by: str = "user") -> dict:
        """Human acknowledgement of an unprotected/degraded incident WITHOUT
        closing the position -- never calls close_position()."""
        self._ensure_startup()
        return protection_watchdog.acknowledge_incident(self.journal, incident_id, note, resolved_by)

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
        card = cards.get_default_card()
        proposal = TradeProposal(
            symbol=symbol, direction="long", strategy=Strategy.SWING.value,
            entry=entry, stop=stop, target=target, max_holding_days=3,
            qty=(risk.sizing.shares if risk.sizing else 1),
            risk_per_share=(risk.sizing.risk_per_share if risk.sizing else (entry - stop)),
            dollar_risk=(risk.sizing.dollar_risk if risk.sizing else (entry - stop)),
            expected_r=2.0, same_day_exit_eligible=True, candidate_id=cand_id,
            eval_id="demo", is_demo=True, status="pending_approval",
            card_id=card["card_id"], card_version=card["version"],
            invalidation_reason=card["invalidation_rule"],
        )
        self._tag_target_profile(proposal, from_config=True)
        self._stamp_proposal_ttl(proposal, snap)
        self.journal.insert("trade_proposals", {
            **proposal.to_row(),
            "lineage_id": lineage.get_or_create_lineage_id(self.journal, self.settings),
        })
        self._supersede_open_proposals(symbol, proposal.proposal_id)
        self.journal.log_system_event(
            Severity.WARNING, "demo",
            "DEMO_SEED proposal created (bypasses news pipeline; clearly labelled).",
            {"proposal_id": proposal.proposal_id},
        )
        ok, msg = self.approve_proposal(proposal.proposal_id, approver="demo")
        return {"proposal_id": proposal.proposal_id, "approved": ok, "message": msg}

    # --------------------------------------------------------------- helpers
    def _execute(self, proposal: TradeProposal, fill_price: Optional[float] = None):
        # PR6 chokepoint: NO proposal may be submitted once its TTL has lapsed.
        # approve_proposal() already guards the manual path, but _execute is the
        # single funnel EVERY execution route passes through (manual, auto, and
        # any future one), so the stale guard lives here too as an unconditional
        # backstop -- a stale proposal can never reach broker submission
        # regardless of which caller invoked _execute. A fresh proposal (the
        # only kind the manual path reaches here with) no-ops past this check.
        if proposal_ttl.is_expired(proposal.proposal_expires_at_utc):
            self.journal.log_system_event(
                Severity.WARNING, "approval",
                f"Execution blocked for {proposal.symbol}: proposal expired (TTL exceeded).",
                {"proposal_id": proposal.proposal_id, "reason_code": ReasonCode.PROPOSAL_EXPIRED.value},
            )
            return OrderResult(
                blocked=True, block_reason=ReasonCode.PROPOSAL_EXPIRED.value,
                detail="proposal TTL exceeded before submission",
                state=OrderState.REJECTED.value,
            )

        # PR10 exit-first invariant ("no entry without a written exit"): every
        # submission route funnels through this SAME chokepoint, so this is
        # where the law is enforced regardless of caller. Legacy proposals
        # (pre-PR10, NULL invalidation_reason) are grandfathered everywhere
        # EXCEPT here -- fail-safe direction is always block, never wave a
        # stale/incomplete plan through. Falsy checks (not truthy checks)
        # deliberately catch both NULL-hydrated None and the 0/""-defaulted
        # values TradeProposal.from_row() produces for a missing DB column --
        # no real trade ever has a genuine $0 entry/stop/target or 0-day hold.
        missing = [
            name for name, value in (
                ("entry", proposal.entry), ("stop", proposal.stop), ("target", proposal.target),
                ("max_holding_days", proposal.max_holding_days),
                ("invalidation_reason", proposal.invalidation_reason),
            )
            if not value
        ]
        if missing:
            detail = f"exit plan incomplete: missing {', '.join(missing)}"
            self.journal.log_system_event(
                Severity.WARNING, "approval",
                f"Execution blocked for {proposal.symbol}: {detail}.",
                {"proposal_id": proposal.proposal_id, "reason_code": ReasonCode.EXIT_PLAN_INCOMPLETE.value,
                 "missing_fields": missing},
            )
            return OrderResult(
                blocked=True, block_reason=ReasonCode.EXIT_PLAN_INCOMPLETE.value,
                detail=detail, state=OrderState.REJECTED.value,
            )

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

    def _mark_proposal_expired(self, proposal_id: str, reason: str) -> None:
        """PR6: persist the expired transition discovered at approval time.
        Never deletes the row -- entry/stop/target/qty and every prior status
        transition remain exactly as recorded; only the terminal status +
        expiry audit fields are set."""
        now = timeutils.to_iso(timeutils.now_utc())
        self.journal.conn.execute(
            "UPDATE trade_proposals SET status = ?, expired_reason = ?, expired_at_utc = ? "
            "WHERE proposal_id = ?",
            (ProposalStatus.EXPIRED.value, reason, now, proposal_id),
        )
        self.journal.conn.commit()

    # ------------------------------------------------------ EXP-0 shadow tier
    def _record_universe_days(self, shadow_result, universe_doc: dict) -> None:
        """The survivorship-bias law: one row per shadow-tier symbol per
        trading day, written REGARDLESS of whether that symbol produced a
        candidate. Idempotent across the (up to 3) scan windows a day via
        ``idx_universe_days_symbol_date`` -- the first window of the day to
        reach here wins; later same-day attempts hit the unique-index
        IntegrityError and are silently skipped (same idiom as
        ``benchmark_capture._backfill_benchmark_bars``'s own dedup). Rows are
        NEVER updated after insert -- delisted/dropped names simply stop
        appearing in future rows; existing rows are untouched."""
        market_dt = timeutils.market_date().isoformat()
        flags_by_symbol = {s["symbol"]: s for s in universe_doc.get("symbols", []) if s.get("symbol")}
        for sym, outcome in shadow_result.per_symbol.items():
            # Correctness audit F-2: only attempt the insert for a genuinely
            # nameable row -- narrows the IntegrityError catch below to the
            # ONE case it's meant for (the same-day unique-index dedup),
            # rather than also silently swallowing a NOT NULL violation on a
            # falsy symbol. Not reachable via run_scan_once today (symbols
            # are pre-filtered before scan_shadow_tier is ever called), but
            # this table exists specifically to prevent survivorship bias --
            # a silently-dropped row would defeat its own purpose.
            if not sym:
                self.journal.log_system_event(
                    Severity.WARNING, "scanner",
                    f"universe_days: skipping a falsy symbol key in shadow scan per_symbol "
                    f"outcomes ({outcome!r}) -- this should be unreachable; investigate the caller.",
                )
                continue
            flags = flags_by_symbol.get(sym, {})
            try:
                self.journal.insert("universe_days", {
                    "universe_day_id": new_id("univday"),
                    "market_date": market_dt,
                    "symbol": sym,
                    "tier": UniverseTier.WATCHLIST.value,
                    "universe_file_version": universe_doc.get("version"),
                    "recent_ipo": 1 if flags.get("recent_ipo") else 0,
                    "spac_flag": 1 if flags.get("spac_flag") else 0,
                    "freshness_status": outcome.get("freshness_status"),
                    "candidate_found": 1 if outcome.get("candidate_id") else 0,
                    "candidate_id": outcome.get("candidate_id"),
                    "instrument_version": CURRENT_INSTRUMENT_VERSION,
                })
            except sqlite3.IntegrityError:
                pass  # idx_universe_days_symbol_date backstop -- already recorded today

    @staticmethod
    def _count_top_decile_interest(candidates: list) -> int:
        """Count of shadow-tier candidates scoring at/above the 90th
        percentile of THIS scan's own shadow-tier candidate interest scores
        (nearest-rank method -- deterministic, no numpy dependency). Scoped
        to candidates (names that already cleared the momentum/interest bar),
        not the full scanned population -- a relative "standouts among
        tonight's standouts" signal, documented here so the digest number is
        never read as a percentile of the whole shadow universe."""
        scores = sorted((c.get("interest_score") or 0.0) for c in candidates)
        n = len(scores)
        if n == 0:
            return 0
        threshold = scores[max(0, int(0.9 * (n - 1)))]
        return sum(1 for s in scores if s >= threshold)

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

    def _earnings_fields_for(self, earnings_ctx, hold_days=None) -> dict:
        """Denormalized earnings-proximity summary (PR5) for candidates/
        trade_proposals/rejected_candidates/decision_adjustments. With
        ``hold_days`` given, cheaply RE-DERIVE the hold-window flag against the
        REAL holding period (never re-fetches from the provider -- see
        alphaos/earnings/earnings_enricher.py); with ``hold_days=None``, use the
        already-computed default-hold-days view as-is (candidates table, where
        the real hold length isn't known yet). ``None`` earnings_ctx (disabled/
        not yet enriched) yields an all-None dict -- never a false "safe"."""
        empty = {
            "earnings_date": None, "days_until_earnings": None,
            "earnings_within_hold_window": None, "earnings_within_warning_window": None,
            "earnings_timing": None, "earnings_data_status": None,
        }
        if earnings_ctx is None:
            return empty
        ctx = earnings_ctx
        if hold_days is not None:
            ctx = recompute_with_hold_days(
                earnings_ctx, hold_days, self.settings.earnings_proximity_warning_days)
        return ctx.summary_fields()

    def _label_candidate(self, cand: "ScanContext", snapshot: dict, scan_batch_id, enrich: bool = True,
                         l30_mode: Optional[str] = None, earnings_mode: Optional[str] = None):
        """Build the compact packet, (optionally) enrich it with catalyst +
        last30days + earnings-proximity context, journal it, AI-classify it, and
        freeze the label + catalyst + last30days + earnings view onto the
        candidate. Returns the PlaybookClassification (advisory). ``l30_mode``/
        ``earnings_mode`` are each one of: 'enrich' (within the per-scan cap),
        'skipped_budget_cap' (eligible but outside it), or None (disabled).
        Earnings context is NEVER applied to the packet -- it must not reach the
        AI eval/labeller prompt."""
        signals = cand.interest
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
        # PR5: earnings-proximity context, AFTER last30days. Advisory ONLY — never
        # applied to the packet, so it can never reach the AI eval/labeller prompt,
        # never forces a decision, bypasses a gate, or executes.
        earnings = None
        if earnings_mode == "enrich":
            earnings = self.earnings_enricher.enrich(packet)
        elif earnings_mode == "skipped_budget_cap":
            earnings = self.earnings_enricher.skipped_budget_cap(packet)
        regime_row = getattr(self, "_today_regime", None)
        self.journal.insert("candidate_packets", packet.to_row(
            scan_batch_id,
            regime=regime_row["regime"] if regime_row else None,
            regime_rules_version=regime_row["regime_rules_version"] if regime_row else None,
        ))
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
        if earnings is not None:
            self.journal.insert("candidate_earnings", {
                **earnings.to_row(cand["candidate_id"], packet.packet_id, scan_batch_id),
                "lineage_id": lineage.get_or_create_lineage_id(self.journal, self.settings),
            })
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
            self.journal.insert("last30days_polarity", {
                **polarity.to_row(scan_batch_id, packet.packet_id),
                "lineage_id": lineage.get_or_create_lineage_id(self.journal, self.settings),
            })
        self._freeze_label(cand, packet, classification, scan_batch_id, catalyst, last30,
                           polarity, earnings)
        # Stash the advisory context so _resolve_decision can (a) decide whether a
        # real driver justifies a symmetric override and (b) record the driver.
        cand.catalyst = catalyst
        cand.last30 = last30
        cand.polarity = polarity
        cand.earnings = earnings
        cand.packet_id = packet.packet_id
        return classification

    def _freeze_label(self, cand: "ScanContext", packet, classification, scan_batch_id, catalyst=None,
                      last30=None, polarity=None, earnings=None) -> None:
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
        # PR5: earnings-proximity summary, computed with the DEFAULT hold-days
        # view (the real hold length isn't known yet at label-freeze time).
        earn = self._earnings_fields_for(earnings)
        self.journal.conn.execute(
            "UPDATE candidates SET primary_label = ?, secondary_labels_json = ?, "
            "candidate_tags_json = ?, risk_tags_json = ?, label_confidence = ?, "
            "label_decision = ?, label_version = ?, label_source = ?, label_frozen_at_utc = ?, "
            "catalyst_status = ?, catalyst_type = ?, catalyst_suggested_label = ?, label_review_required = ?, "
            "last30days_status = ?, sentiment_label = ?, "
            "polarity_label = ?, polarity_alignment = ?, narrative_driver_type = ?, arming_classification = ?, "
            "earnings_date = ?, days_until_earnings = ?, earnings_within_hold_window = ?, "
            "earnings_within_warning_window = ?, earnings_timing = ?, earnings_data_status = ? "
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
                earn["earnings_date"], earn["days_until_earnings"], earn["earnings_within_hold_window"],
                earn["earnings_within_warning_window"], earn["earnings_timing"], earn["earnings_data_status"],
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
    # driver for it. Shared with alphaos/tqs/scoring.py (PR7); see
    # BEARISH_CATALYST_TYPES/BULLISH_CATALYST_TYPES in constants.py for why
    # the definition lives there rather than here.
    _BEARISH_CATALYSTS = BEARISH_CATALYST_TYPES
    _BULLISH_CATALYSTS = BULLISH_CATALYST_TYPES

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

    def _resolve_decision(self, cand: "ScanContext", evaluation, classification, scan_batch_id,
                          summary) -> str:
        """Compute the final trade decision from the eval + label, applying the
        gated symmetric override, and ALWAYS record how/why it moved (audit for
        learning). Returns the final decision; downstream gates + manual approval
        are unchanged and still authoritative."""
        base = evaluation.decision
        label = classification.label_decision
        catalyst = cand.catalyst
        last30 = cand.last30
        polarity = cand.polarity
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
            cand.arming_classification = ArmingClassification.HIGH_RISK_NARRATIVE.value
            cand.narrative_warning = getattr(polarity, "warning_message", "") or ""
            summary.high_risk_narrative += 1
        elif adjustment == DecisionAdjustment.UPGRADED.value and arming_cls:
            cand.arming_classification = arming_cls

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

        earnings = cand.earnings
        self._record_decision_adjustment(
            cand, evaluation, classification, base, final, adjustment,
            override_active, driver_str, driver_detail, catalyst, last30, earnings, scan_batch_id,
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

    def _record_decision_adjustment(self, cand: "ScanContext", evaluation, classification, base, final,
                                    adjustment, override_active, driver_str, driver_detail,
                                    catalyst, last30, earnings, scan_batch_id, armed_watch=False,
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
            "packet_id": cand.packet_id,
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
            # --- PR5: earnings-proximity, recomputed against the REAL max_holding_days ---
            **self._earnings_fields_for(
                earnings, getattr(evaluation, "max_holding_days", None)
                or self.settings.earnings_proximity_default_hold_days),
            "lineage_id": lineage.get_or_create_lineage_id(self.journal, self.settings),
            "ai_lineage_json": lineage.combine_ai_lineage(
                label={"model": getattr(classification, "model", None),
                       "is_mock": getattr(classification, "is_mock", None),
                       "model_provider": getattr(classification, "model_provider", None),
                       "prompt_hash": getattr(classification, "prompt_hash", None),
                       "system_prompt_hash": getattr(classification, "system_prompt_hash", None)}
                if classification else None,
                last30days={"model": getattr(last30, "model", None),
                            "is_mock": getattr(last30, "is_mock", None),
                            "model_provider": getattr(last30, "model_provider", None),
                            "prompt_hash": getattr(last30, "prompt_hash", None),
                            "system_prompt_hash": getattr(last30, "system_prompt_hash", None)}
                if last30 else None,
            ),
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

    def _tag_target_profile(self, proposal, *, from_config: bool, evaluation=None) -> None:
        """Record target-profile evidence on a proposal. Tracking only for the
        target_profile/reward:risk/stop_loss_pct fields: does not change
        target levels or behavior. Every system-generated trade uses
        configured_standard; the source reflects config (mock baseline) vs
        the live OpenAI engine.

        ``stop_price_source`` is the one exception (INSTR-1): when
        ``evaluation.stop_source`` is set (only ever true on the live PROPOSE
        path _apply_atr_stop() actually overrode -- never mock, never a
        watch/reject evaluation later force-approved via user override), it
        wins over the generic config/openai label, since the stop itself was
        genuinely computed differently. ``target_price_source`` is untouched
        either way -- the AI still sets the target."""
        proposal.target_profile = TargetProfile.CONFIGURED_STANDARD.value
        proposal.target_reward_risk = self.settings.target_reward_risk
        proposal.min_reward_risk = self.settings.min_reward_risk
        proposal.stop_loss_pct = self.settings.stop_loss_pct
        src = TargetSource.CONFIG.value if from_config else TargetSource.OPENAI.value
        proposal.target_price_source = src
        stop_source = getattr(evaluation, "stop_source", None) if evaluation is not None else None
        proposal.stop_price_source = stop_source or src

    def _reject_candidate(self, cand: "ScanContext", stage, evaluation,
                          reason: Optional[str] = None) -> None:
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
                # PR5: earnings-proximity, recomputed against the REAL max_holding_days.
                **self._earnings_fields_for(
                    cand.earnings,
                    getattr(evaluation, "max_holding_days", None)
                    or self.settings.earnings_proximity_default_hold_days),
                "lineage_id": lineage.get_or_create_lineage_id(self.journal, self.settings),
            },
        )
        self._set_candidate_status(cand["candidate_id"], CandidateStatus.REJECTED.value)

    def _record_baselines(self, cand: "ScanContext", evaluation) -> None:
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
                "ref_timestamp": (cand.snapshot or {}).get("quote_timestamp"),
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
            "protection_watchdog": self._protection_watchdog_health(),
        }

    def _protection_watchdog_health(self) -> dict:
        """VISIBILITY into broker protection status, mirroring
        _labeller_failsafe_health(). PURE READ; the block itself is enforced
        elsewhere (OrderManager.execute_proposal preflight)."""
        return protection_watchdog.status_report(self.journal)

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
