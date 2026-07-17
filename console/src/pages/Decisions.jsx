// ND-2 Decisions page -- renders /api/v1/decisions: the decision funnel
// (candidates -> proposed/watch -> rejected/blocked -> filled) + the trade
// ledger. Mirrors streamlit_app.tab_candidate_flow()'s label-summary/
// proposed/watch/rejected/blocked sections and tab_open_trades()/
// tab_closed_trades(). Every candidate/trade field is the raw journal
// column, unreshaped; client-side logic is display formatting only
// (decisions.js: formatHindsight/formatNarrative/universeOf, each mirroring
// or reading a raw journal field) and view state (collapse/expand, the
// shadow-row visibility toggle -- counts always shown, nothing silently
// dropped).
//
// 2026-07-17 operator redesign ruling ("free play ... for this tab to be
// neater"): (1) every candidate row carries a core/shadow universe badge;
// (2) long lists collapse to the first rows with an explicit "show all (N)"
// control; (3) the narrative column shows the polarity LLM's verdict
// (polarity_label), never the legacy always-'unknown' sentiment_label hint --
// shown alongside a separate "last30 hint" column carrying the raw
// sentiment_label as-is (operator request, same day: show both, since they
// are different signals -- one is the enricher's un-classified per-cluster
// hint, the other the LLM's actual verdict); (4) the rejected list hides
// shadow-universe screen rejects by default behind a labelled toggle,
// because 200 shadow rows were burying the ~20 live rejects the operator
// actually needs to read.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { getDecisions } from '../api.js';
import { Block, DataTable, Badge } from '../components/ui.jsx';
import { Funnel } from '../components/Funnel.jsx';
import { StatFooter } from '../components/StatFooter.jsx';
import { describeUnreachable, formatClockUTC, formatR } from '../format.js';
import {
  buildDecisionFunnelStages, formatHindsight, formatNarrative, formatSentimentHint, universeOf,
} from '../decisions.js';

const POLL_MS = 15000;
const COLLAPSED_ROWS = 8;

function UniverseBadge({ row }) {
  const u = universeOf(row);
  if (u === 'shadow') return <Badge tone="warn" caps>shadow</Badge>;
  return <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>core</span>;
}

// Trimmed from the original 10 columns (rank/status/decision dropped: rank
// duplicates interest, status+decision are implied by which section the row
// sits in) -- "dense but scannable".
const CANDIDATE_COLUMNS = [
  { key: 'symbol', label: 'symbol' },
  { key: 'universe', label: 'universe', render: (r) => <UniverseBadge row={r} /> },
  { key: 'primary_label', label: 'label' },
  { key: 'label_confidence', label: 'confidence', numeric: true },
  { key: 'interest_score', label: 'interest', numeric: true },
  { key: 'catalyst_status', label: 'catalyst' },
  { key: 'narrative', label: 'polarity', render: (r) => formatNarrative(r) },
  { key: 'sentiment_label', label: 'last30 hint', render: (r) => formatSentimentHint(r) },
  { key: 'shortlist_reason', label: 'reason' },
];

const REJECTED_COLUMNS = [
  { key: 'symbol', label: 'symbol' },
  { key: 'universe', label: 'universe', render: (r) => <UniverseBadge row={r} /> },
  { key: 'stage', label: 'stage' },
  { key: 'reason_code', label: 'reason code' },
  { key: 'reason_detail', label: 'reason detail' },
  { key: 'hindsight', label: 'hindsight', numeric: true, render: (r) => formatHindsight(r.hindsight_raw) },
];

const BLOCKED_COLUMNS = [
  { key: 'symbol', label: 'symbol' },
  { key: 'proposal_id', label: 'proposal_id' },
  { key: 'trade_id', label: 'trade_id' },
  { key: 'status', label: 'status' },
  { key: 'hindsight', label: 'hindsight', numeric: true, render: (r) => formatHindsight(r.hindsight_raw) },
];

const OPEN_TRADE_COLUMNS = [
  { key: 'symbol', label: 'symbol' },
  { key: 'direction', label: 'dir' },
  { key: 'qty', label: 'qty', numeric: true },
  { key: 'avg_entry_price', label: 'entry', numeric: true },
  { key: 'stop_price', label: 'stop', numeric: true },
  { key: 'target_price', label: 'target', numeric: true },
  { key: 'current_price', label: 'current', numeric: true },
  { key: 'status', label: 'status' },
  { key: 'opened_at', label: 'opened' },
];

const CLOSED_TRADE_COLUMNS = [
  { key: 'symbol', label: 'symbol' },
  { key: 'direction', label: 'dir' },
  { key: 'realized_r', label: 'R', numeric: true, render: (r) => formatR(r.realized_r) },
  { key: 'classification', label: 'classification' },
  { key: 'created_at_utc', label: 'closed (UTC)' },
];

