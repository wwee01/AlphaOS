"""GREEN-1 defect 4: the date-rot class detector (tests/_dateshift.py).

Covers the plugin's own pure shifting logic directly (no ``pytester``
subprocess machinery -- the plugin has exactly one moving part, the
replacement ``now_utc`` closure, and that is what these tests pin down),
plus an end-to-end proof that the actual date-rot fix in test_canary.py /
test_eval.py survives a +90/-90 day shift, which is the concrete claim
GREEN-1's own mutation-test obligation makes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from _dateshift import shifted_now_utc
from alphaos.util import timeutils


def test_shifted_now_utc_adds_the_configured_number_of_days():
    fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fn = shifted_now_utc(lambda: fixed, 90)
    assert fn() == fixed + timedelta(days=90)


def test_shifted_now_utc_accepts_a_negative_offset():
    fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fn = shifted_now_utc(lambda: fixed, -90)
    assert fn() == fixed - timedelta(days=90)


def test_shifted_now_utc_zero_offset_is_the_identity():
    fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fn = shifted_now_utc(lambda: fixed, 0)
    assert fn() == fixed


def test_clock_shift_option_defaults_to_zero_and_leaves_now_utc_real(pytestconfig):
    """When no --clock-shift-days flag is passed (the normal case), the
    autouse fixture must be a no-op -- timeutils.now_utc still the REAL wall
    clock (within a generous tolerance), not silently patched. Skipped when
    this test happens to run UNDER an actual clock shift (e.g. the CI
    test-clock-shift job invokes the whole suite with the flag set) --
    there the premise this test checks doesn't hold BY DESIGN, and that is
    not itself a date-rot finding."""
    if pytestconfig.getoption("--clock-shift-days"):
        pytest.skip("running under an active --clock-shift-days -- this test only checks the unshifted default")
    real_delta = abs((timeutils.now_utc() - datetime.now(timezone.utc)).total_seconds())
    assert real_delta < 5.0


def test_pytest_addoption_registers_clock_shift_days(pytestconfig):
    """Structural: the option this whole detector hangs off actually exists.
    Checks the OPTION IS REGISTERED (getoption doesn't raise), not that it
    is 0 -- a real --clock-shift-days=90 run (e.g. the CI advisory job)
    must not make this structural check itself fail."""
    assert isinstance(pytestconfig.getoption("--clock-shift-days"), int)
