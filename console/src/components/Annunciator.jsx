// ND-3: the annunciator strip, promoted from Tonight-only (ND-1/ND-2) to a
// GLOBAL element rendered on every page. docs/roadmap/console-migration-
// nd.md §4 ND-3 scope is explicit that kill-switch ENGAGE "goes in the
// annunciator strip area (visible on every page)" -- an operator looking at
// e.g. the Positions or System page must be able to hit the kill switch
// without first switching to Tonight. Governance.jsx's Kill Switch panel
// remains PURE READ/explanation-only; this stays the console's one and only
// kill-switch CONTROL surface (design ruling §4/§8 hard constraint #6).
//
// ND-4 added the DISENGAGE counterpart alongside ND-3's engage, gated the
// same way (PIN-prompted) and shown only when the switch is currently
// engaged -- exactly one of the two ever renders, never both, never
// neither.
//
// ND-6: this component is now presentational -- `data`/`poll` are owned by
// hooks/useAnnunciator.js and passed down from components/Masthead.jsx (so
// the mobile condensed summary and this full strip share ONE poller rather
// than two). The read display logic, the kill-switch engage/disengage
// control, the PIN flow, and the polling cadence are otherwise BYTE-
// IDENTICAL to ND-3/ND-4 -- only the JSX/CSS hierarchy changed (primary
// mode+kill-switch lamps vs. secondary chips, design ruling §4).
import React, { useState } from 'react';
import { postKillSwitchDisengage, postKillSwitchEngage } from '../api.js';
import { Badge } from './ui.jsx';
import { PinPrompt } from './PinPrompt.jsx';
import { IconClock, IconShield, IconWarningTriangle } from './icons.jsx';
import { formatHeartbeat, formatOpenR } from '../format.js';

const DEFAULT_ENGAGE_REASON = 'Engaged from console';

export default function Annunciator({ data, poll }) {
  const [reason, setReason] = useState('');

  if (!data) {
    return (
      <div className="annunciator-strip">
        <span className="label-caps">loading annunciator…</span>
      </div>
    );
  }

  return (
    <div className="annunciator-strip">
      {/* Primary lamps (design ruling §4): mode + kill-switch are the two a
          glance must catch, so they render larger/bolder than the
          secondary chips below. */}
      <Badge tone={data.kill_switch_engaged ? 'danger' : 'ok'} style={{ fontSize: 13, padding: '6px 12px', fontWeight: 700 }}>
        {data.kill_switch_engaged ? <IconWarningTriangle size={13} /> : <IconShield size={13} />}
        {data.kill_switch_engaged
          ? `KILL SWITCH ENGAGED — ${data.kill_switch_reason ?? 'no reason recorded'}`
          : 'kill switch armed (not engaged)'}
      </Badge>
      <Badge style={{ fontSize: 13, padding: '6px 12px', fontWeight: 700 }}>mode: {data.mode ?? 'unknown'}</Badge>

      {/* The ONLY kill-switch control in this console (hard constraint #6):
          engage and disengage are mutually exclusive, keyed off the same
          data.kill_switch_engaged read this strip already displays. */}
      {!data.kill_switch_engaged ? (
        <PinPrompt
          label="engage kill switch"
          extraFields={(
            <input
              type="text"
              placeholder={DEFAULT_ENGAGE_REASON}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              style={{
                background: 'var(--surface-low)', color: 'var(--text)', border: '1px solid var(--border)',
                borderRadius: 4, padding: '8px 10px', fontSize: 12, minHeight: 44, width: '100%',
              }}
            />
          )}
          onConfirm={(pin, nonce) => postKillSwitchEngage(pin, nonce, reason.trim() || DEFAULT_ENGAGE_REASON)}
          onDone={(ok) => {
            if (ok) {
              setReason('');
              poll(); // ND-3 plan doc §5: refetch immediately on a successful write
            }
          }}
        />
      ) : (
        <PinPrompt
          label="release kill switch"
          onConfirm={(pin, nonce) => postKillSwitchDisengage(pin, nonce)}
          onDone={(ok) => ok && poll()} // ND-3 plan doc §5: refetch immediately on a successful write
        />
      )}

      {/* Secondary chips (design ruling §4): autonomy, heartbeat, open-R,
          approvals-pending. Same fields/formatting as ND-3, just visually
          demoted a tier below the two primary lamps above. */}
      <div className="annunciator-secondary-row">
        <Badge>{data.autonomy_level_label ?? 'unknown'}</Badge>
        <Badge><IconClock size={12} /> heartbeat: {formatHeartbeat(data.heartbeat_age_seconds)}</Badge>
        <Badge>
          open R ({data.open_position_count ?? 'n/a'} pos): {formatOpenR(data.total_open_r, data.unmeasurable_positions)}
        </Badge>
        <Badge tone={data.approvals_pending_count ? 'warn' : 'default'}>
          approvals pending: {data.approvals_pending_count ?? 'n/a'}
        </Badge>
      </div>
    </div>
  );
}
