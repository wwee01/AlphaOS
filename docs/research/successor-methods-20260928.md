# R1 successor implementation increment — 28 September 2026

This is a **draft, read-only research implementation**, not an activated study. It changes no original registration, frozen result, live policy, schema, scheduler, risk limit or promotion path. It does not complete end-to-end R1 acceptance. It supplies usable measurements, complete cohort exports and statistic-specific inferential primitives; remaining engineering and required review decisions stay visible.

## Executable scope

`scripts/export_research_successors.py` accepts an archived consistent SQLite copy, a capture timestamp and a new output prefix. It opens SQLite with `mode=ro&immutable=1`, enables `query_only`, installs a SELECT-only authorizer, checks integrity and verifies the input file hash afterward. It creates new mode-0600 JSON/Markdown artifacts exclusively, refusing existing output names and WAL/SHM inputs. Source-file hashes and the source revision accompany each export. It never instantiates an orchestrator or writes a ledger.

Example (run with the repository on `PYTHONPATH`):

```sh
python scripts/export_research_successors.py \
  --snapshot /path/to/preserved-copy.db \
  --captured-at-utc 2026-09-28T05:38:39Z \
  --output-prefix /path/to/new-unique-research-export
```

An optional `--draft-horizons /path/to/choices.json` accepts `{ "prereg_id": sessions }` for otherwise undefined horizons. Such choices are named **unreviewed draft sensitivity inputs** and change the draft identity. They cannot change an original numerical horizon, mutate the old registration or activate a study. The default export invents no undefined horizon or target.

Every selected member remains exported when its outcome is missing. Input-selected middle or unclassified members remain visible too. Ranking uses the complete available input population, not the subset with completed outcomes. The proposed tie rule uses fixed-count tails ordered by score then candidate ID; it is disjoint and deterministic, but needs review before prospective use. Original score/population definitions are not silently overwritten.

Source recorded/computed/decision/updated/resolved timestamps are checked before latest-row selection or ranking. Missing/malformed/after-boundary source timestamps produce explicit unavailable-source receipts. This conservative filter does not reconstruct earlier versions of mutable rows. PER uses one stable input clock, `candidate.created_at_utc`, for first-event and control chronology; adding an outcome row cannot change ordering. This explicitly proposed successor opportunity clock is distinct from the old outcome-derived decision clock. TTL executed membership derives from position-linked entry order/fill evidence and retains open outcomes; a closed-trade table never defines its reference denominator.

## Exact retained-family coverage

