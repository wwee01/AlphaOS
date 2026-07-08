"""TEXT-0: point-in-time SEC EDGAR text archive (§H.1 direct construction
throughout -- no live network, no wall-clock dependence). Covers:
* form catalog v1 (exact + prefix matching),
* RateLimiter's interval ceiling (injected fake clock, never a real sleep),
* make_edgar_provider's mock/offline/no-contact-email refusal,
* _universe_tickers (core book minus index ETFs, unioned with EXP-0's shadow
  universe file when present),
* refresh_cik_map (map/refresh/never-delete "once archived-for, always
  archived-for", empty-provider-response handling),
* pull_new_filings (catalog-form fetch + store, non-catalog skip tally,
  idempotent re-fetch, seen_at vs. published_at provenance, missing-document
  and submissions-outage error counting, the gzip round-trip verify-before-
  trust safety check),
* is_probable_trading_day (pure weekday proxy),
* run_text_archive_pull_job (disabled-by-default skip, zero-doc-on-a-
  trading-day alert, weekend silence, no double alert on a real error).

All offline, in-memory or tmp_path-backed. No real money, no network, no
writes under the real repo's data/text_archive/.
"""

from __future__ import annotations

import hashlib as real_hashlib
import json
import os
from datetime import date

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.text_archive.forms import EDGAR_FORMS_V1, is_catalog_form
from alphaos.text_archive.sec_edgar import (
    DEFAULT_MAX_REQUESTS_PER_SECOND,
    RateLimiter,
    SecEdgarProvider,
    make_edgar_provider,
)
from alphaos.text_archive.service import (
    _storage_path,
    _universe_tickers,
    is_probable_trading_day,
    pull_new_filings,
    refresh_cik_map,
)
from conftest import make_settings


# ------------------------------------------------------------------ helpers
class _FakeEdgarProvider:
    """Stands in for SecEdgarProvider -- same three-method surface, canned
    responses, and a call log so idempotency/no-refetch can be asserted."""

    def __init__(self, ticker_to_cik=None, submissions_by_cik=None, documents=None):
        self.ticker_to_cik = ticker_to_cik or {}
        self.submissions_by_cik = submissions_by_cik or {}
        self.documents = documents or {}
        self.get_document_calls = []
        self.get_submissions_calls = []

    def get_company_tickers(self):
        return dict(self.ticker_to_cik)

    def get_submissions(self, cik):
        self.get_submissions_calls.append(cik)
        return self.submissions_by_cik.get(cik)

    def get_document(self, cik, accession_no_dashes, primary_document):
        self.get_document_calls.append((cik, accession_no_dashes, primary_document))
        return self.documents.get((cik, accession_no_dashes, primary_document))


def _submissions_payload(entries):
    """entries: list of (form, accession, filing_date, primary_doc) tuples,
    matching SEC's own parallel-array submissions JSON shape."""
    return {
        "filings": {
            "recent": {
                "form": [e[0] for e in entries],
                "accessionNumber": [e[1] for e in entries],
                "filingDate": [e[2] for e in entries],
                "primaryDocument": [e[3] for e in entries],
            }
        }
    }


@pytest.fixture
def journal():
    store = JournalStore(":memory:")
    yield store
    store.close()


# ================================================================== forms
def test_exact_match_forms_recognized():
    assert is_catalog_form("8-K")
    assert is_catalog_form("10-K")
    assert is_catalog_form("SC 13D/A")
    assert is_catalog_form("DEF 14A")


def test_prefix_match_forms_recognized():
    assert is_catalog_form("424B4")  # 424B family, wildcarded
    assert is_catalog_form("SC TO-T")
    assert is_catalog_form("15-12B")
    assert is_catalog_form("25-NSE")


def test_unknown_forms_rejected():
    assert not is_catalog_form("UNKNOWN-FORM-XYZ")
    assert not is_catalog_form("EFFECT")


def test_none_and_empty_form_do_not_crash():
    assert not is_catalog_form(None)
    assert not is_catalog_form("")


