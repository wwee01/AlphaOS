# HOLD-2 — Extend the live holding window to 10 trading days (card v3 + prompt v4)

**Status:** SPEC — build not started. Fable strategist spec, 2026-08-05.
**Ceremony:** Sonnet builder in an isolated worktree, two independent blind Opus audits, merge only on explicit operator instruction.
**Operator rulings already made (CK, 2026-08-05, logged in §9):**
- **D1 — extend the holding window 3 → 10 TRADING days** (acknowledged as a 3.3× extension of the live `max_holding_days_default: 3`, not "5→10").
- **D2 — BASELINE "third option": pin the existing baseline arms at 3 days (protect the 2026-09-07 pre-registered analysis) AND register a NEW deterministic 10-day baseline arm as a fresh pre-registration** (decision-log law from 2026-07: "v2+ arms are new pre-registrations").

---

## 1. Motivating evidence (HOLD-1, pre-registered 2026-07-24, floor MET 2026-08-05)

Among setups that had NOT reached the 2.4×ATR minimum target distance by day 5
(n=57 scoped vs the ≥30 floor): **proposed 1/4 (25%), watch 16/49 (33%),
rejected 8/20 (40%) reached it by day 10.** Roughly a third of day-5 "failures"
complete in days 6–10 — the geometry argument made quantitative: a 2.4×ATR
minimum target at ~0.3–0.5 ATR/day of net trend progress needs 5–8+ sessions;
a 3-day ceiling structurally starves otherwise-valid targets of time.

Carried caveats (must survive into the card notes and all reporting): the
proposed cohort is n=4 (the signal lives in the watch cohort); the REJECTED
cohort completed most often (40%), so a longer window also helps trades the
evaluator declined — this change makes the geometry reachable, it does not
promise better expectancy. That question resolves from post-v3 outcomes
segmented by card version.

## 2. The discovered blast radius (why this is a ticket, not a YAML edit)

1. **The evaluator prompt hard-codes the horizon** — `prompt_templates.py`
   system prompts say "swing horizon 1-5 trading days" (~lines 81, 92) and the
   response schemas say `"max_holding_days": "integer 1-5"` (~lines 263, 309).
   The AI cannot choose >5 days today regardless of any card. This text is
   SHARED across prompt versions, and v1/v2 are frozen replay control arms
   guarded by byte-identity golden tests — it cannot be edited in place.
2. **BASELINE reads the live default card** — `baseline/tracker.py`
   (~lines 52–70) calls `get_default_card()` for `max_holding_days_default`.
   Swapping the default card would silently change the frozen reference arm
   mid-accumulation (its pre-registration has `analysis_not_before 2026-09-07`).
3. **`DEFAULT_CARD_ID` is a code constant** in `cards/registry.py`
   (INSTR-1 precedent: new card file + constant swap).
4. **Mock evaluator hard-codes `max_holding_days=3`** (`openai_client.py` ~578).
5. **Earnings hold-window widens automatically** — `earnings_enricher` looks
   `max_holding_days` trading days ahead; at 10 more candidates carry earnings
   flags (conservative direction, intended, but visible).
6. Already correct, NO change needed: `position_manager` time-exit counts
   TRADING days from `proposal.max_holding_days`; GTC protective legs.

## 3. Design

### 3.1 Card `catalyst_momentum_v3` (drafted, rides the build branch)

Byte-identical to v2 except: `card_id: catalyst_momentum_v3`, `version: 1`,
`max_holding_days_default: 10`, and a `notes:` block carrying the full HOLD-1
evidence + caveats + operator ruling. Draft exists (session scratchpad —
deliberately NOT in the live checkout, because `orchestrator.py:181` syncs
card files from disk every startup and the card law requires operator commit).
The builder places it in `alphaos/cards/`; **CK's merge instruction is the
operator commit** (INSTR-1 precedent). v2 stays registered and unchanged
forever (append-only registry, Prime Directive 7).

### 3.2 New setting `ACTIVE_CARD_ID` (card selection becomes an operator config axis)

- New `.env` setting, **default `catalyst_momentum_v2`** → the merge is DARK:
  merged code with an unchanged `.env` behaves byte-for-byte like today.
- Validated at settings load against the on-disk card registry: unknown id →
  hard `SettingsError` at startup, never a silent fallback.
- `get_default_card()` resolves through it (builder threads settings through
  the call sites; the `DEFAULT_CARD_ID` constant remains as the default value
  of the setting, single-sourced — no duplicated literal).
- **Joins the config-fingerprint safe snapshot** (same rationale as
  `OPENAI_PROMPT_VERSION` when it was added).
- NOT a TRIP-1 axis for now: it fails TRIP-1's criterion (b) — it is not
  stamped per-row into `openai_evaluations`. But note for TRIP-1 maintenance:
  under prompt v4 the active card changes prompt content; `prompt_hash` (already
  stamped per evaluation) is the existing witness. Documented, out of scope.

### 3.3 Prompt v4 — the horizon becomes card-interpolated (the INSTR-2 cure)

