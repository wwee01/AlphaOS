"""Offline EXPLORE-1 policy/lineage tests; no provider, broker or model calls."""

from dataclasses import replace
from datetime import timedelta
import json
import random
from types import SimpleNamespace

import pytest

from alphaos import explore_policy as ep
from alphaos.ai.openai_client import OpenAIEvaluation
from alphaos.constants import ArmingClassification, ReasonCode
from alphaos.execution.order_schema import build_order_row
from alphaos.journal.journal_store import JournalStore
from alphaos.learning.outcomes_tracker import seed_pending_outcomes
from alphaos.orchestrator import Orchestrator, ScanSummary
from alphaos.scanner.scan_context import ScanContext
from alphaos.strategy.proposal import TradeProposal
from alphaos.util import timeutils
from conftest import make_proposal, make_settings


def settings(**changes):
    return make_settings(
        **{
            "ALPHAOS_MODE": "paper",
            "OFFLINE_MODE": "true",
            "EXPLORE_1_ENABLED": "true",
            "MAX_OPEN_POSITIONS": "20",
            "MAX_RISK_PER_TRADE_PCT": ".0025",
            "MAX_AUTO_APPROVALS_PER_DAY": "20",
            "ACTIVE_CARD_ID": "catalyst_momentum_v3",
            **changes,
        }
    )


def candidate(cid="c", symbol="AAPL", **changes):
    return ScanContext(
        {
            "candidate_id": cid,
            "symbol": symbol,
            "direction": "long",
            "strategy": "swing",
            "status": "detected",
            "scan_id": "s",
            "scan_batch_id": "s",
            **changes,
        },
        snapshot={
            "last_price": 100.0,
            "market_session": "regular",
            "volume": 5_000_000,
            "bid": 99.99,
            "ask": 100.01,
        },
    )


def evaluation(cid="c", symbol="AAPL", decision="watch", **changes):
    return replace(
        OpenAIEvaluation(
            "e" + cid,
            cid,
            symbol,
            "mock",
            "long",
            100.0,
            98.0,
            110.0,
            10,
            5.0,
            0.8,
            decision,
            "fixture",
            is_mock=True,
        ),
        **changes,
    )


def label(decision="watch", **changes):
    return SimpleNamespace(
        **{
            "candidate_id": "c",
            "symbol": "AAPL",
            "label_decision": decision,
            "direction": "long",
            "validation_status": "passed",
            **changes,
        }
    )


@pytest.mark.parametrize(
    "a,b,want",
    [
        (
            a,
            b,
            "A+B"
            if a == b == "propose"
            else "A"
            if a == "propose"
            else "B"
            if b == "propose"
            else "",
        )
        for a in ("propose", "watch", "reject")
        for b in ("propose", "watch", "reject")
    ],
)
def test_arm_matrix_and_original_opinions(a, b, want):
    ev = evaluation(decision=a)
    result = ep.route(candidate(), ev, label(b), settings(), 10)
    assert result[0] == want
    assert ev.decision == a and result[1].decision == a
    assert ev is not result[1]


@pytest.mark.parametrize(
    "classification",
    [
        None,
        label(validation_status="fail_safe"),
        label(validation_status="low_confidence"),
        label(direction="short"),
    ],
)
def test_bad_classifier_cannot_select_b_or_c_or_veto_valid_a(classification):
    assert ep.route(candidate(), evaluation(), classification, settings(), 10) is None
    assert (
        ep.route(
            candidate(), evaluation(decision="propose"), classification, settings(), 10
        )[0]
        == "A"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"validation_status": "failed_validation"},
        {"data_freshness_status": "stale"},
        {"direction": "short"},
        {"max_holding_days": 11},
        {"max_holding_days": True},
        {"risk_flags": [ReasonCode.NO_ATR_DATA.value]},
        {"risk_flags": [ReasonCode.REWARD_RISK_TOO_LOW.value]},
        {"risk_flags": [ReasonCode.OPENAI_REJECT.value]},
    ],
)
def test_primary_safety_failure_never_rescued_by_classifier(changes):
    assert (
        ep.route(candidate(), evaluation(**changes), label("propose"), settings(), 10)
        is None
    )


