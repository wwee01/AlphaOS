# NightDesk statistical-discipline contract (ported for PORT-1)

**Port method, per the specs doc:** this document is prose, schemas, and math
only — no NightDesk code, no AlphaOS code. Source: NightDesk repo
(`/Users/ck/Documents/Claude Playground/nightdesk`), `DECISIONS.md` #85
"Thesis Research Layer" (2026-06-21) and #81 "Paired AI-vs-deterministic
forward measurement" (2026-06-19, referenced by #85 for the clustered-bootstrap
primitive and the forward-tracking destination). Extracted 2026-07-09.

This also serves BASELINE's own port (#81) — BASELINE and PORT-1 share this
one contract doc rather than each extracting their own, since #85 leans on
#81's clustering/bootstrap machinery throughout.

---

## 1. What problem this solves

A research program that tests many hypotheses against the same history will,
by pure chance, find some that look "significant" — roughly `alpha × N` of
them at a nominal `alpha`. Without a correction that scales with the
cumulative test count `N`, every additional hypothesis tested makes the
existing "winners" less trustworthy, silently. NightDesk's design makes that
correction *operative* (it can actually demote a previously-promising result
as more tests accumulate), not cosmetic (a decoration that never changes
anything).

Two failure modes get separate, distinct defenses:

- **Optional stopping on ONE hypothesis** (re-running the same backtest
  hoping for a luckier p-value, keeping the lucky run) — defended by making
  each hypothesis's own evidence immutable once computed.
- **Multiple-comparisons inflation across MANY hypotheses** (testing 20
  variants and only reporting the one that looks good) — defended by a
  family-wise correction that always reflects the true cumulative count, and
  by requiring every pre-specified variant to be its own registry row (no
  variant can be tested and quietly discarded without counting against `N`).

## 2. The three-way verdict

Every tested hypothesis lands in exactly one of three states, computed from
its own evidence plus the family-wide correction:

- **`rejected`** — the walk-forward out-of-sample confidence interval is
  fully below zero. A strong economic prior does **not** rescue a clearly
  negative result — priors can argue for testing something, never for
  keeping it after the data says no.
- **`forward-test-candidate`** (a *recommendation*, never an auto-promotion)
  — either (a) the OOS CI is above zero, the sample is trustworthy (clears
  the minimum-cluster floor), **and** it survives the family-wide FDR gate;
  or (b) the result is inconclusive but the hypothesis carries a
  *strong, pre-documented* prior — the "cheap forward-test escape hatch":
  forward-tracking is safe and inexpensive, so a well-motivated hypothesis
  that the backtest can't yet rule in or out is allowed a live look, gated
  on the prior being genuinely strong and written down *before* the test
  ran, not rationalized after seeing an inconclusive result.
- **`inconclusive`** — everything else. The default, not a failure state.

No code path ever moves a hypothesis out of these lab-computed states into
active use. A separate, human-only field records whether an operator chose
to act on a `forward-test-candidate` — the lab recommends, it never enrolls
itself.

## 3. Multiple-comparisons math

Per-hypothesis evidence includes one **one-sided bootstrap p-value**:
`p = P(resampled OOS mean ≤ 0)`, drawn from the *same* resamples used to
build the hypothesis's own confidence interval (so the p-value and the CI
can never disagree about direction).

Family-wide, over **all** hypotheses that have been evaluated (every
pre-specified variant counts as its own hypothesis; a variant that was
tested and not reported still counts):

**Benjamini-Hochberg step-up procedure (primary gate, q = 0.10 by
default):**
1. Take every hypothesis with a computed p-value. Let `N` be that count.
2. Sort ascending: `p_(1) ≤ p_(2) ≤ ... ≤ p_(N)`.
3. Find the largest rank `k` such that `p_(k) ≤ (k / N) · q`.
4. Every hypothesis at rank `≤ k` is a discovery (survives the FDR gate) at
   level `q`.

