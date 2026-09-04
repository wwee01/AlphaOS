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
