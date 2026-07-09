"""PR11 Daily Brief: the daily human interface. Composes what needs you, what
the machine did today, what it learned, and the one action -- from data every
other report/measurement module already produces. Pure read; never writes,
never touches gates/execution/orders. Not an intraday surface (see specs
doc's PR11 non-goals) -- the dashboard reads live tables for that.

Every top-level section is ALWAYS present, even on a brand-new/empty journal
(the empty-state is a first-class case, not an error): floors gate the
CLAIMS a section can make, never whether the section key itself exists.
"""

from __future__ import annotations

from typing import Optional

from alphaos.data.market_data import MarketDataClient
from alphaos.proposals import seconds_remaining
from alphaos.reports.attribution import ATTRIBUTION_V2_CAVEAT
from alphaos.reports.position_health import VERDICT_EXIT_REVIEW, assess_positions
from alphaos.reports.relative_performance import (
    build_relative_performance_report,
)
from alphaos.scheduler import cadence
from alphaos.scheduler.digest import _start_of_today_sgt_utc, build_daily_digest
from alphaos.stats.fdr import preregistration_family_summary
from alphaos.util import timeutils

# PORT-1's survivorship-denominator caveat (contract doc port spec item 5):
# any report claiming system-level edge must print the FULL preregistration
# family (promoted + demoted + withdrawn), never just a promoted subset --
# otherwise a reader sees only the survivors and mistakes selection for edge.
SURVIVORSHIP_DENOMINATOR_CAVEAT = (
    "This is a trade-level estimate, not a system-level edge claim -- it says "
    "nothing about how many hypotheses were tested to find any strategy/card "
    "currently running. See hypotheses_tested/promoted below for that context."
)

# A pending approval this close to expiry surfaces as the one action item --
# roughly a third of the default 1800s TTL, chosen so there's still enough
# runway for a human to actually act once alerted.
EXPIRING_SOON_SECONDS = 600

# Below this many resolved real trades in the current calendar month, the
# moonshot-gap arithmetic is withheld entirely (no mean-of-a-tiny-sample
# masquerading as a trend). Deliberately smaller than metrics.py's
# MIN_MEANINGFUL_SAMPLE=30 -- that constant is a LIFETIME significance bar;
# this one just gates a monthly progress estimate, a different, weaker claim.
MIN_TRADES_FOR_MOONSHOT_ESTIMATE = 5

# The master build plan's own north star: beat the S&P, pursue >=10% MoM.
MOONSHOT_TARGET_MONTHLY_PCT = 10.0

UP_TO_N_LEARNED_SENTENCES = 3


def _market_condition(journal, settings) -> dict:
    rel_perf = build_relative_performance_report(journal, settings)
    return {
        "excess_return_pct": rel_perf.get("excess_return_pct"),
        "equity_total_return_pct": rel_perf.get("equity_total_return_pct"),
        "benchmark_total_return_pct": rel_perf.get("benchmark_total_return_pct"),
        "paired_trading_days": rel_perf.get("paired_trading_days"),
        "note": rel_perf.get("relative_return_note"),
        "caveat": rel_perf.get("caveat"),
    }


REGIME_CAVEAT = (
    "descriptive only -- no significance claimed (regime-sliced statistics "
    "gain q-values once PORT-1 lands)"
)


def _regime_header(journal) -> Optional[dict]:
    """Today's regime + how many consecutive days (under the SAME rules
    version) it's held, most-recent-first until the first mismatch. None
    when no regime_days row exists for today (REG-1 disabled, cold start, or
    a benchmark-spine gap) -- the brief omits the section entirely rather
    than fabricate a regime; see render_markdown."""
    from alphaos.regime.classifier import REGIME_RULES_V1

    today_row = journal.one(
        "SELECT * FROM regime_days WHERE market_date = ? AND regime_rules_version = ? "
        "ORDER BY id DESC LIMIT 1",
        (timeutils.market_date().isoformat(), REGIME_RULES_V1),
    )
    if not today_row:
        return None

    history = journal.query(
        "SELECT market_date, regime FROM regime_days WHERE regime_rules_version = ? "
        "AND market_date <= ? ORDER BY market_date DESC",
        (REGIME_RULES_V1, today_row["market_date"]),
    )
    streak = 0
    for row in history:
        if row["regime"] == today_row["regime"]:
            streak += 1
        else:
            break

    return {
        "regime": today_row["regime"],
        "rules_version": today_row["regime_rules_version"],
        "consecutive_days": streak,
        "caveat": REGIME_CAVEAT,
    }


