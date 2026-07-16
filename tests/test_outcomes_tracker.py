"""Counterfactual outcome tracker orchestration (Fable 5 review PR2): seeding
candidate_outcomes rows from candidates/proposals/rejects/armed-watch/
user-overrides, and resolving pending rows with forward returns + bracket
replay. Hermetic; every scenario is built from explicit fixture rows (not the
mock scanner's RNG) so each candidate_type is exercised deterministically.
Also proves: seeding/updating never mutates source tables, never
submits/approves an order, and never touches the real-money guard."""

from __future__ import annotations

from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.util.ids import new_id
from conftest import make_settings, inject_pending_proposal

from alphaos.learning.outcomes_tracker import seed_pending_outcomes, update_pending_outcomes


def _orch(**over):
    return Orchestrator(settings=make_settings(**over), journal=JournalStore(":memory:"))


def _candidate(o, symbol="AAPL", armed_watch=0, **over):
    cand_id = new_id("cand")
    row = {
        "candidate_id": cand_id, "symbol": symbol, "direction": "long", "strategy": "swing",
        "momentum_score": 0.7, "status": "watch", "armed_watch": armed_watch,
        "scan_id": "scan_x", "scan_batch_id": "scanb_x", "playbook_name": "momentum",
    }
    row.update(over)
    o.journal.insert("candidates", row)
    return cand_id


def _eval(o, cand_id, symbol="AAPL", entry=100.0, stop=95.0, target=112.0, decision="propose"):
    o.journal.insert("openai_evaluations", {
        "eval_id": new_id("eval"), "candidate_id": cand_id, "symbol": symbol, "model": "mock",
        "direction": "long", "entry": entry, "stop": stop, "target": target,
        "max_holding_days": 5, "expected_r": 2.0, "confidence": 0.7, "decision": decision,
        "reasoning_summary": "t", "is_mock": 1,
    })


def _proposal(o, cand_id, symbol="AAPL", entry=100.0, stop=95.0, target=112.0, status="pending_approval"):
    o.journal.insert("trade_proposals", {
        "proposal_id": new_id("prop"), "candidate_id": cand_id, "symbol": symbol,
        "direction": "long", "strategy": "swing", "entry": entry, "stop": stop, "target": target,
        "max_holding_days": 5, "qty": 10, "risk_per_share": entry - stop,
        "dollar_risk": (entry - stop) * 10, "expected_r": 2.0, "status": status,
        "playbook_name": "momentum",
    })


def _reject(o, cand_id, symbol="AAPL", would_be_entry=100.0, would_be_stop=95.0):
    o.journal.insert("rejected_candidates", {
        "rejection_id": new_id("rej"), "candidate_id": cand_id, "symbol": symbol,
        "stage": "risk_gate", "reason_code": "wide_spread", "direction": "long",
        "would_be_entry": would_be_entry, "would_be_stop": would_be_stop,
    })


# ------------------------------------------------------------------------ seed
def test_seed_creates_proposal_row():
    o = _orch()
    cid = _candidate(o)
    _eval(o, cid)
    _proposal(o, cid, status="pending_approval")
    res = seed_pending_outcomes(o.journal)
    assert res["proposal"] == 1 and res["total"] == 1
    row = o.journal.one("SELECT * FROM candidate_outcomes WHERE candidate_id = ?", (cid,))
    assert row["candidate_type"] == "proposal"
    assert row["entry_reference_price"] == 100.0 and row["stop_price"] == 95.0 and row["target_price"] == 112.0
    assert row["outcome_status"] == "pending"
    o.close()


def test_seed_creates_blocked_row():
    o = _orch()
    cid = _candidate(o)
    _eval(o, cid)
    _proposal(o, cid, status="blocked")
    res = seed_pending_outcomes(o.journal)
    assert res["blocked"] == 1
    row = o.journal.one("SELECT * FROM candidate_outcomes WHERE candidate_id = ?", (cid,))
    assert row["candidate_type"] == "blocked"
    o.close()


