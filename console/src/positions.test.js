import { describe, expect, it } from 'vitest';
import { computeRLadder, verdictIcon } from './positions.js';

describe('verdictIcon', () => {
  it('maps known verdicts to their icons (mirrors streamlit_app._VERDICT_ICON)', () => {
    expect(verdictIcon('HOLD')).toBe('🟢');
    expect(verdictIcon('ATTENTION')).toBe('🟡');
    expect(verdictIcon('EXIT_REVIEW')).toBe('🔴');
  });
  it('falls back to a neutral icon for an unknown verdict', () => {
    expect(verdictIcon('SOMETHING_NEW')).toBe('⚪');
  });
});

describe('computeRLadder', () => {
  it('returns null when current/stop/target is unmeasurable (unavailable fallback)', () => {
    expect(computeRLadder({ stopR: -1, entryR: 0, currentR: null, targetR: 2 })).toBeNull();
    expect(computeRLadder({ stopR: null, entryR: 0, currentR: 0.5, targetR: 2 })).toBeNull();
    expect(computeRLadder({ stopR: -1, entryR: 0, currentR: 0.5, targetR: undefined })).toBeNull();
  });

  it('places ticks proportionally across the stop-target span', () => {
    const ladder = computeRLadder({ stopR: -1, entryR: 0, currentR: 0.5, targetR: 2 });
    const byName = Object.fromEntries(ladder.ticks.map((t) => [t.name, t.pct]));
    expect(byName.stop).toBe(0);
    expect(byName.entry).toBeCloseTo(33.33, 1);
    expect(byName.target).toBe(100);
    expect(ladder.current.pct).toBe(50);
  });

  it('widens the span to include current_r when it sits outside stop..target', () => {
    // current_r below the stop -- span must extend to include it, not clip.
    const ladder = computeRLadder({ stopR: 0, entryR: 0, currentR: -2, targetR: 2 });
    const byName = Object.fromEntries(ladder.ticks.map((t) => [t.name, t.pct]));
    expect(ladder.current.pct).toBe(0);
    expect(byName.target).toBe(100);
    expect(byName.stop).toBeCloseTo(50, 1);
  });

  it('renders at the midpoint on a degenerate (zero-span) ladder rather than dividing by zero', () => {
    const ladder = computeRLadder({ stopR: 1, entryR: 1, currentR: 1, targetR: 1 });
    expect(ladder.current.pct).toBe(50);
    for (const t of ladder.ticks) expect(t.pct).toBe(50);
  });
});