def _text_archive_health(journal, since_sgt: str) -> Optional[dict]:
    """Docs fetched since local midnight, lifetime total, fetch errors summed
    across this window's completed pull run(s), and the oldest probable-
    trading-day gap in seen_at coverage since the archive's own first
    document -- a silent gap is TEXT-0's one unforgivable defect (its own
    module docstring: "EDGAR is never truly quiet"), so any weekday since the
    archive started with zero seen_at rows counts as a gap, same threshold
    the zero-doc job alert already uses. None when nothing has ever been
    archived yet (TEXT_ARCHIVE_ENABLED off, or on but not yet run) -- omitted
    rather than reporting a fabricated "oldest gap: none"; see render_markdown."""
    import json
    from datetime import date as _date
    from datetime import timedelta

    from alphaos.text_archive.service import is_probable_trading_day

    total = journal.count_rows("text_documents")
    if total == 0:
        return None

    docs_last_night = journal.count_rows("text_documents", "seen_at >= ?", (since_sgt,))

    fetch_errors_last_night = 0
    run_rows = journal.query(
        "SELECT result_summary_json FROM job_runs WHERE job_type = 'text_archive_pull' "
        "AND status = 'completed' AND finished_at_utc >= ?",
        (since_sgt,),
    )
    for row in run_rows:
        raw = row.get("result_summary_json")
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        pull_result = parsed.get("pull_result") or {}
        fetch_errors_last_night += pull_result.get("fetch_errors") or 0

    earliest_row = journal.one("SELECT MIN(seen_at) AS d FROM text_documents")
    seen_dates = {
        r["d"] for r in journal.query("SELECT DISTINCT substr(seen_at, 1, 10) AS d FROM text_documents")
    }
    oldest_gap = None
    if earliest_row and earliest_row.get("d"):
        d = _date.fromisoformat(earliest_row["d"][:10])
        yesterday = timeutils.market_date() - timedelta(days=1)
        while d <= yesterday:
            if is_probable_trading_day(d) and d.isoformat() not in seen_dates:
                oldest_gap = d.isoformat()
                break
            d += timedelta(days=1)

    return {
        "docs_last_night": docs_last_night,
        "total": total,
        "fetch_errors_last_night": fetch_errors_last_night,
        "oldest_gap": oldest_gap,
    }


def _atr_health(journal) -> Optional[dict]:
    """INSTR-1's deferred KIV, added 2026-07-09 (operator-directed follow-up,
    Fable strategy review): a persistent per-symbol ATR gap silently and
    permanently rejects every future PROPOSE for that symbol (NO_ATR_DATA)
    with no trace beyond a system_events row and a reason code -- this
    surfaces it where an operator actually looks. Coverage is checked
    against the CORE-BOOK universe only (DEFAULT_UNIVERSE), matching the ATR
    job's own scope (the shadow tier never reaches the evaluator, so has no
    use for ATR data -- see atr_service.py's module docstring). None when
    the job has never written a row yet (ATR capture not yet run) -- omitted
    rather than fabricating a reassuring 0/0, same pattern as
    text_archive_health/eval_health above."""
    from alphaos.data.atr import ATR_RULES_V1
    from alphaos.scanner.candidate_scanner import DEFAULT_UNIVERSE

    latest = journal.one(
        "SELECT MAX(market_date) AS d FROM atr_history WHERE rules_version = ?",
        (ATR_RULES_V1,),
    )
    as_of_date = latest.get("d") if latest else None
    if not as_of_date:
        return None

    covered_rows = journal.query(
        "SELECT DISTINCT symbol FROM atr_history WHERE market_date = ? AND rules_version = ?",
        (as_of_date, ATR_RULES_V1),
    )
    covered = {r["symbol"] for r in covered_rows}
    universe = set(DEFAULT_UNIVERSE)
    missing = sorted(universe - covered)

    return {
        "as_of_date": as_of_date,
        "n_covered": len(universe & covered),
        "n_universe": len(universe),
        "missing_symbols": missing,
    }


