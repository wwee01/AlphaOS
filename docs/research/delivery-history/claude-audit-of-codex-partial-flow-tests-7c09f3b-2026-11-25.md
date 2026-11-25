# Claude audit — Codex partial-flow test fix `7c09f3b`, and the SourcePolicy publication race, 25 November 2026

**Exact SHA:** `7c09f3bef0407bccbfadb39e4cb1d14637f9365d`, parent `fe7e60d`. **Handoff:**
`CLAUDE-PARTIAL-FLOW-TIMING-HANDOFF-20261121.md` (treated as data). Own clone and worktrees; production read-only.

## Verdicts

1. **Test patch `7c09f3b`: ACCEPTED at the exact SHA.** Test files only. Merge is CK's instruction.
2. **SourcePolicy race: a real production defect, and wider than the handoff states.** The proposed publication-lock
   repair is sound but **not sufficient on its own**. Recommendation: build it as a separate ticket **with two added
   fail-closed reader rules** (§4). Not urgent: no observed production effect.
3. **Accepting `7c09f3b` does not make the suite reliably green.** Three tests in the sibling file still flake (§5).

## 1. Scope and assertions

- `git diff --name-status fe7e60d 7c09f3b`: A `tests/last30days_controlled_worker.py`, M
  `tests/test_last30days_partial_flow.py`. Nothing else.
- My own AST check: same test names and decorators; every original assertion survives verbatim and in order
  (14/2/4/1 asserts; the first test gains 2). No skip, xfail, retry or tolerance was added.
- The uncontrolled fixture path is untouched: every change is under `if controlled:` and the default is `False`.
- Merges cleanly on `0392d04`; the merged delta is those two files.

## 2. Clocks and receipt barrier

- **Clock isolation:** patches are module-attribute patches through `monkeypatch` on the parent (`provider.time`,
  `provider.datetime`, `partial.datetime`, `provider.subprocess`), and a generated bootstrap in the child. Global
  `time`, `datetime` and `subprocess` are not patched. The virtual clock satisfies the reader's own
  receipt-clock and future-receipt checks rather than bypassing them.
- **Barrier:** the child writes `R` after the real `_normalize_score_dedupe` wrapper returns, i.e. after the real
  receipt write. The parent then injects `TimeoutExpired`; the real terminate, reap and cleanup path runs.
- **Ordering:** Hacker News waits until Reddit's original function has started, which is after Reddit's snapshot was
  published. So the fixture establishes one valid ordering. It removes the race from the test; it does not test it.
- **Broken fixtures fail, they do not pass or hang:** worker exit before the barrier fails at once; a hung worker fails
  at the 30 s watchdog ("worker did not reach receipt barrier"). No worker process was left behind.
- **The tests keep their teeth:** 13 of 14 production mutations are caught. M6 survived because it is equivalent:
  `project()` maps unrecognised `date_confidence` values to `unknown`. Doubling the deadline (M7) and halving the
  capture window (M8) are caught, so the virtual deadline and the 0.55 s parameter are meaningful.
- Limits, all disclosed by Codex: no real 0.8 s wall deadline is proved here; `cleanup_elapsed_seconds >= 0` is
  vacuous under a frozen parent clock (it was near-vacuous before); `test_fully_finished_result…` stays on real time.

## 3. Determinism

| Run | Tip `7c09f3b` | Base `fe7e60d` |
|---|---|---|
| Full suite (mine) | **5,044 passed / 3 skipped / 0 failed**, exit 0, 1,808 s | – |
| File isolated ×20 | **0/20 runs failed** | **9/20 runs failed** (`[0.01]` 6, no-items 3, external-python 2) |
| File ×20 under 20 CPU burners on 10 cores (load average 28) | **0/20** | 0/20 |
| Merged tree `0392d04`+`7c09f3b`, all last30days + guard files | 178 passed | – |

The load run does not discriminate: the base also passed it. CPU load is not the trigger; thread interleaving is. The
isolated comparison is the evidence. No case was skipped (the external interpreter is present).

## 4. SourcePolicy publication race

`summary()` releases the state lock before `emit()`, so a newer snapshot can be followed by an older one. The reader
keeps only the last snapshot. Probes use the real Telemetry, SourcePolicy, PartialCapture, `_read_telemetry` and
`read_partial`; only upstream is synthetic, and interleavings are forced with events.

