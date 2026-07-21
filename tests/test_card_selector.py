"""SETUP-1 (S1a): pure selector + frozen context. Covers:
* cache health -- the 5-state matrix (ok, refresh_failed_recent via 3
  distinct triggers, stale, cache_empty, unknown),
* supersession-before-date-filtering -- the corrected reschedule-away and
  reschedule-in scenarios, NULL-fiscal singleton handling, tie-break,
* strict-< timestamp boundary (no same-instant ambiguity),
* BMO/AMC/UNKNOWN timing rules, inclusive window boundaries, weekend/
  holiday-spanning windows, bad-data roll-forward,
* degraded health always forces the default card; a healthy-but-
  ineligible symbol also gets the default card, but with status='ok',
* the golden-fixture semantic hash (a deliberate change to selection
  logic must break this test, forcing an explicit re-pin).

Deliberately does NOT touch orchestrator.py/candidate_scanner.py/any
scheduler job -- this module has zero production callers in S1a. All
fixture dates are FIXED, real calendar dates (this suite tests calendar
ARITHMETIC ITSELF -- e.g. "is 2026-11-26 a holiday" -- a static fact, not a
"today"-relative window, so §H.1's own "never hardcode a date" law doesn't
apply the way it does to accumulation-window fixtures elsewhere).

All offline, in-memory, mock mode. No real money, no network.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from alphaos.cards import selector
from alphaos.util.ids import new_id


# --------------------------------------------------------------- cache health
def _insert_cache_row(journal, symbol, report_date, fiscal_date_ending=None, timing=None, created_at_utc=None):
    journal.insert("earnings_calendar_cache", {
        "entry_id": new_id("earncal"), "symbol": symbol, "report_date": report_date,
        "fiscal_date_ending": fiscal_date_ending, "timing": timing, "source": "alpha_vantage",
        **({"created_at_utc": created_at_utc, "created_at_sgt": created_at_utc} if created_at_utc else {}),
    })


def _insert_pull_run(journal, finished_at_utc, status="completed", n_fetched=10, warnings=None):
    result_summary = {"status": status, "earnings_calendar_result": {
        "market_date": finished_at_utc[:10], "n_fetched": n_fetched, "n_written": n_fetched,
        "warnings": warnings or [],
    }}
    journal.insert("job_runs", {
        "job_run_id": new_id("jr"), "job_type": "earnings_calendar_pull",
        "started_at_utc": finished_at_utc, "started_at_sgt": finished_at_utc,
        "finished_at_utc": finished_at_utc, "finished_at_sgt": finished_at_utc,
        "status": status, "result_summary_json": json.dumps(result_summary),
    })


def test_health_ok_when_latest_run_usable(journal):
    _insert_cache_row(journal, "AAPL", "2026-08-01")
    _insert_pull_run(journal, "2026-07-16T01:00:00+00:00", n_fetched=200)
    health = selector.compute_cache_health(journal, "2026-07-16T12:00:00+00:00")
    assert health == selector.CacheHealth.OK


def test_health_cache_empty_overrides_everything(journal):
    _insert_pull_run(journal, "2026-07-16T01:00:00+00:00", n_fetched=200)  # a run happened, but zero cache rows
    health = selector.compute_cache_health(journal, "2026-07-16T12:00:00+00:00")
    assert health == selector.CacheHealth.CACHE_EMPTY


def test_health_refresh_failed_recent_via_zero_n_fetched(journal):
    _insert_cache_row(journal, "AAPL", "2026-08-01")
    _insert_pull_run(journal, "2026-07-15T01:00:00+00:00", n_fetched=200)   # earlier success
    _insert_pull_run(journal, "2026-07-16T01:00:00+00:00", n_fetched=0)     # latest: vendor returned nothing
    health = selector.compute_cache_health(journal, "2026-07-16T12:00:00+00:00")
    assert health == selector.CacheHealth.REFRESH_FAILED_RECENT


def test_health_refresh_failed_recent_via_nonempty_warnings(journal):
    _insert_cache_row(journal, "AAPL", "2026-08-01")
    _insert_pull_run(journal, "2026-07-15T01:00:00+00:00", n_fetched=200)
    _insert_pull_run(journal, "2026-07-16T01:00:00+00:00", n_fetched=180, warnings=["AAPL: write failed: ..."])
    health = selector.compute_cache_health(journal, "2026-07-16T12:00:00+00:00")
    assert health == selector.CacheHealth.REFRESH_FAILED_RECENT


def test_health_refresh_failed_recent_via_explicit_failed_status(journal):
    _insert_cache_row(journal, "AAPL", "2026-08-01")
    _insert_pull_run(journal, "2026-07-15T01:00:00+00:00", n_fetched=200)
    journal.insert("job_runs", {
        "job_run_id": new_id("jr"), "job_type": "earnings_calendar_pull",
        "started_at_utc": "2026-07-16T01:00:00+00:00", "started_at_sgt": "2026-07-16T01:00:00+00:00",
        "finished_at_utc": "2026-07-16T01:00:00+00:00", "finished_at_sgt": "2026-07-16T01:00:00+00:00",
        "status": "failed", "error": "acquire failed",
    })
    health = selector.compute_cache_health(journal, "2026-07-16T12:00:00+00:00")
    assert health == selector.CacheHealth.REFRESH_FAILED_RECENT


def test_health_stale_when_no_run_in_window(journal):
    _insert_cache_row(journal, "AAPL", "2026-08-01")
    _insert_pull_run(journal, "2026-07-10T01:00:00+00:00", n_fetched=200)  # older than 48h
    health = selector.compute_cache_health(journal, "2026-07-16T12:00:00+00:00")
    assert health == selector.CacheHealth.STALE


def test_health_stale_when_every_run_in_window_failed(journal):
    _insert_cache_row(journal, "AAPL", "2026-08-01")
    _insert_pull_run(journal, "2026-07-15T12:00:00+00:00", n_fetched=0)
    _insert_pull_run(journal, "2026-07-16T01:00:00+00:00", n_fetched=0)
    health = selector.compute_cache_health(journal, "2026-07-16T12:00:00+00:00")
    assert health == selector.CacheHealth.STALE


def test_corrupt_result_summary_degrades_gracefully_never_crashes(journal):
    """A single unparseable result_summary_json (and no OTHER usable run in
    the window) degrades to STALE, same as any other unusable-run case --
    a parse failure on one row's payload is not, by itself, grounds for the
    stronger UNKNOWN state, which is reserved for a failure of the health
    check's OWN read (see the broken-journal case below). Above all: it
    must never raise."""
    _insert_cache_row(journal, "AAPL", "2026-08-01")
    journal.insert("job_runs", {
        "job_run_id": new_id("jr"), "job_type": "earnings_calendar_pull",
        "started_at_utc": "2026-07-16T01:00:00+00:00", "started_at_sgt": "2026-07-16T01:00:00+00:00",
        "finished_at_utc": "2026-07-16T01:00:00+00:00", "finished_at_sgt": "2026-07-16T01:00:00+00:00",
        "status": "completed", "result_summary_json": "{not valid json",
    })
    health = selector.compute_cache_health(journal, "2026-07-16T12:00:00+00:00")
    assert health == selector.CacheHealth.STALE  # no usable run exists in-window; degrades, doesn't crash


def test_health_unknown_when_the_health_read_itself_fails(journal):
    """UNKNOWN is reserved for a failure of the health check's OWN read
    (e.g. the journal/query layer itself raising), never for an
    individual row's bad payload (see the corrupt-summary case above)."""
    class _BrokenJournal:
        def scalar(self, *a, **kw):
            raise RuntimeError("boom")

    assert selector.compute_cache_health(_BrokenJournal(), "2026-07-16T12:00:00+00:00") == selector.CacheHealth.UNKNOWN


