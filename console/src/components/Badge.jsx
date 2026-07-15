// ND-visual: the canonical status-pill component (visual-fidelity pass --
// docs/roadmap/console-migration-nd.md's ND series, this phase closing the
// gap against the Stitch operator-console mockups). Ports the mockups'
// PILL SHAPE/WEIGHT only -- colors still come from the existing token
// system (styles.css :root), and this file introduces zero new tokens.
// "Quarantine the script" governs here exactly as it did for the original
// ND-1 CSS port (styles.css's own header comment) and the Streamlit
// console_theme.py CSS pass before it: no mockup text/labels/numbers are
// reproduced, only the pill's shape/weight/tone vocabulary.
//
// This REPLACES ui.jsx's former inline Badge implementation (ui.jsx now
// re-exports this one) -- every existing call site (Annunciator.jsx,
// Approvals.jsx, Governance.jsx, System.jsx) keeps working unchanged
// because the legacy tone names ('ok'/'warn'/'danger'/'default') are still
// accepted below, aliased onto the same two CSS classes they always mapped
// to (.badge-ok/.badge-warn/.badge-danger) -- this pass only adds visual
// weight to those classes in styles.css, it does not rename them.
import React from 'react';

// tone -> CSS modifier class appended to the base "badge" class (mirrors
// the combined-class pattern the original ui.jsx Badge used: "badge
// badge-ok", never "badge-ok" alone, since badge-ok itself sets no
// display/padding/border-radius of its own). New canonical tone names
// (primary/success/warning/danger/neutral) sit alongside the legacy ones
// they're aliased to, so every pre-existing call site keeps rendering
// identically pixel-for-pixel apart from this pass's shared CSS bump.
//
// ND-7 (design ruling §3 semantic migration): `success` now maps to its OWN
// green class (`badge-success`), no longer aliased onto the same class as
// `primary`/`ok` (brand cyan). ND-1..6 collapsed the two -- cyan doubled as
// both "brand" and "good/armed" -- which the ND-visual audit flagged as a
// LOW (primary/success rendering identically). The ruling separates the
// axes: green means good (INTACT/PROTECTED/USABLE/positive-R/ARMED-safe
// states, via TONE_BY_STATUS below), cyan is brand/active only (nav,
// current-price marker, TTL-ok, LONG-direction, the masthead kill-switch
// lamp). `ok` stays aliased to brand cyan (every direct `tone="ok"` call
// site in this app -- Masthead/Annunciator/Governance/System -- is the
// kill-switch-armed state, which the approved mockup's masthead lamp shows
// in cyan specifically, not green; see Badge.test.js's new assertion that
// `primary`/`ok` and `success` now resolve to genuinely distinct classes).
const TONE_MODIFIER = {
  primary: 'badge-ok',
  success: 'badge-success',
  ok: 'badge-ok',
  warning: 'badge-warn',
  warn: 'badge-warn',
  danger: 'badge-danger',
  neutral: '',
  default: '',
};

// Domain-vocabulary -> tone lookup, pure and independently testable (see
// Badge.test.js). Every status word this pass wraps in a Badge across the
// 7 views (direction, thesis status, verdict, protection status, freshness
// status, TQS bucket, kill-switch/hypothesis states) funnels through this
// ONE table, so a color meaning is defined in exactly one place. Sourced
// directly from the enums that produce these strings server-side
// (alphaos/constants.py: TradeDirection, ProtectionStatus, FreshnessStatus,
// TqsBucket; alphaos/reports/position_health.py: THESIS_*/VERDICT_*) --
// nothing here invents a status this codebase doesn't already emit.
// Case-insensitive; anything unrecognized (including null/undefined) reads
// 'neutral' rather than throwing or guessing -- unknown-never-zero applied
// to color, not just numbers: an unmapped status still renders, plainly.
const TONE_BY_STATUS = {
  // direction (TradeDirection)
  LONG: 'primary',
  SHORT: 'neutral',
  // thesis status (position_health.THESIS_*)
  INTACT: 'success',
  AT_RISK: 'warning',
  BROKEN: 'danger',
  // verdict (position_health.VERDICT_*)
  HOLD: 'neutral',
  ATTENTION: 'warning',
  EXIT_REVIEW: 'danger',
  // protection watchdog (constants.ProtectionStatus)
  PROTECTED: 'success',
  DEGRADED: 'warning',
  UNPROTECTED: 'danger',
  CLOSED_MISMATCH: 'danger',
  UNVERIFIABLE: 'danger',
  CHECK_ERROR: 'warning',
  UNKNOWN: 'neutral',
  // market-data freshness (constants.FreshnessStatus)
  USABLE: 'success',
  STALE: 'warning',
  MISSING: 'danger',
  CLOSED_SESSION: 'neutral',
  // TQS bucket (constants.TqsBucket)
  STRONG: 'success',
  GOOD: 'success',
  WATCH: 'warning',
  MIXED: 'warning',
  WEAK: 'danger',
  UNSCORABLE: 'neutral',
  // kill switch
  ENGAGED: 'danger',
  ARMED: 'success',
  // hypothesis status (hypotheses/registry.py)
  MET: 'success',
  FAILED: 'danger',
  WITHDRAWN: 'neutral',
  TESTING: 'primary',
  PROPOSED: 'neutral',
  RESOLVED: 'neutral',
};

export function badgeTone(status) {
  if (status === null || status === undefined) return 'neutral';
  return TONE_BY_STATUS[String(status).trim().toUpperCase()] ?? 'neutral';
}

// ND-7: exported (was module-private) purely so Badge.test.js can assert
// the primary/success class split directly -- badgeTone() only returns TONE
// NAMES ('success', 'primary', ...), which were already distinct before
// this pass; the bug this pass fixes lived one layer down, in which CSS
// CLASS each tone name rendered as. TONE_MODIFIER is that layer.
export { TONE_MODIFIER };

// `tone`: see TONE_MODIFIER above. `caps`: opt-in label-caps treatment
// (uppercase/bold/sans, matching the mockups' short-word status pills) --
// left OFF by default so the many existing mixed word+number badges
// ("heartbeat: 12s ago", "approvals pending: 3") keep their current mono
// look; callers wrapping a single short status word (verdict/thesis/
// direction/bucket) opt in explicitly. `className`: ND-7 addition -- an
// optional extra class appended alongside the tone modifier (e.g. the
// masthead kill-switch lamp's `"lamp"` pill treatment, styles.css) --
// purely additive/cosmetic, no existing call site passes it today. No
// @keyframes/animation prop -- §13 calm-console rule, same as the
// component this replaces.
export function Badge({
  tone = 'neutral', caps = false, className: extraClassName, children, style,
}) {
  const modifier = TONE_MODIFIER[tone] ?? '';
  const className = ['badge', modifier, caps ? 'badge-caps' : '', extraClassName]
    .filter(Boolean).join(' ');
  return <span className={className} style={style}>{children}</span>;
}
