# ENTRY-TTL-1 — Working-Order Staleness Watchdog

**Status:** SPEC — approved for build, not built.
**Spec author:** Fable 5 (strategy), 2026-07-31.
**Build protocol:** T4 (dedicated branch → Sonnet build → 2 independent Opus
audits → fix → full validation → hold for explicit merge instruction).

---

## 1. Problem (observed live, 2026-07-29)

AlphaOS's staleness protection ends at broker submission. The proposal TTL
(PR6) is checked at approval time — but once an approved bracket order is
submitted to Alpaca it is GTC (the protective legs must survive overnight,
so the entry leg inherits GTC), and nothing ever reviews it again.
`OrderManager.reconcile()` (alphaos/execution/order_manager.py:335) only
*mirrors* broker state; it never cancels, re-prices, or re-checks a thesis.

Live example: AAPL bracket, limit buy 331.865, submitted 2026-07-24, still
`accepted` five days later with the stock at ~340 (+2.5% above entry). For a
momentum thesis this is adverse selection in its purest form: the order can
now only fill on a pullback through the limit — i.e. precisely when the
momentum that justified the trade is failing — on analysis whose age is
unbounded (the 5-day max-holding clock starts at *fill*, not submission).

## 2. Objective

Extend the existing fail-safe TTL philosophy past the broker boundary: an
unfilled entry order is automatically cancelled when its thesis has aged
out or the market has moved decisively past it. Cancellation only ever
*reduces* prospective exposure — it never closes a position, never submits
an order, never touches protective legs of a filled position.

## 3. Mechanism

### 3.1 Triggers (either one fires → cancel)

For every Alpaca-paper bracket order whose entry is unfilled (state in
`submitted`/`accepted`, no `positions` row):

1. **Entry-order TTL** — `N` **trading days** have elapsed since
   `submitted_at` (reuse `alphaos/util/market_calendar.py` arithmetic;
   weekends/holidays never count). Default **N = 2**.
2. **Adverse drift** — the last *usable* price (freshness-guard-clean
   snapshot) has moved beyond the intended entry in the adverse direction
   by more than `X%`:
   - long: `last_price >= intended_entry_price * (1 + X/100)`
   - short: `last_price <= intended_entry_price * (1 - X/100)`
   Default **X = 2.0**.

Fail-safe split, deliberate: the TTL leg depends only on time and must
work even when market data is unavailable/stale (a data outage must never
let orders live forever). The drift leg is skipped — never guessed — when
no usable fresh snapshot exists.

### 3.2 Where it runs

Rides `OrderManager.reconcile()` inside `run_monitor_once()` — reconcile
is the only code that already reads every open order from the broker each
monitor pass; a separate job would duplicate that read and race it. The
staleness check runs AFTER the pass's normal state sync (so a fill that
already happened is seen first) and only then cancels via the existing
`AlpacaClient.cancel_order()` (alphaos/broker/alpaca_client.py:171).

**Explicit law amendment required** (this is the one architectural change
an auditor must scrutinize): `run_monitor_job`'s docstring currently says
the monitor never submits/cancels/closes. Amend it to: the monitor may
initiate exactly ONE class of broker action — cancelling an UNFILLED
ENTRY order under this mechanism. It still never closes positions, never
submits orders, never cancels the protective legs of a filled position.

### 3.3 Kill switch

Staleness cancellation KEEPS RUNNING when the kill switch is engaged —
same reasoning as the monitor exemption itself: it strictly reduces
prospective exposure. (An engaged kill switch that leaves stale GTC buy
orders live at the broker would be a hole in the kill switch.)

### 3.4 Effects of a cancel

- Broker: `cancel_order(broker_order_id)` — builder MUST verify against
  the paper API that cancelling the parent entry also cancels the held
  TP/SL legs (Alpaca's documented bracket behavior; verify, don't assume).
- `paper_orders.state` → `cancelled`; `order_events` transition recorded
  with new reason code `ORDER_STALE_CANCELLED` and a detail payload naming
  which trigger fired (ttl / drift), the age, and the drift %.
- `trade_proposals.status` → `expired` (reuse the existing EXPIRED status
  + additive-lifecycle law; do NOT invent a new proposal status), with the
  same reason code in the audit fields.
- Alert via `alerts.send_alert` (normal priority) — CK's standing rule:
  pings over silence. Never a silent cancel.

