# ALPHAOS SPECS — Primary-Evaluator A/B Replay + ATR-Coherent Targets + Model Tripwire (drafted 2026-07-20)

Companion to the throughput agenda. Same laws as every spec in this directory:
spec → build → independent review → **merge only on explicit human
instruction**. Shadow-first. Additive migrations only; bump `SCHEMA_VERSION`.
§H.1 test discipline (date-seeded mocks, direct construction). Ticket names
checked against the repo 2026-07-20 — no collisions with EVAL-1 /
PROMPT-AB-1 (both labeller-side) or INSTR-1 (shipped 2026-07-09, struck).

## Motivating evidence (ledger, read-only, 2026-07-20)

The proposal funnel died on 2026-07-09 and has produced **zero propose
verdicts in 7+ trading days** despite candidate flow tripling. Two dateable
causes, neither of them the classical risk gates (risk_checks: 7/7 passed;
liquidity/spread/freshness rejected ~145 live-path candidates in 3 weeks):

1. **INSTR-1 target/stop incoherence (2026-07-09/10, 100% kill).**
   `_apply_atr_stop` (alphaos/ai/openai_client.py) replaces the AI's stop
   with entry − 2.0×ATR(14) but keeps the AI's target, recomputes
   `expected_r`, and `_enforce_min_reward_risk` (floor 1.2) force-rejects.
   Prompt v1 never tells the model its stop will be ATR-widened, so targets
   are placed ~2R against the model's own tighter stop and compute to R:R
   0.06–1.05 against the ATR stop. All 18 would-be proposes on 07-09/07-10
   were downgraded this way (`reasoning_summary LIKE 'reward:risk%below
   minimum%'`).
2. **Ungated evaluator model swap (live 2026-07-13).** Primary model
   changed gpt-5.4-mini → gpt-5.6-luna. Mini raw-proposed 15 of ~40
   candidates (07-01→07-08); luna has raw-proposed 0 of ~135 since, mostly
   narrative rejects (avg confidence 0.26). Confounded with market
   conditions — live data cannot separate model temperament from regime.
   CANARY could not catch this: it replays the **labeller**
   (`settings.label_model`), not the primary evaluator, and its baseline
   was pinned on luna itself (2026-07-11). Note also the 2026-07-19 weekly
   canary run flagged TIER_2 (4/20 label mismatches at the 0.2 threshold)
   against luna's own baseline — separate adjudication, not covered here.

Replayable inputs exist: `openai_evaluations.snapshot_json` is populated for
every real evaluation from 2026-07-09 onward (EVAL-1 addendum; 180 rows as
of 2026-07-20). Rows before 07-09 have no snapshots and are NOT replayable.

Build order and rationale:
**AB-EVAL-1 → TRIP-1 (tiny, anytime) → INSTR-2 (gated on AB-EVAL-1 evidence
+ operator model ruling).**
AB-EVAL-1 first because it is pure shadow (zero live surface) and its report
is the evidence gate for both the keep-vs-revert model decision and INSTR-2's
prompt cutover. INSTR-2 is the only live-path change in this file and may
not merge, let alone cut over, without the replay report in hand.

---

## AB-EVAL-1 — Primary-evaluator A/B replay harness (shadow, read-only)

### Goal
Attribute the propose drought between (a) INSTR-1 floor mechanics, (b) model
temperament (mini vs luna), and (c) market conditions, by replaying
**identical stored snapshots** through both models and comparing verdicts.
Controlled experiment; kills the market confound by construction.

### Non-goals
- No live-path change of any kind. Results are **never read by any
  gate/eval/risk/execution path** (same zero-decision-surface law as CANARY).
- No model cutover automation. Keep/revert is an operator decision logged in
  the decision log.
- Not the labeller. EVAL-1 / PROMPT-AB-1 own that side.
- No prompt changes (that is INSTR-2). This harness replays prompt v1 as-is.
- No auto-repin of the canary baseline.

