"""Benchmark spine capture (PR9.5): the write side of measuring performance
vs the S&P 500. Hermetic -- mock mode / injected fakes, never real network.
Covers: idempotent equity capture, live-vs-static equity source selection +
fail-safe fallback, SPY bar backfill idempotency + gap-filling, and that
nothing here ever raises regardless of failure mode.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

import alphaos.reports.benchmark_capture as benchmark_capture_module
from alphaos.journal.journal_store import JournalStore
from alphaos.reports.benchmark_capture import (
    BENCHMARK_SYMBOL,
    capture_benchmark_spine,
)
from conftest import make_settings


class _FakeAlpacaClient:
    def __init__(self, equity=123456.78, raises=None):
        self._equity = equity
        self._raises = raises

    def get_account(self):
        if self._raises:
            raise self._raises
        return {"equity": self._equity}


class _FakeBarsProvider:
    def __init__(self, bars=None):
        self._bars = bars if bars is not None else []
        self.calls = []

    def get_daily_bars(self, symbol, start, end, limit=200):
        self.calls.append((symbol, start, end))
        return self._bars


class _PagingBarsProvider:
    """Simulates what the plain _FakeBarsProvider does not: a real paginated
    API only ever returns up to `limit` bars per call, oldest-first, from
    within [start, end]. This is the exact shape that silently truncated a
    gap bigger than one page before _backfill_benchmark_bars learned to page."""

    def __init__(self, all_bars):
        self._all_bars = sorted(all_bars, key=lambda b: b["date"])
        self.calls = []

    def get_daily_bars(self, symbol, start, end, limit=200):
        self.calls.append((symbol, start, end, limit))
        in_range = [b for b in self._all_bars if start <= b["date"] <= end]
        return in_range[:limit]


def _bar(d, close, open_=None, high=None, low=None, volume=1_000_000):
    return {
        "date": d, "open": open_ if open_ is not None else close,
        "high": high if high is not None else close, "low": low if low is not None else close,
        "close": close, "volume": volume,
    }


@pytest.fixture
def journal():
    j = JournalStore(":memory:")
    yield j
    j.close()


# --------------------------------------------------------------- equity: mock
def test_mock_mode_always_uses_static_config_equity(journal):
    settings = make_settings()  # mock mode by default
    result = capture_benchmark_spine(journal, settings, alpaca_client=_FakeAlpacaClient())

    assert result["equity_snapshot"]["source"] == "static_config"
    assert result["equity_snapshot"]["equity"] == settings.paper_equity
    row = journal.one("SELECT * FROM equity_snapshots")
    assert row["equity_source"] == "static_config"
    assert row["is_mock"] == 1


# --------------------------------------------------------------- equity: live
def test_live_mode_uses_broker_equity_when_available(journal):
    settings = make_settings(
        ALPHAOS_MODE="paper", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s",
        ALPACA_PAPER="true", ALPACA_BASE_URL="https://paper-api.alpaca.markets",
        EXECUTION_PROVIDER="simulated_internal",
    )
    fake_client = _FakeAlpacaClient(equity=987654.32)

    result = capture_benchmark_spine(journal, settings, alpaca_client=fake_client)

    assert result["equity_snapshot"]["source"] == "live_broker"
    assert result["equity_snapshot"]["equity"] == 987654.32
    row = journal.one("SELECT * FROM equity_snapshots")
    assert row["equity_source"] == "live_broker"
    assert row["is_mock"] == 0


def test_live_mode_falls_back_to_static_config_on_broker_failure(journal):
    settings = make_settings(
        ALPHAOS_MODE="paper", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s",
        ALPACA_PAPER="true", ALPACA_BASE_URL="https://paper-api.alpaca.markets",
        EXECUTION_PROVIDER="simulated_internal",
    )
    fake_client = _FakeAlpacaClient(raises=RuntimeError("network down"))

    result = capture_benchmark_spine(journal, settings, alpaca_client=fake_client)

    assert result["equity_snapshot"]["source"] == "static_config"
    assert result["equity_snapshot"]["equity"] == settings.paper_equity
    warning = journal.one(
        "SELECT * FROM system_events WHERE category = 'benchmark_spine' AND severity = 'warning'"
    )
    assert warning is not None
    assert "network down" in warning["message"]


def test_live_mode_without_alpaca_keys_uses_static_config_no_client_call(journal):
    """No keys -> never even attempt a broker call (the _capture_equity guard
    itself), regardless of what a client would return."""
    settings = make_settings(ALPHAOS_MODE="paper", EXECUTION_PROVIDER="simulated_internal")
    fake_client = _FakeAlpacaClient(equity=1.0)  # would prove it if ever called

    result = capture_benchmark_spine(journal, settings, alpaca_client=fake_client)

    assert result["equity_snapshot"]["source"] == "static_config"


# --------------------------------------------------------- equity: idempotent
def test_equity_capture_is_idempotent_same_market_date(journal):
    settings = make_settings()
    fake_client = _FakeAlpacaClient(equity=100.0)

    r1 = capture_benchmark_spine(journal, settings, alpaca_client=fake_client)
    r2 = capture_benchmark_spine(journal, settings, alpaca_client=fake_client)

    assert r1["equity_snapshot"] is not None       # first call captures
    assert r2["equity_snapshot"] is None           # second call same day: no-op
    assert journal.count_rows("equity_snapshots") == 1


def test_equity_capture_never_raises_even_if_journal_insert_fails(journal, monkeypatch):
    settings = make_settings()

    def boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(journal, "insert", boom)
    result = capture_benchmark_spine(journal, settings)  # must not raise

    assert result["equity_snapshot"] is None
    assert any("equity snapshot failed" in w for w in result["warnings"])


# ------------------------------------------------------------- bars: backfill
def test_bars_backfill_writes_all_returned_bars_on_first_run(journal):
    settings = make_settings(ALPHAOS_MODE="paper", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s",
                            ALPACA_PAPER="true", ALPACA_BASE_URL="https://paper-api.alpaca.markets",
                            EXECUTION_PROVIDER="simulated_internal")
    bars = [_bar("2026-07-01", 500.0), _bar("2026-07-02", 502.0), _bar("2026-07-03", 501.0)]
    provider = _FakeBarsProvider(bars)

    result = capture_benchmark_spine(
        journal, settings, now=None, alpaca_client=_FakeAlpacaClient(), bars_provider=provider,
    )

    assert result["benchmark_bars_written"] == 3
    rows = journal.query("SELECT * FROM benchmark_bars ORDER BY bar_date")
    assert [r["bar_date"] for r in rows] == ["2026-07-01", "2026-07-02", "2026-07-03"]
    assert rows[0]["symbol"] == BENCHMARK_SYMBOL
    assert rows[0]["close"] == 500.0


def test_bars_backfill_only_fetches_the_gap_since_last_cached_date(journal):
    settings = make_settings(ALPHAOS_MODE="paper", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s",
                            ALPACA_PAPER="true", ALPACA_BASE_URL="https://paper-api.alpaca.markets",
                            EXECUTION_PROVIDER="simulated_internal")
    from alphaos.util.ids import new_id
    journal.insert("benchmark_bars", {
        "bar_id": new_id("bar"), "symbol": "SPY", "bar_date": "2026-07-03", "close": 501.0,
    })
    provider = _FakeBarsProvider([_bar("2026-07-04", 505.0)])

    capture_benchmark_spine(journal, settings, alpaca_client=_FakeAlpacaClient(), bars_provider=provider)

    assert provider.calls[0][1] == "2026-07-04"  # start = last cached date + 1, not the 90d lookback


def test_bars_backfill_no_op_when_already_up_to_date(journal):
    settings = make_settings(ALPHAOS_MODE="paper", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s",
                            ALPACA_PAPER="true", ALPACA_BASE_URL="https://paper-api.alpaca.markets",
                            EXECUTION_PROVIDER="simulated_internal")
    from alphaos.util.ids import new_id
    today = date.today().isoformat()
    journal.insert("benchmark_bars", {"bar_id": new_id("bar"), "symbol": "SPY", "bar_date": today, "close": 500.0})
    provider = _FakeBarsProvider([_bar("2099-01-01", 999.0)])  # would prove it if ever called

    result = capture_benchmark_spine(journal, settings, alpaca_client=_FakeAlpacaClient(), bars_provider=provider)

    assert result["benchmark_bars_written"] == 0
    assert provider.calls == []


def test_bars_backfill_is_rerun_safe_via_the_unique_index(journal):
    """Belt: the last-cached-date pre-check should already prevent overlap.
    Suspenders: even if it somehow tried to re-insert an existing (symbol,
    date), the partial-unique-index-equivalent (a plain UNIQUE here, since
    symbol/bar_date are never NULL) must swallow the IntegrityError, not
    crash the caller."""
    settings = make_settings(ALPHAOS_MODE="paper", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s",
                            ALPACA_PAPER="true", ALPACA_BASE_URL="https://paper-api.alpaca.markets",
                            EXECUTION_PROVIDER="simulated_internal")
    from alphaos.util.ids import new_id
    journal.insert("benchmark_bars", {"bar_id": new_id("bar"), "symbol": "SPY", "bar_date": "2026-07-05", "close": 1.0})
    # Force the provider to return an OVERLAPPING date (simulating a race/rerun).
    provider = _FakeBarsProvider([_bar("2026-07-05", 2.0), _bar("2026-07-06", 3.0)])

    result = capture_benchmark_spine(journal, settings, alpaca_client=_FakeAlpacaClient(), bars_provider=provider)

    assert result["benchmark_bars_written"] == 1  # only the genuinely new date
    assert journal.count_rows("benchmark_bars", "bar_date = '2026-07-05'") == 1  # not duplicated


def test_bars_backfill_mock_mode_is_a_clean_noop(journal):
    settings = make_settings()  # mock mode -- make_bars_provider returns None

    result = capture_benchmark_spine(journal, settings, alpaca_client=_FakeAlpacaClient())

    assert result["benchmark_bars_written"] == 0
    assert result["warnings"] == []


def test_bars_backfill_never_raises_on_provider_error(journal):
    settings = make_settings(ALPHAOS_MODE="paper", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s",
                            ALPACA_PAPER="true", ALPACA_BASE_URL="https://paper-api.alpaca.markets",
                            EXECUTION_PROVIDER="simulated_internal")

    class BoomProvider:
        def get_daily_bars(self, *a, **k):
            raise RuntimeError("provider exploded")

    result = capture_benchmark_spine(
        journal, settings, alpaca_client=_FakeAlpacaClient(), bars_provider=BoomProvider(),
    )

    assert result["benchmark_bars_written"] == 0
    assert any("benchmark bars backfill failed" in w for w in result["warnings"])
    warning = journal.one(
        "SELECT * FROM system_events WHERE category = 'benchmark_spine' AND message LIKE '%provider exploded%'"
    )
    assert warning is not None


# ------------------------------------------------------- bars: pagination
def test_bars_backfill_pages_through_a_gap_larger_than_one_page(journal, monkeypatch):
    """A bare get_daily_bars call truncates at its own limit -- this is the
    exact MEDIUM finding from the PR9.5 audit. 7 bars over a 3-bar page size
    forces 3 pages; all 7 must still land in a single capture_benchmark_spine
    call, not dribble in over several days' worth of reruns."""
    monkeypatch.setattr(benchmark_capture_module, "_BARS_PAGE_SIZE", 3)
    settings = make_settings(ALPHAOS_MODE="paper", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s",
                            ALPACA_PAPER="true", ALPACA_BASE_URL="https://paper-api.alpaca.markets",
                            EXECUTION_PROVIDER="simulated_internal")
    all_bars = [_bar(f"2026-06-0{d}", 500.0 + d) for d in range(1, 8)]  # 06-01 .. 06-07
    provider = _PagingBarsProvider(all_bars)
    now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)

    result = capture_benchmark_spine(
        journal, settings, now=now, alpaca_client=_FakeAlpacaClient(), bars_provider=provider,
    )

    assert result["benchmark_bars_written"] == 7
    assert len(provider.calls) == 3  # ceil(7/3) -- pagination genuinely occurred
    rows = journal.query("SELECT bar_date FROM benchmark_bars ORDER BY bar_date")
    assert [r["bar_date"] for r in rows] == [f"2026-06-0{d}" for d in range(1, 8)]


