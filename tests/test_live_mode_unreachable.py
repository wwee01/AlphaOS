"""Live mode cannot be enabled / does not exist as a code path (test #5)."""

from __future__ import annotations

import pytest

from alphaos.config.settings import SettingsError
from alphaos.constants import RuntimeMode
from conftest import make_settings


def test_runtime_mode_enum_has_no_live_member():
    assert "live" not in {m.value for m in RuntimeMode}


def test_loading_live_mode_raises():
    with pytest.raises(SettingsError):
        make_settings(ALPHAOS_MODE="live")


def test_unknown_mode_raises():
    with pytest.raises(SettingsError):
        make_settings(ALPHAOS_MODE="real")


def test_paper_and_mock_are_loadable():
    assert make_settings(ALPHAOS_MODE="mock").is_mock
    # paper just needs to parse here; broker safety is validated separately.
    assert make_settings(ALPHAOS_MODE="paper").is_paper
