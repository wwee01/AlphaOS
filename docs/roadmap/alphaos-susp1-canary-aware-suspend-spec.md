# ALPHAOS SPEC — SUSP-1: Canary-Aware Shadow-Label Auto-Suspend — drafted 2026-08-04

Same laws as every spec in this directory: spec → build → independent review →
**merge only on explicit human instruction**. §H.1 test discipline. Shadow/
measurement-layer only — this ticket touches nothing on the live trading
path. **HARD GATE: this ticket must MERGE before `SHADOW_LABELLING_ENABLED`
is ever set true.** It is the named blocker on EXP-1 arming (§9, 2026-08-03;
CANARY-2 audit round, both reviewers).

## Motivating evidence (both CANARY-2 audits, 2026-08-03, probe-proven)

`alphaos/scheduler/shadow_label.py::check_auto_suspend` gates EXP-1 shadow
labelling on:

```sql
SELECT run_id FROM canary_runs WHERE drift_tier = 'TIER_1' ORDER BY id DESC LIMIT 1
```

Unfiltered in every dimension that CANARY-2 just made meaningful:

1. **No confirmation filter.** A TIER_1/failsafe trigger row that CANARY-2
   subsequently proved a transient wobble (confirmation clean, NO page
   sent) still latches the suspend. Probe-reproduced end-to-end by both
   auditors: a safety mechanism disarms a subsystem based on evidence the
   system itself judged unreliable, while the canary page explaining it
   was deliberately suppressed. (The suspend path sends its OWN page, so
   it is loud — but it re-introduces through a side door exactly the
   noise-page desensitization CANARY-2 exists to close.)
2. **No trigger/confirmation distinction.** A confirmation *run* row that
   trips TIER_1 latches independently of its trigger — double-counting
   one event.
3. **No time window.** ANY historical TIER_1 row latches forever.
   **Live consequence, already true on main:** run
   `canaryrun_f607c73a2589` (2026-08-02, TIER_1) sits in the production
   ledger. It is the token-truncation artifact — root-caused same day,
   fixed in `f448412` (LABEL_MAX_OUTPUT_TOKENS 800→1200), re-run clean
   (`canaryrun_1f386eb1d820`, drift none). Yet the moment
   `SHADOW_LABELLING_ENABLED` flips, the FIRST tick auto-suspends shadow
   labelling on that fully-explained, fixed, three-day-old event — and
   the switch is non-self-healing (manual file delete,
   `alphaos/safety.py`). A suspension sitting unnoticed costs
   pre-registered SETUP-1 observations (2026-10-15 analysis) that cannot
   be back-filled.

The feed-coverage arm of `check_auto_suspend` is untouched by all of this
and by this ticket.

## Goal

The suspend latch should fire on evidence the system itself stands behind:
a CURRENT identity change, a CURRENT confirmed drift, or a CURRENT trip
whose confirmation could not run (fail toward suspension) — never on a
trip the confirmation machinery already proved transient, never twice for
one event, and never on stale history the operator has already resolved.

## Fail direction

Where CANARY-2's protected asset is the page, SUSP-1's is **honest
suspension**: when the evidence is ambiguous, suspend (EXP-1's own
established direction — contaminated shadow labels are irreversible;
a paused collector is merely paused). Concretely: an `unconfirmed_page`
TIER_1 (confirmation could not execute) LATCHES; a malformed/unparseable
`drift_detail_json` on a TIER_1 row LATCHES (never skip a row because its
metadata is broken); only an explicit `not_confirmed` verdict releases.

## Latch semantics (the whole ticket)

~~A TIER_1 row~~ A TIER_1 row (or a TIER_2 row via `confirmed-cross-class`
— see the STATUS CORRECTION after clause 3 below) latches the suspend iff
ALL of:

1. **It is a trigger row** — `confirmation_of IS NULL`. Confirmation runs
   never latch independently; their verdict lives on their trigger row's
   annotation.
2. **Its confirmation status is not an explicit all-clear.** Reading the
   trigger row's `drift_detail_json → confirmation.status`:
   - `identity_immediate` → LATCHES (deterministic identity change).
   - `confirmed` → LATCHES.
   - `unconfirmed_page` → LATCHES (fail toward suspension).
   - `not_confirmed` → does NOT latch (system-proven transient, no page
     was sent — suspending here is the side-door noise this ticket
     closes).
   - Key absent / JSON malformed / legacy pre-CANARY-2 row → LATCHES
     (conservative default; legacy rows are never silently un-armed by
     code — see the operator decision below).
   - *(added, STATUS CORRECTION below)* a real, non-None status matching
     NONE of the four literals above → LATCHES too, but under its OWN
     `unrecognized-status` arm, not lumped in with the legacy case — a
     legacy row ages out of the window on its own; an unrecognized status
     does not, since current code is writing it every cycle.
   - *(TIER_2 rows only, STATUS CORRECTION below)* `confirmed` with
     `confirming_drift_tier == TIER_1` → LATCHES (`confirmed-cross-class`).
     Every other TIER_2 outcome does NOT latch.
