"""alphaos/api/pin.py -- ND-3 console PIN infrastructure (docs/roadmap/
console-migration-nd.md §3, §4 ND-3 scope).

Two independent pieces, both process-local and deliberately simple:

* ``PinStore`` -- file-backed PIN hash storage, one JSON file
  (``data/console_pin.hash`` by default). This mirrors ``alphaos/safety.py``
  ``KillSwitch``'s own pattern exactly: a marker/secret file whose PATH is a
  constructor parameter (not a ``Settings`` field), because this is a local
  operator secret -- never environment/deployment configuration, and never
  the SQLite journal (a PIN hash has no place in the trading ledger, and a
  ``mode=ro`` read-only journal handle -- ND-1/ND-2's whole point -- could
  never write it anyway). The hash is scrypt (stdlib ``hashlib.scrypt``, no
  new dependency), salted per-PIN, verified with ``hmac.compare_digest``
  (constant-time -- ND-3 plan doc §3 mandate: "never ``==``, always
  ``hmac.compare_digest``" on a secret comparison).

* ``PinRateLimiter`` -- an in-memory, NOT-restart-durable consecutive-
  failure lockout. Threat model (ND-3 plan doc §3): this PIN protects a
  loopback-only API from a compromised/malicious browser TAB, not a remote
  network attacker -- a process-local counter that resets on restart is a
  proportionate, honestly-documented trade-off, not a gap (the plan doc's
  own words: "in-memory counter is fine, doesn't need to survive restart").

Fail-closed discipline throughout: no PIN configured -> every write route
returns 503 (see write_routes.py's ``_authorize_write``), never silently
"unlocked". A corrupted/unreadable hash file verifies as a rejected PIN
(``False``), never as an exception that could be mishandled into an accept.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

# KillSwitch's own default is "data/KILL_SWITCH" (alphaos/safety.py) --
# same directory, same "operator-local file, not committed, not env-
# configured" posture. Listed in .gitignore alongside data/*.db*.
DEFAULT_PIN_PATH = "data/console_pin.hash"

# scrypt cost parameters -- OWASP's 2023+ interactive-login minimums
# (N=2**14, r=8, p=1). This runs once per write REQUEST (never in a hot
# loop, never per-candidate/per-scan-item), so its ~50-100ms cost on
# ordinary hardware is the point: it is exactly what makes offline
# brute-force of a captured hash file expensive, while staying imperceptible
# to an operator typing a PIN before clicking "run scan".
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


class PinStore:
    """File-backed PIN hash storage. ``path`` is a constructor parameter
    (exactly like ``KillSwitch(path=...)``) so tests point it at a
    ``tmp_path`` file instead of ``data/console_pin.hash`` -- via FastAPI's
    ``app.dependency_overrides[get_pin_store]``, matching this app's
    existing ``get_settings``/``get_journal`` override pattern, not
    monkeypatching a module constant.
    """

    def __init__(self, path: str = DEFAULT_PIN_PATH):
        self.path = path

    def is_configured(self) -> bool:
        return os.path.exists(self.path)

    def set_pin(self, pin: str) -> None:
        """Hash + persist ``pin``, overwriting any existing hash. Refuses an
        empty/whitespace-only PIN (a blank PIN is not "no PIN configured" --
        it is a mis-set one, and must not be silently accepted as if the
        operator meant it)."""
        if not pin or not pin.strip():
            raise ValueError("PIN must not be empty")
        salt = os.urandom(16)
        digest = hashlib.scrypt(
            pin.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
        )
        payload = {
            "algorithm": "scrypt",
            "n": _SCRYPT_N,
            "r": _SCRYPT_R,
            "p": _SCRYPT_P,
            "dklen": _SCRYPT_DKLEN,
            "salt": salt.hex(),
            "hash": digest.hex(),
        }
        parent = os.path.dirname(self.path) or "."
        os.makedirs(parent, exist_ok=True)
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp_path, self.path)  # atomic on the same filesystem
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover -- best-effort; not every FS supports chmod
            pass

    def verify(self, candidate: str) -> bool:
        """Constant-time verification. Returns ``False`` (never raises) for
        "no PIN configured" or a corrupted/unreadable hash file -- callers
        must check ``is_configured()`` first and fail CLOSED (503) rather
        than reading a ``False`` here as "PIN just happens to be wrong"."""
        if not self.is_configured():
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            salt = bytes.fromhex(payload["salt"])
            expected = bytes.fromhex(payload["hash"])
            candidate_digest = hashlib.scrypt(
                candidate.encode("utf-8"),
                salt=salt,
                n=payload["n"],
                r=payload["r"],
                p=payload["p"],
                dklen=payload["dklen"],
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            # audit-fixup (correctness L1): a syntactically-valid but
            # type-confused hash file (e.g. "n" stored as a string) makes
            # hashlib.scrypt raise TypeError, not caught by the original
            # tuple -- confirmed live to 500 instead of failing closed as
            # this method's own docstring promises. TypeError added so
            # every corrupted-file shape verifies as False, never raises.
            return False
        # ND-3 plan doc §3 mandate: constant-time compare, never `==`, on a
        # secret comparison (a `==` here would leak timing information about
        # how many leading bytes of the guess matched the real hash).
        return hmac.compare_digest(candidate_digest, expected)


@dataclass
class PinRateLimiter:
    """Consecutive-failure lockout, in-memory only. ``max_attempts``
    consecutive failed ``verify()`` calls (any success resets the counter --
    see ``record_success``) locks out further attempts for
    ``cooldown_seconds``. Not restart-durable BY DESIGN (module docstring
    above) -- a process restart clears it, which is an accepted, documented
    trade-off for a localhost-only threat model, not an oversight.
    """

    max_attempts: int = 5
    cooldown_seconds: float = 300.0
    _consecutive_failures: int = field(default=0, init=False, repr=False)
    _locked_until: Optional[float] = field(default=None, init=False, repr=False)

    def is_locked_out(self) -> bool:
        if self._locked_until is None:
            return False
        if time.monotonic() >= self._locked_until:
            # Cooldown elapsed -- clear the lock and give a fresh run of
            # attempts, rather than requiring a separate "unlock" action
            # (there is no operator-facing unlock affordance in ND-3; time
            # is the only way out, by design).
            self._locked_until = None
            self._consecutive_failures = 0
            return False
        return True

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.max_attempts:
            self._locked_until = time.monotonic() + self.cooldown_seconds

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._locked_until = None


# Process-wide singleton -- rate limiting must persist ACROSS requests
# within one running server process (unlike PinStore/JournalStore, which
# ND-1/ND-2/ND-3 all deliberately reconstruct fresh per-request). Exposed as
# a function (not a bare module import) so alphaos/api/deps.py's
# get_rate_limiter dependency -- and its docstring explaining how tests get
# isolation from this singleton via dependency_overrides -- is the one
# documented seam, rather than every call site reaching into this module's
# globals directly.
_RATE_LIMITER = PinRateLimiter()


def default_rate_limiter() -> PinRateLimiter:
    return _RATE_LIMITER
