// Research tab -- renders /api/v1/research: the shadow-tier (EXP-0/EXP-1)
// instrument's OWN surface, split out of Decisions 2026-07-17 specifically
// so shadow measurement data never shares a page with live trade decisions
// (Fable 5 strategic architecture consult, same day -- see the /research
// route's own docstring in alphaos/api/routes.py for the full reasoning).
//
// Every Block here carries tone="shadow" (same convention Learning.jsx
// established for its own measurement-only/zero-decision-surface data --
// the dim indigo border/tint + "shadow" tag, so nothing on this page is
// ever mistakable for a live control). Headline is instrument HEALTH
// (capture days, coverage, provisional-constants warning); raw shadow rows
// are demoted to one small table at the bottom -- this page answers "is the
// instrument healthy and how close to audit-ready", never "what should the
// AI conclude" (that's scripts/shadow_saturation_audit.py's job, run by
// hand -- this page reports readiness, never percentiles/conclusions).
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { getResearch } from '../api.js';
import { Block, DataTable, Badge } from '../components/ui.jsx';
import { CollapsedTable } from '../components/CollapsedTable.jsx';
import { StatFooter } from '../components/StatFooter.jsx';
import { StatTile } from '../components/StatTile.jsx';
import { ProgressBar } from '../components/ProgressBar.jsx';
import { describeUnreachable, formatClockUTC } from '../format.js';
import { auditProgressPct, describeAuditReadiness } from '../research.js';

const POLL_MS = 15000;

// Local explainer box -- same visual pattern Learning.jsx's HowToRead
// established for "how do I read this table" copy.
function HowToRead({ children }) {
  return (
    <div
      className="prose"
      style={{
        fontSize: 12, color: 'var(--text-dim)', marginBottom: 12, padding: '8px 10px',
        background: 'var(--surface-low)', border: '1px solid var(--border)', borderRadius: 4, lineHeight: 1.5,
      }}
    >
      {children}
    </div>
  );
}

const CAPTURE_EXPLAINER = (
  <>
    <b>What this is:</b> a wider, unproven universe (500 small/mid-cap symbols, vs. the 38-name
    live book) that gets scanned every day purely to collect evidence — it can never trade, and
    the AI never even forms an opinion on it yet. The point: once enough real trading days have
    accumulated, a saturation audit turns today's guessed thresholds into ones measured from
    actual data, so any future decision to widen live trading into this band is evidence-based,
    not a guess.
  </>
);

function CaptureHealth({ capture, constants, shadowLabellingEnabled }) {
  const pct = auditProgressPct(capture.capture_days, capture.audit_min_trading_days);
  return (
    <Block title="Shadow instrument — capture health" tone="shadow" style={{ height: '100%' }}>
      <HowToRead>{CAPTURE_EXPLAINER}</HowToRead>
      <StatTile
        label="capture days"
        value={capture.capture_days ?? 0}
        tone="shadow"
        context={`of ${capture.audit_min_trading_days ?? '?'} trading days before the saturation audit is viable`
          + (capture.first_market_date ? ` · capture since ${capture.first_market_date}` : '')}
      />
      <div style={{ marginTop: 10, marginBottom: 10 }}>
        {/* Audit LOW (2026-07-17): ProgressBar's tone map aliases "success"
            to the same track class as "primary" (no distinct green state
            exists yet) -- always pass "primary" rather than implying a color
            change that won't render. */}
        <ProgressBar pct={pct} tone="primary" />
      </div>
      <div className="prose" style={{ fontSize: 12, marginBottom: 10 }}>
        {describeAuditReadiness(capture)}
      </div>

      {constants.provisional ? (
        <Badge tone={shadowLabellingEnabled ? 'danger' : 'warn'} caps>
          {shadowLabellingEnabled
            ? 'labelling armed while constants are still provisional'
            : `${constants.interest_score_version} — provisional, not data-derived`}
        </Badge>
      ) : (
        <Badge tone="success" caps>{constants.interest_score_version} — data-derived</Badge>
      )}

      <div style={{ marginTop: 12 }} />
      <StatFooter
        stats={[
          { label: 'captured rows', value: capture.shadow_candidate_rows_total },
          { label: 'universe size', value: capture.universe_size_latest },
          { label: 'shadow labels', value: capture.shadow_labels_total },
          { label: 'screen rejects', value: capture.screen_rejects_total },
        ]}
      />
    </Block>
  );
}

const COVERAGE_COLUMNS = [
  { key: 'market_date', label: 'market date' },
  { key: 'symbols', label: 'symbols', numeric: true },
  { key: 'usable', label: 'usable', numeric: true },
  { key: 'stale', label: 'stale', numeric: true },
  { key: 'usable_pct', label: 'usable %', numeric: true },
  { key: 'candidates_found', label: 'candidates found', numeric: true },
];

function CoverageBlock({ coverageByDay }) {
  return (
    <Block title="Daily capture coverage" tone="shadow" style={{ height: '100%' }}>
      <DataTable columns={COVERAGE_COLUMNS} rows={coverageByDay} emptyText="No capture yet." />
      <div className="prose" style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 8 }}>
        "usable" = passed the freshness guard at scan time; a low rate means the day's snapshot
        was stale for many symbols, not that they were uninteresting.
      </div>
    </Block>
  );
}

const REASON_COLUMNS = [
  { key: 'reason_code', label: 'reason code' },
  { key: 'n', label: 'count', numeric: true },
];

