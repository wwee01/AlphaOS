# D1: continue pending legacy replays without changing evidence provenance

Claude's D1 probe correctly identified a continuation defect: a legacy row could complete its five-day outcome family while its ten-day replay remained pending, then disappear from the update selection because its `replay_method_version` was NULL.

The repair selects an already-complete five-day row only when its replay is explicitly `pending`, its frozen window is positive, both its final replay result and R are absent, and its method is either NULL or the current repaired version. Thus already-stranded rows can resume on the next normal outcomes pass. A legacy row transitioning from partial to complete follows the same path.

The updater deliberately preserves the original method stamp. In the existing report contract, a NULL stamp excludes legacy/unverified evidence; assigning the repaired stamp retrospectively would wrongly admit historical rows to the prospective diagnostic cohort. This repair does not backfill that stamp, change baseline eligibility, consume correction sidecars, or modify any preregistration or frozen verdict.

Completed replay values, ambiguous final replay results, unavailable outcome rows, unknown method versions and missing/invalid continuation windows are not reopened. Continuation writes preserve completed forward statistics. If the existing no-bars timeout marks a pending replay unavailable, the five-day family's completed status remains intact. The separate HOLD-1 ten-day shadow family retains its existing writer and semantics.

## Verification

- 220 passing tests across `test_replay_maturity_repair`, `test_outcomes_tracker`, `test_outcomes_engine`, `test_baseline`, `test_research_contract_quarantine`, `test_aileg1_replay_window_coherence`, and `test_vocab_guard`.
- 70 passing tests across `test_hold1_10day_shadow`, `test_hold2_10day_window`, `test_learning_tab`, and `test_research_status`.
- Ruff passed both changed Python files. Mypy passed `alphaos/learning/outcomes_tracker.py`.
- New and extended regressions verify legacy and new rows observing day-six target, stop and same-bar ambiguity; an already-stranded row remaining pending at nine bars and completing at ten; preserved five-day values; terminal idempotence even after supplied bars change; excluded final/unavailable/unknown evidence; and continued legacy exclusion while a mature v2 pair can enter the descriptive baseline report.

Read-only selection checks used the consistent copied ledger `funnel-review-ledger-20260913T094201Z.db`, captured September 13 at 09:42:01 UTC. Both old and repaired selectors currently match 2,055 rows: 1,557 legacy pending and 498 legacy partial. Of those partial rows, 35 have explicitly pending replays. No row is already stranded in that snapshot. The tests reproduce that state directly rather than claiming it exists in this snapshot.

No data backfill or migration is required. No production ledger, broker, configuration, decision policy or orchestration changes were made. Verification is offline and focused; the integrating owner remains responsible for the full integrated suite and deployment.

## Remaining limits

This change completes the pending replay computation; it does not turn legacy observations into confirmatory evidence, establish profitable trading performance, or change the existing assumptions about completed daily bars, idealized fills, data-provider gaps or no-bars timeout. Original research contracts remain quarantined.
