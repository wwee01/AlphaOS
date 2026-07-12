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
import { Block, Badge, badgeTone } from '../components/ui.jsx';
import { ProgressBar } from '../components/ProgressBar.jsx';
import { StatFooter } from '../components/StatFooter.jsx';
import { IconArrowDownRight, IconArrowUpRight, IconShield, IconWarningTriangle } from '../components/icons.jsx';
import { describeUnreachable, formatClockUTC, formatR } from '../format.js';
import { computeRLadder, verdictIcon } from '../positions.js';

const POLL_MS = 10000;

// Verdict -> the tone-bearing marker color used for the R-ladder's ticks
// (stop always reads danger, target always reads primary/success -- that
// part is fixed regardless of verdict, same as ND-2's inline literals
// below). Presentation-only lookup, mirrors Badge.jsx's tone table.
const MARK_COLOR = { stop: 'var(--red)', target: 'var(--primary)', entry: 'var(--text-dim)' };

function RLadder({ stopR, entryR, currentR, targetR, verdict }) {
  const ladder = computeRLadder({ stopR, entryR, currentR, targetR });
  if (!ladder) {
    return <div className="pbar-unavailable">R-ladder unavailable (missing stop, target, or current price)</div>;
  }
  return (
    <ProgressBar
      withLabels
      tone={badgeTone(verdict)}
      pct={ladder.current.pct}
      marks={ladder.ticks.map((t) => ({
        name: t.name, pct: t.pct, value: formatR(t.value), label: t.name, color: MARK_COLOR[t.name],
      }))}
      marker={{ pct: ladder.current.pct, label: formatR(ladder.current.value), title: `now ${formatR(ladder.current.value)}` }}
    />
  );
}

function PositionCard({ p }) {
  const hasStopTarget = p.current_r !== null && p.current_r !== undefined
    && p.distance_to_stop_r !== null && p.distance_to_stop_r !== undefined
    && p.distance_to_target_r !== null && p.distance_to_target_r !== undefined;
  const stopR = hasStopTarget ? p.current_r - p.distance_to_stop_r : null;
  const targetR = hasStopTarget ? p.current_r + p.distance_to_target_r : null;
  const DirIcon = String(p.direction).toUpperCase() === 'SHORT' ? IconArrowDownRight : IconArrowUpRight;

  return (
    <Block
      title={null}
      style={{ marginBottom: 10, borderColor: p.verdict === 'EXIT_REVIEW' ? 'var(--red)' : 'var(--border)' }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', fontSize: 14, fontWeight: 600, marginBottom: 6 }}>
        <span>{verdictIcon(p.verdict)} {p.symbol}</span>
        <Badge tone={badgeTone(p.direction)} caps><DirIcon size={12} />{p.direction}</Badge>
        <span style={{ fontSize: 12, color: 'var(--text-dim)', fontWeight: 400 }}>verdict</span>
        <Badge tone={badgeTone(p.verdict)} caps>{p.verdict}</Badge>
      </div>

      {p.current_r === null || p.current_r === undefined ? (
        <div style={{ fontSize: 13, color: 'var(--text-dim)' }}>R: unavailable (no live price, or a degenerate risk basis)</div>
      ) : hasStopTarget ? (
        <>
          <RLadder stopR={stopR} entryR={0} currentR={p.current_r} targetR={targetR} verdict={p.verdict} />
          <StatFooter
            stats={[
              { label: 'to stop', value: `${p.distance_to_stop_r}R` },
              { label: 'to target', value: `${p.distance_to_target_r}R` },
            ]}
          />
        </>
      ) : (
        <div className="num" style={{ fontSize: 13 }}>
          now {formatR(p.current_r)} (distance to stop: {p.distance_to_stop_r ?? 'n/a'}, to target: {p.distance_to_target_r ?? 'n/a'})
        </div>
      )}

      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, marginTop: 8 }}>
        thesis: <Badge tone={badgeTone(p.thesis_status)} caps>{p.thesis_status}</Badge>
      </div>

      {p.verdict === 'EXIT_REVIEW' && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 8, fontSize: 12, color: 'var(--red)' }}>
          <IconWarningTriangle size={13} /> Human decision required — AlphaOS does not auto-exit on health verdicts.
        </div>
      )}

      <StatFooter
        stats={[
          {
            // audit-fixup (correctness LOW-2): the shield previously
            // rendered neutral-colored for every status including
            // UNPROTECTED/DEGRADED, which could read as reassuring next to
            // a bad status. Tinted by reusing Badge's own tone->color
            // mapping (icons.jsx's stroke="currentColor" means Badge's
            // `color` CSS tints both the icon and the text together,
            // never disagreeing) rather than duplicating that lookup table
            // here -- chrome (border/background/padding) stripped via
            // inline style so it still reads as plain footer text, not a
            // second pill nested inside the stat row.
            label: 'protection',
            value: (
              <Badge
                tone={badgeTone(p.protection_status)}
                style={{
                  display: 'inline-flex', alignItems: 'center', gap: 4,
                  border: 'none', background: 'none', padding: 0,
                }}
              >
                <IconShield size={12} />{p.protection_status}
              </Badge>
            ),
          },
          { label: 'freshness', value: p.freshness_status },
          { label: 'trading days held', value: `${p.trading_days_held}/${p.max_holding_days ?? 'n/a'}` },
          { label: 'calendar age', value: `${p.days_held ?? 'n/a'}d` },
          { label: 'earnings in hold window', value: p.earnings_within_hold_window ? 'yes' : 'no' },
        ]}
      />
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
