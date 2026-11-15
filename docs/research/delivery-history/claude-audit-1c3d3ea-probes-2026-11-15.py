"""Claude audit probes on 1c3d3ea (P0 + EXPLORE-1). Offline: fake broker, temp ledgers, frozen clock.

Run:  ALPHAOS_REPO="$PWD" PYTHONPATH="$PWD" .venv/bin/python -m pytest -q -p no:cacheprovider <this file>
"""
import os, sys, json
sys.path.insert(0, os.path.join(os.environ["ALPHAOS_REPO"], "tests"))
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import alphaos
from alphaos import explore_policy as ep
from alphaos.ai.openai_client import OpenAIEvaluation
from alphaos.broker.alpaca_client import AlpacaClient
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator, ScanSummary
from alphaos.scanner.scan_context import ScanContext
from alphaos.util import timeutils
from conftest import make_settings
from test_entry_submission_recovery import RecoverableBroker

assert os.environ["ALPHAOS_REPO"] in alphaos.__file__, alphaos.__file__


class Broker(RecoverableBroker):
    """Fake paper broker. instant=True reproduces the production shape: the POST answers "accepted",
    the marketable limit fills at the broker a moment later (1-5 s in 5 of 10 post-repair entries), and
    the local position is only written by the dedicated monitor 2-13 minutes after that."""
    def __init__(self, instant=True):
        super().__init__()
        self.instant, self.unfilled = instant, []

    def get_account(self):
        while self.unfilled:                 # the previous entry has filled by the time anyone looks again
            self.fill(self.unfilled.pop())
        return {"equity": 95000.0, "last_equity": 95000.0, "cash": 95000.0, "trading_blocked": False, "status": "ACTIVE"}

    def submit(self, spec):
        order = super().submit(spec)         # response status: accepted
        if self.instant:
            self.unfilled.append(order)
        return order

    def fill(self, order):
        order.status, order.filled_qty, order.filled_avg_price = "filled", order.qty, order.limit_price
        order.filled_at = "2026-11-16T14:40:01Z"
        self.set_position(order.symbol, order.qty, avg_entry_price=order.limit_price)
        order.legs[1].status = "held"                       # real Alpaca: stop held, target listed top-level
        self._stray_orders[order.legs[0].id] = order.legs[0]

    def get_orders(self):
        return list(self._stray_orders.values()) + [o for o in self.orders.values() if o.status == "accepted"]


START = datetime(2026, 11, 16, 14, 36, tzinfo=timezone.utc)   # 09:36 ET, inside the first scan window


def harness(tmp_path, monkeypatch, *, instant=True, **over):
    clock = [START]
    monkeypatch.setattr(timeutils, "now_utc", lambda: clock[0])
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: None)
    cfg = {"ALPHAOS_MODE": "paper", "EXECUTION_PROVIDER": "alpaca_paper", "ALPACA_API_KEY": "k",
           "ALPACA_SECRET_KEY": "s", "ALPACA_PAPER": "true", "ALPACA_BASE_URL": "https://paper-api.alpaca.markets",
           "REAL_TRADING_ENABLED": "false", "OFFLINE_MODE": "true", "EXPLORE_1_ENABLED": "true",
           "MAX_OPEN_POSITIONS": "20", "MAX_RISK_PER_TRADE_PCT": ".0025", "MAX_AUTO_APPROVALS_PER_DAY": "20",
           "MAX_UNATTENDED_APPROVALS_PER_DAY": "20", "MAX_PAPER_TRADES_PER_DAY": "20",
           "APPROVAL_MODE": "auto", "REQUIRE_MANUAL_APPROVAL": "false", "ACTIVE_CARD_ID": "catalyst_momentum_v3",
           "ENTRY_ORDER_STALENESS_ENABLED": "false", "TIME_EXIT_ENFORCEMENT_ENABLED": "false",
           # production values (read from the live configuration on 2026-11-15)
           "SCHEDULER_SCAN_WINDOWS": "09:35-09:50,11:30-12:00,15:45-16:00",
           "PROPOSAL_TTL_RTH_SECONDS": "1800", "MAX_PRICE_DRIFT_BPS_SINCE_PROPOSAL": "50"}
    cfg.update(over)
    settings = make_settings(**cfg)
    journal = JournalStore(str(tmp_path / "audit.db"))
    orch = Orchestrator(settings=settings, journal=journal)
    orch.startup()
    fake = Broker(instant)
    orch.orders.alpaca = AlpacaClient(settings, journal, trading_client=fake)
    assert orch.orders.real_paper and orch.orders.broker_connected
    quote = orch.market.get_snapshot          # offline mock quote, pinned to the fixture price (no drift)
    monkeypatch.setattr(orch.market, "get_snapshot",
                        lambda symbol: {**quote(symbol), "last_price": 100.0, "bid": 99.99, "ask": 100.01})
    monitor_ok(journal, clock)
    return orch, journal, fake, clock


