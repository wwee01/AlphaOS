"""Live prompt composition (PR9.1): private ``_``-prefixed candidate keys must
never be serialized into any LLM prompt.

Regression tests for the 2026-07-06 exit-review CRITICAL: the scanner
(`candidate_scanner.py` — ``_snapshot``/``_interest``) and orchestrator
(`_label_candidate` — ``_catalyst``/``_last30``/``_polarity``/``_earnings``/
``_packet_id``) used to stash full enrichment objects on the candidate dict,
and ``build_no_news_user_prompt`` serialized the WHOLE dict — leaking
catalyst/narrative text into a prompt whose system message asserts no news
exists. Mock mode never builds prompts, so ordinary end-to-end tests can
never catch a regression here.

The production pipeline now carries that plumbing on ``ScanContext`` typed
attributes instead of underscore dict keys (see
``alphaos/scanner/scan_context.py``), which makes ``row`` structurally
incapable of holding a private key. The dict-based tests below still cover
``_public()``'s defense-in-depth filter (plain dicts, e.g. test fixtures,
remain supported); ``test_*_via_scan_context`` covers the actual current
production shape.
"""

from __future__ import annotations

from alphaos.ai.prompt_templates import (
    _public,
    build_claude_user_prompt,
    build_no_news_user_prompt,
    build_openai_user_prompt,
)
from alphaos.scanner.scan_context import ScanContext

# A sentinel that could only ever appear in the prompt via a leaked private key.
SENTINEL = "LEAK_SENTINEL_9f2c1a"


def _stashed_candidate() -> dict:
    """A candidate dict shaped exactly like the production pipeline's —
    public row fields plus every ``_``-stash the scanner/orchestrator attach
    (see candidate_scanner.py and Orchestrator._label_candidate)."""
    return {
        "candidate_id": "cand_test123",
        "symbol": "AAPL",
        "direction": "long",
        "strategy": "swing",
        "momentum_score": 0.7,
        "last_price": 210.55,
        # -- private plumbing, exact keys from production code --
        "_snapshot": {"symbol": "AAPL", "last_price": 210.55, "note": SENTINEL},
        "_interest": {"score": 0.9, "why": SENTINEL},
        "_catalyst": {"catalyst_type": "earnings_beat", "summary": f"confirmed catalyst: {SENTINEL}"},
        "_last30": {"narrative": f"retail is euphoric about {SENTINEL}"},
        "_polarity": {"sentiment_label": "bullish", "driver": SENTINEL},
        "_earnings": {"days_to_earnings": 3, "source": SENTINEL},
        "_packet_id": f"pkt_{SENTINEL}",
    }


def test_no_news_prompt_never_contains_private_keys_or_their_content():
    prompt = build_no_news_user_prompt(_stashed_candidate(), {"symbol": "AAPL"}, "usable")

    assert SENTINEL not in prompt  # no leaked VALUE from any private stash
    for key in ("_snapshot", "_interest", "_catalyst", "_last30", "_polarity", "_earnings", "_packet_id"):
        assert key not in prompt  # no leaked KEY either


def test_no_news_prompt_still_contains_the_public_candidate_fields():
    prompt = build_no_news_user_prompt(_stashed_candidate(), {"symbol": "AAPL"}, "usable")

    assert '"candidate_id": "cand_test123"' in prompt
    assert '"symbol": "AAPL"' in prompt
    assert '"momentum_score": 0.7' in prompt
    assert "NO-NEWS MODE" in prompt  # the mode instruction itself is intact


def test_news_mode_prompt_strips_private_keys_too():
    prompt = build_openai_user_prompt(_stashed_candidate(), {"symbol": "AAPL"}, [], "usable")

    assert SENTINEL not in prompt
    assert '"symbol": "AAPL"' in prompt


def test_claude_review_prompt_strips_private_keys_too():
    prompt = build_claude_user_prompt(_stashed_candidate(), {"decision": "propose"})

    assert SENTINEL not in prompt
    assert '"decision": "propose"' in prompt  # the eval payload itself is intact


def test_public_helper_preserves_everything_not_underscored():
    cand = _stashed_candidate()
    public = _public(cand)

    assert set(public) == {
        "candidate_id", "symbol", "direction", "strategy", "momentum_score", "last_price",
    }
    # And the original dict is untouched (no mutation of pipeline state).
    assert "_catalyst" in cand


def test_public_helper_tolerates_non_string_keys():
    """Defensive: a malformed dict with a non-string key must not crash prompt
    construction (isinstance guard) — keep whatever isn't a private string key."""
    weird = {"symbol": "AAPL", 42: "numeric-key", "_private": SENTINEL}
    public = _public(weird)

    assert public == {"symbol": "AAPL", 42: "numeric-key"}


def _stashed_scan_context() -> ScanContext:
    """A ScanContext shaped exactly like the real scanner/orchestrator produce
    it: public row fields plus every enrichment object set as a typed
    attribute (never as a row key — ``ScanContext.__setitem__`` would reject
    an underscore key outright)."""
    ctx = ScanContext(row={
        "candidate_id": "cand_test123",
        "symbol": "AAPL",
        "direction": "long",
        "strategy": "swing",
        "momentum_score": 0.7,
        "last_price": 210.55,
    })
    ctx.snapshot = {"symbol": "AAPL", "last_price": 210.55, "note": SENTINEL}
    ctx.interest = {"score": 0.9, "why": SENTINEL}
    ctx.catalyst = {"catalyst_type": "earnings_beat", "summary": f"confirmed catalyst: {SENTINEL}"}
    ctx.last30 = {"narrative": f"retail is euphoric about {SENTINEL}"}
    ctx.polarity = {"sentiment_label": "bullish", "driver": SENTINEL}
    ctx.earnings = {"days_to_earnings": 3, "source": SENTINEL}
    ctx.packet_id = f"pkt_{SENTINEL}"
    return ctx


def test_no_news_prompt_never_leaks_scan_context_typed_attributes():
    """The actual current production shape: a ScanContext, not a raw dict."""
    prompt = build_no_news_user_prompt(_stashed_scan_context(), {"symbol": "AAPL"}, "usable")

    assert SENTINEL not in prompt
    assert '"symbol": "AAPL"' in prompt
    assert "NO-NEWS MODE" in prompt


def test_news_mode_prompt_never_leaks_scan_context_typed_attributes():
    prompt = build_openai_user_prompt(_stashed_scan_context(), {"symbol": "AAPL"}, [], "usable")

    assert SENTINEL not in prompt
    assert '"symbol": "AAPL"' in prompt


def test_claude_review_prompt_never_leaks_scan_context_typed_attributes():
    prompt = build_claude_user_prompt(_stashed_scan_context(), {"decision": "propose"})

    assert SENTINEL not in prompt
    assert '"decision": "propose"' in prompt


def test_public_helper_on_scan_context_matches_row_exactly():
    ctx = _stashed_scan_context()
    public = _public(ctx)

    assert public == ctx.row
    assert set(public) == {
        "candidate_id", "symbol", "direction", "strategy", "momentum_score", "last_price",
    }
    # And ScanContext itself is untouched (no mutation of pipeline state).
    assert ctx.catalyst is not None
