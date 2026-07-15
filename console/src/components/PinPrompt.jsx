// ND-3 PIN entry -- gates every write action (docs/roadmap/console-
// migration-nd.md §3, §4 ND-3 scope). A small inline confirm control, not a
// full modal component library (this app has none, matches ui.jsx's
// "kept intentionally minimal" convention). The PIN lives in this
// component's OWN local state only (never lifted to a parent, never
// logged -- no console.log/console.error of `pin` anywhere in this file),
// is sent in the POST body (api.js's apiPost, never a URL param), and is
// cleared via actions.js:clearedPinState() the instant a request settles,
// success OR failure.
//
// ND-6: the SUBMIT logic below (nonce minting, PIN clearing on cancel/
// success/failure, the onConfirm/onDone contract, the POST-body
// construction happening one level up in api.js) is byte-identical to
// ND-3/ND-4 -- design ruling §8 hard constraint #8. This pass only restyles
// the open panel: on a narrow viewport it becomes a proper bottom sheet
// (design ruling §6) -- a backdrop + a fixed bottom panel with a big
// numeric PIN input and full-width confirm/cancel -- via the
// `pin-sheet-backdrop`/`pin-sheet-panel` CSS classes (styles.css's
// >=768px media query leaves both inert on desktop, where this renders
// exactly as the ND-3/ND-4 inline panel always did). Touch targets bumped
// to 44px (design ruling §6/§8 hard constraint #9) at every width.
//
// Rendered via a `createPortal` into `document.body` rather than inline
// where the trigger button sits: a PinPrompt can be nested inside an
// InstrumentBlock that opts into the one-shot reveal animation (`reveal`
// prop, design ruling §3.5), and a CSS `transform` -- even one an
// `animation-fill-mode` leaves frozen at `translateY(0)` after the
// animation completes -- makes that block a new *containing block* for any
// `position: fixed` descendant (CSS Transforms spec), which broke this
// panel's `bottom: 0` sheet positioning on mobile (it anchored to the
// animated card instead of the viewport). Portaling to `document.body`
// sidesteps that entirely, on every viewport, regardless of what ancestor
// styling exists now or later -- a pure DOM-target change, not a submit-
// logic change.
import React, { useState } from 'react';
import { createPortal } from 'react-dom';
import { clearedPinState, generateNonce } from '../actions.js';

const INPUT_STYLE = {
  background: 'var(--surface-low)',
  color: 'var(--text)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  padding: '10px 12px',
  fontSize: 13,
  minHeight: 44,
};

// `label`: the trigger button's text.
// `onConfirm(pin, nonce)`: must return a Promise resolving with the parsed
//   response body on success, or rejecting (api.js's apiPost already
//   rejects with a `.status`/`.detail`-bearing Error on a non-2xx). This
//   component doesn't know what the write DOES, only how to collect a PIN,
//   mint a fresh nonce per submit attempt, and report the outcome inline --
//   same "frontend computes nothing business-critical" discipline as
//   everywhere else in this app.
// `onDone(ok, resultOrError)`: optional, called after every settle
//   (success or fail) -- callers use this to trigger an immediate refetch
//   of the view the write just changed (ND-3 plan doc §5: "you may trigger
//   an immediate refetch after a successful write if that's a small clean
//   addition").
// `extraFields`: optional extra inputs rendered above the PIN field (e.g.
//   the kill-switch engage reason text box) -- kept as a render prop rather
//   than a second, action-specific component, so there is exactly one PIN-
//   handling implementation in this app.
// `disabled`: optional (default false) -- ND-4 addition. Disables the
//   trigger button WITHOUT hiding it, e.g. Approvals.jsx's "approve"
//   button while a proposal `requires_margin` and its checkbox is
//   unchecked (docs/roadmap/console-migration-nd.md §4 ND-4: "never
//   silently defaults to approved OR silently blocks without
//   explanation" -- the button stays visible, just inert, alongside the
//   checkbox that explains why). Never used to hide a button for a
//   server-side-only condition (e.g. TTL expiry) -- those stay enabled so
//   the operator sees the server's own authoritative message instead of a
//   client-side guess (see approvals.js:shouldShowProposalActions).
// `triggerClassName`: ND-7 addition -- an optional extra class on the
// CLOSED-state trigger button only (e.g. "badge-success" for approve,
// "badge-danger" for reject/kill-switch engage-disengage), purely cosmetic.
// The open panel's confirm/cancel buttons are unaffected -- same hardcoded
// classes as ND-3/4/6, no submit-logic/DOM-structure change anywhere here.
export function PinPrompt({
  label, extraFields, onConfirm, onDone, disabled = false, triggerClassName,
}) {
  const [open, setOpen] = useState(false);
  const [pin, setPin] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);

  const cancel = () => {
    setPin(clearedPinState());
    setOpen(false);
    setBusy(false);
    setMessage(null);
  };

  const submit = async () => {
    setBusy(true);
    setMessage(null);
    // Freshly minted for THIS submit attempt -- a wrong-PIN retry gets its
    // own new nonce automatically, rather than replaying the same one.
    const nonce = generateNonce();
    try {
      const result = await onConfirm(pin, nonce);
      setPin(clearedPinState());
      setBusy(false);
      setOpen(false);
      onDone?.(true, result);
    } catch (err) {
      setPin(clearedPinState());
      setBusy(false);
      setMessage(err?.detail || err?.message || 'request failed');
      onDone?.(false, err);
    }
  };

  if (!open) {
    return (
      <button
        type="button"
        className={['badge', 'badge-caps', triggerClassName].filter(Boolean).join(' ')}
        style={{ cursor: disabled ? 'default' : 'pointer', minHeight: 44, opacity: disabled ? 0.5 : 1 }}
        disabled={disabled}
        onClick={() => { setOpen(true); setMessage(null); }}
      >
        {label}
      </button>
    );
  }

  return createPortal(
    <>
      {/* Inert on desktop (styles.css only gives this a visual treatment
          under the mobile media query); tapping it cancels, same as the
          Cancel button. */}
      <div className="pin-sheet-backdrop" onClick={cancel} aria-hidden="true" />
      <div
        className="block pin-sheet-panel"
        style={{ display: 'inline-flex', flexDirection: 'column', gap: 8, padding: 10, minWidth: 200 }}
      >
        <div className="label-caps">{label}</div>
        {extraFields}
        <input
          type="password"
          inputMode="numeric"
          autoComplete="off"
          placeholder="PIN"
          value={pin}
          onChange={(e) => setPin(e.target.value)}
          style={{ ...INPUT_STYLE, width: '100%' }}
        />
        <div className="pin-sheet-actions" style={{ display: 'flex', gap: 8 }}>
          <button
            type="button"
            className="badge badge-ok badge-caps"
            style={{ cursor: pin && !busy ? 'pointer' : 'default', opacity: pin && !busy ? 1 : 0.5, minHeight: 44 }}
            disabled={busy || !pin}
            onClick={submit}
          >
            {busy ? 'working…' : 'confirm'}
          </button>
          <button type="button" className="badge badge-caps" style={{ cursor: 'pointer', minHeight: 44 }} disabled={busy} onClick={cancel}>
            cancel
          </button>
        </div>
        {message && <div style={{ fontSize: 11, color: 'var(--red)' }}>{message}</div>}
      </div>
    </>,
    document.body,
  );
}
