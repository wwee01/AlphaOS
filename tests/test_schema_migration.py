"""Schema migration: a DB written by an older build (missing newer columns) is
reconciled on open, so the ledger/positions schema stays stable across pulls
(the bug that broke position-open after pulling the real-execution columns)."""

from __future__ import annotations

import sqlite3

from alphaos.journal.journal_store import JournalStore
from alphaos.journal.schema import SCHEMA_VERSION


def test_missing_columns_added_on_open(tmp_path):
    db = str(tmp_path / "stale.db")
    # Simulate an older ledger: a positions table that predates the
    # execution-tracking columns added by the real-Alpaca-execution change.
    raw = sqlite3.connect(db)
    raw.execute(
        "CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "position_id TEXT, symbol TEXT, qty REAL, status TEXT)"
    )
    raw.execute("PRAGMA user_version = 0")
    raw.commit()
    raw.close()

    j = JournalStore(db)
    try:
        cols = {r["name"] for r in j.conn.execute("PRAGMA table_info(positions)")}
        assert "execution_source" in cols
        assert "broker_order_id" in cols
        assert j.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        j.close()


def test_fresh_db_stamped_and_complete(tmp_path):
    j = JournalStore(str(tmp_path / "fresh.db"))
    try:
        assert j.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        cols = {r["name"] for r in j.conn.execute("PRAGMA table_info(positions)")}
        assert {"execution_source", "broker_order_id"} <= cols
    finally:
        j.close()


def test_migration_idempotent_on_reopen(tmp_path):
    db = str(tmp_path / "x.db")
    JournalStore(db).close()
    j = JournalStore(db)  # second open must not error or churn
    try:
        assert j.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        j.close()


