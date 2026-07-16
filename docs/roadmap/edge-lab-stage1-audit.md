# Edge Lab — Stage 1 Capability Audit (2026-07-16)

**Status: AUDIT COMPLETE. EVID-1 shipped (operator go-ahead 2026-07-16: "start
with EVID-1 as you recommended"), branch `feat/evid1-market-adjusted-evidence`,
T4 audit-fixup applied. Remaining slices (EVID-2, SETUP-1, SECT-1, INTRA-1)
still hold for their own explicit operator go-ahead.**

Provenance: two independent read-only Opus audit agents against main @ `ac8ed2e`
(post ND-8 + EXP-1 merges), one covering the evidence/data-path side, one covering
outcomes/analytics/jobs/data-feasibility. Both were structurally write-incapable
(no Edit/Write tools) and derived all schema facts from `schema.py`, code, and
tests — the production `data/alphaos.db` was never touched. This doc is the
operator-facing synthesis; the underlying agent reports are preserved in the
session transcript.

Origin: an external (ChatGPT) proposal for a "Minimal Edge Lab / counterfactual
evidence foundation" plus a setup roadmap (Earnings Interpretation Gap,
Catalyst-Verified Opening Drive, Residual Strength, calibration, later setups,
meta-allocator). Stage 1 of that proposal — audit before building — is this
document. The proposal's own constraint #3 ("avoid an Edge Lab monolith; reuse
existing architecture") turns out to be the single most correct thing in it.

---

## 1. Headline verdict

**Roughly 70% of the proposed foundation already exists, is actively wired, and
is tested.** AlphaOS already runs an idempotent, session-aware, counterfactual
outcomes pipeline (`candidate_outcomes`) covering essentially the ENTIRE candidate
funnel — proposals, blocked, rejected, armed-watch, plain candidates,
user-overrides, and shadow-tier — at 1/3/5-trading-day horizons with
MFE/MAE magnitudes, anchored no-lookahead on decision time, resolved hourly by an
existing job. The append-only / stamp-at-birth / version-everything discipline
the proposal demands as "non-negotiable constraints" is already house law here,
enforced in code and tests.

The genuinely missing pieces are few and specific:

1. **Per-candidate market-adjusted returns** — SPY daily bars exist
   (`benchmark_bars`, captured daily); nothing joins them to
   `candidate_outcomes`. Cheapest high-value gap.
2. **Sector context and sector-adjusted returns** — `candidates.sector_cluster_key`
   is a reserved column with ZERO writers; no sector ETF return series is stored.
   True gap, needs a data-source decision.
3. **Setup identity as a discriminator** — the versioned setup-card machinery
   (PR10/PR13: `setup_cards`, content-hash-guarded versions, scoreboard,
   promotion/demotion) is EXACTLY the setup_id/setup_version host the proposal
   wants, and `card_id`/`card_version` are already stamped on every candidate and
   proposal — but stamped as a CONSTANT (`catalyst_momentum_v2` /
   `"momentum_continuation"` everywhere). Storage exists; a card-selection
   (setup classifier) step does not.
4. **Intraday markouts (30-min, session-close)** — structurally impossible on
   stored data. No continuous intraday bars exist anywhere; the bars provider
   hardcodes `timeframe=1Day`; price capture is 3 scan-window snapshots/day.
   Session-close ≈ already covered by the 1-day daily-bar markout; 30-min needs a
   new fetch capability (feasible via Alpaca, with IEX-sparsity caveats).
5. **Time-to-excursion** — MFE/MAE magnitudes exist per 1/3/5d window; WHEN the
   extreme occurred is not stored.
6. **Core-tier survivorship** — "every scanned symbol per window" exists only for
   the shadow tier (`universe_days`). A core-tier symbol that passes gates but
   isn't interesting leaves only a `price_snapshots` row.
7. **Point-in-time packets for everyone** — the frozen evidence packet
   (`candidate_packets`, the exact AI input, replayable) is written only for
   shortlisted candidates. The packet build itself is pure/cheap; only the AI
   call is expensive. Un-gating the freeze is a small change.

And one blunt data-feasibility ruling:

