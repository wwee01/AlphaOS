# Independent review of Codex repair `cd1cb2c` — Claude, 2026-09-13 SGT

**Reviewed range:** `6172335..cd1cb2c` (20 non-merge commits, 72 files, +4,464/−1,174).
**Method:** blind read of every execution/risk/research diff; full suite + ruff + mypy on the
deployed checkout; read-only broker calls to verify the SDK shapes the new code assumes;
column-level diff of the production ledger against Codex's pre-deployment backup
(`deployment-backup-20260913T032344Z.db`, SHA-256 `f8007aed…` — matches the handoff);
adversarial probes on the repo's own offline fakes (files alongside this one:
`claude-review-probes-lifecycle-2026-09-13.py`, `claude-review-probes-research-2026-09-13.py`).
No production writes. No broker mutation. No `.env` change. Nothing merged.

This commit set bypassed the standing spec → build → dual-blind-audit → operator-merge ceremony
(§9). CK approved Codex's deployment directly; this document is the after-the-fact audit.

## 1. Verification results

| Check | Result |
|---|---|
| Full suite on `cd1cb2c` (main session, `run_in_background`, explicit exit code) | **2,857 passed, 6 skipped, 0 failed, EXITCODE=0** (~27 min) — the single all-green full-suite run the handoff said not to claim is now on record |
| `ruff check alphaos scripts tests` | exit 0, all checks passed |
| `mypy alphaos` | exit 0, 200 source files |
| Broker (read-only): `list_positions` | AVGO 43, META 27 = ledger |
| Broker (read-only): `get_order(META bracket, nested=True)` | parent `filled` 27/27; tp `new` 27 @670; stop `held` 27 @581.39 → `verified_live_roles` = {stop_loss, take_profit} ✓ |
| Broker (read-only): `list_open_orders()` (status=open, nested=True) | returns the two **take-profit legs as top-level orders** (`order_class=bracket`, `legs=[]`); the `held` stops are **not listed**. The deployed `_fresh()`/`read_exposure()` handle this only because they pass the parent's leg ids as `allowed_ids`/`known_reductions` — the fake (`get_orders() → []`) never exercises this shape. Documented so the next fake change doesn't regress it. |
| Broker (read-only): `get_calendar(2026-09-14, 2026-09-15)` | `{'date': '2026-09-14', 'open': '2026-09-14T09:30:00', 'close': '…T16:00:00'}` — `_regular_session()` parses this shape correctly |
| Ledger vs pre-deploy backup — `positions`, `paper_orders`, `paper_fills`, `exits`, `trade_outcomes`, `trade_proposals`, `preregistrations`, `shadow_baseline_decisions`, `candidates` | 0 original rows changed in original columns |
| Ledger vs pre-deploy backup — `candidate_outcomes` (22,190 rows) | 998 rows touched after deploy by the 12:14 SGT outcomes job: 963 timestamp-only; **35 `partial` rows moved from a truncated MTM `replay_r` to `NULL/window_incomplete`** (34 with a 10-day window). Legitimate under the new maturity rule — these were not `complete` and would have been re-replayed anyway — but see D1: those 35 rows are now on the stranding path. |
| `replay_evidence_corrections` | 1,065 rows, all `bracket_maturity_v2`, single timestamp 03:23:47Z; **no reader anywhere in `alphaos/`** — inert sidecar, as the handoff says |
| `data/SHADOW_LABEL_SUSPENDED` | present, unchanged (mtime 2026-08-06 23:39) |
| `.env` | mtime 2026-09-04 13:47 — untouched by Codex; `MAX_RISK_PER_TRADE_PCT=0.01`, `MAX_OPEN_POSITIONS=5`, `MAX_DAILY_LOSS_PCT=0.03` |
| `docs/research/README.md`, `REVIEWED.md`, `CHATGPT_TASK_PROMPT.md`, `inbox/` | no diff in range — the data-not-instruction boundary is intact |
| Services | console, dashboard, scheduler, heartbeat, backup all registered, last exit 0; monitor completing every 15 min (4 s each); 0 open protection incidents; 0 CRITICAL/ERROR events since deploy |

## 2. Defects (concrete, reproduced)

Severity is about consequence on the live paper account and on pre-registered evidence, not code size.

### D1 — HIGH (research validity): legacy 10-day replays are stranded at `pending` forever

**Trigger.** `update_pending_outcomes` selects rows with
`outcome_status IN ('pending','partial') OR (replay_method_version='bracket_maturity_v2' AND replay_status='pending')`.
It writes `replay_status` on every row it touches but **never writes `replay_method_version`** —
only `_insert_outcome_row` (new rows) does. A row that existed before the deploy therefore carries
`replay_method_version=NULL`. When its 5-day family completes (`outcome_status='complete'`) but its
10-day replay has not matured (`replay_status='pending'`), it drops out of both halves of the
selection and is never visited again.

