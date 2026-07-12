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
import Annunciator from './components/Annunciator.jsx';
import { ErrorBoundary } from './components/ErrorBoundary.jsx';
import {
  IconBars, IconBook, IconCheckShield, IconFunnel, IconGear, IconMoon, IconShield,
} from './components/icons.jsx';

const VIEWS = [
  { key: 'tonight', label: 'Tonight', Icon: IconMoon, Component: Tonight },
  { key: 'positions', label: 'Positions', Icon: IconBars, Component: Positions },
  { key: 'approvals', label: 'Approvals', Icon: IconCheckShield, Component: Approvals },
  { key: 'decisions', label: 'Decisions', Icon: IconFunnel, Component: Decisions },
  { key: 'learning', label: 'Learning', Icon: IconBook, Component: Learning },
  { key: 'governance', label: 'Autonomy & Risk', Icon: IconShield, Component: Governance },
  { key: 'system', label: 'System & Audit', Icon: IconGear, Component: System },
];

export default function App() {
  const [view, setView] = useState('tonight');
  const active = VIEWS.find((v) => v.key === view) ?? VIEWS[0];
  const ActiveComponent = active.Component;

  return (
    <>
      {/* ND-visual: the masthead -- wordmark + the ND-3 global annunciator
          (restyled in place below, not refetched -- see Annunciator.jsx's
          own docstring) + the nav tab strip, wrapped as one visually
          cohesive top bar instead of ND-2's bare flex header. */}
      <header className="masthead">
        <div className="masthead-top">
          <div className="masthead-brand">
            <span className="wordmark">ALPHAOS</span>
            <span className="label-caps">console · ND-3</span>
          </div>
        </div>

        <Annunciator />

        <nav className="nav-tabs">
          {VIEWS.map((v) => (
            <button
              key={v.key}
              type="button"
              className={`nav-tab${v.key === view ? ' nav-tab-active' : ''}`}
              onClick={() => setView(v.key)}
            >
              <v.Icon size={13} />
              {v.label}
            </button>
          ))}
        </nav>
      </header>

      <ErrorBoundary key={active.key}>
        <ActiveComponent />
      </ErrorBoundary>
    </>
  );
}
