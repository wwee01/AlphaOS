"""AlphaOS v1 dashboard — single local Streamlit app, minimal tabs.

Tabs:
* Tonight — the daily human interface (UI-PR-A): one action, needs-you,
  today's activity, brief narrative, moonshot gap. Consumes daily_brief.py's
  dict, same as the `alphaos brief` CLI and the scheduler's digest alert.
* Positions — per-open-position health (R-ladder, thesis/verdict).
* Approval Center — OpenAI eval, optional Claude review, approve/reject,
  and a "Request Claude second opinion" button (disabled without an Anthropic key).
* Candidates / Proposals, Candidate Flow (decisions funnel + hindsight)
* Learning (PR-UI-B2) — TQS / Attribution / Hypotheses / Journal, four
  read-only sub-panels over PR7/PR8/PR12/HGEN-1/PR13 report data. Pure read;
  the operator-only MET/FAILED/WITHDRAWN ruling and hypothesis_accept/
  hypothesis_reject stay CLI-only actions (never a UI button here).
* Autonomy & Risk (PR-UI-B3) — the governance console: generated may/may-not
  panel + unattended-window exception, kill-switch explanation (control
  stays in the annunciator only), read-only hard limits, real-money lock,
  trading calendar. Pure read; see alphaos/reports/governance_report.py.
* Open Trades / Closed Trades
* System Health — mode, broker status, data freshness, kill switch.

A permanent annunciator strip (UI-PR-A item 1) renders above every tab: mode,
autonomy level, kill-switch state+control, scheduler heartbeat age, open R,
pending-approvals count (UI/UX doc §1.2, "the annunciator principle").

OPS-A: the approval surface must never be reachable off-machine. main()
refuses to render anything (no sidebar, no tabs, no action buttons -- the
whole script stops) unless the connection is loopback; see
_is_loopback_request(). Prefer `deploy/run_dashboard.sh` to launch this, or
`streamlit run alphaos/dashboard/streamlit_app.py` (the `.streamlit/
config.toml` in this repo pins the safe bind address as the default either
way). Remote access, if ever wanted, is an SSH tunnel
(`ssh -L 8502:127.0.0.1:8502 user@host`) -- never a LAN bind, never
port-forwarding.

This is intentionally not the full 15-tab UI. It never presents simulated
performance as real: everything is labelled paper/simulated.

PR-UI-B1 (styling only): a dark "cockpit instrument" visual theme
(console_theme.py + .streamlit/config.toml's [theme] section) is layered on
top of the above -- same tabs, same data, same gates, same read-only-render
discipline. Nothing in console_theme.py fetches data, writes anything, or
adds a code path; see that module's docstring for the full theme + the two
pure HTML-rendering helpers (R-ladder, TTL bar) it provides.
"""

from __future__ import annotations

from typing import Optional

import streamlit as st

from alphaos.ai.claude_reviewer import ClaudeUnavailable
from alphaos.config.settings import load_settings
from alphaos.constants import ProposalStatus
from alphaos.dashboard import console_theme
from alphaos.orchestrator import Orchestrator
from alphaos.reports.attribution import ATTRIBUTION_V2_CAVEAT
from alphaos.reports.daily_brief import build_daily_brief, render_markdown
from alphaos.reports.position_health import (
    VERDICT_ATTENTION,
    VERDICT_EXIT_REVIEW,
    VERDICT_HOLD,
    assess_positions,
)
from alphaos.reports.trade_packet import assemble_trade_packet
from alphaos.safety import KillSwitch
from alphaos.util import timeutils

# Static until PR15 (L3 autonomy promotion) actually lands -- see UI/UX doc
# §10 and the specs doc's PR15 skeleton. Not derived from settings because
# there is nothing to derive yet: v1 has exactly one level.
AUTONOMY_LEVEL_LABEL = "L1 — unattended cadence"

_LOOPBACK_ADDRESSES = {"127.0.0.1", "::1", "localhost"}


def _is_loopback_request() -> bool:
    """OPS-A: true only if THIS request is genuinely local.

    ``st.get_option("server.address")`` -- the server's own configured bind
    address -- is the AUTHORITATIVE signal: if it reports a loopback value,
    that is structurally sufficient on its own. The OS kernel itself refuses
    a non-loopback TCP connection to a loopback-bound socket before Streamlit
    (or Python) ever sees it, so a positively-loopback bind can't be
    contradicted by a real direct connection.

    ``st.context.ip_address`` (the actual connecting client's IP) is checked
    as a SECOND, defense-in-depth signal, but only actionable when it is
    actually populated with a value that positively disagrees with a
    loopback bind -- which should never happen for a genuine direct
    connection, so if it ever does, something is wrong enough to refuse.
    Its absence (``None``) must NOT veto an otherwise-safe bind: empirically,
    ``ip_address`` returns ``None`` even for a real, local Safari connection
    in this deployment (root-caused 2026-07-08 -- a genuine operator was
    locked out of their own dashboard by treating a merely-unpopulated
    secondary signal as equivalent to "unsafe"). Unknown-never-safe still
    applies to the AUTHORITATIVE signal (an unreadable/non-loopback bind
    address refuses outright); it does not extend to a redundant, empirically
    unreliable secondary check once the primary signal has already proven
    the connection safe.

    KNOWN RESIDUAL RISK (accepted, not a defect in this control): a reverse
    proxy in front of Streamlit would make Streamlit's own bind genuinely
    loopback (safe by this check) while actually being reachable more widely
    via the proxy in front of it. That is exactly why reverse proxies are
    forbidden here and an SSH tunnel -- whose forwarded connection
    legitimately originates from loopback on this host (the operator already
    authenticated via SSH) -- is the only sanctioned remote path.

    ``st.context`` is read via getattr so a Streamlit older than the pinned
    floor (``pyproject`` requires ``streamlit>=1.42``, where ``st.context.
    ip_address`` exists) fails CLOSED cleanly (refuse) rather than raising
    AttributeError -- belt-and-suspenders below the floor the pin already
    guarantees."""
    bind_addr = st.get_option("server.address")
    if bind_addr not in _LOOPBACK_ADDRESSES:
        # The bind itself is unsafe, unknown, or unreadable -- refuse
        # outright. This does NOT depend on what this specific connection's
        # ip_address claims: a misconfigured non-loopback bind should be
        # impossible to miss (even from the console), not silently "still
        # works for me" while quietly reachable from the LAN too.
        return False
    ctx = getattr(st, "context", None)
    ip = getattr(ctx, "ip_address", None) if ctx is not None else None
    if ip is not None and ip not in _LOOPBACK_ADDRESSES:
        return False
    return True


def get_orchestrator() -> Orchestrator:
    # Fresh per run keeps the SQLite connection on Streamlit's script thread.
    settings = load_settings()
    return Orchestrator(settings=settings)


def _heartbeat_age_seconds(journal) -> Optional[float]:
    """Read-only heartbeat staleness check for the annunciator. Deliberately
    NOT JobRunner.heartbeat_check() -- that method sends an ntfy alert when
    stale, which would fire on every dashboard page load; this is a pure read
    of the same job_runs row, safe to call unconditionally on every render."""
    last = journal.one(
        "SELECT finished_at_utc FROM job_runs WHERE status = 'completed' "
        "ORDER BY finished_at_utc DESC LIMIT 1"
    )
    if not last or not last.get("finished_at_utc"):
        return None
    return timeutils.age_seconds(last["finished_at_utc"])


def _format_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


