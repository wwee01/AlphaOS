# AlphaOS

A clean, standalone, **learning-first paper-trading operating system**. AlphaOS
runs daily, finds liquid US stock/ETF trade candidates, uses the OpenAI API +
market data to score them, creates **paper** trades, tracks outcomes, and
produces forward paper-trading evidence — while real-money trading stays
**hard-disabled and unreachable**.

> **v1 is a bounded, runnable, tested skeleton.** It is intentionally *not* the
> full system described in the design constitution. It honors the constitution's
> posture (risk-first, journal-heavy, live-disabled-by-default) and builds the
> foundation the rest will grow into.

**v1 data/news posture (deliberate):** ONE real market-data path (Alpaca, free
IEX tier), **no news** (no-news momentum baseline), execution **simulated
internally**, real money **unreachable**. Massive market data and Benzinga/web
news are **deferred** (see `connectors/deferred/DEFERRED.md`) — labelled seams,
not wired into the runtime.

- **Primary horizon:** tactical swing, 1–5 trading days.
- **Playbook (v1):** **momentum continuation (no-news baseline)**.
- **AI:** OpenAI is the primary runtime engine (no-news mode; never fabricates a catalyst); Claude is an optional, **manual-only** second-opinion reviewer.
- **Market data:** **Alpaca only**, free **IEX** tier (limited-market data). Massive is deferred.
- **News:** **off in v1.** Benzinga + web scraper are deferred.
- **Broker / execution:** Alpaca **paper** only; v1 fills are **simulated internally** (`execution_provider=simulated_internal`). A fill is never labelled an Alpaca paper fill unless it comes from the real Alpaca paper API.
- **Source of truth:** SQLite journal.

This is not NightDesk and does not depend on it.

---

## Safety posture (non-negotiable)

- `REAL_TRADING_ENABLED` must be **exactly** `false`. Anything else blocks every order and is logged.
- There is **no `live` mode** — it is not a member of the runtime-mode enum and `ALPHAOS_MODE=live` refuses to load.
- No real-money trading, margin, leverage, or options anywhere in v1.
- No trading on stale/unverifiable data (mandatory freshness gate).
- No order without a recorded candidate, OpenAI evaluation, risk check, and (in `manual` mode) user approval.
- Claude review can never auto-approve, submit, bypass risk/approval, or overwrite the OpenAI record.
- A file-backed **kill switch** blocks all new orders.
- Shorting is **paper/mock only**; any path needing margin/borrow/leverage is surfaced and requires explicit approval first.

---

## Quick start (mock mode, zero external keys)

```bash
# Python 3.9+ (developed on 3.11). Only test deps are needed for mock mode.
pip install pytest tzdata          # or: pip install -e ".[test]"

# Run the test suite (offline, in-memory SQLite):
python -m pytest                   # 48 tests, ~0.2s

# One-shot CLI runners (mock mode is the default):
python -m alphaos status                 # mode / safety / startup checks
python -m alphaos scan_once              # universe -> candidates -> eval -> proposals
python -m alphaos seed_demo              # labelled demo trade (exercises execution end-to-end)
python -m alphaos monitor_once           # watchdog over open positions (stop/target/time)
python -m alphaos generate_daily_report  # daily learning report (markdown)
python -m alphaos kill engage|release    # kill switch

# Dashboard (needs streamlit):
pip install streamlit
streamlit run alphaos/dashboard/streamlit_app.py
```

In **mock mode** AlphaOS runs fully offline. Market data is simulated (provider
`massive_mock`, always carrying a fresh source timestamp). **News is never
mocked at runtime** — with no news source, candidates are marked
`NEWS_UNAVAILABLE` and the news-confirmed playbook downgrades them to *watch*,
so a pure mock scan produces **no proposals**. The `seed_demo` command exists to
exercise the execution/journal/dashboard layers without fabricating news.

---

## Environment variables

Copy `.env.example` to `.env`. All secrets come from the environment; nothing is
hardcoded.

