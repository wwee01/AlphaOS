"""GROUP-B / TIME-1: the time exit that never fires for broker-managed
(alpaca_paper) positions.

Covers:
* Part 1 (ALWAYS ON): honest time_stop_status -- NOT_ENFORCED_BROKER_MANAGED
  for a broker-managed position instead of the previous dishonest "active".
* Part 2 (ALWAYS ON): _one_action + a "## Needs you" line whenever
  trading_days_held >= max_holding_days on any open position.
* Part 3 (DARK, TIME_EXIT_BREACH_ALERT_ENABLED, default false, DETECT-AND-
  ALERT ONLY -- it never closes or cancels anything): flag OFF is
  byte-identical to today (no alert, position untouched). Flag ON detects
  the past-window condition (reusing _check_exit's own two-guard arithmetic)
  and raises ONE loud alert -- see
  execution/position_manager.py::_maybe_alert_time_exit_breach's own
  docstring for WHY it can never close anything: tests/test_entry_ttl.py::
  test_position_manager_monitor_and_protection_watchdog_never_touch_the_broker
  is a pre-existing, deliberately-enforced architecture guard proving
  PositionManager's source may never call the broker directly (all broker
  mutation is confined to execution/order_manager.py's OrderManager, outside
  this ticket's file group) -- so flag ON and flag OFF are IDENTICAL in
  their effect on any position (never closes); "cancel failure never submits
  a close" therefore holds trivially, by construction, for every flag state.

All hermetic -- NO real network/API calls anywhere in this file, and no
broker client is ever constructed by PositionManager in this build.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from alphaos.config import settings as settings_module
from alphaos.constants import TimeStopStatus
from alphaos.execution.position_manager import PositionManager
from alphaos.reports.daily_brief import _one_action, _positions_past_time_window, render_markdown, build_daily_brief
from alphaos.util import timeutils
from alphaos.util.ids import new_id
from conftest import make_settings

_MIDDAY_UTC = timezone.utc


def _utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 17, 0, 0, tzinfo=_MIDDAY_UTC)


def _freeze(monkeypatch, d: date) -> None:
    monkeypatch.setattr(timeutils, "now_utc", lambda: _utc(d))


def _order_row(**overrides):
    row = {
        "order_id": new_id("ord"), "symbol": "AMD", "direction": "long", "qty": 10.0,
        "strategy": "swing", "stop_loss_price": 50.0, "take_profit_price": 300.0,
        "execution_source": "alpaca_paper", "is_short": 0, "is_demo": 0,
        "trade_id": new_id("trade"), "proposal_id": None,
        "broker_order_id": new_id("alpaca_ord"),
    }
    row.update(overrides)
    return row


def _open_broker_position(journal, monkeypatch, entry_date: date, max_holding_days: int, **overrides):
    _freeze(monkeypatch, entry_date)
    settings = make_settings(ALPHAOS_MODE="mock")
    pm = PositionManager(settings, journal)
    row = _order_row(**overrides)
    proposal_id = new_id("prop")
    journal.insert("trade_proposals", {
        "proposal_id": proposal_id, "candidate_id": new_id("cand"), "symbol": row["symbol"],
        "direction": row["direction"], "strategy": "swing", "entry": 100.0, "stop": 50.0,
        "target": 300.0, "max_holding_days": max_holding_days, "qty": 10.0,
        "risk_per_share": 50.0, "dollar_risk": 500.0, "expected_r": 4.0,
        "same_day_exit_eligible": 0, "status": "pending_approval",
    })
    row["proposal_id"] = proposal_id
    position_id = pm.open_position(row, 100.0)
    pos = journal.one("SELECT * FROM positions WHERE position_id = ?", (position_id,))
    return pos


# ------------------------------------------------------------------ Part 1
# (ALWAYS ON -- both tests below construct settings with the flag at its
# default/unset, proving this is independent of TIME_EXIT_BREACH_ALERT_ENABLED.)
def test_time_stop_status_broker_managed_default_off_is_honest_not_active():
    status = PositionManager._time_stop_status(decision=None, broker_managed=True)
    assert status == TimeStopStatus.NOT_ENFORCED_BROKER_MANAGED.value
    assert status != "active"


def test_time_stop_status_simulated_internal_path_unchanged():
    """Part 1 touches ONLY the broker-managed branch -- the simulated_internal
    path's own "active"/"expired" meaning must be byte-identical."""
    assert PositionManager._time_stop_status(None, False) == "active"
    assert PositionManager._time_stop_status("time_expiry", False) == "expired"
    assert PositionManager._time_stop_status("stop", False) == "active"