def monitor_ok(journal, clock, tag="initial"):
    journal.insert("job_runs", {
        "job_run_id": f"monitor-{tag}", "job_type": "monitor", "lock_key": tag, "status": "completed",
        "started_at_utc": clock[0].isoformat(), "started_at_sgt": timeutils.stamp(clock[0]).local_sgt,
        "finished_at_utc": clock[0].isoformat(), "result_summary_json": {"worker_role": "monitor"}})


def cand(cid, symbol, price=100.0, **changes):
    return ScanContext({"candidate_id": cid, "symbol": symbol, "direction": "long", "strategy": "swing",
                        "status": "detected", "scan_id": "s", "scan_batch_id": "s", **changes},
                       snapshot={"last_price": price, "market_session": "regular", "volume": 5_000_000,
                                 "bid": price - 0.01, "ask": price + 0.01})


def ev(cid, symbol, decision, price=100.0, **changes):
    return replace(OpenAIEvaluation("e" + cid, cid, symbol, "mock", "long", price, price * .98, price * 1.10, 10,
                                    5.0, 0.8, decision, "fixture", is_mock=True), **changes)


def lab(cid, symbol, decision, **changes):
    return SimpleNamespace(**{"candidate_id": cid, "symbol": symbol, "label_decision": decision,
                              "direction": "long", "validation_status": "passed", **changes})


def stage(journal, rows, batch="s"):
    if not journal.one("SELECT 1 FROM scan_batches WHERE scan_batch_id=?", (batch,)):
        journal.insert("scan_batches", {"scan_batch_id": batch, "status": "started"})
    triples = []
    for cid, symbol, a, b in rows:
        c, e = cand(cid, symbol, scan_batch_id=batch), ev(cid, symbol, a)
        journal.insert("candidates", c.row)
        journal.insert("openai_evaluations", e.to_row())
        triples.append((c, e, lab(cid, symbol, b)))
    return triples


ARMS3 = [("c1", "AMZN", "watch", "watch"),     # the only control-eligible candidate -> C
         ("a1", "AAPL", "propose", "watch"),   # A
         ("b1", "MSFT", "watch", "propose")]   # B


def proposals(journal):
    return {r["symbol"]: r for r in journal.query("SELECT * FROM trade_proposals ORDER BY id")}


# ============================================================ F1: same-pass fill starves the rest of the pass
def test_f1_instant_fill_lets_one_entry_through_and_the_rest_expire_before_any_retry(tmp_path, monkeypatch):
    orch, journal, fake, clock = harness(tmp_path, monkeypatch, instant=True)
    summary = ScanSummary("s")
    orch._route_campaign(stage(journal, ARMS3), summary, "s", START.isoformat(), False)
    props = proposals(journal)
    assert {s: p["arm"] for s, p in props.items()} == {"AMZN": "C", "AAPL": "A", "MSFT": "B"}
    # exactly one broker entry; the two later arms are deferred, not blocked
    assert fake.submits == 1 and summary.auto_submitted == 1 and summary.entry_deferred == 2
    assert sorted(p["status"] for p in props.values()) == ["pending_approval", "pending_approval", "submitted"]
    deferred = journal.query("SELECT detail_json FROM system_events WHERE category='entry_deferred'")
    assert len(deferred) == 2
    first = next(s for s, p in props.items() if p["status"] == "submitted")
    assert first == "AMZN"          # candidate order decides which arm trades (here the control, ahead of A and B)
    # production cadence: TTL 1,800 s; the next scan window opens 115 min after this one (09:35 -> 11:30 ET)
    ttl = {p["proposal_ttl_seconds"] for p in props.values()}
    assert ttl == {1800}
    # the dedicated monitor reconciles the fill 8 minutes later (production median) -- nothing retries
    clock[0] = START + timedelta(minutes=8)
    assert not orch.orders.reconcile()["errors"] and journal.count_open_positions() == 1
    monitor_ok(journal, clock, "after-reconcile")
    # next scan (11:31 ET): the only place _retry_deferred_entries is called
    clock[0] = START + timedelta(minutes=115)
    monitor_ok(journal, clock, "next-window")
    retry = ScanSummary("next")
    orch._retry_deferred_entries(retry, unattended=False, exclude_batch_id="next")
    after = proposals(journal)
    assert fake.submits == 1 and retry.auto_submitted == 0
    assert sorted(p["status"] for p in after.values()) == ["expired", "expired", "filled"]
    assert {after["AAPL"]["status"], after["MSFT"]["status"]} == {"expired"}   # arms A and B never traded