def test_levels_and_horizon_fallback_and_card_bound():
    s = settings(STOP_LOSS_PCT=".02", TARGET_REWARD_RISK="2.5")
    ev = evaluation(entry=None, stop=None, target=None, max_holding_days=None)
    arm, plan = ep.route(candidate(), ev, label("propose"), s, 10)
    assert arm == "B" and (plan.entry, plan.stop, plan.target, plan.stop_source) == (
        100.0,
        98.0,
        105.0,
        "config",
    )
    assert plan.max_holding_days == 10 and ev.max_holding_days is None
    assert ep.route(candidate(), evaluation(), label(), s, 3) is None
    assert ep.route(candidate(shadow_tier=1), ev, label("propose"), s, 10) is None
    bad = candidate()
    bad.snapshot["last_price"] = float("nan")
    assert ep.route(bad, ev, label("propose"), s, 10) is None


def scan(j, sid):
    j.insert("scan_batches", {"scan_batch_id": sid, "status": "started"})


def test_draw_persists_before_reuse_empty_offwindow_and_restart(tmp_path):
    path = str(tmp_path / "j.db")
    j = JournalStore(path)
    for sid in ("first", "second", "empty", "late", "off"):
        scan(j, sid)
    s = settings()
    at = "2026-11-16T14:35:00+00:00"
    chosen = ep.freeze_draw(j, s, "first", at, ["z", "a", "m"])
    original = j.one(
        "SELECT explore_draw_json FROM scan_batches WHERE scan_batch_id='first'"
    )["explore_draw_json"]
    draw = json.loads(original)
    assert chosen == random.Random(draw["seed"]).choice(["a", "m", "z"])
    assert ep.freeze_draw(j, s, "first", at, ["different"]) == chosen
    j.close()
    j = JournalStore(path)
    assert ep.freeze_draw(j, s, "second", at, ["other"]) is None
    assert (
        j.one(
            "SELECT explore_draw_json FROM scan_batches WHERE scan_batch_id='second'"
        )["explore_draw_json"]
        == original
    )
    assert ep.freeze_draw(j, s, "empty", "2026-11-16T17:00:00+00:00", []) is None
    assert ep.freeze_draw(j, s, "late", "2026-11-16T17:01:00+00:00", ["later"]) is None
    assert ep.freeze_draw(j, s, "off", "2026-11-16T16:00:00+00:00", ["outside"]) is None


def tagged(j, arm="A", symbol="AAPL"):
    c = candidate(
        symbol, symbol, arm=arm, policy_version=ep.VERSION, policy_decision="propose"
    )
    j.insert("candidates", c.row)
    p = replace(
        make_proposal(symbol=symbol, candidate_id=symbol),
        arm=arm,
        policy_version=ep.VERSION,
        proposal_expires_at_utc=(timeutils.now_utc() + timedelta(hours=1)).isoformat(),
    )
    j.insert("trade_proposals", p.to_row())
    return p


def test_fairness_counts_pending_orders_overlap_and_expired_pending():
    j = JournalStore(":memory:")
    s = settings()
    p = tagged(j)
    waiting = tagged(j, "B", "MSFT")
    for i in range(9):
        j.insert(
            "positions",
            {
                "position_id": str(i),
                "order_id": str(i),
                "symbol": "Z",
                "direction": "long",
                "qty": 1,
                "avg_entry_price": 100,
                "status": "open",
                "arm": "A+B",
            },
        )
    order = build_order_row(
        order_id="pending",
        proposal=p,
        side="buy",
        order_type="bracket",
        execution_source="test",
        protection_path="broker",
        state="accepted",
        qty=1,
    )
    j.insert("paper_orders", order)
    assert "slot fairness" in ep.entry_refusal(j, s, p)
    j.conn.execute(
        "UPDATE trade_proposals SET proposal_expires_at_utc='2000-01-01T00:00:00+00:00' WHERE proposal_id=?",
        (waiting.proposal_id,),
    )
    j.conn.commit()
    assert ep.entry_refusal(j, s, p) is None
    # A position and its partially filled entry occupy one slot, not two.
    j.conn.execute("UPDATE paper_orders SET order_id='0', state='partially_filled'")
    j.conn.commit()
    waiting = tagged(j, "C", "NVDA")
    assert ep.entry_refusal(j, s, p) is None


