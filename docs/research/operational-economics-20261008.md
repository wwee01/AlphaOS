# Operational common-close cash-basis observations

This increment connects the existing manual cash-basis accounting engine to the
existing `benchmark_spine` job and relative-performance report. It collects one
bounded paper-account observation, archives its normalized source evidence, and
compares consecutive archived endpoints when the evidence reconciles. It is a
descriptive accounting report, not a trading input or research activation.

## Operational surfaces

`alphaos.reports.operational_economics.capture_operational_economics(journal,
settings, should_yield=None)` is called after the existing provisional benchmark
capture. The production source is the existing `EconomicSource`: explicit paper
settings, GET allowlist, rejected redirects, configured feed, and existing
pagination/request limits. Mock/offline/missing-key settings do not construct a
source. No new source configuration, paid feed, strategy, risk gate, schema, or
scheduled job is introduced.

The dedicated research role supplies its existing scan-window budget. The new
collector checks it before work and before every transport GET, then after
collection. It records incomplete attempts as `yielded` and may resume a yielded
attempt on the next invocation. A completed source receipt can be retained even
if the job yields at the final check. This does not cancel an in-flight request,
create a total network deadline, or add cooperative checkpoints to the preceding
legacy capture. The existing scheduler admission remains in force. Manual/all
role semantics retain their existing lack of a scan-window callback.

There is no catch-up loop. A non-yielded attempt is recorded at most once per
Eastern-market attempt date, including an unavailable attempt. An exact same-date
retry returns the existing receipt identity rather than selecting a new price.
A complete capture that yielded only at the final budget check is also reused
without another GET; a genuinely incomplete yielded capture remains resumable.
A same or older valuation cutoff never replaces the previous archived endpoint.

`build_relative_performance_report` adds `operational_economics` to its normal
JSON, and the existing command's Markdown renderer displays the state, interval,
account gain, eligible SPY comparison, blockers, coverage floor and cost limits.
The old provisional-equity fields remain unchanged and do not become eligible
returns. This increment does not add a dashboard or daily-brief presentation.

## Receipt and read contract

Receipts are inserted into the existing `system_events` table under
`operational_economics_receipt`, version `operational_cash_basis_economics_v1`.
The envelope pins its event ID, source/feed/method contract, previous receipt,
chronological opening endpoint, attempt and actual recording clocks, normalized
source observations, request count, capture state, and recomputable result.
Provider account IDs are hashed by the existing source adapter; descriptions,
credentials, and raw exception messages are not persisted by this integration.
Reports expose only bounded accounting fields and receipt identifiers, not raw
activity/account payloads.

These are application append-only receipts with hash and semantic checks, not
database-trigger immutability, external provider authentication, or proof against
an actor rewriting the entire database. No existing row is rewritten or promoted.
Every receipt in the chronological chain is checked on read and before append,
including failed attempts; each opening must follow the last archived endpoint,
even when that endpoint itself was blocked. An intermediate failure cannot choose
an older favorable opening. Missing, malformed, foreign, future-dated, or changed
ancestors make the current evidence unreadable. The chain is bounded to 512
receipts and 64 MiB in total, with at most 8 MiB per receipt. Reaching capacity
refuses another receipt; bounded archival/rollover is unfinished engineering.

All timestamps supplied as receipt clocks must be aware. Actual recording must
follow the attempt and all retained source observations and cannot be in the
future. The opening receipt must already have been recorded before the ending
valuation cutoff. Source requests run outside a SQLite transaction. A short
`BEGIN IMMEDIATE` rechecks the exact previously observed row and complete chain,
then atomically appends the new receipt. Concurrent changes or failed writes do
not overwrite prior data. The report uses one read snapshot, preserves a caller's
transaction, and performs no source calls or data writes.

The newest attempted state is the headline. A newer started/failed benchmark job,
or a newer completed/yielded job without a valid new receipt, withholds an older
comparison. Missing operational metadata in a newer job is not considered a
legacy success once this receipt contract exists. A matching idempotent retry can
refer to the same receipt. Scheduler `completed` means the attempted job ended;
its nested measurement may still be waiting, blocked, or unavailable.

## Measurement and cost boundaries

The imported accounting method is
`common_minute_reported_cash_economics_v1`. Its source, endpoint reconciliation and
arithmetic are unchanged. It reconstructs the entire designated account NAV,
including cash and open/inherited holdings, using the same final regular-session
minute convention as SPY. It does not promote a broker portfolio-history point
or sum only closed trades. The separately retained strict common-valuation
diagnostic may be blocked by date-only cash activity while the cash-basis engine
can accept an unambiguously interior internal cash event. Those two statuses are
reported separately.

The first valid endpoint is `waiting_for_second_endpoint`. No historical opening
is regenerated from current holdings or imported automatically. Two independently
archived endpoints can produce `reconciled_asof` after the original cash,
quantity, fill, activity and corporate-action checks pass. Any external flow,
including offsetting transfers with net zero, withholds return/excess comparisons
without appropriate subperiod valuations. Missing historical marks after a full
post-cutoff sale, ambiguous date-only cash, late prior activity, incomplete pages,
unknown activity types, and inconsistent account/feed evidence remain blockers.
Reported broker fee cash is already in account cash and is not deducted a second
time. This remains an as-of cash-basis comparison, not independently verified
total return or corporate-action/dividend completeness.

The agreed $150 monthly subscription convention is shown separately as
`150 × 12 / 52 = 450/13` USD per seven days, allocated over the exact interval
(for example, `450/91` USD for one day). This is not the older calendar-month
scenario and is not an invoice/payment receipt. Its inclusion in account cash has
not been reconciled, so no subscription deduction is made. Other actual bills
and shared API allocation are unknown. `subscription_only_adjusted_gain_usd`,
`all_in_net_gain_usd`, and `weekly_target_verdict` remain null.

The original trial weeks (September 18 16:00 to September 25 16:00 UTC, and
September 25 16:00 to October 2 16:00 UTC) are not replaced by these forward
regular-session-close intervals. This operational connection cannot establish
the original trial verdict retrospectively.

## Bounded history and remaining work

The existing engine detects revisions by requiring the closing source to cover
the entire opening receipt's activity creation-time query. The lower bound is
therefore retained across subsequent observations and its lookback grows. The
receipt/report explicitly shows `activity_coverage_start_utc`, lookback age, the
history policy, 10 activity pages of 100 rows each, and the existing maximum of
37 GETs per new capture (separate from the preceding legacy capture). Hitting a
page/request cap records the latest attempt as unavailable, not an old success.
Source observations or receipts exceeding storage bounds are also refused; the
job/report guard exposes the absent new receipt.

A safe bounded-history rollover contract remains necessary before those limits
are reached. No truncation, pagination relaxation, hidden extra capture, or
reclassification of missing history as zero is introduced here. Full ancestral
validation has explicit storage bounds rather than an unbounded scan.

Acceptance requires offline public-path positives and adverse tests, independent
review, exact-source preservation, and separately observed natural receipts after
deployment. Synthetic tests are not natural account performance. The routine
benchmark schedule can supply a first receipt and only a later independent
endpoint can supply a forward interval; no job should be forced to manufacture
that evidence.
