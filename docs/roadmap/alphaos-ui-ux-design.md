# AlphaOS UI/UX Design — The Operator Console

**Version 1.0 · 2026-07-05 · Fable 5**
**Reviewed as: trading command-center product designer · trading systems architect ·
risk/audit UX designer · AI-agent transparency designer.**
**Companion to `alphaos-master-build-plan.md` §13 and `alphaos-pr-implementation-specs.md`.**

Current substrate (ground truth): Streamlit dashboard
(`alphaos/dashboard/streamlit_app.py`) — sidebar with kill-switch engage/release +
manual action buttons, four tabs (Approval Center, Candidates, Open Trades, Closed
Trades), read-only rendering with actions routed through orchestrator methods (same
gates as CLI). This document designs where that grows — not a generic brokerage
dashboard, but the console for a **controlled-autonomy AI trading OS**.

---

## 1. Core UX philosophy

**The user is not a trader watching charts. The user is the accountable supervisor of
a machine that trades.** Every design decision follows from that inversion.

1. **Four questions, always answerable in ≤5 seconds:** What is the machine doing?
   Why? What does it need from me? How do I stop it? Any screen that can't route to
   those four answers is decoration.
2. **The annunciator principle (aviation, not casino).** Mode confusion is the
   deadliest failure in supervised autonomy. A permanent status strip shows: mode
   (PAPER/LIVE), autonomy level (L0–L5), kill-switch state, governor state, scheduler
   heartbeat age, open R, pending-approvals count. It never scrolls away, on any
   screen. Aircraft crews know the autopilot mode at a glance; so does an AlphaOS
   operator.
3. **Asymmetric friction.** Viewing: instant. Approving: one deliberate confirm with
   the full exit plan visible. Increasing risk/autonomy: heavy friction (typed
   confirmation, cooling-off notice). Stopping: the single easiest action in the
   entire UI — the kill switch is always one click away and never behind a menu.
4. **Evidence-state honesty.** Every number wears its provenance: live vs mock vs
   degraded, sample size vs floor, paper-upper-bound caveat. Mock/paper data is
   NEVER styled identically to live/proven data. Below-floor aggregates render as
   counts with a greyed "below floor n/N" badge — the UI enforces the reporting law,
   it doesn't just repeat it.
5. **Calm by default; silence is a designed state.** No flashing, no red/green tick
   noise, no autorefresh anxiety. "Nothing needs you — next scan 14:00 SGT" is a
   first-class, well-designed message. Alerts are ranked, rate-limited, and few.
6. **Progressive disclosure of machine reasoning.** Verdict → one-line why → evidence
   table → full narrative → raw provenance. Never a wall of LLM prose as the primary
   surface; never a bare score without its confidence.