# ============================================================ rate limiter
def test_rate_limiter_sleeps_the_remaining_interval_when_called_too_fast():
    clock = {"t": 0.0}
    sleeps = []

    def fake_time():
        return clock["t"]

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    limiter = RateLimiter(max_per_second=4.0, sleep_fn=fake_sleep, time_fn=fake_time)
    limiter.wait()  # first call -- no prior request, never sleeps
    clock["t"] += 0.05  # only 50ms elapsed, ceiling wants 250ms (1/4 req/s)
    limiter.wait()

    assert sleeps == [0.20]  # 0.25 - 0.05, to within float repr


def test_rate_limiter_does_not_sleep_when_calls_are_already_spaced_out():
    clock = {"t": 0.0}
    sleeps = []

    limiter = RateLimiter(
        max_per_second=4.0, sleep_fn=lambda s: sleeps.append(s), time_fn=lambda: clock["t"]
    )
    limiter.wait()
    clock["t"] += 1.0  # a full second later -- well past the 250ms floor
    limiter.wait()

    assert sleeps == []


def test_default_rate_limit_is_conservative_below_secs_own_ceiling():
    assert DEFAULT_MAX_REQUESTS_PER_SECOND <= 10.0


# ======================================================= make_edgar_provider
def test_make_edgar_provider_returns_none_in_mock_mode():
    settings = make_settings(SEC_EDGAR_CONTACT_EMAIL="ops@example.com")
    assert make_edgar_provider(settings) is None


def test_make_edgar_provider_returns_none_without_a_contact_email(journal):
    settings = make_settings(ALPHAOS_MODE="paper", SEC_EDGAR_CONTACT_EMAIL="")
    assert make_edgar_provider(settings, journal) is None
    warning = journal.one(
        "SELECT * FROM system_events WHERE category = 'text_archive' AND severity = 'warning'"
    )
    assert warning is not None
    assert "SEC_EDGAR_CONTACT_EMAIL" in warning["message"]


def test_make_edgar_provider_returns_a_real_provider_when_configured():
    settings = make_settings(ALPHAOS_MODE="paper", SEC_EDGAR_CONTACT_EMAIL="ops@example.com")
    provider = make_edgar_provider(settings)
    assert isinstance(provider, SecEdgarProvider)
    assert "ops@example.com" in provider._user_agent


# ============================================================= universe set
def test_universe_tickers_is_core_book_minus_index_etfs_with_no_shadow_file(tmp_path):
    settings = make_settings(SHADOW_TIER_UNIVERSE_FILE=str(tmp_path / "nope.json"))
    tickers = _universe_tickers(settings)
    assert "SPY" not in tickers and "QQQ" not in tickers and "IWM" not in tickers and "DIA" not in tickers
    assert "AAPL" in tickers and "MSFT" in tickers


def test_universe_tickers_unions_in_the_shadow_universe_file(tmp_path):
    path = tmp_path / "shadow_universe.json"
    path.write_text(json.dumps({
        "version": 1, "as_of_date": "2026-07-01", "sha256": "test", "screen_params": {},
        "symbols": [{"symbol": "ZZZZ1"}, {"symbol": "ZZZZ2"}],
    }))
    settings = make_settings(SHADOW_TIER_UNIVERSE_FILE=str(path))

    tickers = _universe_tickers(settings)

    assert "ZZZZ1" in tickers and "ZZZZ2" in tickers
    assert "AAPL" in tickers  # core book still present alongside the union


# ============================================================= refresh_cik_map
def test_refresh_cik_map_maps_and_inserts_new_tickers(tmp_path, journal):
    settings = make_settings(SHADOW_TIER_UNIVERSE_FILE=str(tmp_path / "nope.json"))
    provider = _FakeEdgarProvider(ticker_to_cik={"AAPL": "320193", "MSFT": "789019"})

    result = refresh_cik_map(journal, settings, edgar_provider=provider)

    assert result["mapped"] >= 2
    assert "error" not in result
    row = journal.one("SELECT * FROM cik_map WHERE ticker = 'AAPL'")
    assert row is not None and row["cik"] == "320193"


