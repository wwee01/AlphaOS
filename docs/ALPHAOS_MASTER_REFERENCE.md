# ALPHAOS MASTER REFERENCE — The Founding Team's Handoff

**Version 1.6 · 2026-07-09 · The retiring team: founder/architect (Fable 5), quant
researcher, senior software engineer, ML engineer, head quant trader, infra/DevOps,
chief risk officer.** *(v1.6, 2026-07-09 overnight autonomous session continued:
EVAL-1 struck as merged in §5 (item 14); §9 gains the labeller-vs-evaluator scope
decision + the per-item-isolation-must-wrap-the-whole-chain lesson. v1.5, 2026-07-09:
TEXT-0 struck as merged in §5 (item 13c); §9 gains the TEXT_ARCHIVE_ENABLED-default
+ once-daily-lock-key decision rows. v1.4, 2026-07-09: REG-1 struck
as merged in §5 (item 13b); §9 gains the stale-regime-refusal + REGIME_ENABLED-default
decision rows. v1.3, 2026-07-09: OPS-A and EXP-0 struck as merged in §5
(items 13/13a); §9 gains the EXP-0 override-path guard decision row. v1.2, 2026-07-08
late: the operator+Fable regime/text-archive specs reconciled into Lane A — REG-1 at
13b, TEXT-0 at 13c, UNIV-1 superseded (salvage into EXP-0; UNIV-D later), EXP-1's own
classifier deleted, item-20 "Regime Engine v1" renamed REG-2; §9 decision rows added.
v1.1, the team's last night: T5 resolved; §3.5
final-review record added — learning-loop audit + partners' verdicts; §5 punch list
reordered to the two-lane ruling incl. EXP-0; §7 loop-closure law; §9 decision rows.
v1.0: 2026-07-06.)*

This is the root document of AlphaOS. If you read one file before touching anything —
human or AI, this year or in five — read this one. It consolidates what we built, what
we found when we audited ourselves on the way out, what we decided and why, and what
you must do next. Everything here was verified against the running system on
2026-07-06, not recalled from memory.

---

## 0. Document authority hierarchy

When documents disagree, the LOWER number wins. When any document disagrees with the
code and its tests, the code is the truth and the document has a bug — fix the doc.

1. **This file** — mission, law pointers, the exit review, the operating knowledge.
2. **`HANDOVER.md`** (repo root) — *current state*, refreshed every working session.
   Answers "where are we right now." Always read it second.
3. **`docs/roadmap/alphaos-master-build-plan.md`** — strategy: Prime Directives,
   autonomy ladder, six phases, Never-List, failure playbook. Answers "where are we
   going and why."
4. **`docs/roadmap/alphaos-pr-implementation-specs.md`** — machine drawings: full
   specs for the next PRs, T1–T4 process templates, the house-patterns appendix (§H —
   the tribal knowledge, paid for in bugs).
5. **`docs/roadmap/alphaos-ui-ux-design.md`** — operator-console design.
6. `docs/incidents/` — dated incident and drill records. Append-only.

