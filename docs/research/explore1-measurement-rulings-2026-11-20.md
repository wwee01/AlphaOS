# EXPLORE-1 measurement rulings — CK, 20 November 2026 SGT

CK approved both pending questions in this task before any campaign close_ref
return was saved. This note records the user rulings; it is not a new
registration, evaluation, deployment instruction, or Claude audit.

## Fail-safe exclusion

Apply the existing `ai.labeller_health.is_openai_reject_row` predicate to
historical and future evaluator records. A matching original record remains
measurable and is preserved, with `ranking_eligible=false` and
`ranking_exclusion_reason=evaluator_failsafe`. Count it explicitly.
It occupies no ranking key: filter qualifying originals before selecting the
earliest record for each symbol/reference session. The approved original-row
rule still applies per candidate; this does not substitute a later re-evaluation
of that candidate for its earliest evaluation.

No source evaluation, label, registration, or legacy outcome is rewritten.
Arm and control-pool routing rules remain unchanged.

## Price adjustment

Request split-adjusted closes for both the candidate and SPY, with cash dividends
unadjusted. Persist `adjustment=split` in request and outcome provenance.
Each symbol's reference and endpoint prices come from the same response.
Do not fall back to raw prices or total-return adjustment when a request fails.
Existing non-campaign callers retain their raw-price default.

Alpaca documents `split` as a separate adjustment from dividend adjustment:
[historical bars](https://docs.alpaca.markets/us/reference/stockbarsingle-1).
The offline HTTP-boundary tests verify the requested parameter; they do not
claim to independently certify the provider's corporate-action data.

## Required repair and delivery gates

Claude's D2 is also repaired: transient committed-source proof failures defer
the whole scan and retry; they must not freeze an unavailable measurement or
ranking exclusion. Verified source mismatches retain permanent exclusions.

The governing registration contract remains `f7452f6`. The three production
registrations are unchanged and must not be rewritten or evaluated here.
The prior mapping `a641b3c` remains rejected by Claude's 19 November audit.
The successor needs its own exact-SHA audit and CK merge/deploy instruction.
API-credit restoration is a separate operator dependency, not fixed by these
changes. No production or broker mutation is authorized by this note.
