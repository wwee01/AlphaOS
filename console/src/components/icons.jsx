// ND-visual: a small hand-authored inline-SVG icon set (ground rule #4 --
// "no CDN fonts, no icon-font CDN... inline SVG... no <link> to any
// external service, ever"). Every path below is original simple geometry
// (lines/circles/basic paths) sized on a 24x24 viewBox, stroke="currentColor"
// so each icon inherits whatever text color its wrapping element (often a
// Badge) already has -- no icon-specific color prop needed, no risk of an
// icon and its adjacent status text disagreeing on tone.
//
// Kept deliberately minimal: only the icons the 7 views actually use
// (verified against the mockups' icon PLACEMENT, not their exact glyphs --
// per this pass's quarantine-the-script rule, nothing here was traced or
// copied from the Stitch mockup assets or from any third-party icon
// library's path data).
import React from 'react';

function Svg({ size = 14, style, children, ...rest }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ display: 'inline-block', flex: 'none', verticalAlign: '-2px', ...style }}
      aria-hidden="true"
      {...rest}
    >
      {children}
    </svg>
  );
}

// Long / upward direction.
export function IconArrowUpRight(props) {
  return (
    <Svg {...props}>
      <path d="M7 17 17 7" />
      <path d="M9 7h8v8" />
    </Svg>
  );
}

// Short / downward direction.
export function IconArrowDownRight(props) {
  return (
    <Svg {...props}>
      <path d="M7 7 17 17" />
      <path d="M9 17h8V9" />
    </Svg>
  );
}

// Protection / safety state (protection watchdog).
export function IconShield(props) {
  return (
    <Svg {...props}>
      <path d="M12 3l7 3v5.5c0 4.7-3 8-7 9.5-4-1.5-7-4.8-7-9.5V6l7-3z" />
    </Svg>
  );
}

// Affirmative / resolved / all-clear.
export function IconCheck(props) {
  return (
    <Svg {...props}>
      <path d="M5 12.5 10 17 19 7" />
    </Svg>
  );
}

// Alert / needs-attention / incident.
export function IconWarningTriangle(props) {
  return (
    <Svg {...props}>
      <path d="M12 3 2 20h20L12 3z" />
      <line x1="12" y1="10" x2="12" y2="14.5" />
      <line x1="12" y1="17" x2="12" y2="17" />
    </Svg>
  );
}

// TTL / time remaining.
export function IconClock(props) {
  return (
    <Svg {...props}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3.2 3.2" />
    </Svg>
  );
}

// -- masthead nav accents (one per view, small/decorative) --------------

export function IconMoon(props) {
  return (
    <Svg {...props}>
      <path d="M20.5 13.4A8.5 8.5 0 1110.6 3.5a6.7 6.7 0 009.9 9.9z" />
    </Svg>
  );
}

export function IconBars(props) {
  return (
    <Svg {...props}>
      <line x1="5" y1="20" x2="5" y2="11" />
      <line x1="11" y1="20" x2="11" y2="5" />
      <line x1="17" y1="20" x2="17" y2="14" />
    </Svg>
  );
}

export function IconCheckShield(props) {
  return (
    <Svg {...props}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M8.2 12.3 10.7 15 15.8 9" />
    </Svg>
  );
}

export function IconFunnel(props) {
  return (
    <Svg {...props}>
      <path d="M4 4.5h16l-6 7.2v6.3l-4 2v-8.3L4 4.5z" />
    </Svg>
  );
}

export function IconBook(props) {
  return (
    <Svg {...props}>
      <path d="M12 6.3c-2-1.4-4.8-1.8-7.5-1v13c2.7-.8 5.5-.4 7.5 1 2-1.4 4.8-1.8 7.5-1v-13c-2.7-.8-5.5-.4-7.5 1z" />
      <line x1="12" y1="6.3" x2="12" y2="19.3" />
    </Svg>
  );
}

// ND-6: masthead mobile "expand full annunciator" toggle chevron.
export function IconChevronDown(props) {
  return (
    <Svg {...props}>
      <path d="M5 8.5 12 15.5 19 8.5" />
    </Svg>
  );
}

export function IconGear(props) {
  return (
    <Svg {...props}>
      <circle cx="12" cy="12" r="3" />
      <circle cx="12" cy="12" r="8" />
      <line x1="12" y1="1.5" x2="12" y2="4.5" />
      <line x1="12" y1="19.5" x2="12" y2="22.5" />
      <line x1="1.5" y1="12" x2="4.5" y2="12" />
      <line x1="19.5" y1="12" x2="22.5" y2="12" />
    </Svg>
  );
}
