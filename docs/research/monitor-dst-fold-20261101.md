# Distinguish the repeated Eastern monitor hour

On 1 November 2026, production monitor starts were 05:52:01.554025 and
07:02:12.439633 UTC, 4,210.885608 seconds apart. The first was 01:52 EDT;
the second was 02:02 EST. Eleven retained worker attempts during the
repeated 01:00–01:59 hour returned `duplicate_lock`. The four quarter-hour
keys matched completed jobs from the first occurrence of that hour.

The existing interval key discarded the UTC offset and fold. The public
`market_now_et` path also converts through ISO text, producing a fixed-offset
datetime whose `fold` is zero even in the second occurrence. Merely checking
that parsed flag would leave the production bug unchanged.

The interval key now reconstructs the America/New_York fold from the instant
using existing `timeutils.to_et`. Only a second-fold interval gets the suffix
`:fold1`. For example, the 05:07 UTC monitor key remains
`monitor:2026-11-01T01:00`, while 06:07 UTC becomes
`monitor:2026-11-01T01:00:fold1`. The shared interval helper also covers
`outcomes_update`; its elapsed-time cadence remains authoritative.

Normal dates, first-fold keys, spring-transition keys, scan/window keys and
once-daily keys retain their previous spelling. The interval-due elapsed check,
JobRunner acquisition transaction, unique-index race backstop, ownership,
recovery, restart deduplication and failure gates are unchanged. This does not
backfill a missed historical job or force a monitor. No trading decisions,
risk settings, broker operations, production configuration or schema change.

The before-fix packet reproduced 19 failures and 12 passing controls on
`dc663658c2ea7e919f0745131aacfef00a302b6b`, including an actual JobRunner
second-fold refusal in an isolated SQLite database. The same packet covers
public UTC instants, both interval job types, seven configured interval sizes,
unchanged elapsed thresholds, normal/first-fold/spring keys, daily/window keys,
durable restart deduplication and no execution rows created by the fixture.
Network transport is denied in these focused tests.

The corrected focused packet passes all 31 cases. The affected packet passes
145 cases across the new regression, scheduler, isolated monitor, entry guard,
notification and monitor-summary suites (57.70 seconds). Ruff is clean for the
changed application/test files. Mypy reports no issues for the changed module;
its existing repository override remains unchanged. These are offline tests,
not a claim that this candidate has run in production.

Readiness did detect the historical stale monitor after its existing 30-minute
threshold: eight retained observations showed `monitor_stale` and
`entry_ready=false`. No order, fill, proposal, approval, risk check, exit or
outcome was created in the gap. Subsequent observed checks recovered, but
neither recovery nor current broker instruction coverage proves continuous
protection during the historical gap. The source/log evidence identifies
duplicate keys; it does not attribute the gap to diagnostic CPU load or sleep.

This isolated source change is not a deployment receipt. Deployment requires
the separately reviewed exact-source helper, preserved dirty overlays,
quiet-writer preflight, immutable ledger backup, ordinary post-boundary monitor
and restoration of the original service states. The active structural
diagnostic and all its source/caps/artifacts remain untouched.
