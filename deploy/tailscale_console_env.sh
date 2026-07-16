#!/usr/bin/env bash
# ND-8: prints this Mac's Tailscale IPv4, its MagicDNS name (if enabled), and
# the exact .env lines to paste to expose the AlphaOS console (alphaos/api)
# on the tailnet ONLY -- see console/README.md's "Tailscale access" section
# for the full writeup this script is a shortcut for.
#
# READ-ONLY DISCOVERY. This script does not install, start, stop, configure,
# or authenticate Tailscale, and does not touch the console process, .env,
# or any AlphaOS file -- it only shells out to the already-installed
# `tailscale` CLI to read its current state and prints suggested lines for
# the operator to paste themselves. Never invoked by AlphaOS itself.
#
# Usage:
#   deploy/tailscale_console_env.sh            # uses CONSOLE_PORT env or 8601
#   CONSOLE_PORT=9000 deploy/tailscale_console_env.sh

set -euo pipefail

if ! command -v tailscale >/dev/null 2>&1; then
  echo "ERROR: 'tailscale' CLI not found on PATH." >&2
  echo "Install Tailscale first: https://tailscale.com/download" >&2
  exit 1
fi

TS_IP="$(tailscale ip -4 2>/dev/null || true)"
if [ -z "$TS_IP" ]; then
  echo "ERROR: could not read a Tailscale IPv4 address." >&2
  echo "Is Tailscale running and logged in? Check: tailscale status" >&2
  exit 1
fi

# MagicDNS name, if this tailnet has it enabled (empty string otherwise) --
# read from `tailscale status --json`'s own Self.DNSName, which always ends
# in a trailing dot; stripped here for a clean hostname to paste into a URL
# or env line. Falls back to empty (not an error) if python3 or the JSON
# shape isn't as expected -- MagicDNS is optional, not required for this
# script to still be useful with the raw IP alone.
TS_DNS_NAME="$(
  tailscale status --json 2>/dev/null | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    name = (data.get("Self") or {}).get("DNSName") or ""
    print(name.rstrip("."))
except Exception:
    pass
' 2>/dev/null || true
)"

PORT="${CONSOLE_PORT:-8601}"

echo "Tailscale IPv4:  ${TS_IP}"
if [ -n "$TS_DNS_NAME" ]; then
  echo "MagicDNS name:   ${TS_DNS_NAME}"
else
  echo "MagicDNS name:   (not set / MagicDNS off for this tailnet)"
fi
echo
echo "--- paste into .env ---"
echo "CONSOLE_BIND_HOST=${TS_IP}"
if [ -n "$TS_DNS_NAME" ]; then
  echo "CONSOLE_ALLOWED_ORIGINS=http://${TS_IP}:${PORT},http://${TS_DNS_NAME}:${PORT}"
else
  echo "CONSOLE_ALLOWED_ORIGINS=http://${TS_IP}:${PORT}"
fi
echo "------------------------"
echo
echo "Restart the console process for the new .env values to take effect."
echo
echo "Then, on the iPhone (same tailnet), open:"
if [ -n "$TS_DNS_NAME" ]; then
  echo "  http://${TS_DNS_NAME}:${PORT}"
else
  echo "  http://${TS_IP}:${PORT}"
fi
echo "Writes (approve/reject, kill-switch, scan/monitor/report) still require the console PIN."