def test_bars_backfill_stops_once_caught_up_even_on_a_full_final_page(journal, monkeypatch):
    """A full-size final page that reaches market_dt must stop immediately --
    no extra call past today, distinct from stopping via a short page."""
    monkeypatch.setattr(benchmark_capture_module, "_BARS_PAGE_SIZE", 3)
    settings = make_settings(ALPHAOS_MODE="paper", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s",
                            ALPACA_PAPER="true", ALPACA_BASE_URL="https://paper-api.alpaca.markets",
                            EXECUTION_PROVIDER="simulated_internal")
    all_bars = [_bar(f"2026-06-0{d}", 500.0 + d) for d in range(1, 7)]  # 06-01 .. 06-06
    provider = _PagingBarsProvider(all_bars)
    now = datetime(2026, 6, 6, 14, 30, tzinfo=timezone.utc)  # market_dt == last bar's date

    result = capture_benchmark_spine(
        journal, settings, now=now, alpaca_client=_FakeAlpacaClient(), bars_provider=provider,
    )

    assert result["benchmark_bars_written"] == 6
    assert len(provider.calls) == 2  # 3 + 3 reaches market_dt exactly; no wasted 3rd call


def test_bars_backfill_hits_the_page_cap_and_logs_a_warning_without_crashing(journal, monkeypatch):
    """A pathological gap that would take more than _MAX_BACKFILL_PAGES pages
    must still return cleanly (never raise) -- it logs a WARNING and leaves
    the remainder for a later run, matching this module's fail-safe contract."""
    monkeypatch.setattr(benchmark_capture_module, "_BARS_PAGE_SIZE", 2)
    monkeypatch.setattr(benchmark_capture_module, "_MAX_BACKFILL_PAGES", 2)
    settings = make_settings(ALPHAOS_MODE="paper", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s",
                            ALPACA_PAPER="true", ALPACA_BASE_URL="https://paper-api.alpaca.markets",
                            EXECUTION_PROVIDER="simulated_internal")
    all_bars = [_bar(f"2026-06-{d:02d}", 500.0 + d) for d in range(1, 11)]  # 10 bars, way more than 2*2
    provider = _PagingBarsProvider(all_bars)
    now = datetime(2026, 6, 20, 14, 30, tzinfo=timezone.utc)  # market_dt far beyond page_size*max_pages

    result = capture_benchmark_spine(
        journal, settings, now=now, alpaca_client=_FakeAlpacaClient(), bars_provider=provider,
    )

    assert result["benchmark_bars_written"] == 4  # exactly 2 pages * 2 bars, then capped
    assert len(provider.calls) == 2
    warning = journal.one(
        "SELECT * FROM system_events WHERE category = 'benchmark_spine' AND message LIKE '%safety cap%'"
    )
    assert warning is not None


