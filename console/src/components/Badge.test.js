import { describe, expect, it } from 'vitest';
import { badgeTone } from './Badge.jsx';

describe('badgeTone', () => {
  it('maps thesis status to tone (position_health.THESIS_*)', () => {
    expect(badgeTone('INTACT')).toBe('success');
    expect(badgeTone('AT_RISK')).toBe('warning');
    expect(badgeTone('BROKEN')).toBe('danger');
  });

  it('maps verdict to tone (position_health.VERDICT_*)', () => {
    expect(badgeTone('HOLD')).toBe('neutral');
    expect(badgeTone('ATTENTION')).toBe('warning');
    expect(badgeTone('EXIT_REVIEW')).toBe('danger');
  });

  it('maps trade direction to tone', () => {
    expect(badgeTone('LONG')).toBe('primary');
    expect(badgeTone('SHORT')).toBe('neutral');
  });

  it('maps protection watchdog status to tone (constants.ProtectionStatus)', () => {
    expect(badgeTone('protected')).toBe('success');
    expect(badgeTone('degraded')).toBe('warning');
    expect(badgeTone('unprotected')).toBe('danger');
    expect(badgeTone('closed_mismatch')).toBe('danger');
    expect(badgeTone('unverifiable')).toBe('danger');
    expect(badgeTone('check_error')).toBe('warning');
    expect(badgeTone('unknown')).toBe('neutral');
  });

  it('maps TQS bucket to tone (constants.TqsBucket)', () => {
    expect(badgeTone('strong')).toBe('success');
    expect(badgeTone('good')).toBe('success');
    expect(badgeTone('watch')).toBe('warning');
    expect(badgeTone('mixed')).toBe('warning');
    expect(badgeTone('weak')).toBe('danger');
    expect(badgeTone('unscorable')).toBe('neutral');
  });

  it('is case-insensitive', () => {
    expect(badgeTone('intact')).toBe('success');
    expect(badgeTone('Intact')).toBe('success');
    expect(badgeTone('INTACT')).toBe('success');
  });

  it('falls back to neutral for null/undefined/unrecognized input, never throws', () => {
    expect(badgeTone(null)).toBe('neutral');
    expect(badgeTone(undefined)).toBe('neutral');
    expect(badgeTone('')).toBe('neutral');
    expect(badgeTone('SOMETHING_NEW_NOT_IN_THE_TABLE')).toBe('neutral');
  });
});
