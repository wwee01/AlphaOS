"""ntfy notification client (optional).

No-op when ``NTFY_TOPIC`` is unset. Best-effort delivery with a short timeout;
failures are logged, never raised, so notifications can't break the pipeline.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from alphaos.constants import Severity

HTTP_TIMEOUT = 8


class NtfyClient:
    def __init__(self, settings, journal=None):
        self.settings = settings
        self.journal = journal

    @property
    def enabled(self) -> bool:
        return bool(self.settings.ntfy_topic)

    def notify(self, message: str, title: str = "AlphaOS", priority: str = "default") -> bool:
        if not self.enabled:
            return False
        url = f"https://ntfy.sh/{self.settings.ntfy_topic}"
        try:  # pragma: no cover - network
            req = urllib.request.Request(
                url,
                data=message.encode("utf-8"),
                headers={"Title": title, "Priority": priority},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
            return True
        except (urllib.error.URLError, ValueError) as exc:  # pragma: no cover
            if self.journal is not None:
                self.journal.log_system_event(
                    Severity.WARNING, "notifications", "ntfy send failed.", {"error": str(exc)}
                )
            return False
