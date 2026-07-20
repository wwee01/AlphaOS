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

// Builds the Decisions page's gate-funnel stages (design ruling §2/§5:
// "candidates -> proposed/watch -> rejected/blocked -> filled... visualize
// it as a funnel").
//
// 2026-07-17 (operator report "gate funnel numbers seem off"): REWRITTEN to
// source from the ACTUAL decision arrays this same /api/v1/decisions
// response already carries -- the exact arrays that render the tables right
// below the funnel -- so every bar is mechanically equal to the table
// beneath it and can never disagree. The old version read
// `label_summary.by_label_decision`: the *advisory AI labeller's* suggested
// decision (downgrade-only, NOT the resolved outcome -- see orchestrator.py
// `_apply_label_floor`), counted per labelling EVENT (a candidate re-labelled
// across N scans counted N times), then prepended a synthetic sum as a
// "candidates" total. That triple-counted the wrong thing: the funnel showed
// watch=206/propose=9/reject=6 while the tables directly below showed
// watch=24/proposed=5/rejected=32. Pure count of arrays already on the page
// -- no new data, no business decision.
//
// Stage order mirrors the tables' own top-to-bottom order on the page
// (proposed, watch, rejected, blocked, then the trade ledger => filled) so a
// bar always sits directly above the table it counts. `filled` is every
// position that reached execution (open + closed), the pipeline's endpoint.
export function buildDecisionFunnelStages(decisions) {
  if (!decisions) return [];
  const n = (arr) => (Array.isArray(arr) ? arr.length : 0);
  const stages = [
    { label: 'proposed', value: n(decisions.proposed) },
    { label: 'watch', value: n(decisions.watch) },
    { label: 'rejected', value: n(decisions.rejected) },
    { label: 'blocked', value: n(decisions.blocked) },
    { label: 'filled', value: n(decisions.open_trades) + n(decisions.closed_trades) },
  ];
  // All-zero => a genuinely empty/fresh journal; return [] so the caller
  // shows its "run an interest scan" empty state rather than five 0-bars.
  if (stages.every((s) => s.value === 0)) return [];
  return stages;
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

// formatSentimentHint() (the raw sentiment_label column) lived here briefly
// on 2026-07-17, same day it was added -- removed once the operator noticed
// it read "unknown" for every single row and traced why: the live CLI
// provider (last30days_provider.py) hardcodes sentiment_hint=None
// unconditionally, only the mock provider ever sets it, so this field
// carries zero information under any real config and always will.
// formatNarrative() above (polarity_label, a separate model call) is the
// real signal and is unaffected.
