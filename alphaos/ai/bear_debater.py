"""PR14: Red-Team Debate v0 -- the adversarial "bear" agent (v0 has only
this one role; a future triad would add 'bull'/'neutral' agents alongside
it, never replacing it).

Hard constraints (mirrors ClaudeReviewer's own "shadow, never gates
anything" law, see that module's docstring):
* Never runs before a proposal's own decision is committed -- see
  ``alphaos/debate/batch.py``'s own module docstring for the batch-end
  call-site guarantee that makes this true by construction.
* Its vote is stored in its OWN table (``agent_votes``) and NEVER changes
  a proposal's status, never blocks, never auto-approves, never bypasses
  risk/approval gates. Zero decision surface.

Unlike ``ClaudeReviewer`` (a manual, human-button feature that simply
raises when no API key is configured -- fine for a button, since a human
is right there to see the disabled state), this class has a genuine mock
path, because it runs AUTOMATICALLY inside the live scan pipeline and
every test in this codebase runs fully offline. Mirrors
``OpenAIClient``'s own ``use_mock = settings.is_mock or not
settings.has_anthropic_key`` convention exactly, not ClaudeReviewer's.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

from alphaos import lineage
from alphaos.ai import prompt_templates as pt
from alphaos.util import structured_json
from alphaos.util.ids import new_id

HTTP_TIMEOUT = 30

# Deterministic per-candidate mock stance -- a real, if crude, "opinion"
# rather than a constant, so mock-mode tests can exercise all three
# stances without needing a live key. Seeded off candidate_id so the same
# mock run is reproducible.
_MOCK_STANCES = ("oppose", "neutral", "support")


@dataclass
class BearVote:
    vote_id: str
    proposal_id: str
    candidate_id: str
    scan_batch_id: Optional[str]
    agent_role: str
    stance: str                       # oppose | neutral | support
    conviction: float                 # 0.0-1.0
    failure_modes: list = field(default_factory=list)
    invalidation_triggers: list = field(default_factory=list)
    reasoning: str = ""
    is_mock: bool = False
    model_provider: Optional[str] = None
    prompt_hash: Optional[str] = None
    system_prompt_hash: Optional[str] = None

    def to_row(self) -> dict:
        return {
            "vote_id": self.vote_id,
            "proposal_id": self.proposal_id,
            "candidate_id": self.candidate_id,
            "scan_batch_id": self.scan_batch_id,
            "agent_role": self.agent_role,
            "stance": self.stance,
            "conviction": self.conviction,
            "failure_modes_json": self.failure_modes,
            "invalidation_triggers_json": self.invalidation_triggers,
            "reasoning": self.reasoning,
            "is_mock": 1 if self.is_mock else 0,
            "model_provider": self.model_provider,
            "prompt_hash": self.prompt_hash,
            "system_prompt_hash": self.system_prompt_hash,
        }


class BearDebater:
    def __init__(self, settings, journal=None):
        self.settings = settings
        self.journal = journal
        self.use_mock = settings.is_mock or not settings.has_anthropic_key
        self.model = settings.debate_bear_model

    def debate(self, candidate: dict, proposal: dict, scan_batch_id: Optional[str] = None) -> BearVote:
        """Cast one bear vote on an already-committed proposal. Never
        raises -- a live-path failure degrades to a mock-shaped vote with
        ``is_mock=True`` (matching OpenAIClient's own "never let an AI call
        crash the caller" posture), logged as a system event rather than
        propagated into the scan batch that is calling this post-commit."""
        if self.use_mock:
            return self._mock_debate(candidate, proposal, scan_batch_id)
        try:
            return self._live_debate(candidate, proposal, scan_batch_id)
        except Exception as exc:  # pragma: no cover - live path
            if self.journal is not None:
                self.journal.log_system_event(
                    "error", "debate",
                    f"Bear debate failed for proposal {proposal.get('proposal_id')}; recorded as mock.",
                    {"error": str(exc)},
                )
            return self._mock_debate(candidate, proposal, scan_batch_id)

    def _mock_debate(self, candidate: dict, proposal: dict, scan_batch_id: Optional[str]) -> BearVote:
        seed = f"{proposal.get('proposal_id', '')}"
        rng = random.Random(seed)
        stance = _MOCK_STANCES[rng.randrange(len(_MOCK_STANCES))]
        return BearVote(
            vote_id=new_id("vote"),
            proposal_id=proposal.get("proposal_id", ""),
            candidate_id=candidate.get("candidate_id", ""),
            scan_batch_id=scan_batch_id,
            agent_role="bear",
            stance=stance,
            conviction=round(rng.uniform(0.1, 0.9), 2),
            failure_modes=["mock: thesis fails to develop"] if stance == "oppose" else [],
            invalidation_triggers=["mock: close below entry"] if stance == "oppose" else [],
            reasoning="Mock bear vote (no ANTHROPIC_API_KEY or ALPHAOS_MODE=mock).",
            is_mock=True,
        )

    def _live_debate(self, candidate, proposal, scan_batch_id):  # pragma: no cover - live path
        import anthropic  # lazy import; optional dependency

        client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        user_prompt = pt.build_bear_user_prompt(candidate, proposal)
        ai_lineage = lineage.ai_call_lineage(
            provider="anthropic", prompt=user_prompt, system_prompt=pt.BEAR_SYSTEM_PROMPT,
        )
        msg = client.messages.create(
            model=self.model,
            max_tokens=600,
            system=pt.BEAR_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            timeout=HTTP_TIMEOUT,
        )
        text = "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")
        obj = structured_json.parse_json_object(text)
        stance = str(obj.get("stance", "neutral")).lower()
        if stance not in ("oppose", "neutral", "support"):
            stance = "neutral"
        conviction = max(0.0, min(1.0, float(obj.get("conviction", 0.0))))
        return BearVote(
            vote_id=new_id("vote"),
            proposal_id=proposal.get("proposal_id", ""),
            candidate_id=candidate.get("candidate_id", ""),
            scan_batch_id=scan_batch_id,
            agent_role="bear",
            stance=stance,
            conviction=conviction,
            failure_modes=obj.get("failure_modes", []),
            invalidation_triggers=obj.get("invalidation_triggers", []),
            reasoning=obj.get("reasoning", ""),
            is_mock=False,
            model_provider=ai_lineage.get("model_provider"),
            prompt_hash=ai_lineage.get("prompt_hash"),
            system_prompt_hash=ai_lineage.get("system_prompt_hash"),
        )