def _baseline_health(journal, settings) -> Optional[dict]:
    """BASELINE's own report, or None if zero shadow rows have EVER resolved
    yet (BASELINE has no scheduler job of its own -- it rides the existing
    outcomes_update cadence; a brand-new deploy or one still catching up on
    its first forward window legitimately has nothing to show). Omitted
    rather than fabricating a reassuring empty summary, same pattern as
    text_archive_health/eval_health above."""
    from alphaos.reports.baseline_report import build_baseline_report

    rep = build_baseline_report(journal, settings)
    return None if rep["n_shadow_resolved"] == 0 else rep


def _eval_health(journal) -> Optional[dict]:
    """The latest eval-harness run's summary, or None if no operator has
    ever run `alphaos eval` yet -- an expected, honest empty state (EVAL-1
    has no scheduler job; it's operator-invoked before a prompt/model
    change), never an error. Shows the LATEST run's status regardless of
    when it happened (a persistent state line, like the backup/regime
    surfaces), not a "did this run today" activity count."""
    from alphaos.reports.eval_report import build_eval_report

    rep = build_eval_report(journal)
    return None if rep["status"] == "no_runs_yet" else rep


def _fused_jobs(journal, settings) -> list[dict]:
    """Every job_type currently self-halted, independent of due-ness (a
    fused job stays fused outside its own due window too -- see
    cadence.is_fused's own docstring)."""
    fused = []
    for job_type in cadence.JobType:
        is_fused, reason, streak = cadence.is_fused(
            job_type.value, settings.scheduler_max_consecutive_failures, journal
        )
        if is_fused:
            fused.append({"job_type": job_type.value, "reason": reason, "streak": streak})
    return fused


def _needs_you(journal, digest: dict, fused_jobs: list[dict]) -> dict:
    pending = journal.open_proposals()
    for p in pending:
        p["seconds_remaining"] = seconds_remaining(p.get("proposal_expires_at_utc"))
    protection = digest.get("protection_status", {})
    return {
        "pending_approvals": pending,
        "pending_approval_count": len(pending),
        "open_incidents": protection.get("open_incidents", []),
        "open_incident_count": protection.get("open_incident_count", 0),
        "fused_jobs": fused_jobs,
    }


def _todays_activity(journal, since_sgt: str) -> dict:
    candidates_today = journal.count_rows("candidates", "created_at_utc >= ?", (since_sgt,))
    proposed_today = journal.count_rows(
        "trade_proposals", "status IN ('pending_approval', 'approved', 'filled') AND created_at_utc >= ?",
        (since_sgt,),
    )
    blocked_today = journal.count_rows(
        "trade_proposals", "status = 'blocked' AND created_at_utc >= ?", (since_sgt,),
    )
    rejected_today = journal.count_rows("rejected_candidates", "created_at_utc >= ?", (since_sgt,))
    return {
        "candidates_today": candidates_today,
        "proposed_today": proposed_today,
        "blocked_today": blocked_today,
        "rejected_today": rejected_today,
    }


def _best_candidate_today(journal, since_sgt: str) -> Optional[dict]:
    """Top TQS-scored PROPOSE-decision candidate today. None on an empty/
    quiet day -- absence is a valid, expected state, not an error."""
    row = journal.one(
        "SELECT c.candidate_id, c.symbol, c.interest_score, c.label_confidence, "
        "t.tqs_score, t.tqs_bucket, t.missing_components_json "
        "FROM candidates c JOIN tqs_scores t "
        "ON t.candidate_id = c.candidate_id AND t.source_type = 'candidate' "
        "WHERE c.label_decision = 'propose' AND c.created_at_utc >= ? "
        "ORDER BY t.tqs_score DESC LIMIT 1",
        (since_sgt,),
    )
    return row