def test_seed_creates_armed_watch_row():
    o = _orch()
    cid = _candidate(o, armed_watch=1)
    _eval(o, cid, decision="watch")
    res = seed_pending_outcomes(o.journal)
    assert res["armed_watch"] == 1
    row = o.journal.one("SELECT * FROM candidate_outcomes WHERE candidate_id = ?", (cid,))
    assert row["candidate_type"] == "armed_watch" and row["armed_watch"] == 1
    o.close()


def test_seed_creates_reject_row_with_eval_levels():
    o = _orch()
    cid = _candidate(o)
    _eval(o, cid, decision="reject")
    _reject(o, cid)
    res = seed_pending_outcomes(o.journal)
    assert res["reject"] == 1
    row = o.journal.one("SELECT * FROM candidate_outcomes WHERE candidate_id = ?", (cid,))
    assert row["candidate_type"] == "reject"
    assert row["target_price"] == 112.0   # sourced from eval (has full E/S/T), not the reject row


def test_seed_creates_reject_row_without_eval_falls_back_to_would_be_levels():
    o = _orch()
    cid = _candidate(o)
    _reject(o, cid, would_be_entry=50.0, would_be_stop=48.0)   # no eval row at all
    res = seed_pending_outcomes(o.journal)
    assert res["reject"] == 1
    row = o.journal.one("SELECT * FROM candidate_outcomes WHERE candidate_id = ?", (cid,))
    assert row["entry_reference_price"] == 50.0 and row["stop_price"] == 48.0
    assert row["target_price"] is None   # would_be levels have no target
    o.close()


def test_seed_creates_candidate_catchall_row():
    o = _orch()
    cid = _candidate(o)
    _eval(o, cid, decision="watch")
    # No proposal, no armed_watch, no reject row -> catch-all.
    res = seed_pending_outcomes(o.journal)
    assert res["candidate"] == 1
    row = o.journal.one("SELECT * FROM candidate_outcomes WHERE candidate_id = ?", (cid,))
    assert row["candidate_type"] == "candidate"
    o.close()


def test_seed_creates_user_override_row_from_resulting_proposal():
    o = _orch()
    pid, entry = inject_pending_proposal(o, symbol="AAPL")
    cid = o.journal.proposal_by_id(pid)["candidate_id"]
    res_ov = o.create_user_override(cid, "watch_to_trade", reason_code="strong_conviction")
    assert res_ov["ok"]
    res = seed_pending_outcomes(o.journal)
    assert res["user_override"] == 1
    row = o.journal.one("SELECT * FROM candidate_outcomes WHERE candidate_type = 'user_override'")
    assert row["user_override"] == 1
    # original_decision = AlphaOS's frozen call; final_decision = the user's own
    # call — the pair a future ΔR comparison needs, sourced from DIFFERENT
    # columns on user_decision_overrides (never rewriting AlphaOS's original).
    assert row["original_decision"] == "propose"    # alphaos_final_decision
    assert row["final_decision"] == "propose"        # user_final_decision (watch_to_trade -> propose)
    assert row["eval_decision"] == "propose" and row["label_decision"] is None
    o.close()


def test_seed_user_override_falls_back_to_eval_levels_without_proposal():
    o = _orch()
    cid = _candidate(o)
    _eval(o, cid, decision="watch")
    o.journal.insert("user_decision_overrides", {
        "override_id": new_id("ovr"), "candidate_id": cid, "proposal_id": None, "symbol": "AAPL",
        "alphaos_eval_decision": "watch", "alphaos_label_decision": "watch",
        "alphaos_final_decision": "watch", "user_override_action": "manual_hold",
        "user_final_decision": "hold", "execution_allowed": 0, "outcome_status": "pending",
        "attribution_result": "pending",
    })
    res = seed_pending_outcomes(o.journal)
    assert res["user_override"] == 1
    row = o.journal.one("SELECT * FROM candidate_outcomes WHERE candidate_type = 'user_override'")
    assert row["entry_reference_price"] == 100.0   # sourced from eval, no proposal existed
    o.close()


def test_seed_is_idempotent():
    o = _orch()
    cid = _candidate(o)
    _eval(o, cid)
    _proposal(o, cid)
    first = seed_pending_outcomes(o.journal)
    assert first["total"] == 1
    second = seed_pending_outcomes(o.journal)
    assert second["total"] == 0
    assert o.journal.count_rows("candidate_outcomes") == 1
    o.close()