def render_annunciator(orch: Orchestrator, positions_health: list[dict]) -> None:
    """Permanent top-of-page status strip (UI-PR-A item 1 / UI/UX doc §1.2):
    mode, autonomy level, kill-switch state+control, scheduler heartbeat age,
    open R, pending-approvals count. Never scrolls away -- called once at the
    top of main(), before the tabs. Read-only except the kill-switch buttons,
    which are the same explicit, logged actions render_sidebar used to expose."""
    s = orch.settings
    ks = KillSwitch()
    heartbeat_age = _heartbeat_age_seconds(orch.journal)
    # None current_r (no live price available) is excluded from the sum, not
    # treated as 0 -- unknown-never-zero. known_r stays None (not "0.0R") if
    # every open position is currently unmeasurable, so the strip reads as
    # "can't tell" rather than falsely "flat".
    r_values = [p["current_r"] for p in positions_health if p.get("current_r") is not None]
    total_r = round(sum(r_values), 2) if r_values else None
    unmeasurable = len(positions_health) - len(r_values)
    approvals_count = len(orch.journal.open_proposals())

    # A single wrapping status line, not a row of st.metric() boxes -- with 5+
    # fields the per-column width (especially once the sidebar takes its
    # share) is too narrow for st.metric()'s labels ("Heartbeat", "Approvals")
    # at common viewport widths, and unlike a metric box, text wraps instead
    # of silently truncating with an ellipsis (verified via preview at 791px
    # and desktop widths -- kept to a single markdown line + kill-switch
    # button as the only two widgets that actually need widget chrome).
    hb_label = "no runs yet" if heartbeat_age is None else f"{_format_age(heartbeat_age)} ago"
    r_label = "n/a" if total_r is None else f"{total_r:+.2f}R"
    if unmeasurable:
        r_label += f" ({unmeasurable} n/a)"

    col_mode, col_ks = st.columns([1, 2])
    with col_mode:
        # PR-UI-B1: keyed only so console_theme.CONSOLE_CSS can scope the
        # "annunciator badge" border/shape to this metric specifically
        # (Streamlit stamps a `st-key-<key>` CSS class on a keyed
        # container) -- same st.metric call, same value, no behavior change.
        with st.container(key="annunciator_mode_badge"):
            st.metric("Mode", s.mode.value.upper())
    with col_ks:
        # Same scoping trick for the kill-switch alert: outline-by-default /
        # filled-only-when-engaged badge styling (DESIGN.md "Annunciator
        # Badges") is scoped to just this container, so it never bleeds into
        # the st.error/st.success/st.warning calls elsewhere in the app
        # (e.g. System Health, which this PR does not touch).
        with st.container(key="annunciator_ks_badge"):
            if ks.is_engaged():
                st.error(f"🔴 KILL SWITCH ENGAGED — {ks.reason()}")
                if st.button("Release kill switch", key="annunciator_release_ks"):
                    ks.release()
                    st.rerun()
            else:
                st.success("🟢 Kill switch armed (not engaged)")
                if st.button("Engage kill switch", key="annunciator_engage_ks"):
                    ks.engage("dashboard")
                    st.rerun()
    st.markdown(
        f"**{AUTONOMY_LEVEL_LABEL}**  ·  Heartbeat: **{hb_label}**  ·  "
        f"Open R ({len(positions_health)} pos): **{r_label}**  ·  "
        f"Approvals pending: **{approvals_count}**"
    )
    st.caption("Real-money trading unreachable (structural, not a setting).")
    st.divider()


def render_sidebar(orch: Orchestrator) -> None:
    st.sidebar.title("AlphaOS")
    st.sidebar.caption("Paper-trading OS · real money disabled")
    s = orch.settings
    st.sidebar.markdown(
        f"**Mode:** `{s.mode.value}`  \n"
        f"**Approval:** `{s.approval_mode.value}`  \n"
        f"**Real trading:** `disabled`  \n"
        f"**DB:** `{s.db_path}`"
    )
    # Kill-switch state+control lives in the top annunciator now (UI-PR-A item
    # 1 / UI/UX doc §1.2 "the annunciator principle" -- it must never scroll
    # away, which a sidebar can).
    st.sidebar.divider()
    st.sidebar.caption("⚠️ Actions below WRITE to the connected ledger.")
    if st.sidebar.button("Run scan_once"):
        summ = orch.run_scan_once()
        st.sidebar.success(f"Scan: {summ.proposed} proposed, {summ.watch} watch, {summ.rejected} rejected")
    if st.sidebar.button("Run monitor_once"):
        res = orch.run_monitor_once()
        st.sidebar.success(f"Monitor: {len(res['exits'])} exit(s)")
    if st.sidebar.button("Generate daily report"):
        rep = orch.generate_daily_report()
        st.sidebar.success(f"Report {rep['report_date']} generated")
    if st.sidebar.button("Seed demo trade (simulated)"):
        d = orch.seed_demo()
        st.sidebar.info(f"Demo: {d['message']}")


def _format_seconds_remaining(seconds) -> str:
    """PR6: human-readable TTL countdown for the Approval Center. None (missing/
    unparseable expiry) reads as 'unknown' -- never as a blank/"fine" value."""
    if seconds is None:
        return "unknown"
    if seconds <= 0:
        return f"expired {int(abs(seconds))}s ago"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs}s"


def tab_tonight(orch: Orchestrator) -> None:
    """UI-PR-A item 2: the daily human interface (UI/UX doc §5 wireframe,
    blocks ①②③④⑤⑥⑦ — ⑥ moonshot gap included since PR11 shipped the
    arithmetic). Consumes build_daily_brief() -- the exact dict the
    `alphaos brief` CLI and the scheduler's digest alert already use. Every
    section is always present; the empty/quiet state (⑦) is first-class, not
    an afterthought (UI/UX doc §1.5). Read-only."""
    st.subheader("Tonight")
    brief = build_daily_brief(orch.journal, orch.settings, KillSwitch())

    st.markdown(f"### ▶ {brief['one_action']}")
    if brief["kill_switch_engaged"]:
        st.error(f"🔴 KILL SWITCH ENGAGED — {brief['kill_switch_reason']}")

    ny = brief["needs_you"]
    ph = brief["positions_health"]
    exit_review = [p for p in ph if p["verdict"] == VERDICT_EXIT_REVIEW]
    quiet = (
        ny["pending_approval_count"] == 0 and ny["open_incident_count"] == 0
        and not ny["fused_jobs"] and not exit_review
    )
    if quiet:
        st.success("✓ Nothing needs you right now.")
    else:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(console_theme.render_section_label("② Needs you"), unsafe_allow_html=True)
            for p in ny["pending_approvals"]:
                remaining = _format_seconds_remaining(p.get("seconds_remaining"))
                st.write(f"- {p.get('symbol')} proposal — TTL {remaining} (see Approval Center)")
            for inc in ny["open_incidents"]:
                st.write(f"- ⚠️ Open incident: {inc.get('symbol', '?')} — {inc.get('protection_status', '?')}")
            for fj in ny["fused_jobs"]:
                st.write(f"- Fused job: `{fj['job_type']}` ({fj['reason']}, {fj['streak']} consecutive failures)")
            for p in exit_review:
                st.write(f"- {p['symbol']} position EXIT_REVIEW — human decision required (see Positions)")
            if not (ny["pending_approvals"] or ny["open_incidents"] or ny["fused_jobs"] or exit_review):
                st.write("(nothing here)")
        with c2:
            st.markdown(console_theme.render_section_label("③ Open risk now"), unsafe_allow_html=True)
            r_values = [p["current_r"] for p in ph if p.get("current_r") is not None]
            if ph:
                r_label = f"{round(sum(r_values), 2):+.2f}R total" if r_values else "R n/a"
                st.write(f"{len(ph)} position(s) · {r_label}")
                worst = min((p for p in ph if p.get("current_r") is not None),
                           key=lambda p: p["current_r"], default=None)
                if worst is not None:
                    st.write(f"worst: {worst['symbol']} {worst['current_r']:+.2f}R")
            else:
                st.write("No open positions.")

    st.divider()
    ta = brief["todays_activity"]
    st.markdown(console_theme.render_section_label("④ Today's machine activity"), unsafe_allow_html=True)
    st.write(
        f"Candidates: {ta['candidates_today']} · Proposed: {ta['proposed_today']} · "
        f"Blocked: {ta['blocked_today']} · Rejected: {ta['rejected_today']}"
    )

    st.divider()
    st.markdown(console_theme.render_section_label("⑤ Tonight's brief"), unsafe_allow_html=True)
    mc = brief["market_condition"]
    if mc.get("excess_return_pct") is not None:
        st.write(
            f"Market: excess return **{mc['excess_return_pct']:+.2f}%** vs S&P "
            f"(paired {mc['paired_trading_days']} trading days)"
        )
    else:
        st.write(f"Market: {mc.get('note', 'not yet measurable')}")
    st.caption(f"⚠️ {mc['caveat']}")

    bc = brief["best_candidate"]
    if bc:
        st.write(
            f"Best candidate today: **{bc['symbol']}** — TQS {bc['tqs_score']} ({bc['tqs_bucket']}), "
            f"interest {bc['interest_score']}, confidence {bc['label_confidence']}"
        )
    else:
        st.write("Best candidate today: (none)")

    wl = brief["what_learned"]
    st.write(f"Learned today ({wl['total_resolved_today']} resolved):")
    if wl["sentences"]:
        for sentence in wl["sentences"]:
            st.write(f"- {sentence}")
    else:
        st.write("- (nothing newly resolved today)")
    st.caption(f"⚠️ {wl['caveat']}")

    st.divider()
    st.markdown(console_theme.render_section_label("⑥ Moonshot gap (10% MoM target)"), unsafe_allow_html=True)
    mg = brief["moonshot_gap"]
    if mg["status"] == "ok":
        st.write(
            f"Implied monthly: **{mg['implied_monthly_pct']}%** vs target {mg['target_monthly_pct']}% "
            f"(expectancy {mg['expectancy_r']}R × {mg['trades_this_month']} trades × "
            f"{mg['risk_per_trade_pct'] * 100:.2f}% risk/trade)"
        )
        st.write(f"Binding constraint: **{mg['binding_constraint']}**")
    else:
        st.write(mg["note"])
    st.caption(mg["data_progress"])

    with st.expander("Full brief (same content as the `alphaos brief` CLI / digest alert)"):
        st.markdown(render_markdown(brief))


