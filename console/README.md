# AlphaOS Console

React 19 + Vite frontend for `alphaos/api`'s read-only FastAPI backend (docs/roadmap/console-migration-nd.md). Deploy: `npm ci && npm run build` here to produce `console/dist/`, then start the API from the repo root with `.venv/bin/python -m alphaos.api` (or load `deploy/com.ck.alphaos.console.plist`) -- that one process serves both the API and the built frontend, loopback-only, on port 8601. `npm run dev` (port 5601) proxies `/api` to `127.0.0.1:8601` for local frontend iteration against an already-running API; it is never used in production.
