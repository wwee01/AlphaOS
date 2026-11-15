# Proposed swing-research freshness policy v1

**ID:** `swing_research_freshness_v1` · **Date:** October 18, 2026 · **Status:** review only; inactive.

## Evidence and purpose

The frozen October 14–16 availability trial passed. Its six forward windows on October 15–16 contain 3,000 observations with complete binding and integrity evidence. Price-valid opening availability improved from 415 to **435/500** (87%; +20) and from 395 to **418/500** (83.6%; +23). Across those windows, the price check rejected **75 raw additions**; accepted invalid prices were zero. October 14 is retrospective, including its later windows.

This supports investigating research availability. It establishes neither profitability over the seven-to-ten-day swing horizon nor live-equivalent fills. A direct production bar-limit edit is unsupported. The trial is complete; this proposal neither extends it nor consumes a formal evaluation.

## Eligibility contract

Apply only to separately identified research discovery/availability outputs using retained Alpaca IEX snapshots, the existing 500-symbol universe version 2, and scheduled regular-session captures. Use the recorded assessment clock in UTC; replay must retain that original clock. Require one matching observation and snapshot per expected symbol, matching provider/feed and response lineage, non-mock data, and a regular session confirmed against the existing market calendar. Missing or duplicate responses remain failures in the fixed denominator; retries cannot replace the first retained scheduled capture.

All conditions must hold:

- Quote age **≤60 seconds**; source minute-bar age **≤300 seconds**; verified last-trade price age **≤180 seconds**.
- Assessment and component timestamps must be present, parseable and explicitly timezone-aware. Reject invalid/naive timestamps, broken request/receipt/assessment ordering, and missing lineage. A timestamp up to five seconds ahead of assessment is permitted solely as the existing clock-skew tolerance; record the raw negative age. Reject anything further ahead. All upper limits are inclusive. Receipt time cannot refresh a source timestamp.
- Require `price_evidence_version=normalized_last_price_v1` and `normalized_last_price_source=latestTrade`. Its timestamp must equal `parts.latestTrade.timestamp`. Persisted `last_price`, `normalized_last_price` and `raw_latest_trade_price` must each be positive and finite and agree within the existing serialization tolerance (`rel_tol=1e-10`, `abs_tol=1e-8`). Bind them to the same snapshot and response hash.
- Reject daily-close fallback, quote-mid substitution, missing/unverified price binding, mismatched values or symbols, and zero, negative, NaN or infinite prices. A daily bar cannot substitute for the required minute bar. Legacy timestamp-only evidence remains an explicitly unverified proxy; never backfill a binding.

Eligibility means data usable for this research view. Existing spread, liquidity, signal and risk requirements still apply. Retain the lightweight size/liquidity warning; detailed latency/slippage/impact modelling remains deferred.

## Boundaries

Keep the frozen baseline, existing quote/bar limits, strategy cards, prompts, holdings, risk limits, universe, broker configuration and execution routes unchanged. This policy grants no core-candidate promotion, proposal approval or order authority. Entry freshness/drift checks, protective orders, exits, kill switches and monitoring continue under their existing rules.

Keep the global **80%** shadow-labelling coverage gate unchanged: the trailing 14-calendar-day median uses the first retained daily `universe_days` evidence. Report alternative research coverage separately; never overwrite baseline rows or use this policy's coverage to declare that existing gate satisfied. Policy approval alone permits neither labelling calls nor suspension release.

## Proposed implementation points

If separately approved, add a pure research adapter such as `alphaos/research/swing_data_policy.py`, producing separate policy-tagged results from retained evidence. Reuse the diagnostic binding in `alphaos/data/providers/alpaca_data.py::_source_provenance` and the price-validation logic in `alphaos/research/data_freshness_trial.py::price_validity`; preserve the completed runner and frozen protocol. Add strict timestamp/component validation in the new adapter.

Do not change `FreshnessGuard` defaults, `.env`, or route extra research admissions through `candidate_scanner.py::_maybe_candidate`. Any later `scan_shadow_tier` or labelling integration needs its own reviewed patch and must preserve `shadow_label.py` gates and `shadow_recovery.py` recovery checks. No new collector, broker request, service, paid data or experiment is proposed here.

## Audit, versions and rollback

Retain policy/version/hash, code identity, universe version, capture/scan/snapshot IDs, symbol/feed, response hash, request/receipt/assessment and component timestamps, raw ages and skew flags, price source/values/binding, baseline and policy verdicts, rejection reasons, denominator and missing/duplicate counts. Keep secrets and authorization headers out of evidence.

Write separate append-only policy/cohort outputs. Any eligibility change requires a new version; preserve prior outputs and the frozen baseline. Rollback stops using the new research output while retaining its audit history. It changes neither open-position protection nor suspension state.

## Acceptance before activation

Offline fixtures must test inclusive limits and just-over failures; five-second future tolerance and beyond; missing, naive and malformed times; invalid/fallback/mismatched prices; symbol/feed/lineage mismatch; duplicate and absent observations; and unchanged baseline decisions. Reproduce the frozen forward results, including 435/418 opening admissions and 75 rejected raw additions. Prove additional admissions cannot enter core/order routes. Tests must make no API calls or production-ledger writes.

## Separate suspension recovery and decisions

Recovery requires current proof that both shadow flags are enabled, kill is off, the unchanged coverage gate passes, no auto-suspend arm is active, strict retained canary evidence is valid, and planned calls fit existing budgets. Use the existing preview-first audited recovery with matching ledger/config/source/evidence identities and a valid preview, followed by separately authorized apply. Never clear a marker manually or infer recovery from this trial's pass.

**Pending decisions:** managing-agent and user review of this policy, then separate authorization for an implementation/test patch. Any runtime activation, labelling integration or suspension recovery requires its own review and approval. This document activates nothing and requests no numeric strategy/risk change.

Evidence: frozen protocol SHA-256 `3ee3ee14f295156b76850f30563a86e844a4e7a6b2d7944f69a2cdc107f4f795`; final report SHA-256 `00debb672f0f76525dd74d3314e4e774d881608f1be40d92ce63739b9b6a7df0`. Final report and verification are retained in `/Users/ck/Documents/Codex/2026-10-13/task/three-session-freshness-final{,-verification}.json`.