- `PROMPT_VERSIONS` gains `"v4"` in `alphaos/constants.py` (single source;
  allowlist, ab-eval arms CLI, and TRIP-1 vocabulary follow automatically —
  verify via the existing lockstep tests).
- v4 = v3 (ATR_STOP_POLICY + MULTI_DAY_CONTEXT retained; membership gates
  currently `in ("v2","v3")` extend to include `"v4"` — lockstep tests updated)
  **plus**: every horizon occurrence is interpolated from the ACTIVE card's
  `max_holding_days_default` at build time — system prompt "swing horizon
  1-{N} trading days" and schema `"max_holding_days": "integer 1-{N}"`.
  No hardcoded policy number survives (the prompt-must-not-lie law).
- Response validation: the parser's accepted range for `max_holding_days`
  becomes 1..N from the same card value — not a second hardcoded bound.
- **v1, v2, v3 stay byte-identical.** v3 gains its own golden byte-identity
  test in this build (it becomes a frozen control arm the moment v4 goes live).
  v4's interpolation gets a dedicated test proving single-sourcing: active card
  v2 → "1-3", active card v3 → "1-10". (Note: v4-with-card-v2 honestly renders
  "1-3", exposing that today's "1-5" prompt never matched the card's 3 — the
  interpolation FIXES a pre-existing incoherence rather than preserving it.)
- Mock evaluator's `max_holding_days` comes from the active card too (was 3);
  affected test fixtures updated.

### 3.4 BASELINE — pin v1 arms, add the 10-day arm (operator D2)

- **Pin:** `baseline/tracker.py` stops calling `get_default_card()` for the
  hold window and reads `catalyst_momentum_v2` BY EXPLICIT ID, with a comment
  citing this spec + the §9 ruling. Test: swapping `ACTIVE_CARD_ID` does NOT
  change the v1 arms' hold days.
- **New arm:** the tracker additionally records, per candidate, the same
  deterministic decisions under a 10-day hold (`catalyst_momentum_v3` by id),
  labelled as a distinct arm (additive storage only — follow the repo's
  existing migration pattern; no destructive schema change). Deterministic,
  zero API cost.
- **Fresh pre-registration** (operator action post-merge, like
  `baseline_register` on 2026-07-09): same claim structure as BASELINE v1 at
  the 10-day horizon, `analysis_not_before` **2026-10-05** (2-month
  convention). The 10-day arm's rows accumulate from merge; its analysis gate
  is the registration, not the rows.

### 3.5 Proof gate (pre-registered here, run post-merge, BEFORE cutover)

AB-EVAL 2-arm replay on the frozen corpus (60 packets): `luna:v3` vs
`luna:v4` (~120 calls against the 5000 cap). The v4 arm must interpolate the
10-day horizon, so the gate run sets `ACTIVE_CARD_ID=catalyst_momentum_v3`
**as a process-level env override on the replay command only** — replay makes
no orders; the live scheduler keeps reading the unchanged `.env`. The run
report must record the active card id used (small provenance addition to the
ab-eval run metadata).

- **Q0 (validity):** the v3 arm reproduces the v3 anchor behavior
  (`abrun_3ac89bac0ca5`: 3/60 proposes); material deviation invalidates the
  run (current-state bars/ATR caveat acknowledged as before).
- **Q1 (primary, information-landing):** rejections citing
  horizon-reachability (pre-registered patterns: "swing horizon",
  "holding period", "within \\d+ (trading )?days", "% advance") drop
  materially in the v4 arm.
- **Q2:** propose-rate movement REPORTED, never targeted.
- **Q3 (anti-coaching flip audit):** every v3-reject → v4-propose flip must
  cite the longer horizon making a SPECIFIC target reachable; a flip with
  generic-optimism rationale fails the gate.
- **Q4 (mechanical):** 0 errors, all 120 rows present.

### 3.6 Cutover ceremony (operator, after the gate is reviewed)

ONE `.env` edit, both axes together, dated comment, §9 row:
`OPENAI_PROMPT_VERSION=v4` **and** `ACTIVE_CARD_ID=catalyst_momentum_v3`.
From the next tick: prompt says 1-10 (from the card), proposals stamp card v3,
time-exit enforces the AI's chosen ≤10, earnings window looks 10 days out,
BASELINE v1 arms stay at 3, baseline-10 arm runs at 10. No state ever exists
where the card and the prompt disagree about the horizon.

## 4. Non-goals / out of scope

- No selector / S1c / PER changes; no risk-engine or sizing changes.
- No change to HOLD-1's shadow outcome machinery (already 10-day by
  pre-registration — now conveniently matched).
- No same-tier TIER_2 suspend policy (separate open operator question).
- No deletion or mutation of v1/v2 cards or v1–v3 prompts.

## 5. Test obligations (minimum)

1. Golden byte-identity: v1, v2, and NEW v3 goldens all pass.
2. v4 interpolation single-sourcing (card v2 → "1-3"; card v3 → "1-10";
   schema bound follows the same value).
