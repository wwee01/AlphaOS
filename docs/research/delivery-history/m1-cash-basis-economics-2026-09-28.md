# M1: usable common-mark, reported cash-basis economics

This patch adds an explicit offline measurement. It changes no trading policy,
configuration, scheduler, journal schema, historical row or strategy activation.
The old provisional equity series and diagnostic collectors retain their names
and meanings. `account_economics.py` supplies a separate versioned calculation;
`economic_report.py` produces a private JSON and readable report from saved inputs.

The previous cash/quantity bridge rejected the retained September 18–25 endpoints
because they were not adjacent sessions. It also deliberately withheld all
comparative returns. The new method reconciles the whole interval before
calculating account and equal-opening-capital SPY results.

## Exact measurement contract

- Both endpoints are independently archived paper-account receipts, USD cash and
  signed US equity quantities. The account identity and configured IEX/SIP feed
  must match. The endpoint is cash plus holdings at each symbol's last qualifying
  trade in the exact final regular-session minute, including SPY. This is an
  explicit common-mark convention, not broker daily EOD equity or a promise that
  trades in different symbols occur at an identical microsecond.
- Account/cash/holding/activity observations must remain stable through the
  capture. Every raw endpoint hash is checked. Request bounds, page counts,
  continuation tokens, terminal pages and declared coverage are checked against
  the archived page receipts. A caller's derived status, NAV or return is ignored.
- Individual fill `qty` and execution price determine cash; `cum_qty` is checked
  independently and never summed as an incremental quantity. Per-order VWAP
  summaries retain their source identities. End cash and every signed holding
  must reconcile. Account cash has a reported cent precision: an explicit residual
  up to $0.01 is retained, not assigned to fees. Share tolerance is 1e-9.
- Post-cutoff fills or cash with broker transaction timestamps can be reversed to reconstruct an endpoint,
  provided every resulting holding has its exact historical mark. Unknown or
  noncash corporate activity, changed/missing earlier receipts, contradictory
  identities, unsupported types and missing marks block the measurement.
- Fees, interest and paid account dividends are internal cash, included once.
  Execution slippage is already embedded in reported prices. Modeled trade costs
  are never deducted again. A zero *reported* fee amount does not certify zero
  actual fees; late postings can revise the result.
- Nontrade activity `date` is an occurrence/settlement **interval**, not a fabricated
  precise posting time. A date that straddles either closing cutoff blocks the
  measurement. Date-only cash after a cutoff cannot be reversed as a known posting. Date-only cash entirely inside a multi-session interval can be
  included with that explicit timing limitation. This is not accrual accounting.
- External deposits and withdrawals are conserved separately and excluded from
  dollar profit, even if their net is zero. Dollar profit can be reported, but a
  percentage comparison requires no external events in this version. An interval
  with transfers needs additional matching subperiod valuations; the method does
  not invent a time-weighted return or SPY reinvestment price.
- Hypothetical SPY starts fully invested with fractional shares at the opening
  common mark and incurs no transaction friction. It acquires only subsequent
  ex-date entitlements. Normal cash distributions enter on the published payable
  date and remain unreinvested. This is an explicit hypothetical cash schedule,
  not certification of the broker's actual payment date. Unpaid receivables are
  excluded from both cash-basis NAVs. Special/foreign/due-bill dividends,
  canceled/unconfirmed announcements and in-window share-changing actions are
  unsupported. The broad corporate-action query includes all types and incomplete
  records; an unknown response is not treated as an empty calendar.
- Optional overhead is a separately declared USD/month budget allocated by
  calendar seconds in an explicit timezone. It is a scenario, not a broker fee
  or payment receipt. It never changes the archived input or original trade P&L.

Successful output is `reconciled_asof`, and `comparison_eligible=true` refers
**only** to this declared cash-basis comparison. `research_activation=false` and
`final_or_accrued_comparison_eligible=false` always remain explicit. Late or revised
provider records can change an as-of result. This is no statistical edge claim,
strategy promotion, frozen experiment activation or guaranteed profit.

## Real retained evidence

The opening capture is the unchanged September 20 archive of September 18 close.
Root performed the new GET-only capture on September 28, after the earlier repair
deadline. It returned nine GETs, two stable cash/position/activity observations,
exact September 25 final-minute marks and 15 cash-dividend announcements. The
committed test fixture replaces identifiers but preserves amounts, times and
request provenance, and records the original file SHA256.

For September 18 20:00 UTC through September 25 20:00 UTC:

| Item | USD / shares |
|---|---:|
| Opening cash/NAV, no held shares | 95,582.59 |
| Eleven incremental AMD buys (three origins 21/21/18) | −37,089.04 |
| Ending cash | 58,493.55 |
| Ending AMD quantity | 60 |
| AMD common mark | 630.47 |
| Ending reconstructed NAV | 96,321.75 |
| Cash and quantity residuals | 0.00 / 0 |
| Account cash-basis marked gain | 739.16 |
| Account return | 0.773320748% |
| SPY common marks | 761.62 → 771.35 |
| Equal-opening-capital hypothetical SPY gain | 1,221.10580171214 |
| SPY return | 1.277539981% |
| Account minus SPY gain | −481.94580171214 |
| Declared $150/month overhead, seven calendar days | 35.00 |
| Account marked gain after that budget scenario | 704.16 |

No external flows, paid account distributions or fees were returned in this
interval; the returned SPY announcements imply no entitled cash payment in it.
This includes unrealized AMD gains and is **not** a completed $200/week trial.
It supplies one real usable seven-calendar-day comparison (five trading sessions),
not a complete historical return series. The older retained September 11–18 pair
still lacks an AVGO September 11 common-minute mark after reversing its later
sale, so that interval is explicitly blocked. No history is silently reconstructed
from a daily close or current price.

## Use and scope

Offline, with no settings load, network or journal access:

```text
python -m alphaos.reports.economic_report --input inputs.json --output NEW-result.json --markdown NEW-report.md
```

Add `--monthly-overhead-usd 150 --budget-timezone Asia/Singapore` only as an explicit
budget scenario. Existing output files and existing embedded budget assumptions
are not overwritten. Files are created mode 0600.

The separately invoked `economic_capture --collect --start opening-capture.json
--output NEW-inputs.json` is paper-only and GET-only, capped at 37 requests with
15-second request timeouts. It uses the configured feed, exact endpoint allowlists,
no redirects and no credential-bearing error output. Partial failure preserves
sanitized evidence. It never registers scheduled work. Historical opening receipt
hashes and the live ledger are not modified.

## Source semantics

[Alpaca account activities](https://docs.alpaca.markets/us/docs/account-activities)
distinguishes individual and cumulative fill quantities and describes nontrade
`date` as occurrence or settlement date. The
[activities API](https://docs.alpaca.markets/us/reference/getaccountactivities-2)
filters on record creation time, which is why old retained activities must remain
covered and matched. [Corporate-action documentation](https://docs.alpaca.markets/us/reference/corporateactions-1)
uses process-date query bounds and warns of delayed availability; the capture
requests `data_quality=all` with a wide explicit window. No pagination result is
claimed to guarantee all future economic information.

Validation: 193 affected tests passed, including 73 new conservation/source/report
regressions; Ruff and mypy passed for all three new source modules. The retained
real before/after probe is blocked by the old adjacent-session bridge and
reconciled by the new method. No production action is part of these tests.
