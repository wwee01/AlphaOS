# Preview-first shadow suspension recovery

This adds an explicit manual recovery command for shadow labelling only. It does not activate a strategy, change the trading kill switch, alter suspension triggers, lower coverage/canary thresholds, raise API caps, change the universe/feed, or request provider data. Existing historical rows and suspension reasons are not rewritten by preview. A currently failing coverage gate still prevents release.

From the normal application working directory and configured environment:

```sh
python -m alphaos shadow_recovery > /tmp/shadow-recovery-preview.json
```

Review the JSON, especially `eligible`, the canary/coverage/auto-suspend/budget checks, exact ledger and marker identities, and `preview_sha256`. An ineligible preview exits with status 1 and remains useful diagnostic output. To apply an eligible preview within five minutes, supply its exact hash and an operator note:

```sh
python -m alphaos shadow_recovery --apply \
  --preview-file /tmp/shadow-recovery-preview.json \
  --expected-preview-sha256 '<exact preview_sha256>' \
  --note 'Reason for this reviewed recovery'
```

The command always validates the current default canary corpus. It has no corpus-override or force-clear option. It opens an existing database without constructing the Orchestrator, migrating schema, recording startup/configuration rows, or creating provider clients. Preview opens SQLite read-only and creates no application lock or marker file; normal SQLite read coordination can update existing shared-memory read marks. The preview is bounded to the marker reader's 16 KiB input limit on apply. Do not include secrets in the operator note, which is saved in the audit.

## Proof and gates

The shared `canary_recovery_evidence_v1` predicate checks the latest original real run, complete frozen packet membership, current corpus/model/prompt identity, matching recorded per-packet response identity, a valid preceding baseline, successful result validation and freshly recomputed drift/freshness. Optional missing system fingerprints remain an explicit limitation when absent on both sides. It does not authenticate an external model call. Existing canary runtime suspension semantics stay unchanged.

Recovery also checks the existing trailing coverage floor, existing auto-suspend conditions, enabled shadow configuration, full worst-case next-window budget against all existing sub-caps/shared caps, and the independent trading kill switch. The proof hashes relevant ledger rows/counts, the in-memory settings snapshot without exposing credentials, and selected source modules used by recovery/gates. Selected hashes are not a whole-application attestation; deployment records separately bind the reviewed source revision. Preview and every mutation phase revalidate the inode/path captured when the existing ledger connection was opened. The Python API requires that identity-bound `existing_journal` context, rather than inferring the identity of an arbitrary previously opened connection.

## Durable transition

Apply refuses nested/live research ownership and takes the existing research worker lock plus a stable shared shadow-marker lock. All new suspension writers use that same marker lock. A concurrent writer waits and retains its new trigger after recovery releases the lock; a same-thread nested writer poisons recovery so catching its exception cannot authorize release. Recovery never waits for a SQLite writer while holding the marker lock: it temporarily uses zero SQLite busy timeout and restores the caller's setting afterwards.

1. Recheck preview hash, age, exact marker/configuration/ledger/evidence and all gates under a write transaction. Save and commit a `Shadow recovery prepared.` system event.
2. Exclusively create and fsync a `SHADOW_LABEL_SUSPENDED.recovery-pending` sidecar. This file alone means suspended, including across process death.
3. Under another write transaction, freshly recheck proof/gates and exact pending/original marker identities. Remove only the exact original marker, then save and commit a `Shadow recovery completed.` event. This event means gates were verified and the original was removed; the pending barrier may still exist.
4. Begin a new write transaction after that commit, recheck all gates, selected source/config/ledger identity, ownership, and absence of a new original marker. Remove only the exact pending barrier. This closes the gap in which a new canary result or consumed budget could otherwise arrive after completion was committed. It does not claim atomicity with future filesystem kill changes; shadow runtime checks remain active.

A failed final directory fsync attempts to restore the same pending bytes exclusively and re-raises. It never replaces a new file. Persistent filesystem failure cannot guarantee durable writes; it remains an explicit operational failure, not successful recovery. Symlinks, nonregular/hardlinked/oversized files, replaced locks, malformed pending receipts, missing audit proof, naive/future timestamps, changed evidence and audit/SQLite failures refuse release. Legacy plaintext suspension markers are valid inputs and remain unchanged until an authorized apply removes the exact file.

## Restart and conflict boundaries

After an interruption, obtain a fresh preview. An intact pending sidecar binds its exact prepared event/hash, operation, ledger, marker path and configuration. A new apply can resume that operation after fresh gates pass. Repeated completed audit events can describe interrupted retries; an event alone is never proof that the pending barrier was removed. File state and the command's observed result remain distinct.

A new/replacement suspension while pending refuses both files. This command intentionally does not resolve every conflicting pending state; such a conflict requires separately reviewed supersession handling. There is no broad force/reset option. The old raw `ShadowLabelSuspendSwitch.release()` now refuses, so application callers cannot bypass the durable protocol. Trading `KillSwitch.release()` is unchanged.

## Validation scope

Author tests exercise scratch ledgers, real budget writes, unavailable gates, preview tampering/expiry, explicit aware timestamps, persisted restart, immutable schema/old rows, CLI no-startup roundtrip, and failed audit/directory-sync behavior. Independent review separately exercises real process death, other-process/thread suspension, same-thread poison, file and ledger replacement, SQLite write contention and post-completion races. Real canary producer-to-predicate-to-command tests use an offline classifier, never paid requests. Copied production evidence is diagnostic only: no command in this work authorizes clearing an actually failing production latch.
