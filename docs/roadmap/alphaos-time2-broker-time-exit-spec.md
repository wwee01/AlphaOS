# TIME-2 — Enforce the time exit on broker-managed positions

**Status:** SPEC. Fable strategist, 2026-08-26. Ordered by CK ("can we enforce the
max holding days"), completing TIME-1 part 3, which TIME-1 deliberately did not build.
**Ceremony:** Sonnet builder in an isolated worktree, blind Opus audit, merge on
explicit operator instruction. **This is live-execution code — it closes positions.**

**Operator ruling (CK, 2026-08-26):** enforcement uses **each position's OWN stamped
`max_holding_days`**, not a forced constant. The evaluator picks 1–10 per setup under
prompt v4 (recent live picks: XLV=10, PLTR=5, MU=5) and that judgment is what the card
and prompt were built to express. A legacy position stamped 3 (card-v2 era) exits at 3.

---

## 1. Why this is not a one-line change

`max_holding_days` has NEVER closed a live position — 0 `time_expiry` exits in the
system's history. Two facts combine:

1. `PositionManager.monitor()` skips `_check_exit` entirely for
   `execution_source='alpaca_paper'` (all 8 positions ever), because the local
   watchdog must never fight the broker's OCO.
2. An Alpaca bracket has a stop leg and a target leg and **no time leg**.

`PositionManager` is under a grep-enforced invariant
(`test_entry_ttl.py::test_position_manager_monitor_and_protection_watchdog_never_touch_the_broker`)
that its source must never contain `self.alpaca.`, `.cancel_order(`, or
`.submit_bracket(`. **Do not weaken or edit that test.** Enforcement therefore lives in
`OrderManager`, which already holds the narrow broker exemption ENTRY-TTL-1 established.

**A capability gap the builder must close first:** `AlpacaClient.submit_order()` raises
(`use submit_bracket for real paper execution`), and `flatten_paper()` is
all-or-nothing. There is no per-symbol close. Add one — Alpaca's
`DELETE /v2/positions/{symbol}` — as `AlpacaClient.close_position(symbol)`, guarded by
the same `preflight()` every other broker method calls.

## 2. Design

**Placement.** New `OrderManager.enforce_time_exits()`, called from `reconcile()`
immediately AFTER the existing `_cancel_stale_entries()` pass — mirroring ENTRY-TTL-1's
own wiring, and for the same reason: the state-sync loop has already run, so a fill
that happened THIS pass is reflected in `positions` before any exit decision is made.

**Eligibility.** A position qualifies only when ALL hold:
- `execution_source == 'alpaca_paper'` and `status == 'open'`
- its own `max_holding_days` is a positive int, and
  `trading_days_between(opened_market_date, market_date()) >= max_holding_days`
- **the arithmetic comes from the SAME helper `_check_exit` uses** — import it, never
  write a second definition of the two-guard trading-day rule (`is_trading_day(now)`
  AND `trading_days_between(...) >= max_days`).
- the broker still reports the position open this pass (never act on a stale view)

**Sequence, in this order, per position:**
1. Cancel the live protective legs (stop and target) via the broker.
2. **Verify** the cancels — re-read; a non-raising cancel is not proof (the ENTRY-TTL-1
   lesson). If either leg is not confirmed cancelled, **STOP: do not close.** Log,
   alert, retry next pass. A live leg plus a market close is how you get a short.
3. Close the position via the new `close_position(symbol)`.
4. Record the exit through the SAME path every other exit uses so
   `trade_outcomes`/`realized_r` stay consistent — `exit_reason='time_expiry'`. Do not
   invent a second exit-recording path and never write raw SQL.

**Fail directions (all must fail toward "leave the position alone, protected"):**
- cancel unverified → no close, alert, retry next pass
- close call raises → position stays open with legs intact (re-place if step 1 removed
  them and step 3 failed — a position must never be left naked; if re-placement also
  fails, open a CRITICAL protection incident)
- stale/unusable market data → skip this pass entirely (inherit `position_manager`'s
  existing "will not exit on bad data" refusal)
- broker not connected → skip, never simulate a broker exit

**Config.** New `TIME_EXIT_ENFORCEMENT_ENABLED`, **default false**, validated at load,
in the config fingerprint, documented in `.env.example`. Merge is DARK: with the flag
off, behavior is byte-identical to today. The existing
`TIME_EXIT_BREACH_ALERT_ENABLED` (detection/alerting) is INDEPENDENT and stays —
detection tells you, enforcement acts.

## 3. Out of scope

Position sizing, risk limits, the stop/target rules, the earnings gate, AILEG-1's
replay windows, the recompute, and anything touching `PositionManager`'s broker
invariant or the two ratchet tests.

## 4. Test obligations

1. Flag OFF → byte-identical to today (differential probe: same fixtures, same written
   rows, no exits).
2. Flag ON, position past its OWN window → cancel-then-verify-then-close, IN THAT ORDER
   (assert call ordering, not just the end state).
3. Flag ON, position NOT past its window → untouched.
4. **Per-trade window is honored**: a position stamped 5 exits on day 5; one stamped 10
   does not exit on day 5. This is the operator's ruling — pin it.
5. Legacy position stamped 3 exits at 3 (no floor, no forcing).
6. **Cancel unverified → NO close is submitted** (the safety test that matters most).
7. Close raises after legs were cancelled → position ends protected, or a CRITICAL
   incident is opened; never left naked.
8. Stale data → no action.
9. `exit_reason='time_expiry'` reaches `trade_outcomes` with a correct `realized_r`
   through the normal exit path.
10. Both existing ratchet tests still pass, unmodified.
11. Cold-import subprocess check on every touched module.