def _learned_sentence(row: dict) -> str:
    """Plain, descriptive, non-judgmental -- reuses the reporting law's
    'aggregate tone, no moralizing' rule (specs doc §H.9)."""
    delta = row.get("delta_r")
    delta_str = f"{delta:+.2f}R" if delta is not None else "an unresolved ΔR"
    kind = (row.get("attribution_type") or "decision").replace("_", " ")
    return f"{row.get('symbol', '?')}: {kind} resolved, ΔR={delta_str}."


def _what_learned(journal, since_sgt: str, limit: int = UP_TO_N_LEARNED_SENTENCES) -> dict:
    rows = journal.query(
        "SELECT attribution_type, agent, delta_r, symbol FROM attribution_records "
        "WHERE resolved_status = 'resolved' AND is_mock = 0 AND resolved_at_utc >= ? "
        "ORDER BY resolved_at_utc DESC LIMIT ?",
        (since_sgt, limit),
    )
    return {
        "resolved_today": rows,
        "sentences": [_learned_sentence(r) for r in rows],
        "count": len(rows),
        "caveat": ATTRIBUTION_V2_CAVEAT,
    }


def _month_start_utc(now) -> str:
    st = timeutils.stamp(now)
    y, m = st.local_sgt[:4], st.local_sgt[5:7]
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt

    sgt_midnight = _dt(int(y), int(m), 1, tzinfo=ZoneInfo("Asia/Singapore"))
    return timeutils.to_iso(sgt_midnight.astimezone(ZoneInfo("UTC")))


def _moonshot_gap(journal, settings, now=None) -> dict:
    """Monthly: expectancy(R) x trades-this-month x risk-per-trade vs the
    10% MoM target, with the binding constraint named. Weekly (folded in
    here rather than a separate section): data-progress toward the floor
    this very estimate needs, so an operator sees it's temporary, not stuck."""
    now = now or timeutils.now_utc()
    month_start = _month_start_utc(now)
    rows = journal.query(
        "SELECT o.realized_r FROM trade_outcomes o "
        "JOIN positions p ON p.position_id = o.position_id "
        "WHERE (p.is_demo IS NULL OR p.is_demo = 0) AND o.realized_r IS NOT NULL "
        "AND o.created_at_utc >= ?",
        (month_start,),
    )
    r_values = [r["realized_r"] for r in rows if r.get("realized_r") is not None]
    n = len(r_values)

    # PORT-1 survivorship-denominator caveat -- computed over the FULL
    # preregistrations family regardless of this section's own trade-count
    # floor below; an empty/never-populated registry honestly reports
    # hypotheses_tested=0 (the mechanism, not yet armed by any writer --
    # PR12 is the future writer) rather than omitting the line.
    prereg_rows = journal.query("SELECT evaluated_at_utc, operator_approved_for_forward_test FROM preregistrations")
    prereg_family = preregistration_family_summary(prereg_rows)

    if n < MIN_TRADES_FOR_MOONSHOT_ESTIMATE:
        return {
            "status": "below_sample_floor",
            "trades_this_month": n,
            "floor": MIN_TRADES_FOR_MOONSHOT_ESTIMATE,
            "target_monthly_pct": MOONSHOT_TARGET_MONTHLY_PCT,
            "data_progress": f"{n}/{MIN_TRADES_FOR_MOONSHOT_ESTIMATE} resolved real trades this month",
            "note": (
                f"only {n} resolved real trade(s) this month "
                f"(< {MIN_TRADES_FOR_MOONSHOT_ESTIMATE}); implied-% arithmetic withheld until the floor is met"
            ),
            "preregistration_family": prereg_family,
            "survivorship_caveat": SURVIVORSHIP_DENOMINATOR_CAVEAT,
        }

    expectancy_r = sum(r_values) / n
    risk_per_trade_pct = settings.max_risk_per_trade_pct
    implied_monthly_pct = round(expectancy_r * n * risk_per_trade_pct * 100, 2)

    required_trades = None
    if expectancy_r > 0 and risk_per_trade_pct > 0:
        required_trades = round(
            MOONSHOT_TARGET_MONTHLY_PCT / (expectancy_r * risk_per_trade_pct * 100), 1
        )

    if expectancy_r <= 0:
        binding_constraint = "expectancy"
    elif required_trades is not None and n < required_trades:
        binding_constraint = "frequency"
    else:
        binding_constraint = "risk_per_trade"

    return {
        "status": "ok",
        "trades_this_month": n,
        "expectancy_r": round(expectancy_r, 4),
        "risk_per_trade_pct": risk_per_trade_pct,
        "implied_monthly_pct": implied_monthly_pct,
        "target_monthly_pct": MOONSHOT_TARGET_MONTHLY_PCT,
        "required_trades_per_month_at_current_expectancy": required_trades,
        "binding_constraint": binding_constraint,
        "data_progress": f"{n}/{MIN_TRADES_FOR_MOONSHOT_ESTIMATE} resolved real trades this month",
        "preregistration_family": prereg_family,
        "survivorship_caveat": SURVIVORSHIP_DENOMINATOR_CAVEAT,
    }


