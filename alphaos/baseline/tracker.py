"""BASELINE: journal-aware orchestration (house pattern #4's "orchestration
module wires fail-safe" layer) -- looks up ATR/card/settings inputs and
calls the pure rules in ``alphaos.baseline.rules``, then journals + later
resolves the two frozen-rule decisions per candidate.

Two phases, mirroring ``alphaos.learning.outcomes_tracker`` exactly:

* ``record_shadow_baseline_decisions`` -- called ONCE per candidate, inline
  in the orchestrator's scan loop, strictly AFTER the live decision fully
  resolves (spec item 3). NEVER raises: any failure here must be invisible
  to the live decision (shadow law) -- caught, logged, skipped.
* ``resolve_pending_baseline_decisions`` -- called from
  ``Orchestrator.outcomes_update()``, strictly after the existing outcome-
  ledger calls (spec item 4, mirroring Attribution v2's own wiring). Fetches
  forward bars and replays via the ONE replay engine
  (``alphaos.learning.outcomes_engine.replay_bracket``) -- never a second
  replay implementation.
"""

from __future__ import annotations

from typing import Optional

from alphaos.baseline.rules import BASELINE_HOLD10_SUFFIX, RULE_FUNCTIONS
from alphaos.cards.registry import get_card_by_id
from alphaos.constants import Severity
from alphaos.data.atr import ATR_RULES_V1
from alphaos.learning.outcomes_engine import DEFAULT_REPLAY_WINDOW_DAYS, replay_bracket
from alphaos.learning.outcomes_tracker import UNAVAILABLE_AFTER_DAYS
from alphaos.util import timeutils
from alphaos.util.ids import new_id

# HOLD-2 (2026-08-05, spec section 3.4 / operator ruling D2, logged in the
# spec's own S9 row): BASELINE's v1 arms are PINNED to catalyst_momentum_v2
# BY EXPLICIT ID -- deliberately NOT get_default_card()/settings.
# active_card_id. The whole point of this pin is that an operator swapping
# the live ACTIVE_CARD_ID (or any future default-card supersession) must
# NEVER move these already pre-registered v1 arms' hold window
# mid-accumulation (their pre-registration has analysis_not_before
# 2026-09-07). A literal card_id here, never a reference to any "default"
# constant, is deliberate -- see get_card_by_id()'s own docstring for why
# this is the one sanctioned way to bypass the active-default resolution.
BASELINE_V1_PINNED_CARD_ID = "catalyst_momentum_v2"

# HOLD-2: the NEW 10-day arm is likewise pinned by explicit id (never the
# active default) -- catalyst_momentum_v3's 10-trading-day
# max_holding_days_default, regardless of whatever ACTIVE_CARD_ID is set to
# live. Its own fresh pre-registration (analysis_not_before 2026-10-05) is
# an operator action taken after merge; this pin is what makes its rows
# comparable to a fixed horizon from the first row onward.
BASELINE_HOLD10_PINNED_CARD_ID = "catalyst_momentum_v3"


def _lookup_atr(journal, symbol: str) -> Optional[float]:
    """SAME query alphaos.ai.openai_client._apply_atr_stop() uses -- one ATR
    lookup shape, not a second implementation."""
    return journal.scalar(
        "SELECT atr_14 FROM atr_history WHERE symbol = ? AND rules_version = ? "
        "ORDER BY market_date DESC LIMIT 1",
        (symbol, ATR_RULES_V1),
    )


