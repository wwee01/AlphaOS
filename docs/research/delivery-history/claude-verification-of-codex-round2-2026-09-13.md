# Verification of Codex round-two repair `742312f` against D1–D6 — Claude, 2026-09-13 SGT

**Reviewed range:** `1a16f03..742312f` (15 commits, 38 files, +3,842/−345). Application tree
identical between `a1482d3` and `742312f` (installer/tests only), as the delivery states.
**Method:** same as round one — full suite + ruff + mypy on the deployed checkout; read-only
broker calls; column-level diff of the ledger against Codex's round-two backup
(`round2-backup-20260913T115826Z.db`, SHA-256 `eedc5caf…` — matches); the **original round-one
probes re-run unchanged** (they pin the old broken behaviour, so each must now fail in the
direction of the fix); then new corrected-behaviour regressions on the same triggers, plus
adversarial probes of the new surfaces. No production writes, no broker mutation, no `.env`
change, nothing merged. Probe files beside this one: `claude-verify-742312f-*.py`.

## 1. Verification results

| Check | Result |
|---|---|
| Full suite on `742312f` (main session, explicit exit code) | **3,063 passed, 6 skipped, 0 failed, EXITCODE=0** (~27 min) |
| `ruff check alphaos scripts tests` / `mypy alphaos` | both exit 0 (206 modules typed) |
| Original round-one probes re-run on `742312f` | P1, P2, P3, P4, P6 **fail** (old behaviour gone) — each in the direction of the fix: P1 now `errors` not `deferred`; P2 1 push not 9; P3 closes (103.6) instead of `stale_view`; P4 `reprotected` instead of `stale_view`; P6 `updated == 1` instead of 0. P5, P6b still pass. |
| Ledger vs round-two backup — `positions`, `paper_orders`, `paper_fills`, `exits`, `trade_outcomes`, `trade_proposals`, `preregistrations`, `shadow_baseline_decisions`, `replay_evidence_corrections` (1,065), `decision_adjustments` (1,314) | 0 original rows changed in original columns; no columns added |
| Ledger vs round-two backup — `candidate_outcomes` | 771 rows: `updated_at_*` only (the in-flight Sunday outcomes job touching pending rows with no new bars). No replay/forward field changed. |
| Quarantine, safety, risk engine, research-inbox docs | `git diff 1a16f03..742312f` on `stats/research_validity.py`, `stats/preregistration.py`, `hypotheses/resolver.py`, `cards/promotion.py`, `safety.py`, `risk/risk_engine.py`, `docs/research/README.md`, `REVIEWED.md`, `CHATGPT_TASK_PROMPT.md` — **empty** |
| `.env` | mtime 2026-09-04 13:47 — untouched; 5 slots / 1 % / 3 % / `APPROVAL_MODE=auto` / cap 5 / time-exit on / shadow labelling on |
| `data/SHADOW_LABEL_SUSPENDED` | present, mtime 2026-08-06 — unchanged |
| WATCH-1 | `research/watch_advisory.py::decision_diagnostic` is a pure function; its only consumer stores the dict into `decision_adjustments.evidence_json`. `final`, proposal creation, risk and orders are untouched. Measurement-only confirmed. |
| Broker (read-only): `EntrySubmission.validate()` against the live accepted META and AVGO brackets | passes for both `get_order(nested=True)` and `get_order_by_client_id` shapes (the by-client read also returns legs) — the first live entry should not false-halt on shape |
| Services (live) | six registered; dedicated monitor completing every 15 min in ~4 s **while** the research worker ran the 14.5-min outcomes job concurrently (12:59:20Z start vs 12:59:29Z monitor completion) — process isolation working; health worker reporting `ok: true`; 0 failed job rows since deploy |

## 2. D1–D6 verification table