def test_non_dict_result_summary_degrades_like_invalid_json_not_unknown(journal):
    """Audit-fixup regression (correctness MED): a result_summary_json that
    PARSES but whose top-level value isn't a dict (e.g. a bare JSON list)
    must degrade exactly like invalid JSON -- STALE here, since no other
    usable run exists in-window -- never UNKNOWN. Before the fix,
    summary.get(...) on a list raised AttributeError, which
    compute_cache_health's own broad except-Exception caught and mapped to
    UNKNOWN, a state reserved for a failure of the health check's OWN
    read, not one row's malformed payload."""
    _insert_cache_row(journal, "AAPL", "2026-08-01")
    journal.insert("job_runs", {
        "job_run_id": new_id("jr"), "job_type": "earnings_calendar_pull",
        "started_at_utc": "2026-07-16T01:00:00+00:00", "started_at_sgt": "2026-07-16T01:00:00+00:00",
        "finished_at_utc": "2026-07-16T01:00:00+00:00", "finished_at_sgt": "2026-07-16T01:00:00+00:00",
        "status": "completed", "result_summary_json": "[1, 2, 3]",
    })
    health = selector.compute_cache_health(journal, "2026-07-16T12:00:00+00:00")
    assert health == selector.CacheHealth.STALE


def test_non_dict_result_summary_does_not_mask_an_earlier_usable_run(journal):
    """The sharper failure mode: a non-dict payload on the LATEST run must
    not short-circuit the any(...) fallback check and hide an earlier
    usable run -- that combination must read REFRESH_FAILED_RECENT, not
    UNKNOWN."""
    _insert_cache_row(journal, "AAPL", "2026-08-01")
    _insert_pull_run(journal, "2026-07-15T01:00:00+00:00", n_fetched=200)  # earlier, usable
    journal.insert("job_runs", {  # latest, non-dict payload
        "job_run_id": new_id("jr"), "job_type": "earnings_calendar_pull",
        "started_at_utc": "2026-07-16T01:00:00+00:00", "started_at_sgt": "2026-07-16T01:00:00+00:00",
        "finished_at_utc": "2026-07-16T01:00:00+00:00", "finished_at_sgt": "2026-07-16T01:00:00+00:00",
        "status": "completed", "result_summary_json": '"just a string"',
    })
    health = selector.compute_cache_health(journal, "2026-07-16T12:00:00+00:00")
    assert health == selector.CacheHealth.REFRESH_FAILED_RECENT


