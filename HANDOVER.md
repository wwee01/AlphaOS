# HANDOVER

**Checkpoint: 2026-06-25 · `main` @ `c2858a4` (Merge PR #7, Roadmap 2.4) · tests 192 passed / 3 skipped · working tree clean (2 stray untracked dupes only) · mode PAPER · market-data Alpaca/IEX live · AI = MOCK · catalyst = MOCK · real-money UNREACHABLE**

> Single entry point for the next session. This project keeps no other handover docs — everything is here. Verify state before trusting any of it (commands in §8).

## Changelog (most recent first)
- **Roadmap 2.4 — Official News / Catalyst Enrichment** (`c2858a4`, PR #7). New layer AFTER labelling, BEFORE gates: `news/official_news_provider.py` (interface + deterministic mock + lazy disabled-by-default Alpaca seam) + `news/catalyst_enricher.py`. Catalyst is CONTEXT only — never forces a proposal, bypasses a gate, mints an official label, overwrites the frozen label, or executes. New `candidate_catalysts` table + catalyst columns on `candidates`; advisory `catalyst_suggested_label`/`label_review_required`. Read-only catalyst columns in the dashboard. +20 hermetic tests.
- **Roadmap 2.3 — Market Interest Scanner + AI Category/Playbook Labelling** (PR #6). Broadened discovery beyond momentum (interest score) → compact candidate packet → AI playbook label (FIXED official set, schema-validated, mock-deterministic, fail-safe, **downgrade-only**). New `candidate_packets`/`candidate_labels` tables + label columns. +24 tests.
- **Roadmap 1.5 — Cost-model calibration + broker hygiene** (PR #5). `execution_calibration` table + `cost_calibration` report; CLI `calibration_report`/`flatten`/`reconcile_report`; proposal status lifecycle pending → submitted → filled. Seeded **20 live Alpaca-paper calibration samples** (note: marketable-limit-biased — see §4). Paper daily-trade cap removed in `.env` (`MAX_PAPER_TRADES_PER_DAY=1000000`).
- **Roadmap 2.2 — Alpaca paper execution validated live** (PR #4). Real bracket submit → reconcile → monitor proven during RTH; broker-managed audit monitoring snapshot (watchdog never fights the broker OCO).
- **Approval-to-execution console** (PR #3). Read-only dashboard made truly read-only on render; Approval Center + CLI `approve`/`reject`/`proposals`; approval-time freshness/drift/spread/risk re-checks; idempotent approval.
- **Base** — AlphaOS v1 skeleton + Roadmap 2 Trade Packet v1 (trade_id evidence contract).

---

## 1. Current project state
AlphaOS is a **learning-first, paper-trading "operating system"** running locally on a Mac mini, Python 3.12 venv at `.venv` (uv). Operating mode (verified via `system_health()`): `ALPHAOS_MODE=paper`, **market data LIVE** (Alpaca free IEX), **AI = MOCK** (no OpenAI key — deterministic momentum scoring + mock playbook labels), **catalyst enrichment = MOCK** (`NEWS_ENRICHMENT_PROVIDER=mock`, deterministic/offline), **execution = `simulated_internal`** (real Alpaca paper opt-in but not default), **eval no-news baseline** (`NEWS_ENABLED=false`), **real-money trading `unreachable`** (`REAL_TRADING_ENABLED=false`, `ALLOW_REAL_ORDERS=false`). `main` @ `c2858a4`, in sync with origin. Full pipeline:
**Market Interest Scanner → Candidate Packet → AI Category/Playbook Labeller → Official Catalyst Enrichment → existing safety gates → manual approval → sim / Alpaca-paper execution → monitor/exit → ledger / reconciliation / learning.** A Streamlit dashboard runs ephemerally at http://localhost:8502 (dies on reboot/session-end — see §7).

## 2. What was just implemented (this checkpoint — Roadmap 2.4)
- **Provider abstraction** (`news/official_news_provider.py`): `OfficialNewsProvider` interface + `get_news_for_symbol`/`get_news_for_symbols`; deterministic `MockOfficialNewsProvider` (offline, labelled `MOCK_NEWS`); lazy `AlpacaNewsProvider` (alpaca-py NewsClient, **disabled by default**, fails open if SDK/creds missing); `make_news_provider` factory.
- **Catalyst enricher** (`news/catalyst_enricher.py`): `CatalystEnricher` + `CatalystContext`. Derives `catalyst_status` (confirmed / possible / none_found / stale / conflicting / unavailable / error) + type / summary / confidence / sources / age / context fields / risk tags. **Fail-safe**: no provider → `unavailable`; provider error → `unavailable`/`error` (never crashes the scan); old → `stale`; conflicting analyst actions → `conflicting`.
- **Wiring** (`orchestrator._label_candidate`): enrich the packet BEFORE the AI labeller (cost-capped by `NEWS_MAX_SYMBOLS_PER_SCAN`); journal a `candidate_catalysts` row per enriched candidate; set advisory `catalyst_suggested_label` + `label_review_required` when the catalyst implies a different official label — the frozen `primary_label` is **never** overwritten.
- **Schema** (additive, no `SCHEMA_VERSION` bump): `candidate_catalysts` table + catalyst columns on `candidates`. Verified migrating the real calibration ledger with **0 data loss**.
- **Config** (DISTINCT from the no-news posture): `NEWS_ENRICHMENT_ENABLED` (default true), `NEWS_ENRICHMENT_PROVIDER` (mock|alpaca|disabled), `NEWS_LOOKBACK_HOURS`, `NEWS_MAX_ARTICLES_PER_SYMBOL`, `NEWS_MAX_SYMBOLS_PER_SCAN`, `NEWS_MAX_AGE_HOURS`, `NEWS_TIMEOUT_SECONDS`, `NEWS_FAIL_OPEN_AS_UNAVAILABLE`.
- **Dashboard**: read-only catalyst summary + columns + detail in the Candidate Flow tab.

## 3. What is working (verified this checkpoint)
- Full suite **192 passed, 3 skipped** (0.65s). The 3 skips are the gated live-Alpaca tests.
- End-to-end mock scan: interest rank → top-N shortlist → packet → AI label → catalyst enrich → existing gates → `pending_approval` proposals. Controlled scan example: 18 candidates → 15 shortlisted/labelled → 10 catalyst-enriched → 13 propose / 3 watch / 2 reject; catalyst statuses confirmed/none_found/conflicting; example IWM labelled Momentum + confirmed analyst_upgrade catalyst → `catalyst_suggested_label=News Reaction`, `label_review_required=1` (frozen label NOT overwritten).
- Safety: `real_money_trading=unreachable`, `manual_approval=required`, `execution=simulated_internal`, `REAL_TRADING_ENABLED=false`, `ALLOW_REAL_ORDERS=false`. The OpenAI eval stays no-news (never sees catalyst).
- Alpaca paper account **flat**; broker↔ledger reconciliation **in_sync** (0/0). Real Alpaca paper bracket submit→reconcile→monitor proven (Roadmap 2.2).
- Dashboard render is **read-only** (headless full render against a populated ledger writes 0 rows).
- Ledger auto-migrates additively across pulls (v3, 28 sqlite objects); 20 calibration rows preserved through 2.3+2.4 migrations.

## 4. Partially implemented (and what's missing to finish)
- **Real Alpaca paper EXECUTION** — wired + live-validated (Roadmap 2.2) but **opt-in**; default is `simulated_internal`. Enable with `EXECUTION_PROVIDER=alpaca_paper` (paper mode + creds).
- **Cost-model calibration** — pipeline complete; the **20 seeded samples are marketable-limit-biased** (entry padded above market to force fills → ~−5 bps "favorable" slippage vs the limit, not true crossing cost). The recommended model stays conservative (1 bps). Needs **real strategy-driven paper fills** for representative calibration.
- **Catalyst enrichment** — mock provider is the default + fully working; the **live Alpaca news provider is implemented but disabled-by-default and not live-tested** (`NEWS_ENRICHMENT_PROVIDER=alpaca` + creds to try). `catalyst_suggested_label`/`label_review_required` are advisory only — **no automatic relabelling** (deliberate).
- **MFE/MAE** — snapshot-based; `baseline_outcomes.hypothetical_*` columns present but unpopulated.

## 5. Not done yet (deferred / future)
- **Roadmap 2.5 / last30days** — NOT started (next milestone candidate).
- Social sentiment, Reddit/X/StockTwits, broad AI web search/scraping — deferred (placeholder packet fields `last30days_context`/`sentiment_context` stay `unavailable`).
- Automatic relabelling from catalyst; scheduler automation; durable LAN-exposed dashboard hosting; real OpenAI engine (no credits — mock).
- **Live / real-money trading** — intentionally unreachable; not on the roadmap.

## 6. Test results
- **192 passed, 3 skipped** (`.venv/bin/python -m pytest`). The 3 skips = `tests/test_live_alpaca.py` (gated behind `RUN_LIVE_ALPACA_TESTS=true`; needs paper creds + RTH).
- Coverage spans: safety/real-trading-unreachable, manual approval, freshness/spread/liquidity/risk/drift gates, Alpaca paper lifecycle + watchdog segregation, cost calibration math, interest scanner, AI labelling (official-label enforcement, fail-safe, downgrade-only), catalyst enrichment (status derivation, fail-safe, no-official-label-minting, no-exec, frozen-label-not-overwritten), dashboard read-only render, additive schema migration.

## 7. Known risks / blockers (no RISKS.md — recorded here)
- **Ledger is a populated forward-evidence ledger, NOT clean.** `data/alphaos.db` holds 20 calibration rows + accumulated scan/candidate/packet/label/catalyst rows (0 open positions). Before a fresh clean real-evidence run, reset per §8 (archives first). Prior snapshots under `data/archive/`.
- **AI is mock** (no OpenAI credits) and **catalyst is mock**. Proposals/labels use deterministic mock logic, not real model output. Re-enable AI: buy credits → restore `OPENAI_API_KEY` in `.env`. Re-enable live catalyst: `NEWS_ENRICHMENT_PROVIDER=alpaca` (creds present).
- **Calibration sample is biased** (marketable-limit seeding) — see §4. Do not retune live cost assumptions on it.
- **Dashboard is ephemeral** (dies on reboot/sleep/session-end); durable LaunchAgent hosting blocked by macOS TCC (repo under `~/Documents`). Relaunch manually (§8). Do NOT expose on LAN without auth.
- **Cannot push to `main` from this environment** (safety classifier blocks direct pushes; no `gh`/API token). Merges happen via the GitHub **web UI**; feature branches are pushed over the SSH deploy key (`origin` = `git@github-alphaos:wwee01/AlphaOS.git`).
- **2 stray untracked files** (`env.example`, `gitignore.txt`) — stale de-dotted dupes of the tracked `.env.example`/`.gitignore`; safe to `rm`, never committed.

## 8. Exact commands to run next
```bash
cd "/Users/ck/Documents/Claude Playground/AlphaOS"

# verify
.venv/bin/python -m pytest                 # expect: 192 passed, 3 skipped
git status -sb && git log --oneline | head -5

# inspect / operate (one-shot CLI; no daemon)
.venv/bin/python -m alphaos status                 # mode / safety / startup checks
.venv/bin/python -m alphaos interest_scan          # scan -> packet -> AI label -> catalyst -> propose
.venv/bin/python -m alphaos proposals              # list open proposals (Approval Center queue)
.venv/bin/python -m alphaos approve <proposal_id>  # re-checks gates, then sim/alpaca_paper fill
.venv/bin/python -m alphaos monitor_once           # watchdog + alpaca reconcile
.venv/bin/python -m alphaos calibration_report     # modeled vs actual cost
.venv/bin/python -m alphaos reconcile_report       # broker vs ledger
.venv/bin/python -m alphaos flatten                # PAPER-ONLY: cancel orders + close paper positions

# dashboard (ephemeral, localhost:8502; read-only on render)
.venv/bin/streamlit run alphaos/dashboard/streamlit_app.py \
  --server.headless true --server.port 8502 --server.address 127.0.0.1 --browser.gatherUsageStats false

# RESET the working ledger to clean (archive current first), preserving artifacts
TS=$(date +%Y%m%d-%H%M%S); DIR="data/archive/${TS}-handover"; mkdir -p "$DIR"
.venv/bin/python -c "import sqlite3;c=sqlite3.connect('data/alphaos.db');c.execute('PRAGMA wal_checkpoint(TRUNCATE)');c.close()"
mv data/alphaos.db "$DIR/ledger.db"; rm -f data/alphaos.db-wal data/alphaos.db-shm
.venv/bin/python -c "from alphaos.journal.journal_store import JournalStore; JournalStore('data/alphaos.db').close(); print('clean ledger recreated')"
```

## 9. Recommended next prompt (paste into a fresh window)
```
Read HANDOVER.md in the AlphaOS repo first (single source of truth), then verify real state:
run `.venv/bin/python -m pytest` (expect 192 passed, 3 skipped) and `git status -sb`
(expect a clean `main` @ c2858a4 in sync with origin). Confirm system_health():
real_money_trading=unreachable, execution=simulated_internal, manual_approval=required.

Then do ONE of:
(a) Start Roadmap 2.5 — last30days context enrichment, slotting into the existing
    placeholder fields (candidate packet last30days_context / sentiment_context) and the
    same advisory, context-only pattern as the 2.4 catalyst layer, OR
(b) Run a clean controlled forward-evidence pass: reset the ledger (HANDOVER §8),
    interest_scan -> approve one safe proposal -> monitor, to collect representative
    (non-seed-biased) cost-calibration data.

Hard constraints (HANDOVER §10): real-money stays unreachable; no path bypasses manual
approval; no AI/catalyst output bypasses freshness/spread/liquidity/risk/entry/stop/target/
approval gates; AI label is downgrade-only and never mints official labels; catalyst is
context, not execution authority; migrations additive only; keep mock mode + tests green.
Build the smallest clean change; branch off main; test to green; push; merge via the web UI.
```

## 10. Anything the next session must NOT change (hard invariants)
- **Real-money trading stays unreachable.** `REAL_TRADING_ENABLED=false`, `ALLOW_REAL_ORDERS=false`; `ALPHAOS_MODE=live` is rejected. Do not touch `safety.py` guards. `system_health()["real_money_trading"]` must remain `"unreachable"`.
- **Manual approval is the default and non-bypassable** (`APPROVAL_MODE=manual`, `REQUIRE_MANUAL_APPROVAL=true`). No path (scanner/labeller/catalyst/scheduler) may auto-submit or skip approval.
- **No AI/catalyst output bypasses gates.** Freshness, spread, liquidity, crossed-quote, risk, sizing, daily-cap, exposure, kill switch, stop/target, market-session, price-drift gates are authoritative. No execution from a label or a news headline alone.
- **AI category label is ADVISORY.** By DEFAULT it is **downgrade-only** (`_apply_label_floor` = `min` over REJECT<WATCH<PROPOSE): it restricts but never creates a PROPOSE. Roadmap 2.6 adds an OPT-IN **gated symmetric override** (`LABELLER_DECISION_OVERRIDE_ENABLED`, default off): when **armed** — opt-in flag AND real AI (`has_openai_key`, not mock) AND a real per-candidate driver (live catalyst confirmed/possible, or live last30days with a known—non-`unknown`—sentiment) — the label may move the decision UP or DOWN. Even armed it **cannot** upgrade a non-tradeable eval (null levels / unusable freshness = a data-integrity reject), bypass any gate, skip manual approval, or execute; it is **inert while mock**. Every move is tagged + the driver recorded in `decision_adjustments` (audit/learning). The label still NEVER mints an official label (only the fixed `OFFICIAL_LABELS` set) and NEVER overwrites the frozen `primary_label`.
- **Catalyst is CONTEXT, not execution authority.** It adds risk tags / explanation / an advisory `catalyst_suggested_label` only. The OpenAI momentum eval stays **no-news** — it never receives catalyst context; invented catalysts in the eval are still rejected (`NEWS_ENABLED=false`, distinct from `NEWS_ENRICHMENT_*`).
- **Execution = `simulated_internal`** unless deliberately enabling opt-in `alpaca_paper` (still paper-only, requires explicit intent + paper creds).
- **Migrations are additive only.** The reconciler ADDs columns/tables from `schema.py`; it does NOT rename/drop. Additive table/column changes do NOT bump `SCHEMA_VERSION` (it stays 3); bump only for a destructive/transforming migration. Never silently drop data.
- **Audit/evidence writes must never gate execution or exit paths** (best-effort, after the action).
- **Dashboard stays read-only on render** (no `startup()`/writes on load; SELECT-only views). Do not expose it on the network without auth.
- **Do NOT implement** (deferred): last30days*/social sentiment/web scraping until explicitly requested; automatic catalyst-driven relabelling; scheduler automation; live/real-money trading. (* last30days is the likely next milestone, but only on explicit instruction.)
- Do not change OpenAI decision logic / `PROPOSE_MOMENTUM_THRESHOLD` / `MIN_REWARD_RISK` guard / risk/freshness thresholds / bracket-OCO-watchdog exits / Alpaca submission without explicit intent.
