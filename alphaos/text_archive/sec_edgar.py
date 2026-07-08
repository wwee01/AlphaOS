"""TEXT-0: raw SEC EDGAR REST client.

Same pattern as ``alphaos/data/providers/alpaca_bars.py``: raw ``urllib``
(no SDK), fails safe (empty dict/list/None) on any error -- callers must
treat that as "unavailable", never as "nothing exists" -- and real network
calls are only exercised behind ``RUN_LIVE_SEC_TESTS=true``.

SEC's own published requirements (not optional, this IS the compliance
surface the spec calls out -- "the job must be a good citizen or the moat
gets IP-banned"):
* A descriptive User-Agent identifying the requester + a real contact email
  (SEC's fair-access policy; an empty/placeholder email risks a harsher rate
  limit or an outright block). This client REFUSES to make a live request
  without one configured -- see ``make_edgar_provider``.
* <=10 requests/second (SEC's own stated ceiling; ``RateLimiter`` below
  enforces a lower, conservative default and is injectable for tests).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from alphaos.constants import Severity

HTTP_TIMEOUT = 20
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL_TMPL = "https://data.sec.gov/submissions/CIK{cik10}.json"
# Real filing bodies live under the *non-data* sec.gov Archives host.
DOCUMENT_URL_TMPL = "https://www.sec.gov/Archives/edgar/data/{cik_bare}/{accession_no_dashes}/{primary_document}"


class RateLimiter:
    """Enforces a minimum interval between requests. ``sleep_fn``/``time_fn``
    are injectable so tests can assert on the ceiling being honored without
    a real wall-clock sleep (a fake time_fn/sleep_fn pair that just advances
    a counter, matching this codebase's established mock-clock test style)."""

    def __init__(
        self, max_per_second: float,
        sleep_fn: Callable[[float], None] = time.sleep,
        time_fn: Callable[[], float] = time.monotonic,
    ):
        self._min_interval = 1.0 / max_per_second if max_per_second > 0 else 0.0
        self._sleep_fn = sleep_fn
        self._time_fn = time_fn
        self._last_request_at: Optional[float] = None

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        now = self._time_fn()
        if self._last_request_at is not None:
            elapsed = now - self._last_request_at
            remaining = self._min_interval - elapsed
            if remaining > 0:
                self._sleep_fn(remaining)
                now = self._time_fn()
        self._last_request_at = now


# SEC's own guidance ceiling is 10 req/s; hard-coded LOWER here per the spec
# ("<=10 req/s hard-coded lower in config") -- a good citizen leaves margin.
DEFAULT_MAX_REQUESTS_PER_SECOND = 4.0


class SecEdgarProvider:
    name = "sec_edgar"

    def __init__(self, settings, journal=None, rate_limiter: Optional[RateLimiter] = None):
        self.settings = settings
        self.journal = journal
        self.rate_limiter = rate_limiter or RateLimiter(DEFAULT_MAX_REQUESTS_PER_SECOND)

    @property
    def _user_agent(self) -> str:
        return f"AlphaOS-TEXT-0/1 ({self.settings.sec_edgar_contact_email})"

    def _get_json(self, url: str) -> Optional[dict]:
        self.rate_limiter.wait()
        try:  # pragma: no cover - live network path (gated test only)
            req = urllib.request.Request(
                url, headers={"User-Agent": self._user_agent, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
            self._log(Severity.WARNING, f"SEC EDGAR JSON fetch failed for {url}: {exc}")
            return None

    def get_company_tickers(self) -> dict:
        """``{ticker: cik_str}`` from SEC's own free, official ticker->CIK
        map. Returns ``{}`` on any error -- callers must treat that as
        "mapping unavailable this run", never as "no companies exist"."""
        payload = self._get_json(COMPANY_TICKERS_URL)
        if not payload:
            return {}
        out = {}
        for entry in payload.values():
            ticker = entry.get("ticker")
            cik = entry.get("cik_str")
            if ticker and cik is not None:
                out[ticker.upper()] = str(cik)
        return out

    def get_submissions(self, cik: str) -> Optional[dict]:
        """Raw submissions payload for a 10-digit-zero-padded ``cik``.
        Returns None on any error -- "unavailable this run", never "zero
        filings exist"."""
        cik10 = str(cik).zfill(10)
        return self._get_json(SUBMISSIONS_URL_TMPL.format(cik10=cik10))

    def get_document(self, cik: str, accession_no_dashes: str, primary_document: str) -> Optional[bytes]:
        """Raw bytes of one filing document. Returns None on any error."""
        self.rate_limiter.wait()
        cik_bare = str(int(cik))  # SEC's Archives path wants no leading zeros
        url = DOCUMENT_URL_TMPL.format(
            cik_bare=cik_bare, accession_no_dashes=accession_no_dashes, primary_document=primary_document,
        )
        try:  # pragma: no cover - live network path (gated test only)
            req = urllib.request.Request(url, headers={"User-Agent": self._user_agent})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.URLError as exc:
            self._log(Severity.WARNING, f"SEC EDGAR document fetch failed for {url}: {exc}")
            return None

    def _log(self, sev, msg: str) -> None:
        if self.journal is not None:
            self.journal.log_system_event(sev, "text_archive", msg)


def make_edgar_provider(settings, journal=None) -> Optional[SecEdgarProvider]:
    """Build the live EDGAR provider, or None in mock/offline mode OR when no
    contact email is configured (SEC's fair-access policy requires one --
    this client refuses to send a placeholder/empty contact rather than risk
    the operator's IP getting rate-limited harder or banned). Tests inject a
    fake provider directly instead."""
    if settings.is_mock or settings.offline_mode:
        return None
    if not settings.sec_edgar_contact_email:
        if journal is not None:
            journal.log_system_event(
                Severity.WARNING, "text_archive",
                "SEC_EDGAR_CONTACT_EMAIL is not set -- live EDGAR fetches disabled until an "
                "operator configures a real contact email (SEC's fair-access policy).",
            )
        return None
    return SecEdgarProvider(settings, journal)
