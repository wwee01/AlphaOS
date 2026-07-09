"""Earnings-proximity enricher (PR5).

Wraps ``EarningsProximityProvider`` with the same fail-safe posture as
alphaos/news/catalyst_enricher.py and alphaos/research/last30days_enricher.py:
NEVER raises, disabled/error/no-data all surface as an explicit non-OK status
(never silently "safe"), and a per-scan symbol cap is enforced by the caller
(orchestrator), matching the news/last30days precedent.

Two-stage hold-window calculation (mirrors this PR's spec's own suggested
defaults: "use proposal/candidate max_holding_days where available... if
unknown, use a conservative default"): the provider is fetched ONCE per
candidate per scan (``enrich()``), computing the warning-window flag (which
only needs a fixed day count, known immediately) using a conservative default
hold length. ``recompute_with_hold_days()`` cheaply re-derives the
hold-window-specific flag once a REAL max_holding_days becomes known (at
decision_adjustments/proposal/reject time), WITHOUT re-fetching from the
provider -- so the fetch happens once, the classification refines as more
context becomes available.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Optional

from alphaos.constants import EarningsDataStatus, Severity
from alphaos.earnings.earnings_provider import EarningsProximityProvider, make_earnings_provider
from alphaos.util import timeutils

_UNAVAILABLE_STATUSES = frozenset({
    EarningsDataStatus.UNAVAILABLE.value,
    EarningsDataStatus.UNKNOWN.value,
    EarningsDataStatus.STALE.value,
    EarningsDataStatus.PROVIDER_DISABLED.value,
})


@dataclass
class EarningsProximityContext:
    symbol: str
    earnings_date: Optional[str] = None
    earnings_timing: str = "unknown"
    days_until_earnings: Optional[int] = None
    hold_days_used: Optional[int] = None
    earnings_within_hold_window: Optional[int] = None   # tri-state: None means "not yet known"
    earnings_within_warning_window: Optional[int] = None
    earnings_data_status: str = EarningsDataStatus.UNKNOWN.value
    confidence: float = 0.0
    source: str = "unknown"
    provider: str = "unknown"
    enrichment_status: str = "ok"    # ok | disabled | error | skipped
    enrichment_error: Optional[str] = None
    risk_tags: list = field(default_factory=list)
    fetched_at_utc: str = ""

    def to_row(self, candidate_id: str, packet_id: Optional[str], scan_batch_id: Optional[str]) -> dict:
        from alphaos.util.ids import new_id

        return {
            "earnings_id": new_id("earn"),
            "candidate_id": candidate_id,
            "packet_id": packet_id,
            "scan_batch_id": scan_batch_id,
            "symbol": self.symbol,
            "earnings_date": self.earnings_date,
            "earnings_timing": self.earnings_timing,
            "days_until_earnings": self.days_until_earnings,
            "hold_days_used": self.hold_days_used,
            "earnings_within_hold_window": self.earnings_within_hold_window,
            "earnings_within_warning_window": self.earnings_within_warning_window,
            "earnings_data_status": self.earnings_data_status,
            "confidence": self.confidence,
            "source": self.source,
            "provider": self.provider,
            "enrichment_status": self.enrichment_status,
            "enrichment_error": self.enrichment_error,
            "risk_tags_json": self.risk_tags,
            "fetched_at_utc": self.fetched_at_utc,
        }

    def summary_fields(self) -> dict:
        """The denormalized subset stamped onto candidates/trade_proposals/
        rejected_candidates/decision_adjustments (same pattern as
        catalyst_status/last30days_status on those tables)."""
        return {
            "earnings_date": self.earnings_date,
            "days_until_earnings": self.days_until_earnings,
            "earnings_within_hold_window": self.earnings_within_hold_window,
            "earnings_within_warning_window": self.earnings_within_warning_window,
            "earnings_timing": self.earnings_timing,
            "earnings_data_status": self.earnings_data_status,
        }


def _days_between(earnings_date_iso: str, today: Optional[date] = None) -> Optional[int]:
    """Calendar days from today to earnings_date_iso (negative if in the past).
    None if earnings_date_iso doesn't parse."""
    try:
        earnings_day = date.fromisoformat(earnings_date_iso)
    except (TypeError, ValueError):
        return None
    ref = today or timeutils.market_date()
    return (earnings_day - ref).days


def _risk_tags(data_status: str, within_hold: Optional[int], within_warning: Optional[int]) -> list:
    tags: list = []
    if data_status in _UNAVAILABLE_STATUSES:
        tags.append("earnings_data_unavailable")
    if within_hold:
        tags.append("earnings_within_hold_window")
    if within_warning and not within_hold:
        tags.append("earnings_within_7d")
        tags.append("earnings_proximity_warning")
    return tags


