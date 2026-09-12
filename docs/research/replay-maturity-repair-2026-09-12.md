# Replay maturity repair — 12 September 2026

This change repairs measurement; it does not validate a trading strategy or
amend a frozen hypothesis. Production has not been migrated by development
tests or the copied-ledger checks recorded here.

## Changed behavior

The five-day outcome family keeps its existing `outcome_status`. Replay has
its own `replay_status`, `replay_bars_used` and `replay_method_version`.
Newly seeded rows use `bracket_maturity_v2`. An observed stop, target or
same-bar ambiguity finalizes the bracket; an open bracket requires the
whole requested horizon and its final close. Until then its final replay R
and result are withheld. A ten-day replay continues after the five-day
family completes, without restating completed forward statistics.

Migration adds nullable metadata. A legacy NULL version remains unverified;
an existing complete historical replay is not silently recomputed. The
baseline report exposes exclusions and uses a separately labelled,
descriptive repaired cohort. Trading horizons must match. A recorded
no-action rule remains a zero-R cash outcome, so abstentions are not
selectively removed. User-override outcomes never become the AI leg.
Passing the date floor does not authorize a formal test of the repaired
cohort against an unchanged old registration.

## Explicit historical correction path

`scripts/replay_corrections.py --db /path/to/copied.db --as-of-date YYYY-MM-DD`
is offline and dry-run by default. It opens SQLite read-only, does not run
migrations or load broker settings, and uses only cached bars. There is no
production database default. A missing stamped horizon is reported rather
than inferred from today's card.

`--apply` requires an already migrated copy and appends evidence to
`replay_evidence_corrections`. It never updates `candidate_outcomes`,
`preregistrations` or `hypothesis_proposals`. Each record preserves the whole
original row, exact computation inputs, corrected output and an input hash.
It verifies the original row is unchanged and recalculates the output from
the frozen input before insertion. Repeating an identical append inserts
zero rows. The evidence is retrospective and cannot leak into the repaired
baseline cohort. Do not use the older destructive `replay_recompute --apply`
tool for this repair.

## Verification

The selected suite contains 171 tests, all passing: outcome engine/tracker,
AILEG coherence, baseline, and `test_replay_maturity_repair.py`. New cases
include day-six stop/target/ambiguity after a five-day update; an unfinished
ten-day window; a missing final close; preserved legacy replay; immutable
source/frozen tables; repeated append; stale original or altered correction
rejection; byte-preserving dry run; and baseline validity/abstention/
override behavior. Existing pairing fixtures now carry writer metadata;
legacy exclusion has its own explicit test.

An 11 September copied ledger was replayed with a 10 September bar cutoff:
1,023 eligible historical records, 273 value/status changes and 68 still
pending at that cutoff. Of 383 ten-day rows, 262 changed; five three-day and
six five-day rows also changed under the current cache. These are diagnostic
differences, not all attributable solely to the ten-day maturity defect.
Cached observations can differ from the original fetch.

The input copy's SHA-256 remained
`30757c4008b2400bf1362736d8e3fa18de5029afb77b9e0720b6b035c162846d`.
On a further migrated scratch copy, the first append inserted 1,023 evidence
rows and the second inserted zero. Full-row hashes for candidate outcomes,
preregistrations and hypothesis proposals were identical before and after.
Evidence JSON and the scratch validation copy live in the coordinator's
`alphaos-repair-2026-09-11/evidence/` directory, outside this source commit.

## Limits and remaining gates

The caller/provider still owns bar ordering, forward-date selection and
completed-session provenance. The repair does not make cached data a
point-in-time archive, add execution costs, or solve shared-market/overlap
dependence. Existing complete legacy rows stay quarantined; sidecar
corrections are not silently promoted to confirmatory observations. Broader
metric contracts and executable finalization/promotion quarantine are a
separate required integration step. This commit alone is not authorization
to activate a repaired hypothesis or increase trading risk.
