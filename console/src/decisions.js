// ND-2 pure display-formatting for the Decisions page -- ports
// streamlit_app._hindsight_cell()'s formatting exactly (not its logic: the
// API's /api/v1/decisions already attaches the raw attribution row under
// `hindsight_raw`, unformatted; this is the ONLY place that turns it into
// display text). No DOM, no React (ND-1 precedent: format.js).

// An unresolved/missing replay reads "pending", never a fabricated 0R
// (unknown-never-zero). A mock ΔR (is_mock on the attribution row -- only
// ever set in mock mode) is tagged "(mock)" so a simulated learning is never
// styled identically to a real one (same ΔR-surface honesty rule the
// Tonight page's "learned today" sentences already carry).
export function formatHindsight(attr) {
  if (!attr || attr.resolved_status !== 'resolved') return 'pending';
  const delta = attr.delta_r;
  if (delta === null || delta === undefined) return 'pending';
  const sign = delta >= 0 ? '+' : '';
  const suffix = attr.is_mock ? ' (mock)' : '';
  return `${sign}${delta.toFixed(2)}R${suffix}`;
}

// ND-6: builds the Decisions page's gate-funnel stages (design ruling §2/§5:
// "candidates -> proposed -> blocked -> rejected... visualize it as a
// funnel") from /api/v1/decisions' `label_summary.by_label_decision` rows.
// Each row's own field is `decision` (verified against the live API --
// `journal.label_summary()`'s own row shape, e.g. `{"decision": "propose",
// "n": 11}` -- NOT `label_decision`, which is a candidate-row field name
// used elsewhere on this page). A "candidates" total (the sum of every
// decision bucket's count) is prepended as the funnel's first stage, so the
// pipeline always starts at the full evaluated population rather than at
// whichever bucket happens to be listed first. Pure aggregation of numbers
// already shown elsewhere on this page (the raw by_label_decision rows
// themselves) -- not a business decision, same category as Tonight's own
// open-R summation.
export function buildDecisionFunnelStages(byLabelDecision) {
  const rows = byLabelDecision ?? [];
  if (rows.length === 0) return [];
  const total = rows.reduce((sum, r) => sum + (r.n ?? 0), 0);
  return [
    { label: 'candidates', value: total },
    ...rows.map((r) => ({ label: r.decision ?? 'unknown', value: r.n ?? null })),
  ];
}