const VERSION_COLUMNS = [
  { key: 'instrument_version', label: 'instrument version' },
  { key: 'rows', label: 'rows', numeric: true },
  { key: 'sgt_days', label: 'distinct calendar days', numeric: true },
];

function ScreenRejectsBlock({ byReason, byVersion }) {
  return (
    <Block title="Screen rejects — why symbols didn't capture" tone="shadow" style={{ height: '100%' }}>
      <DataTable columns={REASON_COLUMNS} rows={byReason} emptyText="None." />
      <div className="label-caps" style={{ margin: '14px 0 6px' }}>captured rows by instrument version</div>
      <DataTable columns={VERSION_COLUMNS} rows={byVersion} emptyText="None." />
      <div className="prose" style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 8 }}>
        pre-instr1 rows (if any) are known-biased and must never be pooled with instr1 in analysis.
        "distinct calendar days" here counts SGT calendar days a row was captured on — a different,
        looser count than the hero's "capture days" above (TRADING days in universe_days, the number
        that actually gates the saturation audit).
      </div>
    </Block>
  );
}

const RECENT_COLUMNS = [
  { key: 'symbol', label: 'symbol' },
  { key: 'interest_score', label: 'interest', numeric: true },
  { key: 'interest_rank', label: 'rank', numeric: true },
  { key: 'selection_arm', label: 'selection arm' },
  { key: 'scan_window', label: 'scan window' },
  { key: 'created_at_utc', label: 'captured (UTC)' },
];

function RecentCapturesBlock({ recentCaptures }) {
  return (
    <Block title="Recent shadow captures (raw)" tone="shadow">
      <div className="prose" style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8 }}>
        Detail only — none of these symbols can trade or be proposed. This is the ONLY place a
        shadow symbol is named on this page.
      </div>
      <CollapsedTable columns={RECENT_COLUMNS} rows={recentCaptures} emptyText="No shadow captures yet." />
    </Block>
  );
}

function ConfigRow({ label, value, warn }) {
  return (
    <div style={{ display: 'flex', gap: 10, alignItems: 'baseline', padding: '3px 0' }}>
      <span className="label-caps" style={{ flex: '0 0 auto', width: 170 }}>{label}</span>
      <span className="num" style={{ fontSize: 13 }}>{value}</span>
      {warn && <Badge tone="warn" caps>provisional</Badge>}
    </div>
  );
}

function ConfigBlock({ universeConfig, constants }) {
  const provisional = constants.provisional;
  const v = constants.values ?? {};
  return (
    <Block title="Universe & constants (config)" tone="shadow">
      <div className="label-caps" style={{ marginBottom: 6 }}>universe screen</div>
      <ConfigRow label="dollar volume (ADV)" value={`$${(universeConfig.min_adv_usd ?? 0) / 1e6}M – $${(universeConfig.max_adv_usd ?? 0) / 1e6}M`} />
      <ConfigRow label="price band" value={`$${universeConfig.min_price ?? '?'} – $${universeConfig.max_price ?? '?'}`} />
      <ConfigRow label="target / max count" value={`${universeConfig.target_count ?? '?'} / ${universeConfig.max_count ?? '?'}`} />

      <div className="label-caps" style={{ margin: '14px 0 6px' }}>
        interest-score constants ({constants.interest_score_version})
      </div>
      <ConfigRow label="change scale" value={v.change_scale} warn={provisional} />
      <ConfigRow label="rel. volume scale" value={v.rel_vol_scale} warn={provisional} />
      <ConfigRow label="day-range min" value={v.day_range_min} warn={provisional} />
      <ConfigRow label="momentum change cap" value={v.momentum_change_cap} warn={provisional} />
      <ConfigRow label="momentum rel-vol cap" value={v.momentum_relvol_cap} warn={provisional} />
    </Block>
  );
}

export default function Research() {
  const [data, setData] = useState(null);
  const [unreachable, setUnreachable] = useState(false);
  const [lastGoodAsOf, setLastGoodAsOf] = useState(null);
  const mountedRef = useRef(true);

  const poll = useCallback(async () => {
    try {
      const r = await getResearch();
      if (!mountedRef.current) return;
      setData(r);
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
      <div
        className="stale-banner"
        style={{ borderColor: 'var(--shadow-tier-border)', background: 'var(--shadow-tier-bg)', color: 'var(--shadow-tier)' }}
      >
        <span className="shadow-tag" style={{ marginRight: 8 }}>shadow</span>
        This whole view is measurement-only. None of these symbols can trade or be proposed —
        this page exists to track when there is enough real data to calibrate the shadow tier
        honestly, not to make any decision by itself.
      </div>

      {!data ? (
        <div className="label-caps">loading research…</div>
      ) : (
        <>
          <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 12 }}>as of {formatClockUTC(data.as_of)}</div>
          <div className="grid reveal-stagger">
            <div className="col-12">
              <CaptureHealth
                capture={data.capture}
                constants={data.constants}
                shadowLabellingEnabled={data.shadow_labelling_enabled}
              />
            </div>
            <div className="col-6"><CoverageBlock coverageByDay={data.capture.coverage_by_day} /></div>
            <div className="col-6">
              <ScreenRejectsBlock
                byReason={data.capture.screen_rejects_by_reason}
                byVersion={data.capture.rows_by_instrument_version}
              />
            </div>
            <div className="col-12"><RecentCapturesBlock recentCaptures={data.recent_captures} /></div>
            <div className="col-12"><ConfigBlock universeConfig={data.universe_config} constants={data.constants} /></div>
          </div>
        </>
      )}
    </div>
  );
}
