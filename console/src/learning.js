// ND-2 pure display-formatting for the Learning page -- ports three
// streamlit_app.py helpers' EXACT text (not their logic: /api/v1/learning
// already passes each report dict through verbatim from
// alphaos/reports/{attribution,hypothesis_report}.py). No DOM, no React
// (ND-1 precedent: format.js).
//
// Reporting-law discipline (streamlit_app._learned_sentence()'s own
// docstring: "reuses the reporting law's 'aggregate tone, no moralizing'
// rule"; audit C4: never a per-event raw number standing in for a verdict):
// formatAttributionRow() below is the ONE place a mean/sum ΔR is allowed to
// reach the screen, and only when `agg.status === "ok"` -- i.e. only when
// alphaos/reports/attribution.py's own floor gate (effective-N AND span-day
// sample floor) has already cleared. Below the floor this returns null for
// both, never a fabricated or "close enough" number.

// Mirrors streamlit_app._attribution_v2_agg_row() field-for-field and
// word-for-word (the "below floor" status string is user-facing copy, kept
// byte-identical).
export function formatAttributionRow(label, agg, floorN, floorSpan) {
  const n = agg.effective_n ?? agg.resolved_count ?? null;
  if (agg.status === 'ok') {
    return {
      slice: label,
      n,
      spanDays: agg.span_days,
      meanDeltaR: agg.mean_delta_r,
      sumDeltaR: agg.sum_delta_r,
      status: 'ok',
    };
  }
  return {
    slice: label,
    n,
    spanDays: agg.span_days,
    meanDeltaR: null,
    sumDeltaR: null,
    status: `n=${n}/${floorN} below floor — counts only (needs ≥${floorSpan}d span)`,
  };
}

// Mirrors streamlit_app._hypothesis_status_label(): MET/FAILED/WITHDRAWN are
// reserved for an operator ruling on resolved evidence -- always tagged so
// this view never implies AlphaOS judged its own hypothesis.
export function formatHypothesisStatus(status) {
  if (status === 'met' || status === 'failed' || status === 'withdrawn') {
    return `${status} (operator ruling)`;
  }
  return status;
}

// Mirrors streamlit_app._hypothesis_progress_label() exactly, including the
// "—" (em dash) no-progress-yet case and the "n/a" unknown-span case
// (unknown-never-zero: a missing span is never rendered as "0").
export function formatHypothesisProgress(progress) {
  if (!progress) return '—';
  const en = progress.effective_n;
  const floorEn = progress.floor_effective_n;
  const span = progress.span_days;
  const floorSpan = progress.floor_span_days;
  const spanStr = span !== null && span !== undefined ? span.toFixed(0) : 'n/a';
  let ready;
  if (progress.resolver_ready) {
    ready = '✓ resolver-ready';
  } else if (progress.clears_floor) {
    ready = '✓ data floor met · awaiting analysis date';
  } else {
    ready = 'below floor';
  }
  return `n=${en}/${floorEn} · span=${spanStr}/${floorSpan.toFixed(0)}d · ${ready}`;
}