_VERDICT_ICON = {VERDICT_HOLD: "🟢", VERDICT_ATTENTION: "🟡", VERDICT_EXIT_REVIEW: "🔴"}


def tab_positions_health(positions_health: list[dict]) -> None:
    """UI-PR-A item 3 / PR-UI-B1: per-open-position health cards with an
    HTML/CSS R-ladder (UI/UX doc §8 wireframe / §12 item 3 -- still no
    charting library, console_theme.render_r_ladder() is text/HTML/CSS only).
    EXIT_REVIEW is a human decision flag ONLY: AlphaOS never auto-exits on a
    health verdict (position_health.py's own invariant).

    PR-UI-B1 note: the ladder's stop_r/target_r are derived here from the
    SAME two numbers already shown today (distance_to_stop_r,
    distance_to_target_r) -- current_r minus/plus the distance -- not from
    any new query or computation. When either distance is unavailable, this
    falls back to the exact plain-text line PR-UI-A already rendered, so no
    information that used to be visible is ever lost, only re-presented."""
    st.subheader("Positions")
    st.caption(
        "Per-open-position thesis validity, reusing position_manager's R math. "
        "EXIT_REVIEW is a human decision flag -- AlphaOS never auto-exits on a "
        "health verdict."
    )
    if not positions_health:
        st.info("No open positions.")
        return

    for p in positions_health:
        icon = _VERDICT_ICON.get(p["verdict"], "⚪")
        # PR-UI-B1: `key=` is ONLY here so console_theme.CONSOLE_CSS can give
        # this specific bordered container the ported #27272a module-border
        # color via its documented `st-key-<key>` CSS class -- verified
        # against the running app that Streamlit's own border=True styling
        # has no distinct data-testid/inline-style hook to target otherwise
        # (bordered and unbordered st.container()s share the same
        # data-testid="stVerticalBlock"). Same border=True call, same cards,
        # same data -- position_id keeps the key unique per card.
        with st.container(border=True, key=f"poscard_{p['position_id']}"):
            st.markdown(f"**{icon} {p['symbol']}** · {p['direction']} · verdict: **{p['verdict']}**")
            if p["current_r"] is not None:
                stop_r = (
                    p["current_r"] - p["distance_to_stop_r"]
                    if p["distance_to_stop_r"] is not None else None
                )
                target_r = (
                    p["current_r"] + p["distance_to_target_r"]
                    if p["distance_to_target_r"] is not None else None
                )
                if stop_r is not None and target_r is not None:
                    st.markdown(
                        console_theme.render_r_ladder(
                            stop_r=stop_r, entry_r=0.0, current_r=p["current_r"], target_r=target_r,
                        ),
                        unsafe_allow_html=True,
                    )
                    st.caption(
                        f"distance to stop: {p['distance_to_stop_r']}R · "
                        f"to target: {p['distance_to_target_r']}R"
                    )
                else:
                    st.write(
                        f"now **{p['current_r']:+.2f}R** "
                        f"(distance to stop: {p['distance_to_stop_r']}, to target: {p['distance_to_target_r']})"
                    )
            else:
                st.write("R: unavailable (no live price, or a degenerate risk basis)")
            st.write(f"thesis: **{p['thesis_status']}**")
            if p["verdict"] == VERDICT_EXIT_REVIEW:
                st.warning("Human decision required — AlphaOS does not auto-exit on health verdicts.")
            st.caption(
                f"protection: {p['protection_status']} · freshness: {p['freshness_status']} · "
                # HOLD-1: trading days held is PRIMARY -- max_holding_days now
                # means trading days (matches the replay engine). Calendar
                # age stays visible as secondary detail.
                f"trading days held: {p['trading_days_held']}/{p['max_holding_days']} "
                f"(calendar age: {p['days_held']}d) · "
                f"earnings in hold window: {'yes' if p['earnings_within_hold_window'] else 'no'}"
            )


def tab_approval_center(orch: Orchestrator) -> None:
    """The actionable approval queue. Listing is read-only; ledger writes happen
    only when the user clicks Approve/Reject (each is an explicit action)."""
    st.subheader("Approval Center")
    st.caption(
        "Open proposals awaiting your decision. Approving RE-RUNS freshness, "
        "price-drift, spread and risk checks at approval time before any paper order "
        "is created. Manual approval is required — nothing here is auto-submitted."
    )
    views = orch.list_open_proposals()
    if not views:
        st.info("No open proposals. Run a scan from the sidebar to generate proposals.")
        return

    # UI-PR-A item 4: soonest-to-expire first -- the whole point of a TTL is
    # that it's a deadline, so the queue should read like one. A proposal
    # with an unknown/unparseable expiry sorts LAST, not first: we can't
    # claim it's urgent just because we can't measure it (unknown-never-zero
    # extends to "unknown-never-most-urgent" here).
    views = sorted(
        views,
        key=lambda v: v["proposal_seconds_remaining"]
        if v["proposal_seconds_remaining"] is not None else float("inf"),
    )

    st.dataframe(
        [
            {
                "proposal_id": v["proposal_id"], "trade_id": v["trade_id"], "symbol": v["symbol"],
                "side": v["side"], "entry": v["entry"], "stop": v["stop"], "target": v["target"],
                "qty": v["qty"], "R:R": v["reward_risk"], "risk_$": v["risk_amount"],
                "last_freshness": v["last_known_freshness"], "generated_sgt": v["generated_at_sgt"],
                "expires_in": _format_seconds_remaining(v["proposal_seconds_remaining"]),
                "stale": v["proposal_is_stale"],
                # PR7 TQS v0: DISPLAY ONLY -- a shadow measurement signal, never
                # read by approval/risk/execution logic (see alphaos/tqs/). Score
                # and confidence shown paired, never separated (UI/UX doc §9).
                "tqs": v["tqs_score"], "tqs_bucket": v["tqs_bucket"],
                "tqs_confidence": v["tqs_data_confidence"],
            }
            for v in views
        ],
        width="stretch",
    )
    st.divider()
    for v in views:
        pid = v["proposal_id"]
        stale_flag = " ⚠️ STALE (TTL exceeded)" if v["proposal_is_stale"] else ""
        with st.expander(
            f"{v['symbol']} · {v['side']} · qty {v['qty']} · R:R {v['reward_risk']} · "
            f"expires in {_format_seconds_remaining(v['proposal_seconds_remaining'])} · {pid}{stale_flag}"
        ):
            # PR-UI-B1: the same TTL already named in the expander title
            # above, re-presented as a bar -- seconds_remaining/
            # proposal_ttl_seconds are the identical fields the dataframe
            # column and the title's "expires in ..." text already use;
            # `label` is that same _format_seconds_remaining() text, so the
            # words shown are byte-identical, only their layout changes.
            st.markdown(
                console_theme.render_ttl_bar(
                    seconds_remaining=v["proposal_seconds_remaining"],
                    total_ttl_seconds=v["proposal_ttl_seconds"],
                    label=_format_seconds_remaining(v["proposal_seconds_remaining"]),
                ),
                unsafe_allow_html=True,
            )
            if v["proposal_is_stale"]:
                st.warning(
                    "This proposal's TTL has expired — approval will be rejected. "
                    "Run a fresh scan to get a current proposal for this symbol."
                )
            # UI-PR-A item 4: the exit plan verbatim, ahead of the raw field
            # dump -- asymmetric friction means the thing you're about to
            # commit to should be the most visible thing before you click.
            invalidation = v.get("invalidation_reason")
            st.markdown(
                f"**Exit plan:** stop `{v['stop']}` · target `{v['target']}`  \n"
                f"**Invalidation:** {invalidation if invalidation else '(not set on this proposal)'}"
            )
            st.write(
                {
                    "trade_id": v["trade_id"], "candidate_id": v["candidate_id"],
                    "entry": v["entry"], "stop": v["stop"], "target": v["target"],
                    "risk_per_share": v["risk_per_share"], "risk_amount": v["risk_amount"],
                    "expected_r": v["expected_r"], "requires_margin": v["requires_margin"],
                    "last_known_freshness": v["last_known_freshness"],
                    "generated_at_utc": v["generated_at_utc"],
                    "proposal_ttl_seconds": v["proposal_ttl_seconds"],
                    "proposal_expires_at_utc": v["proposal_expires_at_utc"],
                    "expires_in": _format_seconds_remaining(v["proposal_seconds_remaining"]),
                    # PR7 TQS v0 -- shadow measurement signal, display only.
                    "tqs_score": v["tqs_score"], "tqs_bucket": v["tqs_bucket"],
                    "tqs_data_confidence": v["tqs_data_confidence"],
                }
            )
            approve_margin = False
            if v["requires_margin"]:
                approve_margin = st.checkbox(
                    "Explicitly approve margin/borrow for this short", key=f"acmgn_{pid}"
                )
            c1, c2 = st.columns(2)
            if c1.button("Approve + submit (paper)", key=f"acap_{pid}"):
                ok, msg = orch.approve_proposal(pid, approve_margin=approve_margin)
                (st.success if ok else st.error)(msg)
                st.rerun()
            if c2.button("Reject", key=f"acrj_{pid}"):
                ok, msg = orch.reject_proposal(pid)
                st.warning(msg)
                st.rerun()


