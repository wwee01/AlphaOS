"""EXP-0: shadow-tier deterministic universe capture (§H.1 direct
construction throughout -- no wall-clock dependence, no reliance on a
natural mock scan's organic candidate mix).

Covers:
* the universe builder's screen (ADV/price band, ETF exclusion, unscreenable
  symbols skipped not zero-faked, deterministic overflow selection),
* the committed file writer/reader (versioning, sha256, missing/corrupt file),
* the scanner's batch-snapshot shadow-tier pass (tier/shadow_tier/instrument_
  version stamping, per-symbol outcome tracking regardless of candidate),
* the orchestrator's structural isolation (shadow-tier candidates never reach
  AI evaluation or proposal creation -- both a behavior probe AND a grep/
  source-inspection guard-presence check, matching this codebase's existing
  test_attribution_flow.py pattern),
* universe_days survivorship journaling (append-only, idempotent per day),
* the digest's shadow_tier section (always-present keys, correct counts),
* the batch Alpaca snapshot endpoint (chunking + per-symbol fallback),
* the CLI command's graceful no-live-data path.

All offline, in-memory, mock mode. No real money, no network.
"""

from __future__ import annotations

import inspect
import json
import sqlite3

import pytest

import alphaos.data.providers.alpaca_data as alpaca_data_module
from alphaos.config.settings import load_settings
from alphaos.constants import UniverseTier
from alphaos.data.providers.alpaca_assets import is_probable_etf
from alphaos.data.providers.alpaca_data import AlpacaDataProvider
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.scanner.candidate_scanner import CandidateScanner, CURRENT_INSTRUMENT_VERSION
from alphaos.scheduler.digest import build_daily_digest
from alphaos.universe.builder import (
    build_shadow_universe,
    load_universe_file,
    write_universe_file,
)
from conftest import make_settings


# --------------------------------------------------------------------- fakes
class _FakeAssetsProvider:
    def __init__(self, assets):
        self._assets = assets

    def get_tradable_us_equities(self):
        return self._assets


class _FakeBarsProvider:
    """Mirrors test_benchmark_capture.py's _FakeBarsProvider -- per-symbol
    canned bar lists, no network."""

    def __init__(self, by_symbol):
        self._by_symbol = by_symbol

    def get_daily_bars(self, symbol, start, end, limit=200):
        return self._by_symbol.get(symbol, [])


def _bars(close, volume, days=20):
    return [{"date": f"2026-06-{d:02d}", "close": close, "volume": volume} for d in range(1, days + 1)]


def _universe_doc(symbols, version=1):
    return {
        "version": version, "as_of_date": "2026-07-01", "sha256": "test",
        "screen_params": {}, "symbols": symbols,
    }


# ------------------------------------------------------------- builder screen
def test_builder_screens_by_adv_and_price_band():
    settings = make_settings()
    assets = _FakeAssetsProvider([
        {"symbol": "INBAND", "name": "In Band Inc", "exchange": "NYSE", "is_probable_etf": False},
        {"symbol": "TOOBIG", "name": "Too Big Inc", "exchange": "NYSE", "is_probable_etf": False},
        {"symbol": "TOOCHEAP", "name": "Too Cheap Inc", "exchange": "NASDAQ", "is_probable_etf": False},
    ])
    bars = _FakeBarsProvider({
        "INBAND": _bars(close=20.0, volume=1_000_000),      # ADV=$20M, price=$20 -- in band
        "TOOBIG": _bars(close=50.0, volume=10_000_000),     # ADV=$500M -- above the $50M ceiling
        "TOOCHEAP": _bars(close=1.0, volume=1_000_000),     # ADV=$1M -- below the $5M floor
    })
    result = build_shadow_universe(settings, assets_provider=assets, bars_provider=bars)
    assert [s["symbol"] for s in result["symbols"]] == ["INBAND"]
    assert result["screened"] == 3 and result["passed"] == 1
    reasons = {s["symbol"]: s["reason"] for s in result["skipped"]}
    assert reasons["TOOBIG"] == "adv_out_of_band"
    assert reasons["TOOCHEAP"] == "adv_out_of_band"


