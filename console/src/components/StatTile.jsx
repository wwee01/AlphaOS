// ND-6: the big-number "hero" primitive (design ruling §3.2/§3.4) -- each
// view has ONE number that matters most (Tonight -> open-R, Positions ->
// total open R, Learning -> resolved-N, ...); this renders it large,
// confident, mono, tabular -- the anchor the eye lands on first. Pure
// presentation: `value` is pre-formatted display text from the SAME
// formatter every plain-text rendering already used (format.js et al.) --
// this component computes nothing business-critical, it only gives the
// number weight and, when the underlying value changes between polls, a
// brief one-shot highlight (design ruling §3.5 "value-change highlight":
// <=600ms, decays to nothing, fires once per real change, never loops),
// gated on prefers-reduced-motion.
import React, { useEffect, useRef, useState } from 'react';

function prefersReducedMotion() {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

const TONE_COLOR = {
  primary: 'var(--primary)',
  danger: 'var(--red)',
  warning: 'var(--amber)',
  shadow: 'var(--shadow-tier)',
  neutral: 'var(--text)',
};

export function StatTile({
  label, value, unit, tone = 'primary', context, size = 'lg',
}) {
  const [flash, setFlash] = useState(false);
  const prevRef = useRef(value);
  const mountedOnceRef = useRef(false);

  useEffect(() => {
    // Skip the flash on first mount (a fresh view mount is already covered
    // by the page-load reveal -- this highlight is reserved for a REAL
    // change of an already-displayed value between polls).
    if (!mountedOnceRef.current) {
      mountedOnceRef.current = true;
      prevRef.current = value;
      return undefined;
    }
    if (prevRef.current !== value) {
      prevRef.current = value;
      if (!prefersReducedMotion()) {
        setFlash(true);
        const id = setTimeout(() => setFlash(false), 600);
        return () => clearTimeout(id);
      }
    }
    return undefined;
  }, [value]);

  return (
    <div className={`stat-tile${flash ? ' value-flash' : ''}`}>
      <div className="label-caps stat-tile-label">{label}</div>
      <div
        className={`num stat-tile-value stat-tile-${size}`}
        style={{ color: TONE_COLOR[tone] || TONE_COLOR.primary }}
      >
        {value}
        {unit ? <span className="stat-tile-unit">{unit}</span> : null}
      </div>
      {context ? <div className="stat-tile-context">{context}</div> : null}
    </div>
  );
}
