# Durable first-family turns — 21 September 2026

This bounded L2 repair changes scheduling only. It does not change outcome
math, maturity, provenance, per-family row limits, research/entry policies,
failure fuses or frozen evidence. No production deployment is performed by
this patch.

## Reproduced problem

On base `00ca00924253ce9b36292af795d671bf3b9ac017`, the forward/replay block
always ran first. The new regression inserts one ready HOLD1 row and four
persistently partial forward rows, interrupts the second provider request,
then closes and reopens the ledger. The next call previously began with
`FWD_2`, not `HOLD_READY`; the waiting HOLD1 row could continue to be starved
through repeated external interruptions. This reproduces the independent
September 20 liveness review. It is a synthetic interruption, not a claim
that the research worker has a configured runtime kill budget.

## Dispatch contract

`outcome_family_dispatch` is an additive, initially empty scheduling table.
Its singleton `dispatcher='candidate_outcomes'` stores `next_family`,
`revision` and `updated_at_utc`. Existing outcome rows and both existing
`outcome_processing_cursors` records are not migrated. Schema version remains
3 under the existing additive-table startup mechanism.

Before either family starts, a short `BEGIN IMMEDIATE` transaction saves the
opposite family as next. If processing is interrupted in the first **or
second** block, the opposite family starts the next eligible recovery call.
After both blocks finish normally, another short transaction leaves the
opposite of this call's original first family as the next first turn. Thus
successful invocations alternate their first family as well.

The first turn is forward on a new ledger. With a provider and positive
limit, even empty family blocks consume a turn. An empty first block can
therefore legitimately mean the second family makes the first actual
provider request. With no provider or limit zero, neither dispatch state
nor per-family cursors advance; invalid/negative limits still fail before
any scheduling mutation.

Each family runs at most one block at the unchanged per-family limit. If
forward runs first, rows it newly completes may receive HOLD1 processing in
the same call. If HOLD1 runs first, those newly eligible rows wait until a
subsequent invocation; there is no second HOLD1 burst exceeding the limit.
The existing frozen high-water/cursor rules and backlog can delay that row
beyond the immediately next call; this is not a one-invocation latency bound.

The return dictionary retains its existing shape: top-level counts refer
to forward/replay and `hold1_10d` contains the continuation counts, regardless
of execution order. Provider/calculation errors retain their existing
family-local counters and handling. Uncaught errors still propagate.

Every turn is committed before row claiming or provider work; no provider
request runs in the scheduling transaction. Revision checks reject stale
overlapping dispatchers and roll back without overwriting the newer turn.
This is not a replacement worker lease: the existing research-worker
process lock and failure fuse remain responsible for ownership/recovery.
No repair promises progress while all future calls are stopped by a fuse,
or if every process is killed before doing any useful work.

## Validation scope

`tests/test_outcome_family_dispatch.py` covers the old failure, symmetric
first/second-block interruptions, crashes before either block's first row
claim, real ledger reopening, normal alternating order, empty families,
positive caps and unchanged result shape, no-provider/zero/invalid limits,
new HOLD1 eligibility in both orderings, caught provider errors and stale
revision rollback. Existing fairness, maturity, baseline, quarantine and
schema tests provide the affected regression coverage. Exact run counts
and independent review artifacts are recorded with the delivery evidence;
they are not a full-repository test claim or evidence of new trading edge.
