"""Daily learning report (recon).

v1 produces a factual, honest daily report from the journal: what was scanned,
proposed, approved, rejected, blocked, filled, and the realized P&L — plus a
same-day-exit breakdown. It explicitly does NOT claim statistical conclusions
(the forward sample is too small); it builds the structure those conclusions
will later use.

When an OpenAI key is present and not in mock mode, the narrative can be authored
by the model; otherwise a deterministic template is used and labelled as such.
"""

from __future__ import annotations

from typing import Optional

from alphaos.constants import Severity
from alphaos.reports.metrics import compute_metrics
from alphaos.util import timeutils
from alphaos.util.ids import new_id


class DailyRecon:
    def __init__(self, settings, journal):
        self.settings = settings
        self.journal = journal

    def _counts(self) -> dict:
        j = self.journal
        start = j.start_of_trading_day_utc()
        c = lambda table, where="created_at_utc >= ?", params=(): j.count_rows(  # noqa: E731
            table, where, params or (start,)
        )
        proposals = c("trade_proposals")
        approvals = j.count_rows("approvals", "approved = 1 AND created_at_utc >= ?", (start,))
        rejections = c("rejected_candidates")
        fills = j.count_rows("paper_fills", "created_at_utc >= ?", (start,))
        blocks = j.count_rows(
            "system_events",
            "category = 'execution' AND severity IN ('error','critical') AND created_at_utc >= ?",
            (start,),
        )
        candidates = c("candidates")
        same_day = j.count_rows("exits", "is_same_day = 1 AND created_at_utc >= ?", (start,))
        net_pnl = j.realized_pnl_today()
        return {
            "candidates": candidates,
            "proposals": proposals,
            "approvals": approvals,
            "rejections": rejections,
            "blocks": blocks,
            "fills": fills,
            "same_day_exits": same_day,
            "net_pnl": round(net_pnl, 2),
            "open_positions": j.count_open_positions(),
            "labeller": j.labeller_source_summary(limit=50),
        }

    def _today_metrics(self) -> dict:
        start = self.journal.start_of_trading_day_utc()
        outcomes = self.journal.query(
            "SELECT * FROM trade_outcomes WHERE created_at_utc >= ?", (start,)
        )
        return compute_metrics(outcomes)

    def generate(self) -> dict:
        report_date = timeutils.market_date().isoformat()
        counts = self._counts()
        metrics = self._today_metrics()
        counts["metrics"] = metrics
        content = self._render_markdown(report_date, counts, metrics)
        report_id = new_id("rep")
        self.journal.insert(
            "daily_learning_reports",
            {
                "report_id": report_id,
                "report_date": report_date,
                "mode": self.settings.mode.value,
                "summary": f"{counts['candidates']} candidates, {counts['proposals']} proposals, "
                f"{counts['approvals']} approvals, {counts['fills']} fills, net {counts['net_pnl']}.",
                "metrics_json": counts,
                "proposals_count": counts["proposals"],
                "approvals_count": counts["approvals"],
                "rejections_count": counts["rejections"],
                "blocks_count": counts["blocks"],
                "fills_count": counts["fills"],
                "net_pnl": counts["net_pnl"],
                "content_md": content,
                "generated_by": "template" if (self.settings.is_mock or not self.settings.has_openai_key) else "openai",
            },
        )
        self.journal.log_system_event(Severity.INFO, "report", f"Daily report {report_date} generated.")
        return {"report_id": report_id, "report_date": report_date, "counts": counts, "content_md": content}

    def _labeller_line(self, lf: dict) -> str:
        """One Activity line for AI-labeller health; tags WARNING/CRITICAL when the
        fail-safe rate is high (a failing labeller silently rejects everything)."""
        from alphaos.ai.labeller_health import evaluate_failsafe_health

        s = self.settings
        health = evaluate_failsafe_health(
            lf or {}, s.labeller_failsafe_warn_rate, s.labeller_failsafe_critical_rate,
            s.labeller_failsafe_min_sample)
        pct = round((lf.get("fail_safe_rate") or 0) * 100)
        tag = "" if health["level"] == "ok" else f" — **{health['level'].upper()}**"
        return (
            f"- AI labeller: **{lf.get('total', 0)}** labels, "
            f"**{lf.get('fail_safe', 0)}** fail-safe, **{lf.get('openai', 0)}** openai, "
            f"**{lf.get('mock', 0)}** mock, fail-safe rate **{pct}%**{tag}\n"
        )

    def _render_markdown(self, report_date: str, c: dict, m: dict) -> str:
        market_mode = self.settings.market_data_mode
        exec_label = self.settings.execution_provider
        data_label = f"{self.settings.data_provider}/{self.settings.market_data_feed} ({market_mode})"
        return (
            f"# AlphaOS Daily Learning Report — {report_date}\n\n"
            f"_Mode: **{self.settings.mode.value}** · Approval: **{self.settings.approval_mode.value}** · "
            f"Real trading: **disabled**_\n\n"
            f"_Playbook: **momentum continuation (no-news baseline)** · "
            f"Market data: **{data_label}** · Execution: **{exec_label}** · News: **disabled_v1**_\n\n"
            + (
                "> ⚠️ **Market data is MOCKED (offline)** — not live.\n\n"
                if market_mode == "mock"
                else ""
            )
            + "> v1 forward-evidence report. No statistical conclusions are claimed; "
            "this records what happened and seeds the no-news baseline-comparison data.\n\n"
            "## Activity\n"
            f"- Candidates detected: **{c['candidates']}**\n"
            f"- Proposals: **{c['proposals']}**\n"
            f"- Approvals: **{c['approvals']}**\n"
            f"- Rejections/blocks: **{c['rejections']}** / **{c['blocks']}**\n"
            f"- Fills: **{c['fills']}**\n"
            f"- Open positions: **{c['open_positions']}**\n"
            f"- Same-day exits: **{c['same_day_exits']}**\n"
            + self._labeller_line(c.get("labeller", {}))
            + "\n"
            "## P&L (paper) — after modelled costs\n"
            f"- Trades closed: **{m['trades']}**\n"
            f"- Gross P&L: **{m['gross_pnl']}** · Costs: **{m['total_costs']}** · "
            f"**Net P&L: {m['net_pnl']}**\n"
            f"- Win rate: **{m['win_rate']}** · Expectancy/trade: **{m['expectancy']}** · "
            f"Profit factor: **{m['profit_factor']}**\n"
            f"- Avg win / avg loss: **{m['avg_win']}** / **{m['avg_loss']}** · "
            f"Max drawdown: **{m['max_drawdown']}**\n"
            f"- Avg hold (days): **{m['avg_hold_days']}** · Same-day-exit rate: **{m['same_day_exit_rate']}**\n\n"
            "## Notes\n"
            "- Net P&L is **after modelled costs** (commission + slippage); MFE/MAE are exit-time approximations.\n"
            f"- {m['note']}. Baseline comparisons are logged but need a larger forward sample.\n"
        )
