"""alphaos.api -- ND-1 read-only FastAPI console API (loopback-only, default
port 8601).

See docs/roadmap/console-migration-nd.md for the phased console-migration
plan; this package implements ND-1's scope only: four read-only `/api/v1/*`
endpoints backing the Tonight cockpit, wrapping the exact Python functions
`alphaos/dashboard/streamlit_app.py` already uses for the same views (no
business logic is re-derived here -- see `alphaos/api/routes.py`).

Importing this package is side-effect-free (ND-1 plan doc §2.1: no
scheduler, no provider call, no scan). `create_app()` in `alphaos/api/app.py`
is a plain factory -- nothing is instantiated at import time; callers
(`python -m alphaos.api`, or a test's `TestClient(create_app())`) construct
the app explicitly.
"""

from __future__ import annotations
