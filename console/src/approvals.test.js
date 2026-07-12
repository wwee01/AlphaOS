import { describe, expect, it } from 'vitest';
import { computeTtlBar, sortByTtl } from './approvals.js';

describe('computeTtlBar', () => {
  it('renders "unknown" (null pct) when seconds_remaining or total_ttl_seconds is unmeasurable', () => {
    expect(computeTtlBar(null, 1800)).toEqual({ state: 'unknown', pct: null });
    expect(computeTtlBar(900, null)).toEqual({ state: 'unknown', pct: null });
    expect(computeTtlBar(900, 0)).toEqual({ state: 'unknown', pct: null });
  });

  it('draws an expired TTL FULL (100%), a solid alert rather than an empty track', () => {
    const bar = computeTtlBar(-5, 1800);
    expect(bar.state).toBe('expired');
    expect(bar.pct).toBe(100);
  });

  it('flags "low" under 20% remaining without being expired', () => {
    const bar = computeTtlBar(300, 1800); // 16.67%
    expect(bar.state).toBe('low');
    expect(bar.pct).toBeCloseTo(16.67, 1);
  });

  it('reads "ok" at 20% or above', () => {
    const bar = computeTtlBar(900, 1800); // 50%
    expect(bar.state).toBe('ok');
    expect(bar.pct).toBe(50);
  });
});

describe('sortByTtl', () => {
  it('orders soonest-to-expire first', () => {
    const input = [
      { symbol: 'B', proposal_seconds_remaining: 500 },
      { symbol: 'A', proposal_seconds_remaining: 100 },
      { symbol: 'C', proposal_seconds_remaining: 300 },
    ];
    expect(sortByTtl(input).map((p) => p.symbol)).toEqual(['A', 'C', 'B']);
  });

  it('sorts an unknown/unparseable TTL LAST, never first (unknown-never-most-urgent)', () => {
    const input = [
      { symbol: 'unknown', proposal_seconds_remaining: null },
      { symbol: 'urgent', proposal_seconds_remaining: 50 },
    ];
    expect(sortByTtl(input).map((p) => p.symbol)).toEqual(['urgent', 'unknown']);
  });

  it('does not mutate its input array', () => {
    const input = [
      { symbol: 'B', proposal_seconds_remaining: 500 },
      { symbol: 'A', proposal_seconds_remaining: 100 },
    ];
    const originalOrder = input.map((p) => p.symbol);
    sortByTtl(input);
    expect(input.map((p) => p.symbol)).toEqual(originalOrder);
  });
});
