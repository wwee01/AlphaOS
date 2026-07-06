"""Config startup-safety and database schema completeness."""

from __future__ import annotations

from alphaos.journal.schema import ALL_TABLES
from conftest import make_settings


REQUIRED_TABLES = {
    "universe", "price_snapshots", "news_items", "candidates", "openai_evaluations",
    "claude_reviews", "trade_proposals", "approvals", "paper_orders", "paper_fills",
    "positions", "exits", "trade_outcomes", "rejected_candidates", "baseline_outcomes",
    "daily_learning_reports", "system_events", "config_versions",
}


def test_all_required_tables_present(journal):
    existing = {
        r["name"]
        for r in journal.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = REQUIRED_TABLES - existing
    assert not missing, f"missing tables: {missing}"
    assert REQUIRED_TABLES.issubset(set(ALL_TABLES))


def test_mock_startup_is_ok_without_keys():
    s = make_settings()
    assert s.startup_ok() is True


def test_paper_mode_without_keys_refuses_execution():
    s = make_settings(ALPHAOS_MODE="paper")  # no alpaca keys
    ok, failing = s.paper_execution_allowed()
    assert ok is False
    assert any(c.name in ("alpaca_credentials", "alpaca_base_url", "alpaca_paper_flag") for c in failing)


def test_paper_mode_with_full_safe_config_allows_execution():
    s = make_settings(
        ALPHAOS_MODE="paper",
        ALPACA_PAPER="true",
        ALPACA_BASE_URL="https://paper-api.alpaca.markets",
        ALPACA_API_KEY="k",
        ALPACA_SECRET_KEY="s",
        REAL_TRADING_ENABLED="false",
    )
    ok, failing = s.paper_execution_allowed()
    assert ok is True, [f.name for f in failing]


def test_config_version_recorded(journal, settings):
    journal.record_config_version(settings)
    rows = journal.query("SELECT * FROM config_versions")
    assert len(rows) == 1
    assert rows[0]["real_trading_enabled_raw"] == "false"
    assert "openai_api_key" not in rows[0]["config_json"]  # secrets never stored


def test_config_hash_is_deterministic_across_independent_journal_instances(settings):
    """PR9.5 fix: builtin hash() on a str is PYTHONHASHSEED-randomized (a
    security default since Python 3.3) -- the SAME config produced a
    DIFFERENT config_hash every process restart, defeating the point of a
    config-change fingerprint. Must now use the codebase's own deterministic
    stable_hash convention instead (already used correctly elsewhere by
    build_config_hashes()). Two INDEPENDENT JournalStore instances (standing
    in for two separate process runs) must agree on the hash for identical
    config."""
    from alphaos.journal.journal_store import JournalStore

    j1 = JournalStore(":memory:")
    j2 = JournalStore(":memory:")
    j1.record_config_version(settings)
    j2.record_config_version(settings)
    hash1 = j1.one("SELECT config_hash FROM config_versions")["config_hash"]
    hash2 = j2.one("SELECT config_hash FROM config_versions")["config_hash"]

    assert hash1 == hash2
    assert len(hash1) == 16  # stable_hash's fixed hex-digest length
    assert all(c in "0123456789abcdef" for c in hash1)  # hex, not the old decimal-mod format
    j1.close()
    j2.close()
