"""A crash before the incident write still reserves uncertain entry exposure."""

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.risk.account_exposure import ExposureUnavailable, read_exposure


@pytest.mark.parametrize("phase", ["submitting", "unknown", "reconciling"])
def test_unresolved_intent_blocks_exposure_before_any_broker_read(phase):
    journal = JournalStore(":memory:")
    journal.insert("paper_orders", {
        "order_id": "intent", "client_order_id": "stable-proposal-id", "symbol": "XYZ",
        "order_type": "bracket", "execution_source": "alpaca_paper", "state": "submitted",
        "raw_request_json": {"purpose": "paper_entry", "phase": phase},
    })
    class Broker:
        def get_account(self):
            pytest.fail("Uncertain exposure must block before asking broker about capacity")
    try:
        assert journal.count_rows("protection_checks") == 0
        with pytest.raises(ExposureUnavailable, match="submission remains unresolved"):
            read_exposure(Broker(), journal)
    finally:
        journal.close()


def test_unreadable_intent_ledger_never_becomes_verified_capacity():
    class BrokenJournal:
        def query(self, *args):
            raise RuntimeError("unreadable")
    with pytest.raises(ExposureUnavailable, match="intent state cannot be verified"):
        read_exposure(object(), BrokenJournal())
