// ND-2 Learning page -- renders /api/v1/learning: TQS / Attribution /
// Hypotheses / Journal, the four read-only sub-panels
// streamlit_app.tab_learning() renders (PR-UI-B2). PURE READ, zero writes,
// zero buttons that change state -- matches the Streamlit tab's own
// invariant exactly.
//
// Reporting-law discipline (see routes.py's /learning docstring and
// learning.js's module docstring): every mean/sum ΔR shown below passes
// through formatAttributionRow(), which withholds both fields unless the
// aggregate's own `status === "ok"` -- i.e. unless alphaos/reports/
// attribution.py's sample-floor gate has already cleared. This is the one
// guard on this page that was swap-tested (see console/src/learning.test.js
// and the ND-2 build report).
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { getLearning } from '../api.js';
import { Block, DataTable } from '../components/ui.jsx';
import { describeUnreachable, formatClockUTC } from '../format.js';
import { formatAttributionRow, formatHypothesisProgress, formatHypothesisStatus } from '../learning.js';

const POLL_MS = 15000;

function TqsPanel({ tqs }) {
  if (tqs.scored_count === 0) {
    return (
      <Block title="TQS — evidence-weighted setup quality">
        <div style={{ fontSize: 13, color: 'var(--text-dim)' }}>No TQS scores yet (mock rows excluded).</div>
      </Block>
    );
  }
  const buckets = Object.entries(tqs.bucket_histogram).sort(([a], [b]) => a.localeCompare(b));
  return (
    <Block title="TQS — evidence-weighted setup quality">
      <div style={{ display: 'flex', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
        <span className="badge num">scored (live) {tqs.scored_count}</span>
        <span className="badge num">mean confidence {tqs.mean_data_confidence}</span>
        <span className="badge num">mock excluded {tqs.mock_excluded_count}</span>
      </div>
      <div className="label-caps" style={{ marginBottom: 6 }}>bucket histogram</div>
      <DataTable
        columns={[{ key: 'bucket', label: 'bucket' }, { key: 'n', label: 'n', numeric: true }]}
        rows={buckets.map(([bucket, n]) => ({ bucket, n }))}
      />
      <div className="label-caps" style={{ margin: '10px 0 6px' }}>component availability</div>
      <DataTable
        columns={[
          { key: 'component', label: 'component' },
          { key: 'available', label: 'available', numeric: true },
          { key: 'missing', label: 'missing', numeric: true },
          { key: 'availability_rate', label: 'rate', numeric: true },
        ]}
        rows={Object.entries(tqs.component_availability).map(([name, c]) => ({ component: name, ...c }))}
      />
    </Block>
  );
}

const ATTR_ROW_COLUMNS = [
  { key: 'slice', label: 'slice' },
  { key: 'n', label: 'n', numeric: true },
  { key: 'spanDays', label: 'span_days', numeric: true },
  { key: 'meanDeltaR', label: 'mean ΔR', numeric: true },
  { key: 'sumDeltaR', label: 'sum ΔR', numeric: true },
  { key: 'status', label: 'status' },
];

function AttributionPanel({ attribution }) {
  const v2 = attribution.v2;
  const rows = [];
  for (const [atype, byAgent] of Object.entries(v2.aggregate_delta_r_by_type_and_agent ?? {})) {
    for (const [agent, agg] of Object.entries(byAgent)) {
      rows.push(formatAttributionRow(`${atype} / ${agent}`, agg, v2.sample_floor_resolved, v2.sample_floor_span_days));
    }
  }
  const cardRows = Object.entries(v2.aggregate_delta_r_by_card ?? {}).map(([cardId, agg]) => formatAttributionRow(
    cardId, agg, v2.sample_floor_subslice_resolved, v2.sample_floor_span_days,
  ));
  const executionGapRow = v2.execution_gap_propose_approved_executed
    ? [formatAttributionRow('execution_delta_r', v2.execution_gap_propose_approved_executed, v2.sample_floor_resolved, v2.sample_floor_span_days)]
    : [];

  return (
    <Block title="Attribution — floor-gated ΔR aggregates">
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8 }}>{v2.caveat}</div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
        <span className="badge num">total records {v2.total_records}</span>
        <span className="badge num">mock excluded {v2.mock_excluded_count}</span>
      </div>
      <DataTable columns={ATTR_ROW_COLUMNS} rows={rows} emptyText="No attribution aggregates yet." />
      <div className="label-caps" style={{ margin: '10px 0 6px' }}>by setup card</div>
      <DataTable columns={ATTR_ROW_COLUMNS} rows={cardRows} emptyText="None." />
      <div className="label-caps" style={{ margin: '10px 0 6px' }}>execution gap (propose → approved → executed)</div>
      <DataTable columns={ATTR_ROW_COLUMNS} rows={executionGapRow} emptyText="None." />
    </Block>
  );
}

