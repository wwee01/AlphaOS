# Verification of Codex round-four repair `208544b` — Claude, 2026-09-14 SGT

**Reviewed revision:** `208544b432d594b7fc77d5d8e9fdef444fa5b4d0` (production HEAD, `main`, tracked tree
clean at review time). **Compared against:** my round-three record at `4ffc860`. **Range:**
`4ffc860..208544b`, 3 commits, 12 files, +1,556/−72.
**Method:** full offline suite + ruff + mypy on the deployed checkout; read-only ledger diff against
Codex's round-four pre-deploy backup (`round4-deployment-20260913T160205Z-0b532a59.db`, SHA-256
`e0c2b49a…` — matches the handoff); independent `launchctl print-disabled` / plist / interface /
readiness checks; offline probes on the repo fakes and on the **real alpaca-py HTTP path** with
`HTTPAdapter.send` intercepted (no socket ever opened); and one **read-only production attribution
check** on a scratch copy of the ledger against the live paper broker. No production writes, no broker
mutation, no rejection manufactured, no intent resolved, no `.env`/limit/quarantine/latch change, no
code edited, nothing merged. Probe files beside this one:
`claude-verify-208544b-rejection-held-2026-09-14.py` (13 cases),
`claude-verify-208544b-live-attribution-2026-09-14.py` (scratch-copy + read-only broker script).
Rounds one–three probes are preserved unchanged.

## 1. Verification results

| Check | Result |
|---|---|
| Full suite on `208544b` (`pytest -q tests/`, main session, explicit exit code) | **3,284 passed, 6 skipped, 0 failed, EXITCODE=0** (~28 min; same 6 calendar/live-broker-gated skips as rounds one–three) |
| `ruff check alphaos scripts tests` / `mypy alphaos` | both exit 0 (209 modules typed) |
| My round-four probes | **13 passed** (real-SDK 403 path ×8, held-symbol recovery ×7 incl. refusals) |
| Archived round-three N3/N4 probes (generic `APIError` without HTTP provenance; holding without originating order/fills) | still **refused** — correct negative controls, unchanged |
| Round-three N2 probes + D5 stale-quantity test | still pass — deferral and rejection semantics unchanged |
| **Read-only production attribution (N4 usability on the real book)** | `verify_entry_absence` for a synthetic failed META add-on and a synthetic failed AVGO add-on, on a scratch copy, against the live broker: **both ATTRIBUTION OK** — META `pos_b4026419eeee` 27 sh, protection `bae2cf1c…` (parent + tp `b1480270…` + held stop `4216c401…`); AVGO `pos_996e6b0d29a1` 43 sh, protection `da49fa35…`. The attribution copes with Alpaca's real open-order shape (tp legs top-level, held stops hidden). |
| Ledger vs round-four backup — 13 tables incl. `candidate_outcomes` (22,190), `decision_adjustments` (1,314), `replay_evidence_corrections` (1,065), `approvals` (16), `hypothesis_proposals` (8), `paper_orders` (23), `paper_fills` (22) | **0 original rows changed in original columns; no columns added** |
| Unchanged surfaces (`git diff 4ffc860..208544b`) — `stats/research_validity.py`, `stats/preregistration.py`, `hypotheses/resolver.py`, `cards/promotion.py`, `safety.py`, `risk/risk_engine.py`, `learning/outcomes_tracker.py`, `reports/baseline_report.py`, `research/watch_advisory.py`, `orchestrator.py`, `approval.py`, `scheduler/`, `execution/time_exit_lifecycle.py`, `execution/protection_watchdog.py`, research-inbox docs, `deploy/` | **empty** — quarantine, replay provenance, WATCH boundary, safety, N2 deferral, D5, monitor isolation untouched |
| `.env` / caps | mtime 2026-09-04; 5 slots / 1 % / 3 % / auto cap 5 / unattended cap 1 / `APPROVAL_MODE=auto` / `REQUIRE_MANUAL_APPROVAL=false` |
| Shadow latch | `data/SHADOW_LABEL_SUSPENDED` present, mtime 2026-08-06 |
| Services — **independent** `launchctl print-disabled gui/<uid>` (native words) | all six `enabled`; `launchctl list` shows all six registered, last exit 0; installed monitor/scheduler/heartbeat plists byte-identical to `deploy/` |
| Production state (read-only) | 0 pending entry intents, 0 open incidents, last dedicated monitor 16:22:17Z in 3.7 s, 0 failed jobs since deploy; readiness `ok=True entry_ready=True last_success_role=monitor` |
| Interfaces (read-only) | console `/api/v1/health` 200; dashboard `/_stcore/health` 200 |

## 2. Focus areas — verification table

