# AlphaOS checkpoint audit — 2026-11-14 SGT

**Author:** Claude (strategist seat), at CK's request: audit Codex's and Claude's work to date, the
infrastructure, the failsafes, the strategy direction, and what must happen next for AlphaOS to
become profitable and autonomous.
**Reviewed state:** production checkout `29b7b85` on `main` (in sync with `origin/main`) **plus
uncommitted production code** (2 modified tracked files, 5 untracked modules — see §3).
**Method:** read-only. Production ledger queries, read-only broker and market-data calls, service and
log inspection, Codex's own status files. No writes to the ledger, no broker mutation, no code edits.
A full test-suite run on the current tree was started for this audit; its result is appended to the §9 row when it completes.

---

## 1. Bottom line

**The machine works. The strategy does not, and for two months nobody has been working on the strategy.**

- Execution and safety are in the best shape they have ever been: 12 trades since the September
  repairs, every one opened, protected and closed by the system without a manual intervention or a
  protection incident.
- The account is down **4.8 %** since inception (SPY: **+1.0 %**). 22 closed trades, mean
  **−0.23 R** per trade. No layer of the decision stack — scanner, evaluator, classifier — shows
  measurable selection edge in the data we already hold.
- At about 5 trades a month, live trading alone needs **2.5–3 years** to prove or disprove a modest
  edge. The mechanism built to shorten that (shadow labelling) has been switched off for 100 days.
- Since late September, engineering effort has gone into execution edge cases and research
  *plumbing* (provenance, an observer, a collection store that is not deployed). None of it moves the
  one question that decides profitability: **is there an edge, and where?**

The correct next move is not more hardening. It is to freeze infrastructure and run the cheapest
possible experiments that can say yes or no about edge.

---

## 2. Scoreboard (measured 2026-11-14)

| Item | Value |
|---|---|
| Broker equity | **$95,198.89** from $100,000 on 2026-06-18 → **−4.80 %**; max drawdown −5.09 %; flat, no open orders |
| SPY, same span | 746.75 → 754.27 = **+1.01 %** |
| Closed trades | 22 · net **−$4,886** · mean **−0.226 R** (SE 0.21) · 10 winners |
| Before 09-14 repairs | 10 trades · −$4,893 · mean −0.485 R |
| After 09-14 repairs | 12 trades · **+$7** · mean −0.010 R (SPY −0.85 % over the same span) |
| Exit mix | 9 stops (avg −1.15 R) · 4 targets (avg +1.36 R) · 6 time exits (avg +0.22 R, 5 of 6 positive) · 3 manual |
| Throughput | ≈5 trades/month; last 15 sessions: ≈40 AI evaluations/day → 5 proposals → 3 fills |
| Research registry | 15 pre-registrations, all quarantined since 09-13; **0 successors registered**; 0 promotions ever |
| Shadow tier | 39,923 completed shadow outcome rows, **0 AI labels**; suspend latch in place since 2026-08-06 |
| Code size | 71k lines app + 69k lines tests; +42k lines since 09-10; Codex workspace 71 GB |

Two readings of the trade record that matter:

1. **The post-repair flat result is the honest baseline.** The pre-repair losses include execution
   faults (a stale entry order filled six days late into an earnings gap, manual flattens). With
   those removed the strategy is break-even before costs in a flat market — i.e. no evidence of edge,
   and no evidence of a large negative edge either.
2. **Losses arrive overnight.** 8 of 9 stop-outs fired within about 30 minutes of an opening bell and
   4 of 9 opened *through* the stop (hence −1.15 R average instead of −1.00 R). Targets (+5–10 % in a
   week on mega-caps) were reached in 4 of 22 trades. With reward:risk ≈1.3 the strategy needs ≈43 %
   target hits to break even on target/stop outcomes; it is getting 4 targets to 9 stops. n is small —
   this is a hypothesis to test on historical bars, not a conclusion.

---

## 3. Infrastructure and architecture

**Sound (keep):**

- Broker-native GTC bracket on every position — protection survives a dead Mac.
- Process-isolated monitor: 1,388 of 1,388 passes completed in the last 14 days, ≈3 s each,
  unaffected by 15-minute research jobs.
- Time-exit lifecycle (TIME-2 as rebuilt by Codex): 6 of 6 natural exits clean — the path that left
  AAPL unprotected on 09-04 has not recurred.
- Durable entry intents, cumulative-fill accounting, fresh-account risk gate, append-only ledger,
  daily verified backup to iCloud.
- Paper-only boundary intact; limits unchanged (5 slots, 1 % risk, 3 % daily loss).

**Problems:**

