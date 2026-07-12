"""alphaos/api/security.py -- ND-1 plan doc §3 security model: the part that
applies even though every route in this phase is read-only.

A localhost HTTP API is reachable by ANY web page the operator's browser
happens to have open (`fetch("http://localhost:8601/...")` from an unrelated
tab). Streamlit had its own built-in XSRF protection; this app has none of
that for free, so it is built here, from ND-1, before any write endpoint
exists.

Two independent gates, applied as a single ASGI middleware ahead of routing:

1. **Origin allowlist.** A request carrying an `Origin` header that is not
   in `ALLOWED_ORIGINS` is refused (403), for every path this app serves
   (API routes and the mounted console static files alike). A request with
   NO `Origin` header at all (curl, the CLI, direct server-to-server calls)
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
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# The console is only ever served from this app's own loopback bind, on the
# ND-1 plan doc's chosen port (§1, "Serving": 8601) -- both loopback
# spellings are allowlisted since a browser may resolve either.
ALLOWED_ORIGINS = frozenset({"http://localhost:8601", "http://127.0.0.1:8601"})

CONSOLE_HEADER = "x-alphaos-console"
CONSOLE_HEADER_VALUE = "1"
API_PATH_PREFIX = "/api/"


class ConsoleSecurityMiddleware(BaseHTTPMiddleware):
    """Both §3 read-phase gates in one pass, cheapest check first."""

    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return JSONResponse({"detail": "origin not allowed"}, status_code=403)
        if request.url.path.startswith(API_PATH_PREFIX):
            if request.headers.get(CONSOLE_HEADER) != CONSOLE_HEADER_VALUE:
                return JSONResponse({"detail": "missing X-AlphaOS-Console header"}, status_code=403)
        return await call_next(request)