def test_monitor_writes_honest_status_for_a_broker_managed_position_not_past_window(journal, monkeypatch):
    entry_date = date(2026, 7, 9)
    pos = _open_broker_position(journal, monkeypatch, entry_date, max_holding_days=10)
    _freeze(monkeypatch, date(2026, 7, 10))
    settings = make_settings(ALPHAOS_MODE="mock", TIME_EXIT_BREACH_ALERT_ENABLED="false")
    pm = PositionManager(settings, journal)

    pm.monitor(price_overrides={"AMD": 120.0})

    row = journal.one(
        "SELECT time_stop_status FROM monitoring_snapshots WHERE position_id = ? ORDER BY id DESC LIMIT 1",
        (pos["position_id"],),
    )
    assert row["time_stop_status"] == "not_enforced_broker_managed"


# ------------------------------------------------------------------ Part 2
def test_positions_past_time_window_flags_only_positions_at_or_past_their_window():
    positions_health = [
        {"symbol": "AMD", "trading_days_held": 7, "max_holding_days": 3},
        {"symbol": "NVDA", "trading_days_held": 2, "max_holding_days": 3},
        {"symbol": "MSFT", "trading_days_held": 3, "max_holding_days": 3},  # exactly at window
        {"symbol": "AAPL", "trading_days_held": None, "max_holding_days": 3},  # unknown -- never flagged
    ]
    flagged = _positions_past_time_window(positions_health)
    symbols = {p["symbol"] for p in flagged}
    assert symbols == {"AMD", "MSFT"}


def test_one_action_names_symbols_and_day_counts_for_positions_past_window():
    needs_you = {
        "open_incident_count": 0, "fused_jobs": [], "hypothesis_resolution": None,
        "pending_approvals": [],
        "positions_past_time_window": [
            {"symbol": "AMD", "trading_days_held": 7, "max_holding_days": 3},
            {"symbol": "AVGO", "trading_days_held": 7, "max_holding_days": 3},
        ],
    }
    action = _one_action(needs_you, positions_health=[], moonshot_gap={"status": "ok"})
    assert "AMD (7/3td)" in action
    assert "AVGO (7/3td)" in action
    assert "2 position(s) past their time-exit window" in action


def test_render_markdown_needs_you_section_shows_zero_on_a_quiet_journal(orchestrator):
    """Always present, even at 0 -- the whole point of this line is that it
    never has to be remembered to be checked for."""
    brief = build_daily_brief(orchestrator.journal, orchestrator.settings, orchestrator.kill_switch)
    md = render_markdown(brief)
    assert "Positions past their time-exit window: **0**" in md


def test_build_daily_brief_example_a_position_past_its_window(journal, monkeypatch):
    """Part 2 is ALWAYS ON: TIME_EXIT_BREACH_ALERT_ENABLED is explicitly
    left at its default (false) here, and the "## Needs you" line + the
    _one_action rung still fire -- proving this visibility fix is
    independent of that flag, exactly as the spec requires."""
    entry_date = date(2026, 7, 9)
    _open_broker_position(journal, monkeypatch, entry_date, max_holding_days=3)
    _freeze(monkeypatch, date(2026, 7, 20))  # well past a 3-trading-day window
    from alphaos.orchestrator import Orchestrator

    settings = make_settings(ALPHAOS_MODE="mock", TIME_EXIT_BREACH_ALERT_ENABLED="false")
    orch = Orchestrator(settings=settings, journal=journal)
    brief = build_daily_brief(journal, settings, orch.kill_switch)
    assert brief["needs_you"]["positions_past_time_window"]
    assert "past their time-exit window" in brief["one_action"]
    md = render_markdown(brief)
    assert "Positions past their time-exit window: **1**" in md
    assert "AMD (" in md and "td)" in md


