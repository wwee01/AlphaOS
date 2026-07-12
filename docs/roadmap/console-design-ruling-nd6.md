# AlphaOS Console — Design Ruling (ND-6 "The Night Desk")

**Authority: Fable 5, design lead, 2026-07-13. Operator granted total design
control.** This ruling governs a full visual redesign of the React console
(`console/`) — its own PR, **ND-6**, to run AFTER ND-4 (approve/reject +
disengage) merges, and after ND-5's parallel-run window is understood as an
operational (not code) phase. Build agent: Sonnet. Same T4 protocol as every
ND phase (2 independent Opus audits → fix → merge on explicit operator
instruction).

This is a **design ruling**, not a spec dump. It tells Sonnet the intent, the
system, and the guardrails. Sonnet owns the pixel-level execution; where this
doc is silent, Sonnet exercises taste consistent with the stated direction.

---

## 0. The one-paragraph brief

AlphaOS is a machine that trades while its operator sleeps. The console is the
one surface where a human supervises it. Most nights the honest answer is
"nothing needs you" — so the console must reward a **five-second glance** with
total situational confidence, and on the rare night something DOES need a
decision (approve a trade, hit the kill switch), it must make that decision
**deliberate, legible, and safe**. The emotional register is a spacecraft
instrument panel run by a calm professional: precise, authoritative, quietly
expensive. Not a dashboard shouting metrics. An instrument that looks like it
is telling the truth.

## 1. What changed since the last design pass — read this first

Every prior console pass (ND-1 through ND-visual) operated under two
constraints that are now PARTLY lifted:

- **The "~65–70% fidelity ceiling" is GONE.** That ceiling was a *Streamlit*
  limitation (the 2026-07-12 ruling in `alphaos-ui-ux-design.md`). We are on
  React now. There is no compositional ceiling. Design to 100% of the vision,
  not to a substrate's limit. The operator's note — "I don't think the latest
  UI/UX is good enough" — is correct: ND-visual added components to an
  under-composed layout; ND-6 composes.
- **"Adopt the skin only" is RETIRED as a conservatism.** We may now design
  original composition, motion, and hierarchy freely.

Two constraints REMAIN ABSOLUTE and are not design choices:

- **Quarantine the script — forever.** The Stitch mockups
  (`/Users/ck/Downloads/stitch_alphaos_operator_console/`) contain fabricated
  content from this project's history: a fake operator identity
  (`CONSOLE_01`/`OPERATOR_ACTIVE`), non-existent futures tickers
  (`NQ1!`/`ES1!`), fabricated dollar figures, a "LIVE" badge on a paper-only
  system, and an inverted ΔR sign. **Never reproduce any mockup text, label,
  number, ticker, or status string.** Adopt composition, spacing, type, motion,
  color-usage — never content. `console/src/guard.test.js` enforces this; keep
  it green and extend it if you add surfaces.
- **Zero external browser calls (§2.2 of the ND plan).** No CDN, no webfonts,
  no icon-font service, no remote images, no analytics. System font stacks
  only (`--mono`/`--sans` already defined). All icons inline SVG. Everything
  self-contained. This is a loopback-only console that may have no internet.

## 2. The subject's own materials (where the "expensive" comes from)

Design lead's rule: distinctive design comes from the subject's own world, not
from a template. This console's world:

