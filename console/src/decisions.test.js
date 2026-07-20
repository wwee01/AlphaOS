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
  // 2026-07-17: counts the ACTUAL decision arrays (the same ones that render
  // the tables), NOT label_summary.by_label_decision -- so every bar equals
  // its table below. filled = open_trades + closed_trades.
  const sample = {
    proposed: [{}, {}, {}, {}, {}], // 5
    watch: new Array(24).fill({}), // 24
    rejected: new Array(32).fill({}), // 32
    blocked: [], // 0
    open_trades: [], // 0
    closed_trades: [{}, {}], // 2 => filled
  };

  it('builds one stage per decision table, each value = that table length, in table order', () => {
    expect(buildDecisionFunnelStages(sample)).toEqual([
      { label: 'proposed', value: 5 },
      { label: 'watch', value: 24 },
      { label: 'rejected', value: 32 },
      { label: 'blocked', value: 0 },
      { label: 'filled', value: 2 },
    ]);
  });

  it('sums open + closed positions into the single "filled" stage', () => {
    const stages = buildDecisionFunnelStages({ ...sample, open_trades: [{}, {}, {}], closed_trades: [{}] });
    expect(stages.find((s) => s.label === 'filled').value).toBe(4);
  });

  it('never fabricates the old synthetic "candidates = sum" total stage', () => {
    const labels = buildDecisionFunnelStages(sample).map((s) => s.label);
    expect(labels).not.toContain('candidates');
  });

  it('returns an empty array when every stage is zero (fresh journal) or data is absent', () => {
    expect(buildDecisionFunnelStages(undefined)).toEqual([]);
    expect(buildDecisionFunnelStages({})).toEqual([]);
    expect(buildDecisionFunnelStages({
      proposed: [], watch: [], rejected: [], blocked: [], open_trades: [], closed_trades: [],
    })).toEqual([]);
  });

  it('treats missing arrays as zero, never a crash (unknown-never-throw)', () => {
    const stages = buildDecisionFunnelStages({ proposed: [{}], watch: null });
    expect(stages.find((s) => s.label === 'proposed').value).toBe(1);
    expect(stages.find((s) => s.label === 'watch').value).toBe(0);
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
