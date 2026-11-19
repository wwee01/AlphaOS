"""Claude's independent probes for Codex decision-time mapping a641b3c (audit 2026-11-19).

Run from a clone at a641b3caa95c325c6ad97547dd62f0b9e16a30a7 by copying this file into tests/
(uses that tree's conftest `journal` fixture and test_explore_policy helpers):
    PYTHONPATH=$PWD .venv/bin/python -m pytest tests/<this file> -p no:cacheprovider
Offline only: no provider call, no production write.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json

import pytest

from alphaos import explore_policy as ep
from alphaos.learning import close_ref_engine as engine
from alphaos.learning import close_ref_mapping as mapping
from alphaos.util import timeutils
from test_explore_policy import scan, settings

ET = ZoneInfo("America/New_York")
UTC = timezone.utc


def et(*a):
    return datetime(*a, tzinfo=ET)


def rec(exact=None, lower=None, upper=None, *, arm="A", sym="XYZ", batch_row=1, cand_row=1, batch="s1"):
    cand = {"id": cand_row, "candidate_id": f"c{cand_row}", "symbol": sym}
    b = {"id": batch_row, "scan_batch_id": batch}
    iso = lambda t: t.isoformat() if t is not None else None
    return mapping._record("arm", arm, cand, b, "scan_batches", batch,
                           "exact_routing_v1" if exact else "bounded_routing_v1", "f" * 40,
                           exact=iso(exact), lower=iso(lower), upper=iso(upper))


# G1 -- spec §6 examples (exact instants): (instant, D, D+5, D+10)
SPEC = [
    (et(2026, 11, 18, 16, 1), "2026-11-19", "2026-11-27", "2026-12-04"),            # 1 opinion 16:01
    (et(2026, 11, 18, 15, 59, 30), "2026-11-18", "2026-11-25", "2026-12-03"),       # 1b ranking row
    (et(2026, 11, 18, 16, 2), "2026-11-19", "2026-11-27", "2026-12-04"),            # 1b routing
    (et(2026, 11, 18, 16, 0, 0, 0), "2026-11-19", "2026-11-27", "2026-12-04"),      # 2 exactly at close
    (et(2026, 11, 18, 15, 59, 59, 999999), "2026-11-18", "2026-11-25", "2026-12-03"),  # 2b
    (et(2026, 11, 18, 15, 59, 58), "2026-11-18", "2026-11-25", "2026-12-03"),       # 3 stamp, late commit
    (et(2026, 11, 27, 12, 59, 59), "2026-11-27", "2026-12-04", "2026-12-11"),       # 4 early close
    (et(2026, 11, 27, 13, 0, 0), "2026-11-30", "2026-12-07", "2026-12-14"),         # 4b
    (et(2026, 11, 27, 15, 58), "2026-11-30", "2026-12-07", "2026-12-14"),           # 4c
    (et(2026, 12, 24, 11, 40), "2026-12-24", "2027-01-04", "2027-01-11"),           # 4d
    (et(2026, 12, 24, 15, 50), "2026-12-28", "2027-01-05", "2027-01-12"),           # 4d
    (et(2026, 11, 26, 10, 0), "2026-11-27", "2026-12-04", "2026-12-11"),            # 5 Thanksgiving
    (et(2026, 11, 21, 11, 0), "2026-11-23", "2026-12-01", "2026-12-08"),            # 5b Saturday
]


@pytest.mark.parametrize("instant,d,d5,d10", SPEC)
def test_g1_spec_examples_exact(instant, d, d5, d10):
    r = rec(exact=instant)
    assert r.status == "ready", r.reason
    assert (r.window.reference.date, r.window.endpoint_5.date, r.window.endpoint_10.date) == (d, d5, d10)


def test_g2_bounded_and_boundary_rules():
    ok = rec(lower=datetime(2026, 11, 16, 20, 58, 22, 152072, tzinfo=UTC),
             upper=datetime(2026, 11, 16, 20, 58, 24, 449569, tzinfo=UTC))
    assert ok.status == "ready" and ok.window.reference.date == "2026-11-16"
    assert ok.window.endpoint_5.date == "2026-11-23" and ok.window.endpoint_10.date == "2026-12-01"
    assert rec(lower=et(2026, 11, 18, 15, 59, 40), upper=et(2026, 11, 18, 16, 6, 7)).reason == "session_unproven"
    assert rec(lower=et(2026, 11, 18, 15, 0), upper=et(2026, 11, 18, 14, 0)).reason == "session_unproven"
    pre = rec(exact=et(2026, 11, 16, 9, 29, 59))
    assert (pre.status, pre.reason) == ("out_of_scope", "before_campaign")
    assert rec(lower=et(2026, 11, 16, 9, 29, 50), upper=et(2026, 11, 16, 9, 30, 10)).reason == "boundary_unproven"
    naive = mapping._record("arm", "A", {"id": 1, "candidate_id": "c", "symbol": "X"}, {"id": 1, "scan_batch_id": "s"},
                            "scan_batches", "s", "exact_routing_v1", "f" * 40, exact="2026-11-18T15:00:00")
    assert naive.status == "unavailable" and naive.reason == "timestamp_unavailable"
    # exact stamp outside the retained interval fails closed
    assert rec(exact=et(2026, 11, 18, 15, 0), lower=et(2026, 11, 18, 15, 1),
               upper=et(2026, 11, 18, 15, 2)).reason == "identity_mismatch"


def test_g3_dedupe_overlap_and_order():
    a = rec(lower=et(2026, 11, 18, 10, 40), upper=et(2026, 11, 18, 10, 45), batch_row=1, cand_row=1, batch="s1")
    b = rec(exact=et(2026, 11, 18, 10, 44), batch_row=2, cand_row=2, batch="s2")
    out = {r.scan_batch_id: r for r in mapping.deduplicate([a, b])}
    assert out["s1"].reason == out["s2"].reason == "order_unproven"
    c = rec(exact=et(2026, 11, 18, 12, 0), batch_row=3, cand_row=3, batch="s3")
    out = {r.scan_batch_id: r for r in mapping.deduplicate([a, c])}
    assert out["s1"].status == "ready" and out["s3"].status == "duplicate_in_session"
    # post-close decision and next-morning decision share D -> one decision (CK ruling 1)
    late = rec(exact=et(2026, 11, 18, 16, 2), batch_row=4, cand_row=4, batch="s4")
    morning = rec(exact=et(2026, 11, 19, 9, 40), batch_row=5, cand_row=5, batch="s5")
    out = {r.scan_batch_id: r for r in mapping.deduplicate([late, morning])}
    assert out["s4"].status == "ready" and out["s5"].status == "duplicate_in_session"


def test_g4_stamp_is_event_time_not_commit_time(journal, monkeypatch):
    scan(journal, "s")
    clock = [datetime(2026, 11, 18, 20, 59, 58, tzinfo=UTC)]
    monkeypatch.setattr(timeutils, "now_utc", lambda: clock[0])
    real_commit = journal.conn.commit

    class Slow:
        def __init__(self, conn): self.c = conn
        def __getattr__(self, n): return getattr(self.c, n)
        def execute(self, *a): return self.c.execute(*a)
        def commit(self):
            clock[0] = datetime(2026, 11, 18, 21, 0, 4, tzinfo=UTC)  # commit lands after the close
            return real_commit()
    journal.conn = Slow(journal.conn)
    ep.freeze_draw(journal, settings(), "s", "2026-11-18T20:45:00+00:00", ["a", "b"])
    row = journal.one("SELECT * FROM scan_batches WHERE scan_batch_id='s'")
    assert row["explore_routed_at_utc"] == "2026-11-18T20:59:58.000000+00:00"
    r = mapping._record("arm", "A", {"id": 1, "candidate_id": "a", "symbol": "A"}, {"id": 1, "scan_batch_id": "s"},
                        "scan_batches", "s", "exact_routing_v1", None, exact=row["explore_routed_at_utc"])
    assert r.window.reference.date == "2026-11-18"


def test_g5_crash_before_commit_persists_nothing_then_later_stamp(journal, monkeypatch):
    scan(journal, "s")
    monkeypatch.setattr(timeutils, "now_utc", lambda: datetime(2026, 11, 18, 20, 59, 58, tzinfo=UTC))

    class Boom:
        def __init__(self, conn): self.c = conn
        def __getattr__(self, n): return getattr(self.c, n)
        def execute(self, *a): return self.c.execute(*a)
        def commit(self): raise RuntimeError("crash")
    original = journal.conn
    journal.conn = Boom(original)
    with pytest.raises(RuntimeError):
        ep.freeze_draw(journal, settings(), "s", "2026-11-18T20:45:00+00:00", ["a"])
    journal.conn = original
    row = journal.one("SELECT * FROM scan_batches WHERE scan_batch_id='s'")
    assert row["explore_draw_json"] is None and row["explore_routed_at_utc"] is None
    monkeypatch.setattr(timeutils, "now_utc", lambda: datetime(2026, 11, 18, 21, 3, tzinfo=UTC))
    ep.freeze_draw(journal, settings(), "s", "2026-11-18T20:45:00+00:00", ["a"])
    row = journal.one("SELECT * FROM scan_batches WHERE scan_batch_id='s'")
    assert row["explore_routed_at_utc"] == "2026-11-18T21:03:00.000000+00:00"
    # re-entry never restamps
    monkeypatch.setattr(timeutils, "now_utc", lambda: pytest.fail("restamped"))
    ep.freeze_draw(journal, settings(), "s", "2026-11-18T20:45:00+00:00", ["a"])


def test_g6_copying_scan_gets_own_stamp_no_pool(journal, monkeypatch):
    scan(journal, "s1"); scan(journal, "s2")
    monkeypatch.setattr(timeutils, "now_utc", lambda: datetime(2026, 11, 18, 20, 50, tzinfo=UTC))
    ep.freeze_draw(journal, settings(), "s1", "2026-11-18T20:45:00+00:00", ["a", "b"])
    monkeypatch.setattr(timeutils, "now_utc", lambda: datetime(2026, 11, 18, 20, 55, tzinfo=UTC))
    assert ep.freeze_draw(journal, settings(), "s2", "2026-11-18T20:52:00+00:00", ["c"]) is None
    row = journal.one("SELECT * FROM scan_batches WHERE scan_batch_id='s2'")
    assert row["explore_routed_at_utc"] == "2026-11-18T20:55:00.000000+00:00" and row["explore_window_key"] is None
    batch = dict(row); batch["id"] = 2
    recs = mapping.routing_records(batch, [{"id": 9, "candidate_id": "c", "symbol": "C", "scan_batch_id": "s2",
                                            "shadow_tier": 0, "arm": None, "policy_version": "explore_1"}], [], [])
    assert [r for r in recs if r.population == "pool"] == []


def fallback_eval(cid="c1"):
    return {"id": 1, "eval_id": "e1", "candidate_id": cid, "symbol": "AAPL", "direction": "long",
            "decision": "reject", "confidence": 0.0, "validation_status": "passed", "is_mock": 0,
            "risk_flags_json": '["OPENAI_REJECT"]', "reasoning_summary": "OpenAI call failed; rejected for safety.",
            "total_tokens": None, "created_at_utc": "2026-11-18T14:40:25.798389+00:00"}


def test_g7_quota_fallback_is_ranking_eligible_at_a641b3c():
    """FINDING D1 reproduction: a provider-failure fallback enters the ranking roster."""
    cand = {"id": 1, "candidate_id": "c1", "symbol": "AAPL", "direction": "long", "shadow_tier": 0, "scan_batch_id": "s"}
    batch = {"id": 1, "scan_batch_id": "s"}
    [r] = mapping.evaluator_records([cand], [batch], [fallback_eval()], non_degraded_scan_ids={"s"})
    assert r.status == "ready" and r.ranking_eligible is True  # should be excluded as a fail-safe, not an opinion


def test_g8_raw_split_would_freeze_a_corporate_action_as_a_return():
    """OBSERVATION O2: raw adjustment turns a 4:1 split inside the window into r = -0.75."""
    w = engine.resolve_window(datetime(2026, 11, 18, 15, 0, tzinfo=UTC))
    bars = [{"date": w.reference.date, "close": 400.0}, {"date": w.endpoint_5.date, "close": 101.0}]
    t = w.endpoint_5.close.replace(hour=23)
    leg = engine.observe_leg(w, 5, "XYZ", bars, provider="alpaca", feed="iex", adjustment="raw",
                             requested_at=t, fetched_at=t, now=t)
    assert leg.status == "complete" and abs(leg.return_fraction + 0.7475) < 1e-9


def test_g9_leg_timing_rules():
    w = engine.resolve_window(datetime(2026, 11, 18, 15, 0, tzinfo=UTC))
    early = w.endpoint_5.close.replace(minute=0) if w.endpoint_5.close.minute else w.endpoint_5.close.replace(hour=20)
    leg = engine.observe_leg(w, 5, "XYZ", [], provider="alpaca", feed="iex", adjustment="raw",
                             requested_at=early, fetched_at=early, now=early)
    assert (leg.status, leg.reason) == ("pending", "endpoint_not_closed_at_request")
    t = w.endpoint_5.close.replace(hour=23)
    leg = engine.observe_leg(w, 5, "XYZ", [{"date": w.reference.date, "close": 10.0}], provider="alpaca",
                             feed="iex", adjustment="raw", requested_at=t, fetched_at=t, now=t)
    assert (leg.status, leg.reason) == ("pending", "exact_endpoint_bar_missing")
    late = engine.retry_until(w.endpoint_5).replace(microsecond=1)
    leg = engine.observe_leg(w, 5, "XYZ", [], provider="alpaca", feed="iex", adjustment="raw",
                             requested_at=late, fetched_at=late, now=late)
    assert leg.status == "unavailable"