// DataTable wrapper that renders only the first `initial` rows until the
// operator expands it. The full count is ALWAYS visible in the control --
// collapsing is a view choice, never silent truncation.
function CollapsedTable({ columns, rows, emptyText, initial = COLLAPSED_ROWS }) {
  const [expanded, setExpanded] = useState(false);
  const all = rows ?? [];
  const visible = expanded ? all : all.slice(0, initial);
  return (
    <>
      <DataTable columns={columns} rows={visible} emptyText={emptyText} />
      {all.length > initial && (
        <button
          type="button"
          className="linklike"
          onClick={() => setExpanded((e) => !e)}
          style={{
            background: 'none', border: 'none', padding: '6px 0 0', cursor: 'pointer',
            fontSize: 12, color: 'var(--primary)',
          }}
        >
          {expanded ? '▴ show fewer' : `▾ show all ${all.length}`}
        </button>
      )}
    </>
  );
}

function GateFunnel({ labelSummary }) {
  const stages = buildDecisionFunnelStages(labelSummary.by_label_decision);
  return (
    <Block title="Gate funnel" reveal>
      {stages.length === 0 ? (
        <div style={{ fontSize: 13, color: 'var(--text-dim)' }}>No labels yet — run an interest scan.</div>
      ) : (
        <Funnel stages={stages} />
      )}
      <div className="label-caps" style={{ margin: '16px 0 6px' }}>by primary label</div>
      <DataTable
        columns={[{ key: 'label', label: 'label' }, { key: 'n', label: 'n', numeric: true }]}
        rows={labelSummary.by_primary_label}
        emptyText="No labels yet — run an interest scan."
      />
    </Block>
  );
}

// Rejected candidates: live-book rows shown by default; shadow-universe
// screen rejects (stage='shadow_scan' -- research-capture volume, hundreds
// of rows) sit behind a labelled toggle with their count always visible.
function RejectedBlock({ rejected }) {
  const [showShadow, setShowShadow] = useState(false);
  const all = rejected ?? [];
  const live = all.filter((r) => universeOf(r) === 'core');
  const shadowCount = all.length - live.length;
  const rows = showShadow ? all : live;
  return (
    <Block title={`Rejected candidates (${live.length} live)`}>
      <CollapsedTable columns={REJECTED_COLUMNS} rows={rows} emptyText="None." />
      {shadowCount > 0 && (
        <label style={{ display: 'block', marginTop: 8, fontSize: 11, color: 'var(--text-dim)', cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={showShadow}
            onChange={(e) => setShowShadow(e.target.checked)}
            style={{ marginRight: 6, verticalAlign: 'middle' }}
          />
          include {shadowCount} shadow-universe screen reject(s) — research capture, not live decisions
        </label>
      )}
    </Block>
  );
}

export default function Decisions() {
  const [data, setData] = useState(null);
  const [unreachable, setUnreachable] = useState(false);
  const [lastGoodAsOf, setLastGoodAsOf] = useState(null);
  const mountedRef = useRef(true);

  const poll = useCallback(async () => {
    try {
      const r = await getDecisions();
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

      {!data ? (
        <div className="label-caps">loading decisions…</div>
      ) : (
        <>
          <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 12 }}>as of {formatClockUTC(data.as_of)}</div>

          <div className="grid reveal-stagger">
            <div className="col-12"><GateFunnel labelSummary={data.label_summary} /></div>

            <div className="col-6">
              <Block title={`Proposed candidates (${(data.proposed ?? []).length})`}>
                <CollapsedTable columns={CANDIDATE_COLUMNS} rows={data.proposed} emptyText="No proposed candidates." />
              </Block>
            </div>
            <div className="col-6">
              <Block title={`Watch candidates (${(data.watch ?? []).length})`}>
                <CollapsedTable columns={CANDIDATE_COLUMNS} rows={data.watch} emptyText="No watch candidates." />
              </Block>
            </div>

            <div className="col-6">
              <RejectedBlock rejected={data.rejected} />
            </div>
            <div className="col-6">
              <Block title="Blocked by gate">
                <CollapsedTable columns={BLOCKED_COLUMNS} rows={data.blocked} emptyText="None." />
              </Block>
            </div>

            <div className="col-12">
              <Block title="Open trades (paper, simulated)">
                <DataTable columns={OPEN_TRADE_COLUMNS} rows={data.open_trades} emptyText="No open positions." />
              </Block>
            </div>
            <div className="col-12">
              <Block title="Closed trades (paper) — net of modelled costs">
                <ClosedTradeMetrics m={data.closed_trade_metrics} />
                <DataTable columns={CLOSED_TRADE_COLUMNS} rows={data.closed_trades} emptyText="No closed trades yet." />
              </Block>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function ClosedTradeMetrics({ m }) {
  return (
    <>
      <StatFooter
        stats={[
          { label: 'net P&L', value: m.net_pnl },
          { label: 'win rate', value: m.win_rate ?? 'n/a' },
          { label: 'expectancy', value: m.expectancy ?? 'n/a' },
          { label: 'profit factor', value: m.profit_factor ?? 'n/a' },
        ]}
      />
      {m.small_sample && (
        <div style={{ marginTop: 8 }}>
          <Badge tone="warn">{m.note}</Badge>
        </div>
      )}
    </>
  );
}
