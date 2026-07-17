// ND-2 pure display-formatting for the Decisions page -- ports
// streamlit_app._hindsight_cell()'s formatting exactly (not its logic: the
// API's /api/v1/decisions already attaches the raw attribution row under
// `hindsight_raw`, unformatted; this is the ONLY place that turns it into
// display text). No DOM, no React (ND-1 precedent: format.js).
//
// 2026-07-17: this file previously also carried `universeOf()` (a core/
// shadow badge helper) -- removed once /api/v1/decisions started hard-
// filtering to core-only server-side (journal_store.py) and shadow data
// moved to its own tab (pages/Research.jsx, research.js). Nothing on this
// page can be a shadow row anymore, so the distinction no longer applies
// here.

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

// Narrative/sentiment cell (operator bug 2026-07-17: the column showed
// sentiment_label, a legacy per-cluster HINT the live CLI provider never
// sets -- see last30days_provider.py:54 "mock sets it, CLI leaves None" --
// so every live row read "unknown" even when the polarity LLM HAD classified
// it). The real classification is polarity_label (Roadmap 2.7). Fallbacks
// distinguish the three honest non-answers instead of one misleading
// "unknown": research never ran / ran but found nothing / ran but the
// polarity classifier didn't produce a verdict.
export function formatNarrative(row) {
  if (!row) return 'not researched';
  if (row.polarity_label) return row.polarity_label;
  const status = row.last30days_status;
  if (status === 'available' || status === 'stale') return 'not classified';
  if (status === 'none_found') return 'no narrative found';
  return 'not researched';
}

// Raw last30days sentiment hint, shown alongside the polarity verdict rather
// than collapsed into it (operator request 2026-07-17, while diagnosing why
// "sentiment" read unknown: the two are different signals -- this is the
// un-classified per-cluster hint the enricher attaches directly, always
// populated (defaults to 'unknown' with the live CLI provider -- see
// last30days_provider.py:54) -- so a direct read, not a fallback chain.
export function formatSentimentHint(row) {
  return row?.sentiment_label ?? 'unknown';
}
