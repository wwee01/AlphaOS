# AlphaOS PR Implementation Specs — PR9–PR15 (+ standing templates)

**Version 1.0 · 2026-07-05 · Fable 5 (retiring-trader handoff detail)**
**Companion to `alphaos-master-build-plan.md` (strategy) and `HANDOVER.md` (state).**

This document is the machine-drawing layer: implementation-ready specs for the
Ignition phase (PR9–PR11), tight skeletons for PR12–PR15, the reusable
spec/build/audit templates, and the house-patterns appendix. Rule of freshness:
**PR9–PR11 are spec'd in full because they are next. PR12–PR15 are skeletons on
purpose** — detailed specs written months ahead of the code they target go stale;
whoever builds them writes the full spec AT BUILD TIME against the then-current
code, using the template in §T1 and the skeleton here as the fixed intent.

Every PR below inherits, without restating: additive-only migrations
(`SCHEMA_VERSION` stays 3; `_reconcile_columns()` handles old DBs), StrEnums in
`alphaos/constants.py`, settings via `_get_bool/_get_int/_get` in
`alphaos/config/settings.py` + a config-hash category in
`alphaos/lineage/config_snapshot.py` when a new settings family appears,
fail-safe wrappers logging to `system_events`, the shadow-first law, full-suite
green before review, independent review agents + Opus audit before merge, merge
only on explicit human instruction.

---

## PR9 — TURN IT ON (unattended cadence + alerting + fuses)

> ✅ **SHIPPED 2026-07-06** — merged `85ae705` (branch commit `c6823b6`), Opus-audited
> (verdict APPROVE, no BLOCKER/HIGH/MEDIUM open), LaunchAgents installed AND activated
> the same day; first unattended ticks verified in `job_runs` (`trigger_source='scheduler'`).
> Suite 804/3/0. **As-built deltas vs this spec** (all deliberate, none scope-expanding):
> 1. Heartbeat "market hours" was defined as **any non-CLOSED session** (premarket/
>    regular/afterhours count; nights/weekends exempt) — user-decided.
> 2. `alerts.py` additionally gained **secret-value redaction + a 1000-char length cap**
>    applied before text leaves the process (audit finding: ntfy.sh is a new public
>    egress channel for exception text that used to stay local; redaction reuses
>    `lineage.hashing.SECRET_SETTINGS_FIELDS`).
> 3. Fuse dedupe is watermark-based (one `scheduler_fused` system_event per episode,
>    keyed off the last completed row) rather than a separate fuse-state row; a NULL
>    `finished_at_utc` edge case was audit-caught and hardened.
> 4. `cadence.is_fused()` returns `(fused, reason, streak)`; the fuse check lives in
>    `run_due_jobs()` only — the CLI `scheduler_run_job` deliberately bypasses it
>    (that IS the documented human reset procedure).
> 5. `deploy/uninstall_launchagent.sh` added (stopping must be the easiest action).
> 6. Two non-blocking audit follow-ups spun off: README note on fused-monitor
>    semantics; make `send_alert`'s never-raises literal (getattr on ntfy_topic).
> **Still open from §9.5 acceptance:** the 10-consecutive-trading-day unattended streak
> (clock started 2026-07-06; first scan window fires the next trading day), the
> kill-switch drill, and the failure-alert drill. ⚠️ The drills REQUIRE `NTFY_TOPIC`
> to be set in `.env` first — it was still empty at activation, meaning every alert
> path silently no-ops. Log drill results in `docs/incidents/` as dated notes.

**Goal:** 100% of trading days produce scans/fills/outcomes/TQS/attribution rows
with zero human initiation, with visible death (alerts) and self-limiting failure.

### 9.1 Architecture decision (made — do not relitigate)

Keep the brains in the scheduler, keep launchd dumb. ONE LaunchAgent fires
`scheduler_run_once` on a fixed short interval; `JobRunner.run_due_jobs()`
(`alphaos/scheduler/job_runner.py:158`) already decides due-ness per job type via
`cadence.py` (scan windows / monitor interval / outcomes interval / digest time),
holds the SQLite partial-index job lock (PR3), and respects the cost cap. Do NOT
build per-job plists or a daemon loop — the idempotent tick is the design.

### 9.2 Deliverables

