// ND-6 pure display-math for the Decisions page's gate funnel (design
// ruling §3.4/§5: "the Funnel component fed by label_summary/
// by_label_decision... visualize it as a funnel, not a table of counts").
// No DOM, no React -- same pattern as positions.js/approvals.js.

// `stages`: [{ label, value, tone? }] in pipeline order. Returns each stage
// with a `pct` (0-100, two-decimal-rounded) width relative to the LARGEST
// measurable stage value -- never relative to the first stage, since a
// funnel built from label_summary/by_label_decision rows has no guaranteed
// order-of-magnitude (a "blocked" or "rejected" stage is not guaranteed to
// be smaller than "candidates" the way a textbook funnel would be). A stage
// with a null/undefined/NaN value is unmeasurable (unknown-never-zero) and
// gets `pct: null` -- the caller renders an explicit "n/a" bar rather than
// a fabricated zero-width one.
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
