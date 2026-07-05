# HANDOVER

**Checkpoint: 2026-07-06 (post PR8 merge + Fable 5 legacy roadmap docs) · branch `main` @ `b659147` · tests 763 passed / 3 skipped / 0 failed · working tree clean · mode PAPER · execution `alpaca_paper` · AI = LIVE (OpenAI) · real-money UNREACHABLE · 0 open positions · kill switch OFF**

> Single entry point for the next session. This project keeps no other handover docs — everything is here. Verify state before trusting any of it (commands in §8).
> 📐 **New this checkpoint — the long-horizon plan exists, read it before proposing "what's next."** `docs/roadmap/alphaos-master-build-plan.md` (strategy: Prime Directives, autonomy ladder L0–L5, 6-phase plan Month 0 → Year 2+), `docs/roadmap/alphaos-pr-implementation-specs.md` (implementation-ready specs for PR9–PR11, skeletons for PR12–PR15, reusable spec/build/audit templates, house-patterns appendix), `docs/roadmap/alphaos-ui-ux-design.md` (operator-console design + Google Stitch prompt). **PR9 ("Turn It On" — unattended scheduler cadence) is the explicitly-designated next PR, fully spec'd already** — see §5/§9.
> ✅ **Still true — protection watchdog is built, merged, AND hardened** (PR2.5/PR2.6, both Opus-audited). Zero-diff against it through PR3–PR8 (each PR's own audit confirms).
> ✅ **Direct push to `main` remains enabled and in active use** (`.claude/settings.json`, `353d9f5`) — PR8 and this checkpoint's docs were both pushed straight to `main`.
> ⚠️ **Two independent AI sessions have now pushed directly to `main` concurrently at least twice** (once during the PR7 follow-up checkpoint, once implied by the HANDOVER-only commits `b411143`/`660de18` appearing between this session's PR8 work) — harmless so far (no conflicts), but `git fetch` + check before pushing if you suspect concurrent activity, same lesson as last checkpoint.

## Changelog (most recent first)
- **AlphaOS Master Build Plan + PR Implementation Specs + UI/UX Design** (`bb669fe`, `b659147`, this checkpoint) — Fable 5's legacy handoff, written because the strategic review that produced it concluded AlphaOS is data-bottlenecked, not intelligence-bottlenecked (TQS/attribution floors near-zero live data), and the prior PR9–PR14 sketch had no PR that actually turns the scheduler on. Three documents in `docs/roadmap/`:
  - `alphaos-master-build-plan.md` — 10 Prime Directives (target sizes the roadmap never the trade; shadow-first always; auto-demote/manual-promote; pre-registration; unknown≠zero; deterministic gates own execution; agents at the edges; enumerated risk classes A/B/C; system always killable+visible); the L0–L5 **Autonomy Ladder** with entry criteria AND automatic rollback triggers per level; six phases with exit-gate numbers (not dates) from Ignition (M0–1) through the Edge Factory (Year 2+), including the **real-money crossing protocol** (§6 Phase 4: preconditions, Tier-0 at 0.25% risk, the new **drawdown governor** −5%/−8% MTD thresholds) and **sleeve architecture** for evidence-driven capital allocation (Phase 5); an honest edge assessment vs retail (structural win, already built) vs institutions (winnable: capacity niches, learning velocity, AI-native breadth, discipline without fatigue — unwinnable: speed, balance sheet); the Never-List (10 hard invariants that survive every phase); standing audit program; post-Fable Sonnet/Opus working protocol; failure playbook.
  - `alphaos-pr-implementation-specs.md` — full implementation-ready specs for **PR9** (LaunchAgent-driven unattended scheduler cadence, an `alphaos/util/alerts.py` ntfy sender, a consecutive-failure self-halt fuse, a dead-man heartbeat CLI+LaunchAgent), **PR10** (versioned setup cards as YAML+DB registry, `card_id`/`card_version` stamping, the new `invalidation_reason` field, the "no entry without a written exit" invariant enforced at the `_execute()` chokepoint), **PR11** (position-health engine + daily brief builder + Moonshot Gap arithmetic, merged into one PR); deliberate skeletons (not full specs — written at build time instead) for **PR12** (hypothesis engine + pre-registration registry), **PR13** (promotion/demotion state machine), **PR14** (red-team debate v0, ONE bear agent, shadow-only), **PR15** (first bounded auto-approval promotion, evidence-gated); reusable **T1–T4 templates** (spec / build-report-back / Opus-audit-rubric / merge-protocol formats — literally the format used to build PR7/PR8); a **house-patterns appendix** writing down the tribal knowledge this session paid for in bugs (date-seeded mock data traps, the SQLite NULL-uniqueness partial-index recipe, additive migration mechanics, the enricher/pure-compute split, the `_execute()` chokepoint rule, the shadow-layer proof kit, the reporting law, "auditors run their own empirical probes, never trust testimony").
  - `alphaos-ui-ux-design.md` — the operator-console design for a controlled-autonomy trading OS (explicitly NOT a generic brokerage dashboard): the annunciator principle (permanent mode/autonomy/kill-switch/heartbeat strip on every screen), asymmetric friction (viewing instant, approving deliberate, escalating risk heavy, **stopping always the single easiest action**, real-money lock has literally no unlock UI affordance), evidence-state honesty enforced in pixels (no score without confidence, no aggregate without n-vs-floor, mock/paper always watermarked), a fixed 5-rung reasoning-disclosure ladder, 9 screens across 3 planes (Operate/Understand/Govern) with full written wireframes for each, the "Tonight" home-screen wireframe, a false-confidence avoid-list, and a copy-paste Google Stitch mockup prompt. First-build recommendation: annunciator strip + Tonight tab + position health cards, alongside PR11, on the existing Streamlit dashboard (no rewrite).
  - These three documents are **strategy + machine-drawings, not code** — nothing in `alphaos/` changed as part of this entry. Read them before proposing "what should we build next"; the answer is already written down (PR9, see the banner above and §5/§9).
- **PR8 — Attribution v2 / Counterfactual ΔR** (merged `799d9cc`; branch commits `613e28e` + audit-fixup `a517b41`). A measurement-only ledger pairing decision-**divergence** events with `delta_r = actual_path_r − alphaos_path_r`, read entirely from the EXISTING outcome ledger (`candidate_outcomes.replay_r` / `trade_outcomes.realized_r`) — explicitly **no second replay engine**, one replay engine one truth. Exactly 5 supported event types (rows exist ONLY where two paths diverged; pure one-path no-action decisions like reject-no-action get no row and stay analyzable via report-time joins on `candidate_outcomes`, by design — avoids doubling the outcome ledger):
  - `propose_user_rejected` (agent=user): AlphaOS proposed, human rejected. `alphaos_path_r`=frozen bracket replay_r, `actual_path_r`=0.0 (directly observed — no position opened), `delta_r = 0 − replay_r`. Winning replay → negative delta (rejection cost value); losing replay → positive (rejection saved value).
  - `user_override_trade` (agent=user): AlphaOS said watch/reject/no-propose, user traded anyway (`alphaos_would_have_traded=0 AND user_final_decision='propose'`; explicitly excludes `PROPOSE_TO_REJECT` overrides — those flow through the type above instead, via the proposal they reject, never double-counted). `alphaos_path_r`=0.0 (directly observed), `actual_path_r`=`trade_outcomes.realized_r` if a real trade closed, else the override's own `candidate_type='user_override'` replay_r.
  - `propose_approved_executed` (agent=execution): both sides agreed — measures the EXECUTION gap, never decision divergence. `delta_r` is ALWAYS NULL; `execution_delta_r = realized_r − replay_r` (r_basis=`net_vs_gross`, since realized_r is net-of-costs and replay_r is gross — recorded as a label, not "fixed"). Partial if only one side resolves.
  - `propose_expired` (agent=operational) / `propose_blocked` (agent=gate): same `0 − replay_r` formula as the reject type, with `expired_reason`/`blocked_reason_code` copied verbatim from the source row. **Gate ΔR must never automatically tune or weaken a gate** — this is measurement of the gate, never an input to it.
  - Schema: new `attribution_records` table + two SQLite NULL-safe **partial unique indexes** (`idx_attr_proposal_unique` WHERE proposal_id IS NOT NULL, `idx_attr_override_unique` WHERE override_id IS NOT NULL — same fix class as PR7's `tqs_scores`, since a plain UNIQUE with nullable columns does not dedupe when every NULL is distinct); `lineage_snapshots.attribution_config_hash` column. Single setting `ATTRIBUTION_ENABLED` (default true). Hooked into exactly ONE call site — `Orchestrator.outcomes_update()`, strictly after the existing `seed_pending_outcomes()`/`update_pending_outcomes()` calls — zero orchestrator decide/approve/execute wiring. Reporting: `attribution_report()` v2 block + digest `attribution_shadow` section, floor-gated (≥30 resolved live events per type AND ≥28-day span before showing a mean/sum delta; below floor → counts only + `below_sample_floor`), aggregated by `attribution_type`×`agent` only — **never a single global cross-type "system value"**, since one candidate lifecycle can legitimately produce more than one attribution row (e.g. a blocked proposal a user later overrode via a separate event). +82 tests (`test_attribution.py` 29 pure formulas, `test_attribution_flow.py` 53 end-to-end incl. behavior-neutrality A/B, SQLite `IntegrityError` probes, unknown-never-zero policy tests, mock/demo exclusion, the eval-less-mock-candidate regression twin of the PR7 fix).
  - **Opus formal audit, same PR**: verdict APPROVE, no BLOCKER/HIGH/MEDIUM. Two LOW findings, both same root cause (`candidate_outcome_for_proposal()` looked up a candidate's frozen replay by `candidate_id` alone) — **LOW-1**: `candidate_outcomes` seeds at most one `'proposal'/'blocked'` row per candidate, frozen at first seed; a candidate that later grows a SECOND proposal with different levels could silently cross-link to the wrong proposal's replay (confirmed present in code, confirmed unreachable today — 0 candidates with >1 proposal across 5 real mock scans, but latent). **LOW-2**: an override-created proposal's frozen levels are seeded under `candidate_type='user_override'`, not `'proposal'/'blocked'`, so `propose_approved_executed` could never find them — its execution gap stayed permanently `partial`/unresolvable for every override-origin trade. **Fixed together** (`a517b41`): the lookup now (a) also checks `'user_override'` (closes LOW-2 — override-origin proposals now resolve real execution gaps) and (b) requires the row's frozen `entry_reference_price`/`stop_price`/`target_price` to exactly match the SPECIFIC proposal being resolved, treating a mismatch as "no row" — an honest `pending`, never a wrong number (closes LOW-1). +2 regression tests, verified against the exact adversarial cases the audit constructed. Both audits (formula/scope/no-read + this follow-up) ran with independent empirical probes (cross-link injection, full-table SHA256 immutability hashing across extra passes, NULL-uniqueness IntegrityError injection) — not just reading the PR's own tests.
- **PR7 follow-up: fix mock-mode row mislabeling in TQS** (`cc347fd`, audit finding MEDIUM-1) + **test-hygiene fix: 2 pre-existing flaky tests, date-seeded-mock-data class** (`6cde2c2`) — both already covered in detail by the prior checkpoint; unchanged since, full detail preserved in `git log -- HANDOVER.md` if needed. Short version: an eval-less mock-mode candidate could score `is_mock=0`/`degraded` instead of `is_mock=1`/`'mock'` in TQS (fixed: derive from `settings.is_mock OR eval.is_mock`, same rule PR8 reuses); `test_scheduler.py`/`test_decision_override.py` had two different date-seeded-mock-data fragilities (fixture symbol colliding with the scanner universe; an assertion assuming the mock scan always organically produces a specific decision-category mix) — both rewritten to deterministic direct construction.
- **PR3–PR7 (Scheduler v1.5, Decision Lineage, Earnings-Proximity, Proposal TTL, TQS v0 Shadow Scoring)** — all merged, audited, unchanged since. Full per-PR detail (formulas, config, audit findings incl. PR6's adversarially-proven HIGH finding on the auto-approval TTL bypass) preserved in `git log --oneline -- HANDOVER.md` / prior revisions of this file — not re-transcribed here to keep this document from growing unboundedly; nothing about them changed this checkpoint.
- **Prior to PR3 (2026-07-03 checkpoint and earlier)**: protection watchdog built + hardened (PR2.5/PR2.6, both Opus-audited); the 2026-07-02 META protection-mismatch incident (root cause fixed at source); measurement foundation (MFE/MAE + counterfactual `candidate_outcomes` ledger); labeller fail-safe visibility; Roadmap 2.3–2.8 (interest scanner, catalyst/last30days/polarity enrichment, gated labeller override, Armed Watch, User Override). Full detail: `git log`.

---

## 1. Current project state
AlphaOS is a **learning-first, paper-trading "operating system"** on a Mac mini, Python 3.12 venv at `.venv` (uv). `main` is the single line of development, clean at `b659147` (763/3/0). Pipeline: **Scheduler (cadence exists, CLI-only, still not wired unattended) → Scanner → Candidate Packet → AI Labeller → Catalyst/last30days/Polarity/Earnings-Proximity enrichment → decision combine (lineage-stamped) → Armed Watch → gates (incl. Proposal TTL) → manual approval (+ User Override layer) → sim/paper execution → monitor/reconcile/exit → protection watchdog → ledger → counterfactual outcome measurement → TQS v0 shadow scoring → Attribution v2 counterfactual ΔR ledger.** Real-money trading remains `unreachable` throughout; every layer since PR3 was independently audited zero-diff against the safety-critical paths.

**The strategic picture, established this checkpoint**: AlphaOS is a complete, audited instrument that has barely been switched on. TQS's own validation floor (≥300 live-resolved candidates over ≥8 weeks) and Attribution's floor (≥30 live events/type over ≥28 days) are both near-zero today because nothing runs the scheduler unattended — `scheduler_run_once`/`scheduler_run_job` still require manual invocation (or an external scheduler not yet configured). **This is now the explicitly-designated next problem to solve (PR9), not an open question** — see the master build plan's Phase 1 (Ignition) and the fully-written PR9 spec in `alphaos-pr-implementation-specs.md`.

## 2. What was just implemented (this checkpoint)
- **Nothing in `alphaos/` changed this checkpoint.** This was a documentation-only handover refresh (per the handover-checkpoint skill's own rule: verify state, do not start new feature work).
- The actual "just implemented" work belongs to the PREVIOUS session/turn, already on `main`: **PR8 Attribution v2** (merged + audit-fixed, see changelog) and the **three Fable 5 legacy roadmap documents** (`docs/roadmap/alphaos-master-build-plan.md`, `alphaos-pr-implementation-specs.md`, `alphaos-ui-ux-design.md`).

## 3. What is working (verified this checkpoint)
- **Full suite: 763 passed, 3 skipped, 0 failed** — directly re-run fresh this session (`.venv/bin/python -m pytest tests/`), exit code 0.
- `git status` clean; `main` exactly matches `origin/main` at `b659147` (verified via `git fetch` + `git status -sb`).
- **Live-account facts independently re-verified this session** (this environment DOES have real `.env` credentials, unlike the prior checkpoint's bare-worktree session): read-only Alpaca paper-account check → **0 open positions**. `system_health()` → `real_money_trading="unreachable"`, `market_data_mode="live"`, `ai_primary="openai / configured"`. Kill switch → `is_engaged()=False` (off). `.env` confirms `EXECUTION_PROVIDER=alpaca_paper`, `ALPHAOS_MODE=paper`, `APPROVAL_MODE=manual`, `REAL_TRADING_ENABLED=false`, `ALLOW_REAL_ORDERS=false`; no `ATTRIBUTION_ENABLED`/`TQS_SHADOW_ENABLED`/`SCHEDULER_*` overrides present — all PR3–PR8 features run on code defaults in the real deployment.
- PR8's own audit re-confirmed with independent adversarial probes this checkpoint's authoring session, not re-run today (nothing changed to warrant it).

## 4. Partially implemented (and what's missing to finish)
- **Scheduler has cadence but no automation wiring** (PR3, still true) — `alphaos/scheduler/` jobs (scan/monitor/outcomes_update/daily_digest) exist, are tested, and are correctly gated, but nothing invokes them unattended. **This is PR9, fully spec'd** in `alphaos-pr-implementation-specs.md` §PR9 (LaunchAgent + alert sender + self-halt fuse + heartbeat) — the single highest-leverage next PR, because every downstream floor depends on it.
- **TQS v0 and Attribution v2 both have near-zero real accumulated data** — both score/attribute every mock-mode run too (correctly flagged `is_mock`/`'mock'` so it's excludable), but the actual validation questions ("do high-TQS trades outperform?", "what does gate X actually cost/save in ΔR?") can't be answered until the floors are met. Neither has its own calibration/analysis engine yet (explicit non-goal both times) — that's downstream of PR9, not before it.
- **Earnings-proximity provider is mock/static only** (PR5) — a live earnings-calendar vendor integration is deferred, designed to slot behind the existing `make_earnings_provider` factory later with zero call-site changes.
- **Protection watchdog cosmetic follow-ups** (PR2.6 audit, still open, all non-blocking): dashboard doesn't show `unverifiable`/qty-mismatch as dedicated tiles (CLI does); qty-mismatch severity doesn't grade by magnitude; no live-gated test confirms Alpaca accepts GTC brackets; `REPLACED` leg state isn't in `_LIVE_LEG_STATES`; `_simulate_fill` doesn't pass a resolved TIF.
- **Dashboard has no UI/UX work yet against the new design doc** — `alphaos-ui-ux-design.md` specifies an annunciator strip + Tonight tab + position-health cards as the first build, recommended alongside PR11 (not started).
- **Cost-model calibration**: still ~1 real fill ever (the historical META trade). Every expectancy number the system currently produces should be read as a paper upper bound, per the master plan's own operating doctrine.

## 5. Not done yet (deferred / future)
**The roadmap is now written down in full** — do not re-derive it from scratch; read `docs/roadmap/alphaos-master-build-plan.md` (strategy/phases) and `docs/roadmap/alphaos-pr-implementation-specs.md` (implementation detail) first. Summary of what's next, in order:
- **PR9 — Turn It On** (next, fully spec'd): LaunchAgent-driven unattended scheduler cadence, `alphaos/util/alerts.py` (ntfy sender — `ntfy_topic` setting already exists, unused), consecutive-failure self-halt fuse, dead-man heartbeat. Starts every data clock (TQS 8-week, attribution 28-day, cost calibration).
- **PR10 — Setup Cards v1** + the "no entry without a written exit" invariant (new `invalidation_reason` field, enforced at the `_execute()` chokepoint).
- **PR11 — Daily Brief + Portfolio Health** (merged): per-position health engine, the Moonshot Gap arithmetic report, one-action-item daily brief.
- **PR12–PR15** (skeletons only, full specs written at build time): hypothesis engine + pre-registration registry; promotion/demotion state machine; red-team debate v0 (ONE bear agent, shadow); first bounded auto-approval promotion (L3 autonomy, evidence-gated).
- Beyond PR15: universe expansion (shadow-ranked), live earnings provider, regime engine, portfolio concentration monitor, the real-money crossing protocol (Phase 4, with its own drawdown governor), sleeve-based capital allocation (Phase 5), and the "edge factory" phase (Year 2+: alternative data enrichers, champion-challenger model governance, chaos drills) — all detailed in the master build plan §6.
- UI/UX work against the new design doc (annunciator strip, Tonight tab, health cards) has no PR assigned yet — natural to bundle with PR11.

## 6. Test results
- **763 passed, 3 skipped, 0 failed** on `main` @ `b659147`, directly re-verified this session.
- Skips = `tests/test_live_alpaca.py` (gated behind `RUN_LIVE_ALPACA_TESTS=true`). Fully hermetic otherwise.
- 62 test files total. This checkpoint's relevant additions since the last handover (681 baseline): PR8 attribution +82 (`test_attribution.py` 29, `test_attribution_flow.py` 53, the latter including the 2 audit-fixup regression tests).
- Run: `.venv/bin/python -m pytest` (~6–7 minutes; budget for it).

## 7. Known risks / blockers
1. **Data bottleneck, not intelligence bottleneck** (the master plan's central finding): TQS/Attribution validation floors are near-zero because nothing runs the scheduler unattended. This is the #1 risk to the whole roadmap's credibility and is exactly what PR9 exists to fix — do not let PR10+ get built before PR9 lands, or you'll be building consumers for data that still isn't flowing.
2. **Concurrent-session pushes to `main`**: has now happened at least twice (see the ⚠️ banner). Harmless so far. `git fetch` + check before pushing if you suspect another session is active against this repo.
3. **Stale local feature branches**: `feat/attribution-v2-counterfactual-delta-r` plus everything already listed at prior checkpoints. All content is in `main`; harmless; `git branch -d` at your discretion.
4. **Chain cost**: every real `interest_scan` still costs OpenAI money; `LAST30DAYS_ENABLED`/`POLARITY_ENABLED` on in `.env`.
5. **Mock market data is date-seeded** (`{symbol}:{market_date()}`) — has broken merged tests twice already via two different failure shapes (exact-boundary price probes; assuming the mock scan's organic candidate mix always contains a specific decision category). Generalized rule, now proven twice: prefer deterministic direct construction (`inject_pending_proposal`, hand-built rows) over depending on what a natural mock scan happens to produce, for anything even loosely safety- or assertion-critical. Written down permanently in the PR specs doc's house-patterns appendix (§H.1).
6. **Protection watchdog cosmetic follow-ups** (§4) — none urgent/blocking.
7. **Paper expectancy is an upper bound, not a fact** — cost model calibrated on ~1 real fill; treat every expectancy/ΔR number in reports as optimistic until the calibration campaign (Phase 3 of the master plan) runs.

## 8. Exact commands to run next
```bash
cd "/Users/ck/Documents/Claude Playground/AlphaOS"

# confirm the account is actually clean (read-only) — confirmed 0 open positions this checkpoint
.venv/bin/python -c "
from alphaos.config.settings import load_settings
from alpaca.trading.client import TradingClient
s = load_settings(); tc = TradingClient(s.alpaca_api_key, s.alpaca_secret_key, paper=True)
print('open positions:', tc.get_all_positions())
"   # expect: []

# verify code state
.venv/bin/python -m pytest                 # expect: 763 passed, 3 skipped, 0 failed (~6-7 min)
git status -sb && git log --oneline | head -8
git branch --show-current                  # expect: main

# read the roadmap BEFORE proposing "what's next" -- it's already answered (PR9)
# docs/roadmap/alphaos-master-build-plan.md            (strategy, phases, autonomy ladder)
# docs/roadmap/alphaos-pr-implementation-specs.md      (PR9-PR11 full specs, PR12-15 skeletons)
# docs/roadmap/alphaos-ui-ux-design.md                 (operator console design)

# system status
.venv/bin/python -m alphaos status
.venv/bin/python -m alphaos protection_status
.venv/bin/python -m alphaos scheduler_status

# attribution / TQS / outcomes visibility (PR7/PR8)
.venv/bin/python -m alphaos outcomes_update        # seed + resolve candidate_outcomes + attribution_records
.venv/bin/python -m alphaos outcomes_report
.venv/bin/python -m alphaos attribution_report      # now includes the PR8 "v2" block
.venv/bin/python -m alphaos scheduler_run_job daily_digest   # includes tqs_shadow + attribution_shadow sections

# SAFE mock testing:
ALPHAOS_MODE=mock EXECUTION_PROVIDER=simulated_internal LAST30DAYS_ENABLED=false \
  LAST30DAYS_POLARITY_ENABLED=false ALPHAOS_DB_PATH=data/demo.db .venv/bin/python -m alphaos interest_scan
```

## 9. Recommended next prompt (paste into a fresh window)
```
Read HANDOVER.md in the AlphaOS repo first (single source of truth). Then read
docs/roadmap/alphaos-master-build-plan.md and docs/roadmap/alphaos-pr-implementation-specs.md
before proposing anything -- the roadmap is already written down in detail, not an open
question. main is clean at b659147, 763 passed / 3 skipped / 0 failed. PR3 through PR8 are
all merged and independently audited (PR8 = Attribution v2 / counterfactual ΔR, its own
Opus audit found + fixed 2 LOW findings, both re-verified). Three Fable 5 legacy roadmap
docs were added this checkpoint (master build plan, PR implementation specs, UI/UX design).

Verify state first: `.venv/bin/python -m pytest` (expect 763/3/0, ~6-7 min), confirm branch
main @ b659147 or later, confirm the paper account has 0 open positions (§8 has the
read-only command -- real credentials ARE available in this environment).

The next task is PR9 -- "Turn It On": unattended scheduler cadence via a LaunchAgent
(the pattern already exists for the SG Card Tracker project's own automation -- follow the
same launchctl approach, NOT cron), a new alphaos/util/alerts.py ntfy sender (the
ntfy_topic setting already exists in Settings, unused so far), a consecutive-failure
self-halt fuse, and a dead-man heartbeat CLI + second LaunchAgent. Full implementation-
ready spec is in docs/roadmap/alphaos-pr-implementation-specs.md section "PR9 -- TURN IT
ON" -- read it in full before writing any code; it specifies exact deliverables, settings,
tests, and acceptance criteria (10 consecutive unattended trading days + one kill-switch
drill + one failure-alert drill).

This PR matters more than it looks: TQS's validation floor (>=300 live-resolved candidates
over >=8 weeks) and Attribution's floor (>=30 live events/type over >=28 days) are both
near-zero today specifically because nothing runs the scheduler unattended. PR9 is what
starts those clocks. Do not let a later PR (setup cards, hypothesis engine, debate layer)
get built before this one -- per the master plan, that's building intelligence layers
before the data engine runs, the single biggest strategic risk this codebase currently has.

Follow the established protocol: ground in the current code before speccing anything not
already fully spec'd, implement, write deterministic tests (never depend on what a natural
mock scan produces -- see HANDOVER section 7.5 and the PR specs doc's house-patterns
appendix), run the full suite, get an independent review + Opus-style audit before merge,
and do NOT merge to main without explicit instruction.

Hard constraints (HANDOVER section 10): real-money stays unreachable; manual approval
non-bypassable; no AI/catalyst/last30days/polarity/earnings/TQS/attribution output bypasses
gates or auto-executes; migrations additive only; keep tests green; TQS/attribution/
lineage/earnings are measurement/audit-only -- none may be read by any gate/eval/labeller/
risk/execution path; the protection watchdog detects + blocks only; the proposal-TTL guard
must stay enforced at the _execute() chokepoint; attribution's two SQLite partial unique
indexes (proposal-anchored, override-anchored) must not be collapsed into a single naive
UNIQUE constraint (NULL-uniqueness trap); attribution never builds a second replay engine
-- it only ever reads candidate_outcomes.replay_r / trade_outcomes.realized_r as already
computed.
```

## 10. Anything the next session must NOT change (hard invariants)
- **Real-money trading stays unreachable.** `REAL_TRADING_ENABLED=false`, `ALLOW_REAL_ORDERS=false`; `ALPHAOS_MODE=live` rejected. Do not touch `safety.py`. `system_health()["real_money_trading"]` must remain `"unreachable"`.
- **Manual approval is the default and non-bypassable** (`APPROVAL_MODE=manual`). No path may auto-submit or skip approval. `high_risk_narrative` proposals are manual-only regardless of approval mode.
- **No AI/catalyst/last30days/polarity/earnings/TQS/attribution output bypasses gates.** Freshness, spread, liquidity, crossed-quote, risk, sizing, daily-cap, exposure, kill switch, stop/target, market-session, price-drift, and proposal-TTL gates are authoritative.
- **AI category label is ADVISORY; the override is gated + symmetric.** Default downgrade-only. When ARMED it may move the decision UP or DOWN, gated + audited (`decision_adjustments`).
- **Polarity is CONTEXT that can ARM, never EXECUTE.** Deterministic AlphaOS-side arming only; fails safe to non-arming.
- **User Override (2.8) is a SEPARATE decision layer; NEVER rewrites AlphaOS's recommendation**, never bypasses gates/approval/real-money guard, never auto-executes.
- **The counterfactual outcome ledger (`candidate_outcomes`) is PURE MEASUREMENT.** Never read by any gate/eval/labeller/risk/execution path — write-only from `alphaos/learning/`. `decision_at_utc` stays the anchor for all forward-window math (never seed-time anchoring).
- **MFE/MAE stay textbook-anchored** (entry = implicit R=0; MFE≥0, MAE≤0 always).
- **The broker protection watchdog (`alphaos/execution/protection_watchdog.py`) is DETECT + BLOCK ONLY.** Never calls `close_position()`, cancels, or submits. Resolution is human-triggered only.
- **Multi-day/swing TIF policy**: `max_holding_days >= 1` (or `None`/unknown) must submit persistent (`gtc`) protective legs by default; only `max_holding_days==0` may default to `day`. `ALLOW_DAY_TIF_FOR_MULTIDAY_POSITIONS` must stay `false` by default.
- **Decision lineage (PR4) is measurement-only.** `lineage_id` must never be read back to influence a decision.
- **Earnings-proximity (PR5) is advisory-only by default.** Missing/disabled/stale/unavailable data must always surface as an explicit non-`ok` status, never silently "safe."
- **Proposal TTL (PR6) is the one PR that IS a hard safety gate.** No stale proposal may reach broker submission via ANY path — both manual approval AND auto-approval funnel through the `_execute()` chokepoint's TTL check. A missing/legacy/unparseable expiry fails safe as expired. Do not add a new execution path that skips it (PR6's own audit caught exactly this bypass once already).
- **TQS v0 (PR7) is shadow/measurement-only.** `score_scan_batch()`/`score_proposal()` run strictly AFTER decisions are committed — the current call sites are the last statement in `run_scan_once()` and right after `_override_open_trade()`'s own commit. No gate/risk/approval/execution code may query `tqs_scores`. Weights/buckets stay code constants tied to `TQS_VERSION`; never env-tunable, never retro-rescored.
- **Attribution v2 (PR8) is shadow/measurement-only, hooked into exactly ONE call site.** `discover_events()`/`resolve_pending()` run only inside `Orchestrator.outcomes_update()`, strictly after the existing outcome-ledger calls. No decision path may import `alphaos.attribution` or query `attribution_records`. **A row exists only where two paths diverged** — do not add rows for pure one-path no-action decisions (reject/watch/armed-watch-no-action); those stay analyzable via report-time joins on `candidate_outcomes`. **Unknown is never 0R** — `0.0` appears only where "no position was opened" is a directly-observed fact (proven by the source row's own terminal status), never as a substitute for missing/ambiguous/unavailable replay data. **One replay engine** — attribution must only ever READ `candidate_outcomes.replay_r`/`trade_outcomes.realized_r` as `alphaos/learning/outcomes_engine.py` already computed them; never build a second replay/backtest implementation inside `alphaos/attribution/`. The two SQLite partial unique indexes (`idx_attr_proposal_unique` WHERE proposal_id IS NOT NULL, `idx_attr_override_unique` WHERE override_id IS NOT NULL) exist because a plain UNIQUE constraint is not NULL-safe in SQLite — do not collapse them into one naive constraint. **Resolved/unresolvable rows are never re-touched** (first-resolution-wins, matching `update_pending_outcomes`'s own convention) — do not add logic that re-derives an already-terminal attribution row. Gate/expiry ΔR (`propose_blocked`/`propose_expired`) must never automatically tune, weaken, or feed back into any gate threshold.
- **`last30days` CLI provider must stay gated on mock mode** — `settings.is_mock=true` always substitutes the mock provider regardless of `LAST30DAYS_PROVIDER=cli`.
- **`LABEL_MAX_OUTPUT_TOKENS` must stay ≥512** (guarded by a test).
- **Execution = `simulated_internal`** unless deliberately enabling opt-in `alpaca_paper` (paper-only, explicit intent).
- **Migrations additive only.** `SCHEMA_VERSION` stays 3 for additive changes; use `_reconcile_columns`/partial unique indexes for any new NULL-uniqueness need (see PR3's `job_runs` lock, PR7's `tqs_scores`, PR8's `attribution_records` for the established pattern).
- **Audit/evidence writes never gate execution/exit paths** (best-effort, after the action).
- **Dashboard stays read-only on render**; do not expose on the network without auth. Any new UI work (per `alphaos-ui-ux-design.md`) must route through the SAME orchestrator methods and gates as the CLI — no UI-only pathways, no client-side state implying authority the server doesn't enforce.
- **Mock market prices are seeded per `{symbol}:{market_date()}`** — new tests that derive exact price/R-multiple/session boundaries, or that assume a natural mock scan's decision-category mix, must use deterministic direct injection instead (this has broken merged tests twice; see §7.5 and the PR specs doc's house-patterns appendix for the full lesson).
- **The moonshot target (10% MoM) may never become an input to position sizing**, per the master build plan's Prime Directive #1 — this is a forward-looking invariant for any future PR touching sizing, written down now so it's never relitigated under pressure later.
- Do not change OpenAI decision logic / risk/freshness thresholds / bracket-OCO-watchdog exits / Alpaca submission (beyond the TIF and TTL policies above) without explicit intent.
