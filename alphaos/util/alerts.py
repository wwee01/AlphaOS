"""Push-notification alert sender (PR9: unattended scheduler cadence).

A thin, fail-safe wrapper around ntfy.sh (``settings.ntfy_topic``, already a
recognized-but-unused setting). This is operator-visibility tooling, not a
decision path: no gate/risk/approval/execution/scanner code may import this
module (enforced by a grep test, same pattern as PR7/PR8's shadow-layer
isolation).

Alerting must never block or fail the job that triggers it -- belt: this
function itself never raises; suspenders: every call site also wraps its
``send_alert`` call in its own try/except.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from alphaos.constants import Severity
from alphaos.lineage.hashing import SECRET_SETTINGS_FIELDS

_TIMEOUT_SECONDS = 5
# Defense-in-depth bound: ntfy.sh is a new PUBLIC egress channel for text that
# was previously local-only (system_events). Job-failure text is normally just
# str(exc), but nothing structurally prevents a future exception (or library)
# from embedding something long/unexpected -- cap it rather than trust every
# possible caller/exception forever.
_MAX_TEXT_LENGTH = 1000
_MIN_REDACTABLE_LENGTH = 6  # guard against redacting a trivially short value (e.g. an unset/short field) and mangling unrelated text


def _sanitize(text: str, settings) -> str:
    """Redact any configured secret VALUE (same allowlist as
    ``lineage.hashing.strip_secrets``, reused here for free text rather than
    dict keys) and bound length, before this text ever leaves the process --
    to the network (ntfy.sh) or to the local system_events audit log."""
    if not text:
        return text
    sanitized = text
    for field in SECRET_SETTINGS_FIELDS:
        value = getattr(settings, field, None)
        if isinstance(value, str) and len(value) >= _MIN_REDACTABLE_LENGTH and value in sanitized:
            sanitized = sanitized.replace(value, "***REDACTED***")
    if len(sanitized) > _MAX_TEXT_LENGTH:
        sanitized = sanitized[:_MAX_TEXT_LENGTH] + "...(truncated)"
    return sanitized


def send_alert(
    settings,
    title: str,
    message: str,
    priority: str = "default",
    journal=None,
) -> bool:
    """POST one push notification to ``https://ntfy.sh/{settings.ntfy_topic}``.

    Never raises. Returns True only on a successful send. Two distinct
    "not sent" cases:

    - No topic configured: a silent no-op (returns False, no network call, no
      log) -- this is the expected default state until an operator sets
      NTFY_TOPIC, not a failure worth an audit-trail row.
    - Any network/HTTP/encoding error: returns False AND, if ``journal`` is
      supplied, best-effort logs a ``system_events`` WARNING (never lets a
      logging failure mask the original send failure). Every scheduler call
      site holds a journal and should always pass it; ``journal`` is optional
      only so this module has no hard dependency on JournalStore.

    ``title``/``message`` are sanitized (secret-value redaction + length cap)
    before being used ANYWHERE -- the outbound POST and the local
    system_events failure log both see only the sanitized text.
    """
    topic = (settings.ntfy_topic or "").strip()
    if not topic:
        return False

    title = _sanitize(title, settings)
    message = _sanitize(message, settings)

    detail = None
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            status = getattr(resp, "status", 200)
        ok = 200 <= status < 300
        if not ok:
            detail = f"HTTP {status}"
    except Exception as exc:  # noqa: BLE001 - alerting must never crash a caller
        ok = False
        detail = _sanitize(str(exc), settings)

    if not ok and journal is not None:
        try:
            journal.log_system_event(
                Severity.WARNING,
                "alerts",
                f"alert send failed: {title}",
                {"message": message, "priority": priority, "detail": detail},
            )
        except Exception:  # noqa: BLE001 - best-effort; must not mask the original failure
            pass

    return ok
