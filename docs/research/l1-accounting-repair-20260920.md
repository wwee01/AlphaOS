# L1 accounting and exit provenance repair — September 20

This repair makes gross/net labels truthful without changing the stored `trade_outcomes.realized_r` definition, trade decisions, cost settings or historical outcomes. It has no migration and no broker calls. It does not manufacture lessons or convert the failed September 18 acceptance into a pass.

## Read-time accounting

`trade_accounting_v1` is attached to the existing Trade Packet and attribution report. It retains stored gross R, computes an explicit unrounded gross R and net R, and includes a SHA-256 receipt over the exact original outcome, position, orders and fills. The denominator is the absolute actual-entry-to-original-order-stop distance times the full quantity. The position stop must match the original entry order stop; otherwise net R is withheld rather than assigning an unsupported historical risk denominator.

The view requires complete positive cumulative entry and exit fills, matching position/order identities and fill sides, quantity-weighted prices agreeing with the outcome, consistent gross/cost/net dollar accounting, and the expected rounded stored gross R. Missing costs, missing fills, zero risk, nonfinite numbers or contradictory evidence produce an explicit unavailable/invalid result. It does not infer a denominator by dividing through rounded historical R. For the audited AVGO and META outcomes the net values are respectively −1.077126950829296 and +1.455986457371499 R; the original −1.074 and +1.460 stored values remain gross.

The cost basis is explicitly **ledger-modeled trading costs**. Recorded fill commissions are shown separately and are not claimed to be verified broker fees. No subscription, infrastructure costs, cashflows or full-account return is introduced by this trade-level view.

Attribution read views retain both `stored_basis` and `corrected_basis`, as well as the stored attribution version and numeric value. Historical `realized_net` and `net_vs_gross` claims are shown as corrections to `realized_gross` and `gross_vs_gross`, respectively. They are not overwritten. The aggregate execution gap is explicitly labeled gross-versus-gross.

Future attribution discoveries use version `2.1.0` and truthful gross labels. Discovery treats an already-attributed source under any version as enrolled, preventing a version bump from silently duplicating every historical event. Existing pending/partial sources continue their ordinary resolution; an existing non-null 2.0.0 basis and its version are preserved. Already resolved sources remain untouched. The R formulas and numeric aggregation behavior are unchanged.

The Trade Packet now includes all position-linked fills and their orders, including earlier partial-exit orders, rather than returning only entry fills. The APIs and existing packet viewer receive this read-time addition through their existing assembler. Reports never write sidecars to the ledger.

## Future execution receipts

New broker cumulative-fill rows retain a versioned receipt in the existing `raw_broker_fill_reference` field. It records the broker order ID, cumulative quantity/average price, previously recorded quantity, new quantity/price, broker-reported cumulative fill timestamp (when present), and local observation time. This is **not** a claim to know each incremental execution's exact time: a cumulative broker sample can combine several fills. A missing broker time remains an observation fallback. Read-time timestamp evidence checks the receipt against the fill's ID, quantity, price and both timestamp fields before displaying the broker-reported time. Historical receipts are not invented.

New synthesized broker exit orders retain actual order type/class, client ID, TIF and protective prices from the normalized broker record, with entry-linked data-provider/feed provenance and explicit paper execution/fill basis. Unknown broker attributes stay unknown; a protective stop is no longer falsely called a simple market order. The normalizer preserves those broker fields without changing order behavior.

At a new close, the existing exit order's JSON receives a `trade_close_accounting_v1` receipt within the same final transaction. It separates effective close time, broker execution time when supported, and local accounting time, and retains the single computed cost-model breakdown and stop at close. Malformed, null or non-object legacy response JSON is preserved under a wrapper; it cannot prevent an otherwise valid close. Receipt generation does not recompute costs a second time. Repeated reconciliation after closure remains a no-op.

Legacy entry execution times that were only recorded as observation times remain visible as unverified ledger timestamps. The independent audit's separate saved broker capture continues to corroborate those legacy times; this patch neither imports that capture into production nor overwrites historical timestamps. No historical row becomes retrospectively prospective or fully conformant merely because a new read view can explain it.

## Verification scope

Adverse tests cover the audited AVGO/META arithmetic, preserved input dictionaries, missing/nonfinite costs and fills, mismatched risk stops, short positions, mismatched receipt fields, malformed old JSON, a single cost-model call, mixed legacy/new attribution states, partial 4+6 entry and 6+4 exit fills, complete packet/report linkage and repeated reconciliation without duplicate records. Broader affected attribution, packet, entry and exit lifecycle suites are run separately; this document makes no full-suite or deployment claim.

The external read-only acceptance probe `evidence/l1-readview-20260920.py` uses the frozen September 20 ledger copy. It recomputes both audited trades and all closed-trade views, retains original and corrected basis labels, and proves all columns across orders, fills, positions, exits, outcomes and attribution records remain byte-for-byte equal under canonical row hashes. No production write, broker call or historical sidecar insert occurs.