3. **It is within the recency window** — `started_at_utc` within the last
   `SHADOW_SUSPEND_CANARY_WINDOW_DAYS` (new setting, default **14**,
   validated [7, 90]). Rationale: real drift is persistent and the canary
   runs weekly, so genuine drift REFRESHES the window with a new TIER_1
   row every cycle; a stale row aging out means the drift stopped
   appearing, which is exactly the operator-resolved case (the 08-02
   truncation row: fixed, re-run clean, but latched forever under current
   code). The window converts "resolved history" into release WITHOUT any
   bespoke un-arm mechanism, while the weekly cadence guarantees current
   drift cannot age out. 14 days = two missed weekly cycles of margin.

The suspend reason string names which arm latched (identity / confirmed /
unconfirmed / legacy-conservative) and the run id, so the suspend's own
page tells the operator what class of evidence tripped it.

> **STATUS CORRECTION (2026-08, audit-fixup round 2 — two independent Opus
> audits, both convergent on this finding as their own recommendation, both
> re-verified the scoping in round 3):** the "A TIER_1 row latches..."
> framing above, and Design 1's `drift_tier = 'TIER_1'` query further down,
> are narrower than what actually ships. **The operator's D1–D3 ruling
> below was given against this TIER_1-only text — D1–D3 themselves still
> hold, nothing here reopens them** — but the selection widened after
> build, for a reason the ruling never had in front of it:
>
> **The gap (a proven regression vs pre-SUSP-1 `main`):** CANARY-2 can
> confirm a TIER_2/label-drift trigger with a TIER_1-severity (identity or
> failsafe) same-day re-run — a genuine, currently-pageable "model drift
> CONFIRMED (TIER_1)" event. A TIER_1-only query never sees that row (its
> own `drift_tier` is TIER_2), so round-1 SUSP-1 code did NOT suspend on a
> confirmed TIER_1-severity drift — worse than pre-SUSP-1 `main`, whose
> original unfiltered query would have caught it via the confirming row.
>
> **The fix:** the canary arm now selects trigger rows with
> `drift_tier IN ('TIER_1', 'TIER_2')`. A fifth latch arm,
> `confirmed-cross-class`, fires iff BOTH: the trigger row's own
> `confirmation.status == confirmed` AND
> `confirmation.confirming_drift_tier == TIER_1` — i.e. ONLY a
> TIER_1-severity confirmed outcome, regardless of which row's own
> `drift_tier` carried the original trigger. Every other TIER_2 outcome
> (not_confirmed, unconfirmed_page, a same-tier TIER_2-confirmed-by-TIER_2,
> legacy/malformed/unrecognized) stays UNLATCHED — pre-SUSP-1 `main` never
> read TIER_2 rows at all, so there is no regression to guard against in
> that direction, and **whether a same-tier confirmed TIER_2 should EVER
> suspend is an open operator policy question, deliberately left unbuilt
> and unruled here** (not decided by this correction, not decided by
> D1–D3 — a separate future ruling if the operator ever wants one).
>
> Also round 3: a sixth case was added to clause 2's own table — a real,
> non-None confirmation status matching NONE of the four known literals
> (e.g. a future deployment's new status, or a hand-edited row) latches
> under its own `unrecognized-status` arm, not `legacy-conservative` — the
> two look alike (both latch, both are "the system has no proof this was
> transient") but carry opposite operator implications: a legacy row ages
> out of the recency window on its own; an unrecognized status means
> CURRENT code is writing it every cycle and will not. The reason string
> now names six arms (identity / confirmed / unconfirmed /
> legacy-conservative / confirmed-cross-class / unrecognized-status), not
> four, and for the two new arms it also names the specific trigger tier
> and (for unrecognized-status) the offending status value itself. See
> `alphaos/scheduler/shadow_label.py::_canary_confirmation_latch`'s own
> docstring for the exact, current per-tier branching — this correction
> describes intent and history; that docstring is the executable truth.

## Operator decisions required BEFORE build (CK — rule on these)

- **D1. Window default 14 days** ([7, 90] bounds). Shorter = faster
  self-release of resolved events; longer = more margin for missed weekly
  runs. 14 recommended.
- **D2. The existing `canaryrun_f607c73a2589` row.** Under this spec it is
  a legacy row (no confirmation key) → latches conservatively — but only
  while inside the window, so it ages out ~2026-08-16. If EXP-1 arms
  BEFORE then, first tick suspends on it; the operator either waits out
  the window or clears the switch once, knowingly. No code un-arms it.
  Confirm this is acceptable (recommended: yes — it is one manual clear
  at most, and the alternative is code that silently erases a latch).
- **D3.** Confirm `not_confirmed` releasing the latch is acceptable — this
  is the one arm where SUSP-1 trusts CANARY-2's verdict over raw
  conservatism. (Recommended: yes — that trust is the entire point of
  having built the confirmation machinery.)

## Non-goals (frozen)

- The feed-coverage suspend arm: untouched.
- The suspend switch's non-self-healing character (manual clear):
  untouched. Making it self-heal on a later clean canary run changes a
  safety mechanism's nature — separate ticket if ever wanted. Recorded,
  not built.
- No change to CANARY-2 semantics, thresholds, corpus, or paging.
- No schema change at all (reads existing columns + JSON; new setting is
  config, not schema). `SCHEMA_VERSION` stays 3.
- No change to `run_shadow_label`'s flag gate, cadence, or caps.
- Zero live decision surface, unchanged (this is shadow-layer gating
  shadow-layer).

## Design

1. **`check_auto_suspend` canary arm** replaced with a query selecting
   recent trigger rows (`confirmation_of IS NULL`,
   ~~`drift_tier = 'TIER_1'`~~ `drift_tier IN ('TIER_1', 'TIER_2')` — see
   the STATUS CORRECTION above, `confirmed-cross-class` —
   `started_at_utc >= now − window`) ordered newest first, then per-row
   latch evaluation per the semantics above — first latching row wins.
   Reason string per above (six arms as of the STATUS CORRECTION, not
   four).
2. **New setting** `shadow_suspend_canary_window_days` /
   `SHADOW_SUSPEND_CANARY_WINDOW_DAYS`, default 14, validated [7, 90],
   documented in `.env.example` next to the other shadow-label settings.
3. **Docstring updates**: `shadow_label.py`'s auto-suspend docstring and
   `canary/run.py`'s consumer note both describe the new semantics; the
   CANARY-2 spec's Out-of-scope pointer gains a "resolved by SUSP-1"
   banner (append-style). Audit-fixup rounds 2–3 additionally touch
   `alphaos/safety.py`'s `ShadowLabelSuspendSwitch` docstring (was still
   describing the pre-SUSP-1 "any CANARY Tier-1 drift event" trigger) and
   `alphaos/reports/canary_report.py` (third independent spelling of the
   confirmation-status vocabulary, now importing the same
   `alphaos.constants` symbols as the other two, and covered by the same
   AST lockstep test).

## Tests (hermetic, §H.1)

1. `identity_immediate` TIER_1 trigger within window → latches; reason
   names identity.
2. `confirmed` TIER_1 within window → latches.
3. `unconfirmed_page` TIER_1 within window → latches (fail direction).
4. `not_confirmed` TIER_1 within window → does NOT latch (the headline
   fix — the exact both-auditor probe scenario: failsafe trip, clean
   confirmation, no page → shadow labelling keeps running).
5. Confirmation RUN row tripping TIER_1 (with `confirmation_of` set) →
   never latches independently.
6. Legacy row (no confirmation key in drift_detail) within window →
   latches (conservative); same row OUTSIDE window → does not.
7. Malformed `drift_detail_json` on an in-window TIER_1 trigger →
   latches (never skipped-because-broken).
8. Window boundary: row at exactly window-edge behaves per the documented
   comparison (pin the operator, >= vs >, in a test).
9. Real-ledger shape regression: a row shaped exactly like
   `canaryrun_f607c73a2589` (TIER_1, legacy, dated N days ago) latches at
   N < window and releases at N > window.
10. Feed-coverage arm byte-untouched (existing tests stay green
    unmodified) + the settings validation bounds.
11. End-to-end through `run_shadow_label` (production entry): the
    not-confirmed scenario proceeds to labelling; the confirmed scenario
    suspends, engages the switch, and pages — proving the wiring, not
    just the helper (HOLD-1 audit lesson).

## Sequencing

Small ticket: one Sonnet builder, standard two-Opus-audit ceremony, merge
on explicit operator instruction. **Blocks EXP-1 arming; nothing blocks
it.** Build starts after CK rules on D1–D3.
