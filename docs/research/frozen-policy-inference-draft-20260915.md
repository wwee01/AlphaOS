# First corrected inference primitive — 15 September 2026

**Status: implementation for review, not an activated study or a replacement frozen verdict.** This bounded R1 slice implements `alphaos/stats/frozen_policy_inference.py` and a reproducible synthetic example. It changes no existing evaluator, registration, quarantine, caller, schema, scheduler, policy, broker or historical row. All 15 existing contracts retain their earlier status. The September 18 acceptance deadline is unchanged.

The concrete priority is the approved **current policy versus narrow WATCH-advisory whole-portfolio comparison**, not a candidate-level AI-R contrast. The [successor contract draft](watch1-successor-contract-draft-20260915.json) deliberately leaves unavailable real policy/collector identities and review choices unset; it cannot be submitted to this evaluator as an executable contract. The [synthetic bundle](fixtures/frozen-policy-synthetic-20260915.json) is runnable and labeled synthetic throughout.

## Estimand and accounting

Each unit is a complete fixed-duration portfolio episode in one arm. Every episode must start with the same frozen equity as the declared initial capital, in addition to sharing that return denominator. Giving one arm extra starting capital cannot pass merely because its dollar profit happens to match. Daily increments from a continuing account with different starting equity are not supported as reset episodes. Its saved score is independently recomputed as:

`Y_arm = (end_equity − start_equity − external_net_inflow − costs_not_already_in_equity) / common_initial_capital`.

Open gains/losses belong in equity. Deposits are not profit. Do not subtract modeled execution slippage again when actual fill/equity values already include it. `cashflow_coverage=complete` and `cost_coverage=complete` are required inputs; their truth still needs the upstream accounting review. Accounting and source evidence are retained and hashed. Start/end mark timestamps must match the registered episode exactly. This module checks arithmetic and identity, not broker mark authenticity or omitted invoices.

For a **paired** design, both arms must have exactly one row for every frozen pair, the same opportunity/input hash, decision time, measurement identity and interval. The statistic is `mean(Y_challenger,i − Y_control,i)` with equal episode weights. A reference-arm outcome is never replaced by an estimated fixed constant.

For an **unpaired** design, the statistic is `mean(Y_challenger) − mean(Y_control)`; different arm sizes are allowed. There are no pair IDs and the arms are jointly resampled. This API supports explicit unpaired mechanics for future reviewed applications/tests; it does **not** authorize converting WATCH-1 into an unpaired experiment or treating unmatched candidate samples as interchangeable portfolios.

The effect is always challenger minus control. Direction and target are separate frozen fields. For `greater`, the margin is `effect − target`; for `less`, it is `target − effect`. The interval relation compares that margin with zero. A positive effect below a stated +0.05 target cannot be called support for +0.05. Equivalence/noninferiority margins, medians, quartiles, candidate R and selected realized winners are unsupported, rather than silently coerced into this estimand. WATCH's draft target is zero **net portfolio return difference**, not the legacy +0.05R candidate claim.

## Dependence and uncertainty

The method constructs connected components across **both arms**. Closed actual dependency intervals that overlap or touch are linked even across different symbols. An explicit, predeclared time buffer can add links; shared capital-path IDs, declared dependence keys and pair IDs add further links. Transitive chains remain one component. Actual interval coverage must contain the complete measured episode; a nominal five-day label cannot shorten an observed ten-day dependence interval. The returned evidence lists every component and member, arm counts and largest component share. It does not rename component count as a proven “effective n.”

Bootstrap draws sample G complete components with replacement, where G is their observed count. Every member of each drawn component travels together, including both arms and each reference observation. Paired differences are formed inside components before aggregation; unpaired replicate means include both arms' uncertainty. Pooled episode weights are preserved, so an average of component means cannot silently change the target. The confidence interval is the two-sided percentile interval, with explicitly implemented linear empirical quantiles, frozen seed and resample count.