- **R, not dollars.** Everything position/risk-related speaks in R-multiples
  (the system's native risk unit). The R-ladder — stop → entry → current →
  target on one horizontal instrument — is the single most characteristic
  visual in the whole product. Make it the centerpiece it deserves to be.
- **The annunciator.** A permanent status rail (mode · kill-switch · autonomy ·
  heartbeat · open-R · approvals-pending) that never scrolls away. Aviation/
  spacecraft annunciator panels are the reference — a row of labeled state
  lamps that a trained eye reads instantly.
- **The gate funnel.** Candidates → proposed → blocked → rejected. A pipeline
  with real attrition. Visualize it as a funnel, not a table of counts.
- **Shadow vs. live.** TQS, attribution, hypotheses, the canary — all
  measurement-only "shadow" signals that never touch a decision. They deserve
  a visually distinct "being measured, not acted on" treatment so the operator
  never confuses a shadow number for a live control.
- **Trading-day time.** Holding periods count trading days, not calendar days
  (the HOLD-1 fix). Small touch, but the UI should honor it (already does:
  "trading days held 0/3").

## 3. The design system (extend `styles.css` — do not fight it)

### 3.1 Color & depth
Keep the LOCKED palette (the console and the Streamlit fallback must read as
one system, and the guard/tests assume these hexes):
`--bg #131313`, `--primary #4cd7f6` (cyan), `--amber #ffb873`, `--red #ef4444`,
the `--surface-*` ramp, `--text`/`--text-dim`. Semantic meaning is FIXED:
**cyan = good / active / armed-safe**, **amber = attention**, **red =
critical / stop / irreversible**.

Add, deliberately (these are the only new tokens permitted):
- **One shadow-tier accent** — a dim indigo/violet (propose e.g. `#8b8bd9` at
  low saturation), used ONLY to tint shadow/measurement surfaces (TQS,
  attribution, hypotheses, canary) so "measured, not acted on" reads at a
  glance. Never used for a control or a live value.
- **Ambient depth** — a single, very faint radial wash behind the page
  (`radial-gradient` at low alpha near the masthead, like NightDesk's own
  background), so the flat black gains atmosphere without an image. No second
  gradient competing with it. Keep it subtle enough that text contrast is
  untouched (verify against WCAG AA on body text).
- **Elevation via line + surface, not shadow-heavy cards.** Instrument blocks
  are 1px-bordered on a slightly-raised surface. A whisper of shadow is fine
  for the masthead and modals; avoid a soft-drop-shadow-on-everything look
  (that reads generic/AI-default).

### 3.2 Typography — the biggest lever for "fancy" with zero assets
Set a real type scale and hold to it. Mono (`--mono`, SF Mono on every Mac
mini) for ALL numerals and machine-state text — tabular, slightly tracked.
Sans (`--sans`) for labels, prose, nav.
- **Display numerals**: each view has ONE number that matters most (Tonight →
  open-R or moonshot-gap %, Positions → total open R, Learning → resolved-N,
  etc.). Render it large, confident, mono, tabular — the anchor the eye lands
  on first. This is the "hero is a thesis" principle applied to a cockpit.
- **Uppercase micro-labels** (`label-caps` already exists): 11px/700/0.08em
  tracking, `--text-dim`. Section headers, stat labels, chip labels.
- **Body/prose**: sans, ~14px/1.45, `--text`, kept to a readable measure (do
  NOT let the daily-brief prose run the full width of a 27" display — cap the
  reading column ~68ch even inside a wide block).
- Give headings `text-wrap: balance`.

### 3.3 Spacing & grid
- Use a consistent spacing scale (4/8/12/16/24/32) via layout `gap`, not
  ad-hoc margins. Lay siblings out with flex/grid + gap so spacing can't
  silently collapse or double.
- **Widen the desktop canvas.** `#root { max-width: 1200px }` is too narrow for
  a Mac mini on an external display. Go to a responsive shell: comfortable up
  to ~1440–1600px, with multi-column instrument grids that USE the width
  (Tonight: decision + pending side-by-side; Positions: 2-up card grid;
  System: dense multi-panel). Never let a single column of content strand a
  huge empty gutter on a big screen.

### 3.4 Component library (extend what ND-visual built)
ND-visual already shipped `Badge`, `ProgressBar`, `StatFooter`, `icons.jsx`,
`Annunciator`, `PinPrompt`. Do NOT rebuild them — elevate them, and add:
- **`Masthead`** — a real top bar (see §4).
- **`InstrumentBlock`** (formalize the bordered-panel primitive: title-row with
  optional right-aligned status chip, body, optional footer) so every view
  composes from the same brick.
- **`StatTile`** — the big-number + label + optional trend/context unit, for
  the display-numeral moments.
- **`Funnel`** — the candidates→proposed→blocked→rejected attrition viz for
  Decisions (horizontal bars, proportional, real counts).
- **`Sparkline`** — a tiny inline SVG line/bar chart, used ONLY where real
  series data exists (e.g. `todays_activity` counts, scan-batch history from
  `/api/v1/system`'s `scan_batches`). If a view has no real series, do NOT
  fabricate one — a StatTile is the honest fallback. (Unknown-never-zero
  extends to charts: never draw a flat line implying "measured zero" when the
  truth is "no data yet.")
- **`ShadowChip`/shadow treatment** — the indigo-tinted wrapper for shadow-tier
  data, with a tiny "shadow" affordance so its status is unmistakable.

### 3.5 Motion (this is where §13 needs care)
§13 (calm console) bans **flashing/pulsing/blinking** — ongoing, attention-
grabbing animation. It does NOT ban tasteful, one-shot, purposeful motion.
Permitted and encouraged, ALL respecting `prefers-reduced-motion: reduce`
(when set, everything below becomes instant):
- **Page-load reveal**: a quick staggered fade/rise-in of instrument blocks on
  first paint (≤ ~250ms total, one-shot, never repeats).
- **Value-change highlight**: when a polled number changes, a brief one-shot
  background flash-to-normal on that value (≤ ~600ms, decays to nothing, never
  loops). This is the opposite of a pulse — it fires once per real change.
- **Micro-interactions**: hover/focus states on controls, a subtle press state
  on buttons, smooth expand/collapse for progressive disclosure.
- **Explicitly still BANNED**: anything that animates continuously while idle —
  no pulsing kill-switch, no blinking "live" dot, no breathing glows, no
  looping shimmer. If it moves when nothing changed, it's wrong.

## 4. The masthead & annunciator (always visible, both platforms)

**Desktop**: a fixed top bar. Left: `ALPHAOS` wordmark + a small live UTC/SGT
clock. Center/right: the annunciator as a row of state lamps — **mode** and
**kill-switch** are primary (larger, the two a glance must catch), then
autonomy level, heartbeat age, open-R, approvals-pending as secondary chips.
The **kill-switch control** lives here and ONLY here (never duplicated — a
duplicated safety control is a drift trap): armed state reads calm/cyan;
engaged state reads unmistakably red across the whole rail. Engage is always
reachable; per the law, **stopping is the easiest action in the UI** — the
engage affordance is never more than one deliberate click + PIN away, and after
ND-4 the disengage counterpart appears only when engaged.

**Mobile (iPhone Safari)**: the masthead collapses. Wordmark + kill-switch
state + a single compact "status" summary that expands on tap to reveal the
full annunciator. Respect the notch and home indicator (`env(safe-area-inset-*)`).
The kill-switch stays reachable without scrolling.

## 5. Per-view direction (compose, don't just list)

Ground every view in its REAL payload (I verified these against the running
API — do not invent fields):

- **Tonight** (`/api/v1/tonight`): the home. Lead with `one_action` as a large
  hero statement + its supporting StatTile (open-R or moonshot-gap %). Then a
  desktop 2-col grid: "Needs you" (`needs_you`) beside "Open risk"
  (`positions_health` summary). Then "Today's activity" (`todays_activity`,
  with a Sparkline if the counts support one) beside "Moonshot gap"
  (`moonshot_gap` — render its arithmetic as a real formula panel, cyan mono,
  like the mockup's `Δ = Σ(α·wᵢ) − λ(vol)` treatment but with OUR real
  numbers). The many `*_health` keys (canary/eval/atr/baseline/backup/
  hypothesis/text_archive/card_scoreboard) become a compact "system vitals"
  strip of small status lamps — green/amber/red dots with labels, collapsible.
  Quiet state ("nothing needs you") must be a FIRST-CLASS, calm, confident
  screen — not an empty void. Order: hero → kill-switch banner (if engaged) →
  ②③ → ④⑤⑥ (the numeric-order lesson from PR-UI-B4 — do not reorder the brief).
- **Positions** (`/api/v1/positions`): the R-ladder is the star. Each position
  is an InstrumentBlock: symbol + direction Badge + verdict Badge in the title
  row, the big filled R-ladder as the body centerpiece (stop/entry/current/
  target ticks, gradient fill, current-price marker), then a StatFooter
  (to-stop / to-target / thesis / protection / freshness / trading-days-held).
  2-up grid on desktop, stacked on mobile. Protection status tinted by its real
  value (the ND-visual audit-fixup — keep it).
- **Approvals** (`/api/v1/approvals` → `proposals`): after ND-4 this is
  actionable. Each proposal is an InstrumentBlock with a **TTL countdown bar**
  (thick, ok/low/expired states, the "expired paints full red" rule intact),
  the exit-plan (stop/target) stated plainly BEFORE the raw fields (asymmetric
  friction — the thing you're committing to is most visible), the margin
  checkbox when `requires_margin`, and Approve/Reject as deliberate controls
  (Approve = confident primary; both PIN-gated via `PinPrompt`). Approve
  restates the max loss. Empty state = calm "no open proposals."
- **Decisions** (`/api/v1/decisions`): the Funnel component fed by
  `label_summary`/`by_label_decision`. Below it, the `proposed` list as compact
  rows. This is an "understand," not "operate," view — dense but scannable.
- **Learning** (`/api/v1/learning`): ALL of it is shadow-tier — wrap the whole
  view in the indigo shadow treatment so it's unmistakably measurement, not
  control. `tqs`/`attribution`/`hypotheses`/`hypothesis_drafts`/`journal_feed`
  as InstrumentBlocks. **Preserve the reporting-law floor gate** (below-floor
  aggregates never show a fabricated mean/sum — the existing `learning.js`
  guard; do not regress it).
- **Autonomy & Risk** (`/api/v1/governance`): the governance console —
  `autonomy` (level + may/may-not), `hard_limits` (read-only, as a clean spec
  sheet), `kill_switch` (state only — the CONTROL is in the masthead, never
  duplicated here), `real_money_lock` (display-only, no unlock affordance — by
  law), `trading_calendar`. Authoritative, spec-sheet calm.
- **System & Audit** (`/api/v1/system`): the dense one. `health` +
  `startup_checks` as a status grid, `recent_events`/`scan_batches`/
  `scheduler_runs`/`recent_snapshots`/`recent_candidates` as tabbed or
  segmented tables. A Sparkline of scan-batch cadence over time if the data
  supports it. This view may be information-dense — it's for forensics, not
  glancing — but still typographically ordered.

## 6. Mobile is a first-class deliverable, not a media-query afterthought

The operator approves trades from an iPhone. Design mobile Safari as its own
composition, not a squished desktop:
- **Bottom tab bar** for the 7 views (thumb-reachable), or a horizontal
  scroll-snap strip if 7 tabs won't fit — Sonnet's call, but nav must be
  reachable without reaching the top of the screen.
- Touch targets **≥44px**, always. Buttons, tabs, chips, checkboxes, the PIN
  pad.
- Cards full-width, single column, generous vertical rhythm.
- **The PIN prompt becomes a proper bottom sheet** on mobile — big numeric
  input (`inputMode="numeric"`), large confirm/cancel, never a cramped inline
  box. The approve/reject flow must be genuinely comfortable one-handed.
- Safe-area insets top and bottom. No content under the notch or home bar.
- Test at **390px** (iPhone) AND a **Mac mini external-display width** (say
  1440–1600px). Both must feel composed, not just "not broken."

## 7. "Fancy" — the guardrails (so it stays user-friendly)

The operator said "as fancy as possible" AND "user friendly, easy for overview
and controls." When those tension, **usability wins** — a beautiful console the
operator misreads at 3am is a failure. Concretely:
- Fancy = typographic confidence, real composition, one hero moment per view,
  tasteful one-shot motion, the R-ladder and funnel done beautifully, depth via
  layered surfaces. Fancy is NOT: decoration for its own sake, gratuitous
  gradients, motion that competes with data, dense ornamentation that slows the
  glance, or anything that obscures a control.
- Every view must still answer in ≤5s: what is the machine doing, does anything
  need me, what's my open risk, how do I stop it.
- Controls (approve/reject/kill-switch/scan) must be the most obvious
  interactive things on their screen — never buried under styling.
- Spend the boldness in ONE place per view (the hero); keep everything around
  it quiet. A page where everything shouts reads as generic.

## 8. Hard constraints checklist (Sonnet: satisfy every one)

1. Zero external calls — no CDN/webfont/icon-service/remote-image. System fonts
   + inline SVG only. (Guard test + build-output grep must stay clean.)
2. No fabricated mockup content — `CONSOLE_01`/`OPERATOR_ACTIVE`/`NQ1!`/`ES1!`
   and any invented number/ticker/label never appear. Every value from real
   API data.
3. No continuous/looping/pulsing/flashing motion. One-shot only, all gated on
   `prefers-reduced-motion`.
4. Unknown-never-zero, in numbers AND charts. Missing series → honest
   empty/"n/a", never a fabricated flat line or a zero.
5. Shadow-tier data visually distinct from live data/controls.
6. Kill-switch control exists in exactly ONE place (masthead). Real-money lock
   has no unlock affordance.
7. Reporting-law floor gate preserved (Learning).
8. Zero business-logic/data-contract change: this is presentation only. Do NOT
   touch `alphaos/api/*.py`, do NOT change what any endpoint returns, do NOT
   alter write-action logic in `actions.js`/`PinPrompt.jsx` beyond visual
   chrome + the mobile-sheet restructure (the SUBMIT logic — nonce, PIN
   clearing, POST body — stays byte-equivalent). Proof: the Python API test
   suite (`test_api_console*.py`) must pass UNCHANGED.
9. Accessibility: visible keyboard focus states, WCAG AA body-text contrast,
   `aria-hidden` on decorative SVG, semantic buttons/links.
10. Diff scoped to `console/` only. Any `alphaos/*.py` in the diff is a scope
    violation.

## 9. Process for Sonnet

Same T4 discipline as every ND phase. Build in a worktree
(`/tmp/alphaos-nd6`), verify live against an **isolated seeded scratch DB**
(never the production `data/alphaos.db` — copy a `demo-*.db` or init a fresh
journal + `seed_demo()`), screenshot desktop (1440px) AND mobile (390px) for
every view for your own verification, run `npm test` (build+vitest) + the
Python API suite (unchanged) + ruff + mypy, extend `guard.test.js` for any new
surface, commit, and HOLD for the two Opus audits + explicit operator merge.
Where this ruling is silent, use taste consistent with §0's brief and §7's
guardrails — you have real latitude on execution, none on the §8 constraints.
