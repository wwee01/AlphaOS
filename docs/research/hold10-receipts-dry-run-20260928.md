# HOLD10 receipt persistence: disabled dry-run layer

This addition implements durable protocol, cohort, paired member and measurement
receipts for a HOLD10 successor draft. It does not activate a registration,
collect live inputs, place an order, calculate a formal p/q value, or approve a
strategy. The original `prereg_35bbce762e7f` and all existing tables are untouched.

## Fixed analytical meaning

The supported draft retains the original greater-than **0.05 R** target and
uses a paired mean of candidate R differences over **10 completed sessions**.
The caller must explicitly choose `primary_proposed` or `final_proposed` in the
pinned draft. That choice remains unreviewed and inactive.

Both arms bind the same saved opportunity input, decision anchor, horizon and
common risk policy/budget identity. Each proposed arm also preserves its **own
original entry, stop, target and stop-distance denominator**. A 20-dollar profit
against a 10-dollar stop distance is 2 R; the same profit against a 20-dollar
stop distance is 1 R. This is a candidate-level contrast, not a capital-constrained
portfolio return, net profit, equal dollar exposure, or evidence that either
saved policy actually observed precisely these inputs in historical operation.

The measurement adapter is the previously reviewed
`successor_measurement.reconstruct_bracket`. The day after the ET decision date
is session one. Every scheduled bar is required even after an early barrier.
Ambiguous bars, missing sessions, unknown actions and malformed prices remain
blocked; cash requires an explicit `no_action` policy decision. Saved daily
bars must carry a completed-session observation time. The resulting gross,
idealized R excludes trading costs and is not a real fill. Holding dependence is
bounded by the saved decision and daily exit bound, not `replay_bars_used`.

## Explicit API and additive schema proposal

Only the new `alphaos/research/hold10_receipts.py` module and its tests/docs are
added. `journal/schema.py` and runtime callers are unchanged. `DDL` contains four
tables, uniqueness constraints and immutable update/delete triggers:

| Table | Identity and purpose |
| --- | --- |
| `research_hold10_protocols` | New `hold10-draft:` identifier; complete pinned policy/risk/method preimages |
| `research_hold10_cohorts` | Immutable full input-defined opportunity manifest and source identity |
| `research_hold10_members` | Exactly two arms for every declared opportunity, including missing plans and ineligible inputs |
| `research_hold10_measurements` | Append-only revisions; previous-receipt hash; completed measurements cannot be revised |

Explicit scratch/manual initialization is required:
`initialize_dry_run_receipts(connection, purpose="scratch_or_manual_dry_run")`.
Importing or constructing `Hold10Receipts(connection)` does not initialize any
table. Mutating methods return `status=disabled` unless the caller explicitly
uses `dry_run=True`. This is a persistence dry run: it **does write the new
tables in the supplied scratch/manual connection**. There is no live collector
or command that invokes it.

`register_protocol` requires an externally pinned SHA256 of the full draft.
`enroll_cohort` requires protocol and manifest pins and an explicit source
classification, SHA256 and selection scope. The declared opportunity IDs must
exactly equal the submitted opportunity objects. Both arms share their input
pin and common risk pin. Missing arms create immutable placeholder members;
ineligible opportunities are retained. The API verifies completeness relative
to this declared manifest. A future input collector must independently prove
that the declared opportunity scope includes every eligible input; a caller
cannot establish that fact merely by supplying a hash or a prose scope.

`record_measurement` accepts only caller-supplied saved bars and external pins
for the member receipt and bars. It computes the result before acquiring the
short write transaction, rechecks member identity, then uses the expected
previous measurement receipt as a compare-and-set token. Identical retries are
idempotent, even with an old token. Changed evidence after a completed result
requires a separate correction protocol; old observations are never rewritten.

`cohort_view` reads one consistent SQLite snapshot, verifies the frozen
manifest against all expected members, verifies every measurement revision and
hash link, and returns missing measurements explicitly. It calculates no
success, p value, q value or promotion verdict.

## Chronology, integrity and concurrency

Actual protocol, cohort and measurement recording timestamps are assigned by
the process clock inside the write transaction. Callers cannot supply an old
receipt timestamp. Protocol receipt time must precede the decision, and cohort
recording must precede the first measurement session, even to receive the
informational `timing_eligible_dry_run_only` label. A wait for the writer lock
cannot preserve a stale pre-session timestamp. Archived copies, synthetic
inputs and late protocols/enrollment are explicitly retrospective.

**Every receipt, including a timing-eligible one, remains `mode=dry_run`,
`prospective_enrollment=False` and `formal_allowed=False`.** Nothing created
from a copied legacy ledger becomes a prospective research sample.

Protocol/cohort/member insertion uses short `BEGIN IMMEDIATE` transactions;
there are no provider or broker calls. A crash after one member insertion rolls
back the entire cohort. Unique keys make concurrent identical enrollment
idempotent; competing different measurement revisions require a refreshed
compare-and-set token. Read views cannot combine measurements from different
SQLite snapshots. UPDATE/DELETE triggers prevent ordinary edits. Receipt hashes
detect changes against supplied external pins and hash chains; these are not
digital signatures or protection against an actor who can replace both a
database and all externally trusted pins.

## Validation and remaining engineering

Author tests cover disabled operation, empty manifests, missing members,
unchanged existing rows, same-input/risk/horizon checks, original separate risk
denominators, source/clock/identity tampering, late legacy enrollment, atomic
crash rollback, concurrent enrollment and measurement CAS, restart/idempotence,
whole-snapshot reads, day-six barriers, incomplete horizons, and broken revision
history. The copied-ledger proof lives outside the application under repair
evidence, with exact source and database hashes and all-original-table digests.

This completes a disabled durable receipt API, not end-to-end R1 acceptance.
Codex still owns schema/startup integration after review, an input-first
collector and independently verifiable opportunity enumeration, saved policy
execution receipts for both arms, prospective source/clock provenance, a
reviewed successor population decision, and finalization/multiplicity wiring.
Those need separate bounded implementation and review. Actual prospective
data, completed horizons, enough independent holding intervals, and profitability
remain empirical requirements; they cannot be manufactured by this layer.
