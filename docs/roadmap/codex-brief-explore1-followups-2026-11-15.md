# Codex brief — EXPLORE-1 follow-ups, 15 November 2026 SGT

**Drafted by Claude for CK to hand to Codex.** P0 + EXPLORE-1 are deployed and active. Items 1 and 2
and the control-pool change in item 3 are CK's rulings of 15 November. The other small items in 3 are
Claude's recommendations, low priority. Same ceremony as before: commit, hand the exact SHA to Claude, no deploy until CK's merge
instruction.

## Deployed state — rebase onto this before any new work

- `main` = merge `cc117ee` (your `319003d`; application byte-identical to `1c3d3ea`) plus `96dc7a1`
  (test-only fix, see below). Records: `docs/research/delivery-history/claude-audit-of-codex-p0-explore1-2026-11-15.md`
  and `claude-deployment-explore1-2026-11-15.md`.
- The ten approved settings are applied; `EXPLORE_1_ENABLED=true`. First live session: 16 November.
- The 25 overlay files are now tracked; the production tree has no uncommitted code.
- Your branch broke `tests/test_swing_audit.py::test_existing_research_hook_never_runs_for_monitor`
  from `bbbe8cf` (fixture lacked `monitor_deadman_enabled`). Fixed in `96dc7a1`. Run the full suite,
  not a focused subset, before the next handoff and state the totals.
- `data/swing-audit-activation.json` was renamed (CK stopped the swing audit). Do not restore it.
- Unchanged and still yours to respect: shadow latch (fresh all-gates preview required), external
  heartbeat disabled, research quarantine, paper-only boundary.

## 1. EXPLORE-1S — paper shorts in arm A (CK ruling, 15 Nov)

**Why.** Arm A is 0.94 long proposals a day, too few to judge alone by 19 December. The evaluator
also made 42 short proposals since 1 October (about 1.3 a day) that the margin rule blocks.

**Scope.** Arm A only: the evaluator says propose with direction short. Arms B and C stay long-only.
A short is never tagged `A+B`. Paper only.

**This system has never placed a short order** (0 short orders, 0 short positions in the ledger).
Treat every short path as untested, whatever the code appears to support.

Changes needed, as read at `cc117ee`:

| Where | Today | Needed |
|---|---|---|
| `explore_policy.route` | returns None for a short candidate or `ev.direction != "long"`; level check `stop < entry < target`; long-only config fallback | accept a short evaluator proposal for arm A; mirrored check `target < entry < stop` with the same reward-to-risk floor; mirrored fallback; record the level source as now |
| `explore_policy.entry_refusal` | refuses any `direction != "long"` | accept a tagged arm-A short when the new flag is on |
| same, fairness queries | count `o.side='buy'` orders and `direction='long'` pending proposals | count short entries and short pending proposals too |
| Margin gate: `approval.py` (auto path), `risk/risk_engine.py`, `execution/order_manager.py` | block `requires_margin and not margin_approved`; only `approve_proposal(approve_margin=True)` sets it | keep all three gates. Grant `margin_approved` at tagging time only for a paper, `explore_1`, arm-A short when the flag is on, and record that the authority was CK's 15 November ruling. Untagged, legacy, manual and demo shorts stay blocked |
| Broker eligibility | no `shortable` / `easy_to_borrow` / account `shorting_enabled` check anywhere | refuse before submission, as a terminal block with its own reason, when the asset or account cannot short; whole shares only |

New setting `EXPLORE_1_ALLOW_SHORTS`, default false, refused at startup unless paper mode and
`EXPLORE_1_ENABLED`. Merge it off; CK turns it on separately.

Prove, offline, with the real exposure read and a fake broker that fills after accept:

- full short lifecycle three times — stop exit, target exit, time exit — each ending in an outcome
  with the correct R sign, arm `A`, `policy_version explore_1`, and a short `candidate_outcomes` seed;
- bracket shape at submission: sell-short limit parent, buy stop above, buy limit below, GTC;
- protection watchdog, position attribution, entry recovery and rejection paths accept the short
  shape (they are direction-aware on reading; none has run on a short);
- drift, staleness and entry-TTL checks are direction-correct for a short;
- the per-symbol cap blocks a short on a symbol held or pending long, and the reverse;
- heat, notional and the 3 % daily-loss limit count short exposure;
- refusals: short routed to B or C; flag off; not paper; untagged short; non-shortable asset.

The weekly scorecard must report arm A long and arm A short separately.

**Budget.** About 250 changed application lines. If it needs more, or any safety control has to be
weakened to make shorts work, stop and report.

## 2. F1 — a deferred entry is never retried (audit finding, medium, fails safe) — CK: build now

Build this first and hand it over on its own SHA; do not hold it for the shorts work.

Only a scan calls `_retry_deferred_entries`; proposals expire after 1,800 s and scans are at least
105 minutes apart. When the first staged entry of a pass fills before the next one's exposure read,
the rest are deferred and then expire. Estimated loss 5–10 % of proposals.
**Requirement:** a second proposal in the same pass must not be lost because the first one filled.
Booking the pass's own verified, protected fill before the next read is the direct fix; a retry from
the 5-minute tick alone is weak because of the 50 bps drift limit. Reproduction:
`docs/research/delivery-history/claude-audit-1c3d3ea-probes-2026-11-15.py`, the three `test_f1_*` cases.

## 3. Smaller items (one commit is fine)

- F3 (**CK ruled: yes**): exclude symbols already held or with a pending entry from the control pool
  before the draw. Persist the filtered pool as the eligible set and record which symbols were
  excluded and why, so the draw stays reproducible. Still one draw per window; no redraw after a
  blocked draw.
- L1: give non-routed campaign candidates a specific rejection reason.
- L3: add the new keys to `.env.example`.
- Migration race: on 15 November the first scheduler tick after reload failed once, most likely
  because five processes ran the additive `ALTER TABLE` migration in the same second. Make the
  migration safe under concurrent openers (take the write lock first, re-read columns inside it).

## Still due from you

The 28 November scorecard (not built). Shadow latch: fresh preview only.

## Leave alone

- Paper observer: CK keeps it running as a read-only record. It stays frozen — no repairs, no
  alert changes, no effect on the heartbeat exit code.
- External heartbeat: stays disabled (`MONITOR_DEADMAN_ENABLED` false, no URL) until CK supplies an
  endpoint. Do not enable it or add a URL.

## Not yours

Historical-bar study and the primary-question registration (Claude). Deployment and settings changes
happen on CK's instruction.