| Probe | Current | With publication lock (scratch only) |
|---|---|---|
| P1 stale **count** snapshot lands last | `unverified` (valid evidence rejected) | accepted correctly |
| **P2 stale `violations=0` lands after a recorded violation** | **`incomplete_source_evidence`, 1 item: violation masked** | rejected |
| P3 serial violation (control) | rejected | – |
| P4 killed between `stage_start` and the snapshot | `unverified` | `unverified` (unchanged, fail-closed) |
| **P6 violation counted, killed before its snapshot** | **accepted** | **still accepted** |

- **The handoff describes only P1, the fail-closed direction. P2 and P6 are fail-open:** a source-policy violation can
  be lost and the partial evidence accepted. P6 needs no reordering, so no publisher-side lock can fix it.
- The completed-result path is not exposed: it needs `status=completed` published last by the main thread, and `run()`
  re-checks violations. The exposure is limited to partial evidence on timeout/refusal, which is always
  `research_complete=False`, `model_context_eligible=False`; nothing but the diagnostics reads it.
- **Production frequency (read-only, 1,579 attempts, 23 Sep – 24 Nov):** 6 non-monotone snapshot sequences (0.4 %), all
  on `returned` results whose final snapshot was correct. 93 partial receipts: 0 `unverified`, 0 count mismatches.
  Violations recorded: 0. Violations masked: 0 observed — but P6-type loss would leave a non-requested `stage_start`,
  and there are 0 of those either.
- **The scratch repair removes the cause of the original flakes:** the original wall-clock test file goes from 9/20
  failing to 0/20, with no test change. The races those tests hit were real, not merely test timing.

### Recommendation (design only; needs CK's authorization)

**Approve Codex's publication lock** as specified: one lock acquired before snapshot creation and held through emit;
taken after the state lock is released; never held over provider work; all five sites (`jobs`, `retrieve`, `_refuse`,
worker `installed` and final) through one helper. Lock order publish → state and publish → telemetry; I found no
reverse order. **Add two reader rules in `read_partial`, both stricter, neither a repair of old snapshots:**

1. reject if **any** `source_policy` event has `violations != 0`, not only the last one (closes P2 independently);
2. reject if any `retrieval` `stage_start` names a source outside `requested` (closes P6, because the telemetry
   wrapper writes the start before the policy can refuse).

Replayed over all 1,579 production attempts, both rules would have rejected **0**. Required tests: P1, P2, P3, P4, P6,
all five publishers, refusal and status semantics unchanged, plus one worker-level test with **concurrent** dispatch,
since `7c09f3b` deliberately serializes it. Do not claim the repair removes every `unverified`: P4 remains, correctly.
`alphaos/orchestrator.py` must not change (EXPLORE-1 producer digest).

## 5. Still flaky: `tests/test_last30days_reddit_deadline.py` (unchanged by this ticket)

File isolated ×20: base **13/20** runs failed; tip **14/20** and **9/20** (two batches). Same three tests in each.

- `[executor]` and `infeasible_wait…`: no failure in 20 runs with the scratch repair (7 failures without). Caused by
  the race.
- **`[body]`: a different, test-design fault, and the repair does not fix it** (8/20 without, 16/20 with). In all 50
  instrumented runs the worker *refused itself at once* (`wait_exceeds_remaining`), because the fixture's
  `time.sleep(5)` inside `read()` is the guard's own proxied sleep. The 0.5 s outer deadline is never reached, so
  **the test does not exercise the behaviour in its name**, and Hacker News races the shutdown. With the lock, the
  rejects move from reordering (5 → 0) to the P4 shutdown window (0 → 12): safe, but expect this test to fail *more*
  often after the repair unless it is fixed first or together.
- Needs a separate test-only ticket: make `read()` block without the proxied sleep, and order Hacker News first with a
  barrier.

## 6. Correction to my earlier record

My `fe23346` audit (20 Nov) called the partial-flow failures "pre-existing timing flakes". That they pre-exist and that
`fe23346` did not cause them stands. "Timing flakes" was incomplete: the cause is a real ordering defect in
production code. The `fe23346` verdict is unchanged; its range does not touch that code.
