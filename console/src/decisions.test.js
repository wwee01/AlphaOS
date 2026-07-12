import { describe, expect, it } from 'vitest';
import { formatHindsight } from './decisions.js';

describe('formatHindsight', () => {
  it('reads "pending" when there is no attribution row at all (unknown-never-zero)', () => {
    expect(formatHindsight(null)).toBe('pending');
    expect(formatHindsight(undefined)).toBe('pending');
  });

  it('reads "pending" when the row exists but has not resolved yet', () => {
    expect(formatHindsight({ resolved_status: 'open', delta_r: 1.2 })).toBe('pending');
  });

  it('reads "pending" when resolved but delta_r is still null', () => {
    expect(formatHindsight({ resolved_status: 'resolved', delta_r: null })).toBe('pending');
  });

  it('formats a resolved real delta with a sign, never a bare unsigned number', () => {
    expect(formatHindsight({ resolved_status: 'resolved', delta_r: 0.5, is_mock: 0 })).toBe('+0.50R');
    expect(formatHindsight({ resolved_status: 'resolved', delta_r: -0.75, is_mock: 0 })).toBe('-0.75R');
  });

  it('tags a mock delta with "(mock)" so it is never styled like a real one', () => {
    expect(formatHindsight({ resolved_status: 'resolved', delta_r: 1.1, is_mock: 1 })).toBe('+1.10R (mock)');
  });
});