def test_health_latest_run_tiebreak_uses_id_not_insertion_order(journal):
    """Audit-fixup regression (correctness LOW): two runs sharing the exact
    same finished_at_utc must resolve "latest" by id DESC, not by
    whatever order SQLite happens to return ties in. Insert the FAILED run
    first and the USABLE run second (higher id = later, still within the
    same instant) -- health must read OK, matching "the higher-id run is
    the real latest," never STALE/REFRESH_FAILED_RECENT from picking the
    lower-id row instead."""
    _insert_cache_row(journal, "AAPL", "2026-08-01")
    same_instant = "2026-07-16T01:00:00+00:00"
    _insert_pull_run(journal, same_instant, n_fetched=0)     # inserted first -> lower id
    _insert_pull_run(journal, same_instant, n_fetched=200)   # inserted second -> higher id, real latest
    health = selector.compute_cache_health(journal, "2026-07-16T12:00:00+00:00")
    assert health == selector.CacheHealth.OK


def test_n_fetched_non_numeric_garbage_is_not_usable(journal):
    """Audit-fixup regression (correctness LOW): a malformed n_fetched
    (non-numeric string, or a numeric-looking string like "0") must not
    be read as usable via bare truthiness -- both are truthy Python
    values but neither is a valid positive count."""
    for garbage in ("abc", "0", -5):
        summary = json.dumps({"earnings_calendar_result": {"n_fetched": garbage, "warnings": []}})
        row = {"status": "completed", "result_summary_json": summary}
        assert selector._run_is_usable(row) is False, f"n_fetched={garbage!r} was wrongly treated as usable"


# ----------------------------------------------------------- context loading
def test_context_excludes_rows_at_or_after_as_of_strict_less_than(journal):
    _insert_pull_run(journal, "2026-07-16T01:00:00+00:00", n_fetched=200)
    _insert_cache_row(journal, "AAPL", "2026-08-01", created_at_utc="2026-07-16T12:00:00+00:00")  # == as_of
    ctx = selector.build_selector_context(journal, "2026-07-16T12:00:00+00:00", ["AAPL"])
    assert ctx.current_belief_by_symbol == {}  # same-instant row excluded, never ambiguous


