"""PR12: Hypothesis Engine v0 (registry-first).

Seeds `hypothesis_proposals` with 8 human pre-registered hypotheses, each
backed by a `preregistrations` row (PORT-1) -- the registry IS the load-
bearing part (PD#4); the nightly LLM generator is deferred to v1.1, gated on
this registry demonstrating it can resolve hypotheses at all (verdict V2).

Zero decision surface: this package only reads already-journaled tables and
writes to `hypothesis_proposals`/`preregistrations`. Never read by any
gate/eval/labeller/risk/execution path.
"""

from alphaos.hypotheses.constants import DraftStatus, HypothesisStatus, RiskClass, SEEDED_HYPOTHESES
from alphaos.hypotheses.generator import run_hypothesis_generate
from alphaos.hypotheses.proposer import accept_draft, list_drafts, reject_draft
from alphaos.hypotheses.registry import (
    check_status_change_preconditions,
    mark_hypothesis_status,
    propose_hypothesis,
    seed_all,
)
from alphaos.hypotheses.resolver import resolve_due_hypotheses

__all__ = [
    "RiskClass",
    "HypothesisStatus",
    "DraftStatus",
    "SEEDED_HYPOTHESES",
    "propose_hypothesis",
    "seed_all",
    "resolve_due_hypotheses",
    "mark_hypothesis_status",
    "check_status_change_preconditions",
    "list_drafts",
    "accept_draft",
    "reject_draft",
    "run_hypothesis_generate",
]