8. **Earnings Interpretation Gap V1 is not buildable as specified.** The current
   stack supplies earnings event dates, BMO/AMC timing, and ONE pre-event
   consensus EPS estimate (Alpha Vantage `EARNINGS_CALENDAR`, free tier,
   25 req/day, once-daily pull, append-only point-in-time). It supplies NO
   reported actuals, NO revenue estimates, NO estimate-revision history, NO
   guidance, NO margins/FCF, NO management language. The proposal's own
   "information-strength" input list is ~90% unobtainable. An
   "interpretation gap" (information strength minus market response) cannot be
   measured when the information-strength side has no inputs; the setup would
   silently degenerate into the "EPS beat = bullish" classifier the proposal
   explicitly forbids. What IS buildable from current data: a
   post-earnings-reaction setup (known event date/timing + price/volume/relative
   reaction + forward markouts) — honest, but a different and weaker thesis.

## 2. Requirement matrix

Statuses use the proposal's own vocabulary. "Wired" = actively producing records
in the default pipeline today (not dormant code).

| # | Requirement | Status | Where / notes |
|---|---|---|---|
| 1 | Every evaluated candidate recorded (not only proposals) | PARTIALLY_IMPLEMENTED | `candidates` + `price_snapshots` per scanned symbol; shadow tier gets full `universe_days` survivorship; core-tier non-candidates leave only snapshots |
| 2 | Rejected candidates + structured reasons | EXISTS_AND_REUSABLE | `rejected_candidates` w/ `ReasonCode` enum, 3 wired write paths (scan gates, post-eval, order manager); scanner-stage rejects have NULL candidate_id (join by symbol+batch) |
| 3 | Risk/execution-gate blocks + reasons | EXISTS_AND_REUSABLE | `trade_proposals.status='blocked'` + `risk_checks` + parallel `rejected_candidates` row; read reasons from `risk_checks`, not free-text `proposal_reason` |
| 4 | Qualified-but-never-triggered / expired | EXISTS_AND_REUSABLE | PR6 TTL machinery: `expired`/`superseded` statuses w/ reasons+timestamps; watch-never-proposed identifiable via `candidates.status='watch'` |
| 5 | Setup identity + setup version | EXISTS_BUT_NEEDS_EXTENSION | `(card_id, card_version)` stamped on candidates AND proposals, hash-guarded append-only registry, scoreboard/promotion lifecycle — but a constant today; needs a card-selection step and ≥2 real cards |
| 6 | Feature snapshots at decision time | PARTIALLY_IMPLEMENTED | `candidate_packets` = exact frozen AI input, replayable (EVAL-1/TASK-R proven) — but shortlist-only; `candidates` row freezes scan-time price/volume/scores for all |
| 7 | Data-source + model lineage | EXISTS_AND_REUSABLE | `lineage_snapshots` (git SHA, 15 config hashes) FK'd from every decision table; `system_prompt_hash` CANARY-pinned; `is_mock` stamped everywhere; caveat: `scanner_version` etc. are hand-bumped literals |
| 8a | Market regime context | EXISTS_AND_REUSABLE | REG-1 `regime_days` (SPY SMA50/200, realized vol, chop), computed per scan, stamped on packets; the master-reference "regime tag v0" MED is RESOLVED, not open |
| 8b | Sector/industry context | MISSING | `sector_cluster_key` reserved, zero writers; no sector ETF return series stored |
| 9 | Hypothetical vs actual entry prices | EXISTS_AND_REUSABLE | `would_be_entry/stop` on rejects; eval entry/stop/target; proposal prices w/ source provenance; fills; `baseline_outcomes` freezes counterfactual P&L for every evaluator-reaching candidate |
| 10 | Event/catalyst context point-in-time | EXISTS_AND_REUSABLE | `candidate_earnings` (EARN-1), `candidate_catalysts` (official news via the ACTIVE Alpaca news provider; the direct Benzinga connector is the deferred one), `candidate_last30days` + polarity — all append-only, shortlist-scoped, budget-capped |
| 11 | Forward returns, fixed horizons | EXISTS_BUT_NEEDS_EXTENSION | `candidate_outcomes` 1/3/5-trading-day returns + R, all populations incl. shadow, hourly idempotent job, no-lookahead anchor; missing 30-min (no intraday bars) — session-close ≈ 1-day daily bar |
| 12 | MFE/MAE | PARTIALLY_IMPLEMENTED | Magnitudes per 1/3/5d window (candidates) + `trade_outcomes.mfe/mae` (real trades, backfillable); time-to-excursion NOT stored; daily-bar resolution only; same-bar ambiguity explicitly surfaced not guessed |
| 13 | Market-adjusted returns per candidate | EXISTS_BUT_NEEDS_EXTENSION | SPY `benchmark_bars` daily series + pure compute helpers exist (`relative_performance.py`, portfolio-level); per-candidate subtraction is a join + new columns |
| 14 | Sector-adjusted returns | MISSING | No sector return data at all; `benchmark_bars` schema is generic (symbol column) so it COULD hold the ~11 SPDR sector ETFs via the existing daily-bars provider; symbol→sector map is the missing input |
| 15 | Calibration/report infra for setup review | EXISTS_AND_REUSABLE | PR13 `compute_card_scoreboard` (expectancy, clustered-bootstrap CI, effective-N, floor gating) IS a per-setup-version evidence report; PR12 evidence queries w/ correct latest-outcome dedup; BH-FDR over the full preregistration family (`stats/fdr.py`) |
| 16 | Idempotent delayed-outcome jobs | EXISTS_AND_REUSABLE | `outcomes_update` hourly: pending/partial-only touch, NOT EXISTS seeding, decision-time anchor, unavailable-after-N-days gate; baseline + attribution resolvers are exact mirrors — the template to copy |
| 17 | Trading-calendar correctness | EXISTS_BUT_NEEDS_EXTENSION | `nth_trading_day_after`/`trading_days_between`, full NYSE holiday set, tested; HOL-2 gap: early closes NOT modeled — harmless for daily-bar markouts, a real correctness bug for any future intraday markout |
| 18 | AI cost governance for new classifiers | EXISTS_AND_REUSABLE | `cost_guard` 30d global cap + EXP-1's nested sub-cap precedent (`check_shadow_budget`: own 30d cap ≤25% of global + daily cap + global check, whole-batch pre-flight refusal) — mandatory pattern for any setup classifier |
| 19 | EIG-V1 information-strength inputs | MISSING (structural) | See §1 item 8 — free-tier stack supplies event timing + one consensus EPS estimate only |
| 20 | Regime-aware meta-allocator | DEFERRED_BY_EVIDENCE_GATE | Correctly deferred by the proposal itself; no setup has ANY forward observations yet, let alone several setups across regimes |

