# M1 common valuation: manual capture and reconciliation

Status: implemented in an isolated worktree, **not activated or accepted as complete M1**. No production code, ledger, settings, services or orders were changed by this work. No new broker/data request was made while developing it.

The one-week plan requires a common account/benchmark convention, explicit cashflows, complete weekend/holiday movement and independently recomputed returns. Withholding the existing comparison is containment. This patch provides the missing manual evidence collector and a tested reconstruction candidate; it does not turn incomplete evidence into a verified return.

## Confirmed root causes

The retained 12 September broker capture contains three different samples near the 11 September close: 19:58 UTC **95,617.11**, 19:59 **95,629.17**, and 20:00 **95,617.38**. Its daily series has **95,622.51** at source timestamp 12 September 00:00 UTC. These values are not interchangeable. The [retained evidence comparison](evidence/common-valuation-retained-2026-09-15.json) preserves all four sessions, the daily labels without rebinding them, and the SHA-256 of the original capture.

The installed SDK describes history timestamps as the beginning of a window and equity as its end. Alpaca's API also describes different last-trade and official-close pricing regimes. Neither statement proves that this account's 19:59 sample is the same mark as a SPY closing price. See [SDK model](https://alpaca.markets/sdks/python/api_reference/trading/models.html) and [portfolio history](https://docs.alpaca.markets/us/v1.1/reference/getaccountportfoliohistory-1).

The current daily bars adapter discards source timestamps and does not store feed/adjustment provenance. Market-hours portfolio history is also insufficient evidence of overnight cashflow coverage. The [activity API](https://docs.alpaca.markets/us/reference/getaccountactivities-2) filters by creation time; an activity's date can instead describe settlement. Exhausting pagination cannot prove that no late economic event remains to be posted.

## New convention and its limits

The reconstruction uses broker cash plus signed holdings, marked at each symbol's exact final regular-session one-minute bar. SPY uses that same minute, configured feed and raw adjustment. It records the bar's actual interval-start timestamp and the separate exclusive interval-end valuation cutoff. It never assigns a broker history sample a new timestamp. The convention uses the last qualifying trade in the interval; it is not the primary exchange's official closing auction or guaranteed executable liquidation value. [Historical bars reference](https://docs.alpaca.markets/us/reference/stockbarsingle-1).

Two fresh account/holding reads must agree. The collector performs two complete activity traversals spanning the prior close with a seven-day creation-time overlap, captures request/receipt times and pagination evidence, and requires their results to agree. It refuses post-cutoff fills, missing/duplicate bars, unknown assets, changed identities, unknown quantities, incomplete pages, date-only nontrade activities, unsupported corporate actions and ambiguous cash events. There is no fallback to another minute, daily bar, feed, static paper balance or current quote.

The manual collector selects the latest completed session from a 14-day broker-calendar request, requires the preceding session to be an adjacent exchange trading day, and waits at least 15 minutes after close. It cannot reconstruct an arbitrary earlier holding from today's positions. Request/receipt timestamps describe local reads, not an atomic broker snapshot; pagination may also lag postings. Cash and quantity stability plus the returned activity checks narrow these uncertainties but do not remove them. An actual capture and separate review remain required.

Fees and known dividends/interest stay inside cash and are not added back. Deposits and withdrawals are external; offsetting events are retained even when their net is zero. An external-flow interval needs separate subperiod valuations before an investment return can be justified. SPY is a price benchmark, without reinvested dividends. An eventual account return including its own dividends must disclose this asymmetry or use an independently specified comparable benchmark.

Even a numerically matched capture remains `reconstructed_candidate`, with `comparison_eligible=false`. The code has no caller flag to promote it. Pagination and two stable account reads are observations, not proof of complete economic-event knowledge. Date-only fees may therefore block reconstruction even when small; amount size does not repair missing event timing.

## Manual use after review

No existing scheduler, CLI subcommand, gate or orchestrator imports the collector. The following command explicitly makes GET requests using the existing paper credentials and configured feed. It creates no JournalStore and writes only a **new** evidence file with mode 0600. Run from the reviewed worktree with the established runtime and a controlled effective configuration. Do not assume a worktree contains production settings.

```sh
PYTHONPATH=. /path/to/existing/python -m alphaos.reports.common_valuation_capture \
  --collect --output /private/reviewed/evidence/common-valuation-new.json
```

Recompute a saved capture without credentials or network:

```sh
PYTHONPATH=. /path/to/existing/python -m alphaos.reports.common_valuation_capture \
  --input /private/reviewed/evidence/common-valuation-new.json \
  --output /private/reviewed/evidence/common-valuation-recomputed.json
```

Existing output files are refused. Exit 0 means a candidate was reconstructed; exit 2 means blocked/incomplete evidence. Neither exit code certifies M1 or economic profitability. Provider failures produce a sanitized failure artifact and no valuation. Evidence contains private account activity, so keep it with other private operational artifacts.

The source permits only five fixed read endpoints. Each request has a 15-second socket timeout, and each source instance has a 27-request cap: one calendar request, four account/position reads, up to two ten-page activity traversals, and up to two bar pages. The unchanged configured feed is required; inaccessible data fail visibly. This is a manual bounded diagnostic, not new recurring usage or a paid dependency.

`record_common_valuation` is an explicit append-only diagnostic writer for a reviewed caller. It only appends a system event; it never creates or overwrites equity observations. The report exposes a small status summary, keeps reconstructed NAV separate from broker equity, and always excludes it from return/beta calculations. Existing provisional rows retain their original stored data; the report shows their source timestamps and inferred window ends separately, with no verified valuation instant.

## Validation and remaining acceptance work

Focused verification covers common long/short/flat marks, independent cash-plus-holdings arithmetic, exact minute provenance, holidays/DST/early closes, missing sessions, changing cash/holdings/activities, duplicate/truncated pages, zero-net external flows, ambiguous corporate actions, private offline files, nonzero failure exit, append-only storage and report suppression. Existing benchmark/close/report regressions are included.

Validation command: `pytest tests/test_common_valuation.py tests/test_relative_performance.py tests/test_close_equity.py tests/test_benchmark_capture.py -o addopts='' -q`. Result: **107 passed**. Ruff passed for the changed Python files; mypy passed for all four changed source files. These are offline tests, not real market observations or a full application regression run.

M1 remains incomplete until a reviewed real capture and independent economic-event reconciliation establish a usable convention and continuity, followed by independently recomputed multi-session returns. This patch deliberately has no scheduled activation, return calculation for candidates, cashflow reconstruction across unknown events, or claim that synthetic tests are real observations. The original 18 September deadline and scorecard criteria remain unchanged.
