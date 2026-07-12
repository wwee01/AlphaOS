"""alphaos/api/app.py -- ND-1 FastAPI app factory.

Side-effect-free at creation time (ND-1 plan doc §2.1): calling
`create_app()` starts no scheduler, calls no provider, triggers no scan.
Every request-scoped dependency (Settings, a read-only JournalStore, a
MarketDataClient) is constructed per-request instead (see `alphaos/api/
deps.py`) -- app creation itself touches no journal and no settings. The one
filesystem touch at creation time is an `os.path.isdir()` existence check for
the built console (`console/dist/`), guarded so creating this app never
requires the frontend to have been built (tests, and a fresh checkout before
`npm run build`, must not fail here).

No permissive CORS middleware is added (ND-1 plan doc §3): the built console
is served same-origin by this same process, so none is needed, and adding
one would defeat `ConsoleSecurityMiddleware`'s whole purpose.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from alphaos.api.routes import router
from alphaos.api.security import ConsoleSecurityMiddleware

# alphaos/api/app.py -> alphaos/api -> alphaos -> <repo root> -> console/dist
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONSOLE_DIST = os.path.join(_REPO_ROOT, "console", "dist")


def create_app() -> FastAPI:
    app = FastAPI(title="AlphaOS Console API", docs_url=None, redoc_url=None)
    app.add_middleware(ConsoleSecurityMiddleware)
    app.include_router(router)

    # Mounted LAST (and only if built) so /api/v1/* above always wins route
    # matching over the static catch-all -- guarded so `import alphaos.api.app`
    # / `create_app()` never requires console/dist to exist (ND-1 ships the
    # API before the frontend is necessarily built in every environment, e.g.
    # this test suite, which never runs `npm run build`).
    if os.path.isdir(CONSOLE_DIST):
        app.mount("/", StaticFiles(directory=CONSOLE_DIST, html=True), name="console")

    return app