Equivalently, expose a per-hypothesis **q-value** (the adjusted p-value —
the smallest FDR level at which this specific hypothesis would be called a
discovery), so any consumer can compare against a threshold without
re-deriving `N` or re-running the procedure:
```
sort ascending: p_(1) ≤ p_(2) ≤ ... ≤ p_(N)
raw_(i)  = (N / i) · p_(i)
q_(i)    = min(raw_(i), raw_(i+1), ..., raw_(N))   -- running minimum from N down to i,
                                                        enforces q is non-decreasing in p
```
A hypothesis is a discovery at level `q` iff its `q_value ≤ q`. This is
mathematically equivalent to the step-up procedure above (same discoveries,
same threshold) — NightDesk's own implementation returns the step-up form
(rank / critical-value / boolean); AlphaOS's own spec calls for a stored,
directly-comparable `q_value` field, so this port exposes the q-value
formulation instead. Same math, different (standard, also-textbook) output
shape.

**Bonferroni (stricter family-wise cross-check, reported alongside, α =
0.05 by default):** a hypothesis is significant iff `p ≤ α / N`. Report
both the BH-FDR result (primary gate) and the Bonferroni result (a second,
more conservative opinion) — NightDesk surfaces both rather than picking
one, on the reasoning that a hypothesis clearing the stricter Bonferroni
bar is extra-trustworthy, and one clearing BH-FDR but missing Bonferroni is
still gated only by the primary rule, not silently downgraded.

**Expected false positives** (context, not a gate): `N × α` — "N
hypotheses tested; ~`N × α` false positives expected by chance alone" —
surfaced next to any result so a good-looking point estimate at high `N`
is read in its true context.

## 4. Evidence is immutable; the verdict is never stored

This is the one place the compressed AlphaOS punch-list summary and
NightDesk's actual, adversarially-verified (47-agent) implementation
diverge — flagged here explicitly per the port method's own rule
("anything that doesn't map cleanly gets a decision in the doc, not an
improvisation in code").

**What NightDesk actually does** (`theses` table's own schema comment,
verbatim intent): *"Evidence is IMMUTABLE once written; the verdict is NOT
STORED — it is computed registry-wide at read time so the BH-FDR
correction always reflects the cumulative test count."* There is no
`q_value` column at all. Every report/CLI command that needs a verdict
calls the same pure correction function over the full, current set of
evaluated hypotheses' stored p-values. A hypothesis that looked like a
candidate at `N=1` can be correctly re-classified `inconclusive` once
`N=21` — this is presented as the mechanism *working as intended*, not a
bug: the whole point of a cumulative correction is that it gets stricter
as more tests accumulate, and a stored-forever verdict computed against a
now-stale, too-small `N` would be exactly the kind of quiet false
confidence this layer exists to prevent.

**What the compressed AlphaOS spec literally says**: *"q_value stored on
the preregistration row at evaluation time (immutable, one-shot); reports
read q_value, never recompute BH over ad-hoc slices."*

**Resolution adopted for this port**: split "immutable" from "never
recomputed."
- A hypothesis's own **evidence** (effect size, CI, one-sided bootstrap p,
  effective-N, span) is computed exactly once, at `evaluated_at_utc`, and
  is genuinely immutable thereafter — this is what defends against
  optional stopping on that one hypothesis, and it is the part the
  original spec's "one-shot" language correctly describes.
- The **verdict** (and its q-value) is never stored as a column at all. It
  is always derived, on demand, by one shared function that pulls every
  evaluated preregistration's stored p-value and runs the step-up
  procedure fresh. Every report, digest line, and CLI command calls this
  SAME function — never its own slice, never its own re-derivation — so
  "reports never recompute BH over ad-hoc slices" is honored in spirit
  (no inconsistent, wrong, partial-family computations anywhere) even
  though the letter of "store q_value, read-only" is not. This is the
  `effective_n`/one-floor-law pattern this codebase already applies
  everywhere else (one shared function, every caller uses it, never a
  local reimplementation) — applied here to the verdict, not just to
  effective-N.
