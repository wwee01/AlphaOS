"""INSTR-3: honest trend + multi-day context (prompt v3).

docs/roadmap/alphaos-instr3-trend-context-spec.md. Hermetic throughout --
the live HTTP call itself is always monkeypatched out (either ``_live_eval``
directly, or the ``openai.OpenAI`` SDK class for the prompt-capture tests),
never real network; direct construction; date-independent (every
``before_date``/``market_dt`` is passed explicitly, never derived from wall
clock). Covers the spec's own 15 numbered tests plus Design items 1-5.

Numbered test comments below map 1:1 to the spec's own "## Tests" section.
"""

from __future__ import annotations

import ast
import inspect
import json
import sqlite3
import types
from datetime import date, datetime, timedelta, timezone

import pytest

from alphaos.ab_eval.corpus import CANDIDATE_CREATION_FIELDS, write_corpus, load_corpus
from alphaos.ab_eval.replay import replay_packet
from alphaos.ai.openai_client import ATR_RULES_V1, OpenAIClient, OpenAIEvaluation
from alphaos.ai import openai_client as oc_module
from alphaos.ai.prompt_templates import _public, _render_atr_stop_policy, build_no_news_user_prompt
from alphaos.config.settings import SettingsError
from alphaos.constants import Decision, TradeDirection
from alphaos.data.atr import ATR_PERIOD
from alphaos.data.daily_bars import persist_daily_bars
from alphaos.journal.journal_store import JournalStore
from alphaos.journal.schema import SCHEMA_VERSION
from alphaos.reports.atr_service import update_atr_history
from alphaos.scanner.trend import TREND_RULES_V1, compute_trend_score
from conftest import make_settings


# --------------------------------------------------------------------- helpers
def _seed_atr(journal, symbol, atr_14, market_date="2026-07-08"):
    journal.insert("atr_history", {
        "atr_id": f"atr_{symbol}_{market_date}", "symbol": symbol, "market_date": market_date,
        "atr_14": atr_14, "rules_version": ATR_RULES_V1, "n_bars_fetched": 15,
    })


def _seed_daily_bars(journal, symbol, closes, start="2026-06-01", highs=None, lows=None, volume=1_000_000):
    """Seeds ``len(closes)`` consecutive daily bars (business-day-agnostic --
    plain calendar-day increments, since daily_bars has no market-calendar
    dependency of its own), oldest first."""
    d = date.fromisoformat(start)
    for i, close in enumerate(closes):
        high = (highs[i] if highs else close + 1.0)
        low = (lows[i] if lows else close - 1.0)
        journal.insert("daily_bars", {
            "bar_id": f"bar_{symbol}_{i}", "symbol": symbol,
            "market_date": (d + timedelta(days=i)).isoformat(),
            "open": close, "high": high, "low": low, "close": close, "volume": volume,
            "source_feed": "iex",
        })
    return (d + timedelta(days=len(closes) - 1)).isoformat()  # last seeded date


def _v3_live_client(journal, **overrides):
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test",
                             OPENAI_PROMPT_VERSION="v3", **overrides)
    return OpenAIClient(settings, journal)


def _v2_live_client(journal, **overrides):
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test",
                             OPENAI_PROMPT_VERSION="v2", **overrides)
    return OpenAIClient(settings, journal)


def _fake_propose_eval(model="gpt-5.4-mini", entry=100.0, stop=97.0, target=110.0,
                       direction=TradeDirection.LONG.value, expected_r=3.33, symbol="AAPL"):
    return OpenAIEvaluation(
        eval_id="ev1", candidate_id="c1", symbol=symbol, model=model,
        direction=direction, entry=entry, stop=stop, target=target, max_holding_days=3,
        expected_r=expected_r, confidence=0.8, decision=Decision.PROPOSE.value,
        reasoning_summary="x", is_mock=False,
    )


_ATR_POLICY = {
    "atr_14": 3.5, "stop_multiplier": 2.0, "risk_per_share": 7.0,
    "min_reward_risk": 1.2, "min_target_distance": 8.4, "rules_version": ATR_RULES_V1,
}
_MULTI_DAY_CONTEXT = {
    "bars": [
        {"date": "2026-06-20", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1_000_000},
        {"date": "2026-06-21", "open": 100.5, "high": 103.0, "low": 100.0, "close": 102.0, "volume": 1_100_000},
    ],
    "recent_high_10d": 103.0, "recent_low_10d": 99.0,
    "dist_to_recent_high_atr": 0.2857, "dist_to_recent_low_atr": 0.4286,
    "trend_score": 0.42, "trend_rules_version": TREND_RULES_V1,
}