| # | Invariant now enforced | Reproduction (original trigger) | Result |
|---|---|---|---|
| **D1** | A pre-deploy row with an explicitly pending replay stays selectable after its 5-day family completes; `replay_method_version` is **retained (NULL)**; completed forward fields untouched; the row is excluded from the prospective cohort | `…research…::test_d1a` legacy `partial` 10-day row → 5 bars → `complete`/`pending`/NULL stamp → 10 bars → `replay_status='complete'`, `replay_r` set, `replay_bars_used=10`, stamp still NULL, `forward_5d_r` unchanged, idempotent; `_pair_exclusion_reason` → `legacy_or_unverified_ai_replay`. `d1b` day-six target hit → `target_hit`/2.0. `d1c` the exact post-`cd1cb2c` stranded shape (complete/pending/NULL/`window_incomplete`) recovers. `d1d` legacy complete-with-final never revisited. `d1e` no bars → `replay_status='unavailable'` with `outcome_status` still `complete`. `d1f` new v2 rows enter the cohort. | **FIXED**, provenance preserved. The version-stamp backfill was **not** applied and is not needed; I agree it would wrongly admit legacy rows to the prospective cohort. |
| **D2** | A partially filled protective leg no longer short-circuits `advance()`: fills are booked, residual re-read, working partials proceed through cancel → verify → close, terminal partials go to `recover()` | `…lifecycle…::test_d2a` tp `partially_filled` 3/10 → closes exactly 7, fills sum to 10, one outcome qty 10, `exit_reason=time_expiry`. `d2b` stop 4/10 then cancelled, tp cancelled → OCO for 6 (`reprotected`), watchdog `protected`; next pass cancels the OCO and closes 6. `d2c` +2 fill lands during the cancel → intent shrinks to 5, closes 5, no oversell. `d2d` restart mid-`canceling` → one close, one intent row. `…new-surfaces…::test_n5` tp fully fills during the cancel → booked as `target`, no market order, intent `terminal`. | **FIXED** |
| **D3** | Post-cancel verification polls up to 5 reads with 4 × 0.5 s waits before declaring uncertainty; fills/identity validated on every read | `test_d3a` `pending_cancel` on the first read, `canceled` on the second → closes in the **same pass**, no incident, no page. `d3b` persistently `pending_cancel` → ≥5 reads, bounded, one CRITICAL in `errors`, no close/OCO, one page. `d3c` real delay budget measured 1.9–3.5 s. | **FIXED** (bounded). As Codex notes, a genuinely stuck cancel still leaves the interval — correctly reported, entries blocked. |
| **D4** | Lifecycle incidents page/log only on an incident **transition**; every pass is still audited | `test_d4a` stuck state, 1 + 8 passes → **1** push, 1 CRITICAL event, 9 audit rows. `d4b` lifecycle `unverifiable` → watchdog interleaves and opens `unprotected` → later uncertain lifecycle reads map onto the known `unprotected` (no new page) → cancel lands, close, watchdog reconfirms → `has_blocking_incident` clears. | **FIXED** |
| **D5** | Stale approved quantity exceeding the fresh allowance is rejected; no downsizing | `tests/test_account_exposure.py::test_final_execution_blocks_stale_approved_quantity_before_any_broker_submission` passes; no 5 % tolerance or qty mutation anywhere in `orchestrator.py`/`entry_submission.py` (grep). | **UNCHANGED, as instructed** |
| **D6** | CRITICAL lifecycle outcomes reach `time_exits_errors`; `run_monitor_once` persists `error_count` and exposes `errors` | `test_d6` `reconcile()` → `time_exits_deferred == []`, one `critical` entry in `time_exits_errors`. `monitor_health` reads `inner.errors`/`time_exits_errors` → `ok=False`, `entry_ready=False`. | **FIXED** |

## 3. New defects (severity, location, reproduction)

### N1 — MEDIUM (operations): an entry POST that never reached the broker halts all new entries indefinitely, with no operator exit

**Location.** `alphaos/execution/entry_submission.py:116-126` (`lookup` raises `LookupError` on a
broker 404), `order_manager.py::_submit_alpaca_paper` (→ `uncertain`), `execute_proposal`
(`pending_entry_intent` → block), `risk/account_exposure.py::read_exposure` (→ `ExposureUnavailable`).

**Trigger.** `submit_bracket` raises before the request leaves (connection refused, DNS,
TLS) — or the process dies between `entry.create()` and the POST. The broker genuinely never saw
the client id, so `get_order_by_client_id` returns 404 on every reconcile forever.

