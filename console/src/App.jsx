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
import Research from './pages/Research.jsx';
import Governance from './pages/Governance.jsx';
import System from './pages/System.jsx';
import Masthead from './components/Masthead.jsx';
import { ErrorBoundary } from './components/ErrorBoundary.jsx';
import {
  IconBars, IconBook, IconCheckShield, IconFlask, IconFunnel, IconGear, IconMoon, IconShield,
} from './components/icons.jsx';

// 2026-07-17: Research sits between Learning and Governance -- Learning's
// sibling (measurement-only, zero-decision-surface), split out of Decisions
// so shadow-tier research data never shares a page with live trade
// decisions again (Fable 5 strategic consult, same day).
const VIEWS = [
  { key: 'tonight', label: 'Tonight', Icon: IconMoon, Component: Tonight },
  { key: 'positions', label: 'Positions', Icon: IconBars, Component: Positions },
  { key: 'approvals', label: 'Approvals', Icon: IconCheckShield, Component: Approvals },
  { key: 'decisions', label: 'Decisions', Icon: IconFunnel, Component: Decisions },
  { key: 'learning', label: 'Learning', Icon: IconBook, Component: Learning },
  { key: 'research', label: 'Research', Icon: IconFlask, Component: Research },
  { key: 'governance', label: 'Autonomy & Risk', Icon: IconShield, Component: Governance },
  { key: 'system', label: 'System & Audit', Icon: IconGear, Component: System },
];

export default function App() {
  const [view, setView] = useState('tonight');
  const active = VIEWS.find((v) => v.key === view) ?? VIEWS[0];
  const ActiveComponent = active.Component;

  return (
    // ND-7: the aurora app shell (design ruling §4.1) -- the living sky (3
    // drifting blobs, ruling §2b's exact motion bounds), a fine grain, and a
    // darkening scrim, rendered ONCE here, behind everything, inert to
    // pointer/AT (aria-hidden + pointer-events:none in styles.css). `.shell`
    // carries the same centered/padded content `#root` used to (ND-6's
    // widened 1600px Mac-mini canvas, unchanged).
    <div className="aurora-root">
      <div className="sky" aria-hidden="true">
        <span className="blob b1" />
        <span className="blob b2" />
        <span className="blob b3" />
      </div>
      <div className="grain" aria-hidden="true" />
      <div className="scrim" aria-hidden="true" />

      <div className="shell">
        {/* ND-6: the masthead -- wordmark + live clock, the annunciator (mode/
            kill-switch as primary lamps, everything else secondary -- design
            ruling §4), and the nav tab strip (desktop top rail + mobile
            bottom tab bar), composed in components/Masthead.jsx. */}
        <Masthead views={VIEWS} activeKey={view} onSelect={setView} />

        {/* Each page's own `.grid` (or card list) carries the
            `reveal-stagger` class (design ruling §3.5) -- remounting via
            `key={active.key}` on every tab switch replays the one-shot
            page-load reveal once per switch, never continuously. */}
        <ErrorBoundary key={active.key}>
          <ActiveComponent />
        </ErrorBoundary>
      </div>
    </div>
  );
}
