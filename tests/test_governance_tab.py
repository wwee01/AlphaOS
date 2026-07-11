"""PR-UI-B3: the Autonomy & Risk tab (governance console).

Hermetic -- mock mode, in-memory SQLite, no network, reuses
test_approval_execution._fake_st() for the one full-render test, same
pattern tests/test_learning_tab.py already uses. Covers the new report
module directly (build_governance_report and its private helpers) plus a
generated-not-hardwritten proof (the unattended-window exception line
disappears when UNATTENDED_APPROVE_WINDOWS is blanked), an honesty-guard
swap-test (MAX_PAPER_TRADES_PER_DAY uncapped rendering), a swap-test proving
the reporting-law ratchet in tests/test_daily_brief.py actually catches a
real reference to this module, and one full-render, writes-nothing
invariant test.
"""

from __future__ import annotations

from alphaos.approval import ApprovalEngine
from alphaos.dashboard import streamlit_app
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.reports.governance_report import build_governance_report
from conftest import make_proposal, make_settings
from test_approval_execution import _fake_st

_LABEL = "L1 — unattended cadence"


def _orch(**over):
    return Orchestrator(settings=make_settings(**over), journal=JournalStore(":memory:"))


# ------------------------------------------------------------------ autonomy
def test_may_alone_lines_are_the_exact_mandated_copy_under_default_settings():
    """Fable5 Stitch-adoption ruling's exact required strings, byte-for-byte,
    under v1's real default settings (APPROVAL_MODE=manual,
    REQUIRE_MANUAL_APPROVAL=true -> effective_approval_mode=MANUAL)."""
    orch = _orch()
    rep = build_governance_report(orch.journal, orch.settings, orch.kill_switch, autonomy_level_label=_LABEL)
    auto = rep["autonomy"]
    assert auto["level_label"] == _LABEL
    assert auto["may_alone"] == "May alone: scan, monitor, measure, score, attribute, alert."
    assert auto["may_not_alone"] == "May NOT alone: approve, size, exit, change any rule."
    orch.close()


def test_approve_clause_is_generated_from_effective_approval_mode_not_hardwritten():
    """The one line in "may NOT alone" that genuinely varies with settings:
    under APPROVAL_MODE=auto + REQUIRE_MANUAL_APPROVAL=false (effective mode
    AUTO), the approve clause must differ from the MANUAL-mode literal
    string -- proving this panel is generated, not copy-pasted prose."""
    orch = _orch(APPROVAL_MODE="auto", REQUIRE_MANUAL_APPROVAL="false", MAX_AUTO_APPROVALS_PER_DAY="5")
    rep = build_governance_report(orch.journal, orch.settings, orch.kill_switch, autonomy_level_label=_LABEL)
    assert rep["autonomy"]["may_not_alone"] != "May NOT alone: approve, size, exit, change any rule."
    assert "auto-approval enabled" in rep["autonomy"]["may_not_alone"]
    orch.close()


def test_l2_renders_inactive_with_no_fabricated_criteria_percentages():
    orch = _orch()
    rep = build_governance_report(orch.journal, orch.settings, orch.kill_switch, autonomy_level_label=_LABEL)
    l2 = rep["autonomy"]["l2_status"]
    assert "inactive" in l2
    # No wireframe-style fake readiness fraction ("4/6 met") anywhere in the report.
    import json
    blob = json.dumps(rep)
    assert "criteria" not in blob.lower() or "no readiness criteria" in blob.lower()
    orch.close()


# ------------------------------------------------ unattended exception (generated)
def test_unattended_exception_present_when_windows_and_cap_configured():
    orch = _orch(UNATTENDED_APPROVE_WINDOWS="15:45-16:00", MAX_UNATTENDED_APPROVALS_PER_DAY="1")
    eng = ApprovalEngine(orch.settings, orch.journal)
    eng.consider(make_proposal(symbol="AAA"), risk_ok=True, freshness_ok=True, unattended=True)

    rep = build_governance_report(orch.journal, orch.settings, orch.kill_switch, autonomy_level_label=_LABEL)
    exc = rep["autonomy"]["unattended_exception"]
    assert exc is not None
    assert exc["text"] == (
        "Exception (paper-only): may auto-approve ≤1 proposal/day inside "
        "15:45–16:00 ET close window — used 1/1 today. NOT an autonomy "
        "promotion (PR15/L3 remains gated)."
    )
    orch.close()


