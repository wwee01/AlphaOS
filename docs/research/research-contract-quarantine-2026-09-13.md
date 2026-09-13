# Research repair: executable quarantine and draft successor contracts

Status: local repair for review. No production ledger mutation, no new hypothesis activation, no promoted card, and no historical verdict restatement.

## What is now enforced

The generic storage-writing `evaluate_hypothesis` refuses the seven audited PR12 metric functions, the AI/baseline delta families, shadow-tier contracts, and PER contracts. It detects the original registered parameters and linked proposal identity; strong-prior and operator-approval flags do not bypass quarantine. The paired PER writer preserves identity/calendar/sample diagnostics but, even with a full valid population, refuses to freeze the repaired event membership under original or v2 registrations. Existing evaluated rows retain their immutable evidence and continue to be readable.

The resolver returns a separate `quarantined` list with reasons. Full-family BH calculations still include every previously evaluated row, and their raw verdict/q values remain unchanged. Those results now carry `research_validity` and `forward_test_eligible`; a quarantined raw positive verdict is not eligible. Promotion preconditions use the same validity guard, including for operator-marked MET hypotheses. The existing progress report never labels such a contract `resolver_ready`.

This targeted guard is not certification of other, unaudited registrations. A new method name cannot be treated as approval: the successor review and evaluator still have to be implemented before any successor is used.

## Small data-contract fixes

- H-POL's descriptive query uses the actual classifier's `conflicting` value, rather than the nonexistent `divergent` value. The old H-POL registration is preserved and blocked from freezing that changed cohort.
- PER chooses the earliest exact decision timestamp per event; candidate ID breaks exact timestamp ties only. It uses the America/New_York decision date. The CLI locates the identity-frozen v2 pair by its exact registered texts; it cannot substitute the original pair, and reports the selected v2 IDs together with quarantine.
- Future unresolved AI/baseline calculations and the explicitly requested historical correction path use the decision's US market date, including winter and DST midnight boundaries. Existing completed replay values are not automatically recomputed. The separate unfinished ten-day forward-stat family uses the same anchor when it receives new data.
- `replay_recompute --apply` is retired at both CLI and direct API entry points. Its old UPDATE helpers are removed. Use the explicit copied-DB `replay_corrections` preview and append-only path; old original outcomes and frozen evidence stay intact.
- The first maturity repair's replay statuses are explicit vocabulary literals, and its two typing errors are corrected.

Daily-bar session completion and feed provenance still belong to the provider/caller contract. This patch does not certify an in-progress daily bar as a completed session. It also does not repair the old centered-arm inferential methods merely by adding quarantine. The paired report remains descriptive, and historical correction sidecars are never silently included in original tests.

## DRAFT contracts for CK/Claude review — not preregistrations

All drafts require a **future cohort opening timestamp**, an outcome method version, immutable implementation/card/prompt/selector hashes, a fixed end-of-enrollment date, a maturity lag, and an analysis date after that lag. Specify a maximum extension rule in advance, or close as underpowered. Do not stop when the result first looks attractive. No observation used to choose these drafts may enter their confirmatory cohort.

At enrollment, append the eligible decision/event identifier, timestamp, original features, treatment/arm assignment, control rule, symbol, data-source time, and rule version. At analysis, freeze both arms' complete row IDs, exact values, actual holding intervals, source bar IDs/hashes, exclusions and reasons, and the input/membership hash before resampling. Never reselect arms, thresholds or controls inside a bootstrap replicate. Missing/ambiguous outcomes require a predeclared disposition and sensitivity analysis; absence of an outcome is not a free exclusion.

Uncertainty must include both compared arms. Use paired differences only where pairing is real and fixed. Otherwise use a jointly resampled two-arm contrast with symbol and calendar dependence assessed using actual exposure intervals (entry through observed exit or scheduled horizon), including cross-arm overlap. A proposed bootstrap's coverage and null rejection rate must be calibrated on simulated dependent data before freezing a production claim. Counting candidate rows is not effective N.

