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

## TRIP-1 — Evaluator identity tripwire (tiny, rides the scan)

REFINED 2026-07-27 against the post-v3-cutover code and ledger state. The
2026-07-20 draft (a) watched `openai_primary_model` only — but
`prompt_template_version` has since moved three times in production
(ledger, verified read-only 2026-07-27: `gpt-5.6-luna|v3|5 rows`,
`gpt-5.6-luna|v2|21`, `gpt-5.6-luna|v1|261`, `gpt-5.4-mini|v1|74`) and the
§9 KEEP-luna ruling (2026-07-26) names TRIP-1 as the tripwire for the NEXT
swap on either axis; and (b) rested idempotency entirely on "the most
recent eval row moves" — disproven below (Design 4): an `ai_degraded` scan,
an empty shortlist, or a mock-mode day produces zero real eval rows, so the
reference cannot be relied on to move.

### Goal
Make the NEXT silent evaluator-identity change loud: exactly one page per
distinct change, at the first scan that would produce verdicts under the
new identity, before those verdicts accumulate. Closes the blind spot the
2026-07-13 mini→luna swap walked through (CANARY replays the LABELLER,
`settings.label_model`, against a baseline that was itself pinned on luna).
House doctrine served: **a model change is a strategy change** (§9,
2026-07-26) — replay before trusting new verdicts, log the decision.

### Scope rule (which axes belong to TRIP-1)
An axis belongs to TRIP-1 iff BOTH hold:

- **(a)** it changes the PRIMARY evaluator's judgment identity — which
  model judges, or which prompt contract that model is shown; and
- **(b)** it is stamped per-row into `openai_evaluations` — the ledger
  itself records which identity produced every verdict, so the last real
  row is a valid detection reference with zero new state.

Future sessions answer "does axis X belong in TRIP-1?" from this rule
alone. Today exactly two axes qualify:

| Watched axis | Settings field | Stamped column |
|---|---|---|
| primary model | `openai_primary_model` | `openai_evaluations.model` |
| prompt version | `openai_prompt_version` | `openai_evaluations.prompt_template_version` |

Excluded, with reasons:
- `label_model`: fails (a) — labeller identity is CANARY's job (pinned
  baseline, corpus replay; a different instrument answering a different
  question). TRIP-1 must not duplicate it.
- `openai_review_model`: fails BOTH today. Grep-verified 2026-07-27: no
  non-test code reads it except `settings.py` and the config fingerprint —
  it drives no call path and stamps no rows. There is no verdict stream to
  protect. The day a review path actually consumes it AND stamps its rows,
  it satisfies the rule and joins TRIP-1 — that is the maintenance
  trigger, not a re-litigation.
- Provider-side aliasing (the API silently serving a different underlying
  model for the same configured name): out of scope — the `model` column
  stamps the CONFIGURED name (`OpenAIClient.model`), so settings-vs-ledger
  comparison cannot see it. Response-identity drift is CANARY's Tier-1
  concept (`response_models_json`); an evaluator-side analog would first
  need response identity persisted per eval row — a prerequisite, same
  shape as §9's EVAL-1 scope ruling.

### Non-goals (frozen)
- No acknowledgment mechanism, ack CLI, or "expected change"
  pre-registration of any kind (Design 5 defends the decision).
- No blocking, no gating, no scan abort under ANY branch (Design 6).
- No new tables, no schema change, no `SCHEMA_VERSION` bump —
  `system_events` is both the audit record and the dedupe store.
- No new scheduler job, cadence, or lock key — rides `run_scan_once`.
- Does not read, diff, or extend `config_versions` (Design 2 justifies).
- No auto-revert; nothing here ever writes settings or `.env`.
- Not a CANARY replacement or extension; zero decision surface (never read
  by any gate/eval/risk/execution path — same law as CANARY/AB-EVAL-1).

### Design
1. **Module + hook point.** New module `alphaos/tripwire.py`, one public
   function `check_evaluator_identity(journal, settings) -> dict`.
   `Orchestrator.run_scan_once` calls it immediately after
   `self._ensure_startup()` and DISCARDS the return value (the dict —
   `{"checked", "fired", "suppressed", "reference_eval_id"}` — exists for
   tests only). Placement is load-bearing: the check MUST run before this
   scan's first `openai_evaluations` insert (single production insert
   site: `orchestrator.py` ~line 517, inside the candidate loop). If it
   ran after, the scan's own rows would move the reference to the new
   identity before it was ever compared, and the tripwire would be
   structurally silent forever — Design 4 depends on this ordering.

