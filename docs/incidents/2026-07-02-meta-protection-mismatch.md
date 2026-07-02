# Incident: META paper position lost broker-side protection (silent)

**Date detected:** 2026-07-02 (during a routine handover-checkpoint verification pass)
**Date resolved:** 2026-07-02
**Severity:** Real execution-safety finding, treated as an incident despite paper-only capital.
**Status:** Resolved (position flattened, ledger reconciled). Root cause known. Systemic fix not yet built (see follow-up).

## Summary
A live Alpaca **paper** position (META, opened 2026-07-01) lost both of its broker-side protective orders (stop and target) at the end of its first trading session. Because AlphaOS has no scheduler/daemon, nothing noticed or re-submitted protection on day 2. The position then sat naked while price fell through the original stop level, with the local ledger continuing to believe it was protected. No real money was ever at risk (paper account only), but the failure mode — a risk control silently disappearing with no alert — is exactly the class of bug that would be dangerous in live trading.

## Timeline
| Time (UTC) | Event |
|---|---|
| 2026-07-01 16:03:25 | Bracket order submitted (entry + stop 594.99 + target 666.45), `time_in_force=day` on all three legs |
| 2026-07-01 16:03:57 | Entry leg filled: 41 shares @ 618.78 |
| 2026-07-01 20:00:00 | Market close (day 1) |
| 2026-07-01 20:02:29 | Stop leg → `canceled`; target leg → `expired` (same timestamp — both day-TIF legs expiring together at end-of-session) |
| 2026-07-01 → 2026-07-02 | Position remains open, broker-side, with **no protective orders**. No AlphaOS process ran to notice (no scheduler exists). |
| 2026-07-02 (detected) | Handover-checkpoint verification (a routine, unrelated read-only check) surfaced the mismatch: broker price (587.72) already below the original stop (594.99), local ledger still showed `stop_price=594.99` and a stale `current_price=618.71` from the entry-day sync |
| 2026-07-02 | Incident response: re-verified read-only, confirmed root cause, user confirmed flatten, executed, reconciled ledger |

## Broker state (before action)
- Position: META, 41 shares LONG, avg entry 618.78, current price 587.725, unrealized P&L −$1,273.26
- Orders: entry (`filled`), stop (`canceled`, `time_in_force=day`), target (`expired`, `time_in_force=day`) — both legs updated at `2026-07-01T20:02:29Z`
- Zero live protective orders. Zero other open orders/positions on the account.

## Local ledger state (before action)
- `positions` row: `status=open`, `stop_price=594.99`, `target_price=666.45`, `current_price=618.71` (stale — last synced ~2 minutes after entry fill, never updated since)
- Believed the position was protected. Had no signal that the broker-side legs had expired.

## Mismatch
Broker truth (naked, price already through stop) diverged from local-ledger belief (protected, stale price) for roughly 19+ hours with no alert anywhere in the system.

## Root cause — KNOWN
AlphaOS submits bracket orders with `time_in_force=day` (Alpaca's default for a bracket unless explicitly overridden). Alpaca correctly expires unfilled day orders at session close — this is standard, expected broker behavior, not a broker bug. The gap is entirely on AlphaOS's side:
1. The bracket's exit legs are submitted as **day** orders, even though the swing playbook this trade came from has `max_holding_days=5` — a structural mismatch between the playbook's intent (multi-day hold) and the order's actual lifetime (single session).
2. AlphaOS has **no scheduler/daemon** (confirmed, not new information — HANDOVER.md has flagged this gap for several checkpoints). Every pipeline step, including `monitor_once` (which is what would reconcile broker vs. ledger state), is a manual one-shot CLI invocation. With nothing running unattended, a day-2+ loss of protection has no chance of being noticed until a human happens to check.
3. `OrderManager.reconcile()` only detects a position close when a **bracket leg itself fills** (`take_profit`/`stop_loss` role, `state=filled`). It does not detect — and was never designed to detect — a position closing via an unrelated order (e.g. a manual flatten, or in a live scenario, protection simply being gone with the position still open and drifting). This was independently confirmed during the incident response: running `monitor_once` after the flatten reported `reconciled: 1` but `exits: []`, `open_positions: 1` — the standard reconcile path did not notice the position had closed at the broker. It had to be reconciled manually via a direct `close_position()` call.

## Action taken
User was given the read-only findings and a recommendation (flatten, since the original risk control had already failed and price was already through it), and explicitly confirmed. Action:
1. `alphaos flatten` (whole-account; verified beforehand that META was the only open position/order on the account, so this was equivalent to a META-only close — no other symbol was touched).
2. Broker-side result: 0 orders cancelled (none were open), 1 position closed. New market sell order filled: 41 @ 586.70439.
3. Re-verified broker state (read-only): position closed, 0 open positions on the account, 0 dangling META orders. Original stop (`canceled`) and target (`expired`) legs remain visible in their terminal states for audit — nothing was deleted or overwritten.
4. Local ledger did **not** auto-reconcile (see root cause #3 above) — manually reconciled via `PositionManager.close_position()` (the same code path every other exit in the system uses, not a raw SQL edit) with the real broker fill data:
   - `exit_reason = "protection_mismatch_manual_flatten"`
   - `triggered_by = "manual_incident_flatten"`
   - `execution_source = "alpaca_paper"`, `broker_order_id` = the real flatten order id
   - Auto-classified `classification = "risk-control"` (accurate — unknown exit reasons default conservatively per `exit_rules.classify_exit`)

## Broker state (after action)
- 0 open positions on the account. 0 dangling META orders.
- META order history intact and unmodified: entry `filled`, stop `canceled`, target `expired`, flatten exit `filled` @ 586.70439 — full audit trail preserved.

## Local ledger state (after action) — reconciled
- `positions.status = closed`
- `exits`: `exit_reason=protection_mismatch_manual_flatten`, `classification=risk-control`, `net_pnl=-1320.04`
- `trade_outcomes`: `realized_r=-1.348`, `mfe=0.0`, `mae=-1.3991`, `mfe_mae_source=live_tracked`, `outcome_classification=loss`

## Why this matters for live-readiness
This is precisely the risk class Fable 5's architecture review flagged before this incident occurred (`FABLE_REVIEW_RESPONSE.md`, safety risk #3: *"the inverse variant — a failure that silently degrades protection — is the nightmare case"*), now observed for real rather than hypothetically. In a live-money context this would mean a position sitting completely unprotected for many hours with no alert, discoverable only by manual luck. It also concretely validates why the review ranked "cadence" (a scheduler) and measurement/monitoring ahead of any autonomy work — a system with no unattended monitoring cannot safely hold anything past a single session.

## Follow-up
See [`docs/roadmap/protection-watchdog.md`](../roadmap/protection-watchdog.md) for the tracked follow-up item (broker protection watchdog / naked-position detector). A second, narrower fix worth tracking alongside it: multi-day-hold brackets (swing playbooks with `max_holding_days > 1`) should not be submitted `time_in_force=day` in the first place — but per this incident's scope, no code fix was made; this is deliberately left as a design decision for a future PR, not something to patch reactively.
