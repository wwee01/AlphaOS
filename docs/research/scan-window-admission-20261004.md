# Research admission and fair outcome continuation — 4 October 2026

The retained October 2 observation shows an outcomes job starting at 19:44:04 UTC and returning at 20:00:54, spanning the entire configured 15:45–16:00 ET scan window. The prior scheduler only rechecked SCAN after each whole job returned. It correctly refused an expired window, but could not recover that missed opportunity. The new offline actual-writer reproduction demonstrates the same pattern; it is not a claim of natural post-repair coverage.

This repair applies to the dedicated scheduled research role. It does not expand windows, create late catch-up scans, bypass fresh monitor readiness/fuses/kill or cost gates, change strategy/feed/universe/caps, or alter outcome arithmetic. The dedicated monitor and default manual/all-role execution keep their existing behavior. Direct unbudgeted `outcomes_update()` preserves its former stage order and baseline query behavior.

## Admission and stop boundaries

Before claiming any due non-scan research job, admission finds the next unserved configured scan window on the current/next seven calendar dates using the existing trading-day helper. Only a completed scan for its exact window key serves a window. A started, failed, skipped or yielded scan does not make the remainder available to background work; a failed readiness/fuse check likewise does not serve it.

The admission envelope is `max(300 seconds, maximum recorded job duration in the prior 30 days + 60 seconds)`. It reads completed/failed/yielded job durations. If work would enter the envelope before an unserved window, dispatch reports `deferred` without creating a job claim, consuming the window or changing a failure streak. This observed envelope is not a hard execution-duration bound. A served current window no longer blocks background work; later windows remain protected.

An admitted scheduled outcomes job additionally receives a cooperative budget. Its first boundary is five minutes before the captured next unserved scan window. Before another outcome row is claimed or another stage begins, the budget stops the slice once that boundary is reached. It remains stopped even if one in-flight call already overran the whole window. A backwards/naive clock also stops work. A concurrently completed matching scan allows the budget to consider the next unserved window. No callback authorizes a scan: the normal scheduler recheck owns actual window, monitor, fuse and persistent-claim validation.

Alpaca daily-bars retrieval currently makes one HTTP request per symbol, with its existing 15-second socket inactivity timeout; it does not page or retry in this path. An inactivity timeout is not a total deadline. No threads, forced cancellation, transport changes, additional requests or shared-SQLite concurrency were introduced. A request already running may still consume an entire window; the next cooperative boundary prevents further research work but does not fabricate a scan or missing outcome. Other research job bodies retain their synchronous behavior and can still exceed their observed admission envelope. This bounded repair cannot promise every future scan window will be reached under arbitrary provider/process stalls.

## Fair continuation and status

The job records real progress, then returns `yielded` when the cooperative budget stops it. It does not report a partial invocation as `completed`. Yielded rows do not consume the active/completed idempotency claim, do not impose the completed outcomes job's 60-minute cadence, and are excluded from failure-streak calculation rather than resetting failures. Existing future ticks retry normally; there is no new worker or faster polling.

The cooperative path owns the existing research lock and uses two explicit append-only system-event categories:

- `outcomes_work_turn_outer`: candidate outcomes, attribution discovery, attribution resolution, baseline.
- `outcomes_work_turn_baseline`: baseline v1, baseline HOLD10.

Each `outcomes_work_turn_v1` receipt binds its domain, visited stage, next stage, positive revision and aware recorded time. Latest malformed/future/unknown receipts refuse processing. A short transaction compares the expected latest row and advances the turn before work; an interrupted stage leaves its sibling next after restart. Zero-work stop occurs before any turn or row claim. Disabled attribution/baseline stages may advance the outer bookkeeping turn with an explicit `disabled` result; they are not provider calls or completed measurements. Lookup is per stage, never per row. The existing event table needs no new schema/index at current measured scale.

Candidate forward/replay and HOLD10 retain their existing separate fair cursors, frozen cycle high-water limits and durable opposite-family dispatch. Checks occur before requesting the next cursor claim, so an unvisited row is not skipped by yielding. An already claimed row can finish and commit its actual received evidence; a crash retains the existing next-cycle retry rule. A yielded candidate block does not run the normal final alternation that would undo its sibling's turn.

Baseline adds two stable cursor identities in the existing generic scheduling table: `baseline_v1_v1` and `baseline_hold10_v1`. The claim helper binds those exact names exclusively to `shadow_baseline_decisions`; candidate cursor names remain exclusively bound to `candidate_outcomes`. No arbitrary table name is accepted. Baseline arms keep their separate limits and pinned 3/10-session replay windows. Their high-water/cycle cursor now lets newer pending rows progress across scheduled slices, while unfinished older rows return in later cycles. Default manual baseline resolution keeps its prior ordering. Unknown-arm warnings are retained, and all row inputs, completion/maturity rules and replay calculations remain unchanged.

Attribution reads already committed candidate/trade outcomes. A rotated slice can read work committed by a prior invocation, so it need not follow candidate processing in the same invocation as the old synchronous path did. Discovery and resolution have separate durable turns and explicit unvisited-stage reporting. Their original source IDs, proposal-level matching, versions and formulas remain unchanged. Delaying first resolution may change which genuinely available source is selected (for example, a later realized outcome instead of an earlier replay for an override); identical timing or identical results across different schedules are not claimed. A partial or missing source never becomes a manufactured final result.

Results retain `seeded`, `updated`, `attribution` and `baseline` only for visited work, plus `stages`, `stages_not_started`, `yielded`, and the versioned budget receipt. Unvisited stages/arms are explicitly named rather than populated with invented completion counts. Baseline session-admission evidence remains available both per arm and in the compatible `session_admission_by_arm` projection.

## Validation and deployment boundary

Focused tests use real scheduler claims, outcome writers, durable close/reopen, original replay engine and harmless fake clocks/providers. They retain the former failure evidence, verify no late scan, admit only served-window work, test repeated slices reaching all four candidate/baseline families and newer baseline rows, preserve failure fuses, refuse corrupt/competing turn receipts, and verify existing outcome/maturity regressions. The older post-job scan-recheck fixture explicitly stubs admission so its original health/fuse/window/claim assertions still test an already-admitted overrun; separate actual-writer cases test the real admission path.

No production action, historical backfill, strategy activation, threshold change or provider/broker request is part of this author change. Natural improvement must be measured after a separately reviewed deployment and subsequent real scheduled opportunities.
