# Paper execution crash recovery, October 1, 2026

This bounded repair addresses reproduced split commits and stranded, identified monitor ownership. It changes no strategy, risk limit, calendar, kill switch, scheduler cadence, production configuration or SQL schema.

## Transaction and ownership contract

- A broker time close commits its outcome, original accounting receipt and durable `complete` phase together. Failure before commit rolls back those writes; previously recorded cumulative fills remain available for idempotent reconciliation. Failure after commit cannot leave the new intent `working`.
- Each monitor OS-lock acquisition has a new epoch. Nested calls retain that epoch. The started job and started monitor pass each receive a row-bound ownership event in their own start transaction, including exact pass-to-job linkage.
- The next owner may atomically mark both interrupted rows failed only while holding the same lock inode under a different epoch. A live owner, reentrant callback, missing/malformed/duplicated receipt, unrelated parent, replaced lock inode or failed transaction does not authorize recovery. Time elapsed is never death proof. Fresh successful reconciliation remains necessary for readiness.
- A recovered interruption counts as a failure. Scheduled dispatch rechecks the existing fuse after recovery; manual named jobs retain their existing explicit fuse bypass, but cannot bypass ownership proof. Research stale-job behavior is unchanged.
- An accepted close gets at most four additional application-level exact-order reads, separated by half-second waits, stopping on terminal status. Each call retains existing transport timeouts and SDK rate-limit handling, so this is not a four-HTTP-request or two-second total-time guarantee. No submission retry is added. If still pending, the existing incident and next-pass reconciliation remain.

## Explicit limits

Generic `scheduler_run_job monitor` no longer clears unreceipted historical monitor rows by age. These remain blocked pending a separately reviewed stopped-worker recovery procedure. An old closed origin with an unfinished intent likewise remains unchanged and unready; a saved receipt alone cannot prove the entire old economic chain. No historical receipt, timestamp, order or outcome is reconstructed. No such orphan was observed in the parent's current predeployment ledger.

The original attempt to automatically finalize old orphan phases was rejected during independent review because a receipt/outcome match alone did not prove all underlying fills and exit arithmetic. That mutation path was removed, rather than admitted on weaker evidence. Every unfinished manual monitor pass now blocks readiness, including one older than a later success.

The confirmation window reduces avoidable observation delay for fills arriving within it. It does not guarantee immediate broker execution or eliminate later-pass accounting when fills, position snapshots or transport remain uncertain. The existing D5 quantity-change rejection, strict N1/N3 absence/rejection proof, held-position lineage checks and aggregate-origin safeguards remain authoritative.

## Validation

Offline author regressions cover transaction faults before and after economic close, real subprocess `os._exit`, nested callbacks, atomic job/pass recovery rollback, replaced inode, fuse threshold, old orphan refusal, old manual-pass readiness, delayed cumulative fills and budget exhaustion without duplicate POST. Existing execution, receipt, scheduler and entry-readiness suites are retained; legacy age-reaper assertions now explicitly test refusal while retaining research reaper/fuse coverage. Independent review and deployment proof are separate acceptance steps; this document does not claim deployment or natural recovery observation.