def tab_candidates(orch: Orchestrator) -> None:
    st.subheader("Candidates / Proposals")
    st.caption("News-confirmed momentum playbook. No verifiable news ⇒ watch/reject.")
    cands = orch.journal.recent_candidates(100)
    if not cands:
        st.info("No candidates yet — run scan_once from the sidebar.")
        return
    claude_enabled = orch.claude.available
    if not claude_enabled:
        st.caption("ℹ️ Claude second-opinion disabled (no ANTHROPIC_API_KEY).")

    for c in cands:
        ev = orch.journal.evaluation_for_candidate(c["candidate_id"])
        header = f"{c['symbol']} · {c.get('status')} · news={c.get('news_status')}"
        with st.expander(header):
            st.write({k: c[k] for k in ("direction", "momentum_score", "rel_strength", "unusual_volume") if k in c})
            if ev:
                st.markdown(
                    f"**OpenAI:** `{ev['decision']}` · conf {ev.get('confidence')} · "
                    f"entry {ev.get('entry')} / stop {ev.get('stop')} / target {ev.get('target')}"
                )
                st.caption(ev.get("reasoning_summary") or "")
            # PR7 TQS v0 -- shadow measurement signal, display only (never read
            # by any decision above). Absent when scoring is disabled/hasn't
            # run yet for this candidate.
            tqs_row = orch.journal.one(
                "SELECT tqs_score, tqs_bucket, data_confidence FROM tqs_scores "
                "WHERE candidate_id = ? AND source_type = 'candidate' ORDER BY id DESC LIMIT 1",
                (c["candidate_id"],),
            )
            if tqs_row:
                st.caption(
                    f"TQS (shadow): {tqs_row['tqs_score']} · {tqs_row['tqs_bucket']} "
                    f"· confidence {tqs_row['data_confidence']}"
                )
            review = orch.journal.claude_review_for_candidate(c["candidate_id"])
            if review:
                st.markdown(f"**Claude (2nd opinion):** `{review['verdict']}` — {review.get('reasoning')}")
            if st.button("Request Claude second opinion", key=f"clr_{c['candidate_id']}", disabled=not claude_enabled):
                try:
                    r = orch.request_claude_review(c["candidate_id"])
                    st.success(f"Claude verdict: {r.verdict}")
                except (ClaudeUnavailable, ValueError) as exc:
                    st.error(str(exc))

            prop = orch.journal.one(
                "SELECT * FROM trade_proposals WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
                (c["candidate_id"],),
            )
            if prop and prop["status"] in ProposalStatus.approvable():
                from alphaos.proposals import is_expired as _proposal_is_expired
                from alphaos.proposals import seconds_remaining as _proposal_seconds_remaining

                expires_in = _format_seconds_remaining(
                    _proposal_seconds_remaining(prop.get("proposal_expires_at_utc")))
                st.markdown(f"**Proposal** `{prop['proposal_id']}` — qty {prop['qty']} · status {prop['status']} "
                           f"· expires in {expires_in}")
                if _proposal_is_expired(prop.get("proposal_expires_at_utc")):
                    st.warning("This proposal's TTL has expired — approval will be rejected. "
                              "Run a fresh scan to get a current proposal for this symbol.")
                approve_margin = False
                if prop.get("requires_margin"):
                    approve_margin = st.checkbox(
                        "Explicitly approve margin/borrow for this short", key=f"mgn_{prop['proposal_id']}"
                    )
                col1, col2 = st.columns(2)
                if col1.button("Approve + submit (paper)", key=f"ap_{prop['proposal_id']}"):
                    ok, msg = orch.approve_proposal(prop["proposal_id"], approve_margin=approve_margin)
                    (st.success if ok else st.error)(msg)
                if col2.button("Reject", key=f"rj_{prop['proposal_id']}"):
                    ok, msg = orch.reject_proposal(prop["proposal_id"])
                    st.warning(msg)


def tab_open_trades(orch: Orchestrator) -> None:
    st.subheader("Open Trades (paper, simulated)")
    rows = orch.journal.open_positions()
    if rows:
        st.dataframe(rows, width="stretch")
    else:
        st.info("No open positions.")


def tab_closed_trades(orch: Orchestrator) -> None:
    st.subheader("Closed Trades (paper) — net of modelled costs")
    rows = orch.journal.closed_outcomes(500)
    if not rows:
        st.info("No closed trades yet.")
        return
    from alphaos.reports.metrics import compute_metrics

    m = compute_metrics(rows)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Net P&L", m["net_pnl"])
    c2.metric("Win rate", m["win_rate"])
    c3.metric("Expectancy/trade", m["expectancy"])
    c4.metric("Profit factor", m["profit_factor"])
    c1.metric("Gross P&L", m["gross_pnl"])
    c2.metric("Total costs", m["total_costs"])
    c3.metric("Max drawdown", m["max_drawdown"])
    c4.metric("Same-day rate", m["same_day_exit_rate"])
    if m["small_sample"]:
        st.caption(f"⚠️ {m['note']}")
    st.dataframe(rows, width="stretch")


def tab_system_health(orch: Orchestrator) -> None:
    st.subheader("System Health")
    s = orch.settings
    health = orch.system_health()
    checks = orch.settings.validate_startup()

    st.caption(f"Playbook: **{health['playbook']}**")
    c1, c2, c3 = st.columns(3)
    c1.metric("Mode", s.mode.value)
    c2.metric("Approval", health["manual_approval"])
    c3.metric("Real-money trading", health["real_money_trading"])
    c1.metric("Market data", f"{health['market_data_provider']}/{health['market_data_feed']}")
    c2.metric("Market data mode", health["market_data_mode"])
    c3.metric("Data freshness", health["market_data_freshness"])
    c1.metric("Execution", health["execution_provider"])
    c2.metric("Kill switch", health["kill_switch"])
    c3.metric("Open positions", health["open_positions"])

    st.markdown("#### Layers (mocked / deferred / disabled / live)")
    st.json(
        {
            "AI primary": health["ai_primary"],
            "AI reviewer": health["ai_reviewer"],
            "Market data provider": health["market_data_provider"],
            "Market data feed": f"{health['market_data_feed']} ({health['market_data_limited']})",
            "Market data mode": health["market_data_mode"],
            "News provider": health["news_provider"],
            "Benzinga": health["benzinga"],
            "Web scraper": health["web_scraper"],
            "Massive": health["massive"],
            "Execution provider": health["execution_provider"],
            "Real Alpaca paper execution": health["real_alpaca_paper_execution"],
            "Real-money trading": health["real_money_trading"],
            "Manual approval": health["manual_approval"],
        }
    )

    st.markdown("#### AI labeller health")
    lf = health.get("labeller_failsafe", {})
    lc1, lc2, lc3 = st.columns(3)
    lc1.metric("Labels (recent)", lf.get("total", 0))
    lc2.metric("Fail-safe", lf.get("fail_safe", 0))
    lc3.metric("Fail-safe rate", f"{round((lf.get('fail_safe_rate') or 0) * 100)}%")
    if lf.get("message"):
        (st.error if lf.get("level") == "critical" else st.warning)(lf["message"])
    else:
        st.caption(f"Label sources (last {lf.get('total', 0)}): {lf.get('by_source', {})}")
    if lf.get("by_failsafe_reason"):
        st.caption(f"Fail-safe reasons: {lf['by_failsafe_reason']}")

    st.markdown("#### Protection watchdog")
    pw = health.get("protection_watchdog", {})
    pc1, pc2, pc3 = st.columns(3)
    pc1.metric("Broker-managed positions", pw.get("checked", 0))
    pc2.metric("Unprotected/mismatched", pw.get("unprotected", 0) + pw.get("closed_mismatch", 0))
    pc3.metric("Open incidents", pw.get("open_incident_count", 0))
    if pw.get("blocking"):
        st.error(f"NEW ENTRIES BLOCKED: {pw.get('blocking_detail')}")
    elif pw.get("degraded", 0) > 0:
        st.warning(f"{pw['degraded']} position(s) degraded (target leg missing, stop still live) — not blocking.")
    else:
        st.success(pw.get("summary_label", "all protected"))
    if pw.get("open_incidents"):
        st.dataframe(
            [{k: i.get(k) for k in ("check_id", "symbol", "protection_status", "detail", "created_at_utc")}
             for i in pw["open_incidents"]],
            width="stretch",
        )

    st.markdown("#### Startup safety checks")
    for c in checks:
        (st.success if c.ok else st.error)(f"{c.name}: {c.detail}")

    st.markdown("#### Recent data freshness")
    snaps = orch.journal.query(
        "SELECT symbol, provider, freshness_status, is_usable, data_delay_seconds, source_timestamp "
        "FROM price_snapshots ORDER BY id DESC LIMIT 20"
    )
    if snaps:
        st.dataframe(snaps, width="stretch")

    st.markdown("#### Recent system events")
    events = orch.journal.recent_system_events(30)
    if events:
        st.dataframe(
            [{k: e[k] for k in ("created_at_utc", "severity", "category", "message")} for e in events],
            width="stretch",
        )