7. **Hindsight is a feature, not an accusation.** Rejects/misses/blocks are shown
   with their counterfactual outcomes in a neutral learning frame ("rejection saved
   +1.0R" / "block cost −2.1R"), aggregated before judged, never as FOMO alarms.
8. **The UI can never do what the CLI cannot.** Every button routes through the same
   orchestrator methods and gates. No UI-only pathways, no client-side state that
   implies authority the server doesn't enforce.

---

## 2. Information architecture

Three planes, one permanent strip:

```
┌─ ANNUNCIATOR STRIP (permanent, all screens) ─────────────────────────────┐
│ MODE:PAPER · AUTONOMY:L1 · KILL:ARMED[button] · GOV:NORMAL · ♥ 4m ago    │
│ Open 3 pos / 2.1R at risk · Day P&L −0.4R · Approvals: 2 (1 expiring)    │
└──────────────────────────────────────────────────────────────────────────┘
OPERATE (daily)      UNDERSTAND (why)             GOVERN (control)
 1. Tonight           4. Candidates & Decisions    7. Autonomy & Risk
 2. Approvals         5. Learning                  8. System & Audit
 3. Positions         6. Cards (post-PR10)         9. Brief Archive
```

Drill-down rule: every number is a link — metric → decision row → lineage snapshot →
raw ledger rows. Nothing is a dead end; the audit trail IS the navigation.

---

## 3–4. Screens and what each shows

**1 · Tonight (home).** The operating screen — see §5.

**2 · Approvals.** The queue, sorted by TTL remaining. Per proposal: symbol/direction/
card, entry→stop→target ladder, R:R, size & $ risk, TTL countdown, TQS score+
confidence+bucket (with missing-component chips), freshness state, earnings flag,
narrative warning if `high_risk_narrative`, bear-vote stance (post-PR14, shadow
label), the written exit plan verbatim, and — critically — **why this exists**: the
one-line decision driver. Actions: Approve (confirm modal re-showing max loss),
Reject (reason picker feeding `user_reason_code`), Snooze-to-expiry (do nothing —
TTL handles it, shown honestly as an option).

**3 · Positions.** Per-position health cards (PR11 engine): R-ladder visual
(−1R…0…now…target), distance-to-stop/target, thesis status INTACT/AT_RISK/BROKEN
with the invalidation_reason text, HOLD/ATTENTION/EXIT_REVIEW verdict, protection
status, TTL/freshness, catalyst-ahead flag, days-held vs max, MFE/MAE so far.
Actions: flatten (manual, gated), acknowledge ATTENTION, open full decision trail.

**4 · Candidates & Decisions.** Today's scan output as a decision funnel:
scanned → shortlisted → evaluated → proposed/watch/rejected/blocked, with counts vs
30-day typical. Every row: card, TQS, decision driver, reason codes. Filter chips:
rejects / blocked (by gate) / watch / expired / superseded. **Hindsight column**
(from attribution + candidate_outcomes): "what happened next" — replay verdict and
ΔR once resolved, honest `pending`/`unresolvable` states otherwise. Weekly "misses
that mattered" panel: top rejects/blocks by resolved counterfactual ΔR, floor-gated,
neutral tone.

**5 · Learning.** Four sub-panels: **TQS** (score distribution by bucket, data-
confidence histogram, per-component availability rates — "evidence coverage", never
score-without-confidence); **Attribution** (ΔR by event type × agent, floor-gated
means, mock-excluded count visible, plain-language event feed: "You rejected AAPL
6/12 → replay stopped out → +1.0R saved [resolved]"); **Hypotheses** (post-PR12:
registry with frozen criteria, testing progress bars n/N and days/span, met/failed
outcomes); **Journal** (chronological: resolved events, promotions/demotions with
evidence links, weekly self-audit findings later).

**6 · Cards (post-PR10).** One page per setup card: version history, state
(shadow/paper/live-eligible/retired), per-card TQS profile vs expected, per-card
attribution ΔR and expectancy vs floors, pending hypotheses against it, promotion/
demotion log. The card page is where "does this setup actually work?" gets answered.

**7 · Autonomy & Risk.** The governance console — see §10.

**8 · System & Audit.** Scheduler runs + job health (fuse states, last success per
job type, heartbeat), cost budget (AI calls/30d vs cap, debate calls), incidents
(protection/watchdog, open + history, resolve/ack actions), lineage explorer (paste
any id → full provenance chain), data-quality counters (mock share, degraded rows),
test/audit status of the running build (git sha, last audit verdict).

**9 · Brief Archive.** Every daily brief + weekly review + monthly Moonshot Gap
Report, rendered, searchable. The gap report page shows the one line that matters
(`expectancy × frequency × risk vs 10% — binding constraint: X`) with its trend.

---

## 5. The "Tonight" operating screen (written wireframe)

```
┌ ANNUNCIATOR STRIP ────────────────────────────────────────────────────────┐
├───────────────────────────────────────────────────────────────────────────┤
│ ① THE ONE ACTION                                                          │
│ ▸ Approve or reject NVDA long before 22:31 SGT (TTL 41m) — everything     │
│   else is nominal.                                    [Open in Approvals] │
├───────────────────────────────────────────────────────────────────────────┤
│ ② NEEDS YOU (2)                          │ ③ OPEN RISK NOW                │
│ • NVDA proposal — TTL 41m   [review]     │ 3 positions · 2.1R total       │
│ • AMD position ATTENTION:                │ worst: AMD −0.6R ▂▄▆ stop 0.4R │
│   earnings in hold window   [review]     │ away · all protected ✓         │
├──────────────────────────────────────────┴────────────────────────────────┤
│ ④ TODAY'S MACHINE ACTIVITY                                                │
│ 3/3 scans ✓ · 41 scanned → 12 shortlisted → 3 proposed · 6 rejected ·     │
│ 2 blocked (risk) · 1 expired · TQS today: ▁▃▆▃▁ median 58 (conf 0.74)     │
│ vs typical day: proposals +1, rejects normal                [Decisions →] │
├───────────────────────────────────────────────────────────────────────────┤
│ ⑤ TONIGHT'S BRIEF                                                         │
│ Market: SPY above trend, vol compressing (regime: trend_up/low-vol)       │
│ Best candidate: NVDA — catalyst_momentum_v1 · TQS 71 (conf 0.85)          │
│   why ▸ / bear case ▸ / risk case ▸ / exit plan ▸        (collapsed rows) │
│ Learned today (2 resolved):                                               │
│ • 6/28 TSLA reject → replay stopped out → +1.0R saved [resolved]          │
│ • 6/25 expiry → replay hit target → −1.8R operational cost [resolved]     │
├───────────────────────────────────────────────────────────────────────────┤
│ ⑥ MOONSHOT GAP (monthly)                                                  │
│ 0.31R × 22 trades × 0.75% ≈ +5.1%/mo vs target 10% — binding constraint:  │
│ FREQUENCY (universe). n=61 resolved live · paper-upper-bound caveat ⚠     │
├───────────────────────────────────────────────────────────────────────────┤
│ ⑦ (when quiet) ✓ Nothing needs you. Next scan 21:30 SGT. Heartbeat 4m.    │
└───────────────────────────────────────────────────────────────────────────┘
```

Blocks ①/② collapse away when empty; ⑦ replaces them. Block ⑥ shows weekly
data-progress toward floors until the floors are met, then monthly arithmetic.

---

## 6. Showing AlphaOS reasoning without overwhelm

A fixed five-rung disclosure ladder, identical everywhere a decision appears:

```
Rung 1  VERDICT      PROPOSE · long · catalyst_momentum_v1        (always visible)
Rung 2  ONE LINE     driver: confirmed product-launch catalyst + interest 0.82;
                     top risk: earnings in 9 days                 (always visible)
Rung 3  EVIDENCE     TQS component table: score·weight·available/missing+reason
                     + gate results (freshness ✓ risk ✓ TTL 2h)   (one click)
Rung 4  NARRATIVE    eval reasoning summary, label rationale, polarity/catalyst
                     detail, bear vote w/ failure modes           (one click)
Rung 5  PROVENANCE   lineage_id chain, model+prompt hashes, config hashes,
                     raw rows                                     (audit drill)
```

Rules: no chain-of-thought dumps as primary UI (summaries at rung 4, raw only at
rung 5); agent votes render as structured stance/conviction/failure-mode chips,
never prose paragraphs; **"why not" is symmetric** — rejected/blocked candidates get
the same ladder with reason codes at rung 1; every rung-3 score carries its
confidence/availability; anthropomorphic language banned ("AlphaOS is confident" →
"evaluator confidence 0.8 (mock=false)").

## 7. Rejects, missed trades, blocked trades

- One funnel view (screen 4), filter chips per outcome class — rejects, risk-blocks
  (by gate code), freshness-blocks, expiries, supersessions.
- Each row's **hindsight cell** fills in as PR8 attribution resolves: replay verdict
  chip (`stop_hit`/`target_hit`/`ambiguous`/`unavailable`) + signed ΔR with the
  standard convention legend ("ΔR>0 = the non-trade added value"). Unresolved shows
  `pending` — the UI never backfills zeros (unknown-never-zero carries to pixels).
- Aggregates panel: ΔR by event type × agent, floor-gated exactly like the report
  layer; gate table for blocks ("wide_spread: 14 blocks, net +3.2R saved [n=14/30
  below floor — counts only]").
- Anti-FOMO: hindsight cells are informational grey, never alarm-red; single misses
  are never headlined; only floor-met aggregates may appear on Tonight.

## 8. Open positions display

The R-ladder is the centerpiece — everything positions-related speaks in R, not $
(dollars shown secondary):

```
AMD · long · catalyst_momentum_v1 · day 2/3          verdict: ATTENTION
  -1R ────────●──────────○──────────────▲ +2R
      stop   now(−0.6R)  entry          target
  thesis: AT_RISK — earnings inside hold window (flag, not stop)
  invalidation: "catalyst refuted or reclaim fails" — not triggered
  protection ✓ GTC · MFE +0.4R · MAE −0.7R · freshness ok · TTL n/a (filled)
  [explain ▸ rungs]  [flatten — gated]  [acknowledge attention]
```

EXIT_REVIEW verdicts are visually distinct but explicitly labeled "human decision
required — AlphaOS does not auto-exit on health verdicts."

## 9. TQS, attribution, ΔR, learning journal — human-readable

- **TQS** = "evidence-weighted setup quality": big number + bucket + an **evidence
  coverage bar** (data_confidence) directly beneath — the two are never separated.
  Components as horizontal bars; missing ones greyed with their reason chip
  (`mock_ai`, `earnings_unavailable`…). Mock rows carry a MOCK watermark.
- **Attribution** = plain sentences, generated from typed rows: subject (You/Gate/
  TTL/Execution) + action + counterfactual + signed R + resolution state. The ΔR
  sign convention appears as a one-line legend on every attribution surface.
- **execution ΔR** shown separately from decision ΔR always (matching the PR8 rule
  that `propose_approved_executed` never implies decision divergence).
- **Journal** = a feed, newest first, three entry types only: resolved events,
  hypothesis lifecycle (proposed→testing n/N→met/failed), promotions/demotions with
  evidence links. Every entry links into rung-5 provenance.

## 10. Autonomy, risk limits, kill switch, real-money lock

Screen 7, the governance console — deliberately the most physical-feeling screen:

```
┌ AUTONOMY ───────────────────────────────┬ HARD LIMITS (read-only view) ────┐
│ Level: L1 — unattended cadence          │ risk/trade 0.75% · max pos 3     │
│ "May alone: scan, monitor, measure,     │ auto-approvals 0/1 today         │
│  score, attribute, alert.               │ daily-loss stop 2.0% · TTL 2h/45m│
│  May NOT alone: approve, size, exit,    │ AI budget 214/600 (30d)          │
│  change any rule."                      │ [changes → Class C protocol]     │
│ L2 criteria: 4/6 met  [details]         │                                  │
├ KILL SWITCH ────────────────────────────┼ REAL-MONEY LOCK ─────────────────┤
│  ● ARMED (not engaged)                  │  🔒 UNREACHABLE (structural)     │
│  [ENGAGE — one click + type ENGAGE]     │  REAL_TRADING_ENABLED=false      │
│  engaged: everything halts except       │  ALLOW_REAL_ORDERS=false         │
│  monitor/protection. Release requires   │  mode=paper · flip = human,      │
│  reason, logged.                        │  out-of-band, never a UI action  │
├ GOVERNOR (post-crossing) ───────────────┴──────────────────────────────────┤
│  NORMAL · MTD −1.2% · throttle at −5% (risk halves) · halt at −8%          │
└────────────────────────────────────────────────────────────────────────────┘
```

Principles: the **"may alone / may not alone"** plain-language panel is generated
from actual settings+level, not hand-written (mode-confusion prevention); the
real-money lock has NO unlock affordance in the UI — its state is display-only by
design and says so; limit *changes* are not editable fields but a link to the
Class C protocol; kill-switch engage is one click + typed confirm, release requires
a reason string (both logged to system_events, as the current dashboard already does).

## 11. Evolution path (PR8 foundation → autonomous AlphaOS)

- **Now → PR9–11 (Console v1, Streamlit):** add Tonight tab (consumes the PR11
  brief dict) + annunciator strip (sidebar → top strip: mode, kill, heartbeat age,
  approvals count) + Positions tab upgraded to health cards. Keep the four existing
  tabs; rename Candidates → Decisions with the funnel + reason codes.
- **PR12–15 (Learning surfaces):** Learning tab (TQS/attribution/hypotheses/journal
  panels), Cards tab, bear-vote chips on Approvals, autonomy-readiness panel
  (`alphaos autonomy_readiness` rendered).
- **Crossing (M6–9):** LIVE mode styling (amber-bordered annunciator, "TIER-0"
  badge), governor block, drill-status page, live-vs-paper execution ΔR comparison.
- **L4/L5 era (M9+):** Autonomy console gains the auto-approval activity feed
  ("what it did alone today" — every autonomous action gets a feed entry with
  rung-ladder), sleeve allocation view, self-audit findings inbox.
- **Substrate:** stay on Streamlit until it demonstrably hurts (auth for remote
  access, mobile push-to-approve, or >1s render pain are the triggers); the IA
  above is substrate-independent. Push channel: ntfy (PR9) for needs-you events —
  the phone notification IS the mobile app until the crossing justifies more.
  Server-side read-only rendering + gate-checked actions remain law regardless of
  substrate.

## 12. What Sonnet builds first (UI-PR-A, alongside PR11)

1. Annunciator strip (top of every page): mode badge, autonomy label (static "L1"
   until PR15), kill-switch state+control (move from sidebar), heartbeat age (max
   `job_runs.finished_at_utc`), open-R + approvals count.
2. Tonight tab rendering `build_daily_brief()` — blocks ①②③④⑤⑦ (⑥ once gap
   arithmetic lands). Empty-state designed, not accidental.
3. Positions tab → health cards with the R-ladder (text/emoji ladder is fine in
   Streamlit v1; no charting library needed).
4. Approvals tab: add TTL countdown sort, exit-plan verbatim block, TQS
   score+confidence pairing (fields already surfaced by PR6/PR7).
5. Decisions funnel + hindsight column (attribution rows exist since PR8).
Defer: Cards/Learning tabs (need PR10/12 data), any charting, any styling system.

## 13. What to avoid (false confidence & clutter)

- **Equity-curve porn** on paper data — no compounding curves front-and-center
  until live floors are met; paper curves always watermarked "paper — upper bound".
- **Casino colors** — red/green as the dominant channel trains dopamine, not
  judgment; reserve saturated color for safety states (kill engaged, governor
  throttled, incident open).
- **Bare scores** — TQS without confidence, expectancy without n/floor, win-rate
  without expectancy: all banned by the §1.4 law.
- **LLM prose walls** as primary surfaces; **anthropomorphic confidence theater**
  ("AlphaOS strongly believes…"); **chat-with-your-portfolio** interfaces — this is
  a console, not a companion.
- **Real-time tick streaming / autorefresh anxiety** — the OS operates on scan
  cadence; the UI should breathe at the same tempo (manual refresh + heartbeat).
- **Vanity dashboards** — "AI calls made", "candidates scanned" as hero numbers;
  they belong in System, small.
- **One-click risk escalation** anywhere; **any unlock affordance** on the
  real-money lock; **notification spam** (alerts are ranked: incident > fuse >
  expiring approval > attention; everything else is digest-only).
- **Premature charts** below sample floors — a chart of n=7 is a lie with axes.

## 14. Written wireframes (remaining screens, compact)

Screens 1 (Tonight), 8-partial (governance) are wireframed above (§5, §10).

**Approvals**
```
┌ APPROVALS (2) — sorted by TTL ──────────────────────────────────────────┐
│ NVDA · long · catalyst_momentum_v1 · TTL ▓▓▓▓▓░░ 41m                    │
│  entry 142.10 · stop 138.60 (−1R=$85) · target 149.80 (+2.2R) · 24 sh   │
│  TQS 71 (conf 0.85) ▂▅▇ · missing: options_flow (unavailable)           │
│  driver: confirmed launch catalyst + interest 0.82 · earnings 9d ⚠      │
│  exit plan: stop 138.60 · target 149.80 · max 3d · invalidation:        │
│  "catalyst refuted or reclaim fails"                                    │
│  [Approve → confirm max loss $85] [Reject → reason] [rungs ▸]           │
├─────────────────────────────────────────────────────────────────────────┤
│ (expired/superseded today: 1 · shown collapsed, hindsight pending)      │
└─────────────────────────────────────────────────────────────────────────┘
```

**Decisions (funnel + hindsight)**
```
┌ TODAY  41 scanned → 12 shortlist → 8 evaluated → 3 PROPOSE · 6 REJECT   │
│        · 2 BLOCK(risk) · 1 EXPIRE          [vs typical: proposals +1]   │
│ filter: [rejects][blocks][watch][expired][all]     legend: ΔR>0 = the   │
│                                                    non-trade added value│
│ sym  card       decision  reason          hindsight                     │
│ TSLA cat_mom_v1 REJECT    label_conf_low  stop_hit → +1.0R ✓resolved    │
│ AMD  cat_mom_v1 BLOCK     wide_spread     pending (day 2/5)             │
│ MSFT cat_mom_v1 EXPIRE    ttl_2h          target_hit → −1.8R ✓resolved  │
│ ── weekly: misses that mattered (floor-gated) · gate table ▸ ──         │
└─────────────────────────────────────────────────────────────────────────┘
```

**Learning**
```
┌ [TQS] [Attribution] [Hypotheses] [Journal] ─────────────────────────────┐
│ TQS: bucket histogram ▁▃▆▃▁ · mean conf 0.74 · component availability:  │
│  interest 100% · ai_conviction 92% · narrative 41% · options 0% (n/a)   │
│ Attribution (live only · 38 mock excluded):                             │
│  propose_user_rejected/user  n=14/30 below floor — counts only          │
│  propose_blocked/gate        n=31 ✓ · mean ΔR +0.21 · sum +6.5R         │
│ Feed: "6/28 TSLA reject → stopped out → +1.0R saved [resolved]" …       │
└─────────────────────────────────────────────────────────────────────────┘
```

**System & Audit**
```
┌ SCHEDULER  scan ✓10:31 · monitor ✓10:44 · outcomes ✓09:00 · digest ✓EOD │
│  fuses: none · heartbeat 4m · cost 214/600 calls(30d) · debate 0/10     │
│ INCIDENTS  none open · history(3) ▸        KILL/DRILLS last: 6/30 ✓     │
│ LINEAGE EXPLORER  [paste any id…] → decision → config/model → raw rows  │
│ DATA QUALITY  mock share today 0% · degraded rows 2 ▸ · git 799d9cc     │
└─────────────────────────────────────────────────────────────────────────┘
```

## 15. Google Stitch prompt (copy-paste)

> Design a desktop web app called **AlphaOS Console** — the operator console for a
> controlled-autonomy AI trading operating system (supervised machine, not a
> brokerage app). Dark theme, calm and instrument-like: near-black background,
> high-legibility neutral grays, a single accent for interactive elements, and
> saturated color reserved ONLY for safety states (amber = live/attention, red =
> kill/incident). Dense but breathable layout, monospaced numerals, aviation-
> annunciator aesthetic — think cockpit instrument panel meets terminal, zero
> casino/crypto styling, no stock-photo finance imagery.
>
> **Permanent top annunciator strip on every screen:** MODE:PAPER badge ·
> AUTONOMY:L1 badge · KILL SWITCH: ARMED with a prominent one-click ENGAGE button ·
> GOV:NORMAL · heartbeat "♥ 4m" · "3 positions / 2.1R at risk" · "Approvals: 2
> (1 expiring 41m)".
>
> **Screen 1 — Tonight (home):** stacked blocks: (1) "THE ONE ACTION" hero card:
> "Approve or reject NVDA long before 22:31 (TTL 41m)"; (2) "Needs You" list (2
> items with countdown chips); (3) "Open Risk Now" compact panel with a worst-
> position mini R-ladder; (4) "Today's Machine Activity" funnel line "41 scanned →
> 12 shortlisted → 3 proposed · 6 rejected · 2 blocked" plus a tiny 5-bar
> histogram; (5) "Tonight's Brief" with collapsed rows why/bear/risk/exit;
> (6) "Moonshot Gap" single formula line with a "binding constraint: FREQUENCY"
> chip and a small "paper — upper bound ⚠" watermark.
>
> **Screen 2 — Approvals:** proposal cards sorted by TTL progress bars; each card:
> symbol+direction+setup-card name, entry/stop/target ladder with R annotations,
> "TQS 71" ALWAYS paired with an "evidence coverage 0.85" bar, greyed
> missing-evidence chips, verbatim exit-plan block, Approve (opens confirm modal
> restating max dollar loss) and Reject (reason dropdown) buttons, and a "why ▸"
> progressive-disclosure expander.
>
> **Screen 3 — Positions:** health cards with a horizontal R-ladder (−1R stop ●
> now ○ entry ▲ +2R target), thesis status chip INTACT/AT_RISK/BROKEN with an
> italic invalidation sentence, verdict chip HOLD/ATTENTION/EXIT_REVIEW (labeled
> "human decision — never auto-exited"), protection ✓, MFE/MAE small stats.
>
> **Screen 4 — Decisions:** a decision funnel header, a filterable table (reject/
> block/watch/expired) whose last column "hindsight" shows counterfactual chips:
> "stop_hit → +1.0R ✓resolved", "pending (day 2/5)" in neutral grey — never
> alarm-colored.
>
> **Screen 5 — Learning:** four tabs (TQS / Attribution / Hypotheses / Journal);
> attribution aggregates with "n=14/30 below floor — counts only" badges; a
> plain-sentence event feed.
>
> **Screen 6 — Autonomy & Risk:** two-column governance console: autonomy level
> with a generated "May alone / May NOT alone" plain-language panel; read-only
> hard-limits panel with "changes → Class C protocol" link (no editable fields);
> kill-switch panel with typed-confirmation ENGAGE; a real-money lock panel with a
> padlock icon, "UNREACHABLE (structural)" state and explicitly NO unlock control;
> a drawdown-governor bar (NORMAL / −5% throttle / −8% halt marks).
>
> Include empty/quiet states ("✓ Nothing needs you. Next scan 21:30. Heartbeat
> 4m.") and a MOCK-data watermark variant. No charts with tiny samples; no
> red/green P&L heroes; numbers wear their confidence everywhere.

---

*The console's job is trust with teeth: every pixel either helps the operator
supervise, audit, or stop the machine. Anything else is decoration — cut it.*
