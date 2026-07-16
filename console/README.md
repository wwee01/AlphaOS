# AlphaOS Console

React 19 + Vite frontend for `alphaos/api`'s read-only FastAPI backend (docs/roadmap/console-migration-nd.md). Deploy: `npm ci && npm run build` here to produce `console/dist/`, then start the API from the repo root with `.venv/bin/python -m alphaos.api` (or load `deploy/com.ck.alphaos.console.plist`) -- that one process serves both the API and the built frontend, loopback-only by default, on port 8601. `npm run dev` (port 5601) proxies `/api` to `127.0.0.1:8601` for local frontend iteration against an already-running API; it is never used in production.

## Tailscale access (ND-8)

By default the console is reachable only from the same machine (loopback bind + loopback-only origin allowlist). If you have [Tailscale](https://tailscale.com) installed and want to open the console from your iPhone **over the tailnet only** -- not the LAN, not the public internet -- two env vars opt you in. Nothing below changes anything if you leave both unset.

### 1. Find your tailnet values

Run `deploy/tailscale_console_env.sh` from the repo root. It prints your Mac's Tailscale IPv4, its MagicDNS name (if your tailnet has MagicDNS on), and the exact `.env` lines to paste -- it only reads Tailscale's own state (`tailscale ip -4` / `tailscale status --json`); it never starts, stops, or reconfigures anything.

If you'd rather do it by hand, the two commands it wraps are:

```bash
tailscale ip -4                 # your Mac's tailnet IPv4, e.g. 100.64.1.2
tailscale status --json         # Self.DNSName is the MagicDNS name, e.g. mac-mini.tailnet-name.ts.net.
```

### 2. Add to `.env`

```bash
# Binds the console to this ONE interface address instead of 127.0.0.1 --
# only traffic arriving over the tailnet reaches it; the LAN cannot.
CONSOLE_BIND_HOST=100.64.1.2

# Extra origins the security middleware accepts, on top of the loopback
# origins it always accepts unconditionally. Comma-separated, both the raw
# IP and the MagicDNS name if you have one.
CONSOLE_ALLOWED_ORIGINS=http://100.64.1.2:8601,http://mac-mini.tailnet-name.ts.net:8601
```

Restart the console process (reload `deploy/com.ck.alphaos.console.plist`, or however you run `python -m alphaos.api`) for the new `.env` values to take effect.

### 3. On the iPhone

With the iPhone on the same tailnet (Tailscale app running, signed into the same account), open:

```
http://100.64.1.2:8601
```

or the MagicDNS name if you set one (`http://mac-mini.tailnet-name.ts.net:8601`). The console loads the same same-origin app it does locally; writes (approve/reject, kill-switch, scan/monitor/report) still require the console PIN exactly as they do on the Mac -- nothing about the PIN/nonce/rate-limit gate changes for a tailnet request.

### What this does NOT do

- **Not LAN exposure.** `CONSOLE_BIND_HOST` binds one specific interface address (the Tailscale interface), not `0.0.0.0` -- a device on your home Wi-Fi that isn't on your tailnet cannot reach the console. Setting `CONSOLE_BIND_HOST=0.0.0.0` yourself is possible (nothing here refuses it) but that really would expose the console to your entire LAN -- don't do that just to reach your phone; use the Tailscale IP instead.
- **Not internet exposure.** Tailscale is a private, authenticated WireGuard mesh; nothing here opens a port on your router or makes the console reachable from the public internet.
- **Not cross-origin.** No CORS headers are added. The iPhone loads the app from the console's own origin (same-origin), so `CONSOLE_ALLOWED_ORIGINS` is only ever adding origins that a browser tab loaded from THIS app could itself carry as its `Origin` header -- it is not a general-purpose CORS allowlist.
- **The Streamlit fallback (`http://localhost:8502`) stays loopback-only.** The console's own footer/deep-link to Streamlit will not work from the iPhone -- that's expected; Streamlit is the break-glass fallback for the Mac itself, not a phone-reachable surface.