def _write_baseline_row(
    journal, *, cand, symbol, scan_batch_id, rule_version, out, setup_card_id,
    decided_at, lineage_id,
) -> None:
    """Shared row-shaping + insert for ONE (candidate, rule) decision --
    factored out of ``record_shadow_baseline_decisions`` (HOLD-2) so the
    pinned v1 arms and the new 10-day arm write through the exact same
    shaping logic; only ``rule_version`` and the ``out`` dict's own
    ``max_holding_days_default``-derived values differ between callers."""
    decision = out["decision"]
    # A directly-observed fact (no position ever opened), matching
    # Attribution v2's own 0-is-a-fact convention -- NOT a substitute
    # for missing data (that's the 'unavailable' branch below).
    # entry_fill_status (spec item 4, added 2026-07-09 scope/safety-
    # audit finding LOW-1): 'assumed_filled' for every 'propose' row
    # -- by construction, _build_bracket() only reaches 'propose'
    # when a real last_price was available (a missing/unusable price
    # already resolves to 'unavailable' below), so there is no live
    # signal in v1 that would ever produce 'needs_review'. The
    # column exists now so a future version (e.g. one that also
    # considers snapshot staleness/quality) can populate it without
    # a migration; None for no_action/unavailable rows, which never
    # represent an entry attempt at all.
    if decision == "no_action":
        replay_status, replay_result, replay_r = "complete", "no_action", 0.0
        entry_fill_status = None
    elif decision == "unavailable":
        replay_status, replay_result, replay_r = "unavailable", None, None
        entry_fill_status = None
    else:
        replay_status, replay_result, replay_r = "pending", None, None
        entry_fill_status = "assumed_filled"

    try:
        journal.insert("shadow_baseline_decisions", {
            "baseline_decision_id": new_id("basedec"),
            "candidate_id": cand["candidate_id"],
            "symbol": symbol,
            "scan_batch_id": scan_batch_id,
            "rule_version": rule_version,
            "decision": decision,
            "decision_reason": out.get("decision_reason"),
            "direction": out.get("direction"),
            "entry": out.get("entry"),
            "stop": out.get("stop"),
            "target": out.get("target"),
            "max_holding_days": out.get("max_holding_days"),
            "setup_card_id": setup_card_id,
            "entry_fill_status": entry_fill_status,
            "input_sha": out["input_sha"],
            "decision_at_utc": decided_at,
            "replay_status": replay_status,
            "replay_result": replay_result,
            "replay_r": replay_r,
            "lineage_id": lineage_id,
        })
    except Exception as exc:  # noqa: BLE001 - a partial-uniqueness race must never surface
        journal.log_system_event(
            Severity.WARNING, "baseline",
            f"{symbol}/{rule_version}: shadow_baseline_decisions insert failed: {exc}",
        )


