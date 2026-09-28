import hashlib
import json
import sqlite3
from copy import deepcopy

from alphaos.research.successor_cohorts import TABLES, export_cohorts, rank_membership
from alphaos.research.successor_contracts import draft_contract, known_registration_ids
from alphaos.research.successor_per import fixed_per_membership
from alphaos.research.successor_analysis import analyze_export


def connection(records):
    c = sqlite3.connect(":memory:")
    for table in TABLES:
        rows = [{"created_at_utc": "2026-06-30T15:00:00Z", **r} for r in records.get(table, [])]
        columns = sorted({"id"} | {k for r in rows for k in r})
        c.execute(f"CREATE TABLE {table} ({','.join(columns)})")
        for r in rows:
            keys = list(r)
            c.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", [r[k] for k in keys])
    c.commit()
    return c


def test_every_retained_registration_has_preserved_exact_original_and_no_activation():
    assert len(known_registration_ids()) == 15
    for key in known_registration_ids():
        r = {"prereg_id": key, "hypothesis": "verbatim original", "point_estimate": -.415359,
             "evidence_status": "ok", "operator_approved_for_forward_test": 0}
        original = deepcopy(r)
        d = draft_contract(r, [])
        assert d["status"] == "draft_not_activated" and d["formal_allowed"] is False
        assert d["original"] == r == original
    assert draft_contract({"prereg_id": "unknown"}, [])["status"] == "unsupported_registration"


def test_targets_never_copied_into_undefined_shadow_or_ttl_contract():
    shadow = draft_contract({"prereg_id": "prereg_89fcdf3974ff"}, [])
    ttl = draft_contract({"prereg_id": "prereg_09080b322ff2"}, [])
    assert shadow["effect_target"] is shadow["direction"] is None
    assert ttl["equivalence_margin"] is ttl["effect_target"] is None
    assert draft_contract({"prereg_id": "prereg_dfc7ac34b379"}, [])["direction"] == "less"


def test_tied_quartiles_never_overlap_and_are_independent_of_outcomes():
    rows = [{"candidate_id": str(i), "score": 1., "future_outcome": i} for i in range(8)]
    a = rank_membership(rows, "score", .25)
    for r in rows:
        r["future_outcome"] *= -999
    assert a == rank_membership(list(reversed(rows)), "score", .25)
    assert list(a.values()).count("tested") == list(a.values()).count("reference") == 2
    assert list(a.values()).count("middle") == 4


def test_export_keeps_missing_adverse_members_and_whole_source_identity_without_writes():
    records = {"preregistrations": [{"id": 1, "prereg_id": "prereg_bb6dc46834dd", "hypothesis": "TQS +.3R", "metric": "3day bracket"}],
               "candidates": [], "candidate_outcomes": [], "tqs_scores": [], "daily_bars": []}
    for i in range(4):
        records["candidates"].append({"id": i + 1, "candidate_id": f"c{i}", "symbol": "AAA", "created_at_utc": "2026-06-30T15:00:00Z"})
        records["tqs_scores"].append({"id": i + 1, "candidate_id": f"c{i}", "source_type": "candidate", "is_mock": 0, "data_quality_status": "ok", "tqs_score": i})
        records["candidate_outcomes"].append({"id": i + 1, "candidate_id": f"c{i}", "candidate_type": "candidate", "decision_at_utc": "2026-06-30T15:00:00Z",
             "entry_reference_price": 100., "stop_price": 90. if i != 3 else None, "target_price": 120., "direction_hint": "long", "forward_3d_r": 99.})
    for i, d in enumerate(["2026-07-01", "2026-07-02", "2026-07-06"]):
        records["daily_bars"].append({"id": i + 1, "bar_id": d, "symbol": "AAA", "market_date": d, "open": 100., "high": 110., "low": 89., "close": 103.,
                                      "source_feed": "saved", "created_at_utc": "2026-07-07T00:00:00Z"})
    c = connection(records)
    before = hashlib.sha256("\n".join(c.iterdump()).encode()).hexdigest()
    c.execute("PRAGMA query_only=ON")
    bundle = export_cohorts(c, as_of_utc="2026-07-08T00:00:00Z", snapshot_sha256="a" * 64)
    f = bundle["families"][0]
    assert len(f["members"]) == 4
    top = next(r for r in f["members"] if r["arm"] == "tested")
    assert top["candidate_id"] == "c3" and top["measurement"]["status"] == "blocked"
    bottom = next(r for r in f["members"] if r["arm"] == "reference")
    assert bottom["measurement"]["value"] == -1.
    assert f["summary"]["point_estimate"] is None  # Never drop missing top then select a replacement.
    assert bundle["source_rows"]["candidate_outcomes:4"]["stop_price"] is None
    assert before == hashlib.sha256("\n".join(c.iterdump()).encode()).hexdigest()
    json.dumps(bundle, allow_nan=False)
    analyzed = analyze_export(bundle, f, resamples=100)
    assert analyzed["result"]["status"] == "incomplete_cohort"
    assert len(analyzed["members"]) == 2  # Fixed tails, not the measured-only subset.
    f["members"].pop()
    assert analyze_export(bundle, f)["status"] == "invalid_export_identity"


