# EXPLORE-1 — arm governor rule and primary-question registration, v1

> **STATUS CORRECTION, 17 November 2026:** read with `docs/research/explore1-rule-addendum-1-2026-11-17.md`,
> which corrects this document's sample counts and fixes the measurement and test conventions. Where
> they differ, the addendum governs.

**Author:** Claude (strategy). **Date:** 16 November 2026 SGT. **Authority:** CK ruling 16 November
("agreeable on the arm governor"; loss brake at −10 %). **Builder:** Codex. This document is the
rule; the build brief is `docs/roadmap/codex-brief-automated-learning-2026-11-16.md`.

Nothing here changes risk per trade, total slots, the daily-loss limit, any entry or exit gate, the
research quarantine or the paper-only boundary.

## 1. What can be learned, and how fast

Measured on the 1,105 live candidates with a 5-day outcome since 1 October (31 decision days, 35.6 a
day): the standard deviation of a 5-day return is 7 %, and 6 % within a day. Raw returns cluster
strongly by day (clustered variance 7.5 times the naive figure), so every comparison below is made
within the day.

| Question | Decisions by 15 Jan | Smallest effect it can detect (5 % one-sided, 80 % power) |
|---|---|---|
| Arm A beats a random scanner candidate | about 34 long; about 69 if shorts start by 1 Dec | about 2.6 points per 5-day decision long-only; about 1.9 with shorts |
| Arm B beats a random scanner candidate | about 9 | not testable; descriptive only |
| Scanner pool differs from the index | about 570 | about 1.2 points, assuming moderate day clustering after index adjustment (unmeasured: the adjusted field is empty since 3 September) |
| Evaluator opinion ranks candidates (all evaluated, not only proposals) | about 1,260 | rank correlation about 0.07 |

A realistic stock-selection edge is a fraction of a point per 5 days. At two decisions a day arm A
cannot confirm an edge of that size in months. **A null result on 15 January means "no large edge",
not "no edge".** The ranking test uses every evaluated candidate and is about two and a half times
more sensitive; it is the instrument most likely to say something by January.

## 2. Definitions (used by the governor, the scorecard and the registration)

- **Decision:** the first qualifying candidate per symbol per ET trading day per arm, from scans that
  were not degraded, shadow tier excluded, `policy_version = explore_1`, decision time on or after
  the 16 November 2026 open. A decision counts whether or not it became an order.
- **Pool:** every candidate in the persisted eligible set of that day's control draws
  (`explore_draw_json.eligible_ids`), de-duplicated the same way. Its daily mean is the baseline.
- **r:** the candidate's 5-trading-day forward return from the decision reference price.
  **b:** the benchmark's return over the same window. **s:** +1 long, −1 short.
- **Absolute result of a decision:** `s × (r − b)`.
- **Relative result of a decision:** `s × (r − b) − (p − b)`, where `p` is the same day's pool mean
  of `r`. For a long this is `r − p`.
- **Standard error:** clustered by decision day. With `G` days, `N` decisions, mean `m` and daily
  sums `S_g` over `n_g` decisions: `SE = sqrt( G/(G−1) × Σ (S_g − n_g m)² ) / N`. `t = m / SE`.
- A decision is **matured** when its 5-day window is complete on both the candidate and the
  benchmark. Unmatured, unavailable or quality-failed rows are excluded and counted in the output.

## 3. The governor

**Lever.** One number per arm: the maximum positions the arm may hold at once. A position tagged
`A+B` counts against both. Caps replace the current half-the-slots fairness rule while the governor
is on. No borrowing between arms: slots an arm does not use stay empty.

| Arm | Start | Floor | Ceiling | Step |
|---|---|---|---|---|
| A (evaluator, long and short) | 9 | 3 | 13 | 2 |
| B (classifier) | 3 | 1 | 5 | 1 |
| C (control) | 8 | 2 | 12 | 2 |

The caps never sum to more than 20. Risk per trade stays 0.25 % in every arm.

**Run.** Once a week, Saturday after 09:00 SGT, on all matured campaign decisions to date. New caps
apply from the next US session. Existing positions are never closed to meet a lower cap; the arm
simply cannot open more until it is under.

**Rule, per arm, each run:**

1. *Not enough data* — fewer than 15 matured decisions or fewer than 8 decision days (pool: fewer
   than 100 decisions or 8 days): **hold**.
2. *Arm A or B:*
   - **step down** if relative `t ≤ −1.5`, or absolute `t ≤ −1.5`;
   - **step up** if relative `t ≥ +1.5` and the absolute mean is above zero;
   - otherwise **move one slot back toward the start value** (no move if already there).
