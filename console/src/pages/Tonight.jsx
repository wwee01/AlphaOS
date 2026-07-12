// ND-1 Tonight cockpit -- the one page ND-1 shipped (docs/roadmap/
// console-migration-nd.md ND-1 scope). Renders the brief blocks ①-⑦ in
// numeric order (matching alphaos/dashboard/streamlit_app.py's
// tab_tonight() -- same order, same data, same quiet-state handling). This
// component computes nothing business-critical: every value shown comes
// straight from /api/v1/tonight; the only "logic" here is display
// formatting (see ../format.js) and which block to show.
//
// ND-3: the annunciator strip that used to live at the top of this page
// moved to App.jsx (components/Annunciator.jsx, rendered globally on every
// page -- see its own module docstring). This page also gains a small
// "Actions" instrument block wiring the three ND-3 writes (scan/monitor/
// report) that streamlit_app.render_sidebar() already exposes -- kill-switch
// ENGAGE lives in the global annunciator strip instead, not here.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { getTonight, postMonitor, postReport, postScan, STREAMLIT_URL } from '../api.js';
import { Block, StreamlitLink } from '../components/ui.jsx';
import { PinPrompt } from '../components/PinPrompt.jsx';
import {
  describeUnreachable, formatClockUTC, formatR, formatSecondsRemaining,
} from '../format.js';

const POLL_MS = 10000;

function ActionsBlock({ onWriteSuccess }) {
  return (
    <Block title="⚙ actions (console writes)">
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
        <PinPrompt label="run scan" onConfirm={(pin, nonce) => postScan(pin, nonce)} onDone={(ok) => ok && onWriteSuccess()} />
        <PinPrompt label="run monitor" onConfirm={(pin, nonce) => postMonitor(pin, nonce)} onDone={(ok) => ok && onWriteSuccess()} />
        <PinPrompt label="generate daily report" onConfirm={(pin, nonce) => postReport(pin, nonce)} onDone={(ok) => ok && onWriteSuccess()} />
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 8 }}>
        Each action requires the console PIN (set with <code className="num">alphaos console set-pin</code>).
        Approve/reject and kill-switch release still require the{' '}
        <StreamlitLink href={STREAMLIT_URL}>Streamlit app</StreamlitLink> (ND-4).
      </div>
    </Block>
  );
}

function OneAction({ brief }) {
  return (
    <Block title="① one action" style={{ borderColor: 'var(--primary)' }}>
      <div style={{ fontSize: 16, fontWeight: 600 }}>{brief.one_action}</div>
      {brief.kill_switch_engaged && (
        <div style={{ marginTop: 10, fontSize: 13, color: 'var(--red)' }}>
          ● KILL SWITCH ENGAGED — {brief.kill_switch_reason ?? 'no reason recorded'}
        </div>
      )}
    </Block>
  );
}

function NeedsYou({ needsYou, exitReview }) {
  const rows = [];
  for (const p of needsYou.pending_approvals ?? []) {
    rows.push(
      <div key={`appr_${p.proposal_id}`} style={{ fontSize: 13, padding: '4px 0' }}>
        {p.symbol} proposal — TTL {formatSecondsRemaining(p.seconds_remaining)}
      </div>,
    );
  }
  for (const inc of needsYou.open_incidents ?? []) {
    rows.push(
      <div key={`inc_${inc.check_id ?? inc.symbol}`} style={{ fontSize: 13, padding: '4px 0', color: 'var(--red)' }}>
        ⚠ open incident: {inc.symbol ?? '?'} — {inc.protection_status ?? '?'}
      </div>,
    );
  }
  for (const fj of needsYou.fused_jobs ?? []) {
    rows.push(
      <div key={`fj_${fj.job_type}`} style={{ fontSize: 13, padding: '4px 0', color: 'var(--amber)' }}>
        fused job: {fj.job_type} ({fj.reason}, {fj.streak} consecutive failures)
      </div>,
    );
  }
  for (const p of exitReview) {
    rows.push(
      <div key={`ex_${p.position_id}`} style={{ fontSize: 13, padding: '4px 0', color: 'var(--red)' }}>
        {p.symbol} position EXIT_REVIEW — human decision required
      </div>,
    );
  }
  return (
    <Block title="② needs you" right={<StreamlitLink href={STREAMLIT_URL}>Approval Center</StreamlitLink>}>
      {rows.length ? rows : <div style={{ fontSize: 13, color: 'var(--text-dim)' }}>(nothing here)</div>}
    </Block>
  );
}

function OpenRisk({ positionsHealth }) {
  const measurable = positionsHealth.filter((p) => p.current_r !== null && p.current_r !== undefined);
  const totalR = measurable.length
    ? measurable.reduce((sum, p) => sum + p.current_r, 0)
    : null;
  const worst = measurable.length
    ? measurable.reduce((min, p) => (p.current_r < min.current_r ? p : min))
    : null;
  return (
    <Block title="③ open risk now">
      {positionsHealth.length === 0 ? (
        <div style={{ fontSize: 13, color: 'var(--text-dim)' }}>No open positions.</div>
      ) : (
        <>
          <div className="num" style={{ fontSize: 15 }}>
            {positionsHealth.length} position(s) · {formatR(totalR)} total
          </div>
          {worst && (
            <div className="num" style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 4 }}>
              worst: {worst.symbol} {formatR(worst.current_r)}
            </div>
          )}
        </>
      )}
    </Block>
  );
}

function Quiet() {
  return (
    <Block title="⑦ status" style={{ borderColor: 'var(--primary)' }}>
      <div style={{ fontSize: 14, color: 'var(--primary)' }}>✓ Nothing needs you right now.</div>
    </Block>
  );
}