**Affected invariant.** "A ten-session replay cannot become final after five unresolved bars; a
day-six stop/target is captured" (scorecard L2). For legacy rows the deployed code does the first
half and not the second: the replay never becomes final at all.

**Evidence.** `claude-review-probes-research-2026-09-13.py::test_p6_*` — a legacy `partial` row with
a 10-day window: after 5 bars → `complete` / `replay_status='pending'` / `replay_method_version=NULL`;
after 10 bars → `update_pending_outcomes` reports `updated == 0`, row still `pending`, `replay_r`
still `NULL`. Control test with the version stamped completes normally.
Production exposure (read-only count, 12:40 SGT): **2,055 legacy rows still pending/partial**
(1,557 `pending` + 498 `partial`; decisions 2026-08-31 → 2026-09-11 — every AI-leg row of the last
two weeks, i.e. the current hold10 accrual window); 35 of them are already at
`replay_status='pending'` with `replay_method_version=NULL`. Already stranded today: 0 (no legacy
row has crossed 5 bars since the deploy). First stranding will occur on the next outcomes pass
that completes a 5-day family — i.e. Monday's.

**Proposed repair (two lines + one idempotent backfill).**
1. In `update_pending_outcomes`, add `update["replay_method_version"] = REPLAY_METHOD_VERSION`
   whenever `replay_status` is written by `replay_bracket_with_maturity`.
2. Make the selection tolerant of legacy rows: `... OR (replay_status='pending' AND replay_window_days IS NOT NULL)`.
3. One-off, operator-invoked: `UPDATE candidate_outcomes SET replay_method_version='bracket_maturity_v2' WHERE replay_status='pending' AND replay_method_version IS NULL` (35 rows today) — log the count in §9 before running.
Test to keep: the P6 probe as a regression.

### D2 — HIGH (execution): any protective leg with `filled_qty > 0` disables the time exit and blocks re-protection

**Trigger.** `TimeExitLifecycle.advance()` line 239: if **any** leg of the current protection
parent reports `filled_qty > 0`, it books the fill (idempotently — fine) and returns
`{"deferred": True, "stale_view": True}` **unconditionally**, before creating the intent, before
cancelling, and before `recover()` is reachable.

**Affected invariant.** TIME-2: a position past its stamped `max_holding_days` is closed;
"never left naked".

**Evidence.**
- `…probes-lifecycle…::test_p3_*` — take-profit `partially_filled` 3/10 (working), stop `held`,
  position due: five consecutive passes each return `stale_view`; zero `cancel`/`close` calls;
  position stays open. The time exit is defeated for as long as the partial fill persists
  (indefinitely for a resting limit that got a partial at the touch).
- `…::test_p4_*` — stop partially filled 4/10 then **cancelled**, tp cancelled, residual 6 shares:
  both legs terminal (no broker protection at all), three passes return `stale_view`, no
  `oco`/`close`/`cancel`; `protection_watchdog.check_position` on the same state returns
  `unprotected`. The residual is naked, blocks all new entries via the incident, and nothing in
  the system will ever re-protect or exit it — only an operator.

**Proposed repair.** Replace the early return with a decision on the *re-read* legs:
book fills (as now); if every leg is `TERMINAL` and local `qty > 0` → fall through to
`recover()` (both legs dead, residual needs an OCO); if a leg is still `_WORKING` with a partial
fill → continue into the normal cancel → verify → close path (the re-read loop at lines 260-270
already books a further partial and the post-cancel `_fresh()` re-verifies the quantity).
Return `stale_view` only when the position row itself is gone/closed after booking.
Tests to add: P3 and P4 with the expected `close`/`oco` calls.

### D3 — MEDIUM (execution/ops): post-cancel verification is a single immediate re-read

**Trigger.** Lines 249-274: cancel each leg, then re-read each leg **once, immediately**. Alpaca
returns `pending_cancel` for a short interval after `DELETE /orders/{id}` (that is what stuck on
AAPL overnight on 09-04 when the market was closed; in-session it usually clears in well under a
second). Any leg still `pending_cancel` → `verified=False` → CRITICAL incident
("protective cancellation is incomplete/unknown; protection may be absent"), high-priority push,
return. The stop is by then already `canceled`; the position has no working stop until the next
monitor pass (`SCHEDULER_MONITOR_INTERVAL_MINUTES` = 15, default).

**Affected invariant.** Bounded unprotected interval during a time exit. Also operator trust:
the first live exercise will likely page CRITICAL for a benign transient.

**Evidence.** `…::test_p1_*` — tp `pending_cancel` on the first re-read: pass 1 → CRITICAL
`unverifiable` incident, one high push, stop `canceled`, no close, no re-protect; pass 2 (tp now
`canceled`) → closes exactly once, position `closed`. The fix path is correct; the interval and
the page are the defect. This is the same single-read design TIME-2 shipped; Codex made the log
honest but did not close the window.

