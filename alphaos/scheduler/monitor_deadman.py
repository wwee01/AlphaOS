"""Optional external success heartbeat; never an execution/readiness input.

One HTTPS GET after actual healthy monitor completion, no retry or redirect.
The operator supplies the endpoint and configures its external grace period
around the unchanged monitor cadence. No response body or URL is logged.
"""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request

TIMEOUT_SECONDS = 5


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def ping_monitor_success(settings, *, role: str, results: list, health: dict) -> dict:
    if not settings.monitor_deadman_enabled:
        return {"status": "disabled"}
    if role != "monitor" or settings.offline_mode:
        return {"status": "skipped", "reason": "not_live_monitor"}
    # A stale previous success or a not-due/busy tick is not a new success ping.
    completed = any(r.get("job_type") == "monitor" and r.get("status") == "completed"
                    and not r.get("completed_with_incidents") and not r.get("protection_blocking")
                    for r in results)
    if (not completed or health.get("ok") is not True or health.get("entry_ready") is not True
            or any(r.get("status") in ("failed", "fused", "blocked") for r in results)):
        return {"status": "skipped", "reason": "no_healthy_completed_supervision"}
    # Keep invalid configuration diagnostic-only: it cannot stop reconciliation.
    url = settings.monitor_deadman_url
    if not url:
        return {"status": "failed", "reason": "missing_url"}
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment
                or any(c.isspace() or ord(c) < 32 for c in url)):
            return {"status": "failed", "reason": "invalid_url"}
        # Do not forward the bearer URL through a redirect or an environment proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        request = urllib.request.Request(url, method="GET")
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
        return {"status": "sent" if 200 <= status < 300 else "failed", "http_status": status}
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        return {"status": "failed", "http_status": status}
    except Exception:
        # Exception strings, response headers and bodies may contain the secret URL.
        return {"status": "failed", "reason": "transport_error"}
