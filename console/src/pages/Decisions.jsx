// ND-2 Decisions page -- renders /api/v1/decisions: the decision funnel
// (candidates -> proposed/watch -> rejected/blocked -> filled) + the trade
// ledger. Mirrors streamlit_app.tab_candidate_flow()'s label-summary/
// proposed/watch/rejected/blocked sections and tab_open_trades()/
// tab_closed_trades(). Every candidate/trade field is the raw journal
// column, unreshaped; client-side logic is display formatting only
// (decisions.js: formatHindsight/formatNarrative/formatSentimentHint) and
// view state (collapse/expand, which symbol's history is expanded).
//
// 2026-07-17 operator redesign, TWO passes same day:
//   pass 1: every candidate row carried a core/shadow universe badge, and
//     the rejected list hid shadow-universe screen rejects behind a toggle.
//   pass 2 (this one, Fable 5 architecture consult): shadow data moved to
//     its OWN tab (pages/Research.jsx) -- the server now hard-filters
//     proposed/watch/rejected to core-only (journal_store.py), so the badge
//     and toggle are dead weight and removed. Separately, watch/rejected
//     were an append-only per-scan log (134 rows for 21 symbols read as
//     noise, the operator's own complaint) -- the server now returns ONE
//     row per symbol (the latest) with `occurrence_count`/
//     `first_seen_at_utc`/`history`; this page renders a "seen ×N" control
//     that expands that symbol's history inline, one symbol at a time.
import React, {
  useCallback, useEffect, useRef, useState,
} from 'react';
import { getDecisions } from '../api.js';
import { Block, DataTable, Badge } from '../components/ui.jsx';
import { CollapsedTable } from '../components/CollapsedTable.jsx';
import { Funnel } from '../components/Funnel.jsx';
import { StatFooter } from '../components/StatFooter.jsx';
import { describeUnreachable, formatClockUTC, formatR } from '../format.js';
import {
  buildDecisionFunnelStages, formatHindsight, formatNarrative,
} from '../decisions.js';

const POLL_MS = 15000;

// "seen ×N" -- a clickable control only when there IS history to show (a
// symbol seen once has nothing to expand). `onToggle` receives the row's
// symbol; the caller owns which symbol (if any) is currently expanded.
function SeenControl({ row, expanded, onToggle }) {
  const n = row.occurrence_count ?? 1;
  if (n <= 1) return <span className="num" style={{ color: 'var(--text-dim)' }}>1</span>;
  return (
    <button
      type="button"
      className="linklike"
      onClick={() => onToggle(row.symbol)}
      style={{
        background: 'none', border: 'none', padding: 0, cursor: 'pointer',
        fontSize: 12, color: 'var(--primary)', fontFamily: 'inherit',
      }}
    >
      {expanded ? '▴' : '▾'} ×{n}
    </button>
  );
}

const HISTORY_COLUMNS_BY_KIND = {
  candidate: [
    { key: 'created_at_utc', label: 'seen (UTC)', render: (r) => formatClockUTC(r.created_at_utc) },
    { key: 'primary_label', label: 'label' },
    { key: 'label_confidence', label: 'confidence', numeric: true },
    { key: 'interest_score', label: 'interest', numeric: true },
  ],
  rejected: [
    { key: 'created_at_utc', label: 'seen (UTC)', render: (r) => formatClockUTC(r.created_at_utc) },
    { key: 'reason_code', label: 'reason code' },
    { key: 'reason_detail', label: 'reason detail' },
  ],
};

// Renders, directly beneath a watch/rejected table, the expanded symbol's
// older sightings -- one row per prior scan, newest-first, exactly what
// `occurrence_count` was counting. Collapses to nothing when no symbol in
// this table is expanded.
function HistoryPanel({ rows, expandedSymbol, kind }) {
  if (!expandedSymbol) return null;
  const row = (rows ?? []).find((r) => r.symbol === expandedSymbol);
  const history = row?.history ?? [];
  return (
    <div style={{ marginTop: 10, paddingTop: 10, borderTop: '1px solid var(--border)' }}>
      <div className="label-caps" style={{ marginBottom: 6 }}>
        history — {expandedSymbol} ({row?.occurrence_count ?? history.length} sighting(s))
      </div>
      <DataTable columns={HISTORY_COLUMNS_BY_KIND[kind]} rows={history} emptyText="No earlier sightings." />
    </div>
  );
}

