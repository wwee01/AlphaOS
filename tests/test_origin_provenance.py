"""Origin audit regression: exact saved anchors, chronology and snapshot reads."""

from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.reports import origin_provenance as audit

NOW = datetime(2026, 10, 1, 17, tzinfo=timezone.utc)


def seed(j, monkeypatch, *, final=True):
    monkeypatch.setattr(audit, "_now", lambda: NOW)

    def add(table, **fields):
        return j.insert(
            table, {"created_at_utc": "2026-09-01T14:00:00+00:00", **fields}
        )

    add("candidates", candidate_id="c", symbol="AMD")
    add(
        "openai_evaluations",
        eval_id="e",
        candidate_id="c",
        symbol="AMD",
        created_at_utc="2026-09-01T14:01:00Z",
        model="saved-model",
        model_provider="openai",
        prompt_hash="abcdef0123456789",
        system_prompt_hash="0123456789abcdef",
        prompt_template_version="v4",
        schema_version="v1",
        validation_status="passed",
        decision="watch",
    )
    add(
        "trade_proposals",
        proposal_id="p",
        candidate_id="c",
        eval_id="e",
        trade_id="t",
        symbol="AMD",
        direction="long",
        risk_check_id="r",
        qty=2,
        entry=100,
        stop=90,
        target=120,
        created_at_utc="2026-09-01T14:02:00Z",
    )
    add(
        "risk_checks",
        risk_check_id="r",
        proposal_id="p",
        candidate_id="c",
        trade_id="t",
        result="pass",
        block_reasons_json=[],
        position_size=3,
        max_risk_amount=30,
        entry_price=100,
        stop_price=90,
        target_price=120,
        liquidity_check_result="not_rechecked",
        spread_check_result="not_rechecked",
        daily_loss_check_result="pass",
        max_trades_check_result="pass",
        max_open_positions_check_result="pass",
        created_at_utc="2026-09-01T14:03:00Z",
    )
    add(
        "approvals",
        approval_id="a",
        proposal_id="p",
        candidate_id="c",
        symbol="AMD",
        approved=1,
        freshness_ok=1,
        risk_ok=1,
        created_at_utc="2026-09-01T14:04:00Z",
    )
    add(
        "paper_orders",
        order_id="o",
        proposal_id="p",
        candidate_id="c",
        trade_id="t",
        symbol="AMD",
        direction="long",
        side="buy",
        risk_check_id="r",
        qty=2,
        execution_source="alpaca_paper",
        intended_entry_price=100,
        stop_loss_price=90,
        take_profit_price=120,
        created_at_utc="2026-09-01T14:05:00Z",
    )
    add(
        "positions",
        position_id="pos",
        order_id="o",
        proposal_id="p",
        candidate_id="c",
        eval_id="e",
        trade_id="t",
        symbol="AMD",
        direction="long",
    )
    add(
        "trade_outcomes",
        outcome_id="out",
        position_id="pos",
        proposal_id="p",
        candidate_id="c",
        trade_id="t",
        symbol="AMD",
        direction="long",
        created_at_utc="2026-09-02T14:00:00Z",
    )
    if final:
        detail = {
            "proposal_id": "p",
            "risk_check_id": "r",
            "stage": audit.FINAL_STAGE,
            "scope": audit.FINAL_SCOPE,
            "proposal_qty": 2,
            "market_snapshot_rechecked": False,
            "risk": {
                "approved": True,
                "block_reasons": [],
                "warnings": [],
                "sizing": {"shares": 3, "risk_budget": 30},
                "account_context": {
                    "equity": 3000,
                    "gross_committed_notional": 0,
                    "open_positions": 0,
                    "account_pnl_today": -5,
                },
            },
        }
        add(
            "system_events",
            event_id="event",
            severity="info",
            category="entry_risk_snapshot",
            message="saved",
            detail_json=detail,
            created_at_utc="2026-09-01T14:03:01Z",
        )
    return j.one("SELECT * FROM trade_outcomes")


@pytest.fixture
def linked(monkeypatch):
    j = JournalStore(":memory:")
    outcome = seed(j, monkeypatch)
    yield j, outcome
    j.close()


def result(linked):
    j, outcome = linked
    return audit.validate_origin_provenance(j, outcome)