def test_seed_never_mutates_source_tables():
    o = _orch()
    cid = _candidate(o)
    _eval(o, cid)
    _proposal(o, cid)
    before = {
        "candidates": o.journal.count_rows("candidates"),
        "trade_proposals": o.journal.count_rows("trade_proposals"),
        "openai_evaluations": o.journal.count_rows("openai_evaluations"),
    }
    seed_pending_outcomes(o.journal)
    after = {
        "candidates": o.journal.count_rows("candidates"),
        "trade_proposals": o.journal.count_rows("trade_proposals"),
        "openai_evaluations": o.journal.count_rows("openai_evaluations"),
    }
    assert before == after
    o.close()


def test_seed_never_submits_order_or_approval():
    o = _orch()
    cid = _candidate(o)
    _eval(o, cid)
    _proposal(o, cid)
    seed_pending_outcomes(o.journal)
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_rows("approvals") == 0
    assert o.journal.count_open_positions() == 0
    h = o.system_health()
    assert h["real_money_trading"] == "unreachable"
    o.close()


# --------------------------------------------------- decision_at_utc sourcing
# (Opus audit HIGH-1: forward windows must anchor on the SOURCE row's own
# decision timestamp, not on when the candidate_outcomes row got seeded.)
def test_decision_at_utc_sourced_from_proposal_not_candidate():
    o = _orch()
    cid = _candidate(o)
    o.journal.conn.execute(
        "UPDATE candidates SET created_at_utc = '2020-01-01T00:00:00+00:00' WHERE candidate_id = ?", (cid,))
    _eval(o, cid)
    _proposal(o, cid)   # proposal's created_at_utc is "now" (real stamp), distinct from the candidate's
    o.journal.conn.commit()
    seed_pending_outcomes(o.journal)
    row = o.journal.one("SELECT decision_at_utc FROM candidate_outcomes WHERE candidate_id = ?", (cid,))
    assert row["decision_at_utc"] is not None
    assert not row["decision_at_utc"].startswith("2020-01-01")   # sourced from the proposal, not the stale candidate
    o.close()


def test_decision_at_utc_sourced_from_reject_row():
    o = _orch()
    cid = _candidate(o)
    _eval(o, cid, decision="reject")
    _reject(o, cid)
    seed_pending_outcomes(o.journal)
    row = o.journal.one("SELECT co.decision_at_utc AS ts, rc.created_at_utc AS rej_ts "
                        "FROM candidate_outcomes co JOIN rejected_candidates rc ON rc.candidate_id = co.candidate_id "
                        "WHERE co.candidate_id = ?", (cid,))
    assert row["ts"] == row["rej_ts"]
    o.close()


def test_decision_at_utc_sourced_from_candidate_for_catchall():
    o = _orch()
    cid = _candidate(o)
    _eval(o, cid, decision="watch")
    seed_pending_outcomes(o.journal)
    row = o.journal.one("SELECT co.decision_at_utc AS ts, c.created_at_utc AS cand_ts "
                        "FROM candidate_outcomes co JOIN candidates c ON c.candidate_id = co.candidate_id "
                        "WHERE co.candidate_id = ?", (cid,))
    assert row["ts"] == row["cand_ts"]
    o.close()


def test_decision_at_utc_sourced_from_override_not_original_candidate():
    """The user_override row's decision_at_utc is when the USER acted, not
    when the original candidate was scanned — that's the decision this row's
    forward outcome actually measures."""
    o = _orch()
    pid, entry = inject_pending_proposal(o, symbol="AAPL")
    cid = o.journal.proposal_by_id(pid)["candidate_id"]
    o.journal.conn.execute(
        "UPDATE candidates SET created_at_utc = '2020-01-01T00:00:00+00:00' WHERE candidate_id = ?", (cid,))
    o.journal.conn.commit()
    o.create_user_override(cid, "watch_to_trade", reason_code="strong_conviction")
    seed_pending_outcomes(o.journal)
    row = o.journal.one(
        "SELECT decision_at_utc FROM candidate_outcomes WHERE candidate_type = 'user_override'")
    assert row["decision_at_utc"] is not None
    assert not row["decision_at_utc"].startswith("2020-01-01")   # sourced from the override, not the old candidate
    o.close()


