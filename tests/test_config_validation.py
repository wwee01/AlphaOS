"""Config validation: fail-fast on unsupported v1 settings, and no silent
fallback to another data source in live mode (Change Prompt §1, §5, §9)."""

from __future__ import annotations

import pytest

from alphaos.config.settings import SettingsError
from alphaos.data.market_data import MarketDataClient
from conftest import make_settings


def test_invalid_data_provider_fails_fast():
    with pytest.raises(SettingsError):
        make_settings(DATA_PROVIDER="yahoo")
    with pytest.raises(SettingsError):
        make_settings(DATA_PROVIDER="massive")


def test_news_enabled_true_fails_fast():
    with pytest.raises(SettingsError):
        make_settings(NEWS_ENABLED="true")


def test_allow_real_orders_true_fails_fast():
    with pytest.raises(SettingsError):
        make_settings(ALLOW_REAL_ORDERS="true")


def test_unsupported_execution_provider_fails_fast():
    with pytest.raises(SettingsError):
        make_settings(EXECUTION_PROVIDER="alpaca_paper")


def test_default_v1_config_is_alpaca_no_news_simulated():
    s = make_settings()
    assert s.data_provider == "alpaca"
    assert s.market_data_feed == "iex"
    assert s.news_enabled is False
    assert s.execution_provider == "simulated_internal"


def test_live_mode_missing_creds_does_not_fall_back_to_mock(journal):
    # paper (live data) mode with NO Alpaca creds must not silently mock.
    s = make_settings(ALPHAOS_MODE="paper")
    assert s.offline_mode is False
    client = MarketDataClient(s, journal)
    assert client.use_mock is False
    assert client.provider_name == "alpaca"  # NOT alpaca_mock
    snap = client.get_snapshot("AAPL")
    assert snap["is_mock"] is False
    # No creds => unusable data (null timestamp), never fabricated.
    assert snap["source_timestamp"] is None
    assert snap["last_price"] is None


def test_mock_mode_market_data_is_labelled_mock(journal):
    s = make_settings()  # mock
    client = MarketDataClient(s, journal)
    assert client.use_mock is True
    assert client.mode == "mock"
    snap = client.get_snapshot("AAPL")
    assert snap["is_mock"] is True
    assert snap["provider"] == "alpaca_mock"


def test_scheduler_cost_cap_bounds_validation():
    with pytest.raises(SettingsError):
        make_settings(SCHEDULER_AI_COST_CAP_CALLS_PER_30D=49)
    with pytest.raises(SettingsError):
        make_settings(SCHEDULER_AI_COST_CAP_CALLS_PER_30D=100001)
    # EXP-1: SHADOW_AI_CAP_CALLS_PER_30D's own joint-validation (<=25% of the
    # shared pool) must clear this lowered global cap too -- its default of
    # 500 only clears the default global cap of 2000.
    s = make_settings(SCHEDULER_AI_COST_CAP_CALLS_PER_30D=50, SHADOW_AI_CAP_CALLS_PER_30D=12)
    assert s.scheduler_ai_cost_cap_calls_per_30d == 50
    s = make_settings(SCHEDULER_AI_COST_CAP_CALLS_PER_30D=100000)
    assert s.scheduler_ai_cost_cap_calls_per_30d == 100000


def test_debate_daily_cap_cannot_exceed_25pct_of_shared_30day_cap():
    """PR14 audit fix (scope/safety HIGH): DEBATE_MAX_CALLS_PER_DAY's own
    [0, 500] bound and SCHEDULER_AI_COST_CAP_CALLS_PER_30D's own [50, 100000]
    bound are each individually sane, but were NOT jointly validated -- a
    legal combination (daily=500, shared at its own floor of 50) let debate
    alone exhaust the ENTIRE 30-day shared cap in a single day, starving the
    live evaluator for the rest of the window. Reuses the same 25%-of-pool
    ceiling this session's EXP-1 Fable consultation already established for
    an equivalent nested shadow sub-cap (500/2000)."""
    with pytest.raises(SettingsError):
        make_settings(DEBATE_MAX_CALLS_PER_DAY=500, SCHEDULER_AI_COST_CAP_CALLS_PER_30D=50)
    with pytest.raises(SettingsError):
        make_settings(DEBATE_MAX_CALLS_PER_DAY=13, SCHEDULER_AI_COST_CAP_CALLS_PER_30D=50)  # 13 > 12.5
    s = make_settings(
        DEBATE_MAX_CALLS_PER_DAY=12, SCHEDULER_AI_COST_CAP_CALLS_PER_30D=50,
        SHADOW_AI_CAP_CALLS_PER_30D=12,  # EXP-1's own joint-validation must clear this cap too
    )  # 12 <= 12.5
    assert s.debate_max_calls_per_day == 12
    s = make_settings()  # defaults: 10 <= 0.25 * 2000 = 500
    assert s.debate_max_calls_per_day == 10
    assert s.scheduler_ai_cost_cap_calls_per_30d == 2000