def test_refresh_cik_map_counts_unmapped_tickers(tmp_path, journal):
    settings = make_settings(SHADOW_TIER_UNIVERSE_FILE=str(tmp_path / "nope.json"))
    # Non-empty ticker map (so the "provider health" early-return below does
    # NOT fire -- see test_refresh_cik_map_empty_ticker_response_warns_and_
    # does_not_crash for that separate case) that simply has nothing for OUR
    # universe -- a genuine per-ticker miss.
    provider = _FakeEdgarProvider(ticker_to_cik={"UNRELATED_TICKER": "000001"})

    result = refresh_cik_map(journal, settings, edgar_provider=provider)

    assert result["mapped"] == 0
    assert result["tickers_considered"] > 0
    assert result["unmapped"] == result["tickers_considered"]


def test_refresh_cik_map_refreshes_existing_rows_not_duplicates(tmp_path, journal):
    settings = make_settings(SHADOW_TIER_UNIVERSE_FILE=str(tmp_path / "nope.json"))
    provider = _FakeEdgarProvider(ticker_to_cik={"AAPL": "320193"})
    refresh_cik_map(journal, settings, edgar_provider=provider)

    provider2 = _FakeEdgarProvider(ticker_to_cik={"AAPL": "999999"})  # CIK "changed"
    result2 = refresh_cik_map(journal, settings, edgar_provider=provider2)

    assert result2["refreshed"] >= 1
    rows = journal.query("SELECT * FROM cik_map WHERE ticker = 'AAPL'")
    assert len(rows) == 1  # updated in place, never duplicated
    assert rows[0]["cik"] == "999999"


def test_refresh_cik_map_never_deletes_a_ticker_that_drops_out_of_universe(tmp_path, journal):
    """'Once archived-for, always archived-for' -- a ticker's cik_map row
    survives even after it's no longer in the live universe."""
    wide_path = tmp_path / "wide.json"
    wide_path.write_text(json.dumps({
        "version": 1, "as_of_date": "2026-07-01", "sha256": "t", "screen_params": {},
        "symbols": [{"symbol": "TEMPCO"}],
    }))
    settings_wide = make_settings(SHADOW_TIER_UNIVERSE_FILE=str(wide_path))
    provider = _FakeEdgarProvider(ticker_to_cik={"TEMPCO": "111111"})
    refresh_cik_map(journal, settings_wide, edgar_provider=provider)
    assert journal.one("SELECT * FROM cik_map WHERE ticker = 'TEMPCO'") is not None

    narrow_path = tmp_path / "narrow.json"
    narrow_path.write_text(json.dumps({
        "version": 1, "as_of_date": "2026-07-02", "sha256": "t", "screen_params": {}, "symbols": [],
    }))
    settings_narrow = make_settings(SHADOW_TIER_UNIVERSE_FILE=str(narrow_path))
    refresh_cik_map(journal, settings_narrow, edgar_provider=_FakeEdgarProvider(ticker_to_cik={}))

    assert journal.one("SELECT * FROM cik_map WHERE ticker = 'TEMPCO'") is not None


def test_refresh_cik_map_with_none_provider_is_a_safe_noop(tmp_path, journal):
    settings = make_settings(SHADOW_TIER_UNIVERSE_FILE=str(tmp_path / "nope.json"))
    result = refresh_cik_map(journal, settings, edgar_provider=None)
    assert "error" not in result
    assert result["mapped"] == 0


def test_refresh_cik_map_empty_ticker_response_warns_and_does_not_crash(tmp_path, journal):
    settings = make_settings(SHADOW_TIER_UNIVERSE_FILE=str(tmp_path / "nope.json"))
    provider = _FakeEdgarProvider(ticker_to_cik=None)
    provider.ticker_to_cik = {}  # get_company_tickers() -> {} (falsy)

    result = refresh_cik_map(journal, settings, edgar_provider=provider)

    assert "error" not in result
    warning = journal.one(
        "SELECT * FROM system_events WHERE category = 'text_archive' AND severity = 'warning'"
    )
    assert warning is not None


