# ALPHAOS SPECS ADDENDUM — Exit-Review Upgrades (drafted 2026-07-07)

> ⚠️ **ARCHIVED — INTEGRATED 2026-07-08.** This file is the original operator-drafted
> addendum, preserved verbatim for provenance. Its items were integrated into
> `alphaos-pr-implementation-specs.md` v1.1 under canonical names — TASK-R, OPS-A,
> OPS-B, **CANARY** (was "PR9.6"), **BASELINE** (was "PR9.7"), PORT-1 — with
> 2026-07-08 final-review upgrades (three-arm baseline, day-block-bootstrap CI,
> one-shot preregistration evaluation, cumulative FDR family, and more; audit
> findings in the master reference §3.5). **Build from the specs doc, not from this
> file.**

Companion to `docs/roadmap/alphaos-pr-implementation-specs.md`. Same rules apply:
spec → build → independent review → audit → **merge only on explicit human
instruction (T4)**. Everything here is shadow/ops-layer work: nothing below may
read into, or alter, any live decision path. All schema changes are **additive
only**; bump `SCHEMA_VERSION`. All tests follow §H.1 (date-seeded mocks, direct
construction — no organic-scan assertions).

**Rebase note (2026-07-07):** drafted against the handoff-day state; the system
is now at PR11 (Daily Brief live, PR10 Setup Cards live, PR9.5 shipped). The
build session must read HANDOVER.md first and renumber these items into the
current sequence — the labels below (PR9.6/9.7) are placeholders. All "daily
digest" references below mean the PR11 Daily Brief.

Suggested build order: TASK-R (any time; its PR9.1 dependency is satisfied) →
OPS-A → OPS-B → CANARY → BASELINE → PORT-1 (must land before or alongside
PR12 — the hypothesis engine consumes its preregistrations table and FDR gate).

---

## TASK-R — Retro-relabel of the contaminated 2026-07-01 baseline

**Type:** one-off CLI task, not a standing job. Run once, keep the code.
**Depends on:** PR9.1 hotfix merged (clean prompt builder + regression test green).

### Goal
Produce clean AI labels for the 7 journaled 2026-07-01 candidates by replaying
their stored `packet_json` through the **fixed** prompt builder. Purpose is (a) a
clean baseline row set and (b) live proof the T5 fix works on real inputs — the
class of bug mock mode cannot catch.

### Non-goals
- Never modify or overwrite the original contaminated evaluations (append-only law).
- No change to any decision, outcome, or attribution row. Labels only.
- Not a general re-labelling framework. Hardcode nothing, but scope the CLI to
  explicit id lists / date ranges passed by the operator.

### Changes
1. New CLI: `python -m alphaos relabel --from 2026-07-01 --to 2026-07-01 [--dry-run]`
   - Selects candidate packets in range from `candidate_packets`.
   - `--dry-run` prints the fully composed prompts and exits **without** any
     OpenAI call — the operator eyeballs that no `_`-prefixed keys appear.
2. For each packet: build the prompt with the current (post-PR9.1) builder from
   the stored `packet_json` **exactly as stored** — no re-scanning, no fresh data.
3. Call the labeller with the standard client path (cost guard applies; these
   calls must count against the cap like any other).
