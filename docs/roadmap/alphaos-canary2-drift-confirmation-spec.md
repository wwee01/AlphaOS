# ALPHAOS SPEC — CANARY-2: Drift-Page Confirmation + Stable/Boundary Split — drafted 2026-08-03

Same laws as every spec in this directory: spec → build → independent review →
**merge only on explicit human instruction**. Additive migrations only
(`SCHEMA_VERSION` stays 3). §H.1 test discipline. Measurement/alerting-layer
only: zero decision surface on the LIVE trading path (CANARY is never read by
any gate/eval/labeller/risk/execution path — unchanged law). **Correction
(post-build audit, 2026-08-03):** this is narrower than "never read by any
code" — `shadow_label.check_auto_suspend` (EXP-1, itself a shadow/
measurement mechanism, not a live decision path) DOES read
`canary_runs.drift_tier` to gate shadow-labelling suspension; see Out of
scope below for the follow-up this surfaces.

## Motivating evidence (9-run characterization, 2026-08-03, operator-instructed)

Label-mismatch counts vs the pinned baseline (`canaryrun_de5814877cc1`)
across every non-baseline run: **1, 4, 1, 4, 1, 1, 1, 2, 1** — normally
1–2/20 (5–10%), occasionally 4/20 (20%). The two 20% readings are exactly
the runs that fired (or would have fired) TIER_2 at the 0.2 threshold.
Four same-day runs (2026-08-03, unchanged model, unchanged corpus, unchanged
baseline) gave 1, 1, 2, 1 — so a single run's count is a noisy draw, and
the threshold sits at the edge of the labeller's run-to-run
nondeterminism.

The noise is structurally concentrated: 13 of 20 packets never flipped
once across all 9 runs; packets whose BASELINE label is
`Other/Unclassified` are 75% unstable (6 of 8) vs 9% for `Momentum`
(1 of 11), and 10 of 16 total flips are the same
`Other/Unclassified → Mean Reversion` wobble. These are
decision-boundary packets: ordinary sampling nondeterminism tips them
either way. A one-run mismatch count therefore mostly reports how the coin
landed on the boundary packets, not whether the model changed — while the
count jumps 1→4 in a single step because correlated boundary flips move
together (the same correlated-observations error the PORT-1 effective-N
law names elsewhere).

Operator ruling (CK, 2026-08-03, §9): adopt confirmation-before-paging +
a stable/boundary split in the report. Explicitly REJECTED: excluding
boundary packets from the metric (they are the corpus's most sensitive
sensors — a subtle real drift shows up first at decision boundaries;
excluding them deletes the early-warning zone), re-pinning the baseline
(the instability is in the model's sampling, not the baseline's labels —
a fresh baseline wobbles identically and breaks 9 runs of anchored
history), and leaving it unchanged (2 false-ish pages in 4 weekly runs is
a desensitization rate; the operator being trained to shrug is the
failure mode).

## Goal

A canary page should mean "this shift is REAL — it survived a same-day
re-measurement," except where the trigger is deterministic (an identity
change), which needs no confirmation. Real drift is persistent; boundary
wobble is transient; one confirmation re-run is precisely the
discriminator between them.

## The fail direction (the one law that shapes every branch below)

**Any failure in the confirmation machinery degrades to the OLD behavior —
page on first trip — never to silence.** TRIP-1's fail-open protects the
scan (the scan must continue); CANARY-2's fail direction protects the PAGE
(the operator must be told). If the confirmation run cannot execute
(cost-cap preflight refusal, exception, corpus tamper — anything), the
original trip pages immediately, marked "UNCONFIRMED — confirmation run
could not execute: {reason}". A confirmation mechanism that can eat a page
is worse than no confirmation mechanism.

## Non-goals (frozen)

- **No threshold changes.** `canary_tier2_label_diff_pct` stays 0.2;
  `canary_tier3_confidence_shift_band` stays 0.15. The confirmation is the
  fix; the split is context. Per-set thresholds (e.g. a lower bar on
  stable-packet flips) are a FUTURE, evidence-gated change — not this
  ticket.
