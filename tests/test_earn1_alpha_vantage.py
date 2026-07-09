"""EARN-1: the live earnings-calendar provider (vendor: Alpha Vantage).
HERMETIC throughout -- no real network calls (the HTTP layer is exercised
only via injected fake fetchers / direct CSV-parsing unit tests; the ONE
real integration test against the live vendor was run manually during this
build, see the build report, not as part of the automated suite). Covers:
* alpha_vantage_client.py's CSV parsing (valid/malformed/empty responses,
  missing-key short-circuit),
* earnings_calendar_service.py's once-daily capture (idempotent per
  (symbol, report_date, fiscal_date_ending); a revision --  same fiscal
  period, new report_date -- is a NEW row; per-row failure isolation),
* earnings_provider.py's AlphaVantageEarningsProvider (stale/fresh cache,
  per-symbol lookup, timing mapping) and make_earnings_provider's wiring
  (journal-required fail-safe, never silently substitutes mock),
* scheduler wiring completeness (cadence + job_runner + jobs.py, the exact
  "is_due but not the hardcoded dispatch tuple" bug class TEXT-0's own
  audit caught),
* settings/config-hash/secrets plumbing,
* additive schema migration.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from alphaos.constants import EarningsDataStatus, EarningsTiming
from alphaos.earnings.alpha_vantage_client import _parse_csv, _parse_float, fetch_earnings_calendar
from alphaos.earnings.earnings_provider import (
    AlphaVantageEarningsProvider,
    MockEarningsProximityProvider,
    make_earnings_provider,
)
from alphaos.journal.journal_store import JournalStore
from alphaos.reports.earnings_calendar_service import update_earnings_calendar
from alphaos.scheduler import cadence
from alphaos.scheduler.job_runner import JobRunner, _JOB_FUNCS
from alphaos.util import timeutils
from conftest import make_settings


# ------------------------------------------------------------- CSV parsing
_VALID_CSV = (
    "symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay\n"
    "ARTW,ART'S-WAY MANUFACTURING,2026-07-09,2026-05-31,,USD,\n"
    "BYRN,BYRNA TECHNOLOGIES,2026-07-09,2026-05-31,-0.1,USD,pre-market\n"
    "AAPL,APPLE INC,2026-08-17,2026-06-30,1.5,USD,post-market\n"
)


def test_parse_csv_valid_response():
    rows = _parse_csv(_VALID_CSV)
    assert rows is not None
    assert len(rows) == 3
    aapl = next(r for r in rows if r["symbol"] == "AAPL")
    assert aapl["report_date"] == "2026-08-17"
    assert aapl["fiscal_date_ending"] == "2026-06-30"
    assert aapl["estimate_eps"] == 1.5
    assert aapl["currency"] == "USD"
    assert aapl["timing"] == "post-market"
    artw = next(r for r in rows if r["symbol"] == "ARTW")
    assert artw["estimate_eps"] is None  # blank estimate -- never fabricated as 0.0
    assert artw["timing"] is None


def test_parse_csv_missing_symbol_header_is_treated_as_error_response():
    """A rate-limit/error response comes back as JSON or a short text
    message, not this CSV shape -- must never be silently parsed as valid
    rows (unknown != safe)."""
    error_body = '{"Information": "Thank you for using Alpha Vantage! Our standard API..."}'
    assert _parse_csv(error_body) is None


def test_parse_csv_empty_result_treated_as_unavailable():
    """A genuinely empty result is not a real state for a rolling
    multi-month market-wide calendar."""
    header_only = "symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay\n"
    assert _parse_csv(header_only) is None


def test_parse_csv_skips_rows_missing_symbol_or_report_date():
    body = (
        "symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay\n"
        "AAPL,APPLE INC,2026-08-17,2026-06-30,1.5,USD,post-market\n"
        ",MISSING SYMBOL,2026-08-17,2026-06-30,1.5,USD,post-market\n"
        "MSFT,MICROSOFT,,2026-06-30,1.5,USD,post-market\n"
    )
    rows = _parse_csv(body)
    assert rows is not None
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAPL"


def test_parse_csv_skips_row_with_non_iso_report_date():
    """A malformed reportDate (not a real vendor case observed so far, but
    never trust external input) must be skipped, never cached as an
    unparseable date -- audit NIT-1."""
    body = (
        "symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay\n"
        "AAPL,APPLE INC,2026-08-17,2026-06-30,1.5,USD,post-market\n"
        "MSFT,MICROSOFT,07/09/2026,2026-06-30,1.5,USD,post-market\n"
        "TSLA,TESLA INC,TBD,2026-06-30,1.5,USD,post-market\n"
    )
    rows = _parse_csv(body)
    assert rows is not None
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAPL"


def test_parse_float_blank_and_invalid_never_raise():
    assert _parse_float(None) is None
    assert _parse_float("") is None
    assert _parse_float("not_a_number") is None
    assert _parse_float("1.23") == 1.23


def test_fetch_earnings_calendar_no_key_returns_none_without_network(monkeypatch):
    """Missing API key short-circuits before any HTTP call is attempted."""
    import urllib.request

    def _explode(*a, **kw):
        raise AssertionError("must not attempt a network call with no API key configured")

    monkeypatch.setattr(urllib.request, "urlopen", _explode)
    s = make_settings()  # no ALPHA_VANTAGE_API_KEY set
    assert fetch_earnings_calendar(s) is None


def test_fetch_earnings_calendar_logs_via_journal_on_missing_key():
    j = JournalStore(":memory:")
    s = make_settings()
    fetch_earnings_calendar(s, j)
    events = j.query("SELECT message FROM system_events WHERE category = 'earnings_calendar'")
    assert any("API key" in e["message"] for e in events)
    j.close()


class _FakeHTTPResponse:
    def __init__(self, body_bytes: bytes):
        self._body = body_bytes

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_fetch_earnings_calendar_url_encodes_special_characters_in_api_key(monkeypatch):
    """audit LOW-1: a key containing characters meaningful in a query string
    (&, =, space) must be percent-encoded, never raw-interpolated -- a raw
    '&' would silently truncate/corrupt the apikey param and could smuggle
    extra query params in."""
    import urllib.request

    captured_urls = []

    def _fake_urlopen(req, timeout=None):
        captured_urls.append(req.full_url)
        return _FakeHTTPResponse(_VALID_CSV.encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    s = make_settings(ALPHA_VANTAGE_API_KEY="ab&cd=ef gh")
    rows = fetch_earnings_calendar(s)
    assert rows is not None
    assert len(captured_urls) == 1
    assert "ab&cd=ef gh" not in captured_urls[0]  # never raw
    assert "apikey=ab%26cd%3Def" in captured_urls[0] or "apikey=ab%26cd%3Def+gh" in captured_urls[0]


def test_fetch_earnings_calendar_decodes_utf8_bom(monkeypatch):
    """audit NIT-2: a leading UTF-8 BOM must not corrupt the 'symbol'
    fieldname and cause a valid response to be misread as the unexpected-
    shape error path."""
    import urllib.request

    def _fake_urlopen(req, timeout=None):
        # "utf-8-sig" encoding prepends the actual BOM bytes (EF BB BF) --
        # don't also embed a literal "﻿" in the source string, or the
        # fixture ends up double-BOM-prefixed.
        return _FakeHTTPResponse(_VALID_CSV.encode("utf-8-sig"))

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    s = make_settings(ALPHA_VANTAGE_API_KEY="test-key")
    rows = fetch_earnings_calendar(s)
    assert rows is not None
    assert len(rows) == 3
    assert any(r["symbol"] == "AAPL" for r in rows)


# ---------------------------------------------------------- capture service
def _fetch_ok(rows):
    def _fn(settings, journal=None):
        return rows
    return _fn


def _fetch_fail(settings, journal=None):
    return None


def test_update_earnings_calendar_writes_new_rows():
    j = JournalStore(":memory:")
    rows = [
        {"symbol": "AAPL", "company_name": "Apple", "report_date": "2026-08-17",
         "fiscal_date_ending": "2026-06-30", "estimate_eps": 1.5, "currency": "USD", "timing": "post-market"},
        {"symbol": "ARTW", "company_name": "Art's-Way", "report_date": "2026-07-09",
         "fiscal_date_ending": "2026-05-31", "estimate_eps": None, "currency": "USD", "timing": None},
    ]
    result = update_earnings_calendar(j, make_settings(), fetch_fn=_fetch_ok(rows))
    assert result["n_fetched"] == 2
    assert result["n_written"] == 2
    assert result["warnings"] == []
    assert j.count_rows("earnings_calendar_cache") == 2
    j.close()


def test_update_earnings_calendar_idempotent_on_rerun():
    j = JournalStore(":memory:")
    rows = [
        {"symbol": "AAPL", "company_name": "Apple", "report_date": "2026-08-17",
         "fiscal_date_ending": "2026-06-30", "estimate_eps": 1.5, "currency": "USD", "timing": "post-market"},
    ]
    update_earnings_calendar(j, make_settings(), fetch_fn=_fetch_ok(rows))
    result2 = update_earnings_calendar(j, make_settings(), fetch_fn=_fetch_ok(rows))
    assert result2["n_fetched"] == 1
    assert result2["n_written"] == 0  # already known -- not a duplicate insert
    assert j.count_rows("earnings_calendar_cache") == 1
    j.close()


def test_update_earnings_calendar_revised_report_date_adds_a_new_row():
    """The SAME fiscal period, a DIFFERENT report_date (a revision) must be
    tracked as a NEW row -- point-in-time history, never overwritten."""
    j = JournalStore(":memory:")
    original = [{"symbol": "AAPL", "company_name": "Apple", "report_date": "2026-08-17",
                "fiscal_date_ending": "2026-06-30", "estimate_eps": 1.5, "currency": "USD",
                "timing": "post-market"}]
    revised = [{"symbol": "AAPL", "company_name": "Apple", "report_date": "2026-08-20",
               "fiscal_date_ending": "2026-06-30", "estimate_eps": 1.5, "currency": "USD",
               "timing": "post-market"}]
    update_earnings_calendar(j, make_settings(), fetch_fn=_fetch_ok(original))
    result = update_earnings_calendar(j, make_settings(), fetch_fn=_fetch_ok(revised))
    assert result["n_written"] == 1
    assert j.count_rows("earnings_calendar_cache") == 2
    dates = {r["report_date"] for r in j.query("SELECT report_date FROM earnings_calendar_cache")}
    assert dates == {"2026-08-17", "2026-08-20"}
    j.close()


def test_update_earnings_calendar_fetch_none_never_raises_and_logs():
    j = JournalStore(":memory:")
    result = update_earnings_calendar(j, make_settings(), fetch_fn=_fetch_fail)
    assert result["n_fetched"] == 0
    assert result["n_written"] == 0
    events = j.query("SELECT message FROM system_events WHERE category = 'earnings_calendar'")
    assert any("no usable data" in e["message"] for e in events)
    j.close()


def test_update_earnings_calendar_one_bad_row_does_not_abort_the_batch():
    """A row missing a required NOT NULL column (simulated via a malformed
    dict) must fail in isolation -- the other rows still get written (this
    codebase's own per-item isolation law)."""
    j = JournalStore(":memory:")
    rows = [
        {"symbol": "AAPL", "company_name": "Apple", "report_date": "2026-08-17",
         "fiscal_date_ending": "2026-06-30", "estimate_eps": 1.5, "currency": "USD", "timing": "post-market"},
        {"symbol": None, "company_name": "Broken", "report_date": "2026-08-18",  # violates NOT NULL
         "fiscal_date_ending": "2026-06-30", "estimate_eps": None, "currency": "USD", "timing": None},
        {"symbol": "MSFT", "company_name": "Microsoft", "report_date": "2026-07-29",
         "fiscal_date_ending": "2026-06-30", "estimate_eps": 3.2, "currency": "USD", "timing": "post-market"},
    ]
    result = update_earnings_calendar(j, make_settings(), fetch_fn=_fetch_ok(rows))
    assert result["n_fetched"] == 3
    assert result["n_written"] == 2  # AAPL + MSFT, the broken row isolated
    assert len(result["warnings"]) == 1
    assert j.count_rows("earnings_calendar_cache") == 2
    j.close()


# ------------------------------------------------------------- live provider
def _seed_entry(journal, symbol, report_date, timing=None, created_at_utc=None):
    fields = {
        "entry_id": f"e_{symbol}_{report_date}", "symbol": symbol, "company_name": symbol,
        "report_date": report_date, "fiscal_date_ending": "2026-06-30", "estimate_eps": 1.0,
        "currency": "USD", "timing": timing, "source": "alpha_vantage",
    }
    if created_at_utc is not None:
        fields["created_at_utc"] = created_at_utc
        fields["created_at_sgt"] = created_at_utc
    journal.insert("earnings_calendar_cache", fields)


def test_provider_never_captured_is_stale():
    j = JournalStore(":memory:")
    p = AlphaVantageEarningsProvider(j, staleness_days=3)
    r = p.get_earnings_for_symbol("AAPL")
    assert r.status == EarningsDataStatus.STALE.value
    assert r.earnings_date is None
    j.close()


def test_provider_fresh_cache_returns_ok_with_soonest_upcoming_date():
    j = JournalStore(":memory:")
    today = timeutils.market_date()
    _seed_entry(j, "AAPL", (today + timedelta(days=10)).isoformat(), timing="post-market")
    _seed_entry(j, "AAPL", (today + timedelta(days=3)).isoformat(), timing="pre-market")  # soonest
    p = AlphaVantageEarningsProvider(j, staleness_days=3)
    r = p.get_earnings_for_symbol("AAPL")
    assert r.status == EarningsDataStatus.OK.value
    assert r.earnings_date == (today + timedelta(days=3)).isoformat()
    assert r.earnings_timing == EarningsTiming.BEFORE_OPEN.value
    assert r.confidence == AlphaVantageEarningsProvider._CONFIDENCE_OK
    assert r.source == "alpha_vantage"
    j.close()


def test_provider_ignores_past_report_dates():
    j = JournalStore(":memory:")
    today = timeutils.market_date()
    _seed_entry(j, "AAPL", (today - timedelta(days=5)).isoformat())  # already reported
    p = AlphaVantageEarningsProvider(j, staleness_days=3)
    r = p.get_earnings_for_symbol("AAPL")
    assert r.status == EarningsDataStatus.UNAVAILABLE.value
    j.close()


def test_provider_old_capture_is_stale_even_with_upcoming_date_cached():
    j = JournalStore(":memory:")
    today = timeutils.market_date()
    old_ts = timeutils.to_iso(timeutils.now_utc() - timedelta(days=10))
    _seed_entry(j, "AAPL", (today + timedelta(days=10)).isoformat(), created_at_utc=old_ts)
    p = AlphaVantageEarningsProvider(j, staleness_days=3)
    r = p.get_earnings_for_symbol("AAPL")
    assert r.status == EarningsDataStatus.STALE.value
    j.close()


def test_provider_unknown_symbol_is_unavailable_not_stale():
    j = JournalStore(":memory:")
    today = timeutils.market_date()
    _seed_entry(j, "AAPL", (today + timedelta(days=5)).isoformat())  # fresh cache exists
    p = AlphaVantageEarningsProvider(j, staleness_days=3)
    r = p.get_earnings_for_symbol("ZZZZ")  # never captured for this symbol
    assert r.status == EarningsDataStatus.UNAVAILABLE.value
    j.close()


@pytest.mark.parametrize("raw_timing,expected", [
    ("pre-market", EarningsTiming.BEFORE_OPEN.value),
    ("post-market", EarningsTiming.AFTER_CLOSE.value),
    ("PRE-MARKET", EarningsTiming.BEFORE_OPEN.value),  # case-insensitive
    (None, EarningsTiming.UNKNOWN.value),
    ("", EarningsTiming.UNKNOWN.value),
    ("some_unexpected_value", EarningsTiming.UNKNOWN.value),  # never guessed
])
def test_provider_timing_mapping(raw_timing, expected):
    j = JournalStore(":memory:")
    today = timeutils.market_date()
    _seed_entry(j, "AAPL", (today + timedelta(days=5)).isoformat(), timing=raw_timing)
    p = AlphaVantageEarningsProvider(j, staleness_days=3)
    r = p.get_earnings_for_symbol("AAPL")
    assert r.earnings_timing == expected
    j.close()


def test_provider_never_raises_on_missing_created_at():
    """A row with an unparseable/missing created_at_utc must fail toward
    STALE, never crash or silently trust it."""
    j = JournalStore(":memory:")
    j.conn.execute(
        "INSERT INTO earnings_calendar_cache "
        "(entry_id, symbol, report_date, source, created_at_utc, created_at_sgt) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("e1", "AAPL", "2099-01-01", "alpha_vantage", "not-a-real-timestamp", "not-a-real-timestamp"),
    )
    j.conn.commit()
    p = AlphaVantageEarningsProvider(j, staleness_days=3)
    r = p.get_earnings_for_symbol("AAPL")  # must not raise
    assert r.status == EarningsDataStatus.STALE.value
    j.close()


# -------------------------------------------------------- factory wiring
def test_factory_alpha_vantage_without_journal_fails_safe_to_none_never_mock():
    """mock != real -- a caller that asked for the live provider but forgot
    to pass a journal must get an honest 'disabled', never fabricated mock
    data masquerading as real."""
    s = make_settings(EARNINGS_PROXIMITY_ENABLED="true", EARNINGS_PROXIMITY_PROVIDER="alpha_vantage")
    assert make_earnings_provider(s, journal=None) is None


def test_factory_alpha_vantage_with_journal_returns_live_provider():
    j = JournalStore(":memory:")
    s = make_settings(EARNINGS_PROXIMITY_ENABLED="true", EARNINGS_PROXIMITY_PROVIDER="alpha_vantage")
    provider = make_earnings_provider(s, journal=j)
    assert isinstance(provider, AlphaVantageEarningsProvider)
    assert provider.name == "alpha_vantage"
    j.close()


def test_factory_still_defaults_to_mock_backward_compatible():
    """Existing callers that never pass journal (e.g. any pre-EARN-1 script)
    keep working exactly as before for the mock/static/disabled paths."""
    s = make_settings(EARNINGS_PROXIMITY_ENABLED="true", EARNINGS_PROXIMITY_PROVIDER="mock")
    assert isinstance(make_earnings_provider(s), MockEarningsProximityProvider)


def test_enricher_end_to_end_with_live_provider():
    """The one real call site (EarningsProximityEnricher) now threads
    journal into make_earnings_provider -- confirm the whole enrichment
    path works with the live provider, zero enricher-level code changes
    needed beyond the one-line factory call fix."""
    from alphaos.earnings.earnings_enricher import EarningsProximityEnricher

    class _Packet:
        symbol = "AAPL"

    j = JournalStore(":memory:")
    today = timeutils.market_date()
    _seed_entry(j, "AAPL", (today + timedelta(days=2)).isoformat(), timing="pre-market")
    s = make_settings(EARNINGS_PROXIMITY_ENABLED="true", EARNINGS_PROXIMITY_PROVIDER="alpha_vantage")
    enricher = EarningsProximityEnricher(s, j)
    ctx = enricher.enrich(_Packet())
    assert ctx.enrichment_status == "ok"
    assert ctx.earnings_data_status == EarningsDataStatus.OK.value
    assert ctx.provider == "alpha_vantage"
    assert ctx.earnings_within_hold_window == 1  # within the default 3-day hold
    j.close()


# ------------------------------------------------------------ scheduler wiring
def test_earnings_calendar_pull_in_default_lock_key_once_daily_group():
    s = make_settings()
    key1 = cadence.default_lock_key(cadence.JobType.EARNINGS_CALENDAR_PULL, s)
    key2 = cadence.default_lock_key(cadence.JobType.EARNINGS_CALENDAR_PULL, s)
    assert key1 == key2  # deterministic, date-keyed -- not a per-instant key
    assert key1.startswith("earnings_calendar_pull:")


def test_earnings_calendar_pull_is_due_dispatch_wired():
    """The exact 'is_due wired but not the hardcoded dispatch tuple' bug
    class TEXT-0's own audit caught -- verify is_due() actually dispatches
    to a real due-check, not the unknown-job_type fallback."""
    j = JournalStore(":memory:")
    s = make_settings(SCHEDULER_EARNINGS_CALENDAR_PULL_TIME="00:00")
    due, reason = cadence.is_due(cadence.JobType.EARNINGS_CALENDAR_PULL, s, j)
    assert "unknown job_type" not in reason
    j.close()


def test_earnings_calendar_pull_in_job_funcs_dispatch_table():
    assert cadence.JobType.EARNINGS_CALENDAR_PULL in _JOB_FUNCS


def test_earnings_calendar_pull_in_run_due_jobs_and_status_report():
    """Structural completeness: both loops in job_runner.py must include
    the new job type (source-level check, not just behavioral)."""
    import inspect

    source = inspect.getsource(JobRunner)
    run_due_jobs_src = source.split("def run_due_jobs")[1].split("def _handle_fuse")[0]
    status_report_src = source.split("def status_report")[1].split("def heartbeat_check")[0]
    assert "EARNINGS_CALENDAR_PULL" in run_due_jobs_src
    assert "EARNINGS_CALENDAR_PULL" in status_report_src


def test_run_due_jobs_includes_earnings_calendar_pull_end_to_end():
    from alphaos.orchestrator import Orchestrator

    o = Orchestrator(settings=make_settings(), journal=JournalStore(":memory:"))
    runner = JobRunner(o)
    results = runner.run_due_jobs()
    job_types = {r["job_type"] for r in results}
    assert cadence.JobType.EARNINGS_CALENDAR_PULL in job_types
    o.close()


# --------------------------------------------------------- settings/config-hash
def test_alpha_vantage_api_key_excluded_from_config_hash():
    from alphaos.lineage.config_snapshot import build_config_hashes

    with_key = build_config_hashes(make_settings(ALPHA_VANTAGE_API_KEY="secret-value-1"))
    without_key = build_config_hashes(make_settings(ALPHA_VANTAGE_API_KEY=""))
    assert with_key["config_hash"] == without_key["config_hash"]
    assert with_key["earnings_config_hash"] == without_key["earnings_config_hash"]


def test_earnings_config_hash_changes_with_scheduler_time_but_not_others():
    from alphaos.lineage.config_snapshot import build_config_hashes

    a = build_config_hashes(make_settings(SCHEDULER_EARNINGS_CALENDAR_PULL_TIME="06:45"))
    b = build_config_hashes(make_settings(SCHEDULER_EARNINGS_CALENDAR_PULL_TIME="09:00"))
    assert a["earnings_config_hash"] != b["earnings_config_hash"]
    assert a["risk_config_hash"] == b["risk_config_hash"]


def test_earnings_calendar_staleness_days_bounds_validation():
    from alphaos.config.settings import SettingsError

    with pytest.raises(SettingsError):
        make_settings(EARNINGS_CALENDAR_STALENESS_DAYS="0")
    with pytest.raises(SettingsError):
        make_settings(EARNINGS_CALENDAR_STALENESS_DAYS="31")


def test_has_alpha_vantage_key_property():
    assert make_settings(ALPHA_VANTAGE_API_KEY="").has_alpha_vantage_key is False
    assert make_settings(ALPHA_VANTAGE_API_KEY="a-real-looking-key").has_alpha_vantage_key is True


# ------------------------------------------------------------------ schema
def test_old_db_gets_earnings_calendar_cache_table_added_additively(tmp_path):
    db_path = tmp_path / "pre_earn1.db"
    j1 = JournalStore(str(db_path))
    j1.conn.execute("DROP TABLE IF EXISTS earnings_calendar_cache")
    j1.conn.execute("DROP INDEX IF EXISTS idx_earnings_calendar_cache_symbol_date_fiscal")
    j1.conn.commit()
    j1.close()

    j2 = JournalStore(str(db_path))  # re-opening must additively recreate it
    cols = j2._cols("earnings_calendar_cache")
    for expected in ("entry_id", "symbol", "company_name", "report_date", "fiscal_date_ending",
                    "estimate_eps", "currency", "timing", "source"):
        assert expected in cols, f"missing column {expected}"
    idx = {r["name"] for r in j2.query(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='earnings_calendar_cache'")}
    assert "idx_earnings_calendar_cache_symbol_date_fiscal" in idx
    j2.close()
