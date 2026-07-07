# AlphaOS Master Build Plan — The Long Game

**Version 1.2 · 2026-07-08 · Authored by Fable 5 (final strategic architect pass)**
**Baseline: `main` @ `c3eeefb` · ~959 tests collected · PR1–PR11 + SC + UI-PR-A merged · scheduler LIVE unattended since 2026-07-06 · Phase 1 (Ignition) build-complete**

> **v1.2 (2026-07-08, the founding team's last night):** Phase 1's build scope is
> complete — PR9/9.1/9.5/10/11 shipped, plus SC (typed ScanContext; exit-review T5
> structurally closed) and UI-PR-A (the operator console v1). The phase exit gate
> still accrues on the calendar (the ≥2-week unattended streak). The 2026-07-08
> final review (Opus learning-loop audit + four-partner debate — findings and
> verdicts in the master reference §3.5/§9) **revises Phase 2's content and order**:
> see the "Phase 2 revision" block in §6. The exit-review addendum items now live in
> the specs doc under canonical names (TASK-R, CANARY, BASELINE, OPS-A/B, PORT-1,
> EVAL-1, INSTR-1, EARN-1, EXP-1, COST-1). Rationale for this bump: recording the
> phase-state change and the Phase-2 revision, per §0's own rule.
>
> **v1.1 (2026-07-06):** PR9 shipped and activated — the Ignition phase underway, L1
> entered pending its drills + 10-day streak. The team's consolidated exit review and
> the root reference document live at `docs/ALPHAOS_MASTER_REFERENCE.md` — read THAT
> first; it indexes this plan.

---

## 0. What this document is

This is the standing build plan for AlphaOS from PR9 through Year 2+. It consolidates
everything decided across the PR1–PR8 build/audit cycle and the 2026-07-05 strategic
review (AlphaOS-first doctrine, playbook adaptation, moonshot arithmetic) into one
document that outlives any single AI session.

How to use it:

- **Every new working session** reads `HANDOVER.md` first (current state), then this
  document (direction), in that order. HANDOVER answers "where are we"; this answers
  "where are we going and why."
- **Companion documents** (same directory): `alphaos-pr-implementation-specs.md` —
  machine-drawing detail for PR9–PR11, skeletons for PR12–PR15, the reusable
  spec/build/audit templates (T1–T4), and the house-patterns appendix (tribal
  knowledge, written down). `alphaos-ui-ux-design.md` — the operator-console design
  (§13 summarizes it). Build from the specs doc; strategize from this one.
- **Specs for each PR** are written fresh against the current code at build time — this
  plan fixes intent, scope boundaries, and acceptance gates, not implementation detail.
- **Nothing here overrides §1 (Prime Directives) or §8 (the Never-List).** If a future
  phase appears to conflict with them, the phase is wrong.
- Changes to this plan are commits with rationale, never silent edits. The plan is
  versioned like everything else in this system.

The ambition it serves, verbatim from the refined objective:

> AlphaOS is an AI-native trading operating system built around a non-negotiable
> moonshot target: outperform the S&P 500 and pursue at least 10% month-on-month
> growth — aggressive, adaptive, designed to push beyond conventional trading-bot
> limits while remaining survivable and auditable.

---

## 1. Prime Directives (the constitution)

Ten laws. Every PR, every phase, every autonomy promotion is subordinate to these.

1. **The target sizes the roadmap, never the trade.** The 10% MoM moonshot pressures
   what gets built next. It is never an input to position sizing, and the sizing
   formula's inputs are enumerated in code and enforced by test.
2. **Shadow-first, always.** Every new intelligence layer (score, vote, hypothesis,
   health signal) lands as a measurement-only writer to its own table, computed after
   decisions commit, read by no decision path — proven by behavior-neutrality A/B tests
   and no-read greps, exactly as TQS (PR7) and Attribution (PR8) did it. Only
   accumulated forward evidence can later promote a shadow signal toward influence.
3. **Demotion is automatic; promotion is never automatic.** Any card, signal, sleeve,
   or autonomy tier that breaches its evidence floor demotes itself and alerts. Nothing
   promotes without floors met AND explicit human acknowledgment.
4. **Pre-registration or it didn't happen.** A hypothesis freezes its success criteria
   (metric, floor, sample size, time span) *before* its forward test begins. Promotion
   reads the frozen criteria. Post-hoc analysis never promotes anything.
5. **Unknown is never zero, missing is never safe.** Established in PR2/PR5/PR7/PR8;
   applies to every future layer. Absent evidence lowers confidence or blocks — it
   never defaults to "fine."
6. **Deterministic gates own execution; AI owns opinion.** Freshness, risk, TTL, kill
   switch, protection, session, drift — these decide. Every AI output (eval, label,
   polarity, TQS, votes, hypotheses) is advisory into a gate, never around one.
7. **Everything is versioned; nothing is rewritten.** TQS_VERSION / ATTRIBUTION_VERSION
   / card versions / lineage snapshots: behavior changes are new versions with full
   provenance. Old rows keep their version forever. Append-only decision history.
8. **Agents at the edges, determinism at the core.** LLM agents are ephemeral,
   schema-forced, cost-capped workers over the structured ledger — deployed only where
   judgment adds lift (adversarial review, narrative synthesis, hypothesis generation,
   nightly research). Screening, gating, sizing, execution, reconciliation stay code.
9. **Risk classes are enumerated in code, not judged by vibes.**
   - **Class A** — parameter change within pre-declared bounds on an existing card →
     auto-testable in shadow immediately.
   - **Class B** — new card / new filter / new evidence source → paper-testable after
     human acknowledgment.
   - **Class C** — structural: sizing formula, leverage, shorts, new asset class,
     overnight event-risk policy, any gate change, autonomy tier change → NightDesk
     research + human approval, mandatory, no exceptions.
10. **The system must always be killable, and its death must be visible.** Kill switch
    honored at every job entry; dead-man heartbeat when unattended; self-halt fuses on
    repeated failure; an operator who doesn't know the system stopped — or that it's
    still running — is the real hazard.

---

## 2. Ground truth — where AlphaOS stands (2026-07-05)

What exists and is audited (PR1–PR8, all merged, 763 tests green):

- **Full paper loop:** scanner → freshness gates → AI eval + labeller (+ catalyst /
  last30days / polarity / earnings enrichment) → decision combine → armed watch →
  risk gates → manual approval → sim/Alpaca-paper execution → monitor → exits →
  protection watchdog → reconciliation → ledger.
- **Safety substrate:** real money structurally unreachable; manual approval default;
  kill switch; broker protection watchdog (detect+block only, hardened); proposal TTL
  with the `_execute()` chokepoint; multi-day GTC protection policy (META incident
  fix); additive-only migrations (SCHEMA_VERSION 3).
- **Measurement substrate:** MFE/MAE; `candidate_outcomes` counterfactual ledger with
  1/3/5-day forward windows + bracket replay (refuse-to-guess ambiguity rule);
  `trade_outcomes` net realized R; PR4 decision lineage (git/config/model/prompt
  provenance on every decision); PR7 `tqs_scores` shadow quality score (7 components,
  data-confidence-scaled, version-pinned); PR8 `attribution_records` counterfactual
  ΔR ledger (5 divergence event types, floor-gated reporting).
- **Cadence machinery (LIVE as of 2026-07-06, PR9):** Scheduler jobs
  (scan/monitor/outcomes/digest) now run unattended via two LaunchAgents
  (`com.ck.alphaos.scheduler` 300s tick; `com.ck.alphaos.heartbeat` 1800s dead-man
  check), with a per-job-type consecutive-failure self-halt fuse and ntfy failure
  alerting (`alphaos/util/alerts.py`). First unattended ticks verified same day.
  Still pending from PR9 acceptance: the 10-trading-day streak, kill-switch drill,
  failure-alert drill — and `NTFY_TOPIC` must be set or every alert silently no-ops.
- **Known deliberate gaps:** earnings provider is mock; cost model calibrated on ~1
  real fill; universe small (20 mega-caps/ETFs); no benchmark-vs-S&P tracking yet;
  no DB backup automation yet; TQS floors (≥300 live-resolved candidates over ≥8
  weeks) and attribution floors (≥30 live events/type over ≥28 days) unmet —
  **live learning data ≈ zero (1 closed trade in the production ledger). PR9 started
  the clocks; the floors now fill on their own.**

The strategic implication that shapes everything below: **AlphaOS today is a complete,
audited instrument that has barely been switched on.** Data is the bottleneck, not
intelligence. Phase 1 exists to fix exactly that.

*Update 2026-07-08:* Phase 1's build scope is done — add to the above: PR9.1
(prompt-leak hotfix), PR9.5 (backups + benchmark spine + cost true-up), PR10 (setup
cards + exit-first invariant), PR11 (daily brief + position health), SC (typed
ScanContext — the `_*` side-channel is structurally dead; ruff+mypy CI), UI-PR-A
(operator console v1: annunciator, Tonight, health cards, hindsight ΔR). The data
bottleneck diagnosis stands and now has a treatment plan: Phase 2's revision block
(§6) pulls the shadow universe forward behind honest instruments.

---

## 3. The moonshot arithmetic

10% MoM ≈ 3.14×/year. Make it an instrument, not a slogan:

```
implied monthly growth ≈ expectancy(R) × trades/month × risk-per-trade(% equity)
```

- At 0.5–1.0% risk per trade, 10%/month requires **≈ 10–20 net R per month**.
- A good swing card nets perhaps 0.2–0.4R expectancy → **30–100 trades/month** needed.
- Therefore the moonshot's real demands are: **frequency** (universe breadth, scan
  cadence, multiple holding-period classes), **parallelism** (many cards running
  simultaneously at machine discipline), and **learning velocity** (weekly promotion
  loops, not quarterly reviews). Not hero trades. Not size.