| Draft successor family | Estimand and cohort to review | Direction/target and unresolved decision |
|---|---|---|
| TQS | Fixed prospective universe and version; score at decision; mature 3-session R; top vs bottom quartile boundaries fixed from prior training data, with both arms represented | Review whether the economic claim remains top-minus-bottom > +0.30R. Test against the target, not merely zero. Freeze quartile policy and required N for each arm. |
| CAT | Confirmed catalyst vs explicitly searched `none_found`, matched by declared universe/session/regime and one fixed holding horizon; exclude unsearched/missing catalyst states from both arms by policy | Review +0.20R target, horizon and a realistic prospective control-collection plan. Sparse controls cannot be repaired by resampling the same few rows. |
| INT core and shadow | Separate declared universe tiers; frozen score/decile boundaries; top-decile outcome vs a clearly defined reference population with its median uncertainty included | Review whether reference is median return, middle-score cohort, or mean; this choice changes the estimand. Specify minimum useful uplift and keep core/shadow hypotheses distinct in the family. |
| WIN | Fixed US market-time session windows and scheduled exposure opportunities; jointly estimated morning-minus-afternoon R for comparable horizon/population | Decide exact ET windows and directional economic target. Same symbol's overlapping windows and common market days must be clustered, not counted independently. |
| TTL | Expired vs comparable executed opportunities using the same counterfactual outcome definition, or a clearly named policy comparison | Decide equivalence margin ±epsilon and use a valid equivalence procedure. Failure to reject zero does not establish equivalence; realized-vs-replay differences need a separately justified estimand. |
| REJ | Every prospectively presented proposal with the operator decision timestamp; fixed foregone-R definition and actual overlapping exposure intervals | Review a nonpositive or materially negative target and its test direction. Positive-edge PORT-1 terminology cannot adjudicate the rejection claim. |
| POL | Producer `conflicting` vs `aligned`, classifier/hash frozen, confidence/coverage filters declared, one mature horizon, jointly estimated arm difference | Review a negative conflicting-minus-aligned target and the intended policy consequence. Correct vocabulary changes membership; old registration cannot absorb this silently. |
| AI baseline, hold10 and shadow | Fixed AI policy including exactly which decisions/abstentions are enrolled, against a specified frozen baseline; same candidate and same horizon, with deliberate no-action cash defined across that horizon | Review whether +0.05R remains the target; choose per-policy 3/5/10-session comparisons or one horizon. Use actual paired membership and jointly resampled market-day blocks; do not pool unequal horizons as the same economic claim. |
| PER positive/negative | One first eligible exact timestamp per symbol/earnings event; declare ET date binding, all event/control matching and exclusion rules, selector/card identity and source availability | Review the new chronology contract before an identity-frozen successor. Freeze primary/control membership once, jointly resample both arms, keep placebo diagnostic separate, and explicitly define directional targets and family inclusion. |

The family is to be specified prospectively across all confirmatory claims, including paired directional and core/shadow claims. Retain old results as historical, quarantined evidence and keep their existing raw FDR family view intact. Do not remove failed old results to make a new dashboard look stronger. A new confirmatory family boundary, number of tests and treatment of paired directions require explicit review.

Decisions needed before activation: economic targets, cohort/policy and horizon choices, collecting adequate controls, fixed calendar/stopping rule, and reviewed uncertainty/independence validation. These are decision points, not changes this patch makes automatically.

## Validation

The final acceptance run passed 397 tests across `test_research_contract_quarantine`, `test_replay_maturity_repair`, `test_stats_preregistration`, `test_stats_fdr`, `test_s1b_registration_integration`, `test_s1b_per_evidence`, `test_s1b_integrity_followup`, `test_aileg1_replay_window_coherence`, `test_pr12_hypotheses`, `test_pr13_2_promotion`, `test_vocab_guard`, `test_baseline`, `test_outcomes_tracker`, `test_outcomes_engine`, and `test_learning_tab` (42.12 seconds). Ruff passed the backend and all changed Python files. Mypy passed all 194 backend source files.

Against the explicit read-only preparation-ledger copy: all 15 registrations were identified as quarantined; direct generic evaluator calls blocked all 12 unevaluated registrations before a write; all three frozen results remained immutable; zero frozen results were forward-test eligible. Copy SHA-256 before and after was `30757c4008b2400bf1362736d8e3fa18de5029afb77b9e0720b6b035c162846d`. Full structured evidence is retained outside the repository in the repair workspace's `evidence/research-contract-quarantine-validation.json`. This proves copied-state guard coverage; deployment and the integrated full-suite check remain the integrating agent's responsibility.