# ------------------------------------------------------------------ Part 3
def test_flag_off_never_alerts_position_stays_open_no_exit(journal, monkeypatch):
    """THE flag-off byte-identical proof: a broker-managed position well
    past its window, TIME_EXIT_BREACH_ALERT_ENABLED left at its default
    (unset -> false), must be untouched -- no alert, position still open,
    zero exits/trade_outcomes rows. This is what "byte-identical to today's
    behavior" means operationally: WHEN a position exits is unchanged by
    this merge."""
    entry_date = date(2026, 7, 9)
    pos = _open_broker_position(journal, monkeypatch, entry_date, max_holding_days=3)
    _freeze(monkeypatch, date(2026, 7, 20))
    settings = make_settings(ALPHAOS_MODE="mock")  # TIME_EXIT_BREACH_ALERT_ENABLED unset -> default false
    assert settings.time_exit_breach_alert_enabled is False
    alerts_sent = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: alerts_sent.append(k))
    pm = PositionManager(settings, journal)

    exits = pm.monitor(price_overrides={"AMD": 120.0})

    assert exits == []
    row = journal.one("SELECT status FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert row["status"] == "open"
    assert journal.count_rows("exits") == 0
    assert journal.count_rows("trade_outcomes") == 0
    assert alerts_sent == []
    snap = journal.one(
        "SELECT time_stop_status, action_taken FROM monitoring_snapshots WHERE position_id = ? "
        "ORDER BY id DESC LIMIT 1", (pos["position_id"],),
    )
    assert snap["time_stop_status"] == "not_enforced_broker_managed"
    assert snap["action_taken"] == "broker_managed"


def test_flag_on_past_window_alerts_loudly_but_still_never_closes(journal, monkeypatch):
    """THE 'cancel failure never submits a close' proof, adapted to this
    build's actual architecture (see execution/position_manager.py::
    _maybe_alert_time_exit_breach's own docstring): PositionManager cannot
    call the broker at all -- a pre-existing, deliberately-enforced
    architecture test (tests/test_entry_ttl.py) asserts its source never
    does. So there is no code path, success or failure, through which
    flag-on could ever submit a close; this proves the observable behavior
    that guarantees -- the position stays open, no exit is ever recorded --
    while still surfacing ONE loud, high-priority alert so an operator who
    arms the flag never gets silent inaction."""
    entry_date = date(2026, 7, 9)
    pos = _open_broker_position(journal, monkeypatch, entry_date, max_holding_days=3)
    _freeze(monkeypatch, date(2026, 7, 20))
    settings = make_settings(ALPHAOS_MODE="mock", TIME_EXIT_BREACH_ALERT_ENABLED="true")
    alerts_sent = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: alerts_sent.append(k))
    pm = PositionManager(settings, journal)

    exits = pm.monitor(price_overrides={"AMD": 120.0})

    assert exits == []
    row = journal.one("SELECT status FROM positions WHERE position_id = ?", (pos["position_id"],))
    assert row["status"] == "open"
    assert journal.count_rows("exits") == 0
    assert len(alerts_sent) == 1
    assert alerts_sent[0]["priority"] == "high"
    assert "past its time-exit window" in alerts_sent[0]["title"]
    assert "detect-and-alert-only" in alerts_sent[0]["message"]
    snap = journal.one(
        "SELECT time_stop_status, action_taken FROM monitoring_snapshots WHERE position_id = ? "
        "ORDER BY id DESC LIMIT 1", (pos["position_id"],),
    )
    assert snap["time_stop_status"] == "not_enforced_broker_managed"
    assert snap["action_taken"] == "broker_managed"


