# ALPHAOS MASTER REFERENCE — The Founding Team's Handoff

**Version 1.0 · 2026-07-06 · The retiring team: founder/architect (Fable 5), quant
researcher, senior software engineer, ML engineer, head quant trader, infra/DevOps,
chief risk officer.**

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
LaunchAgents + fuse + heartbeat + alerts; **activated 2026-07-06**). Every PR since
PR3: spec → build → independent review agents → Opus audit → explicit-instruction
merge (T4).

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
HIGH, found independently)** 🟡 The scanner/orchestrator stash private objects
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
10. 🟡 PR10 Setup Cards v1 (spec ready) — the join key for all learning. **In progress.**
11. 🔴 PR11 Daily Brief + Portfolio Health + Moonshot Gap (now measurable, thanks
    to #7) + UI-PR-A annunciator/Tonight tab alongside.
12. 🔴 **Structural PR before PR12** (SWE #1): typed `ScanContext` replacing the
    `_*` side-channel; ruff + loose mypy in CI; enum-ify status literals as touched.
13. 🔴 **Eval harness before any prompt/model change** (ML #1): `alphaos eval` —
    replay journaled `packet_json` through current templates vs a frozen golden set;
    store raw completions on ALL paths including failures; then PR12–15 per specs.
14. 🔴 Phase-3 pull-forwards, evidence-gated (trader #1): time-of-day-normalized
    20d rel_volume; ATR-scaled stops; real earnings provider as hard gate for the
    niche book; shadow small/mid catalyst universe; portfolio gates (gross-notional
    cap, sector cap, live-equity sizing input — kill the static 100k).
15. 🔴 Researcher's standing items: effective-N/symbol-day dedup in every floor;
    attribution touch-conditioning caveat; align candidate-level max-favorable
    anchoring with trade-level MFE.

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
- **Loop 1 (PR12–13):** nightly hypothesis generation over its own ledger →
  pre-registered forward tests → automatic demotion / human-acknowledged promotion of
  setup cards. This is the first true self-improvement, and it is governed by
  pre-registration (PD#4) and promotion asymmetry (PD#3).
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