def tab_trade_packet(orch: Orchestrator) -> None:
    st.subheader("Trade Packet (audit)")
    st.caption("Assemble the full lifecycle for a candidate_id or trade_id (read-only).")
    cands = orch.journal.recent_candidates(100)
    options = [c["candidate_id"] for c in cands]
    chosen = st.selectbox("Recent candidate_id", options) if options else None
    manual = st.text_input("…or paste a candidate_id / trade_id").strip()
    anchor = manual or chosen
    if not anchor:
        st.info("No candidates yet — run scan_once from the sidebar.")
        return
    kwargs = {"trade_id": anchor} if anchor.startswith("trade_") else {"candidate_id": anchor}
    st.json(assemble_trade_packet(orch.journal, **kwargs))


def tab_scan_batches(orch: Orchestrator) -> None:
    st.subheader("Scan Batches")
    rows = orch.journal.recent_scan_batches(50)
    if rows:
        st.dataframe(rows, width="stretch")
    else:
        st.info("No scan batches yet — run scan_once from the sidebar.")


def tab_scheduler_runs(orch: Orchestrator) -> None:
    st.subheader("Scheduler Runs")
    rows = orch.journal.recent_scheduler_runs(50)
    if rows:
        st.dataframe(rows, width="stretch")
    else:
        st.info("No scheduler runs recorded yet.")


def tab_system_events(orch: Orchestrator) -> None:
    st.subheader("System Events")
    rows = orch.journal.recent_system_events(200)
    if rows:
        st.dataframe(rows, width="stretch")
    else:
        st.info("No system events yet.")


def _hindsight_cell(attr: "Optional[dict]") -> str:
    """UI-PR-A item 5: per-row hindsight for the decisions funnel. No
    attribution row yet, or one that exists but hasn't resolved, both read as
    'pending' -- the UI never backfills a fabricated 0 for an unresolved
    replay (unknown-never-zero, same posture as position_health.py).

    A mock ΔR (is_mock=1 on the attribution row -- happens in mock mode, never
    in live paper where settings.is_mock is False) is tagged '(mock)' so a
    simulated learning is never styled identically to a real one (UI/UX doc
    §1.4 evidence-state honesty / §9 'mock rows carry a MOCK watermark'). This
    is the ΔR-surface analogue of PR11's daily_brief filtering is_mock=0 out
    of its 'learned today' sentences -- here the candidate row is shown either
    way, so the ΔR is tagged rather than hidden."""
    if not attr or attr.get("resolved_status") != "resolved":
        return "pending"
    delta = attr.get("delta_r")
    if delta is None:
        return "pending"
    suffix = " (mock)" if attr.get("is_mock") else ""
    return f"{delta:+.2f}R{suffix}"


