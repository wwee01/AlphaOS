"""Candidate scanner.

Builds a liquid US stock/ETF universe, pulls Massive snapshots (with source
timestamps), gates each on data freshness, and detects momentum candidates:
relative strength / recent momentum, unusual volume, clean trend, acceptable
liquidity and spread.

Stale/unverifiable or illiquid names are not silently dropped — they are written
to ``rejected_candidates`` with a reason. News and AI evaluation happen later in
the orchestrator (the scanner does not touch news).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from alphaos.constants import (
    CandidateStatus,
    NewsStatus,
    PLAYBOOK_V1,
    ReasonCode,
    Severity,
    SHADOW_LIQUIDITY_INSTRUMENTATION_VERSION_V1,
    Strategy,
    TradeDirection,
    UniverseTier,
)
from alphaos.data.freshness_guard import FreshnessGuard, quote_crossed_or_invalid
from alphaos.data.market_data import MarketDataClient
from alphaos import lineage
from alphaos.cards.registry import get_default_card
from alphaos.scanner.interest_scanner import InterestScanner
from alphaos.scanner.scan_context import ScanContext
from alphaos.util.ids import new_id

# A small, deliberately liquid default universe for v1 (core tier). Illiquid
# small caps / penny stocks are intentionally excluded.
DEFAULT_UNIVERSE = [
    "SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "AMZN",
    "GOOGL", "META", "NFLX", "AVGO", "JPM", "XLK", "XLE", "XLF", "SMH", "COST",
]

# EXP-0/INSTR-1 (flipped 2026-07-09): every shadow-tier candidate row stamps
# this. Pre-INSTR-1 interest ranks are known-biased (dead intraday
# rel_volume) and must never be silently mixed with post-fix rows in
# analysis -- segment on this field, never assume it's uniform.
CURRENT_INSTRUMENT_VERSION = "instr1"

# EXP-1 mechanism 3: interest_score_shadow_v1 -- tier-scoped recalibration of
# InterestScanner's megacap-calibrated scale constants (0.06 change-scale /
# 2.0 rel-vol scale / 0.02 day-range floor) plus this module's own momentum
# caps (8% change / 3x rel-vol) for the shadow small/mid band. Same formula
# shape, no fork -- see interest_scanner.py's own InterestScanner docstring.
#
# ******************** PROVISIONAL -- NOT DATA-DERIVED ********************
# The spec mandates these literals come from a build-time saturation audit
# of EXP-0's own accumulated instr1 shadow captures in the OPERATOR'S REAL
# data/alphaos.db ("the data answers 0.12-vs-0.15, this spec doesn't
# guess") -- never a guess, never env-tunable, never retro-scored. That
# audit could NOT be run this build session: computing it requires reading
# the live production alphaos.db (~1132 shadow-tier rows across ~4 trading
# days since 2026-07-10), and this session's sandbox permission classifier
# refused that read as a production-database access outside this build's
# remit (this build's own instructions separately forbid writing to that
# same file, and the read was declined out of caution for the same reason).
# scripts/shadow_saturation_audit.py is written and ready -- run it against
# the real DB and replace every literal below (bumping the version to _v2)
# before ever setting SHADOW_LABELLING_ENABLED=true. Until then these are
# reasoned placeholders only (roughly 2x the megacap scale, since small/mid
# names move more per dollar of catalyst and the spec explicitly warns
# today's megacap constants saturate at this band), not evidence.
SHADOW_V1_CHANGE_SCALE = 0.12          # PROVISIONAL (megacap: 0.06)
SHADOW_V1_REL_VOL_SCALE = 3.0          # PROVISIONAL (megacap: 2.0)
SHADOW_V1_DAY_RANGE_MIN = 0.035        # PROVISIONAL (megacap: 0.02)
SHADOW_V1_MOMENTUM_CHANGE_CAP = 0.12   # PROVISIONAL (megacap: 0.08, candidate_scanner.py momentum_score)
SHADOW_V1_MOMENTUM_RELVOL_CAP = 4.0    # PROVISIONAL (megacap: 3.0, candidate_scanner.py momentum_score)


def _spread_pct_mid(snapshot: dict) -> Optional[float]:
    """EXP-1 mechanism 10: spread as a fraction of the quote MIDPOINT, not
    ``last_price`` (the snapshot's existing ``spread_pct`` divides by
    possibly-stale last-trade price -- a cleaner, less lagged denominator
    for the liquidity instrumentation specifically)."""
    bid, ask = snapshot.get("bid"), snapshot.get("ask")
    if bid is None or ask is None:
        return None
    mid = (bid + ask) / 2.0
    if not mid:
        return None
    return round((ask - bid) / mid, 6)


@dataclass
class ScanResult:
    scan_id: str
    candidates: "list[ScanContext]" = field(default_factory=list)
    snapshots: int = 0
    blocked_stale: int = 0
    rejected_illiquid: int = 0
    # EXP-0: per-symbol outcome for the shadow-tier pass ONLY (core-tier scans
    # leave this empty -- ScanResult's shape/behavior for the core path is
    # otherwise byte-identical to before EXP-0). Keyed by symbol:
    # {"freshness_status": str, "candidate_id": Optional[str]} -- lets the
    # orchestrator write one universe_days survivorship row per requested
    # shadow symbol, including ones that never became a candidate.
    per_symbol: "dict[str, dict]" = field(default_factory=dict)


class CandidateScanner:
    def __init__(self, settings, journal, market_data: Optional[MarketDataClient] = None):
        self.settings = settings
        self.journal = journal
        self.market = market_data or MarketDataClient(settings, journal)
        self.freshness = FreshnessGuard.from_settings(settings)
        self.interest = InterestScanner(settings)   # Roadmap 2.3: deterministic interest scoring
        # EXP-1 mechanism 3: a SEPARATE InterestScanner instance, same class/
        # formula, tier-scoped constants only -- used for the shadow-tier
        # pass exclusively. The core `self.interest` above is never touched.
        self.interest_shadow = InterestScanner(
            settings, change_scale=SHADOW_V1_CHANGE_SCALE,
            rel_vol_scale=SHADOW_V1_REL_VOL_SCALE, day_range_min=SHADOW_V1_DAY_RANGE_MIN,
        )
        self._spy: Optional[dict] = None
        self._qqq: Optional[dict] = None

    def build_universe(
        self, scan_id: str, symbols: Optional[list[str]] = None,
        tier: str = UniverseTier.CORE.value, universe_file_version: Optional[int] = None,
    ) -> list[str]:
        symbols = symbols or DEFAULT_UNIVERSE
        for sym in symbols:
            self.journal.insert(
                "universe",
                {
                    "symbol": sym,
                    "asset_class": "etf" if sym in {"SPY", "QQQ", "IWM", "DIA", "XLK", "XLE", "XLF", "SMH"} else "stock",
                    "tier": tier,
                    "is_active": 1,
                    "scan_id": scan_id,
                    "universe_file_version": universe_file_version,
                },
            )
        return symbols

    def scan(
        self, symbols: Optional[list[str]] = None, scan_batch_id: Optional[str] = None
    ) -> ScanResult:
        # When the orchestrator mints a scan_batch_id, use it as the scan_id so
        # candidates.scan_id == the batch id and a candidate row also carries the
        # explicit scan_batch_id link.
        scan_id = scan_batch_id or new_id("scan")
        self._scan_batch_id = scan_batch_id
        result = ScanResult(scan_id=scan_id)
        symbols = self.build_universe(scan_id, symbols)
        # Index references for relative-strength signals (best-effort; never fatal).
        try:
            self._spy = self.market.get_snapshot("SPY")
            self._qqq = self.market.get_snapshot("QQQ")
        except Exception:  # pragma: no cover - defensive; rel-strength just degrades
            self._spy = self._qqq = None
        self.journal.log_system_event(
            Severity.INFO, "scanner", f"Scan {scan_id} over {len(symbols)} symbols started."
        )

        for sym in symbols:
            snapshot = self.market.get_snapshot(sym)
            report = self.freshness.assess(snapshot)
            snapshot_id = new_id("snap")
            self._persist_snapshot(snapshot_id, snapshot, report)
            result.snapshots += 1

            # Freshness gate: never evaluate a candidate on stale/unverifiable data.
            if not report.is_usable:
                result.blocked_stale += 1
                self._reject(
                    None, sym, "scan", report.block_reason or ReasonCode.STALE_DATA.value,
                    f"freshness={report.freshness_status}", snapshot,
                )
                continue

            # Tradeability gate: crossed/invalid quote, liquidity, spread.
            reason = self._tradeability_reason(snapshot)
            if reason is not None:
                result.rejected_illiquid += 1
                self._reject(None, sym, "scan", reason, "tradeability gate", snapshot)
                continue

            cand = self._maybe_candidate(scan_id, sym, snapshot, snapshot_id)
            if cand is not None:
                result.candidates.append(cand)

        self.journal.log_system_event(
            Severity.INFO,
            "scanner",
            f"Scan {scan_id} done: {len(result.candidates)} candidates, "
            f"{result.blocked_stale} stale-blocked, {result.rejected_illiquid} illiquid.",
        )
        return result

    def scan_shadow_tier(
        self, symbols: list[str], scan_batch_id: Optional[str] = None,
        universe_file_version: Optional[int] = None, scan_window: Optional[str] = None,
        adv_by_symbol: Optional[dict] = None,
    ) -> ScanResult:
        """EXP-0: the shadow-tier pass -- same 3 windows (batch snapshot ->
        freshness assess -> deterministic interest score) as ``scan()``, but
        against the committed shadow-universe symbol list, tagged
        ``tier=watchlist`` / ``shadow_tier=1``.

        Returns its OWN, entirely separate ``ScanResult`` -- callers (the
        orchestrator) must NEVER feed this result's ``.candidates`` into the
        AI-evaluation/proposal-creation loop. That is a structural property of
        HOW this result is used downstream, not something this method can
        enforce by itself (this class never calls AI/proposal code either
        way) -- see ``Orchestrator.run_scan_once``'s own chokepoint guards for
        the belt-and-suspenders backstop.

        Uses ``self.market.get_snapshots()`` (one batched call per ~100
        symbols) rather than looping ``get_snapshot()`` per symbol -- the
        whole reason EXP-0 needed the batch endpoint at all.
        """
        scan_id = scan_batch_id or new_id("scan")
        self._scan_batch_id = scan_batch_id
        result = ScanResult(scan_id=scan_id)
        symbols = self.build_universe(
            scan_id, symbols, tier=UniverseTier.WATCHLIST.value,
            universe_file_version=universe_file_version,
        )
        snapshots = self.market.get_snapshots(symbols)

        for sym, snapshot in zip(symbols, snapshots):
            report = self.freshness.assess(snapshot)
            snapshot_id = new_id("snap")
            self._persist_snapshot(snapshot_id, snapshot, report)
            result.snapshots += 1
            result.per_symbol[sym] = {"freshness_status": report.freshness_status, "candidate_id": None}

            if not report.is_usable:
                result.blocked_stale += 1
                self._reject(
                    None, sym, "shadow_scan", report.block_reason or ReasonCode.STALE_DATA.value,
                    f"freshness={report.freshness_status}", snapshot,
                )
                continue

            # EXP-1 mechanism 10 INVARIANT: the core tradeability gate
            # (MIN_DOLLAR_VOLUME/MAX_SPREAD_PCT/crossed-quote) NEVER filters
            # shadow capture or selection eligibility -- the $2M cumulative
            # floor alone fails roughly half the band before noon; gating
            # would censor exactly the tail COST-1 needs. Compute the
            # verdict (for instrumentation/reporting) but never act on it
            # here -- freshness above is a SEPARATE, still-enforced gate
            # (data trustworthiness, not tradeability).
            core_gate_verdict = self._tradeability_reason(snapshot) or "pass"

            cand = self._maybe_candidate(
                scan_id, sym, snapshot, snapshot_id,
                shadow_tier=True, instrument_version=CURRENT_INSTRUMENT_VERSION,
                interest_scanner=self.interest_shadow, core_gate_verdict=core_gate_verdict,
                scan_window=scan_window,
                adv_20d_dollar=(adv_by_symbol or {}).get(sym),
                quote_age_seconds=report.quote_age_seconds,
            )
            if cand is not None:
                result.candidates.append(cand)
                result.per_symbol[sym]["candidate_id"] = cand["candidate_id"]

        self.journal.log_system_event(
            Severity.INFO,
            "scanner",
            f"Shadow-tier scan {scan_id} over {len(symbols)} symbols: "
            f"{len(result.candidates)} candidates, {result.blocked_stale} stale-blocked, "
            f"{result.rejected_illiquid} illiquid.",
        )
        return result

    # ------------------------------------------------------------- internals
    def _tradeability_reason(self, snapshot: dict) -> Optional[str]:
        """Return a reason code if the symbol is not tradeable, else None.

        Rejects crossed/non-positive quotes (a negative spread must not pass the
        ``spread_pct > max`` gate), then liquidity, then spread.
        """
        if quote_crossed_or_invalid(snapshot):
            return ReasonCode.CROSSED_QUOTE.value
        dv = snapshot.get("dollar_volume")
        if dv is not None and dv < self.settings.min_dollar_volume:
            return ReasonCode.LOW_LIQUIDITY.value
        sp = snapshot.get("spread_pct")
        if sp is not None and sp >= 0 and sp > self.settings.max_spread_pct:
            return ReasonCode.WIDE_SPREAD.value
        return None

    def _maybe_candidate(
        self, scan_id, sym, snapshot, snapshot_id,
        shadow_tier: bool = False, instrument_version: Optional[str] = None,
        interest_scanner: Optional[InterestScanner] = None,
        core_gate_verdict: Optional[str] = None, scan_window: Optional[str] = None,
        adv_20d_dollar: Optional[float] = None, quote_age_seconds: Optional[float] = None,
    ) -> Optional[ScanContext]:
        change = float(snapshot.get("change_pct") or 0.0)
        rel_vol = float(snapshot.get("rel_volume") or 1.0)
        # Roadmap 2.3: deterministic market-interest signals (broadens discovery
        # beyond pure momentum: gap / near hi-lo / rel-strength / breakout /
        # reversal / volatility). "Interesting" != "trade" — the trade decision
        # is still owned by the OpenAI eval + the existing safety gates.
        # EXP-1 mechanism 3: shadow-tier callers pass `self.interest_shadow`
        # (tier-scoped constants); core callers omit this and get the
        # unmodified `self.interest` -- byte-identical to pre-EXP-1 behavior.
        scorer = interest_scanner or self.interest
        signals = scorer.score(snapshot, self._spy, self._qqq)
        # Candidate if momentum-y OR interesting enough for AI classification.
        # EXP-1 mechanism 3: momentum caps are ALSO tier-scoped -- shadow
        # rows use the recalibrated 12%/4x caps, core keeps 8%/3x unchanged.
        momentum_change_cap = SHADOW_V1_MOMENTUM_CHANGE_CAP if shadow_tier else 0.08
        momentum_relvol_cap = SHADOW_V1_MOMENTUM_RELVOL_CAP if shadow_tier else 3.0
        is_momentum = abs(change) >= 0.02 or rel_vol >= 1.5
        is_candidate = is_momentum or signals.interest_score >= self.settings.interest_min_score
        if not is_candidate:
            return None
        direction = TradeDirection.LONG.value if change >= 0 else TradeDirection.SHORT.value
        momentum_score = round(
            min(1.0, (abs(change) / momentum_change_cap) * 0.6 + min(rel_vol / momentum_relvol_cap, 1.0) * 0.4), 3,
        )
        trend_quality = round(min(1.0, abs(change) * 10), 3)

        candidate_id = new_id("cand")
        asset_type = "etf" if sym in {"SPY", "QQQ", "IWM", "DIA", "XLK", "XLE", "XLF", "SMH"} else "stock"
        card = get_default_card()
        cand = {
            "candidate_id": candidate_id,
            "scan_id": scan_id,
            "scan_batch_id": getattr(self, "_scan_batch_id", None),
            "symbol": sym,
            "direction": direction,
            "strategy": Strategy.SWING.value,
            "momentum_score": momentum_score,
            "rel_strength": round(change, 4),
            "unusual_volume": rel_vol,
            "trend_quality": trend_quality,
            "liquidity_ok": 1,
            "spread_ok": 1,
            "news_status": NewsStatus.NEWS_UNAVAILABLE.value,  # set later by orchestrator
            "price_snapshot_id": snapshot_id,
            "status": CandidateStatus.DETECTED.value,
            # --- Trade Packet v1 evidence fields ---
            "asset_type": asset_type,
            "playbook_name": PLAYBOOK_V1,
            "setup_classification": "momentum_continuation",
            "card_id": card["card_id"],
            "card_version": card["version"],
            "status_reason": CandidateStatus.DETECTED.value,
            "price_at_scan": snapshot.get("last_price"),
            "volume_at_scan": snapshot.get("volume"),
            # --- Roadmap 2.3: deterministic interest evidence (rank assigned later) ---
            "interest_score": signals.interest_score,
            "shortlist_reason": signals.shortlist_reason,
            "notes_json": {"snapshot": {k: snapshot.get(k) for k in ("last_price", "change_pct", "rel_volume")}},
            # PR4: measurement-only lineage stamp (never influences the candidate decision above).
            "lineage_id": lineage.get_or_create_lineage_id(self.journal, self.settings),
            # --- EXP-0: shadow tier stamping. shadow_tier=1 is the structural
            # marker the orchestrator's chokepoint guards refuse on -- this
            # scanner class never calls AI/proposal code itself either way. ---
            "shadow_tier": 1 if shadow_tier else 0,
            "instrument_version": instrument_version,
        }
        if shadow_tier:
            # EXP-1 mechanism 10: liquidity instrumentation -- RECORD, NEVER
            # GATE (see the INVARIANT comment in scan_shadow_tier). NULL for
            # every core-tier row (additive, shadow-only fields).
            dollar_volume = snapshot.get("dollar_volume")
            cand.update({
                "bid_size": snapshot.get("bid_size"),
                "ask_size": snapshot.get("ask_size"),
                "quote_age_seconds": quote_age_seconds,
                "spread_pct_mid": _spread_pct_mid(snapshot),
                "adv_20d_dollar": adv_20d_dollar,
                "volume_today_pct_of_adv": (
                    round(dollar_volume / adv_20d_dollar, 4)
                    if (dollar_volume and adv_20d_dollar) else None
                ),
                "scan_window": scan_window,
                "data_feed": snapshot.get("feed"),
                "crossed_or_locked_quote": 1 if quote_crossed_or_invalid(snapshot) else 0,
                "core_gate_verdict": core_gate_verdict,
                "liquidity_instrumentation_version": SHADOW_LIQUIDITY_INSTRUMENTATION_VERSION_V1,
            })
        self.journal.insert("candidates", cand)
        # Keep a dict the orchestrator can use directly (with last_price handy).
        cand["last_price"] = snapshot.get("last_price")
        ctx = ScanContext(row=cand)
        ctx.snapshot = snapshot
        ctx.interest = signals   # full InterestSignals for the packet builder
        return ctx

    def _persist_snapshot(self, snapshot_id, snapshot, report) -> None:
        self.journal.insert(
            "price_snapshots",
            {
                "snapshot_id": snapshot_id,
                "symbol": snapshot.get("symbol"),
                "provider": snapshot.get("provider"),
                "feed": snapshot.get("feed"),
                "is_mock": 1 if snapshot.get("is_mock") else 0,
                "last_price": snapshot.get("last_price"),
                "bid": snapshot.get("bid"),
                "ask": snapshot.get("ask"),
                "spread": snapshot.get("spread"),
                "spread_pct": snapshot.get("spread_pct"),
                "volume": snapshot.get("volume"),
                "dollar_volume": snapshot.get("dollar_volume"),
                "bar_open": snapshot.get("bar_open"),
                "bar_high": snapshot.get("bar_high"),
                "bar_low": snapshot.get("bar_low"),
                "bar_close": snapshot.get("bar_close"),
                "quote_timestamp": report.quote_timestamp,
                "bar_timestamp": report.bar_timestamp,
                "quote_age_seconds": report.quote_age_seconds,
                "bar_age_seconds": report.bar_age_seconds,
                "source_timestamp": report.source_timestamp,
                "received_at": report.received_at,
                "data_delay_seconds": report.data_delay_seconds,
                "market_session": report.market_session,
                "freshness_status": report.freshness_status,
                "is_usable": 1 if report.is_usable else 0,
                "block_reason": report.block_reason,
            },
        )

    def _reject(self, candidate_id, symbol, stage, reason_code, detail, snapshot) -> None:
        self.journal.insert(
            "rejected_candidates",
            {
                "rejection_id": new_id("rej"),
                "candidate_id": candidate_id,
                "symbol": symbol,
                "stage": stage,
                "reason_code": reason_code,
                "reason_detail": detail,
                "would_be_entry": snapshot.get("last_price") if snapshot else None,
                "scan_batch_id": getattr(self, "_scan_batch_id", None),
                "lineage_id": lineage.get_or_create_lineage_id(self.journal, self.settings),
            },
        )
