# AlphaOS Console — Design Ruling ND-7 ("Aurora Deep-Space")

**Authority: Fable 5, design lead, 2026-07-14, under the operator's standing
total-design-control grant. Operator selected this direction from four
presented identities and approved the interactive concept mockup.**
Builder: Sonnet. Audits: 2× Opus per T4. Merge: explicit operator instruction.

This ruling SUPERSEDES ND-6's visual system (§3 of
`console-design-ruling-nd6.md`) while keeping ND-6's structure, components,
views, and every non-visual law. ND-6 composed the console correctly; the
operator's verdict was that its *look* was still too conservative — "a far cry
from what I know you can do." ND-7 is the answer: commit fully to one
cinematic identity instead of another timid variation on flat-black-plus-cyan.

## 0. The approved reference

The operator approved a working interactive concept:
- Artifact: https://claude.ai/code/artifact/54676032-a6a9-40d6-85a8-87a81b6fffb0
- Source file (readable on this machine):
  `/private/tmp/claude-501/-Users-ck-Documents-Claude-Playground-AlphaOS/fb58125c-44a4-4486-b7ba-30cd4a772e81/scratchpad/aurora-console.html`

Sonnet: read that file. Port its **visual language** — tokens, glass, light,
composition — into the real console. Do NOT port its data (all illustrative:
NVDA/AAPL/MSFT rows, every numeral, the funnel counts — none of it is real).
The real console renders only real API data, as it does today.

## 1. The identity in one paragraph

A luminous deep-space operations deck. The ground is an indigo-black void with
a **living aurora** — slow-drifting fields of cyan, violet and magenta light —
behind everything, textured with fine grain. Panels are **real glass**:
translucent, backdrop-blurred, top-lit with a bright hairline edge, floating
over the sky. Light is the design material: the decision that needs the
operator glows; the current-price marker on an R-ladder is a small luminous
orb; the one hero numeral per view emits its own aura. Everything that is not
one of those lit moments stays quiet, dim, and precise.

## 2. TWO DELIBERATE LAW CHANGES (the audits must check against THESE)

ND-1..ND-6 carried two rules that ND-7 explicitly retires. Both retirements
are rulings, not oversights:

**2a. The palette lock is RETIRED.** Earlier phases locked the console to the
Streamlit dashboard's palette (#131313 / cyan #4cd7f6) so the two surfaces
"read as one system." That coupling served the migration; the migration is
functionally complete (ND-4) and Streamlit is a break-glass fallback awaiting
retirement (ND-5). The console now owns its own identity. The Streamlit
dashboard keeps its old theme untouched — divergence is intended.
Consequence: guard/tests that pin the OLD hex values (if any assert `#131313`
etc. in console CSS) are updated to pin the NEW tokens instead — that is a
legitimate test update, not test-weakening, when done together with this
ruling. `docs/roadmap/ported/stitch-design-tokens.md` remains historical
record; it no longer governs the console.

**2b. A narrow AMBIENT-MOTION exception to the §13-derived "nothing moves
while idle" rule.** The aurora background drifts continuously and slowly.
Ruling: permitted, because §13's intent is "no false urgency, no flashing, no
attention-grabbing motion on data or status" — a barely-moving low-opacity sky
signals nothing and demands nothing. The exception is exactly this wide and
no wider:
- Ambient motion is allowed ONLY on the background sky layer (the aurora
  blobs). Cycle time ≥ 30s, opacity ≤ ~0.55 pre-blend, heavy blur (≥80px),
  `mix-blend-mode: screen`, behind a darkening scrim.
- Data, status, badges, lamps, bars, numerals, borders: STILL one-shot only.
  No pulsing kill switch, no breathing panels, no looping glow — unchanged.
- `prefers-reduced-motion: reduce` disables the aurora drift entirely (the
  static gradient wash remains — the look survives, the motion stops).

## 3. Token system (authoritative — from the approved mockup)

Replace the console's `:root` tokens with (names may be adapted to existing
conventions, values are the ruling):