**Invariant violated.** "Fail closed" is honoured, but there is no bounded or operator-driven
path back to "open". `protection_resolve` resolves the incident row; `pending_entry_intent()`
reads `paper_orders.raw_request_json.phase`, so the halt survives that. No CLI touches the
phase; the only exit today is a manual SQL edit of production.

**Evidence.** `…new-surfaces…::test_n7` — POST refused → `submission_uncertain`; 20 reconcile
passes → still `unknown`; a second proposal is blocked at `execute_proposal` and at
`read_exposure`; resolving the protection incident leaves the halt in place; `submits == 1`.

**Proposed repair (coordinate with Codex).** An explicit operator command
`entry_intent_resolve <order_id>` that (a) re-reads by client id and requires a fresh 404, (b)
requires no broker position and no open order for the symbol, (c) marks the intent
`acknowledged`/`state='failed'` with an audit event, and (d) resolves the linked incident. No
automatic resolution.

### N2 — LOW (operations): an entry that collides with a monitor pass is permanently BLOCKED, not deferred

**Location.** `orchestrator.py::_execute` (`monitor_ownership` not acquired → `blocked=True`),
`orchestrator.py::_execute_checked` (`monitor_health.entry_ready` is False while a monitor row is
in flight), `_handle_proposal` (a blocked result → proposal status BLOCKED, no retry).

**Trigger.** The research worker reaches submission during the dedicated monitor's ~4 s pass
(every 15 min; ≈0.5 % of wall time). The proposal is lost rather than retried on the next tick.

**Evidence.** `…new-surfaces…::test_n11` — a foreign thread holds the monitor lock →
`_execute` returns `blocked`, detail "Execution monitor owns the account; new entry deferred"
(the detail says deferred; the proposal lifecycle says blocked).

**Proposed repair.** Bounded wait for ownership (e.g. ≤10 s) before blocking, or return a
distinct "deferred" result that the auto path leaves in `pending_approval` for the next tick.

### Observations (not defects)

- Any error in the latest completed monitor pass (including one transient `get_order` failure
  in `reconcile`) makes `monitor_health.ok=False` → one unhealthy-episode page and entries
  blocked until the next clean pass (≤15 min). Fail-closed and deduplicated; just know it is that
  sensitive.
- The research worker exits non-zero when its scan is `blocked` by monitor readiness
  (`worker.run_role` counts `blocked` as failure) — launchd's "last exit status" for
  `com.ck.alphaos.scheduler` will show 1 in that case. Cosmetic.
- `_read_protection` re-reads each leg individually (3 `get_order` calls per due position per
  pass) and `_verify_cancellation` books fills up to 5×2 times — all idempotent; within rate limits.
- Both writer processes share one SQLite file with a 5 s busy timeout; the outcomes job commits
  per row, and the live overlap on 12:59Z completed cleanly. A long write transaction elsewhere
  would surface as a failed monitor row, i.e. visibly.

## 4. Preserved invariants — verified

Historical evidence ✓ (backup diff) · all 15 preregistrations still quarantined, no code change ✓ ·
three frozen verdicts untouched ✓ · shadow latch ✓ · `.env`/limits ✓ · kill-switch policy ✓ ·
WATCH measurement-only ✓ · no new broker order (23 `paper_orders`, 22 `paper_fills`, unchanged) ✓.

## 5. What to watch on Tuesday (META, stamped 7 days, due 2026-09-15)

With D3 in place the expected path is: one in-session monitor pass → cancel both legs → verified
within ≤2 s → market close → `time_expiry` outcome → "closed" push. If Alpaca holds
`pending_cancel` longer than the poll, you get one CRITICAL page, entries blocked, and the close
on the next pass — no longer silent, no longer repeated.

## 6. Coordination

No code edited. N1 is the only item I would implement before the first live entry; N2 can wait.
Both touch Codex's new paths (`entry_submission.py`, `orchestrator._execute`), so per instruction
they are proposals, not patches, until CK assigns them.