function TodaysActivity({ ta }) {
  return (
    <Block title="④ today's machine activity">
      <div className="num" style={{ fontSize: 13 }}>
        candidates: {ta.candidates_today} · proposed: {ta.proposed_today} · blocked: {ta.blocked_today} · rejected: {ta.rejected_today}
      </div>
    </Block>
  );
}

function TonightsBrief({ brief }) {
  const mc = brief.market_condition;
  const bc = brief.best_candidate;
  const wl = brief.what_learned;
  return (
    <Block title="⑤ tonight's brief">
      <div style={{ fontSize: 13, marginBottom: 8 }}>
        {mc.excess_return_pct !== null && mc.excess_return_pct !== undefined ? (
          <>
            market: excess return{' '}
            <span className="num">
              {mc.excess_return_pct >= 0 ? '+' : ''}
              {mc.excess_return_pct.toFixed(2)}%
            </span>{' '}
            vs S&amp;P (paired {mc.paired_trading_days} trading days)
          </>
        ) : (
          <>market: {mc.note ?? 'not yet measurable'}</>
        )}
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 10 }}>⚠ {mc.caveat}</div>

      <div style={{ fontSize: 13, marginBottom: 8 }}>
        best candidate today: {bc ? (
          <>{bc.symbol} — TQS {bc.tqs_score} ({bc.tqs_bucket}), interest {bc.interest_score}, confidence {bc.label_confidence}</>
        ) : '(none)'}
      </div>

      <div style={{ fontSize: 13, marginBottom: 4 }}>learned today ({wl.total_resolved_today} resolved):</div>
      {wl.sentences.length ? (
        wl.sentences.map((s, i) => (
          <div key={i} style={{ fontSize: 12, color: 'var(--text-dim)', padding: '2px 0' }}>· {s}</div>
        ))
      ) : (
        <div style={{ fontSize: 12, color: 'var(--text-dim)' }}>(nothing newly resolved today)</div>
      )}
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 6 }}>⚠ {wl.caveat}</div>
    </Block>
  );
}

function MoonshotGap({ mg }) {
  return (
    <Block title="⑥ moonshot gap (10% MoM target)">
      {mg.status === 'ok' ? (
        <>
          <div className="num" style={{ fontSize: 13 }}>
            implied monthly: {mg.implied_monthly_pct}% vs target {mg.target_monthly_pct}% (expectancy {mg.expectancy_r}R × {mg.trades_this_month} trades × {(mg.risk_per_trade_pct * 100).toFixed(2)}% risk/trade)
          </div>
          <div style={{ fontSize: 12, marginTop: 4 }}>binding constraint: <b>{mg.binding_constraint}</b></div>
        </>
      ) : (
        <div style={{ fontSize: 13 }}>{mg.note}</div>
      )}
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 6 }}>{mg.data_progress}</div>
    </Block>
  );
}

export default function Tonight() {
  const [brief, setBrief] = useState(null);
  const [unreachable, setUnreachable] = useState(false);
  const [lastGoodAsOf, setLastGoodAsOf] = useState(null);
  const mountedRef = useRef(true);

  const poll = useCallback(async () => {
    try {
      const t = await getTonight();
      if (!mountedRef.current) return;
      setBrief(t);
      setUnreachable(false);
      setLastGoodAsOf(t.as_of ?? null);
    } catch {
      if (!mountedRef.current) return;
      // ND-1 plan doc §2.4: never silently show stale data as fresh -- flag
      // unreachable and KEEP whatever we already have (dimmed by the caller),
      // rather than clearing it to a loading/blank state.
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
  const contentStyle = unreachable ? { opacity: 1 } : {};
  const dimClass = unreachable ? 'dim' : '';

  return (
    <div className={dimClass} style={contentStyle}>
      {unreachableMsg && <div className="stale-banner">{unreachableMsg}</div>}

      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 12 }}>
        as of {formatClockUTC(brief?.as_of)}
      </div>

      {!brief ? (
        <div className="label-caps">loading tonight's brief…</div>
      ) : (
        <div className="grid">
          <div className="col-12">
            <ActionsBlock onWriteSuccess={poll} />
          </div>
          <div className="col-12" style={{ marginTop: 4 }}>
            <OneAction brief={brief} />
          </div>

          {(() => {
            const ny = brief.needs_you;
            const exitReview = (brief.positions_health ?? []).filter((p) => p.verdict === 'EXIT_REVIEW');
            const quiet = ny.pending_approval_count === 0 && ny.open_incident_count === 0
              && (ny.fused_jobs ?? []).length === 0 && exitReview.length === 0;
            if (quiet) {
              return (
                <div className="col-12" style={{ marginTop: 4 }}>
                  <Quiet />
                </div>
              );
            }
            return (
              <>
                <div className="col-6" style={{ marginTop: 4 }}>
                  <NeedsYou needsYou={ny} exitReview={exitReview} />
                </div>
                <div className="col-6" style={{ marginTop: 4 }}>
                  <OpenRisk positionsHealth={brief.positions_health ?? []} />
                </div>
              </>
            );
          })()}

          <div className="col-4" style={{ marginTop: 4 }}>
            <TodaysActivity ta={brief.todays_activity} />
          </div>
          <div className="col-8" style={{ marginTop: 4 }}>
            <TonightsBrief brief={brief} />
          </div>
          <div className="col-12" style={{ marginTop: 4 }}>
            <MoonshotGap mg={brief.moonshot_gap} />
          </div>
        </div>
      )}

      <div style={{ marginTop: 20, fontSize: 11 }}>
        <StreamlitLink href={STREAMLIT_URL}>Open the full Streamlit app (writes, approvals, kill switch)</StreamlitLink>
      </div>
    </div>
  );
}