| Variable | Purpose |
|---|---|
| `ALPHAOS_MODE` | `mock` \| `paper` (default `mock`). `shadow`/`research` are recognized-but-inactive stubs; `live` is unreachable. |
| `DATA_PROVIDER`, `MARKET_DATA_FEED` | Must be `alpaca` / `iex` in v1 (anything else fails fast). |
| `NEWS_ENABLED`, `NEWS_PROVIDER` | Must be `false` / `disabled` in v1 (no-news mode; `true` fails fast). |
| `EXECUTION_PROVIDER`, `ALLOW_REAL_ORDERS` | Must be `simulated_internal` / `false` in v1 (else fails fast). |
| `REQUIRE_MANUAL_APPROVAL` | Default `true`; auto mode needs this `false` to take effect. |
| `APPROVAL_MODE` | `manual` \| `auto` (default `manual`). |
| `REAL_TRADING_ENABLED` | Must be exactly `false`. |
| `RUN_MODE`, `OFFLINE_MODE` | Explicit mock/offline toggles (mock is always labelled). |
| `OPENAI_API_KEY`, `OPENAI_PRIMARY_MODEL`, `OPENAI_REVIEW_MODEL` | OpenAI primary engine. |
| `ANTHROPIC_API_KEY`, `CLAUDE_REVIEW_MODEL` | Optional Claude manual reviewer. |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER`, `ALPACA_BASE_URL` | Alpaca **paper** broker + live market data. |
| `NTFY_TOPIC` | Optional notifications. |
| Risk limits | `MAX_RISK_PER_TRADE_PCT`, `MAX_PAPER_TRADES_PER_DAY`, `MAX_OPEN_POSITIONS`, `MAX_DAILY_LOSS_PCT`, `PAPER_EQUITY`, `MAX_AUTO_APPROVALS_PER_DAY`, `MAX_SPREAD_PCT`, `MIN_DOLLAR_VOLUME` |
| Freshness | `MAX_QUOTE_AGE_SECONDS_RTH`, `MAX_BAR_AGE_SECONDS_RTH`, `MAX_QUOTE_AGE_SECONDS_PREMARKET`, `MAX_BAR_AGE_SECONDS_PREMARKET`, `MAX_PRICE_DRIFT_BPS_SINCE_PROPOSAL` |
| Storage | `ALPHAOS_DB_PATH`, `ALPHAOS_JSONL_MIRROR` |
| Deferred (NOT used in v1) | `# MASSIVE_API_KEY`, `# BENZINGA_API_KEY` (commented out) |

**Paper mode requires** `ALPACA_PAPER=true`, `ALPACA_BASE_URL=https://paper-api.alpaca.markets`,
and both Alpaca keys (used for both paper execution and live IEX market data). If
any check fails, paper execution refuses to start and the failure is logged to
`system_events`. Missing Alpaca creds in live mode never silently fall back to
mock or any other provider — data is blocked instead.

---

## Architecture

```
alphaos/
  config/settings.py        # config + startup-safety validation
  safety.py                 # real-trading guard + kill switch (the hard "no")
  constants.py              # enums: modes, decisions, order states, exit classes, reasons
  util/                     # timestamps (UTC+SGT+ET), ids, defensive JSON parsing
  journal/                  # SQLite schema + JournalStore (source of truth)
  data/                     # market_data interface + providers/{alpaca_data,mock} + freshness_guard
  news/                     # news_service (v1 NO-NEWS mode; imports no connectors)
  ai/                       # openai_client (no-news), prompt_templates, validation, claude_reviewer (manual)
  scanner/                  # candidate_scanner (universe -> snapshots -> candidates)
  strategy/                 # swing_strategy + daytrade_experiment (gated stub) + proposal
  risk/                     # risk_engine (sizing from risk + gates)
  execution/                # order_schema (shared), order_manager, position_manager, exit_rules
  broker/                   # alpaca_client (paper-only guarded stub)
  approval.py               # APPROVAL_MODE manual/auto wiring
  orchestrator.py           # ties the daily workflow together
  reports/                  # daily_recon + weekly_review (stub)
  notifications/            # ntfy_client (optional)
  dashboard/streamlit_app.py# minimal tabs + System Health (mocked/deferred/disabled/live)
  __main__.py               # CLI runners
connectors/deferred/        # DEFERRED (NOT runtime): massive, benzinga, web_news + DEFERRED.md
tests/                      # offline test suite
```

