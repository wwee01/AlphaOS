# P0.3 / P0.4 entry safety handoff

Base: `03cea70d549812b6b21fbac239f7974fb3e082e8`. Isolated checkout:
`alphaos-edge-p0-risk-20261114`. This change is pending independent review and
Claude audit before CK merge. It has not been deployed.

Fresh paper exposure now reports occupied symbols from broker positions,
pending entry orders, unresolved local positions, and locally working bracket
entries. A proposal for an occupied symbol fails the fresh risk gate, including
the final account check under entry ownership. Other symbols remain eligible
under the existing account limits. No numeric cap changes.

A changing broker snapshot or a positively verified protected fill awaiting local
reconciliation keeps proposals pending with their original expiry. Uncertain entry
intent phases and unexplained external positions remain hard refusals.
An initially unsized proposal carries an explicit audit marker and zero quantity;
only a later successful fresh risk check may size that marked proposal. Monitor
missing/stale/failed/inflight readiness also defers entry. Protection/execution
incidents, ambiguous ownership, fused/stalled monitoring, invalid health, unknown
exposure, invalid stops and other hard risk refusals remain closed.

Only a previously deferred automatic proposal is marked for next-scan retry.
Each retry rechecks current auto/unattended eligibility, original TTL, status,
existing entry, kill/protection, margin, fresh quote/drift, fresh exposure and
final monitor readiness. Manual deferral grants no standing automatic approval.
No approval receipt or daily allowance is consumed until the final submission
gate; uncertain or actual submissions retain existing idempotency. Capture-only
degraded scans do not retry entries. The queue is bounded to 200 pending proposals
per scan, ordered by creation; no retry extends expiry or replaces history.

Final targeted exposure/retry controls: 120 passed in 6.30s. The earlier directly
affected suite passed 181 in 54.38s before the stricter ambiguity correction;
its unchanged orchestration/TTL checks were retained. Validation uses fake broker inputs and the existing internal simulator, temporary
ledgers and clocks only. The before log reproduces duplicate-symbol acceptance,
initial/final transient rejection and absent next-scan retry. Focused cases also
cover expiry, authority withdrawal, hard faults, final-gate races and local
working-entry reservation. Ruff and diff checks pass. Targeted mypy has only the
unchanged dependency error in `alphaos/data/providers/alpaca_data.py:238`;
no unrelated cleanup is included. Test logs are retained under the repair
workspace's `evidence/edge-p0-risk-*-20261114.log`.

E2 integration touches the same orchestrator: retain the separate next-scan retry
hook, both proposal-creation branches' arm tags, and the shared final execution
guard. E2 owns campaign staging/fairness. This P0 patch does not qualify campaign
routing, restart behavior against production, broker execution or service health.
No production files, services, configuration, databases or broker APIs were used.
