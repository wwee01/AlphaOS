// ND-6: the candidates -> proposed -> blocked -> rejected attrition viz
// (design ruling §3.4/§5 Decisions) -- horizontal proportional bars fed by
// real counts (funnel.js:computeFunnelStages, pure and tested), never a
// bare count table. `stages`: [{ label, value, tone? }].
import React from 'react';
import { computeFunnelStages } from '../funnel.js';

const TONE_COLOR = {
  primary: 'var(--primary)',
  warning: 'var(--amber)',
  danger: 'var(--red)',
  neutral: 'var(--text-dim)',
};

export function Funnel({ stages }) {
  const computed = computeFunnelStages(stages);
  return (
    <div className="funnel">
      {computed.map((s) => (
        <div className="funnel-row" key={s.label}>
          <div className="funnel-row-label label-caps">{s.label}</div>
          <div className="funnel-row-track">
            {s.pct === null ? (
              <div className="funnel-row-unknown">n/a</div>
            ) : (
              <div
                className="funnel-row-fill"
                style={{ width: `${Math.max(s.pct, s.value > 0 ? 2 : 0)}%`, background: TONE_COLOR[s.tone] || TONE_COLOR.primary }}
              />
            )}
          </div>
          <div className="num funnel-row-value">{s.value === null || s.value === undefined ? 'n/a' : s.value}</div>
        </div>
      ))}
    </div>
  );
}
