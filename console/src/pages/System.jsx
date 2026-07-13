// ND-2 System & Audit page -- renders /api/v1/system (+ the trade-packet
// drill-down at /api/v1/system/trade-packet). Consolidates the 5 Streamlit
// tabs the plan doc names into ONE view with a simple segmented sub-view
// selector -- plain useState, no router, matching this project's "don't
// over-engineer" house style.
//
// ND-6: adds a Sparkline of scan-batch cadence (candidates_found per batch)
// to the Batches panel -- the one place on this console a real ordered
// series exists (design ruling §3.4/§5). `scan_batches` is returned
// newest-first (same convention as recent_events/the Journal feed
// elsewhere in this app), so the series is reversed to chronological
// (oldest-to-newest, left-to-right) purely for the chart; the table below
// it keeps the API's own newest-first row order unchanged.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { getSystem, getTradePacket } from '../api.js';
import { Badge, Block, DataTable } from '../components/ui.jsx';
import { Sparkline } from '../components/Sparkline.jsx';
import { IconCheck, IconWarningTriangle } from '../components/icons.jsx';
import { describeUnreachable, formatClockUTC } from '../format.js';

const POLL_MS = 15000;
const SUBVIEWS = [
  { key: 'health', label: 'Health' },
  { key: 'events', label: 'Events' },
  { key: 'batches', label: 'Batches & runs' },
  { key: 'packet', label: 'Trade packet' },
];

function HealthPanel({ health, startupChecks }) {
  const lf = health.labeller_failsafe;
  const pw = health.protection_watchdog;
  return (
    <div className="grid reveal-stagger">
      <div className="col-12">
        <Block title="System Health">
          <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 8 }}>Playbook: {health.playbook}</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 10 }}>
            <span className="badge num">approval {health.manual_approval}</span>
            <span className="badge num">real-money {health.real_money_trading}</span>
            <span className="badge num">market data {health.market_data_provider}/{health.market_data_feed} ({health.market_data_mode})</span>
            <span className="badge num">freshness {health.market_data_freshness}</span>
            <span className="badge num">execution {health.execution_provider}</span>
            <Badge tone={health.kill_switch === 'ENGAGED' ? 'danger' : 'ok'}>kill switch {health.kill_switch}</Badge>
            <span className="badge num">open positions {health.open_positions}</span>
            <span className="badge num">broker connected {health.broker_connected ? 'yes' : 'no'}</span>
          </div>
          <div className="label-caps" style={{ marginBottom: 6 }}>layers (mocked / deferred / disabled / live)</div>
          <div className="num" style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.8 }}>
            AI primary: {health.ai_primary} · AI reviewer: {health.ai_reviewer} · News: {health.news_provider} ·
            Benzinga: {health.benzinga} · Web scraper: {health.web_scraper} · Massive: {health.massive} ·
            last30days: {health.last30days_research} · label override: {health.labeller_decision_override} ·
            last30days polarity: {health.last30days_polarity} · real Alpaca paper: {health.real_alpaca_paper_execution}
          </div>
        </Block>
      </div>

      <div className="col-6">
        <Block title="AI labeller health" style={{ height: '100%' }}>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
            <span className="badge num">labels (recent) {lf.total}</span>
            <span className="badge num">fail-safe {lf.fail_safe}</span>
            <span className="badge num">fail-safe rate {Math.round((lf.fail_safe_rate ?? 0) * 100)}%</span>
          </div>
          {lf.message && (
            <div style={{ fontSize: 12, color: lf.level === 'critical' ? 'var(--red)' : 'var(--amber)' }}>{lf.message}</div>
          )}
        </Block>
      </div>

      <div className="col-6">
        <Block title="Protection watchdog" style={{ height: '100%' }}>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
            <span className="badge num">broker-managed positions {pw.checked}</span>
            <span className="badge num">unprotected/mismatched {pw.unprotected + pw.closed_mismatch}</span>
            <span className="badge num">open incidents {pw.open_incident_count}</span>
          </div>
          {pw.blocking ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--red)' }}>
              <IconWarningTriangle size={13} /> NEW ENTRIES BLOCKED: {pw.blocking_detail}
            </div>
          ) : pw.degraded > 0 ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--amber)' }}>
              <IconWarningTriangle size={13} /> {pw.degraded} position(s) degraded (target leg missing, stop still live) — not blocking.
            </div>
          ) : (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--primary)' }}>
              <IconCheck size={13} /> {pw.summary_label ?? 'all protected'}
            </div>
          )}
        </Block>
      </div>

      <div className="col-12">
        <Block title="Startup safety checks">
          {startupChecks.map((c) => (
            <div key={c.name} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: c.ok ? 'var(--primary)' : 'var(--red)', padding: '3px 0' }}>
              {c.ok ? <IconCheck size={13} /> : <IconWarningTriangle size={13} />} {c.name}: {c.detail}
            </div>
          ))}
        </Block>
      </div>
    </div>
  );
}

