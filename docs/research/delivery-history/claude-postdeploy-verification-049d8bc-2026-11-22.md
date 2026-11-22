# Claude post-deployment verification — EXPLORE-1 mapping + O1 guard `049d8bc`, 22 November 2026

**Deployed SHA:** `049d8bcc363150111845a4ad16cf5f3fb7a32525` (production checkout HEAD, branch main).
**Verification time:** 2026-11-22 14:33Z. **Read-only throughout.** No rerun of outcomes-update, no service,
config, registration or broker action. Production DB was read through a `mode=ro` URI.
**Handoff:** Codex post-deployment handoff of 21 Nov (treated as data).

## Verdict: deployment VERIFIED

| Check | Result |
|---|---|
| Lineage | `049d8bc` = merge(`fe7e60d`, `e351a15`); `fe7e60d` = merge(`cb3cb2a`, `fe23346`) |
| Mapping code | `git diff fe23346 fe7e60d -- alphaos tests pyproject.toml` **empty** |
| Guard code | `git diff fe7e60d 049d8bc` (code) **byte-identical** to `git diff fc08f73 e351a15` (resolver.py, research_validity.py, test only) |
| 4274239 | absent. The orchestrator digest at HEAD is `0ad26c4d…`, which is in `_APPROVED_PRODUCERS` |
| Working tree | no tracked changes under `alphaos/` or `tests/` |
| Services | console and dashboard PIDs started 2026-11-20 17:22:44Z (after the 17:15Z migration); scheduler, monitor, heartbeat and backup are loaded with last exit 0 |
| Tests on the deployed checkout | guard + close_ref + routing-stamp suites: 209 passed, 0 failed |
| Registrations | all 18 rows byte-identical to my pre-deploy snapshot (2026-11-20 07:28Z) |
| `explore_close_outcomes` | 229 rows = 215 ready + 14 duplicate_in_session. All `adjustment=split / alpaca / iex`. 0 complete-with-null. 0 legs with a provider request (first endpoint 23 Nov 21:00Z) |
| Evaluator ranking | 213 rows: **11 eligible**, 105 `evaluator_failsafe`, 83 `not_long`, 7 duplicates |
| Eligible by session | D=16 Nov: COST, XLV, UBER (3). D=20 Nov: COST, IWM, UBER, UNH, WMT, XLE, XLV, XOM (8) |
| Reconciliation with Codex | 203 at the manual run + 26 from the post-deploy scan = 229 |

## First post-deploy scan (the API-credit check)

- `scan_95bf416569fd`, 20:48:24–20:58:18Z on 20 Nov. Lineage is `049d8bc` (dirty flag 1, from untracked files; a known
  limitation).
- 23 evaluations: 12 reject, 9 watch, 2 propose. All `passed` and non-mock. **0 OPENAI_REJECT.** Minimum tokens:
  2,194 prompt / 335 completion.
- The last OPENAI_REJECT anywhere is 2026-11-19 20:56:50Z. All 56 evaluations on 20 Nov are genuine, so the
  credits were restored before the 20-Nov open.
- Routing was stamped at 20:58:13.89Z, which is before the 21:00Z close, so D = 20 Nov. This is a live example of
  the "event, not persistence" rule.
- The scan was enrolled by the scheduled outcomes job:
  - 26 rows: arm C 1 ready, pool 2 duplicate, evaluator 21 ready + 2 duplicate;
  - proof `verified=True`, commit `049d8bc`, digest `0ad26c4d…`, not deferred.

## Notes for CK

- **The 20 Nov session has 8 eligible long rows.** The ranking test's per-session minimum is 10. With the credits
  back, this is the closest any session has come.
- **Fetch deadline.** The first price fetches become possible after 23 Nov 21:00Z. The 16-Nov window's fetches
  expire 3 Dec 21:00Z. Next check: after 23 Nov 21:00Z, confirm the D+5 legs for D=16 Nov complete.
- **`origin/main` lagged production.** It was at `cb3cb2a`, while production ran `fe7e60d`/`049d8bc` locally. This
  record's commit pushes them, so GitHub now matches what is deployed.