def change_event(j, **changes):
    detail = json.loads(j.one("SELECT detail_json FROM system_events")["detail_json"])
    detail.update(changes)
    j.conn.execute("UPDATE system_events SET detail_json=?", (json.dumps(detail),))
    j.conn.commit()


def test_complete_recorded_chain_preserves_distinct_authenticity_and_no_side_effects(
    linked,
):
    j, _ = linked
    before = j.conn.total_changes
    one = result(linked)
    assert (
        one["complete"] and one["recorded_origin_status"] == "verified_recorded_origin"
    )
    assert (
        one["final_execution_risk"]["status"]
        == "verified_recorded_final_execution_risk"
    )
    assert one["ids"]["risk_check_id"] == "r" and one["ids"]["approval_id"] == "a"
    assert not any(
        one[k]
        for k in (
            "model_call_authenticated",
            "broker_fees_verified",
            "policy_updated",
            "causal_lesson_claimed",
        )
    )
    assert one == result(linked) and j.conn.total_changes == before
    assert not j.conn.in_transaction


@pytest.mark.parametrize("decision", ["watch", "reject", "propose"])
def test_primary_decision_is_not_combined_execution_decision(linked, decision):
    linked[0].conn.execute("UPDATE openai_evaluations SET decision=?", (decision,))
    assert result(linked)["complete"]


@pytest.mark.parametrize(
    "field",
    [
        "model",
        "model_provider",
        "prompt_hash",
        "system_prompt_hash",
        "prompt_template_version",
        "schema_version",
    ],
)
def test_missing_recorded_model_identity_is_not_authenticated_or_completed(
    linked, field
):
    linked[0].conn.execute(f"UPDATE openai_evaluations SET {field}=NULL")
    r = result(linked)
    assert not r["complete"] and f"evaluation_{field}_missing" in r["limitations"]


@pytest.mark.parametrize("value", ["g" * 16, "a" * 64, "", 123])
def test_only_documented_hash_format_is_accepted(linked, value):
    linked[0].conn.execute("UPDATE openai_evaluations SET prompt_hash=?", (value,))
    assert not result(linked)["complete"]


@pytest.mark.parametrize(
    "table,prefix", [("openai_evaluations", "evaluation"), ("risk_checks", "risk")]
)
@pytest.mark.parametrize(
    "timestamp,code",
    [
        ("2099-01-01T00:00:00Z", "timestamp_future"),
        ("2026-09-01T14:06:00Z", "after"),
        ("2026-09-01T14:01:00", "timestamp_naive"),
        ("not-a-date", "timestamp_malformed"),
        (None, "timestamp_missing"),
    ],
)
def test_chronology_blindspots_become_explicit_limits(
    linked, table, prefix, timestamp, code
):
    linked[0].conn.execute(
        f"UPDATE {table} SET created_at_utc=?",
        (timestamp if timestamp is not None else "",),
    )
    r = result(linked)
    assert not r["complete"] and any(
        g.startswith(prefix + "_") and code in g for g in r["limitations"]
    )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE risk_checks SET result='fail',fail_reason='RISK_OVERSIZED'",
        'UPDATE risk_checks SET block_reasons_json=\'[{"code":"RISK_OVERSIZED"}]\'',
        "UPDATE risk_checks SET block_reasons_json='{}'",
        "UPDATE risk_checks SET fail_reason='RISK_OVERSIZED'",
        "UPDATE risk_checks SET proposal_id='foreign'",
        "UPDATE risk_checks SET trade_id='foreign'",
        "UPDATE paper_orders SET risk_check_id=NULL",
        "UPDATE trade_proposals SET risk_check_id='foreign'",
        "UPDATE positions SET order_id='foreign'",
        "UPDATE openai_evaluations SET candidate_id='foreign'",
        "UPDATE openai_evaluations SET symbol='foreign'",
        "UPDATE positions SET eval_id='foreign'",
        "UPDATE paper_orders SET side='sell'",
        "UPDATE approvals SET symbol='foreign'",
        "UPDATE approvals SET freshness_ok=0",
        "UPDATE approvals SET created_at_utc='2026-09-01T14:01:00Z'",
    ],
)
def test_explicit_identity_risk_and_approval_contradictions_cannot_complete(
    linked, sql
):
    linked[0].conn.execute(sql)
    assert not result(linked)["complete"]


