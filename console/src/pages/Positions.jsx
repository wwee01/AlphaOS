// ND-2 Positions page -- renders /api/v1/positions (ND-1 endpoint, unchanged;
// this is the first frontend consumer of it beyond Tonight's summary count).
// Mirrors streamlit_app.tab_positions_health() field-for-field: same verdict
// icon, same R-ladder-or-plain-text fallback, same EXIT_REVIEW human-decision
// warning, same protection/freshness/trading-days caption. This component
// computes nothing business-critical -- assess_positions() already decided
// every verdict/R value server-side; the R-ladder's pixel placement
// (positions.js:computeRLadder()) is presentation math only, same category
// as format.js's formatting helpers.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { getPositions } from '../api.js';
import { Block } from '../components/ui.jsx';
import { describeUnreachable, formatClockUTC, formatR } from '../format.js';
import { computeRLadder, verdictIcon } from '../positions.js';

const POLL_MS = 10000;

function RLadder({ stopR, entryR, currentR, targetR }) {
  const ladder = computeRLadder({ stopR, entryR, currentR, targetR });
  if (!ladder) {
    return <div className="r-ladder-unavailable">R-ladder unavailable (missing stop, target, or current price)</div>;
  }
  return (
    <div className="r-ladder-track">
      <div
        className="r-ladder-current"
        style={{ left: `${ladder.current.pct}%` }}
        title={`now ${formatR(ladder.current.value)}`}
      >
        <span className="r-ladder-current-value">{formatR(ladder.current.value)}</span>
      </div>
      {ladder.ticks.map((t) => (
        <div
          key={t.name}
          className="r-ladder-tick"
          style={{
            left: `${t.pct}%`,
            borderLeftColor: t.name === 'stop' ? 'var(--red)' : t.name === 'target' ? 'var(--primary)' : 'var(--text-dim)',
          }}
          title={`${t.name} ${formatR(t.value)}`}
        >
          <span className="r-ladder-tick-label label-caps">{t.name}</span>
          <span className="r-ladder-tick-value">{formatR(t.value)}</span>
        </div>
      ))}
    </div>
  );
}

function PositionCard({ p }) {
  const hasStopTarget = p.current_r !== null && p.current_r !== undefined
    && p.distance_to_stop_r !== null && p.distance_to_stop_r !== undefined
    && p.distance_to_target_r !== null && p.distance_to_target_r !== undefined;
  const stopR = hasStopTarget ? p.current_r - p.distance_to_stop_r : null;
  const targetR = hasStopTarget ? p.current_r + p.distance_to_target_r : null;

  return (
    <Block
      title={null}
      style={{ marginBottom: 10, borderColor: p.verdict === 'EXIT_REVIEW' ? 'var(--red)' : 'var(--border)' }}
    >
      <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 6 }}>
        {verdictIcon(p.verdict)} {p.symbol} · {p.direction} · verdict: <span className="num">{p.verdict}</span>
      </div>

      {p.current_r === null || p.current_r === undefined ? (
        <div style={{ fontSize: 13, color: 'var(--text-dim)' }}>R: unavailable (no live price, or a degenerate risk basis)</div>
      ) : hasStopTarget ? (
        <>
          <RLadder stopR={stopR} entryR={0} currentR={p.current_r} targetR={targetR} />
          <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>
            distance to stop: {p.distance_to_stop_r}R · to target: {p.distance_to_target_r}R
          </div>
        </>
      ) : (
        <div className="num" style={{ fontSize: 13 }}>
          now {formatR(p.current_r)} (distance to stop: {p.distance_to_stop_r ?? 'n/a'}, to target: {p.distance_to_target_r ?? 'n/a'})
        </div>
      )}

      <div style={{ fontSize: 13, marginTop: 8 }}>thesis: <b>{p.thesis_status}</b></div>

      {p.verdict === 'EXIT_REVIEW' && (
        <div style={{ marginTop: 8, fontSize: 12, color: 'var(--red)' }}>
          ⚠ Human decision required — AlphaOS does not auto-exit on health verdicts.
        </div>
      )}

      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 8 }}>
        protection: {p.protection_status} · freshness: {p.freshness_status} · trading days held: {p.trading_days_held}/{p.max_holding_days ?? 'n/a'}{' '}
        (calendar age: {p.days_held ?? 'n/a'}d) · earnings in hold window: {p.earnings_within_hold_window ? 'yes' : 'no'}
      </div>
    </Block>
  );
}

export default function Positions() {
  const [positions, setPositions] = useState(null);
  const [unreachable, setUnreachable] = useState(false);
  const [lastGoodAsOf, setLastGoodAsOf] = useState(null);
  const [asOf, setAsOf] = useState(null);
  const mountedRef = useRef(true);

  const poll = useCallback(async () => {
    try {
      const r = await getPositions();
      if (!mountedRef.current) return;
      setPositions(r.positions ?? []);
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

  return (
    <div className={unreachable ? 'dim' : ''}>
      {unreachableMsg && <div className="stale-banner">{unreachableMsg}</div>}
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 12 }}>as of {formatClockUTC(asOf)}</div>

      {!positions ? (
        <div className="label-caps">loading positions…</div>
      ) : positions.length === 0 ? (
        <Block title="Positions">
          <div style={{ fontSize: 13, color: 'var(--text-dim)' }}>No open positions.</div>
        </Block>
      ) : (
        positions.map((p) => <PositionCard key={p.position_id} p={p} />)
      )}
    </div>
  );
}
