"""Real scheduled/report/storage boundaries; all source transports are offline."""

import json
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
import sys
import socket

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.reports.relative_performance import (
    build_relative_performance_report,
    render_markdown,
)
from alphaos.scheduler.jobs import run_benchmark_spine_job
from alphaos.util import timeutils
from conftest import make_settings
from test_economic_capture import Transport
from test_cash_quantity_bridge import fill, cash_event


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("offline test forbids network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def live_settings(**extra):
    return make_settings(
        ALPHAOS_MODE="paper",
        OFFLINE_MODE="false",
        ALPACA_API_KEY="test-key",
        ALPACA_SECRET_KEY="test-secret",
        **extra,
    )


def test_normal_report_includes_operational_state():
    journal = JournalStore(":memory:")
    result = build_relative_performance_report(journal, make_settings())
    assert result["operational_economics"]["status"] == "not_collected"
    assert result["excess_return_pct"] is None


def test_scheduler_dispatches_capture(monkeypatch):
    import alphaos.reports.benchmark_capture as old

    calls = []
    monkeypatch.setattr(
        old, "capture_benchmark_spine", lambda *a: {"legacy": "preserved"}
    )
    monkeypatch.setitem(
        sys.modules,
        "alphaos.reports.operational_economics",
        SimpleNamespace(
            capture_operational_economics=lambda *a, **k: (
                calls.append(k)
                or {"status": "waiting_for_second_endpoint", "written": 1}
            )
        ),
    )
    result = run_benchmark_spine_job(
        SimpleNamespace(journal=JournalStore(":memory:"), settings=make_settings()),
        SimpleNamespace(role="all"),
    )
    assert len(calls) == 1
    assert result["benchmark_spine_result"]["legacy"] == "preserved"
    assert (
        result["benchmark_spine_result"]["operational_economics"]["status"]
        == "waiting_for_second_endpoint"
    )


@pytest.fixture
def state(tmp_path, monkeypatch):
    from alphaos.reports import operational_economics as m
    from alphaos.reports.economic_capture import EconomicSource

    journal = JournalStore(str(tmp_path / "scratch.db"))
    settings = live_settings()
    fake = Transport()
    cell = [fake]
    monkeypatch.setattr(timeutils, "now_utc", lambda: cell[0].now())

    def run(next_fake=None, should_yield=None):
        if next_fake is not None:
            cell[0] = next_fake
        return m.capture_operational_economics(
            journal,
            settings,
            should_yield=should_yield,
            source_factory=lambda st, **kw: EconomicSource(
                st, transport=cell[0].get, clock=cell[0].now, **kw
            ),
        )

    return SimpleNamespace(
        m=m, journal=journal, settings=settings, fake=fake, cell=cell, run=run
    )


def second(day="15"):
    return Transport(
        calendar=[
            {"date": "2026-09-14", "open": "09:30", "close": "16:00"},
            {"date": "2026-09-" + day, "open": "09:30", "close": "16:00"},
        ],
        start="2026-09-" + day + "T21:00:00+00:00",
    )


def saved(s):
    return s.journal.query(
        "SELECT * FROM system_events WHERE category=? ORDER BY id", (s.m.CATEGORY,)
    )


def test_real_capture_seed_restart_pair_report_and_no_double_fee(state):
    s = state
    first = s.run()
    assert (
        first["status"] == "waiting_for_second_endpoint"
        and first["actual_get_attempts"] == 8
    )
    original = saved(s)[0]
    second_fake = second()
    second_fake.activities = [
        fill("sell1", 1, 105),
        fill("sell2", 1, 106),
        cash_event("fee", "FEE", -1),
        cash_event("div", "DIV", 2),
    ]
    second_fake.account["cash"] = "1112"
    second_fake.positions = []
    second_fake.mutate = lambda fake, path, params: (
        {
            "bars": {
                "SPY": [
                    {
                        "t": params["start"],
                        "o": 505,
                        "h": 505,
                        "l": 505,
                        "c": 505,
                        "v": 10,
                    }
                ]
            }
        }
        if path == "/v2/stocks/bars"
        else None
    )
    result = s.run(second_fake)
    assert result["status"] == "reconciled_asof" and result["actual_get_attempts"] == 9
    assert saved(s)[0] == original
    report = build_relative_performance_report(s.journal, s.settings)
    op = report["operational_economics"]
    e = op["economic"]
    assert e["account_gain_usd"] == "12" and float(e["benchmark_gain_usd"]) == 11
    assert float(e["excess_gain_usd"]) == 1 and e["reported_broker_fee_cash"] == "-1"
    assert (
        e["modeled_trade_cost_deduction"] == "0" and op["all_in_net_gain_usd"] is None
    )
    assert (
        op["subscription_only_adjusted_gain_usd"] is None
        and op["weekly_target_verdict"] is None
    )
    assert op["subscription_allocation_usd"]["numerator"] == 450
    assert op["subscription_allocation_usd"]["denominator"] == 91
    assert report["excess_return_pct"] is None  # legacy curve remains withheld
    text = render_markdown(report)
    assert "$12.00" in text and "Other operating expenses" in text
    assert "private-account-id" not in str(saved(s)) and "test-secret" not in str(
        saved(s)
    )
    path = s.journal.db_path
    s.journal.conn.close()
    s.journal = JournalStore(path)
    assert (
        s.m.build_operational_economics_report(s.journal, s.settings)["economic"] == e
    )
    count = len(second_fake.calls)
    assert (
        s.m.capture_operational_economics(s.journal, s.settings)["status"]
        == "already_recorded"
        and len(second_fake.calls) == count
    )


def test_date_only_internal_cash_can_be_valid_without_strict_common_capture(state):
    s = state
    s.run()
    fake = second("16")
    fake.calendar[0]["date"] = "2026-09-15"
    fake.activities = [
        {"id": "fee", "activity_type": "FEE", "date": "2026-09-15", "net_amount": "-1"}
    ]
    fake.account["cash"] = "899"
    result = s.run(fake)
    assert result["status"] == "reconciled_asof"
    assert result["measurement"]["common_capture_status"] == "blocked"
    assert result["measurement"]["economic"]["account_gain_usd"] == "-1"


@pytest.mark.parametrize(
    "cause",
    [
        "cash_gap",
        "quantity_gap",
        "offsetting_flows",
        "unknown_activity",
        "different_account",
        "missing_mark",
        "postclose_sold_symbol",
        "late_old_activity",
    ],
)
def test_real_source_adverse_intervals(state, cause):
    s = state
    s.run()
    fake = second()
    if cause == "cash_gap":
        fake.account["cash"] = "901"
    elif cause == "quantity_gap":
        fake.positions[0]["qty"] = "3"
    elif cause == "offsetting_flows":
        fake.activities = [cash_event("in", "CSD", 100), cash_event("out", "CSW", -100)]
    elif cause == "unknown_activity":
        fake.activities = [cash_event("unknown", "OTHER", 0)]
    elif cause == "different_account":
        fake.account["id"] = "different"
    elif cause == "missing_mark":
        fake.mutate = lambda f, p, q: {"bars": {}} if p == "/v2/stocks/bars" else None
    elif cause == "postclose_sold_symbol":
        row = fill("sell", 2, 100)
        row["transaction_time"] = "2026-09-15T20:30:00Z"
        fake.activities = [row]
        fake.positions = []
        fake.account["cash"] = "1100"
    elif cause == "late_old_activity":
        fake.activities = [cash_event("late", "FEE", 0)]
        fake.activities[0]["transaction_time"] = "2026-09-14T15:00:00Z"
    result = s.run(fake)
    report = s.m.build_operational_economics_report(s.journal, s.settings)
    assert (
        report["comparison_eligible"] is False and report["all_in_net_gain_usd"] is None
    )
    if cause == "offsetting_flows":
        assert (
            result["status"] == "reconciled_asof"
            and report["economic"]["account_gain_usd"] == "0"
        )
        assert report["economic"]["benchmark_return"] is None
    else:
        assert result["status"] == "blocked"


def test_latest_transport_failure_never_falls_back_to_previous_success(state):
    s = state
    s.run()
    s.run(second())
    fake = second("16")

    def fail(*args):
        raise ValueError("secret https://private.account")

    fake.get = fail
    result = s.run(fake)
    assert result["status"] == "unavailable"
    report = s.m.build_operational_economics_report(s.journal, s.settings)
    assert report["status"] == "unavailable" and "economic" not in report
    assert "private.account" not in str(saved(s))


def test_missing_source_creation_persists_failed_attempt(state):
    s = state

    def failed(*a, **kw):
        raise ValueError("private-token")

    result = s.m.capture_operational_economics(
        s.journal, s.settings, source_factory=failed
    )
    assert result["status"] == "unavailable" and result["written"] == 1
    assert "private-token" not in str(saved(s))


def test_yield_between_gets_retains_incomplete_attempt_and_resumes(state):
    s = state
    result = s.run(should_yield=lambda: len(s.cell[0].calls) >= 3)
    assert result["status"] == "yielded" and result["actual_get_attempts"] == 3
    assert (
        s.m.build_operational_economics_report(s.journal, s.settings)["status"]
        == "yielded"
    )
    result = s.run()
    assert result["status"] == "waiting_for_second_endpoint"
    assert len(saved(s)) == 2


def test_zero_work_yield_does_not_claim_or_call(state):
    s = state
    result = s.run(should_yield=lambda: True)
    assert result["status"] == "yielded" and result["written"] == 0
    assert not saved(s) and not s.fake.calls


@pytest.mark.parametrize(
    "mutation",
    [
        "future",
        "naive",
        "pin",
        "derived",
        "parent_pin",
        "row_identity",
        "foreign_opening",
    ],
)
def test_latest_receipt_tamper_fails_closed_without_read_writes(state, mutation):
    s = state
    s.run()
    s.run(second())
    row = saved(s)[-1]
    receipt = json.loads(row["detail_json"])
    e = receipt["envelope"]
    if mutation == "future":
        e["recorded_at_utc"] = "2099-01-01T00:00:00Z"
    elif mutation == "naive":
        e["attempt_started_at_utc"] = "2026-09-15T21:00:00"
    elif mutation == "pin":
        e["capture"]["input_sha256"] = "0" * 64
    elif mutation == "derived":
        e["derived"]["economic"]["account_gain_usd"] = "99999"
    elif mutation == "parent_pin":
        e["previous_receipt_sha256"] = "0" * 64
    elif mutation == "row_identity":
        e["event_id"] = "foreign"
    elif mutation == "foreign_opening":
        e["opening_reference"]["event_id"] = "missing"
    receipt["sha256"] = s.m.evidence_hash(e)
    s.journal.conn.execute(
        "UPDATE system_events SET detail_json=? WHERE id=?",
        (json.dumps(receipt), row["id"]),
    )
    s.journal.conn.commit()
    before = s.journal.conn.total_changes
    report = s.m.build_operational_economics_report(s.journal, s.settings)
    assert (
        report["status"] == "evidence_unreadable" and not report["comparison_eligible"]
    )
    assert s.journal.conn.total_changes == before


def test_compare_and_swap_refuses_changed_receipt_during_source(state):
    s = state
    s.run()
    fake = second()
    changed = False

    def race(f, p, q):
        nonlocal changed
        if not changed:
            changed = True
            s.journal.log_system_event("warning", s.m.CATEGORY, "foreign", {})

    fake.mutate = race
    with pytest.raises(s.m.EvidenceError, match="concurrent_capture"):
        s.run(fake)
    assert len(saved(s)) == 2


def test_read_inside_caller_transaction_never_commits_it(state):
    s = state
    s.run()
    s.journal.conn.execute("BEGIN")
    s.journal.conn.execute(
        "INSERT INTO system_events(event_id,severity,category,message,created_at_utc,created_at_sgt) VALUES('test','info','test','pending','2026-09-14T21:10:00Z','2026-09-15T05:10:00+08:00')"
    )
    result = s.m.build_operational_economics_report(s.journal, s.settings)
    assert (
        result["status"] == "waiting_for_second_endpoint"
        and s.journal.conn.in_transaction
    )
    s.journal.conn.rollback()
    assert not s.journal.one("SELECT * FROM system_events WHERE event_id='test'")


def test_future_source_clock_refuses_write(state):
    s = state
    from alphaos.reports.economic_capture import EconomicSource

    future = Transport(start="2099-09-14T21:00:00+00:00")
    with pytest.raises(s.m.EvidenceError, match="source_observation_after_recording"):
        s.m.capture_operational_economics(
            s.journal,
            s.settings,
            source_factory=lambda st, **kw: EconomicSource(
                st, transport=future.get, clock=future.now, **kw
            ),
        )
    assert not saved(s)


def test_mock_path_never_constructs_source():
    from alphaos.reports.operational_economics import capture_operational_economics

    def fail(*a, **kw):
        raise AssertionError("source touched")

    assert (
        capture_operational_economics(
            JournalStore(":memory:"), make_settings(), source_factory=fail
        )["written"]
        == 0
    )


def rewrite_receipt(s, row, envelope):
    receipt = {"envelope": envelope, "sha256": s.m.evidence_hash(envelope)}
    s.journal.conn.execute(
        "UPDATE system_events SET detail_json=? WHERE id=?",
        (json.dumps(receipt), row["id"]),
    )
    s.journal.conn.commit()
    return receipt


@pytest.mark.parametrize("damage", ["missing", "null"])
def test_historical_receipt_damage_is_visible_without_crashing_normal_report(
    state, damage
):
    s = state
    s.run()
    for day in ("15", "16", "17"):
        assert s.run(second(day))["status"] == "reconciled_asof"
    first = saved(s)[0]
    if damage == "missing":
        s.journal.conn.execute("DELETE FROM system_events WHERE id=?", (first["id"],))
    else:
        s.journal.conn.execute(
            "UPDATE system_events SET detail_json=NULL WHERE id=?", (first["id"],)
        )
    s.journal.conn.commit()
    result = build_relative_performance_report(s.journal, s.settings)[
        "operational_economics"
    ]
    assert result["status"] == "evidence_unreadable" and "economic" not in result


def test_resealed_failed_ancestor_cannot_choose_an_older_opening(state):
    # Regression for independent review's A/B/failed-C/D selection attack.
    s = state
    s.run()
    s.run(second())
    failed = second("16")

    def fail(*args):
        raise ValueError("controlled source failure")

    failed.get = fail
    s.run(failed)
    s.run(second("17"))
    a, _, c, d = saved(s)
    ar = json.loads(a["detail_json"])
    ce = json.loads(c["detail_json"])["envelope"]
    de = json.loads(d["detail_json"])["envelope"]
    ref = {"event_id": a["event_id"], "sha256": ar["sha256"]}
    ce["opening_reference"] = ref
    changed = rewrite_receipt(s, c, ce)
    de["opening_reference"] = ref
    de["previous_receipt_sha256"] = changed["sha256"]
    de["economic_inputs"]["start_capture"] = deepcopy(ar["envelope"]["capture"])
    actions = de["economic_inputs"]["corporate_actions"]
    actions["request"]["start"] = "2025-09-01"
    actions["pages"][0]["request"]["start"] = "2025-09-01"
    de["derived"] = s.m._derive(
        de,
        {
            **ref,
            "capture": ar["envelope"]["capture"],
            "recorded_at_utc": ar["envelope"]["recorded_at_utc"],
        },
    )
    assert de["derived"]["status"] == "reconciled_asof"
    rewrite_receipt(s, d, de)
    assert (
        s.m.build_operational_economics_report(s.journal, s.settings)["status"]
        == "evidence_unreadable"
    )


def add_newer_job(s, status, summary):
    s.cell[0] = second("16")
    s.journal.insert(
        "job_runs",
        {
            "job_run_id": "newer-benchmark",
            "job_type": "benchmark_spine",
            "started_at_utc": "2026-09-16T21:00:00Z",
            "started_at_sgt": "2026-09-17T05:00:00+08:00",
            "status": status,
            "result_summary_json": json.dumps(summary),
        },
    )


@pytest.mark.parametrize(
    "summary",
    [
        None,
        [],
        {"benchmark_spine_result": []},
        {"benchmark_spine_result": {"operational_economics": []}},
    ],
)
def test_malformed_newer_job_does_not_crash_or_show_prior_gain(state, summary):
    s = state
    s.run()
    s.run(second())
    add_newer_job(s, "completed", summary)
    result = build_relative_performance_report(s.journal, s.settings)[
        "operational_economics"
    ]
    assert result["status"] == "evidence_unreadable" and "economic" not in result


@pytest.mark.parametrize("status", ["started", "failed", "completed", "yielded"])
def test_newer_job_without_receipt_withholds_prior_result(state, status):
    s = state
    s.run()
    s.run(second())
    add_newer_job(
        s,
        status,
        {
            "benchmark_spine_result": {
                "operational_economics": {"status": "unavailable", "written": 0}
            }
        },
    )
    result = s.m.build_operational_economics_report(s.journal, s.settings)
    assert result["status"] == "unavailable" and "economic" not in result
    assert (
        result["comparison_eligible"] is False and result["all_in_net_gain_usd"] is None
    )


def test_history_lower_bound_is_retained_and_page_cap_is_latest_unavailable(state):
    s = state
    s.run()
    first = json.loads(saved(s)[0]["detail_json"])["envelope"]
    previous_age = first["derived"]["activity_lookback_days_at_attempt"]
    for day in ("15", "16"):
        result = s.run(second(day))["measurement"]
        assert (
            result["activity_coverage_start_utc"]
            == first["activity_coverage_start_utc"]
        )
        assert result["activity_lookback_days_at_attempt"] > previous_age
        previous_age = result["activity_lookback_days_at_attempt"]
    fake = second("17")

    def full_page(f, path, params):
        if path == "/v2/account/activities":
            return [
                {
                    "id": f"{params.get('page_token', 'first')}-{i}",
                    "activity_type": "FEE",
                    "date": "2026-09-01",
                    "net_amount": "0",
                }
                for i in range(100)
            ]
        return None

    fake.mutate = full_page
    result = s.run(fake)
    assert result["status"] == "unavailable" and result["actual_get_attempts"] <= 37
    report = s.m.build_operational_economics_report(s.journal, s.settings)
    assert (
        report["blockers"] == ["activity_pagination_cap_reached"]
        and "economic" not in report
    )
    assert report["activity_coverage_start_utc"] == first["activity_coverage_start_utc"]


def test_opening_recorded_after_end_cutoff_is_not_an_eligible_pair(state, monkeypatch):
    from alphaos.reports.economic_capture import EconomicSource

    s = state
    clocks = iter(
        [
            datetime.fromisoformat("2026-09-14T21:00:00+00:00"),
            datetime.fromisoformat("2026-09-15T21:00:00+00:00"),
        ]
    )
    monkeypatch.setattr(timeutils, "now_utc", lambda: next(clocks))
    s.m.capture_operational_economics(
        s.journal,
        s.settings,
        source_factory=lambda st, **kw: EconomicSource(
            st, transport=s.fake.get, clock=s.fake.now, **kw
        ),
    )
    fake = second()
    fake.clock = datetime.fromisoformat("2026-09-16T21:00:00+00:00")
    monkeypatch.setattr(timeutils, "now_utc", lambda: s.cell[0].now())
    result = s.run(fake)
    assert result["status"] == "blocked"
    assert result["measurement"]["blockers"] == [
        "opening_receipt_not_archived_before_ending_cutoff"
    ]


def test_receipt_chain_capacity_refuses_append_without_rewriting(state, monkeypatch):
    s = state
    s.run()
    s.run(second())
    before = saved(s)
    monkeypatch.setattr(s.m, "MAX_RECEIPTS", 2)
    with pytest.raises(s.m.EvidenceError, match="receipt_chain_capacity_reached"):
        s.run(second("16"))
    assert saved(s) == before


def test_receipt_pins_the_actual_unchanged_economic_method(state):
    from alphaos.reports.account_economics import METHOD

    state.run()
    assert (
        json.loads(saved(state)[0]["detail_json"])["envelope"]["contract"][
            "economic_method"
        ]
        == METHOD
    )


def test_default_source_with_fake_keys_is_fail_visible_when_network_is_denied(state):
    s = state
    result = s.m.capture_operational_economics(s.journal, s.settings)
    assert result["status"] == "unavailable" and result["written"] == 1
    report = s.m.build_operational_economics_report(s.journal, s.settings)
    assert report["status"] == "unavailable" and "economic" not in report
    assert "offline test forbids network" not in str(saved(s))


@pytest.mark.parametrize("summary", [{}, {"benchmark_spine_result": {}}])
def test_newer_missing_capture_metadata_cannot_masquerade_as_legacy(state, summary):
    s = state
    s.run()
    s.run(second())
    add_newer_job(s, "completed", summary)
    report = s.m.build_operational_economics_report(s.journal, s.settings)
    assert report["status"] == "unavailable" and "economic" not in report


@pytest.mark.parametrize("paired", [False, True])
def test_complete_receipt_followed_by_yield_retries_without_replacing_it(state, paired):
    s = state
    if paired:
        s.run()
        fake = second()
    else:
        fake = s.fake
    expected_gets = 9 if paired else 8
    result = s.run(fake, should_yield=lambda: len(fake.calls) == expected_gets)
    assert (
        result["status"] == "yielded" and result["actual_get_attempts"] == expected_gets
    )
    before = saved(s)
    report = s.m.build_operational_economics_report(s.journal, s.settings)
    assert report["status"] == (
        "reconciled_asof" if paired else "waiting_for_second_endpoint"
    )
    retry = s.run()
    assert (
        retry["status"] == "already_recorded"
        and retry["event_id"] == result["event_id"]
    )
    assert len(fake.calls) == expected_gets and saved(s) == before
    assert s.m.build_operational_economics_report(s.journal, s.settings) == report
