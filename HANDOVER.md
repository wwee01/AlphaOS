# HANDOVER

**Checkpoint: 2026-07-03 (post PR2.5 merge) · branch `main` @ `5387320` · tests currently 418 passed / 3 skipped / 3 FAILED on `main` (pre-existing, unrelated, fix ready but not yet merged — see banner + §6) · working tree clean · mode PAPER · execution `alpaca_paper` · AI = LIVE (OpenAI) · real-money UNREACHABLE · 0 open positions**

> Single entry point for the next session. This project keeps no other handover docs — everything is here. Verify state before trusting any of it (commands in §8).
> ✅ **RESOLVED — protection watchdog is now built and merged** (PR2.5, PR #21). The META incident's root cause (day-TIF bracket legs expiring at session close, no watchdog to notice) is fixed at the source (multi-day/swing holds now submit GTC protective legs by default) AND backstopped by a new watchdog that detects missing protection or a broker-closed/local-open mismatch every monitor pass and blocks all new entries until resolved. Full detail in the changelog below. Two Opus audits (initial + narrow re-audit after a HIGH-1 fix) both concluded **approve**; several MEDIUM/LOW follow-ups remain (§4/§7) but none block current safety.
> ⚠️ **NEW this checkpoint**: post-merge verification surfaced a **pre-existing, unrelated flaky test** (`tests/test_mfe_mae.py`, 3 tests) — date-dependent, not caused by PR2.5 (verified zero-diff on every file involved, and reproduced identically on pre-PR2.5 `main`). Root cause + fix below (§6). Fix is committed and pushed on `fix/mfe-mae-date-dependent-flake` but **not yet merged** — `main` will show 3 failures until that PR lands.

## Changelog (most recent first)
- **PR2.5 — Broker protection watchdog + multi-day TIF root-cause fix** (`main` @ `5387320`, PR #21, this checkpoint). Implements `docs/roadmap/protection-watchdog.md` in full. **Root-cause fix**: `AlpacaClient.submit_bracket()` no longer hardcodes `time_in_force=day` — new `_resolve_tif()` submits `gtc` for any `max_holding_days >= 1` (any hold that can cross a session boundary) unless the new `ALLOW_DAY_TIF_FOR_MULTIDAY_POSITIONS` flag explicitly opts back in (default `false`); only the intentionally-intraday `max_holding_days==0` daytrade experiment keeps `day`. **Watchdog**: new `alphaos/execution/protection_watchdog.py` runs every monitor pass (after `reconcile()`, before the local watchdog — ordering matters, avoids false positives on same-pass legitimate closes) and checks every open `alpaca_paper` position's stop/target legs + whether the broker still holds the position at all. Missing stop → `unprotected` (CRITICAL, blocks ALL new entries system-wide). Missing target only (stop still live) → `degraded` (WARNING, logged once per transition, does NOT block). Local-open/broker-closed → `closed_mismatch` (CRITICAL, blocks). **Detect + block only** — the watchdog never calls `close_position()`, cancels, or submits anything itself; a human resolves via new `alphaos protection_resolve <id> --exit-price X` (closes via the same `close_position()` path every other exit uses, only for `closed_mismatch`) or `alphaos protection_ack <id>` (lifts the block without touching the position, for `unprotected`/`degraded`). Self-healing: an incident auto-resolves if a later pass reconfirms protection is restored. New `alphaos protection_status` CLI, `alphaos status`/dashboard surfacing. +27 tests incl. a direct reproduction of the META incident and a genuine restart-recovery test (file-backed DB, not `:memory:`, to prove no in-memory state is relied on). Additive schema only (`protection_checks` table, `positions.protection_status` column); `alphaos/safety.py`/scanner/AI-labeller/risk/freshness-guard verified zero-diff.
  - **Opus audit + HIGH-1 fix, same PR**: initial audit found one required fix — the TIF boundary was `max_holding_days > 1`, leaving 1-day swings (which CAN cross a session boundary, unlike the 0-day daytrade experiment) still exposed to the exact META failure mode. Fixed to `> 0` in both places the boundary lived (submission logic + the watchdog's own `tif_appropriate` check, which had the same threshold duplicated). +3 boundary tests. Narrow re-audit confirmed the fix and **approved merge**. Remaining audit follow-ups (non-blocking, tracked in §4): `check_error` (broker lookup failure) fails open rather than blocking; qty-mismatch is detected but not surfaced; no live-gated test confirms Alpaca actually accepts GTC brackets; a `REPLACED` leg state isn't treated as live-but-different; short-position qty sign could false-positive a qty mismatch if ever wired up.
- **Flaky test discovered + fixed (separate PR, not yet merged)**: post-merge full-suite verification found `tests/test_mfe_mae.py` failing 3/22 — confirmed unrelated to PR2.5 (zero-diff on every file involved; reproduces identically on pre-PR2.5 `main`). Root cause: `mock_provider.py` seeds mock prices per `{symbol}:{market_date()}` (deterministic per calendar trading day, by design); `conftest.py`'s `inject_pending_proposal` hardcodes stop=3%/target=6% of entry, which makes `target` **always** algebraically exactly `entry + 2*risk`; three tests used a `+2R` monitor() price probe that can land exactly on that target depending on the day's rounding, closing the position via a target-hit on the first `monitor()` call instead of leaving it open for the intended second probe. This session ran long enough to cross a trading-day boundary and hit exactly this. Fix: probe at `+1.5R` instead (0.5R margin, verified safe across 400 simulated trading dates) — test-only change, pushed on `fix/mfe-mae-date-dependent-flake`, **not yet merged**.
- **Both audited branches merged to `main`** (2026-07-03). `feat/labeller-failsafe-visibility` → PR #19, then `feat/measurement-foundation` (incl. the Fable 5 review docs + incident/roadmap docs committed as `bee083b`) → PR #20, merged in that order via the GitHub web UI (`main` was at `0248a71`). Re-verified post-merge at the time: 394 passed, 3 skipped, 0 open positions, `real_money_trading=unreachable`, `manual_approval=required`, `kill_switch=off`. No new code changed as part of this merge — integration/verification only. Numerous other stale local feature branches exist (already merged individually over prior sessions) — harmless, cleanup optional (§7).
- **Incident: META protection mismatch, detected + resolved** (2026-07-02). Routine handover verification surfaced that META's broker-side stop (`canceled`) and target (`expired`) orders had both expired at the end of their first trading session (`time_in_force=day` on a multi-day-hold bracket, no scheduler to resubmit) — the position sat naked for ~19 hours while price fell through the original stop, undetected by the local ledger (stale). User confirmed flatten; executed cleanly (0 open orders, 1 position closed, verified read-only after); local ledger manually reconciled via the standard `close_position()` path (not raw SQL) since `OrderManager.reconcile()` was confirmed NOT to detect a non-bracket-leg close — itself a finding, now tracked. Full writeup + timeline: `docs/incidents/2026-07-02-meta-protection-mismatch.md`. Follow-up roadmap item opened: `docs/roadmap/protection-watchdog.md`. No code changed as part of this incident response — detection + manual resolution only.
- **Opus audit fix pass** (`e00595e`, this checkpoint). Opus audited `feat/measurement-foundation` (verdict: APPROVE WITH REQUIRED FIXES) and found two real bugs in the just-built measurement layer. Both fixed on the same branch: **HIGH-1** — forward-return windows were anchored on `candidate_outcomes.created_at_utc` (seed time) instead of the true decision time, which would silently corrupt any backlog-catchup seeding (a candidate decided 30 days ago, seeded today, would have its next bar mislabeled a "1-day" return using a stale price). Fixed with a new additive `decision_at_utc` column sourced from each type's real source row, plus a self-healing repair path for any row missing it. **MEDIUM-1** — MFE/MAE folding could report a positive MAE for a trade that was favorable at every observed point (no anchor). Fixed: entry is now an implicit R=0 observation in both the live and backfill excursion paths (textbook MFE≥0, MAE≤0), while a genuinely stop-less position still correctly returns `(None, None)` rather than a false zero. **NIT-1** — doc comment added. MEDIUM-2 (a minor backfill edge case) deliberately left as follow-up per the audit's own scoping. +18 tests (381 total, was 363). No trading/execution/gate/scheduler behavior touched — see full audit + fix report in this session's transcript if unclear. (This fix's correctness was independently confirmed live during the incident above: the reconciled META exit correctly shows `mfe=0.0` — never favorable at any monitored point — instead of the pre-fix bug's wrong small-negative value.)
- **Measurement foundation — PR1 + PR2** (`eede83c`, this checkpoint, on top of Fable 5's architecture/roadmap review). Fable 5 reviewed AlphaOS end-to-end (packet + response now live at repo root: `FABLE_REVIEW_PACKET.md` / `FABLE_REVIEW_RESPONSE.md`, currently **untracked** — see §7) and recommended measurement before scanner v2/universe expansion/autonomy. Two parts landed: **PR1 (MFE/MAE)** — turned out to be a bug fix, not new work: per-check excursion tracking already existed (`monitoring_snapshots`) but `close_position()` discarded it and wrote a crude `%`-based approximation instead; fixed to fold the exit tick into the real running R-based extremum, plus an idempotent backfill for old closed trades (`alphaos backfill_mfe_mae`). **PR2 (counterfactual outcome ledger)** — new `alphaos/learning/` package + `candidate_outcomes` table: every scanned candidate/proposal/reject/armed-watch/user-override becomes learnable data via 1/3/5-day forward returns and bracket replay, whether or not it became a real trade (NOT a backtest — only replays decisions AlphaOS actually recorded). CLI: `alphaos outcomes_update`, `alphaos outcomes_report`. All additive, `SCHEMA_VERSION` stays 3.
- **Live calibration run + 3 bugfixes** (merged to `main` @ `e381096` — PRs #16/#17/#18). Fixed a labeller output-token-budget bug (`220→800`) that had been silently truncating every real labeller call and blocking ALL live proposals; fixed the live Alpaca catalyst provider (SDK `NewsSet` parsing bug, was silently returning zero catalysts); shipped the User-Override attribution report. Live-fired: **META** long, real Alpaca-paper bracket, filled 41 @ 618.78 — see the incident entry above for how this trade ultimately closed.
- **Labeller fail-safe visibility** (`feat/labeller-failsafe-visibility` — health/status/dashboard/daily-report coverage for labeller output, `alphaos/ai/labeller_health.py`; now merged, see top entry).
- Prior: Roadmap 2.8 (Armed Watch + labeller reasoning + User Override Mode), 2.7 (last30days polarity), 2.6 (gated labeller override), 2.5 (last30days enrichment), 2.4 (catalyst enrichment), 2.3 (interest scanner + AI labelling) — see git log for full history.

---

## 1. Current project state
AlphaOS is a **learning-first, paper-trading "operating system"** on a Mac mini, Python 3.12 venv at `.venv` (uv). `main` is the single line of development. Pipeline (shape unchanged, one new stage added this checkpoint): **Scanner → Candidate Packet → AI Labeller → Catalyst/last30days/Polarity enrichment → decision combine → Armed Watch → gates → manual approval (+ User Override layer) → sim/paper execution → monitor/reconcile/exit → NEW: protection watchdog → ledger → counterfactual outcome measurement.** Real-money trading remains `unreachable` throughout. The top open items are: (1) merge the pending flaky-test-fix PR (§9), (2) decide PR3 (scheduler) vs. the watchdog hardening follow-ups (§4/§9).

## 2. What was just implemented (this checkpoint)
- **Broker protection watchdog + multi-day TIF fix** (PR2.5, PR #21) — see changelog for full detail. New `alphaos/execution/protection_watchdog.py`; TIF root-cause fix in `alphaos/broker/alpaca_client.py`; new `positions.protection_status` column + `protection_checks` table; new CLI `protection_status`/`protection_resolve`/`protection_ack`; `alphaos status` + dashboard surfacing.
- **Flaky test fix** (separate PR, not yet merged) — `tests/test_mfe_mae.py` price-probe fix, test-only.
- Everything from the prior checkpoint (measurement foundation, labeller fail-safe visibility) is unchanged and already on `main` — see git log / prior HANDOVER revisions for that detail.

## 3. What is working (verified this checkpoint)
- **PR2.5's own tests: 27/27 pass**, independent of the unrelated flaky-test issue below.
- Reproduced the exact META incident end-to-end against the new watchdog: a 5-day-hold bracket now submits `gtc` (not `day`); when both legs are forced missing anyway (simulating some other loss-of-protection path), the watchdog detects it within one monitor pass, opens a CRITICAL incident, and a subsequent new proposal for a *different* symbol is correctly blocked with `PROTECTION_INTEGRITY_FAILURE`.
- Verified via direct probe (not just tests): a stale `protection_ack` cannot durably unblock a still-unprotected position — it re-blocks on the very next watchdog pass. A `check_error` (broker lookup failure) does NOT block — tracked as a known follow-up, not a regression (§4).
- Safety invariants reconfirmed: 0 orders/cancels/auto-closes ever originate from the watchdog; `real_trading_guard` fires before the new protection check in `execute_proposal()`; `alphaos/safety.py` zero-diff against pre-PR2.5 `main`; scanner/AI-labeller/risk/freshness-guard/strategy all zero-diff.
- The flaky-test fix verified stable across 400 simulated trading dates (not just today's), so it's a structural fix, not a lucky patch.

## 4. Partially implemented (and what's missing to finish)
- **Protection watchdog follow-ups (Opus audit, non-blocking)**:
  - `check_error` (a broker per-order lookup failure on an otherwise-open position) does NOT block new entries — it's logged WARNING but treated as "unverifiable, carry on." Recommend escalating a *persistent/consecutive* `check_error` to blocking, especially before/alongside PR3 (an unattended loop makes this more consequential).
  - Qty mismatch (local vs. broker) is computed and stored per check but never surfaces (no log, no status effect). Recommend at least a WARNING log + counting it in `status_report`.
  - No live-gated (`RUN_LIVE_ALPACA_TESTS=true`) test confirms the real Alpaca paper API actually accepts a GTC bracket — the whole TIF fix rests on this being true; failure mode is safe (submission would be rejected → `_blocked`, not a silent naked position) but unverified against the real API.
  - A `REPLACED` leg state isn't in `_LIVE_LEG_STATES` — a stop modification (replace) could transiently read as unprotected (false alarm, errs safe, not confirmed against real Alpaca replace behavior).
  - `_simulate_fill` (internal-sim path) doesn't pass a resolved `time_in_force` — harmless (sim positions are never watchdog-checked, no real expiry risk), but inconsistent with the real-paper path.
- **Attribution report** exists (Roadmap 2.8 follow-up) but has never been exercised on *real* (non-mock) override data — no live overrides have accumulated yet.
- **Cost-model calibration**: still 1/20 real fills (the META entry, now closed). Needs many more real paper fills.
- **MEDIUM-2** (backfill treats a transient empty-bars fetch as permanently `unavailable`) — deliberately left as a fast-follow per the measurement-foundation audit's own scoping; not yet done.
- **Measurement data has near-zero real volume yet**: the counterfactual ledger works correctly but has only been run against mock scans and hand-built fixtures — no real scheduled cadence exists to actually accumulate it (that's PR3, not started).

## 5. Not done yet (deferred / future)
Per the Fable 5 review's PR-sized roadmap (`FABLE_REVIEW_RESPONSE.md`), none of these are started: **PR3 scheduler v1.5** (the next recommended step — daily scan+monitor+outcomes_update+digest; should call `run_monitor_once()`, which already runs reconcile→watchdog→local-monitor in the correct order), decision-lineage stamping, earnings-proximity flag, proposal TTL, TQS v0, attribution v2 (ΔR), portfolio concentration monitor, playbook registry v0, generalized anomaly monitor, NightDesk export. Also still open from before: real Alpaca paper execution beyond the one (now-closed) META trade, universe expansion (deliberately deferred — shadow-rank only per the review), the mock-mode-doesn't-hard-disable-last30days footgun (§7.6).

## 6. Test results
- **On `main` @ `5387320` right now: 418 passed, 3 skipped, 3 FAILED.** The 3 failures are `tests/test_mfe_mae.py::test_monitor_pass_folds_running_mfe_mae_in_r_terms` / `test_close_position_folds_exit_tick_into_running_excursion` / `test_favorable_then_adverse_path_captures_both_correctly` — the pre-existing, date-dependent flake described in the changelog. **This is NOT a PR2.5 regression** (verified zero-diff on every file involved; reproduces identically on pre-PR2.5 `main` @ `0248a71`).
- **Fix is ready**: branch `fix/mfe-mae-date-dependent-flake` (pushed, PR not yet opened/merged). With that branch's one file (`tests/test_mfe_mae.py`) applied: **421 passed, 3 skipped, 0 failed.** Merge that PR first thing next session (§9) — after it lands, `main` should show 421/3/0.
- Skips = `tests/test_live_alpaca.py` (gated behind `RUN_LIVE_ALPACA_TESTS=true`). Fully hermetic otherwise.
- New this checkpoint: `tests/test_protection_watchdog.py` (27) — see PR2.5 changelog entry for coverage detail (META reproduction, TIF boundary at every holding-day value, reconcile-before-watchdog ordering, alert dedup, restart-recovery against a file-backed DB, human-only resolution never auto-close).

## 7. Known risks / blockers
1. **Merge the pending flaky-test-fix PR** (`fix/mfe-mae-date-dependent-flake`) — until then `main`'s test suite shows 3 failures that look alarming but are understood, pre-existing, and unrelated to PR2.5. Low urgency (test-only, no execution-path impact) but do it before trusting a raw `pytest` count again. **This is the top open item — see §9.**
2. **Protection watchdog follow-ups** (§4) — none are urgent/blocking, but `check_error` failing open is the one worth prioritizing, ideally before or alongside PR3.
3. **Stale local feature branches**: many older local branches (`feat/alpaca-paper-validation`, `feat/cost-model-calibration`, `feat/interest-scanner-ai-labelling`, etc. — already merged individually in prior sessions) are still sitting in local `git branch -vv`. Harmless (all content is in `main`); safe to `git branch -d` at your discretion, not done automatically.
4. **Stray WAL files**: `data/demo-chain.db-shm` / `-wal` — pre-existing, untouched, harmless; clean up at your discretion.
5. **Chain cost**: every real `interest_scan` still costs OpenAI money; `LAST30DAYS_ENABLED`/`POLARITY_ENABLED` are on in `.env`.
6. **Operational footgun (documented, not fixed)**: `ALPHAOS_MODE=mock` does NOT disable `LAST30DAYS_ENABLED=true`, causing subprocess hangs during ad-hoc CLI testing. Always pair mock-mode testing with explicit `LAST30DAYS_ENABLED=false LAST30DAYS_POLARITY_ENABLED=false EXECUTION_PROVIDER=simulated_internal`. Still not fixed.
7. **Cannot push to `main` from this environment** — feature branches push fine over the SSH deploy key; any future `main`-bound change needs a PR + GitHub web UI merge, same pattern as this checkpoint.
8. **Mock market data is date-seeded** (`{symbol}:{market_date()}`) — by design, for reproducibility, but it means any test that derives exact price/R-multiple boundaries from mock prices (like the flake just fixed) can silently re-break on some future date if it re-introduces an exact-boundary probe. Worth keeping in mind when writing new mock-price-dependent tests: leave a margin, don't probe exactly at a computed boundary.

## 8. Exact commands to run next
```bash
cd "/Users/ck/Documents/Claude Playground/AlphaOS"

# confirm the account is actually clean (read-only) before doing anything else
.venv/bin/python -c "
from alphaos.config.settings import load_settings
from alpaca.trading.client import TradingClient
s = load_settings(); tc = TradingClient(s.alpaca_api_key, s.alpaca_secret_key, paper=True)
print('open positions:', tc.get_all_positions())
"   # expect: []

# verify code state
.venv/bin/python -m pytest                 # expect (BEFORE the flake-fix PR merges): 418 passed, 3 skipped, 3 failed (known, see §6)
                                            # expect (AFTER it merges): 421 passed, 3 skipped, 0 failed
git status -sb && git log --oneline | head -6
git branch --show-current                  # expect: main

# broker protection watchdog status (new this checkpoint)
.venv/bin/python -m alphaos protection_status

# operate (chain is LIVE -> real OpenAI calls each run; use ALPHAOS_MODE=mock for safe testing)
.venv/bin/python -m alphaos status
.venv/bin/python -m alphaos outcomes_update        # seed + resolve counterfactual outcomes
.venv/bin/python -m alphaos outcomes_report        # measurement visibility (no statistical claims)
.venv/bin/python -m alphaos backfill_mfe_mae       # backfill any legacy closed trades

# SAFE mock testing (avoids the last30days subprocess-hang footgun, §7.6):
ALPHAOS_MODE=mock EXECUTION_PROVIDER=simulated_internal LAST30DAYS_ENABLED=false \
  LAST30DAYS_POLARITY_ENABLED=false ALPHAOS_DB_PATH=data/demo.db .venv/bin/python -m alphaos interest_scan
```

## 9. Recommended next prompt (paste into a fresh window)
```
Read HANDOVER.md in the AlphaOS repo first (single source of truth). Note PR2.5 (broker
protection watchdog + multi-day TIF fix) is merged to main (PR #21) — audited twice by
Opus, both times approved. No merge decision pending on that.

First: merge the pending fix/mfe-mae-date-dependent-flake PR (test-only, unrelated to
PR2.5 -- a pre-existing date-dependent flaky test discovered during post-merge
verification; full root-cause writeup in the changelog). After merging, verify
`.venv/bin/python -m pytest` shows 421 passed, 3 skipped, 0 failed on `main`.

Then help me decide, in order:
1. Which protection-watchdog follow-up to prioritize first (§4): check_error failing
   open is probably the one that matters most before automating anything.
2. Whether to do that follow-up before or alongside PR3 (scheduler v1.5,
   FABLE_REVIEW_RESPONSE.md §16) -- an unattended monitor loop makes "unverifiable
   protection state = carry on" more consequential than it is today under manual
   invocation, which is the argument for doing it first or together.

Do NOT start new feature work until we've discussed this ordering.

Hard constraints (HANDOVER §10): real-money stays unreachable; manual approval
non-bypassable; no AI/catalyst/last30days/polarity/measurement output bypasses gates;
migrations additive only; keep tests green; the counterfactual outcome ledger AND the
protection_checks table are measurement/audit-only and must never be read by any
gate/eval/labeller/risk/execution path; the protection watchdog detects + blocks only,
never auto-closes/cancels/submits.
```

## 10. Anything the next session must NOT change (hard invariants)
- **Real-money trading stays unreachable.** `REAL_TRADING_ENABLED=false`, `ALLOW_REAL_ORDERS=false`; `ALPHAOS_MODE=live` rejected. Do not touch `safety.py`. `system_health()["real_money_trading"]` must remain `"unreachable"`.
- **Manual approval is the default and non-bypassable** (`APPROVAL_MODE=manual`). No path may auto-submit or skip approval. `high_risk_narrative` proposals are manual-only regardless of approval mode.
- **No AI/catalyst/last30days/polarity/measurement output bypasses gates.** Freshness, spread, liquidity, crossed-quote, risk, sizing, daily-cap, exposure, kill switch, stop/target, market-session, price-drift gates are authoritative.
- **AI category label is ADVISORY; the override is gated + symmetric.** Default downgrade-only. When ARMED it may move the decision UP or DOWN, gated + audited (`decision_adjustments`).
- **Polarity is CONTEXT that can ARM, never EXECUTE.** Deterministic AlphaOS-side arming only; fails safe to non-arming.
- **User Override (2.8) is a SEPARATE decision layer; NEVER rewrites AlphaOS's recommendation**, never bypasses gates/approval/real-money guard, never auto-executes.
- **The counterfactual outcome ledger (`candidate_outcomes`) is PURE MEASUREMENT.** It must never be read by any gate, eval, labeller, risk check, or execution path — write-only from `alphaos/learning/`. `decision_at_utc` must stay the anchor for all forward-window math (never revert to seed-time anchoring — Opus audit HIGH-1 on the measurement-foundation PR, a real bug, not a style choice).
- **MFE/MAE stay textbook-anchored** (entry = implicit R=0; MFE≥0, MAE≤0 always) — do not revert to the old unanchored fold (Opus audit MEDIUM-1 on the measurement-foundation PR).
- **NEW — the broker protection watchdog (`alphaos/execution/protection_watchdog.py`) is DETECT + BLOCK ONLY.** It must never call `close_position()`, cancel an order, or submit an order itself. Resolution is human-triggered only, via `protection_resolve` (closed_mismatch only, requires an operator-confirmed exit price, uses the same `close_position()` path as every other exit) or `protection_ack` (unprotected/degraded, never touches the position). Do not add auto-repair/auto-flatten without explicit new intent — this was a deliberate design decision (Opus-reviewed), not an oversight.
- **NEW — multi-day/swing TIF policy**: any `max_holding_days >= 1` proposal must submit persistent (`gtc`) protective legs by default; only `max_holding_days==0` (pure intraday) may default to `day`. `ALLOW_DAY_TIF_FOR_MULTIDAY_POSITIONS` must stay `false` by default — this is the fix for the 2026-07-02 META incident's root cause (Opus audit HIGH-1 got this exact boundary wrong once already at `>1`; do not reintroduce that mistake).
- **last30days is a SEPARATE layer; no vendoring.** Real-AI calls use `max_completion_tokens` (NOT `max_tokens`).
- **`LABEL_MAX_OUTPUT_TOKENS` must stay ≥512** (guarded by a test) — 220 silently truncated every real label and blocked all live proposals for an unknown period; do not lower it.
- **Execution = `simulated_internal`** unless deliberately enabling opt-in `alpaca_paper` (paper-only, explicit intent).
- **Migrations additive only.** `SCHEMA_VERSION` stays 3 for additive changes.
- **Audit/evidence writes never gate execution/exit paths** (best-effort, after the action).
- **Dashboard stays read-only on render**; do not expose on the network without auth.
- **Mock market prices are seeded per `{symbol}:{market_date()}`** (deterministic per calendar trading day, not fixed forever) — new tests that derive exact price/R-multiple boundaries from mock prices must leave a margin (e.g. probe at 1.5R, not exactly 2R when a 2:1 target is in play), or they can silently re-break on some future date the way `test_mfe_mae.py` just did.
- Do not change OpenAI decision logic / risk/freshness thresholds / bracket-OCO-watchdog exits / Alpaca submission (beyond the TIF policy above) without explicit intent.