**Moonshot Gap Report** (ships in PR11, runs monthly forever): measured expectancy ×
measured frequency × current sizing → implied monthly rate vs 10%, with the binding
constraint named (edge / frequency / sizing / costs). The gap report is how the target
steers the backlog. It is the only place the 10% number is allowed to exert force.

---

## 4. Operating doctrine (the razors)

Compact restatement of the standing patterns — every future builder inherits these:

- *The target sizes the roadmap, never the trade.*
- *Shadow-first; evidence promotes; never enthusiasm.*
- *Auto-demote, manual-promote.*
- *Pre-register, then test — never the reverse.*
- *Unknown is never zero; missing is never safe; mock is never real.*
- *Agents at the edges, determinism at the core.*
- *Paper expectancy is an upper bound, not a fact* (paper fills flatter; treat every
  paper-derived number as optimistic until cost-calibrated and live-confirmed).
- *One replay engine, one truth* (all counterfactuals via `outcomes_engine`; no
  parallel replay implementations, ever).
- *Every layer is a join on stable keys* (candidate_id / proposal_id / card_id /
  lineage_id) — no orphan intelligence.
- *Tempo: machine cadence with the human as approval throttle* — never hobbyist
  weekly-report tempo.

---

## 5. The Autonomy Ladder

Explicit levels. Each promotion is Class C (NightDesk + human, with an Opus-grade
audit). Each level names its rollback trigger — rollback is automatic.

