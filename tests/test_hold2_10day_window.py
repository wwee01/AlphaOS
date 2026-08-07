"""HOLD-2: extend the live holding window to 10 trading days (card v3 +
prompt v4).

docs/roadmap/alphaos-hold2-10day-window-spec.md. Hermetic throughout -- the
live HTTP call is always monkeypatched out (never real network); direct
construction; no wall-clock dependence anywhere in this file. Covers the
spec's own 11 numbered test obligations (section 5); numbered test-comment
headers below map 1:1 to that list. Obligations 6 and 7 (BASELINE pin /
10-day arm) are proven in tests/test_baseline.py (see
test_record_shadow_baseline_decisions_pin_unaffected_by_active_card_id_swap
and test_record_shadow_baseline_decisions_writes_two_rows_per_candidate)
rather than duplicated here, since that file already owns BASELINE's full
fixture/helper set.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from datetime import timedelta

import pytest

from alphaos.ai import prompt_templates as pt
from alphaos.ai.openai_client import ATR_RULES_V1, OpenAIClient
from alphaos.ai.validation import validate_max_holding_days_range
from alphaos.cards import registry as cards
from alphaos.cards.activation import build_scan_card_activation
from alphaos.config.settings import SettingsError
from alphaos.constants import Decision, PROMPT_VERSIONS, ReasonCode
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.scanner.candidate_scanner import DEFAULT_UNIVERSE
from alphaos.util import timeutils
from conftest import make_settings
from test_instr3_trend_context import _seed_daily_bars


def _seed_atr(journal, symbol, atr_14, market_date="2026-07-08"):
    journal.insert("atr_history", {
        "atr_id": f"atr_{symbol}_{market_date}", "symbol": symbol, "market_date": market_date,
        "atr_14": atr_14, "rules_version": ATR_RULES_V1, "n_bars_fetched": 15,
    })


def _v4_client(journal, active_card_id="catalyst_momentum_v2", **overrides):
    settings = make_settings(
        ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test",
        OPENAI_PROMPT_VERSION="v4", ACTIVE_CARD_ID=active_card_id, **overrides,
    )
    return OpenAIClient(settings, journal)


def _fake_openai(monkeypatch, fixed_payload):
    """Monkeypatches openai.OpenAI to always return ``fixed_payload``,
    capturing every call's ``messages`` list (mirrors
    test_instr3_trend_context.py's own ``_capture_prompt_via_fake_openai``,
    parameterized here so each test controls its own response)."""
    captured = {"calls": []}
    import openai

    class _FakeMessage:
        def __init__(self, content):
            self.content = content

    class _FakeChoice:
        def __init__(self, content):
            self.message = _FakeMessage(content)

    class _FakeResponse:
        def __init__(self, content):
            self.choices = [_FakeChoice(content)]
            self.usage = None

    class _FakeCompletions:
        def create(self, model, response_format, messages, timeout):
            captured["calls"].append(messages)
            return _FakeResponse(json.dumps(fixed_payload))

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeOpenAI:
        def __init__(self, api_key=None):
            self.chat = _FakeChat()

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    return captured


def _propose_payload(max_holding_days=10, symbol="AAPL"):
    return {
        "symbol": symbol, "direction": "long", "entry": 100.0, "stop": 90.0,
        "target": 130.0, "max_holding_days": max_holding_days, "expected_r": 3.0,
        "confidence": 0.8, "decision": "propose", "reasoning_summary": "x",
        "catalyst": "not_available_v1", "news_status": "disabled_v1",
        "news_sources": [], "data_freshness_status": "usable", "risk_flags": [],
    }


_ATR_POLICY = {
    "atr_14": 3.5, "stop_multiplier": 2.0, "risk_per_share": 7.0,
    "min_reward_risk": 1.2, "min_target_distance": 8.4, "rules_version": ATR_RULES_V1,
}
_MULTI_DAY_CONTEXT = {
    "bars": [{"date": "2026-06-20", "open": 100.0, "high": 101.0, "low": 99.0,
              "close": 100.5, "volume": 1_000_000}],
    "recent_high_10d": 103.0, "recent_low_10d": 99.0,
    "dist_to_recent_high_atr": 0.2857, "dist_to_recent_low_atr": 0.4286,
    "trend_score": 0.42, "trend_rules_version": "trend_rules_v1",
}


# ===================================================== obligation 1: goldens
def _golden_v3_prompt(candidate, snapshot, freshness_status, atr_policy, multi_day_context):
    """Hand-reproduction of build_no_news_user_prompt()'s v3 output shape
    (post-INSTR-3), reusing the SAME render helpers HOLD-2 left untouched --
    proves HOLD-2 did not move v3's byte stream even by one character (v3 is
    now a frozen control arm the moment v4 exists, per the spec's own §3.3
    note). v1's and v2's own dedicated golden tests
    (test_instr2_atr_coherent_prompt.py::test_v1_golden_prompt_byte_identical_to_pre_instr2,
    test_instr3_trend_context.py::test_2_golden_v2_byte_identical_to_pre_instr3)
    are unmodified by this build and still pass -- see the full-suite run."""
    schema = {
        "symbol": "string", "direction": "long | short", "entry": "number",
        "stop": "number", "target": "number", "max_holding_days": "integer 1-5",
        "expected_r": "number (reward/risk)", "confidence": "number 0..1",
        "decision": "reject | watch | propose",
        "reasoning_summary": "string (<= 80 words; PRICE/VOLUME/STRUCTURE ONLY)",
        "catalyst": "MUST be exactly 'not_available_v1'",
        "news_status": "MUST be exactly 'disabled_v1'",
        "news_sources": "MUST be an empty list []",
        "data_freshness_status": "usable | stale | unverifiable",
        "risk_flags": ["list of short risk flag strings"],
    }
    market_snapshot = dict(snapshot)
    market_snapshot.pop("atr_policy", None)
    market_snapshot.pop("multi_day_context", None)
    multi_day_context_section = (
        f"{pt._render_multi_day_context(multi_day_context)}\n" if multi_day_context else ""
    )
    atr_policy_section = f"{pt._render_atr_stop_policy(atr_policy)}\n" if atr_policy else ""
    return (
        "Evaluate this candidate in NO-NEWS MODE. Return JSON ONLY matching the "
        "schema. Base the thesis ONLY on price action, volume, relative strength, "
        "trend structure, and risk/reward. Do NOT reference or invent any news or "
        "catalyst.\n\n"
        f"SCHEMA:\n{json.dumps(schema, indent=2)}\n\n"
        f"CANDIDATE:\n{json.dumps(pt._public(candidate), default=str)}\n\n"
        f"MARKET_SNAPSHOT:\n{json.dumps(market_snapshot, default=str)}\n\n"
        f"{multi_day_context_section}"
        f"{atr_policy_section}"
        f"DATA_FRESHNESS:\n{freshness_status}\n\n"
        "Rules: stale/unverifiable data => 'reject'. Long stop below entry; short "
        "stop above entry; target on the profit side. catalyst='not_available_v1', "
        "news_status='disabled_v1', news_sources=[]. Output the JSON object now."
    )


def test_1_golden_v3_byte_identical_after_hold2():
    candidate = {"candidate_id": "cand_x", "symbol": "AAPL", "direction": "long",
                "momentum_score": 0.7, "last_price": 210.55}
    snapshot = {"last_price": 210.55, "volume": 1_200_000, "rel_strength": 0.6}

    golden = _golden_v3_prompt(candidate, snapshot, "usable", _ATR_POLICY, _MULTI_DAY_CONTEXT)
    actual = pt.build_no_news_user_prompt(
        candidate, snapshot, "usable", atr_policy=_ATR_POLICY, multi_day_context=_MULTI_DAY_CONTEXT,
    )
    assert actual == golden
    assert '"max_holding_days": "integer 1-5"' in actual
    assert "1-10" not in actual


def test_1_default_call_path_still_renders_frozen_1_5_bound():
    """The shared default-parameter path every v1/v2/v3 call site actually
    uses (no max_holding_days_bound kwarg) still renders the frozen "1-5"
    text -- the single place a HOLD-2 regression could silently move all
    three golden tests at once."""
    prompt = pt.build_no_news_user_prompt({"symbol": "AAPL"}, {"last_price": 100.0}, "usable")
    assert '"max_holding_days": "integer 1-5"' in prompt
    assert "swing horizon 1-5 trading days" in pt.NO_NEWS_SYSTEM_PROMPT
    assert "swing horizon 1-5 trading days" in pt.OPENAI_SYSTEM_PROMPT


# ============================================== obligation 2: v4 interpolation
def test_2_system_prompt_interpolates_from_card_v2_then_v3():
    assert "swing horizon 1-3 trading days" in pt.build_no_news_system_prompt(3)
    assert "swing horizon 1-10 trading days" in pt.build_no_news_system_prompt(10)
    assert "swing horizon 1-3 trading days" in pt.build_openai_system_prompt(3)
    assert "swing horizon 1-10 trading days" in pt.build_openai_system_prompt(10)
    # v1/v2/v3's own frozen constants stay untouched.
    assert "swing horizon 1-5 trading days" in pt.NO_NEWS_SYSTEM_PROMPT
    assert "swing horizon 1-5 trading days" in pt.OPENAI_SYSTEM_PROMPT


def test_2_user_prompt_schema_bound_interpolates_from_card_v2_then_v3():
    prompt_v2_bound = pt.build_no_news_user_prompt(
        {"symbol": "AAPL"}, {"last_price": 100.0}, "usable", max_holding_days_bound=3,
    )
    prompt_v3_bound = pt.build_no_news_user_prompt(
        {"symbol": "AAPL"}, {"last_price": 100.0}, "usable", max_holding_days_bound=10,
    )
    assert '"max_holding_days": "integer 1-3"' in prompt_v2_bound
    assert '"max_holding_days": "integer 1-10"' in prompt_v3_bound


def test_2_end_to_end_v4_prompt_single_sources_the_active_card(journal, monkeypatch):
    """Full round-trip through OpenAIClient._live_eval: active card v2 ->
    "1-3" rendered in BOTH the system prompt and the schema; active card v3
    -> "1-10" -- one number, three uses (system prompt, schema, response
    range check -- obligation 3 below), never re-derived."""
    _seed_atr(journal, "AAPL", atr_14=2.0)
    captured = _fake_openai(monkeypatch, _propose_payload(max_holding_days=3))
    client_v2 = _v4_client(journal, active_card_id="catalyst_momentum_v2")
    client_v2._live_eval({"symbol": "AAPL", "direction": "long"}, {"last_price": 100.0}, "usable")
    system_msg, user_msg = captured["calls"][0][0]["content"], captured["calls"][0][1]["content"]
    assert "swing horizon 1-3 trading days" in system_msg
    assert '"max_holding_days": "integer 1-3"' in user_msg

    captured2 = _fake_openai(monkeypatch, _propose_payload(max_holding_days=10))
    client_v3 = _v4_client(journal, active_card_id="catalyst_momentum_v3")
    client_v3._live_eval({"symbol": "AAPL", "direction": "long"}, {"last_price": 100.0}, "usable")
    system_msg2, user_msg2 = captured2["calls"][0][0]["content"], captured2["calls"][0][1]["content"]
    assert "swing horizon 1-10 trading days" in system_msg2
    assert '"max_holding_days": "integer 1-10"' in user_msg2


# ================================================= obligation 3: parser range
def test_3_parser_accepts_max_holding_days_10_under_v4_card_v3(journal, monkeypatch):
    _seed_atr(journal, "AAPL", atr_14=2.0)
    _fake_openai(monkeypatch, _propose_payload(max_holding_days=10))
    client = _v4_client(journal, active_card_id="catalyst_momentum_v3")
    ev = client._live_eval({"symbol": "AAPL", "direction": "long"}, {"last_price": 100.0}, "usable")
    assert ev.decision == Decision.PROPOSE.value
    assert ev.max_holding_days == 10


def test_3_parser_rejects_max_holding_days_above_bound(journal, monkeypatch):
    _seed_atr(journal, "AAPL", atr_14=2.0)
    _fake_openai(monkeypatch, _propose_payload(max_holding_days=11))
    client = _v4_client(journal, active_card_id="catalyst_momentum_v3")
    ev = client._live_eval({"symbol": "AAPL", "direction": "long"}, {"last_price": 100.0}, "usable")
    assert ev.decision == Decision.REJECT.value
    assert ReasonCode.MAX_HOLDING_DAYS_OUT_OF_RANGE.value in (ev.risk_flags or [])


def test_3_parser_accepts_max_holding_days_3_rejects_4_under_v4_card_v2(journal, monkeypatch):
    """Same enforcement under the DARK default card (v2, bound 3) -- proves
    the bound is genuinely card-derived, not hardcoded to 10 somewhere."""
    _seed_atr(journal, "AAPL", atr_14=2.0)
    _fake_openai(monkeypatch, _propose_payload(max_holding_days=3))
    client = _v4_client(journal, active_card_id="catalyst_momentum_v2")
    ev = client._live_eval({"symbol": "AAPL", "direction": "long"}, {"last_price": 100.0}, "usable")
    assert ev.decision == Decision.PROPOSE.value

    _fake_openai(monkeypatch, _propose_payload(max_holding_days=4))
    ev2 = client._live_eval({"symbol": "AAPL", "direction": "long"}, {"last_price": 100.0}, "usable")
    assert ev2.decision == Decision.REJECT.value
    assert ReasonCode.MAX_HOLDING_DAYS_OUT_OF_RANGE.value in (ev2.risk_flags or [])


def test_3_v1_v2_v3_never_enforce_a_max_holding_days_range(journal, monkeypatch):
    """v1/v2/v3 never validated this at all (the "1-5" prompt text was
    advisory only) -- HOLD-2 must not retroactively start rejecting an
    out-of-range value on those frozen versions; only v4 gets the new
    enforcement."""
    _seed_atr(journal, "AAPL", atr_14=2.0)
    for version in ("v1", "v2", "v3"):
        _fake_openai(monkeypatch, _propose_payload(max_holding_days=99))
        settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test",
                                 OPENAI_PROMPT_VERSION=version)
        client = OpenAIClient(settings, journal)
        ev = client._live_eval({"symbol": "AAPL", "direction": "long"}, {"last_price": 100.0}, "usable")
        assert ev.decision == Decision.PROPOSE.value, f"{version} must not enforce the new range check"
        assert ev.max_holding_days == 99


def test_3_validate_max_holding_days_range_unit():
    assert validate_max_holding_days_range({"max_holding_days": 10}, 10) is None
    assert validate_max_holding_days_range({"max_holding_days": 1}, 10) is None
    assert validate_max_holding_days_range({"max_holding_days": 11}, 10) is not None
    assert validate_max_holding_days_range({"max_holding_days": 0}, 10) is not None
    assert validate_max_holding_days_range({"max_holding_days": None}, 10) is not None
    assert validate_max_holding_days_range({}, 10) is not None


# ============================ audit-fixup MEDIUM-7: validator integrality
def test_medium7_integral_float_coerces_and_writes_back():
    """10.0 (an integral float -- exactly what a JSON number with no
    fractional part deserializes to in Python) is accepted, coerced to
    int, and WRITTEN BACK -- obj["max_holding_days"] must be the int 10
    afterward, not the original float 10.0."""
    obj = {"max_holding_days": 10.0}
    assert validate_max_holding_days_range(obj, 10) is None
    assert obj["max_holding_days"] == 10
    assert type(obj["max_holding_days"]) is int


def test_medium7_fractional_float_rejected_not_truncated():
    """STATUS CORRECTION item 6 / audit B: plain int(10.9) == 10 used to
    silently truncate a fractional value and PASS as if it had said "10" --
    this must now be a hard rejection, and obj must be left UNCHANGED (no
    silent coercion of a value that was never actually validated)."""
    obj = {"max_holding_days": 10.9}
    failure = validate_max_holding_days_range(obj, 10)
    assert failure is not None
    assert obj["max_holding_days"] == 10.9  # untouched -- rejection, not silent coercion


def test_medium7_bool_rejected_never_reads_as_0_or_1():
    """bool is an int subclass in Python -- True/False must never silently
    validate as 1/0."""
    obj_true = {"max_holding_days": True}
    assert validate_max_holding_days_range(obj_true, 10) is not None
    assert obj_true["max_holding_days"] is True  # untouched

    obj_false = {"max_holding_days": False}
    assert validate_max_holding_days_range(obj_false, 10) is not None


def test_medium7_non_numeric_string_rejected():
    obj = {"max_holding_days": "abc"}
    assert validate_max_holding_days_range(obj, 10) is not None
    assert obj["max_holding_days"] == "abc"  # untouched


def test_medium7_clean_numeric_string_coerces_and_writes_back():
    """A clean digit string ("10") is accepted (matches the pre-fixup
    behavior of int("10")) but now WRITTEN BACK as a real int -- the
    original bug (audit A) was that "10" persisted as a string downstream
    even though the check itself passed."""
    obj = {"max_holding_days": "10"}
    assert validate_max_holding_days_range(obj, 10) is None
    assert obj["max_holding_days"] == 10
    assert type(obj["max_holding_days"]) is int


def test_medium7_out_of_range_int_leaves_obj_unchanged():
    obj = {"max_holding_days": 11}
    assert validate_max_holding_days_range(obj, 10) is not None
    assert obj["max_holding_days"] == 11  # untouched -- rejection, not clamped


def test_medium7_write_back_propagates_through_live_eval(journal, monkeypatch):
    """End-to-end: a fractional-but-integral response (10.0) round-trips
    through _live_eval() as the real int 10 on the returned
    OpenAIEvaluation, proving the write-back actually reaches
    _from_json(), not just the validator's own local obj copy."""
    _fake_openai(monkeypatch, _propose_payload(max_holding_days=10.0))
    client = _v4_client(journal, active_card_id="catalyst_momentum_v3")
    ev = client._live_eval({"symbol": "AAPL", "direction": "long"}, {"last_price": 100.0}, "usable")
    assert ev.decision == Decision.PROPOSE.value
    assert ev.max_holding_days == 10
    assert type(ev.max_holding_days) is int


