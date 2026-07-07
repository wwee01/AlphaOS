# AlphaOS PR Implementation Specs — PR9–PR15 + final-review items (+ standing templates)

**Version 1.1 · 2026-07-08 · Fable 5 (final founding-team review, last session)**
**Companion to `alphaos-master-build-plan.md` (strategy) and `HANDOVER.md` (state).**

> **v1.1 (2026-07-08):** SC (ScanContext refactor) and UI-PR-A recorded as SHIPPED;
> the exit-review addendum (TASK-R / canary / shadow baseline / OPS-A / OPS-B /
> PORT-1) integrated under canonical names with the two-lane build order; PR12–PR15
> skeletons expanded per the 2026-07-08 Opus learning-loop audit + four-partner
> debate (verdicts recorded in the master reference §3.5/§9); new items EVAL-1 /
> INSTR-1 / EARN-1 / EXP-1 / COST-1 added. Original addendum archived at
> `docs/roadmap/alphaos-exit-review-addendum-specs.md`.

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

## SC — ScanContext structural refactor (punch #12)

> ✅ **SHIPPED 2026-07-08** — merged `5e39f6f` (branch commit `931de21`,
> `feat/scancontext-structural-refactor`). The `cand["_*"]` side-channel
> (exit-review T5) is gone: candidates travel as a typed `ScanContext` dataclass
> (`alphaos/scanner/scan_context.py`) whose `row` holds exactly the persisted
> candidates-table shape (always safe to serialize); every former `_`-key is a
> typed attribute; `__setitem__` raises on any `_`-prefixed key — the PR9.1
> prompt-leak bug class is now structurally impossible, with the prompt-builder
> strip retained as defense in depth. Enum-ified the 4 raw candidate-status
> literals (`CandidateStatus`). Ruff + mypy (loose: default rules,
> `check_untyped_defs`) added to CI as a lint job. Consequence for BASELINE
> below: its frozen rule reads the typed public fields, as the addendum required.

---

## UI-PR-A — Operator Console v1

> ✅ **SHIPPED 2026-07-08** — merged `c3eeefb` (branch commits `2dd1d75` +
> post-audit `449a0ff`). First build of the UI/UX doc §12 list, all five items:
> (1) permanent annunciator strip (mode · autonomy L1 · kill-switch state+control
> moved from sidebar · heartbeat age via a pure-read check that never alerts ·
> open R · approvals count); (2) Tonight tab rendering the PR11
> `build_daily_brief()` dict; (3) Positions tab → per-position health cards with
> a text R-ladder (EXIT_REVIEW labeled "human decision — never auto-exited");
> (4) Approval Center gains TTL soonest-first sort (unknown expiry sorts last,
> fail-safe), verbatim exit plan + `invalidation_reason`, TQS-with-confidence
> pairing; (5) Candidate Flow gains the hindsight ΔR column via a new batch
> `JournalStore.attribution_by_candidate()` (one query, no N+1), with
> pending/unresolvable rendered honestly (never 0R) and mock ΔR tagged `(mock)`
> (post-audit fix). Read-only-on-render preserved; every action routes through
> the same orchestrator methods and gates as the CLI. +67 tests; suite 959
> collected post-merge. **Follow-up raised by the addendum: OPS-A must land
> promptly — the console widened the dashboard's action surface.**

---

## Final-review integration (2026-07-08) — canonical names, lanes, order

The exit-review addendum (drafted 2026-07-07; archived verbatim at
`docs/roadmap/alphaos-exit-review-addendum-specs.md`) is integrated below,
renumbered per its own rebase note and upgraded with the 2026-07-08 final-review
findings (Opus learning-loop audit findings cited as A1–D4; four-partner debate
verdicts V1–V5 — both recorded in the master reference §3.5/§9). Canonical names,
replacing the addendum's placeholder PR9.6/PR9.7 labels:

| Item | Was | Lane / slot |
|---|---|---|
| TASK-R — retro-relabel of 2026-07-01 | TASK-R | B (any session's slack) |
| OPS-A — dashboard loopback bind | OPS-A | A1 (immediately, UI-PR-A follow-up) |
| OPS-B — off-ecosystem backup + env.enc | OPS-B | B |
| CANARY — model-drift canary | "PR9.6" | B — **must be live before EXP-1** |
| BASELINE — deterministic shadow baseline | "PR9.7" | A5 |
| PORT-1 — effective-N + FDR + preregistrations | PORT-1 | A3 (hard prereq of PR12) |
| EVAL-1 — offline eval harness (punch #13) | — | A2 |
| INSTR-1 — rel_volume + ATR micro-pack | — | A4 |
| EARN-1 — real earnings provider (punch #14 part) | — | A7 |
| EXP-0 — shadow-tier deterministic capture (no AI) | — | **A2 — start the dataset now** |
| EXP-1 — shadow small/mid catalyst universe (AI top-K) | — | A8 |
| COST-1 — execution-cost model v1 | — | Phase 3; gates expectancy-ladder rung 2 |

**Lane A (critical path, one build session at a time):** ✅ UI-PR-A → **A1**
OPS-A → ✅ SC → **A2** EXP-0 (deterministic shadow-tier capture — added
2026-07-08 late session, operator-directed) → **A3** EVAL-1 → **A4** PORT-1 →
**A5** INSTR-1 → **A6** BASELINE → **A7** EARN-1 → **A8** EXP-1 (AI labelling
on the tier EXP-0 has been capturing) → **A9** PR12 (registry-first) → **A10**
PR13 slice 1 (scoreboard + demotion) then slice 2 + PR13.5 → **A11** cards
v2–v3 → PR14 → Regime Engine v1 + COST-1 → portfolio-risk gates (Class C) →
PR15/L3 (evidence-gated; additionally blocked on the CRO restore-drill law).
**Lane B (parallel, any slack):** TASK-R · CANARY (before EXP-1) · OPS-B ·
BRIEF-FIX-1 (small: audit C4) · the operator's quarterly restore drill
(user-only; blocks L3).

Everything below inherits the same standing rules as the addendum: shadow/ops
only, nothing reads into any live decision path, schema changes additive only,
§H.1 test discipline, T4 merge protocol.

---

## TASK-R — Retro-relabel of the contaminated 2026-07-01 baseline

**Type:** one-off CLI task, not a standing job. Run once, keep the code.
**Depends on:** PR9.1 ✅ (satisfied). **Lane B — any session's slack.**

Produce clean AI labels for the 7 journaled 2026-07-01 candidates by replaying
their stored `packet_json` through the fixed prompt builder — (a) a clean
baseline row set, (b) live proof the T5 fix works on real inputs (the bug class
mock mode cannot catch). Bonus discovered in this review: the relabeled packets
are prime **CANARY-corpus and EVAL-1 golden-set candidates** — select from them.

1. CLI `python -m alphaos relabel --from 2026-07-01 --to 2026-07-01 [--dry-run]`.
   `--dry-run` prints fully composed prompts, **zero** OpenAI calls (operator
   eyeballs that no `_`-keys appear — post-SC this is structurally guaranteed,
   the eyeball is belt-and-suspenders).
2. Build prompts from stored `packet_json` exactly as stored — no re-scan.
3. Standard client path; **cost guard counts these calls**.
4. Persist as NEW rows in the existing evaluations table: full PR4 lineage
   stamp; new additive nullable column `relabel_of` (→ original evaluation id,
   NULL on all normal rows); raw completion stored verbatim.
5. One `system_event` per packet: `event=relabel`,
   payload={original_id, new_id, prompt_sha256}.

**Never:** modify/overwrite the original contaminated evaluations (append-only
law); touch any decision/outcome/attribution row; generalize into a relabeling
framework (operator-passed id lists/date ranges only).

**Tests:** `_catalyst`/`_polarity`-bearing packet → composed prompt clean
(through the relabel path specifically); `--dry-run` → zero network (client not
invoked); `relabel_of` set on new rows, originals checksum-identical
before/after; cost-guard accounting includes relabel calls.
**Acceptance:** dry-run inspected, then live; 7 new rows with `relabel_of`
populated; CLI prints an old-vs-new label diff table; no original row modified.

---

## CANARY — Model-Drift Canary

**Type:** small standing job + golden corpus. Shadow-only. **Lane B, but must be
live before EXP-1** (CRO: never multiply archived model output 10× while blind
to silent upstream model changes).

Detect silent upstream changes to the configured OpenAI model before they
contaminate weeks of ledger data: weekly replay of a frozen prompt set, alert on
drift. NOT the eval harness (EVAL-1 answers "which prompt is better?"; CANARY
answers only "did the upstream model change?") — but they share corpus
machinery, and EVAL-1 may consume the canary corpus.

1. **Golden corpus** `data/canary/` (committed to git — frozen fixtures, not
   runtime data; note `.gitignore` excludes only `data/*.db*` patterns, JSON
   commits fine): 12–20 operator-selected journaled real packets (post-PR9.1
   clean; prefer TASK-R's relabeled seven plus a spread across symbols and
   interest-score bands). JSON files + `MANIFEST.json` with sha256 each. Corpus
   versioned: any change = new `corpus_version`, never edited in place.
2. **Job:** new scheduler job type `canary_run`, weekly (Sun 10:00 SGT — market
   closed). Standard fuse + cost-guard coverage. Compose prompts with the live
   builder, call the live configured model, store full raw completions. Record
   per run: configured model name + every model-identity field the API exposes
   (`response.model`, `system_fingerprint` if present), token usage, latency.
3. **Storage (additive):** `canary_runs` (run id, ts, corpus_version,
   configured_model, response_model, fingerprint, n_prompts, cost fields) +
   `canary_results` (run id, packet sha, prompt sha, raw completion, parsed
   label fields).
4. **Drift tiers vs the pinned baseline run** (first post-merge run; re-pin via
   CLI): Tier 1 (page immediately) `response_model`/fingerprint changed, or any
   parse/failsafe rate change from 0 · Tier 2 (page) any categorical label
   field differs on ≥20% of packets (threshold in config, not code) · Tier 3
   (digest line) numeric mean shift beyond a configured band. Tiers exist to
   avoid crying wolf on sampling noise while never missing an identity change.
5. **Alerting:** ntfy on Tier 1/2 (`alerts.py`); always one digest line
   ("Canary: last run <date>, drift: none / TIER-n").
6. CLI `alphaos canary run|status|pin-baseline`.
7. **Lineage joint (audit D4):** attribution/moonshot reports must segment ΔR by
   `prompt_hash`/`response_model` and **refuse to aggregate across a canary
   Tier-1 boundary** (mirror of §H.8's cross-version law). A silent model shift
   that also moves ΔR must be attributable to the model, never to a card.

**Tests:** fixture completions → drift=none; changed `response.model` fixture →
Tier 1 alert (mocked); label flip 3/12 → Tier 2, 1/12 → none; corpus tamper
(sha mismatch) → loud job failure, fuse-eligible; cost guard counts canary
calls; cross-boundary ΔR aggregation refuses (D4).
**Acceptance:** two consecutive real weekly runs; forced-drift drill on a COPY
of the DB (never the live ledger) → page received; drill logged in
`docs/incidents/`.

---

## BASELINE — Deterministic Shadow Baseline (the "does the AI add R?" instrument)

**Type:** shadow measurement layer. Ported by design from NightDesk #81 (paired
AI-vs-deterministic forward measurement) — see PORT-1's method note. **Lane A5 —
as early as possible: it accrues paired data forward-only; every week without
it is unrecoverable evidence.** Depends on SC ✅ (typed public fields) and
PORT-1 (its pre-registration becomes `preregistrations` row #1).

⚠️ **Naming:** a legacy `baseline_outcomes` table already exists (old no-news
hypothetical-P&L tracker, `journal/schema.py:620`). This item's table is
`shadow_baseline_decisions` — distinct on purpose; never conflate them.

For every candidate packet sent to the AI labeller, also compute and journal a
frozen deterministic decision from the same inputs; attribution later computes
`ai_delta_r = replay_r(AI path) − replay_r(deterministic path)` per candidate.
This is the evidence gate for ever spending on bigger models.

1. **Three arms, not two (audit C2):** the original two-arm design (AI vs
   interest-threshold rule) is confounded — both arms condition on
   `interest_score`, which sits on both sides of the scanner gate
   (`candidate_scanner.py:158`). v1 therefore journals TWO frozen rules:
   `threshold_v1` (PROPOSE iff `interest_score ≥ X`, X pre-registered as the
   historical median interest score of AI-proposed candidates, computed at
   build time, frozen as a literal, stored as `baseline_rule_x` in reports) and
   `propose_all_v1` (PROPOSE every labeller-eligible candidate). The pair
   brackets the AI between "propose-all" and "interest-threshold" and exposes
   whether its value is selection or inherited from interest_score. The honest
   claim is **conditional** added-R (given a candidate reached the labeller) —
   the pre-registration must say so, never overclaim.
2. Inputs: only typed public `ScanContext` fields (post-SC). Bracket
   construction: the identical live function (one sizing formula law). Output
   per rule: {decision, bracket, rule_version, input_sha}.
3. **Storage (additive):** `shadow_baseline_decisions` (candidate id FK, ts,
   rule_version, decision, bracket fields, input sha, nullable `setup_card_id`
   join key — populated when the AI path assigned one, never read by the rule).
   Written by the orchestrator in the same tick, strictly AFTER the live
   decision fully resolves (ordering enforced by test).
4. **Counterfactual join:** extend the existing counterfactual outcomes job to
   produce `replay_r` for shadow-baseline PROPOSEs via the ONE replay engine,
   stored with `path='baseline_threshold'` / `path='baseline_all'` — labeled,
   never mixed. Note audit C1: replay idealizes fills (no gap risk); until
   COST-1 lands, every ΔR line carries "gross, gap-free upper bound" verbatim,
   and stop-hit rows are earmarked for the COST-1 gap-haircut re-statement
   (applied identically to all arms, versioned). Add `entry_fill_status`
   (assumed_filled/needs_review) so ΔR never credits a path that couldn't have
   entered.
5. **Estimator (audit A3 — replaces the addendum's naive CI):** paired mean ΔR
   with a **day-block bootstrap CI** (resample whole decision-days, 10k
   resamples, BCa; fallback: `se = sd(ΔR)/sqrt(effective_n)`). Store `n_rows`,
   `effective_n`, `ci_method` with every report row. One-sided test at the
   pre-registered floor, reported as `q` (PORT-1 FDR), not raw `p`.
6. **Pre-registration block** (= `preregistrations` row #1, per PD#4):
   hypothesis "AI adds ≥ +0.05R mean paired ΔR over `threshold_v1` on proposed
   candidates, conditional on labeller reach"; metric per #5; floors
   (min effective-N, min span); analysis-not-before date; rule v1 immutable —
   improvements are rule v2, a new pre-registered arm.

**Never:** read by the live combine/gates/execution (shadow law, import-graph
test per §H.6); a second replay engine; tuning any frozen rule after freeze.

**Tests:** shadow rows written iff an AI evaluation occurred; write ordering
(shadow never precedes live resolution); determinism across runs and
PYTHONHASHSEED; `_`-key-injected packet → identical output to stripped packet
(defense in depth even post-SC); `path` labels never aggregated with realized
rows in any existing report query; constructed clustered fixture → block
bootstrap CI wider than naive CI (assert the naive method would false-positive,
the clustered one doesn't); mock prices date-seeded, direct construction (§H.1).
**Acceptance:** one unattended week; `shadow_baseline_decisions` rows 2:1 with
AI evaluations (two rules); one paired ΔR reproduced by hand matches stored.

---

## OPS-A — Dashboard network binding (small, immediate — UI-PR-A follow-up)

The approval surface must not be reachable from the LAN — priority raised now
that UI-PR-A has SHIPPED and widened the dashboard's action surface.

1. Streamlit invoked with `--server.address=127.0.0.1 --server.headless=true`;
   pinned in config if a config file is used; installer writes it.
2. Startup guard in the dashboard entrypoint: read the effective bind address;
   if not loopback → red full-page refusal + **all action components disabled**
   (approve/reject/kill-switch release — including everything UI-PR-A added).
   Defense in depth: the flag protects, the guard verifies.
3. Dashboard runner/installer validates the flag; refuses to install otherwise.
4. HANDOVER §operating notes: remote access, if ever wanted, is an SSH tunnel
   (`ssh -L 8501:127.0.0.1:8501 ck@macmini`) — never a LAN bind, never
   port-forwarding.

**Tests:** mocked non-loopback address → action components not rendered/raise;
installer validation. **Acceptance:** `lsof -iTCP -sTCP:LISTEN | grep <port>`
shows `127.0.0.1` only; a phone on the same wifi cannot load the page; check
logged in `docs/incidents/` as a mini-drill.

---

## OPS-B — Off-ecosystem backup + encrypted `.env` (extends shipped PR9.5)

Break the single-ecosystem failure domain (everything lands in iCloud) and stop
`.env` recovery depending on a possibly-stale password-manager copy. **Lane B.**

1. Nightly PR9.5 job additionally encrypts `.env` → `env.enc` next to the DB
   backup, same dated folder, same rotation (DB backup and the config that ran
   it can never drift apart).
2. Monthly (1st, after the nightly succeeds): copy `{db.gz, env.enc, MANIFEST}`
   to a second target OUTSIDE Apple's account domain. Operator-configured:
   `BACKUP2_METHOD=rclone|disk`, `BACKUP2_DEST=…` (rclone → any S3/B2/GDrive
   remote; disk → external drive, loud alert if volume absent). Keep 12
   monthlies at the second target.
3. Encryption: `age -p` (or symmetric openssl); passphrase ONLY in the
   operator's head/password manager — never `.env`, never the repo. Encrypt the
   DB at the second target too (cheap); local plain `.backup` stays for fast
   restore.
4. MANIFEST per backup: sha256 of each artifact + schema_version + git rev;
   restore verifies shas before use.
5. Failure of either leg pages via ntfy; digest carries
   `Backups: nightly OK <ts> · offsite OK <date>`.
6. Quarterly restore drill (master reference §6) now includes decrypting
   `env.enc` and restoring from the SECOND target, not just iCloud.

**Tests:** mocked targets → artifacts + MANIFEST + sha round-trip; `env.enc`
decrypts byte-identical (fixture env, never the real one); second target absent
→ alert, nightly leg still completes; grep test — no passphrase/key material in
any captured log/journal output. ⚠️ New LaunchAgent legs hit §H.13 (Full Disk
Access) — read it first. **Acceptance:** one real restore from the second
target onto a scratch directory, logged in `docs/incidents/`.

---

## PORT-1 — Effective-N + FDR + preregistrations (the statistical-discipline layer)

**Lane A3 — hard prerequisite of PR12 AND of EXP-1's first aggregate** (audit
A1: today every floor gates on `len(rows)` — `reports/attribution.py:212–231` —
on a one-beta-cluster universe where 30 rows ≈ 3–5 independent bets; a
promotion decided on that is the false-edge machine).

**Port method (contract, not code — applies to all NightDesk ports):** extract
a portable design doc in the NightDesk repo from #85 (Thesis Research Layer)
and #81 (paired instrument) — inputs, outputs, invariants, formulas (FDR
procedure, effective-N rules, pre-registration fields), failure modes; **no
NightDesk code, no AlphaOS code — prose, schemas, math only** — saved as
`docs/roadmap/ported/nightdesk-stats-contract.md`. Then map each concept onto
AlphaOS's actual tables in an adaptation review; anything that doesn't map
cleanly gets a decision in the doc, not an improvisation in code. Build as a
normal T-process PR. Record lineage in the Decision Log. (BASELINE is the #81
port done this way; PORT-1 is the #85 port.)

1. **`effective_n(rows)`** — ONE shared function
   (`alphaos/stats/effective_n.py`): dedup to one observation per
   `(symbol, decision_date)`; overlapping `[decision_date, +max_holding_days]`
   windows on the same symbol cluster together. **Every floor call site
   switches from `len(rows)` to `effective_n(rows)`** — reports AND the PR13
   promotion gate consume the IDENTICAL function (one floor law, mirror of the
   one-replay-engine rule). Reports show both `resolved_count` and
   `effective_n` so row inflation stays visible.
2. **FDR gate:** Benjamini–Hochberg with the family defined explicitly (audit
   A5) as **all pre-registered hypotheses with `evaluated_at_utc` set to date**
   — cumulative, never per-render. `q_value` stored on the preregistration row
   at evaluation time (immutable, one-shot); reports read `q_value`, never
   recompute BH over ad-hoc slices. Digest prints `q=…`, not `p=…`.
3. **`preregistrations` table (additive):** hypothesis text, metric, floors
   (effective-N + span), `analysis_not_before`, `registered_at_utc`,
   `evaluated_at_utc` (nullable, **set exactly once** — a second write raises;
   audit A2's optional-stopping guard), `q_value`, immutable. PR12 writes here
   BEFORE any forward test; BASELINE's block migrates in as row #1.
4. **Ride-alongs (punch #15, researcher MEDs):** attribution touch-conditioning
   caveat text; candidate-level `max_favorable_*_r` anchoring — decide ONE way
   (0-anchor to match trade MFE, or rename `path_favorable_extreme_r` and
   document as signed-from-reference), version the change per §H.8, test on an
   all-adverse fixture (audit C3).
5. **Survivorship denominator (audit A4):** any report claiming system-level
   edge (moonshot gap, pivot-criteria evaluation) computes over the FULL
   preregistration family (promoted + demoted + withdrawn) and prints
   `hypotheses_tested=N, promoted=k` as a mandatory caveat line.

**Tests:** clustered fixtures (10 rows, 3 symbol-days → n_eff=3); BH on a
textbook vector → exact q's; two renders of the same evaluated set → identical
q (family stability); grep/AST — no remaining `len(` floor checks; one-shot
`evaluated_at_utc` write enforcement.

---

## EVAL-1 — Offline eval harness (punch #13, now with ground truth)

**Lane A2 — before any prompt/model change, and before PR12-era temptation
arrives.** `alphaos eval`: replay journaled `packet_json` through current
templates vs a frozen golden set; store raw completions on ALL paths INCLUDING
failures (`raw={"fail_safe": …}` rows are precisely the examples the harness
needs most — retention starts here).

**Ground truth (audit D1 — the piece the punch-list item was missing):** a
canary can only detect drift-from-baseline, not drift-from-correct; ΔR-based
quality takes years at current cadence. So: a small **operator-adjudicated
ground-truth set** — 20–30 golden packets where the operator records the
ex-post correct decision (with hindsight bars), stored as
`ground_truth_label` alongside the corpus MANIFEST. Both EVAL-1 and CANARY
score against it (accuracy, not just drift). This is the only instrument that
judges a prompt change in days instead of years — and it de-confounds BASELINE
(a labeller that agrees with ground truth but adds no R says the *edge* is
absent, not the labeller). Seed from TASK-R's relabeled packets + the cleanest
post-PR9.1 week.

Skeleton (full spec at build time): frozen golden corpus shared with CANARY
machinery; metrics = parse rate, label agreement vs ground truth, categorical
stability across temperature; every eval run lineage-stamped
(prompt/model/config hashes); report block + one digest line; zero decision
surface.

---

## INSTR-1 — Honest-instruments micro-pack (rel_volume + ATR stops)

**Lane A4 — the niche must be measured with honest instruments BEFORE its data
is archived (T5's lesson at universe scale).** Two small changes, both
behavior-affecting (Class A/B — NOT shadow; spec, review, and audit
accordingly; neither is a gate change):

1. **Time-of-day-normalized rel_volume:** replace today-cumulative ÷
   yesterday-full-day (reads 0.1–0.3 every morning — the volume trigger is
   structurally dead intraday, exit review T3) with cumulative-to-now ÷ 20-day
   average cumulative-to-same-time-of-day. The scanner's core catalyst signal
   starts meaning what it says.
2. **ATR-scaled stops** as `catalyst_momentum_v2` (version bump per PD#7,
   never retro-scored): a fixed 3% stop is ~4 daily sigma on SPY and <1 on
   TSLA — "1R" currently means different trades. Stop = k×ATR(14) with k
   pre-registered per card; risk engine still validates.

Pre-registered before/after instrumentation: H-WIN-1 (PR12 seed) audits the old
rel_volume's window bias from our own ledger; card v2 vs v1 is a clean
versioned comparison. **Non-goals:** no threshold retuning beyond the formula
swap, no new gates, no universe change (that's EXP-1).

---

## EARN-1 — Real earnings-calendar provider (punch #14 part)

**Lane A6 — defines "catalyst" for the niche; hard gate for card v2
(`earnings_reaction_drift_v1`).** Live vendor behind the existing PR5
`make_earnings_provider` factory — zero call-site changes by design. Missing/
stale/unavailable stays an explicit non-`ok` status (never silently safe).
Mock provider remains for tests; **mock ≠ real: no earnings-conditioned card or
hypothesis may go live on the mock provider.** Vendor choice at build time
(cost floor; the factory makes it swappable).

---

## EXP-0 — Shadow-tier universe capture, deterministic only (START THE DATASET NOW)

**Added 2026-07-08 late session, operator-directed ("the current universe is way too
small") — the PM dissent from verdict V1, upgraded with instrument-version labeling
to answer the quant's contamination objection.** The debate's concern was archiving
AI labels ranked by broken instruments; it was never about *collecting* deterministic
data. Snapshot + interest-score capture on a shadow tier is the benchmark-spine
argument again: contemporaneous data that cannot be backfilled honestly later. Every
week the 20-name book runs alone is unrecoverable niche data. **Lane A —
immediately after OPS-A; one build session; zero AI calls.**

Grounding facts (verified against code 2026-07-08): universe is a hardcoded 20-name
list (`candidate_scanner.py:38`); `UniverseTier` already defines unused
`WATCHLIST`/`EXPERIMENTAL` tiers (`constants.py:201`); snapshots fetch ONE HTTP call
per symbol (`market_data.py:57` loops singles) — a 500-name tier requires Alpaca's
batch endpoint; the free IEX feed is sparse on small/mids, which the freshness
guard will honestly mark — that sparsity is itself a measurement (see #6).

1. **Universe builder CLI** `alphaos universe_build` (one-off + quarterly refresh,
   never a scheduler job in v0): screen Alpaca's assets endpoint (tradable US
   common stock, exclude ETFs) → pull daily bars in batches → select the
   **$5–50M ADV(20d) band, price $5–100** (the master plan §7 capacity niche),
   target ~300 names to start (500 max). Output: a reviewed, committed
   `alphaos/universe/shadow_universe.json` (symbols + as-of date + screen
   parameters + sha) — git-versioned like a card; the operator eyeballs the list
   before it's committed. Refresh = new file version, old rows keep their version.
2. **Batch snapshots**: extend `AlpacaDataProvider` with
   `GET /v2/stocks/snapshots?symbols=…` (~100 symbols/call; ~5 calls per window
   for the full tier); `MarketDataClient.get_snapshots()` uses it transparently.
   Core-tier behavior byte-identical (A/B test).
3. **Shadow-tier scan pass**, same 3 windows: batch snapshots → freshness assess →
   deterministic interest score → journal `universe` rows with
   `tier='watchlist'` + candidates stamped `shadow_tier=1` (additive column).
   **NO AI labelling, NO enrichment calls, NO proposals from the shadow tier —
   structurally**: the proposal-creation path refuses `shadow_tier=1` candidates
   (chokepoint-style check + test), and the labeller is never invoked for them
   in v0. Zero OpenAI cost, zero decision surface, zero approval-queue noise.
4. **Instrument-version labeling (the quant's condition):** every shadow-tier
   candidate row stamps `instrument_version='pre_instr1'` until INSTR-1 lands,
   then `'instr1'`. Post-INSTR-1 analysis segments on it; pre-fix interest ranks
   are known-biased (dead intraday rel_volume) and labeled as such, never mixed.
5. **Digest additions** (floor-gated, counts only): shadow tier scanned N,
   fresh M, stale K, top-decile interest count — plus `feed_coverage`: fraction
   of shadow-tier snapshots with usable quotes on the free IEX feed. **This
   number decides empirically whether the SIP data upgrade (~$99/mo) is needed
   before EXP-1 — measure first, spend on evidence** (decision-log row added).
6. **Storage arithmetic** (fine for SQLite/WAL): ~300 symbols × 3 windows/day ≈
   900 snapshot rows/day (~25k/month); prune nothing — this IS the dataset.

**Non-goals:** no AI labelling (EXP-1), no rel_volume fix (INSTR-1), no
tradeability (ever, until per-card earned promotion much later), no new gates,
no scheduler cadence changes (the shadow pass rides the existing scan job).
**Tests:** builder screen math on constructed bars; batch-snapshot mapping parity
with the single path; shadow tier can never reach `_handle_proposal`/labeller
(grep + behavior probe); core-tier scan artifacts byte-identical with the shadow
tier enabled/disabled (§H.6 A/B); freshness/degraded statuses recorded honestly;
migration test for the additive columns.
**Acceptance:** one week of unattended shadow-tier capture; `feed_coverage`
number in the digest; operator-reviewed universe file committed; zero AI-cost
delta; zero proposals from the shadow tier.

---

## EXP-1 — Shadow small/mid catalyst universe (the payload)

**Lane A8 — the partners' verdict V1: the single highest-leverage change for
finding tradeable alpha, pulled forward from Phase 3, shipped only behind
EVAL-1/PORT-1/INSTR-1/EARN-1 (+ CANARY live).** Learnable trade flow is the
named binding constraint (master reference §1: the megacap book cannot validate
its own hypothesis on a useful timeline); a ~10× shadow universe is a ~10×
learning-velocity multiplier at zero decision risk (PD#2 — shadow symbols are
scanned/scored/attributed, never tradeable; tradeability is earned per-card
later). **EXP-0 (above) already started the deterministic capture; EXP-1 is
the AI-labelling layer on top of it** — by the time EXP-1 lands, the tier has
weeks of instrument-versioned interest/freshness history to pick its top-K from.

Skeleton (full spec at build time):
1. Universe: EXP-0's committed tier file (300–500 liquid small/mid names,
   $5–50M ADV band — the capacity niche institutions can't enter, master plan
   §7), `tier='watchlist'` + `shadow_tier=1` rows already flowing.
2. **Cost-tiered scanning (CRO condition):** deterministic pre-rank
   (interest-score family, honest rel_volume from INSTR-1) over the whole
   shadow tier → **AI labelling only for the top-K per window** (K in config;
   budget arithmetic in the spec vs the 2000/30d hard cap using PR9.5's
   true-up accounting). Naive full-universe labelling would be 15–25× current
   spend — refused by design, not by hope.
3. Spread/liquidity instrumentation for the band (the 1bps slippage + $2M
   floor assumptions are megacap-calibrated fantasy here — record honest
   spread/ADV fields on every shadow candidate for COST-1 to consume).
4. **Regime tag v0 (audit A6):** stamp `regime_tag` on candidate outcomes at
   decision time from a frozen, dumb, versioned classifier (SPY 20d
   realized-vol tercile × 50d trend sign — pre-registered, never tuned;
   Regime Engine v1 refines later). Cross-regime aggregates carry
   `regime_mixed=true` caveat; per-regime claims need per-regime effective-N
   floors.
5. Every aggregate over the new universe floor-gates through `effective_n()`
   from day one (correlated small-caps on the same catalyst day inflate row
   counts worst of all).
6. Fuse coverage, `is_mock`/shadow-tier exclusion from all live aggregates,
   BASELINE arms cover shadow candidates from their first day.

---

## COST-1 — Execution-cost model v1 (Phase 3; gates expectancy-ladder rung 2)

**Audit D2: the whole ladder gates on "positive net expectancy after calibrated
costs" and no PR owned the calibration** (`cost_calibration.py` is OpenAI spend,
not execution cost). Skeleton: per-symbol half-spread + ADV-scaled impact +
gap-slippage-on-stop, calibrated against actual paper fills vs mid (the
reconcile path already captures fills — join it); versioned; applied
IDENTICALLY in `replay_bracket` (audit C1's gap haircut:
`replay_r = −(1.0 + gap_penalty)` on stop-hits) and in realized R; calibration
window pre-registered. Until it lands, the ladder freezes at rung 1 and every
net-expectancy claim carries "uncalibrated — upper bound" verbatim. Feeds on
EXP-1's spread/ADV instrumentation; target ≥100 real paper fills (Phase 3
campaign).

**Card capacity ride-along (audit D3):** additive card fields
`max_position_adv_bps` + `max_concurrent_positions`; stamp realized
`entry_adv_fraction` on outcomes; capacity-decay caveat on any promoted-card
report when observed sizes approach the cap. Shadow-only until Phase 3.

---

## BRIEF-FIX-1 — Daily-brief reporting-law fix (small, Lane B)

**Audit C4 (MED, live in shipped code):** `daily_brief.py`'s "what learned"
block renders per-event ΔR sentences with no floor gate and no caveat
(`reports/daily_brief.py:121–139`) — violating §H.9 ("no per-event verdicts")
in the one artifact the human reads daily. Fix: aggregate, floor-gated language
only ("N decisions resolved today, M cumulative; aggregates in
`alphaos attribution` once floors met"); if per-symbol lines stay, strip the ΔR
number below the aggregate floor and attach the standing
`ATTRIBUTION_V2_CAVEAT` (already imported at `:20`, unused here). Test: single
resolved row → no ΔR figure in rendered markdown; caveat present.

---

## PR12–PR15 — skeletons (full spec at build time via §T1)

### PR12 — Hypothesis Engine v0: registry-first (REVISED 2026-07-08, verdict V2)

**Inversion:** v1 is the **pre-registration registry + resolver, seeded with 8
human pre-registered hypotheses**; the nightly LLM generator is deferred to
v1.1, cost-capped, and gated on the registry demonstrating it can resolve
hypotheses at all. PD#4's load-bearing part is the registry, not the generator
— at current N a generative agent emits plausible hypotheses the data cannot
resolve, and the registry fills with zombies. **Depends on PORT-1** (consumes
`preregistrations`, `effective_n()`, and the FDR gate).

- Table `hypothesis_proposals`: `hypothesis_id, card_id, card_version,
  proposed_diff_json, risk_class TEXT CHECK IN ('A','B','C'), claim TEXT,
  evidence_json, success_metric TEXT, success_floor REAL, min_sample INTEGER,
  min_span_days INTEGER, frozen_at_utc, status
  (proposed|testing|met|failed|withdrawn), resolved_at_utc, lineage_id,
  preregistration_id FK, …`. Frozen-at-insert; later UPDATE to success_*
  fields forbidden by trigger-style test. Risk-class enum in `constants.py`.
- **Test rigor is code-fixed, never model-authored (audit B4):**
  `success_floor`/`min_sample`/`min_span_days` are looked up from a frozen
  `constants.py` table keyed by `risk_class` (Class C strictest); an
  agent-supplied value is overwritten; test asserts stored == constant.
- **One-shot evaluation (audit A2):** each hypothesis evaluates exactly once,
  at/after `analysis_not_before` (`evaluated_at_utc` set once, second write
  refused). Early promotion attempt → `NOT_YET_ELIGIBLE`.
- Resolver: nightly job computes each testing hypothesis's metric via the SAME
  floor function reports use (`effective_n`, never `len(rows)`); resolution
  stores `q_value` from the PORT-1 cumulative FDR family.
- **Seeded v1 set (8, frozen at merge; full entry/floor sketches in the
  2026-07-08 debate record, master reference §9):** H-TQS-1 (top-vs-bottom
  quartile TQS ≥ +0.3R on 3d replay_r — precondition for TQS ever leaving
  shadow) · H-CAT-1 (catalyst presence ≥ +0.2R — the card family's core
  thesis; FALSE starts the §12 pivot clock) · H-INT-1 (interest-score top
  decile > median, q<0.10 — if FALSE the scanner ranking EXP-1 multiplies is
  noise) · H-WIN-1 (morning vs afternoon windows — the rel_volume audit on our
  own ledger) · H-TTL-1 (expired proposals' replay_r ≈ approved realized —
  TRUE is the evidence case for L3; FALSE kills the "approval is the
  bottleneck" narrative) · H-REJ-1 (operator rejections ΔR ≤ 0 — either way is
  gold) · H-POL-1 (polarity divergence underperforms — gates card v5) ·
  H-AI-1 (= BASELINE's pre-registration verbatim, `preregistrations` row #1).
  No earnings-conditioned hypothesis until EARN-1 (mock ≠ real).
- v1.1 generator (separate mini-PR): schema-forced agent output, fuse +
  cost-cap coverage like any job; hypothesis-volume cap is the L2 rollback
  trigger (master plan §5).
- Report block + digest counts. Zero decision surface, as before.

### PR13 — Promotion/Demotion state machine (REVISED 2026-07-08: demotion first)

Ship in two slices. **Slice 1 — the per-card scoreboard + auto-demotion (the
safe half; may ship before slice 2):** a deterministic rolling per-card ledger
(expectancy, ΔR, effective-N vs floor, span vs floor) computed by the outcomes
job, rendered in brief/digest/Cards tab, wired to `auto_floor_breach → demote +
alert`. Partners' verdict V4: this is the smallest mechanism that closes the
loop at the actuator — today every measured fact terminates in the brief's
prose and changes nothing. Demotion satisfies PD#3 with zero promotion risk. A
demotion breach must persist **≥2 consecutive evaluation windows** before
firing (audit A2 — a sequential-test crumb against single-night noise;
documented asymmetry, not a floor weakening).

**Slice 2 — promotion.** Table `promotion_decisions` (append-only): card_id/
version, from_state, to_state, direction, trigger (auto_floor_breach|manual),
evidence_json, decided_by, lineage_id, **required `preregistration_id` FK**
(audit A4 — makes the full test family reconstructable; the survivorship
denominator). Card state transitions ONLY via `alphaos/cards/promotion.py`.
Promote preconditions, enforced in code and surfaced by
`alphaos autonomy_readiness`: effective-N floors met via the IDENTICAL floor
function the reports use (audits A1/B2) · `q_value < 0.1` · span floor ·
`decided_by != 'system'` (tested) · `research_ref` non-null when
risk_class='C'. CLI `alphaos cards` / `card_promote` / `card_demote`.

**Anti-double-jeopardy (audit B3):** a demoted card VERSION is terminal —
re-entry to `live_eligible` requires a NEW version evaluated on a
pre-registered forward window whose start is after the demotion timestamp;
`card_promote` refuses overlapping windows with `STALE_DATA_REUSE` (tested:
demote v1, attempt re-promote v1 on overlapping data → refused).

### PR13.5 — Diff→Version materialization (NEW — the joint that closes the loop)

Audit B1 (CRITICAL): as previously drawn, PR12 proposes diffs and PR13 toggles
STATE, but no mechanism turned a promoted `proposed_diff_json` into a new card
VERSION — cards are YAML files read from disk, the registry refuses unversioned
content changes (`registry.py:103–109`), and no spec said who writes
`catalyst_momentum_v2.yaml`. The loop could toggle cards on/off but never
change what a card DOES. The law, now written:

> **PR12 proposes diffs; PR13 toggles state; only an operator-committed YAML
> version changes card behavior. No job ever writes `cards/*.yaml`.**

- `alphaos card_promote <hypothesis_id>`: (a) renders the promoted diff as a
  proposed `<card_id>_v<N+1>.yaml` on disk for the operator to inspect;
  (b) BLOCKS until the operator commits the YAML and re-runs with `--confirm`;
  (c) registry sync registers the new version; the new version enters its
  declared state, the old version retires. The YAML author is the operator,
  aided by the rendered diff — never the nightly job (PD#3: promotion is never
  automatic, now structurally).
- Test: `hypothesis_pass` and `promotion.py` have zero filesystem-write
  reachability to `cards/` (grep/AST, §H.6 pattern).

### Cards v2–v5 (Lane A10+, all Class B, born `state: shadow`, verdict V3)

Entry/stop/invalidation sketches from the 2026-07-08 debate — full card specs
at build time; all long-only, ATR-scaled stops (INSTR-1), exit-first invariant,
`regimes: [any]` until Regime Engine v1 (no imaginary regime conditioning):

- **v2 `earnings_reaction_drift_v1`** (needs EARN-1, hard-gated on real data):
  earnings within 1–2 sessions + day-1 reaction ≥ +3% on normalized
  rel_volume ≥ 2 → enter first pullback holding above earnings-day open; stop
  1.5×ATR(14) below earnings-day low; target ≥ 2R; hold ≤ 5d; invalidation:
  close below earnings-day open, or catalyst-reversing headline → EXIT_REVIEW.
  No entry if next earnings inside hold window.
- **v3 `catalyst_continuation_pullback_v1`**: day-1 catalyst move ≥ +4% +
  sustained day-2 rel_volume ≥ 2 → enter day-2/3 pullback holding the upper
  half of day-1's range; stop below day-1 low (ATR-floored); hold ≤ 4d;
  invalidation: close below day-1 midpoint or polarity flip. Promotion gated
  on H-CAT-1 resolving TRUE.
- **v4 `no_news_gap_fade_long_v1`**: gap down ≥ 3% with EMPTY catalyst
  enrichment + neutral/positive polarity (no news IS the signal) → enter on
  opening-range-high reclaim; stop below gap-day low; target prior close
  (gap fill), half off at 50% fill; hold ≤ 3d; invalidation: a catalyst
  emerges explaining the gap, or second gap-down. Exists partly to trade the
  reversal regime that bleeds v1.
- **v5 `polarity_divergence_reclaim_v1`**: price −5%/5d while polarity
  positive and improving → enter first close above the 5-day high; stop below
  divergence low (ATR-floored); hold ≤ 7d; invalidation: polarity turns
  negative or fresh negative catalyst. Stays shadow until H-POL-1 resolves —
  a hypothesis wearing a card schema, joined on `card_id`, on purpose.

### PR14 — Red-Team Debate v0 (shadow)
Table `agent_votes`: vote_id, proposal_id, candidate_id, agent_role ('bear'
only in v0), stance (oppose|neutral|support), conviction REAL 0–1,
failure_modes_json (top-3), invalidation_triggers_json, model/prompt lineage,
is_mock, cost fields. Invoked batch-at-scan-end for PROPOSE decisions only
(the PR7 call-site pattern, after commit), capped `DEBATE_MAX_CALLS_PER_DAY`
(default 10) inside the PR3 cost budget. Pre-registered evaluation hypothesis
shipped WITH the PR (as a `preregistrations` row, one-shot-evaluated like every
other): "proposals with bear stance=oppose & conviction ≥0.7 underperform by
≥0.3R over effective-N≥30, 28d" — expansion to a triad is gated on that
resolving TRUE.

### PR15 — First autonomy promotion (L3: bounded auto-approval, paper)
No new machinery — an EVIDENCE + DRILL + AUDIT gate around flipping
`APPROVAL_MODE=auto_within_bounds` with existing caps
(`MAX_AUTO_APPROVALS_PER_DAY`, risk caps, TTL `_execute()` chokepoint, PR6
auto-path guard). Deliverables: the L3 entry-criteria checklist as an
executable report (`alphaos autonomy_readiness`), the drill script (born-
expired proposals in AUTO mode → zero fills — re-run of the PR6 adversarial
probe), the Opus audit, HANDOVER §10 autonomy-state note. Live-eligible cards
only (join on `setup_cards.state`). **Additional preconditions (2026-07-08):**
the CRO restore-drill law (a backup restored once for real) and the
portfolio-risk gates (Class C — the 166%-gross / static-100k findings bind at
L3, not before) must both be closed first; `autonomy_readiness` checks them.

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