# ---------------------------------------------------------------------- update
class _FakeBars:
    def __init__(self, bars_by_symbol):
        self.bars_by_symbol = bars_by_symbol

    def get_daily_bars(self, symbol, start, end):
        return self.bars_by_symbol.get(symbol, [])


def _seeded_row(o, entry=100.0, stop=95.0, target=112.0, symbol="AAPL"):
    cid = _candidate(o, symbol=symbol)
    _eval(o, cid, symbol=symbol, entry=entry, stop=stop, target=target)
    _proposal(o, cid, symbol=symbol, entry=entry, stop=stop, target=target)
    seed_pending_outcomes(o.journal)
    return o.journal.one("SELECT * FROM candidate_outcomes WHERE candidate_id = ?", (cid,))


def test_update_resolves_forward_returns_and_replay_end_to_end():
    o = _orch()
    row = _seeded_row(o)
    created_date = row["created_at_utc"][:10]
    import datetime
    d0 = datetime.date.fromisoformat(created_date)
    bars = [{"date": (d0 + datetime.timedelta(days=i)).isoformat(),
            "open": 100, "high": 101, "low": 99, "close": 100.5} for i in range(1, 3)]
    bars.append({"date": (d0 + datetime.timedelta(days=3)).isoformat(),
                "open": 100, "high": 113, "low": 100, "close": 112})   # target hit
    for i in range(4, 6):
        bars.append({"date": (d0 + datetime.timedelta(days=i)).isoformat(),
                    "open": 112, "high": 113, "low": 111, "close": 112})
    res = update_pending_outcomes(o.journal, bars_provider=_FakeBars({"AAPL": bars}))
    assert res["updated"] == 1 and res["completed"] == 1
    updated = o.journal.one("SELECT * FROM candidate_outcomes WHERE outcome_id = ?", (row["outcome_id"],))
    assert updated["outcome_status"] == "complete"
    assert updated["replay_result"] == "target_hit"
    assert updated["forward_5d_r"] is not None
    o.close()


# ------------------------------------------------------- market-adjusted return (EVID-1)
def _insert_benchmark_bar(o, bar_date, close, symbol="SPY"):
    o.journal.insert("benchmark_bars", {
        "bar_id": new_id("bench"), "symbol": symbol, "bar_date": bar_date, "close": close,
    })


def test_update_computes_market_adjusted_return_against_benchmark_bars():
    """market_adjusted_return_1d_pct = candidate's own 1-day return minus
    SPY's own 1-day return over the identical forward window, both anchored
    on the same decision-date reference close."""
    o = _orch()
    row = _seeded_row(o)   # entry=100, long
    created_date = row["created_at_utc"][:10]
    import datetime
    d0 = datetime.date.fromisoformat(created_date)
    day1 = (d0 + datetime.timedelta(days=1)).isoformat()

    bars = [{"date": day1, "open": 100, "high": 103, "low": 99, "close": 102}]   # AAPL: +2%
    _insert_benchmark_bar(o, created_date, 400.0)   # SPY reference
    _insert_benchmark_bar(o, day1, 404.0)            # SPY: +1%

    res = update_pending_outcomes(o.journal, bars_provider=_FakeBars({"AAPL": bars}))
    assert res["updated"] == 1
    updated = o.journal.one("SELECT * FROM candidate_outcomes WHERE outcome_id = ?", (row["outcome_id"],))
    assert updated["forward_1d_return_pct"] == 0.02
    assert updated["market_adjusted_return_1d_pct"] == round(0.02 - 0.01, 4)
    o.close()


