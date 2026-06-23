# HANDOVER

**Checkpoint: 2026-06-23 · tests 127 passed / 3 skipped · code @ `ccd4498` (Trade Packet v1) + this handover commit on `main` · ledger reset clean (0 rows, v3) · working tree clean (2 stray untracked dupes only) · mode: PAPER, mock-AI + live Alpaca data, real-money UNREACHABLE**

> Single entry point for the next session. This project keeps no other handover docs; everything is here. Verify state before trusting any of it (commands in §8).

## Changelog (most recent first)
- **2026-06-23 — Roadmap 2: Trade Packet v1 + evidence contract** (`ccd4498`). 4 new tables, a stable `trade_id` threaded scan→outcome, risk_check/monitoring/scan_batch/scheduler_run persistence, `assemble_trade_packet()`, 4 additive dashboard audit tabs. Adversarially reviewed; a blocker was fixed (an audit-snapshot write could suppress a watchdog exit). Schema v2→v3.
- target_profile tracking (`e322f6a`) — `configured_standard` default, threaded to outcomes + grouped in metrics.
- Configurable stop/target sizing (`7e43f60`) — `STOP_LOSS_PCT`/`TARGET_REWARD_RISK`/`MIN_REWARD_RISK`; reachable 1.5R default + min-R:R floor that clamps live AI output.
- Lightweight forward DB migration + `user_version` (`5a38fcb`) — additive column reconciliation from `schema.py`; new tables via `CREATE TABLE IF NOT EXISTS`.
- Daily-cap UTC boundary fix (`db58dc3`) — `start_of_trading_day_utc` was emitting a local-tz string; now UTC.
- Base: AlphaOS v1 skeleton — Alpaca-only market data, no-news baseline, cost modelling + outcome metrics, opt-in (but disabled) real Alpaca paper execution.

---

## 1. Current project state
AlphaOS is a **learning-first, paper-trading "operating system" (v1)** running locally on a Mac mini. Operating mode (verified via `load_settings()`/`system_health()`): `ALPHAOS_MODE=paper`, **market data LIVE** (Alpaca free IEX tier), **AI = MOCK** (OpenAI key blanked — no credits; mock momentum scoring), **execution = `simulated_internal`**, **news disabled** (no-news momentum baseline), **real-money trading `unreachable`** (`REAL_TRADING_ENABLED=false`, `ALLOW_REAL_ORDERS=false`). Python 3.12.13 venv at `.venv` (created via `uv` 0.11.23). `main` is at `ccd4498` and in sync with `origin/main`. A Streamlit dashboard is up **ephemerally** at http://localhost:8502 (dies on reboot/sleep/session-end — see §7).

## 2. What was just implemented (this checkpoint)
**Roadmap 2 — Trade Packet v1 evidence contract** (tracking/schema only; no trading-behaviour change):
- **4 new tables:** `scan_batches`, `scheduler_runs`, `risk_checks`, `monitoring_snapshots` (`SCHEMA_VERSION=3`).
- **`trade_id`** correlation key minted at proposal birth, threaded proposal → paper_order → paper_fill → position → exit → trade_outcome (+ monitoring_snapshots, risk_checks). `positions`/`exits`/`trade_outcomes` now also carry `candidate_id`/`proposal_id` (fixes the broken candidate↔outcome join).
- **Persistence:** a `scan_batch` per `scan_once`; a `risk_check` per proposal (scan + manual re-check) with per-gate results + configured R:R/stop snapshot; a `monitoring_snapshot` per watchdog pass (best-effort, **after** the exit, can never suppress it); `scheduler_run` audit rows for scan + monitor (`trigger=manual_cli` — **no scheduler built**).
- **`alphaos/reports/trade_packet.py::assemble_trade_packet(...)`** joins the full lifecycle from any anchor (candidate_id/trade_id/position_id/proposal_id), degrading gracefully.
- Minimal additive Streamlit tabs: Trade Packet, Scan Batches, Scheduler Runs, System Events (8 tabs total).
- Additive evidence columns across candidates/eval/review/proposal/order/fill/exit/outcome/baseline/snapshot.

## 3. What is working (verified)
- Full suite **127 passed, 3 skipped**.
- End-to-end traceability: `trade_id` survives proposal→order→position→exit→outcome; the 4 new tables populate; `assemble_trade_packet` resolves a full id chain (regression-tested, incl. the watchdog-exit-safety guard).
- DB migration auto-applies v2→v3 to an existing ledger (new tables + nullable columns), preserving old rows.
- Live Alpaca/IEX market data + freshness gate; configurable stop/target sizing; target_profile tracking; cost model + outcome metrics (+ grouping by target_profile).
- Safety: `system_health()["real_money_trading"] == "unreachable"`, `real_trading_enabled_raw == "false"`.
- Git: pushes work via an SSH **deploy key** (config alias `github-alphaos`); `origin` default branch = `main`.

