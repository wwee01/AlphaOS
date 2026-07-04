"""Proposal TTL computation (PR6): session-bucket selection, expiry-instant
computation, and the fail-safe is_expired()/seconds_remaining() contract.
Pure functions -- hermetic, no journal/orchestrator involved."""

from __future__ import annotations

from datetime import timedelta

from alphaos.config.settings import load_settings
from alphaos.proposals.ttl import (
    compute_expiry,
    is_expired,
    seconds_remaining,
    ttl_seconds_for_session,
)
from alphaos.util import timeutils
from conftest import make_settings


def _settings(**over):
    return make_settings(**over)


# --------------------------------------------------------- ttl_seconds_for_session
def test_regular_session_uses_rth_bucket():
    s = _settings(PROPOSAL_TTL_RTH_SECONDS="1800")
    assert ttl_seconds_for_session(s, "regular") == 1800


def test_premarket_and_afterhours_share_the_extended_hours_bucket():
    s = _settings(PROPOSAL_TTL_EXTENDED_HOURS_SECONDS="300")
    assert ttl_seconds_for_session(s, "premarket") == 300
    assert ttl_seconds_for_session(s, "afterhours") == 300


def test_closed_session_uses_the_shortest_bucket():
    s = _settings(PROPOSAL_TTL_CLOSED_SESSION_SECONDS="0")
    assert ttl_seconds_for_session(s, "closed") == 0


def test_unrecognized_session_fails_safe_to_closed_bucket():
    """Defensive: market_session() today only ever returns one of 4 known
    values, but if a future session type appeared, TTL must fail safe (use
    the MOST conservative bucket), never silently fall through to RTH."""
    s = _settings(PROPOSAL_TTL_RTH_SECONDS="3600", PROPOSAL_TTL_CLOSED_SESSION_SECONDS="0")
    assert ttl_seconds_for_session(s, "some_future_session_type") == 0


# --------------------------------------------------------------- compute_expiry
def test_compute_expiry_with_explicit_session():
    s = _settings(PROPOSAL_TTL_RTH_SECONDS="1800")
    created = timeutils.to_iso(timeutils.now_utc())
    result = compute_expiry(s, created, session="regular")
    assert result["proposal_ttl_seconds"] == 1800
    expected_expiry = timeutils.parse_iso(created) + timedelta(seconds=1800)
    actual_expiry = timeutils.parse_iso(result["proposal_expires_at_utc"])
    assert abs((actual_expiry - expected_expiry).total_seconds()) < 1


def test_compute_expiry_defaults_to_live_session_when_none_given():
    s = _settings()
    created = timeutils.to_iso(timeutils.now_utc())
    live_session = timeutils.market_session().value
    result = compute_expiry(s, created)  # session omitted
    assert result["proposal_ttl_seconds"] == ttl_seconds_for_session(s, live_session)


def test_compute_expiry_fails_safe_on_unparseable_created_at():
    s = _settings(PROPOSAL_TTL_RTH_SECONDS="1800")
    result = compute_expiry(s, "not-a-timestamp", session="regular")
    assert result["proposal_ttl_seconds"] == 0
    assert result["proposal_expires_at_utc"] == "not-a-timestamp"


# ------------------------------------------------------------ seconds_remaining
def test_seconds_remaining_positive_when_fresh():
    future = timeutils.to_iso(timeutils.now_utc() + timedelta(minutes=10))
    remaining = seconds_remaining(future)
    assert remaining is not None and remaining > 0


def test_seconds_remaining_negative_when_past_expiry():
    past = timeutils.to_iso(timeutils.now_utc() - timedelta(minutes=10))
    remaining = seconds_remaining(past)
    assert remaining is not None and remaining < 0


def test_seconds_remaining_none_when_missing():
    assert seconds_remaining(None) is None
    assert seconds_remaining("") is None


def test_seconds_remaining_none_when_unparseable():
    assert seconds_remaining("garbage") is None


# ------------------------------------------------------------------ is_expired
def test_is_expired_false_when_fresh():
    future = timeutils.to_iso(timeutils.now_utc() + timedelta(minutes=10))
    assert is_expired(future) is False


def test_is_expired_true_when_past():
    past = timeutils.to_iso(timeutils.now_utc() - timedelta(seconds=1))
    assert is_expired(past) is True


def test_is_expired_true_at_exact_boundary():
    """remaining == 0 counts as expired (inclusive boundary: "<=", not "<")."""
    now = timeutils.now_utc()
    assert is_expired(timeutils.to_iso(now), now=now) is True


def test_is_expired_true_when_missing():
    """A missing expiry (e.g. a pre-PR6 DB row) must NEVER read as fresh."""
    assert is_expired(None) is True
    assert is_expired("") is True


def test_is_expired_true_when_unparseable():
    assert is_expired("garbage-timestamp") is True


# --------------------------------------------------------- settings integration
def test_default_settings_load_with_sane_ttl_values():
    s = load_settings(load_env_file=False, env={"ALPHAOS_MODE": "mock"})
    assert s.proposal_ttl_rth_seconds == 1800
    assert s.proposal_ttl_extended_hours_seconds == 300
    assert s.proposal_ttl_closed_session_seconds == 0
