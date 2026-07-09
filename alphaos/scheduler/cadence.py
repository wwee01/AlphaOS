"""Scheduler v1.5 cadence rules (PR3).

Pure, side-effect-free (beyond reading ``job_runs`` via the journal's existing
query helpers) functions that decide WHEN a job type is due to run and what
its idempotency lock key is for the current window/interval/day. This module
never writes anything and never runs a job itself -- ``is_due`` only answers
"should this job run right now", leaving the actual run to a later stage
(``job_runner.py``).

Conservative bias: any DB read error or ambiguous state defaults to "not due"
rather than "run to be safe" -- a missed scheduler tick is recoverable, a
duplicate/unwanted run is not.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from alphaos.constants import StrEnum
from alphaos.util import timeutils


class JobType(StrEnum):
    """Scheduler job type constants (v1.5 cadence layer)."""

    SCAN = "scan"
    MONITOR = "monitor"
    OUTCOMES_UPDATE = "outcomes_update"
    DAILY_DIGEST = "daily_digest"
    # PR9.5: once-daily paper-equity + SPY-bar capture (measurement only).
    BENCHMARK_SPINE = "benchmark_spine"
    # TEXT-0: once-daily SEC EDGAR pull (collect only).
    TEXT_ARCHIVE_PULL = "text_archive_pull"
    # INSTR-1: once-daily ATR(14) capture (the core-book universe only).
    ATR_UPDATE = "atr_update"
    # CANARY: once-WEEKLY model-drift replay over the frozen golden corpus.
    CANARY_RUN = "canary_run"


def scan_windows(settings) -> list[tuple[str, str]]:
    """Parse ``settings.scheduler_scan_windows`` into (start_hhmm, end_hhmm) pairs.

    Format: "HH:MM-HH:MM,HH:MM-HH:MM,...". Settings has already validated this
    string at load time (``_parse_scan_windows``); this is a light re-parse for
    the cadence layer's own consumption and never raises on the same input.
    """
    windows: list[tuple[str, str]] = []
    for raw_window in settings.scheduler_scan_windows.split(","):
        window = raw_window.strip()
        if not window:
            continue
        bounds = window.split("-")
        if len(bounds) != 2:
            continue
        start, end = bounds[0].strip(), bounds[1].strip()
        if start and end:
            windows.append((start, end))
    return windows


def _hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _market_now(now: Optional[datetime] = None) -> datetime:
    """Current market-local (US Eastern) instant, honoring an injected ``now``.

    Reuses ``timeutils.stamp()``'s ``market_et`` field (the same America/New_York
    conversion ``market_session()``/``started_at_sgt``-style columns already use)
    rather than reaching into timeutils' private tz objects.
    """
    market_et = timeutils.stamp(now).market_et
    return timeutils.parse_iso(market_et)


def _window_containing(hhmm: str, windows: list[tuple[str, str]]) -> Optional[tuple[str, str]]:
    for start, end in windows:
        if start <= hhmm < end:
            return (start, end)
    return None


def default_lock_key(job_type: str, settings, now: Optional[datetime] = None) -> str:
    """Deterministic idempotency key for ``job_type`` at the current window/day.

    - scan: "scan:<market-date>T<window-start>" (the window START, not now).
    - monitor / outcomes_update: "<job_type>:<market-date>T<now rounded down to
      the interval>".
    - daily_digest: "daily_digest:<sgt-date>" (date only, SGT calendar day).
    """
    market_dt_et = _market_now(now)

    if job_type == JobType.SCAN:
        hhmm = _hhmm(market_dt_et)
        window = _window_containing(hhmm, scan_windows(settings))
        start = window[0] if window else hhmm
        return f"{JobType.SCAN}:{market_dt_et.date().isoformat()}T{start}"

    if job_type == JobType.MONITOR:
        interval = max(1, int(settings.scheduler_monitor_interval_minutes))
        return f"{JobType.MONITOR}:{_rounded_down_key(market_dt_et, interval)}"

    if job_type == JobType.OUTCOMES_UPDATE:
        interval = max(1, int(settings.scheduler_outcomes_interval_minutes))
        return f"{JobType.OUTCOMES_UPDATE}:{_rounded_down_key(market_dt_et, interval)}"

    if job_type in (
        JobType.DAILY_DIGEST, JobType.BENCHMARK_SPINE, JobType.TEXT_ARCHIVE_PULL, JobType.ATR_UPDATE,
        JobType.CANARY_RUN,
    ):
        # CANARY_RUN shares this exact date-keyed shape even though its
        # cadence is weekly, not daily: it can only ever be due on ONE
        # matching weekday per week (see _once_weekly_due), so a plain SGT
        # calendar-date key is automatically once-per-week -- no separate
        # ISO-week key needed.
        st = timeutils.stamp(now)
        return f"{job_type}:{st.local_sgt[:10]}"

    return f"{job_type}:{market_dt_et.isoformat()}"


def _rounded_down_key(dt_et: datetime, interval_minutes: int) -> str:
    total_minutes = dt_et.hour * 60 + dt_et.minute
    rounded = (total_minutes // interval_minutes) * interval_minutes
    hh, mm = divmod(rounded, 60)
    return f"{dt_et.date().isoformat()}T{hh:02d}:{mm:02d}"


def is_due(job_type: str, settings, journal, now: Optional[datetime] = None) -> tuple[bool, str]:
    """Whether ``job_type`` is due to run right now. Never raises.

    On any DB read error, defaults to (False, "error checking cadence: ...")
    rather than crashing the caller -- a conservative "don't run" bias.
    """
    try:
        if job_type == JobType.SCAN:
            return _scan_due(settings, journal, now)
        if job_type == JobType.MONITOR:
            return _interval_due(
                JobType.MONITOR, settings.scheduler_monitor_interval_minutes, journal, now,
            )
        if job_type == JobType.OUTCOMES_UPDATE:
            return _interval_due(
                JobType.OUTCOMES_UPDATE, settings.scheduler_outcomes_interval_minutes, journal, now,
            )
        if job_type == JobType.DAILY_DIGEST:
            return _digest_due(settings, journal, now)
        if job_type == JobType.BENCHMARK_SPINE:
            return _benchmark_spine_due(settings, journal, now)
        if job_type == JobType.TEXT_ARCHIVE_PULL:
            return _text_archive_pull_due(settings, journal, now)
        if job_type == JobType.ATR_UPDATE:
            return _atr_update_due(settings, journal, now)
        if job_type == JobType.CANARY_RUN:
            return _canary_run_due(settings, journal, now)
        return (False, f"unknown job_type: {job_type!r}")
    except Exception as exc:  # never crash the caller -- fail toward "don't run"
        return (False, f"error checking cadence: {exc}")


def _scan_due(settings, journal, now: Optional[datetime]) -> tuple[bool, str]:
    market_dt_et = _market_now(now)
    hhmm = _hhmm(market_dt_et)
    window = _window_containing(hhmm, scan_windows(settings))
    if window is None:
        return (False, f"{hhmm} is outside all configured scan windows")

    lock_key = default_lock_key(JobType.SCAN, settings, now)
    existing = journal.count_rows(
        "job_runs",
        "job_type = ? AND lock_key = ? AND status IN ('started', 'completed')",
        (JobType.SCAN, lock_key),
    )
    if existing:
        return (False, f"scan window {window[0]}-{window[1]} already run (lock_key={lock_key})")
    return (True, f"within scan window {window[0]}-{window[1]}")


def _interval_due(job_type: str, interval_minutes: int, journal, now: Optional[datetime]) -> tuple[bool, str]:
    last = journal.one(
        "SELECT finished_at_utc FROM job_runs WHERE job_type = ? AND status = 'completed' "
        "ORDER BY finished_at_utc DESC LIMIT 1",
        (job_type,),
    )
    if not last:
        return (True, f"no prior completed {job_type} run")
    finished_at = last.get("finished_at_utc")
    elapsed = timeutils.age_seconds(finished_at, now)
    if elapsed is None:
        return (True, f"prior completed {job_type} run has an unparseable timestamp")
    elapsed_minutes = elapsed / 60.0
    if elapsed_minutes >= interval_minutes:
        return (True, f"{elapsed_minutes:.1f}m elapsed since last completed {job_type} (>= {interval_minutes}m)")
    return (False, f"only {elapsed_minutes:.1f}m elapsed since last completed {job_type} (< {interval_minutes}m)")


def is_fused(job_type: str, max_consecutive_failures: int, journal) -> tuple[bool, str, int]:
    """Whether ``job_type`` is self-halted after too many consecutive failures.

    Reads the most recent ``max_consecutive_failures`` TERMINAL (status !=
    'started') ``job_runs`` rows for ``job_type``, most-recent first, and
    counts a leading streak of 'failed' rows (a 'completed' or 'skipped' row
    anywhere in that window breaks the streak). Fused iff the streak reaches
    the threshold.

    No separate fuse-state row exists on purpose: because a fused job type is
    never dispatched by ``run_due_jobs`` (see job_runner.py), no new job_runs
    row is added while fused, so the streak -- and therefore the fused
    verdict -- stays frozen until a human forces a run via the CLI's
    ``scheduler_run_job`` (which bypasses this check entirely). A forced
    success adds a 'completed' row and the fuse clears on the next check; a
    forced failure extends the streak and the fuse stays tripped.

    Never raises; a DB read error fails toward "not fused" (a missed fuse trip
    is recoverable, an incorrectly stuck fuse is not) -- same conservative
    bias as ``is_due``.
    """
    try:
        rows = journal.query(
            "SELECT status FROM job_runs WHERE job_type = ? AND status != 'started' "
            "ORDER BY id DESC LIMIT ?",
            (job_type, max_consecutive_failures),
        )
    except Exception as exc:  # never crash the caller -- fail toward "not fused"
        return (False, f"error checking fuse state: {exc}", 0)

    streak = 0
    for row in rows:
        if row["status"] != "failed":
            break
        streak += 1

    if streak >= max_consecutive_failures:
        return (
            True,
            f"{streak} consecutive failed {job_type} runs (>= {max_consecutive_failures})",
            streak,
        )
    return (
        False,
        f"{streak} consecutive failed {job_type} runs (< {max_consecutive_failures})",
        streak,
    )


def _once_daily_due(job_type: str, time_str: str, settings, journal, now: Optional[datetime]) -> tuple[bool, str]:
    """Generic 'due once per SGT calendar day, at/after time_str (HH:MM)' rule
    -- the shared cadence shape behind daily_digest and benchmark_spine,
    parametrized by which settings-driven time-of-day threshold applies."""
    st = timeutils.stamp(now)
    today_sgt = st.local_sgt[:10]
    hh, mm = (int(p) for p in time_str.split(":"))
    now_hhmm = st.local_sgt[11:16]
    if now_hhmm < f"{hh:02d}:{mm:02d}":
        return (False, f"{now_hhmm} SGT is before {job_type} time {time_str}")

    lock_key = default_lock_key(job_type, settings, now)
    existing = journal.count_rows(
        "job_runs",
        "job_type = ? AND lock_key = ? AND status = 'completed'",
        (job_type, lock_key),
    )
    if existing:
        return (False, f"{job_type} already completed today ({today_sgt} SGT)")
    return (True, f"at/after {job_type} time {time_str} SGT, not yet run today")


def _digest_due(settings, journal, now: Optional[datetime]) -> tuple[bool, str]:
    return _once_daily_due(JobType.DAILY_DIGEST, settings.scheduler_digest_time, settings, journal, now)


def _benchmark_spine_due(settings, journal, now: Optional[datetime]) -> tuple[bool, str]:
    return _once_daily_due(
        JobType.BENCHMARK_SPINE, settings.scheduler_benchmark_spine_time, settings, journal, now,
    )


def _text_archive_pull_due(settings, journal, now: Optional[datetime]) -> tuple[bool, str]:
    return _once_daily_due(
        JobType.TEXT_ARCHIVE_PULL, settings.scheduler_text_archive_pull_time, settings, journal, now,
    )


def _atr_update_due(settings, journal, now: Optional[datetime]) -> tuple[bool, str]:
    return _once_daily_due(
        JobType.ATR_UPDATE, settings.scheduler_atr_update_time, settings, journal, now,
    )


def _once_weekly_due(
    job_type: str, weekday: int, time_str: str, settings, journal, now: Optional[datetime],
) -> tuple[bool, str]:
    """Generic 'due once per SGT calendar week, on `weekday` (0=Monday..
    6=Sunday, Python's ``date.weekday()`` convention), at/after time_str
    (HH:MM)' rule -- the weekly-cadence sibling of ``_once_daily_due``,
    sharing its exact lock-key shape (a plain SGT calendar date, see
    ``default_lock_key``): since this can only ever be due on ONE matching
    weekday per week, a date-keyed lock is automatically once-per-week --
    no separate ISO-week key needed."""
    st = timeutils.stamp(now)
    today_sgt_date = st.local_sgt[:10]
    today_weekday = timeutils.parse_iso(st.local_sgt).weekday()
    if today_weekday != weekday:
        return (False, f"today ({today_sgt_date} SGT, weekday={today_weekday}) is not "
                       f"{job_type}'s configured weekday ({weekday})")

    hh, mm = (int(p) for p in time_str.split(":"))
    now_hhmm = st.local_sgt[11:16]
    if now_hhmm < f"{hh:02d}:{mm:02d}":
        return (False, f"{now_hhmm} SGT is before {job_type} time {time_str}")

    lock_key = default_lock_key(job_type, settings, now)
    existing = journal.count_rows(
        "job_runs", "job_type = ? AND lock_key = ? AND status = 'completed'", (job_type, lock_key),
    )
    if existing:
        return (False, f"{job_type} already completed this week ({today_sgt_date} SGT)")
    return (True, f"at/after {job_type} time {time_str} SGT on the configured weekday, not yet run this week")


def _canary_run_due(settings, journal, now: Optional[datetime]) -> tuple[bool, str]:
    return _once_weekly_due(
        JobType.CANARY_RUN, settings.scheduler_canary_run_weekday,
        settings.scheduler_canary_run_time, settings, journal, now,
    )
