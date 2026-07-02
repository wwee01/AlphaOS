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
