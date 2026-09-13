# Paper entry rejection and held-position recovery

Round four addresses Claude's N3/N4 findings against `8c23a53`, recorded in `4ffc860`. It changes recovery of failed attempts; it does not change entry selection, risk limits, approval caps or research activation.

A synchronous rejection can carry a durable proof only when the installed SDK error is bound to the exact first standard-transport paper POST and its response. Reviewed 400/403/422 rejection reasons are supported; duplicate client IDs, unknown reasons, retry histories, changed requests and ambiguous failures remain gated. The proof preserves sanitized request terms, classification and response digest. Older error messages are not backfilled into trustworthy evidence.

`entry_intent_resolve` previews by default. Applying with a nonempty audit note repeats two fresh client-ID absence reads and checks exposure attribution while owning entry and monitor execution. It stores the operator/proof atomically, marks a confirmed rejected attempt `abandoned_rejected`, and clears only that attempt's incident. Existing unsent recovery remains `abandoned_unsent`. Neither path resubmits the proposal, refunds approvals, mutates the broker, erases history or overrides another incident.

A separate holding in the same symbol is allowed only when its distinct original order/fills, residual quantity, broker positions and current protection are fully attributable and agree. Current bracket or replacement OCO protection must have known lineage and matching terms. Target-related fills/positions, missing origin history, duplicate broker attribution, unknown open orders, changed snapshots and unproven residuals refuse recovery. Positive attribution does not substitute for proof that the failed entry was unsent, rejected or terminal with zero fills.

Terminal-zero reconciliation re-reads the exact failed parent after attribution, verifies all intended terms and confirms zero parent/child fills. Both caller paths recheck the local evidence fingerprint atomically before finalization. Broker requests never run within that transaction. Existing positions and their protection remain intact.

The archived N3 probe constructs an APIError without HTTP provenance; the archived N4 fixture omits the holding's originating local order/fills. They remain useful negative controls. New regressions cover actual installed-SDK HTTP responses, ordinary submitted/filled holdings, restarts, residual/replacement protection, duplicate identities, concurrent changes and audit rollback. Production verification records and operator instructions are published separately in the delivery history and research inbox.

Known bounds: future unreviewed broker message variants and legacy rows without adequate provenance remain unresolved until independently verified. The separate unhealthy-monitor terminal-block observation and D5 sizing rejection are unchanged.