# ============================================================ pull_new_filings
def test_pull_new_filings_fetches_catalog_forms_and_skips_others(tmp_path, journal):
    settings = make_settings()
    journal.insert("cik_map", {
        "ticker": "AAPL", "cik": "320193",
        "first_seen_at_utc": "2026-01-01T00:00:00+00:00", "last_confirmed_at_utc": "2026-01-01T00:00:00+00:00",
    })
    provider = _FakeEdgarProvider(
        submissions_by_cik={"320193": _submissions_payload([
            ("8-K", "0000320193-26-000001", "2026-07-01", "primary1.htm"),
            ("NOT-A-REAL-FORM", "0000320193-26-000002", "2026-07-02", "primary2.htm"),
        ])},
        documents={("320193", "000032019326000001", "primary1.htm"): b"filing body one"},
    )

    result = pull_new_filings(journal, settings, edgar_provider=provider, storage_root=str(tmp_path))

    assert "error" not in result
    assert result["docs_fetched"] == 1
    assert result["skipped_forms"] == {"NOT-A-REAL-FORM": 1}
    row = journal.one("SELECT * FROM text_documents WHERE accession_no = '0000320193-26-000001'")
    assert row is not None
    assert row["form_type"] == "8-K"
    assert row["edgar_forms_version"] == EDGAR_FORMS_V1
    assert row["source"] == "edgar"
    assert row["published_at"] == "2026-07-01"
    assert row["seen_at"] is not None and row["seen_at"] != row["published_at"]
    assert row["sha256"] == real_hashlib.sha256(b"filing body one").hexdigest()


def test_pull_new_filings_is_idempotent_and_never_refetches_an_archived_accession(tmp_path, journal):
    settings = make_settings()
    journal.insert("cik_map", {
        "ticker": "AAPL", "cik": "320193",
        "first_seen_at_utc": "2026-01-01T00:00:00+00:00", "last_confirmed_at_utc": "2026-01-01T00:00:00+00:00",
    })
    provider = _FakeEdgarProvider(
        submissions_by_cik={"320193": _submissions_payload([
            ("8-K", "0000320193-26-000003", "2026-07-01", "primary.htm"),
        ])},
        documents={("320193", "000032019326000003", "primary.htm"): b"body"},
    )

    r1 = pull_new_filings(journal, settings, edgar_provider=provider, storage_root=str(tmp_path))
    r2 = pull_new_filings(journal, settings, edgar_provider=provider, storage_root=str(tmp_path))

    assert r1["docs_fetched"] == 1
    assert r2["docs_fetched"] == 0
    assert r2["docs_already_archived"] == 1
    assert len(provider.get_document_calls) == 1  # never re-fetched over the network


def test_pull_new_filings_counts_a_missing_primary_document_as_an_error(tmp_path, journal):
    settings = make_settings()
    journal.insert("cik_map", {
        "ticker": "AAPL", "cik": "320193",
        "first_seen_at_utc": "2026-01-01T00:00:00+00:00", "last_confirmed_at_utc": "2026-01-01T00:00:00+00:00",
    })
    provider = _FakeEdgarProvider(
        submissions_by_cik={"320193": _submissions_payload([
            ("8-K", "0000320193-26-000004", "2026-07-01", None),
        ])},
    )

    result = pull_new_filings(journal, settings, edgar_provider=provider, storage_root=str(tmp_path))

    assert result["fetch_errors"] == 1
    assert result["docs_fetched"] == 0


