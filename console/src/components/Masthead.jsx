// ND-6: the masthead (design ruling §4) -- wordmark + live UTC clock, the
// annunciator (mode/kill-switch as primary lamps, everything else as
// secondary chips -- see Annunciator.jsx), and the nav tab strip, all in
// one sticky top bar. Owns the ONE useAnnunciator() poller for this whole
// bar (both the mobile condensed summary below and the full Annunciator
// strip read from it -- no duplicate polling, no duplicate kill-switch
// control).
//
// Mobile (design ruling §4/§6): collapses to wordmark + a kill-switch-state
// chip + a "status" toggle that expands the SAME full Annunciator strip
// (with its one kill-switch control) inline -- never a second, simplified
// control. The bottom tab bar (nav-tabs-bottom, CSS-only visibility swap
// with the top nav-tabs-top) is rendered here too since App.jsx composes
// the whole top-of-tree chrome from this one component.
import React, { useEffect, useState } from 'react';
import Annunciator from './Annunciator.jsx';
import { useAnnunciator } from '../hooks/useAnnunciator.js';
import { Badge } from './ui.jsx';
import { IconChevronDown, IconShield, IconWarningTriangle } from './icons.jsx';

function useUtcClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return `${now.toISOString().substring(11, 19)} UTC`;
}

export default function Masthead({ views, activeKey, onSelect }) {
  const [data, poll] = useAnnunciator();
  const [expanded, setExpanded] = useState(false);
  const clock = useUtcClock();

  return (
    <>
      <header className="masthead">
        <div className="masthead-top">
          <div className="masthead-brand">
            <span className="wordmark">ALPHAOS</span>
            <span className="masthead-clock num">{clock}</span>
          </div>

          {/* Mobile-only condensed kill-switch-state chip + expand toggle
              (hidden on desktop by .masthead-expand-toggle's own CSS rule;
              the chip itself is harmless to render at every width, but only
              carries information the full strip below doesn't already show
              at desktop widths, so it's suppressed there too via the same
              breakpoint through the toggle button wrapping it). */}
          <button
            type="button"
            className="masthead-expand-toggle badge"
            onClick={() => setExpanded((e) => !e)}
            aria-expanded={expanded}
          >
            {data ? (
              <Badge tone={data.kill_switch_engaged ? 'danger' : 'ok'} className="lamp" style={{ border: 'none', background: 'none', padding: 0 }}>
                {data.kill_switch_engaged ? <IconWarningTriangle size={12} /> : <IconShield size={12} />}
                {data.kill_switch_engaged ? 'ENGAGED' : 'armed'}
              </Badge>
            ) : (
              <span className="label-caps">status</span>
            )}
            <IconChevronDown size={12} style={{ transform: expanded ? 'rotate(180deg)' : 'none' }} />
          </button>
        </div>

        <div className={`annunciator-shell${expanded ? ' annunciator-shell-expanded' : ''}`}>
          <Annunciator data={data} poll={poll} />
        </div>

        <nav className="nav-tabs nav-tabs-top">
          {views.map((v) => (
            <button
              key={v.key}
              type="button"
              className={`nav-tab${v.key === activeKey ? ' nav-tab-active' : ''}`}
              onClick={() => onSelect(v.key)}
            >
              <v.Icon size={13} />
              {v.label}
            </button>
          ))}
        </nav>
      </header>

      {/* Mobile bottom tab bar -- thumb-reachable, scrollable (design
          ruling §6). CSS hides this entirely at >=769px. */}
      <nav className="nav-tabs-bottom" aria-label="views">
        {views.map((v) => (
          <button
            key={v.key}
            type="button"
            className={`nav-tab-bottom${v.key === activeKey ? ' nav-tab-active' : ''}`}
            onClick={() => onSelect(v.key)}
          >
            <v.Icon size={18} />
            {v.label}
          </button>
        ))}
      </nav>
    </>
  );
}