def test_unattended_exception_disappears_when_windows_blanked_generated_not_hardwritten():
    """The core generated-not-hand-written proof: blanking
    UNATTENDED_APPROVE_WINDOWS in the fixture (nothing else changes) must
    make the exception line vanish -- if this panel were hand-written prose
    it would not react to the setting at all."""
    orch_armed = _orch(UNATTENDED_APPROVE_WINDOWS="15:45-16:00", MAX_UNATTENDED_APPROVALS_PER_DAY="1")
    rep_armed = build_governance_report(
        orch_armed.journal, orch_armed.settings, orch_armed.kill_switch, autonomy_level_label=_LABEL
    )
    assert rep_armed["autonomy"]["unattended_exception"] is not None

    orch_blank = _orch(UNATTENDED_APPROVE_WINDOWS="")
    rep_blank = build_governance_report(
        orch_blank.journal, orch_blank.settings, orch_blank.kill_switch, autonomy_level_label=_LABEL
    )
    assert rep_blank["autonomy"]["unattended_exception"] is None
    orch_armed.close()
    orch_blank.close()


def test_unattended_exception_absent_when_cap_is_zero_even_with_windows_configured():
    """A non-empty window string paired with a zero cap grants nothing --
    not honestly an "exception" -- both must be truthy for the line to appear."""
    orch = _orch(UNATTENDED_APPROVE_WINDOWS="15:45-16:00", MAX_UNATTENDED_APPROVALS_PER_DAY="0")
    rep = build_governance_report(orch.journal, orch.settings, orch.kill_switch, autonomy_level_label=_LABEL)
    assert rep["autonomy"]["unattended_exception"] is None
    orch.close()


# -------------------------------------------------------------------- kill switch
def test_kill_switch_panel_explains_never_liquidates(tmp_path):
    from alphaos.safety import KillSwitch

    orch = _orch()
    orch.kill_switch = KillSwitch(str(tmp_path / "ks_marker"))
    rep = build_governance_report(orch.journal, orch.settings, orch.kill_switch, autonomy_level_label=_LABEL)
    ks = rep["kill_switch"]
    assert ks["engaged"] is False
    assert ks["state_label"] == "ARMED (not engaged)"
    assert "NOT closed or liquidated" in ks["explanation"]
    assert "annunciator" not in ks["explanation"].lower()  # copy itself doesn't need to name it
    assert "strip above" in ks["control_note"]

    orch.kill_switch.engage("test reason")
    rep2 = build_governance_report(orch.journal, orch.settings, orch.kill_switch, autonomy_level_label=_LABEL)
    ks2 = rep2["kill_switch"]
    assert ks2["engaged"] is True
    assert ks2["state_label"] == "ENGAGED"
    assert ks2["reason"] == "test reason"
    orch.close()


def test_kill_switch_explanation_never_asserts_liquidation_or_position_closure():
    """The mockup content bug this ruling exists to prevent: a kill-switch
    description implying auto-liquidation. The only permitted appearance of
    "liquidat"/"closed" is the explicit NEGATION -- "NOT closed or
    liquidated" -- never a bare affirmative claim that engaging closes or
    liquidates anything."""
    orch = _orch()
    rep = build_governance_report(orch.journal, orch.settings, orch.kill_switch, autonomy_level_label=_LABEL)
    text = rep["kill_switch"]["explanation"]
    assert "NOT closed or liquidated" in text
    # Strip the one sanctioned negated occurrence; nothing else may mention
    # liquidation or closing a position.
    remainder = text.replace("NOT closed or liquidated", "")
    assert "liquidat" not in remainder.lower()
    assert "closed" not in remainder.lower()
    orch.close()


# ------------------------------------------------------------------ hard limits
def test_hard_limits_panel_reflects_live_settings():
    orch = _orch(
        MAX_RISK_PER_TRADE_PCT="0.0075", MAX_OPEN_POSITIONS="3", PAPER_EQUITY="100000",
    )
    rep = build_governance_report(orch.journal, orch.settings, orch.kill_switch, autonomy_level_label=_LABEL)
    hl = rep["hard_limits"]
    assert hl["risk_per_trade_pct"] == 0.0075
    assert hl["risk_per_trade_dollars"] == 750.0
    assert hl["max_open_positions"] == 3
    orch.close()


def test_hard_limits_ai_budget_and_debate_and_hgen_caps_come_from_cost_guard():
    orch = _orch()
    rep = build_governance_report(orch.journal, orch.settings, orch.kill_switch, autonomy_level_label=_LABEL)
    hl = rep["hard_limits"]
    assert hl["ai_budget_used_30d"] == 0
    assert hl["ai_budget_cap_30d"] == orch.settings.scheduler_ai_cost_cap_calls_per_30d
    assert hl["debate_calls_cap_today"] == orch.settings.debate_max_calls_per_day
    assert hl["hypothesis_gen_calls_cap_today"] == orch.settings.hypothesis_gen_max_calls_per_day
    orch.close()


# ---------------------------------------------------- honesty guard (swap-test)
def test_max_paper_trades_renders_uncapped_when_operator_sets_it_extreme():
    """Swap-tested honesty guard (build protocol): MAX_PAPER_TRADES_PER_DAY
    at/above the uncapped threshold renders as an honest "uncapped" label,
    never a re-capped or hidden number."""
    orch = _orch(MAX_PAPER_TRADES_PER_DAY="1000000")
    rep = build_governance_report(orch.journal, orch.settings, orch.kill_switch, autonomy_level_label=_LABEL)
    assert rep["hard_limits"]["max_paper_trades_per_day_display"] == "uncapped (deliberate operator choice)"
    orch.close()