def test_context_includes_rows_strictly_before_as_of(journal):
    _insert_pull_run(journal, "2026-07-16T01:00:00+00:00", n_fetched=200)
    _insert_cache_row(journal, "AAPL", "2026-08-01", created_at_utc="2026-07-16T11:59:59+00:00")
    ctx = selector.build_selector_context(journal, "2026-07-16T12:00:00+00:00", ["AAPL"])
    assert "AAPL" in ctx.current_belief_by_symbol


def test_context_scopes_to_universe_symbols_only(journal):
    _insert_pull_run(journal, "2026-07-16T01:00:00+00:00", n_fetched=200)
    _insert_cache_row(journal, "AAPL", "2026-08-01", created_at_utc="2026-07-15T00:00:00+00:00")
    _insert_cache_row(journal, "MSFT", "2026-08-01", created_at_utc="2026-07-15T00:00:00+00:00")
    ctx = selector.build_selector_context(journal, "2026-07-16T12:00:00+00:00", ["AAPL"])
    assert "AAPL" in ctx.current_belief_by_symbol
    assert "MSFT" not in ctx.current_belief_by_symbol


# --------------------------------------------------- supersession (the fix)
def test_reschedule_away_obsolete_row_cannot_assign_per(journal):
    """The corrected mechanism's own headline scenario: an older row says
    report date D; a NEWER pre-scan row for the SAME fiscal quarter says
    D+7 (rescheduled forward, now well outside D's window). The scan
    happens ON D. The obsolete D-row must NOT be able to open a PER
    window -- supersession must resolve BEFORE any date filtering."""
    _insert_pull_run(journal, _fresh_pull_run_utc("2026-07-14T09:00:00+00:00"), n_fetched=200)
    _insert_cache_row(
        journal, "AAPL", report_date="2026-07-14", fiscal_date_ending="2026-06-30",
        timing="pre-market", created_at_utc="2026-07-01T00:00:00+00:00",
    )
    _insert_cache_row(  # newer belief, same fiscal quarter, rescheduled to D+7
        journal, "AAPL", report_date="2026-07-21", fiscal_date_ending="2026-06-30",
        timing="pre-market", created_at_utc="2026-07-05T00:00:00+00:00",
    )
    ctx = selector.build_selector_context(journal, "2026-07-14T09:00:00+00:00", ["AAPL"])
    assignment = selector.select_card(ctx, "AAPL", date(2026, 7, 14))
    assert assignment["card_id"] != selector.PER_CARD_ID
    assert assignment["card_assignment_ref"] is None


def test_reschedule_in_newer_row_can_assign_per(journal):
    """The mirror case: older row says D+7 (far away), newer pre-scan row
    reschedules the SAME quarter back to D. Scan on D. PER should open,
    referencing the NEWER row."""
    _insert_pull_run(journal, _fresh_pull_run_utc("2026-07-14T09:00:00+00:00"), n_fetched=200)
    _insert_cache_row(
        journal, "AAPL", report_date="2026-07-21", fiscal_date_ending="2026-06-30",
        timing="pre-market", created_at_utc="2026-07-01T00:00:00+00:00",
    )
    newer = _insert_cache_row_returning_id(
        journal, "AAPL", report_date="2026-07-14", fiscal_date_ending="2026-06-30",
        timing="pre-market", created_at_utc="2026-07-05T00:00:00+00:00",
    )
    ctx = selector.build_selector_context(journal, "2026-07-14T09:00:00+00:00", ["AAPL"])
    assignment = selector.select_card(ctx, "AAPL", date(2026, 7, 14))
    assert assignment["card_id"] == selector.PER_CARD_ID
    assert assignment["card_assignment_ref"] == newer


def _insert_cache_row_returning_id(journal, symbol, report_date, fiscal_date_ending=None, timing=None, created_at_utc=None):
    entry_id = new_id("earncal")
    journal.insert("earnings_calendar_cache", {
        "entry_id": entry_id, "symbol": symbol, "report_date": report_date,
        "fiscal_date_ending": fiscal_date_ending, "timing": timing, "source": "alpha_vantage",
        **({"created_at_utc": created_at_utc, "created_at_sgt": created_at_utc} if created_at_utc else {}),
    })
    row = journal.one("SELECT id FROM earnings_calendar_cache WHERE entry_id = ?", (entry_id,))
    return row["id"]