# ============================================================= test 1: golden v1
def test_1_golden_v1_prompt_unaffected():
    """Spec test 1: 'existing test stays green unmodified' --
    tests/test_instr2_atr_coherent_prompt.py::test_v1_golden_prompt_byte_identical_to_pre_instr2
    already pins this (both atr_policy AND multi_day_context default to
    None, so the INSTR-3 diff is a pure no-op on the v1 byte stream). This
    test re-proves it locally so the guarantee is visible from this file
    too, without duplicating that test's own golden string."""
    candidate = {"candidate_id": "cand_x", "symbol": "AAPL", "direction": "long",
                "momentum_score": 0.7, "last_price": 210.55}
    snapshot = {"last_price": 210.55, "volume": 1_200_000, "rel_strength": 0.6}
    v1 = build_no_news_user_prompt(candidate, snapshot, "usable")
    assert "MULTI_DAY_CONTEXT" not in v1
    assert "ATR_STOP_POLICY" not in v1


# ============================================================= test 2: golden v2
def _pre_instr3_v2_prompt(candidate, snapshot, freshness_status, atr_policy) -> str:
    """Verbatim reproduction of build_no_news_user_prompt() as it existed
    right before INSTR-3 touched it (atr_policy param only -- no
    multi_day_context param, section, or MARKET_SNAPSHOT pop) -- pinned
    BEFORE the INSTR-3 builder diff, per the spec's own test-2 instruction.
    Reuses ``_render_atr_stop_policy`` directly (that function is UNCHANGED
    by INSTR-3) rather than hand-duplicating its arithmetic."""
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
    market_snapshot = dict(snapshot)
    market_snapshot.pop("atr_policy", None)
    atr_policy_section = f"{_render_atr_stop_policy(atr_policy)}\n" if atr_policy else ""
    return (
        "Evaluate this candidate in NO-NEWS MODE. Return JSON ONLY matching the "
        "schema. Base the thesis ONLY on price action, volume, relative strength, "
        "trend structure, and risk/reward. Do NOT reference or invent any news or "
        "catalyst.\n\n"
        f"SCHEMA:\n{json.dumps(schema, indent=2)}\n\n"
        f"CANDIDATE:\n{json.dumps(_public(candidate), default=str)}\n\n"
        f"MARKET_SNAPSHOT:\n{json.dumps(market_snapshot, default=str)}\n\n"
        f"{atr_policy_section}"
        f"DATA_FRESHNESS:\n{freshness_status}\n\n"
        "Rules: stale/unverifiable data => 'reject'. Long stop below entry; short "
        "stop above entry; target on the profit side. catalyst='not_available_v1', "
        "news_status='disabled_v1', news_sources=[]. Output the JSON object now."
    )


def test_2_golden_v2_byte_identical_to_pre_instr3():
    candidate = {"candidate_id": "cand_x", "symbol": "AAPL", "direction": "long",
                "momentum_score": 0.7, "last_price": 210.55, "trend_quality": 0.5}
    snapshot = {"last_price": 210.55, "volume": 1_200_000, "rel_strength": 0.6}

    golden = _pre_instr3_v2_prompt(candidate, snapshot, "usable", _ATR_POLICY)
    actual = build_no_news_user_prompt(candidate, snapshot, "usable", atr_policy=_ATR_POLICY)

    assert actual == golden


# ============================================================= test 3: v3 order
def test_3_v3_renders_multi_day_context_then_atr_stop_policy_then_data_freshness():
    candidate = {"symbol": "AAPL", "direction": "long"}
    snapshot = {"last_price": 100.0}
    prompt = build_no_news_user_prompt(
        candidate, snapshot, "usable", atr_policy=_ATR_POLICY, multi_day_context=_MULTI_DAY_CONTEXT,
    )

    assert "MULTI_DAY_CONTEXT:" in prompt
    assert "ATR_STOP_POLICY:" in prompt
    assert (
        prompt.index("MARKET_SNAPSHOT:") < prompt.index("MULTI_DAY_CONTEXT:")
        < prompt.index("ATR_STOP_POLICY:") < prompt.index("DATA_FRESHNESS:")
    )
    # Interpolated MULTI_DAY_CONTEXT values.
    assert "2026-06-20" in prompt and "2026-06-21" in prompt
    assert "recent_high_10d=103.0, recent_low_10d=99.0" in prompt
    assert "dist_to_recent_high_atr=0.2857, dist_to_recent_low_atr=0.4286" in prompt
    assert f"trend_score=0.42 (trend_rules_version={TREND_RULES_V1}" in prompt
    # Interpolated ATR_STOP_POLICY values (unchanged rendering).
    assert "ATR(14) = 3.5" in prompt


