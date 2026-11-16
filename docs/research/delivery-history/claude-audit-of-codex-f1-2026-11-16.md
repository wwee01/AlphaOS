# Audit of Codex F1 (same-pass fill booking) — Claude, 16 November 2026 SGT

**Reviewed revision:** `13b5645eb257582529a9134c18ebba829e6d26e2`, branch `codex/f1-same-pass-fill`, one
commit on top of production `main` `378a282`. **Handoff:** Codex `CLAUDE-F1-HANDOFF-2026-11-16.md`.
**Verdict: no defect found; the original failure is repaired; mergeable on CK's instruction.**
Not merged, not deployed, no production write, no broker call.

## What changed

Four files, 122 changed application lines, no schema, setting or service change.

- `orchestrator._route_campaign` opens a per-pass set of the entry orders this dispatch thread itself
  submitted, and clears it in `finally`.
- `orchestrator._assess_current_risk`, when called from `_execute_owned` with that set: if the fresh
  exposure read stops on an unreconciled broker position whose verified origin is one of those
  orders, it books the fill and repeats the whole read. Each owned order can be booked once per check.
- `order_manager._book_verified_entry_fill` re-checks, inside one write transaction, that the
  symbol's open positions, the entry-intent gate, the order row and the proposal are exactly as the
  read saw them, then calls the same two routines the monitor uses (`EntrySubmission.observe`,
  `_open_real_position`) and marks the proposal filled.
- `account_exposure._verify_unreconciled_fill` now returns its evidence instead of discarding it.
  What counts as verified is unchanged: one acknowledged bracket entry, fully filled, with both
  protective legs live at the held quantity.

The booking runs under the entry-submission lock and monitor ownership that `_execute` already holds,
so the monitor cannot reconcile the same order at the same time.

## Verification

| Check | Result |
|---|---|
| Full suite on `13b5645` (private clone, explicit exit code) | **4,849 passed, 5 skipped, 2 failed, EXITCODE=1** (29 min). Both failures are in `tests/test_last30days_reddit_deadline.py`; see the note below |
| `ruff check alphaos scripts tests` | exit 0 |
| `mypy alphaos` | exit 1 — the one existing typing error at `alpaca_data.py:238` (271 files); nothing new |
| Codex's 22 tests + exposure, deferral and routing files | 104 passed |
| My archived 15 November probes on this commit | 7 pass; the 2 that pinned the broken behaviour now fail, as they should (3 submissions where they asserted 1). Left unchanged as a record |
| My 14 new probes (`claude-audit-13b5645-f1-probes-2026-11-16.py`) | 14 passed |

## What the probes establish

| Question | Probe | Result |
|---|---|---|
| Is the original failure gone? | three arms, each entry filling before the next is checked | 3 submissions, 0 deferrals; arms C, A and B all trade. The same probe run on the pre-F1 code (`1c3d3ea`) fails with 1 submission |
| Are the books the same as the monitor would write? | same-pass booking then a monitor pass, against a monitor-only run of the same fills; every non-id, non-timestamp column of the order, position, fills, order events and proposal compared per symbol | identical; same number of operator alerts |
| Does a later monitor pass double-book? | three further reconcile passes | no new position, fill, exit or status change |
| Do limits see the booked position? | `MAX_OPEN_POSITIONS=2`, three arms | third refused with "open 2 >= max 2" |
| Is the daily entry cap widened? | `MAX_PAPER_TRADES_PER_DAY=2` | 2 submissions |
| Can a pass book a fill it did not submit? | entry from an earlier pass fills; a later pass meets it | not booked; the new entry is deferred as before |
| Does the authority leak? | pass raises mid-dispatch; another thread asks during the pass | cleared on exit; the other thread sees none |
| Partial fill | parent half filled before the next read | not booked; no further submission |
| Broker quantity the entry does not explain | position three shares larger than the order | not booked; no further submission |
| Booking fails part-way | position write raises | whole transaction rolled back, including the order-state update; the monitor books it correctly afterwards |
| Read fails after a booking | broker read raises on the re-read | nothing submitted; the booking stands |
| Two owned fills waiting at once | entries 1 and 2 both fill before entry 3 is checked | both booked in the one check; entry 3 submitted |

## Findings

**None that block.** Three things to know:

- **R1 — the retry path is still effectively unused (residual of F1, low).** This commit removes the
  cause that mattered: all four post-repair lost proposals were same-pass fills. A proposal deferred
  for any other reason (monitor ownership not obtained within 10 s, another entry holding the lock,
  drift or stale data after that wait) is still only retried by the next scan, at least 105 minutes
  later against an 1,800 s expiry, so it expires. No such deferral has been seen in production yet.
- **R2 — a failed booking ends the next proposal instead of deferring it (low, fails safe).** A
  refusal inside `_book_verified_entry_fill` is caught as a generic error and returned as a terminal
  block. Nothing is submitted and the monitor books the fill later; the cost is that one proposal.
- **R3 — broker read volume grows with the book (observation, mostly from P0).** Measured: 38 broker
  GETs for a three-entry pass on an empty book — 8 exposure reads (3 at proposal time, 5 at
  submission); the pre-F1 code measured 26 GETs and 6 reads for the same pass, while trading one arm
  instead of three. Each read also fetches one order per open position, so by arithmetic a five-entry
  pass with 15 positions held is roughly 270 GETs (14 reads of 19). The broker SDK retries a
  rate-limit answer (three attempts, three seconds apart), so the likely effect is a slower pass, and
  a failed read refuses the entry rather than submitting it. Worth watching once the book fills.

## Test-suite note

The suite is not green, and the cause is not this commit. My run failed two cases in
`test_last30days_reddit_deadline.py`; Codex's run of the same commit failed a different three (two in
that file, one in `test_last30days_partial_flow.py`). Run alone three times on this commit, the
deadline file passed once and failed twice, on a third parametrisation. Those test files and
`alphaos/ai/` are byte-identical to `main`, and the same file fails intermittently on the production
tree (recorded 15 November). It is a timing-dependent test, still unfixed, and it means no run of this
suite can currently be read as a clean pass. Every other test passed in both runs, including Codex's
22 new ones and the swing-audit test repaired in `96dc7a1`.

## Deployment

Code only. Fast-forward from `378a282`; no migration, no `.env` change. The timed services load code
per tick; console and dashboard need a restart. First natural exercise is the first scan in which two
or more arms propose and the first entry fills at once.