| Target | Invariant now enforced | Reproduction | Result |
|---|---|---|---|
| **N3 positive — real SDK 403** | The first standard-transport paper POST answered 400/403/422 with a reviewed Alpaca `code`/`message` yields `broker_rejection_proof` bound to the exact prepared request (canonical JSON), status/code/classification and a response-body sha256 — no response text, no credentials persisted; the gate closes; `entry_intent_resolve` can release it as `abandoned_rejected` | `…rejection-held…::test_n3a` — real `TradingClient`, `HTTPAdapter.send` → 403 `{"code":40310000,"message":"potential wash trade detected. use complex orders"}` → exactly one request; phase `unknown`, proof `classification=wash_trade`, `request == json.loads(sent body)`, sha256 matches, message text absent from the row. Restart. Preview → eligible, writes nothing. Apply without note → refused. Apply with note → `abandoned_rejected`, `state='rejected'`, `client_order_id` preserved, proposal `blocked`, approvals count unchanged (no refund/receipt), **only its own incident resolved** (an unrelated `unprotected` incident stays open), gate open; same proposal re-invoked → blocked, **0 re-POSTs**; second apply → `already_resolved` | **VERIFIED** |
| **N3 negatives** | 429-then-403 (second attempt), unknown message, duplicate-client-id message, non-JSON body, and an unreviewed buying-power variant produce **no** proof and stay gated | `test_n3b` ×5 — all `submission_uncertain`, no `broker_rejection_proof`, resolve refused "ambiguous" | **VERIFIED** (conservative) |
| **N3 tamper / late order / audit** | Altered stored request terms → refused; broker later knows the id → "use normal reconciliation"; audit write failure → phase still `unknown`, gate and incident intact | `test_n3c` | **VERIFIED** |
| **N4 — terminal-zero add-on while holding** | An ordinary submitted-and-filled holding (entry order + fill + protected bracket, held stop hidden from open orders) plus a broker-created-then-rejected zero-fill add-on: reconciliation attributes the holding, re-reads the failed parent, rechecks the local fingerprint inside the transaction, acknowledges the add-on as `rejected`, and leaves the holding, its fills and its protection untouched | `test_n4a` — `errors == []`, gate open, `recovery_evidence.attributed_positions[0].qty == 5`, holding/fills byte-identical, parent legs `["new","held"]`, no incident, same proposal → blocked, **2 POSTs total** (holding + add-on); operator command then refuses (nothing left to do) | **VERIFIED** |
| **N4 — 403 add-on while holding, operator path** | Real-SDK 403 with a held symbol → `entry_intent_resolve --apply` succeeds with attribution evidence; holding intact | `test_n4b` — `abandoned_rejected`, `exposure_verification.attributed_positions[0].qty == 5` | **VERIFIED** |
| **N4 refusals** | Unknown open order in the symbol, unexplained broker quantity, position snapshot changing between the two reads, and a fill attributed to the failed intent all keep the gate closed | `test_n4c` ×4 — reconcile errors, gate closed, no re-POST | **VERIFIED** |
| **N4 on the real book** | see §1 read-only attribution row — both live holdings attribute cleanly | **VERIFIED (read-only)** |
| **N2 / D5** | unchanged | round-three N2 probes + stale-quantity test pass; `orchestrator.py`/`approval.py` no diff | **UNCHANGED** |

## 3. Findings

### No new application defect found in the round-four paths.

Static review of `entry_rejection.py`, `entry_recovery_exposure.py`, the client's `_capture_rejection` /
`_confirmed_rejection`, and the `finish()` fingerprint recheck, plus the probes above, found the N3 and N4
mechanisms correct on every trigger I could construct offline, and the attribution accepts the actual
holdings against the live broker shape.

### Conservative proof boundaries (not defects, but they keep the N3 failure mode alive on unreviewed inputs)

- **B1 — message whitelist.** `_rejection_classification` accepts only: `400/40010000` "request body
  format is invalid"; `403/40310000` "potential wash trade detected[. use complex orders]" and
  "insufficient buying power[ (required: x, available: y)]"; `422/42210000` six exact strings. Any other
  genuine 4xx (e.g. "insufficient day trading buying power", "account is restricted from trading") is
  handled by the generic path → `unknown` forever → all entries halted → `entry_intent_resolve`
  refuses. The two most likely live messages are covered, but the whitelist is **unverified against a
  real Alpaca response** (none may be manufactured), and any drift in Alpaca's wording reproduces the
  round-three N3 halt with manual SQL as the only exit. The rejected message *is* visible to the
  operator in `last_verification_error` / the incident detail. Suggested follow-up (Codex's path): a
  sanitized, classification-less proof for any well-formed first-response 4xx JSON, releasable only
  with an explicit operator flag naming the reviewed message — same audited command, no automatic path.
- **B2 — held-symbol attribution requires complete, fresh protection.** A holding whose protection is
  `DEGRADED`/`UNPROTECTED` (or whose stop leg quantity was not adjusted after a partial target fill)
  cannot attribute, so a failed add-on stays gated until protection is restored. Consistent with the
  watchdog's own rule; recorded so nobody reads it as a recovery bug.

### Deployment-tool evidence limitation (recorded, independently re-checked)

The pinned round-four helper parsed only boolean `launchctl print-disabled` output while this Mac
emits `enabled`/`disabled`, so the manifest's intermediate "prior-disabled" fields are unreliable.
Codex's corrected verifier (`native_words_and_booleans_v2`, 00:11:56 SGT) and my own independent
`launchctl print-disabled` read (all six `enabled`) agree on the **final** state. The original manifest
must not be cited for generic prior-state preservation; the corrected helper is for future rollouts.

### Unchanged observations

The unhealthy-monitor terminal block (round two) and D5 sizing rejection remain as recorded.

## 4. Coordination

No execution code edited. Nothing in this round needs an edit before the first live entry; B1 is a
judgement call for CK (breadth of accepted rejection messages vs. manual-SQL risk). META's natural
exit on 2026-09-15 remains the first live exercise of the exit lifecycle.
