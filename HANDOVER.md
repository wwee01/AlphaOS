# HANDOVER

**Checkpoint: 2026-07-02 (updated post-incident) · branch `feat/measurement-foundation` @ `e00595e` (pushed, NOT merged) · tests 381 passed / 3 skipped · working tree clean (2 pre-existing untracked docs, 2 pre-existing stray WAL files, 2 new incident docs — see §7) · mode PAPER · execution `alpaca_paper` · AI = LIVE (OpenAI) · real-money UNREACHABLE · 0 open positions**

> Single entry point for the next session. This project keeps no other handover docs — everything is here. Verify state before trusting any of it (commands in §8).
> ✅ **RESOLVED this checkpoint** (was flagged URGENT earlier today): the META paper position had lost both broker-side protective orders and was naked. User confirmed flatten; executed; broker + local ledger both verified clean and reconciled. Full incident record: [`docs/incidents/2026-07-02-meta-protection-mismatch.md`](docs/incidents/2026-07-02-meta-protection-mismatch.md). Root cause is known (day-TIF bracket legs expiring at session close, no scheduler to notice/resubmit) and a follow-up item is tracked at [`docs/roadmap/protection-watchdog.md`](docs/roadmap/protection-watchdog.md) — **not yet built**, so this class of failure can recur on any future live-fired position until that lands.

