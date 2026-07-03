"""Decision lineage stamping (PR4): measurement/audit-only metadata answering
"which code/config/model/prompt/data/scheduler context produced this
decision?" for every candidate/proposal/reject/override/outcome row.

PURE MEASUREMENT, like candidate_outcomes/protection_checks/job_runs before
it: never read by any gate/eval/labeller/risk/execution/approval path, and
every public function here fails toward None/no-op rather than raising --
a lineage stamp failing must never block or alter a real trading decision.
"""

from __future__ import annotations

from alphaos.lineage.builder import ai_call_lineage, combine_ai_lineage, get_or_create_lineage_id
from alphaos.lineage.git_info import GitInfo, get_git_info
from alphaos.lineage.hashing import stable_hash, strip_secrets

__all__ = [
    "ai_call_lineage",
    "combine_ai_lineage",
    "get_or_create_lineage_id",
    "GitInfo",
    "get_git_info",
    "stable_hash",
    "strip_secrets",
]
