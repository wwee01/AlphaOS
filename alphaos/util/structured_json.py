"""Defensive structured-JSON parsing for LLM outputs.

OpenAI (and the optional Claude reviewer) are instructed to return JSON only —
no prose, no markdown fences. Models still occasionally wrap output in ```json
fences or add stray text, so we parse defensively rather than trusting them.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional


class StructuredJsonError(ValueError):
    """Raised when an LLM response cannot be coerced into the expected JSON."""


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text.strip())


def _extract_object(text: str) -> Optional[str]:
    """Return the first balanced {...} block in ``text`` if any."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse ``text`` into a JSON object, tolerating fences/stray prose.

    Raises StructuredJsonError if no JSON object can be recovered.
    """
    if text is None:
        raise StructuredJsonError("empty LLM response")
    candidate = _strip_fences(str(text))
    # Fast path: clean JSON.
    try:
        obj = json.loads(candidate)
        if isinstance(obj, dict):
            return obj
        raise StructuredJsonError(f"expected JSON object, got {type(obj).__name__}")
    except json.JSONDecodeError:
        pass
    # Recovery path: pull the first balanced object out of the noise.
    block = _extract_object(candidate)
    if block is None:
        raise StructuredJsonError("no JSON object found in LLM response")
    try:
        obj = json.loads(block)
    except json.JSONDecodeError as exc:
        raise StructuredJsonError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise StructuredJsonError("recovered JSON was not an object")
    return obj


def require_keys(obj: dict[str, Any], keys: list[str]) -> None:
    """Validate that all ``keys`` are present, else raise StructuredJsonError."""
    missing = [k for k in keys if k not in obj]
    if missing:
        raise StructuredJsonError(f"missing required keys: {missing}")
