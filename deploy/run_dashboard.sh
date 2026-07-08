#!/usr/bin/env bash
# OPS-A: the recommended way to start the AlphaOS dashboard. Hardcodes the
# loopback bind address -- there is deliberately no flag or env var that can
# override it, since the whole point is "an operator in a hurry can't get
# this wrong". Port is the only configurable bit. The dashboard's own
# _is_loopback_request() guard (alphaos/dashboard/streamlit_app.py) is the
# second, independent check -- this script is the first: belt and
# suspenders, not a single point of failure.
#
# Usage:
#   deploy/run_dashboard.sh          # port 8502 (this repo's convention)
#   deploy/run_dashboard.sh 8600     # a different port
#
# Remote access, if ever wanted, is an SSH tunnel from the remote machine:
#   ssh -L 8502:127.0.0.1:8502 user@this-host
# -- never re-run this script with a different bind address, never a reverse
# proxy, never port-forwarding (see the module docstring / master reference
# OPS-A item for why).

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$DEPLOY_DIR")"
PORT="${1:-8502}"
BIND_ADDR="127.0.0.1"

if ! [[ "$PORT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: port must be numeric, got: $PORT" >&2
  exit 1
fi

cd "$REPO_DIR"

if [ ! -x ".venv/bin/streamlit" ]; then
  echo "ERROR: .venv/bin/streamlit not found -- run 'pip install streamlit' inside .venv first" >&2
  exit 1
fi

echo "Starting AlphaOS dashboard on ${BIND_ADDR}:${PORT} (loopback only)..."
exec .venv/bin/python .venv/bin/streamlit run alphaos/dashboard/streamlit_app.py \
  --server.address "$BIND_ADDR" \
  --server.port "$PORT" \
  --server.headless true \
  --browser.gatherUsageStats false
