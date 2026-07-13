// Small shared UI atoms -- kept intentionally minimal (ND-1 ships one page).
// Component layout otherwise stays inline in JSX (NightDesk's convention,
// adopted for AlphaOS's own palette/type scale per the ND-1 plan doc).
import React from 'react';

// ND-visual: Badge moved to its own module (components/Badge.jsx) as part
// of the visual-fidelity component library -- re-exported here so every
// existing `import { Badge } from '../components/ui.jsx'` call site across
// the 7 views keeps working unchanged. See Badge.jsx's own docstring for
// the tone vocabulary and the legacy tone-name aliasing.
export { Badge, badgeTone } from './Badge.jsx';

// ND-6: Block moved to its own module (components/InstrumentBlock.jsx,
// formalized with a `footer` slot and the `tone="shadow"` treatment) as
// part of the ND-6 component library -- re-exported here under its
// original name so every existing `import { Block } from
// '../components/ui.jsx'` call site keeps working unchanged (same
// re-export pattern as Badge above).
export { InstrumentBlock as Block } from './InstrumentBlock.jsx';

// A plain link out to the Streamlit app -- ND-1 has zero write affordances,
// so every action-suggesting element in this console is one of these
// (docs/roadmap/console-migration-nd.md ND-1 scope).
export function StreamlitLink({ href, children }) {
  return (
    <a href={href} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>
      {children} ↗
    </a>
  );
}

// ND-2: a plain data table for list-shaped API payloads (Decisions/Learning/
// System pages -- Tonight/Positions/Approvals use card layout instead).
// `columns`: [{key, label, numeric?, render?(row) => node}]. Renders
// `row[key]` verbatim unless `render` is given -- no reshaping happens
// here, this is pure presentation (same "frontend computes nothing
// business-critical" rule as everywhere else in this app). A missing/null
// cell renders "—", never a blank cell that could be misread as "0" or
// "empty string" (unknown-never-zero).
export function DataTable({ columns, rows, emptyText = 'None.' }) {
  if (!rows || rows.length === 0) {
    return <div style={{ fontSize: 13, color: 'var(--text-dim)' }}>{emptyText}</div>;
  }
  return (
    <div className="dtable-wrap">
      <table className="dtable">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key}>{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={row.id ?? row.candidate_id ?? row.proposal_id ?? row.position_id ?? i}>
              {columns.map((c) => {
                const value = c.render ? c.render(row) : row[c.key];
                return (
                  <td key={c.key} className={c.numeric ? 'num' : undefined}>
                    {value === null || value === undefined || value === '' ? '—' : value}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