## 3. Architecture ruling

**No new platform, no new database, no new dashboard, no third taxonomy.** The
Edge Lab is an extension of four existing pillars:

- **Setup identity** = the existing setup-card registry. `setup_id/setup_version`
  ≡ `(card_id, card_version)`. The work is a card-selection step (deterministic
  classifier assigning cards at scan time) plus authoring a second real card —
  NOT new tables. This also inherits the promotion/demotion lifecycle and
  scoreboard for free, and keeps hypotheses (PR12) as the statistical
  preregistration layer above cards, exactly as today.
- **Evidence capture** = existing `candidates`/`candidate_packets`/
  `rejected_candidates`/`price_snapshots`/lineage, with two un-gatings: packet
  freeze for every evaluated candidate (pure function, cheap), and a core-tier
  survivorship ledger reusing the `universe_days` per-symbol pattern.
- **Outcomes** = existing `candidate_outcomes` + `outcomes_update` job, extended
  with market-adjusted (and later sector-adjusted) return columns and
  time-to-excursion. New columns ride the generic additive
  `_reconcile_columns()` migration — no hand-written migration.
- **Review** = PR13 scoreboard generalized to group by any setup key and report
  multiple horizons, with BH-FDR across the setup-version family.

All of it measurement-only: nothing new reads back into a decision path, nothing
touches gates/approval/sizing, everything stamps versions at birth and appends
rather than mutates. These aren't new constraints to enforce — they're the
existing house law the new code must simply keep following.

## 4. Recommended build slices (each = own PR, T4 protocol, shadow-only)

Ranked by value ÷ effort. Slices below are green-lit only once explicitly
marked SHIPPED with a commit SHA — the rest still need an explicit operator
go-ahead before any build starts.

