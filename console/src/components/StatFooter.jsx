// ND-visual: a small N-column paired-stat row (the mockups' "label over
// value" stat-block treatment) -- used wherever this console already
// renders a run of small labelled numbers as an inline plain-text/`·`
// separated string (Positions' distance-to-stop/target and protection/
// freshness/trading-days caption, Tonight's today's-activity counts,
// Decisions' closed-trade aggregate metrics, Learning's TQS/attribution
// summary counts). Pure presentation: `stats` is built by the caller from
// the SAME fields that were already being interpolated into the old plain-
// text string -- no new value, no recomputation, only the JSX wrapper/CSS
// changes, per this pass's ground rule.
import React from 'react';

const TONE_COLOR = {
  primary: 'var(--primary)',
  danger: 'var(--red)',
  warning: 'var(--amber)',
  neutral: 'var(--text-dim)',
};

// `stats`: [{ label, value, tone? }] -- `tone` is optional and, when given,
// only recolors the VALUE text (primary/danger/warning/neutral); omitted
// tone renders in the default text color, same as the plain text it
// replaces.
export function StatFooter({ stats }) {
  if (!stats || stats.length === 0) return null;
  return (
    <div className="stat-footer">
      {stats.map((s, i) => (
        <div className="stat-footer-item" key={`${s.label}_${i}`}>
          <div className="label-caps stat-footer-label">{s.label}</div>
          <div
            className="num stat-footer-value"
            style={s.tone ? { color: TONE_COLOR[s.tone] || undefined } : undefined}
          >
            {s.value}
          </div>
        </div>
      ))}
    </div>
  );
}