def tab_candidate_flow(orch: Orchestrator) -> None:
    """Read-only Roadmap 2.3 candidate flow: labels summary + proposed / watch /
    rejected / blocked sections. All reads — render writes nothing to the ledger."""
    st.subheader("Candidate Flow — interest scan → AI labels")
    st.caption(
        "The deterministic interest scanner shortlists; the AI labels the shortlist. "
        "Labels are ADVISORY (downgrade-only) and never bypass freshness / spread / "
        "risk / approval gates. Nothing here executes a trade."
    )
    j = orch.journal

    def _rows(cands):
        return [
            {
                "symbol": c.get("symbol"), "primary_label": c.get("primary_label"),
                "label_decision": c.get("label_decision"), "confidence": c.get("label_confidence"),
                "interest": c.get("interest_score"), "rank": c.get("interest_rank"),
                "catalyst": c.get("catalyst_status"), "catalyst_type": c.get("catalyst_type"),
                "last30days": c.get("last30days_status"), "sentiment": c.get("sentiment_label"),
                "polarity": c.get("polarity_label"), "narrative": c.get("narrative_driver_type"),
                "arming": c.get("arming_classification"), "decision_adj": c.get("decision_adjustment"),
                "review?": "yes" if c.get("label_review_required") else "",
                "status": c.get("status"), "reason": c.get("shortlist_reason"),
            }
            for c in cands
        ]

    st.markdown(console_theme.render_section_label("Catalyst enrichment summary"), unsafe_allow_html=True)
    st.caption("Official catalyst context (Roadmap 2.4) — advisory only; never bypasses gates or approval.")
    cs = j.catalyst_summary()
    cc1, cc2 = st.columns(2)
    with cc1:
        st.caption("by catalyst status")
        if cs["by_status"]:
            st.dataframe(cs["by_status"], width="stretch")
        else:
            st.info("No catalyst enrichment yet.")
    with cc2:
        st.caption("by catalyst type")
        if cs["by_type"]:
            st.dataframe(cs["by_type"], width="stretch")
        else:
            st.info("—")
    with st.expander("Catalyst detail (latest enriched candidates)"):
        cats = j.recent_catalysts(100)
        if cats:
            st.dataframe(
                [
                    {k: x.get(k) for k in (
                        "symbol", "catalyst_status", "catalyst_type", "catalyst_confidence",
                        "catalyst_age_minutes", "catalyst_suggested_label", "label_review_required",
                        "enrichment_status", "catalyst_summary",
                    )}
                    for x in cats
                ],
                width="stretch",
            )
        else:
            st.info("None.")

    st.markdown(console_theme.render_section_label("last30days research summary"), unsafe_allow_html=True)
    st.caption(
        "Recent community narrative (Roadmap 2.5) — SEPARATE keyless social/research "
        "layer; advisory CONTEXT only, never bypasses gates/approval and never executes. "
        "'skipped_budget_cap' = eligible but outside the per-scan cap (NOT 'no narrative')."
    )
    # Map raw status -> friendly bucket so a budget-skipped candidate is never shown
    # as "no clear narrative". Buckets: enriched / no_clear_narrative / stale /
    # skipped_budget_cap / unavailable / error.
    _L30_LABEL = {
        "available": "enriched", "none_found": "no_clear_narrative", "stale": "stale",
        "skipped_budget_cap": "skipped_budget_cap", "unavailable": "unavailable",
        "error": "error", "disabled": "disabled",
    }
    l30s = j.last30days_summary()
    if l30s["by_status"]:
        st.dataframe(
            [{"last30days": _L30_LABEL.get(r["status"], r["status"]), "n": r["n"]}
             for r in l30s["by_status"]],
            width="stretch",
        )
    else:
        st.info("No last30days enrichment yet (disabled by default — set LAST30DAYS_ENABLED=true).")
    with st.expander("last30days detail (latest enriched / skipped candidates)"):
        l30rows = j.recent_last30days(100)
        if l30rows:
            st.dataframe(
                [
                    {**{k: x.get(k) for k in (
                        "symbol", "last30days_status", "sentiment_label", "cluster_count",
                        "item_count", "interest_rank", "provider", "enrichment_status",
                        "reason", "summary",
                    )}, "last30days": _L30_LABEL.get(x.get("last30days_status"), x.get("last30days_status"))}
                    for x in l30rows
                ],
                width="stretch",
            )
        else:
            st.info("None.")

    st.markdown(console_theme.render_section_label("last30days narrative polarity"), unsafe_allow_html=True)
    st.caption(
        "LLM-derived polarity over live last30days clusters (Roadmap 2.7) — advisory "
        "CONTEXT. Aligned, high-confidence polarity can ARM an override upgrade; it "
        "never trades, bypasses a gate, or skips approval. Hype/meme/squeeze narratives "
        "are flagged HIGH-RISK (manual-only), not auto-suppressed."
    )
    ps = j.polarity_summary()
    pc1, pc2, pc3 = st.columns(3)
    with pc1:
        st.caption("by sentiment")
        st.dataframe(ps["by_sentiment"], width="stretch") if ps["by_sentiment"] else st.info("No polarity yet.")
    with pc2:
        st.caption("by narrative driver")
        st.dataframe(ps["by_driver"], width="stretch") if ps["by_driver"] else st.info("—")
    with pc3:
        st.caption("by arming class")
        st.dataframe(ps["by_arming"], width="stretch") if ps["by_arming"] else st.info("—")
    with st.expander("Polarity detail (latest) — incl. HIGH-RISK narrative flags"):
        pol = j.recent_polarity(100)
        if pol:
            st.dataframe(
                [
                    {k: x.get(k) for k in (
                        "symbol", "sentiment_label", "direction_alignment", "confidence",
                        "source_coverage_quality", "narrative_driver_type", "hype_or_manipulation_risk",
                        "arming_classification", "should_arm_override", "parse_status", "warning_message",
                    )}
                    for x in pol
                ],
                width="stretch",
            )
        else:
            st.info("None.")

    st.markdown(console_theme.render_section_label("Decision adjustments (label vs eval)"), unsafe_allow_html=True)
    st.caption(
        "How the advisory AI label moved the no-news eval's call (Roadmap 2.6). "
        "Default is downgrade-only; symmetric up/down moves happen ONLY when armed "
        "(real AI + a live catalyst/sentiment driver). Every move is recorded with "
        "its driver. Never bypasses gates or approval; never executes."
    )
    das = j.decision_adjustment_summary()
    if das["by_adjustment"]:
        st.dataframe(das["by_adjustment"], width="stretch")
    else:
        st.info("No decision adjustments recorded yet.")
    with st.expander("Decision-adjustment detail (latest)"):
        da = j.recent_decision_adjustments(100)
        if da:
            st.dataframe(
                [
                    {k: x.get(k) for k in (
                        "symbol", "adjustment", "eval_decision", "label_decision", "final_decision",
                        "override_armed", "driver_source", "driver", "catalyst_status",
                        "catalyst_source", "catalyst_confidence", "sentiment_label", "sentiment_score",
                        "label_confidence",
                    )}
                    for x in da
                ],
                width="stretch",
            )
        else:
            st.info("None.")

    st.markdown(console_theme.render_section_label("Armed Watch / Near Action"), unsafe_allow_html=True)
    st.caption(
        "Override armed a real driver but the decision stayed WATCH (no proposal) — "
        "near-action watchlist items (Roadmap 2.8). NOT rejects. Manual-only via "
        "`alphaos override`; high-risk narrative requires a warning + manual confirm."
    )
    aw = j.armed_watches(100)
    if aw:
        st.dataframe(
            [
                {k: x.get(k) for k in (
                    "symbol", "eval_decision", "label_decision", "final_decision",
                    "arming_classification", "armed_watch_reason", "sentiment_label",
                    "label_confidence", "proposal_readiness", "labeller_reason",
                )}
                for x in aw
            ],
            width="stretch",
        )
    else:
        st.info("No armed-watch / near-action candidates.")

    st.markdown(console_theme.render_section_label("User overrides"), unsafe_allow_html=True)
    st.caption(
        "Manual user overrides of AlphaOS recommendations (Roadmap 2.8) — a SEPARATE "
        "decision layer; AlphaOS's original call is preserved. Safety-gated; never "
        "auto-executes; manual approval still required."
    )
    uos = j.user_override_summary()
    if uos["by_action"]:
        oc1, oc2 = st.columns(2)
        with oc1:
            st.caption("by action")
            st.dataframe(uos["by_action"], width="stretch")
        with oc2:
            st.caption("by attribution")
            st.dataframe(uos["by_attribution"], width="stretch")
        with st.expander("Override detail (AlphaOS recommendation vs user decision)"):
            st.dataframe(
                [
                    {k: x.get(k) for k in (
                        "symbol", "alphaos_final_decision", "user_override_action", "user_final_decision",
                        "override_aggressiveness", "execution_allowed", "blocked_reason",
                        "user_reason_code", "outcome_status", "attribution_result",
                        "nightdesk_research_candidate",
                    )}
                    for x in j.recent_user_overrides(100)
                ],
                width="stretch",
            )
    else:
        st.info("No user overrides yet.")

    st.markdown(console_theme.render_section_label("Labels summary"), unsafe_allow_html=True)
    ls = j.label_summary()
    c1, c2 = st.columns(2)
    with c1:
        st.caption("by primary label")
        if ls["by_primary_label"]:
            st.dataframe(ls["by_primary_label"], width="stretch")
        else:
            st.info("No labels yet — run an interest scan.")
    with c2:
        st.caption("by advisory decision")
        if ls["by_label_decision"]:
            st.dataframe(ls["by_label_decision"], width="stretch")
        else:
            st.info("No labels yet.")

    st.markdown(console_theme.render_section_label("Proposed candidates"), unsafe_allow_html=True)
    prop = j.proposed_candidates(100)
    if prop:
        st.dataframe(_rows(prop), width="stretch")
    else:
        st.info("No proposed candidates.")

    with st.expander("Watch candidates"):
        w = j.watch_candidates(200)
        st.dataframe(_rows(w), width="stretch") if w else st.info("None.")

    with st.expander("Rejected candidates"):
        st.caption(
            "Hindsight column (UI-PR-A item 5): the attribution-v2 replay's ΔR for "
            "this decision, once resolved. ΔR>0 = the actual (non-trade) path added "
            "value vs AlphaOS's frozen plan; ΔR<0 = it cost value. 'pending' = not yet "
            "resolved, never shown as 0. " + ATTRIBUTION_V2_CAVEAT
        )
        r = j.rejected_candidates_recent(200)
        if r:
            hindsight = j.attribution_by_candidate([x.get("candidate_id") for x in r if x.get("candidate_id")])
            st.dataframe(
                [
                    {
                        **{k: x.get(k) for k in ("symbol", "stage", "reason_code", "reason_detail")},
                        "hindsight": _hindsight_cell(hindsight.get(x.get("candidate_id"))),
                    }
                    for x in r
                ],
                width="stretch",
            )
        else:
            st.info("None.")

    with st.expander("Blocked by gate (proposals)"):
        st.caption("Hindsight column: see the Rejected candidates caption above for the ΔR convention.")
        b = j.blocked_proposals(200)
        if b:
            hindsight = j.attribution_by_candidate([x.get("candidate_id") for x in b if x.get("candidate_id")])
            st.dataframe(
                [
                    {
                        **{k: x.get(k) for k in ("symbol", "proposal_id", "trade_id", "status")},
                        "hindsight": _hindsight_cell(hindsight.get(x.get("candidate_id"))),
                    }
                    for x in b
                ],
                width="stretch",
            )
        else:
            st.info("None.")


def _hypothesis_status_label(row: dict) -> str:
    """MET/FAILED/WITHDRAWN are reserved for an operator reading the
    resolved evidence (alphaos.hypotheses.constants.HypothesisStatus's own
    docstring / registry.mark_hypothesis_status()'s docstring) -- never set
    by any automated path. Explicitly labeled 'operator ruling' wherever
    shown so this tab never implies AlphaOS judged its own hypothesis."""
    status = row.get("status")
    if status in ("met", "failed", "withdrawn"):
        return f"{status} (operator ruling)"
    return status


def _hypothesis_progress_label(progress: "Optional[dict]") -> str:
    if progress is None:
        return "—"
    en, floor_en = progress["effective_n"], progress["floor_effective_n"]
    span = progress.get("span_days")
    floor_span = progress["floor_span_days"]
    # Audit fixup (NIT): a missing span is UNKNOWN, never rendered as "0" --
    # unknown-never-zero, same posture as everywhere else numbers can be
    # missing.
    span_str = f"{span:.0f}" if span is not None else "n/a"
    if progress.get("resolver_ready"):
        ready = "✓ resolver-ready"
    elif progress["clears_floor"]:
        # Audit fixup (LOW-1): data floor cleared but the pre-registered
        # analysis_not_before date hasn't arrived -- the resolver will NOT
        # act yet; say so instead of implying readiness.
        ready = "✓ data floor met · awaiting analysis date"
    else:
        ready = "below floor"
    return f"n={en}/{floor_en} · span={span_str}/{floor_span:.0f}d · {ready}"


