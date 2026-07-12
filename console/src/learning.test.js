import { describe, expect, it } from 'vitest';
import { formatAttributionRow, formatHypothesisProgress, formatHypothesisStatus } from './learning.js';

describe('formatAttributionRow (reporting-law floor gate)', () => {
  it('shows mean/sum ΔR only when the aggregate cleared its sample floor (status "ok")', () => {
    const agg = {
      status: 'ok', effective_n: 42, span_days: 30, mean_delta_r: 0.31, sum_delta_r: 13.02,
    };
    const row = formatAttributionRow('candidate / openai', agg, 20, 14);
    expect(row.meanDeltaR).toBe(0.31);
    expect(row.sumDeltaR).toBe(13.02);
    expect(row.status).toBe('ok');
  });

  it('withholds mean/sum ΔR below the sample floor, never a fabricated or approximate number', () => {
    const agg = {
      status: 'below_sample_floor', effective_n: 3, span_days: 2, mean_delta_r: null, sum_delta_r: null,
    };
    const row = formatAttributionRow('candidate / openai', agg, 20, 14);
    expect(row.meanDeltaR).toBeNull();
    expect(row.sumDeltaR).toBeNull();
    expect(row.status).toBe('n=3/20 below floor — counts only (needs ≥14d span)');
  });

  it('falls back to resolved_count when effective_n is absent (the execution-gap empty-case shape)', () => {
    const agg = { status: 'below_sample_floor', resolved_count: 0, span_days: null };
    const row = formatAttributionRow('execution_delta_r', agg, 20, 14);
    expect(row.n).toBe(0);
    expect(row.meanDeltaR).toBeNull();
  });
});

describe('formatHypothesisStatus', () => {
  it('tags every operator-ruled terminal status, never implying AlphaOS judged its own hypothesis', () => {
    expect(formatHypothesisStatus('met')).toBe('met (operator ruling)');
    expect(formatHypothesisStatus('failed')).toBe('failed (operator ruling)');
    expect(formatHypothesisStatus('withdrawn')).toBe('withdrawn (operator ruling)');
  });
  it('passes through a non-terminal mechanical status unchanged', () => {
    expect(formatHypothesisStatus('testing')).toBe('testing');
    expect(formatHypothesisStatus('proposed')).toBe('proposed');
  });
});

describe('formatHypothesisProgress', () => {
  it('renders an em dash when there is no progress yet', () => {
    expect(formatHypothesisProgress(null)).toBe('—');
  });
  it('renders "n/a" for an unknown span, never "0" (unknown-never-zero)', () => {
    const progress = {
      effective_n: 5, floor_effective_n: 20, span_days: null, floor_span_days: 14, clears_floor: false,
    };
    expect(formatHypothesisProgress(progress)).toBe('n=5/20 · span=n/a/14d · below floor');
  });
  it('reads resolver-ready when the floor is cleared and the analysis date has arrived', () => {
    const progress = {
      effective_n: 25, floor_effective_n: 20, span_days: 30, floor_span_days: 14,
      clears_floor: true, resolver_ready: true,
    };
    expect(formatHypothesisProgress(progress)).toBe('n=25/20 · span=30/14d · ✓ resolver-ready');
  });
  it('distinguishes "floor met, awaiting date" from full resolver-ready', () => {
    const progress = {
      effective_n: 25, floor_effective_n: 20, span_days: 30, floor_span_days: 14,
      clears_floor: true, resolver_ready: false,
    };
    expect(formatHypothesisProgress(progress)).toBe('n=25/20 · span=30/14d · ✓ data floor met · awaiting analysis date');
  });
});