| Level | Capability | Entry criteria | Auto-rollback trigger |
|---|---|---|---|
| **L0** | Observe + measure (TQS, attribution, outcomes) | ✅ live today | — |
| **L1** | Unattended cadence: scans/monitor/outcomes/digest run on schedule; all decisions still gated | PR9 shipped ✅ (2026-07-06); heartbeat + fuses unit-tested ✅ AND live-drilled ✅ (2026-07-06, operator-confirmed — `docs/incidents/2026-07-06-pr9-acceptance-drills.md`); only the 10-day unattended streak remains (passive clock, started 2026-07-06) | ≥N consecutive job failures → self-halt |
| **L2** | Self-directed learning: nightly hypothesis generation, auto-demotion of decayed cards | PR12–13 shipped; pre-registration enforced | hypothesis volume/cost cap breach → pause engine |
| **L3** | Bounded auto-approval, **paper**: existing `MAX_AUTO_APPROVALS_PER_DAY`-capped path, live-eligible cards only | ≥8 weeks L1 data; attribution floors met; TTL/watchdog/kill-switch drills passed | any auto-approved trade violating a gate invariant → revert to manual |
| **L4** | Bounded auto-approval, **live, tier-1 size** (see §6 Phase 5) | ≥3 months profitable-after-costs L3 record; crossing protocol (§6 Phase 4) complete | drawdown governor breach → demote to paper |
| **L5** | Self-tuning within pre-registered bounds: Class A parameter moves applied automatically after forward proof | ≥6 months L4; parameter-move audit trail proven | any out-of-bounds move attempt → freeze + alert (this firing even once is an incident) |

There is no L6. Sizing formula changes, gate changes, new asset classes, and real-money
reachability itself remain human decisions forever (§8).

---

## 6. Phase plan

Phases have exit gates, not dates-as-promises. Elapsed-time guidance assumes steady
part-time building with the Sonnet/Opus protocol (§10).

### Phase 1 — IGNITION (Month 0–1) · PR9–PR11

Goal: **the machine runs itself and produces learning data every trading day.**