3. Parser accepts `max_holding_days=10` under v4+card-v3; rejects >N.
4. `ACTIVE_CARD_ID` validation: unknown id → SettingsError at load.
5. Config fingerprint includes `ACTIVE_CARD_ID`.
6. BASELINE pin: `ACTIVE_CARD_ID` swap does not move v1 arms' hold days.
7. Baseline-10 arm rows written alongside v1 arms, correctly labelled.
8. Mock eval hold days follow the active card.
9. Membership-gate + PROMPT_VERSIONS lockstep tests cover v4.
10. Cold-import subprocess test stays green (SUSP-1 lesson).
11. Card v3 registry sync: registers append-only, refuses content-hash
    mutation, `get_default_card()` honors the setting.

## 6. Operator actions (in order)

1. Merge on explicit instruction (= operator commit of card v3).
2. Run the proof gate (env-override command above; ~120 calls).
3. Review gate, then cutover `.env` (§3.6) + §9 row.
4. Register the baseline-10 pre-registration (CLI, `analysis_not_before`
   2026-10-05) + §9 row.
5. Expect: more earnings-flagged candidates; slower observation turnover on
   the 5-slot book (accepted in D1).

---

## STATUS CORRECTION (2026-08-06, after audit round 1 — Fable strategist)

Two independent blind Opus audits of build `1a627c8` (correctness lens; scope/safety
lens) both returned REQUEST-CHANGES and **converged on the same BLOCKER**. Several
findings trace to errors in THIS SPEC, corrected here. Where the text below
contradicts the sections above, this correction governs.

1. **§2/§4 "out of scope: selector/S1c/PER" — WRONG, and it caused the blocker.**
   When S1c activation is live (the production state since 2026-07-25),
   candidate card-stamping flows through `build_selector_context()` →
   `select_card()` → `selector.py:283 get_default_card()` — NOT through the
   scanner/orchestrator call sites §3.2 enumerated. Both audits proved
   post-cutover split-brain: `candidates.card_id=v2` while
   `trade_proposals.card_id=v3`, which silently poisons the per-card ΔR
   attribution and makes the card-version segmentation this ticket promises
   impossible. CORRECTED: threading `settings` through `build_selector_context`
   (minimal — no selection-logic change) is IN scope. The rest of the
   selector/S1c/PER exclusion stands.
2. **§2 item 5 "earnings hold-window widens automatically" — HALF-WRONG.** Only
   proposal-stage fields follow the hold days. The candidate-stage
   `earnings_within_hold_window` reads `EARNINGS_PROXIMITY_DEFAULT_HOLD_DAYS`
   (=3) — a second, un-linked copy of the policy number this ticket exists to
   single-source. CORRECTED: the candidate-stage window follows the ACTIVE
   card's `max_holding_days_default` (threaded at the call site); the env
   setting stays only as the fallback when no card resolves. No third `.env`
   edit in the §3.6 ceremony.
3. **§3.4 hold10 replay convergence.** The shared 15-calendar-day give-up
   (`UNAVAILABLE_AFTER_DAYS`) selectively censors 10-trading-day windows
   (~9% of trading dates span >15 calendar days; ONLY the 0-R
   'neither' outcomes are lost — a directional bias). HOLD-1 already solved
   this by omitting the give-up for its 10-day family. CORRECTED: `_hold10`
   rows use a 30-calendar-day give-up; v1 arms keep 15.0 byte-unchanged.
4. **§3.4/§6 shared budgets + missing tooling.** The v1 baseline report's SQL
   reads ALL rule_versions before filtering (shared `LIMIT 5000` + headline
   counts + resolver budget), so hold10 rows dilute the pre-registered v1
   analysis — the exact harm ruling D2 exists to prevent. CORRECTED: filter by
   `rule_version` at the SQL layer (report queries + headline counts;
   resolver stays shared but rule-aware ordering must not starve v1 rows).
   ALSO: §6 step 4 assumed existing tooling could register/read the hold10
   arm; it cannot. A hold10 registration path (CLI) and a segmented hold10
   report section are IN BUILD SCOPE.
5. **§3.2 `ACTIVE_CARD_ID` validation is too weak.** It accepts the
   shadow-only PER card (`post_earnings_reaction`) — one hand-edit away
   during the cutover ceremony from making a "no trading" card the live
   default. CORRECTED: validation additionally requires
   `state == live_eligible` AND an integer `max_holding_days_default` in
   [1, 30] present on the card, both enforced at settings load.
6. **§3.3 validator hardening.** `max_holding_days` must be integral
   (10.0 accepted and coerced; 10.9 / "10" rejected or coerced-and-written-back —
   the validated int MUST be what persists downstream), and a range rejection
   must surface as `MAX_HOLDING_DAYS_OUT_OF_RANGE` in
   `rejected_candidates.reason_code` (extend the existing `NO_ATR_DATA`
   special-case branch), never as `INVENTED_CATALYST_IN_NO_NEWS_MODE`.
