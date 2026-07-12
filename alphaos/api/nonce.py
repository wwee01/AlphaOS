"""alphaos/api/nonce.py -- ND-3 idempotency/replay-guard (docs/roadmap/
console-migration-nd.md §3 "Idempotency", §4 ND-3 scope).

This is a SEPARATE concern from the PIN (alphaos/api/pin.py): the PIN
proves "this really is the operator"; the nonce prevents "the same
user-intent got submitted twice" -- a flaky network retry, a double-click,
or a malicious page replaying a captured request. A client mints one nonce
per user-intent (e.g. one per button-press -- console/src/actions.js:
generateNonce()) and the server refuses a second request carrying the same
nonce, regardless of whether the PIN on that second request is valid.

In-memory, short TTL, NOT restart-durable -- deliberately (ND-3 plan doc:
"in-memory, short TTL like 5 minutes is fine -- this isn't meant to survive
restart"). A restart clearing the seen-nonce set is harmless: any nonce a
legitimate client could still plausibly replay was minted moments ago by a
still-running browser tab, which will simply mint a fresh one on its next
button-press.
"""

from __future__ import annotations

import threading
import time
from typing import Dict


class NonceStore:
    """Records nonces already consumed by an AUTHORIZED (PIN-valid) write
    request, within a rolling TTL window. See write_routes.py's
    ``_authorize_write`` for the exact ordering: the nonce is checked AFTER
    the PIN succeeds, so a request with a wrong PIN never consumes the
    nonce -- a legitimate retry with a freshly-minted nonce (the client
    always mints one per submit attempt) is unaffected either way, but this
    ordering keeps the nonce check's failure mode (409) meaningfully
    distinct from the PIN check's (401), matching the ND-3 test matrix.
    """

    def __init__(self, ttl_seconds: float = 300.0):
        self.ttl_seconds = ttl_seconds
        self._seen: Dict[str, float] = {}
        # audit-fixup (correctness L2): FastAPI/Starlette runs sync route
        # handlers in a threadpool, not on one event-loop thread -- the
        # original docstring's "atomic under the GIL" claim overstated the
        # guarantee, since the GIL can switch between the `in` check and the
        # `[]=` insert across two OS threads. An explicit lock makes the
        # check-and-record genuinely atomic rather than relying on that
        # (real but narrower) CPython bytecode-level property.
        self._lock = threading.Lock()

    def check_and_record(self, nonce: str) -> bool:
        """Returns True and records `nonce` as seen (now) if it is fresh.
        Returns False -- WITHOUT re-recording it -- if `nonce` was already
        recorded within the TTL window (a replay). Lock-protected so two
        concurrent requests carrying the SAME nonce cannot both observe
        "fresh" (no separate check-then-record race)."""
        now = time.monotonic()
        with self._lock:
            self._purge_expired(now)
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True

    def _purge_expired(self, now: float) -> None:
        expired = [n for n, seen_at in self._seen.items() if now - seen_at > self.ttl_seconds]
        for n in expired:
            del self._seen[n]


# Process-wide singleton, same reasoning as pin.py's _RATE_LIMITER: a replay
# guard must persist ACROSS requests within one running server process.
# alphaos/api/deps.py's get_nonce_store dependency is the documented seam
# tests use to substitute a fresh, isolated instance per test.
_NONCE_STORE = NonceStore()


def default_nonce_store() -> NonceStore:
    return _NONCE_STORE