def test_pull_new_filings_rejects_a_malformed_accession_number(tmp_path, journal):
    """Regression guard (scope/safety audit finding): a path-traversal-shaped
    accessionNumber must never reach the filesystem -- rejected as a fetch
    error, and nothing is written anywhere, including outside storage_root."""
    settings = make_settings()
    journal.insert("cik_map", {
        "ticker": "AAPL", "cik": "320193",
        "first_seen_at_utc": "2026-01-01T00:00:00+00:00", "last_confirmed_at_utc": "2026-01-01T00:00:00+00:00",
    })
    evil_accession = "../../../../../../../../tmp/text_archive_escape/evil-accession"
    provider = _FakeEdgarProvider(
        submissions_by_cik={"320193": _submissions_payload([
            ("8-K", evil_accession, "2026-07-01", "primary.htm"),
        ])},
        documents={("320193", evil_accession.replace("-", ""), "primary.htm"): b"malicious content"},
    )

    result = pull_new_filings(journal, settings, edgar_provider=provider, storage_root=str(tmp_path))

    assert result["fetch_errors"] == 1
    assert result["docs_fetched"] == 0
    assert journal.one("SELECT * FROM text_documents WHERE accession_no = ?", (evil_accession,)) is None
    assert list(tmp_path.rglob("*.gz")) == []
    assert not os.path.exists("/tmp/text_archive_escape")


def test_pull_new_filings_logs_a_warning_when_forms_array_is_longer_than_accessions(tmp_path, journal):
    """Regression guard: a malformed submissions payload where some array is
    longer than accessionNumber must be VISIBLE (a system_event), never a
    silent drop -- 'visible, never silent' is this module's own law."""
    settings = make_settings()
    journal.insert("cik_map", {
        "ticker": "AAPL", "cik": "320193",
        "first_seen_at_utc": "2026-01-01T00:00:00+00:00", "last_confirmed_at_utc": "2026-01-01T00:00:00+00:00",
    })
    payload = _submissions_payload([("8-K", "0000320193-26-000010", "2026-07-01", "primary.htm")])
    payload["filings"]["recent"]["form"].append("10-K")  # forms longer than accessionNumber

    provider = _FakeEdgarProvider(
        submissions_by_cik={"320193": payload},
        documents={("320193", "000032019326000010", "primary.htm"): b"body"},
    )

    pull_new_filings(journal, settings, edgar_provider=provider, storage_root=str(tmp_path))

    warning = journal.one(
        "SELECT * FROM system_events WHERE category = 'text_archive' AND severity = 'warning' "
        "AND message LIKE '%dropped%'"
    )
    assert warning is not None


def test_pull_new_filings_isolates_a_write_failure_to_one_accession_and_continues(tmp_path, journal, monkeypatch):
    """Regression guard (correctness audit MEDIUM finding): a gzip write or
    read-back that RAISES (not just a hash mismatch) for one accession must
    count as one fetch_error and let the run continue to the next accession
    -- never abort the whole run or leave an orphaned file behind."""
    from alphaos.text_archive import service as service_mod

    settings = make_settings()
    journal.insert("cik_map", {
        "ticker": "AAPL", "cik": "320193",
        "first_seen_at_utc": "2026-01-01T00:00:00+00:00", "last_confirmed_at_utc": "2026-01-01T00:00:00+00:00",
    })
    provider = _FakeEdgarProvider(
        submissions_by_cik={"320193": _submissions_payload([
            ("8-K", "0000320193-26-000011", "2026-07-01", "primary1.htm"),
            ("8-K", "0000320193-26-000012", "2026-07-02", "primary2.htm"),
        ])},
        documents={
            ("320193", "000032019326000011", "primary1.htm"): b"first body",
            ("320193", "000032019326000012", "primary2.htm"): b"second body",
        },
    )

    real_gzip_open = service_mod.gzip.open
    call_count = {"n": 0}

    def _flaky_gzip_open(path, mode="rb", *a, **k):
        call_count["n"] += 1
        if call_count["n"] == 1:  # the FIRST accession's write -- fail it
            raise OSError("simulated disk error mid-write")
        return real_gzip_open(path, mode, *a, **k)

    monkeypatch.setattr(service_mod.gzip, "open", _flaky_gzip_open)

    result = pull_new_filings(journal, settings, edgar_provider=provider, storage_root=str(tmp_path))

    assert result["fetch_errors"] == 1
    assert result["docs_fetched"] == 1  # the second accession still succeeded
    assert journal.one("SELECT * FROM text_documents WHERE accession_no = '0000320193-26-000011'") is None
    assert journal.one("SELECT * FROM text_documents WHERE accession_no = '0000320193-26-000012'") is not None


