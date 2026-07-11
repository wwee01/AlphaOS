"""PR-UI-B3: the Autonomy & Risk (governance) tab's server-side report.

PURE READ. Builds one dict from live settings + journal + cost_guard state;
the dashboard tab (``alphaos/dashboard/streamlit_app.py``'s ``tab_governance``)
only renders this dict -- it never queries the journal or reads settings
fields directly, so this module is the single, hermetically testable source
of truth for everything the tab shows.

Binding content rulings (Fable5 Stitch-adoption ruling; see
``docs/roadmap/ported/stitch-design-tokens.md`` and
``docs/roadmap/alphaos-ui-ux-design.md`` §10) this module exists to enforce:

1. The "may alone / may NOT alone" panel is GENERATED from live settings +
   the current autonomy level, never hand-written prose -- see
   ``_may_alone_lines``. The armed unattended close-window is a
   SCOPED EXCEPTION line (not a level change), generated from
   ``unattended_approve_windows`` + ``max_unattended_approvals_per_day`` +
   today's actual usage -- see ``_unattended_exception``. L2 always renders
   INACTIVE/future with no fabricated readiness percentages (there is no
   criteria checklist in v1 -- L3/PR15 is what would introduce one).
2. The kill-switch panel EXPLAINS state; it never adds a second control (the
   only engage/release buttons live in the annunciator strip,
   ``streamlit_app.render_annunciator``). Copy never implies liquidation.
3. The hard-limits panel is read-only. ``MAX_PAPER_TRADES_PER_DAY`` renders
   honestly as "uncapped (deliberate operator choice)" once it clears
   ``_UNCAPPED_PAPER_TRADES_THRESHOLD`` -- never silently re-capped or
   hidden.
4. The real-money lock panel is display-only and says so explicitly; no
   unlock affordance exists anywhere in this module or the tab that renders
   it.
5. The trading-calendar line reports (never gates) today's ET date,
   `is_trading_day()`, and the configured scan windows.

No drawdown-governor block (post-Crossing feature, not built here), no LIVE
badge, no leverage/margin rows (none exist in this codebase), no L2_ACTIVE
state, no typed-confirm duplication -- all explicitly out of scope per the
governing ruling.
"""

from __future__ import annotations

from alphaos.constants import ApprovalMode
from alphaos.scheduler import cost_guard
from alphaos.scheduler.cadence import parse_windows
from alphaos.util import market_calendar, timeutils

# MAX_PAPER_TRADES_PER_DAY is allowed to be set arbitrarily high by an
# operator who deliberately wants no daily trade cap (documented in
# settings.py's own env-var comments). Anything at/above this threshold
# renders as "uncapped" rather than as a literal (and misleading) number --
# see the module docstring, binding ruling #3.
_UNCAPPED_PAPER_TRADES_THRESHOLD = 1_000_000

_L2_STATUS = "inactive (future — no readiness criteria exist in v1; PR15 introduces L3 promotion)"


def _approve_clause(settings) -> str:
    """The one line in "may NOT alone" whose wording is genuinely settings-
    driven: under the current (and only shipped) MANUAL effective mode this
    reads exactly "approve", matching the ruling's mandated copy; if
    ``effective_approval_mode`` were ever AUTO (APPROVAL_MODE=auto AND
    REQUIRE_MANUAL_APPROVAL=false), the clause grows an explicit caveat
    rather than silently staying "approve" while the setting says otherwise."""
    if settings.effective_approval_mode == ApprovalMode.AUTO:
        return "approve (global auto-approval enabled, capped at MAX_AUTO_APPROVALS_PER_DAY — still logged)"
    return "approve"


def _may_alone_lines(settings) -> dict:
    """The "may alone / may NOT alone" panel. Per binding ruling #1: fixed
    verb lists (there is exactly one autonomy level in v1, so there is
    nothing else for scan/monitor/measure/score/attribute/alert to vary
    with), but the "approve" clause inside "may NOT alone" is derived from
    ``effective_approval_mode`` (see ``_approve_clause``) -- proving this
    panel is generated from settings, not hand-written prose."""
    may_alone = "May alone: scan, monitor, measure, score, attribute, alert."
    may_not_alone = f"May NOT alone: {_approve_clause(settings)}, size, exit, change any rule."
    return {"may_alone": may_alone, "may_not_alone": may_not_alone}


def _format_window(start: str, end: str) -> str:
    return f"{start}–{end} ET"


def _format_windows_label(windows: list[tuple[str, str]]) -> str:
    """Pre-formatted "start–end ET and start–end ET" label (or "none
    configured" for an empty list) -- computed once here so the dashboard
    tab never re-derives this formatting itself (single source of truth,
    same discipline console_theme.render_ttl_bar's docstring documents for
    its own pre-formatted ``label`` parameter)."""
    if not windows:
        return "none configured"
    return " and ".join(_format_window(start, end) for start, end in windows)


def _unattended_exception(settings, journal) -> dict | None:
    """The armed unattended close-window, rendered as a scoped EXCEPTION —
    never an autonomy-level change. None (no panel line at all) unless BOTH
    a non-empty ``unattended_approve_windows`` AND a positive
    ``max_unattended_approvals_per_day`` are configured — a non-empty window
    string paired with a zero cap grants nothing, so it is not honestly an
    "exception" either. This is the function the generated-not-hardwritten
    test exercises directly: blanking UNATTENDED_APPROVE_WINDOWS in the
    fixture must make this return None and the exception line disappear."""
    windows = parse_windows(settings.unattended_approve_windows)
    cap = settings.max_unattended_approvals_per_day
    if not windows or cap <= 0:
        return None
    used = journal.count_unattended_approvals_today()
    window_label = _format_windows_label(windows)
    text = (
        f"Exception (paper-only): may auto-approve ≤{cap} proposal/day inside "
        f"{window_label} close window — used {used}/{cap} today. NOT an "
        "autonomy promotion (PR15/L3 remains gated)."
    )
    return {"text": text, "windows": windows, "cap": cap, "used_today": used}


