# EXPLORE-1 decision-time mapping for `close_ref_v1` — Claude, 18 November 2026

**Status:** specification for Codex to implement, at CK's request. Specification only: no code,
production, registration or historical row is changed. **Governs:** addendum 1 §1 at contract
`f7452f6` (D = the first regular session whose close is strictly after the decision timestamp;
endpoints are the 5th/10th scheduled sessions after D). This document only defines *which stored
instant* is "the decision timestamp" for each population. Two interpretation questions for CK are in
§7; nothing here changes the registered claims, floors, seeds or look rule.

## 0. What is actually stored (production, read-only, 18 Nov)

| Fact | Evidence |
|---|---|
| **Arm tags and control draws have no timestamp.** `candidates.arm` / `policy_decision` and `scan_batches.explore_draw_json` are written without time; the draw JSON holds seed, window, eligible ids and chosen id only | `orchestrator._route_campaign`, `explore_policy.freeze_draw` |
| **`candidate_outcomes.decision_at_utc` mixes three meanings** and is not usable: watch rows carry candidate creation (before the opinion existed), reject rows carry rejection processing, propose rows carry the proposal insert. Its `created_at_utc` is a later sweep | `cand_654f1e32fc4f` (XLK watch): `decision_at_utc` 14:38:46Z, evaluator opinion 14:42:35Z; `cand_804c1a461943` (arm C): routed ≈14:44Z, outcome row created 15:04:33Z |
| **Exact and usable:** `openai_evaluations.created_at_utc` (stamped by `journal_store.insert` immediately after the response returns) and `candidate_labels.label_frozen_at_utc` (stamped at freeze, before the insert) | code + rows |
| **The 15:45 scan crosses the close.** Since 2 Nov (EST, close = 21:00Z), 3 of 12 scans in the 15:45 window finished after 16:00 ET (4 Nov 16:06:07, 5 Nov 16:01:38, 9 Nov 16:01:18); 16 Nov finished 15:58:26. Longest scan since 1 Oct: 21 min | `scan_batches` |
| **The system calendar has no early closes.** `market_calendar` answers "trading day yes/no"; `market_session` assumes 16:00 every day | module docstrings |
| Scans never overlapped: 0 of 101 scans since 1 Oct started before the previous one completed | `scan_batches` |
| No `policy_version='explore_1'` row and no draw exists before the 16 Nov open; first campaign scan started 14:38:33Z | `candidates`, `scan_batches` |
| No shadow candidate has been labelled since 16 Nov (shadow latch still engaged) | `candidate_labels` |

## 1. Principle

The decision timestamp of a record is **the instant its membership in the population became fixed —
the latest of its inputs, never an earlier one.** D is non-decreasing in time, so this guarantees no
reference close falls before any information the decision used. **Event time** is a stamp the
producer takes immediately before the write that persists it, carried in that same write. **Persistence
time** (commit, sweep, outcome-row creation), and later events (proposal insert, order, fill, retry)
are never used. Every timestamp must parse as timezone-aware; anything else is unavailable.

## 2. Mapping

| Population | Decision event | Authoritative stored instant |
|---|---|---|
| Arm decisions **A, B, A+B, C** | The scan's **routing event**: the control draw is fixed after every route for the scan has been computed. Arm tags are a deterministic function of those routes and the draw, so they are fixed at this instant; the later per-candidate `UPDATE`s only persist them | **New** `scan_batches.explore_routed_at_utc` (additive). Arm from `candidates.arm`, joined by `scan_batch_id` |
| **Control pool**, drawn and undrawn | The same routing event of the **owning** draw (`explore_draw_json.scan_batch_id = scan_batch_id`, `window_key` not null). Members = its `eligible_ids` | `scan_batches.explore_routed_at_utc` of the owning scan |
| **Evaluator results** (ranking roster, all evaluated live candidates) | The evaluator opinion | `openai_evaluations.created_at_utc` of the candidate's earliest row (`created_at_utc`, then `id`) |
| **Labelled shadow candidates** | The label freeze | `candidate_labels.label_frozen_at_utc` on the original row (`relabel_of IS NULL`, `shadow_tier = 1`). `candidates.label_frozen_at_utc` is a copy; if it differs, unavailable |