1. **LaunchAgent plist** (template committed at `deploy/com.ck.alphaos.scheduler.plist`,
   installed to `~/Library/LaunchAgents/`): `StartInterval` 300s;
   `ProgramArguments` = [`<repo>/.venv/bin/python`, `-m`, `alphaos`,
   `scheduler_run_once`]; `WorkingDirectory` = repo root; `StandardOutPath`/
   `StandardErrorPath` → `/tmp/alphaos-scheduler.log`; `RunAtLoad` true. Follow
   the existing `com.ck.sgparser.daily.plist` house pattern (launchctl, not cron).
   Also commit `deploy/install_launchagent.sh` (copy + `launchctl load`) and
   document load/unload in README or HANDOVER §8.
2. **Alert sender** — new `alphaos/util/alerts.py`:
   `send_alert(settings, title, message, priority="default") -> bool`. POSTs to
   `https://ntfy.sh/{settings.ntfy_topic}` (topic setting EXISTS already —
   settings.py:228/927, secret-stripped in lineage). stdlib `urllib.request`
   only, timeout ≤5s, never raises (returns False + `system_events` WARNING on
   failure), no-op returning False when topic unset. Alerting must never block
   or fail a job — belt: try/except in sender; suspenders: try/except at call
   sites.
3. **Self-halt fuse** — settings `SCHEDULER_MAX_CONSECUTIVE_FAILURES` (int,
   default 3, validated 1–20 at load, SettingsError outside bounds — mirror
   `protection_check_error_escalation_threshold`'s pattern). In
   `run_due_jobs()`: before dispatching, count consecutive most-recent
   `job_runs` rows with `status='failed'` **per job_type**; if ≥ threshold,
   skip that job_type, log `system_events` ERROR `scheduler_fused`, send one
   alert (once per fused state, not per tick — dedupe by checking whether the
   fuse event already exists since the last successful run of that job_type).
   Fuse clears automatically after one successful manual `scheduler_run_job
   <job_type>` (human root-caused it) — document this as the reset procedure.
4. **Dead-man heartbeat** — new CLI `alphaos scheduler_health`: exit 0 if a
   `job_runs` row with `status='completed'` exists within the last
   `SCHEDULER_HEARTBEAT_STALE_MINUTES` (default 120) during market hours, else
   exit 1 + one alert. Second LaunchAgent
   (`deploy/com.ck.alphaos.heartbeat.plist`, StartInterval 1800) runs it. The
   heartbeat is a SEPARATE process on purpose — it must not share the
   scheduler's failure modes.
5. **Failure alerts** — in `JobRunner`, when a job transitions to `failed`:
   `send_alert(..., priority="high")` with job_type + error snippet. When the
   kill switch blocks a scan tick: NO alert (expected state), but digest notes it.
6. **Job-entry kill-switch verification** — already true via orchestrator gates;
   add the explicit regression test (engage kill switch → `run_due_jobs()` →
   zero orders/proposals; monitor/protection still run — PR2.5 doctrine says
   monitor must keep running).

### 9.3 Settings added

`SCHEDULER_MAX_CONSECUTIVE_FAILURES=3`, `SCHEDULER_HEARTBEAT_STALE_MINUTES=120`.
Both join `SCHEDULER_CONFIG_FIELDS` (existing category — value changes must
perturb `scheduler_config_hash`).

### 9.4 Tests (deterministic, direct-construction; NO wall-clock dependence)

- Fuse: inject 3 failed `job_runs` rows for `scan` → `run_due_jobs()` skips scan,
  fires `scheduler_fused` event once; monitor unaffected; a later completed scan
  row clears the fuse.
- Fuse alert dedupe: two consecutive fused ticks → exactly one alert attempt
  (monkeypatch `alerts.send_alert`, count calls).
- Alert sender: monkeypatch `urllib.request.urlopen` — success path, HTTP-error
  path (returns False, logs WARNING, never raises), unset-topic no-op.
- Heartbeat: fresh completed row → exit 0; stale/no row → exit 1 + one alert.
  Anchor rows via injected timestamps, never `now()` arithmetic on real rows
  (date-seeded-flake lesson).
- Kill-switch-at-job-entry regression (9.2.6).
- Behavior-neutrality: alerts module imported nowhere in decision paths (grep
  test, PR7/PR8 pattern).

### 9.5 Acceptance (from master plan, verbatim)

10 consecutive trading days unattended rows; one deliberate kill-switch drill;
one deliberate failure-alert drill (kill the venv path in the plist, watch the
alert arrive, restore). Log drill results in `docs/incidents/` as a dated note.

### 9.6 Non-goals

No new job types, no cadence changes, no autonomy change (approvals untouched),
no Telegram (ntfy only — one channel until it hurts), no daemonization.

---

## PR9.1 — HOTFIX: no-news prompt leak + date-flaky tests (added 2026-07-06, exit review)

**Goal:** stop contaminating the no-news baseline before the first unattended scan.
Found by the 2026-07-06 exit review (ML lens, reproduced by execution; founder
re-verified): `cand["_snapshot"]/_interest` (scanner) and `_catalyst/_last30/
_polarity/_earnings/_packet_id` (labeller stash) ride the candidate dict into
`openai.evaluate()`, and `build_no_news_user_prompt` serializes the WHOLE dict —
so the "no-news" eval sees catalyst/narrative text while being told none exists.

1. Strip `_`-prefixed keys at prompt construction (`ai/prompt_templates.py`) — the
   serialization site, so every current and future caller is covered; also removes
   the duplicated-snapshot token bloat. Check the review-prompt template for the
   same pattern.
2. Live-prompt-composition regression test: build the real prompt from a candidate
   carrying a sentinel string inside `_catalyst`; assert sentinel absent + public
   fields present. (Mock mode never builds prompts — that's why 800+ tests missed it.)
3. Fix the 2 date-flaky `test_decision_override.py` tests (organic-scan assertions,
   third occurrence of the §H.1 class) via deterministic direct construction.

**Non-goals:** the structural fix (typed ScanContext replacing the `_*` side-channel)
is deliberately NOT here — it's the pre-PR12 structural PR. This is the smallest
diff that makes tonight's data clean.

---

## PR9.5 — OPS & MEASUREMENT HARDENING (added 2026-07-06, exit review)

> ✅ **SHIPPED 2026-07-06/07** — merged `e075adb` (branch commit `841b787`,
> `feat/pr9-5-ops-measurement-hardening`), Opus-audited (verdict APPROVE, no
> HIGH findings), backup LaunchAgent installed AND activated, first real backup
> verified end-to-end. Suite 884/3/0. **As-built deltas vs this spec:**
> 1. `_backfill_benchmark_bars` pages through `_BARS_PAGE_SIZE`-sized chunks
>    (audit MEDIUM: a bare `get_daily_bars` call truncates silently past its own
>    200-bar default — a gap bigger than ~200 trading days would have trickled
>    in over many days instead of closing in one run). Re-verified adversarially
>    post-audit: a real 315-business-day gap closes in one call at the actual
>    production page size, zero regression on the normal 1-day path.
> 2. Backup activation hit a real macOS permission wall on first install (exit
>    126, `getcwd` "Operation not permitted") — the repo lives under
>    `~/Documents`, which `launchd`-spawned processes can't enter without an
>    explicit Full Disk Access grant. Fixed by the operator granting FDA to
>    `/bin/bash`; see house pattern §H.13 (new, written specifically because of
>    this). Not a code bug — no fix landed in the script itself.
> 3. Isolation (write-only, never read by any gate/eval/risk/execution path) and
>    the config-hash/cost-guard/schema-additivity claims were each independently
>    re-probed by a second audit pass (real multi-process `PYTHONHASHSEED` runs,
>    pre-PR9.5-schema DB reopen, in-memory cost-guard construction) — all
>    CONFIRMED, nothing broke.
> **Still open:** the operator's own quarterly restore-drill (README has the
> 3-step command) — automation writing backups isn't the same claim as a human
> having confirmed one restores. Not blocking; tracked separately.

**Goal:** the unattended system can page a human, survive a dead disk, and measure
the only question that matters (vs S&P) — before any new intelligence layer.
Consolidated from the exit review (docs/ALPHAOS_MASTER_REFERENCE.md §3/§5).

1. **Backup automation**: `deploy/backup_ledger.sh` + third LaunchAgent
   (`com.ck.alphaos.backup.plist`, StartCalendarInterval 05:30 SGT — US market
   closed): `sqlite3 data/alphaos.db ".backup ..."` (WAL-safe online API — never
   `cp`) → `PRAGMA integrity_check` gate → gzip → copy to iCloud Drive
   (`~/Library/Mobile Documents/com~apple~CloudDocs/AlphaOS-backups/`) → rotate 30
   daily/12 monthly → any failure = `send_alert` priority high. Document the
   quarterly restore drill in README + master reference §6.
2. **Benchmark spine** (measurement-only, additive tables): daily `equity_snapshots`
   row (paper account equity, timestamped) + `benchmark_bars` SPY daily series (the
   bars provider already exists) + a `relative_performance` report block: TWR equity
   curve, SPY total-return comparison, rolling beta, per-month alpha. Floor-gated
   like every other report. This starts the irreplaceable contemporaneous dataset —
   it cannot be backfilled honestly later.
3. **Cost-cap true-up**: `cost_guard` counts labeller + polarity calls too (today it
   counts only `openai_evaluations` — undercounts 2–3×); capture `resp.usage` token
   counts on every AI call row for real dollar accounting.
4. **Ops hygiene**: LaunchAgent logs → `~/Library/Logs/alphaos/` (both plists; /tmp
   is purged on reboot); commit `requirements-lock.txt` (pip freeze); fix
   `config_versions.config_hash` builtin-`hash()` → `lineage.hashing.stable_hash`;
   installer validates the python path before `launchctl load`.
5. **Operator prerequisites** (user-only, day-1 items — ✅ all done 2026-07-06
   except the drills): `NTFY_TOPIC` set + phone subscribed; `MAX_PAPER_TRADES_PER_DAY`
   confirmed intentional at 1000000 (NOT drift — `.env`'s own comment says
   "removed per operator request"; do not re-flag this without new evidence);
   `sudo pmset -a autorestart 1`; `chmod 600 .env`. Still open: the three
   drills logged in `docs/incidents/`.

**Non-goals:** no new gates, no strategy changes, no universe changes, no autonomy
change. Tests: backup script against a temp DB (integrity + rotation), benchmark
math on constructed series, cost-guard counting probe, migration test for new tables.

---

## PR10 — SETUP CARDS v1 + EXIT-FIRST INVARIANT

> ✅ **SHIPPED 2026-07-07** — merged `0e5b3fa` (branch commit `6c4e6a1`,
> `feat/pr10-setup-cards-v1`), Opus-audited (two subagent passes + a direct
> Opus pass; verdict APPROVE, no findings, no bypass). Suite 909/3/0.
> **As-built deltas vs this spec:**
> 1. New dependency **pyyaml** (this project's first — `dependencies` was
>    previously empty). Chosen over hand-rolling a YAML subset or switching
>    cards to JSON so they stay genuinely hand-editable + comment-able;
>    loaded via `yaml.safe_load` (no arbitrary-code-exec surface).
> 2. `seed_demo()` was a 5th proposal-creation site not named in §10.3's
>    stamping list — caught because the exit-first check broke 2 existing
>    demo tests once live. Now stamped like the other four.
> 3. `_execute()`'s completeness check uses `if not value` (falsy), not
>    `is None` — deliberate: `TradeProposal.from_row()` hydrates a NULL
>    `max_holding_days` to `0`, which `is None` would miss. No real proposal
>    has a legitimate 0-day hold (swing floors at 3; daytrade is dead code).
> 4. `tests/conftest.py`'s `make_proposal()` gained a `with_card=True`
>    default so every pre-existing execution-path test stays green.
> 5. by_card report slices: attribution v2 via a `LEFT JOIN candidates`
>    (audit-verified neutral — `candidate_id` UNIQUE forbids fan-out);
>    digest TQS via a separate additive join (main histogram byte-identical).

**Goal:** the versioned join key for the whole learning loop; every candidate/
proposal stamped with the card that produced it; exits written before entry, by law.

### 10.1 Architecture decisions (made)

- Cards are **declarative YAML in-repo** (`alphaos/cards/*.yaml`) — reviewable,
  diffable, versioned by git — PLUS a **`setup_cards` DB registry** synced at
  orchestrator startup (idempotent upsert keyed by `(card_id, version)`), so
  every ledger row can join without filesystem access. Registry rows are
  append-only per version; content changes REQUIRE a version bump (enforced:
  same `(card_id, version)` with different `content_hash` → refuse to start,
  loud SettingsError — a silently mutated card is the exact failure mode
  Prime Directive 7 exists to prevent).
- v1 ships with exactly ONE card: `catalyst_momentum_v1` — a faithful
  transcription of the CURRENT pipeline behavior (existing scanner thresholds,
  freshness/risk gates as references, current bracket policy). PR10 changes NO
  decision behavior; it makes existing behavior addressable. The A/B test must
  prove scan decisions are byte-identical with cards on/off.

### 10.2 Card schema (YAML fields, v1)

```yaml
card_id: catalyst_momentum_v1
version: 1                      # int; bump on ANY content change
name: "Catalyst Momentum (long)"
state: live_eligible            # shadow | paper | live_eligible | retired
direction: long
holding_class: swing            # swing | intraday (future)
regimes: [any]                  # future: [trend_up, chop, ...]
required_evidence:              # documentary, checked against candidate fields
  - interest_score >= settings.interest_min_score
  - freshness gates pass
  - risk gates pass
entry_rule: "as produced by evaluator within freshness/drift gates"
stop_rule: "evaluator stop; risk engine validates"
target_rule: "evaluator target; min RR enforced by risk engine"
invalidation_rule: "catalyst refuted, or thesis condition broken, before stop"
max_holding_days_default: 3
expected_tqs_profile:           # documentation for learning, not a gate
  interest_strength: ">= 0.5"
notes: "v1 = transcription of pre-card pipeline; no behavior change."
```

### 10.3 Schema (DB, additive)

- New table `setup_cards`: `id, card_id TEXT NOT NULL, version INTEGER NOT NULL,
  name, state TEXT NOT NULL, content_hash TEXT NOT NULL, content_json TEXT,
  lineage_id, created_at_utc, created_at_sgt` + UNIQUE index on
  `(card_id, version)`.
- New columns: `candidates.card_id`, `candidates.card_version`,
  `trade_proposals.card_id`, `trade_proposals.card_version`,
  `trade_proposals.invalidation_reason TEXT`.
- Stamping: wherever `playbook_name` is set today (scanner/orchestrator/
  `_override_open_trade`), stamp card fields alongside; override-created
  proposals stamp the same card with `setup_classification='user_override'`
  unchanged.

### 10.4 Exit-first invariant (named: **“no entry without a written exit”**)

`invalidation_reason` populated on every NEW proposal from the card's
`invalidation_rule` (deterministic copy — NOT an LLM call in v1). Enforcement at
the `_execute()` chokepoint (the PR6 pattern — every submission route funnels
there): entry/stop/target/max_holding_days/invalidation_reason all present, else
`OrderResult(blocked=True, reason=EXIT_PLAN_INCOMPLETE)` — a new ReasonCode.
Legacy rows (pre-PR10, NULL invalidation_reason) are grandfathered EXCEPT at
submission: a legacy proposal reaching `_execute()` without a written exit is
blocked like any other (fail-safe direction: block, never wave through).

### 10.5 Reporting

`tqs` and `attribution` report blocks gain optional `by_card` slices (GROUP BY
via join on proposal/candidate card_id; floors per slice = 20, existing
`MIN_RESOLVED_FOR_V2_SUBSLICE`). Digest `proposal_lifecycle` rows show card_id.

### 10.6 Tests

Registry sync idempotent (double startup → no dupes); mutated-content-same-
version → startup refuses; every scan candidate + proposal stamped (100%
coverage query test); `_execute()` blocks exit-incomplete proposals (direct
probe, both missing-invalidation and missing-target variants); behavior-
neutrality A/B (cards registry on/off → byte-identical decision artifacts);
migration test (old DB gains table+columns); by_card slices floor-gated.

### 10.7 Non-goals

No card promotion machinery (PR13), no second card, no regime conditioning, no
LLM-derived invalidation text, no gate threshold changes.

---

## PR11 — DAILY BRIEF + PORTFOLIO HEALTH

> ✅ **SHIPPED 2026-07-07** — merged `1656b3b` (branch commit `b530ac8`,
> `feat/pr11-daily-brief-portfolio-health`), two Opus-subagent audit passes +
> a direct Opus main-loop verification; verdict APPROVE. Suite 946/3/0.
> **Backend only** — UI-PR-A (dashboard annunciator/Tonight tab) deferred as a
> separate follow-up per operator decision. **As-built deltas vs this spec:**
> 1. `position_health.py` gained a 4th AT_RISK trigger not named in §11.1: a
>    degenerate risk basis (a live price IS available but current_r is
>    uncomputable because stop_price is missing or == entry) surfaces as
>    AT_RISK, never silently INTACT ("unknown must never read as fine" --
>    audit-caught). thesis_status's BROKEN still triggers ONLY on an open
>    protection incident (no live invalidation-condition detector in v1; that
>    would mean re-running enrichment against current market state, explicitly
>    out of scope -- inherits PR10's "no LLM-derived invalidation text").
> 2. `_one_action`'s EXIT_REVIEW symbol list is capped (MAX_SYMBOLS_IN_ONE_
>    ACTION=5, then "+N more") -- an unbounded join could exceed alerts.py's
>    1000-char cap and truncate mid-ticker at large position counts
>    (audit-caught; risk engine caps concurrent positions well below this in
>    practice, so it's a defensive floor).
> 3. The digest position_health summary and the brief's own positions_health
>    both call assess_positions() (a deliberate double-compute -- open
>    positions are few, builds once daily). Documented live-mode caveat: the
>    two uncached snapshot fetches can disagree at a verdict boundary, so the
>    two histograms should not be assumed to always agree to the row (cosmetic
>    -- nothing gates on either count).
> 4. Regression fixed in the same PR: `test_scheduler.py`'s fuse-realert test
>    force-mocked every job type "due", which now also runs daily_digest (which
>    always sends its own brief alert), inflating an exact alert-count
>    assertion -- scoped that one test's mock to only force 'monitor' due.
> **Still open:** UI-PR-A (the dashboard consumes the same brief dict; see
> UI/UX doc §5) -- deferred, tracked as the next UI item.

**Goal:** the daily human interface: what needs you, what the machine did, what
it learned, the one action — plus per-position health with thesis validity.

### 11.1 Deliverables

1. **Health engine** — new `alphaos/reports/position_health.py`:
   `assess_positions(journal, settings, market) -> list[dict]`. Per open
   position: `current_r` (from `position_manager`'s unrealized-R logic —
   REUSE, do not reimplement), `distance_to_stop_r`, `distance_to_target_r`,
   `thesis_status` (INTACT / AT_RISK / BROKEN — v1 deterministic:
   BROKEN if protection incident open or invalidation condition flagged;
   AT_RISK if earnings inside hold window (PR5 flags) or current_r ≤ −0.5;
   else INTACT), `verdict` (HOLD / ATTENTION / EXIT_REVIEW — EXIT_REVIEW never
   auto-exits; it is a human flag), `ttl/protection/freshness` statuses,
   `days_held` vs `max_holding_days`. Pure read.
2. **Brief builder** — new `alphaos/reports/daily_brief.py`:
   `build_daily_brief(journal, settings, kill_switch) -> dict` composing:
   market condition (regime fields if present, else index snapshot; consume PR9.5's
   `relative_performance` block — the vs-S&P line is the north-star metric),
   needs-you block (pending approvals w/ TTL seconds remaining, open
   incidents, fused jobs), positions health (from #1), today's machine
   activity (scan/proposal/reject/block counts from digest), best candidate
   (top TQS-scored PROPOSE today w/ score+confidence+missing components),
   what-AlphaOS-learned (up to 3 newly RESOLVED attribution rows rendered as
   plain sentences — reuse report language rules: aggregate tone, no
   moralizing), moonshot gap block (monthly: expectancy × frequency × risk
   arithmetic vs 10% target, binding constraint named; weekly: data-progress
   toward floors), ONE action item (priority order: incident > fused job >
   expiring approval > EXIT_REVIEW position > below-floor data note > "nothing
   needs you"). Always-present caveat strings carry through from source
   reports.
3. **Render + delivery**: `render_markdown(brief)`; CLI `alphaos brief`;
   scheduler `daily_digest` job additionally builds the brief and sends a
   compact version via `alerts.send_alert` (title = the one action item).
   Dashboard Tonight tab consumes the same dict (see UI/UX doc §5).
4. **Digest**: add `position_health` summary counts to the digest dict
   (mirror `tqs_shadow` shape).

### 11.2 Tests

Health verdicts by direct construction (position + injected snapshot →
INTACT/AT_RISK/BROKEN each); EXIT_REVIEW never touches orders (grep + behavior
test); brief always renders with every key present on an EMPTY journal (the
empty-state is a first-class case); one-action priority ordering (construct
competing conditions, assert order); alert send on digest job (monkeypatched);
gap arithmetic unit tests (known inputs → known implied monthly %); no decision
path reads brief/health modules (grep test); floors/caveats present in payload.

### 11.3 Non-goals

No auto-exits from health verdicts, no Telegram, no charts, no intraday
refresh loop (brief is a daily artifact; the dashboard reads live tables for
intra-day state).

---

## PR12–PR15 — skeletons (full spec at build time via §T1)

### PR12 — Hypothesis Engine v0 + pre-registration registry
Table `hypothesis_proposals`: `hypothesis_id, card_id, card_version,
proposed_diff_json, risk_class TEXT CHECK IN ('A','B','C'), claim TEXT,
evidence_json, success_metric TEXT, success_floor REAL, min_sample INTEGER,
min_span_days INTEGER, frozen_at_utc, status
(proposed|testing|met|failed|withdrawn), resolved_at_utc, lineage_id, …`.
Nightly scheduler job `hypothesis_pass` (cost-capped, schema-forced agent
output) reads attribution/TQS/outcomes/rejects → INSERTs proposals.
**Frozen-at-insert criteria; a later UPDATE to success_* fields is forbidden
by trigger-style test.** Report block + digest counts. Zero decision surface;
risk-class enumeration lives in `constants.py` (`HypothesisRiskClass`).

### PR13 — Promotion/Demotion state machine
Table `promotion_decisions` (append-only): card_id/version, from_state,
to_state, direction (promote|demote), trigger (auto_floor_breach|manual),
evidence_json, decided_by, lineage_id. Card state transitions ONLY via
`alphaos/cards/promotion.py`; auto-demote job (rolling per-card ΔR/expectancy
floor breached → demote + alert); promote requires floors met AND
`decided_by != 'system'` (tested). Class C → NightDesk memo reference field
REQUIRED (`research_ref` non-null when risk_class='C'). CLI `alphaos cards` /
`card_promote` / `card_demote`.

### PR14 — Red-Team Debate v0 (shadow)
Table `agent_votes`: vote_id, proposal_id, candidate_id, agent_role ('bear'
only in v0), stance (oppose|neutral|support), conviction REAL 0–1,
failure_modes_json (top-3), invalidation_triggers_json, model/prompt lineage,
is_mock, cost fields. Invoked batch-at-scan-end for PROPOSE decisions only
(the PR7 call-site pattern, after commit), capped `DEBATE_MAX_CALLS_PER_DAY`
(default 10) inside the PR3 cost budget. Pre-registered evaluation hypothesis
shipped WITH the PR (via PR12 registry): "proposals with bear stance=oppose &
conviction ≥0.7 underperform by ≥0.3R over n≥30, 28d" — expansion to a triad
is gated on that resolving TRUE.

### PR15 — First autonomy promotion (L3: bounded auto-approval, paper)
No new machinery — an EVIDENCE + DRILL + AUDIT gate around flipping
`APPROVAL_MODE=auto_within_bounds` with existing caps
(`MAX_AUTO_APPROVALS_PER_DAY`, risk caps, TTL `_execute()` chokepoint, PR6
auto-path guard). Deliverables: the L3 entry-criteria checklist as an
executable report (`alphaos autonomy_readiness`), the drill script (born-
expired proposals in AUTO mode → zero fills — re-run of the PR6 adversarial
probe), the Opus audit, HANDOVER §10 autonomy-state note. Live-eligible cards
only (join on `setup_cards.state`).

---

## T. Standing templates

### T1 — Spec template (architect pass; used for every PR)
```
A. Definition & one-paragraph goal (what measurement/capability exists after)
B. Scope: exact files/modules touched; explicit list of what is NOT touched
C. Formulas / rules (exact, with worked examples incl. both signs)
D. Storage: DDL sketch, indexes (NULL-uniqueness analysis MANDATORY for any
   uniqueness), new columns, migration note
E. Lifecycle & wiring: call sites (file:function), ordering guarantees,
   fail-safe behavior, idempotency mechanism (belt + suspenders)
F. Settings + config-hash category; defaults + validation bounds
G. Missing-data policy table (condition → status; unknown never zero/safe)
H. Mock/demo policy (is_mock derivation, exclusion from aggregates)
I. Reporting/CLI/digest additions; floors; caveat language
J. Lineage anchoring (source-decision, not compute-time)
K. Test list: per-scenario direct-construction; behavior-neutrality A/B;
   no-read greps; idempotency + IntegrityError probes; migration; fail-safe
   injection; empirical adversarial probe of THE central claim
L. Acceptance criteria (numbers, not vibes)
M. Non-goals + split warning
```

### T2 — Build report-back (Sonnet pass)
Files changed · schema changes · full-suite result (exact counts) · example
rows per new artifact · how idempotency/immutability/no-read/unknown-never-
zero/mock-exclusion were each PROVEN (test name or probe) · deviations from
spec with rationale · known gaps.

### T3 — Opus audit rubric (per merge)
Scope control → behavior neutrality (A/B + grep, non-vacuity checked) →
formula correctness vs spec (worked examples re-derived) → missing-data/
unknown-never-zero adversarial probes → schema/NULL-uniqueness IntegrityError
probes → source-table immutability (hash before/after) → circularity check
(new layer reads no other shadow layer) → mock/demo → reporting language +
floors → fail-safe injection → lineage → secrets sweep in *_json → tests
adequacy (incl. reviewer's own mutation test) → non-goals → verdict A–H with
findings by severity; every safety-critical claim verified empirically, never
by testimony.

### T4 — Merge protocol
Full suite green → independent review agents (3–5, parallel) adjudicated →
Opus audit verdict approve → fixes re-verified → **merge only on explicit
human instruction** → post-merge fresh full-suite run on main.

---

## H. House patterns appendix (tribal knowledge, written down)

1. **Date-seeded mock data**: mock prices seed per `{symbol}:{market_date()}`;
   `MockDataProvider` hardcodes `market_session=REGULAR` for determinism. Tests
   must NEVER depend on what a natural mock scan happens to produce (two flaky
   tests paid for this) — use `inject_pending_proposal` / direct row
   construction; never probe exactly at a computed price/session boundary;
   read session from the snapshot dict, never live `timeutils.market_session()`
   in decision-adjacent code.
2. **SQLite NULL-uniqueness**: plain UNIQUE with nullable columns does NOT
   dedupe (every NULL distinct). Recipe: partial unique indexes per anchor
   (`...WHERE proposal_id IS NOT NULL`), belt `WHERE NOT EXISTS` pre-check,
   suspenders `except sqlite3.IntegrityError: skip`. Precedents:
   `idx_jobruns_lock_key_active`, `idx_tqs_*_unique`, `idx_attr_*_unique`.
3. **Additive migration**: append to `SCHEMA`/`INDEXES` in
   `journal/schema.py`; `_reconcile_columns()` ALTERs old DBs; indexes build
   after reconcile; `SCHEMA_VERSION` stays 3 for additive changes; always add
   the old-DB migration test.
4. **Enricher / pure-compute split**: I/O module builds plain inputs; pure
   module computes (no clock/DB/RNG); orchestration module wires fail-safe.
   Precedents: earnings provider/enricher, `tqs inputs/scoring/batch`,
   `attribution discovery/resolve/batch`.
5. **The `_execute()` chokepoint**: every broker-submission route funnels
   through `Orchestrator._execute()`; ANY invariant that must hold at
   submission (TTL, exit-first) is enforced THERE, regardless of caller.
   Never add a submission path that skips it.
6. **Shadow-layer proof kit**: behavior-neutrality A/B fingerprinting
   `trade_proposals/rejected_candidates/decision_adjustments/risk_checks`
   content (+ non-vacuity guard), `inspect.getsource` checks on named decision
   methods, raw-text greps on `risk/approval/execution/scanner`, zero
   orders/fills/positions assertions, full-table hash immutability.
7. **JournalStore.insert()**: auto-stamps `created_at_*`; dict/list values for
   `*_json` columns auto-JSON-encoded (NO sort_keys — determinism comes from
   construction order; pin it with a test if bytes matter); unknown columns
   raise (catches typos).
8. **Versioned formula constants**: `TQS_VERSION`/`ATTRIBUTION_VERSION`/card
   versions — never env-tunable, never retro-scored; changing weights/rules =
   version bump; cross-version aggregation invalid by definition.
9. **Reporting law**: floors before means; caveat strings always present;
   counts below floor; no per-event verdicts; no moralizing ("user was
   wrong"); no cross-type global aggregates; mock excluded with visible count.
10. **Audit discipline**: the auditor runs their OWN adversarial probes
    (scratchpad scripts, in-process monkeypatching, IntegrityError injections,
    table hashes) — passing tests are evidence, never proof. Findings ranked
    BLOCKER/HIGH/MEDIUM/LOW/NIT with reachability analysis; unreachable-today
    still gets fixed or documented.
11. **Live config truth**: the real deployment runs on code defaults unless
    `.env` overrides — check `.env` before assuming a feature is active
    (`EXECUTION_PROVIDER=alpaca_paper, ALPHAOS_MODE=paper, APPROVAL_MODE=
    manual, REAL_TRADING_ENABLED=false` as of this writing).
12. **Push-to-main** is authorized as standing workflow (`.claude/settings.json`)
    for `main` only; never force-push; feature branches merge `--no-ff` with
    the `Merge branch '...' — PRn Title` message convention.
13. **macOS TCC blocks LaunchAgents in protected folders**: a LaunchAgent whose
    `WorkingDirectory`/script/destination lives under `~/Documents`, `~/Desktop`,
    `~/Downloads`, or iCloud Drive (`~/Library/Mobile Documents/...`) gets silently
    blocked — `launchd` spawns the binary (e.g. `/bin/bash`) fresh, with none of
    an interactive Terminal session's inherited grants, so even `getcwd()` fails
    with "Operation not permitted." No error until the first fire (PR9.5's
    `install_launchagent.sh` preflight only checks the binary is executable, not
    that it can actually reach the working directory). Fix: the operator grants
    **Full Disk Access** to the exact `ProgramArguments[0]` binary (System
    Settings → Privacy & Security → Full Disk Access → + → Cmd+Shift+G to type
    the path, since `/bin` isn't Finder-browsable) — this is a one-time,
    human-only action; there is no command-line grant. AlphaOS's repo living
    inside `~/Documents` means every future LaunchAgent touching it will need
    this same grant on its own `ProgramArguments[0]`.
