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
    NewsStatus,
    ReasonCode,
    Severity,
    Strategy,
    TradeDirection,
    UniverseTier,
)
from alphaos.data.freshness_guard import FreshnessGuard, quote_crossed_or_invalid
from alphaos.data.market_data import MarketDataClient
from alphaos.util.ids import new_id

# A small, deliberately liquid default universe for v1 (core tier). Illiquid
# small caps / penny stocks are intentionally excluded.
DEFAULT_UNIVERSE = [
    "SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "AMZN",
    "GOOGL", "META", "NFLX", "AVGO", "JPM", "XLK", "XLE", "XLF", "SMH", "COST",
]


@dataclass
class ScanResult:
    scan_id: str
    candidates: list = field(default_factory=list)
    snapshots: int = 0
    blocked_stale: int = 0
    rejected_illiquid: int = 0


class CandidateScanner:
    def __init__(self, settings, journal, market_data: Optional[MarketDataClient] = None):
        self.settings = settings
        self.journal = journal
        self.market = market_data or MarketDataClient(settings, journal)
        self.freshness = FreshnessGuard.from_settings(settings)

    def build_universe(self, scan_id: str, symbols: Optional[list[str]] = None) -> list[str]:
        symbols = symbols or DEFAULT_UNIVERSE
        for sym in symbols:
            self.journal.insert(
                "universe",
                {
                    "symbol": sym,
                    "asset_class": "etf" if sym in {"SPY", "QQQ", "IWM", "DIA", "XLK", "XLE", "XLF", "SMH"} else "stock",
                    "tier": UniverseTier.CORE.value,
                    "is_active": 1,
                    "scan_id": scan_id,
                },
            )
        return symbols

    def scan(self, symbols: Optional[list[str]] = None) -> ScanResult:
        scan_id = new_id("scan")
        result = ScanResult(scan_id=scan_id)
        symbols = self.build_universe(scan_id, symbols)
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

    def _maybe_candidate(self, scan_id, sym, snapshot, snapshot_id) -> Optional[dict]:
        change = float(snapshot.get("change_pct") or 0.0)
        rel_vol = float(snapshot.get("rel_volume") or 1.0)
        # Momentum candidate: meaningful move OR unusual volume.
        is_candidate = abs(change) >= 0.02 or rel_vol >= 1.5
        if not is_candidate:
            return None
        direction = TradeDirection.LONG.value if change >= 0 else TradeDirection.SHORT.value
        momentum_score = round(min(1.0, (abs(change) / 0.08) * 0.6 + min(rel_vol / 3.0, 1.0) * 0.4), 3)
        trend_quality = round(min(1.0, abs(change) * 10), 3)

        candidate_id = new_id("cand")
        cand = {
            "candidate_id": candidate_id,
            "scan_id": scan_id,
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
            "status": "detected",
            "notes_json": {"snapshot": {k: snapshot.get(k) for k in ("last_price", "change_pct", "rel_volume")}},
        }
        self.journal.insert("candidates", cand)
        # Keep a dict the orchestrator can use directly (with last_price handy).
        cand["last_price"] = snapshot.get("last_price")
        cand["_snapshot"] = snapshot
        return cand

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
            },
        )