**Separation of concerns:** decision engine (OpenAI) → risk engine → execution
engine → journal engine → review engine (Claude, manual). AI proposes; the risk
engine disposes; execution only ever talks to a (paper) broker stub.

### Database (SQLite — source of truth)

All 18 required tables are created: `universe`, `price_snapshots`, `news_items`,
`candidates`, `openai_evaluations`, `claude_reviews`, `trade_proposals`,
`approvals`, `paper_orders`, `paper_fills`, `positions`, `exits`,
`trade_outcomes`, `rejected_candidates`, `baseline_outcomes`,
`daily_learning_reports`, `system_events`, `config_versions` — plus an
append-only `order_events` lifecycle log. Every row is stamped UTC + Asia/
Singapore + market-ET. History is append-only; optional JSONL mirroring is
available (`ALPHAOS_JSONL_MIRROR=true`) but SQLite remains authoritative.

---

## How the key guarantees are enforced

**OpenAI structured outputs.** The model is instructed to return a *single JSON
object only* (no prose, no fences) and is called with `response_format=
{"type": "json_object"}`. Output is parsed by `util/structured_json.py`, which
strips stray fences, extracts the first balanced `{...}`, and validates required
keys. A failed/invalid evaluation is treated as a **rejection**, never a silent
pass. In mock mode the evaluation is deterministic and schema-valid.

**No-news mode (v1).** News is off, so the model evaluates on price/volume/
structure only and must NOT invent a catalyst. Output carries the sentinels
`catalyst="not_available_v1"`, `news_status="disabled_v1"`, `news_sources=[]`,
and is validated (`ai/validation.py`): any invented/inferred catalyst (non-empty
sources, a named catalyst, or marker phrases like "analyst upgrade",
"earnings", "M&A") is rejected as `invented_catalyst_in_no_news_mode`.

**Market data (Alpaca/IEX only).** The rest of the system calls the generic
`MarketDataClient`, never a provider directly. v1 wires `AlpacaDataProvider`
(live IEX) or `MockDataProvider` (offline, Alpaca-shaped, labelled mock). The
freshness guard gates on quote age, bar age (session-dependent thresholds),
missing data, closed session, and price drift since the proposal.

**Claude manual review.** `ai/claude_reviewer.py` runs only when triggered from
the dashboard/CLI and only when `ANTHROPIC_API_KEY` is set (the button is
disabled otherwise). The verdict is stored in its own `claude_reviews` table; it
never overwrites the OpenAI record, never approves, never submits.

**Alpaca paper-only.** `broker/alpaca_client.py` `preflight()`/`submit_order()`
refuse unless `REAL_TRADING_ENABLED=false`, `ALPACA_PAPER=true`, and the base URL
is the paper endpoint, with keys present. In v1 the connector is a stub: after
guardrails pass it raises `AlpacaNotConnected` and the OrderManager falls back to
**simulated internal** execution, labelled honestly (`execution_provider=
simulated_internal`, `execution_mode=internal_simulation`, `fill_source=
internal_sim`, with `data_provider`/`data_feed` recorded). A fill is **never**
labelled an Alpaca paper fill unless it comes from the real Alpaca paper API. No
code path can place a real-money order.

**Order protection hierarchy.** Each order logs one of
`BROKER_NATIVE_BRACKET`, `BROKER_NATIVE_OCO`, `ENTRY_PLUS_WATCHDOG`, or
`BLOCKED_NO_VALID_EXIT_PROTECTION`. No valid exit ⇒ the trade is blocked.

**Approval modes.** `manual` (default) leaves proposals pending until explicit
approval. `auto` may submit only within all guardrails — passes risk + freshness,
not a day-trade experiment, no unapproved margin, and within
`MAX_AUTO_APPROVALS_PER_DAY` (default 1). Every auto approval is labelled
`AUTO_APPROVED` and logged. Auto never bypasses risk or freshness.

