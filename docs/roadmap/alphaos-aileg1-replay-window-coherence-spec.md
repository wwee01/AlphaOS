# AILEG-1 — Make the AI and baseline replay windows coherent

**Status:** SPEC. Fable strategist, 2026-08-16. Ordered by CK ("fix AI leg") after the
Group A audit found H-AI-1's two legs are replayed over different horizons.
**Ceremony:** Sonnet builder in an isolated worktree, blind Opus audit, merge on
explicit operator instruction. Touches PRE-REGISTERED evidence — see §4.

---

## 1. What the audit found, and what I found underneath it

**Audit finding (verified):** `outcomes_tracker.py:503` calls `replay_bracket(...)`
with **no `max_days`**, so the AI leg always replays a **5-day** bracket
(`DEFAULT_REPLAY_WINDOW_DAYS`). The baseline leg (`baseline/tracker.py:363`) uses
`row["max_holding_days"] or 5`. Measured on `threshold_v1` paired rows:

| pairs | n | mean ΔR |
|---|---|---|
| matched (both 5d) | 463 | **+0.0461** |
| mismatched (5d AI vs 3d baseline) | 100 | **−0.1219** |
| pooled — what the report prints | 563 | **+0.0163** |

Against H-AI-1's registered `target_delta_r = 0.05`, that is the difference between
roughly clearing the bar and a third of it.

**What I found underneath it (new, and it changes the fix):**

1. **The AI leg has no per-row window to honour.** `candidate_outcomes` has **no**
   holding-days column at all, and only **11 of 637** `replay_r` rows have an
   associated proposal carrying `max_holding_days` — because the system has only
   ever produced 11 proposals. The AI leg scores CANDIDATES, and a candidate has no
   holding window until it becomes a proposal. So "pass the row's window" is not
   available; the window must come from the **card that governed that candidate**.
2. **The frozen v1 baseline arm is itself internally inconsistent.** Within a single
   pre-registered arm:
   - `threshold_v1`: **643 rows with `max_holding_days` NULL (→5)** and **141 rows at 3**
   - `propose_all_v1`: 784 rows at 3
   - `threshold_v1_hold10`: 141 NULL (→5!) and 20 at 10 — **the 10-day arm is
     replaying most of its rows at 5 days**, which silently undermines HOLD-2's own
     new pre-registration (`prereg_35bbce762e7f`, analysis 2026-10-05).
   The variation is interleaved by date, not a clean version cutover.

So this is not "one leg drifted". **Neither leg has a principled window**, and the
`hold10` arm — registered nine days ago specifically to measure a 10-day horizon —
is mostly not measuring one.

## 2. The rule

**One source of truth for the replay window: the card that governed the row.**
Every `replay_bracket` call passes an explicit `max_days`; no call site relies on the
default. Concretely:

- **AI leg** (`outcomes_tracker`): window = the `max_holding_days_default` of the card
  stamped on that candidate (`candidates.card_id` / `card_version`), resolved by id —
  never the live default card (the HOLD-2 lesson: a frozen arm must not follow
  `ACTIVE_CARD_ID`). Fall back to the pinned v2 card's value only when the candidate
  carries no card, and stamp which path was used.
- **Baseline v1 arms**: window = pinned card `catalyst_momentum_v2` → **3**, by id,
  unconditionally. No `or 5` fallback.
- **Baseline hold10 arm**: window = pinned card `catalyst_momentum_v3` → **10**, by id,
  unconditionally.
- `DEFAULT_REPLAY_WINDOW_DAYS` stops being reachable from any production path; keep it
  only as an explicit default for ad-hoc/CLI use, and add a test asserting no
  production caller omits `max_days`.

**Persist the window on the row.** Add `replay_window_days` to `candidate_outcomes`
and to `shadow_baseline_decisions` (additive columns, the repo's existing migration
pattern). A replay result whose horizon is not recorded is not reproducible — that is
the same provenance gap that made this finding invisible for a month.

## 3. Scope

IN: `learning/outcomes_tracker.py`, `learning/outcomes_engine.py` (signature/doc
only — no arithmetic change), `baseline/tracker.py`, `journal/schema.py` (two additive
columns), the recompute CLI in §4, tests.

OUT: the replay arithmetic itself; `hypotheses/queries.py`; the reports; the
`'complete'`-only pairing rule (frozen 2026-08-14, §9 `b0b9e53`); anything on the
seven-lens review's other open items.

## 4. THE OPERATOR DECISION — forward-only vs recompute

`replay_r` is a **deterministic** function of (entry, stop, target, direction, bars,
window). We hold all six for every row. So recomputation is a *repair*, not a re-roll:
running it twice gives the same answer, and it cannot be influenced by knowing the
result. That is what makes the option legitimate here at all.

- **Option A — forward-only.** New rows get correct windows; the ~637 existing
  `replay_r` rows keep their 5-day values. Consequence: H-AI-1's 2026-09-07 analysis
  reads a series with a discontinuity in the middle, and the hold10 arm's 2026-10-05
  analysis is mostly 5-day data wearing a 10-day label. Both would need segmentation
  and would likely fall below floor once segmented.
- **Option B — recompute (RECOMMENDED).** Re-run `replay_bracket` for every existing
  row with its correct card-derived window, in one auditable pass that writes
  `replay_window_days` alongside. The whole series becomes internally consistent and
  matches what both pre-registrations always *meant*. Cost: it rewrites values in a
  pre-registered evidence table — which must be logged in §9 before it runs, with
  before/after counts, and must be a separate explicit operator command, never a
  side effect of the merge.

**Recommendation: B**, executed as its own operator-invoked CLI (`replay_recompute`,
dry-run by default, `--apply` to write), with the §9 row written BEFORE the apply and
recording the measured before/after ΔR for both arms. Rationale: the alternative is
knowingly analysing a mixed instrument on two dated one-shot gates, which is the
failure this whole remediation arc exists to end. The determinism is what makes this
honest; if the metric were stochastic, forward-only would be the only defensible path.

**The merge must not recompute anything.** Ship the code dark: correct windows for
NEW rows, the CLI present but not run. CK runs the recompute as a separate decision.

## 5. Test obligations

1. AI leg uses the candidate's card window, resolved BY ID, and does not follow
   `ACTIVE_CARD_ID` (swap it; assert the window does not move).
2. Baseline v1 arms are pinned at 3 and hold10 at 10, both by id, with no `or 5`
   fallback reachable.
3. `replay_window_days` is persisted on both tables and matches the window used.
4. No production call site omits `max_days` (AST/grep test, in the style of
   `test_vocab_guard.py`).
5. Determinism: recomputing an unchanged row twice yields byte-identical values.
6. `replay_recompute --dry-run` writes nothing; `--apply` writes exactly the rows it
   reported; both are idempotent.
7. Migration is additive; existing rows read back unchanged before any recompute.
