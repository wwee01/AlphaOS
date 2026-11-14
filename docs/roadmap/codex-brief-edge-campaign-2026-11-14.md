# Codex brief — edge campaign (EXPLORE-1), 14 November 2026 SGT

**From CK.** Read `docs/roadmap/alphaos-checkpoint-audit-2026-11-14.md` first (commit `bbc5bdb`). CK has
accepted its recommendations. This brief replaces your current priority list.

## Why the direction changes

Execution works: 12 trades since the September repairs, no manual step, no protection incident. But the
account is −4.8 % against SPY +1.0 %, no layer of the decision stack shows selection edge, and the
evidence program has been stopped for two months. 19 of 22 live trades were entered when the primary
evaluator said reject or watch and the classifier overrode it; the classifier vetoes about 92 % of the
evaluator's own proposals. The combined policy is retired as of this brief.

The goal for the next four weeks is **evidence rate**: as many tagged, comparable decisions and paper
trades as the account can carry, so we learn which component — if any — has edge. This is a paper
account. CK accepts more trades and more losing trades in exchange for learning.

## Stop (frozen until CK says otherwise)

- The collection store, segmented codec, catalog/radix capacity work and synthetic 61-capture runs.
- The paper observer, learning-audit provenance and calendar-calibration work.
- Any further execution hardening not listed below.

Preserve the files and evidence. Do not continue them.

## Do — in this order

### P0. Housekeeping (target: 2 days)

1. **Nothing runs in production that is not committed.** `alphaos/scheduler/worker.py` and
   `alphaos/data/providers/alpaca_data.py` are modified; `alphaos/research/swing_*.py`,
   `data_freshness_trial.py` and `alphaos/monitoring/` are untracked and loaded by the research worker.
   Commit them through review or remove them. State which, per file.
2. **Make health exit codes mean something.** Heartbeat exits 1 on observer findings you have diagnosed
   as observer bugs; scheduler exits 1 on ordinary "not ready" skips. Detach the observer from the
   heartbeat exit code and return 0 for normal skips. A non-zero exit must mean a real fault.
3. **Per-symbol cap: one open position or pending entry per symbol** (three concurrent AMD positions
   in September). Enforce in the existing fresh-exposure gate.
4. **Deferral, not terminal block, when exposure is unreconciled after a same-pass entry.** 4 of 18
   proposals since the repairs were permanently blocked by `ExposureUnavailable` minutes after another
   fill. Leave the proposal pending and retry on the next scan while its TTL is valid. Same treatment
   for the "monitor not ready" block.
5. **External dead-man ping** from the monitor role to a hosted heartbeat service, so a dead Mac pages
   CK. CK will supply the URL as an environment value.

### E1. Lift the shadow-label latch (target: immediately after P0.1)

Your 8 November status reports rolling feed coverage 82.6 % (floor 0.80) and the canary 20/20. Run the
existing preview-first `shadow_recovery` procedure; CK authorises the apply once the preview passes all
gates. Then raise the shadow labelling volume: `SHADOW_AI_CAP_CALLS_PER_DAY` 15 → 60 and
`SHADOW_LABEL_TOP_K` 5 → 20 (CK-authorised; the 30-day cap rises to match). Shadow rows remain
measurement only — they never create proposals or orders.

### E2. EXPLORE-1: arm-tagged paper trading (target: live for the 23 November session)

Replace the evaluator/classifier combination rule, for order routing only, with explicit arms. Every
proposal, order, position and outcome carries `arm` and `policy_version = explore_1`.

| Arm | A candidate is traded when | Recent rate (since 1 Oct) |
|---|---|---|
| A — evaluator | primary evaluator says propose (classifier opinion recorded, not applied) | ≈1.8 symbol-days/day |
| B — classifier | classifier label says propose (evaluator opinion recorded, not applied) | ≈0.3/day |
| C — control | chosen uniformly at random, once per scan window, from evaluated long candidates that **neither** model proposed (this is where the 50-50 WATCH cases are exercised) | 3/day |

Rules:

- A candidate both models propose is traded once and tagged `A+B`.
- Long only for now. Short proposals stay blocked by the existing margin rule.
- Levels: the evaluator's entry/stop/target when they pass existing validation; otherwise the
  configured defaults (`STOP_LOSS_PCT`, `TARGET_REWARD_RISK`). Record which was used.
- Random selection uses a seeded generator; persist the seed and the eligible set per scan so the
  draw is reproducible.
- **CK-authorised paper limits for this experiment** (total heat and maximum notional are unchanged):
  `MAX_OPEN_POSITIONS` 5 → 20, `MAX_RISK_PER_TRADE_PCT` 0.01 → 0.0025, `MAX_AUTO_APPROVALS_PER_DAY`
  5 → 20. `MAX_DAILY_LOSS_PCT` stays 0.03. Per-symbol cap of one applies.
- Slot fairness: no arm may hold more than half the slots while another arm has a pending candidate.
- Everything else is unchanged and must stay in force: freshness and drift gates, earnings-proximity
  guard, fresh-account risk gate, broker-native bracket on every entry, time exits, kill switch,
  paper-only boundary.
- Keep recording the WATCH-1 diagnostic. Do not unquarantine any old registration.

### E3. One-page weekly scorecard (every Saturday, starting 28 November)

One markdown page in `docs/research/delivery-history/`. Per arm, plus shadow:

- labelled decisions (symbol-days), mean 5-day and 10-day market-adjusted forward return, day-clustered
  standard error;
- trades opened and closed, mean R, exit mix (stop / target / time), stops that fired within 30
  minutes of an open;
- proposals deferred or blocked, by reason.

No certification language, no evidence archive beyond the query that produced each number.

## Not yours

Claude owns: the historical-bar study of the non-AI parts (scanner score, exit geometry, the
overnight-gap pattern), the registration text for the primary question, and the audit of P0 and E2
before deployment. Do not build these.

## How to work

- **Use what exists.** Add columns to existing tables. No new storage engine, codec, sidecar process
  or service.
- **Size budget:** E2 should fit in about 600 changed lines of application code. If it cannot, stop
  and say why before building.
- **Ceremony:** every change committed; hand the exact SHA to Claude for audit; deploy only that SHA;
  CK gives the merge instruction. Limit changes above are applied by CK's authorisation in this brief
  and recorded in §9 of `docs/ALPHAOS_MASTER_REFERENCE.md`.
- **Handoffs:** one page. What changed, the SHA, what was tested, what is not done.

## Dates

| Date (SGT) | Milestone |
|---|---|
| 17 Nov | P0 done; shadow latch preview ready |
| 23 Nov | EXPLORE-1 live for the US session |
| 28 Nov | First weekly scorecard |
| 19 Dec | Four-week read: any arm whose 5-day result is clearly negative is dropped; anything promising continues |
| 15 Jan 2027 | Latest date for a final decision on each arm |

## Boundaries that do not move

Paper only. No real money. No change to the research quarantine. No historical rows rewritten. No
backfilled labels. If a requirement here conflicts with a safety control, stop and report rather than
weaken the control.