def test_builder_excludes_probable_etfs():
    settings = make_settings()
    assets = _FakeAssetsProvider([
        {"symbol": "REALCO", "name": "Real Company Inc", "exchange": "NYSE", "is_probable_etf": False},
        {"symbol": "FAKEETF", "name": "Direxion Daily Fake Bull 3X ETF", "exchange": "NYSE", "is_probable_etf": True},
    ])
    bars = _FakeBarsProvider({
        "REALCO": _bars(close=20.0, volume=1_000_000),
        "FAKEETF": _bars(close=20.0, volume=1_000_000),
    })
    result = build_shadow_universe(settings, assets_provider=assets, bars_provider=bars)
    assert [s["symbol"] for s in result["symbols"]] == ["REALCO"]
    assert {"symbol": "FAKEETF", "reason": "probable_etf"} in result["skipped"]


def test_builder_skips_unfetchable_symbols_never_fakes_a_zero():
    """A symbol whose bars call comes back empty (network error OR genuinely
    no data) must be SKIPPED with a reason, never silently included with a
    fabricated ADV of 0 or excluded for the wrong (ADV/price) reason --
    unknown != safe, the same law as the freshness guard."""
    settings = make_settings()
    assets = _FakeAssetsProvider([
        {"symbol": "NODATA", "name": "No Data Inc", "exchange": "NYSE", "is_probable_etf": False},
    ])
    bars = _FakeBarsProvider({})  # NODATA intentionally absent -> get_daily_bars returns []
    result = build_shadow_universe(settings, assets_provider=assets, bars_provider=bars)
    assert result["symbols"] == []
    assert result["skipped"] == [{"symbol": "NODATA", "reason": "no_bars_data"}]


def test_builder_recent_ipo_flag_from_bar_count():
    settings = make_settings()
    assets = _FakeAssetsProvider([
        {"symbol": "NEWCO", "name": "New Co", "exchange": "NYSE", "is_probable_etf": False},
        {"symbol": "OLDCO", "name": "Old Co", "exchange": "NYSE", "is_probable_etf": False},
    ])
    bars = _FakeBarsProvider({
        "NEWCO": _bars(close=20.0, volume=1_000_000, days=30),   # short history -> recent_ipo
        "OLDCO": _bars(close=20.0, volume=1_000_000, days=250),  # full year -> not recent_ipo
    })
    result = build_shadow_universe(settings, assets_provider=assets, bars_provider=bars)
    by_symbol = {s["symbol"]: s for s in result["symbols"]}
    assert by_symbol["NEWCO"]["recent_ipo"] is True
    assert by_symbol["OLDCO"]["recent_ipo"] is False


def test_builder_deterministic_overflow_selection():
    """More passing symbols than shadow_tier_max_count -- keep the most
    liquid (highest ADV) up to the cap, tie-broken alphabetically; never an
    arbitrary/unstable ordering."""
    settings = make_settings(SHADOW_TIER_MAX_COUNT=2, SHADOW_TIER_TARGET_COUNT=1)
    assets = _FakeAssetsProvider([
        {"symbol": "LOW", "name": "Low Inc", "exchange": "NYSE", "is_probable_etf": False},
        {"symbol": "MID", "name": "Mid Inc", "exchange": "NYSE", "is_probable_etf": False},
        {"symbol": "HIGH", "name": "High Inc", "exchange": "NYSE", "is_probable_etf": False},
    ])
    bars = _FakeBarsProvider({
        "LOW": _bars(close=10.0, volume=1_000_000),   # ADV=$10M
        "MID": _bars(close=10.0, volume=2_000_000),   # ADV=$20M
        "HIGH": _bars(close=10.0, volume=3_000_000),  # ADV=$30M
    })
    result = build_shadow_universe(settings, assets_provider=assets, bars_provider=bars)
    assert [s["symbol"] for s in result["symbols"]] == ["HIGH", "MID"]
    assert {"symbol": "LOW", "reason": "max_count_cap"} in result["skipped"]


def test_builder_mock_mode_returns_empty_screen_not_a_crash():
    settings = make_settings()  # ALPHAOS_MODE=mock by default
    result = build_shadow_universe(settings)  # no providers injected -> make_* factories return None
    assert result == {
        "as_of_date": result["as_of_date"], "screen_params": result["screen_params"],
        "symbols": [], "screened": 0, "passed": 0, "skipped": [],
    }


# ------------------------------------------------------- is_probable_etf heuristic
@pytest.mark.parametrize("name,expected", [
    ("ProShares UltraPro QQQ", True),
    ("Direxion Daily Small Cap Bull 3X Shares", True),
    ("iShares Core S&P 500 ETF", True),
    ("Vanguard Total Stock Market Fund", True),
    ("Apple Inc. Common Stock", False),
    ("Acme Widget Corp", False),
])
def test_is_probable_etf_heuristic(name, expected):
    assert is_probable_etf(name) is expected


