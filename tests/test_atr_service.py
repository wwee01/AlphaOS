"""INSTR-1 part 2: alphaos.reports.atr_service -- hermetic (injected fake
bars provider, never real network), direct construction, no wall-clock
dependence (explicit ``now``/``market_dt`` throughout).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from alphaos.data.atr import ATR_PERIOD, ATR_RULES_V1
from alphaos.reports.atr_service import update_atr_history
from conftest import make_settings


def _now(d: date) -> datetime:
    """A UTC instant that maps to ``d`` on the US-market (ET) calendar --
    mid-afternoon UTC safely lands on the same ET date year-round."""
    return datetime(d.year, d.month, d.day, 18, 0, tzinfo=timezone.utc)


class _FakeBarsProvider:
    def __init__(self, bars=None):
        self._bars = bars if bars is not None else []
        self.calls = []

    def get_daily_bars(self, symbol, start, end, limit=200):
        self.calls.append((symbol, start, end, limit))
        return self._bars


def _uniform_bars(n, high=101.0, low=99.0, close=100.0, end=date(2026, 3, 2)):
    return [
        {"date": (end - timedelta(days=n - 1 - i)).isoformat(), "open": close,
         "high": high, "low": low, "close": close, "volume": 1_000_000}
        for i in range(n)
    ]


def test_writes_one_row_per_symbol_with_enough_bars(journal):
    settings = make_settings()
    provider = _FakeBarsProvider(_uniform_bars(ATR_PERIOD + 1))

    result = update_atr_history(
        journal, settings, symbols=["AAPL", "MSFT"], now=_now(date(2026, 3, 2)), bars_provider=provider,
    )

    assert result["n_written"] == 2
    assert result["n_symbols"] == 2
    rows = journal.query("SELECT * FROM atr_history ORDER BY symbol")
    assert [r["symbol"] for r in rows] == ["AAPL", "MSFT"]
    assert rows[0]["atr_14"] == 2.0
    assert rows[0]["rules_version"] == ATR_RULES_V1
    assert rows[0]["market_date"] == "2026-03-02"


def test_mixed_symbols_isolates_a_thin_history_from_the_rest(journal):
    """One symbol has too little history (e.g. newly listed) -- must not
    abort the run or affect the other symbol's result."""
    settings = make_settings()

    class _PerSymbolProvider:
        def get_daily_bars(self, symbol, start, end, limit=200):
            if symbol == "THIN":
                return _uniform_bars(3)  # far short of ATR_PERIOD + 1
            return _uniform_bars(ATR_PERIOD + 1)

    result = update_atr_history(
        journal, settings, symbols=["THIN", "AAPL"], now=_now(date(2026, 3, 2)),
        bars_provider=_PerSymbolProvider(),
    )

    assert result["n_written"] == 1
    rows = journal.query("SELECT symbol FROM atr_history")
    assert [r["symbol"] for r in rows] == ["AAPL"]


def test_insufficient_bars_logs_a_system_event_not_silent(journal):
    """Scope/safety audit finding: an exception-raising provider already
    logged a WARNING; a thin/sparse-feed result (compute_atr -> None)
    previously logged NOTHING at all, so a persistent per-symbol gap was
    completely invisible in system_events. Every future PROPOSE for that
    symbol then fail-safe-rejects forever with zero operator-visible trace
    of WHY. This test is what makes that visible."""
    settings = make_settings()

    class _ThinProvider:
        def get_daily_bars(self, symbol, start, end, limit=200):
            return _uniform_bars(3)  # far short of ATR_PERIOD + 1

    update_atr_history(
        journal, settings, symbols=["THIN"], now=_now(date(2026, 3, 2)), bars_provider=_ThinProvider(),
    )

    events = journal.query("SELECT * FROM system_events WHERE category = 'atr_update'")
    assert len(events) == 1
    assert "THIN" in events[0]["message"]
    assert "insufficient" in events[0]["message"].lower()


def test_one_symbols_provider_exception_does_not_abort_the_run(journal):
    settings = make_settings()

    class _FlakyProvider:
        def get_daily_bars(self, symbol, start, end, limit=200):
            if symbol == "BROKEN":
                raise RuntimeError("simulated provider failure")
            return _uniform_bars(ATR_PERIOD + 1)

    result = update_atr_history(
        journal, settings, symbols=["BROKEN", "AAPL"], now=_now(date(2026, 3, 2)),
        bars_provider=_FlakyProvider(),
    )

    assert result["n_written"] == 1
    assert len(result["warnings"]) == 1
    assert "BROKEN" in result["warnings"][0]
    rows = journal.query("SELECT symbol FROM atr_history")
    assert [r["symbol"] for r in rows] == ["AAPL"]


def test_idempotent_same_day_rerun_writes_nothing_new(journal):
    settings = make_settings()
    provider = _FakeBarsProvider(_uniform_bars(ATR_PERIOD + 1))
    now = _now(date(2026, 3, 2))

    first = update_atr_history(journal, settings, symbols=["AAPL"], now=now, bars_provider=provider)
    second = update_atr_history(journal, settings, symbols=["AAPL"], now=now, bars_provider=provider)

    assert first["n_written"] == 1
    assert second["n_written"] == 0
    assert journal.count_rows("atr_history") == 1
    # The idempotency short-circuit (existence check) means the provider is
    # never even called the second time.
    assert len(provider.calls) == 1


def test_different_days_write_separate_rows(journal):
    settings = make_settings()
    provider = _FakeBarsProvider(_uniform_bars(ATR_PERIOD + 1))

    update_atr_history(journal, settings, symbols=["AAPL"], now=_now(date(2026, 3, 2)), bars_provider=provider)
    update_atr_history(journal, settings, symbols=["AAPL"], now=_now(date(2026, 3, 3)), bars_provider=provider)

    assert journal.count_rows("atr_history") == 2


def test_mock_mode_writes_nothing_never_errors(journal):
    """settings.is_mock=True -> make_bars_provider() returns None -> the job
    completes with zero rows written, never a crash (matches
    benchmark_capture.py's own mock-mode behavior)."""
    settings = make_settings()  # make_settings() defaults to mock mode
    assert settings.is_mock

    result = update_atr_history(journal, settings, symbols=["AAPL"], now=_now(date(2026, 3, 2)))

    assert result["n_written"] == 0
    assert journal.count_rows("atr_history") == 0


def test_defaults_to_the_core_book_universe_when_symbols_omitted(journal):
    from alphaos.scanner.candidate_scanner import DEFAULT_UNIVERSE

    settings = make_settings()
    provider = _FakeBarsProvider(_uniform_bars(ATR_PERIOD + 1))

    result = update_atr_history(journal, settings, now=_now(date(2026, 3, 2)), bars_provider=provider)

    assert result["n_symbols"] == len(DEFAULT_UNIVERSE)
