"""Earnings-proximity enricher (PR5): status derivation, fail-safe, the
distinct budget-skip record, the two-stage hold-window recompute, and the
advisory-only contract. Hermetic -- providers are stubs; nothing shells out."""

from __future__ import annotations

from datetime import date, timedelta

from alphaos.constants import EarningsDataStatus, EarningsTiming
from alphaos.earnings.earnings_enricher import (
    EarningsProximityEnricher,
    compute_proximity_flags,
    recompute_with_hold_days,
)
from alphaos.earnings.earnings_provider import EarningsProximityResult
from conftest import make_settings


class _StubProvider:
    name = "stub"

    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc

    def get_earnings_for_symbol(self, symbol):
        if self._exc is not None:
            raise self._exc
        return self._result


def _pkt(symbol="AAPL"):
    class _P:
        pass

    p = _P()
    p.symbol = symbol
    return p


def _settings(**over):
    return make_settings(EARNINGS_PROXIMITY_ENABLED="true", **over)


def _result(symbol="AAPL", days_out=None, **kw):
    earnings_date = None
    if days_out is not None:
        earnings_date = (date.today() + timedelta(days=days_out)).isoformat()
    kw.setdefault("status", EarningsDataStatus.OK.value if days_out is not None
                  else EarningsDataStatus.UNAVAILABLE.value)
    return EarningsProximityResult(symbol=symbol, earnings_date=earnings_date, **kw)


# --------------------------------------------------------- compute_proximity_flags
def test_flags_within_hold_window():
    today = date(2026, 1, 1)
    earnings_date = (today + timedelta(days=2)).isoformat()
    flags = compute_proximity_flags(earnings_date, EarningsDataStatus.OK.value,
                                    hold_days=3, warning_days=7, today=today)
    assert flags["days_until_earnings"] == 2
    assert flags["earnings_within_hold_window"] == 1
    assert flags["earnings_within_warning_window"] == 1
    assert "earnings_within_hold_window" in flags["risk_tags"]


def test_flags_within_warning_but_outside_hold():
    today = date(2026, 1, 1)
    earnings_date = (today + timedelta(days=6)).isoformat()
    flags = compute_proximity_flags(earnings_date, EarningsDataStatus.OK.value,
                                    hold_days=3, warning_days=7, today=today)
    assert flags["earnings_within_hold_window"] == 0
    assert flags["earnings_within_warning_window"] == 1
    assert "earnings_within_7d" in flags["risk_tags"]
    assert "earnings_proximity_warning" in flags["risk_tags"]
    assert "earnings_within_hold_window" not in flags["risk_tags"]


def test_flags_outside_warning_window_not_flagged():
    today = date(2026, 1, 1)
    earnings_date = (today + timedelta(days=30)).isoformat()
    flags = compute_proximity_flags(earnings_date, EarningsDataStatus.OK.value,
                                    hold_days=3, warning_days=7, today=today)
    assert flags["earnings_within_hold_window"] == 0
    assert flags["earnings_within_warning_window"] == 0
    assert flags["risk_tags"] == []


def test_flags_past_earnings_not_flagged():
    """An earnings date already in the past (days_until < 0) is not "upcoming"."""
    today = date(2026, 1, 1)
    earnings_date = (today - timedelta(days=2)).isoformat()
    flags = compute_proximity_flags(earnings_date, EarningsDataStatus.OK.value,
                                    hold_days=3, warning_days=7, today=today)
    assert flags["earnings_within_hold_window"] == 0
    assert flags["earnings_within_warning_window"] == 0


def test_flags_unavailable_never_reads_as_safe():
    """Missing data yields concrete False flags (never None), but the status
    field is the caller's REQUIRED signal that this isn't a confirmed no-earnings
    result -- never silently 'safe'."""
    flags = compute_proximity_flags(None, EarningsDataStatus.UNAVAILABLE.value,
                                    hold_days=3, warning_days=7)
    assert flags["days_until_earnings"] is None
    assert flags["earnings_within_hold_window"] == 0
    assert flags["earnings_within_warning_window"] == 0
    assert "earnings_data_unavailable" in flags["risk_tags"]


def test_flags_unparseable_date_treated_like_unavailable():
    flags = compute_proximity_flags("not-a-date", EarningsDataStatus.OK.value,
                                    hold_days=3, warning_days=7)
    assert flags["days_until_earnings"] is None
    assert flags["earnings_within_hold_window"] == 0
    assert "earnings_data_unavailable" in flags["risk_tags"]


# --------------------------------------------------------------- enrich()
def test_enrich_populates_context_for_ok_result():
    res = _result(days_out=2)
    ctx = EarningsProximityEnricher(_settings(), provider=_StubProvider(result=res)).enrich(_pkt())
    assert ctx.earnings_data_status == EarningsDataStatus.OK.value
    assert ctx.enrichment_status == "ok"
    assert ctx.earnings_date == res.earnings_date
    assert ctx.hold_days_used == _settings().earnings_proximity_default_hold_days


def test_enrich_uses_default_hold_days_not_real_one():
    """enrich() runs before the real max_holding_days is known -- it must use the
    conservative DEFAULT, not assume any particular trade's hold length."""
    s = _settings(EARNINGS_PROXIMITY_DEFAULT_HOLD_DAYS="3")
    res = _result(days_out=5)
    ctx = EarningsProximityEnricher(s, provider=_StubProvider(result=res)).enrich(_pkt())
    assert ctx.hold_days_used == 3
    assert ctx.earnings_within_hold_window == 0     # 5 days out > default 3-day hold


