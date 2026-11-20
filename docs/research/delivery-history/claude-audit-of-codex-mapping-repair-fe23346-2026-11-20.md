# Claude audit — Codex EXPLORE-1 mapping repair `fe23346`, 20 November 2026

**Exact SHA audited:** `fe23346a249a3e35e917992b0f41af0273bdbb8f` (branch `codex/close-ref-outcomes-20261118`).
**Range:** `e4884df..fe23346` = `7cd1c17` + `a10cae0` (the rejected `4470356` + `a641b3c`, rebased) + repair `fe23346`.
**Handoff:** `CLAUDE-MAPPING-REPAIR-HANDOFF-2026-11-20.md` (treated as data).
**Prior audit:** `claude-audit-of-codex-decision-mapping-a641b3c-2026-11-19.md` (NOT ACCEPTED: D1, D2; O1, O2).
**Probes:** `claude-audit-fe23346-mapping-repair-probes-2026-11-20.py` (G1–G9), run from my own clone at the exact SHA
against copies of a **fresh read-only production snapshot taken 2026-11-20 07:28:46Z** (not Codex's 19-Nov copy).

## Verdict

**ACCEPTED at the exact SHA `fe23346`.** D1 and D2 are repaired and O1 is closed as CK ruled. Every mapping finding
from the a641b3c audit carries forward unchanged. The full-suite exceptions are pre-existing timing flakes
(§5). Deployment conditions are in §6. Nothing here authorizes a merge or deploy; that is CK's instruction.

## 1. Rebase purity (transfer of unchanged findings)

- `git range-diff` shows `4470356 = 7cd1c17` and `a641b3c = a10cae0`, both **identical** patches.
- `diff <(git diff a641b3c a10cae0) <(git diff 3f1721c e4884df)` is empty. The rebase brought in only my
  docs commits, so the code under the a641b3c audit is byte-identical below the repair.
- **The a641b3c "confirmed" table therefore carries forward**, except where §2–§4 supersede it:
  - the write-once routing stamp in the draw UPDATE;
  - all 13 spec §6 examples;
  - bounds, the campaign boundary and order rules;
  - session dedupe by D, pool from owning in-window draws;
  - the pinned calendar matching the system calendar;
  - leg/scalar atomicity;
  - the producer-digest test guard.
- The orchestrator digest at the tip, `0ad26c4d…`, is in `_APPROVED_PRODUCERS`. Production's main/e4884df digest is
  `161329c3…`. Every lineage commit recorded in production since 14 Nov (ea036e1, 70e0063, 45ca35f, 3f1721c,
  e4884df) is present in the production object store with digest `161329c3…`.

## 2. D1 — evaluator fail-safe exclusion: REPAIRED

Code change: `close_ref_mapping._ranking_reason` checks `ai.labeller_health.is_openai_reject_row` **first**. A match
returns `evaluator_failsafe`, so `ranking_eligible=False`. `deduplicate` already skips non-eligible evaluator rows
when grouping keys, so a fail-safe never consumes a symbol/D key. `summary()` adds an `evaluator_ranking` block that
keeps the ranking denominator separate from measurement enrollment.

| Probe | Result |
|---|---|
| G1 healthy enroll, fresh snapshot | 159 inserted (152 ready / 7 duplicate). Repeat inserts 0. Evaluator ranking: 150 total, **3 eligible**, 147 excluded = 105 `evaluator_failsafe` + 39 `not_long` + 3 `duplicate_in_session` |
| G2 every OPENAI_REJECT original | 105/105 carry `evaluator_failsafe`; 0 ranking-eligible. The five 18-Nov rows from the prior audit (AAPL, AMD, AMZN, AVGO, DIS), plus eight more since, stay **measurement-ready** as ruled |
| G3 an earlier fail-safe twin plus a genuine row on the same key | genuine row stays `ready` and is not demoted; the fail-safe stays measured with no key |

No stored row is rewritten: the predicate reads `risk_flags_json` as stored. The earliest original per candidate stays
authoritative, so a later genuine re-evaluation of the same candidate does not replace an earlier fail-safe. This
matches spec §2 and CK's ruling note.

## 3. D2 — transient source-proof failure: REPAIRED

Code changes:
- `_committed_ordering` is `lru_cache`d and caches **successes only**. A non-zero `git` exit raises, and so does a
  timeout, so neither is cached.
- `source_records` defers the whole scan when the reason is `source_commit_unavailable`, an unrecognised unverified
  reason, or any exception. A deferred scan produces no record in any population.
- Permanent reasons are only `source_commit_missing`, `scan_source_identity_unproven` and
  `source_ordering_unreviewed`. That matches my D2 specification.

| Probe | Pass 1 | Healthy pass 2 (**no `cache_clear`**) | Equals single healthy run |
|---|---|---|---|
| G4 `TimeoutExpired` | 0 inserted, 287 scans deferred (`source_commit_unavailable`) | 159 inserted, 0 deferred | **yes** |
| G5 `git` exits 128 | 0 inserted, 287 deferred | 159 | **yes** |
| G6 arbitrary exception in `scan_proof` | 0 inserted, 291 deferred (`source_proof_exception`) | 159 | **yes** |
| G7 digest unreviewed (permanent) | 159 inserted, 9 `ordering_source_unproven` | 0 inserted (stays frozen) | differs (as specified) |
| G8 one scan holding a cross-scan key deferred, then healed | 145 | 14 inserted, 5 later rows demoted to duplicate | **yes**, identical final table |

G8 matters. A deferral can let a later scan's record take a key first. Once the earlier scan is admitted, the
existing reconciliation path demotes that later record. The demotion uses timing only, never outcomes, and the
end state matches a healthy single run.

## 4. O1 — split-only prices: CLOSED as CK ruled

- `AlpacaBarsProvider.get_daily_bars` takes a keyword-only `adjustment` argument. It defaults to `raw` (other callers
  are unchanged) and refuses anything other than `raw` or `split`.
- `CloseRefSources`, `make_sources` and `enroll` require `split`, and `_check_feed` re-checks it before every call.
- Each horizon's reference and endpoint close come from **one** response, so each leg is internally consistent even
  if a later split re-adjusts history. The SPY cache key includes `adjustment`. The stored row's `adjustment` must
  match the source, or `update` raises.
- G9:
  - the request URL carries `&adjustment=split`; the default call still sends `&adjustment=raw`;
  - `adjustment="all"` is refused;
  - a raw source is refused;
  - a raw enroll is refused.
- Residual risk, not a defect: if the provider has not yet applied a corporate action when it is fetched, a
  split still freezes as a return. Codex's note says correctly that the offline tests do not certify the provider's
  corporate-action data. Cash dividends are unadjusted for both legs, as ruled. SPY's mid-December ex-dividend drop
  (~0.3 %) therefore slightly favours r − b in windows that span it.

## 5. Preservation and the suite

- **Preservation (my own, fresh snapshot copy):** 68 → 69 tables (only `explore_close_outcomes` is new).
  `scan_batches` gains `explore_routed_at_utc`, and every original column is identical across all 291 rows.
  `system_events` gains +1 (the migration event). `preregistrations` is identical. Every other table is
  byte-identical. This agrees with Codex's 69-table and 18-registration evidence.
- **Full suite (mine, exact SHA): 5,044 passed / 3 skipped / 0 failed, exit 0** (§8).
- **Disposition of the two failures:**
  - Both are in `tests/test_last30days_partial_flow.py`: `[0.01]` item-evidence at line 136, and the no-items
    timeout at line 161. Both return `unverified` under a 0.01 s timeout.
  - The range touches no last30days code, its tests, its settings or its fixtures.
  - Both reproduce on the base (Codex: e4884df 2/5 and 3f1721c 1/5; my 19-Nov run: base 2/5).
  - **These are pre-existing timing flakes, not caused by this SHA, and they do not block it.** They still deserve
    their own ticket, because a red main hides real regressions.

## 6. Remaining blockers and deployment conditions (for CK)

1. **API credits (the binding blocker for evidence; not a code blocker).**
   - As of 07:28Z today, all 80 evaluations since 18 Nov are OPENAI_REJECT. The last genuine one was
     2026-11-16 20:57:33Z.
   - With D1 fixed, enrolling before the credits return is now safe: fail-safes are measured but never ranked.
     The earlier "do not enroll until credits are restored" condition is lifted.
   - Every session without credits is still lost to all arms.
2. **Deploy deadline.**
   - Each leg must be fetched within 10 calendar days of its endpoint close (`retry_until`).
   - The 16-Nov records have their D+5 on 23 Nov 21:00Z, so their fetch deadline is **3 Dec 21:00Z**. That group
     holds the only 3 ranking-eligible rows and the 16-Nov arm and pool records.
   - Deploy by about 1 Dec to keep a margin.
3. **Exact tree.**
   - Deploy the tree of `fe23346`. The branch descends from `e4884df` (origin/main), so a fast-forward is
     possible if main has not moved.
   - If main moves (this record's commit moves it), merge with a merge commit or rebase. Then verify that
     `git diff fe23346 <merged> -- alphaos tests pyproject.toml` is empty.
   - Do **not** combine with `4274239` (the evaluator status fix). It changes `orchestrator.py`, so the producer
     digest would leave `_APPROVED_PRODUCERS`. Every subsequent scan would then freeze permanently as
     `ordering_source_unproven` / `non_degraded_scan_unproven`. The test at `tests/test_close_ref_tracker.py:228`
     catches this, but only if the suite is run on the merged tree.
   - `e351a15` does not touch `orchestrator.py`.
4. **Schema migration before reload.** This adds a column and a table. Open the ledger once by hand (migrate)
   before reloading the launchd services, so simultaneous starts cannot race the migration.
5. **First production enrollment.**
   - Run one `outcomes-update` by hand after the deploy.
   - Expect roughly 159+ rows; `source_proof_scans_deferred = 0`; `evaluator_failsafe` ≥ 105; 3 ranking-eligible.
   - A non-zero deferral count means `git` is unreachable from the service environment. That is safe now, but it
     must be fixed before 3 Dec.

## 7. Observations (non-blocking)

- **O2 (ranking-test feasibility) is worse than reported on 19 Nov.**
  - Only 3 genuine long evaluator rows qualify (COST, XLV, UBER, all D = 16 Nov).
  - No session has yet reached the 10-long-row minimum.
  - The 20-session floor needs the credits back and the scanner's long share to recover. This is a strategy
    question for CK, not a code defect.
- **Deferral visibility.** A persistent proof deferral shows up only in the `outcomes-update` result
  (`source_proof_scans_deferred`). Consider adding it to the health or digest surface later.
- **§9 row placement.** Codex's ruling row sits at the top of the §9 table, not directly above the previous newest
  row. This is cosmetic.

## 8. Full-suite result (Claude, own clone at exact SHA)

- Clone at `fe23346`, `PYTHONPATH=$PWD`, main venv. **5,044 passed, 3 skipped, 0 failed; exit 0.** Same 5,047-test
  collection as Codex's run (5,042 + 2 failed + 3 skipped).
- Isolated repeats of `tests/test_last30days_partial_flow.py`, 5 runs per revision, run sequentially after the suite:
  - **base e4884df: 3/5 runs failed** (item-evidence `[0.01]` ×1, no-items ×2);
  - **tip fe23346: 2/5 runs failed** (item-evidence `[0.55]` ×1, no-items ×1).
- The tip's `[0.55]` parameter did not fail in my five base runs. It is the same test function as the base's `[0.01]`
  failure, with the same `unverified` timing path, and the range touches nothing it imports directly.
- Five runs estimate no rate. Together with Codex's runs, the evidence is: **the failures pre-exist, they are timing-
  dependent, and this SHA does not cause them.** My green run is one lucky-timing pass, not proof of determinism.