**Why arm decisions and the pool share one instant per scan:** the relative result compares an arm-A
decision with the pool on the same D. Per-candidate write times can straddle 16:00 inside one scan
(routing, then proposal creation for each arm candidate); one routing instant keeps a scan's arms
and pool on the same D.

**Stamping the new column.** `explore_routed_at_utc` is taken with `timeutils.now_utc()` after the
eligible set, seed and choice are fixed, immediately before the `UPDATE`, and is written in that
same `UPDATE`. It is written by every scan that reaches `freeze_draw`: the owner (with the draw), a
non-owner that copies an existing window draw (in the copy's `UPDATE`) and an off-window scan. It is
written once and never rewritten: a re-entry for the same scan keeps the first value. It is aware UTC
ISO-8601 with microseconds.

## 3. Identity, ordering, retries and repeats

- **Decision key:** `(population, arm, symbol, D)`. The first record by decision instant counts,
  with ties broken by `scan_batches.id`, then the candidate row `id`. Later records with the same key
  are `duplicate_in_session`: excluded and counted. (Key unit: see §7, item 1.)
- **A+B** is an arm-A decision (and a descriptive arm-B decision). A later pure-A record for the
  same symbol and D is a duplicate. One symbol may be an arm decision and also a pool member; they
  are different populations.
- **Separate scan windows** are separate routing events. Only the first per key counts.
- **Non-owner and off-window scans:** their arm A/B decisions count, at their own routing instant.
  Their `explore_draw_json` is a copy or has no draw, so they contribute no pool members (§7, item 2).
- **Retries never move a decision:** deferred-entry retries in later scans, resubmissions,
  expiries, supersessions and fills leave the original routing instant in place. An API retry inside
  one evaluator call persists one row, and that row's stamp is the event.
- **Repeated opinions:** only the earliest evaluator row per candidate is the original. For the
  ranking roster, first filter to qualifying rows (addendum §2), then take the earliest per symbol
  per D. Relabels (`relabel_of` not null) are never used.
- **Identity checks (fail closed as `identity_mismatch`):** arm C must equal the owning draw's
  `chosen_id`. A pool member must be a live, non-shadow candidate of the owning scan. When an
  exact routing instant and a §4 interval both exist, the exact instant must lie inside the interval.
- **Stored with each enrolled outcome** (new columns; never the legacy `decision_at_utc`):
  - basis: `exact_routing_v1`, `exact_evaluation_v1`, `exact_label_v1` or `bounded_routing_v1`;
  - the exact instant, or the lower and upper bounds;
  - the source table and primary key (`scan_batch_id`, `eval_id` or `label_id`), `candidate_id`
    and `symbol`;
  - D and both endpoint dates;
  - `calendar_version` (§5);
  - any unavailable reason.

## 4. Records without an exact routing instant

This covers every scan before the column ships, including all six campaign scans so far.

**Evidence permitted (same `scan_batch_id` only):**
- **L**, the lower bound: the latest `openai_evaluations.created_at_utc` over the scan's live,
  non-shadow candidates. Routing reads every one of these opinions and runs after the per-candidate
  loop. Codex must cite the SHA that ran each scan and confirm that ordering at it.
- **U**, the upper bound: the earlier of `scan_batches.completed_at_utc` and the earliest
  `trade_proposals.created_at_utc` with `policy_version='explore_1'` for the scan. Both are written
  after the draw.
- Nothing else counts as evidence: not candidate creation, `candidate_outcomes`, system events, logs,
  file times or processing time.

**Rules:**
- **Unique D is proven** iff L ≤ U and D(L) = D(U). Because D is non-decreasing, every instant in
  [L, U] then has the same D. Otherwise the record is `session_unproven`.
- **Ordering is proven** between records whose intervals are disjoint. Records from the same scan
  share one routing event. If records competing for one key have overlapping intervals, the whole
  key is `order_unproven`.
- **Campaign boundary** (the 16 Nov open, 2026-11-16T14:30:00Z):
  - an exact instant is in scope iff it is at or after the open;
  - an interval is in scope iff L is at or after the open;
  - an interval with U before the open is out of scope (pre-campaign, not counted as a loss);
  - an interval that straddles the open is `boundary_unproven`.
- **Unavailable is final.** It is recorded with its reason and counted in every output. Never use a
  midpoint, never use processing time, never move a record to a neighbouring session, never
  re-enroll it later.

**Applied to production today. All six are provable; none is unavailable.**

| Scan | Window | L (ET) | U (ET) | D | Pool (owner) | Arms |
|---|---|---|---|---|---|---|
| `scan_f2835c8b1a31` | 16 Nov 09:35 | 09:44:20.247 | 09:44:21.895 | 16 Nov | 1 | A, C |
| `scan_9c70b2f60385` | 16 Nov 11:30 | 11:40:31.637 | 11:40:33.905 | 16 Nov | 2 | A, C |
| `scan_5d35d2c9cc0e` | 16 Nov 15:45 | 15:58:22.152 | 15:58:24.450 | 16 Nov | 1 | C |
| `scan_6b9bcc405cb8` | 17 Nov 09:35 | 09:41:13.789 | 09:41:13.918 | 17 Nov | 0 | — |
| `scan_083cfd2eb2f5` | 17 Nov 11:30 | 11:37:57.272 | 11:37:57.662 | 17 Nov | 0 | — |
| `scan_3342d3946466` | 17 Nov 15:45 | 15:50:03.929 | 15:50:04.156 | 17 Nov | 0 | — |

## 5. Session close times

- **Sessions and endpoint counting:** `market_calendar.is_trading_day`, as the contract requires.
  Half-days count as sessions.
- **Close time:** 16:00 ET, except the pinned NYSE early closes **2026-11-27 13:00 ET** and
  **2026-12-24 13:00 ET**.
- **Coverage:** the table covers through 2027-06-30. A decision, D or endpoint after that date is
  refused.
- **Time zone:** convert with `zoneinfo` `America/New_York` (EST until 14 Mar 2027).
- **Version:** `calendar_version` is the SHA-256 of the pinned table.

This implements "close" in the contract; it does not change it. Without the table, a decision at
15:58 on 27 Nov would be priced from a close that came before it.

## 6. Acceptance examples (dates 2026 unless shown)

| # | Case | Instant used | D | D+5 | D+10 |
|---|---|---|---|---|---|
| 1 | Candidate scanned 15:59 ET on 18 Nov; evaluator opinion **16:01**; routed 16:02 | opinion 16:01 (ranking row); routing 16:02 (arm decision and pool) | 19 Nov for both | 27 Nov | 4 Dec |
| 1b | Same scan, but the opinion lands at 15:59:30 | opinion 15:59:30 (ranking row); routing 16:02 (arm decision and pool) | ranking row **18 Nov**; arm decision and pool **19 Nov** | 25 Nov / 27 Nov | 3 Dec / 4 Dec |
| 2 | Routing stamped exactly **16:00:00.000000** on 18 Nov | routing | 19 Nov (not strictly after) | 27 Nov | 4 Dec |
| 2b | Routing stamped 15:59:59.999999 on 18 Nov | routing | 18 Nov | 25 Nov | 3 Dec |
| 3 | **Late persistence:** routing stamped 15:59:58, the `UPDATE` waits on a lock and commits 16:00:04 | stamp 15:59:58 | 18 Nov | 25 Nov | 3 Dec |
| 3b | Process crashes after the stamp, before commit; the next run re-routes at 16:03 | only the persisted stamp, 16:03 | 19 Nov | 27 Nov | 4 Dec |
| 3c | Legacy outcome row created 15:04Z for a decision routed ≈14:44Z | never used | — | — | — |
| 4 | **Early close** 27 Nov, routed 12:59:59 | routing | 27 Nov | 4 Dec | 11 Dec |
| 4b | 27 Nov, routed exactly 13:00:00 | routing | 30 Nov | 7 Dec | 14 Dec |
| 4c | 27 Nov 15:45 scan (the scheduler does not know the half-day), routed 15:58 | routing | 30 Nov | 7 Dec | 14 Dec |
| 4d | 24 Dec, routed 11:40 / routed 15:50 | routing | 24 Dec / 28 Dec | 4 Jan 2027 / 5 Jan 2027 | 11 Jan 2027 / 12 Jan 2027 |
| 5 | **Non-session day:** Thanksgiving, 26 Nov 10:00 | any | 27 Nov | 4 Dec | 11 Dec |
| 5b | Saturday 21 Nov 11:00 | any | 23 Nov | 1 Dec | 8 Dec |
| 6 | Legacy interval, 16 Nov 15:45 scan: L 15:58:22.152, U 15:58:24.450 | bounded | 16 Nov (proven) | 23 Nov | 1 Dec |
| 6b | Legacy interval like the 4 Nov scan: L 15:59:40, U 16:06:07 | bounded | 18 Nov vs 19 Nov → **unavailable** `session_unproven` | — | — |
| 7 | Exact instant 16 Nov 09:29:59 ET | — | would be 16 Nov, but before the open → out of scope | — | — |
| 7b | Interval with L 09:29:50 and U 09:30:10 on 16 Nov | bounded | **unavailable** `boundary_unproven` | — | — |

## 7. Two interpretations — **CK approved both recommendations, 18 November** ("go with both reco")

Neither changes claims, floors, seeds or look. Ruled: (1) the decision key's unit is the reference
session D; (2) the pool comes only from owning, in-window draws.

1. **Dedupe unit.** v1 §2 says "first qualifying candidate per symbol per ET trading day". I
   recommend the reference session D, which the addendum already uses for the ranking roster. The
   two units give the same result except for decisions at or after a close. Under "ET trading day",
   a Monday 16:02 decision and a Tuesday 09:40 decision on the same symbol both get D = Tuesday, so
   one window would be counted twice. A Saturday decision also has no ET trading day. **Approved.**
2. **Pool source.** The pool is the `eligible_ids` of *owning, in-window* draws only. Off-window
   scans make no draw. A non-owner scan's `explore_draw_json` is a copy of the owner's draw, so its
   own eligible candidates are in no draw. This is the literal reading of "persisted eligible set of
   that day's control draws". **Approved.**

## 8. Operational finding, outside this mapping, urgent

**The classifier has failed on every candidate since 2026-11-16 20:57:55Z.**
- All 25 labels since then are `validation_status='live_exception'`, raw `{"fail_safe":
  "live_exception"}`.
- The evaluator uses the same model and still passes: 23 of 23 on 17 Nov.
- `explore_policy.route` needs a valid classifier label for arm B and for control-pool eligibility.
  So every 17 Nov draw had an empty pool, and arm B, arm C and the pool produced nothing.
- While this lasts, `H-EXP1-POOL` accrues nothing. `H-EXP1-A` loses every session whose matured pool
  is empty.
- The failure signature matches the 7 August outage.

Codex should diagnose, read-only first.

*STATUS CORRECTION to §8, 19 November (raised by Codex's diagnosis, checked by Claude):* the
evaluator was **not** passing on 17 November. Its exception fallback stores `validation_status='passed'`
by default, so the 23 rows I counted as passes are safety rejections ("OpenAI call failed; rejected for
safety.", `OPENAI_REJECT`, no tokens). Cause: API credits exhausted (HTTP 429 `insufficient_quota`)
since 16 Nov 20:57:52Z, for both the classifier and the evaluator. The draws were empty because
routing refuses `OPENAI_REJECT` as well as the failed labels. See
`delivery-history/claude-audit-of-codex-decision-mapping-a641b3c-2026-11-19.md` §4.