### Design
1. **Corpus (frozen selection).** Source rows: `openai_evaluations` with
   `is_mock=0 AND snapshot_json IS NOT NULL`. Default selection: ALL rows
   from 2026-07-09/07-10 (the 34 mini-era kill-zone rows — these are the
   packets where the floor did 100% of the killing) + a stratified sample of
   luna-era rows by (decision, date) to a default total of 60 packets,
   operator-tunable. Materialize the chosen `eval_id`s + snapshot hashes
   into a manifest (same sha256-freeze discipline as `alphaos/canary/corpus.py`);
   a re-run replays the same frozen selection or fails loudly on tamper.
2. **One replay engine, one truth.** Factor `OpenAIClient.evaluate()` so the
   raw model call and the post-processing (`_apply_atr_stop` →
   `_enforce_min_reward_risk`) are separately invokable on a reconstructed
   snapshot. The replay MUST route through the production prompt-build and
   call path with only the model name parameterized — no forked second
   prompt. Enforce structurally (AST test, same pattern as PR13's drift
   test), not by docstring.
3. **Two comparison layers per (packet, model).** Store BOTH the model's
   **raw** verdict (decision/confidence/entry/stop/target/expected_r as
   returned) and the **pipeline-final** verdict after ATR override + floor,
   with `downgrade_reason` (`NULL` | `RR_FLOOR` | `NO_ATR`). Rationale: the
   stored 07-09/07-10 ledger decisions are post-floor; comparing only final
   verdicts would attribute the INSTR-1 defect to the model.
4. **Storage (additive).** New table `ab_eval_results`: ab_run_id, eval_id
   (corpus ref), symbol, model, raw_decision, raw_confidence, raw_entry,
   raw_stop, raw_target, raw_expected_r, pipeline_decision,
   pipeline_expected_r, downgrade_reason, reasoning_summary, token counts,
   lineage_id, created stamps. Plus `ab_eval_runs` header row (manifest
   hash, models compared, n_packets, started/finished). Bump
   `SCHEMA_VERSION`.
5. **Cost discipline.** Same pre-flight as CANARY: `cost_guard` 30-day cap
   check with planned_calls = n_packets × n_models (default 60×2=120);
   refuse to start over cap. Mock mode replays deterministically with no
   network (has_openai_key=false ⇒ mock, same convention as run_canary).
6. **Report (the deliverable).** One markdown report + one digest line:
   - Raw decision distribution per model over identical packets.
   - Per-packet confusion matrix (mini raw vs luna raw), with the flipped
     packets listed alongside both reasoning summaries.
   - Floor autopsy: fraction of raw proposes (per model) downgraded by
     `RR_FLOOR`; distribution of raw_expected_r on those rows.
   - Explicit caveat line: n=60 packets over 8 trading days is
     **descriptive, not significant**; no q-values claimed (FDR law).
7. **Expected observations + branches (pre-registered here):**
   - If mini raw-proposes materially more than luna on identical inputs →
     the swap cost is quantified; operator rules keep/revert/re-prompt as a
     logged decision row.
   - If mini ≈ luna raw verdicts → drought is INSTR-1 + market; model is
     exonerated; proceed to INSTR-2 with luna.
   - If either model's raw proposes die ≥50% at `RR_FLOOR` → INSTR-2's
     premise is confirmed with a number attached.

### Tests
Hermetic; date-seeded mock evaluations constructed directly. Cases: frozen
manifest tamper → loud failure; raw-vs-pipeline capture differs when the
floor trips (inject a raw propose whose ATR R:R < 1.2 and assert
`downgrade_reason='RR_FLOOR'` with raw_decision='propose' preserved); AST
test pinning the replay to the production evaluate core; cost-guard refusal
over cap; one bad snapshot row skips, never aborts the run (same per-packet
isolation law as CANARY/EVAL-1).

---

## TRIP-1 — Primary-model identity tripwire (tiny)

### Goal
Close the blind spot the mini→luna swap walked through: CANARY watches
`label_model` only; `openai_primary_model` changes are unwatched.