def test_explicit_draft_horizon_is_not_written_into_original_or_activated():
    original = {"id": 1, "prereg_id": "prereg_debff71c8fe4", "hypothesis": "top vs median", "metric": "replay_r",
                "created_at_utc": "2026-06-30T15:00:00Z"}
    c = connection({"preregistrations": [original]})
    out = export_cohorts(c, as_of_utc="2026-09-28T00:00:00Z", snapshot_sha256="a" * 64,
                         draft_horizons={original["prereg_id"]: 3})
    contract = out["families"][0]["contract"]
    assert contract["horizon_sessions"] == 3
    assert contract["original"] == original and contract["formal_allowed"] is False
    assert contract["unreviewed_explicit_draft_horizon"] == 3


def per_fixture():
    earnings = [{"id": 1, "report_date": "2026-07-01", "fiscal_date_ending": "2026-06-30"}]
    rows = [{"candidate_id": "z-earliest", "symbol": "AAA", "created_at_utc": "2026-07-01T14:00:00Z", "card_id": "post_earnings_reaction", "card_assignment_status": "ok", "card_assignment_ref": "1", "shadow_tier": 0},
            {"candidate_id": "a-later-complete", "symbol": "AAA", "created_at_utc": "2026-07-01T15:00:00Z", "card_id": "post_earnings_reaction", "card_assignment_status": "ok", "card_assignment_ref": "1", "shadow_tier": 0}]
    rows += [{"candidate_id": f"control-{i}", "symbol": f"CTL{i}", "created_at_utc": "2026-07-01T14:00:00Z", "card_id": "default", "card_assignment_status": "ok", "shadow_tier": 0} for i in range(5)]
    return rows, earnings


def test_per_earliest_event_is_fixed_before_outcome_completion_old_failure():
    rows, earnings = per_fixture()
    outcomes = {"a-later-complete": {"outcome_status": "complete", "market_adjusted_return_5d_pct": .4}}
    new = fixed_per_membership(rows, earnings, outcomes)
    assert new["events"][0]["candidate_id"] == "z-earliest"
    assert new["counts"]["recorded_event_values"] == 0
    assert len(new["events"][0]["control_ids"]) == 5  # Fixed without requiring outcome availability.
    outcomes["z-earliest"] = {"outcome_status": "complete", "market_adjusted_return_5d_pct": -10.}
    after = fixed_per_membership(rows, earnings, outcomes)
    assert after["events"] == new["events"] and after["controls"] == new["controls"]
    outcomes["z-earliest"]["decision_at_utc"] = "2026-07-01T16:00:00Z"
    after_backfill = fixed_per_membership(rows, earnings, outcomes)
    assert after_backfill["events"] == new["events"]


def test_per_no_cross_tier_or_duplicate_control_and_all_population_gates_reported():
    rows, earnings = per_fixture()
    rows.append({**rows[-1], "candidate_id": "earlier-control", "created_at_utc": "2026-07-01T13:00:00Z"})
    rows[2]["shadow_tier"] = 1
    r = fixed_per_membership(rows, earnings, {})
    assert len(r["controls"]) == 5
    assert "earlier-control" in {c["candidate_id"] for c in r["controls"]}
    assert r["events"][0]["control_rung"] == "no_rung"
    assert r["counts"]["rungs"] == {"no_rung": 1}
    assert len(r["population_guards_all_reported"]) == 7


def test_future_feature_does_not_enter_earlier_capture_rank_population():
    c = connection({"preregistrations": [{"id": 1, "prereg_id": "prereg_bb6dc46834dd"}],
                    "candidates": [{"id": 1, "candidate_id": "c", "symbol": "AAA"}],
                    "tqs_scores": [{"id": 1, "candidate_id": "c", "source_type": "candidate", "is_mock": 0,
                                    "data_quality_status": "ok", "tqs_score": 9., "created_at_utc": "2026-09-30T00:00:00Z"}]})
    b = export_cohorts(c, as_of_utc="2026-07-08T00:00:00Z", snapshot_sha256="a" * 64)
    assert b["families"][0]["members"] == []
    assert b["unavailable_sources"] == [{"table": "tqs_scores", "row_id": 1,
                                          "reason": "source_recorded_or_updated_after_capture_boundary"}]


def test_ttl_open_executed_reference_cannot_disappear_until_close():
    rows = {"preregistrations": [{"id": 1, "prereg_id": "prereg_09080b322ff2"}],
            "trade_proposals": [{"id": 1, "proposal_id": "p", "status": "filled", "is_demo": 0}],
            "paper_orders": [{"id": 1, "order_id": "entry", "proposal_id": "p", "is_demo": 0}],
            "paper_fills": [{"id": 1, "fill_id": "fill", "order_id": "entry", "qty": 4.}],
            "positions": [{"id": 1, "position_id": "pos", "proposal_id": "p", "order_id": "entry", "is_demo": 0, "status": "open"}]}
    c = connection(rows)
    b = export_cohorts(c, as_of_utc="2026-07-08T00:00:00Z", snapshot_sha256="a" * 64)
    f = b["families"][0]
    assert len(f["members"]) == 1 and f["members"][0]["arm"] == "reference"
    assert f["members"][0]["measurement"]["reason"] == "executed_entry_outcome_still_open_or_missing"
    assert f["details"]["closed_reference_records"] == 0
