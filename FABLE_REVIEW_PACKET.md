# AlphaOS — Fable 5 Review Packet

**Purpose:** context for a senior trading-system architect / quant strategist / safety reviewer who has not followed this project before. Read this alongside the architecture + roadmap review prompt.

**Snapshot at time of writing:** `main` @ `e381096` (3 PRs merged today: labeller output-token-budget fix, live Alpaca catalyst fix, user-override attribution report). One more PR (`feat/labeller-failsafe-visibility`, commit `07f3b14`) is open, not yet merged. Full test suite: **299 passed, 3 skipped** on that branch (the 3 skips are gated live-Alpaca tests, hermetic otherwise). A single open Alpaca **paper** position exists (META, opened today) being tracked toward one forward-evidence calibration datapoint — this is paper money, not real.

This packet does not modify code, change configs, run scans, execute trades, or alter safety settings. It is documentation only.

---

## 1. AlphaOS mission

AlphaOS is a **live-ready semi-autonomous trading operating system** — not merely a passive research tool or a paper-trading dashboard. It is being built to become a real trading operator as soon as the system is proven safe enough, and it is action-oriented by design: it scans, decides, proposes, and (eventually) executes, rather than just reporting.

**Current mode = sim/paper validation. Target direction = live-ready semi-autonomous trading OS, with future plans toward full autonomy.**

