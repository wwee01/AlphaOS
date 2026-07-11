# AlphaOS Console — Stitch visual design tokens (ported for PR-UI-B1)

**Port method, per the Fable5 ruling 2026-07-11 ("adopt the skin, quarantine
the script"):** this document is colors, typography, spacing, elevation, and
component-pattern tokens ONLY — no application copy, no labels, no sample
values, no fabricated limits. Source: Google-Stitch-generated mockup repo
`/Users/ck/Downloads/stitch_alphaos_operator_console/alphaos_console/DESIGN.md`
(a design-token/style-guide file, not app code or app copy). Ported verbatim
2026-07-11 so the repo has no residual dependency on the local Downloads
folder.

**Explicitly NOT ported (per the same ruling):** any text content, labels, or
values from the Stitch *mockup screens themselves* — those mockups contained
real content bugs (wrong autonomy level shown active, a kill-switch
description implying auto-liquidation, a "LIVE" badge on a paper-only system,
fabricated forex whitelists and a 50 max-daily-trades limit that don't exist,
an inverted ΔR sign convention, pseudocode exit-plan blocks). This file is the
*visual system* extracted from the mockup generator's own design-token
manifest, reviewed and judged safe to adopt on its own merits (colors,
typography, spacing, elevation-via-borders philosophy, component shape
language) — independent of whether the mockup screens' content was correct.
Every string PR-UI-B1 renders comes from real settings/journal data through
the existing call sites in `alphaos/dashboard/streamlit_app.py`, never from
this file or the mockup.

Implemented in `alphaos/dashboard/console_theme.py` and
`.streamlit/config.toml`'s `[theme]` section.

---

```yaml
name: AlphaOS Console
colors:
  surface: '#131313'
  surface-dim: '#131313'
  surface-bright: '#3a3939'
  surface-container-lowest: '#0e0e0e'
  surface-container-low: '#1c1b1b'
  surface-container: '#201f1f'
  surface-container-high: '#2a2a2a'
  surface-container-highest: '#353534'
  on-surface: '#e5e2e1'
  on-surface-variant: '#bcc9cd'
  inverse-surface: '#e5e2e1'
  inverse-on-surface: '#313030'
  outline: '#869397'
  outline-variant: '#3d494c'
  surface-tint: '#4cd7f6'
  primary: '#4cd7f6'
  on-primary: '#003640'
  primary-container: '#06b6d4'
  on-primary-container: '#00424f'
  inverse-primary: '#00687a'
  secondary: '#c6c5cf'
  on-secondary: '#2f3038'
  secondary-container: '#4a4b53'
  on-secondary-container: '#bcbbc5'
  tertiary: '#ffb873'
  on-tertiary: '#4b2800'
  tertiary-container: '#e89337'
  on-tertiary-container: '#5b3200'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#acedff'
  primary-fixed-dim: '#4cd7f6'
  on-primary-fixed: '#001f26'
  on-primary-fixed-variant: '#004e5c'
  secondary-fixed: '#e3e1ec'
  secondary-fixed-dim: '#c6c5cf'
  on-secondary-fixed: '#1a1b22'
  on-secondary-fixed-variant: '#46464e'
  tertiary-fixed: '#ffdcbf'
  tertiary-fixed-dim: '#ffb873'
  on-tertiary-fixed: '#2d1600'
  on-tertiary-fixed-variant: '#6a3b00'
  background: '#131313'
  on-background: '#e5e2e1'
  surface-variant: '#353534'
typography:
  display-data:
    fontFamily: JetBrains Mono
    fontSize: 32px
    fontWeight: '700'
    lineHeight: 40px
    letterSpacing: -0.02em
  headline-sm:
    fontFamily: Inter
    fontSize: 18px
    fontWeight: '600'
    lineHeight: 24px
    letterSpacing: 0.02em
  body-md:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
  data-lg:
    fontFamily: JetBrains Mono
    fontSize: 16px
    fontWeight: '500'
    lineHeight: 24px
  data-sm:
    fontFamily: JetBrains Mono
    fontSize: 12px
    fontWeight: '500'
    lineHeight: 16px
  label-caps:
    fontFamily: Inter
    fontSize: 11px
    fontWeight: '700'
    lineHeight: 16px
    letterSpacing: 0.08em
rounded:
  sm: 0.125rem
  DEFAULT: 0.25rem
  md: 0.375rem
  lg: 0.5rem
  xl: 0.75rem
  full: 9999px
spacing:
  unit: 4px
  gutter: 16px
  margin: 24px
  panel-padding: 12px
  data-gap: 8px
```

## Brand & Style
The design system is engineered for the high-stakes environment of autonomous algorithmic trading. The personality is **Clinical, Analytical, and Mission-Critical**. It rejects the "casino-style" aesthetics of retail trading in favor of a **High-Utility Cockpit** approach.

The visual style is a blend of **Modern Minimalism** and **Technical Instrumentalism**. It prioritizes "Evidence over Emotion," using a dark, low-distraction environment to ensure that semantic signals (alerts, risk shifts) are immediately perceptible. The UI operates as a "silent partner," surfacing data only when it meets specific confidence thresholds. There are no shadows; depth is communicated through technical line work and subtle tonal shifts, mimicking a high-end hardware terminal.

## Colors
The palette is rooted in a **Pure Black (#0A0A0A)** foundation to eliminate backlight bleed and eye strain.
- **Primary (Cyan):** Used exclusively for interactive elements and active system "pulses."
- **Neutral (Zinc/Slate):** Used for structural labels and inactive states to minimize visual noise.
- **Attention (Amber):** Reserved for "Annunciator" states where the AI requires human oversight or is executing a high-slippage trade.
- **Critical (Red):** Used only for "Kill Switches," margin incidents, or hardware failure.
- **Surface:** Layering is achieved by shifting from #0A0A0A to #121212, never through drop shadows.

## Typography
Typography is split by function: **Inter** handles the "Human" layer (labels, instructions, UI controls), while **JetBrains Mono** handles the "Machine" layer (price data, execution logs, timestamps).

All numeric data must be monospaced to prevent "jumping" layouts during high-frequency updates. Labels use uppercase with increased tracking to differentiate them from actionable data.

## Layout & Spacing
The layout follows a **Fixed-Module Grid**. Content is organized into "Instrument Blocks"—self-contained modules that behave like physical rack-mounted hardware.

- **Grid:** 12-column system on desktop, collapsing to a single-column scroll on mobile.
- **Density:** High. Vertical rhythm is tight (4px increments) to maximize "at-a-glance" data coverage.
- **Borders:** Modules are separated by 1px borders (#27272A) instead of margins to maintain the "console" aesthetic.

## Elevation & Depth
This system uses **Tonal Layering** and **Line Logic**.
- **Level 0 (Base):** #0A0A0A - The "Backplane" of the console.
- **Level 1 (Module):** #121212 with a 1px border.
- **Active State:** Elements gain a subtle inner glow or a primary-colored left-hand "active bar."

Avoid all blur-based shadows. Depth is indicated by the thickness of borders: 1px for standard modules, 2px for the focused "Active Instrument."

## Shapes
The shape language is **Precision-Engineered**.
- **Corners:** A minimal 4px (0.25rem) radius is used for primary modules to prevent the UI from feeling "sharp" or hostile, while maintaining a professional, rigid structure.
- **Data Bars:** Rectangular with no rounding, emphasizing the linear nature of time-series data and progress metrics.

## Components
- **Annunciator Badges:** Rectangular status indicators with high-contrast fills. In "Normal" state, they are outlines. In "Alert" state, they pulse with a solid fill (Amber or Red).
- **R-Ladder (Risk/Reward):** A vertical scale component using segmented bars to show current trade positioning relative to Stop-Loss and Take-Profit targets.
- **Evidence Bars:** A multi-segmented horizontal bar chart (0-100%) showing AI confidence. Uses the primary cyan color for "High Confidence" and fades to zinc for "Low Confidence."
- **Monospaced Data Displays:** Large-format numerals for P&L and Balance, always featuring a subtle "LCD-grid" background texture or a dim placeholder (e.g., `888,888.88` faintly behind the actual numbers).
- **Control Inputs:** High-contrast text fields with no fill, only a 1px border that turns Cyan on focus. No hover effects; only active/focus states are visualized to reflect professional equipment behavior.
- **Kill Switch:** A large, specialized button with a guarded state (requires a "Slide to Unlock" or long press) and a solid Red (#EF4444) fill.

---

## PR-UI-B1 deviations from this file (deliberate, recorded here)

- **No pulsing/animated alert fills.** This file's "Alert state, they pulse
  with a solid fill" is not implemented as an animation. The authoritative
  `docs/roadmap/alphaos-ui-ux-design.md` §13 bans flashing/blinking sitewide
  ("No flashing... no autorefresh anxiety") and that document governs UX
  *behavior*; DESIGN.md governs *skin*. PR-UI-B1 renders alert states with a
  static solid fill (no `@keyframes`), which satisfies "filled" without the
  motion.
- **R-Ladder is horizontal, not vertical.** The PR-UI-B1 governing brief
  (Fable5, 2026-07-11) specified a horizontal R-ladder to match the existing
  `alphaos-ui-ux-design.md` §8 wireframe (`-1R ──●──○──▲ +2R`), which
  PR-UI-A already shipped as a horizontal text ladder. This file's "vertical
  scale component" wording is not followed; the horizontal layout is kept
  for continuity with the existing, already-shipped IA.
- **No "Slide to unlock" / long-press kill switch.** The real kill switch is
  a single click + (for engage) already-logged confirmation, per the
  existing, audited `KillSwitch` implementation and the authoritative UX
  doc §10 ("kill-switch engage is one click"). This file's guarded-button
  affordance is a mockup interaction flourish, not adopted — changing the
  kill switch's interaction model is explicitly out of scope for a
  styling-only PR.
- **Audit-fixup 2026-07-11 (correctness + scope/safety, both LOW): the
  Colors/Typography prose above is the generator's own generic style-guide
  language, not a description of AlphaOS.** "executing a high-slippage
  trade" (Amber) and "margin incidents" (Red) describe capabilities AlphaOS
  does not have — this system is paper-only, has no margin trading, and
  `_AMBER`/`_RED` are used exclusively for the TTL-bar "low" state and
  kill-switch-engaged/TTL-expired respectively (see
  `alphaos/dashboard/console_theme.py`), never for a trade-execution or
  margin concept. Left the ported text verbatim (per this file's own
  "ported verbatim" purpose above) rather than editing the source quote;
  recording here instead so the mismatch can't seed future confusion about
  what these two colors actually gate in this codebase.
- **No Google Fonts import.** The Typography section names JetBrains Mono
  and Inter; PR-UI-B1 initially imported both from `fonts.googleapis.com`
  with system fonts as a fallback, then dropped the import entirely
  (audit-fixup 2026-07-11, correctness + scope/safety, both LOW) — a
  webfont fetch would have been this loopback-only dashboard's first-ever
  browser-side external call. `console_theme.py`'s `_MONO_STACK`/
  `_SANS_STACK` are system-font-only; no code in this repo references
  JetBrains Mono or Inter.
- **No dim-placeholder "888,888.88" LCD texture.** Decorative and not
  requested by the governing brief's concrete technical approach; skipped
  to keep the change minimal and avoid a purely-cosmetic addition with no
  functional grounding.