- Practically: a `q_value` *column* still exists on the table for
  operational convenience (so an operator can see the value a report
  used without re-running anything), but it is explicitly labeled a
  **cache of the last computed value, not authoritative** — the
  authoritative path is always "call the shared verdict function," and
  the column is refreshed as a side effect of that call, never written
  independently.

## 5. Sample-size discipline: effective-N and clustering

Multiple candidates observed close together in time on correlated
conditions are not independent evidence — treating each as its own
observation overstates the true sample size and understates uncertainty.
NightDesk's own instrument (#81) resamples at the level of **one trading
night** (its natural decision-batch unit): every candidate proposed the
same night shares that night's market regime and conditions, so the
bootstrap treats "one night" as one cluster, never "one candidate."
NightDesk additionally deduplicates to one row per `(night, symbol)` (the
symbol's own highest-scoring matching setup) before clustering, so a
symbol matching multiple non-mutually-exclusive screens the same night
doesn't inflate its own weight within its own cluster.

A **trustworthy** floor gates whether a result is treated as a verdict at
all versus "insufficient data": NightDesk's own floor is **≥20 clusters**
(≥20 independent trading-night observations) — below that, the answer is
always "insufficient data," never a CI, however extreme the point estimate
looks.

AlphaOS's own clustering unit is **not** "one calendar session" the way
NightDesk's is (NightDesk's decision unit is one overnight batch;
AlphaOS's positions can span multiple holding days) — AlphaOS's own
punch-list spec already correctly adapts this to its own data model:
dedup to one observation per `(symbol, decision_date)`, then cluster
observations on the *same symbol* whose `[decision_date, decision_date +
max_holding_days]` windows overlap (since overlapping holding periods on
the same name share realized market moves during the overlap, the same
non-independence NightDesk's night-clustering defends against). This is
the correct, already-decided adaptation — not something this port
changes.

## 6. Pre-registration discipline

- Parameters must be **pre-specified from already-existing, already-committed
  boundaries** — not searched or tuned to make a backtest look better. If a
  hypothesis needs a new threshold invented specifically to fit, it isn't
  pre-registered, it's curve-fit after the fact.
- **Every pre-specified variant is its own registry row and its own slot in
  the correction.** Testing three phrasings of "the same idea" means three
  hypotheses count toward `N` — there is no way to test many variants and
  only have the best one "count."
- **Failures are never deleted.** The registry is append-only; a rejected or
  inconclusive hypothesis stays in the family forever, because removing it
  would retroactively (and dishonestly) shrink `N` for every hypothesis
  evaluated after it.
- **The lab never auto-promotes.** A hypothesis reaching `forward-test-candidate`
  is a recommendation; a separate, explicitly-operator-set field (never
  written by any automated path) records whether it was actually approved
  for forward tracking. This mirrors AlphaOS's own existing
  diff-to-version-closure law (only an operator-committed action changes
  live behavior).
- **Forward-tracking destination is defined at registration time, wired
  later.** A hypothesis record carries a field describing *how* an approved
  candidate would connect to the live/shadow measurement path (reusing the
  existing paired forward-measurement instrument, never a second one) — but
  the actual wiring is a deliberately separate, deferred step, gated on
  operator approval. The schema anticipates it; nothing auto-activates it.

## 7. What this port explicitly does not carry over

- NightDesk's specific backtester/fill-model/regime-cell machinery (#79/#84)
  is NightDesk-specific; AlphaOS's own outcome/replay engine
  (`alphaos/learning/outcomes_engine.py`) is the "one replay engine" this
  port's evidence must be computed through — never a second implementation.
- NightDesk's `theses` table fields tied to its own backtester shape
  (`spec.baseTags`, `universe`, `entry_rule`/`exit_rule` as free text, etc.)
  are NightDesk's own hypothesis-authoring convenience, not part of the
  statistical contract — AlphaOS's `preregistrations` table only needs
  hypothesis text, the metric under test, the floors (effective-N + span),
  `analysis_not_before`, and the evidence/verdict fields above.