- **PR9 — Turn It On.** LaunchAgent wiring for scheduler (scan 3×/day RTH windows,
  monitor cadence, outcomes_update, daily digest); dead-man heartbeat + failure
  alerting (ntfy/email); max-consecutive-failure self-halt fuse; kill-switch check
  verified at every job entry. *Acceptance: 10 consecutive trading days of
  scans/outcomes/TQS/attribution rows with zero human initiation; one deliberate
  kill-switch drill; one deliberate failure-alert drill.* Starts every data clock
  (TQS 8-week, attribution 28-day, cost calibration).
- **PR10 — Setup Cards v1 + exit-first invariant.** Versioned declarative cards
  (required evidence, entry/stop/target, `invalidation_reason` as new first-class
  field, expected TQS profile, promotion state); `card_id`+`card_version` stamped on
  candidates/proposals; TQS + attribution sliced by card; "no entry without a written
  exit" as a named, tested invariant. Migrate the current implicit playbook into
  Card #1. *The spine — everything later joins on this.*
- **PR11 — Daily Brief + Portfolio Health (merged).** Per-position R / stop / target
  distance, thesis-valid check (via `invalidation_reason`), catalyst proximity,
  HOLD/EXIT/ATTENTION; rejected-candidate review (attribution-informed); one-action-
  item; **Moonshot Gap arithmetic**; renders to digest + push channel.

*Phase exit gate: unattended streak ≥ 2 weeks; cards stamped on 100% of new
candidates; brief delivered daily.*

### Phase 2 — THE LOOP CLOSES (Month 1–3) · PR12–PR15

Goal: **AlphaOS generates, tests, and promotes/demotes its own ideas — under law.**

