// ND-6 pure display-math for the Decisions page's gate funnel (design
// ruling §3.4/§5: "visualize it as a funnel, not a table of counts").
// No DOM, no React -- same pattern as positions.js/approvals.js.
// Source of the stage counts: decisions.js:buildDecisionFunnelStages()
// (the actual decision arrays -- proposed/watch/rejected/blocked/filled;
// 2026-07-17 it no longer reads label_summary/by_label_decision).

// `stages`: [{ label, value, tone? }] in pipeline order. Returns each stage
// with a `pct` (0-100, two-decimal-rounded) width relative to the LARGEST
// measurable stage value -- never relative to the first stage, since these
// stages have no guaranteed order-of-magnitude (rejected/watch routinely
// dwarf proposed/filled, the reverse of a textbook top-heavy funnel). A
// stage with a null/undefined/NaN value is unmeasurable (unknown-never-zero)
// and gets `pct: null` -- the caller renders an explicit "n/a" bar rather
// than a fabricated zero-width one.
export function computeFunnelStages(stages) {
  const list = stages ?? [];
  const measurable = list.filter(
    (s) => s.value !== null && s.value !== undefined && !Number.isNaN(s.value),
  );
  const max = measurable.length ? Math.max(...measurable.map((s) => s.value), 1) : 1;
  return list.map((s) => {
    if (s.value === null || s.value === undefined || Number.isNaN(s.value)) {
      return { ...s, pct: null };
    }
    return { ...s, pct: Math.round((s.value / max) * 100 * 100) / 100 };
  });
}
