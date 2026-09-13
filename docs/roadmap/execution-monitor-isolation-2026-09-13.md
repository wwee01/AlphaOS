# Execution monitor isolation — deployment contract

The scheduler previously ran scan, monitor and research jobs sequentially. A slow scan could delay protection monitoring, and a successful research job could make the general heartbeat healthy while the monitor was fused. This repair assigns monitoring a separate launchd process and reports its own readiness.

## Roles and behavior

- `python -m alphaos.scheduler.worker --role monitor`: only the existing monitor job, with its existing fifteen-minute cadence. launchd invokes it every five minutes. No new AI calls or faster broker polling cadence.
- `--role research`: the existing non-monitor jobs. A scan is blocked unless monitor readiness is established. The research scan context sets `RESEARCH_ENTRY_GUARD` for the final submission check integrated at `_execute_checked`; the integrating change must re-read readiness there to catch a monitor failure during a slow scan. Outcome collection and other research jobs remain available. Existing scan windows and source/call caps are unchanged.
- `--role health`: a separate five-minute monitor heartbeat. Opens SQLite read-only, constructs no broker, and alerts on unhealthy monitoring. A recent research completion cannot substitute for a monitor completion. Monitoring is expected around the clock under the existing cadence, including weekends.
- `--role readiness`: the same read-only check without alert delivery; additionally requires a completed dedicated-monitor worker pass and no in-flight monitor. This is the migration's startup gate.
- `--role preflight`: read-only migration check. Refuses any unfinished scheduler job or unfinished manual monitor pass. It never reaps or modifies those rows.

Normal reconciliation that finds an unresolved cancellation, working close, broker-read uncertainty, or protection incident remains a **completed pass with incidents**. Readiness is false and new scheduled scans are blocked, while monitoring continues. Raised worker failures still create failed job rows and eventually trip the existing fuse. An explicit process failure is not recast as a healthy domain incident.

The heartbeat rejects recent failures/fuses, missing or invalid/future completion times, stale in-flight rows, multiple in-flight rows, recorded execution errors and unresolved protection incidents. Its freshness allowance is the smaller of the existing general heartbeat threshold and the larger of two monitor intervals or one interval plus two launch ticks (30 minutes with present defaults). A fresh in-flight pass can retain worker health, but entry readiness is false until it finishes.

## Ownership and recovery

The canonical ledger path determines a persistent `.monitor.lock` file. A nonblocking OS advisory lock is held throughout the monitor worker and across `JobRunner.run_job('monitor')` and `Orchestrator.run_monitor_once()`. It is reentrant only in the same owning thread. Different threads/processes cannot overlap, even across cadence interval keys. Never delete these files while workers may exist: replacing a lock inode can create two independent owners. The existing SQLite interval key remains the completed-run deduplication backstop.

Research has a separate process lock, so slow research cannot own the monitor's worker slot. Research reaping never touches monitor rows. Scheduled monitor runs leave interrupted `started` rows intact and report that review is required. An explicit manual `scheduler_run_job monitor` can recover a stale monitor row only while holding exclusive ownership, and only a successfully completed pass changes the existing failure streak. Neither deployment nor health clears a fuse, suspension marker, or protection incident.

This is process isolation, not independent infrastructure: both workers still share the Mac, broker access and SQLite. A long database write transaction, machine sleep, lost connectivity or broker outage can still impair both. Those failures must remain visible as unhealthy monitoring. Production broker behavior and launchd activation require the integrating operator's runtime verification.

## Reviewed migration procedure

No service is installed by importing this code or running tests.

1. Integrate the reviewed code and tests into the production checkout. Confirm the intended paper configuration and existing safeguards. The static plists name the production checkout/runtime; the installer refuses execution from another checkout.
2. Run `deploy/install_monitor_isolation.sh --stage`. This validates/copies three plists to a staging directory and changes no services. Review the scheduler=research, monitor=monitor and heartbeat=health role arguments.
3. After authorization, run `deploy/install_monitor_isolation.sh --activate`. It first checks for active workers and unfinished ledger work without changing services. It preserves timestamped original operator plists, SHA-256 hashes and enabled-state flags in a private backup directory. It then disables future scheduler/monitor launches and repeats the checks. A refusal before service replacement restores the previous enabled states; no active worker is killed. After replacement begins, a failure leaves research stopped and reports the monitor service state/recovery requirement; it never starts an old combined worker alongside the new monitor.
4. Once quiet and preflight-clean, the installer replaces the service definitions and starts the dedicated monitor first. It starts the health service and loads research only after dedicated readiness passes. No reset or automatic orphan recovery occurs.
5. With a recent legacy monitor completion, existing cadence may make the first dedicated invocation legitimately not due. The installer's bounded 30-second readiness wait then ends with monitor and heartbeat installed but research stopped. Allow the existing monitor cadence to produce a dedicated pass, inspect its result, and rerun activation when workers are idle. Do not force a new broker pass merely to satisfy the installer.
6. Verify one completed dedicated monitor row (`worker_role=monitor` in result metadata), normal subsequent cadence, no unresolved incidents, health exit status zero, and no monitor entries from the research worker. Observe a real research scan while monitoring remains independently scheduled.

`deploy/install_launchagent.sh scheduler` now uses this coordinated migration, and `all` additionally installs the existing backup job. `deploy/uninstall_launchagent.sh all` stops scheduler, monitor and heartbeat while retaining backup behavior. Stopping only `scheduler` leaves protection monitoring available.

Rollback must first quiesce both scheduler and monitor processes. Do not restore an old sequential scheduler while leaving a separate monitor active, because old already-loaded Python code cannot honor the new ownership contract. Preserve the ledger and protective broker orders; inspect any interrupted pass before returning to service.

## Validation and limits

Tests use temporary/in-memory ledgers, fake broker-free job bodies and real spawned Python processes. They cover a blocked research process while monitoring completes, competing monitor processes with different interval keys, thread ownership/reentrancy, historical fuse masking, weekend heartbeat expectations, four repeated domain incidents without disabling recovery, explicit-only orphan recovery, and read-only health/startup checks.

Source/call caps, trading risk limits, strategy selection, monitor interval and manual approval semantics are unchanged. This patch is not authorization to start services or clear existing incidents. Direct manual entry commands require their existing risk/approval checks; the scheduled monitor-readiness gate is not a new universal manual-entry policy.

Validation on this branch: 248 focused tests passed (51.01 seconds), including 20 new isolation/readiness tests. Ruff passed changed Python code/tests; mypy passed the five new/changed scheduler modules. Three service plists parsed successfully and installer/uninstaller scripts passed bash syntax checks. Launchd migration was reviewed but not executed; integrated final-entry gating and real service readiness remain rollout checks.
