"""python -m alphaos.api -- console API entrypoint.

Loopback-only by DEFAULT (ND-1 plan doc §2.1: "Bind loopback only... and
refuse other binds without an explicit override env that does not exist
yet"). ND-8 (Tailscale access) adds that explicit override, still
loopback-only unless the operator sets it:

* `CONSOLE_BIND_HOST` (default `127.0.0.1`) -- read via
  `alphaos.config.settings.load_settings()`, the SAME env/.env loading path
  every other setting uses, so an unset/empty `.env` reproduces today's
  behavior byte-for-byte. Setting this to a Tailscale IP (100.x.y.z, a
  private authenticated WireGuard mesh address) exposes the console on the
  tailnet ONLY: binding to one specific interface address means only
  traffic arriving on THAT interface is accepted at the socket level -- the
  LAN interface never sees a connection attempt, regardless of what's on the
  local network. Setting it to `0.0.0.0` is also honored (this module places
  no guard against it) but that is a deliberate, explicit operator choice
  with a real consequence: it exposes the console to the ENTIRE LAN, not
  just the tailnet. See console/README.md's "Tailscale access" section for
  the full operator setup writeup and the LAN-exposure caveat spelled out
  again there.
* `CONSOLE_PORT` (default `8601`) -- same override mechanism; the security
  middleware's loopback allowlist (alphaos/api/security.py) is derived from
  this same setting, so local access keeps working even if the port changes.

Side-effect-free at import time, same as `create_app()` (alphaos/api/app.py's
module docstring): `load_settings()` only reads env/`.env`, no scheduler
start, no provider call, no scan.
"""

from __future__ import annotations

import uvicorn

from alphaos.api.app import create_app
from alphaos.config.settings import load_settings

# Kept as module-level names (not inlined into main()) since ND-1 shipped
# them this way and other code may still import them for the ND-1 defaults;
# main() itself no longer reads these -- it reads settings.console_bind_host
# / settings.console_port fresh on every invocation instead.
HOST = "127.0.0.1"
PORT = 8601


def main() -> None:
    settings = load_settings()
    app = create_app()
    uvicorn.run(app, host=settings.console_bind_host, port=settings.console_port)


if __name__ == "__main__":
    main()
