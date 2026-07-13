import { describe, expect, it } from 'vitest';
import { computeFunnelStages } from './funnel.js';

describe('computeFunnelStages', () => {
  it('scales every stage relative to the LARGEST measurable value, not the first stage', () => {
    const out = computeFunnelStages([
      { label: 'candidates', value: 6 },
      { label: 'proposed', value: 3 },
      { label: 'blocked', value: 1 },
      { label: 'rejected', value: 12 }, // larger than "candidates" -- must still be the 100% reference
    ]);
    const byLabel = Object.fromEntries(out.map((s) => [s.label, s.pct]));
    expect(byLabel.rejected).toBe(100);
    expect(byLabel.candidates).toBe(50);
    expect(byLabel.proposed).toBe(25);
    expect(byLabel.blocked).toBeCloseTo(8.33, 1);
  });

  it('gives a null pct (unknown-never-zero) to an unmeasurable stage rather than drawing it at 0%', () => {
    const out = computeFunnelStages([
      { label: 'candidates', value: 6 },
      { label: 'unmeasured', value: null },
    ]);
    const byLabel = Object.fromEntries(out.map((s) => [s.label, s.pct]));
    expect(byLabel.candidates).toBe(100);
    expect(byLabel.unmeasured).toBeNull();
  });

  it('handles an all-unmeasurable stage list without dividing by zero', () => {
    const out = computeFunnelStages([
      { label: 'a', value: null },
      { label: 'b', value: undefined },
    ]);
    expect(out.every((s) => s.pct === null)).toBe(true);
  });

  it('handles an empty stage list', () => {
    expect(computeFunnelStages([])).toEqual([]);
    expect(computeFunnelStages(undefined)).toEqual([]);
  });

  it('gives every stage 100% when all values are equal (degenerate, no divide-by-zero)', () => {
    const out = computeFunnelStages([{ label: 'a', value: 4 }, { label: 'b', value: 4 }]);
    expect(out.every((s) => s.pct === 100)).toBe(true);
  });
});