def test_update_market_adjusted_return_none_without_benchmark_bars():
    """No benchmark_bars rows at all -> market_adjusted_return_*d_pct stay
    None, but the candidate's own forward return is unaffected."""
    o = _orch()
    row = _seeded_row(o)
    created_date = row["created_at_utc"][:10]
    import datetime
    d0 = datetime.date.fromisoformat(created_date)
    bars = [{"date": (d0 + datetime.timedelta(days=1)).isoformat(),
            "open": 100, "high": 103, "low": 99, "close": 102}]
    update_pending_outcomes(o.journal, bars_provider=_FakeBars({"AAPL": bars}))
    updated = o.journal.one("SELECT * FROM candidate_outcomes WHERE outcome_id = ?", (row["outcome_id"],))
    assert updated["forward_1d_return_pct"] == 0.02
    assert updated["market_adjusted_return_1d_pct"] is None
    o.close()


def test_update_market_adjusted_return_none_when_benchmark_window_lags():
    """The candidate's own 5-day window fully resolves (row goes 'complete'
    and will never be revisited again), but only 3 SPY forward bars exist --
    market_adjusted_return_5d_pct must stay None rather than silently
    comparing a full 5-day candidate return against a partial 3-day
    benchmark return under the '5d' name. The 3d figure, which the
    benchmark DOES fully cover, is unaffected."""
    o = _orch()
    row = _seeded_row(o)
    created_date = row["created_at_utc"][:10]
    import datetime
    d0 = datetime.date.fromisoformat(created_date)
    bars = [{"date": (d0 + datetime.timedelta(days=i)).isoformat(),
            "open": 100, "high": 101, "low": 99, "close": 100.2} for i in range(1, 6)]
    _insert_benchmark_bar(o, created_date, 400.0)
    for i in range(1, 4):   # only 3 of the 5 forward SPY bars
        _insert_benchmark_bar(o, (d0 + datetime.timedelta(days=i)).isoformat(), 400.0 + i)

    res = update_pending_outcomes(o.journal, bars_provider=_FakeBars({"AAPL": bars}))
    assert res["completed"] == 1
    updated = o.journal.one("SELECT * FROM candidate_outcomes WHERE outcome_id = ?", (row["outcome_id"],))
    assert updated["outcome_status"] == "complete"
    assert updated["market_adjusted_return_3d_pct"] is not None
    assert updated["market_adjusted_return_5d_pct"] is None
    o.close()


# ------------------------------------------------------------ time-to-excursion (EVID-1)
def test_update_stores_bars_to_favorable_and_adverse():
    o = _orch()
    row = _seeded_row(o, entry=100.0, stop=90.0, target=200.0)   # target far away: no replay noise
    created_date = row["created_at_utc"][:10]
    import datetime
    d0 = datetime.date.fromisoformat(created_date)
    bars = [
        {"date": (d0 + datetime.timedelta(days=1)).isoformat(), "open": 100, "high": 102, "low": 99, "close": 101},
        {"date": (d0 + datetime.timedelta(days=2)).isoformat(), "open": 101, "high": 108, "low": 100, "close": 107},
        {"date": (d0 + datetime.timedelta(days=3)).isoformat(), "open": 107, "high": 105, "low": 96, "close": 104},
        {"date": (d0 + datetime.timedelta(days=4)).isoformat(), "open": 104, "high": 106, "low": 103, "close": 105},
        {"date": (d0 + datetime.timedelta(days=5)).isoformat(), "open": 105, "high": 106, "low": 104, "close": 105},
    ]
    update_pending_outcomes(o.journal, bars_provider=_FakeBars({"AAPL": bars}))
    updated = o.journal.one("SELECT * FROM candidate_outcomes WHERE outcome_id = ?", (row["outcome_id"],))
    assert updated["bars_to_favorable_5d"] == 2   # bar 2's high=108 is the best across the 5-day window
    assert updated["bars_to_adverse_5d"] == 3     # bar 3's low=96 is the worst across the 5-day window
    o.close()


def test_update_with_no_provider_skips_safely_and_stays_pending():
    o = _orch()
    row = _seeded_row(o)
    res = update_pending_outcomes(o.journal, bars_provider=None)
    assert res == {"total": 1, "updated": 0, "completed": 0, "skipped": 1, "unavailable": 0}
    still = o.journal.one("SELECT outcome_status FROM candidate_outcomes WHERE outcome_id = ?",
                          (row["outcome_id"],))
    assert still["outcome_status"] == "pending"
    o.close()


