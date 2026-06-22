"""SQLite schema (DDL) for the AlphaOS journal.

SQLite is the source of truth (resolved decision #4). Every event carries UTC +
Asia/Singapore + market-timezone stamps where relevant. History is append-only:
state changes are recorded as new rows in ``order_events`` even though the
current state is also denormalized onto ``paper_orders`` for cheap reads.

The 18 tables named in the spec are all present; ``order_events`` is added to
back the order lifecycle (the shared mock/Alpaca event schema).
"""

from __future__ import annotations

# Ordered list of (name, DDL). Kept as data so tests can assert table presence.
SCHEMA: list[tuple[str, str]] = [
    (
        "config_versions",
        """
        CREATE TABLE IF NOT EXISTS config_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_hash TEXT NOT NULL,
            mode TEXT NOT NULL,
            approval_mode TEXT NOT NULL,
            real_trading_enabled_raw TEXT NOT NULL,
            config_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        "system_events",
        """
        CREATE TABLE IF NOT EXISTS system_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            severity TEXT NOT NULL,
            category TEXT NOT NULL,
            message TEXT NOT NULL,
            detail_json TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL,
            created_at_market TEXT
        )
        """,
    ),
    (
        "universe",
        """
        CREATE TABLE IF NOT EXISTS universe (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            asset_class TEXT,
            tier TEXT,
            is_active INTEGER DEFAULT 1,
            avg_dollar_volume REAL,
            scan_id TEXT,
            notes TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        "price_snapshots",
        """
        CREATE TABLE IF NOT EXISTS price_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            provider TEXT NOT NULL,
            feed TEXT,
            is_mock INTEGER DEFAULT 0,
            last_price REAL,
            bid REAL,
            ask REAL,
            spread REAL,
            spread_pct REAL,
            volume REAL,
            dollar_volume REAL,
            bar_open REAL,
            bar_high REAL,
            bar_low REAL,
            bar_close REAL,
            quote_timestamp TEXT,
            bar_timestamp TEXT,
            quote_age_seconds REAL,
            bar_age_seconds REAL,
            source_timestamp TEXT,
            received_at TEXT,
            data_delay_seconds REAL,
            market_session TEXT,
            freshness_status TEXT,
            is_usable INTEGER,
            block_reason TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL,
            created_at_market TEXT
        )
        """,
    ),
    (
        "news_items",
        """
        CREATE TABLE IF NOT EXISTS news_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            provider TEXT NOT NULL,
            source_url TEXT,
            source_name TEXT,
            headline TEXT,
            published_at TEXT,
            fetched_at TEXT,
            summary TEXT,
            sentiment TEXT,
            catalyst_type TEXT,
            timestamp_confidence TEXT,
            parsing_notes TEXT,
            is_fixture INTEGER DEFAULT 0,
            label TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        "candidates",
        """
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id TEXT NOT NULL UNIQUE,
            scan_id TEXT,
            symbol TEXT NOT NULL,
            direction TEXT,
            strategy TEXT,
            momentum_score REAL,
            rel_strength REAL,
            unusual_volume REAL,
            trend_quality REAL,
            liquidity_ok INTEGER,
            spread_ok INTEGER,
            news_status TEXT,
            price_snapshot_id TEXT,
            status TEXT,
            notes_json TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        "openai_evaluations",
        """
        CREATE TABLE IF NOT EXISTS openai_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            eval_id TEXT NOT NULL UNIQUE,
            candidate_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            model TEXT,
            direction TEXT,
            entry REAL,
            stop REAL,
            target REAL,
            max_holding_days INTEGER,
            expected_r REAL,
            confidence REAL,
            decision TEXT,
            reasoning_summary TEXT,
            news_sources_json TEXT,
            data_freshness_status TEXT,
            catalyst_type TEXT,
            news_status TEXT,
            sentiment TEXT,
            risk_flags_json TEXT,
            validation_status TEXT,
            raw_json TEXT,
            is_mock INTEGER DEFAULT 0,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        "claude_reviews",
        """
        CREATE TABLE IF NOT EXISTS claude_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id TEXT NOT NULL UNIQUE,
            candidate_id TEXT NOT NULL,
            eval_id TEXT,
            symbol TEXT NOT NULL,
            model TEXT,
            verdict TEXT,
            agrees_with_openai INTEGER,
            risk_flags_json TEXT,
            reasoning TEXT,
            raw_json TEXT,
            is_mock INTEGER DEFAULT 0,
            triggered_by TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        "trade_proposals",
        """
        CREATE TABLE IF NOT EXISTS trade_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id TEXT NOT NULL UNIQUE,
            candidate_id TEXT NOT NULL,
            eval_id TEXT,
            symbol TEXT NOT NULL,
            direction TEXT,
            strategy TEXT,
            entry REAL,
            stop REAL,
            target REAL,
            max_holding_days INTEGER,
            qty REAL,
            risk_per_share REAL,
            dollar_risk REAL,
            expected_r REAL,
            same_day_exit_eligible INTEGER,
            requires_margin INTEGER DEFAULT 0,
            margin_approved INTEGER DEFAULT 0,
            protection_path TEXT,
            status TEXT,
            is_demo INTEGER DEFAULT 0,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        "approvals",
        """
        CREATE TABLE IF NOT EXISTS approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            approval_id TEXT NOT NULL UNIQUE,
            proposal_id TEXT NOT NULL,
            candidate_id TEXT,
            symbol TEXT,
            approval_mode TEXT,
            label TEXT,
            approved INTEGER,
            approver TEXT,
            reason TEXT,
            freshness_ok INTEGER,
            risk_ok INTEGER,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        "paper_orders",
        """
        CREATE TABLE IF NOT EXISTS paper_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL UNIQUE,
            client_order_id TEXT,
            broker_order_id TEXT,
            proposal_id TEXT,
            candidate_id TEXT,
            symbol TEXT NOT NULL,
            direction TEXT,
            side TEXT,
            order_type TEXT,
            qty REAL,
            limit_price REAL,
            entry_price REAL,
            take_profit_price REAL,
            stop_loss_price REAL,
            time_in_force TEXT,
            execution_source TEXT,
            execution_provider TEXT,
            execution_mode TEXT,
            data_provider TEXT,
            data_feed TEXT,
            fill_price_basis TEXT,
            protection_path TEXT,
            state TEXT,
            requires_margin INTEGER DEFAULT 0,
            is_short INTEGER DEFAULT 0,
            strategy TEXT,
            is_demo INTEGER DEFAULT 0,
            submitted_at TEXT,
            accepted_at TEXT,
            filled_at TEXT,
            raw_request_json TEXT,
            raw_response_json TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL,
            created_at_market TEXT
        )
        """,
    ),
    (
        "order_events",
        """
        CREATE TABLE IF NOT EXISTS order_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            order_id TEXT NOT NULL,
            broker_order_id TEXT,
            prev_state TEXT,
            new_state TEXT NOT NULL,
            execution_source TEXT,
            message TEXT,
            detail_json TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        "paper_fills",
        """
        CREATE TABLE IF NOT EXISTS paper_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fill_id TEXT NOT NULL UNIQUE,
            order_id TEXT NOT NULL,
            broker_order_id TEXT,
            symbol TEXT NOT NULL,
            side TEXT,
            qty REAL,
            price REAL,
            commission REAL DEFAULT 0,
            execution_source TEXT,
            execution_provider TEXT,
            data_provider TEXT,
            data_feed TEXT,
            fill_source TEXT,
            fill_price_basis TEXT,
            filled_at TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        "positions",
        """
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id TEXT NOT NULL UNIQUE,
            order_id TEXT,
            symbol TEXT NOT NULL,
            direction TEXT,
            strategy TEXT,
            qty REAL,
            avg_entry_price REAL,
            stop_price REAL,
            target_price REAL,
            max_holding_days INTEGER,
            opened_at TEXT,
            opened_market_date TEXT,
            status TEXT,
            current_price REAL,
            unrealized_pnl REAL,
            execution_source TEXT,
            broker_order_id TEXT,
            is_short INTEGER DEFAULT 0,
            requires_margin INTEGER DEFAULT 0,
            is_demo INTEGER DEFAULT 0,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        "exits",
        """
        CREATE TABLE IF NOT EXISTS exits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exit_id TEXT NOT NULL UNIQUE,
            position_id TEXT NOT NULL,
            order_id TEXT,
            exit_order_id TEXT,
            symbol TEXT NOT NULL,
            exit_price REAL,
            qty REAL,
            exit_reason TEXT,
            classification TEXT,
            is_same_day INTEGER,
            triggered_by TEXT,
            market_date TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        "trade_outcomes",
        """
        CREATE TABLE IF NOT EXISTS trade_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outcome_id TEXT NOT NULL UNIQUE,
            position_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT,
            strategy TEXT,
            entry_price REAL,
            exit_price REAL,
            qty REAL,
            gross_pnl REAL,
            costs REAL DEFAULT 0,
            net_pnl REAL,
            return_pct REAL,
            realized_r REAL,
            holding_days REAL,
            is_same_day INTEGER,
            classification TEXT,
            mfe REAL,
            mae REAL,
            model_confidence REAL,
            catalyst_type TEXT,
            win INTEGER,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        "rejected_candidates",
        """
        CREATE TABLE IF NOT EXISTS rejected_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rejection_id TEXT NOT NULL UNIQUE,
            candidate_id TEXT,
            symbol TEXT NOT NULL,
            stage TEXT,
            reason_code TEXT,
            reason_detail TEXT,
            direction TEXT,
            would_be_entry REAL,
            would_be_stop REAL,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        "baseline_outcomes",
        """
        CREATE TABLE IF NOT EXISTS baseline_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            baseline_id TEXT NOT NULL UNIQUE,
            candidate_id TEXT,
            symbol TEXT NOT NULL,
            baseline_type TEXT,
            direction TEXT,
            reference_price REAL,
            ref_timestamp TEXT,
            ai_decision TEXT,
            claude_consulted INTEGER DEFAULT 0,
            news_status TEXT,
            catalyst TEXT,
            no_news_baseline INTEGER,
            news_confirmed_subset INTEGER,
            news_provider TEXT,
            news_sources TEXT,
            catalyst_type TEXT,
            catalyst_confidence REAL,
            notes_json TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        "daily_learning_reports",
        """
        CREATE TABLE IF NOT EXISTS daily_learning_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id TEXT NOT NULL UNIQUE,
            report_date TEXT NOT NULL,
            mode TEXT,
            summary TEXT,
            metrics_json TEXT,
            proposals_count INTEGER,
            approvals_count INTEGER,
            rejections_count INTEGER,
            blocks_count INTEGER,
            fills_count INTEGER,
            net_pnl REAL,
            content_md TEXT,
            generated_by TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
]

INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_candidates_scan ON candidates(scan_id)",
    "CREATE INDEX IF NOT EXISTS idx_candidates_symbol ON candidates(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_evals_candidate ON openai_evaluations(candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_candidate ON claude_reviews(candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_proposals_candidate ON trade_proposals(candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_proposal ON paper_orders(proposal_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_state ON paper_orders(state)",
    "CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_fills_order ON paper_fills(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)",
    "CREATE INDEX IF NOT EXISTS idx_exits_position ON exits(position_id)",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_position ON trade_outcomes(position_id)",
    "CREATE INDEX IF NOT EXISTS idx_sysevents_sev ON system_events(severity)",
    "CREATE INDEX IF NOT EXISTS idx_approvals_label ON approvals(label)",
]

# Canonical table-name list (used by tests to assert completeness).
ALL_TABLES = [name for name, _ in SCHEMA]

# Schema generation marker, recorded in SQLite's ``PRAGMA user_version``.
#
# Additive column changes do NOT require bumping this: JournalStore reconciles
# any column present in SCHEMA but missing from an existing DB automatically on
# open (see ``JournalStore._migrate``), so a ledger written by an older build
# keeps working across pulls. Bump it only when introducing a change the additive
# reconciler cannot express (a destructive/transforming migration) and add that
# explicit, version-gated step in ``_migrate``.
SCHEMA_VERSION = 1