# ============================ audit-fixup MEDIUM-8: candidate-stage earnings window
def test_medium8_candidate_stage_earnings_window_follows_active_card(monkeypatch):
    """STATUS CORRECTION item 2 / audit B M2: candidate-stage
    earnings_within_hold_window used to ALWAYS read
    EARNINGS_PROXIMITY_DEFAULT_HOLD_DAYS (a second, un-linked policy
    copy) -- now it follows the ACTIVE card's own max_holding_days_default
    (threaded via orchestrator._label_candidate ->
    EarningsProximityEnricher.enrich(hold_days=...)). Proof via a REAL
    orchestrator scan: every enriched candidate's earnings date is forced
    to 8 calendar days out (unambiguously outside a 3-trading-day hold --
    max 5 calendar days -- and unambiguously inside a 10-trading-day hold,
    which always spans at least 10 calendar days), and
    ACTIVE_CARD_ID=catalyst_momentum_v3 -> every enriched row's
    earnings_within_hold_window must read 1, not 0."""
    from alphaos.earnings.earnings_provider import EarningsProximityResult

    settings = make_settings(
        INTEREST_SCAN_TOP_N="8", MAX_CANDIDATES_TO_AI="8",
        EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="8", LABELLING_ENABLED="true",
        ACTIVE_CARD_ID="catalyst_momentum_v3",
    )
    orch = Orchestrator(settings=settings, journal=JournalStore(":memory:"))

    def _earnings_8_days_out(symbol):
        earnings_date = (timeutils.market_date() + timedelta(days=8)).isoformat()
        return EarningsProximityResult(symbol=symbol, earnings_date=earnings_date, status="ok", source="stub")

    monkeypatch.setattr(orch.earnings_enricher._provider, "get_earnings_for_symbol", _earnings_8_days_out)
    orch.run_scan_once()

    rows = orch.journal.query("SELECT * FROM candidate_earnings WHERE enrichment_status = 'ok'")
    assert rows, "scan produced zero enriched earnings rows -- test fixture is vacuous"
    for r in rows:
        assert r["hold_days_used"] == 10
        assert r["earnings_within_hold_window"] == 1
    orch.close()