def test_flag_on_alerts_once_per_day_per_position_not_every_monitor_pass(journal, monkeypatch):
    """Audit-fixup MEDIUM-5: with no dedup, an armed flag would page on
    EVERY monitor pass (~26x/trading day per stuck position at the default
    5-minute interval), desensitizing exactly the channel this ticket
    exists to keep meaningful. Three monitor() passes the SAME SGT day must
    produce exactly ONE alert; a second position must still get its OWN
    alert (the latch is per position_id, not global)."""
    entry_date = date(2026, 7, 9)
    _open_broker_position(journal, monkeypatch, entry_date, max_holding_days=3)
    _open_broker_position(journal, monkeypatch, entry_date, max_holding_days=3, symbol="AVGO",
                          broker_order_id=new_id("alpaca_ord"))
    _freeze(monkeypatch, date(2026, 7, 20))
    settings = make_settings(ALPHAOS_MODE="mock", TIME_EXIT_BREACH_ALERT_ENABLED="true")
    alerts_sent = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: alerts_sent.append(k))
    pm = PositionManager(settings, journal)

    pm.monitor(price_overrides={"AMD": 120.0, "AVGO": 120.0})
    pm.monitor(price_overrides={"AMD": 120.0, "AVGO": 120.0})
    pm.monitor(price_overrides={"AMD": 120.0, "AVGO": 120.0})

    assert len(alerts_sent) == 2  # one per position, not one per (position x pass)
    titles = {a["title"] for a in alerts_sent}
    assert any("AMD" in t for t in titles)
    assert any("AVGO" in t for t in titles)


def test_flag_on_latch_lookup_failure_still_alerts_and_does_not_abort_the_pass(journal, monkeypatch):
    """Audit-fixup FIX-2 (round 2): the M-5 dedup latch added a genuine DB
    READ inside _maybe_alert_time_exit_breach, called UNGUARDED from
    monitor(). A read failure there (e.g. 'database is locked', the
    documented WAL-contention class on this project) must NOT abort the
    entire monitor pass -- it must fail toward ALERTING ANYWAY, and a
    LATER position in iteration order must still get its own full
    snapshot/exit treatment. Proven with an injected raise on journal.one
    scoped to the latch's own query shape only (other journal.one calls --
    e.g. inside close_position -- must keep working)."""
    entry_date = date(2026, 7, 9)
    pos_broker = _open_broker_position(journal, monkeypatch, entry_date, max_holding_days=3)
    # A SECOND, simulated_internal position that is ALSO genuinely past its
    # window -- proves a DB error on the FIRST (broker-managed) position's
    # latch lookup never prevents this one's real time_expiry exit later in
    # the same monitor() iteration.
    settings_for_sim = make_settings(ALPHAOS_MODE="mock")
    pm_open = PositionManager(settings_for_sim, journal)
    sim_row = _order_row(
        symbol="MSFT", execution_source="simulated_internal", broker_order_id=None,
    )
    proposal_id = new_id("prop")
    journal.insert("trade_proposals", {
        "proposal_id": proposal_id, "candidate_id": new_id("cand"), "symbol": "MSFT",
        "direction": "long", "strategy": "swing", "entry": 100.0, "stop": 50.0,
        "target": 300.0, "max_holding_days": 3, "qty": 10.0,
        "risk_per_share": 50.0, "dollar_risk": 500.0, "expected_r": 4.0,
        "same_day_exit_eligible": 0, "status": "pending_approval",
    })
    sim_row["proposal_id"] = proposal_id
    pm_open.open_position(sim_row, 100.0)

    _freeze(monkeypatch, date(2026, 7, 20))
    settings = make_settings(ALPHAOS_MODE="mock", TIME_EXIT_BREACH_ALERT_ENABLED="true")
    alerts_sent = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: alerts_sent.append(k))

    real_journal_one = journal.one

    def _flaky_one(sql, params=()):
        if "time_exit_breach_alert" in sql:
            raise RuntimeError("database is locked")
        return real_journal_one(sql, params)

    monkeypatch.setattr(journal, "one", _flaky_one)
    pm = PositionManager(settings, journal)

    exits = pm.monitor(price_overrides={"AMD": 120.0, "MSFT": 120.0})

    # The broker-managed position: latch lookup raised -> alert fired anyway
    # (fail toward alerting, never toward silence).
    assert len(alerts_sent) == 1
    assert alerts_sent[0]["priority"] == "high"
    # The simulated_internal position: a completely independent code path
    # (never touches the latch at all) -- its genuine time_expiry exit must
    # be entirely unaffected by the OTHER position's latch failure.
    assert len(exits) == 1
    assert exits[0]["symbol"] == "MSFT"
    assert exits[0]["exit_reason"] == "time_expiry"
    row = journal.one("SELECT status FROM positions WHERE position_id = ?", (pos_broker["position_id"],))
    assert row["status"] == "open"  # broker-managed position untouched either way


