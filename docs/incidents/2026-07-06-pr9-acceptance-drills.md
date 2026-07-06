# 2026-07-06 — PR9 acceptance drills (kill switch, forced job failure, stale heartbeat)

Run against the **live production system** (`data/alphaos.db`, real `.env`, real
LaunchAgents already running) during the actual Monday 09:35-09:50 ET scan window,
same day PR9.1 merged. All three drills required by the PR9 spec's acceptance
criteria (§9.5) and the master reference's operating manual (§6).

## Drill 1 — kill switch (engage → verify → release)

No alert expected (kill-switch skips are documented as "expected state, no alert").

- Kill switch state before: `is_engaged() == False`.
- `python -m alphaos kill engage` → `{"kill_switch_engaged": true}`.
- The real automatic scan had already fired for the 09:35-09:50 window moments
  earlier, so a synthetic lock_key (`scan:drill-kill-switch-verify-20260706`) was
  used to exercise `JobRunner.run_job("scan", ...)` without colliding with today's
  already-completed real window (confirmed a skip never consumes a window's real
  lock_key, since `acquire()`/`is_due()` only treat `started`/`completed` rows as
  claimed — a `skipped` row does not block a later legitimate attempt).
- Result: `scan` → `status: skipped, reason: "kill switch engaged, scan skipped",
  kill_switch_engaged: true` — zero AI/scan cost incurred.
- `monitor` (synthetic lock_key `monitor:drill-kill-switch-verify-20260706`) →
  `status: completed` — confirms PR2.5 doctrine holds against the real production
  orchestrator: monitor/protection keeps running even while the kill switch blocks
  new entries.
- `python -m alphaos kill release` → `{"kill_switch_engaged": false}`. Confirmed off.

**Verdict: PASS.**

## Drill 2 — forced job failure → alert

- `orch.outcomes_update` monkeypatched in-process (no file changes) to raise
  `RuntimeError("DRILL: forced failure for the PR9 acceptance test (2026-07-06) --
  not a real error, safe to ignore the underlying condition.")`.
- `JobRunner.run_job("outcomes_update", lock_key="outcomes_update:drill-failure-verify-20260706")`
  → `status: failed`, error message as above (real `job_runs` row + real
  `system_events` ERROR row, both correctly labelled DRILL for future readers).
- This exercises the real `_alert_job_failure` → `alerts.send_alert(real_settings, ...)`
  path against the operator's real NTFY_TOPIC.
- Server-side confirmation: no `system_events` row with `category='alerts'` was
  logged afterward — `send_alert` only logs on a send FAILURE, so its absence
  confirms the POST to ntfy.sh succeeded (2xx).
- Operator confirmation (phone received the push): **CONFIRMED 2026-07-06 — notification received.**

**Verdict: PASS (end-to-end).**

## Drill 3 — dead-man heartbeat staleness → alert

Real-time staleness needs 2+ hours to occur naturally; verified the mechanism via
a forced clock offset instead of waiting, against the same real production
Orchestrator/settings/journal (this is a MECHANISM verification, not a literal
2-hour real-world outage — that fuller drill, stopping the scheduler LaunchAgent
for 2+ hours during market hours and confirming a page arrives, is still worth
running properly at some point per the operating manual §6, but wasn't practical
to do synchronously here).

- Real last completed job: `monitor` at `2026-07-06T13:41:15.304614+00:00` (UTC).
- Forced `now` = that timestamp + 3h = `2026-07-06T16:41:15+00:00` (12:41 ET,
  still REGULAR session — confirmed `market_hours: true` in the result, so the
  staleness check was genuinely enforced, not skipped for being outside hours).
- `JobRunner.heartbeat_check(now=<forced>)` → `{"ok": false, "market_hours": true,
  "detail": "last completed job (monitor) 180.0m ago (> 120m)"}`.
- Server-side confirmation: no `system_events` row with `category='alerts'`
  logged afterward → the POST to ntfy.sh succeeded.
- Operator confirmation (phone received the push): **CONFIRMED 2026-07-06 — notification received.**

**Verdict: PASS (end-to-end via forced-clock mechanism check). Full literal
2h-outage drill still recommended at some point, lower priority, non-blocking.**

## Post-drill system state (verified clean)

- Kill switch: `False`.
- `python -m alphaos scheduler_health` (real wall clock, no forcing): `{"ok": true,
  "market_hours": true, "detail": "last completed job (monitor) 1.9m ago (<= 120m)"}`.
- All drill `job_runs`/`system_events` rows use synthetic lock_keys or are clearly
  labelled "DRILL" — no interference with real cadence going forward, no fuse
  tripped (single failures, threshold is 3 consecutive).

## Outcome

**All three PR9 acceptance drills PASS, end-to-end, operator-confirmed 2026-07-06.**
The only remaining PR9 acceptance item is the 10-consecutive-unattended-trading-day
streak — a passive/observational milestone, not an action item. The clock started
today, the first clean trading day post-PR9.1 (no prompt leak, no date-flaky
tests). Nothing about this streak blocks starting PR9.5/PR10 in parallel; it is
simply tracked and reported when it completes (~2 calendar weeks out, accounting
for weekends). **PR9 is otherwise code-complete, merged, activated, and drilled.**