## Changelog (most recent first)
- **Incident: META protection mismatch, detected + resolved** (2026-07-02, this checkpoint). Routine handover verification surfaced that META's broker-side stop (`canceled`) and target (`expired`) orders had both expired at the end of their first trading session (`time_in_force=day` on a multi-day-hold bracket, no scheduler to resubmit) — the position sat naked for ~19 hours while price fell through the original stop, undetected by the local ledger (stale). User confirmed flatten; executed cleanly (0 open orders, 1 position closed, verified read-only after); local ledger manually reconciled via the standard `close_position()` path (not raw SQL) since `OrderManager.reconcile()` was confirmed NOT to detect a non-bracket-leg close — itself a finding, now tracked. Full writeup + timeline: `docs/incidents/2026-07-02-meta-protection-mismatch.md`. Follow-up roadmap item opened: `docs/roadmap/protection-watchdog.md`. No code changed as part of this incident response — detection + manual resolution only.
- **Opus audit fix pass** (`e00595e`, this checkpoint). Opus audited `feat/measurement-foundation` (verdict: APPROVE WITH REQUIRED FIXES) and found two real bugs in the just-built measurement layer. Both fixed on the same branch: **HIGH-1** — forward-return windows were anchored on `candidate_outcomes.created_at_utc` (seed time) instead of the true decision time, which would silently corrupt any backlog-catchup seeding (a candidate decided 30 days ago, seeded today, would have its next bar mislabeled a "1-day" return using a stale price). Fixed with a new additive `decision_at_utc` column sourced from each type's real source row, plus a self-healing repair path for any row missing it. **MEDIUM-1** — MFE/MAE folding could report a positive MAE for a trade that was favorable at every observed point (no anchor). Fixed: entry is now an implicit R=0 observation in both the live and backfill excursion paths (textbook MFE≥0, MAE≤0), while a genuinely stop-less position still correctly returns `(None, None)` rather than a false zero. **NIT-1** — doc comment added. MEDIUM-2 (a minor backfill edge case) deliberately left as follow-up per the audit's own scoping. +18 tests (381 total, was 363). No trading/execution/gate/scheduler behavior touched — see full audit + fix report in this session's transcript if unclear. (This fix's correctness was independently confirmed live during the incident above: the reconciled META exit correctly shows `mfe=0.0` — never favorable at any monitored point — instead of the pre-fix bug's wrong small-negative value.)
- **Measurement foundation — PR1 + PR2** (`eede83c`, this checkpoint, on top of Fable 5's architecture/roadmap review). Fable 5 reviewed AlphaOS end-to-end (packet + response now live at repo root: `FABLE_REVIEW_PACKET.md` / `FABLE_REVIEW_RESPONSE.md`, currently **untracked** — see §7) and recommended measurement before scanner v2/universe expansion/autonomy. Two parts landed: **PR1 (MFE/MAE)** — turned out to be a bug fix, not new work: per-check excursion tracking already existed (`monitoring_snapshots`) but `close_position()` discarded it and wrote a crude `%`-based approximation instead; fixed to fold the exit tick into the real running R-based extremum, plus an idempotent backfill for old closed trades (`alphaos backfill_mfe_mae`). **PR2 (counterfactual outcome ledger)** — new `alphaos/learning/` package + `candidate_outcomes` table: every scanned candidate/proposal/reject/armed-watch/user-override becomes learnable data via 1/3/5-day forward returns and bracket replay, whether or not it became a real trade (NOT a backtest — only replays decisions AlphaOS actually recorded). CLI: `alphaos outcomes_update`, `alphaos outcomes_report`. All additive, `SCHEMA_VERSION` stays 3.
- **Live calibration run + 3 bugfixes** (merged to `main` @ `e381096` earlier this session — PRs #16/#17/#18). Fixed a labeller output-token-budget bug (`220→800`) that had been silently truncating every real labeller call and blocking ALL live proposals; fixed the live Alpaca catalyst provider (SDK `NewsSet` parsing bug, was silently returning zero catalysts); shipped the User-Override attribution report. Live-fired: **META** long, real Alpaca-paper bracket, filled 41 @ 618.78 — see the incident entry above for how this trade ultimately closed.
- **Labeller fail-safe visibility** (`feat/labeller-failsafe-visibility`, pushed, **still not merged** — carried over from last checkpoint, untouched this session).
- Prior: Roadmap 2.8 (Armed Watch + labeller reasoning + User Override Mode), 2.7 (last30days polarity), 2.6 (gated labeller override), 2.5 (last30days enrichment), 2.4 (catalyst enrichment), 2.3 (interest scanner + AI labelling) — see git log for full history; unchanged this checkpoint.

---

## 1. Current project state
AlphaOS is a **learning-first, paper-trading "operating system"** on a Mac mini, Python 3.12 venv at `.venv` (uv). Two audited, unmerged branches sit on top of `main` (`e381096`): `feat/labeller-failsafe-visibility` (older, untouched this session) and `feat/measurement-foundation` (this session's work, now Opus-audited and fix-applied). Neither is merged yet — **that's the first decision pending** (§9 recommends order). Pipeline (unchanged in shape this checkpoint): **Scanner → Candidate Packet → AI Labeller → Catalyst/last30days/Polarity enrichment → decision combine → Armed Watch → gates → manual approval (+ User Override layer) → sim/paper execution → monitor/exit → ledger → NEW: counterfactual outcome measurement.** Real-money trading remains `unreachable` throughout — this checkpoint's work is entirely measurement/audit-fix, no safety-relevant behavior changed.

## 2. What was just implemented (this checkpoint)
- **Measurement foundation** (`alphaos/learning/outcomes_engine.py` pure compute, `alphaos/learning/outcomes_tracker.py` seed+update orchestration, `alphaos/reports/outcomes_summary.py`, `alphaos/data/providers/alpaca_bars.py` historical daily bars, `alphaos/execution/mfe_mae_backfill.py`). New `candidate_outcomes` table (43 columns incl. `decision_at_utc`) + `trade_outcomes.mfe_mae_source`.
- **MFE/MAE bug fix** in `alphaos/execution/position_manager.py`: `_fold_excursion()` shared by the live monitor pass and `close_position()`, now textbook 0R-anchored.
- **Opus audit fix pass**: `decision_at_utc` anchoring (HIGH-1) + textbook excursion semantics (MEDIUM-1) — see changelog above for detail.
- New CLI: `alphaos backfill_mfe_mae`, `alphaos outcomes_update`, `alphaos outcomes_report`.

## 3. What is working (verified this checkpoint)
- Full suite **381 passed, 3 skipped** (~1.5s, fully hermetic). The 3 skips are the pre-existing gated live-Alpaca tests.
- MFE/MAE: manually verified a +2R/−0.5R path folds correctly through both `monitor()` and `close_position()`; textbook 0-anchoring verified long AND short.
- Counterfactual seeding: verified live against a real mock scan — correctly classifies proposal/blocked/candidate/reject/armed_watch/user_override, sources `decision_at_utc` from each type's true source row (not seed time), idempotent across reruns.
- Forward-outcome resolution + bracket replay: verified end-to-end with fixture bars (target-hit, stop-hit, neither, ambiguous-same-bar all correctly distinguished; no-lookahead — decision-day bar excluded).
- The exact audit regression scenario (backlog candidate decided 30 days ago, seeded "now") now resolves against its real historical bar instead of silently losing it.
- Safety invariants: 0 orders/approvals/fills/positions created by any seed/update/report/backfill call, in every test and in manual runs; `real_money_trading=unreachable` confirmed throughout.

## 4. Partially implemented (and what's missing to finish)
- **Attribution report** exists (Roadmap 2.8 follow-up, merged to `main`) but has never been exercised on *real* (non-mock) override data — no live overrides have accumulated yet.
- **Cost-model calibration**: still 1/20 real fills (the META entry). Needs the exit + many more samples — see §7's urgent item, which is now entangled with this.
- **MEDIUM-2** (backfill treats a transient empty-bars fetch as permanently `unavailable`) — deliberately left as a fast-follow per the audit's own scoping; not yet done.
- **Measurement data has near-zero real volume yet**: the counterfactual ledger works correctly but has only been run against mock scans and hand-built fixtures — no real scheduled cadence exists to actually accumulate it (that's PR3, not started).

## 5. Not done yet (deferred / future)
Per the Fable 5 review's PR-sized roadmap (`FABLE_REVIEW_RESPONSE.md`), none of these are started: **PR3 scheduler v1.5** (the next recommended step — daily scan+monitor+outcomes_update+digest), decision-lineage stamping, earnings-proximity flag, proposal TTL, TQS v0, attribution v2 (ΔR), portfolio concentration monitor, playbook registry v0, generalized anomaly monitor, NightDesk export. Also still open from before: real Alpaca paper execution beyond the one META trade, universe expansion (deliberately deferred — shadow-rank only per the review), the mock-mode-doesn't-hard-disable-last30days footgun (flagged by the audit as its own follow-up PR, not touched).

## 6. Test results
- **381 passed, 3 skipped** (`.venv/bin/python -m pytest`). Skips = `tests/test_live_alpaca.py` (gated behind `RUN_LIVE_ALPACA_TESTS=true`). Fully hermetic otherwise.
- New this checkpoint: `tests/test_mfe_mae.py` (22), `tests/test_outcomes_engine.py` (19), `tests/test_outcomes_tracker.py` (28), `tests/test_outcomes_summary.py` (8) — includes the audit's exact regression scenarios (lagged-backlog anchoring, always-favorable/always-adverse excursion, short-direction close path).

## 7. Known risks / blockers
1. **Protection watchdog does not exist yet.** The META incident (resolved — see banner + changelog) can recur on any future live-fired position: nothing currently checks that a broker-managed position's protective orders are still live, and `OrderManager.reconcile()` does not detect a position closing via any path other than a bracket-leg fill. Tracked at [`docs/roadmap/protection-watchdog.md`](docs/roadmap/protection-watchdog.md) — **not built**. Until it exists, treat any live-fired `alpaca_paper` position as requiring a manual protection check if it survives past its first session (bracket legs are `time_in_force=day`, confirmed root cause of the incident).
2. **Two audited feature branches are unmerged**: `feat/measurement-foundation` (`e00595e`, this session, Opus-approved-with-fixes-applied) and `feat/labeller-failsafe-visibility` (older, still pending). Cannot merge from this environment (no `gh`, safety classifier blocks direct pushes to `main`) — user must merge via the GitHub web UI.
3. **Untracked docs at repo root**: `FABLE_REVIEW_PACKET.md` / `FABLE_REVIEW_RESPONSE.md` — deliberately left uncommitted (not part of the code PRs' scope); decide whether/where they should live. (The two new incident/roadmap docs under `docs/` are also currently untracked — same open question, decide together.)
4. **Stray WAL files**: `data/demo-chain.db-shm` / `-wal` — pre-existing, untouched, harmless; clean up at your discretion.
5. **Chain cost**: every real `interest_scan` still costs OpenAI money; `LAST30DAYS_ENABLED`/`POLARITY_ENABLED` are back on in `.env` from earlier this session.
6. **Operational footgun (documented, not fixed)**: `ALPHAOS_MODE=mock` does NOT disable `LAST30DAYS_ENABLED=true`, causing subprocess hangs during ad-hoc CLI testing. Always pair mock-mode testing with explicit `LAST30DAYS_ENABLED=false LAST30DAYS_POLARITY_ENABLED=false EXECUTION_PROVIDER=simulated_internal`. Opus classified this as its own follow-up PR (safety-adjacent, touches settings resolution) — deliberately not fixed this session.
7. **Cannot push to `main` from this environment** — feature branches push fine over the SSH deploy key; merge via GitHub web UI only.

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
.venv/bin/python -m pytest                 # expect: 381 passed, 3 skipped
git status -sb && git log --oneline | head -6
git branch --show-current                  # expect: feat/measurement-foundation

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
Read HANDOVER.md in the AlphaOS repo first (single source of truth). Note the META
protection incident from last checkpoint is RESOLVED (position flattened, ledger
reconciled) — no action needed there, just be aware docs/incidents/ and docs/roadmap/
now exist and are untracked.

Verify code state: `.venv/bin/python -m pytest` (expect 381 passed, 3 skipped), confirm
branch is `feat/measurement-foundation` @ `e00595e`, and confirm the paper account has
0 open positions (§8 has the read-only command).

Then help me decide, in order:
1. Disposition of the untracked docs (FABLE_REVIEW_*.md at repo root, docs/incidents/,
   docs/roadmap/) — commit them, and if so where/how scoped.
2. Merge order for the two unmerged, audited branches (feat/measurement-foundation and
   feat/labeller-failsafe-visibility) via the GitHub web UI (I'll do the actual clicks).
3. Whether to build the protection watchdog (docs/roadmap/protection-watchdog.md) before
   or after PR3 (scheduler v1.5, FABLE_REVIEW_RESPONSE.md §16) — the incident is a live
   argument for prioritizing the watchdog, but that's your call.

Do NOT start new feature work until we've discussed this ordering.

Hard constraints (HANDOVER §10): real-money stays unreachable; manual approval
non-bypassable; no AI/catalyst/last30days/polarity/measurement output bypasses gates;
migrations additive only; keep tests green; the counterfactual ledger is measurement-only
and must never be read by any gate/eval/labeller/risk/execution path.
```

## 10. Anything the next session must NOT change (hard invariants)
- **Real-money trading stays unreachable.** `REAL_TRADING_ENABLED=false`, `ALLOW_REAL_ORDERS=false`; `ALPHAOS_MODE=live` rejected. Do not touch `safety.py`. `system_health()["real_money_trading"]` must remain `"unreachable"`.
- **Manual approval is the default and non-bypassable** (`APPROVAL_MODE=manual`). No path may auto-submit or skip approval. `high_risk_narrative` proposals are manual-only regardless of approval mode.
- **No AI/catalyst/last30days/polarity/measurement output bypasses gates.** Freshness, spread, liquidity, crossed-quote, risk, sizing, daily-cap, exposure, kill switch, stop/target, market-session, price-drift gates are authoritative.
- **AI category label is ADVISORY; the override is gated + symmetric.** Default downgrade-only. When ARMED it may move the decision UP or DOWN, gated + audited (`decision_adjustments`).
- **Polarity is CONTEXT that can ARM, never EXECUTE.** Deterministic AlphaOS-side arming only; fails safe to non-arming.
- **User Override (2.8) is a SEPARATE decision layer; NEVER rewrites AlphaOS's recommendation**, never bypasses gates/approval/real-money guard, never auto-executes.
- **NEW — the counterfactual outcome ledger (`candidate_outcomes`) is PURE MEASUREMENT.** It must never be read by any gate, eval, labeller, risk check, or execution path — write-only from `alphaos/learning/`. `decision_at_utc` must stay the anchor for all forward-window math (never revert to seed-time anchoring — that was Opus audit HIGH-1, a real bug, not a style choice).
- **MFE/MAE stay textbook-anchored** (entry = implicit R=0; MFE≥0, MAE≤0 always) — do not revert to the old unanchored fold (Opus audit MEDIUM-1).
- **last30days is a SEPARATE layer; no vendoring.** Real-AI calls use `max_completion_tokens` (NOT `max_tokens`).
- **`LABEL_MAX_OUTPUT_TOKENS` must stay ≥512** (guarded by a test) — 220 silently truncated every real label and blocked all live proposals for an unknown period; do not lower it.
- **Execution = `simulated_internal`** unless deliberately enabling opt-in `alpaca_paper` (paper-only, explicit intent).
- **Migrations additive only.** `SCHEMA_VERSION` stays 3 for additive changes.
- **Audit/evidence writes never gate execution/exit paths** (best-effort, after the action).
- **Dashboard stays read-only on render**; do not expose on the network without auth.
- Do not change OpenAI decision logic / risk/freshness thresholds / bracket-OCO-watchdog exits / Alpaca submission without explicit intent.