def test_old_unpinned_risk_event_is_not_inferred(linked):
    j, _ = linked
    change_event(j, risk_check_id=None, stage=None)
    r = result(linked)
    assert r["recorded_origin_status"] == "verified_recorded_origin"
    assert r["limitations"] == ["final_execution_risk_event_unavailable"]
    assert not r["complete"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("proposal_id", "foreign"),
        ("stage", "proposal"),
        ("scope", "all_risk"),
        ("proposal_qty", 3),
        ("proposal_qty", True),
        ("market_snapshot_rechecked", 1),
        ("risk", {"approved": False, "block_reasons": []}),
        ("risk", None),
    ],
)
def test_final_exact_receipt_fields_not_nearby_events(linked, field, value):
    change_event(linked[0], **{field: value})
    assert not result(linked)["complete"]


@pytest.mark.parametrize(
    "field,value", [("shares", 1), ("shares", 4), ("risk_budget", 31)]
)
def test_final_risk_sizing_and_budget_are_bound_to_row(linked, field, value):
    j, _ = linked
    detail = json.loads(j.one("SELECT detail_json FROM system_events")["detail_json"])
    detail["risk"]["sizing"][field] = value
    change_event(j, risk=detail["risk"])
    assert not result(linked)["complete"]


def test_real_refresh_scope_requires_pass_account_only_requires_not_rechecked(linked):
    j, _ = linked
    change_event(j, market_snapshot_rechecked=True)
    assert not result(linked)["complete"]
    j.conn.execute(
        "UPDATE risk_checks SET liquidity_check_result='pass',spread_check_result='pass'"
    )
    assert result(linked)["complete"]


def test_final_risk_precedes_event_and_event_precedes_submission(linked):
    j, _ = linked
    j.conn.execute("UPDATE system_events SET created_at_utc='2026-09-01T14:02:59Z'")
    assert "final_execution_risk_event_before_risk" in result(linked)["limitations"]
    j.conn.execute("UPDATE system_events SET created_at_utc='2026-09-01T14:05:01Z'")
    assert "final_execution_risk_event_after_entry" in result(linked)["limitations"]


def test_duplicate_final_receipt_is_ambiguous(linked):
    j, _ = linked
    row = j.one("SELECT * FROM system_events")
    row.pop("id")
    row["event_id"] = "duplicate"
    j.insert("system_events", row)
    assert "final_execution_risk_event_ambiguous" in result(linked)["limitations"]


def test_pinned_optional_claude_uses_actual_fields_no_invented_schema(linked):
    j, _ = linked
    j.conn.execute("UPDATE trade_proposals SET claude_review_id='cl'")
    assert not result(linked)["complete"]
    j.insert(
        "claude_reviews",
        {
            "review_id": "cl",
            "candidate_id": "c",
            "eval_id": "e",
            "symbol": "AMD",
            "model": "claude",
            "model_provider": "anthropic",
            "prompt_hash": "a" * 16,
            "system_prompt_hash": "b" * 16,
            "created_at_utc": "2026-09-01T14:01:30Z",
        },
    )
    assert result(linked)["complete"]
    j.conn.execute("UPDATE claude_reviews SET eval_id='foreign'")
    assert not result(linked)["complete"]


def test_missing_input_and_supplied_foreign_outcome_do_not_crash(linked):
    j, o = linked
    assert not audit.validate_origin_provenance(j, None)["complete"]
    assert not audit.validate_origin_provenance(j, {**o, "symbol": "foreign"})[
        "complete"
    ]


def test_later_evaluation_does_not_replace_exact_anchor(linked):
    j, _ = linked
    j.insert(
        "openai_evaluations",
        {
            "eval_id": "later",
            "candidate_id": "c",
            "symbol": "AMD",
            "decision": "propose",
        },
    )
    j.conn.execute("UPDATE openai_evaluations SET model=NULL WHERE eval_id='e'")
    assert "evaluation_model_missing" in result(linked)["limitations"]


def test_query_only_and_nested_transaction_never_commit_user_changes(linked):
    j, _ = linked
    j.conn.execute("UPDATE openai_evaluations SET decision='reject'")
    assert j.conn.in_transaction
    assert result(linked)["complete"] and j.conn.in_transaction
    j.conn.rollback()
    assert j.one("SELECT decision FROM openai_evaluations")["decision"] == "watch"
    j.conn.execute("PRAGMA query_only=ON")
    assert result(linked)["complete"]


