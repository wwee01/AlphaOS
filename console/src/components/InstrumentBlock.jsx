// ND-6: the formalized "instrument block" primitive (design ruling §3.4) --
// a bordered, titled panel with an optional right-aligned status chip and
// an optional footer row, the single brick every view composes from. This
// SUPERSEDES ui.jsx's inline Block implementation -- ui.jsx now re-exports
// this one, so every existing `import { Block } from '../components/ui.jsx'`
// call site keeps working unchanged (same re-export pattern Badge.jsx
// established for ui.jsx's Badge).
//
// `tone="shadow"` applies the shadow-tier treatment (design ruling §3.4
// "ShadowChip / shadow treatment", hard constraint #5): a dim indigo
// border/tint plus a small "SHADOW" tag in the title row, so a measurement-
// only surface (TQS/attribution/hypotheses/canary) is never visually
// confused with a live control or value. Never applied to a control surface
// (Approvals' action buttons, the masthead/annunciator).
//
// ND-7: `tone="lit"` applies the aurora "lit" glass variant (design ruling
// §4.2) -- cyan-tinted border + outer glow. Reserved for exactly ONE panel
// per view (Tonight's one-action hero, Approvals' soonest-TTL card,
// otherwise none) -- callers decide which instance qualifies, this
// component only renders whichever tone it's told.
//
// `reveal` opts a block into the one-shot page-load stagger reveal (design
// ruling §3.5) -- left off by default so e.g. a block that mounts well
// after first paint (a lazily-revealed sub-panel) doesn't replay the
// animation out of context; every top-level view block passes it.
import React from 'react';

export function InstrumentBlock({
  title, right, children, footer, tone, style, className, reveal = false,
}) {
  const classes = [
    'block',
    tone === 'shadow' ? 'block-shadow' : '',
    tone === 'lit' ? 'block-lit' : '',
    reveal ? 'reveal' : '',
    className,
  ].filter(Boolean).join(' ');

  return (
    <div className={classes} style={style}>
      {(title || right || tone === 'shadow') && (
        <div className="block-title">
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
            {title ? <span className="label-caps">{title}</span> : <span />}
            {tone === 'shadow' && <span className="shadow-tag">shadow</span>}
          </span>
          {right}
        </div>
      )}
      {children}
      {footer && <div className="block-footer">{footer}</div>}
    </div>
  );
}