def test_medium8_dark_default_earnings_window_still_uses_3(monkeypatch):
    """The merge-dark guarantee: an unchanged ACTIVE_CARD_ID (catalyst_
    momentum_v2, hold=3 -- the same value EARNINGS_PROXIMITY_DEFAULT_
    HOLD_DAYS already defaults to) must behave exactly like before this
    fix -- the SAME 8-days-out earnings date must now read OUTSIDE the
    hold window."""
    from alphaos.earnings.earnings_provider import EarningsProximityResult

    settings = make_settings(
        INTEREST_SCAN_TOP_N="8", MAX_CANDIDATES_TO_AI="8",
        EARNINGS_PROXIMITY_MAX_SYMBOLS_PER_SCAN="8", LABELLING_ENABLED="true",
    )
    orch = Orchestrator(settings=settings, journal=JournalStore(":memory:"))

    def _earnings_8_days_out(symbol):
        earnings_date = (timeutils.market_date() + timedelta(days=8)).isoformat()
        return EarningsProximityResult(symbol=symbol, earnings_date=earnings_date, status="ok", source="stub")

    monkeypatch.setattr(orch.earnings_enricher._provider, "get_earnings_for_symbol", _earnings_8_days_out)
    orch.run_scan_once()

    rows = orch.journal.query("SELECT * FROM candidate_earnings WHERE enrichment_status = 'ok'")
    assert rows, "scan produced zero enriched earnings rows -- test fixture is vacuous"
    for r in rows:
        assert r["hold_days_used"] == 3
        assert r["earnings_within_hold_window"] == 0
    orch.close()


