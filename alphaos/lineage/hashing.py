"""Deterministic hashing for lineage stamps (PR4).

First hashlib usage in this codebase -- defines the one convention every
lineage hash should use: canonicalize via json.dumps(sort_keys=True,
default=str) so key order never affects the hash, then sha256, truncated to
a short hex digest (16 hex chars / 64 bits is plenty of collision safety for
an audit-trail label at this system's scale and keeps stored/printed values
short).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_DIGEST_LENGTH = 16  # hex chars

# Known credential/capability-bearing fields in alphaos.config.settings.Settings
# -- never hash/store these, even redacted. Covers both obvious credentials
# (api keys) and fields whose VALUE alone grants access even though the name
# doesn't say "key"/"secret" (ntfy_topic is the entire auth model for a public
# ntfy.sh topic; last30days_cmd is a user-editable command-template override
# that could embed an inline token). This list is a best-effort audit, not a
# structural guarantee -- do not assume it is exhaustive if Settings grows a
# new field; prefer an allowlist-style review when adding new lineage-hashed
# config surfaces.
SECRET_SETTINGS_FIELDS = frozenset({
    "openai_api_key",
    "anthropic_api_key",
    "massive_api_key",
    "benzinga_api_key",
    "alpaca_api_key",
    "alpaca_secret_key",
    "ntfy_topic",
    "last30days_cmd",
    # Private local filesystem paths -- not credentials, but they leak the
    # deployment's directory layout into the config-hash PREIMAGE (the digest
    # itself never stores them, but keep them out of the preimage entirely as
    # defense-in-depth in case the preimage is ever logged) and are deployment
    # environment, not decision-relevant config, so excluding them also keeps
    # the config hash stable across machines.
    "db_path",
    "last30days_repo_path",
    "last30days_python",
    # OPS-B (audit MEDIUM, 2026-07-10): an rclone remote string can embed
    # auth info depending on how the operator's own rclone config is set
    # up, and a plain disk path leaks deployment layout -- same rationale
    # as db_path/last30days_repo_path above. backup2_method is a bounded
    # enum ("", "rclone", "disk") and stays hashed.
    "backup2_dest",
})


def canonical_json(data: Any) -> str:
    """Deterministic JSON serialization: sorted keys, stable str() fallback
    for non-JSON-native types (enums, etc)."""
    return json.dumps(data, sort_keys=True, default=str)


def stable_hash(data: Any) -> str:
    """Short, deterministic hex digest of data's canonical JSON form. The same
    input (regardless of dict key order) always produces the same hash;
    changing any contained value changes it."""
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]


def strip_secrets(d: dict) -> dict:
    """Copy of `d` with every key in SECRET_SETTINGS_FIELDS removed.
    Exact-key match only -- callers pass already-flattened dicts (e.g.
    dataclasses.asdict(settings)), not nested structures. SECRET_SETTINGS_FIELDS
    is a maintained-by-hand list, not a structural guarantee -- callers storing
    or logging the result should still avoid treating it as an exhaustive
    secret scrubber for arbitrary future dicts."""
    return {k: v for k, v in d.items() if k not in SECRET_SETTINGS_FIELDS}