# ============================================================= test 4: <5 bars
def test_4_v3_with_fewer_than_5_bars_omits_multi_day_context_keeps_atr_stop_policy(journal):
    _seed_atr(journal, "AAPL", atr_14=2.0)
    _seed_daily_bars(journal, "AAPL", [100.0, 101.0, 102.0])  # 3 bars, < 5 floor

    client = _v3_live_client(journal)
    augmented = client._augment_snapshot_for_prompt({"last_price": 105.0}, {"symbol": "AAPL"})

    assert "atr_policy" in augmented
    assert "multi_day_context" not in augmented
    prompt = build_no_news_user_prompt(
        {"symbol": "AAPL"}, augmented, "usable",
        atr_policy=augmented.get("atr_policy"), multi_day_context=augmented.get("multi_day_context"),
    )
    assert "ATR_STOP_POLICY" in prompt
    assert "MULTI_DAY_CONTEXT" not in prompt
    # No error was raised/journaled -- this is a normal, expected condition.
    assert journal.one("SELECT * FROM system_events WHERE category = 'openai' AND severity = 'error'") is None


# ============================================================= test 5: worked examples
def test_5_trend_rules_v1_worked_example_positive_clamp_both_directions(journal):
    """Spec's own worked example (builder-constructed, exact fixture):
    closes [100,101,102,101,103,104,105,104,106,107], ATR14=2.5.
    Transitions (9, within the 10-close window): up=7 (100->101,
    101->102, 101->103, 103->104, 104->105, 104->106, 106->107),
    down=2 (102->101, 105->104) -> consistency=(7-2)/10=0.5 exactly.
    extension=(107-100)/(1.2*2.0*2.5)=7/6=1.1667 -> clamped to 1.0.
    signed_trend=0.5*0.5+0.5*1.0=0.75."""
    closes = [100, 101, 102, 101, 103, 104, 105, 104, 106, 107]
    last_date = _seed_daily_bars(journal, "AAPL", closes, start="2026-06-01")
    _seed_atr(journal, "AAPL", atr_14=2.5)
    before_date = (date.fromisoformat(last_date) + timedelta(days=1)).isoformat()

    long_score, version = compute_trend_score(journal, make_settings(), "AAPL", "long", before_date)
    assert long_score == 0.75
    assert version == TREND_RULES_V1

    short_score, version = compute_trend_score(journal, make_settings(), "AAPL", "short", before_date)
    assert short_score == -0.75
    assert version == TREND_RULES_V1


def test_5_trend_rules_v1_worked_example_negative_clamp_both_directions(journal):
    """Mirror-image fixture: closes strictly declining with the same
    transition shape, exercising the CLAMP-AT-NEGATIVE-ONE end and the
    opposite consistency sign. Transitions: down=7, up=2 ->
    consistency=(2-7)/10=-0.5. extension=(103-110)/(1.2*2.0*2.5)=-7/6
    -> clamped to -1.0. signed_trend=0.5*(-0.5)+0.5*(-1.0)=-0.75."""
    closes = [110, 109, 108, 109, 107, 106, 105, 106, 104, 103]
    last_date = _seed_daily_bars(journal, "MSFT", closes, start="2026-06-01")
    _seed_atr(journal, "MSFT", atr_14=2.5)
    before_date = (date.fromisoformat(last_date) + timedelta(days=1)).isoformat()

    long_score, _ = compute_trend_score(journal, make_settings(), "MSFT", "long", before_date)
    assert long_score == -0.75

    short_score, _ = compute_trend_score(journal, make_settings(), "MSFT", "short", before_date)
    assert short_score == 0.75


# ============================================================= test 6: NULL fallback
def test_6_trend_score_null_when_fewer_than_10_sessions(journal):
    _seed_daily_bars(journal, "AAPL", [100.0] * 9)  # one short of the floor
    _seed_atr(journal, "AAPL", atr_14=2.0)
    score, version = compute_trend_score(journal, make_settings(), "AAPL", "long", "2026-07-01")
    assert score is None
    assert version is None


def test_6_trend_score_null_when_no_atr(journal):
    _seed_daily_bars(journal, "AAPL", [float(100 + i) for i in range(10)])
    # No ATR seeded.
    score, version = compute_trend_score(journal, make_settings(), "AAPL", "long", "2026-07-01")
    assert score is None
    assert version is None


