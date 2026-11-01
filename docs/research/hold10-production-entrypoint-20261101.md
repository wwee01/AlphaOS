# Optional HOLD10 production caller bridge — 2026-11-01

This candidate connects the existing qualified, inactive expected-runtime collector to `scheduler.jobs.run_scan_job`. It does not create a registry, register a study, freeze an expected manifest, activate research, run inference, or call a provider. It is disabled by default. This is engineering preparation, not proof of prospective delivery or inferential readiness.

## Imported foundation and caller behavior

The eleven collection/runtime modules and optional Orchestrator hooks are byte-identical imports from reviewed `cd4eadbe16d83fb7e86d3f09878523c875196fe2`. No existing collection schema, historical receipt, methodology, eligibility rule or capacity is changed. The foundation retains the full batch/core population, unresolved shortlist members, source and runtime gaps, fixed deadlines and inactive flags. A complete runtime receipt is not proof of a usable final plan or a globally authenticated execution environment.

With neither `ALPHAOS_HOLD10_COLLECTOR_CONFIG` nor `ALPHAOS_HOLD10_COLLECTOR_CONFIG_SHA256` present, the scheduler takes the existing path without importing the bridge, reading its files or installing a sink. The kill-switch branch remains before the optional bridge and performs no collection work. If either variable is present, both must identify a valid externally pinned configuration. A missing/invalid configuration produces a sanitized collection refusal while the ordinary scan continues. No path, raw manifest, secret or exception message is included in that disposition.

For an accepted configuration, an instance-scoped expected-runtime sink surrounds the actual scan. Existing begin/finish hooks record the actual selected callables, settings scope and input lineage. Missing finish hooks, skipped AI or failed writes cannot become runtime agreement. The previous sink is never overwritten; an already configured sink refuses this bridge. The sink is restored on return or exception. Original scan errors remain errors. Cost-cap degradation retains all saved candidates and its incomplete/no-AI evidence, with no additional provider calls. Scan/trading success and research capture status are separate.

The bridge preserves a full immutable SQLite backup after the scan and before the outer JobRunner terminal update. Thus this snapshot honestly contains the current job as started. New successful JobRunner completions now populate their existing `scheduler_run_id` column only from the exact returned scan/run pair: both records must be newly produced, completed, correctly linked, and within this job's aware start/finish interval, with no prior job reuse. This is prospective metadata only: no historical backfill, nearest-time matching or schema change. Failed, malformed, foreign and stale results remain unlinked. A later snapshot can expose completed slot evidence to the unchanged exact-link reader.

## Existing authority and configuration

The public API is `alphaos.research.hold10_production_collector`. Configuration is a regular, single-link JSON file within a recognized retained repair workspace, with an external SHA256 of its exact bytes. The workspace marker is only a safety boundary, never authorization to activate research. Configured application ancestors, aliases and symlinks are refused under the existing designated-registry policy. The following exact fields are required; no additional fields are accepted:

- `version`: `hold10_scheduler_bridge_v1`.
- `workspace`: existing retained workspace containing the designation/registry.
- `designation_sha256`: externally retained immutable designation pin.
- `audit_path`, `audit_identity_sha256`: existing audit journal and identity pin.
- `manifest_receipt_sha256`: existing frozen expected-runtime manifest receipt.
- `source_database`, `source_device`, `source_inode`: exact producer ledger identity, verified against the Orchestrator's open main database.
- `snapshot_byte_budget`: explicitly reviewed total retained snapshot/request byte budget.
- `snapshot_file_byte_limit`: explicitly reviewed maximum individual backup size (at most the inherited 2 GiB source limit).
- `closure_not_before_utc`: reviewed aware fixed closure boundary, at or after enrollment end and strictly before the existing close deadline.

The existing `hold10-source-snapshots-v1` directory must contain an existing empty regular `.allocation.lock`. The bridge does not create any authority, lock, marker, registry or directory. Package, manifest and journal validation occurs before installing the sink. Configuration bytes and source/directory/lock identities are rechecked during backup and at delegated audit/registry commits. Guard composition is per instance; inherited read calls still open the registry read-only. Changed authority/source/configuration refuses; no expected manifest is rebuilt.

