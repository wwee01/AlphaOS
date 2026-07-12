// Small shared UI atoms -- kept intentionally minimal (ND-1 ships one page).
// Component layout otherwise stays inline in JSX (NightDesk's convention,
// adopted for AlphaOS's own palette/type scale per the ND-1 plan doc).
import React from 'react';

// An "instrument block" -- a bordered, labelled panel (docs/roadmap/ported/
// stitch-design-tokens.md: "Instrument Blocks -- self-contained modules that
// behave like physical rack-mounted hardware").
export function Block({ title, right, children, style }) {
  return (
    <div className="block" style={style}>
      {(title || right) && (
        <div className="block-title">
          {title ? <span className="label-caps">{title}</span> : <span />}
          {right}
        </div>
      )}
      {children}
    </div>
  );
}

// A static status badge. Deliberately no @keyframes/animation prop --
// §13 calm-console rule (no flashing/pulsing), carried into ND-1 by the
// plan doc §2.4.
export function Badge({ tone = 'default', children }) {
  const cls = {
    default: 'badge',
    ok: 'badge badge-ok',
    warn: 'badge badge-warn',
    danger: 'badge badge-danger',
  }[tone] || 'badge';
  return <span className={cls}>{children}</span>;
}

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