def test_pull_new_filings_counts_a_submissions_outage_as_an_error_and_continues(tmp_path, journal):
    settings = make_settings()
    journal.insert("cik_map", {
        "ticker": "DEAD", "cik": "111111",
        "first_seen_at_utc": "2026-01-01T00:00:00+00:00", "last_confirmed_at_utc": "2026-01-01T00:00:00+00:00",
    })
    journal.insert("cik_map", {
        "ticker": "AAPL", "cik": "320193",
        "first_seen_at_utc": "2026-01-01T00:00:00+00:00", "last_confirmed_at_utc": "2026-01-01T00:00:00+00:00",
    })
    provider = _FakeEdgarProvider(
        submissions_by_cik={"320193": _submissions_payload([
            ("8-K", "0000320193-26-000005", "2026-07-01", "primary.htm"),
        ])},  # note: no entry for cik 111111 -> get_submissions returns None
        documents={("320193", "000032019326000005", "primary.htm"): b"body"},
    )

    result = pull_new_filings(journal, settings, edgar_provider=provider, storage_root=str(tmp_path))

    assert result["fetch_errors"] == 1  # the dead CIK
    assert result["docs_fetched"] == 1  # AAPL still processed despite the earlier outage
    assert result["ciks_checked"] == 2


def test_pull_new_filings_with_none_provider_is_a_safe_noop(tmp_path, journal):
    settings = make_settings()
    result = pull_new_filings(journal, settings, edgar_provider=None, storage_root=str(tmp_path))
    assert "error" not in result
    assert result["docs_fetched"] == 0


def test_pull_new_filings_detects_a_gzip_roundtrip_mismatch_and_does_not_archive_it(tmp_path, journal, monkeypatch):
    """Regression guard for the write-then-verify safety check: if the
    just-written gzip doesn't read back byte-identical, the row must never be
    journaled and the file must be removed, not silently trusted."""
    from alphaos.text_archive import service as service_mod

    settings = make_settings()
    journal.insert("cik_map", {
        "ticker": "AAPL", "cik": "320193",
        "first_seen_at_utc": "2026-01-01T00:00:00+00:00", "last_confirmed_at_utc": "2026-01-01T00:00:00+00:00",
    })
    provider = _FakeEdgarProvider(
        submissions_by_cik={"320193": _submissions_payload([
            ("8-K", "0000320193-26-000006", "2026-07-01", "primary.htm"),
        ])},
        documents={("320193", "000032019326000006", "primary.htm"): b"good content"},
    )

    call_count = {"n": 0}
    real_sha256 = real_hashlib.sha256

    class _TamperedOnSecondCall:
        def __init__(self, data=b""):
            call_count["n"] += 1
            self._digest = real_sha256(data)
            self._tamper = call_count["n"] == 2  # 1st = write-time hash, 2nd = round-trip check

        def hexdigest(self):
            return ("f" * 64) if self._tamper else self._digest.hexdigest()

    monkeypatch.setattr(service_mod.hashlib, "sha256", _TamperedOnSecondCall)

    result = pull_new_filings(journal, settings, edgar_provider=provider, storage_root=str(tmp_path))

    assert result["fetch_errors"] == 1
    assert result["docs_fetched"] == 0
    assert journal.one("SELECT * FROM text_documents WHERE accession_no = '0000320193-26-000006'") is None
    written_files = list(tmp_path.rglob("*.gz"))
    assert written_files == []  # the tampered/mismatched file must have been removed