## Lifecycle, retention and recovery

Automatic scan collection occurs only while the inherited registry is collecting and actual time is strictly before enrollment end. After end, ordinary scans continue without hooks or backups from this bridge; the research disposition names the required closure checkpoint. Follow-up scans are not silently counted as extra scheduled captures.

`closure_checkpoint(config_path, expected_config_sha256)` is an explicit operator API. It performs no scan and synthesizes no runtime identity. Before the pinned `closure_not_before_utc` it refuses without consuming an attempt. Scientific review must establish that this boundary covers the last enrolled ten-session horizon; the code's end/close ordering check does not establish that fact. At/after the boundary and before the inherited deadline, it creates one fixed-identity full-source checkpoint. Later calls reuse that exact saved request/source; they never take another look. Closure is not automatically scheduled by this patch. It is the means to retain terminal final-slot metadata and later source availability; it does not relax missing runtime or membership requirements.

`resume(config_path, expected_config_sha256, request_path)` retries only a retained exact attempt. Each request binds source bytes, designation, expected manifest, config and previous capture. No provider, scan, re-enrollment or new snapshot is involved. Existing collector PREPARED/capture/completion recovery and successor checks remain authoritative. An interrupted backup without a published request is left visibly incomplete and requires review; it is not overwritten, deleted or treated as a successful capture.

Snapshot allocation is serialized by the existing directory lock across inventory, backup and exclusive request publication. The lock is not held during the trading scan. Existing artifacts count toward the retained byte budget; no deletion/replacement occurs. There are at most 500 snapshots total, with normal allocation stopping at 499 and reserving one configured maximum-size snapshot plus a bounded request for closure. The separate inherited compact/audit byte/event limits are unchanged; a reserved snapshot does not guarantee remaining registry/audit capacity. Configuration requires enough budget for a normal snapshot and the closure reserve. Unknown files or malformed retained inventory refuse new allocation.

Backup checks are cooperative with a 30-second backup-progress limit; full collector processing is not a hard process deadline. Input/source/count limits remain bounded, but repeated full-history validation has CPU and latency cost. The current 375,234,560-byte copied production ledger implies 22,889,308,160 bytes for 61 same-size full snapshots before growth or overhead. The prior 25-capture/500-decision synthetic compact test does not qualify that retention or a whole 28-day study. A single copied-ledger benchmark is only a backup observation.

## Evidence and limits

With a valid guarded audit journal, runtime events and the existing full-census collection PREPARED/result records are durable. A bridge failure before completed collection may append an explicit `collection_error` stating that a full census was not retained; it never fabricates PREPARED evidence. If configuration/authority or the journal itself is unavailable, only the ordinary job/system-event disposition can be recorded best-effort. Such a failure is not a complete bad-observation history. A failed scan has a failed job even if research work was attempted in its finally path.

Focused offline tests use actual job/orchestrator hooks with fake model/data transports and denied sockets. They cover default-off and kill zero-touch, populated capture/reopen/retry, missing and wrong pins, degraded and failed scans, configuration mutation before capture and at commit, enrollment-end behavior, exact closure/retry, and future-only current-job linkage. Reviewers separately exercise process death, source/lock changes, concurrent allocation and full-population semantics. Synthetic clock changes are confined to test fixtures. No natural study registration or collection is claimed.

Remaining activation work includes a separately reviewed future scientific contract, explicitly approved local authority/configuration/manifest, full scheduled-population and follow-up storage/latency qualification, closure scheduling responsibility, expected-runtime trust/dependency limitations, sustained natural evidence, missingness handling and unresolved inference calibration. The original final-proposed family, +0.05R target, component floors, inactive/formal flags and fixed stopping rules remain unchanged. A collector route or load test does not satisfy those obligations.