def test_fixA_label_candidate_never_calls_get_default_card_internally(journal, monkeypatch):
    """Round-2 audit-fixup (FIX-A, audit A NEW-3, LOW): _label_candidate()'s
    own earnings-enrichment branch must NOT call get_default_card() itself
    anymore -- the value is threaded in as ``active_card_hold_days`` by the
    caller (a per-scan constant, hoisted once in run_scan_once). Proven by
    making get_default_card() RAISE if called from within this scope, then
    calling _label_candidate() (with earnings_mode="enrich") several times
    directly and asserting none of those calls raise.

    (A whole-scan call-count assertion would NOT isolate this specific
    lookup: alphaos.scanner.candidate_scanner._resolve_card_assignment()
    legitimately calls get_default_card() once per candidate during
    scanning for an unrelated reason -- S1c/PER card-assignment fallback,
    out of this fix's scope -- which would swamp any attempt to count
    calls across a real run_scan_once().)"""
    from alphaos.cards import registry as cards_mod
    from alphaos.earnings.earnings_provider import EarningsProximityResult
    from alphaos.scanner.scan_context import ScanContext

    def _boom(*args, **kwargs):
        raise AssertionError(
            "get_default_card() was called from inside _label_candidate() -- "
            "the per-scan hoist (FIX-A) regressed; active_card_hold_days must "
            "be threaded in by the caller, never re-derived here"
        )

    def _fake_earnings(sym):
        return EarningsProximityResult(symbol=sym, earnings_date=None, status="unavailable", source="stub")

    settings = make_settings(EARNINGS_PROXIMITY_ENABLED="true", LABELLING_ENABLED="true")
    orch = Orchestrator(settings=settings, journal=journal)
    orch.earnings_enricher._provider.get_earnings_for_symbol = _fake_earnings

    monkeypatch.setattr(cards_mod, "get_default_card", _boom)
    for i in range(3):
        symbol = f"SYM{i}"
        snapshot = {"last_price": 100.0, "change_pct": 0.05}
        journal.insert("candidates", {
            "candidate_id": f"cand_fixA_{i}", "symbol": symbol, "direction": "long",
            "strategy": "swing", "momentum_score": 0.9,
        })
        cand = ScanContext(row=journal.candidate_by_id(f"cand_fixA_{i}"))
        cand.snapshot = snapshot
        # Must not raise -- proves no internal get_default_card() call.
        orch._label_candidate(
            cand, snapshot, "sb_fixA", enrich=False, l30_mode=None, earnings_mode="enrich",
            active_card_hold_days=10,
        )
    orch.close()