## 4. Partially implemented (and what's missing to finish)
- **Real Alpaca paper EXECUTION** — wired opt-in (`EXECUTION_PROVIDER=alpaca_paper`) but **NOT enabled**; currently `simulated_internal`. To finish: enable the provider in `.env` (paper mode + creds), exercise the gated submit/cancel smoke (`RUN_LIVE_ALPACA_TESTS=true`).
- **News/catalyst layer** — `news_items` (+ Trade Packet news columns) exist in schema but have **no runtime write-site**; `NEWS_ENABLED=false` (no-news baseline). Deferred connectors: Benzinga/Massive.
- **`daily_report_id` back-link** — `daily_learning_reports` exists with `report_id`, but outcomes/packets don't carry a `daily_report_id`; report↔trade linkage is by time-window only.
- **MFE/MAE** — now snapshot-based (running max/min of `unrealized_r` per `monitoring_snapshots`); `baseline_outcomes.hypothetical_*`/pnl columns are present but unpopulated.
- A few Trade Packet evidence columns are schema-only / nullable for the future (`counter_thesis`, `reasons_to_reject`, `same_day_exit_allowed`, `price_at_order_submission`, `price_move_since_proposal_pct`, news classification fields).

## 5. Not done yet (deferred / future)
- **Scheduler** — the `scheduler_runs` contract + tables are in place, but no scheduler is built (intentional; v1 is one-shot CLI).
- **Dashboard redesign / Google Stitch visual design** — explicitly deferred; current dashboard is audit-first, not polished.
- **Durable Mac-mini hosting** of the dashboard — blocked by macOS TCC (see §7).
- **LAN/remote dashboard access** — needs auth before exposing a trading dashboard; localhost-only for now.
- **Live / real-money trading** — intentionally unreachable; not on the roadmap.

## 6. Test results
- **127 passed, 3 skipped** (`0.3s`). The 3 skips are the gated live-Alpaca tests (`tests/test_live_alpaca.py`, behind `RUN_LIVE_ALPACA_TESTS=true`).
- Run: `cd "/Users/ck/Documents/Claude Playground/AlphaOS" && .venv/bin/python -m pytest`
- Live Alpaca (needs paper creds + network): `RUN_LIVE_ALPACA_TESTS=true .venv/bin/python -m pytest tests/test_live_alpaca.py -v`

## 7. Known risks / blockers (no RISKS.md — recorded here)
- **Working ledger is clean** as of this checkpoint — `data/alphaos.db` reset to 0 rows at schema v3. Three prior snapshots are archived under `data/archive/` (`20260622-215653-validation/`, `20260623-001944-roadmap2-validation/`, `20260623-214234-handover/`). It can re-accumulate rows if scans or the dashboard run against it; re-reset via §8 before a fresh real-evidence run.
- **Dashboard is ephemeral.** It runs only while a session keeps the process alive; it dies on reboot/sleep/session-end. A LaunchAgent host is **blocked by macOS TCC** because the repo lives under `~/Documents/` (launchd is denied read access to the venv/code in its autonomous context). Durable fix later = grant Full Disk Access to `.venv/bin/python3.12`, OR relocate the repo out of `~/Documents` (e.g. `~/AlphaOS`). For now: relaunch manually (§8).
- **AI is mock** (no OpenAI credits). Proposals use mock momentum scoring, not real model output. The real key is preserved commented in `.env` (`OPENAI_API_KEY_SAVED=…`); to re-enable: buy credits → `uv pip install -e ".[live]"` (installs `openai`) → restore the key line → confirm `OPENAI_PRIMARY_MODEL` is callable. `MIN_REWARD_RISK` still clamps live output.
- **2 documented fail-safe robustness nits** (from the Roadmap 2 review, both fail-safe — never create an unsafe trade): `run_scan_once`'s scan_batch/scheduler_run close-out isn't in a `finally` (a mid-loop exception leaves status='started'); `_record_risk_check` isn't internally try/wrapped. Tracked as nice-to-have hardening, not blockers.
- **Two stray untracked files** (`env.example`, `gitignore.txt`) — stale de-dotted duplicates of the tracked `.env.example`/`.gitignore`; safe to `rm`. They are never pushed.