def compute_proximity_flags(
    earnings_date: Optional[str], data_status: str, hold_days: int, warning_days: int,
    today: Optional[date] = None,
) -> dict:
    """Pure computation: given an already-fetched earnings_date + status, plus
    a hold-window length and a warning-window length, return
    {days_until_earnings, earnings_within_hold_window, earnings_within_warning_window,
    risk_tags}. Never raises. Unavailable/unknown/stale data yields
    days_until_earnings=None and both window flags False (not None -- a
    trade-level "is this proposal near earnings" check must resolve to a
    concrete bool once a hold length is chosen; the ambiguity lives entirely
    in earnings_data_status, which callers must check separately before
    trusting a False as a real "no")."""
    if data_status in _UNAVAILABLE_STATUSES or not earnings_date:
        return {
            "days_until_earnings": None,
            "earnings_within_hold_window": 0,
            "earnings_within_warning_window": 0,
            "risk_tags": _risk_tags(data_status, 0, 0),
        }
    days_until = _days_between(earnings_date, today)
    if days_until is None:  # unparseable date -- treat like unavailable, never like "safe"
        return {
            "days_until_earnings": None,
            "earnings_within_hold_window": 0,
            "earnings_within_warning_window": 0,
            "risk_tags": _risk_tags(EarningsDataStatus.UNAVAILABLE.value, 0, 0),
        }
    within_hold = 1 if 0 <= days_until <= hold_days else 0
    within_warning = 1 if 0 <= days_until <= warning_days else 0
    return {
        "days_until_earnings": days_until,
        "earnings_within_hold_window": within_hold,
        "earnings_within_warning_window": within_warning,
        "risk_tags": _risk_tags(data_status, within_hold, within_warning),
    }


class EarningsProximityEnricher:
    def __init__(self, settings, journal=None, provider: Optional[EarningsProximityProvider] = None):
        self.settings = settings
        self.journal = journal
        self._provider = provider if provider is not None else make_earnings_provider(settings, journal)

    def _empty(self, symbol: str, status: str, enrichment_status: str, source: str,
               error: Optional[str] = None) -> EarningsProximityContext:
        flags = compute_proximity_flags(None, status, self.settings.earnings_proximity_default_hold_days,
                                        self.settings.earnings_proximity_warning_days)
        return EarningsProximityContext(
            symbol=symbol, earnings_date=None, earnings_timing="unknown",
            days_until_earnings=flags["days_until_earnings"],
            hold_days_used=self.settings.earnings_proximity_default_hold_days,
            earnings_within_hold_window=flags["earnings_within_hold_window"],
            earnings_within_warning_window=flags["earnings_within_warning_window"],
            earnings_data_status=status, confidence=0.0, source=source,
            provider=getattr(self._provider, "name", "disabled"),
            enrichment_status=enrichment_status, enrichment_error=error,
            risk_tags=flags["risk_tags"], fetched_at_utc=timeutils.to_iso(timeutils.now_utc()),
        )

    def enrich(self, packet) -> EarningsProximityContext:
        """Enrich a candidate packet with earnings-proximity context. Never
        raises. Uses the DEFAULT hold-days (the real hold length isn't known
        yet at this point in the pipeline) -- see module docstring."""
        symbol = getattr(packet, "symbol", None)
        if not self.settings.earnings_proximity_enabled or self._provider is None:
            return self._empty(symbol, EarningsDataStatus.PROVIDER_DISABLED.value, "disabled", "disabled")
        try:
            result = self._provider.get_earnings_for_symbol(symbol)
        except Exception as exc:  # fail-safe: never crash the scan
            if self.journal is not None:
                self.journal.log_system_event(
                    Severity.WARNING, "earnings",
                    f"earnings provider failed for {symbol}; failing safe.", {"error": str(exc)},
                )
            status = (EarningsDataStatus.UNAVAILABLE.value
                     if self.settings.earnings_proximity_fail_open_as_unavailable
                     else EarningsDataStatus.UNKNOWN.value)
            return self._empty(symbol, status, "error", getattr(self._provider, "name", "unknown"), error=str(exc))

        hold_days = self.settings.earnings_proximity_default_hold_days
        warning_days = self.settings.earnings_proximity_warning_days
        flags = compute_proximity_flags(result.earnings_date, result.status, hold_days, warning_days)
        return EarningsProximityContext(
            symbol=symbol, earnings_date=result.earnings_date, earnings_timing=result.earnings_timing,
            days_until_earnings=flags["days_until_earnings"], hold_days_used=hold_days,
            earnings_within_hold_window=flags["earnings_within_hold_window"],
            earnings_within_warning_window=flags["earnings_within_warning_window"],
            earnings_data_status=result.status, confidence=result.confidence, source=result.source,
            provider=self._provider.name, enrichment_status="ok", enrichment_error=None,
            risk_tags=flags["risk_tags"], fetched_at_utc=result.fetched_at_utc,
        )

    def skipped_budget_cap(self, packet) -> EarningsProximityContext:
        """For an eligible candidate outside the per-scan enrichment cap --
        distinct from 'checked, no data' (unavailable) or 'disabled'."""
        symbol = getattr(packet, "symbol", None)
        return self._empty(symbol, EarningsDataStatus.UNKNOWN.value, "skipped",
                           getattr(self._provider, "name", "disabled"))


def recompute_with_hold_days(context: EarningsProximityContext, hold_days: int,
                             warning_days: Optional[int] = None) -> EarningsProximityContext:
    """Re-derive the hold-window-specific flags once a REAL max_holding_days is
    known (decision_adjustments/proposal/reject time), WITHOUT re-fetching
    from the provider. Never raises. Returns a new context (does not mutate
    the original candidate-level enrichment record)."""
    try:
        w_days = warning_days if warning_days is not None else 7
        flags = compute_proximity_flags(context.earnings_date, context.earnings_data_status,
                                        hold_days, w_days)
        return replace(
            context, hold_days_used=hold_days,
            days_until_earnings=flags["days_until_earnings"],
            earnings_within_hold_window=flags["earnings_within_hold_window"],
            earnings_within_warning_window=flags["earnings_within_warning_window"],
            risk_tags=flags["risk_tags"],
        )
    except Exception:
        return context