def test_fixA_label_candidate_threads_active_card_hold_days_directly(journal):
    """Direct unit proof that _label_candidate's own earnings-enrichment
    branch uses the CALLER-SUPPLIED active_card_hold_days verbatim (never
    re-deriving it), i.e. the hoisted parameter is actually load-bearing,
    not a dead/ignored argument."""
    from alphaos.earnings.earnings_provider import EarningsProximityResult
    from alphaos.scanner.scan_context import ScanContext

    settings = make_settings(EARNINGS_PROXIMITY_ENABLED="true", LABELLING_ENABLED="true")
    orch = Orchestrator(settings=settings, journal=journal)
    symbol = "AAPL"
    snapshot = orch.market.get_snapshot(symbol)
    journal.insert("candidates", {
        "candidate_id": "cand_fixA", "symbol": symbol, "direction": "long",
        "strategy": "swing", "momentum_score": 0.9,
    })
    cand = ScanContext(row=journal.candidate_by_id("cand_fixA"))
    cand.snapshot = snapshot

    earnings_date = (timeutils.market_date() + timedelta(days=8)).isoformat()

    def _fake_earnings(sym):
        return EarningsProximityResult(symbol=sym, earnings_date=earnings_date, status="ok", source="stub")

    orch.earnings_enricher._provider.get_earnings_for_symbol = _fake_earnings

    classification = orch._label_candidate(
        cand, snapshot, "sb_fixA", enrich=False, l30_mode=None, earnings_mode="enrich",
        active_card_hold_days=10,  # hand-supplied, as if hoisted from a v3-active scan
    )
    assert classification is not None
    row = journal.one("SELECT * FROM candidate_earnings WHERE candidate_id = 'cand_fixA'")
    assert row["hold_days_used"] == 10
    assert row["earnings_within_hold_window"] == 1  # 8 days out, inside a 10-trading-day hold
    orch.close()


# ============================ audit-fixup HIGH-5: reason-code misfiling
def test_high5_max_holding_days_range_rejection_files_correct_reason_code(journal, monkeypatch):
    """Audit-fixup HOLD-2 (HIGH-5, audit A): OpenAIClient._live_eval()
    already stamps evaluation.risk_flags=[MAX_HOLDING_DAYS_OUT_OF_RANGE]
    and validation_status=<the range failure text> on a v4 range
    rejection (see test_3_parser_rejects_max_holding_days_above_bound
    above) -- but ORCHESTRATOR-LEVEL, _reject_candidate()'s own default
    reason inference reads validation_status (any non-'passed' value) and
    ALWAYS assumes INVENTED_CATALYST_IN_NO_NEWS_MODE (historically the
    only real "failed_validation:" producer). Without the orchestrator's
    own explicit MAX_HOLDING_DAYS_OUT_OF_RANGE branch (mirroring the
    pre-existing NO_ATR_DATA one), every v4 range rejection would land in
    rejected_candidates.reason_code as a fabricated "invented a catalyst"
    -- this proves the REAL row, through the REAL orchestrator scan loop,
    carries the correct code."""
    from test_s1c_activation import _force_momentum

    settings = make_settings(
        ALPHAOS_MODE="paper", OFFLINE_MODE="true", OPENAI_API_KEY="fake-key-for-test",
        OPENAI_PROMPT_VERSION="v4", ACTIVE_CARD_ID="catalyst_momentum_v3",
    )
    orch = Orchestrator(settings=settings, journal=journal)
    _force_momentum(monkeypatch, orch.scanner.market)
    # No ATR needed: the range check runs inside raw_evaluate() and returns
    # a rejection BEFORE post_process()/_apply_atr_stop() ever runs.
    _fake_openai(monkeypatch, _propose_payload(max_holding_days=99))

    orch.run_scan_once()

    rows = orch.journal.query("SELECT reason_code FROM rejected_candidates")
    assert rows, "scan produced zero rejections -- test fixture is vacuous"
    codes = {r["reason_code"] for r in rows}
    assert ReasonCode.MAX_HOLDING_DAYS_OUT_OF_RANGE.value in codes
    assert "INVENTED_CATALYST_IN_NO_NEWS_MODE" not in codes, (
        "a v4 range rejection must never be misfiled as an invented-catalyst rejection"
    )
    orch.close()


# ========================================= obligation 4: ACTIVE_CARD_ID gate
def test_4_active_card_id_unknown_raises_at_load():
    with pytest.raises(SettingsError, match="ACTIVE_CARD_ID"):
        make_settings(ACTIVE_CARD_ID="does_not_exist")


def test_4_active_card_id_valid_values_load_and_default_is_dark():
    assert make_settings().active_card_id == "catalyst_momentum_v2"
    assert make_settings(ACTIVE_CARD_ID="catalyst_momentum_v3").active_card_id == "catalyst_momentum_v3"


# ================================ audit-fixup MEDIUM-6: ACTIVE_CARD_ID gate
def test_medium6_active_card_id_rejects_shadow_only_per_card():
    """STATUS CORRECTION item 5: the shadow-only post_earnings_reaction
    card (state=shadow, "no trading" by its own card YAML) must never be
    settable as the live default -- one hand-edit away, during the cutover
    ceremony, from making a non-trading card the live default."""
    with pytest.raises(SettingsError, match="live_eligible"):
        make_settings(ACTIVE_CARD_ID="post_earnings_reaction")


