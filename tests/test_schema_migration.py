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