def record_shadow_baseline_decisions(
    journal, settings, cand, *, scan_batch_id: Optional[str] = None,
    decision_at_utc: Optional[str] = None, lineage_id: Optional[str] = None,
) -> None:
    """Journal one row per rule_version PER ARM for ``cand`` -- 2 rows for
    the pinned v1 arms (unchanged since PR10/BASELINE) PLUS (HOLD-2) 2 more
    rows for the new 10-day arm, additive storage only. Never raises -- a
    shadow-recording failure must never be visible to the live scan loop it
    runs alongside (matches TQS's own "never raises regardless" posture)."""
    try:
        symbol = cand["symbol"]
        row = {
            "symbol": symbol,
            "direction": cand.get("direction"),
            "last_price": cand.get("last_price"),
            "interest_score": cand.get("interest_score"),
        }
        atr_14 = _lookup_atr(journal, symbol)
        # HOLD-2: PINNED by explicit id -- see BASELINE_V1_PINNED_CARD_ID's
        # own module docstring for why this is never get_default_card().
        card = get_card_by_id(BASELINE_V1_PINNED_CARD_ID)
        max_holding_days_default = card.get("max_holding_days_default")
        if max_holding_days_default is None:
            # A malformed card registry entry -- genuinely unexpected (every
            # real card YAML sets this). Raise into the outer try/except
            # (fail-safe: logged, scan continues) rather than silently
            # defaulting to a fabricated hold-days value.
            raise ValueError(f"card {card.get('card_id')!r} has no max_holding_days_default")
        setup_card_id = cand.get("card_id")
        stamp = timeutils.stamp()
        decided_at = decision_at_utc or stamp.utc

        for rule_version, apply_rule in RULE_FUNCTIONS.items():
            # Each rule isolated in its OWN try/except (2026-07-09
            # correctness-audit NIT-2): a field only ONE rule reads (e.g. a
            # poisoned interest_score, read only by threshold_v1) must not
            # suppress the OTHER rule's row too -- matches harness.py's own
            # lesson about per-item isolation not propagating automatically
            # just because two things share a data path.
            try:
                out = apply_rule(
                    row, atr_14=atr_14, min_reward_risk=settings.min_reward_risk,
                    max_holding_days_default=max_holding_days_default,
                )
            except Exception as exc:  # noqa: BLE001 - one rule's failure must never block the other
                journal.log_system_event(
                    Severity.WARNING, "baseline",
                    f"{symbol}/{rule_version}: apply_rule failed: {exc}",
                )
                continue

            _write_baseline_row(
                journal, cand=cand, symbol=symbol, scan_batch_id=scan_batch_id,
                rule_version=rule_version, out=out, setup_card_id=setup_card_id,
                decided_at=decided_at, lineage_id=lineage_id,
            )

        # HOLD-2 (spec section 3.4 / operator ruling D2): the NEW 10-day arm
        # -- the SAME frozen rule logic, replayed a second time under
        # catalyst_momentum_v3's 10-trading-day hold (pinned by explicit id,
        # same law as the v1 arms above). Additive storage only: distinct
        # rule_version labels (the "_hold10" suffix) mean these rows can
        # never collide with the v1 arms' own UNIQUE (candidate_id,
        # rule_version) index, and compute_baseline_report()'s existing
        # BASELINE_RULE_VERSIONS filter silently ignores them -- the
        # pre-registered v1 report is unaffected by construction, no code
        # change needed there. Isolated in its OWN try/except: a failure
        # here (e.g. the v3 card file missing) must never suppress the v1
        # arms recorded above.
        try:
            hold10_card = get_card_by_id(BASELINE_HOLD10_PINNED_CARD_ID)
            hold10_days_default = hold10_card.get("max_holding_days_default")
            if hold10_days_default is None:
                raise ValueError(
                    f"card {hold10_card.get('card_id')!r} has no max_holding_days_default"
                )
            for rule_version, apply_rule in RULE_FUNCTIONS.items():
                try:
                    out = apply_rule(
                        row, atr_14=atr_14, min_reward_risk=settings.min_reward_risk,
                        max_holding_days_default=hold10_days_default,
                    )
                except Exception as exc:  # noqa: BLE001 - one rule's failure must never block the other
                    journal.log_system_event(
                        Severity.WARNING, "baseline",
                        f"{symbol}/{rule_version}{BASELINE_HOLD10_SUFFIX}: apply_rule failed: {exc}",
                    )
                    continue
                hold10_out = {**out, "rule_version": rule_version + BASELINE_HOLD10_SUFFIX}
                _write_baseline_row(
                    journal, cand=cand, symbol=symbol, scan_batch_id=scan_batch_id,
                    rule_version=rule_version + BASELINE_HOLD10_SUFFIX, out=hold10_out,
                    setup_card_id=setup_card_id, decided_at=decided_at, lineage_id=lineage_id,
                )
        except Exception as exc:  # noqa: BLE001 - the 10-day arm must never affect the v1 arms above
            journal.log_system_event(
                Severity.WARNING, "baseline",
                f"{symbol}: hold10 arm recording failed: {exc}",
            )
    except Exception as exc:  # noqa: BLE001 - shadow recording must never affect the live decision
        try:
            journal.log_system_event(
                Severity.WARNING, "baseline",
                f"record_shadow_baseline_decisions failed for "
                f"{cand.get('candidate_id') if hasattr(cand, 'get') else '?'}: {exc}",
            )
        except Exception:  # noqa: BLE001 - best-effort logging must not itself raise
            pass