| Predecessor(s) | Implemented calculation/export | Current unresolved requirements |
|---|---|---|
| `prereg_e4900d5ee489` H-AI-1 | All `threshold_v1` opportunities, separate primary-proposed and final-proposed views; paired mean primitive, +0.05R target | Numeric horizon and meaning of proposed; original registry/date conflict; complete future enrollment. All-opportunity mean is not presented as the original proposed-only finding. |
| `prereg_bb6dc46834dd` H-TQS-1 | Full scored population and disjoint draft tails; **actual three-session bracket** reconstruction; two-arm mean, +0.30R | Ranking/ties/universe review, missing recorded levels/bars and full selected-member maturity. Preserves adverse old evaluation. |
| `prereg_7d9799b146e1` H-CAT-1 | Confirmed/none-found/unclassified input inventory and source records; two-arm mean, +0.20R | Horizon, card scope, searched-control availability and classification provenance. Missing/unsearched never becomes none-found. |
| `prereg_debff71c8fe4` H-INT-1 | Full core population and top membership; top mean minus **whole population median**, target zero | Horizon and reviewed ranking population. The selected top rows remain in the median reference. |
| `prereg_76c33938c0a4` H-WIN-1 | Literal SGT-before-noon versus other regular-session input membership; two-sided mean contrast draft | Horizon and explicit window/claim review. It is not silently converted to US morning/afternoon. Zero is the no-difference reference, not an invented useful magnitude. |
| `prereg_09080b322ff2` H-TTL-1 | Expiry history retained even if latest proposal status changed; actual trade-outcome reference records, not approved replay substitution; equivalence tail calculations available | Equivalence margin, horizon and realized-versus-counterfactual cost/risk basis. Original realized-R inputs are shown but not certified when basis is unresolved. |
| `prereg_37ec53a06798` H-REJ-1 | Nonmock rejection records, stored actual-minus-proposed values, single-mean primitive with literal nonpositive direction | Sign/policy interpretation, holding intervals and provenance. +1R historical rejection deltas remain +1R, never relabelled foregone loss. |
| `prereg_dfc7ac34b379` H-POL-1 | Conflicting/aligned/unclassified source membership; negative-direction two-arm mean at zero | Explicit review of conflicting replacing original divergent, horizon and classifier identity. |
| `prereg_09365c29313f` H-INT-SHADOW-1 | Separate shadow `instr1` population, same mean-minus-median primitive | Horizon/ranking, real levels and labels under unchanged collection safeguards; not pooled with core. |
| `prereg_89fcdf3974ff` H-AI-SHADOW-1 | Separate `instr1` baseline-pair inventory and paired primitive | Target/direction/horizon remain null, optional collection decision and absent observations; +0.05R is not copied from core. |
| `prereg_eb3ab6bda5a4`, `prereg_2821a9e1b931`, `prereg_e9c3cb494d50`, `prereg_45c5239c4d23` H-PER original/v2 | Earliest input-selected event and chronological symbol/date/tier controls before outcome selection; fixed date-tier/pooled ladder; every population guard reported; matched-event contrast primitive and shared-control dependence | Common-start market-adjusted measurement receipts/collector, historical card-content identity receipts, corrected successor identity, both directions in one declared family. Missing events/controls are retained; original October15/90-day requirements remain. These are four predecessor records, not four independent discoveries. |
| `prereg_35bbce762e7f` AI hold10 | Full `threshold_v1_hold10` membership; saved-bar paired reconstruction at ten sessions, +0.05R; distinct primary/final-proposed analyses | Meaning of proposed, missing outcomes, October5 original date, reviewed future enrollment. Candidate-R is not a capital-constrained portfolio test. |

The full original rows and linked registry records accompany these drafts verbatim. Unknown registration IDs receive an explicit unsupported result. Frozen TQS/INT/WIN values remain historical evidence and are never updated by this path.

## Measurement and provenance

`successor_measurement.reconstruct_bracket` calls the existing pure `replay_bracket_with_maturity`, using recorded entry/stop/target/direction and the copied ledger's `daily_bars`. It validates levels and OHLC coherence, unique scheduled dates, source/feed identities and finite values. Session one is the next US trading date after the **ET** decision date. All scheduled bars must exist, including after an early stop/target, and must first have been recorded after that session's conservative 16:00 ET close bound. This avoids treating cached intraday bars as final or selecting only early barrier completers.

Every result saves the entire plan, scheduled dates and bar preimages. An early barrier records its actual exit **session**, with a conservative daily end bound. `replay_bars_used` is never treated as holding duration. Early-close days conservatively wait until 16:00; daily bars cannot prove intraday touch order. Both-touched bars remain ambiguous. No-action control is explicit zero over the same horizon; unknown action/levels are not cash. A ten-session policy is never shortened to create a result.

These reconstructed measurements are **retrospective, gross and idealized**. They preserve the old engine's assumed fill at levels and do not model gaps, slippage, fees, capacity or full intraday paths. Saved prices and hashes do not prove that source inputs were complete or that a study was enrolled before outcomes. The export does not stamp legacy rows with prospective versions. Record-level source observation times, original method versions and exact source rows remain available for review.

PER's old adjusted-return field used candidate reference price versus a benchmark close reference. A corrected common-start market receipt is not available merely because the stored field is nonnull. The new event/control exporter therefore retains those old values as unverified reference evidence and explicitly blocks corrected measurements. It does not substitute another estimand to make PER appear implemented end-to-end.

## Inference mathematics and limits

