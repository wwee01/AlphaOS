// ND-6: a tiny inline-SVG line chart (design ruling §3.4), used ONLY where
// real series data exists (e.g. System's scan-batch cadence). No fabricated
// series ever -- sparkline.js:computeSparklinePoints (pure, tested) returns
// null when fewer than 2 real points exist, and this component renders an
// honest text fallback in that case rather than a flat line implying a
// measured zero (hard constraint #4, unknown-never-zero extended to
// charts). Informative, not decorative, so it gets a role/aria-label rather
// than aria-hidden.
import React from 'react';
import { computeSparklinePoints } from '../sparkline.js';

const STROKE = {
  primary: 'var(--primary)',
  warning: 'var(--amber)',
  shadow: 'var(--shadow-tier)',
};

// ND-7 (design ruling §4.3): a soft glow on the line, ported from the
// mockup's `.spark i { box-shadow: 0 0 10px -2px var(--cy) }` treatment,
// adapted to this component's real inline-SVG line (not the mockup's
// illustrative bars -- only its visual language is ported, never its data).
const GLOW = {
  primary: 'drop-shadow(0 0 3px rgba(91, 227, 214, 0.6))',
  warning: 'drop-shadow(0 0 3px rgba(255, 194, 75, 0.5))',
  shadow: 'drop-shadow(0 0 3px rgba(143, 139, 224, 0.45))',
};

export function Sparkline({
  values, width = 120, height = 32, tone = 'primary', label = 'trend',
}) {
  const points = computeSparklinePoints(values, width, height);
  if (!points) {
    return <div className="sparkline-unavailable">no series data yet</div>;
  }
  const path = points.map((p) => `${p.x},${p.y}`).join(' ');
  const stroke = STROKE[tone] || STROKE.primary;
  const glow = GLOW[tone] || GLOW.primary;
  const last = points[points.length - 1];
  return (
    <svg
      className="sparkline"
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={label}
      style={{ filter: glow }}
    >
      <polyline points={path} fill="none" stroke={stroke} strokeWidth="1.75" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={last.x} cy={last.y} r="2" fill={stroke} />
    </svg>
  );
}
