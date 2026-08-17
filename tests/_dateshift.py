"""GREEN-1 defect 4: the date-rot class detector (the seven-lens review's
§H.1 recurring flake -- 233c74b, 6ffbc2d, cd057ce, and this same spec's own
GREEN-1 defect 1 re-seeds of test_canary.py / test_eval.py).

A test that stamps a fixture row with a HARDCODED date and then asserts
against a *trailing* window (``cost_guard.calls_in_last_30_days`` and
friends) passes only until the calendar rolls past that window -- then it
goes permanently red with zero code change, discovered whenever someone
next happens to run the suite. This has recurred four times because nothing
catches it BEFORE the calendar does.

``alphaos.util.timeutils.now_utc()`` is verified to be the codebase's ONE
clock: every date-sensitive helper in ``alphaos/util/timeutils.py`` itself
(``stamp()``, ``age_seconds()``, ``market_session()``, ``market_date()``)
calls the bare name ``now_utc()``, which resolves through the module's own
globals -- the SAME object this plugin patches via
``monkeypatch.setattr(timeutils, "now_utc", ...)`` -- so patching the one
module attribute transparently shifts every derived date/time helper too,
with no second clock anywhere left un-shifted to grep for.

Usage: ``pytest --clock-shift-days=90`` runs the WHOLE suite as if 90 days
had already passed, without touching the real system clock. A dedicated,
non-blocking CI job (``test-clock-shift`` in .github/workflows/ci.yml) runs
this nightly-equivalent check on every push; a red result there is a
"hardcoded date about to age out" alarm to investigate, never a merge
blocker on its own.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from alphaos.util import timeutils


def pytest_addoption(parser: "pytest.Parser") -> None:
    parser.addoption(
        "--clock-shift-days",
        action="store",
        default=0,
        type=int,
        help=(
            "Shift alphaos.util.timeutils.now_utc() forward (or backward, "
            "with a negative value) by N days for the whole test session -- "
            "the GREEN-1 date-rot class detector. Default 0 (no shift, "
            "real clock)."
        ),
    )


def shifted_now_utc(real_now_utc, days: int):
    """Pure: builds the replacement ``now_utc`` callable. Factored out of
    the fixture below so it can be unit-tested (tests/test_dateshift.py)
    without going through pytest's own plugin/option machinery."""
    def _now_utc():
        return real_now_utc() + timedelta(days=days)
    return _now_utc


@pytest.fixture(autouse=True)
def _clock_shift(request: "pytest.FixtureRequest", monkeypatch: "pytest.MonkeyPatch") -> None:
    days = request.config.getoption("--clock-shift-days")
    if not days:
        return
    monkeypatch.setattr(timeutils, "now_utc", shifted_now_utc(timeutils.now_utc, days))
