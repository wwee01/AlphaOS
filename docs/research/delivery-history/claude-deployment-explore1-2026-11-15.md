# Deployment record — P0 + EXPLORE-1, 15 November 2026 SGT (Claude, on CK's instruction)

**Instruction (CK, 15 Nov):** "Deploy now. Allow paper shorts in arm A. Stop the frozen swing audit."
**Result:** P0 + EXPLORE-1 deployed and active for the 16 November US session. Swing audit stopped.
Paper shorts are **ruled allowed but not built**; shorts remain blocked (brief:
`docs/roadmap/codex-brief-explore1-followups-2026-11-15.md`).

## What is running

| Item | Value |
|---|---|
| Merge commit on `main` | `cc117ee` (`--no-ff` of Codex `319003d` onto `04b88ff`) |
| Application code | byte-identical to the audited `1c3d3ea` (`git diff 1c3d3ea cc117ee -- alphaos tests scripts deploy pyproject.toml` is empty) |
| Test-only follow-up | `96dc7a1` — audit finding F4, one field added to a test fixture; no runtime effect |
| Audit | `claude-audit-of-codex-p0-explore1-2026-11-15.md` (F1–F3 still open) |

## Configuration applied (`.env`, not in git; pre-deploy copy preserved)

| Key | Before | After |
|---|---|---|
| `EXPLORE_1_ENABLED` | absent (false) | `true` |
| `MAX_OPEN_POSITIONS` | 5 | 20 |
| `MAX_RISK_PER_TRADE_PCT` | 0.01 | 0.0025 |
| `MAX_AUTO_APPROVALS_PER_DAY` | 5 | 20 |
| `MAX_UNATTENDED_APPROVALS_PER_DAY` | 1 | 20 |
| `MAX_PAPER_TRADES_PER_DAY` | 1000000 | 20 |
| `SCHEDULER_AI_COST_CAP_CALLS_PER_30D` | 5000 | 7200 |
| `SHADOW_LABEL_TOP_K` | absent (default) | 20 |
| `SHADOW_AI_CAP_CALLS_PER_DAY` | absent (default) | 60 |
| `SHADOW_AI_CAP_CALLS_PER_30D` | absent (default) | 1800 |

Unchanged: `MAX_DAILY_LOSS_PCT=0.03`, `UNATTENDED_APPROVE_WINDOWS=15:45-16:00`, `APPROVAL_MODE=auto`,
`MONITOR_DEADMAN_ENABLED` false with no URL, paper-only flags. The shadow latch
(`data/SHADOW_LABEL_SUSPENDED`) was not touched; the shadow values above take effect only after a
fresh `shadow_recovery` preview passes every gate.

## Sequence (all times SGT, Sunday; US market closed)

1. 14:14 pre-flight, read-only: 0 open positions, 0 working orders, readiness `entry_ready=true`.
   One legacy `submitted` proposal row (XLK, 4 Aug) — untagged, so it cannot execute under EXPLORE-1.
2. 14:16 preservation, outside the repo:
   `~/Library/Application Support/alphaos/deploy-preservation/explore1-20261115T061639Z/` — ledger
   `.backup` (integrity ok, SHA-256 `4219fe99…`), `.env` copy, tarball of the 25 running overlay
   files, service definitions, `launchctl print-disabled` output, pre-deploy HEAD.
3. 14:21 waited for the running `outcomes_update` job to finish, then `launchctl bootout` of
   scheduler, monitor and heartbeat (no worker process running).
4. 14:21 re-verified all 25 overlay files against the audited import `e9471dd` (identical), restored
   the two modified tracked files, removed the 23 untracked copies, merged.
5. 14:22 applied the ten `.env` changes; loaded settings from the real file and confirmed each value.
6. 14:22 `launchctl bootstrap` of the three services; `kickstart -k` of console and dashboard.
7. 14:22:45 additive schema migration ran on first open (15 nullable columns, 7 tables).

Services were out for about one minute.

## Verification after deploy

- Ledger against the pre-deploy backup: original columns of `candidates` (45,203), `trade_proposals`
  (35), `paper_orders` (46), `positions` (22), `trade_outcomes` (22), `scan_batches` (279), `exits`,
  `paper_fills`, `approvals`, `preregistrations` (15), `decision_adjustments` (2,787), `risk_checks`
  are identical; no rows added; no non-null arm/policy value; unique draw index present;
  `quick_check` ok. `candidate_outcomes` differs in original columns — that table is rewritten hourly
  by the outcomes job, which ran during and after the backup; not attributed to the migration, and
  not separately proven row by row.
- Services two hours later: scheduler, monitor, heartbeat, backup last exit 0; all six `enabled`;
  monitor passes at 14:32 and 14:47 complete with startup checks OK; dashboard health 200; console
  listening on its Tailscale address.
- Tests on the deployed tree: 193 affected tests pass (swing audit, account exposure, entry deferral,
  explore policy, dead-man, observer heartbeat, worker health exit); my 9 audit probes pass. The full
  suite was run on `1c3d3ea` during the audit, not re-run after the merge.

## One fault during deploy

The scheduler's first tick after reload exited 2 with `RuntimeError` (the only such line in 14,343
logged ticks). The worker deliberately prints no traceback. Most likely cause: scheduler, monitor,
heartbeat, console and dashboard all opened the ledger in the same second and raced on the
`ALTER TABLE` migration; one completed it (14:22:45), another failed (`journal_store.py:110`, "schema
migration failed"). Not proven. Every tick since has exited 0, and the migration cannot run twice.
**For the next schema-changing deploy: open the ledger once by hand before reloading services.**

## Swing audit stopped

`data/swing-audit-activation.json` renamed to `data/swing-audit-activation.json.stopped-2026-11-15`
(the file the research worker's hook checks). Code, evidence and logs are preserved. The paper
observer (run by the heartbeat service) still runs — CK's instruction named the swing audit only.

## Rollback

Stop the three services; `git revert -m 1 cc117ee` (and `96dc7a1`); restore `env-predeploy` over
`.env`; reload. The added columns are nullable and ignored by the old code, so the ledger does not
need restoring; the backup is there if it does. The old code ran with the 25 overlay files
uncommitted — restore them from `overlay-predeploy.tar.gz` only if returning to that exact state.

## Not done

Paper shorts (needs code); F1 deferral retry; F3; 28 November scorecard (Codex); historical study and
primary-question registration (Claude); external heartbeat URL (CK).
