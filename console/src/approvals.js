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

// ND-4 pure logic for the Approve/Reject write affordance (docs/roadmap/
// console-migration-nd.md §4 ND-4 scope). No DOM/React dependency, same
// "pure module, tested with vitest" pattern as computeTtlBar/sortByTtl
// above -- Approvals.jsx just calls these rather than re-deriving the
// logic inline in the component.

// True iff `proposal` needs the explicit margin/borrow checkbox before
// Approve may be submitted -- mirrors streamlit_app.tab_approval_center()'s
// `if v["requires_margin"]:` gate exactly. `proposal` may be null/undefined
// (a render can race a poll clearing the list); treated as "not required"
// rather than throwing.
export function marginApprovalRequired(proposal) {
  return Boolean(proposal?.requires_margin);
}

// Whether the Approve button should be enabled right now, given the
// per-card checkbox state `marginApproved`. When margin approval isn't
// required this is always true (nothing gates it); when it IS required,
// Approve stays disabled until the operator has explicitly checked the
// box -- never silently approved by omission, never blocked without the
// checkbox being visible to explain why (ND-4 plan doc: "must be checked
// before Approve is enabled... never silently defaults to approved OR
// silently blocks without explanation"). The server re-validates this
// exact condition independently (orch.approve_proposal()'s own
// `requires_margin`/`approve_margin` gate) -- this function only controls
// the BUTTON's enabled state, it is never the sole gate.
export function canApprove(proposal, marginApproved) {
  if (marginApprovalRequired(proposal)) {
    return Boolean(marginApproved);
  }
  return true;
}

// Whether Approve/Reject should be VISIBLE for a proposal at all. Always
// true, deliberately -- TTL expiry is never a client-side gate on button
// visibility (ND-4 plan doc: "TTL-expired proposals: still show Approve/
// Reject... clicking Approve on an expired one will get the ok:False
// 'expired' message back from the server -- surface that clearly, don't
// hide the button preemptively, since... client-side staleness checks are
// advisory only"). Kept as a named function (not just "always render the
// buttons" inlined in the component) so this is one documented,
// independently-testable invariant rather than an implicit omission that
// a future change could silently regress by adding a hide-when-stale
// condition.
export function shouldShowProposalActions(_proposal) {
  return true;
}