def _attribution_v2_agg_row(label: str, agg: dict, floor_n: int, floor_span: int) -> dict:
    # attribution.py's compute_attribution_v2() returns a hand-written
    # "no rows at all" empty-case dict for the execution-gap slice that
    # omits "effective_n" entirely (distinct from _floor_gated_v2_aggregate's
    # own below-floor return, which always includes it) -- fall back to
    # "resolved_count" (present on both shapes) rather than a KeyError, never
    # fabricating a 0 for a value that's genuinely absent from one shape.
    n = agg.get("effective_n", agg.get("resolved_count"))
    if agg["status"] == "ok":
        return {
            "slice": label, "n (effective)": n,
            "span_days": agg["span_days"], "mean_ΔR": agg["mean_delta_r"],
            "sum_ΔR": agg["sum_delta_r"], "status": "✓ ok",
        }
    return {
        "slice": label, "n (effective)": n, "span_days": agg["span_days"],
        "mean_ΔR": None, "sum_ΔR": None,
        "status": f"n={n}/{floor_n} below floor — counts only "
                  f"(needs ≥{floor_span}d span)",
    }


def tab_learning(orch: Orchestrator) -> None:
    """PR-UI-B2: the Learning tab (UI/UX doc §5/§14) -- four read-only
    sub-panels: TQS, Attribution, Hypotheses, Journal. PURE READ, zero
    writes, zero new orchestrator mutation calls, zero buttons that change
    state -- every value comes from an existing report/query module (PR7
    TQS, PR8 attribution v2, PR12 hypothesis registry, HGEN-1 drafts, PR13
    promotion/demotion history), never a raw SQL query inline here.

    Fable5 ruling (2026-07-11): the Stitch mockup for this screen depicted
    an autonomous self-modifying learner -- the single most prohibited
    misrepresentation this build must avoid. The banner below is the
    anti-"ML adjustment feed" law; nothing on this tab may imply AlphaOS
    adjusts its own weights or rules on its own."""
    st.subheader("Learning")
    st.warning(
        "🧭 Hypothesis outcomes are ruled by the operator. AlphaOS never "
        "adjusts its own weights or rules on its own — every MET/FAILED/"
        "WITHDRAWN verdict below is a human judgment call, not a machine one."
    )

    tqs_tab, attr_tab, hyp_tab, journal_tab = st.tabs(
        ["TQS", "Attribution", "Hypotheses", "Journal"]
    )

    with tqs_tab:
        st.markdown(console_theme.render_section_label("TQS — evidence-weighted setup quality"),
                   unsafe_allow_html=True)
        st.caption(
            "PR7 shadow measurement signal -- score is NEVER shown without its data-"
            "confidence/coverage pairing. Never read by any gate/eval/risk/execution path."
        )
        tqs = orch.tqs_shadow_report()
        if tqs["scored_count"] == 0:
            st.info("No TQS scores yet (mock rows excluded).")
        else:
            c1, c2, c3 = st.columns(3)
            c1.metric("Scored (live)", tqs["scored_count"])
            c2.metric("Mean data confidence", tqs["mean_data_confidence"])
            c3.metric("Mock excluded", tqs["mock_excluded_count"])
            st.caption("Bucket histogram (table, not a chart — a live-only distribution, no fixed sample floor applies here).")
            st.dataframe(
                [{"bucket": b, "n": n} for b, n in sorted(tqs["bucket_histogram"].items())],
                width="stretch",
            )
            st.caption("Per-component availability (evidence coverage) — a missing component is greyed by its own reason elsewhere (Approval Center rung-3); here it's the aggregate rate.")
            st.dataframe(
                [
                    {
                        "component": name, "available": c["available"], "missing": c["missing"],
                        "availability_rate": c["availability_rate"],
                    }
                    for name, c in tqs["component_availability"].items()
                ],
                width="stretch",
            )

    with attr_tab:
        st.markdown(console_theme.render_section_label("Attribution — floor-gated ΔR aggregates"),
                   unsafe_allow_html=True)
        st.caption("ΔR>0 = the non-trade (or non-frozen-path) added value; ΔR<0 = it cost value.")
        st.caption(ATTRIBUTION_V2_CAVEAT)
        attr = orch.attribution_report()
        v2 = attr["v2"]
        c1, c2 = st.columns(2)
        c1.metric("Total attribution records", v2["total_records"])
        c2.metric("Mock excluded", v2["mock_excluded_count"])

        rows = []
        for atype, by_agent in v2["aggregate_delta_r_by_type_and_agent"].items():
            for agent, agg in by_agent.items():
                rows.append(_attribution_v2_agg_row(
                    f"{atype} / {agent}", agg, v2["sample_floor_resolved"], v2["sample_floor_span_days"],
                ))
        if rows:
            st.dataframe(rows, width="stretch")
        else:
            st.info("No attribution aggregates yet.")

        with st.expander("By setup card"):
            card_rows = [
                _attribution_v2_agg_row(
                    card_id, agg, v2["sample_floor_subslice_resolved"], v2["sample_floor_span_days"],
                )
                for card_id, agg in v2["aggregate_delta_r_by_card"].items()
            ]
            st.dataframe(card_rows, width="stretch") if card_rows else st.info("None.")

        with st.expander("Execution gap (propose → approved → executed)"):
            eg = v2["execution_gap_propose_approved_executed"]
            st.dataframe(
                [_attribution_v2_agg_row("execution_delta_r", eg, v2["sample_floor_resolved"],
                                        v2["sample_floor_span_days"])],
                width="stretch",
            )

    with hyp_tab:
        st.markdown(console_theme.render_section_label("Hypotheses — PR12 registry"), unsafe_allow_html=True)
        st.caption(
            "Frozen claim text, mechanical status only (proposed → testing → resolved). "
            "MET/FAILED/WITHDRAWN are operator rulings, never set automatically."
        )
        hyp = orch.hypothesis_report()
        st.write(
            f"{hyp['n_total']} total · {hyp['n_proposed']} proposed · "
            f"{hyp['n_testing']} testing · {hyp['n_resolved']} resolved"
        )
        st.dataframe(
            [
                {
                    "hypothesis_id": h["hypothesis_id"], "risk_class": h["risk_class"],
                    "status": _hypothesis_status_label(h),
                    "overdue": "yes" if h.get("overdue") else "",
                    "progress": _hypothesis_progress_label(h.get("progress")),
                    "analysis_not_before": h["analysis_not_before"],
                    "last_verdict": h.get("last_verdict"), "last_q_value": h.get("last_q_value"),
                    "claim": h["claim"],
                }
                for h in hyp["hypotheses"]
            ],
            width="stretch",
        )

        st.markdown(console_theme.render_section_label("HGEN-1 drafts — quarantined, awaiting operator review"),
                   unsafe_allow_html=True)
        st.caption(
            "Read-only here. Accept/reject is an operator CLI action only "
            "(`alphaos hypothesis_accept` / `hypothesis_reject`) -- a UI accept "
            "button is explicitly out of scope for this PR and would need its own "
            "gate review."
        )
        pending_drafts = orch.hypothesis_drafts_list(status="draft")
        st.write(f"Pending drafts: **{len(pending_drafts)}**")
        if pending_drafts:
            st.dataframe(
                [
                    {
                        "draft_id": d["draft_id"], "title": d["title"],
                        "mechanical_risk_class": d["mechanical_risk_class"],
                        "proposed_risk_class": d["proposed_risk_class"],
                        "source": d["source"], "metric_fn_name": d["metric_fn_name"],
                        "card_id": d.get("card_id"), "created_at_utc": d["created_at_utc"],
                    }
                    for d in pending_drafts
                ],
                width="stretch",
            )
            with st.expander("Draft checks (evidence availability + duplicate check)"):
                for d in pending_drafts:
                    st.markdown(f"**{d['draft_id']}** — {d['title']}")
                    st.json({
                        "evidence_check": d.get("evidence_check_json"),
                        "duplicate_check": d.get("duplicate_check_json"),
                    })
        else:
            st.info("No pending drafts.")

    with journal_tab:
        st.markdown(console_theme.render_section_label("Journal — newest first"), unsafe_allow_html=True)
        st.caption(
            "Resolved events, hypothesis lifecycle transitions, card promotions/"
            "demotions -- three entry types only. ΔR>0 = the non-trade added value; "
            "every entry shows its provenance ids as plain text."
        )
        feed = orch.learning_journal_feed()
        if not feed["entries"]:
            st.info("Nothing in the journal yet.")
        else:
            for e in feed["entries"]:
                st.write(f"`{e['timestamp']}` — {e['text']}")
                prov = ", ".join(f"{k}={v}" for k, v in e["provenance"].items() if v is not None)
                if prov:
                    st.caption(prov)