### 3.5 Races and edge cases

- Cancel of an order that filled between read and cancel: broker errors →
  treat as benign, log INFO, let the next reconcile pass mirror the fill.
  NEVER mark the proposal expired in this case.
- `partially_filled` entries are OUT OF SCOPE for auto-cancel in v1:
  detect, alert loudly, take no action (the filled portion is a real
  position; the right remainder-handling policy is an operator decision).
- Already-cancelled orders: idempotent no-op, no repeat alert.

### 3.6 Config axes (settings.py, two-tier validation per house convention)

| Env var | Default | Meaning |
|---|---|---|
| `ENTRY_ORDER_STALENESS_ENABLED` | `true` | Master switch for auto-cancel. |
| `ENTRY_ORDER_TTL_TRADING_DAYS` | `2` | TTL leg; `0` disables this leg. |
| `ENTRY_ORDER_MAX_ADVERSE_DRIFT_PCT` | `2.0` | Drift leg; `0` disables this leg. |

Validation: negatives rejected at load; master on + both legs 0 → loud
startup warning (configured-but-inert). Defaults ship ON — cancellation is
exposure-reducing, the safe direction (contrast with unattended
auto-approval, which shipped opt-in because it *adds* exposure).

### 3.7 Operator CLI (small, part of this ticket)

`python -m alphaos cancel_order <proposal_id|order_id>` — targeted manual
cancel through the SAME code path (reason `ORDER_CANCELLED_BY_OPERATOR`),
with the same audit trail. Today the only options are the Alpaca dashboard
(invisible to the journal until reconcile) or `flatten` (nuclear).

### 3.8 Observability

Daily brief / Tonight "Working orders" section gains two columns: age in
trading days and drift-vs-entry % (computed at render time, read-only).
Cancel events surface as system_events + the alert; no new dashboard.

## 4. Non-goals (explicitly out of scope)

- Re-pricing / order modification ("chase the market") — cancel only.
  A fresh scan can always propose anew at current prices.
- Re-running the AI evaluation on pending orders.
- Any handling of `simulated_internal` orders (they fill instantly).
- Cancelled-entry counterfactual outcome tracking (EVID family, later).
- Partial-fill remainder policy (alert-only in v1; see 3.5).
- Real-money paths (paper only, like all execution today).

## 5. Required tests

Injectable clock throughout (house law: no wall-clock sleeps, no lucky-
timing greens). Mock broker for cancel-call assertions.

1. TTL fires at exactly N trading days; N-1 does not; weekend/holiday
   spans counted correctly (reuse market-calendar fixtures).
2. Drift fires for long above threshold and short below; direction never
   inverted; at-threshold boundary behavior pinned.
3. Stale/unusable snapshot → drift leg skipped, TTL leg still fires.
4. Filled-between-read-and-cancel race → benign, proposal NOT expired,
   fill mirrored next pass.
5. Partial fill → alert, no cancel.
6. Kill switch engaged → cancel still fires.
7. Master flag off / both legs 0 → no-op (and the startup warning fires
   for configured-but-inert).
8. Proposal transitions to `expired` additively; row never deleted;
   order_events chain intact with reason + trigger detail.
9. Protective legs of FILLED positions are never touched (swap-style
   probe: a filled bracket with open position must survive a pass).
10. Idempotency: second pass over an already-cancelled order does nothing.
11. Alert sent exactly once per cancel.
12. CLI targeted cancel: works by proposal_id and order_id, refuses
    unknown ids, same audit trail, distinct reason code.
13. Monitor-law amendment is tested structurally: the only broker-mutating
    call reachable from `run_monitor_once` is `cancel_order` (no
    submit/close paths).

## 6. Deployment note (operator awareness)

On first deploy, any ALREADY-stale working order is cancelled on the first
monitor pass — including the live AAPL 331.865 order (age ≥ 5 trading
days) if it is still unfilled at merge time. This is the intended
behavior, flagged here so it is not a surprise.

## 7. Open decision for the operator (non-blocking, defaults chosen)

Defaults N=2 trading days / X=2.0% are Fable's judgment call: tight enough
that a momentum thesis can't silently age out, loose enough that a normal
intraday pullback entry (the whole point of a limit below market) isn't
cancelled the day it's placed. Both are plain config axes — tune freely
post-merge without a code change.
