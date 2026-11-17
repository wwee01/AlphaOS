# Codex brief — automated learning, 16 November 2026 SGT

> **STATUS CORRECTION, 17 November 2026:** read with `docs/research/explore1-rule-addendum-1-2026-11-17.md`,
> which corrects this document's sample counts and fixes the measurement and test conventions. Where
> they differ, the addendum governs.

**Drafted by Claude on CK's instruction. CK's rulings of 16 November:** the arm governor is approved
(slot caps move automatically each week within bounds, no approval step); the campaign loss brake is
−10 %; **all work runs through Codex — Claude writes instructions, strategy and audits and does no
building, studies, merging or deploying.** Trading and learning are to be automated.

This brief extends `codex-brief-explore1-followups-2026-11-15.md`; items there that are not yet done
are folded into the order below. The governing rule text is
`docs/research/explore1-governor-and-registration-v1-2026-11-16.md` — build to it exactly, and stop
and report if it cannot be built as written.

## State

- `main` = `d4bf485`. F1 is merged and live (`d78ad87`, your `13b5645`, audited with no defect).
  Rebase onto `main`.
- EXPLORE-1 is active; first session 16 November.

## What changes in how we work

- **You now merge and deploy.** Order: Claude audits the exact SHA → CK gives the merge instruction
  → you merge and deploy that SHA → you write a one-page deployment record → Claude verifies it
  read-only.
- **Deployment sequence** (learned on 15 November, record
  `docs/research/delivery-history/claude-deployment-explore1-2026-11-15.md`): wait for any running
  job to finish; back up the ledger and `.env` outside the repo; stop scheduler, monitor and
  heartbeat; merge; apply any setting change; **if the schema changed, open the ledger once by hand
  and confirm the migration before starting anything**; start services one at a time; restart
  console and dashboard; verify exits, readiness and the ledger against the backup.
- Every handoff: one commit range, the full suite with totals, one page. Unchanged.

## Order of work

### 1. Outcome measurement is broken — fix first (report before changing)

Found 16 November, read-only:
- `candidate_outcomes.market_adjusted_return_5d_pct` has not been written since **3 September 2026**
  (959 rows all-time, none after; 0 of 1,135 live candidates since 1 October). Raw
  `forward_5d_return_pct` is present for 1,105 of them, `benchmark_bars` is current to 13 November
  and `benchmark_spine` completes daily. The scorecard, the governor and the registration all need
  this number.
- Shadow-tier candidates since 1 October: 16,445 rows, **none** with a forward return. If the latch
  is released, shadow labels would have no outcomes to be measured against.
- The fields named `_pct` hold fractions (5-day standard deviation 0.07). Confirm the unit and state
  it wherever a number is printed.

Do: find the cause of each, report it, then make campaign decisions (16 November onward) carry a
correct benchmark-adjusted 5-day and 10-day result, and make labelled shadow candidates carry forward
returns. **Do not rewrite historical rows.** If old rows are needed for a report, compute the
adjustment at read time from stored returns and benchmark bars. Must be working before the first
campaign decision matures on 23 November.

### 2. Register the primary questions — before the US close on 20 November

Enter `H-EXP1-A`, `H-EXP1-POOL`, `H-EXP1-IC-EVAL`, `H-EXP1-IC-LABEL` exactly as written in section 4
of the rule document, through the existing preregistration mechanism, with the bootstrap seed.
Return the registration identifiers and hashes. If the mechanism cannot express a statistic, say so
at once — do not approximate it. Old registrations stay quarantined.

### 3. Small, already ruled

- Control pool: exclude symbols held or pending before the draw (15 November brief, section 3).
- **Make the timing-dependent tests deterministic**: `tests/test_last30days_reddit_deadline.py` and
  the no-items case in `tests/test_last30days_partial_flow.py`. They failed in both suite runs of
  `13b5645` (different cases each time) and fail intermittently on `main`. Replace wall-clock waits
  with an injected clock or executor. Accept: 20 consecutive isolated runs pass and the full suite
  is green. If you find a real deadline bug rather than a test fault, report before changing
  production behaviour.

### 4. Scorecard as a scheduled job (first output due 28 November)

A job in the research role, weekly, that writes the one-page scorecard and the same numbers in
machine-readable form. No hand assembly. Use the definitions in section 2 of the rule document. Per
arm (A long and A short separately once shorts exist) and for the pool:
- decisions and matured decisions; mean absolute and relative 5-day and 10-day result with
  day-clustered error and `t`;
- the two ranking correlations with their daily count;
- trades opened and closed, mean R, exit mix, stops within 30 minutes of an open;
- proposals deferred or blocked by reason; same-pass bookings;
- scan start lag after window open, scan duration, entries submitted after 15:55 ET or refused for
  a closed session.