## 8. Exact commands to run next
```bash
cd "/Users/ck/Documents/Claude Playground/AlphaOS"

# verify
.venv/bin/python -m pytest                 # expect: 127 passed, 3 skipped
git status -sb && git log --oneline | head -5

# inspect / operate (one-shot CLI; no daemon)
.venv/bin/python -m alphaos status                 # mode / safety / startup checks
.venv/bin/python -m alphaos scan_once              # universe -> candidates -> eval -> proposals
.venv/bin/python -m alphaos monitor_once           # watchdog over open positions
.venv/bin/python -m alphaos generate_daily_report  # daily learning report
.venv/bin/python -m alphaos seed_demo              # labelled demo trade (exercises exec->monitor)

# dashboard (ephemeral, localhost:8502)  — :8501 is a DIFFERENT app (app.py), not AlphaOS
.venv/bin/streamlit run alphaos/dashboard/streamlit_app.py \
  --server.headless true --server.port 8502 --server.address 127.0.0.1 --browser.gatherUsageStats false

# RESET the working ledger to clean (archive current first), preserving artifacts
.venv/bin/python -c "import sqlite3;c=sqlite3.connect('data/alphaos.db');c.execute('PRAGMA wal_checkpoint(TRUNCATE)');c.close()"
mkdir -p "data/archive/$(date +%Y%m%d-%H%M%S)-handover" && mv data/alphaos.db "data/archive/$(date +%Y%m%d-%H%M%S)-handover/ledger.db" 2>/dev/null; rm -f data/alphaos.db-wal data/alphaos.db-shm
.venv/bin/python -c "from alphaos.journal.journal_store import JournalStore; JournalStore('data/alphaos.db').close(); print('clean ledger recreated at schema v3')"
```

## 9. Recommended next prompt (paste into a fresh window)
```
Read HANDOVER.md in the AlphaOS repo first (it's the single source of truth), then verify the
real state: run `.venv/bin/python -m pytest` (expect 127 passed, 3 skipped) and `git status -sb`
(expect a clean main in sync with origin). The ledger was reset clean at this checkpoint; if a
scan/dashboard has since run against it, re-reset per §8 before collecting real RTH evidence.

Then do ONE of:
(a) Wire the daily_report_id back-link — persist the contributing entity/packet ids onto
    daily_learning_reports so a report enumerates exactly which candidates/trades it covered
    (the only missing arm of the Trade Packet evidence contract), OR
(b) Run a clean controlled RTH scan->approval->monitor validation pass on a freshly reset ledger
    when the US market is open (~21:30 SGT) and report the evidence.

Hard constraints (see HANDOVER.md §10): keep it additive + tracking-only; do NOT change OpenAI
decision logic, risk/freshness thresholds, bracket/OCO/watchdog exits, or Alpaca submission;
live trading stays unreachable. Branch off main, test to green, commit, open a PR.
```

## 10. Anything the next session must NOT change (hard invariants)
- **Real-money trading stays unreachable.** `REAL_TRADING_ENABLED=false`, `ALLOW_REAL_ORDERS=false`; `ALPHAOS_MODE=live` is rejected. Do not touch `safety.py` guards. `system_health()["real_money_trading"]` must remain `"unreachable"`.
- **Execution = `simulated_internal`** unless deliberately enabling the opt-in `alpaca_paper` paper path (still paper-only, requires explicit approval).
- **v1 posture:** `DATA_PROVIDER=alpaca` only (Massive/Benzinga deferred); `NEWS_ENABLED=false` (no-news baseline). No mock/fabricated news at runtime — test fixtures only, labelled `TEST_FIXTURE_NEWS`.
- **Do not change** OpenAI decision logic / `PROPOSE_MOMENTUM_THRESHOLD` / the `MIN_REWARD_RISK` guard, risk thresholds, data-freshness thresholds, or bracket/OCO/watchdog **exit** decisions without explicit intent.
- **Migrations are additive only.** The reconciler ADDs columns from `schema.py` and creates new tables; it does NOT rename/drop. Bump `SCHEMA_VERSION` on schema changes; never silently drop existing data.
- **Audit/evidence writes must never gate execution or exit paths** (the Roadmap 2 blocker: an audit write was able to suppress a watchdog exit — keep such writes best-effort and after the action).
- **Do not expose the dashboard on the network** (bind 0.0.0.0 / LAN) without authentication.
- Manual approval is the default (`APPROVAL_MODE=manual`, `REQUIRE_MANUAL_APPROVAL=true`); a future scheduler must not bypass approval.