| # | Finding | Why it matters |
|---|---|---|
| A1 | **Uncommitted code is running in production.** `scheduler/worker.py` and `data/providers/alpaca_data.py` are modified, and the research worker imports untracked modules (`research/swing_audit*.py`, `swing_data_policy.py`, `swing_session_evidence.py`, `data_freshness_trial.py`, `alphaos/monitoring/`). Codex's own status has called these "retained edits" since at least 11-01. | Not in git, not reviewed, not reproducible, lost on any checkout. This is the exact change-control failure the ceremony exists to prevent. |
| A2 | **Two services are permanently red.** Heartbeat exits 1 because a read-only "paper observer" reports 10 findings that Codex has itself diagnosed as observer bugs; observer alerts are disabled. Scheduler exits 1 on normal "not ready" skips. | A light that is always red carries no information. The next real failure will look identical. |
| A3 | **Complexity is outrunning the problem.** 140k lines to place ≈5 trades a month. Rounds 2–4 added four modules to recover entry failures that have occurred **zero** times live. | Every added path is a path to audit forever. The system's risk is now more in its own complexity than in the broker. |
| A4 | **Dead-man switch lives on the same Mac** it watches (known since the failure playbook: "T1's blind spot"). The Mac crashed once in September. | A dead machine pages nobody. Positions stay protected, but time exits and entries silently stop. |
| A5 | **Scans take 8.5 minutes on average (max 20)** and outcome bookkeeping runs ≈15 minutes every hour. | Tolerable today; it is the reason the monitor had to be isolated. Not a priority, but it is debt. |

---

## 4. Are the failsafes sound?

**For catastrophic loss on a paper account: yes.** Per-trade risk, slot count and daily loss are
enforced from live broker state, unknown state blocks entries, and every position carries broker-side
protection.

**Four real gaps, in order of importance:**

1. **No per-symbol or correlation cap.** In September the system held **three AMD positions at once**
   (3 of 5 slots, ≈3 % risk on one name). Nearly every trade is mega-cap tech or semis; five "independent"
   slots are closer to one or two bets. A single gap can breach the 3 % daily limit on its own.
2. **Stops do not bound gap risk.** Four of nine stops filled beyond the stop. "1 % risk" is a
   nominal figure; realised worst case so far is 2.05 %.
3. **Fail-closed has become fail-lost.** 4 of 18 proposals since the repairs (22 %) were permanently
   blocked by `ExposureUnavailable` — the fresh-exposure gate firing in the minutes after another
   entry, before the monitor had reconciled it. They were blocked, not deferred. (I recorded this
   class as a LOW observation in round two. That rating was wrong.)
4. **A1/A2/A4 above** — unreviewed production code, a meaningless health signal, and no external
   dead-man.

None of these needs new architecture. Each is a small, bounded change.

---

## 5. Is the strategy moving in the right direction?

**No — and the data to see that is already in the ledger.**

**5.1 No layer shows selection edge.** Live universe, one row per symbol-day, day-clustered:

| Group | n (symbol-days) | 10-day forward return | Bracket replay |
|---|---|---|---|
| All scanned | 1,478 | −0.0 % ± 0.3 | — |
| Evaluator says propose | 123 | +0.8 % ± 0.9 | +0.03 R ± 0.11 |
| Evaluator says reject | 1,163 | −0.0 % ± 0.4 | +0.02 R ± 0.05 |
| Final decision propose (what trades) | 32 | −1.5 % ± 1.2 | −0.11 R ± 0.18 |
| Evaluator propose, vetoed to WATCH | 115 | +0.9 % ± 0.9 | +0.07 R ± 0.12 |

Nothing here is statistically distinguishable from zero. That is descriptive, not confirmatory — but
after 4.5 months, "indistinguishable from zero everywhere" is itself the finding.

**5.2 The two AI components contradict each other, and the trades come from the contradiction.**

- When the primary evaluator says *propose*, the classifier vetoes it about **92 %** of the time.
- **19 of the 22 live trades** were entered when the evaluator said *reject* (13) or *watch* (6) and
  the classifier label overrode it (`LABELLER_DECISION_OVERRIDE_ENABLED=true`). Only 3 trades had
  both models agreeing.

The system is described as "AI evaluates, classifier confirms". It actually runs as "classifier
decides, using entry/stop/target levels from an evaluator that did not want the trade". Nobody chose
that design; it emerged. The vetoed evaluator proposals look no worse than the trades taken.

**5.3 The evidence program is stopped.** All 15 pre-registrations are quarantined (correctly — the
contracts were flawed) but no successor has been registered in two months. Shadow labelling — the
only designed route to sample size — has produced zero labels since 08-06. Codex reports feed coverage
now passes the 0.80 floor (82.6 %) and the canary is 20/20; the latch simply has not been lifted.

**5.4 The arithmetic.** To detect +0.2 R per trade (trade SD ≈1 R) at 80 % power needs ≈150–200
trades. At 5 a month that is 2.5–3 years. No amount of execution hardening changes that number. Only
two things do: (a) testing the rule-based parts on *historical* bars, which yields thousands of
symbol-days in an afternoon, and (b) testing the LLM parts *prospectively at scale* through the
shadow tier (LLMs cannot be backtested honestly — their training data contains the future).

