# Audit of Codex P0 + EXPLORE-1 — Claude, 2026-11-15 SGT

**Reviewed application revision:** `1c3d3eaf796b9ddf6c1158f6284deb52c06f682f` (final). **Configuration and
handoff record:** `319003dac5743603a780567337f415b090fe18bf` (docs only on top of `1c3d3ea`).
**Base:** `de1bfea27e9068da3fc7cd6419580e8767cad7d9`. **Production at review time:** `8e7730c` (docs-only ahead
of base), six services running the pre-campaign code, broker flat.

**Commits reviewed, each diff read in full:** `03cea70`, `e9471dd` (P0.1 overlay import) · `20e6d0f` (health
exits) · `bbbe8cf` (heartbeat ping) · `666669b`, `9442ae0` (per-symbol cap, deferral) · `2066b2b`, `c6a5268`,
`1c3d3ea` (EXPLORE-1) · `4931551`, `14b5d36`, `319003d` (handoff docs, settings record).

**Method.** A private clone of the Codex checkout at `1c3d3ea` (Codex's tree untouched); full suite, ruff and
mypy there; every diff read; offline probes on a fake paper broker with the real exposure read, frozen clock
and production's scan windows / TTL / drift limit; read-only replays of production data (ledger opened
`mode=ro`, service logs parsed); the schema migration run on a scratch `.backup` copy of the ledger; the
approved settings loaded through the reviewed loader on top of production's environment. No production
code, configuration, service, order or ledger row was changed. One inert side effect: the branch objects
were fetched into production's git object store for a merge dry-run (no ref, index or working-tree change).

Probe and replay files beside this one: `claude-audit-1c3d3ea-probes-2026-11-15.py` (9 cases),
`claude-audit-1c3d3ea-route-replay-2026-11-15.py`, `claude-audit-1c3d3ea-exit-replay-2026-11-15.py`.
Run the probes with
`ALPHAOS_REPO="$PWD" PYTHONPATH="$PWD" .venv/bin/python -m pytest -q -p no:cacheprovider <file>`.

## 1. Verdict

**No safety defect found. Nothing here can create exposure the existing gates would have refused.** Every
failure I could construct fails toward "no trade".

**The suite is red on the final revision** because of one test this branch broke (F4, a one-line fix). I
would not merge a deterministically failing test to `main`; fix it, hand over the new SHA, and I will
re-verify that delta.