3. *Arm C:* the same, using the pool's absolute result only: down at `t ≤ −1.5`, up at `t ≥ +1.5`,
   otherwise one slot back toward start.
4. Step-downs are applied first. A step-up is applied only if the caps still sum to 20 or less; if
   two arms qualify and there is room for one, the higher `t` goes first.

**What this does.** An arm that is measurably losing shrinks to its floor and the freed slots stay
empty, so the account trades less when the evidence is bad. An arm that is measurably winning grows.
A move made on noise drifts back by itself. Floors keep every arm trading enough to keep being
measured through real fills.

**What to expect.** The pool reaches 100 matured decisions in about three weeks (first usable run
around 5 December). Arm A reaches 15 around mid-December, about a week earlier if shorts are live by
1 December. Arm B will not reach 15 before January. Most weeks the
governor will hold. That is the correct behaviour at this sample size.

**Honest limits.** Weekly looks at cumulative data are not a significance test: over eight weeks an
arm with no edge has, by my rough estimate (not simulated), about a one-in-four chance of one false
step. Steps are small, reversible and
self-correcting, which is why that is acceptable. Five-day windows on consecutive days overlap, so
the day-clustered error is somewhat understated. The governor is an operating control. The
confirmatory answer is section 4, read once.

**Safety properties (required).**
- Off by default (`ARM_GOVERNOR_ENABLED=false`); CK turns it on. Off means today's fairness rule.
- It writes only the three caps, within the table's bounds. It cannot touch any other setting.
- Every run stores its inputs, counts, means, errors, `t` values, the rule that fired and the caps
  before and after, and sends CK one alert line — including "no change".
- If the run fails, or the latest stored caps are missing, unreadable or more than 14 days old, the
  start values apply and CK is alerted. A failed run never blocks trading.
- Deterministic: the same ledger gives the same caps.

## 4. Registration — to be entered through the existing preregistration mechanism

**Enter before the US close on 20 November 2026**, before any campaign decision has matured. Window
for all four: decisions from the 16 November 2026 open to the 7 January 2027 close. Analysis not
before 15 January 2027. One look. No interim result changes the test.

| ID | Claim | Statistic | Test |
|---|---|---|---|
| `H-EXP1-A` (primary) | Evaluator proposals earn more than a same-day random scanner candidate | mean relative result of arm-A decisions (including `A+B`) | one-sided, α = 0.05 |
| `H-EXP1-POOL` | Scanner candidates differ from the index | mean absolute result of pool decisions | two-sided, α = 0.05 |
| `H-EXP1-IC-EVAL` | The evaluator's opinion ranks candidates | mean over days of the within-day Spearman correlation between the evaluator's ordinal opinion (reject < watch < propose; ties broken by its confidence) and `r`, over all evaluated live long candidates; days with fewer than 10 candidates excluded | one-sided, α = 0.05 |
| `H-EXP1-IC-LABEL` | The classifier's label ranks candidates | the same, with the classifier label (ties left tied) | one-sided, α = 0.05 |

- **Inference:** moving-block bootstrap on the daily series, block length 5 trading days, 10,000
  resamples, fixed seed recorded at registration.
- **Family:** Benjamini–Hochberg at 10 % across the four.
- **Floors:** `H-EXP1-A` is read only with at least 40 matured decisions on at least 20 days; the
  pool with at least 300 on 20 days; each ranking test with at least 20 days. Below a floor the
  result is "inconclusive — insufficient sample" and no arm is dropped on that test.
- **Arm B** has no confirmatory test in this window. Its numbers are reported as descriptive.
- **Stated power:** as in the table in section 1, assuming a within-day standard deviation of 6 %.
- **Barred:** decisions before 16 November 2026 (already examined in the 14 November checkpoint
  audit), any later-added statistic, any re-run with a different window. Old registrations stay
  quarantined.

## 5. Campaign loss brake (CK: −10 %)

- **Baseline:** account equity at the 13 November 2026 close, 95,198.89 as recorded in
  `equity_close_snapshots` (flat book; valuation marked provisional — confirm against the broker and
  record the confirmed figure once, immutably).
- **Trip level:** equity at or below 90 % of baseline (85,679.00 on the recorded figure).
- **At an entry check:** a single fresh reading at or below the level refuses that entry.
- **Latch:** two consecutive monitor passes at or below the level engage the existing kill switch
  (which blocks new or increased exposure and leaves protection and exits working), with the reason
  and both readings recorded, and page CK at critical severity.
- **Release:** only by CK, with the existing kill-switch command. It never clears itself.
- A reading that cannot be verified is not a trip; the existing failure handling applies.
