# Audit of Codex EXPLORE-1 registration script `0a7cafe` — Claude, 18 November 2026 SGT

**Verdict: CONFIRMED at exact SHA `0a7cafe6e976aeb7f2178ed86ad0dbb64609b0b0`** (script SHA-256
`159ad237d1c10ceb4e7e8c02c13c3d2209bc10dbacd9cf650aa710548612b07e`). No defect found. Codex may make
the single production run that CK conditionally authorised, before **2026-11-20 21:00 UTC
(05:00 SGT Saturday 21 November)**, under the run conditions in section 4.

**Scope.** Codex's handoff `CLAUDE-REGISTRATION-HANDOFF-2026-11-18.md`; the commit (2 files:
`scripts/register_explore1.py`, `tests/test_explore1_registration_script.py`; no application code);
Codex's copy rehearsal and readback. **Method:** read the script in full; checked every pin; my own
rehearsal on a fresh read-only snapshot of the production ledger (taken 14:00 SGT, 15 registrations);
16 failure-injection probes on copies (`claude-audit-0a7cafe-registration-probes-2026-11-18.py`
beside this file); a call-site sweep for any automated evaluator. Production was only read. Nothing
merged, deployed or registered.

## 1. Results

| Check | Result |
|---|---|
| Script bytes at `0a7cafe` | SHA-256 `159ad237…07e`: matches the handoff. Codex's worktree is clean at that HEAD |
| Contract pins | v1 `a73369d4…c6a6` and addendum `bc00419d…187e` match `f7452f6`. All 9 pinned sources are identical in the commit, in the production tree and in live code `d78ad87` |
| Payloads, checked independently against `f7452f6` | All three rows carry: claims verbatim from v1 section 4; seeds 20261116 / 20261117 / 20261118; sidedness one / two / one; family of three; floors A 40 decisions on 20 sessions, pool 300 on 20, IC-EVAL 20 valid sessions of at least 10 rows; `analysis_not_before` `2027-01-16T01:00:00Z`; cohort start `2026-11-16T14:30:00Z` (09:30 ET); legacy floors 1,000,000 and 1,000,000.0; contract commit `f7452f6` with both documents embedded **byte-for-byte**, plus their hashes; no evaluation, operator or strong-prior fields |
| Preview | the copy stays byte-identical (no migration, no write) |
| My copy rehearsal: preview, then apply, readback and a second apply | 3 rows committed (seq 15 → 18), readback OK, second apply and later preview refused |
| Historical preservation (independent comparison over all 69 tables) | schema identical (259 objects); the only differences are 3 added `preregistrations` rows and `sqlite_sequence` 15 → 18; every other row in every table is unchanged |
| Atomicity (P1, P2, P4) | failure on the third insert, a failed commit guard, or the deadline crossed inside the transaction: the whole family rolls back with nothing written |
| Deadline (P3) | 20:59:59.999 UTC commits; 21:00:00 UTC refuses; a naive clock refuses |
| Live-ledger conditions (P5, P6, on a WAL copy) | another writer holding the lock: refused after about 10.7 s, nothing written, receipt says `refused_or_commit_uncertain`. With a reader open: commits, and the receipt carries the committed readback |
| Refusals (P7–P10) | refuses on any `h-exp1` mention in any proposal column (case-insensitive), on a trigger on the registry, on a symlinked or hard-linked ledger path, or on an existing receipt; nothing is written in any case |
| Scheduled non-discovery (P11 plus code sweep) | The only automated legacy evaluation is `resolver._resolve_one`, reached only through `hypothesis_proposals` rows with status `testing`; the registrations are unlinked. Running `seed_all` and `resolve_due_hypotheses` on the applied copy at 2027-01-17 and 2027-03-01 left all three rows byte-identical and unlinked. `evaluate_two_arm_hypothesis_pair` is CLI-only and matches on PER text |
| Production preconditions (read-only, 14:00 SGT) | `data/alphaos.db` regular file with nlink 1, WAL, no registry triggers, 15 registrations, 0 matching `H-EXP1` |
| Codex's copy readback | `rehearsal-readback-f745.json` SHA-256 `53b6ac80…eef` matches; script pin `159ad237…`, contract `f7452f6`, legacy floors 1,000,000, unevaluated |
| Tests | Codex's 24 registration tests pass on my run. Full suite: I relied on Codex's receipt (`4877 passed, 3 skipped`, exit 0, HEAD `0a7cafe`, clean tree); the diff touches no application code |

## 2. Observations (none blocks the registration)

- **O1 — known hazard, reproduced (P12).** `research_validity.registration_validity` does not flag
  these rows, so a **direct** `evaluate_hypothesis` call today succeeds. It writes
  `insufficient_data` (the 1,000,000 floor works as intended) but permanently spends the one look.
  This is the hazard disclosed in addendum section 4 and in Codex's handoff, now demonstrated.
  **Recommendation:** bring the legacy-refusal guard forward from 9 January. The smallest form is a
  distinct, non-quarantine refusal reason in `registration_validity` when
  `params.registration_version == "explore1_immutable_contract_v1"`. Both `evaluate_hypothesis` (via
  `require_finalization_allowed`) and the resolver already consult it. It needs its own SHA and its
  own audit. The versioned evaluator must not route through it.
- **O2 — the `look_rule` parameter is a faithful paraphrase, not a quotation.** Its first two
  sentences match addendum section 3; the rest condenses the one-look text of section 2. Each row
  embeds the exact addendum bytes, so the documents govern. The versioned evaluator must implement
  from the embedded documents, not from the summary fields. The same applies to IC-EVAL's validity
  condition: the parameters record only the 10-row minimum, while the embedded text adds "scores
  and returns not all equal".

## 3. What I did not do

I did not merge, deploy or register, and made no production write, broker call, `.env` change or
service action. I wrote only to scratchpad copies.

## 4. Run conditions for Codex's single production run

1. Run from the clean checkout at exactly `0a7cafe`, with `--expected-script-sha256 159ad237…07e` and
   `--db` set to the production path. No merge or deploy is needed for this.
2. Run while the US market is closed in SGT daytime, and not during `outcomes_update`, so the
   10-second write-lock wait is not reached.
3. Use a new receipt path for every invocation.
4. **If apply refuses or its outcome is unclear, run `readback` before anything else.**
   - Readback shows exactly the three rows: the registration is done. Do not re-apply.
   - Readback shows zero `H-EXP1` rows: nothing was written, and one fresh apply is safe because
     the duplicate refusal still protects it.
   - Any other result: stop and report to Claude and CK.
5. Publish the three production `prereg_id`s, the row hashes and the receipt hash. Claude then
   verifies them read-only.
