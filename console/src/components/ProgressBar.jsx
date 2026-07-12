// ND-visual: a thick filled progress track, replacing the ND-2 thin-line
// treatments (the former .r-ladder-track top-border + hanging ticks, and
// the 8px .ttl-bar-track) with the mockups' heavier bar weight. Pure
// presentation -- every caller passes an ALREADY-COMPUTED percentage from
// positions.js:computeRLadder() or approvals.js:computeTtlBar() (both
// pure, both still tested in positions.test.js/approvals.test.js,
// UNCHANGED by this pass); this component does no math beyond clamping a
// paint value, per this pass's ground rule "keep the exact same underlying
// math, only how it's drawn." No @keyframes/animation anywhere below --
// §13 calm-console rule: a fill/marker renders at a fixed position once
// per poll, never a client-side ticking transition.
import React from 'react';

const TONE_CLASS = {
  primary: 'pbar-track-primary',
  success: 'pbar-track-primary',
  warning: 'pbar-track-warning',
  danger: 'pbar-track-danger',
  neutral: 'pbar-track-neutral',
};

// `pct`: 0-100 fill width (null/undefined clamps to 0 -- callers needing an
//   "unavailable" state render their own fallback text instead of calling
//   this at all, same as the ND-2 components did).
// `marks`: optional [{ name, pct, value, label, color }] -- tick marks
//   drawn on top of the track (the R-ladder's stop/entry/target).
// `marker`: optional { pct, label, title } -- a single highlighted point
//   (the R-ladder's current-price dot).
// `withLabels`: reserves vertical space above/below the track for
//   marks'/marker's text -- off for the plain TTL bar (no marks).
export function ProgressBar({
  pct, tone = 'primary', height = 10, marks, marker, withLabels = false,
}) {
  const clamped = pct === null || pct === undefined || Number.isNaN(pct)
    ? 0 : Math.max(0, Math.min(100, pct));
  const track = (
    <div className={`pbar-track ${TONE_CLASS[tone] || TONE_CLASS.primary}`} style={{ height }}>
      <div className="pbar-fill" style={{ width: `${clamped}%` }} />
      {(marks ?? []).map((m) => (
        <div
          key={m.name}
          className="pbar-mark"
          style={{ left: `${m.pct}%`, borderLeftColor: m.color || 'var(--text-dim)' }}
          title={`${m.name} ${m.value ?? ''}`}
        >
          {m.label && <span className="pbar-mark-label label-caps">{m.label}</span>}
          {m.value !== undefined && m.value !== null && <span className="pbar-mark-value num">{m.value}</span>}
        </div>
      ))}
      {marker && (
        <div className="pbar-marker" style={{ left: `${marker.pct}%` }} title={marker.title}>
          {marker.label && <span className="pbar-marker-label num">{marker.label}</span>}
        </div>
      )}
    </div>
  );
  return withLabels ? <div className="pbar-wrap">{track}</div> : track;
}