```
--void:      #060812   /* page ground — indigo-black, never pure black */
--abyss:     #0a0e1f   /* panel base under the glass gradient */
--glass:     rgba(34,44,86,.28)      /* chip/low glass fill */
--glass-hi:  rgba(120,150,255,.16)
--hairline:  rgba(140,160,230,.14)   /* default borders */
--hairline-lit: rgba(160,190,255,.45)/* the top-lit inner edge */

--ink:       #eef2ff   /* primary text */
--ink-2:     #c3ccf2   /* secondary */
--ink-dim:   #7d88b8   /* labels — indigo-biased, deliberately not grey */

/* BRAND light (aurora) — interactive/active/identity, NOT semantic state */
--cy:  #5be3d6   /* aurora cyan-teal: brand, active nav, live marker, TTL ok */
--cy2: #38bdf8
--vi:  #a78bfa   /* violet: gradient partner; shadow-tier accent base */
--mag: #e879f9   /* magenta: gradient tail only — never a UI state color */

/* SEMANTIC state — separate axis from brand, per info-design law */
--good: #3ddc97  /* healthy / positive R / intact / protected */
--warn: #ffc24b  /* attention / at-risk / TTL low */
--crit: #ff5d73  /* critical / stop / engaged kill switch / negative R */
--shadow-tier: #8f8be0  /* measurement-only marker (unchanged role) */
```

**SEMANTIC MIGRATION (important, audit this for consistency):** in ND-1..6,
cyan doubled as both brand AND "good/armed". ND-7 separates the axes:
**green (--good) now means good** (INTACT, PROTECTED, USABLE, positive R,
ARMED-safe states), and **cyan is brand/active only** (nav active, current
price marker, TTL-ok fill, primary action). Update `Badge.jsx`'s
`TONE_BY_STATUS`/`TONE_MODIFIER` so `success` maps to a real green class
(distinct from `primary` at last — this also resolves the ND-visual audit's
LOW about primary/success collapsing). One table governs every mapping, as
today. Negative/positive R numerals: --crit / --good with soft matching
text-shadow glow.

Typography: UNCHANGED (system stacks only — the fancy is light and depth,
never a webfont). Keep tabular-nums everywhere digits align. Radius: 14px
panels / 9px small. Type scale from ND-6 stands.

## 4. The four signature treatments (where the boldness budget goes)

1. **The sky.** Three blurred radial blobs (cy / vi / mag), drifting per §2b,
   over `--void`, under a fine SVG-noise grain (≈5% opacity) and a radial
   scrim that darkens edges so content contrast never suffers. This lives in
   the app shell once, behind everything, `pointer-events:none`.
2. **Glass panels.** Every InstrumentBlock becomes glass: gradient fill from
   `rgba(26,34,68,.34)` to `rgba(12,16,36,.30)`, 1px `--hairline` border,
   inner top hairline highlight (`box-shadow: 0 1px 0 var(--hairline-lit)
   inset`), deep soft drop (`0 24px 60px -40px rgba(0,0,0,.9)`),
   `backdrop-filter: blur(16px) saturate(1.25)`. A **lit** variant (cyan-tinted
   border + outer glow) exists for exactly ONE panel per view — the panel that
   needs the operator (Tonight's one-action/decision hero; an Approvals card
   whose TTL is the soonest; otherwise none). The **shadow** variant keeps the
   violet tint for measurement-only surfaces (Learning etc.).
3. **Luminous instruments.**
   - R-ladder: 8px rounded track; gradient fill (crit→warn→good left-to-right
     when current > entry; crit-toned when below entry — port the mockup's
     fill-pos/fill-neg exactly); stop/entry/target ticks with micro-labels;
     the current marker is a 16px radial-gradient orb with dark ring + glow,
     value floated above in matching tone. SAME `computeRLadder` math —
     rendering only.
   - TTL bar: glowing gradient fill (cy→cy2) when ok, --warn when low, --crit
     full-bar when expired (the expired-paints-full law stands). Same
     `computeTtlBar` math.
   - Funnel: gradient bars (vi→cy) with soft glow for the "alive" stages,
     dimmed violet for rejected/blocked. Same funnel math.
   - Sparkline: bars/line in --cy with soft glow, honest empty state stands.