`successor_inference.evaluate_frozen` is pure and accepts a draft contract, complete members, complete outcomes and **externally retained expected hashes**. Missing or extra outcomes, unknown intervals, duplicate underlying observations, changed inputs, mismatched pairs and unknown statistical methods fail closed. The hash helper creates integrity pins; it is not a trusted time/enrollment receipt.

The five statistics are:

1. Paired: average of each opportunity's `tested - reference` difference, with identical pair weights.
2. Separate arms: weighted tested mean minus weighted reference mean; both estimates vary in every draw.
3. Overlapping reference: weighted selected-top mean minus the weighted median of the **entire** underlying population; the reference is recomputed in every draw.
4. Single mean: weighted mean of explicit signed observations.
5. Matched PER: equal-event mean of event outcome minus that event's fixed control-set weighted mean. A reused control is one observation, not independent copies.

Closed measurement intervals are joined across **all symbols and both arms**, transitively, with declared additional dependence keys, pair identities and shared controls. An explicit nonnegative buffer can join nearby exposures. One Exp(1) multiplier is drawn per resulting component, and every linked observation travels together. Positive multipliers preserve all fixed supports; there is no per-draw reselection of events, quantiles or controls. Too few or concentrated components suppress inference. A continuously overlapping market cohort may legitimately collapse to one component. That is not repaired by treating symbols or days as independent.

For an observed statistic `theta` and draw `theta*`, intervals use uncentered draw quantiles. Tail diagnostics compare `theta* - theta` with `theta - target`, with the specified greater/less/two-sided direction and finite-draw correction `(extreme+1)/(B+1)`. Equivalence uses the larger of the two one-sided diagnostics at `target ± margin`; the margin is mandatory. These calculations fix the old empirical-bootstrap-tail-at-zero and frozen-control mistakes. The approach follows the existing repository's positive-weight/null-centering basis in `stats/two_arm.py`, extended to joint components and each actual statistic; it uses only the standard library.

This is an **asymptotic cluster multiplier approximation**, not an exact randomization test or a finite-sample guarantee. It assumes independent/exchangeable components after declared links and adequate support. Strong skew, few components, unequal cluster sizes, a nonregular/discrete median and unrecorded long-range dependence can defeat nominal coverage. The implementation returns `diagnostic_p`, never a formal p/q or success verdict. Original n/span floors are safeguards, not power guarantees. Existing bootstrap/FDR/finalization callers are untouched. Reviewing a future fixed test family, stopping rule and calibration remains necessary before confirmatory use.

`successor_analysis.analyze_export` supplies an executable bridge for complete exported inputs. It verifies contract/member/source/measurement hashes, retains every selected member including missing ones, and records all draft settings. Paired studies require an explicit primary-proposed or final-proposed view. Undefined horizons/targets remain unresolved; PER missing market receipts stay blocked. It cannot turn legacy exports into enrolled prospective evidence.

## Acceptance boundary and next engineering

This increment replaces unusable generic centered rows with reviewable member identities, controls, measurements and correct statistic primitives. It does **not** claim all fifteen experiments complete, valid prospective samples, calibrated formal FDR, weekly profitability, or autonomous strategy learning.

Codex still owns a prospective receipt collector that saves complete opportunity membership before measurement; immutable input/measurement identities including common PER marks; reviewed successor-only persistence/finalization and multiplicity/stopping integration; and independent review of this increment. CK's review choices concern concrete scope, undefined horizons/targets/margins, tie/window definitions and activation timing. No user decision excuses missing engineering. Absent labels, insufficient maturity/independent components and future original analysis dates remain genuine observation constraints after code is repaired.

Regression evidence includes the old endpoint-positive/stop-negative example, +0.02R failing a +0.05R target, noisy controls widening intervals, exact paired-shock cancellation, median/reference overlap, cross-symbol transitive intervals, reused PER controls, missing adverse members, source/target tampering, tied ranks, first-event-before-outcome selection and unchanged copied-ledger contents. These tests establish bounded implementation behavior, not a trading edge.