4. Persist results as **new rows** in the existing evaluations table with:
   - full lineage stamp (PR4 fields: code/config/model/prompt hashes),
   - a new additive column `relabel_of` (nullable FK/text → original evaluation
     id; NULL for all normal rows),
   - raw completion stored verbatim (aligns with punch-list #13's retention rule).
5. Journal one system_event per relabelled packet: `event=relabel`,
   payload = {original_id, new_id, prompt_sha256}.

### Tests
- Direct-construction test: a packet dict containing `_catalyst`/`_polarity`
  keys → composed prompt contains none of them (this may largely duplicate the
  PR9.1 regression test; assert through the relabel path specifically).
- `--dry-run` performs zero network calls (assert client not invoked).
- Relabel rows carry `relabel_of` set; original rows untouched (checksum
  original rows before/after in test).
- Cost-guard accounting includes relabel calls.

### Acceptance
- Operator runs `--dry-run`, inspects prompts, then runs live; 7 new rows exist
  with `relabel_of` populated; digest or CLI summary prints old-label vs
  new-label diff table for the operator's eyes. No original row modified.

---

## PR9.6 — Model-Drift Canary

**Type:** small standing job + golden corpus. Shadow-only.

### Goal
Detect silent upstream changes to the OpenAI model behind `gpt-5.4-mini` (or any
configured model) before they contaminate weeks of ledger data. Weekly replay of
a frozen prompt set; alert on drift.

### Non-goals
- Not the offline eval harness (punch-list #13). The canary answers only "did
  the upstream model change?", not "which prompt is better?". Keep them
  separate; the harness can later consume the canary corpus.
- Never influences decisions, gates, or arming. Detection + page only.

### Design
1. **Golden corpus:** `data/canary/` (committed to git — these are frozen test
   fixtures, not runtime data): 12–20 candidate packets selected by the operator
   from journaled real packets (post-PR9.1, clean — you now have a week-plus
   of these; prefer a spread across symbols and interest-score bands). Stored as JSON files with a
   `MANIFEST.json` carrying sha256 of each. Corpus is versioned: any change to
   the corpus is a new `corpus_version` — never edit in place.
2. **Job:** new scheduler job type `canary_run`, weekly (suggest Sun 10:00 SGT —
   market closed, zero interference). Standard fuse + cost-guard coverage.
   - Compose prompts from each golden packet with the live builder.
   - Call the live configured model; store full raw completion.
   - Record per-run: model name as configured, and every model-identity field
     the API response exposes (e.g. `response.model`, `system_fingerprint` if
     present), token usage, latency.
3. **Storage (additive):** table `canary_runs` (run id, ts, corpus_version,
   configured_model, response_model, fingerprint, n_prompts, cost fields) and
   `canary_results` (run id, packet sha, prompt sha, raw completion, parsed
   label fields).
4. **Drift comparison**, current run vs the **pinned baseline run** (first run
   after merge is baseline; operator can re-pin via CLI):
   - Tier 1 (page immediately): `response_model` string changed, or fingerprint
     changed, or any parse/failsafe rate change from 0.
   - Tier 2 (page): any *categorical* label field differs from baseline on
     ≥ 20% of packets (categoricals should be near-deterministic at the
     temperatures used; threshold in config, not code).
   - Tier 3 (digest line only): numeric fields (confidence etc.) mean shift
     beyond a configured band.
   Rationale for tiers: avoid crying wolf on ordinary sampling noise while
   never missing an identity change.
5. **Alerting:** ntfy on Tier 1/2 (reuses `alerts.py`); always one line in the
   daily digest ("Canary: last run <date>, drift: none / TIER-n").
6. CLI: `alphaos canary run|status|pin-baseline`.

### Tests
- Mock client returns fixture completions → drift=none path.
- Fixture with changed `response.model` → Tier 1 alert emitted (assert on the
  alert call, mocked).
- Label flip on 3/12 packets → Tier 2. On 1/12 → no alert.
- Corpus tamper (sha mismatch vs MANIFEST) → job fails loudly, fuse eligible.
- Cost guard counts canary calls.

### Acceptance
- Two consecutive real weekly runs recorded; forced-drift drill performed
  (operator temporarily edits a stored baseline row in a **copy** of the DB or
  uses a test hook — never the live ledger) → page received; drill logged in
  `docs/incidents/`.

---

## PR9.7 — Deterministic Shadow Baseline (the "does the AI add R?" instrument)

**Type:** shadow measurement layer. Ported *by design* from NightDesk issue #81
(paired AI-vs-deterministic forward measurement). See PORT notes at the end.

### Goal
For every candidate packet that is sent to the AI labeller, also compute and
journal a **frozen deterministic decision** from the same inputs. Downstream,
attribution can then compute `ai_delta_r = replay_r(AI path) − replay_r(
deterministic path)` per candidate — the pre-registered instrument that answers
whether the AI labeller adds expectancy over a no-AI rule. This is the evidence
gate for ever spending on bigger models (Decision-log row "gpt-5.4-mini both
roles" names exactly this prerequisite).

### Non-goals
- The deterministic decision is **never read** by the live decision combine,
  gates, or execution. Shadow law applies in full.
- Not a second replay engine. All counterfactual R must come from the existing
  single replay engine (Never-List / Decision-log: one replay engine).
- No tuning of the deterministic rule after freeze (see pre-registration).

### Design
1. **Frozen rule v1** (deliberately dumb, fully specified, versioned):
   - Inputs: only fields present in the whitelisted `packet_json` (never `_`
     side-channel objects; after the ScanContext refactor, only the typed
     public fields).
   - Decision: PROPOSE if `interest_score ≥ X` and passes the same deterministic
     gates the AI path would face; else SKIP. X is chosen **once**, pre-registered
     in the spec PR description with rationale (suggest: the historical median
     interest score of AI-proposed candidates from the ledger, computed at build
     time and then frozen as a literal).
   - Bracket construction: identical function the live path uses (same sizing
     formula, same stop/target construction — one sizing formula law).
   - Output: {decision, bracket, rule_version, input_sha}.
2. **Storage (additive):** table `shadow_baseline_decisions`
   (candidate id FK, ts, rule_version, decision, bracket fields, input sha,
   **setup_card_id** — nullable join key to PR10 cards, populated when the AI
   path assigned one, so ΔR can later be sliced per card; the frozen rule
   itself never reads the card).
   Written in the same tick the AI evaluation is written, by the orchestrator,
   **after** the live decision is fully resolved (ordering guarantees the shadow
   write can never precede/influence the live path; enforce with a test).
3. **Counterfactual join:** extend the existing counterfactual outcomes job to
   also produce `replay_r` for shadow-baseline PROPOSEs, via the one replay
   engine, stored with `path='baseline'` alongside the existing paths — labeled,
   never mixed (researcher's gross-vs-net labeling discipline applies).
4. **Reporting:** digest gains a monthly line once floors are met:
   `AI vs baseline: n=…, mean ΔR=…, CI=…` — subject to the standard floors, and
   to the effective-N dedup once PORT-1 lands (until then, print the row-count
   caveat verbatim).
5. **Pre-registration block** (paste into the PR description, per PD#4):
   hypothesis (AI adds ≥ +0.05R mean over baseline on proposed candidates),
   metric (paired ΔR via one replay engine), floors (min n, min span), analysis
   date (not before floors met), and the commitment that rule v1 is immutable —
   improvements are rule v2, a new pre-registered arm.

### Tests
- Shadow write occurs iff an AI evaluation occurred for the candidate; never
  read: import-graph/inspection test that no decision-path module imports the
  shadow table accessor (mirrors the existing shadow-proof kit in §H).
- Determinism: same packet → identical decision + bracket across runs and
  across PYTHONHASHSEED values.
- Rule uses only whitelisted fields: packet with `_` keys injected → identical
  output to the stripped packet.
- Replay join labels `path='baseline'`; never aggregated with realized rows in
  any existing report query (regression test on the digest queries).
- Mock prices date-seeded; direct-construction only (§H.1).

### Acceptance
- Runs unattended for a week alongside real scans; `shadow_baseline_decisions`
  rows exist 1:1 with AI evaluations; a manual query reproduces one paired ΔR
  by hand and matches the stored value.

---

## OPS-A — Dashboard network binding (standalone small PR)

### Goal
The approval surface must not be reachable from the LAN. Priority raised now
that UI-PR-A's annunciator/Tonight tab has widened the dashboard's action
surface: the startup guard below must disable **all** action components,
including any added in PR11.

### Changes
1. Launch/config: Streamlit invoked with `--server.address=127.0.0.1` (and
   `--server.headless=true`); if a config file is used, pin it there and have
   the installer write it.
2. Startup guard inside the dashboard entrypoint: read the effective bind
   address; if not loopback, render a red full-page refusal and **disable all
   action buttons** (approve/reject/kill-switch release). Defense in depth —
   the flag protects, the guard verifies.
3. `deploy/install_launchagent.sh` (or dashboard runner script): validate the
   flag is present; refuse to install otherwise.
4. Document in HANDOVER §operating notes: remote access, if ever wanted, is
   SSH tunnel (`ssh -L 8501:127.0.0.1:8501 ck@macmini`) — never a LAN bind,
   never port-forwarding.

### Tests
- Unit test on the guard: mocked non-loopback address → action components not
  rendered / raise.
- Installer validation test.

### Acceptance
- `lsof -iTCP -sTCP:LISTEN | grep <port>` on the mini shows `127.0.0.1` only;
  phone on the same wifi cannot load the page. Log the check in
  `docs/incidents/` as a mini-drill.

---

## OPS-B — Off-ecosystem backup + encrypted `.env` (standalone small PR extending the shipped PR9.5 backup agent)

### Goal
Break the single-ecosystem failure domain (everything currently lands in
iCloud) and stop `.env` recovery from depending on a possibly-stale password
manager copy.

### Changes
1. **`.env` in every backup:** the nightly PR9.5 backup job additionally
   encrypts `.env` (see 3) and places `env.enc` next to the DB backup in the
   same dated folder. Same rotation. The DB backup and the config that ran it
   can now never drift apart.
2. **Monthly off-ecosystem copy:** on the 1st, after the normal nightly run
   succeeds, copy that night's `{db.gz, env.enc, MANIFEST}` to a second target
   **outside Apple's account domain**. Target is operator-configured in `.env`:
   `BACKUP2_METHOD=rclone|disk`, `BACKUP2_DEST=<remote:bucket/path or /Volumes/...>`.
   (rclone → any S3/B2/GDrive-class remote the operator sets up once; `disk` →
   an external drive if plugged in, with a loud alert if the volume is absent.)
   Retention: keep 12 monthlies at the second target.
3. **Encryption:** `age` with a passphrase (`age -p`) or symmetric openssl —
   passphrase lives ONLY in the operator's head/password manager, **never** in
   `.env` or the repo. The DB backup may also be encrypted at the second target
   (cheap; do it) — locally the plain `.backup` copy remains for fast restore.
4. **MANIFEST per backup:** sha256 of each artifact + schema_version + git rev;
   the restore procedure verifies shas before use.
5. **Alerting:** failure of either leg pages via ntfy like any job failure.
   Digest carries `Backups: nightly OK <ts> · offsite OK <date>`.
6. **Restore drill update (§6 of the master reference):** quarterly drill now
   includes decrypting `env.enc` and restoring from the *second* target, not
   just iCloud.

### Tests
- Backup job with mocked targets: artifacts + MANIFEST produced; sha verify
  round-trip; env.enc decrypts to byte-identical `.env` (test fixture env, not
  the real one).
- Second-target absent (disk unplugged / rclone remote down) → alert emitted,
  nightly leg still completes.
- No passphrase or key material ever written to logs/journal (grep test on
  captured log output).

### Acceptance
- One real restore performed from the second target on a scratch directory,
  logged in `docs/incidents/`.

---

## PORT-1 — Porting NightDesk's statistical discipline (method note, then spec)

### How to port instruments between the two systems (the method)

Port the **contract, not the code**. NightDesk and AlphaOS are different stacks
with different schemas; copy-pasting modules imports NightDesk's assumptions
and none of AlphaOS's tests. The working recipe:

1. **Extraction session (in the NightDesk repo):** have Claude Code read the
   Thesis Research Layer (#85) and the paired-measurement instrument (#81) and
   write a *portable design document*: inputs, outputs, invariants, formulas
   (FDR procedure, effective-N rules, pre-registration fields), failure modes,
   and the lessons/incidents that shaped them. Explicitly instruct: **no
   AlphaOS code, no NightDesk code in the output — prose, schemas, and math
   only.** Save as `docs/roadmap/ported/nightdesk-stats-contract.md` in AlphaOS.
2. **Adaptation review (AlphaOS session):** map each contract concept onto
   AlphaOS's actual tables (candidate_packets, evaluations, outcomes,
   attribution) and floors. Anything that doesn't map cleanly gets a decision
   in the doc, not an improvisation in code.
3. **Build as a normal AlphaOS PR** under the standard T-process, with
   AlphaOS-idiom tests. The NightDesk repo is reference material only; the
   contract doc is the spec's parent.
4. Record in the Decision Log: "ported by contract from NightDesk #81/#85" so
   future sessions know where the lineage lives.

PR9.7 above already *is* the #81 port done this way. PORT-1 below is the #85
(FDR / anti-data-mining) port, and it should land together with punch-list #15.

### PORT-1 spec sketch — Effective-N + FDR layer (with punch-list #15)

**Goal:** every aggregate the system shows obeys (a) effective-N counting and
(b) multiple-testing discipline, closing the researcher's HIGH finding.

1. **Effective-N:** a single shared function `effective_n(rows)` that dedups to
   one observation per symbol-day (or clusters overlapping windows on the same
   symbol) before any floor check. Every floor call site switches from
   `len(rows)` to `effective_n(rows)`. One implementation, one law — mirror of
   the one-replay-engine rule.
2. **FDR gate for slicing:** any report/hypothesis run that evaluates >1
   card/regime/slice simultaneously must pass p-values through Benjamini–
   Hochberg before any "significant" flag is shown; the digest prints
   `q=…` not raw `p=…`. Port the exact procedure + thresholds from the
   NightDesk contract doc.
3. **Pre-registration table:** additive table `preregistrations` (hypothesis
   text, metric, floors, analysis-not-before date, registered ts, immutable) —
   PR12's hypothesis engine writes here *before* any forward test; PR9.7's
   block becomes its first row, migrated in.
4. Attribution touch-conditioning caveat + candidate-level max-favorable
   anchoring fix ride along here (researcher MEDs, punch #15).

**Tests:** effective-N on constructed clustered fixtures (10 rows, 3 symbol-days
→ n=3); BH on a known textbook vector → exact expected q-values; floor call
sites covered by a grep/AST test asserting no remaining `len(` floor checks.

---

*All five items are shadow/ops only. None touches the chokepoint, the gates,
or the Never-List. Merge order and timing remain the operator's call.*