def test_medium6_active_card_id_accepts_v1_rollback():
    """Deliberate rollback capability: catalyst_momentum_v1 (state
    live_eligible, max_holding_days_default=3) must still be a legal
    ACTIVE_CARD_ID even though it's superseded as the DEFAULT."""
    assert make_settings(ACTIVE_CARD_ID="catalyst_momentum_v1").active_card_id == "catalyst_momentum_v1"


def test_medium6_validate_card_as_active_default_unit():
    """Direct unit coverage against synthetic card dicts (no filesystem
    needed) -- the real on-disk cards are all well-formed today, so the
    'missing max_holding_days_default' failure mode can only be exercised
    this way."""
    from alphaos.cards.registry import validate_card_as_active_default

    # A well-formed card passes.
    validate_card_as_active_default({
        "card_id": "ok_card", "state": "live_eligible", "max_holding_days_default": 5,
    })

    with pytest.raises(SettingsError, match="live_eligible"):
        validate_card_as_active_default({
            "card_id": "shadow_card", "state": "shadow", "max_holding_days_default": 5,
        })

    # Missing entirely -- would KeyError-crash the mock path if unchecked.
    with pytest.raises(SettingsError, match="max_holding_days_default"):
        validate_card_as_active_default({"card_id": "no_field_card", "state": "live_eligible"})

    # Present but malformed (float, string, zero, negative, over-ceiling, bool).
    for bad_value in (5.5, "5", 0, -1, 31, True):
        with pytest.raises(SettingsError, match="max_holding_days_default"):
            validate_card_as_active_default({
                "card_id": "bad_card", "state": "live_eligible", "max_holding_days_default": bad_value,
            })


# ============================================ obligation 5: config fingerprint
def test_5_config_fingerprint_includes_active_card_id(journal):
    s1 = make_settings(ACTIVE_CARD_ID="catalyst_momentum_v2")
    journal.record_config_version(s1)
    row1 = journal.one("SELECT * FROM config_versions ORDER BY id DESC LIMIT 1")
    assert '"active_card_id": "catalyst_momentum_v2"' in row1["config_json"]

    s2 = make_settings(ACTIVE_CARD_ID="catalyst_momentum_v3")
    journal.record_config_version(s2)
    row2 = journal.one("SELECT * FROM config_versions ORDER BY id DESC LIMIT 1")
    assert '"active_card_id": "catalyst_momentum_v3"' in row2["config_json"]
    assert row1["config_hash"] != row2["config_hash"]  # a card cutover must move the fingerprint


# ================================================ obligation 8: mock eval days
def test_8_mock_eval_max_holding_days_follows_active_card(journal):
    settings_v2 = make_settings(ALPHAOS_MODE="mock")
    client_v2 = OpenAIClient(settings_v2, journal)
    ev = client_v2._mock_eval(
        {"symbol": "AAPL", "direction": "long", "momentum_score": 0.9}, {"last_price": 100.0}, "usable",
    )
    assert ev.max_holding_days == 3  # dark default: catalyst_momentum_v2

    settings_v3 = make_settings(ALPHAOS_MODE="mock", ACTIVE_CARD_ID="catalyst_momentum_v3")
    client_v3 = OpenAIClient(settings_v3, journal)
    ev3 = client_v3._mock_eval(
        {"symbol": "AAPL", "direction": "long", "momentum_score": 0.9}, {"last_price": 100.0}, "usable",
    )
    assert ev3.max_holding_days == 10


# ==================================== obligation 9: membership-gate lockstep
def test_9_prompt_versions_includes_v4():
    assert "v4" in PROMPT_VERSIONS
    assert PROMPT_VERSIONS[-1] == "v4"  # ordered oldest-first, v4 is the newest


def test_9_v4_augment_snapshot_includes_atr_policy_regression_guard(journal):
    """Mirrors test_instr3_trend_context.py's own
    test_10_v3_prompt_contains_atr_policy_block_regression_guard: the
    membership gate MUST include "v4", or v4 silently loses ATR_STOP_POLICY
    (an incoherent-by-construction regression -- v4 is supposed to be v3
    content plus an interpolated horizon, never LESS than v3)."""
    _seed_atr(journal, "AAPL", atr_14=2.0)
    client = _v4_client(journal)
    augmented = client._augment_snapshot_for_prompt({"last_price": 100.0}, {"symbol": "AAPL"})
    assert "atr_policy" in augmented
    assert augmented["atr_policy"]["atr_14"] == 2.0


def test_9_v4_augment_snapshot_includes_multi_day_context_regression_guard(journal):
    _seed_atr(journal, "AAPL", atr_14=2.0)
    _seed_daily_bars(journal, "AAPL", [100.0 + i for i in range(6)])
    client = _v4_client(journal)
    augmented = client._augment_snapshot_for_prompt(
        {"last_price": 100.0}, {"symbol": "AAPL", "trend_score": 0.1, "trend_rules_version": "trend_rules_v1"},
    )
    assert "multi_day_context" in augmented


# ========================================= obligation 10: cold-import safety
# HOLD-2 audit-fixup (S-f, audit B): every cold-import subprocess below MUST
# be pinned to THIS worktree, explicitly -- a bare `subprocess.run([sys.
# executable, "-c", ...])` with no PYTHONPATH relies on cwd-based sys.path
# resolution (Python prepends "" == cwd to sys.path for `-c`/no-script
# invocations), which happens to resolve correctly ONLY when the calling
# process's own cwd is this worktree. Reproduced live: the SAME command run
# from a different cwd (e.g. /tmp) silently imports the MAIN checkout's
# alphaos instead (a genuinely different package on disk, sharing nothing
# but the name) -- exactly what audit B caught. FIX: explicitly build the
# subprocess env with PYTHONPATH set to this worktree's repo root (never
# `-I`, which drops cwd from sys.path AND ignores PYTHONPATH entirely,
# making the resolution wrong in the opposite, unrecoverable direction),
# and assert alphaos.__file__ resolves under THIS tree inside every
# subprocess, so a future regression fails LOUDLY instead of silently
# testing the wrong checkout.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _worktree_pinned_env() -> dict:
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO_ROOT)
    return env