2. **Detection reference = the last real eval row, NOT `config_versions`.**
   One query, both axes:
   `SELECT eval_id, model, prompt_template_version, created_at_utc FROM
   openai_evaluations WHERE is_mock = 0 ORDER BY id DESC LIMIT 1`.
   - `ORDER BY id DESC`, not `created_at_utc DESC`: `id` is
     `AUTOINCREMENT` insertion order (authoritative); `created_at_utc` is
     ISO text stamped by `JournalStore.insert` and can tie within the same
     instant. Test 11 pins this.
   - `is_mock = 0` because mock rows stamp `model='mock'` — including them
     would fire a spurious `'mock'→<configured>` page on any mock-era
     install.
   - Compare `settings.openai_primary_model` vs `row.model`, and
     `settings.openai_prompt_version` vs `row.prompt_template_version`.
   Why NOT build on `config_versions` (decided; don't re-litigate): the
   fingerprint already contains both axes (HGEN-1 fixup + INSTR-2), but it
   is a per-process-startup RECORD, not a pager — rows accumulate
   silently, nothing diffs them, the hash moves on any of ~25 axes, and it
   records config blips that never produced a verdict (flip model → run
   one CLI command → flip back). The doctrine object is *the identity the
   ledger's verdicts were actually produced under*, and that ground truth
   lives in `openai_evaluations` itself — both axes deliberately stamped
   per-row (EVAL-1 addendum + INSTR-2 Design 1). TRIP-1 stays independent:
   `config_versions` remains the forensic record, TRIP-1 is the pager;
   they can never disagree because both read the same frozen settings
   object.
   Edge cases, specified:
   - Empty ledger / fresh install / pure-mock install (no real rows):
     silent no-op — nothing to protect yet; same stance as CANARY's
     `get_baseline_run` returning "no baseline pinned yet" rather than
     fabricating a verdict.
   - `NULL` in a reference column (legacy or hand-tampered row): no-op for
     THAT axis only; never a crash, never an alert on an unknowable old
     value; the other axis is still checked.
   - Zero-eval day (cost-cap `ai_degraded`, empty shortlist, kill switch,
     non-trading day): the reference is stale-but-true — the last real
     identity — which is exactly the durable comparison wanted.
     Idempotency then comes from Design 4's dedupe, NOT from hoping the
     reference moves.
   - Mock-mode scan (`use_mock`): the check still runs, against the last
     REAL rows. A model changed while mock is on pages once — correct: the
     change will bite the moment live resumes, and one early page beats a
     surprise later.

3. **On mismatch: journal + one alert.** Per mismatched axis, one
   `system_events` row: severity `WARNING`, category `"tripwire"` (its own
   category so the dedupe query in Design 4 is exact; the draft's
   `component openai` is retired), message EXACTLY
   `TRIP-1 {axis}: '{old}' -> '{new}'`
   (e.g. `TRIP-1 openai_primary_model: 'gpt-5.4-mini' -> 'gpt-5.6-luna'`)
   — this string is the dedupe key, so it is deterministic: no timestamps,
   no free text. `detail_json` = `{"axis", "old", "new",
   "reference_eval_id", "reference_created_at_utc"}`.
   Then ONE `alerts.send_alert` per scan covering ALL fired axes (both
   axes flipping at once is one operator event, not two pages), title
   `AlphaOS TRIP-1: evaluator identity changed`, priority `"high"` (same
   as CANARY Tier-1/2), `journal=` passed, message (plain ASCII, one
   transition line per fired axis):

   ```
   {axis}: {old} -> {new}   (last real eval under old identity: {eval_id} @ {created_at_utc})
   A model change is a strategy change (S9 ruling, 2026-07-26). Before trusting any new verdicts:
   1) replay the frozen corpus through old and new identities:
      alphaos ab_eval_run --arms {old_model}:{old_version} {new_model}:{new_version}
   2) log the keep/revert decision in the S9 decision log.
   Deliberate change? This page is the receipt -- confirm the decision row exists.
   Not deliberate? Revert .env, restart, and investigate what changed it.
   ```

   The `--arms` line interpolates the current old/new pair across both
   axes (unchanged axis uses its current value on both arms). Event
   inserts happen BEFORE the alert send, so a failed send never loses the
   audit row (test 13).

4. **Idempotency — exactly one alert per distinct transition, proven in
   both orderings.** Before firing an axis, dedupe against the latest
   tripwire event for that axis:
   `SELECT message FROM system_events WHERE category = 'tripwire' AND
   message LIKE 'TRIP-1 {axis}:%' ORDER BY id DESC LIMIT 1`
   — fire iff no row exists or the stored message differs from the
   candidate message.
   - **Ordering A (evals land):** scan N detects, fires, logs the event;
     scan N's own candidate loop then inserts rows under the new identity;
     scan N+1's reference row matches settings → no mismatch. One alert.
   - **Ordering B (no evals land** — `ai_degraded`, empty shortlist,
     labelling off, mock day**):** scan N fires; scan N+1 still
     mismatches, but the latest tripwire event for the axis equals the
     candidate message → suppressed: no event, no alert. Still one alert.
     This is the case the draft's "the most recent eval row moves"
     hand-wave got wrong.
   - **Flip-flop A→B→A→B:** each transition's message differs from the
     latest stored one for that axis, so each fires exactly once — a
     revert is its own page (correct: reverting is also a strategy
     change).
   - **Accepted quiet case:** change fires; operator reverts BEFORE any
     real eval lands under the new identity → settings match the untouched
     reference again → the revert itself is silent. Acceptable: no verdict
     was ever produced under the new identity, and the original page
     already covered the episode.
   Suppressed scans journal nothing — `system_events` stays clean; the
   mismatch state is re-derivable from the ledger at any time.

   > **STATUS CORRECTION (2026-07-28, audit-fixup after two independent
   > Opus audits both returned REQUEST CHANGES on the same convergent
   > root cause):** the dedupe reasoning above is WRONG, not just its
   > implementation. It considered the silent REVERT case (bullet 4
   > above) but never the RE-APPLY case, and never considered an
   > undelivered alert. Message-only dedupe has two real gaps, both
   > reproduced end-to-end against the build:
   > 1. **Re-apply is silently suppressed forever.** A message string can
   >    recur verbatim across two genuinely different reference rows —
   >    e.g. an operator trials a model on a day that produces zero real
   >    eval rows (mock trial / `ai_degraded` / empty shortlist / cost
   >    cap), gets paged, reverts; weeks later, after real evaluations
   >    under the OLD identity keep landing and a proper A/B + decision
   >    log ships the SAME model for real, the transition message is
   >    byte-identical to the first page even though the reference row
   >    has moved on. Message-only dedupe suppresses this second, REAL
   >    transition forever — reproduced with 33 real verdicts landing
   >    unpaged, via exactly the workflow a careful operator uses.
   > 2. **An undelivered page is burned.** `alerts.send_alert` returns a
   >    bool and never raises: it returns `False` silently when
   >    `NTFY_TOPIC` is unset (no log at all), and `False` + a WARNING
   >    log on any network/HTTP failure. The original design journaled
   >    the dedupe row unconditionally, discarding that return value —
   >    one ntfy blip on the single scan a transition occurs burns the
   >    transition's only page forever, with no record it was ever
   >    undelivered.
   >
   > **Corrected dedupe condition:** fire iff no prior tripwire event for
   > this axis has ALL THREE of — (a) the identical candidate message,
   > (b) the identical `reference_eval_id` (already present in
   > `detail_json`, no new data needed), AND (c) `alert_sent is True` in
   > that event's `detail_json`. `alert_sent` is written `False` at
   > insert time (event inserts still happen strictly before the alert
   > send — durability ordering unchanged) and flipped to `True` in a
   > follow-up `UPDATE` only once `alerts.send_alert`'s return value
   > confirms delivery — the same insert-then-update-on-success pattern
   > `alphaos/cards/demotion.py` already uses for its own `alert_sent`
   > column. This also closes a third gap (found independently by the
   > second audit): per-axis events are journaled inside the mismatch
   > loop, before the single shared alert send; if an exception unwinds
   > between one axis's event insert and the send being reached, that
   > axis's row is left with `alert_sent: False` by construction, so the
   > next scan retries it rather than an orphaned, never-alerted event
   > being mistaken for "handled." The dedupe lookup itself also moved
   > from a SQL `LIKE 'TRIP-1 {axis}:%'` pattern to an exact-prefix
   > `substr(message, 1, N) = ?` comparison — `_` is a LIKE wildcard and
   > both current axis names contain underscores; today's two names
   > cannot cross-match, but the spec's own maintenance trigger (Scope
   > rule, above) says a third axis joins later, and a LIKE pattern would
   > silently mismatch it. Implementation: `alphaos/tripwire.py`; tests:
   > `tests/test_trip1_evaluator_tripwire.py`'s `test_fix1a_*`,
   > `test_fix1b_*`, `test_fix1c_*`, `test_fix1_delivered_alert_*`, and
   > `test_fix5_*`.
   >
   > **Severity also corrected 2026-07-28 (MUST FIX 2):** the mismatch
   > event's severity is `ERROR`, not `WARNING` — the daily digest
   > (`scheduler/digest.py:141`) only surfaces `system_events` rows at
   > severity `error`/`critical`, so a WARNING-only event was invisible
   > everywhere except a direct ntfy push, a single point of failure.
   > Verified before raising it that this cannot trip anything: the only
   > self-halt mechanism in this codebase, `scheduler/cadence.py::
   > is_fused`, counts consecutive `job_runs.status == 'failed'` rows and
   > never reads `system_events` at all.
   >
   > **Edge case added (A LOW-3): the kill-switch window is BLIND.**
   > Bullet 3 above (Zero-eval day) lists "kill switch" alongside
   > `ai_degraded`/empty shortlist/non-trading day as a case where "the
   > check still runs" against a stale-but-true reference. That is
   > WRONG for the kill switch specifically: `scheduler/jobs.py:104`
   > skips `run_scan_once` — and therefore `check_evaluator_identity`
   > itself — ENTIRELY while the switch is engaged, so an identity change
   > made during that window is not paged until the switch is released
   > and a scan actually runs. This is defensible (no verdicts are
   > produced during the window either, so there is nothing yet to
   > protect), but the spec text as originally written implied the check
   > runs regardless, which it does not.