This follows the cluster-resampling principle: within-cluster correlation must be retained, and inference additionally assumes independence across clusters; few or unbalanced clusters can undermine inference. **Our interval-component graph is a conservative engineering grouping rule, not a theorem establishing market independence.** [Cameron and Miller, A Practitioner's Guide to Cluster-Robust Inference](https://cameron.econ.ucdavis.edu/research/Cameron_Miller_JHR_2015.pdf).

Percentile bootstrap intervals use quantiles of the resampled statistic, and paired resampling preserves the same resampled indices across related measurements. The implementation uses only Python's existing standard library and does not add SciPy as a dependency. [SciPy bootstrap method documentation](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html).

Minimum components per arm and maximum component share are explicit frozen controls. They are **not sample-size guarantees**. If either fails, there is only a descriptive point estimate. Any bootstrap draw with an absent arm prevents an interval; unsuccessful draws are not silently discarded. A degenerate distribution also prevents an interval, with no normal-approximation or row-level fallback. A reviewable seed-dependent distribution digest is returned.

The output intentionally has `p_value=null`, `formal_evaluation_allowed=false`, and `promotion_allowed=false`. A bootstrap probability of being below zero is not relabeled as a calibrated null p-value. Interval-support labels are draft diagnostic relations, not confirmatory verdicts or multiplicity clearance. Fixed-target p-value calibration, a reviewed family-wide error policy, sequential stopping rules and activation/finalization integration remain future work. Repeatedly trying seeds, grouping rules, targets or populations after observing results is outside this draft.

### What WATCH-1 can actually establish

The approved September 21–October 2 comparison has two portfolios following one continuous market/capital path. Its primary result is **one paired portfolio episode**, with no interval asserting enduring edge. Dividing it into daily returns, repeated candidates or trades cannot create independent episodes: overlapping intervals and the persistent capital-path identity keep those records dependent. There is no permission to reset portfolios or extend the trial to manufacture more units. An eventual repeat-episode inferential study requires a separate reviewed sampling/independence contract and suitable calibration.

The actual paper account's two $200 weekly economic targets are separate from the challenger-minus-control estimand. The weekly score retains open P&L, inherited-book change, flows, all operating costs and the full-precision `150×12/52` subscription allocation. The evaluator does not calculate or certify that actual-account score. Neither a winning challenger nor a positive interval replaces either required weekly result.

## Frozen evidence and provenance

The caller supplies three externally pinned SHA-256 receipts: contract, sorted membership manifest, and sorted outcome bundle. JSON key ordering and row insertion order cannot change the answer. Each member also binds saved policy, measurement and input preimages; each outcome binds its saved accounting/source evidence. The evaluator checks every expected member exactly once. Missing, additional, duplicated, pending, invalid or malformed rows prevent a full-cohort estimate. There is no complete-case filtering, dropping losing rows, or older-row fallback.

Enrollment must fall inside the frozen bounds, before the decision; membership recording cannot occur after the episode starts. Available input cannot be dated after the decision. Measured windows have the same declared duration, and no completed observation can extend beyond the frozen analysis evidence deadline. Naive timestamps, booleans/numeric strings, NaN and Infinity are rejected. Synthetic and prospective modes cannot mix, and legacy/retrospective provenance is refused. Saved data is never automatically stamped prospective.

Receipts need an upstream append-only collector/review boundary. Hashes compare evidence with a trusted saved receipt; a caller recomputing all hashes after editing facts does not recreate that receipt. The engine cannot prove a dishonest timestamp, missing scanner opportunity or concealed shared capital path false merely from self-reported JSON. Input/mark/provider preimages, complete opportunity denominators, externally retained enrollment receipts, stable portfolio identity and prospective availability are therefore required end-to-end acceptance evidence. Raw JSON ingestion must reject duplicate keys before canonicalization; this Python API accepts dictionaries and does not implement a file/network ingestion endpoint.

## Reproduce and review

From an isolated checkout using the existing environment:

```python
import json
from pathlib import Path
from alphaos.stats.frozen_policy_inference import evaluate_frozen_policy

bundle = json.loads(Path("docs/research/fixtures/frozen-policy-synthetic-20260915.json").read_text())
result = evaluate_frozen_policy(bundle["contract"], bundle["members"], bundle["outcomes"], **bundle["receipts"])
for key, expected in bundle["expected"].items():
    assert result[key] == expected
```

The synthetic receipt file is checked by the test suite. Other adversarial tests cover target/direction, noisy controls, paired common shocks versus genuinely unpaired observations, partial/missing adverse rows, malformed numerics, tampered input/contract/receipt hashes, capital denominator and net cashflow arithmetic, mismatched horizons, future data, transitive cross-arm intervals, persistent capital paths, repeated within-component observations, component concentration and invalid resamples. A 100-seed independent Gaussian smoke check detects gross centering/variance regressions; its broad predeclared coverage band is not market, heavy-tail or serial-dependence calibration.

Validation on the isolated application base `208544b`: **56 new tests passed**. The combined affected selection passed **135 tests in 10.31 seconds**, including existing research-contract quarantine, replay maturity, PER integrity and registration-finalization tests. Counts overlap; no full-suite claim. Ruff and mypy passed for the new module/test scope. Independent review reproduced an unequal-starting-equity acceptance gap and a malformed-input exception; both now have fail-closed regression coverage. No production or broker call occurred.

## Remaining acceptance work

1. Independently review this exact code, contract and synthetic proofs; resolve findings before integration.
2. Complete the two-portfolio engine, immutable opportunity/member receipts and explicit exclusions, actual fill/capital/risk accounting, costs, marks and policy/input identities. No synthetic or legacy row may enroll.
3. Freeze the actual WATCH measurement/input identities and observation receipts before the approved prospective collection. The first economic starting mark is September 19 00:00 SGT; comparison entries start at September 21 US open. The economic cutoff stays October 2 23:59:59 SGT, even if evidence receipt or later held-position follow-up occurs later.
4. Keep the short run descriptive. Additional confirmatory inference needs a reviewed independent-unit model, adequate evidence, calibration and multiple-testing/stopping contract. This patch provides no new registration/activation path and repairs none of the other families' unsupported estimands.

This is a tested first replacement primitive, **not completion of R1 or the one-week scorecard**.