> **Phase 2 revision (2026-07-08 final review — authoritative; the PR12–15 sketches
> below stand as intent, the revision governs order and emphasis):**
> 1. **The statistical substrate comes first.** EVAL-1 (eval harness + ground-truth
>    golden set) → PORT-1 (effective-N one-floor law, cumulative BH-FDR,
>    `preregistrations` with one-shot evaluation) → INSTR-1 (honest rel_volume +
>    ATR-scaled "R") → BASELINE (three-arm AI-vs-deterministic paired instrument) →
>    EARN-1 (real earnings provider) → **EXP-1: the shadow small/mid catalyst
>    universe, pulled forward from Phase 3** — learnable flow is the binding
>    constraint, and shadow expansion multiplies learning velocity ~10× at zero
>    decision risk, but only after the instruments it will be measured with are
>    honest. CANARY (Lane B) must be live before EXP-1 widens the tap.
> 2. **PR12 is registry-first**: v1 = pre-registration registry + resolver seeded
>    with 8 named hypotheses; the nightly LLM generator is v1.1, gated on the
>    registry proving it can resolve anything at all.
> 3. **PR13 ships demotion-first** (the per-card scoreboard + auto_floor_breach —
>    the smallest loop-closing mechanism, safe under PD#3), then promotion, then
>    **PR13.5**: the diff→version joint — *PR12 proposes diffs; PR13 toggles state;
>    only an operator-committed YAML version changes card behavior; no job ever
>    writes `cards/*.yaml`.*
> 4. Cards v2–v5 are named and sketched (specs doc): earnings-reaction drift,
>    catalyst continuation pullback, no-news gap fade, polarity-divergence reclaim.
> 5. PR15/L3 additionally gates on the CRO restore-drill law and the
>    portfolio-risk gates (Class C).

- **PR12 — Hypothesis Engine v0 + pre-registration registry.** Nightly shadow agent
  pass over the ledger → structured `hypothesis_proposals` (card-diff, risk class
  A/B/C, frozen success criteria, cited evidence). Report-only.
- **PR13 — Promotion/Demotion state machine.** Card states shadow → paper →
  live-eligible → retired; append-only `promotion_decisions`; auto-demote on breached
  rolling floors; manual promote; Class C routing to NightDesk enforced in code.
- **PR14 — Red-Team Debate v0 (shadow).** ONE adversarial bear agent, PROPOSE
  decisions only, schema-forced vote (stance, conviction, top-3 failure modes,
  invalidation triggers) → `agent_votes`; cost-capped under the PR3 budget;
  attribution-joined. Expand to bull/bear/risk triad **only if** bear-disagreement
  demonstrably discriminates outcomes (pre-registered test). Never a 12-agent
  committee.
- **PR15 — First autonomy promotion (L3).** Bounded auto-approval in paper within the
  existing audited caps, live-eligible cards only, evidence-gated per §5.

*Phase exit gate: first card demoted automatically on evidence; first pre-registered
hypothesis resolved (either way); attribution floors met on ≥2 event types.*

### Phase 3 — BREADTH + CALIBRATION (Month 3–6)

Goal: **enough surface area and measurement honesty to make expectancy numbers real.**

Workstreams (order flexible, each its own PR-sized unit):

- **Universe expansion, shadow-ranked.** From the current small universe toward
  300–500 liquid names; new symbols enter in shadow (scanned/scored/attributed, not
  tradeable) and earn tradeability per-card. Frequency is a moonshot requirement (§3).
- **Live earnings-calendar provider** behind the existing PR5 factory (zero call-site
  changes). Event-risk clearance becomes real data.
- **Regime Engine v1 (deterministic).** Populate `market_regime` from daily SPY/QQQ
  trend + realized-vol + breadth buckets — code, not AI. Cards declare the regimes
  they trade in; attribution slices by regime. This is the prerequisite for
  regime-conditional behavior later.
- **Cost-model calibration campaign.** Target ≥100 real Alpaca-paper fills; recalibrate
  slippage/commission; publish calibrated-vs-assumed drift in the digest. Until this
  lands, every expectancy number carries the "paper upper bound" caveat.
- **Execution-quality program.** Consume PR8's `execution_delta_r` at scale: entry
  tactic experiments (marketable-limit vs limit-in-zone patience) as Class A card
  parameters, judged by execution ΔR.
- **Portfolio-level risk v1.** Correlation/It concentration monitor (sector, theme,
  single-name exposure caps) as a deterministic pre-trade gate — the one genuinely new
  *gate* in this phase (Class C, full audit).
- **Intraday holding class (optional, evidence-permitting).** A second holding-period
  class only if swing-card data shows the scan cadence catches intraday-decaying
  setups; requires same-day-exit machinery already present.

*Phase exit gate: TQS floors met (≥300 live-resolved, ≥8 weeks); calibrated cost
model; ≥3 cards live-eligible with positive net expectancy after calibrated costs;
Moonshot Gap Report names the binding constraint with real numbers.*

### Phase 4 — THE REAL-MONEY CROSSING (Month 6–9)

Goal: **cross from paper to live with a protocol so boring it cannot surprise you.**

The crossing is a protocol, not an event:

1. **Preconditions (all, verified by audit):** ≥3 months unattended operation; ≥100
   resolved live-paper trades; positive net expectancy after calibrated costs; max
   peak-to-trough paper drawdown within pre-registered bound; all Opus standing audits
   green; kill-switch + protection + TTL drills re-passed on the live account config.
2. **Tier-0 live:** manual approval only; one open position max; **0.25% equity risk
   per trade**; longs only; live-eligible cards only; weekly human review mandatory.
3. **Drawdown governor (new, deterministic, pre-registered):** −5% month-to-date →
   risk halves; −8% → live trading halts, auto-demote to paper, human post-mortem
   required to resume. The governor is a gate, Class C to change, and it applies to
   every future tier.
4. **Tier growth is pre-registered:** each tier (position count, risk %, margin
   status) has entry floors (trades, months, expectancy, drawdown) written down
   *before* Tier-0 begins. No mid-flight tier invention.

Parallel workstream — **NightDesk v1 as a real service:** replay/backtest API over
historical bars for Class C questions (shorts policy, sizing ladder changes, new
asset classes), feeding written research memos into `promotion_decisions`. NightDesk
remains advisory: a weak backtest informs, only risk law blocks.

*Phase exit gate: 3 months of Tier-0 live with expectancy ≥ paper-derived floor and
zero governor breaches — or a documented retreat to paper with causes named. Both are
successful outcomes; only silence is failure.*

### Phase 5 — BOUNDED LIVE AUTONOMY + SLEEVES (Month 9–12)

Goal: **the system trades a small live book semi-autonomously, and capital allocation
itself becomes evidence-driven.**

- **L4 autonomy:** auto-approval for Tier-1 live trades (cards with the longest clean
  records, tightest caps), manual approval retained for everything else. Its own
  Opus-grade audit + drill suite before enablement.
- **Sleeve architecture.** Cards group into sleeves (e.g., catalyst-momentum,
  earnings-drift, reclaim/mean-reversion). Rolling sleeve-level expectancy drives
  **bounded** meta-allocation (pre-registered min/max per sleeve, monthly rebalance,
  every move logged + attributed). This is where "portfolio manager" behavior emerges
  — as arithmetic, not vibes.
- **Short side enablement (Class C)** if NightDesk research + live data justify it;
  shorts start at their own Tier-0 regardless of long-side tier.
- **Weekly self-audit agent.** An automated Opus-style audit pass (behavior-neutrality
  spot-checks, invariant greps, floor verification, anomaly scan over system_events)
  producing a findings report. Findings are work items, never auto-fixes.
- **Options overlay research (NightDesk only this phase):** defined-risk expressions
  (long calls/puts, spreads) for high-conviction card signals — research memo first,
  build later only with its own crossing protocol.

*Phase exit gate: live book running with L4 on Tier-1; sleeve allocation moved at
least once on evidence; self-audit agent produced ≥4 weekly reports acted upon.*

### Phase 6 — THE EDGE FACTORY (Year 2+)

Goal: **industrialize the discovery of small edges — the durable moat.**

By now the loop (hypothesize → pre-register → forward-test → attribute → promote)
is proven. Year 2 scales what goes *into* it:

- **Alternative data as enrichers.** Any new dataset (flow, borrow rates, insider
  filings, app-usage proxies, credit-card panels if ever affordable) enters through
  the established enricher pattern: provider factory → fail-safe status → shadow TQS
  component → attribution slice → earn weight. The pattern is the moat; datasets are
  interchangeable.
- **Model governance / champion-challenger.** Candidate evaluator/labeller model
  changes (new LLM versions, fine-tunes on AlphaOS's own labeled ledger, distilled
  small models for cost) run as challengers in shadow against the champion, judged by
  attribution over pre-registered windows. Model swaps become versioned promotions
  like everything else.
- **Cross-asset expansion (each Class C, each its own crossing protocol):** liquid
  ETFs/futures for regime expression; crypto only if its infrastructure (custody,
  execution, data) meets the same protection-watchdog standard — never before.
- **Chaos drills.** Scheduled synthetic-failure injection (stale data feeds, broker
  API errors, partial fills, protection mismatches) against the paper environment;
  the watchdog/fuse/governor stack must catch every injected fault. Institutional
  resilience is rehearsed, not assumed.
- **Capacity honesty.** Small-cap catalyst edges decay with size. The gap report
  gains a capacity line: estimated edge capacity per sleeve vs deployed capital.
  Scaling past capacity is refused by arithmetic, not willpower.
- **The OS as platform.** By Year 2 AlphaOS's real asset is the substrate: versioned
  strategy cards, counterfactual attribution, autonomy law, audit rails. New
  strategies are content; the OS is the factory. Guard the factory.

---

## 7. Where the edge actually comes from (honest assessment)

**vs. retail traders/investors:** retail loses to indiscipline — no written exits, no
sizing rules, no measurement, revenge trading, narrative chasing. AlphaOS beats this
*structurally* (exit-first invariant, TTL, gates, attribution) before any signal edge
exists at all. This is the near-certain win and it is already built.

**vs. institutions:** be precise about which game is winnable.

- *Unwinnable game:* speed (HFT), balance-sheet edges, flow internalization, armies of
  sector analysts. Do not compete; do not envy.
- *Winnable games:*
  1. **Capacity niches.** Small capital is an advantage: catalyst setups in small/mid
     caps with $5–50M daily liquidity are structurally closed to funds that must
     deploy hundreds of millions. This is the moonshot's natural habitat.
  2. **Learning velocity.** AlphaOS's promotion loop iterates weekly with attribution
     evidence; institutional strategy committees iterate quarterly with politics. Two
     orders of magnitude more experiments per year, each cheaper and cleaner.
  3. **AI-native breadth.** The LLM layer reads narrative/catalyst/context across
     hundreds of names nightly at near-zero marginal cost — a capability that
     replaces the coverage function of an analyst desk within its niche.
  4. **Discipline without fatigue.** The system applies every rule to every trade at
     3 a.m. exactly as at 10 a.m. Humans, including institutional humans, do not.

Parity with hedge funds is not "beat Citadel at Citadel's game" — it is *net-of-fees,
risk-adjusted returns in niches they cannot enter, with institutional-grade process at
retail-grade cost.* That target is credible. The 10% MoM moonshot lives at the far end
of it, contingent on frequency scaling (§3) and edge stacking (§6 Phase 6) compounding
without a survivability breach. Set the aim there; let the gap report tell the truth
monthly.

---

## 8. The Never-List (hard invariants, all phases, forever)

Carried forward from HANDOVER §10 and extended by the strategic review. No phase, no
autonomy level, no drawdown of patience ever overrides these:

1. Real-money reachability is flipped only by a human, out-of-band, never by the
   system or a setting the system can write.
2. The 10% target (or any performance gap) is never an input to position sizing.
   Sizing-formula inputs are enumerated and grep-tested.
3. The kill switch is honored at every job/execution entry; the protection watchdog
   remains detect+block only; the drawdown governor cannot be loosened mid-drawdown.
4. Manual approval is never removable for: `high_risk_narrative`, shorts (until their
   own protocol matures), margin, any Tier-0 crossing, any Class C change.
5. No decision path ever reads shadow tables (`tqs_scores`, `attribution_records`,
   `agent_votes`, `hypothesis_proposals`, health reports) except the explicitly
   promoted, versioned, audited signals.
6. Migrations stay additive; decision history is append-only; nothing is retro-scored
   under a new version.
7. Unknown ≠ zero; missing ≠ safe; mock ≠ real; paper expectancy = upper bound.
8. No self-modification outside pre-registered Class A bounds; an attempted
   out-of-bounds move is an incident, not a feature.
9. One replay engine. One sizing formula. One kill switch. Singletons stay singular.
10. Every autonomy promotion requires: floors met + drills passed + Opus-grade audit +
    human acknowledgment. Every demotion requires none of these — it just happens.

---

## 9. Standing audit program

**Every PR (Opus-grade, the PR5–PR8 rubric pattern):** scope control; behavior-
neutrality A/B with non-vacuity guards; no-read greps on decision paths; unknown-
never-zero probes; SQLite NULL-uniqueness probes on any new uniqueness; source-table
immutability hashes; mock/demo exclusion; fail-safe injection; lineage anchoring;
empirical adversarial probe of the PR's central claim (never testimony alone).

**Quarterly deep audits:** sizing-inputs invariant; autonomy-boundary drills (kill
switch, TTL chokepoint, governor, fuses — actually fired, not just unit-tested); cost
caps under real load; promotion-asymmetry verification (find one auto-promotion =
finding); paper-vs-live execution ΔR drift; secrets sweep across all *_json columns.

**Continuous (the Phase 5 self-audit agent, weekly):** invariant greps, floor checks,
anomaly scan, correlation check on agent votes, capacity-vs-deployment check.

---

## 10. Working protocol after Fable

The roles that built PR1–PR8, preserved as process:

- **Spec (architect pass):** ground every spec in the *current* code (read the real
  tables/functions first — never spec from memory); output the A–M format used for
  PR7/PR8: definition, scope, formulas, storage, lifecycle, missing-data policy,
  reporting, floors, implementation scope, acceptance criteria, audit checklist,
  split warning.
- **Build (Sonnet pass):** implement to spec with explicit tightenings; deterministic
  direct-construction tests (never "hope the mock scan produces it" — the flaky-test
  lesson is paid for); report back files/schema/tests/proofs in the standing format.
- **Review (parallel independent agents):** 3–5 agents per PR — formula correctness,
  behavior-neutrality/no-read, schema/idempotency, test quality (with its own
  in-process mutation testing) — findings adjudicated against real code.
- **Audit (Opus pass):** the §9 rubric, with empirical probes; verdict A–H; findings
  by severity; fixes applied and re-verified before merge.
- **Merge:** only on explicit human instruction. Always. This never automates.

Mock-data date-seeding, NULL-uniqueness partial indexes, additive migrations, the
enricher/pure-compute split, StrEnum constants, versioned formula constants — these
house patterns are documented in HANDOVER §10 and the PR7/PR8 module docstrings; new
builders follow the existing file conventions before inventing new ones.

---

## 11. Failure playbook

| Trigger | Automatic response | Human follow-up |
|---|---|---|
| Drawdown governor −5% MTD | Risk halves, alert | Review at week's end |
| Drawdown governor −8% MTD | Live halt, demote to paper | Post-mortem before resume |
| Card floor breach (rolling ΔR/expectancy) | Auto-demote to shadow, alert | Retire or re-hypothesize |
| N consecutive job failures | Scheduler self-halt, alert | Root-cause before restart |
| Missed heartbeat / no digest by T+1 | Push alert | Assume down until proven up |
| Protection incident (unprotected/mismatch/unverifiable) | New entries blocked (existing behavior) | `protection_resolve`/`ack` only |
| Out-of-bounds self-modification attempt | Freeze engine, alert, log as incident | Full audit before re-enable |
| Cost cap breach (AI calls/tokens) | Skip further AI jobs that window (existing) | Rebalance budget or scope |
| Broker/data outage | Freshness gates already block; fuse halts repeats | Verify reconcile on recovery |
| Edge decay across a whole sleeve | Sleeve allocation floors to minimum | NightDesk regime study |

The playbook's principle: **every failure mode has a pre-written first move that the
system takes alone, and a second move that only a human takes.**

---

## 12. Phase gate scorecard

The plan's honesty mechanism — each phase is *done* when its numbers exist, not when
its code merges:

| Phase | The number that matters |
|---|---|
| 1 · Ignition | ≥10 consecutive unattended trading days; 100% card-stamped candidates |
| 2 · Loop closes | First auto-demotion on evidence; first pre-registered hypothesis resolved; attribution floors met (≥2 types) |
| 3 · Breadth | TQS floors met (300/8wk); cost model calibrated (≥100 fills); ≥3 live-eligible cards, positive net expectancy |
| 4 · Crossing | 3 months Tier-0 live, expectancy ≥ paper floor, zero governor breaches |
| 5 · Autonomy | L4 running on Tier-1; ≥1 evidence-driven sleeve reallocation; 4+ self-audit cycles acted on |
| 6 · Edge factory | ≥2 new evidence sources earning TQS weight; ≥1 champion-challenger model promotion; capacity line in every gap report |

And over all of it, one line renewed monthly by the Moonshot Gap Report:

```
measured expectancy × measured frequency × sized risk  vs  10% MoM
— and the name of the binding constraint.
```

That line is the moonshot, operationalized. Chase the constraint it names; never the
number itself.

---

## 13. UI/UX — the operator console

Full design in `alphaos-ui-ux-design.md` (philosophy, information architecture, all
screens with written wireframes, evolution path, first-build order, avoid-list, and
a Google Stitch mockup prompt). The load-bearing decisions, so no future builder
has to rediscover them:

- **The user is the accountable supervisor of a machine that trades** — not a
  chart-watcher. Every screen must answer in ≤5s: what is it doing, why, what does
  it need from me, how do I stop it.
- **Annunciator principle:** a permanent status strip (mode PAPER/LIVE · autonomy
  level · kill switch + one-click engage · governor · heartbeat age · open R ·
  pending approvals) on every screen. Mode confusion is the deadliest UX failure in
  supervised autonomy.
- **Asymmetric friction:** viewing instant · approving deliberate (confirm restating
  max loss) · risk/autonomy increases heavy (typed confirm, Class C link) ·
  **stopping is always the easiest action in the UI.** The real-money lock has no
  unlock affordance, by design.
- **Evidence-state honesty in pixels:** scores never appear without confidence;
  aggregates never without n-vs-floor; mock/paper never styled as live
  ("paper — upper bound" watermark). The reporting law extends to the UI.
- **Progressive disclosure ladder** for all machine reasoning: verdict → one-line
  why → evidence table → narrative → raw provenance. Hindsight (counterfactual ΔR
  on rejects/blocks/expiries) is a neutral learning surface, never a FOMO alarm.
- **Nine screens, three planes:** Operate (Tonight · Approvals · Positions),
  Understand (Decisions · Learning · Cards), Govern (Autonomy & Risk · System &
  Audit · Brief Archive). "Tonight" is the home: the one action, needs-you, open
  risk, machine activity, brief, moonshot gap line.
- **Build order:** annunciator strip + Tonight tab + position health cards +
  approvals TTL/exit-plan upgrades + decisions funnel — all on the existing
  Streamlit substrate (**UI-PR-A — ✅ shipped 2026-07-08, all five items; next UI
  work is the Learning/Cards tabs once PR12/PR13 produce their data, plus OPS-A's
  loopback-bind guard immediately**). Stay on Streamlit until it demonstrably
  hurts; the IA is substrate-independent; ntfy push is the mobile app until the
  crossing justifies more. The UI can never do what the CLI cannot — same
  orchestrator methods, same gates, forever.

---

*Fable 5, 2026-07-05. The foundation is sound, the law is written, the machine is one
PR away from running. Turn it on, let the evidence accumulate, promote nothing without
proof, and let the target size the roadmap — never the trade.*

*Addendum, 2026-07-06: it is on. The scheduler ticks unattended; the data clocks are
running. From here the constraint is patience and process, not code. — Fable 5*
