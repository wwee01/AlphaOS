# AlphaOS — Fable 5 Architecture / Quant / Safety Review

**Reviewer stance:** senior trading-system architect + quant strategist + safety reviewer.
**Inputs:** `FABLE_REVIEW_PACKET.md` (2026-07-02, `main` @ `e381096`, 299 passed / 3 skipped, one open META paper position, calibration 1/20).
**Scope discipline:** review only — no code, config, scans, or trades were touched.

---

## 1. Executive assessment

**The engineering direction is unusually strong; the trading core is the weakest layer — and the roadmap should be re-pointed at fixing that asymmetry.**

What you have built so far is, frankly, the part most retail-grade systems never build: non-bypassable deterministic gates re-checked at approval time, decision layers stored separately with full audit, fail-safe-to-conservative defaults, additive-only schema, hermetic fast tests, broker-native order protection, and honest small-sample caveats. This is institutional-grade *operational* discipline. Keep it; it is the moat that makes eventual autonomy even discussable.

The coherent-ambition test also passes: "live-ready operator, paper as validation harness" is the right framing, and the safety posture (real money unreachable, manual approval non-bypassable) matches the current evidence level exactly.

**The biggest opportunity:** the decision-data flywheel. AlphaOS already snapshots rich context for every decision (candidate packets, eval, label, catalyst, polarity, gates, overrides). What it does *not* yet do is record what happened next for decisions that didn't become trades. Add forward-outcome tracking for every candidate/proposal/reject/armed-watch (a "counterfactual ledger") and every scan becomes labeled training data — decoupling learning speed from trade count. At ~7–20 candidates/day that is 150–400 labeled observations/month versus ~10–20 trades/month. Nearly everything else you asked about (scoring, gate tuning, autonomy proof, user attribution) hangs off this one addition.

**The biggest danger (three-headed):**
1. **Scaling before measuring.** The current signal is an LLM's momentum opinion on 20 of the most efficient instruments on Earth, no-news, at low cadence. The prior on post-cost edge is roughly zero. Expanding universe/cadence/autonomy before an expectancy-measurement engine exists risks automating losses very efficiently.
2. **The silent-failure class.** You have already hit it twice (labeller token truncation, catalyst NewsSet parse) — both failed *safe* and therefore looked like conservatism while silently disabling the system. In live trading the inverse variant (a failure that silently degrades *protection*) is the nightmare case. The fail-safe visibility layer is the right response; it needs to be generalized to every layer.
3. **Small-sample false learning.** With n in the single digits, any "learning loop" (including user-vs-AlphaOS attribution) will confidently learn noise unless sample floors and shrinkage are structural, not aspirational.

**Verdict:** proceed — but re-sequence. Cadence (scheduler) + measurement (counterfactual ledger, MFE/MAE, lineage) come before universe expansion, scanner v2, or any autonomy step. Autonomy milestones should be keyed to *sample counts and proven invariants*, never to calendar time.

Honest math to anchor expectations: distinguishing a +0.25R mean edge from zero (σ≈1R) needs ~100 trades; +0.1R needs ~600. You will not get there on trades alone this year — candidate-level counterfactual data is how you get statistical power sooner.

---

## 2. Top 10 architectural strengths

1. **Real-money unreachability as a code invariant**, not a config habit (`safety.py`, exact-string check, `live` mode rejected).
2. **Two-phase deterministic gating** — gates evaluated at proposal build AND re-checked at approval; no AI output can bypass.
3. **Decision-layer separation with audit** — eval vs label vs final vs user decision, all preserved (`decision_adjustments`, `user_decision_overrides`); nothing is overwritten.
4. **Fail-safe-to-conservative posture everywhere** + the new fail-safe *visibility* layer (rate + reason taxonomy). The truncation incident was diagnosed and answered correctly: keep the fail-safe, make it loud.
5. **Deterministic arming** — polarity can only ARM via AlphaOS-side checks (`_decide_arming`); the model's self-reported enthusiasm is never trusted.
6. **No-news eval baseline** — a clean ablation design: every enrichment layer's marginal value can eventually be measured against a stable core.
7. **User Override as side-by-side record** — textbook counterfactual data collection; also the correct product answer (user stays sovereign, system stays honest).
8. **Broker-native bracket protection** — position safety survives local process death; verified live on META.
9. **Test culture** — 299 hermetic tests in ~1.2s; mock-first providers; date-flakiness fixed at the root rather than papered over.
10. **Ledger discipline** — additive-only migrations, IDs everywhere, reconciliation reports, calibration explicitly `PRELIMINARY` below sample floors.

---

## 3. Top 10 architectural risks