def test_storage_path_partitions_by_seen_at_year_and_month(tmp_path):
    path = _storage_path(str(tmp_path), "2026-07-09T12:00:00+00:00", "0000320193-26-000099")
    assert path == str(tmp_path / "2026" / "07" / "0000320193-26-000099.gz")


# ======================================================= is_probable_trading_day
def test_is_probable_trading_day_true_on_weekdays():
    assert is_probable_trading_day(date(2026, 7, 9))  # Thursday


def test_is_probable_trading_day_false_on_weekends():
    assert not is_probable_trading_day(date(2026, 7, 11))  # Saturday
    assert not is_probable_trading_day(date(2026, 7, 12))  # Sunday


# ================================================== run_text_archive_pull_job
def test_run_text_archive_pull_job_skips_when_disabled_by_default(journal):
    from alphaos.orchestrator import Orchestrator
    from alphaos.scheduler.jobs import run_text_archive_pull_job

    settings = make_settings()  # TEXT_ARCHIVE_ENABLED defaults false
    assert settings.text_archive_enabled is False
    orch = Orchestrator(settings=settings, journal=journal)

    result = run_text_archive_pull_job(orch, runner=None)

    assert result == {"status": "skipped", "reason": "TEXT_ARCHIVE_ENABLED is false"}


def test_run_text_archive_pull_job_disabled_never_calls_the_fetch_pipeline(journal, monkeypatch):
    """Behavior probe, not just testimony: prove the short-circuit by making
    the downstream calls raise if reached at all."""
    from alphaos.orchestrator import Orchestrator
    from alphaos.scheduler import jobs as jobs_mod

    def _boom(*a, **k):
        raise AssertionError("must not be called when TEXT_ARCHIVE_ENABLED is false")

    monkeypatch.setattr("alphaos.text_archive.service.refresh_cik_map", _boom)
    monkeypatch.setattr("alphaos.text_archive.service.pull_new_filings", _boom)
    settings = make_settings()
    orch = Orchestrator(settings=settings, journal=journal)

    result = jobs_mod.run_text_archive_pull_job(orch, runner=None)

    assert result["status"] == "skipped"


def test_run_text_archive_pull_job_pages_on_zero_docs_on_a_trading_day(journal, monkeypatch):
    from alphaos.orchestrator import Orchestrator
    from alphaos.scheduler import jobs as jobs_mod
    from alphaos.util import timeutils

    settings = make_settings(ALPHAOS_MODE="paper", TEXT_ARCHIVE_ENABLED="true",
                              SEC_EDGAR_CONTACT_EMAIL="ops@example.com")
    orch = Orchestrator(settings=settings, journal=journal)
    monkeypatch.setattr(timeutils, "market_date", lambda: date(2026, 7, 9))  # Thursday
    monkeypatch.setattr(
        "alphaos.text_archive.service.refresh_cik_map", lambda *a, **k: {"mapped": 0}
    )
    monkeypatch.setattr(
        "alphaos.text_archive.service.pull_new_filings",
        lambda *a, **k: {"docs_fetched": 0, "docs_already_archived": 0, "ciks_checked": 3, "fetch_errors": 0},
    )
    sent = []
    monkeypatch.setattr(
        "alphaos.util.alerts.send_alert",
        lambda settings, title, message, priority="default", journal=None: sent.append(
            {"title": title, "priority": priority}
        ),
    )

    result = jobs_mod.run_text_archive_pull_job(orch, runner=None)

    assert result["status"] == "completed"
    assert len(sent) == 1
    assert sent[0]["priority"] == "high"
    assert "zero documents" in sent[0]["title"].lower()