# --------------------------------------------------------------- file writer
def test_write_universe_file_versions_and_hashes(tmp_path):
    path = str(tmp_path / "shadow_universe.json")
    payload = {"as_of_date": "2026-07-01", "screen_params": {}, "symbols": [{"symbol": "A"}],
               "screened": 1, "passed": 1, "skipped": []}

    doc1 = write_universe_file(payload, path)
    assert doc1["version"] == 1
    assert len(doc1["sha256"]) == 64

    doc2 = write_universe_file(payload, path)
    assert doc2["version"] == 2  # rebuild increments, even with identical content

    loaded = load_universe_file(path)
    assert loaded["version"] == 2
    assert loaded["symbols"] == [{"symbol": "A"}]


def test_load_universe_file_missing_returns_none(tmp_path):
    assert load_universe_file(str(tmp_path / "nope.json")) is None


def test_load_universe_file_corrupt_returns_none(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("{not valid json")
    assert load_universe_file(str(path)) is None


# ------------------------------------------------------------ batch snapshots
class _FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_alpaca_batch_snapshots_maps_response_and_chunks(monkeypatch):
    # has_alpaca_keys requires real-shaped creds regardless of mock mode's
    # scan-time provider choice -- constructing AlpacaDataProvider directly
    # here to unit-test its batch method in isolation from MarketDataClient's
    # mock/live selection.
    settings_with_keys = make_settings(ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s")
    provider = AlpacaDataProvider(settings_with_keys)

    calls = []

    def fake_urlopen(req, timeout=10):
        calls.append(req.full_url)
        # Simulate Alpaca's real batch shape: {"SYM": {...same as single-snapshot...}}
        chunk_symbols = req.full_url.split("symbols=")[1].split("&")[0].split(",")
        payload = {
            sym: {"latestTrade": {"p": 42.0}, "latestQuote": {"bp": 41.9, "ap": 42.1, "t": "2026-07-08T00:00:00Z"},
                  "dailyBar": {"o": 40, "h": 43, "l": 39, "c": 42.0, "v": 1_000_000},
                  "prevDailyBar": {"c": 41.0, "v": 900_000}}
            for sym in chunk_symbols if sym != "MISSING"
        }
        return _FakeHTTPResponse(payload)

    monkeypatch.setattr(alpaca_data_module.urllib.request, "urlopen", fake_urlopen)

    symbols = [f"SYM{i}" for i in range(150)] + ["MISSING"]  # forces 2 chunks (_BATCH_SIZE=100) + a gap
    results = provider.get_snapshots(symbols)

    assert len(results) == len(symbols)  # every requested symbol gets an entry back
    assert len(calls) == 2  # 151 symbols / 100 per call -> 2 HTTP calls
    assert results[0]["last_price"] == 42.0
    assert results[0]["provider"] == "alpaca"
    missing_result = results[symbols.index("MISSING")]
    assert missing_result["last_price"] is None  # absent from the response -> _empty, never fabricated


def test_alpaca_batch_snapshots_no_creds_returns_empty_for_every_symbol():
    settings = make_settings()  # no ALPACA_API_KEY/SECRET set
    provider = AlpacaDataProvider(settings)
    results = provider.get_snapshots(["A", "B", "C"])
    assert [r["last_price"] for r in results] == [None, None, None]
    assert len(results) == 3


# ------------------------------------------------------------ scanner shadow pass
def test_scan_shadow_tier_stamps_tier_and_shadow_flag(journal):
    settings = make_settings()
    scanner = CandidateScanner(settings, journal)
    result = scanner.scan_shadow_tier(["ZZZZ1", "ZZZZ2"], scan_batch_id="batch1", universe_file_version=3)

    universe_rows = journal.query("SELECT * FROM universe WHERE scan_id = 'batch1'")
    assert len(universe_rows) == 2
    assert all(r["tier"] == UniverseTier.WATCHLIST.value for r in universe_rows)
    assert all(r["universe_file_version"] == 3 for r in universe_rows)

    for sym in ("ZZZZ1", "ZZZZ2"):
        assert sym in result.per_symbol
        assert "freshness_status" in result.per_symbol[sym]

    for cand in result.candidates:
        assert cand["shadow_tier"] == 1
        assert cand["instrument_version"] == CURRENT_INSTRUMENT_VERSION


def test_core_scan_candidates_are_never_shadow_tagged(journal):
    """Regression guard: the core scan() path's default params must keep
    producing shadow_tier=0/instrument_version=None -- EXP-0 must not have
    changed core-tier candidate shape."""
    settings = make_settings()
    scanner = CandidateScanner(settings, journal)
    result = scanner.scan()
    assert result.candidates, "expected at least one core-tier candidate from the default universe"
    for cand in result.candidates:
        assert cand["shadow_tier"] == 0
        assert cand["instrument_version"] is None
    assert result.per_symbol == {}  # per_symbol is a shadow-tier-only field


def test_scan_shadow_tier_per_symbol_populated_even_without_a_candidate(journal):
    """The survivorship law starts here: EVERY requested symbol gets a
    per_symbol entry, whether or not it became a candidate."""
    settings = make_settings()
    scanner = CandidateScanner(settings, journal)
    result = scanner.scan_shadow_tier(["QQQQ1", "QQQQ2", "QQQQ3", "QQQQ4"])
    assert set(result.per_symbol.keys()) == {"QQQQ1", "QQQQ2", "QQQQ3", "QQQQ4"}
    candidate_syms = {c["symbol"] for c in result.candidates}
    non_candidate_syms = set(result.per_symbol) - candidate_syms
    for sym in non_candidate_syms:
        assert result.per_symbol[sym]["candidate_id"] is None


# --------------------------------------------------------- orchestrator wiring
def test_shadow_tier_disabled_by_default_zero_side_effects(orchestrator):
    """Default settings (SHADOW_TIER_ENABLED unset) -- the whole EXP-0 block
    must cost nothing: no shadow scan, no universe_days rows, summary fields
    all zero/None."""
    assert orchestrator.settings.shadow_tier_enabled is False
    summary = orchestrator.run_scan_once()
    assert summary.shadow_tier_scanned == 0
    assert summary.shadow_tier_candidates == 0
    assert summary.shadow_tier_feed_coverage is None
    assert orchestrator.journal.count_rows("universe_days") == 0


def test_shadow_tier_enabled_without_universe_file_warns_and_does_not_crash(tmp_path):
    settings = make_settings(SHADOW_TIER_ENABLED="true",
                             SHADOW_TIER_UNIVERSE_FILE=str(tmp_path / "does_not_exist.json"))
    journal = JournalStore(":memory:")
    orch = Orchestrator(settings=settings, journal=journal)
    summary = orch.run_scan_once()  # must not raise
    assert summary.shadow_tier_scanned == 0
    warnings = journal.query(
        "SELECT * FROM system_events WHERE category = 'scanner' AND message LIKE '%SHADOW_TIER_ENABLED%'"
    )
    assert warnings
    journal.close()


def _orch_with_shadow_universe(tmp_path, symbols, **extra_env):
    path = str(tmp_path / "shadow_universe.json")
    with open(path, "w") as f:
        json.dump(_universe_doc(symbols), f)
    settings = make_settings(SHADOW_TIER_ENABLED="true", SHADOW_TIER_UNIVERSE_FILE=path, **extra_env)
    journal = JournalStore(":memory:")
    return Orchestrator(settings=settings, journal=journal), path


def test_shadow_candidates_never_reach_ai_evaluation_or_proposals(tmp_path):
    """Behavior probe (not just testimony): run a real scan with shadow tier
    enabled + labelling on, then directly query openai_evaluations/
    trade_proposals for any shadow-tagged candidate_id."""
    orch, _ = _orch_with_shadow_universe(
        tmp_path, [{"symbol": "WWWW1"}, {"symbol": "WWWW2"}], LABELLING_ENABLED="true",
    )
    orch.run_scan_once()

    shadow_ids = [r["candidate_id"] for r in orch.journal.query(
        "SELECT candidate_id FROM candidates WHERE shadow_tier = 1"
    )]
    assert shadow_ids, "expected at least one shadow-tier candidate to test isolation against"

    placeholders = ",".join("?" * len(shadow_ids))
    evals = orch.journal.query(
        f"SELECT * FROM openai_evaluations WHERE candidate_id IN ({placeholders})", shadow_ids
    )
    proposals = orch.journal.query(
        f"SELECT * FROM trade_proposals WHERE candidate_id IN ({placeholders})", shadow_ids
    )
    assert evals == []
    assert proposals == []
    orch.journal.close()


def test_shadow_tier_guard_present_at_ai_evaluation_chokepoint():
    """Source-inspection guard-presence check (matches this codebase's own
    test_attribution_flow.py::test_decision_functions_never_reference_
    attribution pattern) -- confirms the structural backstop actually exists
    in the code, not just that today's test data happens not to trigger it."""
    source = inspect.getsource(Orchestrator.run_scan_once)
    assert "shadow_tier" in source
    assert "RuntimeError" in source

    handle_proposal_source = inspect.getsource(Orchestrator._handle_proposal)
    assert "shadow_tier" in handle_proposal_source
    assert "RuntimeError" in handle_proposal_source


def test_universe_days_written_once_per_symbol_per_day_idempotent(tmp_path):
    orch, _ = _orch_with_shadow_universe(tmp_path, [{"symbol": "VVVV1", "recent_ipo": True}])
    orch.run_scan_once()
    orch.run_scan_once()  # same market day -- must not duplicate

    rows = orch.journal.query("SELECT * FROM universe_days WHERE symbol = 'VVVV1'")
    assert len(rows) == 1
    assert rows[0]["tier"] == UniverseTier.WATCHLIST.value
    assert rows[0]["recent_ipo"] == 1
    assert rows[0]["instrument_version"] == CURRENT_INSTRUMENT_VERSION
    orch.journal.close()


def test_universe_days_append_only_enforced_at_db_level(journal):
    """Direct duplicate insert (bypassing the ORM-ish insert() convenience
    method's own dedup path) must still fail at the DB level -- the unique
    index is the real backstop, not application discipline alone."""
    journal.insert("universe_days", {
        "universe_day_id": "ud1", "market_date": "2026-07-08", "symbol": "AAAA", "tier": "watchlist",
    })
    with pytest.raises(sqlite3.IntegrityError):
        journal.insert("universe_days", {
            "universe_day_id": "ud2", "market_date": "2026-07-08", "symbol": "AAAA", "tier": "watchlist",
        })


def test_universe_days_records_every_symbol_including_non_candidates(tmp_path):
    """The survivorship law end-to-end: a symbol that scans fresh but never
    becomes a candidate still gets a universe_days row."""
    orch, _ = _orch_with_shadow_universe(
        tmp_path, [{"symbol": f"NOCAND{i}"} for i in range(5)]
    )
    orch.run_scan_once()
    rows = orch.journal.query("SELECT symbol, candidate_found FROM universe_days")
    assert len(rows) == 5  # every requested symbol recorded, candidate or not


# --------------------------------------------------------------------- digest
def test_digest_shadow_tier_keys_always_present_when_disabled(orchestrator):
    digest = build_daily_digest(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    assert digest["shadow_tier"] == {
        "enabled": False, "scanned_today": 0, "fresh_today": 0, "stale_today": 0,
        "candidates_today": 0, "top_decile_interest_count_today": 0, "feed_coverage_today": None,
    }


def test_digest_shadow_tier_counts_match_universe_days(tmp_path):
    orch, _ = _orch_with_shadow_universe(tmp_path, [{"symbol": "DDDD1"}, {"symbol": "DDDD2"}])
    orch.run_scan_once()
    digest = build_daily_digest(orch.journal, orch.settings, orch.kill_switch)
    assert digest["shadow_tier"]["scanned_today"] == 2
    assert digest["shadow_tier"]["enabled"] is True
    assert digest["shadow_tier"]["feed_coverage_today"] is not None
    orch.journal.close()


# ------------------------------------------------------------------------ CLI
def test_cli_universe_build_mock_mode_exits_nonzero_and_writes_nothing(tmp_path, monkeypatch):
    import os

    from alphaos import __main__ as cli

    universe_path = str(tmp_path / "shadow_universe.json")
    env = {
        "ALPHAOS_MODE": "mock", "APPROVAL_MODE": "manual", "REAL_TRADING_ENABLED": "false",
        "ALPHAOS_DB_PATH": str(tmp_path / "cli.db"), "SHADOW_TIER_UNIVERSE_FILE": universe_path,
    }
    monkeypatch.setattr(cli, "load_settings", lambda: load_settings(load_env_file=False, env=env))

    assert cli.main(["universe_build"]) == 1
    assert not os.path.exists(universe_path)