def test_null_fiscal_rows_never_form_a_reschedule_chain(journal):
    """Two NULL-fiscal rows for the same symbol at different report_dates
    must be treated as two INDEPENDENT singleton events, never as a
    reschedule of one another (no chain is inferable without a fiscal
    key)."""
    _insert_pull_run(journal, _fresh_pull_run_utc("2026-07-14T09:00:00+00:00"), n_fetched=200)
    _insert_cache_row(journal, "AAPL", report_date="2026-07-14", fiscal_date_ending=None,
                      timing="pre-market", created_at_utc="2026-07-01T00:00:00+00:00")
    _insert_cache_row(journal, "AAPL", report_date="2026-07-21", fiscal_date_ending=None,
                      timing="pre-market", created_at_utc="2026-07-05T00:00:00+00:00")
    ctx = selector.build_selector_context(journal, "2026-07-14T09:00:00+00:00", ["AAPL"])
    assert len(ctx.current_belief_by_symbol["AAPL"]) == 2  # both survive as independent events


def _fresh_pull_run_utc(as_of: str) -> str:
    """A pull-run timestamp 1 hour before ``as_of`` -- always inside the
    48h health window regardless of which ``as_of`` a given test uses, so
    fixtures that aren't testing health itself don't need to hand-compute
    the window each time."""
    return (datetime.fromisoformat(as_of) - timedelta(hours=1)).isoformat()


# ------------------------------------------------------------- timing/windows
def _ctx_with_event(report_date, timing, as_of="2026-07-16T09:00:00+00:00", journal=None,
                    fiscal="2026-06-30", created_at_utc="2026-07-01T00:00:00+00:00", symbol="AAPL"):
    _insert_pull_run(journal, _fresh_pull_run_utc(as_of), n_fetched=200)
    _insert_cache_row(journal, symbol, report_date=report_date, fiscal_date_ending=fiscal,
                      timing=timing, created_at_utc=created_at_utc)
    return selector.build_selector_context(journal, as_of, [symbol])


def test_bmo_window_opens_on_report_date_itself(journal):
    ctx = _ctx_with_event("2026-07-14", "pre-market", journal=journal)  # Tuesday
    assignment = selector.select_card(ctx, "AAPL", date(2026, 7, 14))
    assert assignment["card_id"] == selector.PER_CARD_ID


def test_amc_window_does_not_open_on_report_date(journal):
    """An after-close release on day D cannot have been known before that
    day's 16:00 ET close -- no scan window on day D itself may be
    eligible."""
    ctx = _ctx_with_event("2026-07-14", "post-market", journal=journal)
    assignment = selector.select_card(ctx, "AAPL", date(2026, 7, 14))
    assert assignment["card_id"] != selector.PER_CARD_ID


def test_amc_window_opens_next_trading_day_skipping_weekend(journal):
    ctx = _ctx_with_event("2026-07-17", "post-market", journal=journal)  # Friday
    monday = date(2026, 7, 20)
    assignment = selector.select_card(ctx, "AAPL", monday)
    assert assignment["card_id"] == selector.PER_CARD_ID


def test_unknown_timing_treated_as_amc(journal):
    ctx = _ctx_with_event("2026-07-14", None, journal=journal)  # no timing at all
    same_day = selector.select_card(ctx, "AAPL", date(2026, 7, 14))
    next_day = selector.select_card(ctx, "AAPL", date(2026, 7, 15))
    assert same_day["card_id"] != selector.PER_CARD_ID
    assert next_day["card_id"] == selector.PER_CARD_ID


def test_window_inclusive_of_third_trading_day_exclusive_of_fourth(journal):
    """BMO 2026-07-14 (Tue) -> window = {07-14, 07-15, 07-16}. Day 3
    (07-17) must be ineligible -- the boundary this whole mechanism is
    built to pin exactly."""
    ctx = _ctx_with_event("2026-07-14", "pre-market", journal=journal)
    assert selector.select_card(ctx, "AAPL", date(2026, 7, 16))["card_id"] == selector.PER_CARD_ID  # day 3, last eligible
    assert selector.select_card(ctx, "AAPL", date(2026, 7, 17))["card_id"] != selector.PER_CARD_ID  # day 4, ineligible