Budget: about 250 application lines.

### 5. Campaign loss brake (−10 %)

Section 5 of the rule document. Reuse the kill switch; add no new latch. Prove offline: single
reading refuses one entry without latching; two consecutive monitor readings latch and page;
unverifiable reading does not trip; exits and protection keep working while latched; it never
releases itself. Budget: about 60 lines. Baseline figure confirmed against the broker and reported.

### 6. Arm governor

Section 3 of the rule document. Merges switched off (`ARM_GOVERNOR_ENABLED=false`); CK turns it on.
Prove offline: each branch of the rule on constructed data; bounds and the sum-to-20 limit; stale or
missing caps fall back to start values and alert; a failed run does not block trading; existing
positions are never closed by a lower cap; same ledger gives same caps; with the flag off the entry
path is byte-for-byte today's behaviour. Budget: about 200 lines. It depends on items 1 and 4.

### 7. Paper shorts in arm A

Unchanged from the 15 November brief, section 1. Merges switched off.

### 8. Reliability

- **Scan windows.** Since 1 October the hourly `outcomes_update` job (median 15 minutes) overlapped a
  scan window 47 times; one window had no scan (2 October, 15:45) and one scan started 13.9 minutes
  late (9 October). Rule: on a trading day the job must not start within 20 minutes before a scan
  window opens and must not run during one. Accept: a replay of the recorded job history shows no
  overlap under the rule.
- **Broker reads.** The exposure read fetches one order per open position; a three-entry pass on an
  empty book made 38 GETs. Reduce reads so they do not grow one-for-one with open positions,
  **without weakening what is verified** — identical verdicts on a recorded set of reads. If that
  cannot be done safely, report and leave it.
- Remaining small items from the 15 November brief (rejection reason, `.env.example`, migration
  safe under simultaneous starts) and audit residual R1 (retry deferred entries from the 5-minute
  tick).

### 9. Shadow latch

Run a fresh `shadow_recovery` preview each trading day and report the gate readings. CK has already
authorised the apply once a preview passes every gate. Do not bypass a gate.

## Study H1 — exits and overnight gaps (read-only; can run in parallel)

**Why.** Of the ten most recent closed trades with timing data, six ended on the time exit between
−0.13R and +0.69R, one hit its target, and all three stops fired within three minutes of an open.
Ten trades prove nothing; this study asks the question on a usable sample. It changes no production
behaviour. Its result goes to Claude, who will propose any strategy change for CK's ruling.

**Sample.** Every evaluated live long candidate since 1 October 2026 (1,135; state the exact count
and exclusions) as a hypothetical entry, with the levels EXPLORE-1's router would have given it
(`explore_policy.route`), using only information available at decision time.

**Calibration first.** Replay the 22 real trades with the same engine. Report how many reproduce
their real exit reason and R within 0.1R. If fewer than 18 do, stop and report — the engine is not
yet trustworthy.

**Entry model.** Limit at the planned entry; filled only if a later bar trades through it within the
proposal's life; entry bar starts no earlier than 8 minutes after the decision (the measured dispatch
lag). Existing cost settings applied. State the bar feed and every gap in it.

**Exit plans — fixed now; add none.**

| Plan | Rule |
|---|---|
| P0 | as routed: stop, target, maximum holding days |
| P1 | P0, but the stop cannot fire in the first 30 minutes after an open |
| P2 | P0 with the stop 1.5 times as far, size reduced so the money at risk is equal |
| P3 | stop as P0, no target, exit at the close of day 5 |
| P4 | stop as P0, no target, exit at the close of day 10 |
| P5 | P0, split by entry window (09:35, 11:30, 15:45) — a breakdown, not a new rule |

**Report, per plan:** trades filled; mean R and mean benchmark-adjusted return; exit mix; share of
stops within 30 minutes of an open; share of stops filled more than 0.25R beyond the stop; the
**paired** difference from P0 on the same entries with a day-block bootstrap interval; October and
November separately. Benjamini–Hochberg at 10 % across the plan comparisons.

**Also report:** scanner score in fifths against the 5-day benchmark-adjusted result with the daily
rank correlation; the distribution of the first overnight move after entry, candidates against the
benchmark.

**State plainly:** 31 decision days is the real sample size for anything that varies by day;
hypothetical fills are optimistic; the candidates are only names the scanner chose.

**Deliver:** one page, the script, a per-entry CSV, one command that reproduces every number. No
ledger writes.

## Boundaries that do not move

Paper only. No real money. No change to the research quarantine. No historical rows rewritten. No
backfilled labels. Shadow latch released only by a preview that passes every gate. External heartbeat
stays disabled until CK supplies an endpoint. Paper observer stays frozen and running. If a
requirement here conflicts with a safety control, stop and report.
