// ND-2 Approvals page -- VIEW-ONLY per the plan doc (§4 ND-2: "Approvals
// (view-only + TTL bars)"). Renders /api/v1/approvals (list_open_proposals()
// verbatim). Mirrors streamlit_app.tab_approval_center()'s content exactly
// (exit plan first, then the raw field dump, then TQS) but with NO
// Approve/Reject affordance anywhere on this page -- every proposal links
// out to the Streamlit Approval Center instead (the same StreamlitLink
// pattern Tonight's "② needs you" block already uses). Do not add a POST
// call here; that is explicitly ND-3/ND-4 scope.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { getApprovals, STREAMLIT_URL } from '../api.js';
import { computeTtlBar, sortByTtl } from '../approvals.js';
import { Badge, Block, StreamlitLink, badgeTone } from '../components/ui.jsx';
import { ProgressBar } from '../components/ProgressBar.jsx';
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

function ProposalCard({ v }) {
  return (
    <Block
      title={(
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
          {v.symbol} <Badge tone={badgeTone(v.side)} caps>{v.side}</Badge> qty {v.qty ?? 'n/a'}
        </span>
      )}
      right={<StreamlitLink href={STREAMLIT_URL}>Decide in Streamlit</StreamlitLink>}
      style={{ marginBottom: 10, borderColor: v.proposal_is_stale ? 'var(--red)' : 'var(--border)' }}
    >
      <TtlBar
        secondsRemaining={v.proposal_seconds_remaining}
        totalTtlSeconds={v.proposal_ttl_seconds}
        label={formatSecondsRemaining(v.proposal_seconds_remaining)}
      />
      {v.proposal_is_stale && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--red)', marginBottom: 8 }}>
          <IconWarningTriangle size={13} /> This proposal's TTL has expired — approval will be rejected. A fresh scan is needed for a current proposal.
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
            No open proposals. Run a scan from Streamlit to generate proposals.
          </div>
        </Block>
      ) : (
        sorted.map((v) => <ProposalCard key={v.proposal_id} v={v} />)
      )}

      <div style={{ marginTop: 20, fontSize: 11 }}>
        This console is read-only for approvals (ND-2). Approve/Reject stays in{' '}
        <StreamlitLink href={STREAMLIT_URL}>the Streamlit Approval Center</StreamlitLink>.
      </div>
    </div>
  );
}