@pytest.mark.parametrize(
    "change", ["legacy", "short", "shadow", "nonpaper", "tamper", "off"]
)
def test_final_guard_rejects_wrong_campaign_or_mode(change):
    j = JournalStore(":memory:")
    s = settings()
    p = tagged(j)
    if change == "legacy":
        p.arm = p.policy_version = None
    elif change == "short":
        p.direction = "short"
    elif change == "shadow":
        j.conn.execute("UPDATE candidates SET shadow_tier=1")
        j.conn.commit()
    elif change == "nonpaper":
        s = settings(ALPHAOS_MODE="mock")
    elif change == "off":
        s = settings(EXPLORE_1_ENABLED="false")
    else:
        p.arm = "B"
    assert ep.entry_refusal(j, s, p)


def test_off_default_preserves_legacy_and_nonpaper_scan_refuses_before_work():
    s = make_settings()
    j = JournalStore(":memory:")
    assert not s.explore_1_enabled and ep.entry_refusal(j, s, make_proposal()) is None
    o = Orchestrator(settings=settings(ALPHAOS_MODE="mock"), journal=j)
    with pytest.raises(RuntimeError, match="paper-only"):
        o.run_scan_once()
    assert j.scalar("SELECT count(*) FROM scan_batches") == 0


def test_stage_all_arms_before_auto_consider_and_manual_high_risk(monkeypatch):
    j = JournalStore(":memory:")
    o = Orchestrator(settings=settings(), journal=j)
    scan(j, "s")
    triples = []
    for cid, symbol, a, b in [
        ("a", "AAPL", "propose", "watch"),
        ("b", "MSFT", "watch", "propose"),
        ("ab", "NVDA", "propose", "propose"),
        ("c", "AMZN", "watch", "watch"),
    ]:
        c = candidate(cid, symbol)
        ev = evaluation(cid, symbol, a)
        j.insert("candidates", c.row)
        j.insert("openai_evaluations", ev.to_row())
        triples.append((c, ev, label(b, candidate_id=cid, symbol=symbol)))
    triples[-1][0].polarity = SimpleNamespace(
        arming_classification=ArmingClassification.HIGH_RISK_NARRATIVE.value,
        warning_message="manual",
    )
    seen = []

    def consider(*args, **kwargs):
        rows = j.query(
            "SELECT arm FROM trade_proposals WHERE status='pending_approval'"
        )
        seen.append({x["arm"] for x in rows})
        return SimpleNamespace(approved=False)

    monkeypatch.setattr(o.approvals, "consider", consider)
    o._route_campaign(
        triples, ScanSummary("s"), "s", "2026-11-16T14:35:00+00:00", False
    )
    assert seen and all(x == {"A", "B", "A+B", "C"} for x in seen)
    assert len(seen) == 3  # high-risk C is manual-only, no auto consideration
    seed_pending_outcomes(j)
    assert {x["arm"] for x in j.query("SELECT * FROM candidate_outcomes")} == {
        "A",
        "B",
        "A+B",
        "C",
    }
    assert all(
        x["final_decision"] == "propose" and x["policy_version"] == ep.VERSION
        for x in j.query("SELECT * FROM candidate_outcomes")
    )


