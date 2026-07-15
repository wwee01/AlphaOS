// ND-2 Positions page -- renders /api/v1/positions (ND-1 endpoint,
// unchanged). Mirrors streamlit_app.tab_positions_health() field-for-field:
// same verdict icon, same R-ladder-or-plain-text fallback, same EXIT_REVIEW
// human-decision warning, same protection/freshness/trading-days caption.
// This component computes nothing business-critical -- assess_positions()
// already decided every verdict/R value server-side; the R-ladder's pixel
// placement (positions.js:computeRLadder()) is presentation math only.
//
// ND-6: the R-ladder is the design ruling's own "single most characteristic
// visual in the whole product" (§2) -- given full width as each card's
// centerpiece, a 2-up InstrumentBlock grid on desktop (stacked on mobile),
// and a total-open-R StatTile hero above the list. Zero data/verdict logic
// changed.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { getPositions } from '../api.js';
import { Block, Badge, badgeTone } from '../components/ui.jsx';
import { ProgressBar } from '../components/ProgressBar.jsx';
import { StatFooter } from '../components/StatFooter.jsx';
import { StatTile } from '../components/StatTile.jsx';
import { IconArrowDownRight, IconArrowUpRight, IconShield, IconWarningTriangle } from '../components/icons.jsx';
import { describeUnreachable, formatClockUTC, formatR } from '../format.js';
import { computeRLadder, verdictIcon } from '../positions.js';

const POLL_MS = 10000;

// ND-7 (design ruling §4.4): the hero numeral is tone-colored by sign --
// green positive / red negative / ink neutral (unmeasurable). Mirrors
// Tonight.jsx's own rTone() helper; kept local since these are two
// independent page files, not a shared logic module.
function rTone(totalR) {
  if (totalR === null || totalR === undefined) return 'neutral';
  if (totalR > 0) return 'success';
  if (totalR < 0) return 'danger';
  return 'neutral';
}

// Verdict -> the tone-bearing marker color used for the R-ladder's ticks
// (stop always reads crit/red, target always reads good/green -- that part
// is fixed regardless of verdict, ported from the approved mockup's
// `.tick.stop`/`.tick.tgt`). Presentation-only lookup, mirrors Badge.jsx's
// tone table. ND-7: target moves from brand cyan to --good (green) -- the
// ruling's semantic migration reserves cyan for brand/active/current-marker
// only; a target tick is a "good outcome" marker, not a brand marker.
const MARK_COLOR = { stop: 'var(--red)', target: 'var(--good)', entry: 'var(--hairline-lit)' };

function RLadder({ stopR, entryR, currentR, targetR, verdict }) {
  const ladder = computeRLadder({ stopR, entryR, currentR, targetR });
  if (!ladder) {
    return <div className="pbar-unavailable">R-ladder unavailable (missing stop, target, or current price)</div>;
  }
  // ND-7 (design ruling §4.3): the ladder's FILL gradient follows the
  // current-vs-entry SIGN (crit->warn->good when above entry, crit-toned
  // when below) -- an axis orthogonal to `tone` (which still drives the
  // marker/tick colors via badgeTone(verdict), unchanged below). Ported
  // from the approved mockup's `.fill-pos`/`.fill-neg` exactly.
  // computeRLadder()'s own math is untouched -- this only picks a CSS class.
  const isPositive = currentR >= entryR;
  return (
    <ProgressBar
      withLabels
      tone={badgeTone(verdict)}
      fillClassName={isPositive ? 'pbar-fill-pos' : 'pbar-fill-neg'}
      pct={ladder.current.pct}
      height={14}
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
      reveal
      style={{ height: '100%', borderColor: p.verdict === 'EXIT_REVIEW' ? 'var(--red)' : 'var(--border)' }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', fontSize: 15, fontWeight: 600, marginBottom: 10 }}>
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

      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, marginTop: 10 }}>
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
            // audit-fixup (correctness LOW-2): the shield is tinted by
            // reusing Badge's own tone->color mapping rather than a
            // duplicate lookup, so protection never reads reassuring next
            // to a bad status.
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

  const measurable = (positions ?? []).filter((p) => p.current_r !== null && p.current_r !== undefined);
  const totalR = measurable.length ? measurable.reduce((sum, p) => sum + p.current_r, 0) : null;

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
        <>
          <div className="grid reveal-stagger" style={{ marginBottom: 4 }}>
            <div className="col-12">
              <Block>
                <StatTile
                  label="total open R"
                  value={formatR(totalR)}
                  tone={rTone(totalR)}
                  context={`${positions.length} open position(s)${measurable.length !== positions.length ? ` · ${positions.length - measurable.length} unmeasurable` : ''}`}
                />
              </Block>
            </div>
          </div>

          <div className="grid reveal-stagger">
            {positions.map((p) => (
              <div className="col-6" key={p.position_id}>
                <PositionCard p={p} />
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
