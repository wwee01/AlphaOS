"""Explicitly synthetic transport fixture; no broker, provider, or natural evidence."""

import json
from types import SimpleNamespace

from alphaos.orchestrator import Orchestrator
from alphaos.journal.journal_store import JournalStore
from conftest import make_settings


def synthetic_transport_scan(
    monkeypatch, sink, db_path=":memory:", *, prompt_version="v1", before_scan=None
):
    import openai

    calls = []

    def create(**kwargs):
        calls.append(kwargs["model"])
        content = {
            "primary_label": "Momentum",
            "secondary_labels": [],
            "candidate_tags": [],
            "risk_tags": [],
            "direction": "long",
            "decision": "propose",
            "confidence": 0.8,
            "reason_for_label": "SYNTHETIC TEST transport",
            "thesis_stub": "synthetic",
            "invalidation": "synthetic",
            "main_risk": "synthetic",
            "missing_context": [],
            "suggested_new_tags": [],
            "entry": 100,
            "stop": 95,
            "target": 110,
            "max_holding_days": 3 if prompt_version == "v4" else 10,
            "expected_r": 2,
            "reasoning_summary": "Synthetic price action only; no catalyst information.",
            "symbol": None,
            "news_sources": [],
            "catalyst": "not_available_v1",
            "news_status": "disabled_v1",
            "data_freshness_status": "usable",
            "risk_flags": [],
        }
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=json.dumps(content)),
                )
            ],
            model=kwargs["model"],
            usage=None,
            system_fingerprint="synthetic-test",
        )

    monkeypatch.setattr(
        openai,
        "OpenAI",
        lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )
    o = Orchestrator(
        settings=make_settings(
            OPENAI_API_KEY="synthetic-test-key",
            OPENAI_PROMPT_VERSION=prompt_version,
            MAX_CANDIDATES_TO_AI=100,
        ),
        journal=JournalStore(db_path),
        runtime_identity_sink=sink,
    )
    # Exercise actual live parsing/evaluation code against the isolated fake SDK transport.
    o.openai.use_mock = False
    o.labeller.use_mock = False
    from alphaos.scanner.candidate_scanner import DEFAULT_UNIVERSE
    from alphaos.ai.openai_client import ATR_RULES_V1
    from alphaos.util.ids import new_id

    for symbol in DEFAULT_UNIVERSE:
        o.journal.insert(
            "atr_history",
            {
                "atr_id": new_id("synthetic_atr"),
                "symbol": symbol,
                "market_date": "2026-01-01",
                "atr_14": 1.0,
                "rules_version": ATR_RULES_V1,
            },
        )
    original = o.market.get_snapshots

    def snapshots(symbols):
        return [dict(s, is_mock=False) for s in original(symbols)]

    monkeypatch.setattr(o.market, "get_snapshots", snapshots)
    original_one = o.market.get_snapshot
    monkeypatch.setattr(
        o.market,
        "get_snapshot",
        lambda symbol: dict(original_one(symbol), is_mock=False),
    )
    if before_scan is not None:
        before_scan(o)
    summary = o.run_scan_once()
    return o, summary, calls