def tab_governance(orch: Orchestrator) -> None:
    """PR-UI-B3: Autonomy & Risk -- the governance console (UI/UX doc §10),
    "deliberately the most physical-feeling screen". PURE READ, zero writes,
    zero new mutating widgets. The only kill-switch CONTROL stays in the
    annunciator strip (render_annunciator, above every tab) -- this tab only
    EXPLAINS the same state, never a second control surface for it.

    Every string below comes from orch.governance_report() (PR-UI-B3's
    build_governance_report()) -- this function only renders that dict, it
    never queries the journal or reads a settings field directly. See that
    module's docstring for the binding content rulings (generated-not-hand-
    written may/may-not panel, no fake L2 criteria, no liquidation language,
    no drawdown governor, no LIVE badge, no unlock affordance)."""
    st.subheader("Autonomy & Risk")
    st.caption(
        "The governance console — what AlphaOS may and may not do alone, "
        "current safety-switch state, and every hard limit it runs under. "
        "Read-only: the only kill-switch CONTROL lives in the strip above."
    )
    rep = orch.governance_report(autonomy_level_label=AUTONOMY_LEVEL_LABEL)

    col_autonomy, col_limits = st.columns(2)
    with col_autonomy:
        with st.container(border=True):
            st.markdown(console_theme.render_section_label("Autonomy"), unsafe_allow_html=True)
            auto = rep["autonomy"]
            st.markdown(f"**Level: {auto['level_label']}**")
            st.write(auto["may_alone"])
            st.write(auto["may_not_alone"])
            exc = auto["unattended_exception"]
            if exc:
                st.info(exc["text"])
            else:
                st.caption(
                    "No unattended close-window exception armed "
                    "(UNATTENDED_APPROVE_WINDOWS unset or its daily cap is 0)."
                )
            st.caption(f"L2: {auto['l2_status']}")

    with col_limits:
        with st.container(border=True):
            st.markdown(console_theme.render_section_label("Hard limits (read-only)"), unsafe_allow_html=True)
            hl = rep["hard_limits"]
            # Literal "$" is escaped as "\$" throughout this panel -- Streamlit's
            # markdown renderer treats a bare "$...$" pair as inline LaTeX math
            # (confirmed via live preview: an unescaped "Min $ volume: **$2,000,000**"
            # silently swallowed the dollar figure), and this panel is the first
            # place in the app to put a literal dollar amount inside st.write()
            # text (existing tabs speak in R-multiples or use st.metric, which
            # doesn't parse markdown).
            st.write(
                f"Risk/trade: **{hl['risk_per_trade_pct'] * 100:.2f}%** "
                f"(\\${hl['risk_per_trade_dollars']:,.2f})"
            )
            st.write(f"Max open positions: **{hl['max_open_positions']}**")
            st.write(
                f"Daily-loss stop: **{hl['daily_loss_stop_pct'] * 100:.2f}%** "
                f"(\\${hl['daily_loss_stop_dollars']:,.2f})"
            )
            st.write(f"Auto-approvals: **{hl['auto_approvals_used_today']}/{hl['auto_approvals_cap']}** today")
            st.write(
                f"Unattended approvals: **{hl['unattended_approvals_used_today']}/"
                f"{hl['unattended_approvals_cap']}** today · window(s): {hl['unattended_windows_label']}"
            )
            st.write(
                f"Max spread: **{hl['max_spread_pct'] * 100:.2f}%** · "
                f"Min \\$ volume: **\\${hl['min_dollar_volume']:,.0f}**"
            )
            st.write(f"AI budget (30d, all real calls): **{hl['ai_budget_used_30d']}/{hl['ai_budget_cap_30d']}**")
            st.write(f"Bear-debate calls: **{hl['debate_calls_used_today']}/{hl['debate_calls_cap_today']}** today")
            st.write(
                f"Hypothesis-gen calls: **{hl['hypothesis_gen_calls_used_today']}/"
                f"{hl['hypothesis_gen_calls_cap_today']}** today"
            )
            st.write(
                f"Max paper trades/day: **{hl['max_paper_trades_per_day_display']}** "
                f"(used {hl['paper_trades_used_today']} today)"
            )
            st.caption(hl["changes_note"])

    col_ks, col_lock = st.columns(2)
    with col_ks:
        with st.container(border=True):
            st.markdown(console_theme.render_section_label("Kill switch"), unsafe_allow_html=True)
            ks = rep["kill_switch"]
            if ks["engaged"]:
                st.error(f"● {ks['state_label']} — {ks['reason']}")
            else:
                st.success(f"● {ks['state_label']}")
            st.write(ks["explanation"])
            st.caption(ks["control_note"])

    with col_lock:
        with st.container(border=True):
            st.markdown(console_theme.render_section_label("Real-money lock"), unsafe_allow_html=True)
            lock = rep["real_money_lock"]
            st.write(f"🔒 {lock['structural_statement']}")
            st.write(
                f"`REAL_TRADING_ENABLED={lock['real_trading_enabled_raw']}` · "
                f"`ALLOW_REAL_ORDERS={lock['allow_real_orders_raw']}` · mode=`{lock['mode']}`"
            )
            st.caption(lock["no_unlock_note"])

    with st.container(border=True):
        st.markdown(console_theme.render_section_label("Trading calendar"), unsafe_allow_html=True)
        cal = rep["trading_calendar"]
        day_state = "a trading day" if cal["is_trading_day"] else "MARKET CLOSED"
        st.caption(
            f"Today ({cal['today_et']} ET): {day_state} · scan windows: {cal['scan_windows_label']} · "
            f"{cal['note']}"
        )


def main(orch: Orchestrator | None = None) -> None:
    st.set_page_config(page_title="AlphaOS", layout="wide")
    if not _is_loopback_request():
        st.error(
            "🔴 REFUSED — this dashboard is only reachable from the loopback "
            "address (127.0.0.1). Every action here (Approve / Reject / "
            "kill-switch / Run scan / everything UI-PR-A added) is disabled "
            "on this connection. Remote access, if ever needed, is an SSH "
            "tunnel (`ssh -L 8502:127.0.0.1:8502 user@host`) — never a LAN "
            "bind, never port-forwarding."
        )
        st.stop()
        # Explicit backstop: st.stop() aborts by raising in a real Streamlit
        # run, but it does NOT raise unconditionally (it returns normally
        # without a live ScriptRunContext). This return makes the refusal
        # fail-closed regardless of Streamlit internals -- nothing below may
        # ever run for a non-loopback connection.
        return
    # PR-UI-B1: console theme (styling only -- see console_theme.py's module
    # docstring). One CSS injection, after the loopback gate so the refusal
    # path above stays exactly as minimal as it was before this PR.
    st.markdown(console_theme.CONSOLE_CSS, unsafe_allow_html=True)
    orch = orch or get_orchestrator()
    # IMPORTANT: do NOT call orch.startup() here. startup() WRITES (a config
    # snapshot + one system_event per check); calling it on render made the
    # dashboard dirty the ledger on every page load. Each orchestrator action
    # (scan/approve/monitor/report/seed) runs _ensure_startup() itself, so the
    # startup-safety checks still run before any write — the render path stays
    # strictly read-only.
    render_sidebar(orch)
    s = orch.settings
    st.title("AlphaOS — paper trading (v1)")
    st.caption(
        f"🟢 Read-only on load · connected DB `{s.db_path}` · mode `{s.mode.value}` · "
        f"real-money trading unreachable · writes happen ONLY via explicit actions "
        f"(Run scan / Approve / Reject / Monitor / Seed)."
    )
    # Computed once per page load and shared by the annunciator + Positions
    # tab (both need the same per-position R/verdict data); reuses orch.market
    # rather than constructing a fresh MarketDataClient -- a second instance
    # would re-trigger that class's one-time-per-instance "market data is
    # mocked" system_event notice. The Tonight tab calls build_daily_brief()
    # separately, which does its own internal assess_positions() call (and
    # its own internal client) -- an accepted double-compute, same reasoning
    # PR11 already documented for daily_brief.py/scheduler/digest.py (open
    # positions are few; not worth threading a precomputed list through).
    positions_health = assess_positions(orch.journal, s, orch.market)
    render_annunciator(orch, positions_health)

    tabs = st.tabs(
        [
            "Tonight", "Positions", "Approval Center", "Candidates / Proposals",
            "Candidate Flow", "Learning", "Autonomy & Risk", "Open Trades", "Closed Trades",
            "System Health", "Trade Packet", "Scan Batches", "Scheduler Runs", "System Events",
        ]
    )
    with tabs[0]:
        tab_tonight(orch)
    with tabs[1]:
        tab_positions_health(positions_health)
    with tabs[2]:
        tab_approval_center(orch)
    with tabs[3]:
        tab_candidates(orch)
    with tabs[4]:
        tab_candidate_flow(orch)
    with tabs[5]:
        tab_learning(orch)
    with tabs[6]:
        tab_governance(orch)
    with tabs[7]:
        tab_open_trades(orch)
    with tabs[8]:
        tab_closed_trades(orch)
    with tabs[9]:
        tab_system_health(orch)
    with tabs[10]:
        tab_trade_packet(orch)
    with tabs[11]:
        tab_scan_batches(orch)
    with tabs[12]:
        tab_scheduler_runs(orch)
    with tabs[13]:
        tab_system_events(orch)


if __name__ == "__main__":
    main()
