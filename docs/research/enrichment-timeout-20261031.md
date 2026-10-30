# Reddit enrichment attempt deadline — 2026-10-31

This candidate changes only the default reviewed last30days worker adapter.
It is not a provider, source, model, ranking, strategy, or timeout-setting change.
Custom commands remain unverified and retain their existing behavior. The
installed last30days 3.8.1 files are read and pinned, never edited.

## Evidence and bounded repair

The retained October 26–29 cohort has 48 timeouts among 124 completed attempts.
Every timeout has an unfinished Reddit retrieval; that receipt does not identify
the inner request or prove an HTTP, rate-limit, or model cause. A newer partial
Friday cohort has 32 timeouts among 35 attempts. Neither observation establishes
that the defects below caused those individual failures.

Offline execution of the exact installed function bodies demonstrates two
reachable defects: a Retry-After value of 120 requests 120-second sleeps under
an outer 45-second attempt, and an enrichment wait of .005 seconds still takes
about .08 seconds when its executor context joins a running .08-second task.
The production enrichment budget is itself 45 seconds, starting after planning
and discovery. These local budgets do not compose into the attempt budget.

The new stdlib-only `last30days_reddit_deadline.py` carries the parent's original
monotonic start and configured deadline. It never resets that clock or reserves
an arbitrary earlier cutoff. Reviewed Reddit request socket timeouts and
keyless enrichment waits are limited to remaining time. Retry or throttle sleeps
that cannot fit are refused before sleeping; malformed waits are also refused.
The original request arguments, headers, sources, ranking, retry counts and
fallback ordering remain unchanged for work that finishes within the budget.

Any deadline refusal is sticky for the entire attempt. It raises a dedicated
BaseException so existing upstream “catch Exception and return empty” handlers
cannot convert it into an empty success. The existing worker supervisor is
asked to stop immediately, so executor context managers cannot keep the worker
alive while joining uncancellable tasks. A final pipeline check also rejects
swallowed refusals. The provider reports TimeoutError and unavailable context,
while previously returned, verified structural partial receipts remain
ineligible for model context. No new partial Reddit result is manufactured.

Socket timeouts are inactivity limits, not total request deadlines. Blocking
DNS, response reads, thread joins and native calls still depend on the existing
parent process timeout and owned-child cleanup. That boundary is unchanged;
independently detached descendants remain outside its guaranteed coverage.

## Verification and diagnostics

The source-routing pins remain unchanged. Eight additional selected Reddit and
HTTP module files are verified before upstream import and again before hooks
are installed; imported module paths and function signatures must agree.
Unknown versions fail before source work. These are selected routing/transport
source pins, not a claim to authenticate every transitive dependency or a
historical execution. The six adapter file hashes include the new guard.

Module-local proxy references and thread-local scope leave shared time/urllib
modules and other sources' requests untouched. The adapter records bounded,
allowlisted phase, request, HTTP-status, wait and refusal metadata. It never
records request URLs, queries, tokens, headers, bodies or exception messages.
Ordinary phase/request events are capped at 128 with omission counts; refusal
and contract summaries are separate, subject to the existing overall telemetry cap.

A completed deadline contract means that the reviewed pipeline returned within
the original budget without a guard refusal. It does not certify that every
internal source fetch succeeded: existing upstream empty/error fallbacks remain.
Failed installation and failed pipeline runs cannot emit a completed contract.
Ordinary upstream failures are not relabeled as deadline failures.

## Validation and rollout limits

Focused tests cover remaining-time arithmetic, exact wait boundaries, malformed
waits, late responses, sticky refusal, unchanged fast results, privacy, unrelated
thread/source behavior, unsupported source pins, actual worker-to-provider
failure, retained partial evidence, and parent cleanup of stalled bodies and
executor joins. Existing source-policy, partial, provider and process-supervision
tests remain applicable. Temporary synthetic fixtures carry explicitly replaced
test-only pins; production allowlists are never relaxed.

Evidence is retained in the repair workspace:
`enrichment-timeout-before-20261031.{py,json}` and
`enrichment-timeout-exact-function-after-20261031.{py,json}`.
They use exact installed AST bodies with offline transports, and establish no
natural availability gain. A deployed observation cohort must separately measure
attempt status, inner-phase refusals, retained evidence, latency, and unfinished
calls. Faster explicit refusal is not recovered research or a completed source.
