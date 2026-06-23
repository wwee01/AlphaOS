"""Candidate Packet (Roadmap 2.3) — compact, schema-valid, no raw data leak."""

from __future__ import annotations

from alphaos.config.settings import load_settings
from alphaos.constants import CONTEXT_UNAVAILABLE_V1
from alphaos.scanner.candidate_packet import PROMPT_KEYS, build_packet
from alphaos.scanner.interest_scanner import InterestScanner


def _build():
    s = load_settings(load_env_file=False, env={"ALPHAOS_MODE": "mock"})
    snap = {
        "symbol": "AAPL", "last_price": 100.0, "prev_close": 98.0, "bid": 99.9, "ask": 100.1,
        "spread_pct": 0.002, "change_pct": 0.03, "rel_volume": 1.8, "bar_open": 98.5,
        "bar_high": 100.5, "bar_low": 98.0, "dollar_volume": 5e9, "freshness_status": "usable",
    }
    sig = InterestScanner(s).score(snap, spy={"change_pct": 0.01})
    cand = {"candidate_id": "c1", "symbol": "AAPL", "direction": "long",
            "momentum_score": 0.7, "liquidity_ok": 1}
    return build_packet(cand, snap, sig, interest_rank=1)


def test_prompt_dict_is_compact_with_no_raw_data():
    pd = _build().to_prompt_dict()
    # exactly the whitelisted compact keys — nothing more
    assert set(pd.keys()) == set(PROMPT_KEYS)
    # raw market data never leaks to the AI
    assert "_snapshot" not in pd
    assert not any(k in pd for k in ("bar_high", "bar_low", "bar_open", "quote_timestamp"))


def test_placeholder_context_is_unavailable():
    pd = _build().to_prompt_dict()
    for k in ("catalyst_status", "official_news_context", "last30days_context", "sentiment_context"):
        assert pd[k] == CONTEXT_UNAVAILABLE_V1


def test_to_row_has_journal_fields():
    row = _build().to_row(scan_batch_id="scan_1")
    for k in ("packet_id", "candidate_id", "scan_batch_id", "symbol", "interest_score",
              "interest_rank", "shortlist_reason", "packet_json", "missing_data_flags_json",
              "catalyst_status"):
        assert k in row
    assert row["scan_batch_id"] == "scan_1"