def test_window_spans_a_holiday_cluster_correctly(journal):
    """BMO report the Wednesday before Thanksgiving 2026 -- the window
    must skip both Thanksgiving (Thu) and the weekend, landing on
    {11-25, 11-27, 11-30}, a 6-CALENDAR-day span for 3 TRADING days."""
    ctx = _ctx_with_event("2026-11-25", "pre-market", as_of="2026-11-20T09:00:00+00:00", journal=journal)
    assert selector.select_card(ctx, "AAPL", date(2026, 11, 25))["card_id"] == selector.PER_CARD_ID
    assert selector.select_card(ctx, "AAPL", date(2026, 11, 26))["card_id"] != selector.PER_CARD_ID  # Thanksgiving, not a trading day, not in window anyway
    assert selector.select_card(ctx, "AAPL", date(2026, 11, 27))["card_id"] == selector.PER_CARD_ID  # day 2
    assert selector.select_card(ctx, "AAPL", date(2026, 11, 30))["card_id"] == selector.PER_CARD_ID  # day 3, after the weekend
    assert selector.select_card(ctx, "AAPL", date(2026, 12, 1))["card_id"] != selector.PER_CARD_ID   # day 4, ineligible


def test_bmo_bad_data_on_non_trading_day_rolls_forward(journal):
    """A BMO report_date vendor-stamped on a Saturday (impossible in
    reality) must roll forward to the next real trading day rather than
    opening a window on a day with no scan windows at all."""
    ctx = _ctx_with_event("2026-07-18", "pre-market", journal=journal)  # Saturday
    assert selector.select_card(ctx, "AAPL", date(2026, 7, 18))["card_id"] != selector.PER_CARD_ID  # not a trading day
    assert selector.select_card(ctx, "AAPL", date(2026, 7, 20))["card_id"] == selector.PER_CARD_ID  # rolled to Monday


def test_multiple_overlapping_events_most_recent_report_date_wins(journal):
    _insert_pull_run(journal, _fresh_pull_run_utc("2026-07-16T09:00:00+00:00"), n_fetched=200)
    _insert_cache_row(journal, "AAPL", report_date="2026-07-14", fiscal_date_ending="2026-03-31",
                      timing="pre-market", created_at_utc="2026-07-01T00:00:00+00:00")
    newer_id = _insert_cache_row_returning_id(
        journal, "AAPL", report_date="2026-07-15", fiscal_date_ending="2026-06-30",
        timing="pre-market", created_at_utc="2026-07-02T00:00:00+00:00",
    )
    ctx = selector.build_selector_context(journal, "2026-07-16T09:00:00+00:00", ["AAPL"])
    assignment = selector.select_card(ctx, "AAPL", date(2026, 7, 15))  # both windows contain this date
    assert assignment["card_assignment_ref"] == newer_id


def test_overlapping_events_same_report_date_tiebreak_by_id(journal):
    """Audit-fixup regression (correctness LOW): two DISTINCT fiscal-quarter
    events for one symbol can share the exact same report_date (both
    survive _resolve_current_belief as separate groups). The sort's
    primary key (report_date) ties, so the outcome must fall to the
    documented secondary rule (higher id wins) -- never to whatever order
    the underlying SQL happened to return rows in."""
    _insert_pull_run(journal, _fresh_pull_run_utc("2026-07-16T09:00:00+00:00"), n_fetched=200)
    _insert_cache_row(journal, "AAPL", report_date="2026-07-14", fiscal_date_ending="2026-03-31",
                      timing="pre-market", created_at_utc="2026-07-01T00:00:00+00:00")  # lower id
    higher_id = _insert_cache_row_returning_id(
        journal, "AAPL", report_date="2026-07-14", fiscal_date_ending="2026-06-30",
        timing="pre-market", created_at_utc="2026-07-02T00:00:00+00:00",
    )
    ctx = selector.build_selector_context(journal, "2026-07-16T09:00:00+00:00", ["AAPL"])
    assignment = selector.select_card(ctx, "AAPL", date(2026, 7, 14))
    assert assignment["card_assignment_ref"] == higher_id