def _assert_alphaos_file_under_repo_root_snippet() -> str:
    return (
        "import alphaos, pathlib\n"
        f"_root = pathlib.Path({str(_REPO_ROOT)!r}).resolve()\n"
        "_resolved = pathlib.Path(alphaos.__file__).resolve()\n"
        "assert _resolved.is_relative_to(_root), ("
        "f'alphaos resolved to {_resolved}, NOT under the worktree under test {_root} -- '"
        "'PYTHONPATH pinning failed, this subprocess tested the wrong checkout')\n"
    )


def test_10_cold_import_hold2_modules_no_circular_import():
    """SUSP-1 lesson: a circular import invisible to the warm suite (every
    OTHER test in this repo) shipped a P0 -- a subprocess with a genuinely
    fresh sys.modules is the only way this repo can catch this bug class."""
    env = _worktree_pinned_env()
    for module_name in (
        "alphaos.config.settings",
        "alphaos.cards.registry",
        "alphaos.ai.openai_client",
        "alphaos.ai.prompt_templates",
        "alphaos.ai.validation",
        "alphaos.baseline.tracker",
        "alphaos.baseline.rules",
        "alphaos.ab_eval.run",
        "alphaos.orchestrator",
        "alphaos.scanner.candidate_scanner",
    ):
        code = _assert_alphaos_file_under_repo_root_snippet() + f"import {module_name}\n"
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=30, env=env,
        )
        assert result.returncode == 0, (
            f"cold import of {module_name!r} failed in a fresh interpreter:\n{result.stderr}"
        )


