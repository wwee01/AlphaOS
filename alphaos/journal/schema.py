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
