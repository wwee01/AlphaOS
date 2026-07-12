"""python -m alphaos.api -- ND-1 console API entrypoint.

Loopback-only, hardcoded (ND-1 plan doc §3, §2.1: "Bind loopback only... and
refuse other binds without an explicit override env that does not exist
yet"). There is deliberately no flag or env var to override the bind address
in this phase -- same posture as `deploy/run_dashboard.sh`'s OPS-A bind
guard for the Streamlit dashboard ("an operator in a hurry can't get this
wrong"). Port 8601 is the plan doc's own chosen default (§1, "Serving").
"""

from __future__ import annotations

import uvicorn

from alphaos.api.app import create_app

HOST = "127.0.0.1"
PORT = 8601


def main() -> None:
    app = create_app()
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