def test_f1_control_the_retry_itself_works_when_something_calls_it_inside_the_ttl(tmp_path, monkeypatch):
    orch, journal, fake, clock = harness(tmp_path, monkeypatch, instant=True)
    orch._route_campaign(stage(journal, ARMS3), ScanSummary("s"), "s", START.isoformat(), False)
    assert fake.submits == 1
    clock[0] = START + timedelta(minutes=8)
    assert not orch.orders.reconcile()["errors"]
    monitor_ok(journal, clock, "after-reconcile")
    retry = ScanSummary("retry")
    orch._retry_deferred_entries(retry, unattended=False, exclude_batch_id="retry")
    # one more entry goes in, then its own instant fill defers the third again: one entry per reconcile cycle
    assert fake.submits == 2 and retry.auto_submitted == 1
    assert sorted(p["status"] for p in proposals(journal).values()) == ["filled", "pending_approval", "submitted"]


def test_f1_control_unfilled_entries_do_not_starve_the_pass(tmp_path, monkeypatch):
    orch, journal, fake, _ = harness(tmp_path, monkeypatch, instant=False)
    summary = ScanSummary("s")
    orch._route_campaign(stage(journal, ARMS3), summary, "s", START.isoformat(), False)
    assert fake.submits == 3 and summary.auto_submitted == 3 and summary.entry_deferred == 0


# ============================================================ per-symbol cap on the real exposure read
def test_symbol_cap_real_read_blocks_held_and_pending_symbol_and_consumes_the_control_draw(tmp_path, monkeypatch):
    orch, journal, fake, clock = harness(tmp_path, monkeypatch, instant=False)
    orch._route_campaign(stage(journal, [("a1", "AAPL", "propose", "watch")]), ScanSummary("s"), "s", START.isoformat(), False)
    assert fake.submits == 1                                   # AAPL entry working at the broker (pending)
    # same symbol again in the next window, this time as the only control-eligible candidate
    at = START + timedelta(minutes=115); clock[0] = at; monitor_ok(journal, clock, "w2")
    summary = ScanSummary("s2")
    orch._route_campaign(stage(journal, [("c2", "AAPL", "watch", "watch")], "s2"), summary, "s2", at.isoformat(), False)
    row = journal.one("SELECT * FROM trade_proposals WHERE candidate_id='c2'")
    assert fake.submits == 1 and row["status"] == "blocked" and row["arm"] == "C"
    reasons = json.loads(journal.one("SELECT block_reasons_json FROM risk_checks WHERE proposal_id=?", (row["proposal_id"],))["block_reasons_json"])
    assert any("per-symbol cap" in r["detail"] for r in reasons)
    draw = json.loads(journal.one("SELECT explore_draw_json FROM scan_batches WHERE scan_batch_id='s2'")["explore_draw_json"])
    assert draw["chosen_id"] == "c2"                           # the window's single control draw is spent on a held symbol