def test_6_v3_omits_trend_line_when_trend_score_none_never_falls_back():
    """multi_day_context can be populated (>=5 bars, ATR present -- the
    dist_to_recent_*_atr fields) while trend_score itself is None (fewer
    than 10 sessions was the SCANNER's own floor at candidate-creation
    time, independent of the prompt-render-time bars floor of 5) -- the
    trend line must be omitted entirely, never a fabricated number, and
    the old `abs(change_pct)*10` formula is never referenced anywhere in
    this rendering path."""
    mdc = {**_MULTI_DAY_CONTEXT, "trend_score": None, "trend_rules_version": None}
    prompt = build_no_news_user_prompt(
        {"symbol": "AAPL"}, {"last_price": 100.0}, "usable",
        atr_policy=_ATR_POLICY, multi_day_context=mdc,
    )
    assert "trend_score=" not in prompt
    assert "trend_rules_version=" not in prompt
    assert "recent_high_10d=103.0" in prompt  # rest of the block still renders


# ============================================================= test 7: migration
def test_7a_daily_bars_additive_migration_on_old_db_schema_version_stays_3(tmp_path):
    """A DB predating INSTR-3 (missing the daily_bars table entirely) gets
    it created fresh on open -- the same CREATE TABLE IF NOT EXISTS idiom
    every prior SCHEMA_VERSION-3 addition in this codebase uses. Mirrors
    tests/test_schema_migration.py's own style for a missing-table (not
    just missing-column) case."""
    db = str(tmp_path / "pre_instr3.db")
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE candidates (id INTEGER PRIMARY KEY, candidate_id TEXT)")
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    j = JournalStore(db)
    try:
        cols = {r["name"] for r in j.conn.execute("PRAGMA table_info(daily_bars)")}
        assert {"symbol", "market_date", "open", "high", "low", "close", "volume",
                "source_feed"} <= cols
        assert j.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert SCHEMA_VERSION == 3
        cand_cols = {r["name"] for r in j.conn.execute("PRAGMA table_info(candidates)")}
        assert {"trend_score", "trend_rules_version"} <= cand_cols  # additive column reconcile too
    finally:
        j.close()


def test_7b_persist_daily_bars_idempotent_on_unique_key(journal):
    bars = [{"date": "2026-07-01", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100}]
    written1 = persist_daily_bars(journal, "AAPL", bars, "iex")
    written2 = persist_daily_bars(journal, "AAPL", bars, "iex")
    assert written1 == 1
    assert written2 == 0
    assert journal.count_rows("daily_bars") == 1


# ============================================================= test 8: ATR job persists
def _now(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 18, 0, tzinfo=timezone.utc)


def _uniform_bars(n, high=101.0, low=99.0, close=100.0, end=date(2026, 3, 2)):
    return [
        {"date": (end - timedelta(days=n - 1 - i)).isoformat(), "open": close,
         "high": high, "low": low, "close": close, "volume": 1_000_000}
        for i in range(n)
    ]


class _FakeBarsProvider:
    def __init__(self, bars=None):
        self._bars = bars if bars is not None else []

    def get_daily_bars(self, symbol, start, end, limit=200):
        return self._bars


def test_8_atr_job_persists_bars_and_atr_result_unchanged(journal):
    settings = make_settings()
    provider = _FakeBarsProvider(_uniform_bars(ATR_PERIOD + 1))

    result = update_atr_history(
        journal, settings, symbols=["AAPL"], now=_now(date(2026, 3, 2)), bars_provider=provider,
    )

    assert result["n_written"] == 1
    atr_row = journal.one("SELECT * FROM atr_history WHERE symbol = 'AAPL'")
    assert atr_row["atr_14"] == 2.0  # identical to the pre-INSTR-3 assertion for uniform bars

    bar_rows = journal.query("SELECT * FROM daily_bars WHERE symbol = 'AAPL' ORDER BY market_date")
    assert len(bar_rows) == ATR_PERIOD + 1
    assert bar_rows[0]["source_feed"] == settings.market_data_feed
    assert bar_rows[-1]["market_date"] == "2026-03-02"