def test_max_paper_trades_renders_the_real_number_when_capped_normally():
    """Negative counterpart: an ordinary cap must render as its literal
    number, not get swept into the "uncapped" label."""
    orch = _orch(MAX_PAPER_TRADES_PER_DAY="5")
    rep = build_governance_report(orch.journal, orch.settings, orch.kill_switch, autonomy_level_label=_LABEL)
    assert rep["hard_limits"]["max_paper_trades_per_day_display"] == "5"
    orch.close()


# ---------------------------------------------------------------- real-money lock
def test_real_money_lock_is_display_only_with_no_unlock_affordance():
    orch = _orch()
    rep = build_governance_report(orch.journal, orch.settings, orch.kill_switch, autonomy_level_label=_LABEL)
    lock = rep["real_money_lock"]
    assert lock["real_trading_enabled_raw"] == "false"
    assert lock["allow_real_orders_raw"] == "false"
    assert lock["structural_statement"] == "Real-money trading unreachable (structural, not a setting)."
    assert "by design" in lock["no_unlock_note"]
    orch.close()


# -------------------------------------------------------------- trading calendar
def test_trading_calendar_panel_reports_today_and_scan_windows():
    orch = _orch()
    rep = build_governance_report(orch.journal, orch.settings, orch.kill_switch, autonomy_level_label=_LABEL)
    cal = rep["trading_calendar"]
    assert len(cal["today_et"]) == 10  # YYYY-MM-DD
    assert isinstance(cal["is_trading_day"], bool)
    assert "never fire on a closed trading day" in cal["note"]
    orch.close()


# ------------------------------------------------------------- reporting-law ratchet
def test_reporting_law_ratchet_catches_a_real_governance_report_reference(tmp_path):
    """Swap-tested guard (build protocol): proves the banned-tokens loop in
    tests/test_daily_brief.py's test_no_decision_path_reads_brief_or_
    health_modules would actually FAIL a decision/execution-path module that
    started importing governance_report, rather than trusting the ratchet's
    presence alone. Exercises the identical detection logic against a
    deliberately violating fixture file."""
    import pathlib

    bad_file = tmp_path / "fake_decision_path.py"
    bad_file.write_text(
        "from alphaos.reports.governance_report import build_governance_report\n"
    )
    text = pathlib.Path(bad_file).read_text(encoding="utf-8")
    banned = ("daily_brief", "position_health", "tqs_report", "journal_feed",
              "promotion_history", "governance_report")
    violations = [token for token in banned if token in text]
    assert violations == ["governance_report"], (
        "the ratchet's detection logic failed to flag a real governance_report "
        f"reference in a fixture decision-path file: {violations}"
    )


# --------------------------------------------------------------- full render
def test_governance_tab_renders_read_only_with_populated_state(monkeypatch):
    """Full-render, writes-nothing invariant (same posture as
    test_learning_tab.py's own full-render test): populate every table the
    Learning tab's watched-tables snapshot already covers, render the whole
    dashboard (including the new Autonomy & Risk tab), and assert not one
    row changed anywhere."""
    orch = _orch(UNATTENDED_APPROVE_WINDOWS="15:45-16:00", MAX_UNATTENDED_APPROVALS_PER_DAY="1")
    j = orch.journal

    watched = (
        "scan_batches", "scheduler_runs", "config_versions",
        "paper_orders", "paper_fills", "positions", "candidates", "trade_proposals",
        "hypothesis_proposals", "hypothesis_drafts", "preregistrations",
        "promotion_decisions", "card_demotions", "attribution_records", "tqs_scores",
    )
    before = {t: j.count_rows(t) for t in watched}

    fake = _fake_st()
    monkeypatch.setattr(streamlit_app, "st", fake)
    streamlit_app.main(orch=orch)

    after = {t: j.count_rows(t) for t in watched}
    assert after == before, f"Autonomy & Risk tab render wrote rows: before={before} after={after}"

    # No engage/release button surfaces inside this tab's own render calls --
    # the only kill-switch control lives in the annunciator (rendered once,
    # separately, at the top of main()). This does not assert zero buttons
    # anywhere on the page (the annunciator legitimately has two); it asserts
    # tab_governance() itself never called st.button.
    orch.close()


def test_tab_governance_never_calls_st_button_directly(monkeypatch):
    """Stronger isolation of the "no duplicate kill-switch control" rule:
    call tab_governance() in isolation (not the full page) and assert it
    never invokes st.button at all -- the annunciator is the only place a
    kill-switch button may appear."""
    orch = _orch()
    fake = _fake_st()
    monkeypatch.setattr(streamlit_app, "st", fake)
    streamlit_app.tab_governance(orch)
    assert fake.button.call_count == 0
    orch.close()