def test_benchmark_spine_time_malformed_fails_fast():
    with pytest.raises(SettingsError):
        make_settings(SCHEDULER_BENCHMARK_SPINE_TIME="25:99")
    with pytest.raises(SettingsError):
        make_settings(SCHEDULER_BENCHMARK_SPINE_TIME="not-a-time")


def test_benchmark_spine_time_valid_hhmm_accepted():
    s = make_settings(SCHEDULER_BENCHMARK_SPINE_TIME="09:00")
    assert s.scheduler_benchmark_spine_time == "09:00"


# --- TRIP-1 audit L3 (2026-07-28): the prompt-version allowlist is ONE list ---


def test_prompt_version_allowlist_is_single_sourced_no_literal_duplicates():
    """The valid prompt-version set was once duplicated as a literal
    ("v1","v2","v3") in BOTH settings.py's OPENAI_PROMPT_VERSION validation
    and __main__.py's `--arms MODEL:VERSION` parser. A future v4 updating
    only one would make the version either unconfigurable-but-CLI-accepted
    or the reverse -- and the CLI is the site TRIP-1's own alert text tells
    a woken operator to run, so a drifted parser would reject the exact
    remediation command the pager handed them.

    Source-text guard (same honest-naming caveat as TRIP-1's own sweep: it
    catches the realistic accidental case -- someone re-typing the tuple --
    not a deliberately obfuscated one)."""
    import pathlib
    import re

    from alphaos.constants import PROMPT_VERSIONS

    root = pathlib.Path(__file__).resolve().parents[1] / "alphaos"
    # Any re-typed tuple of two-or-more "vN" string literals in the two
    # historical offender modules is the drift this guards against.
    # Comments are stripped first: settings.py legitimately *describes* the
    # `in ("v2", "v3")` gates in prose, and prose is not drift. (This test
    # caught that false positive on its own first run -- kept as a comment
    # so the next person does not "simplify" the stripping away.)
    pattern = re.compile(r"""\(\s*["']v\d["']\s*,\s*["']v\d["']""")
    for rel in ("config/settings.py", "__main__.py"):
        raw = (root / rel).read_text()
        text = "\n".join(line.split("#", 1)[0] for line in raw.splitlines())
        assert not pattern.search(text), (
            f"{rel} re-inlines a prompt-version tuple literal; import "
            f"PROMPT_VERSIONS from alphaos.constants instead (single source)"
        )
        assert "PROMPT_VERSIONS" in raw, f"{rel} must consume PROMPT_VERSIONS"

    # And the constant itself is well-formed / ordered oldest-first.
    assert PROMPT_VERSIONS[0] == "v1"
    assert list(PROMPT_VERSIONS) == sorted(PROMPT_VERSIONS)


def test_openai_review_model_setting_is_gone_and_stays_gone():
    """Removed 2026-07-28: consumed by nothing (the only reviewer is
    Claude's, reading `claude_review_model`). Pins the removal so it is not
    silently re-added 'for symmetry' with openai_primary_model, and so the
    config fingerprint cannot start moving on a no-op setting again."""
    import pathlib

    from alphaos.config.settings import Settings

    assert not hasattr(Settings, "openai_review_model")
    assert "openai_review_model" not in getattr(Settings, "__annotations__", {})

    root = pathlib.Path(__file__).resolve().parents[1]
    fingerprint = (root / "alphaos/journal/journal_store.py").read_text()
    assert '"openai_review_model": settings.' not in fingerprint, (
        "config fingerprint must not capture a setting that drives no call path"
    )