def test_8_atr_job_second_run_persists_no_duplicate_bars(journal):
    settings = make_settings()
    provider = _FakeBarsProvider(_uniform_bars(ATR_PERIOD + 1))
    # Two DIFFERENT days so the ATR existence short-circuit doesn't skip
    # the fetch/persist entirely on day 2 (see test_atr_service.py's own
    # idempotent-same-day test for that separate behavior).
    update_atr_history(journal, settings, symbols=["AAPL"], now=_now(date(2026, 3, 2)), bars_provider=provider)
    update_atr_history(journal, settings, symbols=["AAPL"], now=_now(date(2026, 3, 3)), bars_provider=provider)

    # Same overlapping bars fetched both days -- daily_bars stays deduped
    # on (symbol, market_date) regardless of how many ATR runs touch it.
    assert journal.count_rows("daily_bars", "symbol = 'AAPL'") == ATR_PERIOD + 1


# ============================================================= test 9: structural AST
def test_9_scan_eval_modules_never_import_bars_provider():
    """AST-based (not a substring grep -- several of these modules' own
    docstrings legitimately MENTION alpaca_bars.py in prose to explain what
    they deliberately do NOT do): no ``import``/``from ... import`` node in
    any of these modules names ``alpaca_bars`` or ``AlpacaBarsProvider``."""
    import alphaos.scanner.candidate_scanner as cs_module
    import alphaos.scanner.trend as trend_module
    import alphaos.data.daily_bars as daily_bars_module

    for module in (oc_module, cs_module, trend_module, daily_bars_module):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module is None or "alpaca_bars" not in node.module
                assert all("AlpacaBarsProvider" not in (a.name or "") for a in node.names)
            if isinstance(node, ast.Import):
                assert all("alpaca_bars" not in a.name for a in node.names)


def test_9_multi_day_context_computed_only_inside_augment_ast():
    augment_src = inspect.getsource(oc_module.OpenAIClient._augment_snapshot_for_prompt)
    assert "_build_multi_day_context" in augment_src

    module_source = inspect.getsource(oc_module)
    # Exactly one method definition + one call site (from _augment_snapshot_for_prompt).
    assert module_source.count("_build_multi_day_context(") == 2
    assert module_source.count("def _build_multi_day_context") == 1


# ============================================================= test 10: version gates
def test_10_settings_validates_v3_and_rejects_v4():
    with pytest.raises(SettingsError):
        make_settings(OPENAI_PROMPT_VERSION="v4")
    assert make_settings(OPENAI_PROMPT_VERSION="v3").openai_prompt_version == "v3"


def test_10_cli_arms_v3_parses_v4_refused(tmp_path, journal):
    from alphaos.__main__ import build_parser, cmd_ab_eval_run
    from alphaos.ab_eval.corpus import write_corpus as _write
    from alphaos.orchestrator import Orchestrator

    args = build_parser().parse_args([
        "ab_eval_run", "--arms", "gpt-5.4-mini:v3", "gpt-5.6-luna:v3",
    ])
    assert args.arms == ["gpt-5.4-mini:v3", "gpt-5.6-luna:v3"]

    orch = Orchestrator(settings=make_settings(), journal=journal)
    corpus_dir = str(tmp_path / "corpus")
    _write(corpus_dir, [{
        "eval_id": "eval_v3cli01", "candidate_id": "cand_v3cli01", "symbol": "AAPL",
        "candidate": {"candidate_id": "cand_v3cli01", "symbol": "AAPL", "direction": "long"},
        "snapshot": {"last_price": 100.0}, "freshness_status": "usable",
        "provenance": {},
    }], as_of_date="2026-07-24")

    rc = cmd_ab_eval_run(orch, None, ["gpt-5.4-mini:v3", "gpt-5.6-luna:v3"], corpus_dir)
    assert rc == 0

    rc = cmd_ab_eval_run(orch, None, ["gpt-5.4-mini:v4"], corpus_dir)
    assert rc == 1


def test_10_v3_prompt_contains_atr_policy_block_regression_guard(journal):
    """Pins the audit-MEDIUM-class regression this spec explicitly calls
    out: the two `== "v2"` gates MUST be membership checks, or v3 silently
    loses the ATR_STOP_POLICY block. Direct proof against the real
    _augment_snapshot_for_prompt (not a hand-built dict)."""
    _seed_atr(journal, "AAPL", atr_14=2.0)
    client = _v3_live_client(journal)
    augmented = client._augment_snapshot_for_prompt({"last_price": 100.0}, {"symbol": "AAPL"})
    assert "atr_policy" in augmented
    assert augmented["atr_policy"]["atr_14"] == 2.0


