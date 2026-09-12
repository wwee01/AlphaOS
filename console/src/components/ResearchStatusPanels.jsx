import React from 'react';
import { Block, Badge, DataTable } from './ui.jsx';

const number = (value) => value == null ? '—' : Number(value).toFixed(4);

export function ExperimentRegistry({ report }) {
  if (!report) return <Block title="All retained experiments" tone="shadow">Experiment status unavailable.</Block>;
  return (
    <Block title="All retained experiments" tone="shadow">
      <p>{report.registered_count} registrations · {report.evaluated_count} with frozen evidence · {report.superseded_count} superseded and retained</p>
      <p className="prose">{report.caveat}</p>
      <DataTable rows={report.experiments} emptyText="No experiments registered." columns={[
        { key: 'claim', label: 'Experiment', render: (r) => <><b>{r.hypothesis_id ?? r.prereg_id}</b><p>{r.claim}</p><small>Metric: {r.metric}</small></> },
        { key: 'state', label: 'State / analysis', render: (r) => <>{r.state}<p>Not before {r.analysis_not_before}</p>{r.registry_date_mismatch && <Badge tone="warn">Registry date differs; registration date shown</Badge>}<p>{r.evaluation_path}</p>{r.superseded_by && <small>Replaced by {r.superseded_by}</small>}</> },
        { key: 'verdict', label: 'Frozen result', render: (r) => <>{r.verdict ?? 'Not finalized'}<p>Estimate {number(r.point_estimate)}</p><p>{r.ci_level == null ? '' : `${Math.round(r.ci_level * 100)}% `}CI [{number(r.ci_low)}, {number(r.ci_high)}]</p><small>Family q: {number(r.q_value)}</small><p>{r.verdict_reason}</p></> },
        { key: 'sample', label: 'Evidence', render: (r) => <>Raw n: {r.n_raw ?? '—'}<p>{r.sample_unit}: {r.effective_n ?? r.progress?.effective_n ?? '—'} / {r.floor_effective_n}</p><p>Span: {r.span_days ?? r.progress?.span_days ?? '—'} / {r.floor_span_days} days</p><small>Frozen at: {r.evaluated_at_utc ?? '—'}</small></> },
        { key: 'validity_context', label: 'Interpretation', render: (r) => <>{r.validity_context}<p>Operator status: {r.operator_status ?? 'No registry ruling'}</p>{r.operator_notes && <p>{r.operator_notes}</p>}</> },
      ]} />
    </Block>
  );
}

export function CollectionStatus({ collection }) {
  if (!collection) return <Block title="Research collection" tone="shadow">Collection status unavailable.</Block>;
  const shadow = collection.shadow;
  const checks = shadow.current_checks ?? {};
  const enrichment = collection.last30days;
  return (
    <Block title="Research collection — actual output" tone="shadow">
      <Badge tone={shadow.suspended || checks.error ? 'danger' : 'warn'}>Shadow AI: {shadow.state}</Badge>
      <p>{shadow.suspension_reason}</p>
      <p>{shadow.successful_labels ?? 0} successful real labels · last success {shadow.last_successful_label_at_utc ?? 'never'}</p>
      <p>Last scheduled attempt: {shadow.last_job?.started_at_utc ?? 'never'} · {shadow.last_job?.status ?? '—'}</p>
      <p>Latest real canary: {shadow.latest_canary?.started_at_utc ?? 'never'} · drift {shadow.latest_canary?.drift_tier ?? 'unknown'}</p>
      <p className="prose">{shadow.note}</p>
      <details><summary>Current conditions and unchanged budgets</summary>
        <p>{checks.error ?? checks.auto_suspend_reason}</p>
        <p>{checks.coverage_reason}</p><p>{checks.budget_reason}</p>
        <p>Daily cap {shadow.daily_cap} · shadow 30-day cap {shadow.shadow_30d_cap} · shared 30-day cap {shadow.shared_30d_cap}</p>
      </details>
      <p>Recent-context enrichment: {enrichment.enabled ? 'enabled' : 'disabled'} · {enrichment.errors ?? 0} errors / {enrichment.attempts ?? 0} attempts in {enrichment.window_days} days ({enrichment.timeouts ?? 0} timeouts). Last success {enrichment.last_success_at_utc ?? 'never'}.</p>
      <p>Official text archive: {collection.text_archive.enabled ? 'enabled' : 'disabled'} · {collection.text_archive.documents ?? 0} documents · last seen {collection.text_archive.last_seen_at_utc ?? 'never'}.</p>
      <p>Regime collection: {collection.regime.enabled ? 'enabled' : 'disabled'} · {collection.regime.days ?? 0} days · last session {collection.regime.last_market_date ?? 'never'}.</p>
    </Block>
  );
}
