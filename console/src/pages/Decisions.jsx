// ND-2 Decisions page -- renders /api/v1/decisions: the decision funnel
// (candidates -> proposed/watch -> rejected/blocked -> filled) + the trade
// ledger. Mirrors streamlit_app.tab_candidate_flow()'s label-summary/
// proposed/watch/rejected/blocked sections and tab_open_trades()/
// tab_closed_trades(). Every candidate/trade field is the raw journal
// column, unreshaped; the only formatting done client-side is
// decisions.js:formatHindsight() (mirrors _hindsight_cell() exactly) and
// buildDecisionFunnelStages() (a pure aggregation of already-shown counts).
//
// ND-6: the design ruling's Funnel component (§3.4/§5) replaces the old
// plain by_label_decision table as this view's hero -- "understand, not
// operate": dense but scannable, an attrition VISUAL instead of a count
// table. Zero API/logic change.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { getDecisions } from '../api.js';
import { Block, DataTable, Badge } from '../components/ui.jsx';
import { Funnel } from '../components/Funnel.jsx';
import { StatFooter } from '../components/StatFooter.jsx';
import { describeUnreachable, formatClockUTC, formatR } from '../format.js';
import { buildDecisionFunnelStages, formatHindsight } from '../decisions.js';

const POLL_MS = 15000;

const CANDIDATE_COLUMNS = [
  { key: 'symbol', label: 'symbol' },
  { key: 'primary_label', label: 'label' },
  { key: 'label_decision', label: 'decision' },
  { key: 'label_confidence', label: 'confidence', numeric: true },
  { key: 'interest_score', label: 'interest', numeric: true },
  { key: 'interest_rank', label: 'rank', numeric: true },
  { key: 'catalyst_status', label: 'catalyst' },
  { key: 'sentiment_label', label: 'sentiment' },
  { key: 'status', label: 'status' },
  { key: 'shortlist_reason', label: 'reason' },
];

const REJECTED_COLUMNS = [
  { key: 'symbol', label: 'symbol' },
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
              <Block title="Proposed candidates">
                <DataTable columns={CANDIDATE_COLUMNS} rows={data.proposed} emptyText="No proposed candidates." />
              </Block>
            </div>
            <div className="col-6">
              <Block title="Watch candidates">
                <DataTable columns={CANDIDATE_COLUMNS} rows={data.watch} emptyText="No watch candidates." />
              </Block>
            </div>

            <div className="col-6">
              <Block title="Rejected candidates">
                <DataTable columns={REJECTED_COLUMNS} rows={data.rejected} emptyText="None." />
              </Block>
            </div>
            <div className="col-6">
              <Block title="Blocked by gate">
                <DataTable columns={BLOCKED_COLUMNS} rows={data.blocked} emptyText="None." />
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
