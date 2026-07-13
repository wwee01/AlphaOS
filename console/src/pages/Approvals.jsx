// ND-2 Approvals page, given real write affordances in ND-4 (docs/roadmap/
// console-migration-nd.md §4 ND-4 scope: "the crown jewels, last"). Renders
// /api/v1/approvals (list_open_proposals() verbatim) -- unchanged from
// ND-2/ND-3. Mirrors streamlit_app.tab_approval_center()'s content exactly
// (exit plan first, then the raw field dump, then TQS) AND, as of this
// phase, its Approve/Reject buttons and margin-approval checkbox too, via
// the SAME PinPrompt component ND-3's Tonight/Annunciator write actions
// already use -- no second PIN-handling implementation. approve_proposal()/
// reject_proposal() re-validate everything server-side (TTL, freshness,
// risk, margin) exactly as they always have; this page adds no client-side
// gate beyond the margin checkbox's own button-enablement (approvals.js:
// canApprove) -- TTL-expired proposals still show both buttons (see
// approvals.js:shouldShowProposalActions's docstring for why).
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { getApprovals, postApprove, postReject } from '../api.js';
import { canApprove, computeTtlBar, marginApprovalRequired, sortByTtl } from '../approvals.js';
import { Badge, Block, badgeTone } from '../components/ui.jsx';
import { ProgressBar } from '../components/ProgressBar.jsx';
import { PinPrompt } from '../components/PinPrompt.jsx';
import { IconClock, IconWarningTriangle } from '../components/icons.jsx';
import { describeUnreachable, formatClockUTC, formatSecondsRemaining } from '../format.js';

const POLL_MS = 10000;

// bar.state ('ok'/'low'/'expired', from approvals.js:computeTtlBar() --
// UNCHANGED math) -> ProgressBar tone. Presentation-only lookup, mirrors
// the ND-2 CSS's own .ttl-bar-low/.ttl-bar-expired color choice exactly.
const TTL_TONE = { ok: 'primary', low: 'warning', expired: 'danger' };

function TtlBar({ secondsRemaining, totalTtlSeconds, label }) {
  const bar = computeTtlBar(secondsRemaining, totalTtlSeconds);
  if (bar.state === 'unknown') {
    return (
      <div className="ttl-row">
        <span className="label-caps"><IconClock size={12} /> TTL</span>
        <span className="ttl-row-unknown">{label}</span>
      </div>
    );
  }
  return (
    <div className="ttl-row">
      <span className="label-caps"><IconClock size={12} /> TTL</span>
      <div className="ttl-row-track">
        <ProgressBar pct={bar.pct} tone={TTL_TONE[bar.state]} height={10} />
      </div>
      <span className="ttl-row-label">{label}</span>
    </div>
  );
}

// A write result carrying `ok: false` (expired, margin required, risk-
// blocked, already-approved, ...) is still an HTTP 200 -- the API layer
// deliberately does not translate it into an error status (ND-4 plan doc:
// "surface that message verbatim in the response... this is a normal,
// expected outcome the operator needs to see, exactly as Streamlit shows
// it inline"). PinPrompt's own contract is "resolve = success, reject =
// show the error inline"; this helper bridges the two by throwing when
// `ok` is false, so PinPrompt's existing red-message display surfaces the
// server's message without a second, parallel display mechanism on this
// page. `onDone(true, ...)` (a real approve/reject) still triggers the
// caller's refetch; `ok: false` does not, since nothing changed.
async function postAndSurface(promise) {
  const result = await promise;
  if (!result.ok) {
    const err = new Error(result.message || 'not approvable');
    err.detail = result.message;
    throw err;
  }
  return result;
}