def test_update_with_empty_bars_stays_pending_within_window():
    o = _orch()
    row = _seeded_row(o)   # created "now" -> well within UNAVAILABLE_AFTER_DAYS
    res = update_pending_outcomes(o.journal, bars_provider=_FakeBars({}))
    assert res["skipped"] == 1 and res["unavailable"] == 0
    still = o.journal.one("SELECT outcome_status FROM candidate_outcomes WHERE outcome_id = ?",
                          (row["outcome_id"],))
    assert still["outcome_status"] == "pending"
    o.close()


def test_update_marks_unavailable_after_window_with_no_bars():
    o = _orch()
    row = _seeded_row(o)
    # Simulate a candidate DECIDED 30 days ago with a symbol no bars ever exist
    # for. Backdating decision_at_utc (the real anchor), not created_at_utc
    # (mere seed time) — this is exactly the HIGH-1 distinction.
    o.journal.conn.execute(
        "UPDATE candidate_outcomes SET decision_at_utc = ? WHERE outcome_id = ?",
        ("2020-01-01T00:00:00+00:00", row["outcome_id"]))
    o.journal.conn.commit()
    res = update_pending_outcomes(o.journal, bars_provider=_FakeBars({}))
    assert res["unavailable"] == 1
    still = o.journal.one("SELECT outcome_status, data_quality_status FROM candidate_outcomes "
                          "WHERE outcome_id = ?", (row["outcome_id"],))
    assert still["outcome_status"] == "unavailable"
    o.close()


def test_update_is_idempotent_once_complete():
    o = _orch()
    row = _seeded_row(o)
    created_date = row["created_at_utc"][:10]
    import datetime
    d0 = datetime.date.fromisoformat(created_date)
    bars = [{"date": (d0 + datetime.timedelta(days=i)).isoformat(),
            "open": 100, "high": 101, "low": 99, "close": 100.2} for i in range(1, 6)]
    provider = _FakeBars({"AAPL": bars})
    first = update_pending_outcomes(o.journal, bars_provider=provider)
    assert first["completed"] == 1
    second = update_pending_outcomes(o.journal, bars_provider=provider)
    assert second == {"total": 0, "updated": 0, "completed": 0, "skipped": 0, "unavailable": 0}
    o.close()


def test_update_excludes_decision_day_bar_no_lookahead():
    """A bar dated the SAME day as the decision must never count as 'forward' —
    that would leak information the decision was made without."""
    o = _orch()
    row = _seeded_row(o)
    created_date = row["created_at_utc"][:10]
    # Only a same-day bar exists; nothing strictly after it.
    bars = [{"date": created_date, "open": 100, "high": 200, "low": 50, "close": 199}]
    res = update_pending_outcomes(o.journal, bars_provider=_FakeBars({"AAPL": bars}))
    assert res["skipped"] == 1   # correctly finds zero usable forward bars
    still = o.journal.one("SELECT outcome_status FROM candidate_outcomes WHERE outcome_id = ?",
                          (row["outcome_id"],))
    assert still["outcome_status"] == "pending"
    o.close()


def test_update_never_submits_order_or_approval():
    o = _orch()
    row = _seeded_row(o)
    created_date = row["created_at_utc"][:10]
    import datetime
    d0 = datetime.date.fromisoformat(created_date)
    bars = [{"date": (d0 + datetime.timedelta(days=i)).isoformat(),
            "open": 100, "high": 101, "low": 99, "close": 100.2} for i in range(1, 6)]
    update_pending_outcomes(o.journal, bars_provider=_FakeBars({"AAPL": bars}))
    assert o.journal.count_rows("paper_orders") == 0
    assert o.journal.count_rows("approvals") == 0
    assert o.journal.count_open_positions() == 0
    h = o.system_health()
    assert h["real_money_trading"] == "unreachable"
    assert h["manual_approval"] == "required"
    o.close()


