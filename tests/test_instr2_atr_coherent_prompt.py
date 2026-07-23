"""INSTR-2: ATR-coherent evaluator targets (prompt v2, live-path, gated).

docs/roadmap/alphaos-evaluator-replay-and-coherence-specs.md, "## INSTR-2 --
ATR-coherent evaluator targets (prompt v2, live-path, gated)". Hermetic
throughout -- the live HTTP call itself is always monkeypatched out
(``_live_eval``), never real network; direct construction; date-independent
(no wall-clock dependence anywhere in this file). Covers the 12 tests in the
spec's own Tests section that are NOT about the AB-EVAL-1 harness (tests 13
and 14 -- the arms mechanics and the AST updates to replay_packet/evaluate()
-- live in tests/test_ab_eval.py alongside the rest of that harness).
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
import types

import pytest

from alphaos.ai.openai_client import ATR_RULES_V1, OpenAIClient, OpenAIEvaluation
from alphaos.ai import openai_client as oc_module
from alphaos.ai.prompt_templates import _public, build_no_news_user_prompt
from alphaos.config.settings import SettingsError
from alphaos.constants import Decision, ReasonCode, TradeDirection
from conftest import make_settings


def _seed_atr(journal, symbol, atr_14, market_date="2026-07-08"):
    journal.insert("atr_history", {
        "atr_id": f"atr_{symbol}_{market_date}", "symbol": symbol, "market_date": market_date,
        "atr_14": atr_14, "rules_version": ATR_RULES_V1, "n_bars_fetched": 15,
    })


def _fake_propose_eval(model="gpt-5.4-mini", entry=100.0, stop=97.0, target=110.0,
                       direction=TradeDirection.LONG.value, expected_r=3.33, symbol="AAPL"):
    return OpenAIEvaluation(
        eval_id="ev1", candidate_id="c1", symbol=symbol, model=model,
        direction=direction, entry=entry, stop=stop, target=target, max_holding_days=3,
        expected_r=expected_r, confidence=0.8, decision=Decision.PROPOSE.value,
        reasoning_summary="x", is_mock=False,
    )


def _v2_live_client(journal, **overrides):
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test",
                             OPENAI_PROMPT_VERSION="v2", **overrides)
    return OpenAIClient(settings, journal)


# ------------------------------------------------------------- test 1: builder
def test_v2_builder_renders_interpolated_values_and_both_worked_examples():
    """The rendered ATR_STOP_POLICY section contains the interpolated
    atr_14/risk_per_share/min_target_distance values (read straight off the
    atr_policy dict) AND both the long and short formula lines AND both
    worked examples -- the model needs both regardless of which direction
    it ultimately proposes."""
    atr_policy = {
        "atr_14": 3.5, "stop_multiplier": 2.0, "risk_per_share": 7.0,
        "min_reward_risk": 1.2, "min_target_distance": 8.4, "rules_version": ATR_RULES_V1,
    }
    prompt = build_no_news_user_prompt(
        {"symbol": "AAPL", "direction": "long"}, {"last_price": 100.0}, "usable",
        atr_policy=atr_policy,
    )

    assert "ATR_STOP_POLICY:" in prompt
    assert "- long:  stop = entry - 2.0 x ATR(14)" in prompt
    assert "- short: stop = entry + 2.0 x ATR(14)" in prompt
    assert "ATR(14) = 3.5" in prompt
    assert "2.0 x 3.5 = 7.0" in prompt
    assert "below 1.2" in prompt
    assert "at least 8.4" in prompt
    assert "Worked example (long):" in prompt
    assert "Worked example (short):" in prompt
    # Section sits between MARKET_SNAPSHOT and DATA_FRESHNESS.
    assert prompt.index("MARKET_SNAPSHOT:") < prompt.index("ATR_STOP_POLICY:") < prompt.index("DATA_FRESHNESS:")


# ------------------------------------------------------ test 2: v1 golden test
def _pre_instr2_prompt(candidate, snapshot, freshness_status) -> str:
    """Verbatim reproduction of build_no_news_user_prompt() as it existed
    before INSTR-2 (no atr_policy parameter, no MARKET_SNAPSHOT pop, no
    ATR_STOP_POLICY section) -- the merge-dark guarantee's own reference
    text."""
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


def test_v1_golden_prompt_byte_identical_to_pre_instr2():
    """The merge-dark guarantee, as a hard byte-identity test (not fuzzy):
    default settings (atr_policy omitted -> None) must produce EXACTLY the
    pre-INSTR-2 prompt for a fixed candidate/snapshot."""
    candidate = {"candidate_id": "cand_x", "symbol": "AAPL", "direction": "long",
                "momentum_score": 0.7, "last_price": 210.55}
    snapshot = {"last_price": 210.55, "volume": 1_200_000, "rel_strength": 0.6}

    golden = _pre_instr2_prompt(candidate, snapshot, "usable")
    actual = build_no_news_user_prompt(candidate, snapshot, "usable")

    assert actual == golden


# ------------------------------------------------------- test 3: pop hygiene
def test_builder_pops_atr_policy_from_market_snapshot_in_both_versions():
    """Hygiene against a replayed v2-era fixture leaking its archived
    atr_policy block into a v1-shaped prompt: even when atr_policy=None
    (v1 rendering), a snapshot that ALREADY carries an "atr_policy" key
    (e.g. a stale fixture) must never see that key's values appear in the
    serialized MARKET_SNAPSHOT section."""
    stale_block = {"atr_14": 999.0, "stop_multiplier": 42.0, "rules_version": "should_never_leak"}
    snapshot = {"last_price": 100.0, "atr_policy": stale_block}

    prompt_v1 = build_no_news_user_prompt({"symbol": "AAPL"}, snapshot, "usable", atr_policy=None)
    assert "999.0" not in prompt_v1
    assert "should_never_leak" not in prompt_v1
    assert '"atr_policy"' not in prompt_v1

    # Also true when v2 IS active (a fresh, different block is rendered as
    # the ATR_STOP_POLICY section, but the STALE block embedded inside the
    # snapshot dict itself must still never appear in MARKET_SNAPSHOT).
    fresh_block = {"atr_14": 2.0, "stop_multiplier": 2.0, "risk_per_share": 4.0,
                   "min_reward_risk": 1.2, "min_target_distance": 4.8, "rules_version": ATR_RULES_V1}
    prompt_v2 = build_no_news_user_prompt({"symbol": "AAPL"}, snapshot, "usable", atr_policy=fresh_block)
    assert "999.0" not in prompt_v2
    assert "should_never_leak" not in prompt_v2
    assert '"atr_policy"' not in prompt_v2


# ---------------------------------------------------- test 4: no hard-coding
def test_builder_never_hardcodes_policy_numbers():
    """Rendering with a non-default stop_multiplier/min_reward_risk shows
    THOSE numbers, including in the worked-example arithmetic (which is
    derived from them, not from the spec's own default-config example) --
    proof the template interpolates config, never literals."""
    atr_policy = {
        "atr_14": 4.0, "stop_multiplier": 3.0, "risk_per_share": 12.0,
        "min_reward_risk": 1.5, "min_target_distance": 18.0, "rules_version": ATR_RULES_V1,
    }
    prompt = build_no_news_user_prompt({"symbol": "AAPL"}, {"last_price": 100.0}, "usable",
                                       atr_policy=atr_policy)

    assert "stop = entry - 3.0 x ATR(14)" in prompt
    assert "below 1.5" in prompt
    # Worked example must recompute using stop_multiplier=3.0/min_reward_risk=1.5
    # against the FIXED illustrative anchors (entry 100.00, ATR 2.50) -- never
    # the spec's own default-config 2.0/1.2 worked-example numbers.
    # risk_per_share = 3.0*2.50 = 7.50; target_distance = 1.5*7.50 = 11.25
    assert "92.50, risk per share 7.50; target 111.25 computes 11.25/7.50 = 1.50." in prompt  # long
    assert "107.50, risk per share 7.50; target 88.75 computes 11.25/7.50 = 1.50." in prompt  # short
    assert "= 1.20." not in prompt  # the default-config worked-example rr never leaks in here


# ---------------------------------------------------------- test 5: no ATR
def test_missing_atr_under_v2_renders_v1_shaped_and_no_atr_fail_safe_still_fires(journal):
    """No atr_history seeded: the augmented snapshot carries no atr_policy
    key (prompt renders v1-shaped), and a raw propose is STILL rejected
    NO_ATR_DATA by the completely unchanged _apply_atr_stop fail-safe."""
    client = _v2_live_client(journal)
    candidate = {"symbol": "AAPL", "direction": "long"}
    snapshot = {"last_price": 100.0}

    augmented = client._augment_snapshot_for_prompt(snapshot, candidate)
    assert "atr_policy" not in augmented
    prompt = build_no_news_user_prompt(candidate, augmented, "usable",
                                       atr_policy=augmented.get("atr_policy"))
    assert "ATR_STOP_POLICY" not in prompt

    client._live_eval = types.MethodType(lambda self, c, s, f: _fake_propose_eval(), client)
    result = client.evaluate(candidate, snapshot, freshness_status="usable")

    assert result.decision == Decision.REJECT.value
    assert ReasonCode.NO_ATR_DATA.value in result.risk_flags


# ------------------------------------------------- test 6: augment read raises
def test_augment_time_atr_read_raising_degrades_to_v1_prompt_never_propagates(journal):
    """The augment-time ATR read has its OWN try/except, outside
    raw_evaluate's/post_process's containment: a transient error must
    journal an ERROR and degrade to a v1-shaped snapshot (no atr_policy
    key), never raise out of _augment_snapshot_for_prompt itself."""
    client = _v2_live_client(journal)

    def _raising_scalar(sql, params=()):
        raise Exception("simulated transient SQLite error on atr_history read")

    journal.scalar = _raising_scalar
    candidate = {"symbol": "AAPL", "direction": "long"}
    snapshot = {"last_price": 100.0}

    result = client._augment_snapshot_for_prompt(snapshot, candidate)

    assert result is snapshot  # unchanged, no exception propagated
    assert "atr_policy" not in result
    event = journal.one("SELECT * FROM system_events WHERE category = 'openai' AND severity = 'error'")
    assert event is not None
    assert "AAPL" in event["message"]


# --------------------------------------------- test 7: prompt_template_version
def test_prompt_template_version_stamped_from_active_settings_on_every_path(journal):
    # mock path: default settings (v1)
    mock_client = OpenAIClient(make_settings(), journal)
    mock_result = mock_client.evaluate(
        {"candidate_id": "c1", "symbol": "AAPL", "direction": "long", "momentum_score": 0.9},
        {"last_price": 100.0},
    )
    assert mock_result.prompt_template_version == "v1"

    # live propose path under v2
    _seed_atr(journal, "AAPL", atr_14=2.0)
    live_client = _v2_live_client(journal)
    live_client._live_eval = types.MethodType(lambda self, c, s, f: _fake_propose_eval(), live_client)
    live_result = live_client.evaluate({"symbol": "AAPL", "direction": "long"}, {"last_price": 100.0})
    assert live_result.decision == Decision.PROPOSE.value
    assert live_result.prompt_template_version == "v2"

    # post_process rejection path (NO_ATR_DATA) under v2 -- a NEW rejection
    # object post_process() swaps in must still carry the stamp.
    unseeded_client = _v2_live_client(journal)
    unseeded_client._live_eval = types.MethodType(
        lambda self, c, s, f: _fake_propose_eval(entry=100.0, symbol="NEWSYM"), unseeded_client,
    )
    rej_result = unseeded_client.evaluate({"symbol": "NEWSYM", "direction": "long"}, {"last_price": 100.0})
    assert rej_result.decision == Decision.REJECT.value
    assert rej_result.prompt_template_version == "v2"

    # Default field value: a direct-constructed fixture (no settings involved)
    # keeps working unchanged.
    assert OpenAIEvaluation(
        eval_id="e", candidate_id="c", symbol="AAPL", model="m", direction="long",
        entry=None, stop=None, target=None, max_holding_days=None, expected_r=None,
        confidence=None, decision=Decision.REJECT.value, reasoning_summary="x",
    ).prompt_template_version == "v1"


# ------------------------------------------------- test 8: snapshot journaling
def test_snapshot_journaling_carries_atr_policy_under_v2_not_under_v1(journal):
    _seed_atr(journal, "AAPL", atr_14=2.0)

    v2_client = _v2_live_client(journal)
    v2_client._live_eval = types.MethodType(lambda self, c, s, f: _fake_propose_eval(), v2_client)
    v2_result = v2_client.evaluate({"symbol": "AAPL", "direction": "long"}, {"last_price": 100.0})
    assert "atr_policy" in v2_result.snapshot
    assert v2_result.snapshot["atr_policy"]["atr_14"] == 2.0

    v1_client = OpenAIClient(
        make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test"), journal,
    )
    v1_client._live_eval = types.MethodType(lambda self, c, s, f: _fake_propose_eval(), v1_client)
    v1_result = v1_client.evaluate({"symbol": "AAPL", "direction": "long"}, {"last_price": 100.0})
    assert "atr_policy" not in v1_result.snapshot

    # And to_row() persists it in snapshot_json for the v2 row.
    assert v2_result.to_row()["snapshot_json"]["atr_policy"]["atr_14"] == 2.0
    assert "atr_policy" not in v1_result.to_row()["snapshot_json"]


# ------------------------------------------------------ test 9: settings gate
def test_settings_validates_openai_prompt_version():
    with pytest.raises(SettingsError):
        make_settings(OPENAI_PROMPT_VERSION="v3")

    assert make_settings().openai_prompt_version == "v1"
    assert make_settings(OPENAI_PROMPT_VERSION="v2").openai_prompt_version == "v2"


# --------------------------------------------------- test 10: structural AST
def test_latest_atr_is_the_single_atr_lookup_site_ast():
    """_apply_atr_stop and _augment_snapshot_for_prompt must BOTH route
    through the one _latest_atr() helper (never independently re-issue the
    raw SQL), and the live scan path must still apply _apply_atr_stop after
    a v2 evaluation unconditionally -- post_process() must not gate that
    call on openai_prompt_version."""
    module_source = inspect.getsource(oc_module)
    # Exactly ONE definition of the raw SQL string, inside _latest_atr.
    assert module_source.count("SELECT atr_14 FROM atr_history") == 1

    apply_src = inspect.getsource(oc_module.OpenAIClient._apply_atr_stop)
    apply_tree = ast.parse(textwrap.dedent(apply_src))
    apply_calls = [
        node.func.id for node in ast.walk(apply_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "_latest_atr" in apply_calls

    augment_src = inspect.getsource(oc_module.OpenAIClient._augment_snapshot_for_prompt)
    augment_tree = ast.parse(textwrap.dedent(augment_src))
    augment_calls = [
        node.func.id for node in ast.walk(augment_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "_latest_atr" in augment_calls

    post_process_src = inspect.getsource(oc_module.OpenAIClient.post_process)
    assert "openai_prompt_version" not in post_process_src
    assert "_apply_atr_stop" in post_process_src


# --------------------------------------------------- test 11: containment
def test_containment_preserved_under_v2_atr_read_exception_in_post_process(journal):
    """Audit-HIGH regression guard (2026-07-20), re-proven under v2: a
    genuine raise during post_process()'s OWN _apply_atr_stop call (as
    opposed to the augment-time read, which succeeds here) must still be
    contained to a journaled ERROR + safe OPENAI_REJECT rejection, never
    propagate. The existing test_evaluate_contains_atr_read_exception_as_
    safe_reject in tests/test_ab_eval.py stays green UNMODIFIED through
    this diff (it exercises the v1 default, where the augment step never
    even calls journal.scalar) -- this is the v2-specific companion."""
    _seed_atr(journal, "AAPL", atr_14=2.0)
    client = _v2_live_client(journal)
    client._live_eval = types.MethodType(lambda self, c, s, f: _fake_propose_eval(), client)

    real_scalar = journal.scalar
    calls = {"n": 0}

    def _flaky_scalar(sql, params=()):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_scalar(sql, params)  # the augment-time read succeeds
        raise Exception("simulated transient SQLite error on atr_history read")

    journal.scalar = _flaky_scalar

    result = client.evaluate({"symbol": "AAPL"}, {"last_price": 100.0}, freshness_status="usable")

    assert calls["n"] == 2  # augment read, then post_process's _apply_atr_stop read
    assert result.decision == Decision.REJECT.value  # returned, not raised
    assert ReasonCode.OPENAI_REJECT.value in result.risk_flags
    event = journal.one(
        "SELECT * FROM system_events WHERE category = 'openai' AND severity = 'error'")
    assert event is not None


# ---------------------------------------------------------- test 12: coherence
def test_prompt_implied_stop_arithmetic_matches_apply_atr_stop_enforced_stop(journal):
    """Shared-source guarantee: the stop arithmetic the prompt block implies
    (entry -/+ stop_multiplier x atr_14) equals _apply_atr_stop's OWN
    enforced stop for the same entry -- both read through _latest_atr, so
    they cannot disagree."""
    _seed_atr(journal, "AAPL", atr_14=3.25)
    client = _v2_live_client(journal)
    entry = 187.40

    augmented = client._augment_snapshot_for_prompt({"last_price": entry}, {"symbol": "AAPL"})
    policy = augmented["atr_policy"]
    assert policy["atr_14"] == 3.25

    long_ev = OpenAIEvaluation(
        eval_id="e1", candidate_id="c1", symbol="AAPL", model="gpt-5.4-mini",
        direction=TradeDirection.LONG.value, entry=entry, stop=999.0, target=entry + 20,
        max_holding_days=3, expected_r=1.0, confidence=0.8, decision=Decision.PROPOSE.value,
        reasoning_summary="x",
    )
    enforced_long = client._apply_atr_stop(long_ev, {"symbol": "AAPL"})
    implied_long_stop = round(entry - policy["stop_multiplier"] * policy["atr_14"], 2)
    assert enforced_long.stop == implied_long_stop

    short_ev = OpenAIEvaluation(
        eval_id="e2", candidate_id="c2", symbol="AAPL", model="gpt-5.4-mini",
        direction=TradeDirection.SHORT.value, entry=entry, stop=1.0, target=entry - 20,
        max_holding_days=3, expected_r=1.0, confidence=0.8, decision=Decision.PROPOSE.value,
        reasoning_summary="x",
    )
    enforced_short = client._apply_atr_stop(short_ev, {"symbol": "AAPL"})
    implied_short_stop = round(entry + policy["stop_multiplier"] * policy["atr_14"], 2)
    assert enforced_short.stop == implied_short_stop

    # And the risk-per-share the prompt quoted matches the enforced stop's
    # own distance from entry.
    assert round(abs(entry - enforced_long.stop), 4) == policy["risk_per_share"]