function ProposalCard({ v, onWriteSuccess }) {
  const [marginApproved, setMarginApproved] = useState(false);
  const [rejectReason, setRejectReason] = useState('');
  const marginRequired = marginApprovalRequired(v);
  const approveDisabled = !canApprove(v, marginApproved);

  return (
    <Block
      title={(
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
          {v.symbol} <Badge tone={badgeTone(v.side)} caps>{v.side}</Badge> qty {v.qty ?? 'n/a'}
        </span>
      )}
      right={(
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          <PinPrompt
            label="approve"
            disabled={approveDisabled}
            onConfirm={(pin, nonce) => postAndSurface(postApprove(pin, nonce, v.proposal_id, marginApproved))}
            onDone={(ok) => ok && onWriteSuccess()}
          />
          <PinPrompt
            label="reject"
            extraFields={(
              <input
                type="text"
                placeholder="reason (optional — defaults to 'user rejected')"
                value={rejectReason}
                onChange={(e) => setRejectReason(e.target.value)}
                style={{
                  background: 'var(--surface-low)', color: 'var(--text)', border: '1px solid var(--border)',
                  borderRadius: 4, padding: '8px 10px', fontSize: 12, minHeight: 36, width: '100%',
                }}
              />
            )}
            onConfirm={(pin, nonce) => postAndSurface(
              postReject(pin, nonce, v.proposal_id, rejectReason.trim() || undefined),
            )}
            onDone={(ok) => { if (ok) { setRejectReason(''); onWriteSuccess(); } }}
          />
        </div>
      )}
      style={{ marginBottom: 10, borderColor: v.proposal_is_stale ? 'var(--red)' : 'var(--border)' }}
    >
      <TtlBar
        secondsRemaining={v.proposal_seconds_remaining}
        totalTtlSeconds={v.proposal_ttl_seconds}
        label={formatSecondsRemaining(v.proposal_seconds_remaining)}
      />
      {v.proposal_is_stale && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--red)', marginBottom: 8 }}>
          <IconWarningTriangle size={13} /> This proposal's TTL has expired — Reject always works; Approve will be
          re-checked server-side and refused with a clear reason.
        </div>
      )}

      <div style={{ fontSize: 13, marginBottom: 4 }}>
        <b>Exit plan:</b> stop <span className="num">{v.stop}</span> · target <span className="num">{v.target}</span>
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 8 }}>
        <b>Invalidation:</b> {v.invalidation_reason || '(not set on this proposal)'}
      </div>

      <div className="num" style={{ fontSize: 12, marginBottom: 4 }}>
        entry {v.entry} · R:R {v.reward_risk ?? 'n/a'} · risk/share {v.risk_per_share ?? 'n/a'} · risk $
        {v.risk_amount ?? 'n/a'} · expected {v.expected_r ?? 'n/a'}R
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8 }}>
        freshness at generation: {v.last_known_freshness} · generated {v.generated_at_sgt ?? 'n/a'}
        {v.requires_margin && ' · requires margin/borrow'}
      </div>

      {marginRequired && (
        <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, marginBottom: 8, cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={marginApproved}
            onChange={(e) => setMarginApproved(e.target.checked)}
          />
          Explicitly approve margin/borrow for this short
        </label>
      )}

      {/* TQS: score is never shown without its confidence pairing (UI/UX
          doc §9) -- shown together or not at all, exactly like Approval
          Center's dataframe columns. */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'var(--text-dim)' }}>
        {v.tqs_score !== null && v.tqs_score !== undefined ? (
          <>TQS (shadow): {v.tqs_score} · <Badge tone={badgeTone(v.tqs_bucket)} caps>{v.tqs_bucket}</Badge> · confidence {v.tqs_data_confidence}</>
        ) : (
          <>TQS (shadow): n/a</>
        )}
      </div>
    </Block>
  );
}

export default function Approvals() {
  const [proposals, setProposals] = useState(null);
  const [unreachable, setUnreachable] = useState(false);
  const [lastGoodAsOf, setLastGoodAsOf] = useState(null);
  const [asOf, setAsOf] = useState(null);
  const mountedRef = useRef(true);

  const poll = useCallback(async () => {
    try {
      const r = await getApprovals();
      if (!mountedRef.current) return;
      setProposals(r.proposals ?? []);
      setAsOf(r.as_of ?? null);
      setUnreachable(false);
      setLastGoodAsOf(r.as_of ?? null);
    } catch {
      if (!mountedRef.current) return;
      setUnreachable(true);
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

  const unreachableMsg = describeUnreachable(unreachable, lastGoodAsOf);
  const sorted = proposals ? sortByTtl(proposals) : null;

  return (
    <div className={unreachable ? 'dim' : ''}>
      {unreachableMsg && <div className="stale-banner">{unreachableMsg}</div>}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 12 }}>
        <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>as of {formatClockUTC(asOf)}</div>
        <Badge tone={sorted?.length ? 'warn' : 'default'}>{sorted?.length ?? 0} open proposal(s)</Badge>
      </div>

      {!sorted ? (
        <div className="label-caps">loading approvals…</div>
      ) : sorted.length === 0 ? (
        <Block title="Approval Center">
          <div style={{ fontSize: 13, color: 'var(--text-dim)' }}>
            No open proposals. Run a scan from the Tonight page to generate proposals.
          </div>
        </Block>
      ) : (
        sorted.map((v) => <ProposalCard key={v.proposal_id} v={v} onWriteSuccess={poll} />)
      )}

      <div style={{ marginTop: 20, fontSize: 11 }}>
        Each action requires the console PIN (set with <code className="num">alphaos console set-pin</code>).
        Approve re-validates freshness, price-drift, spread and risk server-side before any paper order is
        created — nothing here is auto-submitted.
      </div>
    </div>
  );
}