---

## 6. Audit of the work itself

**Codex (ChatGPT).** The September repairs were real and valuable: the rebuilt exit lifecycle, monitor
isolation, durable intents and the research quarantine were all correct calls, and the live record
since proves the execution work. But after round five the stream drifted: learning-audit provenance,
a paper observer that now raises false findings, and since late October a bespoke "collection store"
with its own codec, capacity caps and synthetic 61-capture qualification runs — which failed at
capture 48 and is not deployed. By Codex's own status: 0 natural study samples, 0 measurements,
deadlines of 09-18 and 09-25 missed, scorecard still "partial". The work is careful and honestly
reported. It is aimed at the wrong target, and it is consuming CK's usage budget to build storage for
an experiment that has not been defined.

**Claude (me).** Four verification rounds found real defects (D1–D6, N1–N4) and Codex fixed them.
But I let the review loop set the agenda: rounds two to four went into ever-rarer entry-failure
states while the strategy question sat untouched, and I never stepped back to say so. Specific
misses: I under-rated the terminal-block class (§4.3) that then cost 22 % of proposals; "Decision 4"
(feed coverage), owed to CK since 09-04, was never delivered; the weekly research-inbox reviews have
been "thin week" nine times running and have changed nothing; and the power-derived floors I called
for after the H-AI-1 read on 09-10 were never built. The seven-lens review on 08-13 said "the system
has produced nothing"; three months later the sentence is still true of *evidence*.

**The process.** Two AI teams reviewing each other produced rigor on execution and zero ownership of
the outcome. Rigor is not progress when it is pointed at the part that already works.

---

## 7. What to do next (ordered)

**Phase 0 — housekeeping, one short session each, before anything else**

1. Commit or remove the uncommitted production code (A1). Nothing runs in production that is not in git.
2. Make health signals mean something (A2): fix or detach the observer; exit 0 on normal skips.
3. Per-symbol cap of one open position; `ExposureUnavailable` after a same-pass entry → deferral, not block.
4. External dead-man ping (any hosted heartbeat service) from the monitor role (A4).

**Phase 1 — find out whether there is an edge (2–4 weeks, no new infrastructure)**

5. **Freeze** the collection-store stream and all further execution hardening. The existing
   `candidate_outcomes` table and daily-bar store are sufficient.
6. **Historical bar study of everything that is not an LLM**, on the live plus shadow universe
   (daily bars from the existing free feed — about 900 days were already retrieved for the regime backfill):
   scanner interest score → forward return; exit geometry (current 1.3 R target vs time-only exit vs
   trailing stop); the overnight-gap pattern in §2. This answers in days what live trading answers
   in years.
7. **Lift the shadow latch** (coverage and canary both pass) so the LLM layers accumulate labelled
   decisions at ≈20–50 a day instead of ≈1 traded proposal every three days.
8. **Register one primary question, with a power-derived floor and a kill date**: do candidates the
   policy would trade beat same-day scanned candidates it would not, on 10-day market-adjusted return?
   Secondary, same data: does the classifier override add or subtract versus the evaluator alone
   (WATCH-1 already records this; 36 changed decisions so far).

**Phase 2 — act on the answer**

9. If no component shows edge by the kill date: retire `catalyst_momentum_v3` as an AI-gated strategy.
   Keep the platform; change the hypothesis (different horizon, different universe, or a rules-first
   strategy with the LLM as a veto only).
10. If something shows edge: simplify the decision stack to that component, raise throughput
    (e.g. 10 slots at 0.5 % instead of 5 at 1 %), and only then discuss real money.

**Working arrangement.** One owner per path, one direction of travel: strategy/spec and audit in one
seat, build in the other, and no deploy without a commit. Cancel or repurpose the weekly research
inbox; redirect that budget to Phase 1.

---

## 8. Decisions for CK

| # | Decision | Recommendation |
|---|---|---|
| 1 | Freeze Codex's collection-store work and further execution hardening; redirect to Phase 1 | **Yes** |
| 2 | Lift the shadow-label suspend latch (in place since 08-06) | **Yes** — both of its conditions now pass; costs a few dollars a day in API calls |
| 3 | Per-symbol cap = 1 and deferral instead of terminal block | **Yes** — tightens risk, recovers lost proposals |
| 4 | Set a kill date for the current strategy | **Yes** — propose 2027-01-15, with the Phase 1 question registered by 2026-11-28 |
| 5 | Keep live paper trading running meanwhile | **Yes** — it is the only end-to-end exercise of the machine and costs nothing; just do not read its P&L as evidence |
| 6 | Real money | **No.** Nothing in the record supports it, and nothing will before Phase 1 answers |

**Honest expectation.** The $200-a-week target is ≈10 % a year on this account, net of tool
subscriptions (Codex's economic trial already counts $150 a month). That requires a real, repeatable edge. There is currently no
evidence of one. The most valuable thing AlphaOS can do in the next month is find that out cheaply.