# Caps the EXIT_REVIEW symbol list in one_action -- with no cap, an
# unbounded join of every flagged symbol could exceed alerts.py's 1000-char
# truncation limit and cut off mid-ticker (audit-caught: 200 positions ->
# 3.8Kchar action string). The risk engine already bounds concurrent open
# positions well below this in practice; this is a defensive floor, not a
# response to a reachable real-world count.
MAX_SYMBOLS_IN_ONE_ACTION = 5


def _one_action(needs_you: dict, positions_health: list[dict], moonshot_gap: dict) -> str:
    """Priority order per spec: incident > fused job > expiring approval >
    EXIT_REVIEW position > below-floor data note > "nothing needs you"."""
    if needs_you["open_incident_count"] > 0:
        return f"{needs_you['open_incident_count']} open protection incident(s) -- review immediately."
    if needs_you["fused_jobs"]:
        names = ", ".join(j["job_type"] for j in needs_you["fused_jobs"])
        return f"Scheduler job(s) self-halted: {names} -- run `scheduler_run_job <job_type>` to clear."
    expiring = [
        p for p in needs_you["pending_approvals"]
        if p.get("seconds_remaining") is not None and 0 < p["seconds_remaining"] < EXPIRING_SOON_SECONDS
    ]
    if expiring:
        return f"{len(expiring)} pending approval(s) expiring within {EXPIRING_SOON_SECONDS // 60} minutes."
    exit_review = [p for p in positions_health if p["verdict"] == VERDICT_EXIT_REVIEW]
    if exit_review:
        shown = [p["symbol"] for p in exit_review[:MAX_SYMBOLS_IN_ONE_ACTION]]
        remaining = len(exit_review) - len(shown)
        syms = ", ".join(shown) + (f", +{remaining} more" if remaining > 0 else "")
        return f"{len(exit_review)} position(s) flagged EXIT_REVIEW ({syms}) -- a human should look at these."
    if moonshot_gap.get("status") == "below_sample_floor":
        return f"Nothing actionable yet -- still below the data floor ({moonshot_gap['data_progress']})."
    return "Nothing needs you right now."