def test_lineage_proposal_order_position_and_trade_outcome(monkeypatch):
    j = JournalStore(":memory:")
    o = Orchestrator(settings=make_settings(), journal=j)
    p = tagged(j, "A+B")
    assert TradeProposal.from_row(j.proposal_by_id(p.proposal_id)).arm == "A+B"
    row = build_order_row(
        order_id="o",
        proposal=p,
        side="buy",
        order_type="bracket",
        execution_source="test",
        protection_path="broker",
        state="filled",
        qty=1,
        entry_price=p.entry,
        take_profit_price=p.target,
        stop_loss_price=p.stop,
    )
    j.insert("paper_orders", row)
    posid = o.positions.open_position(row, p.entry)
    pos = j.one("SELECT * FROM positions WHERE position_id=?", (posid,))
    assert (pos["arm"], pos["policy_version"]) == ("A+B", ep.VERSION)
    assert row["arm"] == "A+B"
    o.positions.close_position(posid, p.target, "target")
    out = j.one("SELECT * FROM trade_outcomes WHERE position_id=?", (posid,))
    assert (out["arm"], out["policy_version"]) == ("A+B", ep.VERSION)
    assert all(row["arm"] == "A+B" for row in j.query("SELECT arm FROM paper_orders"))


def test_older_retry_sees_new_opposing_arm_before_capacity_check(monkeypatch):
    j = JournalStore(":memory:")
    o = Orchestrator(settings=settings(), journal=j)
    old = tagged(j, "A", "NFLX")
    o._schedule_entry_retry(old)
    for n in range(10):
        j.insert("positions", {"position_id": str(n), "symbol": f"OLD{n}", "direction": "long",
                               "qty": 1, "avg_entry_price": 100, "status": "open", "arm": "A"})
    scan(j, "current")
    c = candidate("new-b", "MSFT", scan_batch_id="current")
    ev = evaluation("new-b", "MSFT", "watch")
    j.insert("candidates", c.row)
    seen = []
    def retry(pid, **kwargs):
        assert pid == old.proposal_id and kwargs["_auto_retry"]
        assert j.one("SELECT 1 FROM trade_proposals WHERE arm='B' AND status='pending_approval'")
        assert "slot fairness" in ep.entry_refusal(j, o.settings, old)
        seen.append(pid)
        return False, "fairness"
    monkeypatch.setattr(o, "approve_proposal", retry)
    monkeypatch.setattr(o, "_dispatch_proposal", lambda *a: None)
    o._route_campaign([(c, ev, label("propose", candidate_id="new-b", symbol="MSFT"))],
                      ScanSummary("current"), "current", "2026-11-16T14:35:00+00:00", True)
    assert seen == [old.proposal_id]


def test_natural_broker_exit_and_time_exit_orders_inherit_position_tags(
    tmp_path, monkeypatch
):
    from alphaos.execution.time_exit_lifecycle import TimeExitLifecycle
    from alphaos.util import alerts
    from test_execution_lifecycle_repair import setup

    monkeypatch.setattr(alerts, "send_alert", lambda *a, **kw: True)
    fake, _, j, manager, pos, _ = setup(tmp_path)
    j.conn.execute("UPDATE positions SET arm='C', policy_version='explore_1'")
    j.conn.commit()
    pos = j.one("SELECT * FROM positions")
    lifecycle = TimeExitLifecycle(manager)
    row, created = lifecycle._new(pos, "reprotect", "prepared")
    assert created and (row["arm"], row["policy_version"]) == ("C", ep.VERSION)
    row, created = lifecycle._new(pos, "time_exit", "prepared")
    assert created and (row["arm"], row["policy_version"]) == ("C", ep.VERSION)
    fake._broker_qty.pop(pos["symbol"], None)
    norm = {
        "broker_order_id": "natural-stop",
        "state": "filled",
        "symbol": pos["symbol"],
        "side": "sell",
        "filled_qty": 10,
        "filled_avg_price": 94.0,
        "qty": 10,
        "filled_at": timeutils.now_utc().isoformat(),
        "order_type": "stop",
    }
    manager._record_broker_exit(pos, norm, "stop_loss")
    natural = j.one("SELECT * FROM paper_orders WHERE broker_order_id='natural-stop'")
    assert (natural["arm"], natural["policy_version"]) == ("C", ep.VERSION)
    assert j.one("SELECT arm FROM trade_outcomes")["arm"] == "C"


