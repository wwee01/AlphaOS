import { describe, expect, it } from 'vitest';
import {
  buildDecisionFunnelStages, formatHindsight, formatNarrative,
} from './decisions.js';

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

describe('buildDecisionFunnelStages', () => {
  it('prepends a "candidates" stage totalling every decision bucket (real API row shape: `decision`, not `label_decision`)', () => {
    const stages = buildDecisionFunnelStages([
      { decision: 'propose', n: 3 },
      { decision: 'watch', n: 6 },
      { decision: 'reject', n: 2 },
    ]);
    expect(stages[0]).toEqual({ label: 'candidates', value: 11 });
    expect(stages.slice(1)).toEqual([
      { label: 'propose', value: 3 },
      { label: 'watch', value: 6 },
      { label: 'reject', value: 2 },
    ]);
  });

  it('returns an empty array when there is no label data yet (never a fabricated stage)', () => {
    expect(buildDecisionFunnelStages([])).toEqual([]);
    expect(buildDecisionFunnelStages(undefined)).toEqual([]);
  });

  it('falls back to "unknown" for a missing decision field, never dropping the row silently', () => {
    const stages = buildDecisionFunnelStages([{ decision: null, n: 4 }]);
    expect(stages[1].label).toBe('unknown');
  });
});


describe('formatNarrative', () => {
  it('shows the polarity verdict when the LLM classified the narrative', () => {
    expect(formatNarrative({ polarity_label: 'bullish', sentiment_label: 'unknown' })).toBe('bullish');
    expect(formatNarrative({ polarity_label: 'unclear' })).toBe('unclear');
  });
  it('never shows the legacy sentiment_label hint (live CLI provider never sets it)', () => {
    expect(formatNarrative({ sentiment_label: 'bullish', last30days_status: 'available' }))
      .toBe('not classified');
  });
  it('distinguishes the three honest non-answers', () => {
    expect(formatNarrative({ last30days_status: 'available' })).toBe('not classified');
    expect(formatNarrative({ last30days_status: 'none_found' })).toBe('no narrative found');
    expect(formatNarrative({ last30days_status: 'unavailable' })).toBe('not researched');
    expect(formatNarrative({})).toBe('not researched');
  });
});
