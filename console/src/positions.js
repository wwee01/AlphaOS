// ND-2 pure display-math for the Positions page's R-ladder -- ports
// alphaos/dashboard/console_theme.py's render_r_ladder() PERCENTAGE MATH
// ONLY (not its HTML string building; the plan doc is explicit: "port the
// concept... write real JSX+CSS"). No DOM, no React, testable in isolation
// (ND-1 precedent: format.js). Mirrors render_r_ladder() line for line:
// same tick set (stop/entry/target), same degenerate-zero-span fallback to
// the track's midpoint (unknown-never-zero: a garbage/coincident risk basis
// still renders, at 50%, rather than dividing by zero or hiding the bar).

// Verdict -> icon, mirrors streamlit_app._VERDICT_ICON exactly.
const VERDICT_ICON = { HOLD: '🟢', ATTENTION: '🟡', EXIT_REVIEW: '🔴' };

export function verdictIcon(verdict) {
  return VERDICT_ICON[verdict] ?? '⚪';
}

// Returns null when any of stop/current/target is unmeasurable -- caller
// renders the same plain-text "R-ladder unavailable" fallback
// render_r_ladder() itself falls back to (unknown-never-zero: never draws a
// ladder with a fabricated position).
export function computeRLadder({ stopR, entryR = 0, currentR, targetR }) {
  if (
    currentR === null || currentR === undefined || Number.isNaN(currentR)
    || stopR === null || stopR === undefined || Number.isNaN(stopR)
    || targetR === null || targetR === undefined || Number.isNaN(targetR)
  ) {
    return null;
  }
  const marks = [
    { name: 'stop', value: stopR },
    { name: 'entry', value: entryR },
    { name: 'target', value: targetR },
  ];
  const allValues = [...marks.map((m) => m.value), currentR];
  const lo = Math.min(...allValues);
  const hi = Math.max(...allValues);
  const span = hi - lo;
  // A degenerate/zero span (every mark coincides) still renders, at the
  // track's midpoint, rather than dividing by zero -- matches
  // render_r_ladder()'s own `_pct()` fallback exactly.
  const pct = (v) => (span <= 1e-9 ? 50 : Math.round(((v - lo) / span) * 100 * 100) / 100);
  return {
    ticks: marks.map((m) => ({ ...m, pct: pct(m.value) })),
    current: { value: currentR, pct: pct(currentR) },
  };
}
