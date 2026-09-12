import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { CollectionStatus, ExperimentRegistry } from './ResearchStatusPanels.jsx';

describe('research evidence visibility', () => {
  it('renders the negative verdict and frozen interval beside the positive claim', () => {
    const html = renderToStaticMarkup(<ExperimentRegistry report={{
      registered_count: 15, evaluated_count: 3, superseded_count: 2,
      caveat: 'Historical methods require review.', experiments: [{
        prereg_id: 'p1', hypothesis_id: 'H-TQS-1', claim: 'Predicts +0.3R', metric: 'centered delta',
        state: 'evaluated', analysis_not_before: '2026-08-21', verdict: 'rejected',
        point_estimate: -.415, ci_low: -.745, ci_high: -.134, ci_level: .9, q_value: 1,
        floor_effective_n: 40, floor_span_days: 42, validity_context: 'Reference uncertainty needs review.',
      }],
    }} />);
    for (const text of ['15 registrations', 'Predicts +0.3R', 'rejected', '-0.4150', '-0.7450', '-0.1340', 'Reference uncertainty']) expect(html).toContain(text);
  });

  it('never presents an enabled but suspended collector as working', () => {
    const html = renderToStaticMarkup(<CollectionStatus collection={{
      shadow: { enabled: true, state: 'suspended', suspended: true, successful_labels: 0,
        suspension_reason: 'Historical latch', current_checks: {}, note: 'No automatic clear.' },
      last30days: {enabled: true, attempts: 115, errors: 56, timeouts: 56, window_days: 7},
      text_archive: {enabled: false, documents: 0}, regime: {enabled: true, days: 0},
    }} />);
    for (const text of ['suspended', 'Historical latch', '0 successful real labels', 'last success never', '56 errors / 115', 'disabled', '0 documents']) expect(html).toContain(text);
    expect(html).not.toContain('healthy');
  });
});
