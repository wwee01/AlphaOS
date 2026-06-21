"""Deferred connectors are not reachable from the active runtime and raise
``deferred in v1`` if called directly (Change Prompt §4, §9)."""

from __future__ import annotations

import pytest

from alphaos.constants import DEFERRED_IN_V1


def test_massive_connector_is_deferred():
    from connectors.deferred.massive import MassiveDataConnector

    with pytest.raises(NotImplementedError) as exc:
        MassiveDataConnector()
    assert str(exc.value) == DEFERRED_IN_V1


def test_benzinga_connector_is_deferred():
    from connectors.deferred.benzinga import BenzingaConnector

    with pytest.raises(NotImplementedError) as exc:
        BenzingaConnector()
    assert str(exc.value) == DEFERRED_IN_V1


def test_web_news_connector_is_deferred():
    from connectors.deferred.web_news import WebNewsConnector

    with pytest.raises(NotImplementedError) as exc:
        WebNewsConnector()
    assert str(exc.value) == DEFERRED_IN_V1


def test_message_is_exactly_deferred_in_v1():
    assert DEFERRED_IN_V1 == "deferred in v1"