# ============================================================ legacy / flag transitions
def test_untagged_pending_proposal_cannot_execute_once_active_and_tagged_cannot_execute_once_inactive(tmp_path, monkeypatch):
    orch, journal, fake, _ = harness(tmp_path, monkeypatch, instant=False, EXPLORE_1_ENABLED="false")
    journal.insert("scan_batches", {"scan_batch_id": "s", "status": "started"})
    c, e = cand("old", "NVDA"), ev("old", "NVDA", "propose")
    journal.insert("candidates", c.row); journal.insert("openai_evaluations", e.to_row())
    orch.approvals.settings = orch.settings = replace(orch.settings, approval_mode="manual", require_manual_approval=True)
    assert orch._handle_proposal(c, e, ScanSummary("s"), scan_batch_id="s")
    legacy = journal.one("SELECT * FROM trade_proposals")
    assert legacy["status"] == "pending_approval" and legacy["arm"] is None and fake.submits == 0
    orch.approvals.settings = orch.settings = replace(orch.settings, explore_1_enabled=True)
    ok, msg = orch.approve_proposal(legacy["proposal_id"])     # even an explicit manual approval
    assert not ok and fake.submits == 0 and journal.count_rows("approvals") == 0
    assert journal.proposal_by_id(legacy["proposal_id"])["status"] == "pending_approval"
    # and the reverse: a tagged campaign proposal is refused after the flag is turned off
    orch.approvals.settings = orch.settings = replace(orch.settings, approval_mode="manual", require_manual_approval=True)
    orch._route_campaign(stage(journal, [("a1", "AAPL", "propose", "watch")], "s3"), ScanSummary("s3"), "s3", START.isoformat(), False)
    tagged = journal.one("SELECT * FROM trade_proposals WHERE symbol='AAPL'")
    assert tagged["arm"] == "A" and tagged["status"] == "pending_approval"
    orch.approvals.settings = orch.settings = replace(orch.settings, explore_1_enabled=False)
    ok, msg = orch.approve_proposal(tagged["proposal_id"])
    assert not ok and fake.submits == 0 and journal.count_rows("approvals") == 0


# ============================================================ control draw
def test_control_draw_is_single_reproducible_and_uniform_over_the_eligible_set(tmp_path, monkeypatch):
    import random
    orch, journal, fake, _ = harness(tmp_path, monkeypatch, instant=False)
    rows = [(f"c{i}", f"S{i}", "watch" if i % 2 else "reject", "watch") for i in range(8)]
    rows += [("a1", "AAPL", "propose", "watch")]
    orch._route_campaign(stage(journal, rows), ScanSummary("s"), "s", START.isoformat(), False)
    draw = json.loads(journal.one("SELECT explore_draw_json FROM scan_batches WHERE scan_batch_id='s'")["explore_draw_json"])
    assert draw["eligible_ids"] == sorted(f"c{i}" for i in range(8))          # evaluator rejects are eligible too
    assert draw["chosen_id"] == random.Random(draw["seed"]).choice(draw["eligible_ids"])
    arms = [r["arm"] for r in journal.query("SELECT arm FROM candidates WHERE arm IS NOT NULL")]
    assert sorted(arms) == ["A", "C"]
    # a second scan in the same window draws nothing
    orch._route_campaign(stage(journal, [("x1", "XOM", "watch", "watch")], "s-again"), ScanSummary("s-again"), "s-again",
                         (START + timedelta(minutes=5)).isoformat(), False)
    assert journal.one("SELECT arm FROM candidates WHERE candidate_id='x1'")["arm"] is None
    # uniformity of the seeded choice across windows (seed = sha256 of date+window; 3,000 synthetic windows)
    from collections import Counter
    import hashlib
    pool = [f"c{i}" for i in range(8)]
    hits = Counter(random.Random(hashlib.sha256(f"explore_1:2026-11-{d}:{w}".encode()).hexdigest()).choice(pool)
                   for d in range(1000) for w in ("09:35", "11:30", "15:45"))
    assert max(hits.values()) / min(hits.values()) < 1.35


# ============================================================ degraded scan: capture only
def test_degraded_scan_under_campaign_makes_no_draw_no_proposal_and_leaves_candidates_untouched(tmp_path, monkeypatch):
    orch, journal, fake, _ = harness(tmp_path, monkeypatch, instant=False)
    c = cand("d1", "AAPL")
    monkeypatch.setattr(orch.scanner, "scan", lambda **kw: SimpleNamespace(scan_id="deg", candidates=[c]))
    monkeypatch.setattr(orch.openai, "evaluate", lambda *a, **k: pytest.fail("degraded scan called the evaluator"))
    orch.run_scan_once(ai_degraded=True)
    assert fake.submits == 0 and journal.count_rows("trade_proposals") == 0
    assert journal.one("SELECT COUNT(*) n FROM scan_batches WHERE explore_draw_json IS NOT NULL")["n"] == 0