def test_campaign_manual_override_and_demo_cannot_create_untagged_proposals():
    o = Orchestrator(settings=settings(), journal=JournalStore(":memory:"))
    rec = {}
    assert o._override_open_trade(candidate().row, {}, rec, "human").startswith(
        "blocked:"
    )
    assert "arm-routed" in rec["execution_result"]
    with pytest.raises(RuntimeError, match="demo"):
        o.seed_demo()
    assert o.journal.count_rows("trade_proposals") == 0


def test_shadow_measurement_cohort_has_no_order_arm():
    j = JournalStore(":memory:")
    j.insert("candidates", candidate(shadow_tier=1, policy_version=ep.VERSION).row)
    seed_pending_outcomes(j)
    row = j.one("SELECT * FROM candidate_outcomes")
    assert row["policy_version"] == ep.VERSION and row["arm"] is None
    assert not j.count_rows("trade_proposals") and not j.count_rows("paper_orders")


def test_full_scan_stamps_policy_and_keeps_watch_diagnostic(monkeypatch):
    from datetime import datetime, timezone
    import socket

    at = datetime(2026, 11, 16, 14, 35, tzinfo=timezone.utc)
    monkeypatch.setattr(timeutils, "now_utc", lambda: at)
    attempted = []

    def forbidden(*a, **kw):
        attempted.append(True)
        raise AssertionError("no network in campaign fixture")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    s = settings(
        LABELLING_ENABLED="true",
        BASELINE_ENABLED="false",
        NEWS_ENRICHMENT_ENABLED="false",
        LAST30DAYS_ENABLED="false",
        EARNINGS_PROXIMITY_ENABLED="false",
        REGIME_ENABLED="false",
        TQS_SHADOW_ENABLED="false",
        DEBATE_SHADOW_ENABLED="false",
    )
    j = JournalStore(":memory:")
    o = Orchestrator(settings=s, journal=j)
    summary = o.run_scan_once()
    rows = j.query("SELECT * FROM candidates")
    assert rows and all(row["policy_version"] == ep.VERSION for row in rows)
    assert summary.proposed > 0
    assert all(
        row["arm"] in ep.ARMS and row["policy_version"] == ep.VERSION
        for row in j.query("SELECT * FROM trade_proposals")
    )
    assert j.count_rows("decision_adjustments") > 0
    assert j.one("SELECT explore_draw_json FROM scan_batches")["explore_draw_json"]
    assert not attempted and j.count_rows("paper_orders") == 0


def test_risk_block_keeps_arm_and_summary_denominator(monkeypatch):
    from alphaos.risk.risk_engine import RiskDecision

    j = JournalStore(":memory:")
    o = Orchestrator(settings=settings(), journal=j)
    scan(j, "s")
    c = candidate()
    ev = evaluation(decision="propose")
    j.insert("candidates", c.row)
    j.insert("openai_evaluations", ev.to_row())
    monkeypatch.setattr(
        o,
        "_assess_current_risk",
        lambda **kw: RiskDecision(
            approved=False,
            block_reasons=[
                {"code": ReasonCode.DAILY_LOSS_LIMIT.value, "detail": "test"}
            ],
        ),
    )
    summary = ScanSummary("s")
    o._route_campaign(
        [(c, ev, label())], summary, "s", "2026-11-16T14:35:00+00:00", False
    )
    assert summary.rejected == summary.risk_blocked == 1 and summary.proposed == 0
    p = j.one("SELECT * FROM trade_proposals")
    assert (
        p["arm"] == "A"
        and p["policy_version"] == ep.VERSION
        and p["status"] == "blocked"
    )
    seed_pending_outcomes(j)
    row = j.one("SELECT * FROM candidate_outcomes")
    assert row["candidate_type"] == "blocked" and row["arm"] == "A"
