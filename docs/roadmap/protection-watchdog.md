# Follow-up: Broker protection watchdog / naked-position detector

**Origin:** [`docs/incidents/2026-07-02-meta-protection-mismatch.md`](../incidents/2026-07-02-meta-protection-mismatch.md) — a live paper position lost both broker-side protective orders (day-TIF bracket legs expired at session close) and sat naked for ~19 hours, undetected, because nothing monitors position protection between manual runs.
**Status:** Not started. Design-level requirements only, captured here so it isn't lost.
**Relationship to the Fable 5 review roadmap:** complements PR3 (scheduler v1.5, `FABLE_REVIEW_RESPONSE.md` §16) — the watchdog is the check that PR3's monitor pass should run, but is independently valuable even before a scheduler exists (it makes the *existing* manual `monitor_once` catch this class of failure).

## Problem
`OrderManager.reconcile()` and the local watchdog (`PositionManager.monitor()`) both assume a broker-managed position's protection is intact unless a bracket leg fills. Neither checks whether the protective orders are actually still *live*. A position can end up naked — bracket legs expired/canceled without filling — and nothing in the system notices. Separately, `reconcile()` was confirmed (during the incident) to not detect a position closing via any path other than a bracket-leg fill (e.g. a flatten, or in principle any other external close) — it silently leaves the local ledger stale rather than erroring or flagging.

## Requirements for the future PR
1. **Every monitor pass verifies each open broker-managed position has live broker-side protective orders.** For each open position with `execution_source=alpaca_paper`, fetch its associated stop/target orders and assert both are in an open/working state.
2. **If protective orders are missing, mark a CRITICAL incident** (system event + a dedicated status flag readable from `alphaos status` / the dashboard) — this must be loud, not a log line. This is a direct extension of the fail-safe *visibility* pattern already shipped for the labeller (`feat/labeller-failsafe-visibility`, still unmerged) — same principle, applied to position protection instead of AI output quality.
3. **Block new entries while unresolved.** A protection-integrity failure should halt new proposals from being approved until acknowledged/resolved, the same way the kill switch does.
4. **Alert the user.** Reuse the existing `alphaos/notifications/ntfy_client.py` path if available, or at minimum make it unmissable in `alphaos status` output.
5. **Repair-or-flatten only under an explicit, opt-in policy** — never automatically re-place orders or close a position without either (a) a user-configured auto-repair policy the user explicitly turned on, or (b) manual confirmation, mirroring how User Override and manual approval already work in this codebase. Default behavior on detection should be: alert + block new entries, nothing more.
6. **Reconcile the local ledger with broker truth as part of this same pass** — this incident showed `reconcile()` does not detect a non-bracket-leg close. The watchdog should independently confirm local `positions.status` matches the broker's actual position state (open/closed) for every broker-managed position, not just react to bracket-leg fills.
7. **Restart-recovery test** — kill the process with an open, broker-managed position, restart, and confirm the watchdog correctly re-discovers the position's true protection state (this is also on the Fable 5 live-readiness checklist, item 9 — this incident is a concrete argument for prioritizing it).
8. **Broker/order-status mismatch tests** — hermetic tests using a fake broker client that returns each of: legs both live (healthy), one leg missing, both legs missing, position closed at broker but open locally (the exact scenario from this incident), position open at broker but closed locally.

## Also worth considering (not a hard requirement, flag for design discussion)
- Should multi-day-hold brackets (`max_holding_days > 1`) be submitted `time_in_force=gtc` instead of `day` for the exit legs? This incident's root cause was a day-TIF/multi-day-playbook mismatch specifically. A watchdog *catches* the symptom; fixing the TIF would prevent this exact scenario from recurring at all. Both are probably worth doing, but the TIF question is more standalone and simpler — could plausibly land as its own small PR ahead of the full watchdog design.

## Explicitly out of scope for now
Per the incident-response instructions this item was captured under: no code was written for this item yet. Do not start it without discussing sequencing against PR3 (scheduler) first — see `HANDOVER.md` §9.