- **EVID-1 (highest value, zero new data) — SHIPPED 2026-07-16**, branch
  `feat/evid1-market-adjusted-evidence`, build `faeaff7` + T4 audit-fixup
  (2 parallel Opus audits: correctness found 2 HIGH + 2 MED, scope/safety
  found 1 MED + several LOW, all fixed and regression-tested). Per-candidate
  market-adjusted 1/3/5d returns (joins `benchmark_bars` SPY in
  `update_pending_outcomes`, date-aligned to the candidate's own bar dates)
  + time-to-excursion columns + `alphaos/cards/setup_evidence.py` generalizing
  the card scoreboard to a per-setup-key, multi-horizon evidence report with
  BH-FDR (`setup_evidence_report` / `setup_population_breakdown` CLI
  commands). Answers "did approvals beat the population?" and "did rejects
  outperform?" — the two questions AlphaOS genuinely cannot answer today.
  Holding for explicit per-PR merge instruction, per this repo's standing
  protocol.
- **EVID-2 (small):** un-gate `candidate_packets` freeze for every evaluated
  candidate; add core-tier per-window survivorship rows (reuse the shadow
  pattern). Makes every future setup measurable from first observation.
- **SETUP-1 (the real taxonomy step):** deterministic card-selection at scan
  time + author a second setup card (the proposal's Catalyst-Verified Opening
  Drive is a reasonable candidate for card #2, WITHOUT its intraday-trigger
  variants initially — daily-bar evidence only). Turns `card_id` from a constant
  into a discriminator.
- **SECT-1 (data decision first):** capture ~11 SPDR sector ETF daily bars into
  `benchmark_bars` via the existing provider + commit a static symbol→sector map
  for the ~550-name universe; then sector-adjusted returns are the same join as
  EVID-1. The map's source/maintenance is the operator decision.
- **INTRA-1 (decide separately; real cost):** 30-min bars fetch capability +
  HOL-2 early-close calendar fix, enabling same-day markouts. Argument for: it
  collapses the evidence loop from ~1 week to same-day. Arguments against: new
  data infrastructure, IEX free-feed sparsity on exactly the small-caps that
  matter, rate limits. Recommend deferring until EVID-1's daily-bar evidence
  proves a setup worth the sharper lens.
- **EIG-V1: NOT as specified.** Either (a) rescope honestly to
  `POST_EARNINGS_REACTION_V1` using data we have, or (b) defer pending a
  fundamentals data-source decision (paid estimates/actuals feed). Do not build
  a setup whose defining input is structurally absent.
- **Meta-allocator: deferred**, interface spec only, after ≥2 setup versions
  have completed forward populations across ≥2 regimes with cost-adjusted
  expectancy CIs clear of zero (exact gate to be set in the SETUP-1 spec).

## 5. Sample-accrual reality check

Shadow tier (~300–500 names, outcomes already seeded unconditionally): a setup
firing on 5–15% of names/day reaches ~100 observations in **days-to-weeks** —
this is where setup evidence will actually come from. Tradeable-core
proposal-level populations accrue at single-digits/day across ALL setups: **many
weeks to months** per setup version, plus ~1 week completion latency per 5-day
cohort. Expect effective-N (clustered bootstrap, PR13) well below raw N when a
setup fires on correlated names in one window. Nobody should read week-2 numbers
as signal, and the scoreboard's existing floor/CI machinery is what says so.

## 6. Corrections to the external proposal worth recording

- Its Stage-3 "build an idempotent forward-outcomes mechanism" is ~80% built and
  running hourly; the audit prevented a full duplicate implementation.
- Its regime-context requirement is already satisfied (REG-1) despite the master
  reference still listing regime as an open MED at line ~373 — the resolution is
  recorded at line ~1286; reconciled here.
- Its "continue automatically into implementation" instruction was dropped —
  incompatible with this repo's per-slice audit-and-hold protocol.
- Its AI-cost silence was closed: any setup classifier gets a nested sub-cap per
  the EXP-1 precedent, mandatory.
- The active official-news path is the Alpaca news provider
  (`NEWS_ENRICHMENT_PROVIDER=alpaca`); the DIRECT Benzinga API connector is
  deferred. Both facts are true; earlier summaries conflating them are
  superseded by this line.
