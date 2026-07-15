// ND-2 Approvals page, given real write affordances in ND-4 ("the crown
// jewels"). Renders /api/v1/approvals (list_open_proposals() verbatim) --
// unchanged from ND-2/ND-3. Mirrors streamlit_app.tab_approval_center()'s
// content exactly (exit plan first, then the raw field dump, then TQS) AND
// its Approve/Reject buttons and margin-approval checkbox, via the SAME
// PinPrompt component ND-3's other write actions already use -- no second
// PIN-handling implementation. approve_proposal()/reject_proposal()
// re-validate everything server-side (TTL, freshness, risk, margin) exactly
// as they always have; this page adds no client-side gate beyond the
// margin checkbox's own button-enablement (approvals.js:canApprove).
//
// ND-6: restyled per the design ruling §5 -- a thicker TTL bar with the
// existing ok/low/expired states, the exit plan stated first (asymmetric
// friction: the thing you're committing to is most visible), a plain
// restatement of the max loss beside Approve, and the margin checkbox
// unchanged. Zero submit-logic change: postApprove/postReject/PinPrompt
// call sites are byte-identical to ND-4.
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
// UNCHANGED math) -> ProgressBar tone.
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
        <ProgressBar pct={bar.pct} tone={TTL_TONE[bar.state]} height={14} />
      </div>
      <span className="ttl-row-label">{label}</span>
    </div>
  );
}

// A write result carrying `ok: false` (expired, margin required, risk-
// blocked, already-approved, ...) is still an HTTP 200 -- surfaced verbatim
// via PinPrompt's own error-message display, exactly as Streamlit shows it
// inline.
async function postAndSurface(promise) {
  const result = await promise;
  if (!result.ok) {
    const err = new Error(result.message || 'not approvable');
    err.detail = result.message;
    throw err;
  }
  return result;
}

// `lit`: ND-7 addition (design ruling §4.2) -- true for exactly the ONE
// card whose TTL is soonest (Approvals.jsx's default export passes
// `lit={i === 0}` on the already-sortByTtl()-ordered list, no new
// sort/logic -- sortByTtl is byte-identical). A stale proposal's red
// border (a warning, not a "this needs your positive attention" glow)
// takes precedence over the lit treatment.
function ProposalCard({
  v, onWriteSuccess, lit,
}) {
  const [marginApproved, setMarginApproved] = useState(false);
  const [rejectReason, setRejectReason] = useState('');
  const marginRequired = marginApprovalRequired(v);
  const approveDisabled = !canApprove(v, marginApproved);

  return (
    <Block
      reveal
      tone={lit && !v.proposal_is_stale ? 'lit' : undefined}
      title={(
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
          {v.symbol} <Badge tone={badgeTone(v.side)} caps>{v.side}</Badge> qty {v.qty ?? 'n/a'}
        </span>
      )}
      style={{ borderColor: v.proposal_is_stale ? 'var(--red)' : undefined }}
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

      {/* Asymmetric friction (design ruling §5): the exit plan -- what
          you're actually committing to -- is the most visible thing on the
          card, stated plainly before the raw field dump below it. */}
      <div style={{ fontSize: 14, marginBottom: 4, marginTop: 6 }}>
        <b>Exit plan:</b> stop <span className="num">{v.stop}</span> · target <span className="num">{v.target}</span>
      </div>
      <div className="prose" style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 10 }}>
        <b>Invalidation:</b> {v.invalidation_reason || '(not set on this proposal)'}
      </div>

      <div className="num" style={{ fontSize: 12, marginBottom: 4, color: 'var(--text-dim)' }}>
        entry {v.entry} · R:R {v.reward_risk ?? 'n/a'} · risk/share {v.risk_per_share ?? 'n/a'} · risk $
        {v.risk_amount ?? 'n/a'} · expected {v.expected_r ?? 'n/a'}R
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 10 }}>
        freshness at generation: {v.last_known_freshness} · generated {v.generated_at_sgt ?? 'n/a'}
        {v.requires_margin && ' · requires margin/borrow'}
      </div>

      {marginRequired && (
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, marginBottom: 10, cursor: 'pointer', minHeight: 44 }}>
          <input
            type="checkbox"
            checked={marginApproved}
            onChange={(e) => setMarginApproved(e.target.checked)}
            style={{ width: 20, height: 20 }}
          />
          Explicitly approve margin/borrow for this short
        </label>
      )}

      {/* TQS: score is never shown without its confidence pairing -- shown
          together or not at all, exactly like Approval Center's dataframe
          columns. Shadow-tier per design ruling §5/§8 -- plain, dim text,
          never given the same weight as the exit plan above. */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'var(--shadow-tier)', marginBottom: 12 }}>
        {v.tqs_score !== null && v.tqs_score !== undefined ? (
          <>TQS (shadow): {v.tqs_score} · <Badge tone={badgeTone(v.tqs_bucket)} caps>{v.tqs_bucket}</Badge> · confidence {v.tqs_data_confidence}</>
        ) : (
          <>TQS (shadow): n/a</>
        )}
      </div>

      <div className="block-footer" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ fontSize: 12, color: 'var(--text-dim)' }}>
          Approving commits to a max loss of <span className="num" style={{ color: 'var(--red)' }}>{v.risk_amount !== null && v.risk_amount !== undefined ? `$${v.risk_amount}` : 'an unmeasured amount'}</span> (-1R at stop).
        </div>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          <PinPrompt
            label="approve"
            disabled={approveDisabled}
            triggerClassName="badge-success"
            onConfirm={(pin, nonce) => postAndSurface(postApprove(pin, nonce, v.proposal_id, marginApproved))}
            onDone={(ok) => ok && onWriteSuccess()}
          />
          <PinPrompt
            label="reject"
            triggerClassName="badge-danger"
            extraFields={(
              <input
                type="text"
                placeholder="reason (optional — defaults to 'user rejected')"
                value={rejectReason}
                onChange={(e) => setRejectReason(e.target.value)}
                style={{
                  background: 'var(--surface-low)', color: 'var(--text)', border: '1px solid var(--border)',
                  borderRadius: 4, padding: '10px 12px', fontSize: 13, minHeight: 44, width: '100%',
                }}
              />
            )}
            onConfirm={(pin, nonce) => postAndSurface(
              postReject(pin, nonce, v.proposal_id, rejectReason.trim() || undefined),
            )}
            onDone={(ok) => { if (ok) { setRejectReason(''); onWriteSuccess(); } }}
          />
        </div>
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
          <div style={{ fontSize: 14, color: 'var(--good)', fontWeight: 600 }}>✓ No open proposals.</div>
          <div style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 4 }}>
            Run a scan from the Tonight page to generate proposals.
          </div>
        </Block>
      ) : (
        <div className="grid reveal-stagger">
          {sorted.map((v, i) => (
            <div className="col-6" key={v.proposal_id}>
              {/* ND-7: exactly one lit panel per view (design ruling §4.2)
                  -- the soonest-TTL card, since sorted is already
                  sortByTtl()-ordered (byte-identical logic module). */}
              <ProposalCard v={v} onWriteSuccess={poll} lit={i === 0} />
            </div>
          ))}
        </div>
      )}

      <div style={{ marginTop: 20, fontSize: 11 }}>
        Each action requires the console PIN (set with <code className="num">alphaos console set-pin</code>).
        Approve re-validates freshness, price-drift, spread and risk server-side before any paper order is
        created — nothing here is auto-submitted.
      </div>
    </div>
  );
}
