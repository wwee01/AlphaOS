// ND-1 pure display-formatting helpers -- no DOM, no React, so they're
// testable in isolation with vitest. Mirrors the exact formatting
// conventions alphaos/dashboard/streamlit_app.py already uses for the same
// values (_format_age, _format_seconds_remaining, render_annunciator's own
// r_label construction) -- the API returns raw numbers/ISO timestamps, and
// this module is the ONLY place that turns them into display text (ND-1
// plan doc §1: "the frontend computes nothing business-critical, ever; it
// formats and displays").
//
// Unknown-never-zero (§2.5) governs every function here: a `null`/
// `undefined` input always renders an explicit "n/a"/"unknown"/"no runs
// yet" word, never a fabricated "0" or empty string that could be misread
// as a real zero value.

// An R-multiple, e.g. "+1.23R" / "-0.40R". null (unmeasurable) -> "n/a".
export function formatR(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return 'n/a';
  const sign = value >= 0 ? '+' : '';
  return `${sign}${value.toFixed(2)}R`;
}

// Age-in-seconds -> short human string. Mirrors streamlit_app._format_age()
// exactly: <60s -> "Ns", <3600s -> "Nm", else "N.Nh". null -> "unknown".
export function formatAge(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return 'unknown';
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

// The annunciator's heartbeat phrase -- mirrors render_annunciator()'s own
// `hb_label` construction in streamlit_app.py verbatim ("no runs yet" when
// unknown, "<age> ago" otherwise -- not just "unknown ago").
export function formatHeartbeat(seconds) {
  if (seconds === null || seconds === undefined) return 'no runs yet';
  return `${formatAge(seconds)} ago`;
}

// A proposal's TTL countdown. Mirrors streamlit_app._format_seconds_remaining()
// exactly: null -> "unknown", <=0 -> "expired Ns ago", else "Mm Ss".
export function formatSecondsRemaining(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return 'unknown';
  if (seconds <= 0) return `expired ${Math.floor(Math.abs(seconds))}s ago`;
  const total = Math.floor(seconds);
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  return `${minutes}m ${secs}s`;
}

// HH:MM:SS in UTC -- matches the API's own `as_of` convention (every
// backend timestamp in this app is UTC ISO). Deterministic regardless of
// the browser's local timezone, unlike toLocaleTimeString().
export function formatClockUTC(isoString) {
  if (!isoString) return 'unknown';
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return 'unknown';
  return `${d.toISOString().substring(11, 19)} UTC`;
}

// The annunciator's "open R" phrase: total (or "n/a" if every open position
// is currently unmeasurable) plus a "(N n/a)" suffix when SOME positions
// are unmeasurable -- mirrors render_annunciator()'s own r_label
// construction in streamlit_app.py.
export function formatOpenR(totalOpenR, unmeasurableCount) {
  const base = formatR(totalOpenR);
  if (unmeasurableCount) return `${base} (${unmeasurableCount} n/a)`;
  return base;
}

// The unreachable-data banner text (ND-1 plan doc §2.4: "if the API is
// unreachable the console says so -- it never silently shows stale
// numbers"). null when reachable (nothing to show).
export function describeUnreachable(isUnreachable, lastGoodAsOf) {
  if (!isUnreachable) return null;
  const last = lastGoodAsOf ? formatClockUTC(lastGoodAsOf) : 'never';
  return `API unreachable — data is stale (last good: ${last})`;
}