def test_flag_on_position_not_yet_past_window_never_alerts(journal, monkeypatch):
    """The 2-guard trading-day arithmetic is reused (not reimplemented):
    _maybe_alert_time_exit_breach delegates straight to _check_exit, which
    is already exhaustively tested for the is_trading_day/
    trading_days_between guard combo
    (tests/test_hold1_trading_day_holding_period.py) -- this proves the
    DELEGATION, not the arithmetic a second time."""
    entry_date = date(2026, 7, 9)  # Thursday
    _open_broker_position(journal, monkeypatch, entry_date, max_holding_days=3)
    _freeze(monkeypatch, date(2026, 7, 10))  # Friday -- only 1 trading day elapsed
    settings = make_settings(ALPHAOS_MODE="mock", TIME_EXIT_BREACH_ALERT_ENABLED="true")
    alerts_sent = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: alerts_sent.append(k))
    pm = PositionManager(settings, journal)

    exits = pm.monitor(price_overrides={"AMD": 120.0})

    assert exits == []
    assert alerts_sent == []


def test_flag_on_stop_or_target_already_hit_defers_to_the_broker_oco_not_this_method(journal, monkeypatch):
    """A broker-managed position whose price already cleared its stop/target
    this pass is left to the broker's own OCO -- enforcement only ever acts
    on a genuine time_expiry verdict."""
    entry_date = date(2026, 7, 9)
    _open_broker_position(journal, monkeypatch, entry_date, max_holding_days=3,
                          stop_loss_price=50.0, take_profit_price=110.0)
    _freeze(monkeypatch, date(2026, 7, 20))
    settings = make_settings(ALPHAOS_MODE="mock", TIME_EXIT_BREACH_ALERT_ENABLED="true")
    alerts_sent = []
    monkeypatch.setattr("alphaos.util.alerts.send_alert", lambda *a, **k: alerts_sent.append(k))
    pm = PositionManager(settings, journal)

    # price already above target -- _check_exit would return "target", not
    # "time_expiry", so _maybe_alert_time_exit_breach must no-op (returns).
    exits = pm.monitor(price_overrides={"AMD": 150.0})

    assert exits == []
    assert alerts_sent == []


def test_position_manager_source_never_calls_the_broker_directly():
    """Locks in the same architecture invariant
    tests/test_entry_ttl.py::test_position_manager_monitor_and_protection_
    watchdog_never_touch_the_broker already enforces -- duplicated here (in
    Group B's own test module) so a future edit to this file that
    reintroduces a direct broker call trips two independent tests, not
    just the pre-existing one this ticket's first draft broke."""
    import pathlib

    import alphaos.execution.position_manager as pm_mod

    text = pathlib.Path(str(pm_mod.__file__)).read_text(encoding="utf-8")
    assert "self.alpaca." not in text
    assert ".cancel_order(" not in text
    assert ".submit_bracket(" not in text