- No corpus changes, no packet exclusions, no baseline re-pin.
- TIER_3 behavior untouched (it does not page today; it gains the split in
  drift_detail like everything else, nothing more).
- No change to the weekly cadence, cost accounting model, or the
  `CorpusTamperedError` loud-failure contract.
- No new tables; one additive column (Design 4). `SCHEMA_VERSION` stays 3.
- Zero decision surface, unchanged.

## Design

1. **Trip classification.** Split `_compute_drift`'s Tier-1 short-circuit
   into two named trigger classes, carried in `drift_detail`:
   - `identity` — `response_models_json` / `system_fingerprints_json`
     changed. Deterministic (no sampling involved).
   - `failsafe` — `failsafe_rate_change` only, identity clean. A fail-safe
     appearance CAN be a sampling phenomenon (proven 2026-08-02: COST's
     verbosity tail crossed the token ceiling once in nine runs).
   TIER_2 (label drift) is inherently sampled. Resulting policy:

   | Trigger | Page timing |
   |---|---|
   | TIER_1 identity | **Immediately** — no confirmation run |
   | TIER_1 failsafe-only | After confirmation (or unconfirmed-page on failure) |
   | TIER_2 label drift | After confirmation (or unconfirmed-page on failure) |
   | TIER_3 | Never paged (unchanged) |

