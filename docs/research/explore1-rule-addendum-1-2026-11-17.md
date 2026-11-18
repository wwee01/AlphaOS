# EXPLORE-1 rule addendum 1 — measurement, test conventions, H1 conventions

**Author:** Claude (strategy). **Date:** 17 November 2026 SGT. **Amends:**
`explore1-governor-and-registration-v1-2026-11-16.md` (v1) and Study H1 in
`docs/roadmap/codex-brief-automated-learning-2026-11-16.md`. **Answers:** Codex
`CLAUDE-AUTOMATED-LEARNING-CLARIFICATIONS-2026-11-16.md` (diagnosis against `ea036e1`).

Sections marked **[CK]** change what v1 measures or tests. **CK approved all five recommendations on
18 November 2026** ("let's go with your recommendation"); the rulings are listed below and the text
of each section states the approved option. Everything else fills a gap v1 left open and is frozen by
this document.

**CK rulings, 18 November 2026** (row 1 corrected the same day — see the note after the table):

| # | Decision | Ruling |
|---|---|---|
| 1 | Reference price | `close_ref_v1` exactly as defined in section 1: reference session D is the first session whose close is after the decision (for an intraday decision, the decision day itself); endpoints are the closes of the 5th / 10th session after D |
| 2 | Classifier ranking test | `H-EXP1-IC-LABEL` dropped from the tested family; classifier numbers reported as descriptive. The family is three tests |
| 3 | Evaluator ranking score | propose by confidence descending; all watch tied; reject by confidence ascending |
| 4 | When to look | count-triggered single look: first Saturday 01:00 UTC on or after 16 January 2027 with arm A at 40 matured decisions on 20 sessions; 27 February 2027 01:00 UTC at the latest |
| 5 | Registration mechanism | immutable records in the existing registry before the 20 November US close (Codex, on CK's instruction); versioned evaluator before 9 January 2027 |

*Correction, 18 November (raised by Codex):* row 1 first read "from the close of the first session
after the decision", which for an intraday decision would start the next day and contradicts
section 1. Section 1 governs, and it is what CK approved: the recommendation put to CK read "both
priced at the close of the decision day". A 16 November intraday decision has D = 16 November and a
5-session endpoint at the 23 November close. The four claims' wording,
the cohort start, every sample floor, the block length, 10,000 resamples, Benjamini–Hochberg at 10 %
and the 18-of-22 calibration gate are unchanged.

## 0. Corrections to v1 (my errors; Codex's corrections confirmed)

Checked read-only on 17 November, decisions 1 October – 13 November, 32 trading days:

- The 1,135 evaluated candidates are **583 long and 552 short**. H1 and the ranking tests are
  long-only, so their base is 583, not 1,135.
- The 5-day returns I used for the noise estimate included partial rows and direction-signed shorts.
  On the 513 complete long rows the within-day standard deviation is **5.1 %** (v1 said 6 %).
- v1 counted rows where its own rule counts one decision per symbol per day:

| | v1 assumed | Measured per day | Expected in the v1 window (36 sessions) |
|---|---|---|---|
| Arm A long | 0.94 | 0.72, on 12 of 32 days | about 26, on about 14 days |
| Arm A short | 1.3 | 1.09, on 16 of 32 days | about 29 if live by 1 December |
| Arm B | 0.25 | 0.25 | about 9 |
| Pool | 16 | 10.7 | about 385 |
| Long candidates for the ranking tests | 35 | median 10; 14 of 32 days have fewer than 10 | about 20 usable days |

Consequences, stated before any outcome exists:

- **`H-EXP1-A` will probably miss its 20-day floor on 15 January unless shorts are live early.**
  Long-only, arm A proposes on about three days in eight.
- **The evaluator ranking test is not the sensitive instrument v1 claimed.** Its detectable rank
  correlation is about 0.17, not 0.07.
- **`H-EXP1-IC-LABEL` cannot be tested.** The classifier labelled 343 of 351 long symbol-days
  "watch"; a constant label cannot rank anything.
- v1's detectable effects for arm A (about 1.9 points with shorts, 2.6 long-only) and the pool
  (about 1.2 points) stand, by the accident of a smaller sample and a smaller deviation offsetting.
  They remain planning assumptions, not maturity-qualified estimates.

## 1. Measurement convention **[CK — decision 1: approved 18 November]**

**Adopted (Codex's proposal):** convention `close_ref_v1`.

- **Reference session D** of a decision: the first regular session whose close is strictly after the
  decision timestamp. A decision stamped at or after a close, or on a non-session day, takes the next
  session.
- **Reference price:** the daily close of D. **Endpoint:** the daily close of the 5th (10th)
  scheduled session after D on the exchange calendar the system already uses; early-close sessions
  count as sessions.
- `r = P_end / P_ref − 1` for the candidate, `b` the same for SPY. Both unsigned; the direction sign
  `s` is applied once, in v1's formulas. Stored returns that are already direction-signed are never
  multiplied by `s` again. The pool mean `p` is the mean of unsigned `r` over pool decisions with the
  same D.
- Same provider, feed and adjustment basis for both legs and both dates; record prices, dates,
  basis, feed and fetch time.
- **Missing prices:** a bar missing at exactly D or the endpoint leaves the result pending; retried
  until 10 calendar days after the endpoint, then permanently `unavailable`. Never another date,
  never "complete" with a null. Benchmark availability is its own pending dependency.
- **Frozen once written:** the stored `r` and `b` are what every test reads; they are not recomputed.
- **Scope:** every campaign candidate from the 16 November 2026 open — arm decisions, pool, all
  evaluated live candidates, and labelled shadow candidates. Additive fields only. Historical rows
  and the legacy `market_adjusted_*` fields are left exactly as they are and are not used by the
  scorecard, governor or tests.

**What this changes.** v1 said "from the decision reference price". This measures from the first
close after the decision, so it leaves out the move between the decision and that close — up to six
and a half hours for a 09:35 decision, minutes for a 15:45 one. If the evaluator's edge is
same-day continuation, this test will not see it; real trades, entered minutes after the decision,
would.

**Why I still recommend it.**
- The two legs match exactly. The stored reference prices do not: the candidate's is often a planned
  entry, and no SPY price is stored for the same instant.
- Every decision on a day shares one window, so "a same-day random scanner candidate" is an exact
  comparison. With decision-time prices a 09:40 proposal would be compared with pool candidates
  priced at 11:30 and 15:45.
- It needs only daily bars, which are complete and auditable; nothing has to be captured in real
  time, so nothing is lost if the build lands after 16 November.
- The strategy holds for days, not hours; the two measures share most of their variation.

**The alternative** keeps v1's literal wording: candidate and SPY both priced from the same
one-minute bar at the decision. It needs new capture or minute-bar retrieval for every candidate,
an agreed tolerance for thin minutes, honest gaps where a bar is missing, and a pool matched by scan
window, which cuts each comparison's baseline to a handful of names. It cannot be built and audited
by 20 November.

**Companion, descriptive only (not registered, not blocking):** once the H1 minute-bar engine
exists, the scorecard also reports each arm's move from decision to the close of D against SPY over
the same minutes, so a same-day effect would be visible.

## 2. Test conventions (frozen)

**Daily series.** Sessions `1..G` are every scheduled session from the first to the last reference
session in the cohort. Each test has a pair `(S_d, n_d)` per session, zero when the session has
nothing:

| Test | `S_d` | `n_d` |
|---|---|---|
| `H-EXP1-A` | sum of relative results of arm-A decisions (including `A+B`) with reference session d | their count |
| `H-EXP1-POOL` | sum of `r − b` over pool decisions | their count |
| `H-EXP1-IC-EVAL` | that session's rank correlation if the session is valid, else 0 | 1 if valid, else 0 |

The statistic is `θ = Σ S_d / Σ n_d`: decision-weighted for A and the pool, equal-day for the ranking
test. An arm-A decision whose session has an empty matured pool is excluded and counted. Membership
is the arm tag at decision time; nothing is tagged afterwards.

**Bootstrap.** Circular moving blocks: `K = ceil(G/5)` block starts drawn uniformly with replacement
from the `G` sessions; each block is 5 consecutive sessions, wrapping from the last to the first;
concatenate and keep the first `G`. Recompute `θ*` from the resampled pairs. A resample with
`Σ n = 0` is invalid; fewer than 9,500 valid of 10,000 makes the test "inconclusive — bootstrap
failed".

**p-values.** One-sided: `(1 + #{θ* − θ ≥ θ}) / (valid + 1)`. Two-sided (pool):
`(1 + #{|θ* − θ| ≥ |θ|}) / (valid + 1)`. Not studentised.

**Random numbers.** `numpy.random.Generator(PCG64(seed))`, one draw of
`integers(0, G, size=(10000, K))` per test; seeds 20261116 (A), 20261117 (pool), 20261118 (ranking).
The numpy version is recorded with the result, and the evaluator ships with fixture vectors.

**Ranking test roster.** One row per symbol per reference session: the earliest evaluation that
day of a live, non-shadow candidate with direction long, from a non-degraded scan, with a passed,
non-mock evaluator opinion of direction long and a decision of reject, watch or propose. The original
opinion only; never a later one. A row without a finite confidence is excluded and counted.
A session is **valid** with at least 10 such rows having a matured `r`, and neither the scores nor
the returns all equal. Invalid sessions do not count toward the 20-day floor. Spearman with average
ranks for ties.

**Ranking score [CK — decision 3: approved 18 November].** v1 said ties are "broken by its confidence". The prompt defines
confidence only as "number 0..1", and in the ledger it is highest on rejections (median 0.88), so it
is confidence in the stated decision, not in the candidate. Ranking a confident rejection above an
unsure one would be backwards. Corrected score, highest first: propose by confidence descending;
then all watch, tied; then reject by confidence ascending.

**Floors** are v1's, in v1's units: matured decisions, and distinct reference sessions with at least
one matured decision.

**Family and verdict.** Benjamini–Hochberg at 10 % over the registered family, with an inconclusive
test entered as `p = 1`. A claim is **supported** only if its own `p ≤ 0.05` and its adjusted
`p ≤ 0.10`. The pool result also states its direction.

**One look.** The family is evaluated once, at instant `T`, on the stored results available then.
A run that fails before writing any result may be repeated; once any result is written the family is
closed and is never re-run. Below-floor tests close as inconclusive.

**Governor (v1 section 3) uses the same measurement.** With fewer than two sessions or a zero
standard error an arm holds. Equal `t` values are ordered A, B, C. A one-slot return toward the start
value is skipped if it would take the caps above 20.

## 3. What is registered, and when it is read **[CK — decisions 2 and 4: approved 18 November]**

**Decision 2 — `H-EXP1-IC-LABEL` is dropped from the tested family.** It is untestable as written.
The registered family is three tests: `H-EXP1-A`, `H-EXP1-POOL`, `H-EXP1-IC-EVAL`. The classifier's
numbers are reported as descriptive, like arm B. Benjamini–Hochberg is over these three.

**Decision 4 — when to look.**
- *Rejected (v1 as written):* cohort is decisions from the 16 November open to the 7 January close;
  `T` is 2027-01-16 01:00 UTC.
- *Adopted:* `T` is the first Saturday 01:00 UTC on or after 16 January 2027 on which arm A has
  at least 40 matured decisions on at least 20 sessions, and 27 February 2027 01:00 UTC at the
  latest. The cohort is every decision from the 16 November open whose 5-day endpoint has closed
  before `T`. The trigger depends only on counts, never on results, so it remains a single
  unconditioned look. It can move the final read by up to six weeks.

## 4. Registration mechanism **[CK — decision 5: approved 18 November]**

Two steps, because the existing evaluator cannot run these tests and a new one cannot be built and
audited by 20 November.

1. **Before the US close on Friday 20 November 2026 (05:00 SGT Saturday 21 November):** three immutable records in the existing
   registry, written with the existing `register_hypothesis` function. Each carries its `H-EXP1` ID,
   the claim, the family, the seed, `analysis_not_before` (2027-01-16T01:00:00Z, the earliest possible `T`), the
   look rule of section 3, and the contract: the commit SHA and
   SHA-256 of v1 and of this addendum. One committed script; run first on a ledger copy; run once on
   production; refuse if any `H-EXP1` record exists; read back and report IDs and hashes. This is a
   production ledger write and needs CK's instruction. No application code changes. Confirm, before
   writing, that no scheduled resolver can evaluate these records before `analysis_not_before`.
   The legacy columns `floor_effective_n` and `floor_span_days` cannot be null and do not express
   these floors. Store **1,000,000 in both**, documented in the parameters as unused legacy
   placeholders, so that a legacy evaluation can never report a floor as cleared; the real floors
   (section 2) go in the parameters. Not 0: a zero floor is one every legacy evaluation passes.
   Until the refusal guard of step 2 is deployed, nothing may call the legacy evaluator on these
   records — a single call would permanently mark them evaluated and spend the one look. No resolver
   links are created.
2. **Before 9 January 2027:** a narrow, versioned evaluator for exactly these contracts, with a guard
   that makes the legacy evaluator refuse them. Old registrations and their quarantine untouched.
   Delivered on its own SHA, audited by Claude, proven on constructed fixtures only.

## 5. Study H1 conventions (frozen)

- **Sample:** the 583 evaluated live long candidates from 1 October to 13 November 2026. Shorts are
  out of scope. Decisions from 16 November are excluded so the study never reads the registered
  window.
- **Bars:** one-minute, regular session, the production feed. A minute with no bar triggers
  nothing. A needed session with no bars at all makes that trade `data-unavailable`: kept in every
  denominator and reported.
- **Calibration replays exits only.** Each of the 22 real trades starts from its recorded fill
  (time, price, quantity) and the stop and target actually placed. Thirteen were filled sooner than
  the study's eight-minute entry lag, so the entry model is not what calibration tests. Compare
  modelled gross R with recorded gross R; a match needs the same exit reason and R within 0.1.
  The three manual exits cannot match and count as misses, so at most 19 can match and the gate of
  18 allows one further miss. The gate is not changed.
- **Stop:** triggers when a bar's low is at or below the stop. Fill at the stop, or at the bar's open
  if the bar opens at or below it.
- **Target:** triggers when a bar's high is at or above the target. Fill at the target, or at the
  bar's open if it opens at or above it.
- **Entry (hypothetical trades):** fill at the limit, or at the bar's open if it opens below it.
- **Ambiguous order within one minute** — stop and target both touched, or the entry bar also
  touches an exit level: mark the trade `ambiguous`; never guess and never drop. In calibration it
  counts as a miss. In plan comparisons report both bounds (stop first, target first) and use the
  stop-first bound as the headline.
- **Time expiry (P0):** the session on which the production rule in `position_manager` first fires
  for that position; fill at the open of the first one-minute bar at or after 09:35 ET that session.
  The trade with no saved expiry uses the same rule from its recorded holding days; if that cannot be
  derived it is a miss.
- **P1:** stop triggers are ignored from 09:30:00 to 09:59:59 ET every session; at the 10:00 bar the
  stop rule applies again, including the open-fill case. The target stays active.
- **P2:** stop 1.5 times as far; R is measured against the P0 money at risk.
- **P3 / P4:** exit at the close of the last regular one-minute bar of the 5th / 10th session after
  the entry session.
- Costs: results reported gross and with the existing modelled costs, separately.