def _autonomy_panel(settings, journal, autonomy_level_label: str) -> dict:
    panel: dict = {"level_label": autonomy_level_label, "l2_status": _L2_STATUS}
    panel.update(_may_alone_lines(settings))
    panel["unattended_exception"] = _unattended_exception(settings, journal)
    return panel


_KILL_SWITCH_EXPLANATION = (
    "ENGAGE halts all new machine actions — scans, proposals, approvals, "
    "order submission. Monitoring and protection keep running (detect and "
    "alert only). Open positions are NOT closed or liquidated. Release "
    "requires a reason; both are logged."
)
_KILL_SWITCH_CONTROL_NOTE = "Engage/release lives in the strip above on every screen."


def _kill_switch_panel(kill_switch) -> dict:
    engaged = kill_switch.is_engaged()
    return {
        "engaged": engaged,
        "reason": kill_switch.reason() if engaged else None,
        "state_label": "ENGAGED" if engaged else "ARMED (not engaged)",
        "explanation": _KILL_SWITCH_EXPLANATION,
        "control_note": _KILL_SWITCH_CONTROL_NOTE,
    }


def _paper_trades_display(cap: int) -> str:
    if cap >= _UNCAPPED_PAPER_TRADES_THRESHOLD:
        return "uncapped (deliberate operator choice)"
    return str(cap)


def _hard_limits_panel(settings, journal) -> dict:
    equity = settings.paper_equity
    risk_pct = settings.max_risk_per_trade_pct
    loss_pct = settings.max_daily_loss_pct

    ai_used = cost_guard.calls_in_last_30_days(journal)
    debate_used = cost_guard.debate_calls_today(journal)
    hgen_used = cost_guard.hypothesis_gen_calls_today(journal)

    return {
        "risk_per_trade_pct": risk_pct,
        "risk_per_trade_dollars": round(equity * risk_pct, 2),
        "max_open_positions": settings.max_open_positions,
        "daily_loss_stop_pct": loss_pct,
        "daily_loss_stop_dollars": round(equity * loss_pct, 2),
        "auto_approvals_used_today": journal.count_auto_approvals_today(),
        "auto_approvals_cap": settings.max_auto_approvals_per_day,
        "unattended_approvals_used_today": journal.count_unattended_approvals_today(),
        "unattended_approvals_cap": settings.max_unattended_approvals_per_day,
        "unattended_windows": parse_windows(settings.unattended_approve_windows),
        "unattended_windows_label": _format_windows_label(parse_windows(settings.unattended_approve_windows)),
        "max_spread_pct": settings.max_spread_pct,
        "min_dollar_volume": settings.min_dollar_volume,
        "ai_budget_used_30d": ai_used,
        "ai_budget_cap_30d": settings.scheduler_ai_cost_cap_calls_per_30d,
        "debate_calls_used_today": debate_used,
        "debate_calls_cap_today": settings.debate_max_calls_per_day,
        "hypothesis_gen_calls_used_today": hgen_used,
        "hypothesis_gen_calls_cap_today": settings.hypothesis_gen_max_calls_per_day,
        "max_paper_trades_per_day_raw": settings.max_paper_trades_per_day,
        "max_paper_trades_per_day_display": _paper_trades_display(settings.max_paper_trades_per_day),
        "paper_trades_used_today": journal.count_paper_orders_today(),
        "changes_note": "changes → Class C protocol (out-of-band, never a UI action)",
    }


_REAL_MONEY_STRUCTURAL_STATEMENT = "Real-money trading unreachable (structural, not a setting)."
_REAL_MONEY_NO_UNLOCK_NOTE = (
    "No unlock control exists in this UI — the absence is by design, not an "
    "oversight. Flipping this requires an out-of-band, human, non-UI change "
    "(Class C protocol)."
)


def _real_money_lock_panel(settings) -> dict:
    return {
        "real_trading_enabled_raw": settings.real_trading_enabled_raw,
        "allow_real_orders_raw": settings.allow_real_orders_raw,
        "mode": settings.mode.value,
        "structural_statement": _REAL_MONEY_STRUCTURAL_STATEMENT,
        "no_unlock_note": _REAL_MONEY_NO_UNLOCK_NOTE,
    }


def _trading_calendar_panel(settings) -> dict:
    now_et = timeutils.to_et(timeutils.now_utc())
    today = now_et.date()
    scan_windows = parse_windows(settings.scheduler_scan_windows)
    return {
        "today_et": today.isoformat(),
        "is_trading_day": market_calendar.is_trading_day(today),
        "scan_windows": scan_windows,
        "scan_windows_label": _format_windows_label(scan_windows),
        "note": "Scans and time-based exits never fire on a closed trading day.",
    }


def build_governance_report(journal, settings, kill_switch, *, autonomy_level_label: str) -> dict:
    """Assemble the full read-only dict the Autonomy & Risk tab renders.

    ``autonomy_level_label`` is passed in (rather than duplicated here) so
    this module has exactly one caller-supplied source for that string --
    ``streamlit_app.AUTONOMY_LEVEL_LABEL``, the same constant the
    annunciator strip already uses -- instead of two copies that could
    drift."""
    return {
        "autonomy": _autonomy_panel(settings, journal, autonomy_level_label),
        "kill_switch": _kill_switch_panel(kill_switch),
        "hard_limits": _hard_limits_panel(settings, journal),
        "real_money_lock": _real_money_lock_panel(settings),
        "trading_calendar": _trading_calendar_panel(settings),
    }