2. **Confirmation mechanic.** When a run trips a confirmable class:
   - Journal a `system_events` row (severity `warning`, category `canary`,
     "drift trip pending confirmation: {tier}/{class}, run {id}") — the
     audit record exists even if everything after this fails. **Correction
     (post-build audit, 2026-08-03, two independent Opus reviews, both
     CONVERGENT):** this journal write, and the one inside the send path,
     must be BEST-EFFORT (try/except, mirroring `JobRunner
     ._log_failure_best_effort`'s own established pattern) — an unwrapped
     write let a momentarily-locked DB escape the confirmation flow
     entirely and turn the drift page itself into a content-free generic
     "AlphaOS job failed: canary_run" alert. The audit record is best-effort
     precisely so it can never gate the page it exists to protect.
   - Immediately execute ONE more `run_canary` replay in the same process
     (same corpus, ~20 calls; subject to the normal cost preflight).
   - ~~Confirmation run ALSO trips (same tier class or worse) → **page
     once**~~ **Correction (post-build audit, 2026-08-03, two independent
     Opus reviews, both CONVERGENT on this exact hole):** "same tier class
     or worse" was read literally as a rank-only rule
     (`rank(confirm) <= rank(trigger)`) and treated TIER_1/failsafe and
     TIER_2/label-drift as one ordered ladder. A TIER_1/failsafe trigger
     confirmed by a TIER_2 re-run — two consecutive genuinely PAGEABLE
     trips — fell through to "not confirmed", was journaled as "transient
     wobble" (factually false: the re-run DID trip), and produced ZERO
     pages. One reviewer's correlation argument: a real model swap that
     pushes verbosity past the token ceiling (failsafe) is the same swap
     that moves labels (TIER_2) — this hole preferentially opened in
     exactly the scenario CANARY exists to catch. **Adjudicated semantics:**
     confirmed = the confirmation run trips ANY pageable tier (TIER_1 or
     TIER_2), regardless of which specific tier/class the trigger was →
     **page once**, titled with the WORSE of the two tiers, stating BOTH
     tiers/classes explicitly (e.g. "trigger TIER_1/failsafe, confirmed by
     TIER_2 label drift"), with both run ids, the stable/boundary split
     (Design 3, from whichever side is TIER_2) in the alert body, and the
     line "confirmed by same-day re-run". When the confirmation's own
     signal shape differs from the trigger's (e.g. a TIER_2 trigger
     confirmed by a TIER_1/identity re-run), the confirmation's own
     detail — never a borrowed or absent one, never a literal
     "confirmation=None" — supplies its half of the body; TIER_2 → TIER_2
     (the common case) keeps this Design's original literal "mismatch
     counts: trigger=N, confirmation=M" + split format. TIER_2 → TIER_3 and
     TIER_2 → clean remain correctly "not confirmed" (neither is pageable).
   - Confirmation run clean → NO page; journal "transient wobble — trip
     not confirmed" (severity `warning`) with both run ids. The weekly
     history keeps both rows; nothing is deleted. This language is now
     reachable ONLY when the confirmation's own tier is genuinely
     non-pageable (TIER_3/none) — never when it tripped TIER_1 or TIER_2,
     per the correction above.
   - Confirmation cannot execute (preflight refusal / exception) → page
     the ORIGINAL trip immediately with the UNCONFIRMED marker (see fail
     direction). Never swallowed.
   - **Loop guard:** a confirmation run never spawns another confirmation,
     enforced structurally (the confirmation invocation passes an explicit
     flag / the run row carries `confirmation_of`), not by convention.
     Exactly one confirmation per trigger run, ever.

3. **Stable/boundary split (`canary_stability_v1`, frozen HERE, before any
   future data).** Derived from the full 9-run history vs baseline
   `canaryrun_de5814877cc1`, computed 2026-08-03 — a packet is STABLE iff
   its `primary_label` never differed from baseline in any non-baseline
   run. The frozen stable set (13 packets):

   ```
   pkt_077c103ae10b  TSLA   pkt_1eeb01e36b6d  IWM    pkt_25ad8d71eedf  XLF
   pkt_31bc21eccf10  SMH    pkt_33cc39aec8c7  NVDA   pkt_4b424842d943  XLE
   pkt_a7c1bcae7175  GOOGL  pkt_af6a10c6ea2b  XLK    pkt_d27d1d035916  AMD
   pkt_d74e3608adc2  AVGO   pkt_d9bc552ddc8e  AVGO   pkt_f182ebbce06b  META
   pkt_fe03feceab9c  SMH
   ```
   The other 7 corpus packets are BOUNDARY. The set lives as a versioned
   module constant (`CANARY_STABLE_PACKETS_V1`) with this derivation
   documented; changing membership = `canary_stability_v2`, a new frozen
   registration — never an in-place edit (same versioning law as
   `regime_rules_v1` / `trend_rules_v1`).
   - `drift_detail`'s label-drift block gains
     `{"stable_flips": n, "stable_total": 13, "boundary_flips": m,
     "boundary_total": 7}` wherever mismatches are computed (all tiers,
     including clean runs — the split is cheap and the history is useful).
   - Alert text renders the split: "flips: {n}/13 stable, {m}/7 boundary"
     with one fixed interpretive sentence: "Stable-packet flips are
     high-signal (never flipped in the 9-run characterization); boundary
     flips are the labeller's known decision-boundary wobble." States a
     measured fact, prescribes nothing (INSTR-2 neutrality discipline).
   - Packets added to the corpus AFTER v1 (not in either set) are counted
     and rendered as `unclassified_new` — never silently folded into
     either bucket.

4. **Storage (additive; SCHEMA_VERSION stays 3).** `canary_runs` gains
   `confirmation_of TEXT` (NULL for normal runs; the triggering run's
   `run_id` for confirmation runs). Trigger class, split counts, and
   confirmation outcome all live in the existing `drift_detail_json` — no
   further columns.

5. **Surfaces.** The weekly scheduled job path drives all of this
   unchanged (the confirmation happens inside `run_canary`'s caller layer,
   NOT by scheduling a second job — no new job types, no new lock keys).
   `canary_status` renders confirmation lineage ("run X confirmed/not
   confirmed by run Y") and the split. The daily brief's canary line
   reflects the CONFIRMED state, not the raw first trip.

## Tests (hermetic, §H.1; date-seeded mocks, direct construction)

1. Identity-triggered TIER_1 pages immediately; NO confirmation run
   executed; alert body carries the identity diff.
2. Failsafe-only TIER_1: no page on first trip; exactly one confirmation
   run; `confirmation_of` set on the second row.
3. TIER_2 trip + clean confirmation → zero alerts; "not confirmed" event
   journaled with both run ids; both runs in `canary_runs`.
4. TIER_2 trip + confirming trip → exactly ONE alert; body contains both
   run ids, both mismatch counts, the split, and "confirmed by same-day
   re-run".
5. Loop guard: a confirmation run that itself trips does NOT spawn a
   second confirmation (structural assert on call count, not convention).
6. Cost preflight refuses the confirmation → original trip pages
   immediately with the UNCONFIRMED marker (fail-toward-paging).
7. Confirmation run raises mid-flight → same as 6: unconfirmed page, error
   journaled, nothing swallowed, weekly job completes.
8. `CANARY_STABLE_PACKETS_V1` is exactly the 13 frozen ids above (literal
   assert — the registration is the test); split counts in drift_detail
   sum correctly (stable + boundary + unclassified_new == compared).
9. A corpus packet absent from both sets renders as `unclassified_new`,
   never silently bucketed.
10. Additive migration: `confirmation_of` materializes on a pre-CANARY-2
    DB; `SCHEMA_VERSION` stays 3; old rows NULL.
11. Scheduler-path proof: drive the WEEKLY JOB entry point end-to-end
    (HOLD-1 audit lesson — prove the production wiring, not just the
    library function) for one confirmed-page and one not-confirmed
    scenario.
12. Alert-send failure on a confirmed page: the confirmed-drift
    system_event persists (event-before-send, existing law).
13. Mock mode: whole flow deterministic, zero network.
14. `canary_status` renders confirmation lineage for both outcomes.

## Out of scope
Threshold recalibration (evidence-gated, needs more weekly history under
CANARY-2 semantics); per-set thresholds; provider-fingerprint blind spot
(`system_fingerprint` NULL on 100% of results — a provider-side silent
swap behind the same model name remains undetectable; separate ticket if
ever addressable); TIER_3 paging.

**Added (post-build audit, 2026-08-03):** `shadow_label.check_auto_suspend`'s
own `canary_runs.drift_tier = 'TIER_1'` latch is pre-existing on `main` and
CANARY-2 deliberately does not touch that query — but one reviewer proved the
latch is ALREADY mis-armed there independent of this ticket (a 2026-08-02
truncation TIER_1 row), currently unreachable only because
`SHADOW_LABELLING_ENABLED=false`. A proper fix needs real operator-owned
semantic decisions (does an `unconfirmed_page` trigger count toward suspend?
must legacy rows NOT be silently un-armed by a query change? does it need a
time window?) — filed as a follow-up ticket, and it must land before EXP-1
arms (`SHADOW_LABELLING_ENABLED=true`), not before this ticket merges.

**This is the named follow-up ticket — SUSP-1** (`docs/roadmap/alphaos-susp1-
canary-aware-suspend-spec.md`, operator D1–D3 ruling 2026-08-05, built on
branch `feat/susp-1`, holding for audit + explicit operator merge
instruction per this project's own change-control law, same as every other
ticket in this file): its own design has `check_auto_suspend`'s canary arm
read only trigger rows (`confirmation_of IS NULL`) within a
`SHADOW_SUSPEND_CANARY_WINDOW_DAYS` (default 14) recency window, latching on
every confirmation status except an explicit `not_confirmed`; legacy rows
(including the named 2026-08-02 `canaryrun_f607c73a2589` artifact) latch
conservatively until they age out of the window — no code deletes or
dismisses that row. Per its own hard gate, this ticket must merge before
`SHADOW_LABELLING_ENABLED` is ever set true; it has not merged yet, and that
flag is still false. **Audit-fixup addition (round 2):** the arm selection
widened beyond TIER_1 to also latch a TIER_2/label-drift trigger when THIS
spec's own cross-class confirmation lands a TIER_1-severity re-run verdict
on it (`confirmed-cross-class`) — the exact scenario CANARY-2's own MUST
FIX 1 exists to catch — so a confirmed identity/failsafe drift suspends
shadow labelling regardless of which row's own `drift_tier` carried the
original trigger; see SUSP-1's own STATUS CORRECTION for the full detail.