### Design
At scan start, compare `settings.openai_primary_model` against the `model`
column of the most recent real (`is_mock=0`) `openai_evaluations` row. On
mismatch: journal a `system_event` (severity WARNING, component `openai`) and
send one alert — "primary evaluator model changed {old}→{new}; a model change
is a strategy change: run the AB-EVAL replay and log the decision before
trusting new verdicts." No new tables, no blocking — a tripwire, not a gate
(fail-open by design: it must never stop a scan; it exists to page, and the
scan's own evaluations remain journaled either way).

### Tests
Mismatch fires exactly one event+alert per change (idempotent across
subsequent scans of the same new model — the "most recent eval row" moves);
no-op when models match; no-op on empty ledger (fresh install).

---

## INSTR-2 — ATR-coherent evaluator targets (prompt v2, live-path, gated)

### Goal
Make target-setting and stop-setting share the ATR risk scale, so the 1.2
floor measures real trade geometry instead of a units mismatch. **This is a
coherence fix, not a loosening: the floor stays 1.2 and the deterministic
2.0×ATR(14) stop override stays exactly as shipped.**

### Non-goals
- No change to `MIN_REWARD_RISK`, `ATR_STOP_MULTIPLIER_V1`, or
  `_apply_atr_stop` / `_enforce_min_reward_risk` semantics (defense in
  depth: the override remains even when the model cooperates).
- No change to the `NO_ATR_DATA` fail-safe reject.
- No response-schema change (the model still returns a stop; it becomes
  advisory).

### Design
1. **Surface ATR into the prompt input.** The snapshot gains `atr_14` (and
   the computed stop price + risk-per-share), fetched the same way
   `_apply_atr_stop` already does (`atr_history`, `ATR_RULES_V1`). One
   fetch, shared — do not query twice per evaluation.
2. **Prompt v1 → v2** (bump `prompt_template_version` literal). v2 states
   the policy plainly: "Your stop WILL be set at entry − 2.0×ATR(14) =
   {stop_price} for longs (entry + for shorts); risk per share is
   {risk_per_share}. Propose a target only if (|target − entry| /
   risk_per_share) ≥ 1.2; otherwise reject." Wording must inform, not
   coach: it may not instruct the model to prefer proposing.
3. **Pre-cutover proof gate (uses AB-EVAL-1's harness).** Replay the frozen
   corpus under (luna, v1) vs (luna, v2). Pre-registered expectations:
   - `RR_FLOOR` downgrade rate on raw proposes drops from ~100% (kill-zone
     packets) to <20%.
   - Flip-direction check: v2 must NOT convert packets that BOTH models
     raw-rejected narratively into proposes — that would be coaching, not
     clarifying. If observed → revise wording, re-run; do not cut over.
4. **Rollout.** Build + merge dark is acceptable (version literal unused
   until cutover); **cutover is a separate explicit operator instruction**
   after the replay report, logged as a decision row. Evaluations under v2
   are a new selection process (new ruler): `prompt_template_version` is
   already journaled per row, and the daily brief notes the cutover date so
   downstream sample-floor accounting can split on it.

### Tests
Prompt-builder unit tests: v2 renders ATR numbers correctly for long and
short; missing ATR renders nothing new (fail-safe path untouched — assert
the `NO_ATR_DATA` reject still fires); version literal bumped exactly once;
snapshot journaling captures the ATR fields (replayability preserved);
structural test that live scan path still applies `_apply_atr_stop` after a
v2 evaluation (the override is not conditionally skipped).

---

## Explicitly out of scope for all three tickets
- Any change to liquidity/spread/freshness gates, position caps, or risk
  sizing — the ledger shows they are not the constraint.
- CF-1 (counterfactual gate ledger) — stays on the throughput agenda as the
  durable per-gate instrument; AB-EVAL-1 is the targeted, cheap version for
  this specific incident and does not replace it.
- The TIER_2 canary adjudication (operator, separate protocol).
- EXP-1 arming — its own gate; unaffected by this file.
