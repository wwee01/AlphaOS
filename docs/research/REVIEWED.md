# Review ledger — append-only

One line per digest that has been reviewed and briefed to the operator. The
weekly reading session consults this file and SKIPS any digest whose
`(filename, sha256)` pair already appears here.

**Why the hash and not just the filename:** if a digest is edited or rewritten
after review — by a re-run of the upstream task, or by anything else — its hash
changes and it is correctly re-reviewed. A filename-only ledger would silently
skip changed content, which is the same silent-degradation failure this project
keeps paying for.

**This file is bookkeeping, not evidence.** It records that a human was briefed;
it makes no claim about what was in the digest or what was decided. Operator
decisions live in §9 of `docs/ALPHAOS_MASTER_REFERENCE.md` as always.

| digest | sha256 | reviewed (SGT) | by | note |
|---|---|---|---|---|
| `inbox/2026-09-04.md` | `768e6865527e4d96f8bab7c2c310bb5ad4f400862f7433f7913e969e51281c7a` | 2026-09-04 13:35 | Claude (manual test run, CK-requested) | 6 items, all sourced. Flagged: CFTC Reg AT shows BOTH kill-switch semantics are legitimate deployed practice (softens the "gate it" recommendation); J.Finance live-execution study puts round-trip cost at 7-46bps vs the 2bps modelled; J.Econometrics few-cluster work independently supports re-registering H-WIN-1, whose frozen resolution claimed 180 clusters against a real ~25 (below its own floor of 30). |
| `inbox/2026-09-05.md` | `320b9654c9276ca6d0c6929e0e05661802b689a8d44441ef31ed3070426a1046` | 2026-09-05 22:12 | Claude (scheduled) | Supersedes a same-day original (sha `41ff7a1...c7a`, preserved in `docs/research/delivery-history/`) that shipped with an unaudited-provenance disclosure comment; corrected version independently re-verified against all 5 primary sources (4/5 fetched directly and confirmed matching, 1/5 — Nasdaq Equity 6 — blocked by 403, not disproved). Most important finding: MacKinnon's cluster-robust inference paper is the 2nd independent econometrics source in as many weeks reinforcing that H-WIN-1's frozen resolution (claimed 180 clusters vs real ~25) needs re-registering. |