# ------------------------------------------------------------------ Part 4
def test_env_example_three_axes_resynced():
    """Audit-fixup FIX-4 (round 2, operator call): ALPHAOS_MODE and
    EXECUTION_PROVIDER stay at their SAFE, offline-bootable defaults
    (mock / simulated_internal) -- production's real values (paper /
    alpaca_paper) are documented in the adjacent comments instead of being
    this template's own bootable default. ACTIVE_CARD_ID and
    OPENAI_PROMPT_VERSION (the other two TIME-1 part 4 axes, neither of
    which conflicts with an offline boot) stay resynced to their live
    values."""
    with open(".env.example", "r", encoding="utf-8") as fh:
        content = fh.read()
    assert "EXECUTION_PROVIDER=simulated_internal" in content
    assert "OPENAI_PROMPT_VERSION=v4" in content
    assert "ACTIVE_CARD_ID=catalyst_momentum_v3" in content
    assert "TIME_EXIT_BREACH_ALERT_ENABLED=false" in content
    assert "ALPHAOS_MODE=mock" in content
    assert "SCHEDULER_PREFLIGHT_TIME=07:15" in content


def test_env_example_loads_without_a_settingserror():
    """`cp .env.example .env` must boot cleanly, offline, with zero
    credentials -- the whole point of FIX-4's reversal. Parses .env.example
    with the SAME minimal parser settings.py's own load_dotenv uses
    (key=value, '#' comments, blank lines skipped) and feeds it through
    load_settings(env=...) directly -- proves the actual documented setup
    path boots, not just that two strings happen to match."""
    env: dict = {}
    with open(".env.example", "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")

    loaded = settings_module.load_settings(load_env_file=False, env=env)

    assert loaded.mode.value == "mock"
    assert loaded.execution_provider == "simulated_internal"


def test_env_divergence_log_silent_with_no_local_env_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text("FOO=1\nBAR=2\n", encoding="utf-8")
    settings_module._log_env_divergence()
    captured = capsys.readouterr()
    assert captured.out == ""


def test_env_divergence_log_reports_missing_keys_only_never_values(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text("FOO=1\nBAR=super-secret-value\nBAZ=3\n", encoding="utf-8")
    (tmp_path / ".env").write_text("FOO=whatever\n", encoding="utf-8")
    settings_module._log_env_divergence()
    captured = capsys.readouterr()
    assert "BAR" in captured.out
    assert "BAZ" in captured.out
    assert "FOO" not in captured.out  # present locally -- not a divergence
    assert "super-secret-value" not in captured.out  # never values, only key names
    assert "2 key" in captured.out


def test_env_divergence_log_silent_when_nothing_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text("FOO=1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("FOO=2\n", encoding="utf-8")
    settings_module._log_env_divergence()
    captured = capsys.readouterr()
    assert captured.out == ""


def test_load_settings_test_path_never_triggers_the_divergence_check(tmp_path, monkeypatch, capsys):
    """Hermeticity: every test in this suite calls load_settings(env={...}),
    which must NEVER touch the filesystem for this check (or anything else
    dotenv-related) -- this is what keeps the whole suite's settings
    fixtures free of real-.env side effects."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SOMETHING=1\n", encoding="utf-8")
    make_settings()
    captured = capsys.readouterr()
    assert captured.out == ""


def test_time_exit_breach_alert_enabled_defaults_false_and_is_in_config_hash():
    from alphaos.lineage.config_snapshot import build_config_hashes

    default_settings = make_settings()
    assert default_settings.time_exit_breach_alert_enabled is False
    on_settings = make_settings(TIME_EXIT_BREACH_ALERT_ENABLED="true")
    assert on_settings.time_exit_breach_alert_enabled is True
    # Adding this field to Settings automatically perturbs config_hash (it
    # hashes the FULL settings dict) -- no separate wiring needed.
    assert (
        build_config_hashes(default_settings)["config_hash"]
        != build_config_hashes(on_settings)["config_hash"]
    )
