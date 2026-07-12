// ND-2: the full 7-view IA (docs/roadmap/console-migration-nd.md ND-2
// scope), navigated by a plain useState-based tab strip -- no router
// library, matching this project's "don't over-engineer" house style (the
// same choice System.jsx makes internally for its own sub-views). Tab
// ORDER puts Tonight/Positions/Approvals first, matching the mobile IA
// priority subset (UX doc §16 item 2: "the four operating surfaces... lead,
// everything else remains reachable but secondary" -- same tabs, same code,
// same gates at every width, mobile is a viewport, never a fork).
import React, { useState } from 'react';
import Tonight from './pages/Tonight.jsx';
import Positions from './pages/Positions.jsx';
import Approvals from './pages/Approvals.jsx';
import Decisions from './pages/Decisions.jsx';
import Learning from './pages/Learning.jsx';
import Governance from './pages/Governance.jsx';
import System from './pages/System.jsx';
import { ErrorBoundary } from './components/ErrorBoundary.jsx';

const VIEWS = [
  { key: 'tonight', label: 'Tonight', Component: Tonight },
  { key: 'positions', label: 'Positions', Component: Positions },
  { key: 'approvals', label: 'Approvals', Component: Approvals },
  { key: 'decisions', label: 'Decisions', Component: Decisions },
  { key: 'learning', label: 'Learning', Component: Learning },
  { key: 'governance', label: 'Autonomy & Risk', Component: Governance },
  { key: 'system', label: 'System & Audit', Component: System },
];

export default function App() {
  const [view, setView] = useState('tonight');
  const active = VIEWS.find((v) => v.key === view) ?? VIEWS[0];
  const ActiveComponent = active.Component;

  return (
    <>
      <header
        style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
          marginBottom: 20, paddingBottom: 12, borderBottom: '1px solid var(--border)',
        }}
      >
        <div className="num" style={{ fontSize: 18, fontWeight: 700, letterSpacing: '0.08em', color: 'var(--primary)' }}>
          ALPHAOS
        </div>
        <div className="label-caps">console · ND-2</div>
      </header>

      <nav className="nav-tabs">
        {VIEWS.map((v) => (
          <button
            key={v.key}
            type="button"
            className={`nav-tab${v.key === view ? ' nav-tab-active' : ''}`}
            onClick={() => setView(v.key)}
          >
            {v.label}
          </button>
        ))}
      </nav>

      <ErrorBoundary key={active.key}>
        <ActiveComponent />
      </ErrorBoundary>
    </>
  );
}