**Freshness gate (Alpaca/IEX aware).** Every snapshot records provider, feed,
quote/bar timestamps, ages, delay estimate, market session, usability, and block
reason. Decisions are based on the provider's own timestamps; no parseable
timestamp ⇒ blocked. The guard blocks stale/missing **quote or bar** (thresholds
differ by session), labels a closed session, and the manual-approval path
re-checks freshness and blocks on price **drift** beyond
`MAX_PRICE_DRIFT_BPS_SINCE_PROPOSAL` since the proposal.

---

## What is mocked vs live-connected (v1)

| Area | v1 status |
|---|---|
| Market data | **Alpaca only.** Offline ⇒ `MockDataProvider` (`alpaca_mock`, Alpaca-shaped, labelled mock). Live ⇒ `AlpacaDataProvider` (free IEX). Missing creds in live mode ⇒ data blocked (never fabricated, never falls back). |
| News | **Off (no-news mode).** No connectors are called at runtime. Benzinga + web scraper are **deferred** (`connectors/deferred/`, raise `deferred in v1`). |
| Massive | **Deferred** (`connectors/deferred/massive.py`). Not imported by the runtime. |
| OpenAI eval | **Mock** deterministic by default; live path implemented (lazy SDK import) when a key is present; no-news output validated. |
| Claude review | Manual-only; live path implemented (lazy SDK import); disabled without a key. |
| Execution | **Simulated internally** (`simulated_internal`). Alpaca paper connector is a guarded stub; no fill is labelled Alpaca paper unless real. |
| Dashboard | Real Streamlit app (needs `streamlit`). |

Everything mocked/deferred/disabled/simulated is labelled as such in code, logs,
reports, and the dashboard's System Health view.

---

## Test results

```
python -m pytest
73 passed
```

Tests prove: real-money trading is disabled/unreachable; manual approval is
required before any order; the freshness guard blocks stale/missing quote or bar
and labels closed sessions; price drift since proposal is blocked; the risk
engine blocks invalid stops, oversized trades, too many positions, daily
trade/loss breaches, wide spreads, low liquidity, and unapproved margin;
Alpaca is the only active data provider and live mode never silently falls back;
no-news evaluations carry the sentinels and invented catalysts are rejected;
Massive/Benzinga are unreachable from the runtime (import-graph + direct-call
guards); config validation fails fast on unsupported v1 settings; `live`
mode cannot be enabled; mock and Alpaca-paper share one order schema; same-day
exits classify into the six categories; the day-trade experiment is gated and
book-separated; auto mode respects the daily auto-approval cap and never bypasses
gates; and mock/fixture news never reaches the runtime path.

---

## Known gaps / honest limitations (v1)

- **Execution is simulated internally.** The Alpaca paper connector is a guarded
  stub; wiring real `alpaca-py` paper calls (broker-native brackets/OCO + order
  reconciliation) is the next step. No fill is labelled an Alpaca paper fill unless real.
- **Live Alpaca data is implemented but not exercised here.** The `AlpacaDataProvider`
  IEX snapshot mapping runs only behind `RUN_LIVE_ALPACA_TESTS=true` (none ran in
  this environment — reported as skipped, not passed). Free/IEX is limited-market data.
- **News is off (no-news baseline).** Benzinga/web/Massive are deferred seams.
- **Costs are not modelled** (net P&L == gross). MFE/MAE are exit-time approximations.
- **No market-holiday calendar** in session classification (weekend-aware only).
- **Baseline comparison** records the no-news baseline structure/fields only; no
  statistical claims are made on the (currently tiny) forward sample.
- `shadow`/`research` modes and the day-trade engine are recognized stubs.
- The dashboard is the minimal 4-tab skeleton, not the full multi-tab UI.

## Recommended next build step

Wire the **real Alpaca paper connector** behind the existing guardrails
(broker-native bracket/OCO with capability checks, order-status reconciliation
feeding `order_events`), validate the live `AlpacaDataProvider` IEX mapping under
`RUN_LIVE_ALPACA_TESTS=true`, and run a forward **no-news** paper sample to start
populating the baseline-comparison tables. Re-introduce the news layer only after
that baseline is proven (see `connectors/deferred/DEFERRED.md` for the triggers).