4. **Hero numeral.** One per view (ND-6's StatTile placement stands): huge
   mono tabular numeral, tone-colored by sign/semantics with a soft matching
   text-shadow aura. Everything else stays 12–14px.

Masthead: glass bar — gradient wordmark (cy→vi→mag, background-clip:text),
live UTC clock, kill-switch lamp as a glowing pill (cyan-dot ARMED / crit
ENGAGED — static glow, no pulse), mode chip, engage/disengage button (crit
outline treatment). Vitals chips row beneath. Nav: quiet links; the active
view carries a cy→vi gradient underline with a soft glow. Mobile: same
collapse pattern as ND-6 (chip + expandable strip, bottom tab bar) reskinned
to glass; the bottom tab bar becomes a glass dock; active tab gets the
gradient underline. PIN bottom-sheet/modal reskinned to glass — SUBMIT LOGIC
UNTOUCHED, as always.

## 5. Performance & platform notes (Mac mini + iPhone Safari)

`backdrop-filter` + large blurs are GPU-costly. Requirements: exactly 3 sky
blobs, `will-change: transform` on them and nothing else; blobs animate
`transform` only (never filter/opacity); on ≤480px viewports halve the blob
blur radius and drop `backdrop-filter` saturation to keep scrolling at 60fps;
`-webkit-backdrop-filter` prefixes everywhere (Safari). If real-device
scrolling stutters, degrade panels to solid `--abyss` fills at 92% opacity
BEFORE weakening anything else — depth is sacrificial, legibility is not.
Verify contrast: body text (--ink-2 on glass-over-void) must hold WCAG AA in
the worst case (lightest aurora blob directly behind a panel) — the scrim +
panel fills are sized to guarantee this; check it, don't assume it.

## 6. Everything that does NOT change (the audits' other half)

All ND-6 §8 constraints except the two §2 retirements, verbatim:
1. Zero external calls — system fonts + inline SVG + data-URI grain only.
2. No fabricated content — banned strings (CONSOLE_01 / OPERATOR_ACTIVE /
   NQ1! / ES1!) and the guard tests stand; extend the guard to new files. The
   mockup's illustrative data ports NOWHERE.
3. Data/status motion: one-shot only (page-load rise, value-change flash),
   reduced-motion-gated. Only the sky is exempt, per §2b.
4. Unknown-never-zero, numbers AND charts.
5. Shadow-tier visually distinct (now: violet glass variant).
6. Kill-switch control single-instance in the masthead; real-money lock has
   no unlock affordance.
7. Reporting-law floor gate on Learning untouched.
8. ZERO backend/data-contract change: `alphaos/**` untouched; all six logic
   modules (positions/approvals/learning/decisions/format/actions + api.js)
   byte-identical except where a TONE table or class name must change per §3's
   semantic migration — those changes live in Badge/ui/components ONLY, never
   in submit logic or compute math. Python API suite passes unchanged.
9. A11y: focus-visible, AA contrast (see §5), aria-hidden decorative SVG,
   semantic controls.
10. Diff scoped to `console/` only.

## 7. Process

Worktree `/tmp/alphaos-nd7`, branch `feat/nd7-aurora`. Same T4: build →
verify (Python API suite unchanged, ruff/mypy untouched-green, `npm test`
build+vitest green, live check on an ISOLATED scratch DB — never the
production `data/alphaos.db` — at desktop ~1440px AND 390px) → single commit →
2× Opus audits → fixups → hold for explicit operator merge. Where this ruling
is silent, match the approved mockup; where both are silent, taste consistent
with §1. The §6 constraints and §2's exact exception boundaries are
non-negotiable.
