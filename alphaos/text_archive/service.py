"""TEXT-0: journal-facing archive service -- CIK map refresh + the nightly
fetch/store pipeline. Collect only: no trading logic, no scanner, no AI
calls, no scoring (this module never touches candidates/proposals/gates).

THE LAW OF THIS SUBSYSTEM: every ``text_documents`` row records BOTH
``published_at`` (the source's own timestamp) and ``seen_at`` (wall clock
when AlphaOS fetched it). All future backtests/shadow tests may only ever
condition on ``seen_at`` -- see the ``text_documents`` schema comment
(alphaos/journal/schema.py) for the full reasoning.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import sqlite3
from datetime import date as _date
from typing import Any, Optional

from alphaos.constants import Severity
from alphaos.scanner.candidate_scanner import DEFAULT_UNIVERSE
from alphaos.text_archive.forms import EDGAR_FORMS_V1, is_catalog_form
from alphaos.text_archive.sec_edgar import make_edgar_provider
from alphaos.universe.builder import load_universe_file
from alphaos.util import timeutils
from alphaos.util.ids import new_id

DEFAULT_STORAGE_ROOT = "data/text_archive"


def _universe_tickers(settings) -> set:
    """Union of the core book (DEFAULT_UNIVERSE) and EXP-0's committed
    shadow-tier universe file (if one has been built yet -- ``universe_build``
    is its own separate, deliberate operator step; its ABSENCE here is not an
    error, just "no shadow tier yet", matching load_universe_file's own
    contract)."""
    tickers = {t for t in DEFAULT_UNIVERSE if t not in {"SPY", "QQQ", "IWM", "DIA"}}  # index ETFs, not filers
    doc = load_universe_file(settings.shadow_tier_universe_file)
    if doc:
        tickers |= {s["symbol"] for s in doc.get("symbols", []) if s.get("symbol")}
    return tickers


def refresh_cik_map(journal, settings, edgar_provider=None) -> dict:
    """Union the core book + EXP-0's shadow universe -> look up each
    ticker's CIK via SEC's free, official ticker->CIK map -> upsert into
    ``cik_map``. "Once archived-for, always archived-for": a ticker already
    in ``cik_map`` is never removed just because it later drops out of the
    live universe -- a refresh only ever adds/refreshes rows, never deletes.
    Never raises; returns a result dict (with an ``"error"`` key on
    failure)."""
    result: dict[str, Any] = {"tickers_considered": 0, "mapped": 0, "unmapped": 0, "refreshed": 0}
    try:
        provider = edgar_provider if edgar_provider is not None else make_edgar_provider(settings, journal)
        if provider is None:
            return result  # mock/offline/no-contact-email -- nothing to refresh against, not a failure

        universe = _universe_tickers(settings)
        result["tickers_considered"] = len(universe)
        ticker_to_cik = provider.get_company_tickers()
        if not ticker_to_cik:
            journal.log_system_event(
                Severity.WARNING, "text_archive",
                "SEC company_tickers.json fetch returned nothing -- cik_map not refreshed this run.",
            )
            return result

        now = timeutils.stamp().utc
        for ticker in sorted(universe):
            cik = ticker_to_cik.get(ticker.upper())
            if cik is None:
                result["unmapped"] += 1
                continue
            result["mapped"] += 1
            existing = journal.one("SELECT id FROM cik_map WHERE ticker = ?", (ticker,))
            if existing:
                journal.conn.execute(
                    "UPDATE cik_map SET cik = ?, last_confirmed_at_utc = ? WHERE ticker = ?",
                    (cik, now, ticker),
                )
                result["refreshed"] += 1
            else:
                journal.insert("cik_map", {
                    "ticker": ticker, "cik": cik, "first_seen_at_utc": now, "last_confirmed_at_utc": now,
                })
        journal.conn.commit()
    except Exception as exc:  # noqa: BLE001 - a cik_map refresh failure must be visible, never silent
        result["error"] = str(exc)
        try:
            journal.log_system_event(Severity.ERROR, "text_archive", f"refresh_cik_map failed: {exc}")
        except Exception:  # noqa: BLE001
            pass
    return result


def _storage_path(storage_root: str, seen_at: str, accession_no: str) -> str:
    year, month = seen_at[:4], seen_at[5:7]
    return os.path.join(storage_root, year, month, f"{accession_no}.gz")


def pull_new_filings(journal, settings, edgar_provider=None, storage_root: Optional[str] = None) -> dict:
    """The nightly fetch: for each ``cik_map`` row, pull recent submissions,
    filter by the v1 form catalog, and for each NEW (not-yet-archived)
    accession, fetch the raw document body, gzip + sha256 + store on disk,
    and insert one ``text_documents`` row. Re-fetch of an already-archived
    accession is a no-op (checked before any network call). Never raises;
    returns a result dict (with an ``"error"`` key on failure instead)."""
    root = storage_root if storage_root is not None else DEFAULT_STORAGE_ROOT
    fetch_run_id = new_id("fetchrun")
    result: dict[str, Any] = {
        "fetch_run_id": fetch_run_id,
        "ciks_checked": 0,
        "docs_fetched": 0,
        "docs_already_archived": 0,
        "fetch_errors": 0,
        "skipped_forms": {},
    }
    try:
        provider = edgar_provider if edgar_provider is not None else make_edgar_provider(settings, journal)
        if provider is None:
            return result  # mock/offline/no-contact-email -- nothing to fetch, not a failure

        cik_rows = journal.query("SELECT ticker, cik FROM cik_map")
        for row in cik_rows:
            result["ciks_checked"] += 1
            submissions = provider.get_submissions(row["cik"])
            if not submissions:
                result["fetch_errors"] += 1
                continue
            recent = (submissions.get("filings") or {}).get("recent") or {}
            forms = recent.get("form") or []
            accessions = recent.get("accessionNumber") or []
            filing_dates = recent.get("filingDate") or []
            primary_docs = recent.get("primaryDocument") or []

            for i in range(len(accessions)):
                form = forms[i] if i < len(forms) else None
                accession_raw = accessions[i] if i < len(accessions) else None
                if not accession_raw:
                    continue
                if not is_catalog_form(form):
                    result["skipped_forms"][form] = result["skipped_forms"].get(form, 0) + 1
                    continue

                already = journal.one(
                    "SELECT id FROM text_documents WHERE accession_no = ?", (accession_raw,)
                )
                if already:
                    result["docs_already_archived"] += 1
                    continue

                accession_no_dashes = accession_raw.replace("-", "")
                primary_document = primary_docs[i] if i < len(primary_docs) else None
                if not primary_document:
                    result["fetch_errors"] += 1
                    continue

                seen_at = timeutils.stamp().utc  # captured BEFORE the fetch attempt (a lower bound)
                content = provider.get_document(row["cik"], accession_no_dashes, primary_document)
                if content is None:
                    result["fetch_errors"] += 1
                    continue

                sha256 = hashlib.sha256(content).hexdigest()
                storage_path = _storage_path(root, seen_at, accession_raw)
                os.makedirs(os.path.dirname(storage_path), exist_ok=True)
                with gzip.open(storage_path, "wb") as f:
                    f.write(content)

                # "sha256 verified on write" (spec's own MANIFEST semantics):
                # read the just-written gzip back and confirm it round-trips
                # byte-identical BEFORE trusting it enough to journal a row --
                # a torn/corrupt write must never be silently indexed as if
                # it were a good copy.
                with gzip.open(storage_path, "rb") as f:
                    roundtrip = f.read()
                if hashlib.sha256(roundtrip).hexdigest() != sha256:
                    os.remove(storage_path)
                    result["fetch_errors"] += 1
                    journal.log_system_event(
                        Severity.ERROR, "text_archive",
                        f"gzip round-trip mismatch for accession {accession_raw} -- file removed, not archived.",
                    )
                    continue

                try:
                    journal.insert("text_documents", {
                        "document_id": new_id("txtdoc"),
                        "cik": row["cik"],
                        "ticker_at_time": row["ticker"],
                        "form_type": form,
                        "edgar_forms_version": EDGAR_FORMS_V1,
                        "accession_no": accession_raw,
                        "published_at": filing_dates[i] if i < len(filing_dates) else None,
                        "seen_at": seen_at,
                        "source_url": (
                            f"https://www.sec.gov/Archives/edgar/data/{int(row['cik'])}/"
                            f"{accession_no_dashes}/{primary_document}"
                        ),
                        "sha256": sha256,
                        "byte_size": len(content),
                        "storage_path": storage_path,
                        "source": "edgar",
                        "fetch_run_id": fetch_run_id,
                    })
                    result["docs_fetched"] += 1
                except sqlite3.IntegrityError:
                    # A concurrent/duplicate insert raced us -- the file we just
                    # wrote is a harmless duplicate of what's already archived.
                    result["docs_already_archived"] += 1

        journal.log_system_event(
            Severity.INFO, "text_archive",
            f"text_archive_pull {fetch_run_id}: {result['docs_fetched']} new docs, "
            f"{result['docs_already_archived']} already archived, {result['fetch_errors']} errors, "
            f"{result['ciks_checked']} CIKs checked.",
            result,
        )
    except Exception as exc:  # noqa: BLE001 - a fetch-run failure must be visible, never silent
        result["error"] = str(exc)
        try:
            journal.log_system_event(Severity.ERROR, "text_archive", f"pull_new_filings failed: {exc}")
        except Exception:  # noqa: BLE001
            pass
    return result


def is_probable_trading_day(d: _date) -> bool:
    """Weekday-only proxy (Mon-Fri) -- this codebase has NO market-holiday
    table anywhere yet (see ``timeutils.market_session``'s own "calendar-
    naive... no holiday table in v1" docstring caveat); a real US-market
    holiday will still read as a probable trading day here, same
    pre-existing, accepted limitation as the rest of the system. Used only
    to suppress an obviously-wrong weekend zero-doc alert, not as a claim of
    full market-calendar accuracy."""
    return d.weekday() < 5
