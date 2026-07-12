// ND-3 pure logic for the write-action flow (docs/roadmap/console-
// migration-nd.md §3 "Idempotency", §4 ND-3 scope). No DOM/React
// dependency -- same "pure module, tested with vitest" pattern format.js/
// positions.js/approvals.js already establish -- so PinPrompt.jsx and the
// Actions/Annunciator components just call these rather than re-deriving
// the logic inline in a component.

// A client-generated nonce, one per user-intent (ND-3 plan doc §3: "one per
// button-press"). crypto.randomUUID() is available in every browser this
// console targets (Vite build, no legacy-browser support claimed anywhere
// in this app) and in this module's own Node 22+ test environment (see
// console/package.json "engines"); the fallback below only matters if
// either of those assumptions is ever wrong, so it exists purely as a
// non-crashing degrade path, not as a claimed-secure alternative -- the
// nonce's job is REPLAY DETECTION (uniqueness), not secrecy; the PIN is
// the actual secret, and is never touched by this module.
export function generateNonce() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  /* c8 ignore next */
  return `nonce_${Date.now()}_${Math.random().toString(36).slice(2)}`;
}

// The PIN field's next value once a write request settles -- SUCCESS or
// FAILURE, always the same answer (ND-3 plan doc §5: "PIN never logged to
// console, cleared from state immediately after the request completes
// (success or fail)"). Extracted as its own zero-argument function (rather
// than an inline `setPin('')` at each of PinPrompt.jsx's two call sites) so
// this one-sentence security property has exactly one place it could be
// gotten wrong, and that place is independently testable without mounting
// React or making a network call.
export function clearedPinState() {
  return '';
}