def test_10_cold_import_reverse_order_cards_registry_before_settings():
    """The specific ordering that would surface a real cycle: import
    alphaos.cards.registry FIRST (it imports SettingsError from
    alphaos.config.settings at module top), THEN load_settings() (which
    deferred-imports alphaos.cards.registry back) -- must not deadlock or
    raise ImportError in either direction."""
    code = (
        _assert_alphaos_file_under_repo_root_snippet()
        + "import alphaos.cards.registry\n"
        "from alphaos.config.settings import load_settings\n"
        "s = load_settings(load_env_file=False, env={'ALPHAOS_MODE': 'mock', "
        "'APPROVAL_MODE': 'manual', 'REAL_TRADING_ENABLED': 'false', "
        "'ALPHAOS_DB_PATH': ':memory:', 'MAX_AUTO_APPROVALS_PER_DAY': '1'})\n"
        "assert s.active_card_id == 'catalyst_momentum_v2'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30, env=_worktree_pinned_env(),
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_10_cold_import_fails_loudly_from_a_different_cwd_without_pinning():
    """Regression proof for the bug S-f fixes: WITHOUT the PYTHONPATH
    pinning above, running from a different cwd silently imports a
    DIFFERENT checkout's alphaos package. This test deliberately runs
    UNPINNED (no PYTHONPATH override) from a neutral cwd and asserts the
    resolved path is NOT this worktree -- proving the failure mode is real,
    not hypothetical, and that the other two tests' explicit pinning is
    load-bearing, not redundant."""
    import os
    import tempfile

    code = (
        "import alphaos, pathlib\n"
        "print(str(pathlib.Path(alphaos.__file__).resolve()))\n"
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory() as neutral_cwd:
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
            env=env, cwd=neutral_cwd,
        )
    assert result.returncode == 0, result.stderr
    resolved = pathlib.Path(result.stdout.strip()).resolve()
    assert not resolved.is_relative_to(_REPO_ROOT), (
        "expected the UNPINNED subprocess to resolve OUTSIDE this worktree "
        "(demonstrating the failure mode) -- if this now fails, either the "
        "environment changed or this regression proof is stale"
    )


# ======================================= obligation 11: card v3 registration
def test_11_card_v3_registers_append_only(journal, settings):
    synced = cards.sync_registry(journal, settings)  # the REAL cards dir
    assert "catalyst_momentum_v3:v1" in synced
    again = cards.sync_registry(journal, settings)
    assert "catalyst_momentum_v3:v1" not in again  # idempotent no-op on the second sync

    row = journal.one("SELECT * FROM setup_cards WHERE card_id = 'catalyst_momentum_v3'")
    assert row is not None
    assert row["version"] == 1
    assert row["state"] == "live_eligible"


def test_11_card_v3_content_mutation_refused(journal, settings, tmp_path):
    import shutil

    real_v3 = cards.CARDS_DIR / "catalyst_momentum_v3.yaml"
    shutil.copy(real_v3, tmp_path / "catalyst_momentum_v3.yaml")
    cards.sync_registry(journal, settings, cards_dir=tmp_path)

    mutated = (tmp_path / "catalyst_momentum_v3.yaml").read_text().replace(
        "max_holding_days_default: 10", "max_holding_days_default: 11"
    )
    (tmp_path / "catalyst_momentum_v3.yaml").write_text(mutated)
    with pytest.raises(SettingsError, match="content changed without a version bump"):
        cards.sync_registry(journal, settings, cards_dir=tmp_path)


def test_11_get_default_card_honors_the_active_card_id_setting():
    default = cards.get_default_card()  # no settings -> DEFAULT_CARD_ID, dark
    assert default["card_id"] == "catalyst_momentum_v2"

    resolved = cards.get_default_card(settings=make_settings(ACTIVE_CARD_ID="catalyst_momentum_v3"))
    assert resolved["card_id"] == "catalyst_momentum_v3"
    assert resolved["max_holding_days_default"] == 10


def test_11_get_card_by_id_ignores_active_card_id_entirely():
    """get_card_by_id() is the pinning primitive BASELINE relies on -- it
    must resolve purely by id, never consulting settings at all."""
    v2 = cards.get_card_by_id("catalyst_momentum_v2")
    v3 = cards.get_card_by_id("catalyst_momentum_v3")
    assert v2["max_holding_days_default"] == 3
    assert v3["max_holding_days_default"] == 10
    with pytest.raises(SettingsError, match="not found"):
        cards.get_card_by_id("does_not_exist")


def test_11_scan_candidates_follow_active_card_id_swap_when_s1c_inactive():
    """End-to-end proof for the S1c-INACTIVE fallback path (no corrected
    PER pair registered on a fresh :memory: journal -- s1c_activation_
    preflight refuses, card_activation.active=False): ACTIVE_CARD_ID drives
    candidate_scanner._resolve_card_assignment's own fallback branches.
    This is a REAL, valid production state (S1c not yet armed / preflight
    failed) but NOT the only production state -- see
    test_11_scan_candidates_and_proposals_follow_active_card_id_when_s1c_active
    below for the S1c-ACTIVE path (BLOCKER-1, audit-fixup round 1: the
    original version of THIS test exercised only the inactive branch and
    was mistaken for full coverage of "the live path")."""
    orch = Orchestrator(
        settings=make_settings(ACTIVE_CARD_ID="catalyst_momentum_v3"), journal=JournalStore(":memory:"),
    )
    activation = build_scan_card_activation(
        orch.journal, "2026-06-03T20:00:00+00:00", DEFAULT_UNIVERSE, settings=orch.settings,
    )
    assert activation.active is False, "this test is specifically about the S1c-INACTIVE fallback branch"
    orch.run_scan_once()
    rows = orch.journal.query("SELECT card_id, card_version FROM candidates")
    assert rows  # non-vacuity guard: a scan producing nothing would make this test meaningless
    for row in rows:
        assert row["card_id"] == "catalyst_momentum_v3"
        assert row["card_version"] == 1
    orch.close()


def test_11_dark_default_scan_still_stamps_v2_unchanged():
    """The merge-dark guarantee, end-to-end (S1c-inactive fallback path):
    an unchanged .env (no ACTIVE_CARD_ID override) must stamp candidates
    with catalyst_momentum_v2 exactly like before this build."""
    orch = Orchestrator(settings=make_settings(), journal=JournalStore(":memory:"))
    orch.run_scan_once()
    rows = orch.journal.query("SELECT card_id, card_version FROM candidates")
    assert rows
    for row in rows:
        assert row["card_id"] == cards.DEFAULT_CARD_ID == "catalyst_momentum_v2"
    orch.close()


def test_11_scan_candidates_and_proposals_follow_active_card_id_when_s1c_active(monkeypatch):
    """BLOCKER-1 fix, both audits (2026-08-06 STATUS CORRECTION item 1):
    when S1c activation IS live (the production state since 2026-07-25,
    once the corrected H-PER-1P-v2/H-PER-1N-v2 pair is registered),
    candidate card-stamping flows through
    cards.selector.build_selector_context() -> select_card(), NOT through
    candidate_scanner.py's fallback branches. The ORIGINAL version of this
    test (pre-audit-fixup) used a fresh :memory: journal with NO corrected
    pair registered, so activation was silently INACTIVE and this test
    never actually exercised the selector path at all -- a false green
    (audit A M-1). This version registers the corrected pair, ASSERTS
    activation.active is True (so a future regression that silently
    re-breaks activation fails LOUDLY here, not quietly), and proves BOTH
    candidates AND trade_proposals follow ACTIVE_CARD_ID through the real
    selector path. Fails on pre-fixup code (get_default_card() called bare
    inside build_selector_context, always returning catalyst_momentum_v2
    regardless of ACTIVE_CARD_ID) and passes once settings is threaded
    through build_selector_context()/build_scan_card_activation()."""
    from alphaos.cards import per_evidence
    from alphaos.stats.preregistration import register_hypothesis
    from test_s1c_activation import _force_momentum

    journal = JournalStore(":memory:")
    settings = make_settings(ACTIVE_CARD_ID="catalyst_momentum_v3")

    # Real startup FIRST -- syncs the REAL post_earnings_reaction card off
    # disk (state=shadow, requires_selector=card_selector_v1, both already
    # satisfied by the real YAML) into setup_cards, so the identity the
    # corrected pair registers against is the genuine production identity,
    # not test_s1c_activation.py's own hand-seeded stand-in row (which
    # would collide with orchestrator.startup()'s own sync_registry() call
    # inside run_scan_once() below -- same card_id/version, different
    # content_hash -> SettingsError).
    orch = Orchestrator(settings=settings, journal=journal)
    orch.startup()
    identity = per_evidence.fetch_active_per_card_identity(journal)
    for hypothesis, metric, direction in (
        (per_evidence.PER_HYPOTHESIS_POS_V2, per_evidence.PER_METRIC_POS_V2, "positive"),
        (per_evidence.PER_HYPOTHESIS_NEG_V2, per_evidence.PER_METRIC_NEG_V2, "negative"),
    ):
        register_hypothesis(
            journal, hypothesis=hypothesis, metric=metric, floor_effective_n=20, floor_span_days=90.0,
            analysis_not_before="2020-01-01", params={**identity, "direction": direction},
        )

    # No PER-eligible earnings event seeded -- every candidate resolves to
    # the frozen SelectorContext's own default_card, which is exactly the
    # value BLOCKER-1 needed threaded through.
    activation = build_scan_card_activation(
        journal, "2026-06-03T20:00:00+00:00", DEFAULT_UNIVERSE, settings=settings,
    )
    assert activation.active is True, (
        f"S1c activation must be ACTIVE for this test to be production-shaped "
        f"(reason={activation.reason}) -- otherwise this silently degrades back "
        f"to the false-green fallback-only test this replaces"
    )
    assert activation.context.default_card["card_id"] == "catalyst_momentum_v3"

    _force_momentum(monkeypatch, orch.scanner.market)
    orch.run_scan_once()

    cand_rows = orch.journal.query("SELECT card_id FROM candidates")
    assert cand_rows, "scan produced zero candidates -- test fixture is vacuous"
    for r in cand_rows:
        assert r["card_id"] == "catalyst_momentum_v3"

    prop_rows = orch.journal.query("SELECT card_id FROM trade_proposals")
    assert prop_rows, "scan produced zero proposals -- test fixture is vacuous"
    for r in prop_rows:
        assert r["card_id"] == "catalyst_momentum_v3"
    orch.close()
