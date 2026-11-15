# P0 — Claude audit handoff, 14 November 2026

**Review required; not deployed.** Base production revision: `de1bfea27e9068da3fc7cd6419580e8767cad7d9`.
The isolated checkout is `/Users/ck/.codex/.chatgpt-projects/g-p-6a43e7af14688191bc461adad2684fe8/alphaos-edge-campaign-2026-11-14`.
Review each commit below and the combined application tree at `9442ae00041a788dcbff085b454243246b829396` before CK's merge instruction.

| Exact commit | Change |
|---|---|
| `03cea70d549812b6b21fbac239f7974fb3e082e8` | Commit the exact existing production overlays; per-file disposition and hashes in `edge-campaign-p0-source-inventory-2026-11-14.md`. Preserve frozen source; no behavioral repair in this commit. |
| `e9471dd6cdd09cdf0b10b65c056636446e222e32` | Retain two frozen documentation dependencies required by those source tests. |
| `20e6d0f55d4c0aebfa47281d913fd64c568187ea` | Observer findings remain visible without failing heartbeat; ordinary healthy scheduler deferrals exit normally; actual monitor faults still fail. |
| `bbbe8cf610c1c0b970f9388afb4f7b1681c1659b` | Optional monitor-success HTTPS ping, disabled by default. Secret URL excluded from logs/lineage; no redirects/retries. Five-second socket timeout is not a total DNS deadline. |
| `666669bf4e3ef44b2bb9bec22b431f1277779ccf` | One held/pending entry per symbol. Verified fill/reconciliation and ordinary readiness defer within original TTL; automatic retries recheck all authority and risk. Unknown submissions/external positions remain hard refusals. |
| `9442ae00041a788dcbff085b454243246b829396` | Replace a brittle source-comment ordering assertion with behavioral manual-only tests for normal and transient initial sizing. |

**Validation.** Exact-source baseline: 302 focused tests pass; imported modules Ruff clean. Integrated health/config: 92 pass. Combined P0 safety run: 258 pass and one obsolete source-order assertion fails; its replacement plus existing authority controls: 7 pass. Final narrow exposure/retry suite: 120 pass; independent reviewer: 9 additional controls pass on the source-equivalent risk commit. One ambiguity-classification defect was caught and corrected before integration. Targeted mypy reports only pre-existing `alpaca_data.py:238` debt in the retained overlay. Tests use temporary ledgers/fake brokers; no production orders or external pings.

**E1 preview: refused.** Read-only production preview at 2026-11-14 14:13:12 UTC: canary passes, 14-day median coverage 81.7%, but latest three daily coverages 79.4%, 77.8%, 70.4% trigger suspension. No apply, cap change or weakened guard. Preview evidence is `alphaos-repair-2026-09-11/evidence/edge-shadow-preview-20261114.json` in the same project. Any later apply needs a fresh passing preview.

**Not done / decisions.** Claude audit, CK merge, controlled deployment and post-deploy observation remain outstanding. Production code/config/services are unchanged. Deadman activation awaits a hosted URL and alert destination. Raising shadow's 30-day cap to 1,800 conflicts with the existing 25%-of-shared-budget limit at 5,000; 7,200 shared needs CK's ruling. Unattended cap remains 1 until separately authorized; the existing broad daily paper-trade cap remains unchanged. EXPLORE-1 and the November 28 scorecard are separate deliverables. This packet has not been delivered through a connected Claude channel; CK can provide it to Claude.
