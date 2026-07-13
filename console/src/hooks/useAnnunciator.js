// ND-6: the annunciator's fetch/poll logic, extracted from
// components/Annunciator.jsx unchanged (same endpoint, same 10s cadence,
// same "keep last-known state on a fetch error" behavior -- every page
// already renders its own "API unreachable" stale-banner, so this hook
// doesn't need a second one). Extracted so the SAME poll/data can be shared
// between components/Masthead.jsx (the mobile condensed summary chip) and
// components/Annunciator.jsx (the full strip carrying the one kill-switch
// control) without two independent pollers hitting /api/v1/annunciator.
import { useCallback, useEffect, useRef, useState } from 'react';
import { getAnnunciator } from '../api.js';

const POLL_MS = 10000;

export function useAnnunciator() {
  const [data, setData] = useState(null);
  const mountedRef = useRef(true);

  const poll = useCallback(async () => {
    try {
      const a = await getAnnunciator();
      if (!mountedRef.current) return;
      setData(a);
    } catch {
      // Keep last-known state -- see this module's own docstring above.
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    poll();
    const id = setInterval(poll, POLL_MS);
    return () => {
      mountedRef.current = false;
      clearInterval(id);
    };
  }, [poll]);

  return [data, poll];
}
