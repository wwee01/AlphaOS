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
from alphaos.reports.trade_packet import assemble_trade_packet
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
        f"**Real trading:** `disabled`  \n"
        f"**DB:** `{s.db_path}`"
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

    st.dataframe(
        [
            {
                "proposal_id": v["proposal_id"], "trade_id": v["trade_id"], "symbol": v["symbol"],
                "side": v["side"], "entry": v["entry"], "stop": v["stop"], "target": v["target"],
                "qty": v["qty"], "R:R": v["reward_risk"], "risk_$": v["risk_amount"],
                "last_freshness": v["last_known_freshness"], "generated_sgt": v["generated_at_sgt"],
            }
            for v in views
        ],
        width="stretch",
    )
    st.divider()
    for v in views:
        pid = v["proposal_id"]
        with st.expander(
            f"{v['symbol']} · {v['side']} · qty {v['qty']} · R:R {v['reward_risk']} · {pid}"
        ):
            st.write(
                {
                    "trade_id": v["trade_id"], "candidate_id": v["candidate_id"],
                    "entry": v["entry"], "stop": v["stop"], "target": v["target"],
                    "risk_per_share": v["risk_per_share"], "risk_amount": v["risk_amount"],
                    "expected_r": v["expected_r"], "requires_margin": v["requires_margin"],
                    "last_known_freshness": v["last_known_freshness"],
                    "generated_at_utc": v["generated_at_utc"],
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
                "review?": "yes" if c.get("label_review_required") else "",
                "status": c.get("status"), "reason": c.get("shortlist_reason"),
            }
            for c in cands
        ]

    st.markdown("#### Catalyst enrichment summary")
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

    st.markdown("#### Labels summary")
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

    st.markdown("#### Proposed candidates")
    prop = j.proposed_candidates(100)
    if prop:
        st.dataframe(_rows(prop), width="stretch")
    else:
        st.info("No proposed candidates.")

    with st.expander("Watch candidates"):
        w = j.watch_candidates(200)
        st.dataframe(_rows(w), width="stretch") if w else st.info("None.")

    with st.expander("Rejected candidates"):
        r = j.rejected_candidates_recent(200)
        if r:
            st.dataframe(
                [{k: x.get(k) for k in ("symbol", "stage", "reason_code", "reason_detail")} for x in r],
                width="stretch",
            )
        else:
            st.info("None.")

    with st.expander("Blocked by gate (proposals)"):
        b = j.blocked_proposals(200)
        if b:
            st.dataframe(
                [{k: x.get(k) for k in ("symbol", "proposal_id", "trade_id", "status")} for x in b],
                width="stretch",
            )
        else:
            st.info("None.")


def main(orch: Orchestrator | None = None) -> None:
    st.set_page_config(page_title="AlphaOS", layout="wide")
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
    tabs = st.tabs(
        [
            "Approval Center", "Candidates / Proposals", "Candidate Flow", "Open Trades",
            "Closed Trades", "System Health", "Trade Packet", "Scan Batches",
            "Scheduler Runs", "System Events",
        ]
    )
    with tabs[0]:
        tab_approval_center(orch)
    with tabs[1]:
        tab_candidates(orch)
    with tabs[2]:
        tab_candidate_flow(orch)
    with tabs[3]:
        tab_open_trades(orch)
    with tabs[4]:
        tab_closed_trades(orch)
    with tabs[5]:
        tab_system_health(orch)
    with tabs[6]:
        tab_trade_packet(orch)
    with tabs[7]:
        tab_scan_batches(orch)
    with tabs[8]:
        tab_scheduler_runs(orch)
    with tabs[9]:
        tab_system_events(orch)


if __name__ == "__main__":
    main()
