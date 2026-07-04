"""Claude manual second-opinion reviewer (OPTIONAL).

Hard constraints (resolved decisions / safety rules):
* Never runs automatically. The user triggers it per-candidate from the
  dashboard. The button is disabled when ``ANTHROPIC_API_KEY`` is absent.
* Its verdict is stored in its OWN table (``claude_reviews``) and NEVER
  overwrites OpenAI's evaluation, never auto-approves, never submits, and never
  bypasses risk or approval gates.

This module only produces a review object; persistence happens in the caller so
the separation is explicit.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from alphaos.ai import prompt_templates as pt
from alphaos import lineage
from alphaos.util import structured_json
from alphaos.util.ids import new_id

HTTP_TIMEOUT = 30


class ClaudeUnavailable(RuntimeError):
    """Raised if a review is requested without an Anthropic key."""


@dataclass
class ClaudeReview:
    review_id: str
    candidate_id: str
    eval_id: Optional[str]
    symbol: str
    model: str
    verdict: str                      # agree | disagree | caution
    agrees_with_openai: bool
    risk_flags: list = field(default_factory=list)
    reasoning: str = ""
    raw: Optional[dict] = None
    is_mock: bool = False
    triggered_by: str = "user"
    # PR4: measurement-only AI-call lineage.
    model_provider: Optional[str] = None
    prompt_hash: Optional[str] = None
    system_prompt_hash: Optional[str] = None

    def to_row(self) -> dict:
        return {
            "review_id": self.review_id,
            "candidate_id": self.candidate_id,
            "eval_id": self.eval_id,
            "symbol": self.symbol,
            "model": self.model,
            "verdict": self.verdict,
            "agrees_with_openai": 1 if self.agrees_with_openai else 0,
            "risk_flags_json": self.risk_flags,
            "reasoning": self.reasoning,
            "raw_json": self.raw or {},
            "is_mock": 1 if self.is_mock else 0,
            "triggered_by": self.triggered_by,
            # --- Trade Packet v1 metadata (Claude review stays manual-only and
            #     never approves; these are audit fields). ---
            "proposal_id": None,
            "user_requested": 1,
            "disagreement_with_openai": (None if self.agrees_with_openai else "disagrees"),
            "final_user_action_after_review": None,
            "model_provider": self.model_provider,
            "prompt_hash": self.prompt_hash,
            "system_prompt_hash": self.system_prompt_hash,
        }


class ClaudeReviewer:
    def __init__(self, settings, journal=None):
        self.settings = settings
        self.journal = journal
        self.model = settings.claude_review_model

    @property
    def available(self) -> bool:
        """The button is enabled only when a key is present. No key => disabled."""
        return self.settings.has_anthropic_key

    def review(self, candidate: dict, openai_eval: dict, triggered_by: str = "user") -> ClaudeReview:
        """Run a manual review. Raises if no key is configured (button should be
        disabled in that case)."""
        if not self.available:
            raise ClaudeUnavailable(
                "Claude review requires ANTHROPIC_API_KEY. The button is disabled."
            )
        return self._live_review(candidate, openai_eval, triggered_by)

    def _live_review(self, candidate, openai_eval, triggered_by):  # pragma: no cover - live path
        import anthropic  # lazy import; optional dependency

        client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        user_prompt = pt.build_claude_user_prompt(candidate, openai_eval)
        # PR4: measurement-only AI-call lineage (model provider + content hashes
        # of the actual prompt sent, never the raw prompt body).
        ai_lineage = lineage.ai_call_lineage(
            provider="anthropic", prompt=user_prompt, system_prompt=pt.CLAUDE_SYSTEM_PROMPT,
        )
        msg = client.messages.create(
            model=self.model,
            max_tokens=600,
            system=pt.CLAUDE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")
        obj = structured_json.parse_json_object(text)
        return ClaudeReview(
            review_id=new_id("clr"),
            candidate_id=candidate.get("candidate_id", ""),
            eval_id=openai_eval.get("eval_id"),
            symbol=candidate.get("symbol"),
            model=self.model,
            verdict=str(obj.get("verdict", "caution")).lower(),
            agrees_with_openai=bool(obj.get("agrees_with_openai", False)),
            risk_flags=obj.get("risk_flags", []),
            reasoning=obj.get("reasoning", ""),
            raw=obj,
            is_mock=False,
            triggered_by=triggered_by,
            model_provider=ai_lineage.get("model_provider"),
            prompt_hash=ai_lineage.get("prompt_hash"),
            system_prompt_hash=ai_lineage.get("system_prompt_hash"),
        )