Paper/sim execution today is a **system-testing and validation harness**, not the end goal. Its job is to prove — before any real capital is ever at risk — that the following all work correctly under real market conditions:
- scanner quality (does it surface the right candidates?)
- AI decision flow (eval → label → combine, does it produce sound calls?)
- manual approval (is the human checkpoint real and non-bypassable?)
- broker execution (do orders route and fill as expected?)
- order protection (do stops/targets/brackets actually protect the position?)
- risk gates (do they actually block what they're supposed to block?)
- ledger integrity (does every action get recorded accurately?)
- monitoring and exits (does the system notice and react to position state?)
- reconciliation (does the local ledger match the broker's truth?)
- cost/slippage assumptions (is the cost model realistic, calibrated against real fills?)
- learning loops (does the system get smarter from what happens?)

AlphaOS should, end to end:
- scan markets
- surface actionable candidates
- enrich candidates with catalyst / social / narrative evidence
- classify narrative polarity and driver type
- generate proposals, or Armed Watch / Near Action items when action isn't quite justified yet
- allow user override while **preserving** AlphaOS's original recommendation (never overwriting it)
- require manual approval for execution during the current phase
- execute in sim/paper for validation now
- monitor positions
- reconcile orders/fills/outcomes
- learn from trades, missed trades, user overrides, and blocked actions
- prepare for eventual live trading only after strict readiness gates are passed

Real-money trading is deliberately **disabled and unreachable** right now — but that is a current-phase safety posture, not the system's identity. The architecture should be evaluated as a live-trading system in training, not as a paper-trading toy.

---

## 2. NightDesk vs AlphaOS relationship

**AlphaOS is the main trading operator. It does not depend on NightDesk in order to trade.**

AlphaOS is designed as a live-ready semi-autonomous trading system with future plans for full autonomy. It is capable — architecturally — of scanning the market, generating trade ideas, evaluating evidence, creating proposals, managing execution, monitoring positions, learning from outcomes, refining its own operational logic, and creating new hypotheses, all on its own.

**NightDesk** is a separate, deeper long-term edge-research lab. Its role is to research, backtest, forward-test, and validate trading edges over longer cycles than AlphaOS's live operational loop allows. When NightDesk proves a meaningful edge, that playbook or thesis can be pushed into AlphaOS as an additional source of strength.

Boundaries that matter:
- AlphaOS should **not** wait for NightDesk before it can act.
- AlphaOS should **not** rely only on NightDesk-approved plays.
- AlphaOS should be able to go deep immediately and trade like a serious quant/operator system, within its safety gates.
- AlphaOS can create hypotheses from live operational data.
- AlphaOS can notify the user when it detects a possible new setup or edge.
- AlphaOS can send hypotheses, user overrides, failed setups, Armed Watch cases, and post-trade learnings back to NightDesk for deeper research/backtesting.
- NightDesk can later return proven insights to AlphaOS.
- AlphaOS must **not** silently promote a newly discovered setup into an active executable playbook without user notification/approval.
- AlphaOS can refine operational parameters and suggest playbook changes, but material changes should be versioned, logged, and user-visible.

**Short version:** AlphaOS = main trading operator, semi-autonomous now, full-autonomy ambition later. NightDesk = long-term edge research lab, useful but not required for AlphaOS to trade.

**Current implementation status:** the NightDesk link today is a **hook only** — `nightdesk_research_candidate` / `nightdesk_research_reason` flags get set on interesting override outcomes (see §6), but there is no actual import/export pipeline built yet. This is an explicit gap, not a hidden one.

---

## 3. Current architecture summary

Pipeline (each stage's module noted):

| Stage | Module | Role |
|---|---|---|
| Market Interest Scanner | `alphaos/scanner/candidate_scanner.py`, `alphaos/scanner/interest_scanner.py` | Deterministic scan of a fixed universe → interest-ranked shortlist |
| Candidate / Evidence Packet | `alphaos/scanner/candidate_packet.py` | Compact, structured packet per candidate (price action, structure, liquidity, freshness) |
| OpenAI momentum eval | `alphaos/ai/openai_client.py` | Real AI, **no-news baseline** — decides direction/entry/stop/target/decision from price action alone (`NEWS_ENABLED=false` always) |
| AI Category/Playbook Labeller | `alphaos/ai/playbook_classifier.py` | Second AI pass — classifies into an official playbook label, advisory decision (downgrade-only by default); ADVISORY reasoning fields (`missing_conditions`, `upgrade_blockers`, `proposal_readiness`, `what_would_upgrade`) |
| Official Catalyst Enrichment | `alphaos/news/catalyst_enricher.py`, `alphaos/news/official_news_provider.py` | Live Alpaca/Benzinga news → catalyst status/type/confidence, advisory only |
| last30days Narrative Enrichment | `alphaos/research/last30days_enricher.py`, `alphaos/research/last30days_provider.py` | Calls a separate, globally-installed `last30days` skill via subprocess (no vendoring) — Reddit/X/YouTube/HN/Polymarket/GitHub |
| last30days Polarity | `alphaos/ai/last30days_polarity.py` | LLM classification of narrative clusters → bullish/bearish/neutral/unclear + confidence + alignment + driver type + hype/high-risk flag |
| Decision combine | `alphaos/orchestrator.py` (`_resolve_decision`, `_apply_label_floor`) | Eval ⊕ label → final decision. Default: label is a downgrade-only floor. When armed (real AI + a real, direction-aligned positive driver): label may move the decision **up or down**, gated + audited (`decision_adjustments`) |
| Armed Watch | `alphaos/orchestrator.py` | When armed but the label doesn't upgrade → a NEAR-ACTION watchlist flag, not a reject |
| User Override Mode | `alphaos/orchestrator.py` (`create_user_override`, `resolve_user_override`) | Separate decision layer; stores AlphaOS's original recommendation and the user's final decision side by side; re-runs the same gates; only ever creates a PENDING_APPROVAL proposal |
| Attribution learning fields | `alphaos/reports/attribution.py` | Aggregates overrides by action/reason/arming class, executed vs blocked, outcomes, and a heuristic "who outperformed" (user vs AlphaOS vs inconclusive) — always caveated as non-statistical below a sample threshold |
| Safety / risk gates | `alphaos/risk/risk_engine.py`, `alphaos/data/freshness_guard.py`, `alphaos/safety.py` | Freshness, spread, liquidity, crossed-quote, sizing, daily-cap, exposure, kill switch, real-money guard — authoritative, cannot be bypassed by any AI/label/narrative output |
| Cost model | `alphaos/execution/costs.py`, `alphaos/reports/cost_calibration.py` | Modeled slippage/commission vs observed real paper fills; PRELIMINARY status below 20 filled samples |
| Execution providers | `alphaos/execution/order_manager.py`, `alphaos/broker/alpaca_client.py` | `simulated_internal` (default) or `alpaca_paper` (real broker-native bracket orders on Alpaca's PAPER API — still no real money) |
| Ledger / database | `alphaos/journal/journal_store.py`, `alphaos/journal/schema.py` | SQLite, `SCHEMA_VERSION=3`, additive-only migrations, full audit trail |
| Dashboard / reporting | `alphaos/dashboard/streamlit_app.py`, `alphaos/reports/*.py` | Read-only Streamlit dashboard (ephemeral, localhost); daily/weekly reports; CLI (`alphaos <command>`) |

Test status on the latest reviewed branch: **299 passed, 3 skipped** (skips are gated live-Alpaca integration tests requiring `RUN_LIVE_ALPACA_TESTS=true`). Suite is otherwise fully hermetic — mock providers, no network, no real API calls.

---

## 4. Deterministic scanner details

Scanner module: `alphaos/scanner/candidate_scanner.py`.

**Current universe** — a deliberately liquid, fixed 20-name starting list:

```
SPY, QQQ, IWM, DIA, AAPL, MSFT, NVDA, AMD, TSLA, AMZN,
GOOGL, META, NFLX, AVGO, JPM, XLK, XLE, XLF, SMH, COST
```

This is a **controlled starting point, not a permanent limitation**. Fable 5 should review whether and how AlphaOS should expand the universe safely. Possible future directions include:
- a larger liquid US large-cap universe
- a sector-ETF universe
- top relative-volume movers
- an earnings/catalyst-driven universe
- a high-liquidity momentum universe
- playbook-specific universes
- a volatility-filtered universe
- a market-regime-specific universe

**Hard gates are current defaults, not sacred constants** (e.g. `MAX_RISK_PER_TRADE_PCT`, `MAX_SPREAD_PCT`, `MIN_DOLLAR_VOLUME`, `STOP_LOSS_PCT`, `TARGET_REWARD_RISK`, `MIN_REWARD_RISK`, freshness-age thresholds — all in `.env` / `alphaos/config/settings.py`). Fable 5 may recommend how to fine-tune or version them, but any changes should be:
- justified
- testable
- logged
- versioned
- initially shadow-tested where appropriate
- **not** allowed to bypass live-readiness safety (see §7's hard invariants — those specific ones, e.g. the real-money guard and manual approval, are non-negotiable and out of scope for tuning)

---

## 5. last30days / polarity status

- **Keyless CLI provider works.** last30days is called via subprocess against a globally-installed skill (not vendored into this repo). Requires Python ≥3.12 (`LAST30DAYS_PYTHON`); fails open to `unavailable` on any error.
- **X auth/wiring works** — authenticated via personal Safari cookies (`AUTH_TOKEN`/`CT0` in `~/.config/last30days/.env`). Known risk: personal-cookie scraping carries some account risk and cookies expire.
- **YouTube/yt-dlp coverage boost works** — installed and live.
- **Polarity classifier works**, using `gpt-5.4-mini` (`alphaos/ai/last30days_polarity.py`). Classifies bullish/bearish/neutral/unclear + confidence + direction-alignment + narrative-driver type + hype-risk. The **arming decision is computed deterministically on the AlphaOS side** (`_decide_arming`), never trusting the model's own word on whether to arm — arms only on: enabled + arming-allowed + direction-aligned + confidence ≥ `LAST30DAYS_POLARITY_MIN_CONFIDENCE` + coverage ≥ `LAST30DAYS_POLARITY_MIN_SOURCE_COVERAGE` + no official-catalyst conflict.
- **Confidence/coverage gates** are configurable (`LAST30DAYS_POLARITY_MIN_CONFIDENCE`, `_MIN_SOURCE_COVERAGE`) and fail safe to non-arming on any invalid/low-confidence/low-coverage output.
- **High-risk narrative handling:** hype/meme/squeeze-type narratives are classified `high_risk_narrative` — they **can still arm**, but are always tagged manual-only and warned (never auto-suppressed, never auto-approved).
- **Latest live scan outcome:** AMD armed as `high_risk_narrative` (a real, non-mock scan), but the final decision stayed WATCH because the AI labeller independently chose `watch` (the override mechanism cannot invent a `propose` the labeller didn't make — this is correct, intentional gating behavior, not a bug). This produced an Armed Watch flag, not an upgrade.

---

## 6. Armed Watch / User Override status

**Armed Watch definition:** when the decision-override mechanism is *armed* (a real, direction-aligned positive driver present, real AI, override enabled) but the final decision still ends up at WATCH (because the labeller didn't independently upgrade, or the eval isn't tradeable), the candidate is flagged `armed_watch=1` with a reason (`polarity_armed_but_labeller_did_not_upgrade` / `polarity_armed_but_eval_not_tradeable`). This is recorded as a **NEAR-ACTION watchlist item, not a reject** — it does not go into the rejected-candidates flow.

**AMD example:** see §5 — the canonical live case of an armed-but-not-upgraded candidate.

**Labeller reasoning fields** (advisory, never change the decision): `missing_conditions`, `upgrade_blockers`, `proposal_readiness` (not_ready / developing / near_action / ready / unclear), `what_would_upgrade`. These are copied onto the armed-watch record for visibility into *why* it didn't upgrade.

**User Override Mode** (`alphaos/orchestrator.py::create_user_override` / `resolve_user_override`) is a **separate decision layer**:
- Stores AlphaOS's original eval/label/final decision **and** the user's final decision side by side in `user_decision_overrides` — **AlphaOS's original recommendation is never overwritten.**
- Actions: `watch_to_trade`, `propose_to_reject`, `reject_to_trade`, `manual_exit`, `manual_hold`, plus a few less-used variants (`long_to_short`, `reduce_size`, etc.).
- `watch_to_trade` / `reject_to_trade` **re-run the same freshness + risk gates** that a normal proposal would; only on pass does it create a PENDING_APPROVAL proposal — approval is still required, nothing auto-executes. On gate failure, it records `execution_allowed=0` + a specific `blocked_reason` (stale data, wide spread, low liquidity, risk-gate failure, etc.).
- High-risk-narrative overrides are tagged with a warning on the resulting proposal and flagged for NightDesk (see below).

**Decision attribution idea / user vs AlphaOS learning loop:** `resolve_user_override` records the trade outcome (`outcome_r`, `outcome_pnl`, `outcome_status`) and computes a **heuristic** attribution result: `user_outperformed` (user won on something AlphaOS would not have traded), `alphaos_outperformed` (user lost on something AlphaOS would not have traded), or `inconclusive` (both traded, both passed, breakeven, etc.). This is explicitly a heuristic, not a statistical claim — the attribution report (`alphaos/reports/attribution.py`, `alphaos attribution_report` CLI) surfaces win rate / expectancy only when the resolved-override sample is large enough, and always includes a small-sample caveat otherwise (threshold: 20 resolved overrides).

**NightDesk research hook:** `nightdesk_research_candidate` + `nightdesk_research_reason` fields get set automatically on interesting cases (high-risk-narrative override, user traded against AlphaOS's recommendation, user rejected an AlphaOS proposal). This is currently a **flag only** — there is no actual export/import pipeline to NightDesk built yet (see §2, §8).

---

## 7. Execution and safety status

- **Real-money trading is disabled and unreachable.** `REAL_TRADING_ENABLED` must be exactly `"false"`; any other value blocks all order placement. `ALPHAOS_MODE=live` is rejected outright. This is enforced in `alphaos/safety.py` and is treated as a hard invariant — not something to be tuned.
- **Manual approval is required** (`APPROVAL_MODE=manual`) and is non-bypassable by any AI output — no path (label, catalyst, narrative, polarity, user override) can skip it. `high_risk_narrative` proposals are manual-only regardless of approval mode.
- **Execution is currently paper/sim.** Default provider is `simulated_internal` (fills modeled internally). An opt-in `alpaca_paper` provider exists and places real broker-native bracket orders on Alpaca's **paper** API — still zero real money, but real order-routing/fill behavior, which is the point (see the calibration effort in §8/§9-3).
- **No auto-execution exists anywhere in the system today.** Every path that could create an order stops at PENDING_APPROVAL.
- **Safety gates that cannot be bypassed:** freshness, spread, liquidity, crossed-quote, risk/sizing, daily-cap, exposure, kill switch, stop/target/reward-risk minimums, market-session checks, price-drift-since-proposal. No AI/catalyst/last30days/polarity output can override any of these; they are checked authoritatively at proposal-build time and again at approval time.
- **Stop/target/R:R config** (`.env`): `STOP_LOSS_PCT=0.03` (3% stop distance), `TARGET_REWARD_RISK=1.5` (implied ~4.5% target on a long), `MIN_REWARD_RISK=1.2` (hard floor — any proposal below this R:R is rejected). For the live OpenAI engine, the model sets its own levels but `MIN_REWARD_RISK` still clamps them.
- **Order protection expectation:** every filled entry should carry a broker-native bracket (target + stop) when using `alpaca_paper` execution — confirmed working in the most recent live run (see §9-3 open question re: how sizing/exits should evolve).

---

## 8. Current roadmap status

### Done
- OpenAI live (real `gpt-5.4-mini`, both the eval and the labeller; `is_mock=False` verified on real calls)
- `max_completion_tokens` fix for gpt-5.x compatibility (chat.completions rejects `max_tokens`)
- **Labeller output-token-budget fix** (`LABEL_MAX_OUTPUT_TOKENS` 220→800): the old default silently truncated the labeller's JSON on every real call, causing every label to fail-safe to reject — which looked exactly like a conservative decision, not an error, and blocked all live proposals for an unknown period. Fixed and merged; a fail-safe **visibility** layer (rate tracking + warn/critical thresholds in status/dashboard/daily report) is built and pending merge (`feat/labeller-failsafe-visibility`) so a future recurrence would be loud, not silent.
- Deterministic scanner + candidate/evidence packet
- Cost-model calibration framework (modeled vs observed slippage/delay)
- last30days live (keyless CLI + X + YouTube coverage)
- Polarity classifier (arming logic, high-risk-narrative handling)
- Armed Watch
- User Override Mode + attribution-report groundwork (report, CLI, tests — validated end-to-end on mock/hermetic data)
- Live Alpaca catalyst provider bugfix (was silently returning zero catalysts for every symbol due to an SDK response-shape mismatch)

### Pending
- **Attribution report validation on real (non-mock) override data** — the mechanism is proven in tests and on a hermetic demo, but has not yet accumulated real live overrides to report on.
- **Clean Alpaca paper forward-evidence calibration** — a first live entry fill was captured today (META, real broker-native bracket, real fill price vs modeled). Still needs the exit leg and many more samples before the cost model can be confidently retuned (target: 20 filled samples; currently 1, with the position still open).
- Playbook registry / category-permission layer (not yet designed)
- Scheduler v1.5 (there is currently no daemon — every run is a manual one-shot CLI invocation)
- Action-first dashboard (current dashboard is informational/read-only; a more operator-focused view is a stated future direction)
- Scanner v2 / quant intelligence (see the open questions in §9 — this is largely undesigned)
- NightDesk thesis-card import (the flag exists; the pipeline does not)
- Deeper learning loop (beyond the current per-trade metrics + override attribution)

---

## 9. Known open questions for Fable 5

Please review this as a serious live-ready trading system, thinking like a quant hedge fund strategist, trading-system architect, and safety reviewer — not as a normal app roadmap review.

1. **What should AlphaOS's trading algorithm eventually look like?** Signal-scoring based? Playbook based? Portfolio/risk-budget based? Event/catalyst driven? Regime-aware? Multi-strategy? Some hybrid of quant scoring + AI evidence synthesis? The current system is closest to "AI-evidence-driven single-name momentum with an advisory playbook label" — is that the right foundation to build on, or should the core decision architecture change?

2. **How should AlphaOS rank candidates?** What's the right separation between "interesting," "tradable," and "worth risking capital"? What scoring layers should exist, and how should catalyst confidence, price action, liquidity, volatility, cost, spread, social narrative, and risk be combined into one ranking (rather than the current sequential gate-and-floor approach)?

3. **How should AlphaOS trade like a serious quant/operator system?** What calculations are missing? What expected-value framework should exist? What risk-adjusted trade-quality score should exist? How should position sizing be decided (currently fixed-fractional risk per trade)? How should exits be improved beyond a static bracket? How should MFE/MAE (currently unpopulated — see the outcome schema) inform future trades? How should AlphaOS avoid overfitting as more signal layers get added?

4. **How should AlphaOS move from semi-autonomous to eventual full autonomy?** What readiness gates are required? What proof (sample size, live track record, stress tests) is required? What kill switches are required beyond the current single global kill switch? What should remain user-approved for longer, and what can eventually be automated first?

5. **How should AlphaOS create and manage new hypotheses?** It should be able to discover possible new setups from its own operational data, notify the user, and log/version them — but should **not** silently promote them into executable playbooks. It can send candidate hypotheses to NightDesk for deeper backtesting/research. What should this pipeline concretely look like?

6. **How should the universe expand?** Should AlphaOS stay with the current 20 names temporarily? When should it expand, and by what liquidity/volatility/catalyst filters? Should each playbook eventually have its own universe?

7. **How should hard gates evolve?** Which gates are truly non-negotiable safety gates (e.g. the real-money guard, manual approval) versus tunable parameters (e.g. `MAX_SPREAD_PCT`, stop distance)? Which should become regime-aware or playbook-specific rather than global constants?

8. **How should User Override learning actually be used?** When the user overrides AlphaOS and wins, what should the system learn — and how would that safely feed back into decision logic without letting a small, biased sample distort real gates? Same question when the user overrides and loses. How should user judgment vs AlphaOS judgment be scored over time in a way that's statistically honest?

9. **How should NightDesk and AlphaOS interact without dependency?** (See §2.) NightDesk researches long-term edge; AlphaOS trades independently; AlphaOS sends research questions/hypotheses back; NightDesk sends proven findings forward. AlphaOS should never depend on NightDesk to operate. What's the concrete interface/data contract for this that keeps the two properly decoupled?

10. **What roadmap should AlphaOS follow from here?** Immediate next steps, a 30-day plan, a 60-day plan, a 90-day plan. What should be built now versus deferred versus deliberately not built at all?

---

## 10. What Fable 5 should review

Please review:
- **architecture** — is the current pipeline shape (scanner → eval → label → enrichment → combine → gates → approval → execution → learning) the right foundation, or does it need restructuring?
- **roadmap** — sequencing and priority of the pending items in §8, and the 30/60/90-day plan requested in §9-10
- **safety gaps** — anything in §7 that looks incomplete or has a hidden bypass path
- **quant gaps** — the questions in §9, especially 1–3 (algorithm design, ranking, EV/sizing/exits)
- **learning-loop gaps** — §6/§9-8's user-vs-AlphaOS attribution, and whether the current heuristic (rather than statistically rigorous) approach is adequate for its current stage
- **sequencing** — is calibration (§8 pending #2) correctly prioritized before universe expansion or algorithm redesign?
- **missing components** — anything not mentioned above that a serious live-ready trading system would need
- **what to build now vs later** — a direct, opinionated recommendation, not just a list of options

**Known uncertainties to flag explicitly, in case they matter to the review:**
- The attribution "who outperformed" heuristic (§6) has not yet been exercised on real (non-hermetic) override data — its real-world behavior is unproven.
- Cost-model calibration (§8) has exactly one live paper fill so far; the modeled slippage assumptions are not yet validated at any meaningful sample size.
- There is no scheduler/daemon (§8) — every pipeline run today is a manual CLI invocation, which matters for any discussion of autonomy readiness.
- The NightDesk integration (§2, §6) is a flag-only stub with no real pipeline; do not assume it's further along than that.
- Universe (§4) and most numeric gates (§7) are stated defaults from a single operator's initial config, not the result of any systematic optimization or backtest — treat them as a starting point to critique, not as validated parameters.