def resolve_pending_baseline_decisions(journal, bars_provider=None, limit: int = 200) -> dict:
    """Resolve 'pending' shadow_baseline_decisions rows (decision='propose')
    with a bracket replay, via the SAME replay_bracket() the primary
    counterfactual ledger uses -- one replay engine, one truth. Idempotent:
    only 'pending' rows are touched; 'complete'/'unavailable' rows (already
    resolved at write time, see record_shadow_baseline_decisions) are never
    revisited."""
    counts = {"total": 0, "updated": 0, "completed": 0, "skipped": 0, "unavailable": 0}
    if bars_provider is None:
        return counts

    rows = journal.query(
        "SELECT * FROM shadow_baseline_decisions WHERE replay_status = 'pending' "
        "ORDER BY id ASC LIMIT ?", (limit,),
    )
    counts["total"] = len(rows)
    now = timeutils.now_utc()

    for row in rows:
        decision_at = timeutils.parse_iso(row.get("decision_at_utc"))
        if decision_at is None:
            counts["skipped"] += 1
            continue
        age_days = (now - decision_at).total_seconds() / 86400.0
        decision_date = decision_at.date().isoformat()
        bars = bars_provider.get_daily_bars(row["symbol"], decision_date, now.date().isoformat()) or []
        forward_bars = [b for b in bars if b.get("date") and b["date"] > decision_date]
        window_days = row.get("max_holding_days") or DEFAULT_REPLAY_WINDOW_DAYS

        if not forward_bars:
            if age_days > UNAVAILABLE_AFTER_DAYS:
                _mark_baseline_row(journal, row["baseline_decision_id"], "unavailable",
                                   "no_bars_after_window", None, "no_bars_after_window")
                counts["unavailable"] += 1
            else:
                counts["skipped"] += 1
            continue

        replay = replay_bracket(
            row.get("entry"), row.get("stop"), row.get("target"), row.get("direction"),
            forward_bars, max_days=window_days,
        )

        if replay["result"] == "unavailable":
            # entry/stop/target/risk_per_share genuinely invalid -- this can
            # NEVER resolve regardless of how many more bars arrive.
            _mark_baseline_row(journal, row["baseline_decision_id"], "unavailable",
                               None, None, replay["replay_exit_reason"])
            counts["unavailable"] += 1
            continue

        if replay["result"] == "neither" and len(forward_bars) < window_days:
            # The window has NOT actually elapsed yet -- more bars may still
            # arrive next call; a "neither" here would be a premature,
            # not-yet-final mark-to-market read (mirrors
            # update_pending_outcomes' own bars_used>=N completeness gate).
            if age_days > UNAVAILABLE_AFTER_DAYS:
                _mark_baseline_row(journal, row["baseline_decision_id"], "unavailable",
                                   None, None, "window_never_completed")
                counts["unavailable"] += 1
            else:
                counts["skipped"] += 1
            continue

        # Resolved: a level was hit, the window genuinely elapsed with
        # neither hit (a real mark-to-market read), or same-bar ambiguity
        # (replay_r stays None in that one sub-case -- matches the primary
        # ledger's own convention of a 'complete' row with a null replay_r).
        _mark_baseline_row(journal, row["baseline_decision_id"], "complete",
                           replay["result"], replay["replay_r"], replay["replay_exit_reason"])
        counts["updated"] += 1
        counts["completed"] += 1

    return counts


def _mark_baseline_row(journal, baseline_decision_id: str, replay_status: str,
                       replay_result, replay_r, replay_exit_reason) -> None:
    journal.conn.execute(
        "UPDATE shadow_baseline_decisions SET replay_status = ?, replay_result = ?, "
        "replay_r = ?, replay_exit_reason = ? WHERE baseline_decision_id = ?",
        (replay_status, replay_result, replay_r, replay_exit_reason, baseline_decision_id),
    )
    journal.conn.commit()
