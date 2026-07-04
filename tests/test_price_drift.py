"""Price-drift gate: a proposal is blocked if price moves more than
MAX_PRICE_DRIFT_BPS_SINCE_PROPOSAL between proposal and approval (§1, §9)."""

from __future__ import annotations

from alphaos.data.freshness_guard import FreshnessGuard
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.strategy.proposal import TradeProposal
from alphaos.util.ids import new_id
from conftest import make_settings


def test_check_price_drift_unit():
    g = FreshnessGuard(max_price_drift_bps=50)
    ok, bps = g.check_price_drift(100.0, 100.40)  # 40 bps
    assert ok is True and bps == 40.0
    ok, bps = g.check_price_drift(100.0, 101.0)   # 100 bps
    assert ok is False and bps == 100.0


def test_approval_blocked_on_material_drift():
    s = make_settings(MAX_PRICE_DRIFT_BPS_SINCE_PROPOSAL="50")
    orch = Orchestrator(settings=s, journal=JournalStore(":memory:"))
    symbol = "AAPL"
    snap = orch.market.get_snapshot(symbol)
    price = float(snap["last_price"])
    # Proposal entry deliberately 2% (200 bps) away from the current mock price.
    skewed_entry = round(price * 1.02, 2)
    cand_id = new_id("cand")
    orch.journal.insert("candidates", {
        "candidate_id": cand_id, "symbol": symbol, "direction": "long",
        "strategy": "swing", "status": "proposed",
    })
    prop = TradeProposal(
        symbol=symbol, direction="long", strategy="swing",
        entry=skewed_entry, stop=round(skewed_entry * 0.97, 2), target=round(skewed_entry * 1.06, 2),
        max_holding_days=3, qty=10, risk_per_share=skewed_entry * 0.03,
        dollar_risk=skewed_entry * 0.03 * 10, expected_r=2.0, same_day_exit_eligible=True,
        candidate_id=cand_id, status="pending_approval",
    )
    orch._stamp_proposal_ttl(prop, snap)  # PR6: fresh by construction, not expired-by-omission
    orch.journal.insert("trade_proposals", prop.to_row())

    ok, msg = orch.approve_proposal(prop.proposal_id, approver="tester")
    assert ok is False
    assert "drift" in msg.lower()
    assert orch.journal.count_rows("paper_fills") == 0
    orch.close()
