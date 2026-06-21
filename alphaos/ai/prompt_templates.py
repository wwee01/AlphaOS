"""Prompt templates for the OpenAI primary engine and the Claude reviewer.

OpenAI MUST return a single JSON object only — no prose, no markdown fences. The
schema is spelled out explicitly and the parser (util.structured_json) is
defensive in case the model misbehaves.
"""

from __future__ import annotations

import json

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


def build_openai_user_prompt(
    candidate: dict, snapshot: dict, news_items: list[dict], freshness_status: str
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
        f"CANDIDATE:\n{json.dumps(candidate, default=str)}\n\n"
        f"MARKET_SNAPSHOT:\n{json.dumps(snapshot, default=str)}\n\n"
        f"NEWS_ITEMS:\n{json.dumps(news_items, default=str)}\n\n"
        f"DATA_FRESHNESS:\n{freshness_status}\n\n"
        "Rules: no verifiable news => not 'propose'. Stale/unverifiable data => "
        "'reject'. Long stop below entry; short stop above entry; target on the "
        "profit side. Output the JSON object now."
    )


CLAUDE_SYSTEM_PROMPT = (
    "You are an INDEPENDENT second-opinion risk reviewer for a paper-trading "
    "system. You do NOT approve, submit, or size trades. You review another "
    "model's evaluation and flag risks. Respond with a SINGLE JSON object ONLY: "
    '{"verdict": "agree|disagree|caution", "agrees_with_openai": true/false, '
    '"risk_flags": [..], "reasoning": "<= 80 words"}. No prose, no fences.'
)


def build_claude_user_prompt(candidate: dict, openai_eval: dict) -> str:
    return (
        "Review the primary evaluation for risk. Return JSON ONLY.\n\n"
        f"CANDIDATE:\n{json.dumps(candidate, default=str)}\n\n"
        f"PRIMARY_EVALUATION:\n{json.dumps(openai_eval, default=str)}\n\n"
        "Give your independent verdict now."
    )