# ------------------------------------------------- HIGH-1 regression (Opus audit)
# Forward windows must anchor on decision_at_utc (when AlphaOS actually
# decided), never on created_at_utc (when this outcome row happened to get
# seeded) — those diverge whenever seeding lags the decision, e.g. catching up
# on a backlog. Anchoring on seed time instead mislabels an old candidate's
# next bar as a "1-day" return using a stale entry price.
def test_backlog_seeding_anchors_forward_windows_on_original_decision_date():
    o = _orch()
    cid = _candidate(o, symbol="AAPL")
    _eval(o, cid, symbol="AAPL", entry=100.0, stop=95.0, target=200.0)   # target far away: no replay noise
    _proposal(o, cid, symbol="AAPL", entry=100.0, stop=95.0, target=200.0)
    # Decision happened 30 days ago; SEEDING happens only now (a backlog
    # catch-up run), so created_at_utc (seed time) and decision_at_utc (the
    # proposal's real timestamp) must diverge by ~30 days.
    o.journal.conn.execute(
        "UPDATE trade_proposals SET created_at_utc = '2026-06-02T10:00:00+00:00' WHERE candidate_id = ?", (cid,))
    o.journal.conn.commit()

    seed_pending_outcomes(o.journal)
    row = o.journal.one("SELECT * FROM candidate_outcomes WHERE candidate_id = ?", (cid,))
    assert row["decision_at_utc"].startswith("2026-06-02")
    assert not row["created_at_utc"].startswith("2026-06-02")   # seeded "now", not backdated

    # Only ONE bar exists, dated the day after the ORIGINAL decision (not
    # "today"). If the code wrongly anchored on seed time, this bar would be
    # filtered out entirely (it's far in the "past" relative to today) and the
    # window would never resolve.
    bars = [{"date": "2026-06-03", "open": 100, "high": 108, "low": 99, "close": 106}]
    res = update_pending_outcomes(o.journal, bars_provider=_FakeBars({"AAPL": bars}))
    assert res["updated"] == 1

    updated = o.journal.one("SELECT * FROM candidate_outcomes WHERE outcome_id = ?", (row["outcome_id"],))
    # A genuine 1-day move from the real decision price (100 -> 106), NOT a
    # ~30-day move mislabeled as 1-day.
    assert updated["forward_1d_r"] == round((106 - 100) / 5, 4)
    assert updated["forward_1d_return_pct"] == round((106 - 100) / 100, 4)
    o.close()


def test_bars_fetch_start_uses_decision_date_not_seed_date():
    o = _orch()
    cid = _candidate(o, symbol="AAPL")
    _eval(o, cid, symbol="AAPL")
    _proposal(o, cid, symbol="AAPL")
    o.journal.conn.execute(
        "UPDATE trade_proposals SET created_at_utc = '2026-05-01T00:00:00+00:00' WHERE candidate_id = ?", (cid,))
    o.journal.conn.commit()
    seed_pending_outcomes(o.journal)

    captured = {}

    class _SpyBars:
        def get_daily_bars(self, symbol, start, end):
            captured["start"] = start
            return []

    update_pending_outcomes(o.journal, bars_provider=_SpyBars())
    assert captured["start"] == "2026-05-01"   # decision date, not today's seed date
    o.close()


# ------------------------------------------------------------- repair (legacy rows)
def test_repair_recovers_decision_at_utc_from_still_present_source_row():
    o = _orch()
    row = _seeded_row(o)
    # Simulate a row seeded before decision_at_utc existed.
    o.journal.conn.execute(
        "UPDATE candidate_outcomes SET decision_at_utc = NULL WHERE outcome_id = ?", (row["outcome_id"],))
    o.journal.conn.commit()

    res = update_pending_outcomes(o.journal, bars_provider=_FakeBars({}))   # no bars needed for this assertion
    assert res["total"] == 1   # the row was picked up and processed
    repaired = o.journal.one("SELECT decision_at_utc, data_quality_status FROM candidate_outcomes "
                             "WHERE outcome_id = ?", (row["outcome_id"],))
    assert repaired["decision_at_utc"] is not None
    assert repaired["data_quality_status"] != "decision_time_unrecoverable"   # recovered cleanly, not a fallback
    o.close()


