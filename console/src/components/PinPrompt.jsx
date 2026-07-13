// ND-3 PIN entry -- gates every write action (docs/roadmap/console-
// migration-nd.md §3, §4 ND-3 scope). A small inline confirm control, not a
// full modal component library (this app has none, matches ui.jsx's
// "kept intentionally minimal" convention). The PIN lives in this
// component's OWN local state only (never lifted to a parent, never
// logged -- no console.log/console.error of `pin` anywhere in this file),
// is sent in the POST body (api.js's apiPost, never a URL param), and is
// cleared via actions.js:clearedPinState() the instant a request settles,
// success OR failure.
import React, { useState } from 'react';
import { clearedPinState, generateNonce } from '../actions.js';

const INPUT_STYLE = {
  background: 'var(--surface-low)',
  color: 'var(--text)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  padding: '8px 10px',
  fontSize: 12,
  minHeight: 36,
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
export function PinPrompt({ label, extraFields, onConfirm, onDone, disabled = false }) {
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
        className="badge badge-caps"
        style={{ cursor: disabled ? 'default' : 'pointer', minHeight: 36, opacity: disabled ? 0.5 : 1 }}
        disabled={disabled}
        onClick={() => { setOpen(true); setMessage(null); }}
      >
        {label}
      </button>
    );
  }

  return (
    <div
      className="block"
      style={{ display: 'inline-flex', flexDirection: 'column', gap: 6, padding: 8, minWidth: 180 }}
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
      <div style={{ display: 'flex', gap: 6 }}>
        <button
          type="button"
          className="badge badge-ok badge-caps"
          style={{ cursor: pin && !busy ? 'pointer' : 'default', opacity: pin && !busy ? 1 : 0.5 }}
          disabled={busy || !pin}
          onClick={submit}
        >
          {busy ? 'working…' : 'confirm'}
        </button>
        <button type="button" className="badge badge-caps" style={{ cursor: 'pointer' }} disabled={busy} onClick={cancel}>
          cancel
        </button>
      </div>
      {message && <div style={{ fontSize: 11, color: 'var(--red)' }}>{message}</div>}
    </div>
  );
}