def test_missing_schema_is_database_failure_not_silent_limited_evidence(linked):
    j, _ = linked
    j.conn.execute("DROP TABLE approvals")
    with pytest.raises(sqlite3.OperationalError):
        result(linked)
    assert not j.conn.in_transaction


def test_single_snapshot_excludes_concurrent_anchor_change(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.db"
    j = JournalStore(str(path))
    outcome = seed(j, monkeypatch)
    other = sqlite3.connect(path)
    original = j.query
    changed = False

    def query(sql, params=()):
        nonlocal changed
        rows = original(sql, params)
        if "FROM trade_outcomes " in sql and not changed:
            changed = True
            other.execute("UPDATE risk_checks SET result='fail'")
            other.commit()
        return rows

    proxy = SimpleNamespace(conn=j.conn, query=query)
    assert audit.validate_origin_provenance(proxy, outcome)["complete"]
    assert not audit.validate_origin_provenance(j, outcome)["complete"]
    other.close()
    j.close()


@pytest.mark.parametrize(
    "table,field",
    [
        ("risk_checks", "entry_price"),
        ("risk_checks", "stop_price"),
        ("risk_checks", "target_price"),
        ("trade_proposals", "entry"),
        ("trade_proposals", "stop"),
        ("trade_proposals", "target"),
        ("paper_orders", "intended_entry_price"),
        ("paper_orders", "stop_loss_price"),
        ("paper_orders", "take_profit_price"),
        ("paper_orders", "qty"),
        ("trade_proposals", "qty"),
        ("risk_checks", "position_size"),
    ],
)
def test_original_risk_terms_and_requested_size_must_match_saved_intent(
    linked, table, field
):
    linked[0].conn.execute(f"UPDATE {table} SET {field}=1")
    assert not result(linked)["complete"]


def test_fill_slippage_and_broker_rounding_never_replace_original_risk_basis(linked):
    j, _ = linked
    j.conn.execute("UPDATE trade_proposals SET entry=100.123456")
    j.conn.execute("UPDATE risk_checks SET entry_price=100.123456")
    j.conn.execute(
        "UPDATE paper_orders SET intended_entry_price=100.123456,limit_price=100.12,entry_price=100.10"
    )
    assert result(linked)["complete"]


@pytest.mark.parametrize("side", ["sell", "sell_short"])
def test_short_entry_aliases_are_supported_without_approving_the_policy(linked, side):
    j, o = linked
    for table in ("trade_proposals", "positions", "paper_orders", "trade_outcomes"):
        j.conn.execute(f"UPDATE {table} SET direction='short'")
    j.conn.execute("UPDATE paper_orders SET side=?", (side,))
    o["direction"] = "short"
    assert result(linked)["complete"]


def test_recorded_risk_may_precede_proposal_or_follow_approval(linked):
    j, _ = linked
    j.conn.execute("UPDATE risk_checks SET created_at_utc='2026-09-01T14:01:59Z'")
    r = result(linked)
    assert r["recorded_origin_status"] == "verified_recorded_origin"
    assert "final_execution_risk_before_proposal" in r["limitations"]
    j.conn.execute("UPDATE risk_checks SET created_at_utc='2026-09-01T14:04:01Z'")
    j.conn.execute("UPDATE system_events SET created_at_utc='2026-09-01T14:04:02Z'")
    assert result(linked)["complete"]


@pytest.mark.parametrize(
    "context",
    [
        {"unrelated": True},
        {"equity": -1},
        {"equity": True},
        {"equity": "unknown"},
        {
            "equity": 3000,
            "gross_committed_notional": -1,
            "open_positions": 0,
            "account_pnl_today": 0,
        },
        {
            "equity": 3000,
            "gross_committed_notional": 0,
            "open_positions": 0.5,
            "account_pnl_today": 0,
        },
        {
            "equity": 3000,
            "gross_committed_notional": 0,
            "open_positions": 0,
            "account_pnl_today": "nan",
        },
    ],
)
def test_final_context_validates_actual_input_dimensions_not_policy(linked, context):
    j, _ = linked
    detail = json.loads(j.one("SELECT detail_json FROM system_events")["detail_json"])
    detail["risk"]["account_context"] = context
    change_event(j, risk=detail["risk"])
    assert (
        "final_execution_risk_account_context_unverifiable"
        in result(linked)["limitations"]
    )
