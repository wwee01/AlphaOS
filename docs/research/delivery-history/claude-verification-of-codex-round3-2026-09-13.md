# Verification of Codex round-three repair `8c23a53` — Claude, 2026-09-13 SGT

**Reviewed revision:** `8c23a53dbddd5ff36114d0c78c90de124fe30949` (production HEAD, `main`, tracked tree
clean). **Compared against:** my round-two record at `0d2a81c`. **Range:** `0d2a81c..8c23a53`,
6 commits, 16 files, +1,383/−56.
**Method:** full offline suite + ruff + mypy on the deployed checkout; read-only ledger diff against
Codex's round-three pre-deploy backup (`round3-deployment-20260913T143207Z-69e497bd.db`,
SHA-256 `9b2f5985…` — matches the deployment record); read-only service/interface/readiness checks;
offline probes on the repo's own fakes plus the **real alpaca-py transport** with socket creation
replaced (Codex's technique, re-used independently). No production writes, no broker mutation, no
`.env` change, no code edited, nothing merged, `entry_intent_resolve` **not** run against production.
Probe files beside this one: `claude-verify-8c23a53-entry-recovery-2026-09-13.py` (11 cases),
`claude-verify-8c23a53-n2-deferral-2026-09-13.py` (4 cases). Round-one and round-two probes are
preserved unchanged.

## 1. Verification results

| Check | Result |
|---|---|
| Full suite on `8c23a53` (`pytest -q tests/`, main session, explicit exit code) | **3,148 passed, 6 skipped, 0 failed, EXITCODE=0** (~28 min; skips = the same 6 calendar/live-broker-gated cases as rounds one and two) |
| `ruff check alphaos scripts tests` / `mypy alphaos` | both exit 0 (207 modules typed) |
| My round-three probes | 11 + 4 = **15 passed** (two of them *demonstrate* new defects — §3) |
| Ledger vs round-three backup — 12 tables incl. `candidate_outcomes` (22,190), `decision_adjustments` (1,314), `replay_evidence_corrections` (1,065), `approvals` (16), `paper_orders` (23), `paper_fills` (22) | **0 original rows changed in original columns; no columns added** |
| Quarantine / safety / risk engine / replay provenance / WATCH / research-inbox docs / `deploy/` | `git diff 0d2a81c..8c23a53` on `stats/research_validity.py`, `stats/preregistration.py`, `hypotheses/resolver.py`, `cards/promotion.py`, `safety.py`, `risk/risk_engine.py`, `learning/outcomes_tracker.py`, `reports/baseline_report.py`, `research/watch_advisory.py`, `docs/research/{README,REVIEWED,CHATGPT_TASK_PROMPT}.md`, `deploy/` — **empty** |
| `.env` / limits | mtime 2026-09-04 — untouched. `MAX_OPEN_POSITIONS=5`, `MAX_RISK_PER_TRADE_PCT=0.01`, `MAX_DAILY_LOSS_PCT=0.03`, `MAX_AUTO_APPROVALS_PER_DAY=5`, `MAX_UNATTENDED_APPROVALS_PER_DAY=1`, `APPROVAL_MODE=auto`, `REQUIRE_MANUAL_APPROVAL=false` |
| Shadow latch | `data/SHADOW_LABEL_SUSPENDED` present, mtime 2026-08-06 |
| Services (read-only) | six registered, all last-exit 0; installed monitor/scheduler/heartbeat plists byte-identical to `deploy/` (sha256 `7fef1b50…`, `7af996a1…`, `dc3c8421…`); last dedicated monitor completed 15:02:22Z in 4.2 s; 0 failed jobs since deploy |
| Readiness (`worker --role readiness`, read-only) | `ok=True`, `entry_ready=True`, `last_success_role=monitor`, no issue codes, no blocking incident |
| Interfaces (read-only) | console `/api/v1/health` → 200; dashboard `/_stcore/health` → 200 |
| Pending entry intents / open incidents in production | 0 / 0 |
| Mac proxy state (affects the transport-proof detector) | `scutil --proxy`: no HTTP/HTTPS/PAC/SOCKS proxy; no `*_proxy` env — the narrow `Session`/`HTTPAdapter` proof path is reachable in production |

## 2. Focus areas — verification table

| Target | Invariant now enforced | Reproduction | Result |
|---|---|---|---|
| **N1 — provably unsent entry, positive proof** | First entry POST through the standard `requests.Session`/`HTTPAdapter` that fails **before connecting** (`NewConnectionError` / `ConnectTimeoutError`) is recorded as `no_dispatch_proof{transport_never_sent}`; the gate closes; `entry_intent_resolve` can release it | `…entry-recovery…::test_n1_real_sdk_connect_refused…` — real `TradingClient`, socket creation raises `ECONNREFUSED` → exactly one outbound attempt, phase `unknown` with proof `new_connection_failure`, gate + incident open. Restart. Preview → `eligible_for_abandonment`, writes nothing. Apply without note → refused. Apply with note → `abandoned_unsent`, `state='failed'`, `client_order_id` preserved, proposal `blocked`, incident resolved, gate open; same proposal re-invoked → blocked, **0 POSTs**; second apply → `already_resolved` | **VERIFIED** |
| **N1 — negative (my original generic trigger)** | A generic `ConnectionError("connection refused")` raised by the caller carries no transport evidence → stays `unknown` with **no** proof; `entry_intent_resolve` refuses | `test_n1_generic_connection_error_remains_ambiguous…` — refused with "ambiguous"; gate stays closed by design | **VERIFIED** (archived round-two probe unchanged) |
| **N1 — crash before / after the dispatch claim** | `prepared` (never claimed) is provable (`dispatch_not_claimed`); `submitting` (claimed, no proof) is not; an abandoned intent revokes a paused sender's claim | `test_n1_crash_before_claim…` — prepared → restart → reconcile still errors → preview proof kind `dispatch_not_claimed` → apply → `claim_dispatch(stale_row)` raises → 0 POSTs. After-claim variant → refused "ambiguous" | **VERIFIED** |
| **N1 — refusals and rollback** | Broker knows the id → refused; local position in symbol → refused; audit write failure → abandonment rolled back, incident still open | `test_n1_apply_refuses_when…` (real-SDK proof + fake corroboration): (a) "broker knows this client ID" (b) "position evidence" (c) `log_system_event` raises → phase still `unknown`, gate + incident intact | **VERIFIED** |
| **Terminal-zero broker recovery** | A rejected/cancelled/expired parent with zero fills and no children recovers through normal reconciliation only when all intended terms match and no contradictory exposure exists | `test_tz_matching_rejected_parent…` — restart → `reconciled=1`, no errors, gate open, incident clear, no fills/positions, same proposal → blocked, **1 POST total**. `test_tz_conflicting…` ×4 — wrong limit / partial fill / active status / child fill → gate stays closed | **VERIFIED** |
| **N2 — ownership contention** | Bounded 10 s wait; a 4 s monitor hold yields exactly one submission and one receipt; timeout leaves the proposal `pending_approval` with no receipt, no attempt, no cap consumption; entry-lock contention defers identically; a fresh manual retry works once | `…n2-deferral…::test_n2a` (4 s hold → 1 attempt, 1 approval, 3.5–10 s) · `test_n2b` (timeout → `ok=False` "deferred", 0 approvals, 0 auto-approvals, status `pending_approval`, one `entry_deferred` event; retry → 1 attempt) · `test_n2c` (foreign `flock` on `.entry.lock` → deferred, 0 receipts) | **VERIFIED** |
| **D5** | unchanged | `test_final_execution_blocks_stale_approved_quantity…` in suite; no tolerance code | **UNCHANGED** |

## 3. Remaining / new defects

### N3 — HIGH (operations, reachable): a definitive synchronous broker rejection is classified as "uncertain" and can never be released

**Location.** `alphaos/execution/order_manager.py::_submit_alpaca_paper` — `except Exception` after
`except AlpacaSubmissionNotSent`; `entry_submission.py::lookup` (404 → `LookupError`);
`entry_intent_resolution.py::_proof` (requires transport proof).

**Trigger.** Alpaca answers the POST with HTTP 4xx and a JSON error body — it received the request
and **did not create an order**. alpaca-py raises `APIError(text, http_error)` (`rest.py::_one_request`,
verified on the installed 0.43.4). Real causes: `403 40310000 "potential wash trade detected"` (a
new BUY while our own take-profit SELL leg is open on the same symbol), `403` insufficient buying
power, `422` invalid input. The wash-trade case is **reachable today**: the pipeline does propose
symbols already held (`prop_768a75bce2d2` XLK, 2026-08-04, created while `pos_a551ca1bb632` was open).

**Invariant violated.** A broker-confirmed non-acceptance is treated as an unknown submission: the
intent stays `unknown`, every reconcile re-reads a 404, `pending_entry_intent` closes the entry gate,
`read_exposure` raises, and `entry_intent_resolve` refuses ("ambiguous") because a 4xx is not a
pre-connect transport failure. No operator exit except manual SQL — exactly the N1 class, on the
most likely real-world path.

**Evidence.** `test_n3_synchronous_broker_rejection…` — `APIError('{"code":40310000,…}')` at POST →
`submission_uncertain`; 5 reconcile passes → errors; gate closed; resolve refused; a second proposal
blocked; `read_exposure` raises.

**Proposed repair (coordinate with Codex).** Add a *broker-confirmed rejection* proof alongside the
transport proof: `isinstance(exc, APIError)` with `status_code ∈ {400, 403, 422}` and a parsed Alpaca
error body, recorded at the moment of failure, plus the existing two fresh client-id 404s and two
flat reads at resolution — release via the same audited `entry_intent_resolve` (never automatically).
Optionally auto-mark such an intent `acknowledged/rejected` in `reconcile` after N consistent 404s
when the rejection proof is present. Pre-round-2 code treated every exception as
`ALPACA_SUBMIT_FAILED`; round two/three made it fail-closed without a door.

### N4 — MEDIUM (operations, reachable): terminal-zero recovery is impossible when the symbol is already held

**Location.** `entry_submission.py::_verify_recovery` terminal-zero branch → `_no_local_exposure`
(`positions WHERE symbol=? AND status='open'`), `_flat_positions`, `_no_orders` (any open order in
the symbol, including our own protective leg); `entry_intent_resolution.py` refuses because the
broker knows the id.

**Trigger.** An add-on entry in a held symbol is created by the broker and then rejected with zero
fill (asynchronous reject). The held position, its broker exposure and its take-profit leg all count
as "contradictory evidence", so reconciliation refuses forever and the operator command points back
to reconciliation.

**Evidence.** `test_n4_terminal_zero_rejection_in_a_held_symbol…` — held XYZ (local + broker +
protective bracket) + rejected zero-fill add-on → 5 reconcile passes all error, phase never
`acknowledged`, gate closed, resolve refused, second proposal blocked.

**Proposed repair.** Scope the contradiction checks to the intent itself: exposure attributable to
*other* verified positions/orders (matching `positions.order_id` ≠ this intent and open orders whose
ids belong to a known protection lineage) must not block a zero-fill terminal recovery; only fills or
orders attributable to *this* client id do. Alternatively, block add-on proposals for held symbols
at scan time — which would also remove the wash-trade path in N3.

### Observation (unchanged from round two, LOW): unhealthy-monitor block is terminal, not deferred

`_execute_checked` still returns `blocked` (proposal → BLOCKED) when `monitor_health.entry_ready`
is False, e.g. after a single transient `get_order` failure in the latest monitor pass.
`…n2-deferral…::test_n2d` reproduces it. Same lost-proposal class N2 fixed for ownership contention;
out of N2's stated scope, so recorded, not counted against the fix.

## 4. Preserved invariants — verified

Historical evidence ✓ (12-table diff) · all 15 registrations quarantined, no code change ✓ · replay
provenance code unchanged ✓ · shadow latch ✓ · `.env` and both caps ✓ · WATCH diagnostic-only, module
unchanged ✓ · six services, definitions match repo ✓ · no new order/fill/exit (23/22/10) ✓.

## 5. Coordination

No execution code edited. N3 is the one I would want closed before the first live entry: it is the
ordinary way a paper broker says no, and today it parks the whole entry pipeline behind an
unresolvable gate. N4 rides with it (same recovery module). Both are Codex's paths; they remain
proposals until CK assigns them.
