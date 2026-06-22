"""AlphaOS v1 dashboard — single local Streamlit app, minimal tabs.

Tabs:
* Candidates / Proposals — OpenAI eval, optional Claude review, approve/reject,
  and a "Request Claude second opinion" button (disabled without an Anthropic key).
* Open Trades
* Closed Trades
* System Health — mode, broker status, data freshness, kill switch.

Run with:  streamlit run alphaos/dashboard/streamlit_app.py

This is intentionally not the full 15-tab UI. It never presents simulated
performance as real: everything is labelled paper/simulated.
"""

from __future__ import annotations

import streamlit as st

from alphaos.ai.claude_reviewer import ClaudeUnavailable
from alphaos.config.settings import load_settings
from alphaos.orchestrator import Orchestrator
from alphaos.safety import KillSwitch


def get_orchestrator() -> Orchestrator:
    # Fresh per run keeps the SQLite connection on Streamlit's script thread.
    settings = load_settings()
    return Orchestrator(settings=settings)


def render_sidebar(orch: Orchestrator) -> None:
    st.sidebar.title("AlphaOS")
    st.sidebar.caption("Paper-trading OS · real money disabled")
    s = orch.settings
    st.sidebar.markdown(
        f"**Mode:** `{s.mode.value}`  \n"
        f"**Approval:** `{s.approval_mode.value}`  \n"
        f"**Real trading:** `disabled`"
    )
    ks = KillSwitch()
    if ks.is_engaged():
        st.sidebar.error(f"KILL SWITCH ENGAGED — {ks.reason()}")
        if st.sidebar.button("Release kill switch"):
            ks.release()
            st.rerun()
    else:
        if st.sidebar.button("Engage kill switch"):
            ks.engage("dashboard")
            st.rerun()

    st.sidebar.divider()
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
            if prop and prop["status"] in ("pending_approval", "proposed"):
                st.markdown(f"**Proposal** `{prop['proposal_id']}` — qty {prop['qty']} · status {prop['status']}")
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
    st.subheader("Closed Trades (paper, simulated)")
    rows = orch.journal.closed_outcomes(200)
    if rows:
        st.dataframe(rows, width="stretch")
        net = round(sum((r.get("net_pnl") or 0) for r in rows), 2)
        st.metric("Realized net P&L (paper)", net)
    else:
        st.info("No closed trades yet.")


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


def main() -> None:
    st.set_page_config(page_title="AlphaOS", layout="wide")
    orch = get_orchestrator()
    orch.startup()
    render_sidebar(orch)
    st.title("AlphaOS — paper trading (v1)")
    t1, t2, t3, t4 = st.tabs(["Candidates / Proposals", "Open Trades", "Closed Trades", "System Health"])
    with t1:
        tab_candidates(orch)
    with t2:
        tab_open_trades(orch)
    with t3:
        tab_closed_trades(orch)
    with t4:
        tab_system_health(orch)


if __name__ == "__main__":
    main()