def build_daily_brief(journal, settings, kill_switch) -> dict:
    """The composed daily brief. Every key below is always present.

    Note: build_daily_digest() (below) computes its own position_health
    summary, so assess_positions() runs twice here (once nested, once for
    this module's own full-detail positions_health section). Accepted
    deliberately -- open positions are few (the risk engine caps concurrent
    count) and this whole brief builds once a day, not in a hot loop;
    threading a precomputed rows list through build_daily_digest's signature
    to save a handful of snapshot fetches isn't worth the coupling.

    Audit-verified caveat (live mode only -- mock mode's deterministic
    per-day price makes this a non-issue, confirmed identical across both
    sweeps): MarketDataClient does not cache, so the two independent
    snapshot fetches, moments apart, can return different live prices. A
    position sitting exactly at a verdict boundary (e.g. current_r crossing
    -0.5) could show a different verdict in digest["position_health"]'s
    histogram than in this brief's own positions_health for the SAME position
    within the SAME brief build. Purely a reporting/observability wrinkle --
    nothing here gates a real decision on either count -- but real enough
    that digest and brief histograms should not be assumed to always agree
    to the row."""
    now = timeutils.now_utc()
    since_sgt = _start_of_today_sgt_utc(now)
    digest = build_daily_digest(journal, settings, kill_switch)
    market = MarketDataClient(settings, journal)

    positions_health = assess_positions(journal, settings, market)
    regime = _regime_header(journal)
    fused_jobs = _fused_jobs(journal, settings)
    needs_you = _needs_you(journal, digest, fused_jobs)
    todays_activity = _todays_activity(journal, since_sgt)
    text_archive_health = _text_archive_health(journal, since_sgt)
    best_candidate = _best_candidate_today(journal, since_sgt)
    what_learned = _what_learned(journal, since_sgt)
    moonshot_gap = _moonshot_gap(journal, settings, now)
    one_action = _one_action(needs_you, positions_health, moonshot_gap)

    # REG-1 acceptance criterion: the shadow arming-map scorer's first
    # (caveated) report surfaces in the brief. Import kept local -- avoids a
    # brief -> regime_arming_scorer -> ... import cycle risk and matches this
    # module's existing local-import style for report submodules.
    from alphaos.reports.regime_arming_scorer import build_regime_arming_report

    regime_arming = build_regime_arming_report(journal, settings)
    eval_health = _eval_health(journal)
    atr_health = _atr_health(journal)
    baseline_health = _baseline_health(journal, settings)

    return {
        "date_sgt": since_sgt[:10],
        "kill_switch_engaged": kill_switch.is_engaged(),
        "kill_switch_reason": kill_switch.reason(),
        "market_condition": _market_condition(journal, settings),
        "regime": regime,
        "regime_arming": regime_arming,
        "needs_you": needs_you,
        "positions_health": positions_health,
        "todays_activity": todays_activity,
        "text_archive_health": text_archive_health,
        "eval_health": eval_health,
        "atr_health": atr_health,
        "baseline_health": baseline_health,
        "best_candidate": best_candidate,
        "what_learned": what_learned,
        "moonshot_gap": moonshot_gap,
        "one_action": one_action,
    }