# ============================================================ fairness
def test_fairness_defers_the_eleventh_slot_of_an_arm_only_while_another_arm_waits(tmp_path, monkeypatch):
    orch, journal, fake, _ = harness(tmp_path, monkeypatch, instant=False)
    for i in range(10):
        journal.insert("positions", {"position_id": f"p{i}", "order_id": f"o{i}", "symbol": f"H{i}", "direction": "long",
                                     "qty": 1, "avg_entry_price": 100, "status": "open", "arm": "C", "policy_version": "explore_1"})
    journal.insert("candidates", cand("cx", "AMZN", arm="C", policy_version="explore_1").row)
    journal.insert("candidates", cand("ax", "AAPL", arm="A", policy_version="explore_1").row)
    from conftest import make_proposal
    mk = lambda cid, sym, arm: replace(make_proposal(symbol=sym, candidate_id=cid), arm=arm, policy_version="explore_1",
                                       proposal_expires_at_utc=(START + timedelta(minutes=30)).isoformat())
    pc, pa = mk("cx", "AMZN", "C"), mk("ax", "AAPL", "A")
    journal.insert("trade_proposals", pc.to_row())
    assert ep.entry_refusal(journal, orch.settings, pc) is None            # nobody else waiting: C may take slot 11
    journal.insert("trade_proposals", pa.to_row())
    assert "slot fairness" in ep.entry_refusal(journal, orch.settings, pc)  # A waiting: C's 11th slot is refused
    assert ep.entry_refusal(journal, orch.settings, pa) is None             # A itself is free to enter


# ============================================================ lineage: proposal -> order -> position -> exit -> outcome -> learning
def test_arm_lineage_survives_broker_fill_natural_exit_and_candidate_outcome_seeding(tmp_path, monkeypatch):
    from alphaos.learning.outcomes_tracker import seed_pending_outcomes
    orch, journal, fake, clock = harness(tmp_path, monkeypatch, instant=False)
    rows = [("c1", "AMZN", "watch", "watch"), ("c2", "XOM", "reject", "watch"), ("b1", "MSFT", "watch", "propose")]
    orch._route_campaign(stage(journal, rows), ScanSummary("s"), "s", START.isoformat(), False)
    draw = json.loads(journal.one("SELECT explore_draw_json FROM scan_batches WHERE scan_batch_id='s'")["explore_draw_json"])
    chosen = draw["chosen_id"]; other = ({"c1", "c2"} - {chosen}).pop()
    assert draw["eligible_ids"] == ["c1", "c2"] and fake.submits == 2            # B + the drawn control
    for order in list(fake.orders.values()):
        fake.fill(order)
    assert not orch.orders.reconcile()["errors"]
    pos = {p["symbol"]: p for p in journal.open_positions()}
    assert pos["MSFT"]["arm"] == "B" and pos["MSFT"]["policy_version"] == "explore_1"
    control_symbol = "AMZN" if chosen == "c1" else "XOM"
    assert pos[control_symbol]["arm"] == "C"
    fake.fill_leg("MSFT", "take_profit", 110.0); fake.remove_position("MSFT")
    fake._stray_orders.pop(fake._find("MSFT").legs[0].id, None)
    assert not orch.orders.reconcile()["errors"]
    out = journal.one("SELECT * FROM trade_outcomes")
    assert out["symbol"] == "MSFT" and out["arm"] == "B" and out["policy_version"] == "explore_1"
    exit_order = journal.one("SELECT * FROM paper_orders WHERE order_id LIKE 'exit_%'")
    assert exit_order["arm"] == "B" and exit_order["policy_version"] == "explore_1"
    seed_pending_outcomes(journal)
    co = {r["candidate_id"]: r for r in journal.query("SELECT * FROM candidate_outcomes")}
    assert co["b1"]["arm"] == "B" and co["b1"]["final_decision"] == "propose" and co["b1"]["eval_decision"] == "watch"
    assert co[chosen]["arm"] == "C" and co[chosen]["final_decision"] == "propose"
    assert co[other]["arm"] is None and co[other]["policy_version"] == "explore_1"   # undrawn pool member stays measurable
    assert co[other]["final_decision"] in ("watch", "reject")
    # level provenance is on the proposal
    for p in journal.query("SELECT arm, stop_price_source, target_price_source, proposal_reason FROM trade_proposals"):
        assert p["proposal_reason"].startswith(f"EXPLORE-1 {p['arm']}; levels=")
