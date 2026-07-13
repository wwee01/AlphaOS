// ND-3: the annunciator strip, promoted from Tonight-only (ND-1/ND-2) to a
// GLOBAL element App.jsx renders on every page. docs/roadmap/console-
// migration-nd.md §4 ND-3 scope is explicit that kill-switch ENGAGE "goes
// in the annunciator strip area (visible on every page, matching the plan's
// own framing of where kill-switch control belongs)" -- an operator looking
// at e.g. the Positions or System page must be able to hit the kill switch
// without first switching to Tonight. The badge strip below is MOVED
// verbatim from Tonight.jsx's former (ND-1/ND-2) `AnnunciatorStrip` --
// same fields, same formatting helpers, same unknown-never-zero handling,
// same polling cadence -- per this phase's own instruction: "do NOT touch
// the annunciator's existing read display logic, only add the engage
// action alongside it." Governance.jsx's Kill Switch panel remains PURE
// READ/explanation-only (its own docstring is updated to point here); this
// stays the console's one and only kill-switch CONTROL surface.
//
// ND-4 adds the DISENGAGE counterpart alongside ND-3's engage, gated the
// same way (PIN-prompted) and shown only when the switch is currently
// engaged -- mirrors render_annunciator()'s own engage/disengage toggle in
// streamlit_app.py exactly (one button or the other is visible, never
// both, never neither).
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { getAnnunciator, postKillSwitchDisengage, postKillSwitchEngage } from '../api.js';
import { Badge } from './ui.jsx';
import { PinPrompt } from './PinPrompt.jsx';
import { IconClock, IconShield, IconWarningTriangle } from './icons.jsx';
import { formatHeartbeat, formatOpenR } from '../format.js';

const POLL_MS = 10000;
const DEFAULT_ENGAGE_REASON = 'Engaged from console';

export default function Annunciator() {
  const [data, setData] = useState(null);
  const [reason, setReason] = useState('');
  const mountedRef = useRef(true);

  const poll = useCallback(async () => {
    try {
      const a = await getAnnunciator();
      if (!mountedRef.current) return;
      setData(a);
    } catch {
      // Every page already renders its own "API unreachable" stale-banner
      // (format.js:describeUnreachable, per-page `unreachable` state) -- a
      // second, global one here would be a redundant, potentially
      // conflicting signal. This strip just keeps its last-known state.
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    poll();
    const id = setInterval(poll, POLL_MS);
    return () => {
      mountedRef.current = false;
      clearInterval(id);
    };
  }, [poll]);

  if (!data) {
    return (
      <div className="annunciator-strip">
        <span className="label-caps">loading annunciator…</span>
      </div>
    );
  }

  return (
    <div className="annunciator-strip">
      <Badge>mode: {data.mode ?? 'unknown'}</Badge>
      <Badge tone={data.kill_switch_engaged ? 'danger' : 'ok'}>
        {data.kill_switch_engaged ? <IconWarningTriangle size={12} /> : <IconShield size={12} />}
        {data.kill_switch_engaged
          ? `kill switch engaged — ${data.kill_switch_reason ?? 'no reason recorded'}`
          : 'kill switch armed (not engaged)'}
      </Badge>
      <Badge>{data.autonomy_level_label ?? 'unknown'}</Badge>
      <Badge><IconClock size={12} /> heartbeat: {formatHeartbeat(data.heartbeat_age_seconds)}</Badge>
      <Badge>
        open R ({data.open_position_count ?? 'n/a'} pos): {formatOpenR(data.total_open_r, data.unmeasurable_positions)}
      </Badge>
      <Badge tone={data.approvals_pending_count ? 'warn' : 'default'}>
        approvals pending: {data.approvals_pending_count ?? 'n/a'}
      </Badge>

      {/* The ONLY kill-switch control in this console: engage (ND-3) and
          disengage (ND-4) are mutually exclusive, matching Streamlit's own
          toggle -- exactly one of these two PinPrompts renders at a time,
          keyed off the same `data.kill_switch_engaged` read this strip
          already displays. */}
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
                borderRadius: 4, padding: '8px 10px', fontSize: 12, minHeight: 36, width: '100%',
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
    </div>
  );
}
