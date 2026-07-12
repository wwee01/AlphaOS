// ND-2 pure display-math for the Approvals page's TTL bars -- ports
// alphaos/dashboard/console_theme.py's render_ttl_bar() FILL-PERCENTAGE +
// STATE logic (not its HTML) and streamlit_app.tab_approval_center()'s
// soonest-to-expire sort. No DOM, no React (ND-1 precedent: format.js).
//
// §13 calm-console rule carries over unchanged: this computes a STATIC
// percentage once per poll (matching the API's own "as of" cadence) --
// nothing here starts a client-side ticking countdown between polls, which
// would visually tick down even though nothing has actually been re-checked
// (render_ttl_bar()'s own docstring: "no autorefresh anxiety").

// null state when seconds_remaining/total_ttl_seconds is unknown/unparseable
// -- unknown-never-zero: never a fabricated 0% or 100% bar.
export function computeTtlBar(secondsRemaining, totalTtlSeconds) {
  if (
    secondsRemaining === null || secondsRemaining === undefined || Number.isNaN(secondsRemaining)
    || totalTtlSeconds === null || totalTtlSeconds === undefined || totalTtlSeconds <= 0
  ) {
    return { state: 'unknown', pct: null };
  }
  const isExpired = secondsRemaining <= 0;
  // An expired TTL draws FULL (a solid alert), not an empty near-invisible
  // track -- mirrors render_ttl_bar()'s own documented choice exactly.
  const pct = isExpired
    ? 100
    : Math.max(0, Math.min(100, (secondsRemaining / totalTtlSeconds) * 100));
  const isLow = !isExpired && pct < 20;
  const state = isExpired ? 'expired' : (isLow ? 'low' : 'ok');
  return { state, pct };
}

// Soonest-to-expire first; a proposal with unknown/unparseable
// seconds_remaining sorts LAST, never first -- we can't claim it's urgent
// just because we can't measure it (unknown-never-zero extended to
// "unknown-never-most-urgent"). Mirrors tab_approval_center()'s own sort
// key exactly. Pure -- returns a new array, never mutates its input.
export function sortByTtl(proposals) {
  return [...proposals].sort((a, b) => {
    const av = a.proposal_seconds_remaining ?? Infinity;
    const bv = b.proposal_seconds_remaining ?? Infinity;
    return av - bv;
  });
}