// `deduped`: proposed_candidates() is NOT server-deduped (it's already a
// self-pruning "currently pending" list -- see journal_store.py's own
// docstring on why), so its rows never carry occurrence_count/history. A
// "seen" column there would always render a bare, meaningless "1" -- so the
// column itself is omitted rather than shown-but-always-1 (Audit LOW,
// 2026-07-17).
//
// 2026-07-17 (operator report): dropped the "last30 hint" (sentiment_label)
// column added earlier the same day -- it read "unknown" for every row, not
// as a partial gap but permanently: the live CLI provider hardcodes
// sentiment_hint=None unconditionally (last30days_provider.py, only the
// mock provider ever sets it), so this column carries zero information
// under the operator's actual live config and never will. "polarity" below
// (polarity_label, a separate model call) is the real signal and stays.
function buildCandidateColumns(expandedSymbol, onToggle, deduped = true) {
  const cols = [{ key: 'symbol', label: 'symbol' }];
  if (deduped) {
    cols.push({ key: 'seen', label: 'seen', numeric: true, render: (r) => <SeenControl row={r} expanded={r.symbol === expandedSymbol} onToggle={onToggle} /> });
  }
  return [
    ...cols,
    { key: 'primary_label', label: 'label' },
    { key: 'label_confidence', label: 'confidence', numeric: true },
    { key: 'interest_score', label: 'interest', numeric: true },
    { key: 'catalyst_status', label: 'catalyst' },
    { key: 'narrative', label: 'polarity', render: (r) => formatNarrative(r) },
    { key: 'shortlist_reason', label: 'reason' },
  ];
}

function buildRejectedColumns(expandedSymbol, onToggle) {
  return [
    { key: 'symbol', label: 'symbol' },
    { key: 'seen', label: 'seen', numeric: true, render: (r) => <SeenControl row={r} expanded={r.symbol === expandedSymbol} onToggle={onToggle} /> },
    { key: 'reason_code', label: 'reason code' },
    { key: 'reason_detail', label: 'reason detail' },
    { key: 'hindsight', label: 'hindsight', numeric: true, render: (r) => formatHindsight(r.hindsight_raw) },
  ];
}

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

// One row per symbol now (server-side dedup) -- "N candidates" in the title
// is the true distinct-symbol count, not a per-scan event count.
function WatchBlock({ watch }) {
  const [expandedSymbol, setExpandedSymbol] = useState(null);
  const rows = watch ?? [];
  const onToggle = useCallback(
    (symbol) => setExpandedSymbol((cur) => (cur === symbol ? null : symbol)),
    [],
  );
  return (
    <Block title={`Watch candidates (${rows.length})`}>
      <CollapsedTable columns={buildCandidateColumns(expandedSymbol, onToggle)} rows={rows} emptyText="No watch candidates." />
      <HistoryPanel rows={rows} expandedSymbol={expandedSymbol} kind="candidate" />
    </Block>
  );
}

function RejectedBlock({ rejected }) {
  const [expandedSymbol, setExpandedSymbol] = useState(null);
  const rows = rejected ?? [];
  const onToggle = useCallback(
    (symbol) => setExpandedSymbol((cur) => (cur === symbol ? null : symbol)),
    [],
  );
  return (
    <Block title={`Rejected candidates (${rows.length})`}>
      <CollapsedTable columns={buildRejectedColumns(expandedSymbol, onToggle)} rows={rows} emptyText="None." />
      <HistoryPanel rows={rows} expandedSymbol={expandedSymbol} kind="rejected" />
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
                <CollapsedTable columns={buildCandidateColumns(null, () => {}, false)} rows={data.proposed} emptyText="No proposed candidates." />
              </Block>
            </div>
            <div className="col-6">
              <WatchBlock watch={data.watch} />
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