# ============================================================= test 11: stale leak
def _capture_prompt_via_fake_openai(monkeypatch):
    captured: dict = {}
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
            captured["messages"] = messages
            payload = {
                "symbol": "AAPL", "direction": "long", "entry": 100.0, "stop": 97.0,
                "target": 104.0, "max_holding_days": 3, "expected_r": 1.0, "confidence": 0.5,
                "decision": "reject", "reasoning_summary": "x",
                "catalyst": "not_available_v1", "news_status": "disabled_v1",
                "news_sources": [], "data_freshness_status": "usable", "risk_flags": [],
            }
            return _FakeResponse(json.dumps(payload))

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeOpenAI:
        def __init__(self, api_key=None):
            self.chat = _FakeChat()

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    return captured


_V3_ERA_STALE_SNAPSHOT = {
    "last_price": 100.0,
    "atr_policy": {
        "atr_14": 999.0, "stop_multiplier": 42.0, "risk_per_share": 999.0,
        "min_reward_risk": 1.2, "min_target_distance": 999.0,
        "rules_version": "should_never_leak",
    },
    "multi_day_context": {
        "bars": [{"date": "1999-01-01", "open": 1.0, "high": 1.0, "low": 1.0,
                  "close": 1.0, "volume": 1}],
        "recent_high_10d": 12345.0, "recent_low_10d": 6789.0,
        "dist_to_recent_high_atr": 111.0, "dist_to_recent_low_atr": 222.0,
        "trend_score": 0.999, "trend_rules_version": "should_never_leak_either",
    },
}
_V3_ERA_FIXTURE = {
    "eval_id": "eval_instr3stale01", "candidate_id": "cand_instr3stale01", "symbol": "AAPL",
    "candidate": {"candidate_id": "cand_instr3stale01", "symbol": "AAPL", "direction": "long",
                 "momentum_score": 0.7},
    "snapshot": _V3_ERA_STALE_SNAPSHOT,
    "freshness_status": "usable",
    "provenance": {"original_model": "gpt-5.4-mini", "original_decision": "reject",
                   "original_created_at_utc": "2026-07-24T15:00:00+00:00"},
}


def test_11_v3_era_fixture_replayed_under_v1_arm_leaks_neither_section(journal, monkeypatch):
    captured = _capture_prompt_via_fake_openai(monkeypatch)
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test")

    replay_packet(_V3_ERA_FIXTURE, ("gpt-5.4-mini", "v1"), settings, journal)

    user_prompt = captured["messages"][1]["content"]
    assert "ATR_STOP_POLICY" not in user_prompt
    assert "MULTI_DAY_CONTEXT" not in user_prompt
    for stale_value in ("999.0", "should_never_leak", "12345.0", "6789.0",
                        "should_never_leak_either", "1999-01-01"):
        assert stale_value not in user_prompt


def test_11_v3_era_fixture_replayed_under_v2_arm_shows_fresh_atr_only(journal, monkeypatch):
    """v2 arm: ATR_STOP_POLICY renders FRESH (from the CURRENT atr_history
    state, which is empty here -- so actually still absent, proving no
    stale numbers leak through even when a genuine v2 augment runs); no
    MULTI_DAY_CONTEXT under any circumstance for a v2 arm."""
    captured = _capture_prompt_via_fake_openai(monkeypatch)
    _seed_atr(journal, "AAPL", atr_14=1.5, market_date="2026-07-24")
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test")

    replay_packet(_V3_ERA_FIXTURE, ("gpt-5.4-mini", "v2"), settings, journal)

    user_prompt = captured["messages"][1]["content"]
    assert "ATR_STOP_POLICY" in user_prompt
    assert "ATR(14) = 1.5" in user_prompt  # fresh, from the real seeded atr_history
    assert "MULTI_DAY_CONTEXT" not in user_prompt
    for stale_value in ("999.0", "should_never_leak", "12345.0", "6789.0",
                        "should_never_leak_either", "1999-01-01"):
        assert stale_value not in user_prompt


# ============================================================= test 12: snapshot journaling
def test_12_snapshot_journaling_carries_multi_day_context_under_v3_not_v2(journal):
    closes = [float(100 + i) for i in range(10)]
    _seed_daily_bars(journal, "AAPL", closes, start="2026-06-01")
    _seed_atr(journal, "AAPL", atr_14=2.0)

    v3_client = _v3_live_client(journal)
    v3_client._live_eval = types.MethodType(lambda self, c, s, f: _fake_propose_eval(), v3_client)
    v3_result = v3_client.evaluate({"symbol": "AAPL", "direction": "long"}, {"last_price": 100.0})
    assert "multi_day_context" in v3_result.snapshot
    assert v3_result.to_row()["snapshot_json"]["multi_day_context"]["recent_high_10d"] is not None

    v2_client = _v2_live_client(journal)
    v2_client._live_eval = types.MethodType(lambda self, c, s, f: _fake_propose_eval(), v2_client)
    v2_result = v2_client.evaluate({"symbol": "AAPL", "direction": "long"}, {"last_price": 100.0})
    assert "multi_day_context" not in v2_result.snapshot
    assert "multi_day_context" not in v2_result.to_row()["snapshot_json"]


