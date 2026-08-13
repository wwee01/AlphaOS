# Post-review remediation — PRE-1 · TIME-1 · VOCAB-1 · GREEN-1

**Status:** SPEC. Fable strategist, 2026-08-13. Ordered by CK ("fix 2 to 5") after the
seven-lens strategic review (§9 row `fc7f013`).
**Ceremony:** two Sonnet builders in isolated worktrees (Group A / Group B below),
dual blind Opus audits, merge only on explicit operator instruction.

Item 1 of the review's list (restore OpenAI credits) is DONE — verified 2026-08-13
with a live minimal call: served model `gpt-5.6-luna`, 17 tokens, no 429.

---

## Grouping (two parallel builders, minimal file overlap)

- **Group A — VOCAB-1 + GREEN-1** (measurement + test/CI layer). Files:
  `reports/baseline_report.py`, `reports/regime_arming_scorer.py`, `tests/**`,
  `.github/workflows/ci.yml`, `pyproject.toml`, `README.md`, plus the mypy fixes in
  `cards/registry.py`, `ai/openai_client.py`, `ai/validation.py`.
- **Group B — PRE-1 + TIME-1** (visibility + execution layer). Files:
  `scheduler/**`, `reports/daily_brief.py`, `execution/position_manager.py`,
  `config/settings.py`, `.env.example`, `constants.py`.

Both groups touch `tests/` — each adds its own new test module; neither edits the
other's. If a builder needs a file outside its list, it must say so in its report
rather than reaching for it.

---

## VOCAB-1 — the phantom `outcome_status` literal (P0-C)

**Defect.** `reports/baseline_report.py:186` and `reports/regime_arming_scorer.py:212`
filter `co.outcome_status = 'resolved'`. The writer (`learning/outcomes_tracker.py`)
only ever emits `complete` / `partial` / `pending` / `unavailable`. Verified in
production: **0 rows** match `'resolved'`, **10,509** match `'complete'`. So
`n_paired_total = 0` while the digest prints `n_shadow_resolved = 1370`, and BASELINE's
pre-registered pairing (H-AI-1, analysis 2026-09-07) has been empty for 35 days.
Six test fixtures insert the phantom literal, which is why the suite stayed green.

**Fix.**
1. Both call sites: `'resolved'` → `'complete'`. **`partial` is NOT included** — those
   rows are computed on a truncated bar window and are overwritten by a later pass;
   pairing on them would freeze a censored value.
2. Purge `"outcome_status": "resolved"` from every fixture:
   `tests/test_baseline.py:440`, `tests/test_regime.py:644`,
   `tests/test_pr12_hypotheses.py:192`, `tests/test_pr13_card_scoreboard.py:45,144,149`,
   `tests/test_pr13_2_promotion.py:63`. Replace with `'complete'`. Where a fixture
   deliberately wants a non-pairing row, use `'pending'` and say so in a comment.
3. **Vocabulary guard (the class fix).** New test that walks the AST of `alphaos/`,
   collects every string literal compared against `outcome_status` (in SQL strings
   and in Python comparisons), and asserts each is a member of the set the writer
   actually emits — sourced from one new `constants.OUTCOME_STATUSES` tuple that
   `outcomes_tracker` also writes from. Mutation-test it: flip a literal, prove red.
4. Same sweep for `replay_result` and `label_source` while the AST walker exists —
   report what it finds; fix only genuine mismatches, and list any it can't classify.

**Expected effect, to be stated in the builder's report as measured numbers:**
`build_baseline_report()` against a fixture corpus goes from `n_paired_total = 0` to
non-zero; `regime_arming_scorer` likewise. Do NOT run it against production.

---

## GREEN-1 — `main` is red and CI has never run the suite (P0-D)

**Defect 1 — date rot (§H.1, 4th occurrence).**
`tests/test_canary.py::test_cost_guard_counts_canary_results_from_non_mock_runs_only`
and `tests/test_eval.py::test_run_eval_refuses_a_live_run_once_the_cost_cap_is_reached`
seed rows at a hardcoded `"2026-07-09T00:00:00+00:00"` and assert against
`cost_guard.calls_in_last_30_days` (30-day trailing window). 2026-07-09 + 30d =
2026-08-08 — both went permanently red that day. Reproduced by clock-shift bisect.
**Fix:** seed relative to `timeutils.now_utc()` / `timeutils.market_date()` (the
`cd057ce` precedent). Mutation-test: shift the clock ±90d, both must stay green.