5. **Intentional changes: NO acknowledgment mechanism — decided, with
   reasons.** TRIP-1 fires identically on deliberate and accidental
   changes, exactly once per transition. Why this is right and not alert
   fatigue:
   - Desensitization needs repetition. Design 4 caps a transition at ONE
     page, and identity transitions are rare — four in the entire
     production ledger (mini→luna, v1→v2, v2→v3, and the ratified swap
     itself). One page per rare event cannot train anyone to ignore it.
   - Every candidate ack mechanism (an ack CLI, an env ack token, an
     expected-identity file) is performable by an automated session — and
     an ack channel a session can operate is a tripwire a session can
     disarm, recreating the exact blind spot this ticket closes. The only
     genuinely operator-only channel this project has is the human reading
     the page, which is what this design uses.
   - The page on a deliberate cutover is positive value, not noise: it is
     a free end-to-end self-test of the tripwire on every real cutover
     (the v2 and v3 cutovers would each have produced exactly one
     confirmation receipt), and house doctrine is explicitly ping-over-
     silence until reliability is proven. The alert text (Design 3) does
     the disambiguation an ack mechanism would have done: deliberate →
     receipt, confirm the decision row; not deliberate → revert and
     investigate.

6. **Fail-open containment (named pattern).** The ENTIRE check body —
   reference read, dedupe reads, event inserts, alert send — is wrapped in
   one try/except at the `check_evaluator_identity` boundary: any
   exception is journaled (`Severity.ERROR`, category `"tripwire"`,
   message `TRIP-1 check failed: {exc} -- scan continues`) and swallowed;
   nothing ever propagates to `run_scan_once`. This is the established
   auxiliary-read containment pattern (the INSTR-2 audit-HIGH fix in
   `post_process`; INSTR-3's augment-time read) combined with §9's
   per-item-isolation law: wrap the WHOLE body, not just the step that
   looks riskiest — the dedupe `LIKE` or the event insert can raise as
   legitimately as the reference read. Belt-and-suspenders on alerting per
   `alerts.py`'s own contract: `send_alert` never raises AND the wrapper
   would swallow it anyway. A failure while journaling the failure itself
   is also swallowed (same best-effort stance as `send_alert`'s own
   failure log).

### Tests (hermetic; §H.1 discipline — direct construction; seed `openai_evaluations` reference rows by direct insert)
1. Model-axis mismatch (seed real row `model='gpt-5.4-mini'`, settings
   `gpt-5.6-luna`): exactly one `system_events` row (WARNING, category
   `tripwire`, message exactly
   `TRIP-1 openai_primary_model: 'gpt-5.4-mini' -> 'gpt-5.6-luna'`) and
   exactly one alert whose body contains the transition line, the
   `ab_eval_run --arms` instruction, and both the receipt and revert
   sentences.
2. Prompt-axis mismatch alone (v2→v3 shape) fires with message
   `TRIP-1 openai_prompt_version: 'v2' -> 'v3'`; model axis silent.
3. Both axes mismatch in one scan → two `system_events` rows (one per
   axis), ONE alert naming both transitions.
4. Match on both axes → no event, no alert, `fired == []`.
5. Empty ledger (no rows at all) → silent no-op.
6. Only mock rows (`is_mock=1`, `model='mock'`) → silent no-op (never a
   `'mock'`→real page).
7. `NULL` `prompt_template_version` on the reference row → model axis
   still checked and can fire; prompt axis no-ops; no exception.
8. Ordering A self-heal: fire once, insert a real row under the new
   identity, re-run → silent (no second event, no second alert).
9. Ordering B dedupe: fire once, insert NO new rows, re-run → suppressed
   (no second event, no second alert; `suppressed` lists the axis).
10. Flip-flop: A→B fires; seed a real row under B; B→A fires; seed under
    A; A→B fires AGAIN — three events, three alerts total.
11. Reference ordering: two real rows sharing one `created_at_utc`, the
    higher `id` carrying the currently configured identity → no fire
    (proves `id DESC`, not `created_at_utc`).
12. Fail-open: monkeypatch the reference read (`journal.one`) to raise →
    ERROR event journaled (category `tripwire`), `run_scan_once` completes
    normally, no exception propagates.
13. Alert-failure isolation: monkeypatch `alerts.send_alert` to raise →
    swallowed, and the per-axis WARNING event STILL exists (event insert
    precedes alert send).
14. Placement structural test (house AST / call-order pattern): in
    `run_scan_once`, `check_evaluator_identity` is invoked before the
    scan's first `openai_evaluations` insert.
15. Zero decision surface: scan output (candidates, proposals,
    `ScanSummary` fields) is identical with the tripwire firing vs.
    silent — same direct-comparison proof REG-1's scope audit used.

---

## INSTR-2 — ATR-coherent evaluator targets (prompt v2, live-path, gated)

REFINED 2026-07-23 against (a) the post-AB-EVAL-1 code state of
`alphaos/ai/openai_client.py` (the `evaluate()` → `raw_evaluate()` /
`post_process()` refactor) and (b) the first REAL AB-EVAL-1 replay run.
The 2026-07-20 draft's "bump the version literal, merge dark" mechanics
were unbuildable as written and are replaced by a settings axis (Design 1).

### Real evidence baseline (AB-EVAL-1 run `abrun_513b8d9441ad`, 2026-07-21)
60 identical frozen packets replayed through both models under prompt v1:

| | raw propose | raw watch | raw reject | RR_FLOOR on raw proposes | final proposes |
|---|---|---|---|---|---|
| gpt-5.4-mini | 40 | 20 | 0 | **40/40 (100%)** | 0 |
| gpt-5.6-luna | 7 | 17 | 36 | **7/7 (100%)** | 0 |

The floor mismatch kills 100% of raw proposes under BOTH models — the
defect is **model-independent**, exactly the draft's premise, now with a
real number attached (the draft's "~100%" estimate is retired; all
acceptance criteria below reference this run). Consequence: the proof gate
tests prompt v2 under BOTH models, so the operator's still-open
keep-vs-revert model decision stays orthogonal to this ticket — whichever
model survives that ruling, INSTR-2 must be proven to fix it.

### Goal
Make target-setting and stop-setting share the ATR risk scale, so the 1.2
floor measures real trade geometry instead of a units mismatch. **This is a
coherence fix, not a loosening: the floor stays 1.2 and the deterministic
2.0×ATR(14) stop override stays exactly as shipped.**

### Non-goals (re-verified against current code, 2026-07-23)
- No change to `MIN_REWARD_RISK` (`settings.min_reward_risk`, default 1.2,
  `alphaos/config/settings.py`) or `ATR_STOP_MULTIPLIER_V1` (= 2.0,
  `alphaos/data/atr.py` — relocated there 2026-07-09; `openai_client.py`
  re-exports it).
- No change to `_apply_atr_stop` / `_enforce_min_reward_risk` semantics
  (defense in depth: the override remains even when the model cooperates).
  Post-refactor these run inside **`OpenAIClient.post_process()`** (not
  inline in `evaluate()` as the original draft said): live-path-gated ATR
  override, then the floor, in that order. `post_process()`'s try/except
  around `_apply_atr_stop` is an audit-HIGH behavior (2026-07-20): a
  genuine raise (e.g. transient SQLite error on the `atr_history` read) is
  contained to a journaled ERROR + safe `OPENAI_REJECT` rejection and must
  NEVER abort the caller's scan loop. **The INSTR-2 diff must preserve this
  containment** (test 11 below pins it).
- No change to the `NO_ATR_DATA` fail-safe reject.
- No response-schema change (the model still returns a stop; it becomes
  advisory). `NO_NEWS_EVAL_KEYS` unchanged; `structured_json.require_keys`
  unchanged.
- No prompt change for the mock path (`_mock_eval` never builds a prompt;
  hundreds of existing tests depend on its deterministic stop/target math).

### Design

1. **Version mechanics: a settings axis, not a literal bump.** The draft's
   "bump `prompt_template_version` literal … version literal unused until
   cutover" is factually wrong against the code: `to_row()` stamps
   `pt.PROMPT_TEMPLATE_VERSION` into every `openai_evaluations` row today,
   so editing the literal is a live behavior change at merge, not a dark
   one. Instead:
   - New frozen-settings field `openai_prompt_version: str`, env
     `OPENAI_PROMPT_VERSION`, default `"v1"`, validated against
     `{"v1", "v2"}` (`SettingsError` otherwise — same pattern as
     `MIN_REWARD_RISK` validation).
   - `OpenAIEvaluation` gains field `prompt_template_version: str = "v1"`;
     `evaluate()` stamps it from settings on every path (mock / live /
     rejection — stamped LAST alongside the existing snapshot stamp, for
     the same reason: `post_process()` can swap in a new rejection object);
     `to_row()` reads the instance field instead of the module literal.
     Direct-constructed test objects keep working via the default.
   - Merge dark is now genuinely dark: with the default `"v1"`, the built
     user prompt is **byte-identical** to today's (golden test 2 below).
   - Cutover = operator sets `OPENAI_PROMPT_VERSION=v2` in `.env`, logged
     as a decision row; the daily brief notes the cutover date so
     downstream sample-floor accounting can split on the new ruler.

2. **Surface ATR into the prompt — without touching the v1 bytes.**
   `build_no_news_user_prompt` serializes the WHOLE snapshot dict into the
   `MARKET_SNAPSHOT:` section, so naively adding `atr_14` to the snapshot
   would change v1 prompts too. Exact change instead:
   - Extract the `atr_history` lookup in `_apply_atr_stop` (the
     `SELECT atr_14 FROM atr_history WHERE symbol = ? AND rules_version = ?
     ORDER BY market_date DESC LIMIT 1` scalar) into a module-level helper
     `_latest_atr(journal, symbol)`; `_apply_atr_stop` calls it — same SQL,
     same semantics, one query definition so prompt and enforcement can
     never read different sources.
   - New method `OpenAIClient._augment_snapshot_for_prompt(snapshot,
     candidate)`: returns the snapshot UNCHANGED unless (`not use_mock` AND
     `settings.openai_prompt_version == "v2"`). When active, returns a
     **copy** with key `"atr_policy"` set to
     `{"atr_14", "stop_multiplier" (= ATR_STOP_MULTIPLIER_V1),
     "risk_per_share" (= stop_multiplier × atr_14, 4 dp),
     "min_reward_risk" (= settings.min_reward_risk),
     "min_target_distance" (= min_reward_risk × risk_per_share, 4 dp),
     "rules_version" (= ATR_RULES_V1)}` — always recomputed fresh,
     overwriting any pre-existing `"atr_policy"` key (a replayed
     v2-era fixture must never smuggle a stale archived block past the
     current `atr_history` state that `_apply_atr_stop` will enforce
     against). No ATR available (`None`/`<= 0`) ⇒ no key added ⇒ the
     unchanged `NO_ATR_DATA` fail-safe handles any raw propose. The ATR
     read here is wrapped in its own try/except (journal ERROR, proceed
     without the block) — this call sits OUTSIDE `raw_evaluate()`'s and
     `post_process()`'s containment, and a transient DB error must degrade
     to a v1-shaped prompt, never abort the scan loop.
   - `evaluate()` becomes: `snapshot = self._augment_snapshot_for_prompt(
     snapshot, candidate)` → `raw_evaluate` → `post_process` → stamp
     `evaluation.snapshot = snapshot` (the augmented copy — so v2-era
     `snapshot_json` archives exactly what the model was shown;
     replayability test 8).
   - `build_no_news_user_prompt(candidate, snapshot, freshness_status)`
     gains keyword `atr_policy: Optional[dict] = None` and, always,
     `pop`s `"atr_policy"` out of the dict it serializes as
     `MARKET_SNAPSHOT` (hygiene for the SERIALIZED JSON text: a v1 replay
     arm over a future v2-era fixture must not leak the archived block's
     raw key/values into the MARKET_SNAPSHOT section; a no-op on all
     present-day data). **This pop does NOT by itself gate whether the
     ATR_STOP_POLICY section renders** — that section is driven entirely by
     the `atr_policy` keyword argument the caller passes in, independent of
     what the pop leaves behind in the dict. Correcting the 2026-07-23
     draft's own claim here (audit-caught MEDIUM, 2026-07-23 fixup): `_live_
     eval` must NOT pass `atr_policy=snapshot.get("atr_policy")`
     unconditionally — a snapshot dict can already carry a stale
     `"atr_policy"` key (e.g. a real v2-era `snapshot_json` later replayed
     as an AB-EVAL-1 fixture under a v1 control arm; `_augment_snapshot_
     for_prompt`'s own v1/mock no-op deliberately returns `snapshot`
     UNCHANGED, never stripping a pre-existing key), and reading it
     unconditionally would render the full v2 section into a supposedly
     byte-identical v1 prompt. The correct, shipped gate: `_live_eval`
     passes `atr_policy=snapshot.get("atr_policy") if self.settings.
     openai_prompt_version == "v2" else None` — gated on the ACTIVE
     settings version, never on the snapshot dict's own contents.
     `atr_policy=None` (the only value possible under v1) renders a prompt
     byte-identical to today's v1. This keeps `raw_evaluate` → `_live_eval`
     as the only route to the one production prompt builder (existing AST
     test `test_live_eval_still_calls_the_one_production_prompt_builder_ast`
     stays green unmodified).
   - Note on the draft's "one fetch, shared — do not query twice": post-
     refactor, prompt-build and enforcement are separately invokable (the
     replay harness depends on that), so the shared thing is the **query
     definition** (`_latest_atr`), executed once at augment time and once
     in `_apply_atr_stop` — two scalar reads on the same connection against
     a table only the nightly `run_atr_update_job` writes, so they cannot
     disagree within one evaluation. Threading a single cached value
     through `post_process` would change `_apply_atr_stop`'s call shape —
     explicitly a non-goal.

3. **Prompt v2 wording (exact).** When `atr_policy` is present the builder
   inserts the following section between `MARKET_SNAPSHOT` and
   `DATA_FRESHNESS` (values interpolated from the block; floor and
   multiplier are NEVER hard-coded in the template — render
   `min_reward_risk` and `stop_multiplier` so the prompt cannot lie if an
   operator ever changes config):

   ```
   ATR_STOP_POLICY:
   After your evaluation, this system will REPLACE your returned stop with
   a deterministic one before any trade is considered:
   - long:  stop = entry - {stop_multiplier} x ATR(14)
   - short: stop = entry + {stop_multiplier} x ATR(14)
   For this symbol ATR(14) = {atr_14}, so risk per share is fixed at
   {stop_multiplier} x {atr_14} = {risk_per_share} regardless of the stop
   you return. reward:risk is then recomputed as
   |target - entry| / {risk_per_share} and the evaluation is automatically
   rejected if that value is below {min_reward_risk}. A surviving target
   therefore sits at least {min_target_distance} from entry on the profit
   side. The displayed figures are rounded; the recomputation uses exact
   values — a target placed exactly at the minimum distance can fail on
   rounding, so leave margin above it rather than placing targets at the
   boundary. Your returned stop is still required by the schema and is
   recorded, but it is advisory only. This policy does not make proposing
   more or less desirable; it only tells you the geometry any target will
   be judged against — apply your usual evidence standards unchanged.
   Worked example (long):  entry 100.00, ATR(14) 2.50 -> enforced stop
   95.00, risk per share 5.00; target 106.00 computes 6.00/5.00 = 1.20.
   Worked example (short): entry 100.00, ATR(14) 2.50 -> enforced stop
   105.00, risk per share 5.00; target 94.00 computes 6.00/5.00 = 1.20.
   ```

   Framing rules (unchanged from draft, made testable): the section states
   mechanism and arithmetic only. It MUST NOT instruct the model to prefer
   any decision, mention the drought, or say "propose more" — the one
   permitted normative clause is the anti-boundary-rounding sentence and
   the explicit "does not make proposing more or less desirable"
   disclaimer. Everything else in the v1 prompt (system prompt, schema,
   rules line, sentinels) is byte-identical in v2.

4. **Harness extension: prompt-version arms.** The shipped harness
   parameterizes ONLY the model (`replay_packet` does
   `dataclasses.replace(settings, openai_primary_model=model)`), so the
   proof gate below needs a second axis:
   - An arm is `(model, prompt_version)`. `replay_packet(fixture, arm, …)`
     does `replace(settings, openai_primary_model=model,
     openai_prompt_version=version)`. Replay mirrors `evaluate()`'s
     three-step sequence: `_augment_snapshot_for_prompt` (a no-op for v1
     arms, and per-arm copy semantics mean arms can never contaminate each
     other's view of the shared fixture dict) → `raw_evaluate` →
     `post_process` (still on a `copy.copy` of the raw verdict). Update the
     two AST structural tests
     (`test_replay_routes_through_production_raw_evaluate_and_post_process_ast`,
     `test_evaluate_calls_raw_evaluate_then_post_process_in_order_ast`) to
     pin the three-step order in BOTH places; `replay.py` must still import
     no prompt-building code.
   - CLI: `ab_eval_run` gains `--arms MODEL:PROMPT_VERSION …` (mutually
     exclusive with the existing `--models`, which remains as sugar for
     `MODEL:<configured openai_prompt_version>`). Dedupe preserves order
     over `(model, version)` pairs; ≥2 distinct arms required; cost
     pre-flight `planned_calls = n_packets × n_arms`.
   - Storage (additive; `SCHEMA_VERSION` stays 3 — same law AB-EVAL-1's own
     audits established: `schema.py`'s governing comment says additive
     column changes never bump it, `JournalStore._migrate` reconciles them
     automatically, and existing tests assert `SCHEMA_VERSION == 3`):
     `ab_eval_results` gains `prompt_version TEXT`; `ab_eval_runs` gains
     `arms_json TEXT` (`models_json` stays populated for old readers).
     Report (`alphaos/reports/ab_eval_report.py`) groups by arm, not model.

5. **Pre-cutover proof gate (pre-registered here; operator or builder runs
   it AFTER merge-dark, BEFORE any cutover).** One four-arm run over the
   SAME frozen corpus as `abrun_513b8d9441ad` (do NOT rebuild the corpus —
   rebuilding changes what the baseline anchors to):

   ```
   python -m alphaos ab_eval_run \
     --arms gpt-5.4-mini:v1 gpt-5.6-luna:v1 gpt-5.4-mini:v2 gpt-5.6-luna:v2
   python -m alphaos ab_eval_status          # or --run-id abrun_<new>
   ```

   240 real calls (60 × 4) — confirm 30-day cost-cap headroom first (the
   run refuses on its own pre-flight otherwise). Fresh v1 arms are included
   deliberately: LLM calls are nondeterministic and the provider can drift,
   so the within-run v1 arms are the concurrent control, with
   `abrun_513b8d9441ad` as the pre-registered anchor. Gates, all evaluated
   per model:
   - **P0 (run validity):** each fresh v1 arm reproduces an `RR_FLOOR`
     downgrade rate on raw proposes ≥ 90% (anchor: 100% both models,
     2026-07-21). Below that, something upstream changed — investigate;
     the run is not interpretable and no other gate may be read.
   - **P1 (primary):** under v2, `RR_FLOOR` downgrade rate on raw proposes
     **< 20%** for EACH of mini and luna (real baseline: 100% and 100%).
     Denominator guard: a model with < 5 raw proposes under v2 makes P1
     INDETERMINATE for that model, not passed — investigate, do not cut
     over on a vacuous denominator.
   - **P2 (anti-coaching):** per model, packets flipping that model's own
     fresh-v1-arm raw `reject` → v2 raw `propose` ≤ 10% of that model's
     v1 rejects (anchor: luna 36 rejects ⇒ ≤ 3 flips at baseline counts;
     mini had 0 rejects ⇒ vacuous unless its fresh v1 arm differs). Every
     flipped packet is listed in the report with both reasoning summaries;
     **any** flip whose v2 reasoning cites the stop policy as an
     affirmative reason to propose fails P2 regardless of count. (The
     draft's "packets BOTH models raw-rejected" formulation is retired —
     the real run shows mini rejected 0 packets, making the intersection
     empty and that check permanently vacuous.)
   - **P3 (fail-safe untouched):** `NO_ATR` downgrade counts identical
     between each model's v1 and v2 arms (baseline: 0 — every 2026-07-21
     downgrade was `RR_FLOOR`).
   - **P4 (integrity):** `n_corpus_errors = 0` and 240 result rows.
   Any P-gate failure ⇒ revise wording (or investigate), re-run the full
   four arms; do not cut over. Passing all gates ⇒ the report goes to the
   operator; **cutover remains a separate explicit operator instruction**
   (decision row, per Design 1), and stays sequenced AFTER the operator's
   keep-vs-revert model ruling from the 2026-07-21 report.

### Tests (hermetic; §H.1 discipline — date-seeded mocks, direct construction)
1. Builder v2 long/short: with an `atr_policy` block, the rendered section
   contains the interpolated `atr_14` / `risk_per_share` /
   `min_target_distance` values and both worked examples; long and short
   formula lines both present.
2. **v1 golden test:** default settings ⇒ snapshot not augmented and
   `build_no_news_user_prompt` output byte-identical to the pre-INSTR-2
   prompt for a fixed candidate/snapshot (the merge-dark guarantee).
3. Builder pops `"atr_policy"` from the serialized `MARKET_SNAPSHOT` in
   both versions (feed a snapshot containing the key with
   `atr_policy=None` and assert the key's values appear nowhere).
4. No hard-coded policy numbers: rendering with `min_reward_risk=1.5` /
   `stop_multiplier=3.0` in the block shows 1.5/3.0 (the template
   interpolates config, never literals).
5. Missing ATR under v2: no block, prompt renders v1-shaped, and a raw
   propose is still rejected `NO_ATR_DATA` by the unchanged fail-safe.
6. Augment-time ATR read raising ⇒ journaled ERROR, prompt built without
   the block, scan loop never sees the exception.
7. `prompt_template_version` stamping: rows journal the ACTIVE settings
   version on every path (mock, live, post_process rejection); default
   field value keeps direct-constructed fixtures at "v1".
8. Snapshot journaling: under v2 the journaled `snapshot_json` contains
   the `atr_policy` block shown to the model; under v1 it does not.
9. Settings validation: `OPENAI_PROMPT_VERSION=v3` ⇒ `SettingsError`;
   unset ⇒ `"v1"`.
10. Structural: live scan path still applies `_apply_atr_stop` after a v2
    evaluation (the override is not conditionally skipped on prompt
    version), and `_latest_atr` is the single ATR-lookup site for both
    augment and override (AST).
11. Containment preserved: monkeypatch `journal.scalar` to raise inside
    `post_process` under v2 ⇒ journaled ERROR + safe `OPENAI_REJECT`
    rejection, never a propagated exception (the existing
    `test_evaluate_contains_atr_read_exception_as_safe_reject` must stay
    green through the diff).
12. Coherence: with seeded `atr_history`, the stop arithmetic implied by
    the prompt block (`entry − stop_multiplier × atr_14`) equals
    `_apply_atr_stop`'s enforced stop for the same entry (shared-source
    guarantee, exercised end-to-end).
13. Harness arms: `--arms` parsing; `(model, version)` dedupe; refusal on
    < 2 distinct arms; `planned_calls = packets × arms` in the cost
    pre-flight; `ab_eval_results.prompt_version` populated; a v1 arm and a
    v2 arm replaying the SAME fixture object see uncontaminated inputs
    (order-independence of arms).
14. Updated AST tests per Design 4 (three-step mirror pinned in
    `evaluate()` and `replay_packet`; `replay.py` still imports no
    prompt-building code).

---

## Explicitly out of scope for all three tickets
- Any change to liquidity/spread/freshness gates, position caps, or risk
  sizing — the ledger shows they are not the constraint.
- CF-1 (counterfactual gate ledger) — stays on the throughput agenda as the
  durable per-gate instrument; AB-EVAL-1 is the targeted, cheap version for
  this specific incident and does not replace it.
- The TIER_2 canary adjudication (operator, separate protocol).
- EXP-1 arming — its own gate; unaffected by this file.
