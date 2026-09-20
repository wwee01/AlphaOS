# Enrichment deadline supervision and measurement — 20 September 2026

This is a bounded C1 repair from source `56a3f0f`. It does not close C1 or claim
better live completion. No provider call, production write, service operation,
paid dependency, source-list change, timeout increase, retry increase, shadow
latch change, or risk/strategy change was used to implement or test it.

## Observed problem

The retained September 20 04:53:31 UTC ledger has 142 enrichment attempts in the
September 14–19 interval: 71 completed and 71 hit the wrapper's own 45-second
deadline. The matched Tuesday–Friday intervals have 63/107 completed on
September 8–11 versus 42/96 on September 15–18. These are observational weekday
cohorts, not identical symbols/conditions or causal evidence of a regression.
The old ledger does not contain successful-call latency or individual provider
stage timing. It cannot identify the source that caused each timeout.

The current configured CLI profile is quick, six requested sources
`reddit,hackernews,polymarket,github,x,youtube`, 45 seconds, and the existing
per-scan cap. Installed provider 3.8.1 waits for retrieval futures before
returning final JSON (`lib/pipeline.py:399–482`). Its quick YouTube search still
has a 120-second inner subprocess bound (`lib/youtube_yt.py:322`); X can retry
30-second quick searches (`lib/bird_x.py:341–393`). Those are possible budget
conflicts, not proof that either source caused the recorded failures. Successful
cluster coverage in the newer 42 rows includes jobs35, Reddit34, HackerNews23,
GitHub5, Polymarket4, X0 and YouTube0. The provider itself adds jobs for company
topics; configured sources are not proof of actual attempted sources.

A concrete cleanup bug is proven: installed `lib/subproc.py` detaches helpers
using `os.setsid`. Killing only the outer CLI process group leaves such helpers
running. A local marker regression reproduces that old behavior and demonstrates
the fix for both `start_new_session=True` and `preexec_fn=os.setsid`, including a
helper's child in that detached group. Historical orphan counts and their
contribution to timeouts remain unknown.

Reproducible baseline evidence is retained separately at repair-root
`evidence/enrichment-repair-baseline-20260920.{py,json}`. It opens the copied
ledger immutable/read-only, verifies unchanged bytes, and hashes the inspected
upstream files. No old research outcome or receipt is overwritten.

## Implemented contract

The default command runs the same installed CLI and arguments through a small
AlphaOS stdlib bootstrap, using the same configured interpreter. It calls each
original stage exactly once and does not alter queries, providers, ranking,
sources, retries or returned JSON. Timed hooks cover planning, source retrieval,
supplemental/thin retries, finalization, reranking, GitHub enrichment and
clustering. Future missing hooks are explicitly incomplete/unavailable, never
synthetic zero latency. A started stage without an end receipt is incomplete;
it is not a successful or zero-duration stage.

The bootstrap retains direct `Popen` child handles. On the outer deadline it
receives SIGTERM and stops only child identities proven still its own. Under
CPython's wait lock, `waitpid(..., WNOHANG)==0` proves the child remains unreaped;
the lock prevents a competing waiter from releasing that PID for reuse before
signalling. A detached group is signalled only while that verified child is its
leader. After a bounded one-second cooperative grace, the outer wrapper uses
the same identity rule for its own process and bounds reaping to five seconds.
The configured work deadline remains 45 seconds; cleanup latency is separate.

This is **not a general guarantee that every possible descendant is gone**.
Already-reaped/exited parents and unverifiable identities are counted as
`unverified_descendant_roots`. A grandchild independently detaching from an
already-exited parent cannot be attributed safely by this mechanism. Those
groups are not blindly killed. Custom command templates remain uninstrumented:
their outer group cleanup is retained, and missing stage/descendant assurance
is explicit. The installed provider's other native process behavior is not
certified by this adapter. The identity guard relies on CPython's Popen wait
lock; unsupported implementations report unverified identity rather than
signalling an unverified PID.

The bootstrap keeps its owned process factory through exit, so a provider daemon
thread cannot bypass registration during the cleanup window. Tests also cover
startup import failures, malformed output and explicit stage errors.

## Receipts and honest comparisons

No schema migration is needed. Existing `system_events` receives a started and
finished `last30days_attempt_v1` receipt under category `last30days`, sharing an
attempt ID and existing candidate/packet IDs. These record total elapsed time,
configured source/profile/deadline, completion versus timeout/error, returned
source coverage, source-error names, and bounded stage metadata. The provider
entry-script SHA is recorded; it is not a hash of every installed library.
The temporary telemetry descriptor contains only allowlisted metadata, never
raw stderr/stdout, command arguments, prompts, article bodies, URLs or exception
payloads. Partial raw JSON is never used as research and no labels are invented.

The stage adapter reports hook coverage separately from overall completion and
cleanup assurance. A source function returning does not prove that source
supplied useful evidence. Provider `errors_by_source` names remain visible even
when the overall result contains usable clusters.

Telemetry persistence failure must not crash optional enrichment. It preserves
the same `to_packet_dict()` fields and official-label behavior, marks the
existing last30days row's `reason=runtime_telemetry_incomplete` and operational
risk tag if that row can subsequently be saved, and does not expose database
error text. This gap must be included in a later audit, not excluded from the
denominator.

For the extension review, use a fresh copied ledger and:

1. Join started/finished receipts by attempt ID. Count the union, unfinished
   starts, orphan finishes, duplicate receipts and telemetry-incomplete rows.
   Disabled/budget-skipped candidates are not attempted provider calls.
2. Report completed/attempted, timeouts/attempted, other failures and unfinished
   attempts. Split returned empty results and source errors from useful context.
3. Report measured p50/p95 separately for all finished attempts and successful
   attempts; include sample counts. Do not compare against invented old latency.
4. Group by provider, entry-script SHA, requested sources/profile/deadline and
   comparable weekday/session/symbol cohorts. Describe selection differences.
5. Count incomplete retrieval stages at deadlines by source, actual elapsed
   completed stages, missing hooks and unresolved cleanup roots. Concurrent
   source times overlap and must not be added as total wall time.

This gives the evidence for a specific next decision: if an identified optional
source consistently exhausts the shared deadline with little usable evidence,
review source selection or explicit per-source budget allocation. Do not
increase the global timeout blindly, buy a feed, or silently skip/retry queries.
The current patch removes demonstrated escaped-helper accumulation, but leaves
sequential per-candidate attempts and the existing caps intact. Actual completion
or scan-latency improvement requires postdeployment observations. Shadow's 80%
coverage condition and existing latch/canary requirements remain unchanged.