def test_run_text_archive_pull_job_stays_silent_when_everything_was_already_archived(journal, monkeypatch):
    """Regression guard (scope/safety audit finding): zero NEW docs on a
    trading day must NOT page if everything found was already archived --
    that means the fetcher worked, not that it's broken."""
    from alphaos.orchestrator import Orchestrator
    from alphaos.scheduler import jobs as jobs_mod
    from alphaos.util import timeutils

    settings = make_settings(ALPHAOS_MODE="paper", TEXT_ARCHIVE_ENABLED="true",
                              SEC_EDGAR_CONTACT_EMAIL="ops@example.com")
    orch = Orchestrator(settings=settings, journal=journal)
    monkeypatch.setattr(timeutils, "market_date", lambda: date(2026, 7, 9))  # Thursday
    monkeypatch.setattr(
        "alphaos.text_archive.service.refresh_cik_map", lambda *a, **k: {"mapped": 0}
    )
    monkeypatch.setattr(
        "alphaos.text_archive.service.pull_new_filings",
        lambda *a, **k: {"docs_fetched": 0, "docs_already_archived": 5, "ciks_checked": 3, "fetch_errors": 0},
    )
    sent = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: sent.append(k))

    jobs_mod.run_text_archive_pull_job(orch, runner=None)

    assert sent == []


def test_run_text_archive_pull_job_stays_silent_on_a_weekend_zero_doc_day(journal, monkeypatch):
    from alphaos.orchestrator import Orchestrator
    from alphaos.scheduler import jobs as jobs_mod
    from alphaos.util import timeutils

    settings = make_settings(ALPHAOS_MODE="paper", TEXT_ARCHIVE_ENABLED="true",
                              SEC_EDGAR_CONTACT_EMAIL="ops@example.com")
    orch = Orchestrator(settings=settings, journal=journal)
    monkeypatch.setattr(timeutils, "market_date", lambda: date(2026, 7, 11))  # Saturday
    monkeypatch.setattr(
        "alphaos.text_archive.service.refresh_cik_map", lambda *a, **k: {"mapped": 0}
    )
    monkeypatch.setattr(
        "alphaos.text_archive.service.pull_new_filings",
        lambda *a, **k: {"docs_fetched": 0, "docs_already_archived": 0, "ciks_checked": 3, "fetch_errors": 0},
    )
    sent = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: sent.append(k))

    jobs_mod.run_text_archive_pull_job(orch, runner=None)

    assert sent == []


def test_run_text_archive_pull_job_does_not_double_page_when_an_error_is_already_present(journal, monkeypatch):
    from alphaos.orchestrator import Orchestrator
    from alphaos.scheduler import jobs as jobs_mod
    from alphaos.util import timeutils

    settings = make_settings(ALPHAOS_MODE="paper", TEXT_ARCHIVE_ENABLED="true",
                              SEC_EDGAR_CONTACT_EMAIL="ops@example.com")
    orch = Orchestrator(settings=settings, journal=journal)
    monkeypatch.setattr(timeutils, "market_date", lambda: date(2026, 7, 9))  # Thursday
    monkeypatch.setattr(
        "alphaos.text_archive.service.refresh_cik_map", lambda *a, **k: {"mapped": 0}
    )
    monkeypatch.setattr(
        "alphaos.text_archive.service.pull_new_filings",
        lambda *a, **k: {"docs_fetched": 0, "docs_already_archived": 0, "ciks_checked": 0,
                         "error": "provider outage"},
    )
    sent = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: sent.append(k))

    jobs_mod.run_text_archive_pull_job(orch, runner=None)

    assert sent == []  # a hard error is a different failure mode, not "suspiciously quiet"


# =============================================== no-decision-path grep guard
def test_text_archive_module_never_touches_the_order_submission_surface():
    """Collect-only law: this subsystem must never introduce a path toward
    broker submission/approval/position-close -- same exact token list and
    pattern as the scheduler package's own PR9 guard test
    (test_no_orders_approvals_fills_positions_created_by_pr9_code)."""
    import pathlib

    banned = ("execute_proposal", "approve_proposal", "close_position",
              "submit_bracket", "submit_order", "place_order")
    text_archive_dir = pathlib.Path(__file__).resolve().parents[1] / "alphaos" / "text_archive"
    for py_file in text_archive_dir.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{py_file.name} references {token!r}"
