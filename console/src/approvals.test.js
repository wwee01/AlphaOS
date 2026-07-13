import { describe, expect, it } from 'vitest';
import {
  canApprove, computeTtlBar, marginApprovalRequired, shouldShowProposalActions, sortByTtl,
} from './approvals.js';

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

describe('marginApprovalRequired', () => {
  it('is true only when requires_margin is exactly truthy', () => {
    expect(marginApprovalRequired({ requires_margin: true })).toBe(true);
    expect(marginApprovalRequired({ requires_margin: false })).toBe(false);
    expect(marginApprovalRequired({})).toBe(false);
  });

  it('treats a missing proposal as "not required" rather than throwing', () => {
    expect(marginApprovalRequired(null)).toBe(false);
    expect(marginApprovalRequired(undefined)).toBe(false);
  });
});

describe('canApprove', () => {
  it('is always approvable when margin is not required, regardless of checkbox state', () => {
    expect(canApprove({ requires_margin: false }, false)).toBe(true);
    expect(canApprove({ requires_margin: false }, true)).toBe(true);
  });

  it('requires the explicit checkbox when margin is required -- never silently approved by omission', () => {
    expect(canApprove({ requires_margin: true }, false)).toBe(false);
    expect(canApprove({ requires_margin: true }, undefined)).toBe(false);
    expect(canApprove({ requires_margin: true }, true)).toBe(true);
  });
});

describe('shouldShowProposalActions', () => {
  it('stays true regardless of staleness -- client-side checks are advisory only, never hide the button preemptively', () => {
    expect(shouldShowProposalActions({ proposal_is_stale: true })).toBe(true);
    expect(shouldShowProposalActions({ proposal_is_stale: false })).toBe(true);
    expect(shouldShowProposalActions(null)).toBe(true);
  });
});