def render_markdown(brief: dict) -> str:
    lines = [
        f"# AlphaOS Daily Brief — {brief['date_sgt']}",
        "",
        f"## ▶ {brief['one_action']}",
        "",
    ]
    if brief["kill_switch_engaged"]:
        lines += [f"⚠️ **KILL SWITCH ENGAGED** — {brief['kill_switch_reason']}", ""]

    regime = brief.get("regime")
    if regime:
        lines += [
            f"## Regime: {regime['regime']} ({regime['rules_version']}, day "
            f"{regime['consecutive_days']} of current state)",
            f"> ⚠️ {regime['caveat']}",
            "",
        ]

    mc = brief["market_condition"]
    lines += ["## Market condition (vs S&P 500)"]
    if mc.get("excess_return_pct") is not None:
        lines.append(
            f"- Excess return: **{mc['excess_return_pct']:+.2f}%** "
            f"(paired {mc['paired_trading_days']} trading days)"
        )
    else:
        lines.append(f"- {mc.get('note', 'not yet measurable')}")
    lines += [f"> ⚠️ {mc['caveat']}", ""]

    ny = brief["needs_you"]
    lines += [
        "## Needs you",
        f"- Pending approvals: **{ny['pending_approval_count']}**",
        f"- Open protection incidents: **{ny['open_incident_count']}**",
        f"- Fused (self-halted) jobs: **{len(ny['fused_jobs'])}**",
        "",
    ]

    ph = brief["positions_health"]
    lines += [f"## Positions ({len(ph)} open)"]
    if ph:
        for p in ph:
            lines.append(
                f"- {p['symbol']} ({p['direction']}): {p['verdict']} — "
                f"R={p['current_r']}, thesis={p['thesis_status']}, "
                f"days_held={p['days_held']}/{p['max_holding_days']}"
            )
    else:
        lines.append("- (none)")
    lines.append("")

    ta = brief["todays_activity"]
    lines += [
        "## Today's activity",
        f"- Candidates: {ta['candidates_today']}  ·  Proposed: {ta['proposed_today']}  ·  "
        f"Blocked: {ta['blocked_today']}  ·  Rejected: {ta['rejected_today']}",
        "",
    ]

    tah = brief.get("text_archive_health")
    if tah:
        gap = tah["oldest_gap"] or "none"
        lines += [
            "## Text archive",
            f"- Text archive: +{tah['docs_last_night']} docs last night · "
            f"{tah['total']:,} total · {tah['fetch_errors_last_night']} fetch errors · "
            f"oldest gap: {gap}",
            "",
        ]

    ah = brief.get("atr_health")
    if ah:
        lines += ["## ATR coverage (core book)"]
        if ah["missing_symbols"]:
            lines.append(
                f"- As of {ah['as_of_date']}: {ah['n_covered']}/{ah['n_universe']} covered · "
                f"GAP: {', '.join(ah['missing_symbols'])} will fail-safe-reject any PROPOSE "
                f"(NO_ATR_DATA) until covered"
            )
        else:
            lines.append(f"- As of {ah['as_of_date']}: {ah['n_covered']}/{ah['n_universe']} covered")
        lines.append("")

    eh = brief.get("eval_health")
    if eh:
        from alphaos.reports.eval_report import render_markdown as _render_eval

        lines += [_render_eval(eh), ""]

    bc = brief["best_candidate"]
    lines += ["## Best candidate today"]
    lines.append(
        f"- {bc['symbol']}: TQS={bc['tqs_score']} ({bc['tqs_bucket']}), "
        f"interest={bc['interest_score']}, confidence={bc['label_confidence']}"
        if bc else "- (none today)"
    )
    lines.append("")

    wl = brief["what_learned"]
    lines += ["## What AlphaOS learned"]
    lines += [f"- {s}" for s in wl["sentences"]] or ["- (nothing newly resolved today)"]
    lines += [f"> ⚠️ {wl['caveat']}", ""]

    from alphaos.reports.regime_arming_scorer import render_markdown as _render_regime_arming

    lines += [_render_regime_arming(brief["regime_arming"]), ""]

    bh = brief.get("baseline_health")
    if bh:
        from alphaos.reports.baseline_report import render_markdown as _render_baseline

        lines += [_render_baseline(bh), ""]

    mg = brief["moonshot_gap"]
    lines += ["## Moonshot gap (10% MoM target)"]
    if mg["status"] == "ok":
        lines.append(
            f"- Implied monthly: **{mg['implied_monthly_pct']}%** vs target {mg['target_monthly_pct']}% "
            f"(expectancy={mg['expectancy_r']}R × {mg['trades_this_month']} trades × "
            f"{mg['risk_per_trade_pct']*100}% risk)"
        )
        lines.append(f"- Binding constraint: **{mg['binding_constraint']}**")
    else:
        lines.append(f"- {mg['note']}")
    lines.append(f"- Data progress: {mg['data_progress']}")
    pf = mg["preregistration_family"]
    lines.append(
        f"- hypotheses_tested={pf['hypotheses_tested']}, promoted={pf['promoted']} "
        f"(of {pf['hypotheses_registered']} registered)"
    )
    lines.append(f"> ⚠️ {mg['survivorship_caveat']}")

    return "\n".join(lines)


def render_compact(brief: dict) -> str:
    """A short, alert-friendly summary -- distinct from render_markdown's
    full report. Comfortably clears alerts.py's _MAX_TEXT_LENGTH truncation
    cap (1000 chars) even with the one_action title repeated as the ntfy
    push's own title -- audit-verified this actually depends on
    _one_action's own MAX_SYMBOLS_IN_ONE_ACTION cap; an earlier version
    joined an unbounded EXIT_REVIEW symbol list, which could exceed the cap
    and truncate mid-ticker at large enough position counts."""
    mc = brief["market_condition"]
    excess = (
        f"{mc['excess_return_pct']:+.2f}%" if mc.get("excess_return_pct") is not None else "n/a"
    )
    ny = brief["needs_you"]
    lines = [
        f"AlphaOS Daily Brief -- {brief['date_sgt']}",
        f"Action: {brief['one_action']}",
        f"vs S&P: {excess}  |  Positions: {len(brief['positions_health'])}  |  "
        f"Pending approvals: {ny['pending_approval_count']}",
        f"Open incidents: {ny['open_incident_count']}  |  Fused jobs: {len(ny['fused_jobs'])}",
    ]
    return "\n".join(lines)
