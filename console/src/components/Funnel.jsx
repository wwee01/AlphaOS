// ND-6: the candidates -> proposed -> blocked -> rejected attrition viz
// (design ruling §3.4/§5 Decisions) -- horizontal proportional bars fed by
// real counts (funnel.js:computeFunnelStages, pure and tested), never a
// bare count table. `stages`: [{ label, value, tone? }].
//
// ND-7 (design ruling §4.3): gradient bars (violet->cyan) with a soft glow
// for the "alive" stages, dimmed violet for rejected/blocked -- ported from
// the approved mockup's `.fbar`/`.fbar.dim`. decisions.js:
// buildDecisionFunnelStages() never attaches a `tone` field (verified --
// its rows are plain `{label, value}`), so the alive/dim split is decided
// here, from the SAME `label` string already rendered in the row (pure
// presentation, no new data/business decision -- an explicit `tone` on a
// stage, if a future caller ever sets one, still wins over the label
// heuristic).
import React from 'react';
import { computeFunnelStages } from '../funnel.js';

const DIM_TONES = new Set(['danger', 'neutral']);
const DIM_LABEL_RE = /reject|block/i;

function isDimStage(s) {
  if (s.tone) return DIM_TONES.has(s.tone);
  return DIM_LABEL_RE.test(s.label ?? '');
}

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
                className={`funnel-row-fill${isDimStage(s) ? ' funnel-row-fill-dim' : ''}`}
                style={{ width: `${Math.max(s.pct, s.value > 0 ? 2 : 0)}%` }}
              />
            )}
          </div>
          <div className="num funnel-row-value">{s.value === null || s.value === undefined ? 'n/a' : s.value}</div>
        </div>
      ))}
    </div>
  );
}
