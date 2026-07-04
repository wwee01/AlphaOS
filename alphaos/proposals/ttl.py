"""Proposal TTL computation (PR6).

TTL is computed ONCE, at proposal-creation time, from the market session
active at that instant, and frozen into ``proposal_ttl_seconds`` /
``proposal_expires_at_utc`` so it can never silently drift if settings change
later -- it is never recomputed against the session at approval time. This
mirrors the "anchor on source, not record time" principle used elsewhere in
this codebase (PR4 lineage snapshots, PR5's two-stage earnings recompute).

Fail-safe throughout: an unrecognized/future session value, an unparseable
timestamp, or a missing expiry (e.g. a proposal row from before this PR
existed) is always treated as the MORE conservative case -- shortest TTL,
already-expired -- never as "plenty of time left" or "not applicable".
"""

from __future__ import annotations

from datetime import timedelta, timezone
from typing import Optional

from alphaos.constants import MarketSession
from alphaos.util import timeutils


def ttl_seconds_for_session(settings, session: str) -> int:
    """The TTL (seconds) for a proposal born during ``session``. Regular hours
    get the least-conservative bucket; premarket+afterhours share ONE bucket
    (mirrors FreshnessGuard's own precedent of one lenient threshold pair for
    both); CLOSED -- or any value not recognized above -- gets the most
    conservative bucket. Fail safe, never fail lenient."""
    if session == MarketSession.REGULAR.value:
        return int(settings.proposal_ttl_rth_seconds)
    if session in (MarketSession.PREMARKET.value, MarketSession.AFTERHOURS.value):
        return int(settings.proposal_ttl_extended_hours_seconds)
    return int(settings.proposal_ttl_closed_session_seconds)


def compute_expiry(settings, created_at_utc: str, session: Optional[str] = None) -> dict:
    """{"proposal_ttl_seconds", "proposal_expires_at_utc"} for a proposal
    created at ``created_at_utc``. ``session`` defaults to the CURRENT live
    session (the normal case: a proposal being created right now). An
    unparseable ``created_at_utc`` fails safe -- TTL 0, expires immediately."""
    if session is None:
        session = timeutils.market_session().value
    ttl_seconds = ttl_seconds_for_session(settings, session)
    created = timeutils.parse_iso(created_at_utc)
    if created is None:
        return {"proposal_ttl_seconds": 0, "proposal_expires_at_utc": created_at_utc}
    expires_at = created + timedelta(seconds=ttl_seconds)
    return {
        "proposal_ttl_seconds": ttl_seconds,
        "proposal_expires_at_utc": timeutils.to_iso(expires_at),
    }


def seconds_remaining(expires_at_utc: Optional[str], now=None) -> Optional[float]:
    """Seconds until expiry (negative once expired). None only when
    ``expires_at_utc`` itself is missing/unparseable -- callers must treat
    None as unknown/stale, never as fresh (see ``is_expired``)."""
    if not expires_at_utc:
        return None
    expires = timeutils.parse_iso(expires_at_utc)
    if expires is None:
        return None
    ref = now or timeutils.now_utc()
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return (expires - ref).total_seconds()


def is_expired(expires_at_utc: Optional[str], now=None) -> bool:
    """True iff ``now`` is at/after the expiry instant. A missing/unparseable
    ``expires_at_utc`` -- e.g. a proposal row written before this PR existed,
    or any other loss of TTL context -- is treated as EXPIRED: an unknown
    expiry can never be mistaken for "still fresh" (fail safe, per this PR's
    "if session/time context is unclear, treat as stale" requirement)."""
    remaining = seconds_remaining(expires_at_utc, now)
    if remaining is None:
        return True
    return remaining <= 0