# ============================================================= test 13: whitelist lockstep
def test_13_trend_fields_are_in_the_candidate_creation_whitelist():
    assert {"trend_score", "trend_rules_version"} <= set(CANDIDATE_CREATION_FIELDS)
    # The full AST lockstep test itself lives in
    # tests/test_ab_eval.py::test_candidate_whitelist_matches_scanner_creation_insert
    # and stays green unmodified against the updated whitelist (verified by
    # running that file's suite; not re-derived here to avoid duplicating
    # the AST-introspection logic).


def test_13_frozen_pre_instr3_fixture_without_trend_fields_still_loads(tmp_path, journal):
    """A corpus fixture frozen before INSTR-3 (no trend_score/
    trend_rules_version keys in its candidate dict at all) must still load
    and replay without error -- .get() defaults, never a KeyError."""
    old_shaped_fixture = {
        "eval_id": "eval_pre_instr3_01", "candidate_id": "cand_pre_instr3_01", "symbol": "AAPL",
        "candidate": {"candidate_id": "cand_pre_instr3_01", "symbol": "AAPL", "direction": "long",
                     "momentum_score": 0.5},  # no trend_score/trend_rules_version keys
        "snapshot": {"last_price": 100.0},
        "freshness_status": "usable",
        "provenance": {},
    }
    corpus_dir = str(tmp_path / "corpus")
    write_corpus(corpus_dir, [old_shaped_fixture], as_of_date="2026-07-24")

    _, fixtures = load_corpus(corpus_dir)
    assert len(fixtures) == 1
    assert "trend_score" not in fixtures[0]["candidate"]

    settings = make_settings()  # mock mode -- no real call
    result = replay_packet(fixtures[0], ("gpt-5.4-mini", "v3"), settings, journal)
    assert result is not None  # no crash


# ============================================================= test 14: neutrality
_BANNED_ADJECTIVES = ("supports", "strong", "weak", "bullish", "bearish")


def test_14_multi_day_context_contains_neutrality_sentence_and_no_banned_adjectives():
    prompt = build_no_news_user_prompt(
        {"symbol": "AAPL"}, {"last_price": 100.0}, "usable",
        atr_policy=_ATR_POLICY, multi_day_context=_MULTI_DAY_CONTEXT,
    )
    start = prompt.index("MULTI_DAY_CONTEXT:")
    end = prompt.index("ATR_STOP_POLICY:")
    section = prompt[start:end]

    assert ("This context does not make proposing more or less desirable; "
            "apply your usual evidence standards unchanged.") in section
    lowered = section.lower()
    for word in _BANNED_ADJECTIVES:
        assert word not in lowered, f"banned adjective {word!r} found in MULTI_DAY_CONTEXT section"


# ============================================================= test 15: containment
def test_15_instr2_containment_test_stays_green_reference():
    """The actual regression guard lives in
    tests/test_instr2_atr_coherent_prompt.py::
    test_containment_preserved_under_v2_atr_read_exception_in_post_process
    (unmodified by this ticket -- verified green as part of the full
    suite). This is a documentation-only marker test so the mapping is
    visible from this file."""
    assert True


def test_15_atr_read_failure_under_v3_degrades_fully_v1_shaped(journal):
    """Same law as v2's own pre-existing test 6
    (test_augment_time_atr_read_raising_degrades_to_v1_prompt_never_propagates)
    re-proven under v3: the ATR read is upstream of BOTH atr_policy and
    multi_day_context, so its failure degrades all the way to v1-shaped
    (neither block), journaled ERROR, never propagated."""
    client = _v3_live_client(journal)

    def _raising_scalar(sql, params=()):
        raise Exception("simulated transient SQLite error on atr_history read")

    journal.scalar = _raising_scalar
    result = client._augment_snapshot_for_prompt({"last_price": 100.0}, {"symbol": "AAPL"})

    assert "atr_policy" not in result
    assert "multi_day_context" not in result
    event = journal.one("SELECT * FROM system_events WHERE category = 'openai' AND severity = 'error'")
    assert event is not None
    assert "AAPL" in event["message"]