**It does not yet deliver one thing it was built for:** a deferred entry is never retried in production
(finding F1). And the campaign will produce fewer arm-A trades than my own brief told CK (finding F2 — my
error, not Codex's).

## 2. Verification results

| Check | Result |
|---|---|
| Full suite on `1c3d3ea` (`pytest -q tests/`, private clone, explicit exit code) | **4,827 passed, 4 skipped, 3 failed, EXITCODE=1.** One failure is deterministic and introduced by this branch (F4). Two are in `tests/test_last30days_reddit_deadline.py`, the timing-dependent file already recorded on 2026-11-14; the branch does not touch it, and it fails intermittently on the production tree too (1 pass, 2 fails in three isolated runs today). |
| `ruff check alphaos scripts tests` | exit 0 |
| `mypy alphaos` | **exit 1** — one error, `alphaos/data/providers/alpaca_data.py:238` (typing only; in the retained overlay; disclosed by Codex). Runtime-safe: `dict.get(None)` returns `None` and falls through `or {}`. |
| My probes (9) | 9 passed — one of them demonstrates F1 |
| My archived round 2–4 probes on this revision | all pass except the one that pinned the pre-round-three N2 bug (expected since `8c23a53`) — no regression in entry recovery, rejection proof, held-symbol attribution, N2 deferral |
| P0.1 — 23 overlay files + 2 frozen docs in `03cea70`/`e9471dd` | **byte-identical** to the files running in production today (SHA-256, 25 of 25) |
| P0.2 — scheduler exit code, replayed over 14,286 recorded research ticks | 65 ordinary "monitor busy" skips change 1 → 0; all 144 ticks with a real fault (execution incident, protection block, stale monitor, failed job) still exit 1 |
| P0.2 — heartbeat exit code, 8,986 recorded ticks | 7,331 observer-only failures change 1 → 0; the 8 genuine monitor-health failures still exit 1 |
| P0.3 — per-symbol cap on the real exposure read | held symbol and working-entry symbol both blocked at proposal time with reason "per-symbol cap"; other symbols unaffected; all 46 historical order rows are terminal, so no symbol is stuck occupied |
| P0.4 — hard cases stay hard | unknown / unresolved submission, unexplained broker position, unverified protection, execution incident, fused or stalled monitor: still terminal refusals (Codex tests + my archived probes) |
| External heartbeat | disabled by default; URL excluded from `repr`, config snapshot, lineage hash input and alert text; result carries status only; no redirect, no proxy |
| EXPLORE-1 routing on real data — 1,135 live candidates, 95 scans, 32 trading days since 1 Oct | A 30 · B 8 · A+B 0 · control-eligible 519 · not routable 578 (552 short). Evaluator levels usable for 556 of 557 routed rows; one config fallback |
| Control draw | one per window; seed = SHA-256 of date + window; eligible set and seed persisted; second scan in the window draws nothing; reproducible from the stored record |
| Slot fairness | arm's 11th slot refused only while another arm has a live pending proposal; A+B counts toward both |
| Legacy / flag transitions | untagged pending proposal cannot execute once the flag is on (even by manual approval); tagged proposal cannot execute once it is off; manual override and demo paths refuse |
| Degraded scan | no evaluation, no draw, no proposal, no retry |
| Lineage | arm and policy version carried proposal → entry order → position → exit order → trade outcome → candidate outcome; undrawn control-pool members stay in the outcome table with their own opinions, so the control can be compared with its pool |
| WATCH-1 / quarantine | `_resolve_decision` still runs and records the old combined decision and the WATCH diagnostic; no change to `stats/research_validity.py`, preregistrations or resolver |
| Schema migration on a scratch copy of production | 15 nullable columns added across 7 tables in 0.01 s; original columns of 13 tables hash-identical before and after; no value written to any new column |
| Approved settings record | loads cleanly on top of production's environment; startup checks all pass; shadow 1,800 of 7,200 sits exactly on the 25 % guard (1,801 refused; 7,200 → 5,000 refused); daily-loss limit 3 % unchanged; heartbeat disabled |
| Merge dry-run onto `8e7730c` | clean; the §9 rows are preserved |

## 3. Findings

### F1 — MEDIUM (purpose not met; fails safe): a deferred entry is never retried in production

**Where.** `orchestrator.py::_retry_deferred_entries` — called only from a scan
(`run_scan_once`, `_route_campaign`).

**Facts.** Proposal TTL is 1,800 s (34 of 35 production proposals; the other is a legacy row with none).
Scans run at most once per window (95 scans over 32 trading days since 1 Oct); the shortest observed gap
between two scans on the same day is 105 minutes. So by the next scan every deferred proposal has expired. P0.4 changes the end state from `blocked` to `expired`; the trade is still lost.

**Under EXPLORE-1.** Arms are staged and dispatched back-to-back. If the first entry fills at the broker
before the next proposal's exposure read, that proposal and every later one in the pass is deferred —
correctly — and then expires. The local position is written by the monitor 2–13 minutes later (10 of 10
production entries), far too late for the pass.

**Reproduction.** `test_f1_instant_fill_lets_one_entry_through_and_the_rest_expire_before_any_retry`:
three arms staged (C, A, B); broker answers "accepted" then fills; 1 submission, 2 deferrals; monitor
reconciles at +8 min; next scan at +115 min → both `expired`, arms A and B never traded. Controls:
unfilled entries → 3 of 3 submitted; a retry called at +8 min → one more entry goes in.

**Size.** 23 of 95 replayed scans would have staged two or more proposals; 37 of 132 staged proposals are
second-or-later. Production fills landed within about 5 s of submission in 4 of 10 entries (1.2, 3.0, 3.4, 5.3 s). My estimate is that
roughly 5–10 % of campaign proposals are lost this way. That estimate rests on ten fills.

**Also.** A monitor pass that reported any execution error still produces a terminal block until the next
clean pass (Codex kept `execution_incident` hard; disclosed). Same lost-proposal class.

**Requirement for the fix (Codex's design choice).** A second proposal in a pass must not be lost because
the first one filled. Either book the verified own fill before the next exposure read, or count a verified,
protected own fill as known exposure, or retry inside the TTL from the five-minute research tick. The third
option alone is weak: the 50 bps drift limit will refuse many retries after 5–15 minutes.

### F2 — MEDIUM (my brief, not the code): arm A will trade about half as often as I told CK

The brief said "≈1.8 symbol-days/day" for arm A. That counted short proposals. Long-only, the replay gives
**A 0.94/day, B 0.25/day, control 2.94/day — about 4.1 staged proposals a day** before any block. In four
weeks arm A has about 19 proposals and arm B about 5. The 19 December read cannot judge A or B on its own;
it can judge the control against its pool, and the system overall. 42 evaluator short proposals in the same
period are excluded (margin rule). Whether paper shorts should be admitted to arm A is CK's decision.

### F3 — LOW (design decision): the control draw is made before the per-symbol cap

`test_symbol_cap_real_read_blocks_held_and_pending_symbol_and_consumes_the_control_draw`: the window's one
draw landed on a symbol with a working entry; it was blocked and not redrawn. The live universe is 38
symbols; as the book fills, a growing share of control draws is spent this way. Excluding occupied symbols
from the eligible set (and recording that) would keep the draw rate; it also makes the draw depend on the
book. CK's call; the current behaviour is as specified.

### F4 — MEDIUM (process; no runtime effect): the branch breaks one of the tests it imports

`tests/test_swing_audit.py::test_existing_research_hook_never_runs_for_monitor` passes at `03cea70` and
`20e6d0f` and fails from `bbbe8cf` onward, every run: the worker now calls `ping_monitor_success`, which
reads `settings.monitor_deadman_enabled`, and this test passes a bare namespace without it
(`AttributeError`, `monitor_deadman.py:22`). Production is unaffected — the real settings object always has
the field. The handoff's focused runs (92, 258, 120, 399, 80 tests) did not include this file, so no
handoff claimed a green suite, but none reported this failure either. Fix: give the fixture the attribute.

### Low-severity items

- **L1.** Under the campaign, a candidate that is not routed is filed with the default rejection reason.
  An evaluator proposal lost to missing ATR, an out-of-range holding period or short direction is recorded
  as `OPENAI_REJECT` / `INVENTED_CATALYST` at stage `explore_1`. The evaluation row keeps the true flags.
- **L2.** `MAX_PAPER_TRADES_PER_DAY=20` counts every order row created that day, exit records included —
  it is "20 order rows", not "20 entries". At about four entries a day it will not bind.
- **L3.** `.env.example` does not list `EXPLORE_1_ENABLED` or `MONITOR_DEADMAN_*`.
- **L4.** The heartbeat ping runs while the monitor still owns the account (5 s socket timeout, DNS
  unbounded). Disabled today; move it after release before enabling.
- **L5.** R-multiples are not comparable across arms: arm A uses the ATR stop (mean 3.6 % from entry), B and
  C use the evaluator's own stop (4.3–6.7 %). The source is recorded per proposal. Compare arms on
  market-adjusted forward return, as the scorecard already specifies.

### Observations for CK

- **"Frozen" work is still running.** The committed research hook runs the swing availability audit every
  research tick while `data/swing-audit-activation.json` exists, and the paper observer still runs inside
  the heartbeat (now without affecting its exit code; its alerts are off). Removing the activation file
  stops the first without a code change.
- **Entries are sent later after their quote than before.** Staging dispatches every arm after the last
  evaluation — on average 8 minutes after the scan-time quote, 21 at most (95 scans since 1 Oct); the old
  flow dispatched each candidate straight after its own evaluation. The automatic path has never had a
  fresh-quote or drift check (the manual and retry paths do). The limit price caps what is paid, so the
  cost is adverse selection, not overpaying: fills favour names that fell during the scan. Gate unchanged;
  exposure to the gap is larger. It applies to all arms alike.
- **Deployment mechanics.** The production tree holds the 25 overlay files as uncommitted or untracked
  copies. Git will refuse the merge until they are removed or restored; they are byte-identical to the
  commit, so nothing is lost. `2066b2b` and `c6a5268` must not be deployed on their own.
- **Unchanged and still required:** shadow latch stays until a fresh preview passes every gate; external
  heartbeat stays disabled; the 28 November scorecard is not built.

## 4. Coordination

No application code edited. F4 should be fixed before merge. F1 is Codex's path and can follow. F2 is corrected here and in §9; the brief's dates stand,
but the four-week read must be described as a control-and-overall read, not an arm-A verdict.
