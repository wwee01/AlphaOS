# Audit of Codex EXPLORE-1 legacy-refusal guard `e351a15`, and verification of the production registration — Claude, 18 November 2026 SGT

**Verdict: CONFIRMED at exact SHA `e351a15ee8fb5916d75f70c80bbc00fb7ccfe5a5`** (parent `fc08f73`). No
defect found. It is ready for CK's merge and deploy instruction. Until the guard is deployed, O1
remains live in production code.

**Production registration: VERIFIED read-only.** The three production rows, row hashes and receipt
hashes match Codex's record. Nothing was re-applied or evaluated.

**Method.** Read the diff in full (3 files: `alphaos/stats/research_validity.py` +20,
`alphaos/hypotheses/resolver.py` +5, new test file). Swept every consumer of the validity
functions. Ran probes on copies of a fresh production snapshot, once under the guard and once under
the parent, using the same script (`claude-audit-e351a15-legacy-guard-probes-2026-11-18.py` beside
this file). Ran my own full suite on a private clone at the exact SHA. Production was only read.
Nothing merged or deployed.

## 1. Production registration (read-only)

| Check | Result |
|---|---|
| Rows | 18 registrations, sequence 18. `H-EXP1-A` `prereg_c1b61ea18ba4` (id 16); `H-EXP1-POOL` `prereg_33d3f0c7ea57` (17); `H-EXP1-IC-EVAL` `prereg_699b00b2a9b9` (18); all registered 2026-11-18 07:22:05 UTC |
| Row hashes (the audited script's canonical digest, recomputed by me) | `624810fb…dbed9e9`, `b801059f…6265ab6f`, `85ea8965…4c1338ce`; all three equal the published values |
| Payload | each row's parameters equal, byte-for-byte in canonical form, what audited script `0a7cafe` generates from `f7452f6`; the script's own `readback()` on the read-only production connection passes |
| State | unevaluated, not approved, no strong prior, **0 proposal links** |
| Preservation | the 15 earlier registrations and all `hypothesis_proposals` rows are identical to my snapshot taken at 14:00 SGT, before the run |
| Receipts | preview `19a5fe7b…`, apply `b7375c94…`, readback `63ff7d6a…`, run record `0d034ad9…`, independent review `b377dccc…`: all match the record; there is exactly one apply receipt |

## 2. Guard — protection of the single look

| Probe (on copies of the production snapshot) | Parent `fc08f73` | Guard `e351a15` |
|---|---|---|
| G1: direct `evaluate_hypothesis` on each production row, with 60 usable rows | **all three evaluated** (`insufficient_data`), look spent | **all three refused** with `LegacyFinalizationRefusedError` `VERSIONED_EVALUATOR_REQUIRED`; registry byte-identical |
| G2: each row accidentally linked as a `testing` proposal with a working metric function, resolver run at 2027-03-01 | not evaluated (held only by the 1,000,000 placeholder floor: `not_yet_sufficient`) | refused earlier, under `legacy_refused`, before the metric is computed; rows untouched; no errors |
| G4: two-arm PER writer with EXPLORE-1 IDs | deferred `identity_fields_missing`, nothing written | same |
| G5: an ordinary new registration still evaluates through the legacy path | yes | yes |

The refusal sits after the existing "already evaluated" check and before any statistics or write. An
EXPLORE-1 row that also matched an audit reason would be quarantined instead, which also refuses.
Unreadable parameters are quarantined, as before.

## 3. Existing quarantine compatibility

`registration_validity` was run on all 18 production registrations, with and without each linked
proposal:
- the 15 existing rows return output identical to the parent's (all
  `AUDITED_CONTRACT_REQUIRES_SUCCESSOR`);
- only the three EXPLORE-1 rows change, to `quarantined=False`, `legacy_finalization_refused=True`.

The resolver still reports H-CAT-1, H-TTL-1, H-REJ-1, H-POL-1 and H-AI-1 as quarantined.

## 4. The two disclosed full-suite failures

| Run | Result |
|---|---|
| Codex, full suite at `e351a15` | 4,869 passed, 3 skipped, **2 failed**, exit 1 |
| Claude, full suite at `e351a15` (private clone, clean, 08:11–08:36 UTC) | **4,870 passed, 3 skipped, 1 failed, EXITCODE=1**. The failure is `test_real_default_worker_infeasible_wait_retains_other_returned_source`; total 4,874, the same as Codex's |
| Claude, `test_last30days_reddit_deadline.py` alone, 5 runs on the guard and 5 on the parent `fc08f73` | guard: 4 runs failed, 1 passed; **parent: 4 runs failed, 1 passed** (`[body]` ×3, `[executor]` ×1). Same failure rate without the guard |
| Focused tests at `e351a15` | 142 passed: `test_explore1_legacy_refusal` 18, `test_stats_preregistration` 10, `test_hgen1_hypothesis_proposer` 60, `test_pr12_hypotheses` 35, `test_research_contract_quarantine` 19. `ruff` clean on the edited files |

Both failing cases are in `tests/test_last30days_reddit_deadline.py`, and four facts point away from
this commit:
1. The **same two cases** failed in my own full run of F1 `13b5645` on 16 November, before this commit
   existed (`claude-audit-of-codex-f1-2026-11-16.md`).
2. Importing the test module loads neither `research_validity` nor `resolver`.
3. The parent fails the file at the same rate as the guard: 4 of 5 runs each in my reruns. Codex's reruns also failed the body case on the parent.
4. Making the file deterministic is already brief item 3.

Not caused by this guard, and not a reason to hold it. The file stays red until item 3 lands.

## 5. Observations (none blocks)

- **O3. Display-only consumers still read only `quarantined`.** These are
  `hypothesis_report.resolver_ready`, `fdr.forward_test_eligible` and `cards/promotion`, plus
  `preregistration.py:347`, which is only reachable after the two-arm identity check that already
  defers EXPLORE-1. None can write evidence. If an EXPLORE-1 row were ever linked, the report could
  show it as "resolver ready" while the resolver refuses it. Fold this into the versioned evaluator's
  work; no action now.
- **O4. The guard keys on one exact parameter string**, `registration_version ==
  "explore1_immutable_contract_v1"`, which the three immutable rows carry (verified). A future
  contract version needs its own entry. It is a routing refusal, not a write barrier: raw SQL can
  still write, as Codex states.

## 6. Not done

No merge, deploy, registration, evaluation, production write, broker call, `.env` change or service
action.