**Maintenance protocol for this file:** update it at every phase gate, at every
autonomy-level change, whenever a §3 finding is resolved (mark it, don't delete it),
and at least quarterly. Changes are commits with rationale. If this file is more than
a quarter stale, treat its state claims as suspect and re-verify before relying on them.

---

## 1. The mission, and its honest physics

**North star:** beat the S&P 500 consistently; pursue ≥10% month-on-month growth. A
self-learning system that improves itself continuously — survivable and auditable
above all.

The founding team's last obligation is to hand you the arithmetic without romance:

- 10% MoM ≈ 3.14×/year. `monthly growth ≈ expectancy(R) × trades/month × risk-per-trade`.
  At 0.5–1% risk and a good swing card's 0.2–0.4R net expectancy, you need **25–100
  trades/month**.
- **The configuration we are handing you cannot produce that.** Verified 2026-07-06:
  20-symbol megacap universe × 3 scan windows × 5-position cap × ~5-day holds gives a
  hard ceiling of ~21 trades/month at *perfect* operation, realistically 4–8 once
  manual approval and 30-minute TTLs bite. That is 1–6% MoM **even if** paper
  expectancy survives cost calibration — and our trader's honest prior is that the
  current signal (no-news momentum continuation on AAPL/SPY-class names) is
  zero-to-negative after costs, because short-horizon megacap behavior is
  reversal-dominated and maximally arbitraged. At 4–8 trades/month, statistical
  significance takes ~2 years: **the current book cannot even validate its own
  hypothesis on a useful timeline.**
- Therefore: the megacap book is the **control group**, never the fund. The fund is
  what Phase 3 builds — breadth (hundreds of shadow-ranked names), the small/mid-cap
  catalyst niche institutions can't enter, more holding-period classes, machine
  cadence. The moonshot is reached through **frequency and edge-stacking**, never
  through sizing. Prime Directive #1 stands forever: *the target sizes the roadmap,
  never the trade.*
- Expectation ladder, in order, each gated on evidence: (1) survive and measure
  honestly → (2) positive net expectancy after calibrated costs → (3) beat SPX
  after costs → (4) 2–5% MoM in capacity niches → (5) 10% MoM as the stretch state
  when frequency × edge compounds. Do not let anyone — including the operator on a
  good week — skip rungs.
- **"Beat the S&P" is currently unmeasurable** — the system records no equity curve
  and no benchmark series (§3, researcher CRITICAL). Fixing that is in the punch
  list's first week, because the first months of data are irreplaceable.

---

## 2. The system as built (verified 2026-07-06)

Fifteen days old (first commit 2026-06-21). One pipeline, paper-only, on a Mac mini,
Python 3.12, stdlib-first, SQLite source of truth (`data/alphaos.db`, WAL), 100%
offline-testable. ~804 tests, CI on GitHub Actions (py3.11/3.12).

**Pipeline:** Scheduler (LaunchAgent tick 300s → `JobRunner.run_due_jobs()`) → Scanner
(`alphaos/scanner/`, 20-symbol `DEFAULT_UNIVERSE`, interest scoring) → Candidate
Packet (whitelisted evidence, `candidate_packets.packet_json`) → AI Labeller +
enrichment (catalyst / last30days / polarity / earnings-proximity — all advisory) →
decision combine + Armed Watch (`orchestrator._resolve_decision`) → deterministic
gates (see §4 table) → manual approval (`approval.py`) → sim/Alpaca-paper execution
through the single `_execute()` chokepoint → monitor/reconcile/exits
(`position_manager`, `exit_rules`) → protection watchdog (detect+block only) →
journal (append-only) → counterfactual outcomes (`alphaos/learning/`) → TQS v0 shadow
score (`alphaos/tqs/`) → Attribution v2 ΔR ledger (`alphaos/attribution/`) → daily
digest.

**PR ledger:** PR1–2.8 (skeleton, execution, watchdog + hardening, measurement
foundation, enrichment chain, armed watch, user override — GitHub PRs #13–25) · PR3
Scheduler cadence · PR4 Decision lineage · PR5 Earnings proximity · PR6 Proposal TTL
(the one hard-gate PR) · PR7 TQS v0 · PR8 Attribution v2 · PR9 Turn It On (unattended
LaunchAgents + fuse + heartbeat + alerts; **activated 2026-07-06**) · PR9.1 prompt-leak
hotfix · PR9.5 Ops & Measurement (backups, benchmark spine, cost true-up) · PR10 Setup
Cards v1 + exit-first invariant · PR11 Daily Brief + Portfolio Health · SC ScanContext
structural refactor (typed context, `_*` side-channel dead, ruff+mypy CI; 2026-07-08)
· UI-PR-A Operator Console v1 (annunciator strip, Tonight tab, position health cards,
hindsight ΔR column; 2026-07-08 — the dashboard now leads with Tonight/Positions and
the permanent annunciator; kill switch moved from sidebar to strip). Every PR since
PR3: spec → build → independent review agents → Opus audit → explicit-instruction
merge (T4). Suite ~959 collected as of 2026-07-08.

**Data assets at handoff — the number that matters:** 7 real candidates, 7 real
evaluations, **1 closed trade (−1.35R, the META incident trade)**, 1 attribution
event, 0 TQS-scored scans, 197 system events. The instrument is complete; the data is
approximately zero. PR9 turned the tap on **today**. Protect the tap (§5) before
adding intelligence.

**Live config truth** (`.env` overrides code defaults — always check it): paper mode,
`alpaca_paper` execution, manual approval, real trading unreachable, last30days +
polarity ON (each real scan costs OpenAI calls), `gpt-5.4-mini` primary+review,
NTFY_TOPIC **now set** (operator's own topic, subscribed 2026-07-06 — see §3),
MAX_PAPER_TRADES_PER_DAY **1000000, deliberate** (operator's own inline `.env`
comment: "removed per operator request" — NOT a bug; confirmed with the operator
2026-07-06 after an earlier draft of this review wrongly called it drift).

---

## 3. The exit review (2026-07-06) — what six specialists found

Full agent reports are preserved in this session's transcript; the durable findings
are here. **Every item was verified against code or the live journal unless marked
(read).** Status keys: 🔴 open · 🟡 fix-in-flight · ✅ resolved (date).

### Convergent themes — what multiple lenses found independently

**T1 · The system cannot page a human. (infra CRITICAL + CRO CRITICAL)** 🔴
`NTFY_TOPIC` is empty, so job-failure alerts, fuse alerts, and the dead-man heartbeat
all silently no-op (`alphaos/util/alerts.py` returns False without logging on unset
topic; launchd ignores `scheduler_health`'s exit 1). The expected failure mode of the
unattended system is a **silent halt**. The daily digest's delivery channel is also
ntfy. *Fix: operator sets a topic + subscribes their phone (user-only input), then
run the drill suite. One line of config activates every alert path already built.*

**T2 · The ledger dies with one disk.** ✅ **Backup automation shipped 2026-07-06**
(PR9.5): `deploy/backup_ledger.sh` + `com.ck.alphaos.backup` LaunchAgent (05:30 SGT
daily) — SQLite's own `.backup` API (WAL-safe, never `cp`) → `PRAGMA
integrity_check` gate (never rotates in a bad backup) → gzip → copy to iCloud
Drive (`~/Library/Mobile Documents/com~apple~CloudDocs/AlphaOS-backups/`) →
rotate 30 daily / 12 monthly (monthly slot self-heals if the 1st is missed) →
any failure alerts priority=high. 10 tests (integrity, restore-roundtrip,
rotation math, corrupt-DB rejection, missing-source rejection). `pmset
autorestart` ✅ already fixed (§2). **Residual, honestly stated**: this protects
against logical failure (corruption, accidental deletion, a bad migration) —
NOT physical disk failure, since the backup still lives on the same physical
Mac mini (iCloud Drive sync gives genuine off-machine redundancy IF it's
working, but that wasn't independently verified end-to-end for a real
LaunchAgent — do the quarterly restore drill, and see the Week-2 punch-list
item for a true second physical layer, e.g. Time Machine to an external
drive).

**T3 · The strategy is aimed where the signal can't win, at a frequency that can't
learn. (trader CRITICAL + researcher CRITICAL)** 🔴 See §1. Additionally the
plumbing is megacap-calibrated: `rel_volume` = today-cumulative ÷ yesterday-full-day
(no time-of-day normalization — reads 0.1–0.3 every morning, so the volume trigger is
effectively dead and proposes require ~5% moves); the 1bps slippage model and $2M
liquidity floor are fantasy in the $5–50M-ADV niche the master plan §7 names as the
winnable habitat. *Fix path (after PR10/11): 20-day time-of-day-normalized relative
volume; ATR-scaled stops (a fixed 3% stop is ~4 daily sigma on SPY, <1 on TSLA —
"1R" currently means different trades); real earnings provider as a HARD gate for
the niche book; shadow small/mid universe behind the existing gates. Keep the megacap
book running as the control it is.*

**T4 · "Beat the S&P" is unmeasurable, and the clock is running. (researcher
CRITICAL)** 🔴 No daily equity snapshot, no SPY total-return series, no relative
return / rolling beta, no per-trade benchmark-adjusted R — while 8 of 20 universe
symbols are index ETFs, so returns will be beta-dominated and benchmark adjustment
*is* the question. *Fix: the benchmark spine, week 1 (PR9.5) — contemporaneous data
cannot be reconstructed later.*

**T5 · The `cand["_*"]` side-channel seam is a defect factory. (ML CRITICAL + SWE
HIGH, found independently)** ✅ **RESOLVED in full 2026-07-08** — the hotfix (PR9.1,
prompt-strip + regression tests) closed the leak 2026-07-06; the structural fix (SC,
merged `5e39f6f`) replaced the side-channel with a typed `ScanContext` whose
`__setitem__` refuses `_`-prefixed keys — the bug class is now structurally
impossible, with the prompt-builder strip retained as defense in depth. Original
finding preserved below for lineage: 🟡 The scanner/orchestrator stash private objects
(`_snapshot`, `_interest`, `_catalyst`, `_last30`, `_polarity`, `_earnings`) on the
candidate dict; `build_no_news_user_prompt` does `json.dumps(candidate)` — **so the
"no-news" eval prompt has been leaking catalyst/narrative text into the model while
instructing it that no news exists** (reproduced by execution; founder re-verified at
all four code sites). The July-1 baseline data is contaminated and cannot be
relabeled. Mock mode never builds prompts, so no test caught it. *Fix tonight (PR9.1
hotfix, before the first unattended scan): strip `_`-prefixed keys at prompt
construction + a live-prompt-composition regression test. Fix structurally (before
PR12): replace the side-channel with a typed `ScanContext` — the SWE's #1
recommendation; every next PR presses on this exact seam.*

### Remaining findings by lens (condensed)

**Quant researcher** — measurement engines verified SOUND (decision-time anchoring,
refuse-to-guess ambiguity handling, one-replay-engine, floor/caveat discipline; no
look-ahead found; 122 core tests + own probes). Open: 🔴 HIGH floors count rows, not
independent observations — same-symbol/overlapping-window/correlated-universe rows
inflate effective N; no FDR/holdout discipline planned for future card/regime slicing
(false edges near-guaranteed at n≈30 floors). 🔴 MED attribution aggregates condition
on bracket-touch resolution (overweights high-vol paths; caveat text doesn't say so).
🔴 MED candidate-level `max_favorable_*_r` isn't 0-anchored like trade-level MFE
(probe: "max favorable" = −0.6) — silent inconsistency for future comparisons. 🔴 MED
gross (replay) vs net (realized) asymmetry across attribution types — labeled, never
mixed, but cross-type narratives will overstate rejection costs. LOW: same-evening
blind window on 15:45 decisions; TQS confidence-scaling ≡ imputing 0 for missing
components (tension with unknown≠zero; visible, not silent).

**Software engineer** — no CRITICALs; institutional memory in code verified
excellent (rationale-dense comments with incident lineage; invariants enforced
structurally: chokepoint, partial unique indexes, import-graph test, additive
migrations). Open: 🟡 HIGH **suite is date-flaky and red on today's seed** — 2
`test_decision_override.py` tests assert on the organic mock scan (third occurrence
of the banned §H.1 class; fix in PR9.1). 🔴 HIGH scan-pipeline accretion (see T5).
🔴 MED enum drift (raw status strings at ~10 sites; `"AUTO_APPROVED"` hardcoded);
`config_versions.config_hash` uses builtin `hash()` (PYTHONHASHSEED-randomized —
one-line fix to `stable_hash`); no ruff/mypy gate in CI; per-row autocommit leaves
benign partial state on crash (no janitor for stuck `scan_batches`). LOW: dead line
`orchestrator.py:158`; JSONL mirror path is CWD-relative; PR2.6 watchdog cosmetics
still open.

**ML engineer** — output-handling discipline verified better than most production
LLM systems (whitelist coercion, downgrade-only authority, deterministic arming,
named failsafe reasons with health grading). The prompt→completion→outcome triple is
joinable by design — the training-data substrate exists. Open: 🟡 CRITICAL prompt
leak (T5). 🔴 HIGH zero production lineage rows — the only real scan (07-01)
predates PR4; nothing real has ever been stamped. 🔴 HIGH failed/truncated
completions are discarded (`raw={"fail_safe": reason}`) — precisely the examples an
eval harness needs most. 🔴 MED cost cap undercounts 2–3× (counts `openai_evaluations`
only; labeller + polarity calls invisible; no token/dollar accounting). **The
self-learning verdict:** no eval harness exists — a prompt change today ships blind.
Data accumulates at ~20 label-outcome pairs/day → eval corpus in 1–3 months,
fine-tune scale 6+ months, trade-level reward signal in years. Right order: **eval
harness → prompt iteration → few-shot from own ledger → distillation (not
cost-justified for years at ~$5–15/month spend) → PR13 promotion loops.** Fine-tuning
talk before ~5k clean triples is premature; PR12–15 *is* the self-learning system.

**Quant trader** — safety plumbing rated institutional-grade; audit/lineage
discipline "you can actually do attribution on this book"; honest epistemics. Open:
🔴 CRIT/HIGH universe + rel_volume + earnings (T3). 🔴 HIGH **no portfolio-level
risk** (verified by reproduction): 1% risk ÷ 3% stop = 33% notional/position, 5
concurrent = 166% gross on $100k — "no leverage" is per-position only; daily-loss
gate counts *realized* only (five positions −1% underwater gate nothing); no
sector/correlation caps on a universe that is one beta cluster (QQQ/XLK/SMH/NVDA/AVGO
≈ one trade); **sizing uses the static `paper_equity=100k` constant — the
orchestrator never passes actual account equity.** 🔴 MED calendar-second time stops
(weekends eat horizon); time-expiry exits classified by P&L sign (muddies
attribution); sim fills at last price (no half-spread); relvol≥3 + 0% change scores
exactly the 0.40 propose bar → OPEX-day index-ETF trap. LOW: `trend_quality` is
|change|×10 relabeled; shorts margin-gated → book long-only in practice; no
breakeven-after-1R stop management.

**Infra/DevOps** — launchd architecture verified correct (dumb tick, disjoint
heartbeat failure domain, plists lint clean, deploy/ ≡ installed); git secrets
hygiene verified clean history-wide. Open: T1, T2, plus 🔴 HIGH irreproducible venv
(only pytest/tzdata pinned; openai/alpaca-py/streamlit/numpy live in the venv
unpinned — commit a `pip freeze` lockfile). 🔴 MED fragile path chain (plists
hardcode the spaced repo path + uv-symlinked python; repo move or `uv python` GC =
silent 300s exec failures); LaunchAgents need an Aqua session — safe only because
auto-login=ck + FileVault off: **enabling FileVault or logging out stops the fund**
(document, don't "fix"). 🔴 MED `.env` mode 644 with all keys (`chmod 600`). LOW:
logs in `/tmp` (purged on reboot/periodically) → move to `~/Library/Logs/alphaos/`;
installer doesn't validate the python path; `data/demo-*.db` clutter.

**Chief Risk Officer** — 243 tests across 20 safety files run green; **real money
unreachable via 4 independent locks** (settings startup check · order_manager guard ·
alpaca_client refusal · safety.assert_paper_or_mock); the full gate inventory below
verified at file:line. Open: T1/T2, 🔴 HIGH drills never run (0 kill-switch events in
the journal, ever). ~~`MAX_PAPER_TRADES_PER_DAY=1000000` config drift~~ — RETRACTED
2026-07-06: this was flagged as drift without reading the `.env` comment directly
above it ("removed per operator request") — it is a deliberate operator choice, not
a bug. Left standing as a lesson: even an empirically-minded review can miss
recorded intent it didn't think to look for; see the memory file
`feedback_check_env_comments_before_correcting.md`. MED: kill-switch engage
doesn't cancel existing broker GTC legs (flatten stays manual — acceptable,
documented here); fused monitor pauses detection (residual ≈0 today: broker-side
brackets hold). **Not built, risk-ranked for Phase 4:** drawdown governor (nothing
tracks MTD drawdown) → gap-aware sizing (5 positions gapping 10% ≈ −8% vs the 3%
realized-only check) → concentration caps → regime filter → real earnings calendar →
monthly loss accounting.

**Worst case tonight, bounded (CRO, verified):** trades without a human: **0**
(manual approval double-locked; operator absence → proposals TTL-expire benignly).
Real-money orders: **0** (4 locks). Proposal spam: ≤~60 rows/day, all self-expiring.
OpenAI: ≤~180 calls/day ≈ $1–3, hard-capped 2000/30d. **Residual worst case is
operational, not financial: a silent outage plus un-backed-up ledger loss — exactly
T1 + T2.**

### The gate inventory (CRO-verified, keep current)

| Gate | Enforced at |
|---|---|
| Freshness (session-aware age) | `data/freshness_guard.py:assess`; re-checked `orchestrator.py:616` |
| Market session (closed ⇒ no entry) | `freshness_guard.py:119`; closed-session TTL=0 `orchestrator.py:488` |
| Price drift (50bps re-check at approval) | `freshness_guard.py:155`; `orchestrator.py:626` |
| Crossed/invalid quote | `risk/risk_engine.py:165` |
| Spread ≤1% / dollar-liquidity floor | `risk_engine.py:173` / `:181` |
| Stop sanity + risk sizing + no-leverage notional cap | `risk_engine.py:102/:61/:67` |
| Target sanity (min R:R) | `ai/openai_client.py:131` |
| Max open positions / daily trade cap / daily loss | `risk_engine.py:136/:143/:152` |
| Margin/short explicit approval | `risk_engine.py:191`; `order_manager.py:97`; `orchestrator.py:600` |
| Kill switch (every job entry + execution) | `safety.py:58`; `order_manager.py:83`; `orchestrator.py:592`; `scheduler/jobs.py:34` |
| Proposal TTL | `orchestrator.py:574` (approve) + `:1256` (`_execute()` backstop) |
| Protection-incident block (watchdog detect+block only) | `protection_watchdog.py:395` → `order_manager.py:89/:105` |
| Manual approval non-bypass (auto capped 1/day) | `approval.py:52/:68` |
| AI cost cap | `scheduler/cost_guard.py:29` → `jobs.py:42` |
| Scheduler self-halt fuse + dead-man heartbeat (PR9) | `cadence.is_fused` → `job_runner.run_due_jobs`; `job_runner.heartbeat_check` |

---

## 3.5 The final review (2026-07-08) — the learning-loop audit + the partners' debate

The founding team's last working night. Two instruments ran in parallel — an
adversarial Opus audit of the self-learning loop as designed (PR12–15 skeletons + the
exit-review addendum), and a four-partner strategy debate (PM/quant/trader/CRO) on
where the alpha actually comes from. Every finding is integrated into the specs doc
(v1.1) with a paste-ready fix; the durable headlines live here. Status keys as §3.

**Audit verdict, one sentence:** *the measurement rails are trustworthy, but the loop
as previously drawn could not legally change a card's behavior from evidence, and
would have promoted on noise if it could.* Both joints are now specified closed.

**The three CRITICALs (all now specified into PORT-1/PR12/PR13/PR13.5):**
- 🔴→spec'd **A1 — floors count rows, not independent observations.** Every floor
  gates on `len(rows)` (`reports/attribution.py:212–231`) over a one-beta-cluster
  universe where 30 rows ≈ 3–5 independent bets. Fix: ONE shared `effective_n()`
  (symbol-day dedup + overlapping-window clustering) consumed identically by reports
  AND the PR13 promotion gate — the one-floor law. PORT-1, hard prereq of PR12.
- 🔴→spec'd **A2 — optional stopping.** A rolling nightly floor check crosses any
  threshold eventually. Fix: one-shot evaluation (`evaluated_at_utc` set exactly
  once, at/after `analysis_not_before`); demotion stays rolling (safe direction) but
  requires ≥2 consecutive breached windows.
- 🔴→spec'd **B1 — the loop didn't close.** PR12 proposes diffs, PR13 toggles state,
  but nothing turned a promoted diff into a new card VERSION — no spec said who
  writes `catalyst_momentum_v2.yaml`. Fix: PR13.5 — **PR12 proposes diffs; PR13
  toggles state; only an operator-committed YAML version changes card behavior; no
  job ever writes `cards/*.yaml`.**

**HIGHs, condensed (fixes in specs doc):** A3 naive paired-ΔR CIs are 2–4× too
narrow under day-clustering → day-block bootstrap / effective-N SE (BASELINE §5) ·
A4 survivorship in card retirement → required `preregistration_id` FK + full-family
denominator in any system-edge claim · A5 BH-FDR family defined as the cumulative
evaluated preregistrations, never per-render · B3 demoted card versions are terminal
(`STALE_DATA_REUSE` guard) · C1 replay_r idealizes fills, zero gap risk → COST-1 gap
haircut, "gross, gap-free upper bound" caveat until then · C2 the shadow baseline's
control arm is confounded by interest_score → three-arm design (AI / threshold /
propose-all), honest conditional claim · D1 no label ground truth → operator-
adjudicated golden labels in EVAL-1 · D2 nothing owned execution-cost calibration →
COST-1, gates expectancy-ladder rung 2. **MEDs:** regime tag v0 (EXP-1) · LLM never
authors its own test rigor (PR12) · candidate max-favorable anchoring (PORT-1
ride-along) · 🔴 **C4 — live reporting-law violation: the daily brief renders
per-event ΔR with no floor/caveat** (`daily_brief.py:121–139`) → BRIEF-FIX-1, small,
Lane B · per-card capacity fields (COST-1 ride-along) · ΔR must segment by
model/prompt hash and refuse aggregation across a canary Tier-1 boundary (CANARY §7).

**The partners' verdicts (full debate in the session record; decision rows in §9):**
- **V1 — highest-leverage 90-day change:** pull the shadow small/mid catalyst
  universe forward from Phase 3 to pre-PR12 (EXP-1), shipped only behind the
  honest-instruments stack (EVAL-1, PORT-1, INSTR-1, EARN-1, CANARY live) — because
  learnable trade flow is the binding constraint, but archiving niche data through
  megacap-calibrated, mis-ranked instruments would be irreversible contamination
  (T5's lesson at universe scale). *PM dissent: would ship behind rel_volume+PORT-1
  only.*
- **V2 — PR12 inverted to registry-first:** v1 = preregistration registry + resolver
  seeded with 8 named hypotheses (H-TQS-1 · H-CAT-1 · H-INT-1 · H-WIN-1 · H-TTL-1 ·
  H-REJ-1 · H-POL-1 · H-AI-1); the LLM generator is v1.1, gated on the registry
  resolving anything at all.
- **V3 — cards v2–v5 named:** `earnings_reaction_drift_v1` ·
  `catalyst_continuation_pullback_v1` · `no_news_gap_fade_long_v1` ·
  `polarity_divergence_reclaim_v1` — sketches in the specs doc; all Class B, born in
  shadow, exit-first, ATR stops.
- **V4 — the weakest link is the actuator:** measured facts terminate in the brief's
  prose; nothing changes any card, threshold, or prompt. Fix: the per-card
  scoreboard + auto-demotion (PR13 slice 1, demotion-first — the safe half under
  PD#3, shippable early).
- **V5 — the final two-lane order** — now the authoritative §5 punch list.

---

## 4. The law

The constitution lives in the master build plan: **§1 Prime Directives (10)**, **§5
Autonomy Ladder L0–L5 with automatic rollback triggers**, **§8 Never-List (10 hard
invariants)**, plus HANDOVER §10's per-layer invariants. Do not paraphrase them from
memory — open the file. The ones violated most often by well-meaning newcomers:

- Shadow-first, always. Nothing new influences a decision until promoted on evidence.
- Unknown ≠ zero. Missing ≠ safe. Mock ≠ real. Paper expectancy = upper bound.
- Demotion automatic, promotion never. Pre-register before testing.
- The target sizes the roadmap, never the trade.
- One replay engine, one sizing formula, one kill switch.
- Merges to `main` happen only on explicit human instruction (T4). Docs may be
  pushed directly; code may not.

**New law from this review (CRO directive, adopted):** *No autonomy-ladder promotion
and no Phase-4 planning until every alert path has fired once for real (drill) and a
backup has been restored once (drill).* You cannot supervise what cannot page you.

---

## 5. The punch list (ordered; owner in brackets; struck when done)

**Tonight — before the first unattended scan (Mon 09:35 ET / 21:35 SGT):**
1. ✅ **PR9.1 hotfix** — merged `b70ff2e` 2026-07-06, 810/3/0 post-merge. Done.

**Day 1 (operator-only inputs):**
2. ✅ `NTFY_TOPIC` set (operator's own topic) + subscribed on the ntfy app. Done 2026-07-06.
3. ✅ `MAX_PAPER_TRADES_PER_DAY` — kept at `1000000`, confirmed deliberate by the
   operator (see §2's live-config-truth note; this is NOT the drift it was
   first flagged as). Resolved, no code change needed.
4. ✅ `sudo pmset -a autorestart 1` (operator ran it, verified `autorestart 1`)
   · ✅ `chmod 600 .env` (done). Done.
5. ✅ All three drills PASS, operator-confirmed 2026-07-06 (`docs/incidents/2026-07-06-pr9-acceptance-drills.md`):
   kill-switch engage→verify scan skipped (zero AI cost, monitor kept running)→release;
   forced job failure → phone notification received; stale heartbeat (forced clock
   offset — literal 2h wait wasn't practical) → phone notification received. **PR9 is
   now complete except the passive 10-consecutive-trading-day streak** (started
   2026-07-06; does not block PR9.5/PR10).

**Week 1 — PR9.5 "Ops & Measurement Hardening" (small, spec in the specs doc):**
6. ✅ Backup LaunchAgent built, merged, **activated and verified 2026-07-07**
   (`.backup` → integrity_check → gzip → iCloud → 30d/12m rotation → alert on
   failure, 10 tests). Hit a real macOS Full Disk Access wall on first install
   (repo lives under `~/Documents`; see specs doc §H.13) — operator granted FDA
   to `/bin/bash`, re-verified end-to-end (real gzip files, matching
   integrity-check-passed timestamps). **Still open**: the operator's own
   quarterly restore-test drill (README has the exact 3-step command) — the
   automation writing backups isn't the same claim as a human having confirmed
   one restores.
7. ✅ **Benchmark spine** (T4): daily paper-equity snapshot + SPY total-return
   series + relative-return/rolling-beta report block. Merged `e075adb`. The
   irreplaceable dataset starts accumulating 2026-07-07.
8. ✅ Cost-cap true-up: now counts labeller + polarity calls too (was
   undercounting real spend 2-3x); captures `resp.usage` tokens on all 3 call
   sites. Merged `e075adb`.
9. ✅ Logs → `~/Library/Logs/alphaos/`; `requirements-lock.txt` committed;
   `config_versions` now `stable_hash` (SHA256, deterministic across processes
   — re-verified with real `PYTHONHASHSEED` runs). Merged `e075adb`.

PR9.5 full audit: verdict APPROVE, no HIGH findings; one MEDIUM (benchmark-bars
pagination truncation past ~200 trading days) fixed and adversarially
re-verified (315-business-day gap closes in one call); isolation, schema
additivity, config-hash determinism and cost-guard sums all independently
re-confirmed by a second audit pass. Suite 884/3/0. Full as-built deltas: specs
doc's PR9.5 SHIPPED banner.

**Then, in order:**
10. ✅ PR10 Setup Cards v1 — merged `0e5b3fa` 2026-07-07, Opus-audited APPROVE.
    The versioned join key is live: every candidate/proposal stamped with its
    setup card, exit-first invariant ("no entry without a written exit")
    enforced at `_execute()`, attribution/digest gain floor-gated by_card
    slices. v1 = one card (`catalyst_momentum_v1`), a behavior-neutral
    transcription. Suite 909/3/0.
11. ✅ **PR11 Daily Brief + Portfolio Health + Moonshot Gap** — merged `1656b3b`
    2026-07-07, Opus-audited APPROVE (two subagent passes + a direct Opus
    verification; 3 real issues found and fixed). The daily human interface is
    live: `alphaos brief` CLI, per-position health (thesis INTACT/AT_RISK/BROKEN,
    verdict HOLD/ATTENTION/EXIT_REVIEW — never auto-exits), moonshot-gap
    arithmetic vs the 10% target (now measurable thanks to #7's benchmark spine),
    a compact digest-job alert, and a digest position_health summary. **Backend
    only — UI-PR-A (dashboard annunciator/Tonight tab) deferred as the next UI
    item**, consuming the same brief dict. Suite 946/3/0.
12. ✅ **Structural PR before PR12 (SC)** — merged `5e39f6f` 2026-07-08: typed
    `ScanContext` (side-channel structurally dead, `__setitem__` refuses `_`-keys),
    `CandidateStatus` enum, ruff + loose mypy in CI.
12a. ✅ **UI-PR-A Operator Console v1** — merged `c3eeefb` 2026-07-08: annunciator
    strip, Tonight tab (brief dict), position health cards, approvals TTL-sort +
    verbatim exit plan, hindsight ΔR column (mock-tagged, never-zero). +67 tests.

**From here the authoritative order is the 2026-07-08 two-lane ruling (§3.5 V5;
item specs under their canonical names in the specs doc):**

**Lane A — critical path, one build session at a time:**
13. ✅ **OPS-A** — dashboard loopback bind + non-loopback action-disable guard,
    merged `de92be7` 2026-07-08 (+ a post-merge false-lockout fix, `b0ce043`:
    the server's own bind address is now the authoritative loopback signal,
    `st.context.ip_address` only actionable when it positively contradicts a
    safe bind, never on mere absence — see specs doc OPS-A SHIPPED banner).
13a. ✅ **EXP-0** — shadow-tier deterministic universe capture, merged `ec92c55`
    2026-07-09 (branch commits `329584b`+`d394361`). ~300+ small/mid names
    ($5–50M ADV, price $5–100), `alphaos universe_build` CLI (Alpaca assets +
    historical bars, committed/git-versioned JSON, operator reviews +
    commits — never auto-armed), batched snapshot fetch, same 3-window
    pipeline as core, **zero AI calls, structurally no proposals** (both the
    AI-evaluation and proposal-creation chokepoints refuse `shadow_tier=1`).
    `universe_days` survivorship table (append-only, one row/symbol/trading
    day regardless of candidate outcome). As amended 2026-07-08 late (UNIV-1
    salvage): floor/flags (leveraged-ETF/SPAC/recent-IPO best-effort), + the
    survivorship journaling itself. **Two independent Opus audits (APPROVE
    WITH NOTES)** converged on the same HIGH finding — the user-override path
    had no shadow_tier guard (a stale docstring wrongly claimed full
    coverage) — reproduced by both, fixed with a dedicated graceful-refusal
    guard + regression test before merge. +41 tests, suite 1009/3/0.
    `SHADOW_TIER_ENABLED=false` by default — **the dataset has not started
    accumulating yet**; an operator still needs to run `universe_build`,
    review the symbol list, commit it, and flip the setting. Spec: specs doc
    EXP-0 + reconciliation deltas.
13b. ✅ **REG-1** — regime classifier + packet stamping, merged `548f484`
    2026-07-09 (branch commits `ebdd074`+`daca21c`, overnight autonomous
    session). Frozen four-state classifier (CRISIS/CHOP/TREND_UP/TREND_DN)
    from SPY daily bars (via `benchmark_bars`, no second data vendor),
    computed once per scan and stamped onto every `candidate_packets` row;
    new append-only `regime_days` table; `backfill_regime_days` CLI (one-off,
    deep history); daily brief regime header + caveat; shadow arming-map
    scorer (pre-registered v1 map, CRISIS hard-coded never armed regardless
    of map, floor-gated on distinct regime episodes — the earn-its-existence
    instrument for REG-2). `REGIME_ENABLED` defaults true (no human-review
    gate needed, unlike EXP-0's shadow tier). **Two independent Opus audits**:
    correctness APPROVE (zero bugs after independently reimplementing the
    classifier math and diffing); scope/safety APPROVE WITH NOTES (safety-
    critical paths SHA-256-confirmed untouched, a poisoned regime label
    proven to produce zero decision impact; one MEDIUM — stale-benchmark-
    spine data could be silently stamped as fresh — found, reproduced, and
    fixed pre-merge). +40 tests, suite 1048/3/0. **Not yet done**: no
    operator has run the one-off backfill yet, so `regime_days` only has
    same-day-forward coverage until then. Spec:
    `docs/roadmap/alphaos-regime-and-text-archive-specs.md` REG-1 + the
    specs doc's reconciliation deltas.
13c. ✅ **TEXT-0** — point-in-time EDGAR text archive, merged `1b70a85`
    2026-07-09 (branch commits `3ddc70e`+`cfe3930`, overnight autonomous
    session). Collect only (no strategy, no AI, no trades); every
    `text_documents` row stamps both `published_at` (source) and `seen_at`
    (wall-clock fetch) — all future backtests may only ever condition on
    `seen_at`. Raw `urllib` REST client (no SDK), rate-limited at 4 req/s
    (below SEC's own 10/s ceiling), refuses to fetch live without an
    operator-configured contact email (SEC fair-access policy). Form catalog
    v1 (`EDGAR_FORMS_V1`, exact + prefix families). `cik_map` from EXP-0's
    universe ∪ current book ("once archived-for, always archived-for", never
    deleted). Gzip write-then-verify round-trip check before any row is
    journaled. Scheduler job (`text_archive_pull`, once-daily), backup script
    extended to mirror the archive + sha256 spot-verify, daily brief health
    line (`docs last night · total · fetch errors · oldest gap`).
    `TEXT_ARCHIVE_ENABLED` defaults **false** (unlike REG-1's true) — this
    makes real outbound requests under the operator's own identity, so it
    stays opt-in. **Two independent Opus audits**, both APPROVE WITH NOTES:
    correctness (one MEDIUM — a gzip write/read-back exception, not just a
    hash mismatch, aborted the whole run and orphaned the file; three LOW —
    non-numeric-CIK abort, an already-fully-archived day false-triggering the
    "fetcher is broken" alert, a ragged forms-array silently dropping
    entries); scope/safety (one MEDIUM, reproduced — the scheduler's
    once-daily lock key had no stable per-day branch for this job, so it
    would have re-dispatched, and re-fetched from SEC, on every tick all day
    instead of once; one LOW/MEDIUM, reproduced — an unsanitized
    `accession_no` from SEC's own response could write outside
    `storage_root`, defense-in-depth now closed with a format check).
    Byte-identical-decisions proof (enabled vs. disabled) repeated from
    REG-1's own playbook. All reproduced findings fixed pre-merge; +12 tests
    for the fixes (incl. the daily-brief health line's own coverage gap the
    audit flagged). Suite 1107/3/0, ruff + mypy clean. Spec: same archived
    file, TEXT-0 + deltas.
14. ✅ **EVAL-1** — offline eval harness, merged `28578e5` 2026-07-09
    (branch commits `a271000`+`53ac99b`+`af4ae1f`, overnight autonomous
    session continued). Replays the frozen golden corpus through the
    CURRENT `PlaybookClassifier.classify()` -- the exact production
    labeller call, never a reimplementation ("one replay engine, one
    truth"). **Scope call, made explicitly and logged for review, not
    relitigated here**: replays the LABELLER path, not the primary trade
    evaluator -- `packet_json`/`CandidatePacket` is definitionally "the
    ONLY thing sent to the AI category labeller" per its own docstring,
    and the evaluator's own `snapshot` input is never journaled anywhere,
    so it couldn't be replayed from stored data even if scope had gone the
    other way. Corpus: git-committed JSON fixtures + `MANIFEST.json`
    (sha256 per file), mirroring CANARY's own spec'd layout; additive,
    idempotent writes that never clobber an operator's hand-adjudicated
    `ground_truth_label` (which always starts `None` -- never fabricated,
    an operator fills it in by hand with hindsight). New `eval_runs`/
    `eval_results` tables; every result stored including fail-safe ones.
    Report: parse rate, label agreement vs ground truth (honest N/A until
    adjudicated), categorical stability across repeats. `cost_guard` now
    also counts real eval-replay calls (closing the same undercount bug
    class already fixed once for the labeller/polarity sites). CLI:
    `eval_corpus_build` / `eval` / `eval_report`; daily brief gains an
    "Eval harness" section. **Two independent Opus audits**, both
    **APPROVE WITH NOTES**: correctness found one MEDIUM -- a per-packet
    isolation fix I'd *just* made (self-caught, before either audit even
    started) still had a gap, wrapping packet reconstruction but not the
    `classify()` call itself, so a wrong-*type* (not missing) hand-edited
    fixture field could still escape and abort the whole run; scope/safety
    independently corroborated the same gap via a docstring-accuracy
    finding and confirmed every other standing law holds (ground truth
    never fabricated at any of 3 checkpoints, replay never touches the
    real `candidate_labels` ledger, zero decision surface proven both
    structurally and via a byte-identical mock-scan A/B, live cost cap
    refuses before spending anything). Also fixed: a reproduced (but not
    production-reachable) path-traversal write in the corpus builder,
    hardened to the same standard TEXT-0 already set for external-input-
    into-a-path; a cost-cap pre-flight magnitude check (EVAL-1's overshoot
    potential is `packets x repeats`, operator-tunable far beyond a scan's
    bounded shortlist); a latent seed-selection dedup gap; `--repeats<=0`
    validation; a weak test strengthened after mutation-testing showed it
    would pass a wrong implementation. +36 tests. Suite 1138/3/0, ruff +
    mypy clean. Spec: same archived file, EVAL-1 section.
15. ✅ **PORT-1** — merged `18b563e` 2026-07-09 (branch `685ea60`+`08cdb04`,
    overnight session continued). New `alphaos/stats/` package: `effective_n()`
    (symbol + overlapping-holding-window clustering, dedup to one obs per
    `(symbol, decision_date)`), clustered bootstrap CI + one-sided p, BH-FDR
    step-up + running-minimum q-values + Bonferroni cross-check,
    `compute_verdicts()` (the ONE always-fresh three-way verdict function --
    never cached, never recomputed over an ad-hoc slice), `preregistrations`
    table (evidence frozen exactly once via a DB-level `WHERE evaluated_at_utc
    IS NULL` guard). Ported from NightDesk's real repo (not the compressed
    spec) via `docs/roadmap/ported/nightdesk-stats-contract.md`. **Deliberate,
    documented divergence from the compressed spec's "q_value stored,
    immutable" wording**: NightDesk's own battle-tested implementation stores
    NO verdict at all, always recomputed fresh so a hypothesis can be
    correctly demoted as the family grows -- adopted as the real precedent,
    evidence immutability kept exactly as specified. Switched `attribution.py`'s
    floor from `len(rows)` to `effective_n()` (closes audit A1's false-edge
    risk). `daily_brief.py` gains the survivorship-denominator caveat. **Two
    Opus audits**: correctness found+fixed one HIGH (`r.get(...) or 1.0`
    silently corrupted a legitimate `p=0.0` -- the strongest possible
    bootstrap result -- into 1.0, since 0.0 is falsy in Python) and one
    MEDIUM (`benjamini_hochberg`/`bh_q_values` could disagree at an exact
    float64 boundary tie, verified via brute-force exact-fraction sweep);
    scope/safety confirmed zero decision surface empirically (table has zero
    production writers -- PR12 is the future writer) and one LOW doc-fix.
    +50 tests. Suite green, ruff/mypy clean. **Unblocks BASELINE and EXP-1.**
16. ✅ **INSTR-1** — merged `7219a08` 2026-07-09 (branch `a656c17`+`55a1288`,
    overnight session continued). Class A/B (real trade-decision math, not
    shadow). Part 1: `alphaos/data/intraday_volume_curve.py` -- curve-
    normalized rel_volume (cumulative-to-now ÷ previous-full-day-volume ×
    a market-typical time-of-day curve, a single versioned code constant, no
    new data pipeline) replaces the structurally-dead cumulative-vs-
    yesterday-full-day formula. Part 2: `Stop = entry ∓ k×ATR(14)`, `k=2.0`
    pre-registered, as new default card `catalyst_momentum_v2` (v1 stays
    registered, byte-identical, per PD#7). **Corrected a false premise found
    while building**: the prior mapping's "daily bars already available" was
    wrong -- built a disciplined once-daily scheduler job
    (`alphaos/reports/atr_service.py`, new `atr_history` table) reusing the
    existing `get_daily_bars()`, core-book universe only. Live-path-only
    override (mock untouched); missing ATR data fails safe to reject
    (`NO_ATR_DATA`), never a silent fallback. **Found+fixed a real
    integration gap**: REG-1's regime arming-map was keyed by literal
    card_id string and would have silently treated v2 as never-armed in any
    regime. **Two Opus audits**: correctness APPROVE (zero bugs, 112
    independent adversarial checks incl. DST correctness and hand-derived
    ATR/stop-sign math); scope/safety APPROVE WITH NOTES (one MEDIUM --
    ATR-capture failures were invisible to the operator and the rejection
    reason got flattened downstream -- fixed pre-merge; a dedicated ATR-
    coverage brief line explicitly deferred as KIV, not a safety gap).
    +42 tests. Suite green, ruff/mypy clean. **This was the last item in
    the operator's last explicit instruction — the full REG-1→TEXT-0→
    EVAL-1→PORT-1→INSTR-1 chain is now done.**
17. ✅ **BASELINE** — merged `1af4e3a` (2026-07-09, branch commits `a1fd3b0`+`bb9d934`),
    Opus-audited (two independent audits, both APPROVE WITH NOTES, zero
    BLOCKER/HIGH/MEDIUM). Deterministic shadow baseline: two frozen rules
    (`threshold_v1`, `propose_all_v1`) journaled 2:1 with every AI evaluation,
    resolved via the ONE replay engine, measured with a new day-block BCa
    bootstrap (10k resamples, normal-approx fallback). `entry_fill_status` +
    the verbatim gross/gap-free caveat added pre-merge per audit finding.
    `preregistrations` row #1 registerable via `alphaos baseline_register`
    (idempotent). **Q-value/FDR reporting explicitly deferred as KIV** — it
    requires a FROZEN evaluated-preregistration family that doesn't exist
    until an operator runs the (not-yet-built) evaluate CLI after
    `analysis_not_before` (2026-09-07); premature today, not a safety gap.
    Bundled with 4 small follow-ups from a Fable strategy review of the
    prior overnight session's reversible decisions (REG-1 floor now requires
    BOTH its own episode count AND PORT-1 `effective_n()`; attribution.py's
    `max_holding_days` join via a correlated subquery; ATR-coverage daily-
    brief line; the audit-agent tool-scope lesson codified in the specs doc
    §H.14/§T3). +68 tests. Full suite green, ruff/mypy clean. **Not yet
    done**: no operator has run `baseline_register` against the real
    production DB yet, so the pre-registration hasn't been created there;
    the shadow ledger starts accumulating forward-only from merge time.
18. 🟡 **EARN-1** — real earnings provider behind the PR5 factory (defines
    "catalyst" for the niche; hard gate for card v2). Built + audit-fixed
    2026-07-09/10 (branch `feat/earn-1-alpha-vantage-provider` @ `2d5a0a2`,
    vendor Alpha Vantage, two independent Opus audits both APPROVE/APPROVE
    WITH NOTES) — **committed, not yet merged**, holding for explicit
    operator merge instruction.
19. 🟡 **EXP-1** — shadow small/mid catalyst universe (300–500 names, $5–50M ADV),
    cost-tiered scanning (deterministic pre-rank → AI top-K), effective-N
    floors from day one. The payload. CANARY must be LIVE first (built +
    audit-fixed, not yet live — see item 21). **Full build-ready spec
    written 2026-07-10** (seven-lens Fable synthesis: founder/PM, quant
    researcher, software engineer, ML engineer, quant trader, infra/devops,
    CRO — see `docs/roadmap/alphaos-pr-implementation-specs.md`'s EXP-1
    section) ahead of the CANARY gate clearing, so the eventual build
    session starts from a real spec, not a blank page. **Not yet built.**
20. 🟡 **PR12** (registry-first, 8 seeded hypotheses) — built + audit-fixed
    2026-07-10, branch `feat/pr12-hypothesis-engine` @ `0a96a86` (build
    `fec2945` + audit-fixup `0a96a86`). Two independent Opus audits:
    correctness **REQUEST CHANGES** → fixed (a real HIGH: `candidate_outcomes`
    fan-out via parallel `user_override` rows corrupted every reference-arm
    mean; `h_ttl_1_rows` additionally leaked the same row into both arms via
    re-propose-after-expiry — both fixed with a uniform per-candidate dedup
    + regression tests verified to fail pre-fix/pass post-fix), scope/safety
    **APPROVE WITH NOTES** (zero BLOCKER/HIGH/MEDIUM). 31 tests, full suite
    green (1392 passed), ruff/mypy clean. **COMMITTED, NOT YET MERGED**,
    holding for explicit operator merge instruction. → **PR13 slice 1** 🟡
    (scoreboard + auto-demotion) — built + audit-fixed 2026-07-10, branch
    `feat/pr13-scoreboard-demotion` @ `d809575` (build `1a6add1` +
    audit-fixup `d809575`). Two independent Opus audits, both **APPROVE
    WITH NOTES**, zero BLOCKER/HIGH from either: correctness found one LOW
    (demotion alert fired before the DB insert, a narrow CLI-vs-scheduler
    race could double-page — fixed by reordering to insert-then-alert) and
    a NIT; scope/safety found one MEDIUM (a demoted card vanished silently
    from the scoreboard report with no standing marker — fixed, now shows
    a "Demoted (terminal)" section) and confirmed all other hard invariants
    (Prime Directive 7 never touched, zero promotion scope creep, zero
    decision-surface leakage, floor reused verbatim from `attribution.py`,
    complete scheduler wiring). 22 tests, full suite green (1383 passed),
    ruff/mypy clean. **COMMITTED, NOT YET MERGED**, holding for explicit
    operator merge instruction. → **PR13 slice 2** 🟡 (card promotion/
    graduation + manual demotion) — built + audit-fixed 2026-07-10, branch
    `feat/pr13-slice2-promotion` @ `23e539e` (merge `297b71e` + build
    `f251797` + audit-fixup `23e539e`). A focused Fable5 consult (not full-
    panel) drew the "graduation vs. mutation" distinction before this was
    built: v0 ships graduation ONLY (an existing shadow card version's
    state moves to live_eligible, content untouched, no version minted) —
    mutation (a real content diff via PR13.5's own ceremony) has no
    producer today (PR12 proposes no diff content; PD#4 defers a generator
    to v1.1) and was explicitly ruled out of scope. Gates on operator-set
    `MET` (new `mark_hypothesis_status()`, the only writer of MET/FAILED/
    WITHDRAWN) via a new `check_promotion_preconditions()` (~10 named
    reason codes) + new `promotion_decisions` table, kept deliberately
    separate from slice 1's own `card_demotions`. Never touches
    `setup_cards`/card YAML (Prime Directive 7). Two independent Opus
    audits, both **APPROVE WITH NOTES**, zero BLOCKER/MEDIUM from either —
    scope/safety confirmed via whole-repo grep that a card's registered
    state/content is provably untouched and no automatic path can reach
    promotion; correctness found + fixed 2 real **HIGH**s (a
    `mark_hypothesis_status()` race: the DB-level `WHERE status='resolved'`
    guard was correct, but a losing racer's own call never checked
    `cursor.rowcount` and silently reported the WINNER's row as its own
    success — fixed to match the sibling `evaluate_hypothesis()`'s own
    correct pattern; and a `sqlite3.IntegrityError` catch in
    `promote_card()`/`demote_card()` that was dead code because the index
    it relied on was non-unique — a real concurrent double-promote would
    have silently inserted two rows — fixed by making the index UNIQUE)
    plus a LOW (`Q_VALUE_FLOOR` was a duplicated literal, now a genuine
    import from `alphaos.stats.fdr.DEFAULT_FDR_Q`); scope/safety found one
    LOW (`card_demote`'s CLI dry-run skipped precondition checks entirely,
    unlike `card_promote`'s own dry-run — fixed with a shared
    `check_demotion_preconditions()`). Every fix verified with a regression
    test confirmed to fail pre-fix/pass post-fix. 40 tests, full suite
    green (1454 passed), ruff/mypy clean. **COMMITTED, NOT YET MERGED**,
    holding for explicit operator merge instruction. PR13.5's diff-
    rendering/YAML-writing ceremony explicitly NOT built (no producer of
    real diff content exists yet) — one labeled function-boundary seam is
    left for it. → cards v2–v3 (blocked on real shadow evidence for
    H-CAT-1/H-POL-1 — not yet buildable) →
    **PR14** 🟡 (Red-Team Debate v0, shadow bear-only) — built out of strict
    roadmap order ahead of cards v2–v3 since it has no evidence gate (a
    registry/mechanism-only shadow feature, same "ship mechanism now,
    evidence-gated items wait for real data" precedent as EXP-1/PR13.5);
    built + audit-fixed 2026-07-10, branch `feat/pr14-red-team-debate` @
    `c7e7741` (build `8dc4087` + audit-fixup `c7e7741`). Adversarial "bear"
    LLM agent votes on trade proposals strictly after a scan batch's
    decisions are committed, mirroring TQS's own post-commit call-site
    guarantee. New `agent_votes` table (role-parameterized for a future
    triad); new `alphaos/ai/bear_debater.py` (mock-path convention modeled
    on `OpenAIClient`, not `ClaudeReviewer`'s manual-only/no-mock
    convention, since this runs automatically and every test here is
    offline); new `alphaos/debate/batch.py` scoped via `candidates.status
    ='proposed'` — NOT `trade_proposals.status`, which never actually
    persists `'proposed'` for a real propose decision (a real bug
    self-caught pre-audit, empirically proven via swap-test-restore: the
    naive filter would have voted on zero real proposals in production).
    `debate_max_calls_per_day` (default 10) nests inside the existing
    shared 30-day AI cost cap; `debate_shadow_enabled` defaults **False**
    (a genuinely paid LLM call, follows CANARY's cost posture not TQS's
    zero-cost default-True). New CLI `cmd_debate_register()` pre-registers
    the bear-debate hypothesis (`oppose_high_conviction_v1`). Two
    independent Opus audits, zero BLOCKER: scope/safety found + fixed a
    real **HIGH** (the daily sub-cap's own bound was never validated
    against the shared 30-day cap it nests inside — a legal config could
    let debate alone exhaust the entire shared budget in one day, exactly
    what the nested-cap design was supposed to prevent; fixed via a joint
    25%-of-pool bound, reusing EXP-1's own established ratio) + a MEDIUM
    (independently also flagged by correctness: `agent_votes.lineage_id`
    was always NULL, unlike every other AI-producing table — fixed to
    match `tqs/batch.py`'s own pattern); correctness found only NITs (same
    lineage gap; a concurrent-race edge case logged noisier than TQS's own
    silent-no-op convention — fixed to match). 28 PR14-specific tests
    (25 + 1 settings-validation test), every fix's regression test
    empirically verified to fail pre-fix/pass post-fix, full suite green
    both times, ruff/mypy clean. **COMMITTED, NOT YET MERGED**, holding for
    explicit operator merge instruction.
    → **REG-2** (regime as allocator — what "Regime Engine v1" was;
    renamed 2026-07-08 late since REG-1, its measurement half, now lands at
    13b; evidence-gated on REG-1's shadow arming-map scorer reaching its
    pre-registered floors) + **COST-1** (gates ladder rung 2) →
    portfolio-risk gates (Class C: gross-notional/sector caps, live-equity sizing
    — kill the static 100k) → **PR15/L3** (evidence-gated; also blocked on the
    CRO restore-drill law).
20a. 🔴 **UNIV-D** (floats, non-blocking, post-TEXT-0): retroactive market-cap
    tier derivation (U1/U2/U3) by date-join once TEXT-0's SEC company-facts
    data exists. UNIV-1 as originally drafted is SUPERSEDED — see §9 and the
    specs doc reconciliation subsection. **Standing gate regardless of tiering
    scheme: nothing outside the current book becomes execution-eligible until
    COST-1 ships + calibration covers that liquidity band + per-card shadow
    evidence clears the calibrated-cost bar at effective-N floor.**

**Lane B — parallel, any session's slack:**
21. ✅ **TASK-R** retro-relabel — run 2026-07-10 against the real 7-packet
    contaminated baseline (a date-frame discrepancy between the spec's
    US-market/UTC date and the CLI's SGT filter was found and resolved via
    Fable consult; see specs doc's TASK-R erratum). Feeds CANARY's/EVAL-1's
    future corpus-building. · 🟡 **CANARY** model-drift canary — built +
    audit-fixed 2026-07-10 (branch `feat/canary-model-drift` @ `f44ac93`,
    two independent Opus audits both APPROVE WITH NOTES) — **committed, not
    yet merged**, holding for explicit operator merge instruction; **not
    yet LIVE** (needs corpus population + 2 consecutive real weekly runs
    after merge — this is what still gates EXP-1) · 🟡 **OPS-B**
    off-ecosystem backup + `env.enc` — built + audit-fixed 2026-07-10
    (branch `feat/ops-b-offsite-backup` @ `e1eb3be`, two independent Opus
    audits both APPROVE WITH NOTES; correctness audit caught + fixed a real
    HIGH — the offsite DB copy shipped as plaintext, now properly
    encrypted) — **committed, not yet merged**, holding for explicit
    operator merge instruction · 🟡 **BRIEF-FIX-1** (audit C4: floor-gate
    the brief's per-event ΔR) — built + audit-fixed 2026-07-10 (branch
    `feat/brief-fix-1-reporting-law` @ `2f057b9`, two independent Opus
    audits both APPROVE WITH NOTES) — **committed, not yet merged**,
    holding for explicit operator merge instruction · 🔴 the operator's
    quarterly restore drill (user-only; blocks L3).

---

## 6. Operating manual

**Daily (≤5 min):** read the 18:00 SGT digest (arrives via ntfy — live since 2026-07-06).
Check `python -m alphaos scheduler_status` if anything looks off. Approve/reject any
pending proposals *only* through CLI/dashboard — never raw SQL.

**Weekly:** `pytest` green · `git fetch && git status` clean · glance at
`~/Library/Logs/alphaos/*-error.log` · fuse check: any `scheduler_fused`
system_events? · backup verified current (`ls -la ~/Library/Mobile\ Documents/com~apple~CloudDocs/AlphaOS-backups/daily/ | tail -3`).

**Monthly:** Moonshot Gap line (after PR11): `expectancy × frequency × risk vs 10% —
binding constraint named`. Restore-test a backup quarterly. Re-read §3 of this file;
strike resolved items.

**Drills (log every one in `docs/incidents/`):** kill-switch (engage → next scan
window skips, monitor keeps running → release); failure-alert (break the venv path
in a *copy* of the plist or monkeypatch a job to raise once → page arrives → restore);
heartbeat (stop the scheduler agent for >2h in market hours → page arrives → reload).

**Recovery:**
- *Machine died/replaced:* clone repo → `uv venv && uv pip install --python
  .venv/bin/python -r requirements-lock.txt && uv pip install --python
  .venv/bin/python -e .` → restore newest backup from iCloud Drive to
  `data/alphaos.db` (README has the exact restore commands) → copy `.env`
  from your password manager (it is NOT in git) → `deploy/install_launchagent.sh` →
  verify `scheduler_status` + a heartbeat page.
- *Fused job:* root-cause via `job_runs.error` + logs, fix, then one manual
  `python -m alphaos scheduler_run_job <type>` — success clears the fuse.
- *Protection incident:* the system already blocks new entries; resolve only via
  `protection_resolve` / `protection_ack`. Never raw SQL. Never let an AI session
  "helpfully" clear one without broker-side verification.
- *Suspected data corruption:* stop agents (`deploy/uninstall_launchagent.sh`),
  `PRAGMA integrity_check`, restore from backup, replay nothing — the journal is
  append-only truth, gaps are honest.
- *Operator vacation:* do nothing. Proposals TTL-expire; monitor/outcomes/digest
  continue; worst case is missed opportunities, never unattended positions.

**Stopping the fund (always the easiest action, by design):**
`python -m alphaos kill engage` stops all new entries system-wide (monitor keeps
running; broker GTC protective legs stay live). `deploy/uninstall_launchagent.sh`
stops the unattended cadence entirely. Neither touches positions — flattening is
`python -m alphaos flatten`, deliberate and manual, paper-only.

---

## 7. The self-learning roadmap (what "continuously improves itself" actually means here)

Be precise, because vague "self-learning" talk is how systems get unsafe. AlphaOS
improves itself through **evidence-gated promotion loops**, not weight updates:

- **Loop 0 (live today):** unattended measurement. Every decision — taken or not —
  gets counterfactual outcomes, TQS quality scores, attribution ΔR. The system
  learns *facts*; nothing acts on them.
- **Loop 1 (PR12–13.5):** pre-registered hypotheses over its own ledger (registry
  first — the LLM generator is v1.1, not the point) → one-shot forward tests under
  effective-N floors and cumulative FDR (PORT-1) → automatic demotion via the
  per-card scoreboard / human-acknowledged promotion of setup cards. This is the
  first true self-improvement, governed by pre-registration (PD#4) and promotion
  asymmetry (PD#3). **The closure law (2026-07-08, audit B1): PR12 proposes diffs;
  PR13 toggles state; only an operator-committed YAML version changes card behavior;
  no job ever writes `cards/*.yaml`.** The demotion half (scoreboard +
  auto_floor_breach) is the safe half and ships first — it is the smallest mechanism
  that converts measurement into changed behavior without any promotion risk.
- **Loop 2 (PR14–15, L3):** adversarial red-team votes joined to attribution;
  bounded auto-approval earned on evidence.
- **Loop 3 (Phase 6):** champion-challenger model governance — new prompts/models/
  fine-tunes run in shadow against the champion, judged by attribution over
  pre-registered windows. **Prerequisite discovered in this review: an offline eval
  harness (punch-list #13). Today a prompt change ships blind.** Order of ML
  sophistication: eval harness → prompt iteration → few-shot from own ledger →
  distillation to a small local model (not cost-justified before ~5k clean
  prompt/completion/outcome triples and years of $5–15/month spend) → only then talk
  about fine-tuning. The ledger's joinable triple (packet_json → raw completion →
  forward outcome) is the future training set — which is why T5's contamination fix
  and #13's raw-completion retention matter more than any model choice.
- Data reality: ~20 label-outcome pairs/day at current cadence/universe; ~1
  trade-level reward/day at best. Universe breadth (Phase 3) is therefore also the
  ML-data strategy, not just the trading strategy.

---

## 8. Working with AI builders (the successors are sessions, not staff)

The working protocol that built PR1–PR9, preserved: **Spec (architect/Fable) → Build
(Sonnet) → Review (3–6 parallel independent agents) → Audit (Opus, T3 rubric, own
empirical probes, never testimony) → Merge (only on explicit human instruction) →
post-merge full suite on main.** Templates T1–T4 in the specs doc; house patterns in
§H — an AI session that hasn't read §H will re-pay for lessons already bought (mock
date-seeding, NULL-uniqueness, additive migrations, the chokepoint rule, shadow-proof
kit).

Session hygiene: read HANDOVER.md first, this file second; verify state before
trusting it (§8 of HANDOVER has the commands); `git fetch` before pushing (two
concurrent-session incidents on record); refresh HANDOVER at every checkpoint; one
lesson per memory file. The operator switches models by role deliberately — match
the role you were given. Auto-memory lives outside the repo; durable *project* truth
belongs in these documents, not in any session's memory.

---

## 9. Decision log (why it is the way it is — don't relitigate without new evidence)

| Decision | Why | Revisit when |
|---|---|---|
| SQLite, one file, WAL | Single operator, local-first, zero infra, transactional enough; readers don't block the 5-min writer | Multi-writer or remote access needed |
| launchd (never cron) | macOS-native, session-aware, house pattern proven in sibling project | Platform change |
| Dumb 300s tick; brains in `run_due_jobs` | Idempotent, testable, no daemon state to corrupt | Never, ideally |
| No-news v1 baseline | Kill hallucinated catalysts; enrichment added later as *labeled advisory* layers | It worked — keep the principle |
| Manual approval default | The human is the throttle until L3 evidence exists | PR15, evidence-gated |
| One replay engine | Two implementations = two truths (PR8 fought this) | Never |
| Additive-only migrations, SCHEMA_VERSION pinned | Old DBs must always open; history is append-only | Never |
| Mock prices date-seeded + direct-construction tests | Organic-scan assertions broke merged tests 3× | Never — it's §H.1 law |
| GTC protective legs for multi-day holds | META incident 2026-07-02: day-TIF legs expired, position naked overnight | Never weaken silently |
| TTL enforced at `_execute()` chokepoint | PR6 audit caught an auto-approval bypass; the chokepoint is the only honest place | Never |
| ntfy for alerts (single channel) | Zero-dep stdlib POST; one channel until it hurts | When it hurts |
| Streamlit dashboard, read-only render | UI must never do what the CLI cannot; same orchestrator, same gates | When it demonstrably hurts |
| gpt-5.4-mini both roles | Cost floor while N≈0; quality upgrades are champion-challenger material later | Eval harness exists + floors met |
| Megacap 20-name universe v1 | Deliberate training wheels: liquid, cheap data, low blowup risk while plumbing matured | NOW — it's the control group; Phase 3 builds the real habitat (§3 T3) |
| `MAX_PAPER_TRADES_PER_DAY=1000000` (uncapped) | **Operator's deliberate choice** ("removed per operator request", per the `.env` comment) — manual approval on every trade already bounds real action; a daily proposal-count cap adds no safety here, only friction. Confirmed 2026-07-06 after an AI session mis-flagged it as drift and briefly "corrected" it to 5 before being told to revert — see the memory lesson `feedback_check_env_comments_before_correcting.md`. **Do not re-flag this without new evidence from the operator.** | If proposal volume ever becomes operationally overwhelming to review daily |
| Shadow small/mid universe pulled forward to pre-PR12 (EXP-1), behind the honest-instruments stack | Partners' verdict V1 (2026-07-08): learnable flow is the binding constraint and shadow expansion is a ~10× learning-velocity multiplier at zero decision risk — but archiving niche data through megacap-calibrated instruments (dead intraday rel_volume, non-ATR "R") is irreversible contamination. Instruments first, then the payload. | If EXP-1's cost-tiered scan budget proves unworkable under the AI cap |
| Universe expansion staged: EXP-0 deterministic capture NOW, EXP-1 AI labelling after instruments | Operator-directed 2026-07-08 late session ("the current universe is way too small"). The contamination objection covered AI labels ranked by broken instruments, never deterministic collection — snapshot+interest capture is the benchmark-spine argument again (cannot be backfilled). Pre-INSTR-1 rows carry `instrument_version='pre_instr1'`, labeled never mixed. Shadow tier structurally cannot create proposals or invoke the labeller. | If IEX `feed_coverage` proves the free feed unusable for the band (then decide on SIP data — measure first, spend on evidence) |
| Free IEX feed for the shadow tier until `feed_coverage` says otherwise | Small/mid quotes are sparse on IEX (~3% of tape); rather than pre-paying ~$99/mo for SIP, EXP-0's digest line measures usable-quote coverage empirically. Freshness guard already marks sparse data honestly. | `feed_coverage` materially low after 2 weeks of capture, or EXP-1 needs quote-fresh data the free feed can't give |
| PR12 is registry-first; the LLM hypothesis generator is v1.1 | Partners' verdict V2: pre-registration is the load-bearing part (PD#4); at current N a generator fills the registry with unresolvable zombies. Seeded with 8 named human hypotheses (H-TQS-1 … H-AI-1). | Generator earns v1.1 when the registry demonstrates resolutions |
| Diff→version closure law: only an operator-committed YAML changes card behavior; no job writes `cards/*.yaml` | Audit B1 (2026-07-08): without this joint the loop toggles cards on/off but can never change what a card does — and with it automated, PD#3 would be violated structurally. `card_promote` renders the diff; the operator commits it. | Never |
| One floor law: `effective_n()` shared by reports AND promotion gates; BH-FDR family = cumulative evaluated preregistrations | Audit A1/A5: row-count floors on a one-beta-cluster book ≈ 3–5 independent bets per 30 rows; per-render FDR controls nothing across 365 nightly runs. Mirror of the one-replay-engine rule. | Never |
| BASELINE runs three arms (AI / interest-threshold / propose-all) — **shipped** `1af4e3a` | Audit C2: both original arms conditioned on interest_score — two arms measure selection inherited from the scanner, not AI value. The bracket separates them; the claim stays honest-conditional. | Rule v2+ arms are new pre-registrations |
| BASELINE's deterministic target uses `settings.min_reward_risk` as the target ratio, not a separate "identical live function" | 2026-07-09 build: the live AI path has no formulaic target (the model picks it narratively) — the only thing resembling "the identical live function" is the reward:risk FLOOR every live PROPOSE must already clear. Reusing that same number as BASELINE's own deterministic target ratio satisfies the spec's "one sizing formula law" as literally as possible without inventing a new unregistered constant. The STOP genuinely does reuse a real shared function (`alphaos.data.atr.atr_stop_price`, extracted from the live path's own `_apply_atr_stop`). | If a future card version gives the live path its own formulaic target, revisit whether BASELINE should track that instead |
| BASELINE's q-value/FDR reporting deferred as KIV, not built at merge | 2026-07-09 scope/safety audit (LOW-1): `compute_verdicts()` needs a FROZEN family of EVALUATED preregistrations; BASELINE's own hypothesis can't be evaluated before `analysis_not_before` (2026-09-07). Building a "q" display against a family that doesn't include this hypothesis yet would be statistically incoherent, not just early — the live report's own `one_sided_p_below_zero` stays a descriptive bootstrap diagnostic, never mistaken for the formal test. | When an operator first runs `evaluate_hypothesis()` on BASELINE's row (after 2026-09-07) — wire `compute_verdicts()` into whatever surface displays the result then |
| CANARY + BASELINE + PORT-1 ported by contract from NightDesk #81/#85 | Port the contract, never the code (method note in the specs doc PORT-1 section) — copying modules imports NightDesk's assumptions and none of AlphaOS's tests. | — |
| REG-1 pulled forward to A2.5 (was item 20's "Regime Engine v1" measurement half) | 2026-07-08 late reconciliation: the PORT-1 gate was about trusting sliced *claims*, never collecting labels. REG-1's urgency is epistemic — the classifier must be frozen BEFORE anyone examines regime-conditional outcomes, or every later regime definition is contaminated by peeking. Backfill is a deterministic derivation from vendor daily bars, so stamp-at-birth is recoverable — which is also why EXP-0 (unrecoverable snapshots) keeps the front slot. One classifier, one truth: EXP-1's own "regime tag v0" deleted, consumes `regime_days`. | Never relabel v1 rows in place; threshold changes = `regime_rules_v2`, new rows |
| UNIV-1 (market-cap tiers) superseded by EXP-0's ADV-band screen + UNIV-D later derivation | The capacity niche is liquidity-defined, not cap-defined — ADV is the direct measure, cap a proxy needing reference data that didn't exist. UNIV-1's salvaged parts (survivorship `universe_days` law, floor/flags, non-executability of widened names) are in EXP-0 amended; cap tiers become a retroactive date-join once TEXT-0's SEC data exists. Operator-reviewed committed universe file beats an automated monthly rebalance (human-in-loop, versioned like a card). | If the ADV band systematically misses names the thesis wants (then UNIV-D graduates from derivation to screen input) |
| TEXT-0 ships early (A2.7); reference data = SEC only, free, official | Archive value compounds strictly with calendar time and is pure collection — zero contamination risk. The seen-at law (backtests may only condition on `seen_at`, never `published_at`) is the single fact that makes the archive worth anything. SEC company-facts doubles as UNIV-D's reference source — one source, no new vendors, no scraping. Pre-inception backfill rows are never valid for point-in-time tests. Paid sources (transcripts, news wires) stay gated behind the paired-comparison law. | Never weaken the seen-at law; new free sources only if official-primary-publisher + stable API |
| EXP-0's `_override_open_trade` gets its own `shadow_tier` guard, separate from `_handle_proposal`'s | 2026-07-09, two independent Opus audits (correctness + scope/safety) both reproduced a real gap: `_handle_proposal`'s guard comment claimed it was the one true proposal-creation chokepoint, but `_override_open_trade` builds and inserts its own `trade_proposals` row independently and never calls it — a shadow-tier candidate with a stored eval (harmless today; exactly what EXP-1 is specced to add) could become a real approvable proposal via `alphaos override`. Fixed with a dedicated guard returning a graceful `OverrideBlockedReason.SHADOW_TIER_EXCLUDED` (not a RuntimeError — unlike the scan-loop backstops, this path is reachable by an ordinary user action, not an internal-logic bug). | Any future PR adding a new candidate-to-proposal path must independently verify shadow-tier isolation — do not trust a prior PR's "single chokepoint" comment without re-checking the actual call graph |
| `ensure_regime_for_today()` refuses (returns None) rather than returning a stale day's regime | 2026-07-09, scope/safety Opus audit reproduced: on a benchmark-spine gap of several days, the function's fallback took "the most recently computable day" and returned it unconditionally — silently stamping a stale label onto TODAY's packets with no marker distinguishing it from a fresh one. The `regime_days` table itself was never wrong (each row keeps its true date); only what the ongoing per-scan lookup handed back as "today's" answer was. Fixed: an explicit today-date equality check routes a stale gap through the same NULL-stamp + one-alert path already built for insufficient history. | Any function claiming to answer "as of today" from a cached/derived data source must verify the answer's own timestamp actually matches today, not just that AN answer exists |
| `REGIME_ENABLED` defaults true (unlike EXP-0's `shadow_tier_enabled=False`) | REG-1 is pure computation from data already being captured (`benchmark_bars`), shadow/measurement only, with no human-reviewed artifact to wait for (unlike EXP-0's committed universe file) — both Opus audits independently agreed this default is safe, and the scope/safety audit specifically proved a poisoned/wrong regime label produces zero decision impact by direct comparison of scan output with regime enabled vs. disabled. | If a future regime-conditional report or gate is ever built that DOES trust `candidate_packets.regime` as more than descriptive, re-examine whether the default should gate behind a review step the way EXP-0's does |
| `TEXT_ARCHIVE_ENABLED` defaults **false**, unlike REG-1's true | Unlike REG-1 (pure computation over already-captured data), TEXT-0 makes real outbound HTTP requests to sec.gov under the operator's own identifying contact email the moment it runs — a genuinely new external side effect, not just a new computation over existing data. Stays opt-in until an operator deliberately configures `SEC_EDGAR_CONTACT_EMAIL` and flips the flag, mirroring EXP-0's "ship the mechanism, operator arms it" pattern rather than REG-1's "safe by construction, default on" one. | If a future PR adds a similarly real-outbound-request feature, default to false and require the same explicit two-step arming (contact/identity config + enable flag), not REG-1's precedent |
| `default_lock_key`'s once-daily jobs (daily_digest, benchmark_spine, text_archive_pull) MUST share the same stable-per-SGT-day branch | 2026-07-09, scope/safety Opus audit reproduced: TEXT-0 was built without adding it to that branch, so `_once_daily_due`'s "already ran today" dedup (which keys purely off `default_lock_key`) never matched — the job re-dispatched, and re-fetched from SEC, on every scheduler tick all day instead of once. Fixed by adding it to the existing tuple. | Any FUTURE once-daily job type must be added to this exact tuple in `cadence.default_lock_key` — falling through to the generic per-instant key is the failure mode, and it is silent (the job still "works," it just runs far more often than intended) |
| EVAL-1 replays the playbook LABELLER, not the primary trade evaluator | `CandidatePacket`/`packet_json` is, by its own docstring, "the ONLY thing sent to the AI category labeller" — the primary evaluator (`OpenAIClient.evaluate()`) takes a raw `ScanContext` plus a separate `snapshot` dict that is never journaled anywhere, so it structurally cannot be replayed from stored data regardless of scope choice. The spec's "label agreement"/"categorical stability" language also matches the labeller's fixed categorical taxonomy far better than the evaluator's PROPOSE/WATCH/REJECT + continuous entry/stop/target fields. | Extending replay to the primary evaluator is a natural v2, but requires the evaluator to start persisting its `snapshot` input first — not a small addition, a prerequisite |
| A per-item isolation guard must wrap the ENTIRE per-item body, not just the step that seemed riskiest | 2026-07-09: EVAL-1's own per-packet isolation (added to fix a TEXT-0-class bug) wrapped only `_reconstruct_packet()`; a wrong-*type* (not missing) fixture field sailed through reconstruction (Python dataclasses don't enforce field types at construction) and only raised once `classify()` tried to use it — escaping the guard anyway. Caught by the very next Opus audit, minutes after the narrower fix was committed. Widened to wrap reconstruction AND the classify() call together. | Any future "isolate one bad item in a loop" fix must wrap the WHOLE per-item processing chain, not just the first step that can obviously fail — a downstream step can just as easily be the one that actually raises on malformed input |

---

## 10. Failure playbook

The master plan §11 table is authoritative (governor/fuse/floor/heartbeat/protection/
cost-cap/outage rows, each with its automatic response and human follow-up). This
review adds three operational rows:

| Trigger | First move (system) | Second move (human only) |
|---|---|---|
| Silent halt (no digest by 19:00 SGT, no page) | none — this is T1's blind spot until fixed | Assume down: check `launchctl list \| grep alphaos`, logs, `scheduler_status` |
| Torn/corrupt DB suspected | Gates fail toward not-trading | Stop agents → integrity_check → restore backup (§6) |
| AI session proposes editing safety/gates "for cleanliness" | — | Refuse absent a spec + audit; Never-List §8 items need no defense |

---

## 11. Glossary (house terms)

**Armed Watch** — polarity-armed near-action state; can arm, never execute. ·
**candidate packet** — whitelisted evidence bundle sent to the labeller; the future
training input. · **chokepoint** — `Orchestrator._execute()`, the single door to the
broker. · **ΔR (delta_r)** — actual-path R minus AlphaOS-path R on divergence events.
· **fuse** — per-job-type consecutive-failure self-halt (PR9). · **floors** — minimum
n + span before any aggregate is shown. · **lineage** — code/config/model/prompt
provenance stamped per decision (PR4). · **Never-List** — the 10 forever-invariants
(master plan §8). · **replay_r** — frozen-bracket counterfactual R from the one
replay engine. · **shadow** — measurement-only; written, never read by decisions. ·
**TQS** — Trade Quality Score v0, 7-component shadow score. · **TTL** — proposal
time-to-live; hard gate. · **watchdog** — broker protection checker; detect+block
only.

---

## 12. Pivot criteria (pre-registered now, so nobody re-argues them under stress)

Write the post-mortem *before* the funeral. If, after cost-calibration (Phase 3) and
≥6 months of unattended data:

- no card shows positive net expectancy after calibrated costs, **and** the
  hypothesis engine has produced no promotable edge for 2 consecutive quarters →
  the *edge thesis* is wrong: pivot strategy content (new setup families, different
  holding classes, different universes) — **the OS, measurement rails, and safety
  substrate carry over unchanged. That optionality is the real asset we built.**
- expectancy is positive but frequency can't scale past ~2%/mo compounding → accept
  the honest ceiling or expand capacity surface (more niches, more holding classes) —
  never risk-per-trade escalation (PD#1, Never-List #2).
- the operator stops reading the brief for >2 weeks → the system is unsupervised in
  spirit: drop to L0 cadence-off until attention returns. Unwatched autonomy is not
  autonomy, it's abandonment.

---

*Signed out 2026-07-06. The instrument is sound; the law is written; the tap is
open. Feed it data, hold the law, promote nothing without proof — and the version of
this fund that exists in five years will be one we'd have been proud to run
ourselves. — the founding team*
