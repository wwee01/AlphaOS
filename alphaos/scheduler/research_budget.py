"""Research admission and cooperative checkpoints; never a scan authorization."""

from datetime import timedelta, timezone
import math

from alphaos.scheduler import cadence
from alphaos.util import timeutils
from alphaos.util.market_calendar import is_trading_day

VERSION = "scan_window_budget_v1"
RESERVE_SECONDS = 300
HISTORY_MARGIN_SECONDS = 60


def next_unserved_window(journal, settings, now=None):
    now = now or timeutils.now_utc()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware scheduling clock required")
    local = timeutils.to_et(now)
    # Eight dates include the next ordinary session across a holiday weekend.
    # No work horizon is advertised beyond that bounded search.
    for offset in range(8):
        day = local + timedelta(days=offset)
        if not is_trading_day(day.date()):
            continue
        for start, end in sorted(cadence.scan_windows(settings)):
            sh, sm = map(int, start.split(":"))
            eh, em = map(int, end.split(":"))
            begin = day.replace(hour=sh, minute=sm, second=0, microsecond=0)
            finish = day.replace(hour=eh, minute=em, second=0, microsecond=0)
            if finish <= local:
                continue
            key = f"scan:{day.date().isoformat()}T{start}"
            served = journal.one(
                "SELECT id FROM job_runs WHERE job_type='scan' AND lock_key=? "
                "AND status='completed' LIMIT 1",
                (key,),
            )
            if not served:
                return {"lock_key": key, "start": begin, "end": finish}
    return None


def admission(journal, settings, job_type) -> dict:
    now = timeutils.now_utc()
    window = next_unserved_window(journal, settings, now)
    since = (now - timedelta(days=30)).isoformat()
    maximum = journal.scalar(
        "SELECT MAX(duration_ms) FROM job_runs WHERE job_type=? "
        "AND status IN ('completed','failed','yielded') AND started_at_utc>=?",
        (job_type, since),
    )
    seconds = float(maximum or 0) / 1000
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("invalid recorded research duration")
    estimate = max(RESERVE_SECONDS, seconds + HISTORY_MARGIN_SECONDS)
    deferred = bool(window and now >= window["start"] - timedelta(seconds=estimate))
    return {
        "version": VERSION,
        "allowed": not deferred,
        "reason": "unserved_scan_window_reserved"
        if deferred
        else "research_interval_available",
        "observed_at_utc": now.isoformat(),
        "admission_seconds": estimate,
        "historical_max_seconds": seconds,
        "window_lock_key": window["lock_key"] if window else None,
        "limitation": "Observed duration is not an execution deadline; in-flight calls are not cancelled.",
    }


class ScanWindowBudget:
    """Sticky stop at row/stage boundaries, including a whole-window overrun."""

    def __init__(self, journal, settings):
        self.journal, self.settings = journal, settings
        self.window = next_unserved_window(journal, settings)
        self.last_clock = timeutils.now_utc()
        self.yielded = False

    def __call__(self):
        if self.yielded:
            return True
        now = timeutils.now_utc()
        if now.tzinfo is None or now.utcoffset() is None or now < self.last_clock:
            self.yielded = True
            return True
        self.last_clock = now
        if self.window and now >= self.window["start"] - timedelta(
            seconds=RESERVE_SECONDS
        ):
            served = self.journal.one(
                "SELECT id FROM job_runs WHERE job_type='scan' AND lock_key=? "
                "AND status='completed' LIMIT 1",
                (self.window["lock_key"],),
            )
            if served:
                self.window = next_unserved_window(self.journal, self.settings, now)
                return self()
            # Keep the original boundary even if a single call overran its end;
            # never authorize more background work by simply moving the target.
            self.yielded = True
        return self.yielded

    def as_dict(self):
        return {
            "version": VERSION,
            "yielded": self.yielded,
            "reserve_seconds": RESERVE_SECONDS,
            "window_lock_key": self.window["lock_key"] if self.window else None,
            "yield_before_utc": (
                self.window["start"] - timedelta(seconds=RESERVE_SECONDS)
            )
            .astimezone(timezone.utc)
            .isoformat()
            if self.window
            else None,
            "inflight_call_cancelled": False,
        }