**When you will see it.** META (`pos_b4026419eeee`, stamped 7 days, opened 09-03) is due
**Tuesday 2026-09-15**, first regular-session monitor pass (≈21:30–21:45 SGT). AVGO is due 09-22.

**Proposed repair.** Bounded poll: re-read up to N times (e.g. 5 × 0.5 s) until every leg is
terminal or shows a fill; only then declare unknown. Keep the incident path for the genuinely stuck
case.

### D4 — MEDIUM (ops): lifecycle incidents push and log CRITICAL on every pass, 24/7

**Trigger.** `TimeExitLifecycle.incident()` calls `open_protection_incident` (which correctly
returns `None` for a repeat of the same open incident) but then **unconditionally**
`log_system_event(CRITICAL)` and `alerts.send_alert(priority="high")`. The monitor job runs every
15 min around the clock.

**Evidence.** `…::test_p2_*` — AAPL-style stuck `pending_cancel` across a closed session: 1 + 8
passes → 9 high-priority pushes and 9 CRITICAL `system_events` for one unchanged condition. Live
that is 4 pushes/hour until it clears at the next open (AAPL's took ~16 h → ~64 pages).

**Proposed repair.** Push/log only when `open_protection_incident` returns a new `check_id`
(transition), matching the watchdog's own "every pass audited, not every pass alerted" rule.

### D5 — LOW (execution, by design but worth a ruling): fresh-limit re-check rejects instead of downsizing

`_execute` blocks when `proposal.qty > risk.sizing.shares` (tested and intended:
`test_final_execution_blocks_stale_approved_quantity_before_any_broker_submission`). Sizing is
`floor(0.01 × equity / risk_per_share)` with *live* equity (95,622 today; `PAPER_EQUITY=100000`
is no longer what sizes trades). Between proposal sizing and submission, equity ticks; a 1-share
shortfall blocks the trade with `RISK_OVERSIZED`, WARNING only, no page. Expected equity dip that
costs a trade ≈ 50 × risk-per-share: $300 (XLV-type $6 stop) to $1,800 (META-type $37 stop). In
auto mode the gap is seconds → rare; under manual approval (minutes–hours) it is material and
silent. Option for CK: downsize to `min(qty, shares)` when the shortfall ≤ 5 %, else block as now.

### D6 — LOW (reporting): CRITICAL lifecycle outcomes are reported under `deferred`

`incident()` defaults `deferred=True`, so a CRITICAL `unverifiable` result lands in
`time_exits_deferred` next to benign "session closed" entries and `time_exits_errors` stays
empty (P1 shows `errors == []`). Digest/console consumers of these counters will under-report.

## 3. Behaviour changes CK should know about (not defects)

- **All 15 preregistrations are now quarantined by code**, including `prereg_35bbce762e7f` (hold10,
  2026-10-05), both Sept-14 shadow preregs and SETUP-1's H-PER-1P-v2/1N-v2 (2026-10-15).
  `evaluate_hypothesis` raises; `resolve_due_hypotheses` reports `quarantined`; promotion is
  blocked. There is no allow flag. Nothing pre-registered can finalize until CK approves a
  successor contract (`docs/research/research-contract-quarantine-2026-09-13.md`). Per instruction
  I have preserved this; the consequence is that the evidence program is paused, not merely
  repaired.
- **Risk limits are tightened, not raised:** sizing uses live equity and uncommitted notional; the
  3 % daily-loss gate now trips on `min(realized, equity − last_equity)` (mark-to-market);
  an unresolved qty mismatch now blocks entries; unknown broker state blocks entries.
- **Kill switch:** entries blocked; verified reduce/re-protect allowed — matches CK's 09-04 ruling.
- **`replay_recompute --apply` is retired** (raises). The 09-04 AILEG-1 recompute stands as applied.
- **Broker-call amplification:** `_assess_current_risk` runs `read_exposure` (4 + N_positions
  calls) at proposal, at approval and at submission — ~18 calls per proposal with two positions.
  Within Alpaca's 200/min but worth knowing if scans ever propose many names.

## 4. Preserved invariants — verified

Research inbox boundary ✓ · research-contract quarantine ✓ (executable, no bypass) · shadow
suspension latch ✓ · 5 slots / 1 % / 3 % ✓ (tightened semantics, same numbers) · no real-money
path ✓ (`preflight()` unchanged) · PositionManager still broker-free (suite ratchet).

## 5. Recommended order of edits (coordinated against `cd1cb2c`; none made)

1. D1 (before Monday's outcomes job strands the first rows).
2. D2 + D3 + D4 together in `time_exit_lifecycle.py` (before Tuesday's META exit if CK wants a quiet
   first exercise; otherwise observe Tuesday as-is and fix after — D3 is safe, just noisy).
3. D6 with D4. D5 needs a ruling.
