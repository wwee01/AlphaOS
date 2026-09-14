# Collection receipt timing repair — 15 September 2026

This patch corrects the market-data receipt timestamp. It does not claim to restore shadow collection.

The provider stamped `received_at` before the single request, or once before all five 100-symbol batch requests. The September 11 first-window cohort contains 281 negative delay values among 500 snapshots. Independent clock probes reproduce that defect, and all six new regression cases fail on application `208544b`. The corrected provider stamps receipt immediately after each response body is read; each chunk keeps its own timestamp. Missing credentials and failed requests still return unusable observations. Source quote/bar timestamps, freshness thresholds, symbol order, request counts, provider/feed and trading limits are unchanged.

Time-normalized relative volume now uses the actual response receipt. This can change prospective derived values slightly near clock boundaries; a deployed revision boundary is required. No historical observations, first-window coverage, model outputs or frozen evidence are rewritten. Negative delay from genuine clock/source skew remains reportable; the fix removes the false negative delay caused by stamping before receipt.

Validation: six new transport-clock regressions; 95 combined provider, freshness, shadow-scanner and shadow-labelling tests passed (exit 0); Ruff and module mypy passed. Independent reviewer passed 18 overlapping checks and three additional provider probes covering stale bars, missing symbols and invalid JSON. Counts overlap. The unchanged guard still rejects stale/future source timestamps.

Collection diagnosis remains separate. In the observed September 11 first window, all 95 stale bars were already older than 180 seconds at request start. Removing processing delay would not make them usable. The first September 14 window reached 413/500 usable symbols, while the trailing 14-day median was still 78.1% against 80%. A separate five-request IEX diagnostic at 00:10 SGT September 15, using actual receipt timestamps, returned 394 usable, 98 stale bars and 8 stale quotes. It had zero negative measured delays and was not written into the production collection ledger. Later diagnostics do not replace the immutable first-window denominator.

The provider's documented snapshot contains latest quote/trade, minute and daily bars: https://docs.alpaca.markets/us/reference/stocksnapshotsingle . The observed stale timestamps are present in raw upstream responses; this does not distinguish sparse eligible trades from provider aggregation or distribution delay. A paid feed, new threshold, removed symbol, forced label or cleared latch is not part of this patch.

Implementation is ready for integration after review. Deployment and actual post-deployment observations must be recorded separately; this file is not deployment proof.
