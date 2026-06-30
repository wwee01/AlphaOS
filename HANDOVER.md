# HANDOVER

**Checkpoint: 2026-06-30 · `main` @ `ca0a50b` (Roadmap 2.7 merged, PR #12) · tests 268 passed / 3 skipped · working tree clean · mode PAPER · market-data Alpaca/IEX LIVE · AI = LIVE (OpenAI `gpt-5.4-mini`) · last30days = LIVE (`cli`: Reddit/X/YouTube/HN/Polymarket/GitHub) · polarity = ON + ARMING · labeller override = ARMED (symmetric) · execution = `simulated_internal` · real-money UNREACHABLE**

> Single entry point for the next session. This project keeps no other handover docs — everything is here. Verify state before trusting any of it (commands in §8).
> ⚠️ **Posture changed this checkpoint:** the AI is now REAL and the whole enrichment chain is ENABLED in `.env`, so **every real `interest_scan` now makes real OpenAI calls (costs money)**. Real-money trading is still unreachable and execution is still simulated; the cost is OpenAI API only.

## Changelog (most recent first)
- **Roadmap 2.7 — LLM-derived last30days narrative polarity** (`063178e`, PR #12). `ai/last30days_polarity.py`: classifies live last30days clusters → bullish/bearish/neutral/unclear + confidence + direction-alignment + `narrative_driver_type` + hype risk (real `gpt-5.4-mini`, `max_completion_tokens`, defensive parse, fail-safe to `unclear`/non-arming; deterministic offline mock). The ARMING decision is recomputed DETERMINISTICALLY on the AlphaOS side (`_decide_arming`) — arms only on aligned + confidence≥min + coverage≥min + no catalyst-conflict + arming enabled. Hype/meme/squeeze → `high_risk_narrative` (arms but **manual-only**, warned), not auto-suppressed. New `last30days_polarity` table; `polarity_label`/`polarity_alignment`/`narrative_driver_type`/`arming_classification` on `candidates`; `arming_classification`/`narrative_warning` on `trade_proposals`. Polarity now supplies the last30days override driver. +23 tests.
- **gpt-5.x compatibility fix** (`41dc2dd`). The labeller passed `max_tokens`, which `gpt-5.x` chat.completions rejects (400). Switched to `max_completion_tokens` (accepted by gpt-4o too). Required for ANY real-AI labelling.
- **Operational enablement this checkpoint (config, not code):** real `OPENAI_API_KEY` added (model `gpt-5.4-mini`); `openai` SDK installed in `.venv` (via `uv`); **X/Twitter** authenticated free via Safari cookies (`AUTH_TOKEN`/`CT0` in `~/.config/last30days/.env`); **`yt-dlp`** installed (`~/.local/bin`) → YouTube live; full chain turned ON in `.env` (`LAST30DAYS_ENABLED/PROVIDER=cli`, `POLARITY_ENABLED`, `POLARITY_ARMING_ALLOWED`, `LABELLER_DECISION_OVERRIDE_ENABLED` = true); thresholds `MIN_CONFIDENCE=0.55`, `MIN_SOURCE_COVERAGE=medium`; `LAST30DAYS_SOURCES=reddit,hackernews,polymarket,github,x,youtube`.
- **Roadmap 2.6 — Gated labeller decision override** (`e575ddb`/`e10f896`/`23eb53c`, PR #10). The label may move the eval's decision UP or DOWN, but only when ARMED (flag + real AI + a real direction-aligned positive driver); default downgrade-only. Full audit in `decision_adjustments` (eval/label/final + driver + `evidence_json`). +21 tests.
- **Roadmap 2.5 — last30days research enrichment** (`8418de5`, PR #9). Separate keyless social layer via the globally-installed `last30days` skill (no vendoring); `candidate_last30days` table; per-scan budget cap → `skipped_budget_cap`. +32 tests.
- **Roadmap 2.4 — Official News / Catalyst Enrichment** (PR #7). `candidate_catalysts`; advisory only. +20 tests.
- **Roadmap 2.3 — Interest Scanner + AI labelling** (PR #6). `candidate_packets`/`candidate_labels`. +24 tests.
- **Roadmap 1.5 / 2.2 / approval console / base** — calibration + broker hygiene; Alpaca paper validated; Approval Center; v1 skeleton + Trade Packet v1.

---

## 1. Current project state
AlphaOS is a **learning-first, paper-trading "operating system"** on a Mac mini, Python 3.12 venv at `.venv` (uv). Operating mode (verified via `system_health()`): `ALPHAOS_MODE=paper`, **market data LIVE** (Alpaca free IEX), **AI = LIVE** (`OPENAI_API_KEY` set, model `gpt-5.4-mini`; eval + labeller + polarity all real), **catalyst = MOCK** (`NEWS_ENRICHMENT_PROVIDER=mock`; live Alpaca news still opt-in), **last30days = LIVE** (`cli`; sources reddit/x/youtube/hackernews/polymarket/github + web grounding), **polarity = ON + arming allowed**, **labeller override = ARMED (symmetric)**, **execution = `simulated_internal`**, **eval no-news baseline** (`NEWS_ENABLED=false`), **real-money trading `unreachable`** (`REAL_TRADING_ENABLED=false`, `ALLOW_REAL_ORDERS=false`). `main` @ `ca0a50b`, in sync with origin. Full pipeline:
**Market Interest Scanner → Candidate Packet → AI Category/Playbook Labeller → Official Catalyst Enrichment → last30days Narrative Enrichment → last30days Polarity → label↔eval decision combine (downgrade-only OR gated symmetric override) → safety gates → manual approval → sim execution → monitor/exit → ledger / reconciliation / learning.** A Streamlit dashboard runs ephemerally at http://localhost:8502 (dies on reboot/session-end — see §7).

Config lives in three places: **AlphaOS `.env`** (chain flags, thresholds, sources, `LAST30DAYS_PYTHON`, the OpenAI key — gitignored); the **skill's** `~/.config/last30days/.env` (X cookies `AUTH_TOKEN`/`CT0`, memory dir; chmod 600); and **`~/.local/bin`** (`python3.12`, `yt-dlp`).

## 2. What was just implemented (this checkpoint — Roadmap 2.7 + go-live)
- **Polarity layer** (`ai/last30days_polarity.py`): `Last30DaysPolarityClassifier` + `PolarityEvidence` + `PolarityResult`. Runs in `orchestrator._label_candidate` AFTER last30days enrichment when status=`available`; journals a `last30days_polarity` row (full evidence + `parse_status`); freezes polarity fields onto the candidate. `_real_decision_driver` now treats `polarity.should_arm_override` as the last30days driver (carrying `arming_classification`); pre-2.7 raw-sentiment path kept as the polarity-disabled fallback. `high_risk_narrative` upgrades tag the proposal (`arming_classification`+`narrative_warning`) and are forced **manual-only**.
- **Schema** (additive, `SCHEMA_VERSION` stays 3): `last30days_polarity` table; candidate + proposal columns above. Verified 0 data loss on a real-ledger copy.
- **Config** (safe code-defaults OFF; turned ON in `.env` this session): `LAST30DAYS_POLARITY_ENABLED`, `_MODEL` (blank→`OPENAI_PRIMARY_MODEL`), `_MIN_CONFIDENCE=0.55`, `_MIN_SOURCE_COVERAGE=medium`, `_ARMING_ALLOWED`, `LAST30DAYS_HIGH_RISK_NARRATIVE_MANUAL_ONLY=true`.
- **Dashboard**: polarity summary (by sentiment / driver / arming) + detail with HIGH-RISK flags, read-only.
- **Go-live**: OpenAI key + SDK; X cookies; yt-dlp/YouTube; chain enabled.

## 3. What is working (verified this checkpoint)
- Full suite **268 passed, 3 skipped** on `main` (~1.0s). The 3 skips are the gated live-Alpaca tests.
- **Real AI end-to-end**: live eval + labeller return valid output under `gpt-5.4-mini` (`is_mock=False`); labeller uses `max_completion_tokens` (a test enforces no `max_tokens`).
- **last30days LIVE + X + YouTube**: `last30days_probe` and full scans pull real Reddit/X/YouTube/HN content; coverage went from 1 source (keyless-thin) to up to 5 (e.g. AMD: hackernews/jobs/reddit/x/youtube, 15 items).
- **Polarity ARMED on real data (first time)**: live scan classified AMD `bullish/aligned, conf 0.68, coverage medium → high_risk_narrative`, and the decision-adjustment row shows `armed=1`. It did **not** upgrade only because the AI labeller independently chose `watch` (override can't invent a `propose` the labeller didn't make) — correct behaviour. Other names came back neutral/low-confidence → `non_arming`. **0 upgrades, all correctly gated.**
- Safety on every live scan: `0 orders / 0 fills / 0 positions / 0 approvals`; eval stays no-news; labels stay official; `primary_label` never overwritten; real-money unreachable.
- Dashboard read-only on render (incl. polarity / skipped_budget_cap / decision-adjustment rows). Schema auto-migrates additively.

## 4. Partially implemented (and what's missing to finish)
- **A visible polarity-driven UPGRADE has not yet fired on live data.** The arming gate now fires (AMD `armed=1`), but a watch→propose upgrade also needs the *labeller* to choose `propose` on an armed candidate at the same time. Not seen yet (live narrative for the mega-caps has been mixed/low-conviction). Mechanism proven in tests; just awaiting the right live setup, or looser thresholds.
- **Catalyst is still MOCK** — live Alpaca news provider implemented but opt-in (`NEWS_ENRICHMENT_PROVIDER=alpaca`).
- **Real Alpaca paper EXECUTION** — opt-in (`EXECUTION_PROVIDER=alpaca_paper`); default `simulated_internal`. No real calibration fills collected yet (Step 1 deferred).
- **Cost-model calibration** — the 20 seeded samples are marketable-limit-biased; needs real strategy-driven paper fills.
- **MFE/MAE** — `baseline_outcomes.hypothetical_*` present but unpopulated.

## 5. Not done yet (deferred / future)
- **Step 1 — clean forward-evidence calibration pass** (reset ledger → scan → approve one → monitor; needs `alpaca_paper` execution during RTH for representative fills). Still pending.
- More sources: TikTok / Instagram / Threads (need a free `SCRAPECREATORS_API_KEY`); Perplexity/Bluesky (opt-in).
- Automatic relabelling from catalyst/narrative; scheduler automation; durable LAN dashboard hosting.
- **Live / real-money trading** — intentionally unreachable; not on the roadmap.

## 6. Test results
- **268 passed, 3 skipped** (`.venv/bin/python -m pytest`). 3 skips = `tests/test_live_alpaca.py` (gated behind `RUN_LIVE_ALPACA_TESTS=true`). Default suite is fully hermetic (no network / no subprocess / no real API — mock providers + monkeypatch).
- `test_last30days_polarity.py` (+23) covers: deterministic arming (aligned-arms / bearish-arms-short / conflicting / neutral / unclear / low-conf / low-coverage / catalyst-conflict / hype→high-risk), classify guards (disabled / no-evidence / fail-safe / coerce), mock classifier, integration (normal upgrade, high-risk manual-only+warned, non-arming unchanged, no-execution/approval), and `max_completion_tokens` enforcement.

## 7. Known risks / blockers (no RISKS.md — recorded here)
- **Every real scan now costs OpenAI money.** With the key live + chain on, each `interest_scan` makes ~2×(candidates) eval/label calls + ~1 polarity call per last30days-available candidate (`gpt-5.4-mini`, compact prompts — likely cents/scan; check the OpenAI usage dashboard). $0 only in `ALPHAOS_MODE=mock` or with `LAST30DAYS_POLARITY_ENABLED=false`. Scans are manual one-shots (no daemon), so cost is bounded by how often you run them.
- **X is authenticated via your personal Safari cookies** (`AUTH_TOKEN`/`CT0` in `~/.config/last30days/.env`). Scraping X with personal cookies carries some account risk; cookies also expire (re-paste when X stops appearing). To disable: comment those two lines.
- **The `.env` chain is ON.** Normal scans run live last30days + polarity and CAN arm upgrades (still manual-approval-gated, sim execution). To pause: `ALPHAOS_MODE=mock` or flip the `LAST30DAYS_*`/override flags false.
- **Ledger is forward-evidence, not clean.** Validation scans this session went to temp DBs (`data/demo-*.db`, safe to delete); `data/alphaos.db` is the real ledger. Reset per §8 before a clean calibration run.
- **last30days needs Python ≥3.12** — system `python3` is 3.9.6; the adapter uses `LAST30DAYS_PYTHON=~/.local/bin/python3.12`. Wrong path → fail-open `unavailable`.
- **Calibration sample biased**; **dashboard ephemeral** (relaunch §8; no LAN without auth).
- **Cannot push to `main` from this environment** (safety classifier blocks direct pushes; no `gh`/token). Feature branches push over the SSH deploy key (`origin = git@github-alphaos:wwee01/AlphaOS.git`); **merge via the GitHub web UI**.

## 8. Exact commands to run next
```bash
cd "/Users/ck/Documents/Claude Playground/AlphaOS"

# verify
.venv/bin/python -m pytest                 # expect: 268 passed, 3 skipped
git status -sb && git log --oneline | head -6
.venv/bin/python -m alphaos status         # expect AI configured, chain on, real_money unreachable

# operate (one-shot CLI; chain is LIVE -> real OpenAI calls each run)
.venv/bin/python -m alphaos interest_scan          # scan -> label -> catalyst -> last30days -> polarity -> propose
.venv/bin/python -m alphaos last30days_probe AAPL  # READ-ONLY probe (set PROVIDER=cli for the live skill)
.venv/bin/python -m alphaos proposals              # open proposals
.venv/bin/python -m alphaos approve <proposal_id>  # re-checks gates, then sim fill (manual approval)

# run a scan WITHOUT cost (logic only):  ALPHAOS_MODE=mock .venv/bin/python -m alphaos interest_scan
# run a scan into a TEMP db (keep real ledger clean):  ALPHAOS_DB_PATH=data/demo.db ... interest_scan

# verify a source after re-auth:  cd <skill>; python3.12 scripts/last30days.py --diagnose
#   skill dir = ~/.claude/plugins/cache/last30days-skill/last30days/<ver>/skills/last30days

# dashboard (ephemeral; read-only). Point at a chosen ledger via ALPHAOS_DB_PATH:
ALPHAOS_DB_PATH=data/alphaos.db .venv/bin/streamlit run alphaos/dashboard/streamlit_app.py \
  --server.headless true --server.port 8502 --server.address 127.0.0.1 --browser.gatherUsageStats false

# RESET the working ledger to clean (archive first), preserving artifacts
TS=$(date +%Y%m%d-%H%M%S); DIR="data/archive/${TS}-handover"; mkdir -p "$DIR"
.venv/bin/python -c "import sqlite3;c=sqlite3.connect('data/alphaos.db');c.execute('PRAGMA wal_checkpoint(TRUNCATE)');c.close()"
mv data/alphaos.db "$DIR/ledger.db"; rm -f data/alphaos.db-wal data/alphaos.db-shm
.venv/bin/python -c "from alphaos.journal.journal_store import JournalStore; JournalStore('data/alphaos.db').close(); print('clean ledger recreated')"
```

## 9. Recommended next prompt (paste into a fresh window)
```
Read HANDOVER.md in the AlphaOS repo first (single source of truth), then verify real state:
`.venv/bin/python -m pytest` (expect 268 passed, 3 skipped), `git status -sb` (clean main @ ca0a50b),
and `.venv/bin/python -m alphaos status` (AI configured/gpt-5.4-mini, last30days cli, polarity arming,
override armed, execution simulated_internal, real_money unreachable). NOTE: the chain is LIVE so real
scans cost OpenAI money — use ALPHAOS_MODE=mock or a temp ALPHAOS_DB_PATH for safe experiments.

Then do ONE of:
(a) Tune to see a polarity-driven upgrade fire on live data: run interest_scan during RTH on a temp DB
    and review last30days_polarity + decision_adjustments; if too conservative, lower
    LAST30DAYS_POLARITY_MIN_CONFIDENCE / _MIN_SOURCE_COVERAGE (less safe — note the trade-off); OR
(b) Step 1 — clean forward-evidence calibration pass: reset the ledger (§8), enable EXECUTION_PROVIDER=
    alpaca_paper, interest_scan -> approve ONE safe proposal -> monitor, to collect representative fills; OR
(c) Add TikTok/Instagram via a free SCRAPECREATORS_API_KEY in ~/.config/last30days/.env to broaden coverage.

Hard constraints (HANDOVER §10): real-money stays unreachable; manual approval non-bypassable; no AI/
catalyst/last30days/polarity output bypasses the gates; polarity can ARM an upgrade but never trades/
overwrites labels; hype/meme/squeeze = high_risk_narrative = manual-only; eval stays no-news; migrations
additive only; keep tests green. Branch off main; test to green; push; merge via the web UI.
```

## 10. Anything the next session must NOT change (hard invariants)
- **Real-money trading stays unreachable.** `REAL_TRADING_ENABLED=false`, `ALLOW_REAL_ORDERS=false`; `ALPHAOS_MODE=live` rejected. Do not touch `safety.py`. `system_health()["real_money_trading"]` must remain `"unreachable"`.
- **Manual approval is the default and non-bypassable** (`APPROVAL_MODE=manual`). No path may auto-submit or skip approval. **`high_risk_narrative` proposals are manual-only — never auto-approved**, regardless of approval mode.
- **No AI/catalyst/last30days/polarity output bypasses gates.** Freshness, spread, liquidity, crossed-quote, risk, sizing, daily-cap, exposure, kill switch, stop/target, market-session, price-drift gates are authoritative. No execution from a label, headline, narrative, or polarity alone.
- **AI category label is ADVISORY; the override is gated + symmetric.** Default downgrade-only (`_apply_label_floor` = `min`). When ARMED (`LABELLER_DECISION_OVERRIDE_ENABLED` + real AI + a real direction-aligned positive driver) it may move the decision UP or DOWN. It **cannot** upgrade a non-tradeable eval (null levels / unusable freshness), bypass a gate, skip approval, mint an official label, or overwrite the frozen `primary_label`. Every move is audited in `decision_adjustments` (+ `evidence_json`).
- **Polarity (2.7) is CONTEXT that can ARM, never EXECUTE.** Arms an upgrade only when `_decide_arming` (DETERMINISTIC, AlphaOS-side — never the model's word) finds: enabled + arming_allowed + direction-aligned + `confidence ≥ MIN_CONFIDENCE` + `coverage ≥ MIN_SOURCE_COVERAGE` + no official-catalyst conflict. Hype/meme/squeeze or medium/high hype → `high_risk_narrative` (arms but manual-only + warned), NOT auto-suppressed. Fails safe to `unclear`/non-arming on any error/invalid output. Stored SEPARATELY (`last30days_polarity`); never overwrites last30days / eval / label / risk / approval records.
- **Catalyst & last30days are CONTEXT, not execution authority.** The OpenAI momentum eval stays **no-news** — never receives catalyst / last30days / polarity context (`NEWS_ENABLED=false`, distinct from `NEWS_ENRICHMENT_*` / `LAST30DAYS_*`).
- **last30days is a SEPARATE layer; no vendoring.** Calls the globally-installed skill via subprocess. Real-AI calls use `max_completion_tokens` (NOT `max_tokens`) — do not regress (gpt-5.x rejects `max_tokens`). `skipped_budget_cap` stays distinct from `none_found`.
- **Execution = `simulated_internal`** unless deliberately enabling opt-in `alpaca_paper` (paper-only, explicit intent + paper creds).
- **Migrations additive only.** Reconciler ADDs columns/tables; never rename/drop. `SCHEMA_VERSION` stays 3 for additive changes; bump only for destructive/transforming migrations. Never silently drop data.
- **Audit/evidence writes never gate execution/exit paths** (best-effort, after the action): `decision_adjustments`, `candidate_last30days`, `last30days_polarity`.
- **Dashboard stays read-only on render**; do not expose on the network without auth.
- Do not change OpenAI decision logic / `PROPOSE_MOMENTUM_THRESHOLD` / `MIN_REWARD_RISK` / risk/freshness thresholds / bracket-OCO-watchdog exits / Alpaca submission without explicit intent.
