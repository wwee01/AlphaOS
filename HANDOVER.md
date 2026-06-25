# HANDOVER

**Checkpoint: 2026-06-26 · branch `feat/labeller-decision-override` @ `23eb53c` (Roadmap 2.5 + 2.6, 4 commits ahead of `main` `cd77714`, PENDING MERGE) · tests 245 passed / 3 skipped · working tree clean · mode PAPER · market-data Alpaca/IEX live · AI = MOCK · catalyst = MOCK · last30days = DISABLED (default) · labeller override = DOWNGRADE-ONLY (default) · real-money UNREACHABLE**

> Single entry point for the next session. This project keeps no other handover docs — everything is here. Verify state before trusting any of it (commands in §8).

## Changelog (most recent first)
- **Roadmap 2.6 — Gated labeller decision override** (`e575ddb` + `e10f896` + `23eb53c`, branch `feat/labeller-decision-override`). The AI label can now move the no-news eval's decision **UP or DOWN**, but only under strict gating + full audit. Default stays **downgrade-only**; opt-in `LABELLER_DECISION_OVERRIDE_ENABLED`. An UPGRADE requires **armed** = flag on AND real AI (`has_openai_key`, not mock) AND a real, live, **direction-aligned positive** driver (live confirmed/possible catalyst whose type doesn't oppose the trade, or live `cli` last30days with supportive sentiment). Mock / conflicting / stale / none_found / unavailable / error / unknown / opposing signals NEVER upgrade. Inert while mock. Cannot upgrade a data-integrity reject (null levels), bypass gates, skip approval, overwrite labels, or execute. New append-only `decision_adjustments` table records eval/label/final + direction + driver + a COMPLETE catalyst/sentiment `evidence_json` snapshot per labelled candidate (+ a `decision_adjustment` tag on `candidates`). +21 hermetic tests.
- **Roadmap 2.5 — last30days research / narrative-context enrichment** (`8418de5`). SEPARATE keyless social/research layer that shells out to the GLOBALLY-INSTALLED `last30days` skill (no vendored code) and fills the packet's `last30days_context`/`sentiment_context`. `research/last30days_provider.py` (interface + deterministic mock + `cli` subprocess provider via explicit Python 3.12 + factory) + `research/last30days_enricher.py`. Context only — same advisory pattern as 2.4. Per-scan budget cap (top-N by interest rank; the rest journaled as a DISTINCT `skipped_budget_cap`, never silently dropped). New `candidate_last30days` table + `last30days_status`/`sentiment_label` on `candidates`. Read-only `alphaos last30days_probe <SYMBOL>` CLI. **Disabled by default** (`LAST30DAYS_ENABLED=false`). +32 hermetic tests.
- **Roadmap 2.4 — Official News / Catalyst Enrichment** (`c2858a4`, PR #7). Layer AFTER labelling, BEFORE gates: `news/official_news_provider.py` + `news/catalyst_enricher.py`. Catalyst is CONTEXT only. `candidate_catalysts` table + catalyst columns on `candidates`; advisory `catalyst_suggested_label`/`label_review_required`. +20 tests.
- **Roadmap 2.3 — Market Interest Scanner + AI Category/Playbook Labelling** (PR #6). Interest score → compact candidate packet → AI playbook label (FIXED official set, schema-validated, mock-deterministic, fail-safe, **downgrade-only**). `candidate_packets`/`candidate_labels` tables. +24 tests.
- **Roadmap 1.5 — Cost-model calibration + broker hygiene** (PR #5). `execution_calibration` table + `cost_calibration` report; CLI `calibration_report`/`flatten`/`reconcile_report`. Seeded **20 live Alpaca-paper calibration samples** (marketable-limit-biased — see §4).
- **Roadmap 2.2 — Alpaca paper execution validated live** (PR #4). Real bracket submit → reconcile → monitor proven during RTH.
- **Approval-to-execution console** (PR #3). Read-only dashboard; Approval Center + CLI `approve`/`reject`/`proposals`; approval-time re-checks; idempotent approval.
- **Base** — AlphaOS v1 skeleton + Roadmap 2 Trade Packet v1.

---

## 1. Current project state
AlphaOS is a **learning-first, paper-trading "operating system"** running locally on a Mac mini, Python 3.12 venv at `.venv` (uv). Operating mode (verified via `system_health()`): `ALPHAOS_MODE=paper`, **market data LIVE** (Alpaca free IEX), **AI = MOCK** (no OpenAI key — deterministic momentum scoring + mock playbook labels), **catalyst = MOCK** (`NEWS_ENRICHMENT_PROVIDER=mock`), **last30days research = DISABLED by default** (`LAST30DAYS_ENABLED=false`; mock provider when on; live keyless `cli` provider opt-in), **labeller decision override = DOWNGRADE-ONLY by default** (`LABELLER_DECISION_OVERRIDE_ENABLED=false`), **execution = `simulated_internal`**, **eval no-news baseline** (`NEWS_ENABLED=false`), **real-money trading `unreachable`** (`REAL_TRADING_ENABLED=false`, `ALLOW_REAL_ORDERS=false`). Work sits on branch `feat/labeller-decision-override` @ `23eb53c` — **pending merge to `main`** (§7). Full pipeline:
**Market Interest Scanner → Candidate Packet → AI Category/Playbook Labeller → Official Catalyst Enrichment → last30days Narrative Enrichment → label↔eval decision combine (downgrade-only, or gated symmetric override) → existing safety gates → manual approval → sim / Alpaca-paper execution → monitor/exit → ledger / reconciliation / learning.** A Streamlit dashboard runs ephemerally at http://localhost:8502 (dies on reboot/session-end — see §7).

## 2. What was just implemented (this checkpoint — Roadmap 2.5 + 2.6)
**2.5 — last30days narrative enrichment** (`alphaos/research/`):
- **Provider** (`last30days_provider.py`): `Last30DaysResearchProvider` interface; deterministic offline `MockLast30DaysProvider` (test default, labelled `MOCK_L30D`); `CliLast30DaysProvider` that shells out (`subprocess`, list-args, no `shell=True`) to the globally-installed skill via an **explicit Python 3.12** interpreter (`LAST30DAYS_PYTHON`, since system `python3` is 3.9.6), `--emit json --search <keyless> --quick`, auto-resolving the newest skill cache dir; `make_last30days_provider` factory (None when disabled; `force=True` for the probe).
- **Enricher** (`last30days_enricher.py`): `Last30DaysContext` + `Last30DaysEnricher`. Status `available`/`none_found`/`stale`/`unavailable`/`error`/`disabled`/`skipped_budget_cap`. Fail-open as `unavailable`; never raises.
- **Wiring** (`orchestrator._label_candidate`): enrich AFTER catalyst, BEFORE labelling; fed to the labeller only when `LAST30DAYS_FEED_TO_LABELLER=true`; always journaled. Budget cap (`LAST30DAYS_MAX_SYMBOLS_PER_SCAN=10`) by interest rank; out-of-cap eligible candidates journaled as `skipped_budget_cap`.
- **Schema**: `candidate_last30days` table + `last30days_status`/`sentiment_label` on `candidates`. Dashboard summary (enriched/skipped_budget_cap/unavailable/error/no_clear_narrative) + detail, read-only. CLI `last30days_probe`.

**2.6 — gated labeller decision override** (`alphaos/orchestrator.py`):
- **Combinator** `_combine_decision`: not armed → downgrade-only `min` (legacy); armed → label authoritative (up or down), but never upgrades a non-tradeable eval (null levels/unusable freshness).
- **Arming** `_override_armed` (flag + real AI + not mock) × `_real_decision_driver` (a real, live, direction-aligned POSITIVE driver). See §10 for the exact qualify/reject rules.
- **Audit** `decision_adjustments` table: eval/label/final decision, direction (`upgraded`/`downgraded`/`unchanged`), `override_armed`, `driver`, `driver_source` (catalyst/last30days/mixed/none), full per-source columns (catalyst status/type/summary/source/confidence/timestamp/age; last30days status/provider/sentiment label+score/summary/themes/coverage) **and** a complete `evidence_json` snapshot — one row per labelled candidate. Denormalized `decision_adjustment` + reason tag on `candidates`.

## 3. What is working (verified this checkpoint)
- Full suite **245 passed, 3 skipped** (0.9s). The 3 skips are the gated live-Alpaca tests.
- **last30days**: end-to-end mock scan enriches + journals; budget cap proven (12 eligible, cap 10 → top 10 enriched by rank + ranks 11–12 `skipped_budget_cap`); `skipped_budget_cap` is distinct from `none_found`; packet carries `last30days_context`/`sentiment_context` when fed; disabled-by-default writes 0 rows. The **live keyless `cli` path is verified end-to-end** (`last30days_probe NVDA` with `PROVIDER=cli` → real python3.12 subprocess → parsed `available`, exit 0). Keyless skill sources confirmed: Reddit / Hacker News / Polymarket / GitHub / web grounding (no keys).
- **override**: inert while mock (flag on + mock → `enabled_inert_while_mock`, 0 upgrades, proposals unchanged); armed (monkeypatched real signals) upgrades WATCH→PROPOSE and records `upgraded` + driver + `evidence_json`; the five safety rules enforced (§10); the earlier mock/conflicting/none_found demo row CANNOT upgrade in production (verified: real gate → no positive driver → stays WATCH).
- Safety: `real_money_trading=unreachable`, `manual_approval=required`, `execution=simulated_internal`. The OpenAI eval stays **no-news** (never sees catalyst or last30days). Labels stay in `OFFICIAL_LABELS`; frozen `primary_label` never overwritten.
- Dashboard render is **read-only** (headless full render against a populated ledger — incl. skipped_budget_cap + decision-adjustment rows — writes 0 rows).
- Ledger auto-migrates additively (`SCHEMA_VERSION` stays 3). Verified on a copy of the real ledger: `candidate_last30days` + `decision_adjustments` tables and all new columns added, **0 data loss**, 20 calibration rows preserved.

## 4. Partially implemented (and what's missing to finish)
- **last30days LIVE provider** — `cli` provider implemented + verified working standalone/probe, but **disabled by default** and **not enabled inside scans**. Keyless only; sentiment polarity is `unknown` for keyless retrieval (no reliable bull/bear signal without a polarity source). To enable: `LAST30DAYS_ENABLED=true` + `LAST30DAYS_PROVIDER=cli` (+ confirm `LAST30DAYS_PYTHON`). Query/source tuning likely needed (e.g. grounding can return jobs/web clusters).
- **Labeller override** — fully built + tested, but **upgrades are inert until real AI** (`has_openai_key`, not mock) AND a real live positive driver exist. With AI mock + catalyst mock + last30days keyless(`unknown`), nothing arms an upgrade today.
- **Real Alpaca paper EXECUTION** — wired + live-validated (2.2) but **opt-in** (`EXECUTION_PROVIDER=alpaca_paper`); default `simulated_internal`.
- **Cost-model calibration** — pipeline complete; the **20 seeded samples are marketable-limit-biased**. Needs real strategy-driven paper fills.
- **MFE/MAE** — snapshot-based; `baseline_outcomes.hypothetical_*` columns present but unpopulated.

## 5. Not done yet (deferred / future)
- **Real signals to actually exercise the override** — buy OpenAI credits (un-mock AI), enable a live catalyst (`NEWS_ENRICHMENT_PROVIDER=alpaca`) and/or a sentiment-polarity source for last30days (ScrapeCreators / X cookies / yt-dlp — NOT installed, intentionally).
- **last30days richer sources** — X / YouTube / TikTok / Instagram (need keys); deferred.
- Automatic relabelling from catalyst/narrative; scheduler automation; durable LAN-exposed dashboard hosting.
- **Live / real-money trading** — intentionally unreachable; not on the roadmap.

## 6. Test results
- **245 passed, 3 skipped** (`.venv/bin/python -m pytest`). The 3 skips = `tests/test_live_alpaca.py` (gated behind `RUN_LIVE_ALPACA_TESTS=true`; needs paper creds + RTH).
- +53 hermetic tests this checkpoint: `test_last30days_provider.py` / `test_last30days_enricher.py` / `test_last30days_flow.py` (2.5: factory gating, mock determinism, CLI parse/timeout/non-zero/bad-JSON via monkeypatched subprocess, status derivation, fail-open, budget-cap top-N + distinct skipped_budget_cap, eval-stays-no-news, no-exec, read-only dashboard, probe read-only) and `test_decision_override.py` (2.6: combinator, arming gate, real-positive-driver detection incl. the five rules, inert-while-mock, armed upgrade + audit, full evidence snapshot, no-exec/approval). No network/subprocess in the default suite.

## 7. Known risks / blockers (no RISKS.md — recorded here)
- **MERGE PENDING.** Roadmap 2.5 + 2.6 live on `feat/labeller-decision-override` (4 commits: `8418de5`, `e575ddb`, `e10f896`, `23eb53c`) **+ this handover commit**, ahead of `main` `cd77714`. **Cannot push to `main` from this environment** (safety classifier blocks direct pushes; no `gh`/API token). Feature branches are pushed over the SSH deploy key (`origin` = `git@github-alphaos:wwee01/AlphaOS.git`); **merge via the GitHub web UI** (PR `feat/labeller-decision-override` → `main`).
- **Ledger is a populated forward-evidence ledger, NOT clean.** `data/alphaos.db` (now also has the empty `candidate_last30days` + `decision_adjustments` tables from `status`-triggered migration). Reset per §8 before a clean run. Prior snapshots under `data/archive/`.
- **AI is mock** (no OpenAI credits), **catalyst is mock**, **last30days is disabled** (and keyless sentiment is `unknown`). So override upgrades are inert. Re-enable AI: buy credits → restore `OPENAI_API_KEY`.
- **last30days needs Python ≥3.12** — system `python3` is 3.9.6. The adapter uses `LAST30DAYS_PYTHON` (`~/.local/bin/python3.12`); a wrong path → fail-open `unavailable` (never crashes).
- **Calibration sample is biased** (marketable-limit seeding) — do not retune live cost assumptions on it.
- **Dashboard is ephemeral** (dies on reboot/sleep/session-end); durable hosting blocked by macOS TCC. Relaunch manually (§8). Do NOT expose on LAN without auth.

## 8. Exact commands to run next
```bash
cd "/Users/ck/Documents/Claude Playground/AlphaOS"

# verify
.venv/bin/python -m pytest                 # expect: 245 passed, 3 skipped
git status -sb && git log --oneline | head -6

# inspect / operate (one-shot CLI; no daemon)
.venv/bin/python -m alphaos status                  # mode / safety / startup checks
.venv/bin/python -m alphaos interest_scan           # scan -> packet -> label -> catalyst -> (last30days) -> propose
.venv/bin/python -m alphaos last30days_probe AAPL   # READ-ONLY last30days probe (mock default; PROVIDER=cli for live)
.venv/bin/python -m alphaos proposals               # open proposals (Approval Center queue)
.venv/bin/python -m alphaos approve <proposal_id>   # re-checks gates, then sim/alpaca_paper fill
.venv/bin/python -m alphaos monitor_once            # watchdog + alpaca reconcile
.venv/bin/python -m alphaos calibration_report      # modeled vs actual cost
.venv/bin/python -m alphaos reconcile_report        # broker vs ledger
.venv/bin/python -m alphaos flatten                 # PAPER-ONLY: cancel orders + close paper positions

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
run `.venv/bin/python -m pytest` (expect 245 passed, 3 skipped) and `git status -sb`.
If the feat/labeller-decision-override PR has merged, expect a clean `main`; otherwise that
branch is the tip. Confirm system_health(): real_money_trading=unreachable,
execution=simulated_internal, manual_approval=required, last30days_research=disabled_v1,
labeller_decision_override=downgrade_only.

Then do ONE of:
(a) Clean controlled forward-evidence pass: reset the ledger (HANDOVER §8),
    interest_scan -> approve one safe proposal -> monitor, to collect representative
    (non-seed-biased) cost-calibration data; OR
(b) Live-enable last30days in scans (LAST30DAYS_ENABLED=true, PROVIDER=cli) on a small
    universe and review the journaled candidate_last30days rows for signal quality; OR
(c) Wire a sentiment-polarity source so last30days produces non-'unknown' sentiment, the
    prerequisite for the override to ever arm.

Hard constraints (HANDOVER §10): real-money stays unreachable; no path bypasses manual
approval; no AI/catalyst/last30days output bypasses the gates; the labeller override is
opt-in + gated to real positive direction-aligned drivers, inert while mock, never
overwrites labels or executes; catalyst & last30days are context; eval stays no-news;
migrations additive only; keep tests green. Build the smallest clean change; branch off
main; test to green; push the branch; merge via the web UI.
```

## 10. Anything the next session must NOT change (hard invariants)
- **Real-money trading stays unreachable.** `REAL_TRADING_ENABLED=false`, `ALLOW_REAL_ORDERS=false`; `ALPHAOS_MODE=live` is rejected. Do not touch `safety.py` guards. `system_health()["real_money_trading"]` must remain `"unreachable"`.
- **Manual approval is the default and non-bypassable** (`APPROVAL_MODE=manual`). No path (scanner/labeller/catalyst/last30days/override/scheduler) may auto-submit or skip approval.
- **No AI/catalyst/last30days output bypasses gates.** Freshness, spread, liquidity, crossed-quote, risk, sizing, daily-cap, exposure, kill switch, stop/target, market-session, price-drift gates are authoritative. No execution from a label, a headline, or a narrative alone.
- **AI category label is ADVISORY.** By DEFAULT it is **downgrade-only** (`_apply_label_floor` = `min` over REJECT<WATCH<PROPOSE). Roadmap 2.6 adds an OPT-IN **gated symmetric override** (`LABELLER_DECISION_OVERRIDE_ENABLED`, default off): when **armed** — opt-in flag AND real AI (`has_openai_key`, not mock) AND a real **positive** per-candidate driver — the label may move the decision UP or DOWN. An UPGRADE requires a driver that is real, live, and SUPPORTIVE of the trade direction: a **live** (non-mock) catalyst that is **confirmed/possible** and whose type does not oppose the direction, OR **live** last30days (`cli`) **available** with sentiment **supportive** of direction (bullish↔long, bearish↔short). Mock sources, `conflicting`/`stale`/`none_found`/`unavailable`/`error` catalysts, `unknown`/neutral/mixed sentiment, and opposing signals NEVER upgrade (downgrades need no driver). Even armed it **cannot** upgrade a non-tradeable eval (null levels / unusable freshness), bypass any gate, skip manual approval, or execute; it is **inert while mock**. Every move is tagged + the driver + full `evidence_json` recorded in `decision_adjustments`. The label still NEVER mints an official label (only the fixed `OFFICIAL_LABELS` set) and NEVER overwrites the frozen `primary_label`.
- **Catalyst & last30days are CONTEXT, not execution authority.** They add risk tags / explanation / advisory fields only. The OpenAI momentum eval stays **no-news** — it never receives catalyst OR last30days context (`NEWS_ENABLED=false`, distinct from `NEWS_ENRICHMENT_*` / `LAST30DAYS_*`).
- **last30days is a SEPARATE keyless layer; no vendoring.** AlphaOS calls the globally-installed skill via subprocess at a configured path; do not copy the repo in. Disabled by default; fail-open as `unavailable`. `skipped_budget_cap` must stay distinct from `none_found`.
- **Execution = `simulated_internal`** unless deliberately enabling opt-in `alpaca_paper` (paper-only, explicit intent + paper creds).
- **Migrations are additive only.** The reconciler ADDs columns/tables from `schema.py`; never rename/drop. Additive changes do NOT bump `SCHEMA_VERSION` (stays 3); bump only for a destructive/transforming migration. Never silently drop data.
- **Audit/evidence writes must never gate execution or exit paths** (best-effort, after the action). This includes `decision_adjustments` and `candidate_last30days`.
- **Dashboard stays read-only on render** (no `startup()`/writes on load; SELECT-only views). Do not expose on the network without auth.
- Do not change OpenAI decision logic / `PROPOSE_MOMENTUM_THRESHOLD` / `MIN_REWARD_RISK` guard / risk/freshness thresholds / bracket-OCO-watchdog exits / Alpaca submission without explicit intent.
