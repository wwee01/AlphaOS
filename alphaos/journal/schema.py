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
            recent_ipo INTEGER DEFAULT 0,
            spac_flag INTEGER DEFAULT 0,
            universe_file_version INTEGER,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # EXP-0: the survivorship-bias law. One row per shadow-tier universe
        # member per TRADING DAY, written by the shadow-tier scan pass
        # REGARDLESS of whether that name produced a candidate that day --
        # this is the system's own point-in-time record of what it could see.
        # Delisted/dropped names simply stop appearing in new rows; existing
        # rows are never touched (append-only -- enforced by
        # idx_universe_days_symbol_date below + application-level insert-only
        # access, mirroring benchmark_bars' own idempotent-insert idiom). A
        # name observed more than once on the same trading date (multiple
        # scan windows) writes only its FIRST same-day observation; this
        # table answers "was this name in the universe and observed on this
        # date", not a rolling intraday summary -- per-window candidate
        # detail lives in the ordinary candidates table instead.
        "universe_days",
        """
        CREATE TABLE IF NOT EXISTS universe_days (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            universe_day_id TEXT NOT NULL UNIQUE,
            market_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            tier TEXT NOT NULL,
            universe_file_version INTEGER,
            recent_ipo INTEGER DEFAULT 0,
            spac_flag INTEGER DEFAULT 0,
            freshness_status TEXT,
            candidate_found INTEGER DEFAULT 0,
            candidate_id TEXT,
            instrument_version TEXT,
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
            price_used_for_decision REAL,
            price_at_order_submission REAL,
            price_move_since_proposal_pct REAL,
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
            candidate_id TEXT,
            source_type TEXT,
            catalyst_quality_score REAL,
            contradiction_flags TEXT,
            openai_news_classification_id TEXT,
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
            scan_batch_id TEXT,
            asset_type TEXT,
            playbook_name TEXT,
            setup_classification TEXT,
            card_id TEXT,
            card_version INTEGER,
            status_reason TEXT,
            block_reason TEXT,
            reject_reason TEXT,
            watch_reason TEXT,
            price_at_scan REAL,
            volume_at_scan REAL,
            market_regime TEXT,
            interest_score REAL,
            interest_rank INTEGER,
            shortlist_reason TEXT,
            primary_label TEXT,
            secondary_labels_json TEXT,
            candidate_tags_json TEXT,
            risk_tags_json TEXT,
            label_confidence REAL,
            label_decision TEXT,
            label_version TEXT,
            label_source TEXT,
            label_frozen_at_utc TEXT,
            catalyst_status TEXT,
            catalyst_type TEXT,
            catalyst_suggested_label TEXT,
            label_review_required INTEGER,
            last30days_status TEXT,
            sentiment_label TEXT,
            decision_adjustment TEXT,
            decision_adjustment_reason TEXT,
            polarity_label TEXT,
            polarity_alignment TEXT,
            narrative_driver_type TEXT,
            arming_classification TEXT,
            armed_watch INTEGER,
            earnings_date TEXT,
            days_until_earnings INTEGER,
            earnings_within_hold_window INTEGER,
            earnings_within_warning_window INTEGER,
            earnings_timing TEXT,
            earnings_data_status TEXT,
            lineage_id TEXT,
            shadow_tier INTEGER DEFAULT 0,
            instrument_version TEXT,
            -- EXP-1 mechanism 10: liquidity instrumentation -- RECORD, NEVER
            -- GATE. Persisted for every shadow-tier candidate row regardless
            -- of core_gate_verdict; NULL for core-tier rows (additive,
            -- shadow-only fields). See alphaos/scanner/candidate_scanner.py's
            -- scan_shadow_tier for the write path.
            bid_size REAL,
            ask_size REAL,
            quote_age_seconds REAL,
            spread_pct_mid REAL,
            adv_20d_dollar REAL,
            volume_today_pct_of_adv REAL,
            scan_window TEXT,
            data_feed TEXT,
            crossed_or_locked_quote INTEGER,
            -- What the CORE tradeability gate (MIN_DOLLAR_VOLUME/MAX_SPREAD_PCT/
            -- crossed-quote) would have decided -- computed but NEVER applied to
            -- shadow capture/selection eligibility (invariant, mechanism 10).
            core_gate_verdict TEXT,
            liquidity_instrumentation_version TEXT,
            -- EXP-1 mechanism 2: selection-arm stamping (top_k|explore) +
            -- the selection formula/version, so retuning K/fraction/formula
            -- without a version bump is detectable as selection p-hacking.
            selection_arm TEXT,
            selection_version TEXT,
            -- EXP-1 mechanism 8: per-row feed-coverage-at-scan-time (NOT the
            -- trailing 14-day aggregate the arming gate uses) + why a
            -- selected shadow candidate was never sent to the labeller
            -- (e.g. 'stale') -- NULL when a real AI call was made or the
            -- candidate was never selected at all.
            feed_coverage_at_scan REAL,
            label_skipped_reason TEXT,
            -- EXP-1 mechanism 11: parameterization-only placeholder for a
            -- future sector-clustering key (no sector metadata exists yet
            -- anywhere in this codebase -- UNIV-D/TEXT-0 owns populating
            -- this later). Nullable; no logic reads it yet.
            sector_cluster_key TEXT,
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
            prompt_template_version TEXT,
            schema_version TEXT,
            thesis_summary TEXT,
            counter_thesis TEXT,
            expected_hold_days INTEGER,
            same_day_exit_allowed INTEGER,
            reasons_to_reject TEXT,
            lineage_id TEXT,
            model_provider TEXT,
            prompt_hash TEXT,
            system_prompt_hash TEXT,
            -- PR9.5: real token usage for cost accounting (cost_guard
            -- previously only counted rows in THIS table, which happened to
            -- be the complete AI spend before the labeller/polarity calls
            -- existed -- now genuinely undercounting without this + the
            -- other two tables' equivalents).
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            -- EVAL-1 addendum: the market-snapshot input to this evaluation,
            -- stamped on every path (mock/live/rejection) by
            -- OpenAIClient.evaluate(). Previously the primary evaluator was
            -- the one AI call in this codebase whose input could never be
            -- replayed after the fact (unlike the labeller's packet_json,
            -- which EVAL-1 already replays) -- this starts that record
            -- accruing going forward. NULL on every row before this column
            -- existed; never backfilled (the data didn't exist).
            snapshot_json TEXT,
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
            proposal_id TEXT,
            disagreement_with_openai TEXT,
            user_requested INTEGER,
            final_user_action_after_review TEXT,
            lineage_id TEXT,
            model_provider TEXT,
            prompt_hash TEXT,
            system_prompt_hash TEXT,
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
            target_profile TEXT,
            target_reward_risk REAL,
            min_reward_risk REAL,
            stop_loss_pct REAL,
            target_price_source TEXT,
            stop_price_source TEXT,
            trade_id TEXT,
            risk_check_id TEXT,
            claude_review_id TEXT,
            scan_batch_id TEXT,
            playbook_name TEXT,
            setup_classification TEXT,
            card_id TEXT,
            card_version INTEGER,
            invalidation_reason TEXT,
            expected_hold_days INTEGER,
            proposal_reason TEXT,
            arming_classification TEXT,
            narrative_warning TEXT,
            earnings_date TEXT,
            days_until_earnings INTEGER,
            earnings_within_hold_window INTEGER,
            earnings_within_warning_window INTEGER,
            earnings_timing TEXT,
            earnings_data_status TEXT,
            proposal_ttl_seconds INTEGER,
            proposal_expires_at_utc TEXT,
            expired_reason TEXT,
            expired_at_utc TEXT,
            superseded_by_proposal_id TEXT,
            superseded_at_utc TEXT,
            lineage_id TEXT,
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
            target_profile TEXT,
            trade_id TEXT,
            risk_check_id TEXT,
            order_class TEXT,
            intended_entry_price REAL,
            submitted_price REAL,
            broker_response_summary TEXT,
            error_message TEXT,
            updated_at_utc TEXT,
            updated_at_sgt TEXT,
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
            trade_id TEXT,
            position_id TEXT,
            slippage_amount REAL,
            slippage_bps REAL,
            estimated_costs REAL,
            raw_broker_fill_reference TEXT,
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
            target_profile TEXT,
            target_reward_risk REAL,
            min_reward_risk REAL,
            stop_loss_pct REAL,
            target_price_source TEXT,
            stop_price_source TEXT,
            trade_id TEXT,
            candidate_id TEXT,
            proposal_id TEXT,
            eval_id TEXT,
            protection_status TEXT DEFAULT 'unknown',
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
            target_profile TEXT,
            trade_id TEXT,
            candidate_id TEXT,
            proposal_id TEXT,
            hold_duration_minutes REAL,
            same_day_exit_classification TEXT,
            gross_pnl REAL,
            estimated_costs REAL,
            net_pnl REAL,
            realized_r REAL,
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
            -- HOLD-1: additive trading-day hold length (trading_days_between()
            -- convention -- see PositionManager._check_exit / close_position).
            -- holding_days above is UNCHANGED (calendar days, continuity for
            -- every pre-HOLD-1 row); this column is simply NULL on rows
            -- journaled before HOLD-1 shipped -- never backfilled.
            holding_trading_days INTEGER,
            is_same_day INTEGER,
            classification TEXT,
            mfe REAL,
            mae REAL,
            mfe_mae_source TEXT,
            model_confidence REAL,
            catalyst_type TEXT,
            win INTEGER,
            target_profile TEXT,
            target_reward_risk REAL,
            min_reward_risk REAL,
            stop_loss_pct REAL,
            target_price_source TEXT,
            stop_price_source TEXT,
            trade_id TEXT,
            candidate_id TEXT,
            proposal_id TEXT,
            exit_id TEXT,
            playbook_name TEXT,
            setup_classification TEXT,
            outcome_classification TEXT,
            hold_duration_minutes REAL,
            lessons_learned TEXT,
            lineage_id TEXT,
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
            scan_batch_id TEXT,
            earnings_date TEXT,
            days_until_earnings INTEGER,
            earnings_within_hold_window INTEGER,
            earnings_within_warning_window INTEGER,
            earnings_timing TEXT,
            earnings_data_status TEXT,
            lineage_id TEXT,
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
            target_profile TEXT,
            trade_id TEXT,
            hypothetical_entry REAL,
            hypothetical_stop REAL,
            hypothetical_target REAL,
            hypothetical_exit REAL,
            gross_pnl REAL,
            estimated_costs REAL,
            net_pnl REAL,
            realized_r REAL,
            outcome_notes TEXT,
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
    (
        "scan_batches",
        """
        CREATE TABLE IF NOT EXISTS scan_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_batch_id TEXT NOT NULL UNIQUE,
            scheduler_run_id TEXT,
            scan_type TEXT,
            source TEXT,
            started_at_utc TEXT,
            started_at_sgt TEXT,
            completed_at_utc TEXT,
            completed_at_sgt TEXT,
            status TEXT,
            market_session TEXT,
            universe_count INTEGER,
            candidates_found INTEGER,
            proposals_created INTEGER,
            watch_count INTEGER,
            rejected_count INTEGER,
            blocked_count INTEGER,
            errors_count INTEGER,
            notes TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        "scheduler_runs",
        """
        CREATE TABLE IF NOT EXISTS scheduler_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scheduler_run_id TEXT NOT NULL UNIQUE,
            run_type TEXT,
            trigger_source TEXT,
            started_at_utc TEXT,
            started_at_sgt TEXT,
            completed_at_utc TEXT,
            completed_at_sgt TEXT,
            status TEXT,
            scan_batch_id TEXT,
            candidates_found INTEGER,
            proposals_created INTEGER,
            orders_touched INTEGER,
            positions_touched INTEGER,
            reports_created INTEGER,
            notifications_sent INTEGER,
            system_events_created INTEGER,
            error_count INTEGER,
            error_summary TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        "risk_checks",
        """
        CREATE TABLE IF NOT EXISTS risk_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            risk_check_id TEXT NOT NULL UNIQUE,
            proposal_id TEXT,
            candidate_id TEXT,
            trade_id TEXT,
            result TEXT,
            fail_reason TEXT,
            max_risk_amount REAL,
            max_risk_pct REAL,
            position_size REAL,
            entry_price REAL,
            stop_price REAL,
            target_price REAL,
            reward_risk REAL,
            min_reward_risk REAL,
            stop_loss_pct REAL,
            target_reward_risk REAL,
            target_profile TEXT,
            liquidity_check_result TEXT,
            spread_check_result TEXT,
            daily_loss_check_result TEXT,
            max_trades_check_result TEXT,
            max_open_positions_check_result TEXT,
            short_margin_assumption TEXT,
            margin_or_leverage_required INTEGER,
            user_approval_required_for_margin_or_leverage INTEGER,
            block_reasons_json TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        "monitoring_snapshots",
        """
        CREATE TABLE IF NOT EXISTS monitoring_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            monitoring_snapshot_id TEXT NOT NULL UNIQUE,
            position_id TEXT,
            trade_id TEXT,
            symbol TEXT,
            direction TEXT,
            snapshot_at_utc TEXT,
            snapshot_at_sgt TEXT,
            market_session TEXT,
            current_price REAL,
            unrealized_pnl REAL,
            unrealized_r REAL,
            mfe REAL,
            mae REAL,
            stop_price REAL,
            target_price REAL,
            target_profile TEXT,
            invalidation_status TEXT,
            stop_hit INTEGER,
            target_hit INTEGER,
            time_stop_status TEXT,
            data_freshness_status TEXT,
            action_taken TEXT,
            notes TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # Broker protection watchdog (docs/roadmap/protection-watchdog.md): one row
        # per open, broker-managed (alpaca_paper) position per monitor pass. Verifies
        # the position still exists at the broker and that its stop/target legs are
        # still live -- neither OrderManager.reconcile() (bracket-leg-fill only) nor
        # the local PositionManager watchdog (never touches broker-managed positions)
        # check this. Serves as BOTH the append-only audit log (every pass writes a
        # row) AND the "what's currently blocking new entries" query (rows with
        # protection_status IN ('unprotected','closed_mismatch') AND resolved_at_utc
        # IS NULL are open incidents) -- one table is sufficient for both jobs.
        "protection_checks",
        """
        CREATE TABLE IF NOT EXISTS protection_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            check_id TEXT NOT NULL UNIQUE,
            position_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            trade_id TEXT,
            protection_status TEXT NOT NULL,
            broker_position_exists INTEGER,
            local_qty REAL,
            broker_qty REAL,
            qty_match INTEGER,
            stop_live INTEGER,
            target_live INTEGER,
            time_in_force TEXT,
            tif_appropriate INTEGER,
            dangling_orders_json TEXT,
            severity TEXT NOT NULL,
            detail TEXT,
            scheduler_run_id TEXT,
            resolved_at_utc TEXT,
            resolved_by TEXT,
            resolution_note TEXT,
            resolution_exit_id TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # Scheduler v1.5 (PR3): one row per scheduler-invoked job execution
        # (scan/monitor/outcomes_update/daily_digest) -- the outer cadence-layer
        # record written by the scheduler itself when it starts/finishes a job.
        # Distinct from the existing scheduler_runs table above: scheduler_runs is
        # an internal per-pass audit row written by run_scan_once/run_monitor_once
        # themselves (candidates found, orders touched, etc.), whereas job_runs
        # tracks the scheduler's own invocation lifecycle (which job, when it was
        # triggered, whether it completed/failed/was skipped, and top-level safety
        # signals like kill-switch/protection-blocking/cost-cap state at run time).
        "job_runs",
        """
        CREATE TABLE IF NOT EXISTS job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_run_id TEXT NOT NULL UNIQUE,
            job_type TEXT NOT NULL,
            trigger_source TEXT,
            lock_key TEXT,
            started_at_utc TEXT NOT NULL,
            started_at_sgt TEXT NOT NULL,
            finished_at_utc TEXT,
            finished_at_sgt TEXT,
            duration_ms INTEGER,
            status TEXT NOT NULL,
            error TEXT,
            kill_switch_engaged INTEGER,
            protection_blocking INTEGER,
            cost_cap_exceeded INTEGER,
            scheduler_run_id TEXT,
            result_summary_json TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # Cost-model calibration (Roadmap 1.5): one row per approved/submitted
        # order capturing the EXPECTED/approval-time market context + modeled
        # cost assumptions. Actuals (fill price, delay, status sequence) are
        # DERIVED at report time by joining paper_orders/paper_fills/order_events,
        # so this table never needs an update after the initial capture.
        "execution_calibration",
        """
        CREATE TABLE IF NOT EXISTS execution_calibration (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            calibration_id TEXT NOT NULL UNIQUE,
            proposal_id TEXT,
            candidate_id TEXT,
            trade_id TEXT,
            order_id TEXT,
            symbol TEXT,
            side TEXT,
            execution_provider TEXT,
            broker_managed INTEGER,
            expected_entry REAL,
            approval_bid REAL,
            approval_ask REAL,
            approval_mid REAL,
            approval_spread REAL,
            approval_spread_pct REAL,
            submitted_limit_price REAL,
            modeled_slippage_bps REAL,
            modeled_cost_estimate REAL,
            notes TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # Roadmap 2.3: compact evidence packet per shortlisted candidate (the
        # only data sent to the AI labeller). Placeholder context fields are
        # explicit 'unavailable' markers — no news/last30days integration in v1.
        "candidate_packets",
        """
        CREATE TABLE IF NOT EXISTS candidate_packets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            packet_id TEXT NOT NULL UNIQUE,
            candidate_id TEXT NOT NULL,
            scan_batch_id TEXT,
            symbol TEXT NOT NULL,
            interest_score REAL,
            interest_rank INTEGER,
            shortlist_reason TEXT,
            packet_json TEXT,
            missing_data_flags_json TEXT,
            catalyst_status TEXT,
            official_news_context TEXT,
            last30days_context TEXT,
            sentiment_context TEXT,
            regime TEXT,
            regime_rules_version TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # REG-1: one row per trading day per rules_version, append-only --
        # recompute under a new rules_version (e.g. a future regime_rules_v2)
        # adds new rows, NEVER mutates/relabels an existing v1 row in place
        # (anti-data-mining law: a threshold change must never retroactively
        # rewrite history). market_date is the day this classification
        # APPLIES to; spy_close/sma_*/vol fields are computed from whatever
        # benchmark_bars history was available at classification time (under
        # normal cadence, that's the prior session's official close -- EOD
        # data only, never an intraday peek).
        "regime_days",
        """
        CREATE TABLE IF NOT EXISTS regime_days (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            regime_day_id TEXT NOT NULL UNIQUE,
            market_date TEXT NOT NULL,
            regime TEXT NOT NULL,
            regime_rules_version TEXT NOT NULL,
            spy_close REAL,
            sma_50 REAL,
            sma_200 REAL,
            realized_vol_20d REAL,
            vol_percentile_1y REAL,
            dev_from_sma50_pct REAL,
            chop_streak_days INTEGER,
            computed_at_utc TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # Roadmap 2.3: AI category/playbook label per shortlisted candidate.
        # primary_label MUST be in OFFICIAL_LABELS; suggested_new_tags are stored
        # UNOFFICIAL. label_decision is ADVISORY (downgrade-only at decision time).
        # History is append-only; post_trade_review_label is reserved (NULL in v1)
        # so a later review never rewrites the decision-time label.
        "candidate_labels",
        """
        CREATE TABLE IF NOT EXISTS candidate_labels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label_id TEXT NOT NULL UNIQUE,
            candidate_id TEXT NOT NULL,
            packet_id TEXT,
            scan_batch_id TEXT,
            symbol TEXT NOT NULL,
            primary_label TEXT,
            secondary_labels_json TEXT,
            candidate_tags_json TEXT,
            risk_tags_json TEXT,
            direction TEXT,
            label_decision TEXT,
            label_confidence REAL,
            reason_for_label TEXT,
            thesis_stub TEXT,
            invalidation TEXT,
            main_risk TEXT,
            missing_context_json TEXT,
            suggested_new_tags_json TEXT,
            label_version TEXT,
            label_source TEXT,
            validation_status TEXT,
            model TEXT,
            is_mock INTEGER DEFAULT 0,
            raw_json TEXT,
            label_frozen_at_utc TEXT,
            post_trade_review_label TEXT,
            missing_conditions_json TEXT,
            upgrade_blockers_json TEXT,
            proposal_readiness TEXT,
            what_would_upgrade TEXT,
            -- PR9.5: real token usage for cost accounting (previously
            -- invisible to cost_guard, which only counted openai_evaluations).
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            -- TASK-R: retro-relabel. NULL on every normal (scan-time) row;
            -- set only on a NEW row produced by `alphaos relabel`, pointing
            -- back to the label_id of the row it is a clean replay of. The
            -- original row is NEVER modified (append-only law) -- this is
            -- purely a forward pointer from the new row to the old one.
            relabel_of TEXT,
            -- EXP-1 mechanism 6: the cost-accounting design, not a storage
            -- choice. Stamped 1 at insert time for every shadow-tier label
            -- (see Orchestrator._label_candidate's caller in
            -- alphaos/scheduler/shadow_label.py); 0 for every core-tier
            -- label. Rows still land in cost_guard.calls_in_last_30_days()
            -- UNMODIFIED (is_mock=0 -> counted) -- this column exists so a
            -- live-aggregate report can grep-exclude shadow_tier=1 rather
            -- than needing a fragile join back to candidates.
            shadow_tier INTEGER DEFAULT 0,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # Roadmap 2.4: official news/catalyst enrichment per shortlisted candidate.
        # Context only — never execution authority. EVERY enriched candidate is
        # journaled (confirmed / possible / none_found / stale / conflicting /
        # unavailable / error) so AlphaOS can later learn if catalyst-backed trades
        # perform better. catalyst_suggested_label is ADVISORY (never overwrites
        # the frozen primary_label).
        "candidate_catalysts",
        """
        CREATE TABLE IF NOT EXISTS candidate_catalysts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            catalyst_id TEXT NOT NULL UNIQUE,
            candidate_id TEXT NOT NULL,
            packet_id TEXT,
            scan_batch_id TEXT,
            symbol TEXT NOT NULL,
            catalyst_status TEXT,
            catalyst_type TEXT,
            catalyst_summary TEXT,
            catalyst_confidence REAL,
            catalyst_sources_json TEXT,
            catalyst_timestamp_utc TEXT,
            catalyst_age_minutes REAL,
            source_count INTEGER,
            official_news_context TEXT,
            analyst_context TEXT,
            earnings_context TEXT,
            filing_context TEXT,
            sector_context TEXT,
            macro_context TEXT,
            catalyst_risk_tags_json TEXT,
            catalyst_missing_context_json TEXT,
            catalyst_suggested_label TEXT,
            label_review_required INTEGER,
            enrichment_source TEXT,
            enrichment_status TEXT,
            enrichment_error TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # Roadmap 2.5: last30days research / narrative-context enrichment per
        # shortlisted candidate. SEPARATE social/research layer from official news
        # (2.4). Context only — never execution authority. EVERY eligible candidate
        # is journaled, INCLUDING those skipped by the per-scan budget cap
        # (last30days_status='skipped_budget_cap', enrichment_status='skipped'), so
        # AlphaOS can later distinguish "checked, no narrative" (none_found) from
        # "provider unavailable" (unavailable) from "intentionally skipped, outside
        # the top-N budget cap" (skipped_budget_cap).
        "candidate_last30days",
        """
        CREATE TABLE IF NOT EXISTS candidate_last30days (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            last30days_id TEXT NOT NULL UNIQUE,
            candidate_id TEXT NOT NULL,
            packet_id TEXT,
            scan_batch_id TEXT,
            symbol TEXT NOT NULL,
            last30days_status TEXT,
            summary TEXT,
            top_themes_json TEXT,
            source_coverage_json TEXT,
            item_count INTEGER,
            cluster_count INTEGER,
            top_score REAL,
            sentiment_label TEXT,
            sentiment_score REAL,
            newest_age_hours REAL,
            risk_tags_json TEXT,
            last30days_context TEXT,
            sentiment_context TEXT,
            label_review_required INTEGER,
            query TEXT,
            reason TEXT,
            interest_rank INTEGER,
            interest_score REAL,
            provider TEXT,
            enrichment_status TEXT,
            enrichment_error TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # Roadmap 2.6: append-only audit of how the AI label adjusted the eval's
        # trade decision. ONE row per labelled candidate, recording eval vs label
        # vs final decision, the direction (upgraded/downgraded/unchanged), whether
        # the symmetric override was armed (real signals), and the catalyst /
        # sentiment driver behind the move — so AlphaOS can later learn whether
        # narrative-driven adjustments actually improved outcomes. This NEVER
        # executes; it records a decision that still passes through gates + approval.
        "decision_adjustments",
        """
        CREATE TABLE IF NOT EXISTS decision_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            adjustment_id TEXT NOT NULL UNIQUE,
            candidate_id TEXT NOT NULL,
            packet_id TEXT,
            scan_batch_id TEXT,
            symbol TEXT NOT NULL,
            eval_decision TEXT,
            label_decision TEXT,
            final_decision TEXT,
            adjustment TEXT,
            override_armed INTEGER,
            override_enabled INTEGER,
            driver TEXT,
            driver_source TEXT,
            driver_detail_json TEXT,
            evidence_json TEXT,
            catalyst_status TEXT,
            catalyst_type TEXT,
            catalyst_summary TEXT,
            catalyst_source TEXT,
            catalyst_confidence REAL,
            catalyst_timestamp_utc TEXT,
            catalyst_age_minutes REAL,
            last30days_status TEXT,
            last30days_provider TEXT,
            sentiment_label TEXT,
            sentiment_score REAL,
            last30days_summary TEXT,
            top_themes_json TEXT,
            source_coverage_json TEXT,
            label_confidence REAL,
            arming_classification TEXT,
            armed_watch INTEGER,
            armed_watch_reason TEXT,
            proposal_readiness TEXT,
            labeller_reason TEXT,
            labeller_missing_conditions_json TEXT,
            labeller_upgrade_blockers_json TEXT,
            earnings_date TEXT,
            days_until_earnings INTEGER,
            earnings_within_hold_window INTEGER,
            earnings_within_warning_window INTEGER,
            earnings_timing TEXT,
            earnings_data_status TEXT,
            lineage_id TEXT,
            ai_lineage_json TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # Roadmap 2.7: LLM-derived last30days narrative polarity per candidate.
        # SEPARATE evidence — never overwrites last30days / eval / label / risk /
        # approval records. Records the classification (sentiment / driver type /
        # hype risk / coverage), the DETERMINISTIC AlphaOS arming decision
        # (should_arm_override + arming_classification: normal_driver /
        # high_risk_narrative / non_arming), and parse_status for fail-safe audit.
        # It can ARM an override upgrade but never trades or bypasses a gate/approval.
        "last30days_polarity",
        """
        CREATE TABLE IF NOT EXISTS last30days_polarity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            polarity_id TEXT NOT NULL UNIQUE,
            candidate_id TEXT NOT NULL,
            packet_id TEXT,
            scan_batch_id TEXT,
            symbol TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            prompt_template_version TEXT,
            sentiment_label TEXT,
            sentiment_score REAL,
            confidence REAL,
            direction_alignment TEXT,
            source_coverage_quality TEXT,
            narrative_driver_type TEXT,
            hype_or_manipulation_risk TEXT,
            requires_user_attention INTEGER,
            official_catalyst_conflict INTEGER,
            should_arm_override INTEGER,
            arming_classification TEXT,
            warning_message TEXT,
            reasoning_summary TEXT,
            evidence_json TEXT,
            raw_response_json TEXT,
            parse_status TEXT,
            lineage_id TEXT,
            model_provider TEXT,
            prompt_hash TEXT,
            system_prompt_hash TEXT,
            -- PR9.5: real token usage for cost accounting. No is_mock column
            -- exists on this table (a PR4-era omission, not reproduced) --
            -- model_provider IS NOT NULL is cost_guard's real-call filter
            -- here instead (see cost_guard.py's own docstring).
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # Roadmap 2.8 (Part C): USER DECISION OVERRIDES — a SEPARATE decision layer.
        # A user override NEVER rewrites AlphaOS's original recommendation; both the
        # AlphaOS recommendation and the user's final decision are stored side by
        # side for audit + attribution. Overrides are safety-gated and never bypass
        # manual approval, the gates, or the real-money guard.
        "user_decision_overrides",
        """
        CREATE TABLE IF NOT EXISTS user_decision_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            override_id TEXT NOT NULL UNIQUE,
            candidate_id TEXT,
            proposal_id TEXT,
            symbol TEXT NOT NULL,
            alphaos_eval_decision TEXT,
            alphaos_label_decision TEXT,
            alphaos_final_decision TEXT,
            alphaos_direction TEXT,
            alphaos_confidence REAL,
            alphaos_reasoning_summary TEXT,
            armed_watch INTEGER,
            arming_classification TEXT,
            user_override_action TEXT,
            user_final_decision TEXT,
            user_direction TEXT,
            user_size_override REAL,
            user_reason_code TEXT,
            user_reason_text TEXT,
            override_aggressiveness TEXT,
            execution_allowed INTEGER,
            blocked_reason TEXT,
            execution_result TEXT,
            linked_order_id TEXT,
            linked_trade_id TEXT,
            outcome_r REAL,
            outcome_pnl REAL,
            outcome_status TEXT,
            alphaos_would_have_traded INTEGER,
            user_did_trade INTEGER,
            attribution_result TEXT,
            nightdesk_research_candidate INTEGER,
            nightdesk_research_reason TEXT,
            resolved_at_utc TEXT,
            resolved_at_sgt TEXT,
            lineage_id TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # Measurement foundation (post-2.8, Fable 5 review PR1+PR2): the
        # COUNTERFACTUAL LEDGER. Every scanned candidate/proposal/reject/
        # armed-watch/user-override becomes learnable data via forward
        # 1/3/5-day outcomes, whether or not it ever became a real trade. This
        # is NOT a de-novo historical backtest — it only replays decisions
        # AlphaOS actually made/recorded, using bars observed AFTER the
        # decision. PURE MEASUREMENT: never read by any gate, eval, labeller,
        # risk check, or execution path; write-only from this subsystem.
        # decision_at_utc (the source row's ORIGINAL decision timestamp) is the
        # forward-window anchor — distinct from created_at_utc (when this
        # outcome row itself was seeded, which can lag the decision when
        # catching up on a backlog). Anchoring on seed time instead of decision
        # time would mislabel a multi-week-old candidate's next bar as a
        # "1-day" return (Opus audit HIGH-1).
        "candidate_outcomes",
        """
        CREATE TABLE IF NOT EXISTS candidate_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outcome_id TEXT NOT NULL UNIQUE,
            scan_id TEXT,
            scan_batch_id TEXT,
            candidate_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            candidate_type TEXT NOT NULL,
            decision_at_utc TEXT,
            original_decision TEXT,
            eval_decision TEXT,
            label_decision TEXT,
            final_decision TEXT,
            armed_watch INTEGER DEFAULT 0,
            user_override INTEGER DEFAULT 0,
            playbook_id TEXT,
            playbook_version TEXT,
            scanner_version TEXT,
            entry_reference_price REAL,
            stop_price REAL,
            target_price REAL,
            direction_hint TEXT,
            forward_1d_return_pct REAL,
            forward_3d_return_pct REAL,
            forward_5d_return_pct REAL,
            forward_1d_r REAL,
            forward_3d_r REAL,
            forward_5d_r REAL,
            max_favorable_1d_r REAL,
            max_adverse_1d_r REAL,
            max_favorable_3d_r REAL,
            max_adverse_3d_r REAL,
            max_favorable_5d_r REAL,
            max_adverse_5d_r REAL,
            replay_result TEXT,
            replay_r REAL,
            replay_exit_reason TEXT,
            outcome_status TEXT NOT NULL DEFAULT 'pending',
            data_quality_status TEXT,
            updated_at_utc TEXT,
            updated_at_sgt TEXT,
            lineage_id TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # PR4: decision lineage stamping (measurement/audit-only, like
        # protection_checks/candidate_outcomes/job_runs before it -- never read
        # by any gate/eval/labeller/risk/execution path). One row per DISTINCT
        # environment/config snapshot (repo commit+branch+dirty flag, app/schema
        # version, categorized config hashes, scanner/strategy/universe version
        # constants, market data provider), keyed by a deterministic content
        # hash (lineage_id) so many decisions made under the same snapshot share
        # one row instead of duplicating ~15 columns per decision -- the same
        # "shared batch/run row referenced by lineage_id/scan_batch_id/
        # scheduler_run_id" pattern already used by scan_batches/scheduler_runs/
        # job_runs. Decision rows (candidates, trade_proposals,
        # rejected_candidates, decision_adjustments, user_decision_overrides,
        # candidate_outcomes, trade_outcomes, openai_evaluations,
        # claude_reviews, last30days_polarity) carry their own lineage_id
        # column pointing back to the row here that was in effect when they
        # were created. Row-level lineage that genuinely varies per decision
        # (model/prompt hash actually used, human override reason, freshness
        # evidence) is stamped directly on the decision row instead, not here.
        "lineage_snapshots",
        """
        CREATE TABLE IF NOT EXISTS lineage_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lineage_id TEXT NOT NULL UNIQUE,
            git_commit_sha TEXT,
            git_branch TEXT,
            git_dirty INTEGER,
            app_version TEXT,
            schema_version INTEGER,
            config_hash TEXT,
            scanner_config_hash TEXT,
            risk_config_hash TEXT,
            protection_config_hash TEXT,
            scheduler_config_hash TEXT,
            earnings_config_hash TEXT,
            proposal_ttl_config_hash TEXT,
            tqs_config_hash TEXT,
            attribution_config_hash TEXT,
            shadow_tier_config_hash TEXT,
            regime_config_hash TEXT,
            text_archive_config_hash TEXT,
            baseline_config_hash TEXT,
            canary_config_hash TEXT,
            scanner_version TEXT,
            scanner_rule_version TEXT,
            universe_version_hash TEXT,
            playbook_version TEXT,
            strategy_version TEXT,
            feature_engine_version TEXT,
            market_data_provider TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # PR5: earnings-proximity enrichment per shortlisted candidate, mirroring
        # candidate_catalysts/candidate_last30days -- one row per candidate per
        # scan (INCLUDING those skipped by the per-scan budget cap, so a gap in
        # coverage is visible, not silently absent). Advisory/context ONLY: never
        # hard-blocks a trade by default, never bypasses a gate or manual
        # approval, never fed into the AI eval/labeller prompt (unlike
        # last30days). Summary fields are also denormalized onto candidates/
        # trade_proposals/rejected_candidates/decision_adjustments (same pattern
        # as catalyst_status/last30days_status) so callers don't need to join
        # here just to see the flag.
        "candidate_earnings",
        """
        CREATE TABLE IF NOT EXISTS candidate_earnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            earnings_id TEXT NOT NULL UNIQUE,
            candidate_id TEXT NOT NULL,
            packet_id TEXT,
            scan_batch_id TEXT,
            symbol TEXT NOT NULL,
            earnings_date TEXT,
            earnings_timing TEXT,
            days_until_earnings INTEGER,
            hold_days_used INTEGER,
            earnings_within_hold_window INTEGER,
            earnings_within_warning_window INTEGER,
            earnings_data_status TEXT,
            confidence REAL,
            source TEXT,
            provider TEXT,
            enrichment_status TEXT,
            enrichment_error TEXT,
            risk_tags_json TEXT,
            fetched_at_utc TEXT,
            lineage_id TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # PR7: TQS v0 -- shadow-only Trade Quality Score. Measurement-only: an
        # attention-worthiness ranking signal to compare against
        # candidate_outcomes/trade_outcomes, NOT a probability, NOT expected
        # return, NOT a sizing/approval/gating signal. No decision path may
        # read this table -- see alphaos/tqs/README (module docstring) for the
        # enforced boundary. Separate table (not columns on the decision
        # tables) so "nothing reads this" stays a one-table audit surface, and
        # so a v0 formula that gets reshaped by calibration doesn't require
        # touching four other tables. One row per (source_type, candidate_id[,
        # proposal_id], tqs_version) -- see the partial unique indexes below
        # for why a plain UNIQUE column list is not NULL-safe in SQLite.
        "tqs_scores",
        """
        CREATE TABLE IF NOT EXISTS tqs_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tqs_id TEXT NOT NULL UNIQUE,
            source_type TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            proposal_id TEXT,
            scan_batch_id TEXT,
            symbol TEXT NOT NULL,
            direction TEXT,
            tqs_version TEXT NOT NULL,
            raw_score INTEGER,
            data_confidence REAL NOT NULL,
            tqs_score INTEGER,
            tqs_bucket TEXT NOT NULL,
            components_json TEXT,
            missing_components_json TEXT,
            data_quality_status TEXT NOT NULL,
            is_mock INTEGER DEFAULT 0,
            lineage_id TEXT,
            computed_at_utc TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # PR8: Attribution v2 -- counterfactual DELTA_R ledger. Measurement-only:
        # pairs a decision-DIVERGENCE event (user override, gate block, TTL
        # expiry, or execution vs the frozen AlphaOS plan) with the R already
        # resolved by the outcome ledger (candidate_outcomes/trade_outcomes) --
        # this table NEVER recomputes a replay itself; one replay engine, one
        # truth. No decision path may read this table -- see
        # alphaos/attribution/ module docstring for the enforced boundary.
        # A row exists ONLY where two paths diverged; pure one-path no-action
        # decisions (reject-no-action/watch-no-action/armed-watch-no-action)
        # get no row here and are analyzed via report-time joins on
        # candidate_outcomes instead. Separate table (not columns on the
        # decision tables) for the same one-table-audit-surface reason as
        # tqs_scores. One row per (attribution_type, proposal_id OR
        # override_id, attribution_version) -- see the partial unique indexes
        # below for why a plain UNIQUE column list is not NULL-safe in SQLite.
        "attribution_records",
        """
        CREATE TABLE IF NOT EXISTS attribution_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attribution_id TEXT NOT NULL UNIQUE,
            attribution_type TEXT NOT NULL,
            attribution_version TEXT NOT NULL,
            agent TEXT NOT NULL,
            source_id TEXT NOT NULL,
            candidate_id TEXT,
            proposal_id TEXT,
            override_id TEXT,
            position_id TEXT,
            trade_outcome_id TEXT,
            candidate_outcome_id TEXT,
            symbol TEXT NOT NULL,
            direction TEXT,
            decision_at_utc TEXT,
            alphaos_path_r REAL,
            actual_path_r REAL,
            delta_r REAL,
            execution_delta_r REAL,
            r_basis TEXT,
            replay_status TEXT,
            blocked_reason_code TEXT,
            expired_reason TEXT,
            resolved_status TEXT NOT NULL DEFAULT 'pending',
            resolved_at_utc TEXT,
            data_quality_status TEXT NOT NULL,
            is_mock INTEGER DEFAULT 0,
            lineage_id TEXT,
            missing_data_json TEXT,
            notes_json TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # PR9.5: the benchmark spine. One row per US trading day -- the
        # "paper equity" side of measuring performance against the S&P 500.
        # `equity_source` distinguishes a real broker-reported reading
        # ('live_broker') from the static PAPER_EQUITY config fallback
        # ('static_config', used in mock mode or if the broker read fails) --
        # never silently conflated, per the unknown-never-zero/mock-never-real
        # discipline every other measurement layer in this codebase follows.
        # Write-only from alphaos/reports/benchmark_capture.py; never read by
        # any gate/eval/labeller/risk/execution path.
        "equity_snapshots",
        """
        CREATE TABLE IF NOT EXISTS equity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id TEXT NOT NULL UNIQUE,
            market_date TEXT NOT NULL,
            equity REAL NOT NULL,
            equity_source TEXT NOT NULL,
            is_mock INTEGER DEFAULT 0,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # PR9.5: cached daily OHLCV bars for whatever symbol(s) we benchmark
        # against (SPY today; the symbol column is generic for future
        # benchmarks -- QQQ, a custom blended index, etc). A cache, not a
        # measurement of AlphaOS itself: safe to backfill historical dates
        # (unlike equity_snapshots, SPY's own price history is public record,
        # not something that would need to be "honestly" forward-only).
        "benchmark_bars",
        """
        CREATE TABLE IF NOT EXISTS benchmark_bars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bar_id TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            bar_date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # PR10: the versioned setup-card registry. Cards themselves are
        # declarative YAML in alphaos/cards/*.yaml (reviewable, diffable,
        # git-versioned) -- this table is a DB-synced mirror (idempotent
        # upsert at orchestrator startup, keyed by (card_id, version)) so
        # every ledger row can join on card_id without filesystem access.
        # Append-only per version: alphaos/cards/registry.py refuses to
        # start if a (card_id, version) already registered has a different
        # content_hash than what's on disk -- a silently mutated card is the
        # exact failure mode Prime Directive 7 exists to prevent.
        "setup_cards",
        """
        CREATE TABLE IF NOT EXISTS setup_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            name TEXT,
            state TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            content_json TEXT,
            lineage_id TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # TEXT-0: ticker -> SEC CIK reference lookup, refreshed weekly. One
        # CURRENT mapping per ticker (a reference/lookup table, not a
        # measurement ledger like regime_days/candidate_outcomes -- CIK
        # reassignment is rare but real, so refresh legitimately updates the
        # row rather than appending a new one; last_confirmed_at_utc tracks
        # freshness). "Once archived-for, always archived-for" (spec's own
        # law): a ticker is never REMOVED from this table just because it's
        # since been delisted/dropped from the live universe.
        "cik_map",
        """
        CREATE TABLE IF NOT EXISTS cik_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL UNIQUE,
            cik TEXT NOT NULL,
            company_name TEXT,
            first_seen_at_utc TEXT NOT NULL,
            last_confirmed_at_utc TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # TEXT-0: the point-in-time text archive's metadata ledger (raw
        # gzipped bodies live on disk at storage_path; this table is the
        # honest, queryable index over them). THE LAW OF THIS SUBSYSTEM:
        # published_at is the SOURCE's own timestamp; seen_at is the wall
        # clock when AlphaOS fetched it. ALL future backtests and shadow
        # tests may only ever condition on seen_at, never published_at --
        # conditioning on published_at would let a backtest "know" about a
        # filing before AlphaOS could possibly have seen it (the PEAD-audit
        # lesson, inverted). Append-only; re-fetch of an already-archived
        # accession is a no-op via the UNIQUE constraint below.
        "text_documents",
        """
        CREATE TABLE IF NOT EXISTS text_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL UNIQUE,
            cik TEXT NOT NULL,
            ticker_at_time TEXT,
            form_type TEXT NOT NULL,
            edgar_forms_version TEXT NOT NULL,
            accession_no TEXT NOT NULL UNIQUE,
            published_at TEXT,
            seen_at TEXT NOT NULL,
            source_url TEXT,
            sha256 TEXT NOT NULL,
            byte_size INTEGER,
            storage_path TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'edgar',
            fetch_run_id TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # EVAL-1: the offline eval harness. One row per `alphaos eval`
        # invocation (a "run" = one pass over the frozen golden corpus,
        # `repeats` times per packet). Zero decision surface -- never read by
        # any gate/eval/labeller/risk/execution path; this table exists so a
        # prompt/model change can be measured in days via replay, instead of
        # waiting months for enough real ledger data.
        "eval_runs",
        """
        CREATE TABLE IF NOT EXISTS eval_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            corpus_dir TEXT NOT NULL,
            corpus_version INTEGER,
            label_version TEXT,
            model TEXT,
            is_mock INTEGER DEFAULT 0,
            repeats INTEGER NOT NULL DEFAULT 1,
            n_packets INTEGER NOT NULL DEFAULT 0,
            lineage_id TEXT,
            started_at_utc TEXT NOT NULL,
            started_at_sgt TEXT NOT NULL,
            finished_at_utc TEXT,
            finished_at_sgt TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # EVAL-1: one row per (packet x repeat) replayed through the CURRENT
        # PlaybookClassifier -- the real production classify() path, never a
        # reimplementation. Stored on EVERY path including fail-safe (a
        # discarded fail-safe completion is precisely the example the
        # harness needs most -- see raw_json). ground_truth_label is
        # deliberately NOT duplicated here: it lives in the corpus fixture
        # file (operator-editable) and is joined fresh at report time by
        # packet_id, so a report always reflects the LATEST adjudication,
        # never a stale snapshot frozen at run time.
        "eval_results",
        """
        CREATE TABLE IF NOT EXISTS eval_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            result_id TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL,
            packet_id TEXT NOT NULL,
            symbol TEXT,
            repeat_index INTEGER NOT NULL DEFAULT 0,
            primary_label TEXT,
            label_decision TEXT,
            label_confidence REAL,
            validation_status TEXT,
            label_source TEXT,
            raw_json TEXT,
            model TEXT,
            is_mock INTEGER DEFAULT 0,
            model_provider TEXT,
            prompt_hash TEXT,
            system_prompt_hash TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # INSTR-1 part 2: ATR(14) capture, the daily write side of ATR-scaled
        # stops (alphaos/reports/atr_service.py). One row per (symbol,
        # market_date, rules_version) -- a v2 rules recompute would add new
        # rows under a new version rather than mutating v1 rows, same
        # pattern as regime_days. Read ONLY by OpenAIClient's live-only stop
        # override (alphaos/ai/openai_client.py) -- never by any
        # gate/risk/execution path directly. Scoped to the core-book
        # universe (the shadow tier never reaches the evaluator/proposal
        # path, so it has no use for this data -- see module docstring).
        "atr_history",
        """
        CREATE TABLE IF NOT EXISTS atr_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            atr_id TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            market_date TEXT NOT NULL,
            atr_14 REAL NOT NULL,
            rules_version TEXT NOT NULL,
            n_bars_fetched INTEGER,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # PORT-1: the pre-registration registry (ported from NightDesk's
        # Thesis Research Layer -- see
        # docs/roadmap/ported/nightdesk-stats-contract.md). One row per
        # pre-specified hypothesis; every variant being compared gets its
        # OWN row, never a shared/reused one (failures are never deleted --
        # removing a row would retroactively shrink N for every hypothesis
        # evaluated after it). `evaluated_at_utc` and the evidence columns
        # (effective_n through evidence_status) are written EXACTLY ONCE by
        # alphaos.stats.preregistration.evaluate_hypothesis() -- immutable
        # thereafter, the anti-optional-stopping guard. Deliberately NO
        # verdict/q_value column: the verdict is NEVER stored as
        # authoritative (a documented departure from this table's own
        # originally-compressed spec wording, resolved in the contract doc
        # Sec 4) -- every consumer calls alphaos.stats.fdr.compute_verdicts()
        # fresh over the full evaluated family instead.
        # operator_approved_for_forward_test/operator_decision_at_utc/
        # operator_notes are written ONLY by a human-facing path (none exists
        # yet) -- the lab recommends, it never enrolls itself.
        "preregistrations",
        """
        CREATE TABLE IF NOT EXISTS preregistrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prereg_id TEXT NOT NULL UNIQUE,
            hypothesis TEXT NOT NULL,
            metric TEXT NOT NULL,
            params_json TEXT,
            floor_effective_n INTEGER NOT NULL,
            floor_span_days REAL NOT NULL,
            analysis_not_before TEXT NOT NULL,
            strong_prior_pre_documented INTEGER NOT NULL DEFAULT 0,
            strong_prior_reasoning TEXT,
            registered_at_utc TEXT NOT NULL,
            registered_at_sgt TEXT NOT NULL,
            evaluated_at_utc TEXT,
            effective_n INTEGER,
            n_raw INTEGER,
            span_days REAL,
            point_estimate REAL,
            ci_low REAL,
            ci_high REAL,
            ci_level REAL,
            one_sided_p_below_zero REAL,
            evidence_status TEXT,
            operator_approved_for_forward_test INTEGER NOT NULL DEFAULT 0,
            operator_decision_at_utc TEXT,
            operator_notes TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # BASELINE: the deterministic shadow baseline -- one row per
        # (candidate, rule_version) for every candidate that reaches the
        # primary AI evaluator (2:1 with openai_evaluations rows, two rules).
        # Written by the orchestrator in the SAME tick, strictly AFTER the
        # live decision fully resolves (never influences it -- shadow law,
        # see alphaos.baseline.tracker). NOT the legacy `baseline_outcomes`
        # table (the old no-news hypothetical-P&L tracker,
        # journal/schema.py's own candidates-era table) -- distinct on
        # purpose, never conflate them.
        #
        # replay_status: 'complete' immediately for a 'no_action' decision
        # (replay_r=0.0 is a DIRECTLY OBSERVED fact -- no position was ever
        # opened, matching Attribution v2's own 0-is-a-fact convention, never
        # a substitute for missing data); 'unavailable' immediately for an
        # 'unavailable' decision (a bracket genuinely could not be
        # constructed -- unknown != zero, replay_r stays NULL forever);
        # 'pending' for a 'propose' decision, resolved later by
        # alphaos.baseline.tracker.resolve_pending_baseline_decisions() via
        # the ONE replay engine (alphaos.learning.outcomes_engine.
        # replay_bracket -- never a second replay implementation).
        #
        # entry_fill_status (spec item 4, added 2026-07-09): 'assumed_filled'
        # for every 'propose' row (a real last_price is required to reach
        # 'propose' at all -- see tracker.py), NULL for no_action/unavailable
        # (no entry attempt to characterize). No live signal produces
        # 'needs_review' yet -- the column exists for a future version that
        # might, without a further migration.
        "shadow_baseline_decisions",
        """
        CREATE TABLE IF NOT EXISTS shadow_baseline_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            baseline_decision_id TEXT NOT NULL UNIQUE,
            candidate_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            scan_batch_id TEXT,
            rule_version TEXT NOT NULL,
            decision TEXT NOT NULL,
            decision_reason TEXT,
            direction TEXT,
            entry REAL,
            stop REAL,
            target REAL,
            max_holding_days INTEGER,
            setup_card_id TEXT,
            entry_fill_status TEXT,
            input_sha TEXT NOT NULL,
            decision_at_utc TEXT NOT NULL,
            replay_status TEXT NOT NULL DEFAULT 'pending',
            replay_result TEXT,
            replay_r REAL,
            replay_exit_reason TEXT,
            lineage_id TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # EARN-1: the once-daily earnings-calendar capture cache (the
        # write side of the live AlphaVantageEarningsProvider). Append-only:
        # a (symbol, report_date, fiscal_date_ending) triple already seen is
        # never rewritten, only newly-seen or REVISED triples (a company's
        # report_date shifting for the same fiscal period) add a new row --
        # this is a point-in-time record, not a "current best guess" table,
        # matching TEXT-0's own seen_at law (created_at_utc, auto-stamped by
        # JournalStore.insert(), is the ONLY field any future backtest may
        # condition on; report_date is the EVENT date, not the discovery
        # date). Read ONLY by AlphaVantageEarningsProvider's live per-symbol
        # lookup (alphaos/earnings/earnings_provider.py) -- never by any
        # gate/risk/execution path directly.
        "earnings_calendar_cache",
        """
        CREATE TABLE IF NOT EXISTS earnings_calendar_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            company_name TEXT,
            report_date TEXT NOT NULL,
            fiscal_date_ending TEXT,
            estimate_eps REAL,
            currency TEXT,
            timing TEXT,
            source TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # PR13 slice 1: one rolling scoreboard snapshot per (card_id,
        # card_version, evaluation_date) -- a DAILY re-evaluation, never
        # one-shot-frozen like a PORT-1 preregistration (a card's own health
        # is an operational monitor, re-checked every day, not a scientific
        # hypothesis -- see alphaos/cards/scoreboard.py's own module
        # docstring for why this deliberately does NOT go through
        # register_hypothesis()/evaluate_hypothesis()). `breach` is TRUE
        # only when the card clears its own MIN_RESOLVED_FOR_V2_AGGREGATE/
        # MIN_SPAN_DAYS_FOR_V2_AGGREGATE floor (reused verbatim from
        # attribution.py -- one-floor law) AND the clustered-bootstrap CI is
        # reliably below zero (ci_high < 0) -- never a raw negative mean,
        # which could easily be noise. `state` is a SNAPSHOT of
        # setup_cards.state at evaluation time, purely for report
        # convenience -- never itself mutated by this table (Prime
        # Directive 7: only an operator-committed YAML version bump changes
        # a card's own state).
        "card_scoreboard_snapshots",
        """
        CREATE TABLE IF NOT EXISTS card_scoreboard_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id TEXT NOT NULL UNIQUE,
            card_id TEXT NOT NULL,
            card_version INTEGER NOT NULL,
            evaluation_date TEXT NOT NULL,
            state TEXT,
            expectancy_r REAL,
            ci_low REAL,
            ci_high REAL,
            effective_n INTEGER NOT NULL,
            n_raw INTEGER NOT NULL,
            span_days REAL,
            clears_floor INTEGER NOT NULL DEFAULT 0,
            breach INTEGER NOT NULL DEFAULT 0,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # CANARY: one row per `alphaos canary run` invocation (a weekly pass
        # over the frozen golden corpus, `data/canary/`). Detects SILENT
        # upstream model changes before they contaminate weeks of ledger data
        # -- distinct from EVAL-1 (which answers "is this prompt better?");
        # CANARY only answers "did the configured model change under us?".
        # is_baseline marks the ONE pinned reference run every later run
        # diffs against (re-pinnable via `canary_pin-baseline`, never more
        # than one row true at a time -- enforced in canary/run.py, not by a
        # DB constraint, since "demote the old baseline" is a two-statement
        # transaction, not expressible as a single CHECK/trigger here).
        "canary_runs",
        """
        CREATE TABLE IF NOT EXISTS canary_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            corpus_dir TEXT NOT NULL,
            corpus_version INTEGER,
            configured_model TEXT,
            is_mock INTEGER DEFAULT 0,
            n_prompts INTEGER NOT NULL DEFAULT 0,
            n_parse_or_failsafe INTEGER NOT NULL DEFAULT 0,
            response_models_json TEXT,
            system_fingerprints_json TEXT,
            mean_confidence REAL,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            latency_ms_total INTEGER,
            is_baseline INTEGER NOT NULL DEFAULT 0,
            drift_tier TEXT,
            drift_detail_json TEXT,
            lineage_id TEXT,
            started_at_utc TEXT NOT NULL,
            started_at_sgt TEXT NOT NULL,
            finished_at_utc TEXT,
            finished_at_sgt TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # CANARY: one row per packet replayed through the CURRENT
        # PlaybookClassifier -- the real production classify() path, same
        # "one replay engine" reuse as EVAL-1's eval_results (never a second
        # implementation of the live call). Stores the full raw completion
        # on every path including fail-safe, since a parse failure IS a
        # drift signal (Tier 1: "any parse/failsafe rate change from 0").
        "canary_results",
        """
        CREATE TABLE IF NOT EXISTS canary_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            result_id TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL,
            packet_id TEXT NOT NULL,
            symbol TEXT,
            primary_label TEXT,
            label_decision TEXT,
            label_confidence REAL,
            validation_status TEXT,
            is_failsafe INTEGER NOT NULL DEFAULT 0,
            raw_json TEXT,
            response_model TEXT,
            system_fingerprint TEXT,
            prompt_hash TEXT,
            system_prompt_hash TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # PR14: Red-Team Debate v0 -- one row per (proposal, agent_role) vote.
        # `agent_role='bear'` only in v0 (a future triad would add 'bull'/
        # 'neutral' rows to this SAME table -- it is role-parameterized on
        # purpose, not a bear-only schema). Invoked batch-at-scan-end, AFTER
        # the scan batch's own decisions are already committed (the PR7/TQS
        # call-site pattern -- see orchestrator.py's own run_scan_once,
        # "MUST run last"), so a debate vote can never influence the
        # proposal it is voting on: shadow law, true by construction, not
        # discipline. Pure measurement -- nothing downstream reads this
        # table yet; the one planned consumer is this feature's OWN
        # pre-registered hypothesis (registered via `alphaos debate_register`,
        # mirroring BASELINE's cmd_baseline_register()), evaluated once,
        # exactly like every other PORT-1 preregistration.
        #
        # is_mock mirrors OpenAIClient's own convention
        # (`settings.is_mock or not settings.has_anthropic_key`) -- NOT
        # ClaudeReviewer's (that class is a manual, on-demand, human-button
        # feature that simply raises if no key is configured, which is fine
        # for a button but wrong for an automated batch job that must run
        # safely offline in every test).
        "agent_votes",
        """
        CREATE TABLE IF NOT EXISTS agent_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vote_id TEXT NOT NULL UNIQUE,
            proposal_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            scan_batch_id TEXT,
            agent_role TEXT NOT NULL,
            stance TEXT NOT NULL,
            conviction REAL NOT NULL,
            failure_modes_json TEXT,
            invalidation_triggers_json TEXT,
            reasoning TEXT,
            is_mock INTEGER NOT NULL DEFAULT 0,
            model_provider TEXT,
            prompt_hash TEXT,
            system_prompt_hash TEXT,
            lineage_id TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # PR12: the hypothesis_proposals registry -- one row per seeded
        # hypothesis, mapping a fixed human-assigned hypothesis_id (e.g.
        # "H-TQS-1") onto a preregistrations row (PORT-1). `status` is a
        # MECHANICAL lifecycle marker only (proposed -> testing -> resolved);
        # it is never a semantic verdict -- the fresh verdict always comes
        # from alphaos.stats.fdr.compute_verdicts() over the full evaluated
        # preregistrations family (see alphaos/hypotheses/resolver.py's own
        # module docstring for why MET/FAILED/WITHDRAWN are operator-only and
        # never set by the resolver). last_verdict/last_q_value/last_reason
        # are a CACHE of that function's most recent output for this row --
        # read-optimization for the report only, never treated as
        # authoritative by any other code path (same non-authoritative-cache
        # posture preregistrations' own now-removed verdict column would have
        # had; see that table's comment above).
        #
        # H-AI-1 is special-cased throughout alphaos/hypotheses/: it has
        # metric_fn_name NULL and prereg_id links to BASELINE's own existing
        # preregistrations row (found by text match, cmd_baseline_register()'s
        # own hypothesis+metric strings) -- resolver.py never calls
        # evaluate_hypothesis() for this row, only mirrors whatever verdict
        # that row already carries once BASELINE's own (not yet built)
        # evaluation step sets it.
        "hypothesis_proposals",
        """
        CREATE TABLE IF NOT EXISTS hypothesis_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hypothesis_id TEXT NOT NULL UNIQUE,
            risk_class TEXT NOT NULL,
            claim TEXT NOT NULL,
            metric_description TEXT,
            success_floor REAL,
            metric_fn_name TEXT,
            card_id TEXT,
            prereg_id TEXT,
            status TEXT NOT NULL DEFAULT 'proposed',
            analysis_not_before TEXT NOT NULL,
            resolved_at_utc TEXT,
            last_verdict TEXT,
            last_q_value REAL,
            last_reason TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # PR13 slice 1: one demotion EVENT per (card_id, card_version),
        # EVER -- the anti-double-jeopardy law (spec audit B3): a demoted
        # card version is terminal, re-entry to live_eligible requires a NEW
        # version. The UNIQUE index below is the real backstop (a second
        # demotion attempt for the same card_id+version is a DB-level no-op,
        # not just an application-level check). Fires only after >= 2
        # CONSECUTIVE breach snapshots (audit A2 -- a sequential-test crumb
        # against single-night noise), computed by re-querying
        # card_scoreboard_snapshots history (the SAME "no separate counter
        # column, count the streak from history" idiom
        # alphaos.scheduler.cadence.is_fused() already uses for job_runs).
        # Slice 2 (promotion, PR13/PR13.5) is explicitly out of scope here --
        # this table only ever demotes, never promotes or un-demotes.
        "card_demotions",
        """
        CREATE TABLE IF NOT EXISTS card_demotions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            demotion_id TEXT NOT NULL UNIQUE,
            card_id TEXT NOT NULL,
            card_version INTEGER NOT NULL,
            reason TEXT NOT NULL,
            triggering_snapshot_id_1 TEXT NOT NULL,
            triggering_snapshot_id_2 TEXT NOT NULL,
            alert_sent INTEGER NOT NULL DEFAULT 0,
            demoted_at_utc TEXT NOT NULL,
            demoted_at_sgt TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # HGEN-1: the draft quarantine -- the load-bearing safety property of
        # the whole Hypothesis Proposer build. A generated (or manually
        # authored) candidate hypothesis lands HERE first and ONLY here;
        # nothing in this table is ever read by compute_verdicts()'s family,
        # inserted into hypothesis_proposals, or linked to a preregistrations
        # row until an operator explicitly runs `hypothesis_accept` (which
        # calls the SAME propose_hypothesis() every seeded PR12 hypothesis
        # goes through -- never a second registration path). This is why a
        # draft can never shift the seeded 8's q-values: BH-FDR's family is
        # "every evaluated preregistration" (fdr.py's own module docstring),
        # and a quarantined draft touches none of preregistrations,
        # hypothesis_proposals, or evaluate_hypothesis() until accepted.
        #
        # status: 'draft' (awaiting operator review) -> 'accepted' (operator
        # ran hypothesis_accept; accepted_hypothesis_id now points at the
        # real hypothesis_proposals row propose_hypothesis() created) or
        # 'rejected' (operator ran hypothesis_reject, OR the intake pipeline
        # itself hard-blocked a duplicate -- see rejected_reason). Mechanical
        # lifecycle only, same non-semantic-verdict posture as
        # hypothesis_proposals.status (constants.HypothesisStatus's own
        # docstring) -- 'accepted'/'rejected' describe the DRAFT's own fate,
        # never a statistical verdict on the underlying claim (that verdict,
        # once the draft is accepted and resolved, comes from
        # compute_verdicts() exactly like every other hypothesis).
        #
        # source: 'generated' (HypothesisGenerator, alphaos/hypotheses/
        # generator.py) or 'manual' (a human-authored candidate run through
        # the same intake_draft() quarantine pipeline -- no CLI ships for
        # this in v0, but the schema/logic layer supports it so a future
        # thin wrapper needs no migration). model_id/model_provider/
        # prompt_hash/system_prompt_hash/lineage_id are NULL for source=
        # 'manual' (no LLM call was made) -- mirrors every other AI-producing
        # table's own "populated only on a real call" convention (agent_
        # votes, last30days_polarity).
        #
        # mechanical_risk_class is what alphaos.hypotheses.proposer's
        # classifier assigned (see its own module docstring for the mapping
        # + "default up a class on any ambiguity" rule) -- it, not
        # proposed_risk_class, is what hypothesis_accept actually passes to
        # propose_hypothesis() (floors are never settable by a draft any
        # more than by a seeded spec; RISK_CLASS_FLOORS still derives them
        # mechanically from the class alone).
        "hypothesis_drafts",
        """
        CREATE TABLE IF NOT EXISTS hypothesis_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            claim_text TEXT NOT NULL,
            metric_fn_name TEXT,
            direction TEXT,
            card_id TEXT,
            proposed_risk_class TEXT NOT NULL,
            mechanical_risk_class TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            source TEXT NOT NULL,
            model_id TEXT,
            model_provider TEXT,
            prompt_hash TEXT,
            system_prompt_hash TEXT,
            lineage_id TEXT,
            evidence_check_json TEXT,
            duplicate_check_json TEXT,
            rejected_reason TEXT,
            accepted_hypothesis_id TEXT,
            accepted_at_utc TEXT,
            accepted_by TEXT,
            created_at_utc TEXT NOT NULL,
            created_at_sgt TEXT NOT NULL
        )
        """,
    ),
    (
        # PR13 slice 2: one row per MANUAL card state-transition decision --
        # graduation (direction='promote', an existing shadow version moves
        # to live_eligible, CONTENT UNCHANGED -- v0 mints no new version;
        # see alphaos/cards/promotion.py's own module docstring for the
        # "graduation vs mutation" distinction a focused Fable5 consult
        # drew, 2026-07-10) or a manual override demotion
        # (direction='demote'). Deliberately a SEPARATE table from slice 1's
        # own card_demotions (which stays automatic-trigger-only, unchanged,
        # per that consult's own "don't reopen it" ruling) -- the full
        # transition history is a reporting-level UNION of both tables, not
        # a shared one. `preregistration_id` is required (enforced in
        # promotion.py, not a DB constraint) for direction='promote' only --
        # audit A4's reconstructability payload; a manual demote is an
        # operator override that needs no evidence to be safe. `trigger` is
        # always 'manual' for rows this module writes (an automatic
        # promotion would violate Prime Directive 3 and cannot exist by
        # construction -- promotion.py has no caller that doesn't pass an
        # operator-supplied decided_by). `research_ref` is required (again,
        # enforced in code) only when the underlying hypothesis's
        # risk_class='C' (PD#9) -- unreachable today since none of the 8
        # seeded hypotheses with a card_id are Class C, fixture-tested only.
        # `lineage_id` is deliberately left NULL by both writers in v0
        # (scope/safety-audit NIT): populating it would mean threading
        # `settings` through promote_card()/demote_card() for a column no
        # report currently reads -- correct scope for a later pass once a
        # real consumer needs it, not a gap in this one.
        "promotion_decisions",
        """
        CREATE TABLE IF NOT EXISTS promotion_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT NOT NULL UNIQUE,
            card_id TEXT NOT NULL,
            card_version INTEGER NOT NULL,
            from_state TEXT NOT NULL,
            to_state TEXT NOT NULL,
            direction TEXT NOT NULL,
            trigger TEXT NOT NULL DEFAULT 'manual',
            hypothesis_id TEXT,
            preregistration_id TEXT,
            decided_by TEXT NOT NULL,
            research_ref TEXT,
            evidence_json TEXT,
            lineage_id TEXT,
            decided_at_utc TEXT NOT NULL,
            decided_at_sgt TEXT NOT NULL,
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
    # PR6: supersession looks up open proposals by symbol.
    "CREATE INDEX IF NOT EXISTS idx_proposals_symbol ON trade_proposals(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_orders_proposal ON paper_orders(proposal_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_state ON paper_orders(state)",
    "CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_fills_order ON paper_fills(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)",
    "CREATE INDEX IF NOT EXISTS idx_exits_position ON exits(position_id)",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_position ON trade_outcomes(position_id)",
    "CREATE INDEX IF NOT EXISTS idx_sysevents_sev ON system_events(severity)",
    "CREATE INDEX IF NOT EXISTS idx_approvals_label ON approvals(label)",
    "CREATE INDEX IF NOT EXISTS idx_candidates_scan_batch ON candidates(scan_batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_positions_trade ON positions(trade_id)",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_trade ON trade_outcomes(trade_id)",
    "CREATE INDEX IF NOT EXISTS idx_riskchecks_proposal ON risk_checks(proposal_id)",
    "CREATE INDEX IF NOT EXISTS idx_monitoring_position ON monitoring_snapshots(position_id)",
    "CREATE INDEX IF NOT EXISTS idx_scheduler_runs_batch ON scheduler_runs(scan_batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_calibration_trade ON execution_calibration(trade_id)",
    "CREATE INDEX IF NOT EXISTS idx_calibration_order ON execution_calibration(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_packets_candidate ON candidate_packets(candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_packets_scan_batch ON candidate_packets(scan_batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_labels_candidate ON candidate_labels(candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_labels_scan_batch ON candidate_labels(scan_batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_catalysts_candidate ON candidate_catalysts(candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_catalysts_scan_batch ON candidate_catalysts(scan_batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_last30days_candidate ON candidate_last30days(candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_last30days_scan_batch ON candidate_last30days(scan_batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_decadj_candidate ON decision_adjustments(candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_decadj_scan_batch ON decision_adjustments(scan_batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_polarity_candidate ON last30days_polarity(candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_polarity_scan_batch ON last30days_polarity(scan_batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_overrides_candidate ON user_decision_overrides(candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_overrides_symbol ON user_decision_overrides(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_overrides_status ON user_decision_overrides(outcome_status)",
    # One outcome row per (candidate, counterfactual path) — enforces idempotent
    # seeding at the DB level (seeding also pre-checks, this is defense in depth).
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_candoutcomes_candidate_type "
    "ON candidate_outcomes(candidate_id, candidate_type)",
    "CREATE INDEX IF NOT EXISTS idx_candoutcomes_status ON candidate_outcomes(outcome_status)",
    "CREATE INDEX IF NOT EXISTS idx_candoutcomes_symbol ON candidate_outcomes(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_candoutcomes_scan_batch ON candidate_outcomes(scan_batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_protchecks_position ON protection_checks(position_id)",
    "CREATE INDEX IF NOT EXISTS idx_protchecks_open ON protection_checks(protection_status, resolved_at_utc)",
    "CREATE INDEX IF NOT EXISTS idx_jobruns_type ON job_runs(job_type)",
    "CREATE INDEX IF NOT EXISTS idx_jobruns_status ON job_runs(status)",
    "CREATE INDEX IF NOT EXISTS idx_jobruns_lock_key ON job_runs(lock_key)",
    # At most one active (started/completed) job_runs row per lock_key --
    # enforces JobRunner.acquire()'s idempotency guarantee at the DB level
    # (acquire() also pre-checks via SELECT, this is defense in depth against
    # a check-then-insert race between two concurrent scheduler invocations).
    # A 'failed' row is deliberately excluded so a failed window can still be
    # retried under the same lock_key.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobruns_lock_key_active ON job_runs(lock_key) "
    "WHERE status IN ('started', 'completed')",
    # PR4 decision lineage: forward lookups (decision row -> lineage_snapshots
    # row) go through each table's own lineage_id value directly (no index
    # needed for a single-row fetch by unique lineage_id on the small
    # lineage_snapshots table itself -- UNIQUE already indexes it). These
    # indexes are for the reverse direction: "every decision made under this
    # lineage/config snapshot", used by CLI reporting and tests.
    "CREATE INDEX IF NOT EXISTS idx_candidates_lineage ON candidates(lineage_id)",
    "CREATE INDEX IF NOT EXISTS idx_proposals_lineage ON trade_proposals(lineage_id)",
    "CREATE INDEX IF NOT EXISTS idx_rejected_lineage ON rejected_candidates(lineage_id)",
    "CREATE INDEX IF NOT EXISTS idx_decisionadj_lineage ON decision_adjustments(lineage_id)",
    "CREATE INDEX IF NOT EXISTS idx_useroverrides_lineage ON user_decision_overrides(lineage_id)",
    "CREATE INDEX IF NOT EXISTS idx_candoutcomes_lineage ON candidate_outcomes(lineage_id)",
    "CREATE INDEX IF NOT EXISTS idx_tradeoutcomes_lineage ON trade_outcomes(lineage_id)",
    # PR5: earnings proximity.
    "CREATE INDEX IF NOT EXISTS idx_earnings_candidate ON candidate_earnings(candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_earnings_scan_batch ON candidate_earnings(scan_batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_earnings_lineage ON candidate_earnings(lineage_id)",
    # PR7: TQS v0 shadow scoring. Idempotency at the DB level: SQLite treats
    # every NULL as distinct from every other NULL, so a plain
    # UNIQUE(source_type, candidate_id, proposal_id, tqs_version) would NOT
    # stop two candidate-level rows (proposal_id IS NULL both times) for the
    # same candidate+version from being inserted twice. Two PARTIAL unique
    # indexes close that gap: the candidate-level one never mentions
    # proposal_id at all, and the proposal-level one only applies where
    # proposal_id is actually populated (never NULL there, since a
    # source_type='proposal' row is only ever inserted with a real
    # proposal_id) -- mirrors PR3's idx_jobruns_lock_key_active partial-index
    # pattern for the same class of problem.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tqs_candidate_unique "
    "ON tqs_scores(candidate_id, tqs_version) WHERE source_type = 'candidate'",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tqs_proposal_unique "
    "ON tqs_scores(candidate_id, proposal_id, tqs_version) WHERE source_type = 'proposal'",
    "CREATE INDEX IF NOT EXISTS idx_tqs_scan_batch ON tqs_scores(scan_batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_tqs_bucket ON tqs_scores(tqs_bucket)",
    "CREATE INDEX IF NOT EXISTS idx_tqs_lineage ON tqs_scores(lineage_id)",
    # PR8: Attribution v2. Same SQLite NULL-uniqueness trap as tqs_scores above,
    # same cure: two PARTIAL unique indexes. Proposal-anchored types
    # (propose_user_rejected/propose_approved_executed/propose_expired/
    # propose_blocked) never share a proposal_id with an override-anchored row
    # (user_override_trade), so the two indexes never compete for the same row.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_attr_proposal_unique "
    "ON attribution_records(attribution_type, proposal_id, attribution_version) "
    "WHERE proposal_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_attr_override_unique "
    "ON attribution_records(attribution_type, override_id, attribution_version) "
    "WHERE override_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_attr_resolved_status ON attribution_records(resolved_status)",
    "CREATE INDEX IF NOT EXISTS idx_attr_type ON attribution_records(attribution_type)",
    "CREATE INDEX IF NOT EXISTS idx_attr_candidate ON attribution_records(candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_attr_lineage ON attribution_records(lineage_id)",
    # PR9.5: benchmark spine. One equity snapshot per trading day (idempotent
    # capture -- a second same-day run must not double-insert); one bar per
    # (symbol, date) (idempotent re-fetch/backfill).
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_equity_snapshots_date ON equity_snapshots(market_date)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_benchmark_bars_symbol_date ON benchmark_bars(symbol, bar_date)",
    "CREATE INDEX IF NOT EXISTS idx_benchmark_bars_symbol ON benchmark_bars(symbol)",
    # PR10: one registry row per (card_id, version) -- neither column is ever
    # NULL here (both are required fields on every card), so a plain UNIQUE
    # is safe; no NULL-uniqueness trap like tqs_scores/attribution_records.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_setup_cards_id_version ON setup_cards(card_id, version)",
    "CREATE INDEX IF NOT EXISTS idx_candidates_card ON candidates(card_id)",
    "CREATE INDEX IF NOT EXISTS idx_proposals_card ON trade_proposals(card_id)",
    # EXP-0: one universe_days row per (symbol, market_date) -- the backstop
    # that makes multi-window-per-day writes idempotent (same idiom as
    # idx_benchmark_bars_symbol_date: attempt an insert every scan window,
    # let the DB reject same-day repeats via IntegrityError).
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_universe_days_symbol_date ON universe_days(symbol, market_date)",
    "CREATE INDEX IF NOT EXISTS idx_universe_days_date ON universe_days(market_date)",
    "CREATE INDEX IF NOT EXISTS idx_candidates_shadow_tier ON candidates(shadow_tier)",
    # REG-1: one regime_days row per (market_date, regime_rules_version) --
    # a v2 rules recompute adds new rows under the new version rather than
    # colliding with (or mutating) v1 rows for the same date.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_regime_days_date_version ON regime_days(market_date, regime_rules_version)",
    "CREATE INDEX IF NOT EXISTS idx_candidate_packets_regime ON candidate_packets(regime)",
    # TEXT-0: re-fetch of an already-archived accession is a no-op (idempotent
    # insert idiom, same as universe_days/benchmark_bars/regime_days).
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_text_documents_accession ON text_documents(accession_no)",
    "CREATE INDEX IF NOT EXISTS idx_text_documents_cik ON text_documents(cik)",
    "CREATE INDEX IF NOT EXISTS idx_text_documents_seen_at ON text_documents(seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_text_documents_fetch_run ON text_documents(fetch_run_id)",
    # EVAL-1: recent-run lookups (report defaults to the latest run) and
    # per-run result joins.
    "CREATE INDEX IF NOT EXISTS idx_eval_runs_started ON eval_runs(started_at_utc)",
    "CREATE INDEX IF NOT EXISTS idx_eval_results_run ON eval_results(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_eval_results_packet ON eval_results(packet_id)",
    # PORT-1: compute_verdicts()'s family is "every evaluated preregistration"
    # -- this is the index that lookup leans on.
    "CREATE INDEX IF NOT EXISTS idx_preregistrations_evaluated ON preregistrations(evaluated_at_utc)",
    # INSTR-1: idempotent-insert idiom, same role as idx_regime_days_date_version
    # -- a same-day re-run is a no-op, never a duplicate row.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_atr_history_symbol_date_version "
    "ON atr_history(symbol, market_date, rules_version)",
    "CREATE INDEX IF NOT EXISTS idx_atr_history_symbol ON atr_history(symbol)",
    # BASELINE: candidate_id/rule_version are both always populated (never
    # null), so a plain UNIQUE index is NULL-safe here -- unlike attribution_
    # records' proposal_id/override_id, no partial-index trick needed (house
    # pattern #2). One row per (candidate, rule) -- a re-run of the same scan
    # tick is structurally impossible (the write happens exactly once, inline
    # in run_scan_once), but the index is the real backstop regardless.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_baseline_decisions_candidate_rule "
    "ON shadow_baseline_decisions(candidate_id, rule_version)",
    "CREATE INDEX IF NOT EXISTS idx_baseline_decisions_replay_status "
    "ON shadow_baseline_decisions(replay_status)",
    "CREATE INDEX IF NOT EXISTS idx_baseline_decisions_symbol ON shadow_baseline_decisions(symbol)",
    # EARN-1: (symbol, report_date, fiscal_date_ending) is expected always
    # non-null in practice (AlphaVantage populates all three on every row
    # observed so far), so a plain UNIQUE index is the right primary
    # backstop here; the capture job ALSO wraps each insert in a try/except
    # IntegrityError (belt + suspenders, house pattern #2) in case a future
    # vendor response ever omits one.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_earnings_calendar_cache_symbol_date_fiscal "
    "ON earnings_calendar_cache(symbol, report_date, fiscal_date_ending)",
    "CREATE INDEX IF NOT EXISTS idx_earnings_calendar_cache_symbol ON earnings_calendar_cache(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_earnings_calendar_cache_report_date "
    "ON earnings_calendar_cache(report_date)",
    # CANARY: recent-run lookups (report/status default to the latest run;
    # drift comparison reads the one pinned baseline) and per-run result joins.
    "CREATE INDEX IF NOT EXISTS idx_canary_runs_started ON canary_runs(started_at_utc)",
    "CREATE INDEX IF NOT EXISTS idx_canary_runs_is_baseline ON canary_runs(is_baseline)",
    "CREATE INDEX IF NOT EXISTS idx_canary_results_run ON canary_results(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_canary_results_packet ON canary_results(packet_id)",
    # PR14: one vote per (proposal, role) -- a re-run of the same batch's
    # debate pass must be a no-op, never a duplicate vote (idempotent-insert
    # idiom, same as idx_universe_days_symbol_date/idx_regime_days_date_version).
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_votes_proposal_role "
    "ON agent_votes(proposal_id, agent_role)",
    "CREATE INDEX IF NOT EXISTS idx_agent_votes_candidate ON agent_votes(candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_votes_scan_batch ON agent_votes(scan_batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_votes_created ON agent_votes(created_at_utc)",
    # PR12: hypothesis_id is the primary lookup key (propose_hypothesis()'s
    # own idempotency check); status/analysis_not_before back the resolver's
    # daily "what's due" scan.
    "CREATE INDEX IF NOT EXISTS idx_hypothesis_proposals_status ON hypothesis_proposals(status)",
    "CREATE INDEX IF NOT EXISTS idx_hypothesis_proposals_prereg ON hypothesis_proposals(prereg_id)",
    # PR13 slice 1: one snapshot per (card, version, day) -- idempotent
    # re-run idiom (same as idx_universe_days_symbol_date/idx_regime_days_
    # date_version: attempt an insert every daily tick, let the DB reject a
    # same-day repeat). idx_card_demotions_card_version is the anti-double-
    # jeopardy backstop -- a demotion is terminal, enforced at the DB level,
    # not just in application code.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_card_scoreboard_snapshots_card_date "
    "ON card_scoreboard_snapshots(card_id, card_version, evaluation_date)",
    "CREATE INDEX IF NOT EXISTS idx_card_scoreboard_snapshots_lookup "
    "ON card_scoreboard_snapshots(card_id, card_version, id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_card_demotions_card_version "
    "ON card_demotions(card_id, card_version)",
    # PR13 slice 2: "has this exact (card_id, version) ever had a manual
    # demote decision" is the other half of the anti-double-jeopardy check
    # (card_demotions covers the automatic half) -- both a card_promote
    # eligibility check and live_eligible_cards() query this. UNIQUE
    # (correctness-audit HIGH-2): a given (card_id, card_version) can have
    # AT MOST ONE 'promote' row (ALREADY_PROMOTED already refuses a second
    # one in application code) and AT MOST ONE 'demote' row
    # (CARD_VERSION_TERMINALLY_DEMOTED already refuses a second one) --
    # this index is the real DB-level backstop for that invariant under a
    # genuine concurrent-write race, matching every other "unique
    # constraint catches the loser" idiom in this codebase
    # (idx_jobruns_lock_key_active, hypothesis_proposals.hypothesis_id).
    # Before this fix the index was a plain (non-unique) INDEX, meaning
    # promotion.py's own sqlite3.IntegrityError catch could never actually
    # fire -- a concurrent double-promote would have silently inserted two
    # rows instead of being caught.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_promotion_decisions_card_version "
    "ON promotion_decisions(card_id, card_version, direction)",
    "CREATE INDEX IF NOT EXISTS idx_promotion_decisions_hypothesis ON promotion_decisions(hypothesis_id)",
    # HGEN-1: status backs the unreviewed-draft ceiling check + the
    # `hypothesis_drafts` list CLI's default view; (metric_fn_name,
    # direction) backs the duplicate-detection query's own lookup (not
    # UNIQUE -- duplicate-ness also depends on normalized title/claim_text
    # text, a Python-side comparison no SQL index can express, so this is a
    # narrowing index only, never the sole guard). created_at_utc backs the
    # 30-day cost-guard pool count.
    "CREATE INDEX IF NOT EXISTS idx_hypothesis_drafts_status ON hypothesis_drafts(status)",
    "CREATE INDEX IF NOT EXISTS idx_hypothesis_drafts_metric_direction "
    "ON hypothesis_drafts(metric_fn_name, direction)",
    "CREATE INDEX IF NOT EXISTS idx_hypothesis_drafts_created ON hypothesis_drafts(created_at_utc)",
    "CREATE INDEX IF NOT EXISTS idx_hypothesis_drafts_accepted_hypothesis "
    "ON hypothesis_drafts(accepted_hypothesis_id)",
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
SCHEMA_VERSION = 3
