"""Stable, human-readable identifiers for AlphaOS records."""

from __future__ import annotations

import uuid


def new_id(prefix: str) -> str:
    """A short, prefixed unique id, e.g. ``ord_a1b2c3d4``.

    Prefixes make ids self-describing in logs and the journal.
    """
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
