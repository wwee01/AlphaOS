// ND-1: vitest coverage for the pure display-formatting helpers (format.js
// has no DOM/React dependency by design, so these run without jsdom).
import { describe, expect, it } from 'vitest';
import {
  describeUnreachable, formatAge, formatClockUTC, formatHeartbeat,
  formatOpenR, formatR, formatSecondsRemaining,
} from './format.js';

describe('formatR', () => {
  it('renders null/undefined as n/a, never 0R (unknown-never-zero)', () => {
    expect(formatR(null)).toBe('n/a');
    expect(formatR(undefined)).toBe('n/a');
  });
  it('signs positive and negative values', () => {
    expect(formatR(1.234)).toBe('+1.23R');
    expect(formatR(-0.4)).toBe('-0.40R');
    expect(formatR(0)).toBe('+0.00R');
  });
});

describe('formatAge', () => {
  it('renders null as unknown', () => {
    expect(formatAge(null)).toBe('unknown');
  });
  it('matches streamlit_app._format_age thresholds', () => {
    expect(formatAge(5)).toBe('5s');
    expect(formatAge(59)).toBe('59s');
    expect(formatAge(60)).toBe('1m');
    expect(formatAge(125)).toBe('2m');
    expect(formatAge(3600)).toBe('1.0h');
    expect(formatAge(5400)).toBe('1.5h');
  });
});

describe('formatHeartbeat', () => {
  it('renders null as "no runs yet", not "unknown ago"', () => {
    expect(formatHeartbeat(null)).toBe('no runs yet');
  });
  it('renders a known age with an "ago" suffix', () => {
    expect(formatHeartbeat(30)).toBe('30s ago');
  });
});

describe('formatSecondsRemaining', () => {
  it('matches streamlit_app._format_seconds_remaining exactly', () => {
    expect(formatSecondsRemaining(null)).toBe('unknown');
    expect(formatSecondsRemaining(-5)).toBe('expired 5s ago');
    expect(formatSecondsRemaining(0)).toBe('expired 0s ago');
    expect(formatSecondsRemaining(125)).toBe('2m 5s');
  });
});

describe('formatClockUTC', () => {
  it('renders unknown for missing/invalid input', () => {
    expect(formatClockUTC(null)).toBe('unknown');
    expect(formatClockUTC('not-a-date')).toBe('unknown');
  });
  it('extracts HH:MM:SS UTC from an ISO timestamp', () => {
    expect(formatClockUTC('2026-07-12T05:24:50.788348+00:00')).toBe('05:24:50 UTC');
  });
});

describe('formatOpenR', () => {
  it('shows n/a with no suffix when nothing is measurable', () => {
    expect(formatOpenR(null, 0)).toBe('n/a');
  });
  it('adds an "(N n/a)" suffix when some positions are unmeasurable', () => {
    expect(formatOpenR(1.5, 2)).toBe('+1.50R (2 n/a)');
  });
  it('omits the suffix when every position is measurable', () => {
    expect(formatOpenR(1.5, 0)).toBe('+1.50R');
  });
});

describe('describeUnreachable', () => {
  it('returns null when reachable', () => {
    expect(describeUnreachable(false, '2026-07-12T05:00:00Z')).toBeNull();
  });
  it('names the last-good timestamp when unreachable', () => {
    expect(describeUnreachable(true, '2026-07-12T05:00:00Z')).toBe(
      'API unreachable — data is stale (last good: 05:00:00 UTC)',
    );
  });
  it('says "never" when there is no prior good fetch at all', () => {
    expect(describeUnreachable(true, null)).toBe('API unreachable — data is stale (last good: never)');
  });
});