def test_job_runs_table_exists_and_schema_version_unchanged(tmp_path):
    """Scheduler v1.5 (PR3) added the job_runs table additively -- SCHEMA_VERSION
    must not have moved for a purely additive migration."""
    j = JournalStore(str(tmp_path / "job_runs.db"))
    try:
        tables = {r["name"] for r in j.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        assert "job_runs" in tables
        assert j.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert SCHEMA_VERSION == 3
    finally:
        j.close()


def test_benchmark_spine_tables_exist_on_a_fresh_db_and_schema_version_unchanged(tmp_path):
    """PR9.5's equity_snapshots/benchmark_bars are additive -- SCHEMA_VERSION
    must not have moved (still 3)."""
    j = JournalStore(str(tmp_path / "fresh_benchmark.db"))
    try:
        tables = {r["name"] for r in j.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        assert {"equity_snapshots", "benchmark_bars"} <= tables
        assert j.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert SCHEMA_VERSION == 3
    finally:
        j.close()


def test_benchmark_spine_tables_added_to_a_pre_pr9_5_db(tmp_path):
    """An old ledger written before PR9.5 (no equity_snapshots/benchmark_bars
    tables at all) must gain both additively on open, exactly like every
    other post-hoc table addition in this codebase's history."""
    db = str(tmp_path / "pre_pr9_5.db")
    raw = sqlite3.connect(db)
    raw.execute(
        "CREATE TABLE system_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_id TEXT NOT NULL UNIQUE, severity TEXT NOT NULL, category TEXT NOT NULL, "
        "message TEXT NOT NULL, detail_json TEXT, created_at_utc TEXT NOT NULL, "
        "created_at_sgt TEXT NOT NULL, created_at_market TEXT)"
    )
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    j = JournalStore(db)
    try:
        tables = {r["name"] for r in j.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        assert {"equity_snapshots", "benchmark_bars"} <= tables
        # And they're immediately usable, not just present.
        j.insert("equity_snapshots", {
            "snapshot_id": "eq1", "market_date": "2026-07-06", "equity": 100000.0,
            "equity_source": "static_config",
        })
        assert j.count_rows("equity_snapshots") == 1
    finally:
        j.close()


def test_benchmark_spine_unique_indexes_enforce_idempotency(tmp_path):
    """One equity snapshot per market_date; one bar per (symbol, date) --
    both plain UNIQUE (never NULL columns, so no NULL-uniqueness trap here,
    unlike the partial-index cases elsewhere in this codebase)."""
    import pytest

    j = JournalStore(str(tmp_path / "unique_test.db"))
    try:
        j.insert("equity_snapshots", {
            "snapshot_id": "eq1", "market_date": "2026-07-06", "equity": 100000.0,
            "equity_source": "static_config",
        })
        with pytest.raises(sqlite3.IntegrityError):
            j.insert("equity_snapshots", {
                "snapshot_id": "eq2", "market_date": "2026-07-06", "equity": 999.0,
                "equity_source": "static_config",
            })

        j.insert("benchmark_bars", {"bar_id": "b1", "symbol": "SPY", "bar_date": "2026-07-06", "close": 500.0})
        with pytest.raises(sqlite3.IntegrityError):
            j.insert("benchmark_bars", {"bar_id": "b2", "symbol": "SPY", "bar_date": "2026-07-06", "close": 501.0})
    finally:
        j.close()


def test_ai_token_usage_columns_added_to_a_pre_pr9_5_db(tmp_path):
    """PR9.5 added prompt_tokens/completion_tokens/total_tokens to
    openai_evaluations, candidate_labels, and last30days_polarity. An old
    ledger predating those columns must gain them additively on open."""
    db = str(tmp_path / "pre_pr9_5_tokens.db")
    raw = sqlite3.connect(db)
    raw.execute(
        "CREATE TABLE openai_evaluations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "eval_id TEXT NOT NULL UNIQUE, candidate_id TEXT NOT NULL, symbol TEXT NOT NULL, "
        "created_at_utc TEXT NOT NULL, created_at_sgt TEXT NOT NULL)"
    )
    raw.execute(
        "CREATE TABLE candidate_labels (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "label_id TEXT NOT NULL UNIQUE, candidate_id TEXT NOT NULL, symbol TEXT NOT NULL, "
        "created_at_utc TEXT NOT NULL, created_at_sgt TEXT NOT NULL)"
    )
    raw.execute(
        "CREATE TABLE last30days_polarity (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "polarity_id TEXT NOT NULL UNIQUE, candidate_id TEXT NOT NULL, symbol TEXT NOT NULL, "
        "created_at_utc TEXT NOT NULL, created_at_sgt TEXT NOT NULL)"
    )
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    j = JournalStore(db)
    try:
        for table in ("openai_evaluations", "candidate_labels", "last30days_polarity"):
            cols = {r["name"] for r in j.conn.execute(f"PRAGMA table_info({table})")}
            assert {"prompt_tokens", "completion_tokens", "total_tokens"} <= cols, table
    finally:
        j.close()


def test_exp_0_columns_and_table_added_to_a_pre_exp_0_db(tmp_path):
    """EXP-0 added recent_ipo/spac_flag/universe_file_version to `universe`,
    shadow_tier/instrument_version to `candidates`, and the new universe_days
    table -- all additive, SCHEMA_VERSION must not have moved."""
    db = str(tmp_path / "pre_exp_0.db")
    raw = sqlite3.connect(db)
    raw.execute(
        "CREATE TABLE universe (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, "
        "tier TEXT, created_at_utc TEXT NOT NULL, created_at_sgt TEXT NOT NULL)"
    )
    raw.execute(
        "CREATE TABLE candidates (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "candidate_id TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL, "
        "created_at_utc TEXT NOT NULL, created_at_sgt TEXT NOT NULL)"
    )
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    j = JournalStore(db)
    try:
        universe_cols = {r["name"] for r in j.conn.execute("PRAGMA table_info(universe)")}
        assert {"recent_ipo", "spac_flag", "universe_file_version"} <= universe_cols
        candidate_cols = {r["name"] for r in j.conn.execute("PRAGMA table_info(candidates)")}
        assert {"shadow_tier", "instrument_version"} <= candidate_cols

        tables = {r["name"] for r in j.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        assert "universe_days" in tables
        j.insert("universe_days", {
            "universe_day_id": "ud1", "market_date": "2026-07-08", "symbol": "AAAA", "tier": "watchlist",
        })
        assert j.count_rows("universe_days") == 1
        assert j.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert SCHEMA_VERSION == 3
    finally:
        j.close()


def test_ab_eval_tables_added_to_a_pre_ab_eval_db(tmp_path):
    """AB-EVAL-1 added ab_eval_runs/ab_eval_results additively -- opening a
    pre-AB-EVAL-1 DB must create both tables (CREATE TABLE IF NOT EXISTS),
    accept inserts, and SCHEMA_VERSION must not have moved (still 3)."""
    db = str(tmp_path / "pre_ab_eval.db")
    raw = sqlite3.connect(db)
    raw.execute(
        "CREATE TABLE candidates (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "candidate_id TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL, "
        "created_at_utc TEXT NOT NULL, created_at_sgt TEXT NOT NULL)"
    )
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    j = JournalStore(db)
    try:
        tables = {r["name"] for r in j.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        assert {"ab_eval_runs", "ab_eval_results"} <= tables
        j.insert("ab_eval_runs", {
            "ab_run_id": "abrun_mig1", "corpus_dir": "data/ab_eval", "is_mock": 1,
            "n_packets": 0, "started_at_utc": "2026-07-20T00:00:00+00:00",
            "started_at_sgt": "2026-07-20T08:00:00+08:00",
        })
        j.insert("ab_eval_results", {
            "ab_result_id": "abres_mig1", "ab_run_id": "abrun_mig1", "eval_id": "eval_mig1",
            "symbol": "AAPL", "model": "gpt-5.4-mini",
        })
        assert j.count_rows("ab_eval_runs") == 1
        assert j.count_rows("ab_eval_results") == 1
        assert j.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert SCHEMA_VERSION == 3
    finally:
        j.close()