1. **Edge risk (highest).** No evidence engine exists: no backtests (deferred to NightDesk, which isn't built), no counterfactual tracking, low forward cadence. You currently cannot distinguish luck from skill on any timescale that matters.
2. **LLM-as-primary-signal.** Uncalibrated confidence numbers, nondeterminism, and *silent model drift* — `gpt-5.4-mini` is a moving target behind a fixed name. Nothing today pins or even logs prompt/model versions per decision, and no calibration curve (stated confidence vs realized outcome) exists.
3. **Silent-failure class, generalized.** Two instances found. Every enrichment/AI layer has a fail-safe path that can mask total failure as conservatism. Only the labeller has visibility so far. The same monitoring must cover: proposal-rate collapse, catalyst `none_found`/`unavailable` rates, polarity `unclear` rates, enrichment timeout rates.
4. **Sample starvation + false learning.** Gates and thresholds are single-operator defaults; the attribution heuristic is asymmetric; nothing structurally prevents tuning on n<30.
5. **Data-feed validity (IEX free tier).** IEX top-of-book is ~2–3% of consolidated volume, not NBBO. Your spread gate, freshness gate, and slippage calibration are validating against unrepresentative quotes. Fine for paper mechanics; **not adequate for live go/no-go**. Also note: `MAX_SPREAD_PCT=0.01` (1%) and `MIN_DOLLAR_VOLUME=$2M` are so permissive for this universe they are effectively decorative — the gates exist but currently filter nothing.
6. **Event blindness.** A 5-day-hold momentum system on single names with **no earnings-calendar awareness** will eventually hold NVDA through earnings by accident. Catalyst enrichment is news-driven (backward-looking), not calendar-driven (forward-looking).
7. **Factor concentration.** The 20-name universe is substantially one mega-tech/AI factor (NVDA, AMD, AVGO, SMH, XLK, QQQ, MSFT, META, AAPL, GOOGL...). `MAX_OPEN_POSITIONS=5` can be five copies of the same bet. No correlation/sector/cluster exposure layer exists.
8. **Decision-lattice complexity creep.** Floors ⊕ arming ⊕ overrides ⊕ conflicts is already a hand-built rules lattice requiring 299 tests to hold together. Each new advisory layer multiplies interaction states. It is auditable but increasingly unpredictable; it needs consolidation into "binary gates stay simple + numeric scoring does the ranking," not more special cases.
9. **Ops fragility for anything ≥ L1 autonomy.** No scheduler/daemon (every run manual), ephemeral dashboard, X auth via personal cookies (expiring, account risk), single machine, observed subprocess hang blocking a scan. Also `PAPER_EQUITY=100000` is a static sizing base — risk fraction doesn't track actual account equity.
10. **Sizing/exit naivety (acceptable now, dangerous later).** A fixed 3% stop treats SPY and TSLA as the same animal in vol terms; exits are static brackets; MFE/MAE is unpopulated so no exit research is possible. The danger is not today's simplicity — it's "improving" these later against noisy small samples without a replay framework.

---

## 4. Roadmap review

**Correctly sequenced:**
- Cost calibration before universe expansion. Yes — keep.
- Fail-safe visibility before further autonomy. Yes.
- Attribution *groundwork* before learning claims. Yes.
- Real-money unreachable until explicit readiness. Obviously yes.

**Wrongly sequenced / mis-weighted:**
- **Scheduler is listed mid-pack; it is the #1 unlock.** Cadence → data → everything downstream (calibration samples, counterfactuals, ops maturity for L1). Nothing else on the pending list compounds like this.
- **"Deeper learning loop" is listed without its prerequisite** — the counterfactual outcome tracker isn't on the roadmap at all. It is the single biggest omission in the whole plan.
- **Scanner v2 before measurement exists** would be building blind — you'd have no way to know if v2 beats v1.
- **Action-first dashboard before the scheduler** puts a UI in front of events that don't yet flow.

**Build next (strict order):**
1. Merge the open `feat/labeller-failsafe-visibility` PR (already built).
2. MFE/MAE population + candidate forward-outcome tracker (the counterfactual ledger).
3. Scheduler v1.5 (scan + monitor + outcomes-update + daily digest; kill-switch aware; cost-capped).
4. Decision lineage stamping (model/prompt/config/scanner versions + hashes on every decision row).
5. Earnings-calendar proximity flag (risk tag + warn-gate; never into the no-news eval).
6. TQS v0 in shadow (see §5).

**Defer:** tradable-universe expansion (do *shadow-ranked* expansion only — free data, zero risk); heavy playbook registry (ship a light v0); NightDesk **import** (build the cheap **export** now); full dashboard rebuild (ship a thin Action Queue tab after the scheduler).

**Do not build yet:** auto-approve of anything; regime models driving behavior; Kelly/dynamic sizing; multi-strategy portfolio allocation; options/shorts expansion; scanner-v2 live swap; NightDesk bidirectional sync.

**Missing entirely (add to roadmap):** counterfactual outcome tracker; portfolio concentration gate; earnings calendar; generalized anomaly/liveness monitors; gate-margin + approval-latency logging; equity snapshots; model/prompt version pinning + drift alarms; proposal TTL/staleness expiry.

---

## 5. Quant hedge-fund-style trading design

**Should it be playbook / signal-scoring / risk-budget / event / regime / hybrid?**
**Hybrid, layered — with a deterministic quant spine and the LLM demoted from signal-generator to evidence-synthesizer:**

```
Layer 0  Data + quality flags (bars, quotes, calendar, feeds)
Layer 1  Deterministic feature engine (momentum, structure, RVOL, ATR%, RS, trend quality)
Layer 2  Playbooks as containers (signal template + universe + gates + exit family + sizing policy + lifecycle state)
Layer 3  Evidence synthesis (LLM): catalyst typing, narrative polarity, playbook tagging, risk flagging, explanation
Layer 4  Scoring: TQS (trade quality) + EV engine  → ranking & conviction
Layer 5  Deterministic gates (binary, simple, versioned)  → tradable or not
Layer 6  Portfolio/risk-budget layer → worth capital or not (budget, correlation, exposure)
Layer 7  Execution + protection + monitoring
Layer 8  Measurement: outcomes, counterfactuals, calibration, attribution
```

Regime-awareness enters later as a *modifier* on caps/thresholds (Layer 5/6 inputs), not a rewrite. The current system is Layers 0–3 + 5 + 7 with Layer 3 doing double duty as Layer 4 — that's the thing to fix. The LLM should never be the primary alpha; it is nondeterministic, uncalibrated, and drifts. It is genuinely good at Layer 3.

**Interesting vs tradable vs worth-capital — make these three explicit, logged verdicts:**
- **Interesting** = scanner score top-N (Layer 1/4). Cheap, wide net, logged for counterfactuals.
- **Tradable** = all hard gates pass + data quality clean (Layer 5). Binary, deterministic, auditable.
- **Worth capital** = EV_net > threshold AND TQS ≥ playbook floor AND portfolio budget available (Layers 4+6). This is where ranking, cost, and book-state combine.

Today "interesting→tradable" exists; "worth capital" is implicitly "the labeller felt like proposing." That's the gap.

**Scoring layers that should exist (TQS components):**
- Signal strength: momentum/structure z-scores, RVOL, RS vs SPY/QQQ, trend quality (e.g., R² of 20d log-price fit).
- Evidence quality: catalyst status/type/recency/source-count; narrative polarity alignment × coverage.
- Tradability: spread in bps (real ones, see §11), cost-in-R, liquidity tier.
- Risk penalty: event proximity (earnings), vol regime extreme, high_risk_narrative (hard cap: TQS ≤ 60), correlation to open book.
- Conviction: LLM eval confidence — **included but flagged uncalibrated until a reliability curve proves it** (log stated confidence vs realized outcome; this is cheap and brutal).

**Missing calculations (concrete):** ATR/vol normalization anywhere; forward-return baselines per setup; EV with costs; distance-to-gate margins; slippage by time-of-day; confidence calibration; MFE/MAE distributions per playbook; net-R:R after modeled costs.

**Expected-value framework:**
`EV_R = p̂·W̄_R − (1−p̂)·L̄_R − c_R`, per (playbook × TQS bucket × regime later), with hierarchical shrinkage: `p̂ = (n·p_local + k·p_global)/(n+k)`, k≈20, so small cells collapse toward global priors instead of hallucinating edge. Log every input on every proposal so you can later test *forecast calibration* ("when we said EV=+0.3R, what did we realize?"). Until samples exist, EV runs in shadow — its first job is to be *scored*, not to decide.

**Risk-adjusted trade quality score:** TQS 0–100 = Σ wᵢ·subscoreᵢ with **weights fixed by design, not fitted** (no fitting until n is in the hundreds), hard caps from risk flags, full component breakdown logged as JSON per candidate. Shadow-only initially; its first success criterion is monotonicity (higher TQS cohorts → better forward returns).

**Sizing:** keep fixed-fractional 1% now. Evolution path: (1) vol-normalized stops (same 1% dollar risk, stop distance = k·ATR14 instead of fixed 3% — this changes share count, not risk); (2) TQS-tiered risk in [0.25%, 1.0%] once TQS is validated; (3) Kelly-style never before hundreds of samples, and probably never above half-Kelly. Sizing base must track actual account equity snapshots, not a static constant.

**Exits:** keep static brackets for validation. Log per-check excursions (MFE/MAE). Then run **exit-policy replay** offline over recorded excursion paths — breakeven-move@+1R, ATR trail, partial@1R, time-stop tightening — and only promote a policy that beats the static bracket robustly across regimes on recorded data. Never A/B live exits at n<50.

**MFE/MAE usage:** target placement (if median winner MFE ≫ target, target is too tight); stop placement (if stop sits inside the 60th percentile of adverse excursion of eventual winners, you are paying premature-stop tax); playbook health (MAE distribution drift = regime change alarm).

**Overfitting protection (structural, not aspirational):** versioned frozen parameter sets with a change-control ledger; shadow → probation → active for any behavior change; minimum-sample rules (no parameter change on n<30; no symbol-level learning below n≈100); pre-registered metrics before each experiment; a small deliberately-limited tunable surface; hierarchy-regularized learning (global → playbook → never per-symbol at this scale); and a "number of looks" log — every time you peek and adjust, record it.

---

## 6. Scanner and universe strategy

**Is the 20-name universe right for now? Yes — with eyes open.** It is liquid, cheap to trade, and operationally simple. Its two real problems: (a) it is close to a single factor (mega-tech/AI beta), so diversification within it is partly an illusion; (b) it is the most efficient corner of the market, i.e., the hardest place to find edge. Both are acceptable for a *plumbing-validation* phase and unacceptable as an *edge* thesis.

**When and how to expand:**
- **Phase A (now, zero risk):** keep trading the 20. Simultaneously **shadow-rank a 100–150 name liquid universe daily** — log features, scores, and forward returns; execute nothing. This builds the distributional data that makes Phase B a measured decision instead of a vibe, and stress-tests the scanner at scale.
- **Phase B (after ≥60 shadow sessions + counterfactual data):** expand tradable to ~50 names via explicit filters — e.g., ADV > $500M, price > $10, median spread below a bps threshold *measured from your own logged quotes*, longs-only. Expansion is a versioned, user-approved change.
- **Phase C:** catalyst/earnings-driven candidates as a **separate playbook** with its own (tighter) caps — event names behave differently and must not pollute the momentum playbook's statistics.

**Per-playbook universes: yes, eventually** — momentum wants high-beta/high-RVOL names; mean-reversion wants range-bound liquid ETFs; event playbooks want that week's calendar. The playbook container (§9) should own its universe definition + version.

**Ranking a broader universe safely:** deterministic feature ranking first (Layer 1), LLM evaluation only on the top slice (cost control + determinism), hard liquidity floor *before* anything else looks at a name, and every ranked-but-untraded name flows into the counterfactual ledger.

**Gate taxonomy:**
- **Non-negotiable safety gates (never learning-tunable):** real-money guard; manual-approval boundary per autonomy level; kill switches; daily-loss halt; data-freshness hard floor; crossed/locked-quote block; max positions / max exposure ceilings; high-risk-narrative manual-only.
- **Tunable (versioned + shadow-tested):** `MAX_SPREAD_PCT` (currently 1% — decorative; tighten to liquidity-tiered bps), `MIN_DOLLAR_VOLUME` (currently $2M — decorative), stop distance, `MIN_REWARD_RISK`, drift bps, near-extreme %.
- **Regime-aware later:** spread/vol thresholds, daily trade cap, base risk fraction.
- **Playbook-specific:** stop/target family, hold time, universe, RVOL floors, event-proximity policy.

**Shadow-testing scanner v2 vs v1:** run both on identical snapshots; log both candidate sets + ranks; compare on pre-registered metrics (forward 1/3/5-day hit-rate of top-N, downstream gate pass rate, overlap stability); require v2 ≥ v1 across ≥60 sessions including at least one ugly week; promote via user-approved versioned switch; keep v1 hot as fallback.

---

## 7. Learning loop design

**The foundational move (worth repeating): outcome-track every decision object, not just trades.** Candidates, proposals, rejects, armed-watch, overrides — each gets forward returns at 1/3/5 days (and for proposals, a counterfactual bracket replay using recorded levels against subsequent bars). Note the philosophical line this preserves: *replaying recorded decisions is post-processing, not backtesting* — de-novo historical simulation stays in NightDesk. AlphaOS only ever replays what it actually decided.

- **From trades:** attribute realized R to (playbook, TQS bucket, regime, costs); maintain shrunk expectancy tables; review MFE/MAE vs the exit actually taken.
- **From missed trades (armed-watch):** cohort forward returns of armed-watch vs traded. If the armed-watch cohort's 3-day distribution matches traded winners, the labeller floor is too conservative — output: a versioned *proposal to the user* (with evidence), never a silent loosening.
- **From rejects:** same cohort analysis stratified by rejection reason — this measures each gate's opportunity cost. A gate that never blocks a future winner is free; one that blocks many is buying safety at a measurable price. That's how gates get *tuned honestly* later.
- **From blocked actions / gate margins:** log value-vs-threshold margin on every gate evaluation, pass or fail. This enables "what if spread gate were X" analyses offline with zero behavior change.
- **From User Overrides:** stratify by reason code; compute counterfactual ΔR (§8); track per-code hit rates and the user's "action tax."
- **User vs AlphaOS judgment:** report absolute override expectancy AND ΔR vs counterfactual, per stratum, n≥20 before any narrative, bootstrap CIs in every report. Override outcomes must **never** auto-adjust gates — they produce reports, user-facing recommendations, and NightDesk research questions only.
- **Aggressiveness governor (asymmetry principle):** *autonomous changes may only reduce risk.* Tightening (suspend a playbook on a drawdown trigger, lower a cap) may be automatic; loosening (bigger size, wider gates, new playbook activation) always requires evidence thresholds (n≥30 in stratum, EV>0 after costs at ≥80% bootstrap CI) AND user approval.
- **Anti-false-learning:** sample floors; pre-registered metrics; cohort (never anecdote) analysis; shrinkage to global priors; a learning changelog where every adjustment records its evidence snapshot; and the humility to let "inconclusive" be the most common verdict for the first several months.

---

## 8. User Override and attribution

**Is it useful? Very — it is simultaneously a safety valve, a trust-building product feature, and the cleanest human-vs-machine dataset you can collect.** The side-by-side, never-overwrite design is exactly right.

**What should be recorded (mostly present; add the deltas):** full evidence snapshot at override time (already implicit via candidate linkage — make it an explicit frozen snapshot), gate margins at override time, quote at decision, decision latency (candidate seen → override placed), and later the TQS/EV that AlphaOS had computed.

**Attribution v2 — replace the binary heuristic with counterfactual ΔR:**
- `watch_to_trade`: AlphaOS path = 0R (no trade); user path = realized R. ΔR = realized R.
- `propose_to_reject`: AlphaOS path = **replayed bracket outcome** from the proposal's recorded levels (this is why the replay harness matters); user path = 0R. ΔR = −replayed R.
- Size/direction overrides where both traded: realized vs modeled path difference.
This is symmetric, honest, and mechanically computable. The current heuristic ("user wins credit only when AlphaOS wouldn't have traded") is fine as v1 but structurally under-credits AlphaOS and over-simplifies both-traded cases — keep it labeled heuristic and supersede it.

**When does user_outperformed vs alphaos_outperformed make sense?** Only as an *aggregate over ≥20 resolved overrides per stratum with a CI*, never per-trade. A single user win on an armed-watch is one draw from a fat-tailed distribution; report it as ΔR data, not as a verdict.

**Reason codes as behavioral strata (this is the hidden gem):**
- `news_just_broke` wins clustering → evidence of a user speed/information edge → candidate new playbook → NightDesk research card.
- `wants_action` / boredom-adjacent codes underperforming → surface an "action tax" metric to the user. The system should be allowed to tell its operator, kindly and with data, that impatience costs money.
- `risk_reduction` rejections that avoided losses → evidence the user reads risk the gates don't capture → mine for a new gate feature.

**High-risk narrative overrides:** keep manual-only + warned forever-ish; **never pool them** with normal overrides in any statistic (different, fat-tailed distribution); cap concurrent high-risk positions (suggest 1); auto-flag to NightDesk (already done).

**Feed-forward:** overrides → reports + hypothesis cards + user-facing recommendations. Never direct parameter mutation.

---

## 9. Playbook and hypothesis lifecycle

**States:** `draft → shadow → probation → active → suspended → retired`

| State | May propose? | May auto-approve (at L2+)? | Size | Notes |
|---|---|---|---|---|
| draft | no | no | — | idea + definition only |
| shadow | no (log-only signals) | no | — | builds counterfactual sample |
| probation | yes | **never** | min (0.25%) | concurrent cap 1–2; manual approval always |
| active | yes | per autonomy level | policy | normal operation |
| suspended | no new entries | no | — | exits still managed; auto-entry allowed for *tightening* only |
| retired | no | no | — | archived with post-mortem; exported to NightDesk |

**Versioning:** `playbook_id@semver` + config hash; every candidate/proposal row stamps the playbook version; a transitions table records who/when/why/evidence-link for every state change.

**Promotion criteria (explicit sample floors):**
- draft→shadow: definition complete (signal, universe, gates, exit family, sizing) — may be automatic *with user notification* (shadow is non-executable, so this is safe).
- shadow→probation: n≥30 shadow signals, positive counterfactual EV after costs, no data-quality flags — **user approval required**.
- probation→active: n≥20 realized trades, expectancy > 0 after costs, max drawdown within its pre-declared budget, clean reconciliation — **user approval required**.

**Demotion:** automatic suspend on any of (rolling-20 expectancy ≤ −0.3R, drawdown budget breach, upstream data-source failure, fail-safe-rate spike in its pipeline). Automatic because it reduces risk. Reactivation requires user.

**By origin:**
- **NightDesk-imported proven playbooks:** enter at **shadow**, never straight to active — live microstructure must revalidate the backtest (the reality gap is a finding, not an insult).
- **AlphaOS-created hypotheses:** draft with immediate user notification; may auto-advance to shadow; everything beyond requires approval.
- **Override-derived hypotheses:** same path, tagged `source=user_behavior`.
- **High-risk narrative setups:** own track; ceiling at probation-with-manual-approval until the user explicitly, deliberately blesses more.
- **Retired/failed:** post-mortem written, full history exported to NightDesk. Failed setups are among the most valuable research artifacts you will produce.

**What refines automatically vs needs approval:** automatic = anything that only tightens (suspension, cap reduction) + advisory recomputation (scores, stats). Approval = activation, size, universe, gate loosening, exit-policy changes.

---

## 10. Semi-autonomous → full-autonomy path

**Autonomy ladder (make it explicit and visible in the product):**

- **L0 (today):** manual scans, manual approval, paper. Everything else below is earned.
- **L1 — scheduled sensing:** cron-driven scan/monitor/outcomes + notifications + daily digest; approval still 100% manual. *Entry criteria:* scheduler stable, fail-safe visibility merged, kill switch respected by scheduled runs, cost cap per day.
- **L2 — auto-paper entries in a box:** auto-approve **paper** entries only when ALL of: active playbook (never probation), not high-risk-narrative, TQS ≥ threshold, ≤2/day, within risk budget; everything else manual. *Entry criteria:* 30+ manually-approved paper trades; 20+ sessions zero unexplained recon mismatches; kill-switch fire drill passed (including mid-position); anomaly monitors live; approval-staleness/TTL enforced.
- **L3 — live, manually approved:** real money enabled at tiny size, every entry still human-approved. *Entry criteria:* cost model calibrated (n≥20 fills) on live-representative quote data (fix the IEX gap first); 60+ paper trades with expectancy ≥ ~0 after costs *or* an explicit signed-off experimental budget; daily-loss auto-halt tested; broker-error-storm handling tested; restart-recovery drill passed (kill the process with an open position; verify bracket integrity + reconciliation on restart); written runbook.
- **L4 — live auto-entries in a box:** same box as L2 but real; exits automated; human retained for: overrides, new playbooks, size increases, gate changes, kill-switch release.

**Never automated early (arguably never):** kill-switch release; gate loosening; size-up; universe expansion; playbook activation; margin/short enablement; high-risk-narrative approval.

**Kill switches required:** global (exists) + per-playbook + per-symbol + **auto-halts**: daily-loss breach, order-reject/error storm (n errors in m minutes), data-quality collapse (staleness/anomaly), reconciliation mismatch, fail-safe-rate critical. All halts stop *new entries* and never cancel *protective exits*.

**Safety dashboards:** always-visible safety strip (mode, provider, guards, halt states, fail-safe rates, data freshness, recon status) + autonomy-level scorecard showing progress against the entry criteria above — make "earning L2" a visible, motivating artifact.

**Audit logs:** current base is strong; add halt events with cause snapshots, approval latencies, and version stamps (§13) so any incident is reconstructable end-to-end.

**Failure-mode handling to test deliberately:** OpenAI down (entries fail closed — verified by design; exits unaffected because broker-native), market data stale mid-position, partial fills, duplicate submission (idempotency), process crash with open position, cookie/auth expiry (enrichment degrades gracefully — verify it degrades *loudly* now), clock skew.

---

## 11. Execution and live-readiness review

- **Broker execution readiness:** one verified bracket round (META entry). Needed before L3: ≥20 entries including cancels, rejects, partial fills; cancel/replace behavior; OCO leg behavior after partial fill; explicit TIF documentation.
- **Order protection:** broker-native bracket is the right primitive. Add a **protection watchdog** to every monitor pass: assert "every open position has live protective orders"; alarm on violation; auto-repair allowed (repair is risk-reducing).
- **Bracket/OCO/watchdog logic:** watchdog exists for exits; extend with the protection assertion + a time-stop check vs `max_holding_days`.
- **Manual approval boundary:** correct placement (proposal→order). Add **proposal TTL** (auto-expire after ~30 RTH minutes) alongside the existing 50bps drift re-check — approvals of stale proposals should be impossible, not just risky.
- **Max daily trades / exposure:** 5/day, 5 positions, 3% daily loss exist. Add: max gross exposure % of *snapshotted* equity, per-symbol cap, correlated-cluster cap, consecutive-loss halt (e.g., 3 straight losers → pause the day).
- **Stale data handling:** two-phase checks exist and are good. Gap: monitoring cadence is manual today (scheduler fixes); add staleness alarms during open positions.
- **Spread/liquidity failures:** gates exist but thresholds are currently decorative (§6); more importantly the **quote source is IEX top-of-book** — before live, either upgrade the data plan (SIP) or explicitly document and measure the bias (compare your logged spreads vs realized fill prices; the calibration data itself can quantify the gap).
- **Cost/slippage calibration:** framework is good; push samples via scheduler cadence; segment by time-of-day and order type; run a **dual-run study** (simulated_internal vs alpaca_paper on identical proposals) to validate the simulator itself.
- **Reconciliation:** exists; schedule it; make "N consecutive clean sessions" an explicit L2/L3 gate metric.
- **Position monitor requirements at L1+:** every 5–15 min during RTH, with the protection watchdog, staleness alarm, and excursion logging (feeds MFE/MAE).
- **Live-readiness checklist (maintain as a living scorecard):**

| # | Item | Status today |
|---|---|---|
| 1 | Real-money guard test (attempt live order → blocked) | in tests ✔ |
| 2 | Manual-approval non-bypass test | in tests ✔ |
| 3 | Bracket integrity verified on real fills (n≥20) | 1/20 |
| 4 | Kill-switch drill incl. mid-position | not done |
| 5 | Auto-halts (loss/error-storm/data/recon) implemented + tested | partial (loss gate exists; halts not wired) |
| 6 | Recon clean streak ≥20 sessions | started |
| 7 | Cost model calibrated n≥20, ±bias documented | 1/20 |
| 8 | Quote-source adequacy for live (IEX gap resolved/measured) | open |
| 9 | Restart-recovery drill with open position | not done |
| 10 | Scheduler stable ≥2 weeks incl. failure alerts | not built |
| 11 | Equity snapshotting drives sizing | static constant |
| 12 | Runbook (start/stop/halt/incident) | not written |
| 13 | Earnings-calendar gate live | not built |
| 14 | Anomaly monitors (all layers) | labeller only (pending merge) |
| 15 | User sign-off ritual for L-transitions | define |

---

## 12. Missing quant modules

All start **shadow** unless marked otherwise. "Logged" always means: per-decision row + versioned config.

1. **Scanner v2 (feature ranker).** Purpose: deterministic interest ranking at scale. Inputs: 60d OHLCV, RVOL, gap %, range position, RS vs SPY/QQQ, ATR%, trend quality (R² of 20d log-price fit), extension in ATRs. Output: feature vector + composite score = Σwᵢzᵢ (weights fixed). Test: deterministic unit fixtures + shadow-vs-v1 (§6). Shadow.
2. **Trade Quality Score (TQS).** Purpose: one calibratable conviction number. Inputs: scanner features, catalyst quality, narrative alignment, tradability, risk flags, (flagged) LLM confidence. Output: 0–100 + component JSON; hard cap ≤60 when high_risk_narrative. Test: monotonicity of forward returns by TQS decile on counterfactual data. Shadow.
3. **EV engine.** Purpose: expected value in R after costs. Inputs: shrunk (p, W̄, L̄) per playbook×TQS-bucket, cost model. Output: EV_R + CI. Calc: §5. Test: forecast-calibration report (predicted vs realized by bucket). Shadow → later the "worth capital" gate.
4. **Vol-adjusted stop/target engine.** Purpose: stop reflects the instrument's noise. Calc: stop = k·ATR14 (k≈1.5–2), target = RR·stop, qty = risk$/stop. Log both fixed-% and ATR level sets per proposal; replay compare. Test: replay across vol tiers. Shadow.
5. **Cost-aware net-R:R filter.** Purpose: block trades whose *net* R:R (after modeled entry+exit costs) < floor. Inputs: cost model, levels. Protective (only tightens) → **active after 2 weeks shadow**.
6. **Regime detector v0.** Purpose: context labeling. Inputs: SPY 50/200 state, 20d realized-vol percentile (3y), universe breadth (% above 20d MA). Output: {risk_on_trend, chop, risk_off} + confidence, stamped on every scan. Test: label stability + no look-ahead. Shadow (context only).
7. **Playbook confidence score.** Purpose: promotion/demotion evidence. Calc: rolling shrunk expectancy + n + DD vs budget. Output: per-playbook stats table + dashboard. Reporting-only.
8. **Position sizing engine.** Purpose: policy-driven risk fraction. Calc: clip(base·g(TQS), 0.25%, 1.0%), equity-snapshot based. Logs suggested vs actual. Shadow until TQS validated.
9. **MFE/MAE exit optimizer (replay tool).** Purpose: evaluate exit-policy families on recorded excursions. Inputs: per-trade excursion series. Output: policy → R distribution report. Offline tool; never live-adjusts.
10. **Risk budget allocator.** Purpose: daily/weekly R budgets (e.g., 3R/day, 6R/week, realized + open risk). Blocks new entries when spent. Protective → active at L1 with user opt-in.
11. **Portfolio exposure & correlation monitor.** Purpose: stop 5×-same-bet. Inputs: open book, 60d return correlations, sector map. Output: gross/net %, max pairwise ρ vs candidate, cluster counts. Gate: warn first, then block (ρ>0.8 & cluster≥2). Warn-mode → active.
12. **Catalyst quality scorer.** Purpose: numeric catalyst strength for TQS. Calc: recency-decayed source count × type weight × confirmation. Shadow (feeds TQS).
13. **Narrative alignment scorer.** Purpose: formalize polarity into a numeric TQS input (label × confidence × coverage, high-risk caps). Mostly exists — formalize. Shadow.
14. **Approval TTL / staleness guard.** Purpose: proposals expire (~30 min RTH). Protective → **active quickly**.
15. **Anomaly/liveness monitor (generalized).** Purpose: catch the silent-failure class everywhere. Inputs: per-run rates (candidates, proposals, none_found, unavailable, fail_safe, unclear, timeouts) vs trailing baselines. Output: warn/critical in status/digest. Extends the labeller-visibility pattern you already built. Active (it's read-only).

---

## 13. Data and ledger gaps

Must-record that is missing or partial today:

- **Candidate forward outcomes** (new table): 1/3/5-day returns for every candidate/proposal/reject/armed-watch — the counterfactual ledger. *The single most valuable addition in this review.*
- **Excursions (MFE/MAE):** fields exist, unpopulated — populate from monitor passes + backfill from bars.
- **Decision lineage:** stamp `scanner_version`, `prompt_hash`, `model_id` (and provider response model string), `config_hash`, `cost_model_version` on every candidate/eval/label/proposal row. Some exists (label_version, model); systematize.
- **Gate evaluations:** per gate per decision: value, threshold, margin, pass/fail — enables offline gate tuning with zero behavior change.
- **Evidence snapshots at override time** (freeze, don't just link).
- **Missed-opportunity views:** armed-watch + reject cohorts joined to forward outcomes.
- **Data-quality events:** stale/crossed/gappy quotes, enrichment timeouts, subprocess hangs — as first-class rows, not log lines.
- **Broker errors taxonomy:** reject reasons, retry outcomes, latency.
- **Exit decisions:** which rule fired, what alternatives said at that moment (bracket vs watchdog vs time).
- **Approval latency + quote-at-approval** (staleness evidence).
- **Equity snapshots:** daily account equity → sizing base + DD tracking (replace static `PAPER_EQUITY` as the risk denominator).
- **Order idempotency keys** (duplicate-submission protection at L1+).
- **Post-trade reviews:** structured row per closed trade (thesis kept?, exit quality vs MFE, lesson tag) — even one line; it compounds.

---

## 14. Product / UI implications (action-first dashboard)

Priority order for the operator view:

1. **Action Queue** — pending approvals with: staleness countdown (TTL), drift since proposal, gate snapshot, (later) TQS + EV_R, one-click approve/reject with reason.
2. **Armed Watch / Near Action** — with `what_would_upgrade` front and center; this list is the system saying "I'm close — watch these."
3. **High-risk narrative alerts** — visually distinct, warning text, capped-permissions notice.
4. **Open trades** — protection status (watchdog assertion result), distance to stop/target in R, time-in-trade vs max hold, live uPL.
5. **Blocked/rejected today** — grouped by reason with gate margins ("blocked by spread by 2.1bps" teaches you where the dial sits).
6. **Safety strip (always visible):** mode, execution provider, real-money guard, kill switch/halts, fail-safe rates, data freshness, recon status.
7. **Overrides pending resolution** — open overrides awaiting outcome; nudges the resolve loop.
8. **Learning snapshot** — expectancy ± CI, sample counts, calibration status ("PRELIMINARY n=7/20"), action-tax metric once it exists.
9. **Live-readiness scorecard** — the §11 checklist + autonomy-ladder progress, permanently visible. Making "earn L2" a visible artifact is both motivating and disciplining.

Keep the read-only-on-render guarantee and its tests; approval remains an explicit button path.

---

## 15. 30 / 60 / 90 day roadmap

**Days 0–30 — cadence + measurement foundations.**
Build: merge visibility PR; MFE/MAE population; counterfactual outcome tracker + `outcomes_update` CLI; scheduler v1.5 (scan/monitor/outcomes/digest, kill-switch aware, daily OpenAI cost cap); lineage stamping; earnings-proximity flag (warn-tag); proposal TTL.
Why: everything downstream needs cadence and labels.
Expected outcome: 20+ scheduled sessions; 15–25 paper trades; calibration ≥10/20; counterfactual table accumulating hundreds of rows.
Risks: scheduler ops issues (mitigate: conservative failure alerts); OpenAI cost creep (cap + log).
Acceptance: zero unexplained recon mismatches; digest arrives daily; every new decision row carries version stamps; outcomes table populating automatically.

**Days 31–60 — scoring in shadow + honest analytics.**
Build: TQS v0 (shadow) + first calibration/reliability report (incl. LLM-confidence-vs-outcome curve); attribution v2 (counterfactual ΔR incl. bracket replay for rejected proposals); portfolio concentration monitor (warn mode); playbook registry v0 (states + version stamps + permission enforcement: only `active` may propose); universe shadow-ranking (100–150 names, log-only); generalized anomaly monitor.
Why: converts accumulated data into decision-quality measurement without changing behavior.
Expected outcome: ≥30 total trades; TQS deciles vs forward returns first read; first honest answer to "does eval confidence mean anything?"
Risks: temptation to act on early small-sample reads — resist structurally (sample floors in code).
Acceptance: TQS logged on 100% of candidates; attribution v2 resolves overrides with ΔR; registry blocks a non-active playbook in a test.

**Days 61–90 — decisions from evidence.**
Build: exit-policy replay study (from MFE/MAE); cost-aware net-R:R filter to active; risk budget allocator (user opt-in); NightDesk export v0 (JSONL of flagged rows/hypotheses/retired post-mortems); scanner v2 in shadow vs v1; L2 go/no-go review against the §10 criteria (documented, either way).
Why: first behavior changes that are *earned by data*, plus the autonomy decision made by scorecard rather than enthusiasm.
Expected outcome: 50–70 trades; calibration ~20/20; a written L2 decision; a defensible read on whether the momentum playbook has any post-cost pulse.
Risks: the read may be "no edge yet" — that is a *successful* outcome of a validation harness; the response is playbook iteration via shadow, not despair or gate-loosening.
Acceptance: replay report exists with a recommendation; L2 review document exists; NightDesk export produces a valid artifact.

---

## 16. PR-sized implementation plan

**Division of labor (you asked):** Sonnet 5 is the right implementer for all PRs below — they are well-scoped, test-heavy, mechanical-once-specified. **Opus 4.8 should audit** any PR touching the orchestrator decision path, gates, execution, monitor, or schema (flagged below). **Fable 5 (or Opus) should write the short design specs before implementation** for PR-7/8/9 (TQS formula, attribution ΔR semantics, concentration thresholds) and should run the L2 go/no-go review — these are judgment-heavy, cross-cutting design calls where a wrong formula silently corrupts months of data; a 1-page spec is cheap insurance. Implementation itself stays with Sonnet.

| # | Title | Goal / scope | Non-goals | Acceptance criteria | Tests | Opus audit? | Live-readiness impact |
|---|---|---|---|---|---|---|---|
| 0 | Merge `feat/labeller-failsafe-visibility` | Already built | — | merged, suite green | existing 13 | light | monitoring ✔ |
| 1 | Populate MFE/MAE | Record per-check excursions on open positions; backfill closed trades from bars | no exit behavior change | excursion fields non-null for new trades; backfill script idempotent | hermetic monitor fixtures | **yes** (monitor path) | enables exit research |
| 2 | Counterfactual outcome tracker | `candidate_outcomes` table + `outcomes_update` CLI: 1/3/5d fwd returns for candidates/proposals/rejects/armed-watch; bracket replay for proposals | no scoring, no behavior change | rows for 100% of scanned candidates after N days; replay matches hand-computed fixture | fixtures w/ synthetic bars | **yes** (schema) | measurement core |
| 3 | Scheduler v1.5 | launchd/cron: scan + monitor + outcomes + daily digest; kill-switch respected; per-day OpenAI cost cap; failure alerting | no auto-approve; no daemonized approvals | 5 consecutive clean scheduled days in paper; halt test passes; cost cap enforced | scheduler dry-run tests | **yes** (execution-adjacent) | L1 gate |
| 4 | Decision lineage stamping | config_hash, prompt_hash, model ids, scanner/cost-model versions on all decision rows | no behavior change | every new row carries stamps; hash stable across runs w/ same config | unit | no | audit ✔ |
| 5 | Earnings-proximity flag | calendar source + `earnings_within_hold_window` risk tag + warn-gate (block optional, default warn); never into no-news eval | no eval prompt change | tag correct on fixture calendar; warn surfaces in proposal + dashboard | hermetic fixture calendar | **yes** (gate) | risk gap closed |
| 6 | Proposal TTL + staleness guard | auto-expire proposals after N RTH minutes; approval of expired blocked | no drift-gate change | expired proposal cannot be approved (test) | unit + approval-path test | **yes** (approval path) | L2 prerequisite |
| 7 | TQS v0 (shadow) | compute + log score & components per candidate; **spec by Fable/Opus first** | no decision influence | logged for 100% candidates; monotonicity report generatable | golden-file scoring tests | spec-audit | ranking foundation |
| 8 | Attribution v2 (ΔR) | counterfactual ΔR incl. rejected-proposal bracket replay; report update | no auto-adjustment of anything | ΔR computed on fixtures for all action types; report shows strata + CIs | fixtures | spec-audit | learning honesty |
| 9 | Portfolio concentration monitor | corr matrix + sector map; warn on ρ>0.8/cluster≥2 at proposal time | no blocking yet (warn mode) | warning fires on synthetic correlated book | unit | **yes** | pre-expansion safety |
| 10 | Playbook registry v0 | table + states + version stamps; orchestrator enforces "only active proposes"; transitions logged + user-notified | no auto-promotion | non-active playbook cannot propose (test); transition rows written | unit + orchestrator test | **yes** | lifecycle control |
| 11 | Generalized anomaly monitor | per-run rate baselines + warn/critical in status/digest (extends PR-0 pattern) | no behavior change | synthetic rate-collapse triggers warning | unit | no | silent-failure defense |
| 12 | NightDesk export v0 | JSONL export: flagged overrides, hypotheses, retired post-mortems | no import | export validates against a documented schema | unit | no | research loop opens |

Suggested order: 0→1→2→3→4→5→6 (days 0–30), 7→8→9→10→11 (31–60), 12 + replay/report work (61–90).

---

## 17. What to bring back to Fable 5 later

1. **After 20–30 paper trades + a populated counterfactual ledger:** calibration report (modeled vs realized costs), expectancy ± CI by playbook, TQS decile-vs-forward-return table, LLM-confidence reliability curve, gate-margin/opportunity-cost analysis, recon record. → Review: does anything here have a pulse; first gate-tuning proposals; TQS v1 weights.
2. **After the first scheduler month:** ops incident log, missed/failed run stats, fail-safe + anomaly rate history, cost-per-day curve. → Review: L1 stability verdict; L2 gate progress.
3. **After the first serious drawdown** (daily-loss halt fires, or ~5R cumulative): full timeline reconstruction, which halts fired and when, user behavior during DD (override frequency!), attribution of losses (signal vs execution vs sizing). → Review: behavioral + systemic lessons; whether risk budgets and halts are correctly sized.
4. **After ≥10–20 resolved user overrides:** attribution v2 output with ΔR strata by reason code, action-tax metric, evidence snapshots. → Review: whether the user or the machine is adding decision value where, and what new playbook/gate hypotheses fall out.
5. **After the first NightDesk→AlphaOS import:** the thesis-card contract as implemented, shadow-mode results vs backtest expectations (the reality gap), promotion decision record. → Review: interface contract hardening; import cadence.
6. **Before any autonomy-level transition (L1→L2, L2→L3):** the live-readiness scorecard (§11) with every line evidenced. This one is non-negotiable — bring the scorecard, not a narrative.

---

*Standing constraints honored throughout: AlphaOS independent of NightDesk; not framed as a paper toy; no silent promotion of setups; deterministic safety gates preserved; full auditability; versioned/testable/logged changes; research–sim–paper–live separation; AlphaOS recommendation vs user decision separation; small-sample risk flagged wherever it bites.*
