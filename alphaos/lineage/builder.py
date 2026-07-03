"""Reusable decision-lineage stamp builder (PR4).

``get_or_create_lineage_id()`` is the ONE function every decision call site
uses to get a lineage_id for a new row -- it is cheap to call repeatedly
(idempotent get-or-create against ``lineage_snapshots``, keyed by a content
hash of the environment/config snapshot) so callers never need to cache or
thread a lineage_id through multiple layers themselves; call it right before
the INSERT.

MEASUREMENT-ONLY, like candidate_outcomes/protection_checks/job_runs before
it: this module never reads a lineage value back to influence a decision,
and every public function here fails toward None/no-op rather than raising
-- a lineage stamp failing must never block or alter a real trading decision.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from alphaos import __version__ as APP_VERSION
from alphaos.journal.schema import SCHEMA_VERSION
from alphaos.lineage import versions
from alphaos.lineage.config_snapshot import build_config_hashes
from alphaos.lineage.git_info import get_git_info
from alphaos.lineage.hashing import stable_hash


def _universe_version_hash() -> Optional[str]:
    try:
        from alphaos.scanner.candidate_scanner import DEFAULT_UNIVERSE
        return stable_hash(sorted(DEFAULT_UNIVERSE))
    except Exception:
        return None


def _snapshot_fields(settings) -> dict:
    git = get_git_info()
    hashes = build_config_hashes(settings)
    return {
        "git_commit_sha": git.commit_sha,
        "git_branch": git.branch,
        "git_dirty": None if git.dirty is None else (1 if git.dirty else 0),
        "app_version": APP_VERSION,
        "schema_version": SCHEMA_VERSION,
        **hashes,
        "scanner_version": versions.SCANNER_VERSION,
        "scanner_rule_version": versions.SCANNER_RULE_VERSION,
        "universe_version_hash": _universe_version_hash(),
        "playbook_version": versions.PLAYBOOK_VERSION,
        "strategy_version": versions.STRATEGY_VERSION,
        "feature_engine_version": versions.FEATURE_ENGINE_VERSION,
        "market_data_provider": getattr(settings, "data_provider", None),
    }


def get_or_create_lineage_id(journal, settings) -> Optional[str]:
    """Lineage_id for the CURRENT environment/config snapshot, inserting a new
    ``lineage_snapshots`` row the first time this exact snapshot is seen
    (idempotent -- mirrors candidate_outcomes' NOT-EXISTS seed pattern; a
    UNIQUE index backstops the race between two concurrent first-callers,
    matching job_runs' lock-row pattern). Never raises: any failure (journal
    error, git unavailable, unexpected settings shape) returns None rather
    than blocking the caller's real insert -- a missing lineage_id is a
    measurement gap, never a reason to fail a scan/proposal/reject/override.
    """
    try:
        fields = _snapshot_fields(settings)
        lineage_id = stable_hash(fields)
        existing = journal.one(
            "SELECT lineage_id FROM lineage_snapshots WHERE lineage_id = ?", (lineage_id,)
        )
        if existing:
            return lineage_id
        try:
            journal.insert("lineage_snapshots", {"lineage_id": lineage_id, **fields})
        except sqlite3.IntegrityError:
            pass  # another caller inserted this identical snapshot first -- same lineage_id either way
        return lineage_id
    except Exception:
        return None


def ai_call_lineage(*, provider: Optional[str], prompt: Optional[str],
                     system_prompt: Optional[str] = None) -> dict:
    """Per-call AI lineage for a single-model table (openai_evaluations,
    claude_reviews, last30days_polarity): model_provider + content hashes of
    the actual prompt text sent. Never the raw prompt body itself -- a hash
    is enough to prove/compare what was sent, per PR4's "do not store huge
    prompt bodies if hashes are enough" guidance. Never raises."""
    try:
        return {
            "model_provider": provider,
            "prompt_hash": stable_hash(prompt) if prompt else None,
            "system_prompt_hash": stable_hash(system_prompt) if system_prompt else None,
        }
    except Exception:
        return {"model_provider": provider, "prompt_hash": None, "system_prompt_hash": None}


def combine_ai_lineage(**named_sub_calls) -> Optional[str]:
    """Combine multiple ai_call_lineage()-shaped dicts (e.g. one for the
    playbook classifier's call, one for the polarity classifier's call),
    keyed by caller-supplied labels, into one JSON string for
    decision_adjustments.ai_lineage_json -- a single composite decision row
    can reflect more than one model call, unlike the single-model AI tables.
    Falsy/None sub-calls are dropped. Returns None if nothing to record."""
    import json
    present = {k: v for k, v in named_sub_calls.items() if v}
    if not present:
        return None
    try:
        return json.dumps(present, sort_keys=True, default=str)
    except Exception:
        return None
