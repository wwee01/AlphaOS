"""Offline economic report. Reads evidence; never loads settings or a journal.

Example: python -m alphaos.reports.economic_report --input inputs.json
  --output NEW-result.json --markdown NEW-report.md
Optional monthly overhead is an explicitly declared USD budget, not a fee receipt.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path

from alphaos.reports.account_economics import measure_account_economics


def render_economics(result):
    lines = [
        "# Reported account and SPY economics",
        "",
        f"Method: `{result['method']}`.",
        "",
        f"Status: **{result['status']}**. Research activation: **false**.",
        "",
        result["basis"] + ".",
        "",
    ]
    if result.get("blockers"):
        lines.extend(
            [
                "Measurement blocked:",
                "",
                *["- " + value for value in result["blockers"]],
                "",
            ]
        )
    if result.get("cash_residual") is not None:
        lines.extend(
            [
                f"Interval: {result['start_at_utc']} through {result['end_at_utc']} (ending cutoff exclusive for activities).",
                "",
                "| Accounting item | USD |",
                "|---|---:|",
                f"| Opening marked NAV | {result['start_nav']} |",
                f"| Ending marked NAV | {result['end_nav']} |",
                f"| Opening cash | {result['cash_start']} |",
                f"| Ending cash | {result['cash_end']} |",
                f"| Incremental fill cash | {result['fill_cash']} |",
                f"| Reported internal cash | {result['internal_cash']} |",
                f"| External net inflow | {result['external_net_inflow']} |",
                f"| Cash residual | {result['cash_residual']} |",
                "",
                f"Quantity residuals: `{json.dumps(result['quantity_residuals'], sort_keys=True)}`.",
                "",
            ]
        )
    if result["status"] == "reconciled_asof":
        lines.extend(
            [
                f"Account marked gain after reported broker cash costs: **${float(result['account_gain_usd']):,.2f}**.",
                "This includes unrealized holding marks; it is not closed-trade profit or a completed weekly trial.",
                "",
            ]
        )
        if result["comparison_eligible"]:
            lines.extend(
                [
                    "| Equal opening capital comparison | Return | USD gain |",
                    "|---|---:|---:|",
                    f"| Account | {100 * float(result['account_return']):.6f}% | {float(result['account_gain_usd']):,.2f} |",
                    f"| Hypothetical SPY | {100 * float(result['benchmark_return']):.6f}% | {float(result['benchmark_gain_usd']):,.2f} |",
                    f"| Account minus SPY | {100 * float(result['excess_return']):.6f} percentage points | {float(result['excess_gain_usd']):,.2f} |",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "Percentage comparison unavailable: "
                    + "; ".join(result["percentage_comparison_blockers"])
                    + ".",
                    "",
                ]
            )
        if "operating_cost_usd" in result:
            lines.extend(
                [
                    f"Declared overhead allocation: **${float(result['operating_cost_usd']):,.2f}**; "
                    f"account gain after that estimate: **${float(result['account_gain_after_declared_overhead_usd']):,.2f}**.",
                    "This operating budget is separate from observed broker cash activity and is not a payment receipt.",
                    "",
                ]
            )
        else:
            lines.extend([result["operating_cost_status"] + ".", ""])
        lines.extend(
            [
                result["dividend_convention"],
                "",
                "Execution prices already contain execution slippage; modeled trade friction was not deducted again.",
                "",
            ]
        )
    lines.extend(
        [
            "Limits:",
            "",
            *["- " + value for value in result["source_limitations"]],
            "",
            f"Input receipt SHA256: `{result.get('input_sha256', 'unavailable')}`.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_new(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument(
        "--monthly-overhead-usd", help="Explicit scenario budget; not a broker fee"
    )
    parser.add_argument("--budget-timezone", default="Asia/Singapore")
    args = parser.parse_args(argv)
    outputs = [p for p in (args.output, args.markdown) if p is not None]
    if len(set(p.resolve() for p in outputs)) != len(outputs):
        parser.error("output paths must differ")
    if any(p.exists() or not p.parent.is_dir() for p in outputs):
        parser.error("outputs must be new files in existing directories")
    bundle = deepcopy(json.loads(args.input.read_text()))
    if args.monthly_overhead_usd is not None:
        if "operating_cost" in bundle:
            parser.error("refusing to replace an existing operating-cost assumption")
        bundle["operating_cost"] = {
            "basis": "declared_monthly_budget",
            "monthly_usd": args.monthly_overhead_usd,
            "timezone": args.budget_timezone,
        }
    result = measure_account_economics(bundle)
    _write_new(
        args.output,
        json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n",
    )
    if args.markdown:
        _write_new(args.markdown, render_economics(result))
    print(
        f"{result['status']}; as-of cash-basis comparison eligible={result['comparison_eligible']}; research activation=false"
    )
    return 0 if result["status"] == "reconciled_asof" else 2


if __name__ == "__main__":
    raise SystemExit(main())