def test_fail_open_on_provider_error():
    e = EarningsProximityEnricher(_settings(), provider=_StubProvider(exc=RuntimeError("boom")))
    ctx = e.enrich(_pkt())                            # must NOT raise
    assert ctx.earnings_data_status == EarningsDataStatus.UNAVAILABLE.value
    assert ctx.enrichment_status == "error"
    assert "boom" in (ctx.enrichment_error or "")
    assert ctx.earnings_within_hold_window == 0       # never "safe" by omission


def test_fail_closed_when_configured():
    s = _settings(EARNINGS_PROXIMITY_FAIL_OPEN_AS_UNAVAILABLE="false")
    ctx = EarningsProximityEnricher(s, provider=_StubProvider(exc=RuntimeError("x"))).enrich(_pkt())
    assert ctx.earnings_data_status == EarningsDataStatus.UNKNOWN.value


def test_disabled_returns_provider_disabled_not_safe():
    # No injected provider + master switch off -> make_earnings_provider returns
    # None -> the enricher reports the disabled state explicitly, never as "ok".
    e = EarningsProximityEnricher(make_settings(EARNINGS_PROXIMITY_ENABLED="false"))
    ctx = e.enrich(_pkt())
    assert ctx.earnings_data_status == EarningsDataStatus.PROVIDER_DISABLED.value
    assert ctx.enrichment_status == "disabled"
    assert ctx.earnings_within_hold_window == 0
    assert ctx.earnings_within_warning_window == 0


def test_unavailable_from_provider_surfaces_as_unavailable():
    res = _result(days_out=None)  # no earnings date found
    ctx = EarningsProximityEnricher(_settings(), provider=_StubProvider(result=res)).enrich(_pkt())
    assert ctx.earnings_data_status == EarningsDataStatus.UNAVAILABLE.value
    assert ctx.earnings_date is None
    assert ctx.earnings_within_hold_window == 0


def test_before_open_after_close_unknown_timing_all_pass_through():
    for timing in (EarningsTiming.BEFORE_OPEN.value, EarningsTiming.AFTER_CLOSE.value,
                  EarningsTiming.UNKNOWN.value):
        res = _result(days_out=1, earnings_timing=timing)
        ctx = EarningsProximityEnricher(_settings(), provider=_StubProvider(result=res)).enrich(_pkt())
        assert ctx.earnings_timing == timing


def test_skipped_budget_cap_is_distinct():
    e = EarningsProximityEnricher(_settings(), provider=_StubProvider())
    ctx = e.skipped_budget_cap(_pkt())
    assert ctx.enrichment_status == "skipped"
    assert ctx.earnings_data_status == EarningsDataStatus.UNKNOWN.value
    assert ctx.earnings_within_hold_window == 0
    # explicitly NOT confused with "ran, found nothing" or "provider missing"
    assert ctx.earnings_data_status != EarningsDataStatus.UNAVAILABLE.value


def test_to_row_has_expected_shape():
    res = _result(days_out=2)
    ctx = EarningsProximityEnricher(_settings(), provider=_StubProvider(result=res)).enrich(_pkt("MSFT"))
    row = ctx.to_row("cand1", "pkt1", "scan1")
    for k in ("earnings_id", "candidate_id", "packet_id", "scan_batch_id", "symbol",
              "earnings_date", "earnings_timing", "days_until_earnings", "hold_days_used",
              "earnings_within_hold_window", "earnings_within_warning_window",
              "earnings_data_status", "confidence", "source", "provider",
              "enrichment_status", "enrichment_error", "risk_tags_json", "fetched_at_utc"):
        assert k in row
    assert row["candidate_id"] == "cand1"
    assert row["symbol"] == "MSFT"


def test_summary_fields_subset():
    res = _result(days_out=2)
    ctx = EarningsProximityEnricher(_settings(), provider=_StubProvider(result=res)).enrich(_pkt())
    summary = ctx.summary_fields()
    assert set(summary.keys()) == {
        "earnings_date", "days_until_earnings", "earnings_within_hold_window",
        "earnings_within_warning_window", "earnings_timing", "earnings_data_status",
    }


# ----------------------------------------------------- recompute_with_hold_days
def test_recompute_does_not_refetch_only_reclassifies():
    """The provider is called exactly ONCE (inside enrich()); recompute must
    reclassify using the SAME fetched earnings_date, just against a new hold
    length -- days_until_earnings must be identical across recomputes."""
    res = _result(days_out=5)
    stub = _StubProvider(result=res)
    e = EarningsProximityEnricher(_settings(EARNINGS_PROXIMITY_DEFAULT_HOLD_DAYS="3"), provider=stub)
    ctx = e.enrich(_pkt())
    assert ctx.earnings_within_hold_window == 0        # 5 days out > 3-day default hold

    recomputed = recompute_with_hold_days(ctx, hold_days=10, warning_days=7)
    assert recomputed.days_until_earnings == ctx.days_until_earnings
    assert recomputed.earnings_date == ctx.earnings_date
    assert recomputed.hold_days_used == 10
    assert recomputed.earnings_within_hold_window == 1  # now within the wider 10-day hold


def test_recompute_never_raises_on_bad_context():
    class _Weird:
        earnings_date = object()          # deliberately wrong type
        earnings_data_status = "ok"

    weird = _Weird()
    out = recompute_with_hold_days(weird, hold_days=3, warning_days=7)
    assert out is weird                   # fails safe: returns the same context unmodified


def test_recompute_unavailable_stays_unavailable():
    res = _result(days_out=None)
    ctx = EarningsProximityEnricher(_settings(), provider=_StubProvider(result=res)).enrich(_pkt())
    recomputed = recompute_with_hold_days(ctx, hold_days=30, warning_days=30)
    assert recomputed.earnings_within_hold_window == 0
    assert recomputed.earnings_data_status == EarningsDataStatus.UNAVAILABLE.value