def test_repair_falls_back_safely_when_source_row_is_gone():
    o = _orch()
    cid = _candidate(o)
    _eval(o, cid)
    _proposal(o, cid)
    seed_pending_outcomes(o.journal)
    out_id = o.journal.one("SELECT outcome_id FROM candidate_outcomes WHERE candidate_id = ?", (cid,))["outcome_id"]

    # The proposal AND the candidate are both gone (e.g. an old archived scan) —
    # decision_at_utc cannot be recovered from anywhere.
    o.journal.conn.execute("DELETE FROM trade_proposals WHERE candidate_id = ?", (cid,))
    o.journal.conn.execute("DELETE FROM candidates WHERE candidate_id = ?", (cid,))
    o.journal.conn.execute("UPDATE candidate_outcomes SET decision_at_utc = NULL WHERE outcome_id = ?", (out_id,))
    o.journal.conn.commit()

    res = update_pending_outcomes(o.journal, bars_provider=_FakeBars({}))
    assert res["total"] == 1
    row = o.journal.one("SELECT decision_at_utc, data_quality_status FROM candidate_outcomes "
                        "WHERE outcome_id = ?", (out_id,))
    assert row["decision_at_utc"] is not None    # never left permanently None (falls back to created_at_utc)
    assert row["data_quality_status"] == "decision_time_unrecoverable"   # but honestly flagged, not pretended
    o.close()


def test_repair_unrecoverable_flag_survives_a_successful_resolution_pass():
    """A row repaired with the fallback flag must keep that flag even if the
    SAME update pass goes on to successfully compute forward returns — losing
    the 'this timestamp is not the real decision time' signal would be worse
    than an unresolved row."""
    o = _orch()
    cid = _candidate(o, symbol="AAPL")
    _eval(o, cid, symbol="AAPL")
    _proposal(o, cid, symbol="AAPL")
    seed_pending_outcomes(o.journal)
    out_id = o.journal.one("SELECT outcome_id FROM candidate_outcomes WHERE candidate_id = ?", (cid,))["outcome_id"]
    o.journal.conn.execute("DELETE FROM trade_proposals WHERE candidate_id = ?", (cid,))
    o.journal.conn.execute("DELETE FROM candidates WHERE candidate_id = ?", (cid,))
    o.journal.conn.execute("UPDATE candidate_outcomes SET decision_at_utc = NULL WHERE outcome_id = ?", (out_id,))
    o.journal.conn.commit()

    # A bar dated far in the future is always "forward" of whatever fallback
    # timestamp repair picks, so this pass succeeds rather than skipping.
    bars = [{"date": "2099-01-01", "open": 100, "high": 105, "low": 99, "close": 103}]
    update_pending_outcomes(o.journal, bars_provider=_FakeBars({"AAPL": bars}))
    row = o.journal.one("SELECT data_quality_status, forward_1d_r FROM candidate_outcomes "
                        "WHERE outcome_id = ?", (out_id,))
    assert row["forward_1d_r"] is not None                                  # computation did succeed
    assert row["data_quality_status"] == "decision_time_unrecoverable"      # flag preserved, not clobbered by "ok"
    o.close()


def test_repair_is_idempotent_second_pass_does_not_reprocess():
    o = _orch()
    row = _seeded_row(o)
    o.journal.conn.execute(
        "UPDATE candidate_outcomes SET decision_at_utc = NULL WHERE outcome_id = ?", (row["outcome_id"],))
    o.journal.conn.commit()
    update_pending_outcomes(o.journal, bars_provider=_FakeBars({}))
    ts_after_first = o.journal.one("SELECT decision_at_utc FROM candidate_outcomes WHERE outcome_id = ?",
                                   (row["outcome_id"],))["decision_at_utc"]
    update_pending_outcomes(o.journal, bars_provider=_FakeBars({}))
    ts_after_second = o.journal.one("SELECT decision_at_utc FROM candidate_outcomes WHERE outcome_id = ?",
                                    (row["outcome_id"],))["decision_at_utc"]
    assert ts_after_first == ts_after_second   # repaired once, stable thereafter
    o.close()
