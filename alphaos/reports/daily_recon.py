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
        }

    def generate(self) -> dict:
        report_date = timeutils.market_date().isoformat()
        counts = self._counts()
        content = self._render_markdown(report_date, counts)
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

    def _render_markdown(self, report_date: str, c: dict) -> str:
        market_mode = self.settings.market_data_mode
        data_label = f"{self.settings.data_provider}/{self.settings.market_data_feed} ({market_mode})"
        return (
            f"# AlphaOS Daily Learning Report — {report_date}\n\n"
            f"_Mode: **{self.settings.mode.value}** · Approval: **{self.settings.approval_mode.value}** · "
            f"Real trading: **disabled**_\n\n"
            f"_Playbook: **momentum continuation (no-news baseline)** · "
            f"Market data: **{data_label}** · Execution: **simulated_internal** · News: **disabled_v1**_\n\n"
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
            f"- Same-day exits: **{c['same_day_exits']}**\n\n"
            "## P&L (paper, simulated)\n"
            f"- Realized net P&L today: **{c['net_pnl']}**\n\n"
            "## Notes\n"
            "- Costs are not yet modelled (net == gross); MFE/MAE are exit-time approximations.\n"
            "- Baseline comparisons are being logged but require a larger forward sample.\n"
        )
