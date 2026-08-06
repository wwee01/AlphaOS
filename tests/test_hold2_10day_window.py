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
import subprocess
import sys

import pytest

from alphaos.ai import prompt_templates as pt
from alphaos.ai.openai_client import ATR_RULES_V1, OpenAIClient
from alphaos.ai.validation import validate_max_holding_days_range
from alphaos.cards import registry as cards
from alphaos.config.settings import SettingsError
from alphaos.constants import Decision, PROMPT_VERSIONS, ReasonCode
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
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


# ========================================= obligation 4: ACTIVE_CARD_ID gate
def test_4_active_card_id_unknown_raises_at_load():
    with pytest.raises(SettingsError, match="ACTIVE_CARD_ID"):
        make_settings(ACTIVE_CARD_ID="does_not_exist")


def test_4_active_card_id_valid_values_load_and_default_is_dark():
    assert make_settings().active_card_id == "catalyst_momentum_v2"
    assert make_settings(ACTIVE_CARD_ID="catalyst_momentum_v3").active_card_id == "catalyst_momentum_v3"


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
def test_10_cold_import_hold2_modules_no_circular_import():
    """SUSP-1 lesson: a circular import invisible to the warm suite (every
    OTHER test in this repo) shipped a P0 -- a subprocess with a genuinely
    fresh sys.modules is the only way this repo can catch this bug class."""
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
        result = subprocess.run(
            [sys.executable, "-c", f"import {module_name}"],
            capture_output=True, text=True, timeout=30,
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
        "import alphaos.cards.registry\n"
        "from alphaos.config.settings import load_settings\n"
        "s = load_settings(load_env_file=False, env={'ALPHAOS_MODE': 'mock', "
        "'APPROVAL_MODE': 'manual', 'REAL_TRADING_ENABLED': 'false', "
        "'ALPHAOS_DB_PATH': ':memory:', 'MAX_AUTO_APPROVALS_PER_DAY': '1'})\n"
        "assert s.active_card_id == 'catalyst_momentum_v2'\n"
        "print('OK')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


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


def test_11_scan_candidates_follow_active_card_id_swap():
    """End-to-end proof that ACTIVE_CARD_ID actually drives the LIVE
    candidate-stamping mechanism (candidate_scanner._resolve_card_assignment),
    not just get_default_card() in isolation."""
    orch = Orchestrator(
        settings=make_settings(ACTIVE_CARD_ID="catalyst_momentum_v3"), journal=JournalStore(":memory:"),
    )
    orch.run_scan_once()
    rows = orch.journal.query("SELECT card_id, card_version FROM candidates")
    assert rows  # non-vacuity guard: a scan producing nothing would make this test meaningless
    for row in rows:
        assert row["card_id"] == "catalyst_momentum_v3"
        assert row["card_version"] == 1
    orch.close()


def test_11_dark_default_scan_still_stamps_v2_unchanged():
    """The merge-dark guarantee, end-to-end: an unchanged .env (no
    ACTIVE_CARD_ID override) must stamp candidates with catalyst_momentum_v2
    exactly like before this build."""
    orch = Orchestrator(settings=make_settings(), journal=JournalStore(":memory:"))
    orch.run_scan_once()
    rows = orch.journal.query("SELECT card_id, card_version FROM candidates")
    assert rows
    for row in rows:
        assert row["card_id"] == cards.DEFAULT_CARD_ID == "catalyst_momentum_v2"
    orch.close()
