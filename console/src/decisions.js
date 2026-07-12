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
