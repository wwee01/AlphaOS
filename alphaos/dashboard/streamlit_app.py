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


def _format_seconds_remaining(seconds) -> str:
    """PR6: human-readable TTL countdown for the Approval Center. None (missing/
    unparseable expiry) reads as 'unknown' -- never as a blank/"fine" value."""
    if seconds is None:
        return "unknown"
    if seconds <= 0:
        return f"expired {int(abs(seconds))}s ago"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs}s"


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
                "expires_in": _format_seconds_remaining(v["proposal_seconds_remaining"]),
                "stale": v["proposal_is_stale"],
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
            f"{v['symbol']} · {v['side']} · qty {v['qty']} · R:R {v['reward_risk']} · {pid}{stale_flag}"
        ):
            if v["proposal_is_stale"]:
                st.warning(
                    "This proposal's TTL has expired — approval will be rejected. "
                    "Run a fresh scan to get a current proposal for this symbol."
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

    st.markdown("#### last30days research summary")
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

    st.markdown("#### last30days narrative polarity")
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

    st.markdown("#### Decision adjustments (label vs eval)")
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

    st.markdown("#### Armed Watch / Near Action")
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

    st.markdown("#### User overrides")
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