function HypothesesPanel({ hypotheses, drafts }) {
  const rows = hypotheses.hypotheses.map((h) => ({
    ...h,
    status_label: formatHypothesisStatus(h.status),
    overdue_label: h.overdue ? 'yes' : '',
    progress_label: formatHypothesisProgress(h.progress),
  }));
  return (
    <Block title="Hypotheses — PR12 registry">
      <div style={{ fontSize: 13, marginBottom: 8 }}>
        {hypotheses.n_total} total · {hypotheses.n_proposed} proposed · {hypotheses.n_testing} testing · {hypotheses.n_resolved} resolved
      </div>
      <DataTable
        columns={[
          { key: 'hypothesis_id', label: 'id' },
          { key: 'risk_class', label: 'risk class' },
          { key: 'status_label', label: 'status' },
          { key: 'overdue_label', label: 'overdue' },
          { key: 'progress_label', label: 'progress' },
          { key: 'analysis_not_before', label: 'not before' },
          { key: 'claim', label: 'claim' },
        ]}
        rows={rows}
        emptyText="No hypotheses registered yet."
      />
      <div className="label-caps" style={{ margin: '10px 0 6px' }}>
        HGEN-1 drafts — quarantined, awaiting operator review ({drafts.length} pending)
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 6 }}>
        Read-only here. Accept/reject is an operator CLI action only (`alphaos hypothesis_accept` /
        `hypothesis_reject`) — never a UI button.
      </div>
      <DataTable
        columns={[
          { key: 'draft_id', label: 'draft_id' },
          { key: 'title', label: 'title' },
          { key: 'mechanical_risk_class', label: 'mech. risk' },
          { key: 'proposed_risk_class', label: 'proposed risk' },
          { key: 'source', label: 'source' },
          { key: 'created_at_utc', label: 'created (UTC)' },
        ]}
        rows={drafts}
        emptyText="No pending drafts."
      />
    </Block>
  );
}

function JournalPanel({ feed }) {
  if (!feed.entries.length) {
    return (
      <Block title="Journal — newest first">
        <div style={{ fontSize: 13, color: 'var(--text-dim)' }}>Nothing in the journal yet.</div>
      </Block>
    );
  }
  return (
    <Block title="Journal — newest first">
      {feed.entries.map((e, i) => {
        const prov = Object.entries(e.provenance ?? {})
          .filter(([, v]) => v !== null && v !== undefined)
          .map(([k, v]) => `${k}=${v}`)
          .join(', ');
        return (
          <div key={`${e.timestamp}_${i}`} style={{ padding: '4px 0', borderBottom: '1px solid var(--border)' }}>
            <div style={{ fontSize: 12 }}><span className="num" style={{ color: 'var(--text-dim)' }}>{e.timestamp}</span> — {e.text}</div>
            {prov && <div style={{ fontSize: 10, color: 'var(--text-dim)' }}>{prov}</div>}
          </div>
        );
      })}
    </Block>
  );
}

export default function Learning() {
  const [data, setData] = useState(null);
  const [unreachable, setUnreachable] = useState(false);
  const [lastGoodAsOf, setLastGoodAsOf] = useState(null);
  const mountedRef = useRef(true);

  const poll = useCallback(async () => {
    try {
      const r = await getLearning();
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
      <div className="stale-banner" style={{ borderColor: 'rgba(255,184,115,0.5)', background: 'rgba(255,184,115,0.08)', color: 'var(--amber)' }}>
        🧭 Hypothesis outcomes are ruled by the operator. AlphaOS never adjusts its own weights or rules on its
        own — every MET/FAILED/WITHDRAWN verdict below is a human judgment call, not a machine one.
      </div>

      {!data ? (
        <div className="label-caps">loading learning…</div>
      ) : (
        <>
          <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 12 }}>as of {formatClockUTC(data.as_of)}</div>
          <div className="grid">
            <div className="col-12"><TqsPanel tqs={data.tqs} /></div>
            <div className="col-12" style={{ marginTop: 4 }}><AttributionPanel attribution={data.attribution} /></div>
            <div className="col-12" style={{ marginTop: 4 }}>
              <HypothesesPanel hypotheses={data.hypotheses} drafts={data.hypothesis_drafts} />
            </div>
            <div className="col-12" style={{ marginTop: 4 }}><JournalPanel feed={data.journal_feed} /></div>
          </div>
        </>
      )}
    </div>
  );
}