**Defect 2 — CI cannot collect.** `.github/workflows/ci.yml` runs
`pip install -e ".[test]"`, but every `tests/test_api_console*.py` imports `fastapi`,
which lives in the separate `api` extra. Collection aborts with 6 errors — **the whole
suite never runs in CI**, not just the API tests. Dark since `0017b3f` (2026-07-12).
**Fix:** install `.[test,api]`. Do NOT paper over it with `importorskip` — the API
tests should genuinely run.

**Defect 3 — lint/type gates red on HEAD.** `ruff check alphaos connectors tests`
exits 1: F401 `BASELINE_HOLD10_SUFFIX` (`tests/test_baseline.py:26`, from HOLD-2) and
F401 `conftest.make_settings` (`tests/test_shadow_research_view.py:26`).
`mypy alphaos` exits 1 with 5 errors: `cards/registry.py:163` (`settings: Optional[object]`
→ no `active_card_id` attribute — type it properly, do not `# type: ignore` it),
`ai/openai_client.py:689,691,741` and `ai/validation.py:79` (`Any | None` where `int`
expected — all four from HOLD-2's `max_holding_days_bound`). Fix the types, not the
symptoms.

**Defect 4 — the date-rot class has no detector.** Add a small pytest plugin
(`tests/_dateshift.py` or a conftest option) that patches `timeutils.now_utc` by a
configurable offset — `timeutils` is the codebase's only clock, verified — and a CI
job that runs the full suite at **+90 days**. Failures there are advisory (a separate,
non-blocking job) but must be reported. This is what turns §H.1 from a recurring
surprise into a caught-before-merge class.

**Also:** add a `console/` vitest job to CI (82 tests in 10 files, never run); correct
`README.md`'s stale test counts (`:53` "48 tests", `:340` "90 passed") and the
`pip install -e ".[test]"` instruction that produces an uncollectable suite.

---

## PRE-1 — preflight self-test + the provider-health rung (P0-A, the class fix)

The review's central lesson: **every instrument measures whether the machinery is
turning, none measures whether it is cutting.** Six days of total AI failure produced
"Nothing needs you" every morning. Fix the class, not the instance.

**PRE-1a — provider-health rung in the alert ladder (do this first; it is the
highest-value 20 lines in this spec).**
- `reports/daily_brief.py::_one_action` gains a rung, ABOVE the existing stale-backup
  rung: if the AI fail-safe/error rate over the trailing N candidates exceeds a
  threshold, that becomes the headline and the push goes at `priority="high"`.
- Reuse the existing grader: `ai/labeller_health.py::evaluate_failsafe_health` already
  computes exactly this for the labeller and its docstring names this scenario.
  Generalize it to take a row source so it can grade `openai_evaluations` (the
  evaluator — the path that gates trades) as well as the labeller. Do not fork it.
- Detection signal must NOT rely on `prompt_hash IS NULL` alone: RR-floor and
  `NO_ATR_DATA` rejections also land hash-less (they discard `ai_lineage` on the
  rejection path). Key off `risk_flags_json` containing `OPENAI_REJECT` — and while
  you are there, **preserve `ai_lineage` and token usage through the `post_process`
  rejection paths** so a real, paid call is never recorded as if no call happened
  (this also fixes a cost undercount).
- `render_compact` (the text that actually reaches the phone) gains a
  "did-anything-happen" line: today's proposals produced + AI error count by category.

**PRE-1b — the preflight job** (§5's PREFLIGHT-1, written 2026-07-12, still unbuilt).
- New once-daily scheduled job, pre-open, added to `cadence.default_lock_key`'s
  once-daily tuple (the 2026-07-09 TEXT-0 lesson — omitting it makes the job
  re-dispatch every tick).
- Checks, each pass/fail with a reason string: (1) **OpenAI reachable** — one real
  minimal call, honestly counted against the cost cap, this is the check that would
  have caught P0-A; (2) Alpaca reachable; (3) market-data freshness; (4) canary
  staleness (days since last canary run); (5) backup age from `data/backup_status.json`;
  (6) journal writable + disk headroom; (7) kill switch / suspend latch state reported
  (not judged).
- **Any fail → one ntfy alert at high priority + one digest line.** All passes → one
  digest line, no alert (the digest already always sends).
- Fails must be individually attributable — a single "preflight failed" string is not
  acceptable.

---

## TIME-1 — the time exit that never fires (P0-B)

**Defect.** `execution/position_manager.py:145,163-167` skips `_check_exit` entirely
when `execution_source == 'alpaca_paper'` (all 7 positions ever). `_check_exit` is the
only producer of `time_expiry`, and an Alpaca bracket OCO has a stop leg and a target
leg but **no time leg**. `time_expiry` exits in production history: **0**. AMD and AVGO
are open now at 7 trading days against a stamped 3-day window. Meanwhile
`monitoring_snapshots` records `time_stop_status='active'` on every tick — the audit
trail asserts an enforcement that cannot happen. HOLD-2 moved a number that governs
the prompt, the earnings window and the replay window, but no live exit.

**Three parts. Parts 1 and 2 are ALWAYS ON. Part 3 ships DARK.**

**1 — Stop lying in the audit trail.** For broker-managed positions, write an honest
`time_stop_status` (e.g. `not_enforced_broker_managed`) instead of `active`. Add the
new value to the shared constants; do not invent a second vocabulary (see VOCAB-1).

**2 — Make it visible to the operator.** `_one_action` + a `## Needs you` line whenever
`trading_days_held >= max_holding_days` on any open position, naming the symbols and
their day counts. This alone converts today's silent condition into an actionable one
and is independent of any policy decision.

**3 — Build the enforcement, ship it OFF.** New setting
`TIME_EXIT_ENFORCEMENT_ENABLED`, **default false**, validated at load, in the config
fingerprint, documented in `.env.example`. When true, a broker-managed position at or
past its window is closed via: cancel the OCO legs → verify the cancel → submit the
close through the existing `_execute()` chokepoint → reconcile. Fail directions:
- cancel not confirmed → do NOT submit a close (never risk a double position); log,
  alert, retry next pass.
- any exception → position stays open with its legs intact (status quo), alert.
- never close on stale data — inherit `position_manager`'s existing "will not exit on
  bad data" refusal (`:150-157`).
Tests must prove: flag off → byte-identical behavior to today; flag on → a past-window
broker position gets cancel-then-close in that order; cancel failure → no close
submitted; and the 2-guard trading-day arithmetic (`is_trading_day` AND
`trading_days_between >= max_days`) is unchanged.

**Arming is CK's decision, not this ticket's.** The merge must not change when any live
position exits.

**4 — Fix the drift vector.** `.env.example` is stale on the three axes that caused
this miss: `EXECUTION_PROVIDER=simulated_internal` (live: `alpaca_paper` — this is
literally why the HOLD-2 spec reasoned against a fictional deployment),
`ACTIVE_CARD_ID=catalyst_momentum_v2` (live: v3), `OPENAI_PROMPT_VERSION=v1`
(live: v4). Re-sync all three, and add a startup log line reporting any
`.env` vs `.env.example` key divergence (missing keys only — never values, never
secrets).

---

## Out of scope (explicitly, so no builder drifts into it)

- The fail-safe contamination exclusion for pre-registered populations — **operator
  ruling required before 2026-09-07**, tracked separately.
- Any change to strategy, risk limits, sizing, the cost model, or the spread gate.
- AB-EVAL corpus v2 re-freeze (own ticket, own decision).
- Effective-N clustering re-derivation and the four unanswerable hypotheses — research
  decisions, not code fixes.
- `orchestrator.py` refactors and the lineage/git-info memoization (real, but not P0).
- Arming `TIME_EXIT_ENFORCEMENT_ENABLED`, or clearing the suspend latch.

## Test obligations (both groups)

Full suite green (`> log 2>&1; echo EXITCODE=$?`, never piped), exact counts reported.
`ruff check alphaos connectors tests` and `mypy alphaos` must both exit 0 — that is
part of Group A's definition of done and Group B must not regress it. Every new
behavior gets a named test; every fix to a silent defect gets a test that fails
without the fix (state the revert-proof in the report).
