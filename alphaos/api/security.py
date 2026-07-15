"""alphaos/api/security.py -- ND-1 plan doc §3 security model: the part that
applies even though every route in this phase is read-only.

A localhost HTTP API is reachable by ANY web page the operator's browser
happens to have open (`fetch("http://localhost:8601/...")` from an unrelated
tab). Streamlit had its own built-in XSRF protection; this app has none of
that for free, so it is built here, from ND-1, before any write endpoint
exists.

Two independent gates, applied as a single ASGI middleware ahead of routing:

1. **Origin allowlist.** A request carrying an `Origin` header that is not
   in the allowlist is refused (403), for every path this app serves (API
   routes and the mounted console static files alike). A request with NO
   `Origin` header at all (curl, the CLI, direct server-to-server calls)
   passes this gate -- real browsers always attach `Origin` on a `fetch()`
   (same-origin or cross-origin), so its absence is the honest signature of
   a non-browser caller, not a spoofable browser bypass.
2. **Custom header requirement**, scoped to `/api/*` only. Every API route
   must carry `X-AlphaOS-Console: 1`. This alone defeats simple `<form>`-
   based CSRF (forms cannot set arbitrary request headers), and any
   `fetch()` from another origin that tries to set it triggers a CORS
   preflight -- which gate 1 above then kills before the actual request
   ever reaches a route, since no permissive CORS middleware is configured
   anywhere in this app (same-origin serving needs none; adding one would
   be actively unsafe -- ND-1 plan doc §3).

Write verbs (POST/PUT/DELETE/PATCH) are not specially handled here: ND-1
defines no write routes at all, so FastAPI's own router already answers a
non-GET request to any `/api/*` path with 405 Method Not Allowed once (and
if) it clears both gates above; a request missing the header is instead
turned away with 403 before ever reaching the router.

ND-8 (Tailscale access) note: the allowlist used to be one hardcoded
frozenset. It is now resolved PER REQUEST from Settings as the union of two
parts:

* The loopback origins (`http://localhost:<port>`, `http://127.0.0.1:<port>`)
  -- ALWAYS allowed, unconditionally, computed from `settings.console_port`
  (default 8601) rather than a bare literal so a `CONSOLE_PORT` override
  can't strand local access on a stale port number. With no env set at all,
  this is exactly the ND-1 hardcoded pair, byte-for-byte.
* `settings.console_allowed_origins_list` -- operator-added extras from
  `CONSOLE_ALLOWED_ORIGINS` (e.g. a Tailscale IP or MagicDNS origin). Empty
  by default, so an unconfigured install's effective allowlist is identical
  to ND-1's.

Settings are read the same way every route already does it (`Depends(
get_settings)` in alphaos/api/deps.py), via `request.app.dependency_
overrides` if a test has overridden it -- the SAME override object routes
use -- so a test can exercise `CONSOLE_ALLOWED_ORIGINS`/`CONSOLE_PORT`
without ever mutating real process environment variables, consistent with
this suite's existing `make_settings()` + `dependency_overrides[get_settings]`
pattern (see tests/test_api_console.py). In production there is no override,
so this just calls the real `get_settings()` (`load_settings()`, fresh per
request, no caching -- same posture as every other per-request dependency in
this app, ND-1 plan doc §2.1).
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from alphaos.api.deps import get_settings
from alphaos.config.settings import Settings

CONSOLE_HEADER = "x-alphaos-console"
CONSOLE_HEADER_VALUE = "1"
API_PATH_PREFIX = "/api/"


def loopback_origins(port: int) -> frozenset[str]:
    """Both loopback spellings for `port`, allowed unconditionally -- a
    browser may resolve either. Kept as a standalone function (rather than a
    module-level constant) because ND-8 makes the port configurable
    (`CONSOLE_PORT`); called with the ND-1 default (8601) this returns the
    exact frozenset ND-1 hardcoded."""
    return frozenset({f"http://localhost:{port}", f"http://127.0.0.1:{port}"})


# ND-1's original hardcoded default, kept as a named constant for anything
# (docs, a stray import) that still refers to "the default loopback origins"
# -- NOT read by the middleware itself, which always resolves the current
# per-request port via loopback_origins(settings.console_port) instead.
ALLOWED_ORIGINS = loopback_origins(8601)


def _resolve_settings(request: Request) -> Settings:
    """Settings for this request, honoring `app.dependency_overrides[
    get_settings]` if a test (or, in principle, any caller) has set one --
    the identical mechanism FastAPI's own `Depends(get_settings)` uses in
    every route, just invoked manually here since ASGI middleware sits
    outside the DI system. Falls back to the real `get_settings` (fresh
    `load_settings()` per call) when no override is registered."""
    override = request.app.dependency_overrides.get(get_settings, get_settings)
    return override()


class ConsoleSecurityMiddleware(BaseHTTPMiddleware):
    """Both §3 read-phase gates in one pass, cheapest check first."""

    async def dispatch(self, request: Request, call_next):
        settings = _resolve_settings(request)
        allowed_origins = loopback_origins(settings.console_port) | set(
            settings.console_allowed_origins_list
        )

        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            return JSONResponse({"detail": "origin not allowed"}, status_code=403)
        if request.url.path.startswith(API_PATH_PREFIX):
            if request.headers.get(CONSOLE_HEADER) != CONSOLE_HEADER_VALUE:
                return JSONResponse({"detail": "missing X-AlphaOS-Console header"}, status_code=403)
        return await call_next(request)
