"""Git repo lineage (PR4): commit SHA, branch, dirty-tree flag.

Never raises and never blocks a caller -- if this isn't a git checkout, git
isn't installed, or the subprocess call fails/times out for any reason, every
field resolves to None. Mirrors the "never break the caller" posture already
used by alphaos/research/last30days_provider.py's CliLast30DaysProvider
wrapper (explicit timeout, list-args, no shell=True, catch everything).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class GitInfo:
    commit_sha: Optional[str]
    branch: Optional[str]
    dirty: Optional[bool]


def _run_git(args: list, repo_root: Path) -> Optional[str]:
    """Raw (unstripped) stdout on success (returncode 0), None on any
    failure/exception/timeout. Empty string IS a valid success (e.g. a clean
    `git status --porcelain`) -- callers must not conflate '' with failure,
    only None means "unavailable"."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(repo_root), capture_output=True, text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def get_git_info(repo_root: Optional[Path] = None) -> GitInfo:
    """Best-effort repo lineage. Every field is None if unavailable -- never
    raises."""
    root = repo_root or Path(__file__).resolve().parents[2]
    commit_sha_raw = _run_git(["rev-parse", "HEAD"], root)
    commit_sha = commit_sha_raw.strip() if commit_sha_raw else None

    branch_raw = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    branch = branch_raw.strip() if branch_raw else None

    dirty: Optional[bool] = None
    if commit_sha is not None:  # only meaningful once we've confirmed this IS a git checkout
        status_raw = _run_git(["status", "--porcelain"], root)
        if status_raw is not None:
            dirty = bool(status_raw.strip())

    return GitInfo(commit_sha=commit_sha, branch=branch, dirty=dirty)
