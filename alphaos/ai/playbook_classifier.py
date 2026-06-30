"""AI Category / Playbook Classifier (Roadmap 2.3).

Takes a compact CandidatePacket and returns a strict, schema-validated playbook
label. DISTINCT from ``OpenAIClient.evaluate`` (which owns the trade levels +
decision) — this only categorises the opportunity. Its decision is ADVISORY and
downstream can only DOWNGRADE the trade decision, never create a PROPOSE.

Safety:
* mock mode (no key / offline) → deterministic, schema-valid label.
* live mode → lazy OpenAI call, compact prompt, capped tokens, no web browsing.
* ANY malformed/missing/exception output → ``fail_safe`` → Other/Unclassified +
  REJECT (never PROPOSE).
* ``coerce_and_validate`` enforces the official label set + confidence floor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from alphaos.ai import prompt_templates as pt
from alphaos.ai.label_validation import LABEL_OUTPUT_KEYS, coerce_and_validate
from alphaos.constants import (
    Decision,
    LABEL_OTHER,
    LABEL_VERSION_V1,
    LabelSource,
    OFFICIAL_LABELS,
    Severity,
)
from alphaos.util import structured_json
from alphaos.util.ids import new_id

HTTP_TIMEOUT = 30


@dataclass
class PlaybookClassification:
    label_id: str
    candidate_id: str
    symbol: str
    primary_label: str
    secondary_labels: list
    candidate_tags: list           # UNOFFICIAL ai-suggested tags
    risk_tags: list
    direction: str
    label_decision: str            # Decision value (advisory; downgrade-only downstream)
    confidence: float
    reason_for_label: str
    thesis_stub: str
    invalidation: str
    main_risk: str
    missing_context: list
    suggested_new_tags: list
    label_version: str
    label_source: str
    validation_status: str
    model: str
    is_mock: bool
    raw: dict = field(default_factory=dict)
    # Roadmap 2.8 (Part B) — ADVISORY reasoning (never changes the decision).
    missing_conditions: list = field(default_factory=list)
    upgrade_blockers: list = field(default_factory=list)
    proposal_readiness: str = "unclear"
    what_would_upgrade: str = ""

    def to_row(self, packet_id: Optional[str], scan_batch_id: Optional[str], frozen_at_utc: str) -> dict:
        return {
            "label_id": self.label_id,
            "candidate_id": self.candidate_id,
            "packet_id": packet_id,
            "scan_batch_id": scan_batch_id,
            "symbol": self.symbol,
            "primary_label": self.primary_label,
            "secondary_labels_json": self.secondary_labels,
            "candidate_tags_json": self.candidate_tags,
            "risk_tags_json": self.risk_tags,
            "direction": self.direction,
            "label_decision": self.label_decision,
            "label_confidence": self.confidence,
            "reason_for_label": self.reason_for_label,
            "thesis_stub": self.thesis_stub,
            "invalidation": self.invalidation,
            "main_risk": self.main_risk,
            "missing_context_json": self.missing_context,
            "suggested_new_tags_json": self.suggested_new_tags,
            "label_version": self.label_version,
            "label_source": self.label_source,
            "validation_status": self.validation_status,
            "model": self.model,
            "is_mock": 1 if self.is_mock else 0,
            "raw_json": self.raw or {},
            "label_frozen_at_utc": frozen_at_utc,
            "post_trade_review_label": None,  # reserved; never rewritten in v1
            "missing_conditions_json": self.missing_conditions,
            "upgrade_blockers_json": self.upgrade_blockers,
            "proposal_readiness": self.proposal_readiness,
            "what_would_upgrade": self.what_would_upgrade,
        }


class PlaybookClassifier:
    def __init__(self, settings, journal=None):
        self.settings = settings
        self.journal = journal
        self.use_mock = settings.is_mock or not settings.has_openai_key
        self.model = settings.label_model

    # ---------------------------------------------------------------- public
    def classify(self, packet) -> PlaybookClassification:
        """Classify a CandidatePacket. Never raises — fails safe to REJECT."""
        if self.use_mock:
            return self._mock_classify(packet)
        try:  # pragma: no cover - live path
            return self._live_classify(packet)
        except Exception as exc:  # pragma: no cover - network/SDK/parse
            if self.journal is not None:
                self.journal.log_system_event(
                    Severity.WARNING, "labeller",
                    f"label classify failed for {getattr(packet, 'symbol', '?')}; failing safe to reject.",
                    {"error": str(exc)},
                )
            return self._fail_safe(packet, "live_exception")

    # ------------------------------------------------------------------ mock
    def _mock_classify(self, packet) -> PlaybookClassification:
        momentum = float(getattr(packet, "momentum_score", None) or 0.0)
        structure = getattr(packet, "structure_hint", "range")
        direction = getattr(packet, "direction", "long")
        thr = self.settings.label_propose_threshold

        if structure == "breakout":
            primary = "Breakout"
        elif structure == "reversal":
            primary = "Mean Reversion"
        elif momentum >= thr:
            primary = "Momentum"
        else:
            primary = LABEL_OTHER

        confidence = round(min(0.9, 0.4 + 0.5 * momentum), 3)
        decision = (
            Decision.PROPOSE.value
            if (primary != LABEL_OTHER and momentum >= thr)
            else Decision.WATCH.value
        )
        obj = {
            "symbol": packet.symbol,
            "primary_label": primary,
            "secondary_labels": [],
            "direction": direction,
            "decision": decision,
            "confidence": confidence,
            "reason_for_label": f"no-news deterministic: {getattr(packet, 'shortlist_reason', '')}",
            "thesis_stub": getattr(packet, "setup_hint", ""),
            "invalidation": "loses the level / structure that defined the setup",
            "main_risk": "no catalyst context (no-news mode); price-action only",
            "risk_tags": ["no_news_context"],
            "missing_context": list(getattr(packet, "missing_data_flags", []) or []) + ["news", "last30days"],
            "suggested_new_tags": [],
            # Part B advisory reasoning (deterministic in mock).
            "proposal_readiness": ("ready" if decision == Decision.PROPOSE.value
                                   else "near_action" if momentum >= thr * 0.7 else "developing"),
            "missing_conditions": ([] if decision == Decision.PROPOSE.value else ["clear_entry_trigger"]),
            "upgrade_blockers": ([] if decision == Decision.PROPOSE.value else ["momentum_below_threshold"]),
            "what_would_upgrade": ("" if decision == Decision.PROPOSE.value
                                   else "a clean entry trigger with sustained relative volume"),
        }
        clean, status = coerce_and_validate(obj, self.settings)
        return self._build(packet, clean, status, LabelSource.MOCK.value, "mock", True,
                           raw={"mock": True, "structure": structure, "momentum": momentum})

    # ------------------------------------------------------------------ live
    def _live_classify(self, packet) -> PlaybookClassification:  # pragma: no cover
        from openai import OpenAI  # lazy import; optional dependency

        client = OpenAI(api_key=self.settings.openai_api_key)
        user_prompt = pt.build_label_user_prompt(packet.to_prompt_dict(), sorted(OFFICIAL_LABELS))
        resp = client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            # gpt-5.x chat.completions rejects `max_tokens`; `max_completion_tokens`
            # is the supported param (and is accepted by gpt-4o too — forward-safe).
            max_completion_tokens=self.settings.label_max_output_tokens,
            messages=[
                {"role": "system", "content": pt.LABEL_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            timeout=HTTP_TIMEOUT,
        )
        obj = structured_json.parse_json_object(resp.choices[0].message.content)
        # Missing keys are tolerated by coerce_and_validate (degrade safely), but a
        # total absence of the core fields should fail safe.
        if "primary_label" not in obj and "decision" not in obj:
            return self._fail_safe(packet, "malformed_output")
        clean, status = coerce_and_validate(obj, self.settings)
        return self._build(packet, clean, status, LabelSource.OPENAI.value, self.model, False, raw=obj)

    # ------------------------------------------------------------- fail-safe
    def _fail_safe(self, packet, reason: str) -> PlaybookClassification:
        clean = {
            "primary_label": LABEL_OTHER,
            "secondary_labels": [],
            "candidate_tags": [],
            "risk_tags": ["label_unavailable"],
            "direction": getattr(packet, "direction", "none"),
            "decision": Decision.REJECT.value,   # fail safe: never propose
            "confidence": 0.0,
            "reason_for_label": f"AI label unavailable ({reason}); failed safe to reject.",
            "thesis_stub": "",
            "invalidation": "",
            "main_risk": "no AI classification available",
            "missing_context": ["ai_label"],
            "suggested_new_tags": [],
        }
        return self._build(packet, clean, reason, LabelSource.FAIL_SAFE.value,
                           self.model if not self.use_mock else "mock", self.use_mock, raw={"fail_safe": reason})

    # --------------------------------------------------------------- builder
    def _build(self, packet, clean: dict, status: str, source: str, model: str,
               is_mock: bool, raw: dict) -> PlaybookClassification:
        return PlaybookClassification(
            label_id=new_id("lbl"),
            candidate_id=getattr(packet, "candidate_id", ""),
            symbol=getattr(packet, "symbol", None),
            primary_label=clean["primary_label"],
            secondary_labels=clean["secondary_labels"],
            candidate_tags=clean["candidate_tags"],
            risk_tags=clean["risk_tags"],
            direction=clean["direction"],
            label_decision=clean["decision"],
            confidence=clean["confidence"],
            reason_for_label=clean["reason_for_label"],
            thesis_stub=clean["thesis_stub"],
            invalidation=clean["invalidation"],
            main_risk=clean["main_risk"],
            missing_context=clean["missing_context"],
            suggested_new_tags=clean["suggested_new_tags"],
            label_version=LABEL_VERSION_V1,
            label_source=source,
            validation_status=status,
            model=model,
            is_mock=is_mock,
            raw=raw,
            missing_conditions=clean.get("missing_conditions", []),
            upgrade_blockers=clean.get("upgrade_blockers", []),
            proposal_readiness=clean.get("proposal_readiness", "unclear"),
            what_would_upgrade=clean.get("what_would_upgrade", ""),
        )
