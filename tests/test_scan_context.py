"""ScanContext (structural fix for the exit-review T5 finding): dict-delegation
for real candidate columns, typed attributes for transient enrichment objects,
and a hard guard against ever re-introducing the ``_``-prefixed side-channel
that leaked catalyst/narrative text into the no-news prompt (PR9.1). Hermetic.
"""

from __future__ import annotations

import json

import pytest

from alphaos.scanner.scan_context import ScanContext


def _row():
    return {"candidate_id": "cand_1", "symbol": "AAPL", "direction": "long", "momentum_score": 0.5}


def test_dict_delegation_for_real_columns():
    ctx = ScanContext(row=_row())

    assert ctx["symbol"] == "AAPL"
    assert ctx.get("symbol") == "AAPL"
    assert ctx.get("missing", "default") == "default"
    assert "symbol" in ctx
    assert "missing" not in ctx

    ctx["status"] = "watch"
    assert ctx.row["status"] == "watch"
    assert dict(ctx.items()) == ctx.row


def test_row_stays_a_plain_dict_safe_to_serialize():
    ctx = ScanContext(row=_row())
    ctx["interest_score"] = 0.8

    # row is exactly what candidate_scanner writes to the DB -- always JSON-safe.
    dumped = json.dumps(ctx.row)
    assert "AAPL" in dumped


def test_setitem_rejects_underscore_keys():
    """The structural guard: this is what makes the PR9.1 leak class of bug
    impossible now, not just discouraged by convention."""
    ctx = ScanContext(row=_row())

    with pytest.raises(ValueError):
        ctx["_sneaky"] = {"narrative": "should never reach row"}

    assert "_sneaky" not in ctx.row


def test_typed_attributes_are_independent_of_row():
    ctx = ScanContext(row=_row())
    snapshot = {"last_price": 100.0}
    ctx.snapshot = snapshot
    ctx.interest = object()
    ctx.catalyst = {"catalyst_type": "earnings_beat"}
    ctx.last30 = {"narrative": "retail is euphoric"}
    ctx.polarity = {"sentiment_label": "bullish"}
    ctx.earnings = {"days_until_earnings": 3}
    ctx.packet_id = "pkt_1"
    ctx.arming_classification = "high_risk_narrative"
    ctx.narrative_warning = "meme-driven move"

    assert ctx.snapshot is snapshot
    # None of the typed attributes ever leak into row -- the only thing that
    # gets serialized when someone does json.dumps(ctx.row) or ctx.items().
    for key in (
        "snapshot", "interest", "catalyst", "last30", "polarity",
        "earnings", "packet_id", "arming_classification", "narrative_warning",
    ):
        assert key not in ctx.row
    assert "catalyst_type" not in json.dumps(ctx.row)


def test_typed_attributes_default_to_none():
    ctx = ScanContext(row=_row())
    assert ctx.snapshot is None
    assert ctx.interest is None
    assert ctx.catalyst is None
    assert ctx.last30 is None
    assert ctx.polarity is None
    assert ctx.earnings is None
    assert ctx.packet_id is None
    assert ctx.arming_classification is None
    assert ctx.narrative_warning is None