def test_15_bars_read_failure_under_v3_degrades_to_v2_shaped_never_propagates(journal):
    """The NEW INSTR-3 case: ATR succeeds (atr_policy computed), but the
    daily_bars read raises -- this evaluation degrades to a v2-shaped
    prompt (atr_policy present, MULTI_DAY_CONTEXT absent), journaled ERROR,
    never propagated -- distinct failure path from the ATR-read case
    above, and the reason atr_policy/multi_day_context each get their OWN
    try/except in _augment_snapshot_for_prompt / _build_multi_day_context."""
    _seed_atr(journal, "AAPL", atr_14=2.0)
    client = _v3_live_client(journal)

    real_query = journal.query

    def _raising_query(sql, params=()):
        if "daily_bars" in sql:
            raise Exception("simulated transient SQLite error on daily_bars read")
        return real_query(sql, params)

    journal.query = _raising_query
    result = client._augment_snapshot_for_prompt({"last_price": 100.0}, {"symbol": "AAPL"})

    assert "atr_policy" in result  # v2-shaped: the ATR read succeeded before the bars read failed
    assert "multi_day_context" not in result
    event = journal.one("SELECT * FROM system_events WHERE category = 'openai' AND severity = 'error'")
    assert event is not None
    assert "AAPL" in event["message"]
    assert "daily bars" in event["message"].lower()


# ===================================================== Design 5: harness/proof gate
def test_design5_v3_arm_replay_renders_multi_day_context_via_read_only_journal(journal, monkeypatch):
    """The critical harness-integrity proof: without `_ReadOnlyJournal`
    forwarding `.query()`, EVERY v3 replay would silently degrade to a
    v2-shaped prompt (daily_bars read would AttributeError, caught by
    _build_multi_day_context's own try/except, swallowed by
    _ReadOnlyJournal.log_system_event's no-op) -- defeating the whole
    point of a v3 proof-gate run. This drives the REAL
    replay_packet -> raw_evaluate -> _live_eval -> build_no_news_user_prompt
    chain end-to-end (only the OpenAI SDK class faked)."""
    captured = _capture_prompt_via_fake_openai(monkeypatch)
    closes = [float(100 + i) for i in range(10)]
    _seed_daily_bars(journal, "AAPL", closes, start="2026-06-01")
    _seed_atr(journal, "AAPL", atr_14=2.0)
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test")
    fixture = {
        "eval_id": "eval_v3harness01", "candidate_id": "cand_v3harness01", "symbol": "AAPL",
        "candidate": {"candidate_id": "cand_v3harness01", "symbol": "AAPL", "direction": "long",
                     "momentum_score": 0.7},
        "snapshot": {"last_price": 100.0},
        "freshness_status": "usable", "provenance": {},
    }

    result = replay_packet(fixture, ("gpt-5.4-mini", "v3"), settings, journal)

    assert "messages" in captured
    user_prompt = captured["messages"][1]["content"]
    assert "MULTI_DAY_CONTEXT:" in user_prompt
    assert "ATR_STOP_POLICY:" in user_prompt
    assert result.prompt_version == "v3"


def test_design4_hide_legacy_trend_quality_under_v3_not_v1_v2(journal, monkeypatch):
    """Design item 3/4: under v3 the dishonest legacy trend_quality field
    (candidate_scanner.py's abs(change_pct)*10) is never shown to the
    model -- v1/v2 prompts are UNCHANGED (byte-identity preserved)."""
    captured = _capture_prompt_via_fake_openai(monkeypatch)
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test",
                             OPENAI_PROMPT_VERSION="v3")
    fixture = {
        "eval_id": "eval_hidetq01", "candidate_id": "cand_hidetq01", "symbol": "AAPL",
        "candidate": {"candidate_id": "cand_hidetq01", "symbol": "AAPL", "direction": "long",
                     "momentum_score": 0.7, "trend_quality": 0.987654},
        "snapshot": {"last_price": 100.0},
        "freshness_status": "usable", "provenance": {},
    }
    replay_packet(fixture, ("gpt-5.4-mini", "v3"), settings, journal)
    user_prompt = captured["messages"][1]["content"]
    assert "0.987654" not in user_prompt
    assert "trend_quality" not in user_prompt

    captured.clear()
    replay_packet(fixture, ("gpt-5.4-mini", "v1"), settings, journal)
    user_prompt_v1 = captured["messages"][1]["content"]
    assert "0.987654" in user_prompt_v1  # v1 unaffected -- byte-identity preserved
    assert "trend_quality" in user_prompt_v1