# ------------------------------------------------------ health gates selection
def test_degraded_health_always_forces_default_card(journal):
    _insert_cache_row(journal, "AAPL", "2026-07-14", fiscal_date_ending="2026-06-30",
                      timing="pre-market", created_at_utc="2026-07-01T00:00:00+00:00")
    _insert_pull_run(journal, "2026-07-16T01:00:00+00:00", n_fetched=0)  # unusable -> stale/failed
    ctx = selector.build_selector_context(journal, "2026-07-16T09:00:00+00:00", ["AAPL"])
    assert ctx.cache_health != selector.CacheHealth.OK
    assignment = selector.select_card(ctx, "AAPL", date(2026, 7, 14))  # would otherwise be eligible
    assert assignment["card_id"] != selector.PER_CARD_ID
    assert assignment["card_assignment_status"] == ctx.cache_health


def test_healthy_but_ineligible_symbol_gets_default_with_ok_status(journal):
    _insert_pull_run(journal, "2026-07-16T01:00:00+00:00", n_fetched=200)
    # The cache is non-empty overall (an unrelated symbol, outside this
    # scan's own universe scope) -- distinguishing "healthy cache, this
    # symbol just isn't eligible" from CACHE_EMPTY, which would otherwise
    # fire first and give the wrong reason for the same observable card.
    _insert_cache_row(journal, "MSFT", "2026-08-01", created_at_utc="2026-07-01T00:00:00+00:00")
    ctx = selector.build_selector_context(journal, "2026-07-16T09:00:00+00:00", ["AAPL"])
    assignment = selector.select_card(ctx, "AAPL", date(2026, 7, 16))  # no cache row for AAPL specifically
    assert assignment["card_id"] != selector.PER_CARD_ID
    assert assignment["card_assignment_status"] == "ok"  # evaluated, just not eligible -- never conflated with degraded


# ------------------------------------------------------------- golden fixture
# S1b integrity follow-up: the fixture matrix + hash computation now live
# in PRODUCTION code (alphaos.cards.selector), not here -- this test
# imports them rather than maintaining an independent copy (the prior
# design had the SAME literal hash hardcoded in both this test file and,
# after this follow-up, selector.py; keeping only one copy removes the
# risk of the two silently drifting apart). See selector.py's own
# "semantic identity" section for the full rationale.
def test_golden_fixture_semantic_hash_is_pinned():
    """Pins SELECTOR_VERSION's semantic meaning: ANY change to selection
    logic (timing rules, window math, ordering, tie-breaks, status
    values) changes this hash, forcing a deliberate, reviewed re-pin --
    exactly the golden-fixture binding the mechanisms spec's Amendment 7
    calls for, independent of (and stronger than) the card YAML's own
    content-hash check, which only covers PARAMETERS, not code semantics.
    This is now a real test of PRODUCTION code (selector.compute_golden_
    fixture_hash() against selector.GOLDEN_FIXTURE_SEMANTIC_HASH), not a
    test-local computation -- proving the production semantic hash
    actually matches the approved canonical selector scenarios."""
    computed = selector.compute_golden_fixture_hash()
    print(f"\nGOLDEN FIXTURE HASH: {computed}")
    assert computed == selector.GOLDEN_FIXTURE_SEMANTIC_HASH


def test_verify_selector_semantic_identity_returns_the_matching_hash():
    """verify_selector_semantic_identity() is the production 'fail loudly
    on drift' entry point -- confirm it succeeds today and returns the
    same value the direct computation does."""
    assert selector.verify_selector_semantic_identity() == selector.GOLDEN_FIXTURE_SEMANTIC_HASH


def test_verify_selector_semantic_identity_raises_on_drift():
    """Swap test: a live hash that no longer matches the pinned constant
    must raise SelectorSemanticDriftError, not silently pass or return a
    wrong value -- proves the 'fail loudly' mechanism the integrity
    follow-up requires actually fires. Monkeypatches only the PINNED
    constant (never the computation), simulating 'the code changed but
    nobody re-pinned it' -- the exact scenario this guard exists for."""
    import pytest

    original = selector.GOLDEN_FIXTURE_SEMANTIC_HASH
    selector.GOLDEN_FIXTURE_SEMANTIC_HASH = "deliberately-wrong-hash-to-prove-the-guard-fires"
    try:
        with pytest.raises(selector.SelectorSemanticDriftError):
            selector.verify_selector_semantic_identity()
    finally:
        selector.GOLDEN_FIXTURE_SEMANTIC_HASH = original
