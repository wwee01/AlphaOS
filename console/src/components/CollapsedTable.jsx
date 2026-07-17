// 2026-07-17: extracted from Decisions.jsx (originally built for the
// proposed/watch/rejected lists) so the Research tab's own "recent shadow
// captures" table can reuse the same collapse behavior rather than a copy.
import React, { useState } from 'react';
import { DataTable } from './ui.jsx';

const DEFAULT_COLLAPSED_ROWS = 8;

// DataTable wrapper that renders only the first `initial` rows until the
// operator expands it. The full count is ALWAYS visible in the control --
// collapsing is a view choice, never silent truncation.
export function CollapsedTable({ columns, rows, emptyText, initial = DEFAULT_COLLAPSED_ROWS }) {
  const [expanded, setExpanded] = useState(false);
  const all = rows ?? [];
  const visible = expanded ? all : all.slice(0, initial);
  return (
    <>
      <DataTable columns={columns} rows={visible} emptyText={emptyText} />
      {all.length > initial && (
        <button
          type="button"
          className="linklike"
          onClick={() => setExpanded((e) => !e)}
          style={{
            background: 'none', border: 'none', padding: '6px 0 0', cursor: 'pointer',
            fontSize: 12, color: 'var(--primary)',
          }}
        >
          {expanded ? '▴ show fewer' : `▾ show all ${all.length}`}
        </button>
      )}
    </>
  );
}
