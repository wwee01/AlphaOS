// 2026-07-17 Research tab -- pure display-formatting for /api/v1/research.
// No DOM, no React (same split as decisions.js/learning.js). Every number
// this page shows comes straight from the API; the only "logic" here is
// clamping a percentage and choosing a sentence -- never a business
// decision (this codebase's own "frontend computes nothing business-
// critical" rule -- see Decisions.jsx's module docstring for the same
// discipline applied to the funnel view).

// Clamped 0-100, null-safe -- a missing/zero minDays reads as 0% progress
// rather than a divide-by-zero or NaN bar.
export function auditProgressPct(days, minDays) {
  if (!minDays || minDays <= 0) return 0;
  const pct = ((days ?? 0) / minDays) * 100;
  if (Number.isNaN(pct)) return 0;
  return Math.max(0, Math.min(100, pct));
}

// The one sentence answering "is it time to run the saturation audit yet" --
// mirrors scripts/shadow_saturation_audit.py's own <20-day warning, worded
// for the console rather than a CLI. Never states a recommended constant
// value (that's the script's job, run by hand -- see routes.py's /research
// docstring for why this page reports readiness, not conclusions).
export function describeAuditReadiness(capture) {
  if (!capture) return 'capture status unavailable.';
  if (capture.audit_viable) {
    return `audit-viable (${capture.capture_days} trading days captured) — `
      + 'run scripts/shadow_saturation_audit.py against the real DB when ready.';
  }
  const remaining = capture.audit_days_remaining ?? capture.audit_min_trading_days;
  const noun = remaining === 1 ? 'day' : 'days';
  return `${remaining} more trading ${noun} of capture needed before the saturation audit is viable `
    + `(${capture.capture_days ?? 0} of ${capture.audit_min_trading_days ?? '?'} so far).`;
}
