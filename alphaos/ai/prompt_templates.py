"""Prompt templates for the OpenAI primary engine and the Claude reviewer.

OpenAI MUST return a single JSON object only — no prose, no markdown fences. The
schema is spelled out explicitly and the parser (util.structured_json) is
defensive in case the model misbehaves.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from alphaos.scanner.scan_context import ScanContext

# Prompt-template generation marker (recorded on each evaluation for audit).
PROMPT_TEMPLATE_VERSION = "v1"


def _public(candidate: "Union[dict, ScanContext]") -> dict:
    """Strip private ``_``-prefixed keys before ANY candidate is serialized
    into a prompt.

    Historically the scanner/orchestrator stashed full enrichment objects on
    the candidate dict under ``_``-prefixed keys (``_snapshot``/``_interest``/
    ``_catalyst``/``_last30``/``_polarity``/``_earnings``/``_packet_id``) as
    internal plumbing between pipeline stages. Serializing the whole dict here
    leaked catalyst/last30days/polarity text into the NO-NEWS eval prompt — a
    prompt whose system message asserts no news was provided — and duplicated
    the snapshot (token bloat). Found + reproduced by the 2026-07-06 exit
    review (CRITICAL); mock mode never builds prompts, which is why no test
    caught it. The scanner/orchestrator now carry that plumbing on
    ``ScanContext`` typed attributes instead of ``row`` (see
    ``alphaos/scanner/scan_context.py``), which makes a private key in
    ``row``/``candidate.items()`` structurally impossible going forward. This
    filter stays anyway as the enforced chokepoint (defense in depth) and to
    keep supporting plain dicts (e.g. test fixtures). (The labeller path is
    unaffected — it serializes the explicitly whitelisted candidate packet
    instead.)
    """
    return {k: v for k, v in candidate.items() if not (isinstance(k, str) and k.startswith("_"))}

# Required keys in the OpenAI evaluation JSON object.
OPENAI_EVAL_KEYS = [
    "symbol",
    "direction",
    "entry",
    "stop",
    "target",
    "max_holding_days",
    "expected_r",
    "confidence",
    "decision",
    "reasoning_summary",
    "news_sources",
    "data_freshness_status",
    "catalyst_type",
    "sentiment",
    "risk_flags",
]

OPENAI_SYSTEM_PROMPT = (
    "You are AlphaOS's primary trade-evaluation engine for a paper-trading "
    "system. The active playbook is NEWS-CONFIRMED MOMENTUM CONTINUATION on "
    "liquid US stocks/ETFs, swing horizon 1-5 trading days. You are risk-first: "
    "survive, then learn, then profit. If there is no verifiable news catalyst, "
    "you must NOT 'propose'; downgrade to 'watch' or 'reject'. If data is stale "
    "or unverifiable, reject. Respond with a SINGLE JSON object ONLY. No prose, "
    "no markdown, no code fences."
)

# v1 runs in NO-NEWS mode: momentum continuation (no-news baseline).
NO_NEWS_SYSTEM_PROMPT = (
    "You are AlphaOS's primary trade-evaluation engine for a paper-trading "
    "system. The active playbook for v1 is MOMENTUM CONTINUATION (NO-NEWS "
    "BASELINE) on liquid US stocks/ETFs, swing horizon 1-5 trading days. "
    "The system is operating in NO-NEWS MODE: no verified news, catalyst feed, "
    "analyst headline, company event, macro event, or web source has been "
    "provided to you. You MUST base the thesis ONLY on price action, volume, "
    "relative strength, trend structure, and risk/reward. You MUST NOT invent, "
    "infer, assume, or imply any news or catalyst (no company news, analyst "
    "up/downgrade, earnings, FDA, M&A, macro headline, social-media claim, or "
    "'likely news-driven' language). Mark news/catalyst fields as unavailable. "
    "If data is stale or unverifiable, reject. You are risk-first. Respond with "
    "a SINGLE JSON object ONLY. No prose, no markdown, no code fences."
)

# Required keys for the no-news evaluation output.
NO_NEWS_EVAL_KEYS = [
    "symbol",
    "direction",
    "entry",
    "stop",
    "target",
    "max_holding_days",
    "expected_r",
    "confidence",
    "decision",
    "reasoning_summary",
    "catalyst",
    "news_status",
    "news_sources",
    "data_freshness_status",
    "risk_flags",
]


def build_no_news_user_prompt(
    candidate: "Union[dict, ScanContext]", snapshot: dict, freshness_status: str
) -> str:
    """User prompt for no-news mode. Forces the catalyst/news sentinels."""
    schema = {
        "symbol": "string",
        "direction": "long | short",
        "entry": "number",
        "stop": "number",
        "target": "number",
        "max_holding_days": "integer 1-5",
        "expected_r": "number (reward/risk)",
        "confidence": "number 0..1",
        "decision": "reject | watch | propose",
        "reasoning_summary": "string (<= 80 words; PRICE/VOLUME/STRUCTURE ONLY)",
        "catalyst": "MUST be exactly 'not_available_v1'",
        "news_status": "MUST be exactly 'disabled_v1'",
        "news_sources": "MUST be an empty list []",
        "data_freshness_status": "usable | stale | unverifiable",
        "risk_flags": ["list of short risk flag strings"],
    }
    return (
        "Evaluate this candidate in NO-NEWS MODE. Return JSON ONLY matching the "
        "schema. Base the thesis ONLY on price action, volume, relative strength, "
        "trend structure, and risk/reward. Do NOT reference or invent any news or "
        "catalyst.\n\n"
        f"SCHEMA:\n{json.dumps(schema, indent=2)}\n\n"
        f"CANDIDATE:\n{json.dumps(_public(candidate), default=str)}\n\n"
        f"MARKET_SNAPSHOT:\n{json.dumps(snapshot, default=str)}\n\n"
        f"DATA_FRESHNESS:\n{freshness_status}\n\n"
        "Rules: stale/unverifiable data => 'reject'. Long stop below entry; short "
        "stop above entry; target on the profit side. catalyst='not_available_v1', "
        "news_status='disabled_v1', news_sources=[]. Output the JSON object now."
    )


def build_openai_user_prompt(
    candidate: "Union[dict, ScanContext]", snapshot: dict, news_items: list[dict],
    freshness_status: str
) -> str:
    """Construct the user prompt with the strict JSON schema instruction."""
    schema = {
        "symbol": "string",
        "direction": "long | short",
        "entry": "number",
        "stop": "number",
        "target": "number",
        "max_holding_days": "integer 1-5",
        "expected_r": "number (reward/risk)",
        "confidence": "number 0..1",
        "decision": "reject | watch | propose",
        "reasoning_summary": "string (<= 80 words)",
        "news_sources": ["list of source urls/names actually used"],
        "data_freshness_status": "usable | stale | unverifiable",
        "catalyst_type": "string or null",
        "sentiment": "bullish | bearish | neutral | unclear",
        "risk_flags": ["list of short risk flag strings"],
    }
    return (
        "Evaluate this candidate. Return JSON ONLY matching the schema.\n\n"
        f"SCHEMA:\n{json.dumps(schema, indent=2)}\n\n"
        f"CANDIDATE:\n{json.dumps(_public(candidate), default=str)}\n\n"
        f"MARKET_SNAPSHOT:\n{json.dumps(snapshot, default=str)}\n\n"
        f"NEWS_ITEMS:\n{json.dumps(news_items, default=str)}\n\n"
        f"DATA_FRESHNESS:\n{freshness_status}\n\n"
        "Rules: no verifiable news => not 'propose'. Stale/unverifiable data => "
        "'reject'. Long stop below entry; short stop above entry; target on the "
        "profit side. Output the JSON object now."
    )


# --- Roadmap 2.3: AI category / playbook labelling --------------------------
LABEL_SYSTEM_PROMPT = (
    "You are AlphaOS's category/playbook CLASSIFIER for a paper-trading system. "
    "You do NOT size, approve, submit, or execute trades — you only classify what "
    "KIND of opportunity a shortlisted candidate is, from a FIXED official label "
    "set, based ONLY on the compact deterministic evidence provided. You are in "
    "NO-NEWS mode: no news/catalyst/last30days context is available, so do NOT "
    "invent or assume any catalyst. primary_label MUST be exactly one of the "
    "official labels. You may suggest new tags, but they are unofficial. If the "
    "evidence is weak or unclear, choose 'Other/Unclassified' and decision "
    "'watch' or 'reject' — never force 'propose'. Respond with a SINGLE JSON "
    "object ONLY. No prose, no markdown, no code fences."
)


def build_label_user_prompt(packet: dict, official_labels: list[str]) -> str:
    """Compact user prompt for the playbook classifier. ``packet`` is the
    whitelisted compact evidence dict (never raw market data)."""
    schema = {
        "symbol": "string",
        "primary_label": f"one of: {sorted(official_labels)}",
        "secondary_labels": "list of official labels (may be empty)",
        "direction": "long | short | none",
        "decision": "PROPOSE | WATCH | REJECT",
        "confidence": "number 0..1",
        "reason_for_label": "string (<= 60 words, evidence only)",
        "thesis_stub": "string (<= 40 words)",
        "invalidation": "string (what would void this)",
        "main_risk": "string",
        "risk_tags": ["list of short risk tags"],
        "missing_context": ["list of missing data/context"],
        "suggested_new_tags": ["optional unofficial tag suggestions"],
        "missing_conditions": ["what is missing for a proposal (e.g. clear_entry_trigger)"],
        "upgrade_blockers": ["what currently blocks an upgrade (e.g. mixed_evidence)"],
        "proposal_readiness": "one of: not_ready | developing | near_action | ready",
        "what_would_upgrade": "string: what concrete change would make this proposable",
    }
    return (
        "Classify this shortlisted candidate. Return JSON ONLY matching the "
        "schema. primary_label MUST be from the official set; do NOT invent "
        "official labels or any news/catalyst. Weak/unclear => "
        "'Other/Unclassified' + 'watch'/'reject'.\n\n"
        f"OFFICIAL_LABELS:\n{json.dumps(sorted(official_labels))}\n\n"
        f"SCHEMA:\n{json.dumps(schema, indent=2)}\n\n"
        f"CANDIDATE_PACKET:\n{json.dumps(packet, default=str)}\n\n"
        "Output the JSON object now."
    )


CLAUDE_SYSTEM_PROMPT = (
    "You are an INDEPENDENT second-opinion risk reviewer for a paper-trading "
    "system. You do NOT approve, submit, or size trades. You review another "
    "model's evaluation and flag risks. Respond with a SINGLE JSON object ONLY: "
    '{"verdict": "agree|disagree|caution", "agrees_with_openai": true/false, '
    '"risk_flags": [..], "reasoning": "<= 80 words"}. No prose, no fences.'
)


def build_claude_user_prompt(candidate: "Union[dict, ScanContext]", openai_eval: dict) -> str:
    return (
        "Review the primary evaluation for risk. Return JSON ONLY.\n\n"
        f"CANDIDATE:\n{json.dumps(_public(candidate), default=str)}\n\n"
        f"PRIMARY_EVALUATION:\n{json.dumps(openai_eval, default=str)}\n\n"
        "Give your independent verdict now."
    )


# PR14: Red-Team Debate v0 -- the adversarial "bear" agent. Unlike
# CLAUDE_SYSTEM_PROMPT (an independent second opinion, ANY verdict), this
# prompt asks explicitly and only for the strongest case AGAINST a trade
# that has ALREADY been committed -- it cannot change the outcome, and the
# prompt says so, so the model isn't confused into thinking a real decision
# hangs on its answer.
BEAR_SYSTEM_PROMPT = (
    "You are an adversarial red-team reviewer for a paper-trading research system. "
    "A trade has ALREADY been proposed and committed to the ledger before you see "
    "it -- you cannot block, size, or change it in any way. Your only job is to "
    "argue the strongest case for why this specific trade will LOSE money, as if "
    "you were betting against it. Respond with a SINGLE JSON object ONLY: "
    '{"stance": "oppose|neutral|support", "conviction": 0.0-1.0, '
    '"failure_modes": ["<=3 short phrases"], "invalidation_triggers": ["<=3 short phrases"], '
    '"reasoning": "<= 80 words"}. "oppose" means you would bet against it; '
    '"support" means you agree with the trade; "conviction" is how strongly you '
    "hold your stance, regardless of which stance it is. No prose, no fences."
)


def build_bear_user_prompt(candidate: "Union[dict, ScanContext]", proposal: dict) -> str:
    return (
        "This trade is ALREADY committed -- you are voting after the fact, not "
        "deciding. Argue the bear case as strongly as the facts support.\n\n"
        f"CANDIDATE:\n{json.dumps(_public(candidate), default=str)}\n\n"
        f"PROPOSAL:\n{json.dumps(_public(proposal), default=str)}\n\n"
        "Give your adversarial verdict now."
    )


# HGEN-1: the Hypothesis Proposer -- a shadow, registry-first candidate
# generator. Every candidate this prompt produces still passes through
# alphaos.hypotheses.proposer's own draft-quarantine intake pipeline
# (schema validation, hard-block duplicate detection, mechanical risk
# classification) before it can ever become a real hypothesis_proposals
# row -- this prompt is asked for a BATCH of raw candidates, never for a
# final, self-certifying registry entry.
HYPOTHESIS_GEN_SYSTEM_PROMPT = (
    "You are AlphaOS's hypothesis-generation research assistant for a "
    "paper-trading system. You propose NEW candidate hypotheses about "
    "trading-edge claims the system could test -- you do NOT trade, size, "
    "approve, or execute anything, and nothing you propose is registered "
    "automatically: every candidate you produce goes to a human operator "
    "for review first. You may ONLY reuse an existing, already-computable "
    "metric function from the given whitelist -- you must NEVER invent a "
    "new metric, a new data source, or a new table. Each candidate must "
    "state a clear, falsifiable claim and a direction (positive, negative, "
    "or either). Prefer claims that are genuinely different from the "
    "exemplars shown (a new direction on an existing metric, a narrower or "
    "differently-scoped claim) over restating an exemplar's own claim. "
    "Respond with a SINGLE JSON object ONLY. No prose, no markdown, no "
    "code fences."
)


def build_hypothesis_gen_user_prompt(
    exemplars: list[dict], card_summaries: list[dict], metric_whitelist: list[str], n: int,
) -> str:
    """User prompt for HGEN-1's generator. ``exemplars`` are resolved-
    hypothesis rows (regardless of verdict -- see generator.py's own
    ``select_exemplars()`` docstring for why no verdict filter is ever
    applied here), ``card_summaries`` are the current card set's
    (card_id, version, name, state) tuples, ``metric_whitelist`` is the
    SAME list ``alphaos.hypotheses.proposer.METRIC_WHITELIST`` enforces at
    intake (this prompt does not invent its own copy)."""
    schema = {
        "candidates": [
            {
                "title": "string, short",
                "claim_text": "string: the falsifiable claim in one sentence",
                "metric_fn_name": f"one of: {sorted(metric_whitelist)}",
                "direction": "positive | negative | either",
                "proposed_risk_class": "A | B | C (your own best guess -- the system will "
                                       "mechanically re-derive the real class regardless)",
                "card_id": "string or null -- ONLY set this if the claim is specifically "
                          "about promoting/gating one of the given cards",
            }
        ],
    }
    return (
        f"Propose up to {n} NEW candidate hypotheses. Return JSON ONLY matching the schema "
        "(a single object with a 'candidates' array).\n\n"
        f"SCHEMA:\n{json.dumps(schema, indent=2)}\n\n"
        f"METRIC_WHITELIST:\n{json.dumps(sorted(metric_whitelist))}\n\n"
        f"RESOLVED_HYPOTHESIS_EXEMPLARS (every resolved hypothesis regardless of verdict):\n"
        f"{json.dumps(exemplars, default=str)}\n\n"
        f"CURRENT_CARDS:\n{json.dumps(card_summaries, default=str)}\n\n"
        "Rules: metric_fn_name MUST be from the whitelist -- never invent one. Do not restate "
        "an exemplar's own claim verbatim. Output the JSON object now."
    )