function EventsPanel({ snapshots, events }) {
  return (
    <div className="grid reveal-stagger">
      <div className="col-12">
        <Block title="Recent data freshness">
          <DataTable
            columns={[
              { key: 'symbol', label: 'symbol' },
              { key: 'provider', label: 'provider' },
              { key: 'freshness_status', label: 'freshness' },
              { key: 'is_usable', label: 'usable', numeric: true },
              { key: 'data_delay_seconds', label: 'delay (s)', numeric: true },
              { key: 'source_timestamp', label: 'source ts' },
            ]}
            rows={snapshots}
          />
        </Block>
      </div>
      <div className="col-12">
        <Block title="Recent system events">
          <DataTable
            columns={[
              { key: 'created_at_utc', label: 'time (UTC)' },
              { key: 'severity', label: 'severity' },
              { key: 'category', label: 'category' },
              { key: 'message', label: 'message' },
            ]}
            rows={events}
          />
        </Block>
      </div>
    </div>
  );
}

function BatchesPanel({ scanBatches, schedulerRuns }) {
  const cadenceSeries = [...(scanBatches ?? [])].reverse().map((b) => b.candidates_found);
  return (
    <div className="grid reveal-stagger">
      <div className="col-12">
        <Block
          title="Scan batches"
          right={(
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span className="label-caps" style={{ fontSize: 9 }}>candidates/batch</span>
              <Sparkline values={cadenceSeries} label="candidates found per scan batch, oldest to newest" />
            </div>
          )}
        >
          <DataTable
            columns={[
              { key: 'scan_batch_id', label: 'batch_id' },
              { key: 'scan_type', label: 'type' },
              { key: 'status', label: 'status' },
              { key: 'candidates_found', label: 'found', numeric: true },
              { key: 'proposals_created', label: 'proposed', numeric: true },
              { key: 'rejected_count', label: 'rejected', numeric: true },
              { key: 'blocked_count', label: 'blocked', numeric: true },
              { key: 'started_at_utc', label: 'started (UTC)' },
            ]}
            rows={scanBatches}
            emptyText="No scan batches yet."
          />
        </Block>
      </div>
      <div className="col-12">
        <Block title="Scheduler runs">
          <DataTable
            columns={[
              { key: 'scheduler_run_id', label: 'run_id' },
              { key: 'run_type', label: 'type' },
              { key: 'trigger_source', label: 'trigger' },
              { key: 'status', label: 'status' },
              { key: 'positions_touched', label: 'positions', numeric: true },
              { key: 'error_count', label: 'errors', numeric: true },
              { key: 'started_at_utc', label: 'started (UTC)' },
            ]}
            rows={schedulerRuns}
            emptyText="No scheduler runs recorded yet."
          />
        </Block>
      </div>
    </div>
  );
}

function TradePacketPanel({ recentCandidates }) {
  const [candidateId, setCandidateId] = useState('');
  const [packet, setPacket] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const lookup = useCallback(async (id) => {
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      const r = await getTradePacket({ candidate_id: id });
      setPacket(r.packet);
    } catch {
      setError('lookup failed');
    } finally {
      setLoading(false);
    }
  }, []);

  return (
    <Block title="Trade Packet (audit)">
      <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 8 }}>
        Assemble the full lifecycle for a candidate_id (read-only).
      </div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
        <select
          value={candidateId}
          onChange={(e) => { setCandidateId(e.target.value); lookup(e.target.value); }}
          style={{
            background: 'var(--surface-low)', color: 'var(--text)', border: '1px solid var(--border)',
            borderRadius: 4, padding: '10px 12px', fontSize: 13, minHeight: 44,
          }}
        >
          <option value="">— pick a recent candidate —</option>
          {recentCandidates.map((c) => (
            <option key={c.candidate_id} value={c.candidate_id}>{c.symbol} · {c.candidate_id}</option>
          ))}
        </select>
      </div>
      {loading && <div className="label-caps">loading packet…</div>}
      {error && <div style={{ fontSize: 12, color: 'var(--red)' }}>{error}</div>}
      {!loading && !error && packet && (
        <pre className="num" style={{ fontSize: 11, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
          {JSON.stringify(packet, null, 2)}
        </pre>
      )}
      {!loading && !error && !packet && candidateId === '' && (
        <div style={{ fontSize: 12, color: 'var(--text-dim)' }}>Nothing selected yet.</div>
      )}
    </Block>
  );
}

export default function System() {
  const [data, setData] = useState(null);
  const [unreachable, setUnreachable] = useState(false);
  const [lastGoodAsOf, setLastGoodAsOf] = useState(null);
  const [subview, setSubview] = useState('health');
  const mountedRef = useRef(true);

  const poll = useCallback(async () => {
    try {
      const r = await getSystem();
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
        <div className="label-caps">loading system…</div>
      ) : (
        <>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 12, flexWrap: 'wrap', gap: 8 }}>
            <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>as of {formatClockUTC(data.as_of)}</div>
            <div style={{ display: 'flex', gap: 4 }}>
              {SUBVIEWS.map((sv) => (
                <button
                  key={sv.key}
                  type="button"
                  onClick={() => setSubview(sv.key)}
                  className={`nav-tab${subview === sv.key ? ' nav-tab-active' : ''}`}
                  style={{ padding: '6px 10px', minHeight: 44 }}
                >
                  {sv.label}
                </button>
              ))}
            </div>
          </div>

          {subview === 'health' && <HealthPanel health={data.health} startupChecks={data.startup_checks} />}
          {subview === 'events' && <EventsPanel snapshots={data.recent_snapshots} events={data.recent_events} />}
          {subview === 'batches' && <BatchesPanel scanBatches={data.scan_batches} schedulerRuns={data.scheduler_runs} />}
          {subview === 'packet' && <TradePacketPanel recentCandidates={data.recent_candidates} />}
        </>
      )}
    </div>
  );
}
