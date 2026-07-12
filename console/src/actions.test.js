// ND-3: vitest coverage for the pure write-action logic (actions.js has no
// DOM/React dependency by design, so these run without jsdom -- same
// pattern format.js/positions.js already establish).
import { describe, expect, it } from 'vitest';
import { clearedPinState, generateNonce } from './actions.js';

describe('generateNonce', () => {
  it('returns a non-empty string', () => {
    const n = generateNonce();
    expect(typeof n).toBe('string');
    expect(n.length).toBeGreaterThan(0);
  });

  it('returns a different value on every call (one nonce per user-intent, per ND-3 plan doc §3)', () => {
    const seen = new Set();
    for (let i = 0; i < 200; i += 1) {
      seen.add(generateNonce());
    }
    expect(seen.size).toBe(200);
  });

  it('matches UUID shape when crypto.randomUUID is available (this test environment: Node 22+)', () => {
    const n = generateNonce();
    expect(n).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  });
});

describe('clearedPinState', () => {
  it('always returns an empty string -- the PIN field must never retain a value after a request settles', () => {
    expect(clearedPinState()).toBe('');
  });

  it('takes no arguments that could accidentally echo the prior PIN back', () => {
    expect(clearedPinState.length).toBe(0);
  });
});