# ------------------------------------------------------- overall independence
def test_equity_failure_does_not_block_bars_capture(journal, monkeypatch):
    settings = make_settings(ALPHAOS_MODE="paper", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s",
                            ALPACA_PAPER="true", ALPACA_BASE_URL="https://paper-api.alpaca.markets",
                            EXECUTION_PROVIDER="simulated_internal")
    provider = _FakeBarsProvider([_bar("2026-07-01", 500.0)])

    original_one = journal.one

    def boom_only_for_equity_check(sql, *a, **k):
        if "equity_snapshots" in sql:
            raise RuntimeError("equity table exploded")
        return original_one(sql, *a, **k)

    monkeypatch.setattr(journal, "one", boom_only_for_equity_check)

    result = capture_benchmark_spine(
        journal, settings, alpaca_client=_FakeAlpacaClient(), bars_provider=provider,
    )

    assert result["equity_snapshot"] is None
    assert any("equity snapshot failed" in w for w in result["warnings"])
    assert result["benchmark_bars_written"] == 1  # bars capture proceeded independently


# ---------------------------------------------------------------- no-read grep
def test_benchmark_capture_module_never_referenced_by_decision_paths():
    import pathlib

    import alphaos.approval as approval_mod
    import alphaos.risk.risk_engine as risk_mod

    for mod, name in ((approval_mod, "approval.py"), (risk_mod, "risk_engine.py")):
        text = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert "benchmark_capture" not in text and "benchmark_spine" not in text, \
            f"{name} references the benchmark spine module"
