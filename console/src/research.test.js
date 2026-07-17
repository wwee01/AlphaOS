import { describe, expect, it } from 'vitest';
import { auditProgressPct, describeAuditReadiness } from './research.js';

describe('auditProgressPct', () => {
  it('computes a plain percentage of days vs the trading-day bar', () => {
    expect(auditProgressPct(5, 20)).toBe(25);
    expect(auditProgressPct(10, 20)).toBe(50);
  });

  it('clamps at 0 and 100 rather than overshooting', () => {
    expect(auditProgressPct(-3, 20)).toBe(0);
    expect(auditProgressPct(25, 20)).toBe(100);
  });

  it('is null-safe: missing days or a zero/missing bar reads as 0%, never NaN or a crash', () => {
    expect(auditProgressPct(null, 20)).toBe(0);
    expect(auditProgressPct(undefined, 20)).toBe(0);
    expect(auditProgressPct(5, 0)).toBe(0);
    expect(auditProgressPct(5, null)).toBe(0);
  });
});

describe('describeAuditReadiness', () => {
  it('reports a countdown when not yet viable, with correct day/days pluralization', () => {
    const msg = describeAuditReadiness({
      audit_viable: false, capture_days: 5, audit_days_remaining: 15, audit_min_trading_days: 20,
    });
    expect(msg).toContain('15 more trading days');
    expect(msg).toContain('5 of 20');
  });

  it('uses singular "day" when exactly one remains', () => {
    const msg = describeAuditReadiness({
      audit_viable: false, capture_days: 19, audit_days_remaining: 1, audit_min_trading_days: 20,
    });
    expect(msg).toContain('1 more trading day ');
    expect(msg).not.toContain('1 more trading days');
  });

  it('names the audit script (not a recommended value) once viable -- never surfaces conclusions here', () => {
    const msg = describeAuditReadiness({
      audit_viable: true, capture_days: 22, audit_days_remaining: 0, audit_min_trading_days: 20,
    });
    expect(msg).toContain('audit-viable');
    expect(msg).toContain('shadow_saturation_audit.py');
  });

  it('never crashes on a missing capture object', () => {
    expect(describeAuditReadiness(null)).toBe('capture status unavailable.');
    expect(describeAuditReadiness(undefined)).toBe('capture status unavailable.');
  });
});
