"""Read-only committed producer-order proof for bounded EXPLORE-1 routing.

Lineage records identify a commit, not the bytes loaded in a long-lived process.
The dirty flag is retained explicitly; it is never relabelled as a clean tree.
No log/file/lineage timestamp is used as a decision bound.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from functools import lru_cache
from pathlib import Path

# Both Nov16 ea036e1 and Nov17 70e0063 have these exact producer bytes:
# scan_once loops/evaluates/inserts all opinions, then calls _route_campaign;
# _route_campaign resolves all routes, then freezes the draw, then proposals.
# ai_degraded continues before evaluator insertion.
_APPROVED_PRODUCERS = {
    "161329c397de6bbc521f1b99099817944116cfdc872736dbb735e63a446a7e68",
    "0ad26c4dbc26b80e19c2486ae0e7782cc9f7bd863962d4519855ae817077bafb",
}


@lru_cache(maxsize=128)
def committed_ordering(commit: str) -> dict:
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        return {"verified": False, "reason": "source_commit_missing"}
    try:
        proc = subprocess.run(
            ["git", "show", f"{commit}:alphaos/orchestrator.py"],
            cwd=Path(__file__).resolve().parents[2], capture_output=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"verified": False, "reason": "source_commit_unavailable", "commit": commit}
    if proc.returncode:
        return {"verified": False, "reason": "source_commit_unavailable", "commit": commit}
    digest = hashlib.sha256(proc.stdout).hexdigest()
    return {"verified": digest in _APPROVED_PRODUCERS, "commit": commit,
            "orchestrator_sha256": digest,
            "reason": None if digest in _APPROVED_PRODUCERS else "source_ordering_unreviewed"}


def scan_proof(candidates: list[dict], evaluations: list[dict], lineages: dict) -> dict:
    """Require every live original source lineage to agree on its commit."""
    live = {c["candidate_id"]: c for c in candidates if not c.get("shadow_tier")}
    rows = [lineages.get(row.get("lineage_id")) for row in candidates if row["candidate_id"] in live]
    rows += [lineages.get(row.get("lineage_id")) for row in evaluations if row["candidate_id"] in live]
    commits = {r.get("git_commit_sha") for r in rows if r}
    if not rows or any(r is None for r in rows) or len(commits) != 1:
        return {"verified": False, "reason": "scan_source_identity_unproven", "commits": sorted(c for c in commits if c)}
    known_rows = [r for r in rows if r is not None]
    commit = next(iter(commits))
    proof = dict(committed_ordering(commit))
    proof["git_dirty_values"] = sorted({r.get("git_dirty") for r in known_rows}, key=str)
    proof["lineage_ids"] = sorted({r["lineage_id"] for r in known_rows})
    proof["scope"] = "committed_producer_order; loaded_bytes_not_attested"
    return proof
