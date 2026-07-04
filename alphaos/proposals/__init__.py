"""Proposal TTL / stale-approval guard (PR6).

A trade proposal is only approvable while FRESH. This package holds the pure
TTL computation; the actual approval-time guard (and the supersession
side-effect of a new proposal replacing an old one for the same symbol) live
in ``alphaos.orchestrator`` since they need the journal/DB.
"""

from __future__ import annotations

from alphaos.proposals.ttl import (
    compute_expiry,
    is_expired,
    seconds_remaining,
    ttl_seconds_for_session,
)

__all__ = [
    "compute_expiry",
    "is_expired",
    "seconds_remaining",
    "ttl_seconds_for_session",
]
