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

from alphaos.baseline.rules import (
    BASELINE_HOLD10_SUFFIX,
    BASELINE_RULE_VERSIONS,
    BASELINE_RULE_VERSIONS_HOLD10,
    RULE_FUNCTIONS,
)
from alphaos.cards.registry import get_card_by_id
from alphaos.constants import Severity
from alphaos.data.atr import ATR_RULES_V1
from alphaos.learning.outcomes_engine import replay_bracket
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

# Audit-fixup HOLD-2 (STATUS CORRECTION item 3, 2026-08-06, both audits
# convergent HIGH): UNAVAILABLE_AFTER_DAYS (15.0 calendar days) is a
# CALENDAR give-up on a window sized for the OLD 3-trading-day hold. A
# 10-trading-day window spans more than 15 calendar days on ~9% of trading
# dates (weekends/holidays inside the window) -- and give-up marks a row
# 'unavailable' ONLY on the 'neither' (0-R) branch, since a stop/target hit
# resolves immediately regardless of age. That is DIRECTIONAL censoring:
# hold10's own 0-R outcomes are selectively dropped from the sample while
# every winning/losing outcome survives, biasing mean_ai_delta_r away from
# zero. HOLD-1 already solved this exact problem for its own 10-day family
# by using a longer give-up; this reuses that number. v1 arms keep
# UNAVAILABLE_AFTER_DAYS (15.0) byte-unchanged -- only rows whose
# rule_version carries BASELINE_HOLD10_SUFFIX use this wider one.
HOLD10_UNAVAILABLE_AFTER_DAYS = 30.0


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
                # Audit-fixup HOLD-2 (S-a): setup_card_id stamps the card
                # that ACTUALLY supplied this row's max_holding_days_default
                # (BASELINE_V1_PINNED_CARD_ID) -- not cand.get("card_id"),
                # the candidate's OWN live card assignment (which can be a
                # totally unrelated card, e.g. post_earnings_reaction, and
                # never governs a baseline row's bracket window). This is
                # provenance for "which card's policy drove this shadow
                # decision," a different question from "which card did the
                # live pipeline assign this candidate."
                rule_version=rule_version, out=out, setup_card_id=BASELINE_V1_PINNED_CARD_ID,
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
                # Audit-fixup HOLD-2 (S-b): no "rule_version" key needed in
                # the dict passed as `out` -- _write_baseline_row() takes
                # rule_version as its own explicit keyword, never reads it
                # off `out` (the pre-fixup `{**out, "rule_version": ...}`
                # spread wrote a dead key that was never consumed).
                _write_baseline_row(
                    journal, cand=cand, symbol=symbol, scan_batch_id=scan_batch_id,
                    rule_version=rule_version + BASELINE_HOLD10_SUFFIX, out=out,
                    # S-a: hold10 rows stamp the card that supplied THIS
                    # arm's window (catalyst_momentum_v3), same law as the
                    # v1 arms above.
                    setup_card_id=BASELINE_HOLD10_PINNED_CARD_ID,
                    decided_at=decided_at, lineage_id=lineage_id,
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


def _require_positive_int_holding_days(card: dict, card_id: str) -> int:
    days = card.get("max_holding_days_default")
    if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
        raise ValueError(
            f"card {card_id!r} has no usable max_holding_days_default "
            f"(got {days!r}) -- cannot resolve a baseline replay window."
        )
    return days


def resolve_baseline_arm_windows() -> dict[str, int]:
    """AILEG-1 (2026-08-16, spec section 2): the two pinned arm-set replay
    windows -- v1 (threshold_v1/propose_all_v1) from
    ``BASELINE_V1_PINNED_CARD_ID``, hold10 (their ``_hold10`` twins) from
    ``BASELINE_HOLD10_PINNED_CARD_ID`` -- BOTH resolved by explicit card id,
    unconditionally, never a ``row.get("max_holding_days") or
    DEFAULT_REPLAY_WINDOW_DAYS`` fallback. This is the SINGLE source both
    ``resolve_pending_baseline_decisions`` (the live scheduled resolver) and
    ``scripts/replay_recompute.py`` (the separate, operator-invoked repair
    pass over pre-existing rows) read from, so the two can never silently
    diverge. Returns ``{rule_version: window_days}`` covering every
    rule_version in EITHER arm-set, so a caller holding one row can look up
    its window directly by that row's own ``rule_version``."""
    v1_card = get_card_by_id(BASELINE_V1_PINNED_CARD_ID)
    v1_window_days = _require_positive_int_holding_days(v1_card, BASELINE_V1_PINNED_CARD_ID)
    hold10_card = get_card_by_id(BASELINE_HOLD10_PINNED_CARD_ID)
    hold10_window_days = _require_positive_int_holding_days(hold10_card, BASELINE_HOLD10_PINNED_CARD_ID)
    windows: dict[str, int] = {}
    for rv in BASELINE_RULE_VERSIONS:
        windows[rv] = v1_window_days
    for rv in BASELINE_RULE_VERSIONS_HOLD10:
        windows[rv] = hold10_window_days
    return windows


def resolve_pending_baseline_decisions(journal, bars_provider=None, limit: int = 200) -> dict:
    """Resolve 'pending' shadow_baseline_decisions rows (decision='propose')
    with a bracket replay, via the SAME replay_bracket() the primary
    counterfactual ledger uses -- one replay engine, one truth. Idempotent:
    only 'pending' rows are touched; 'complete'/'unavailable' rows (already
    resolved at write time, see record_shadow_baseline_decisions) are never
    revisited.

    Audit-fixup HOLD-2 (HIGH-4, both audits convergent / STATUS CORRECTION
    item 4): rule-AWARE resolution -- v1-arm and hold10-arm pending rows are
    queried and budgeted SEPARATELY, each getting the FULL `limit`, rather
    than one shared ``ORDER BY id ASC LIMIT`` across both arm-sets. hold10
    rows resolve markedly slower (a real 10-trading-day hold plus this
    build's own 30-day give-up, vs v1's 3-day hold / 15-day give-up), so a
    shared query would let an aging hold10 backlog permanently occupy the
    front of the id-ordered window once the COMBINED backlog exceeds
    `limit` -- structurally starving v1 resolution of any progress at all,
    exactly the harm ruling D2 exists to prevent (the pre-registered v1 arm
    must never be affected by the new arm's own existence). Accepted
    residual: this roughly doubles worst-case bars-API calls per invocation
    (no caching exists between the two sub-calls -- noted in the build
    report, not fixed here)."""
    counts = {"total": 0, "updated": 0, "completed": 0, "skipped": 0, "unavailable": 0}
    if bars_provider is None:
        return counts

    # AILEG-1 (2026-08-16, spec section 2): resolve each arm-set's window
    # ONCE per call, by explicit pinned card id -- never per-row, never a
    # `row.get("max_holding_days") or DEFAULT_REPLAY_WINDOW_DAYS` fallback.
    # Same pinned-by-id law record_shadow_baseline_decisions already applies
    # at WRITE time; this is the identical resolution repeated at REPLAY
    # time (via the shared resolve_baseline_arm_windows()) so the two can
    # never silently diverge (e.g. a row whose stored max_holding_days is
    # NULL/stale for any reason no longer changes what window actually gets
    # replayed).
    arm_windows = resolve_baseline_arm_windows()

    for rule_versions in (BASELINE_RULE_VERSIONS, BASELINE_RULE_VERSIONS_HOLD10):
        window_days = arm_windows[rule_versions[0]]
        sub_counts = _resolve_pending_rows_for_arm_set(journal, bars_provider, limit, rule_versions, window_days)
        for key in counts:
            counts[key] += sub_counts[key]

    _warn_on_orphaned_pending_rows(journal)
    return counts


def _warn_on_orphaned_pending_rows(journal) -> None:
    """Round-2 audit-fixup (FIX-B, audit A NEW-2, LOW): the rule-aware split
    above (HIGH-4) turned "resolve every pending row" into "resolve rows in
    these two hardcoded arm-sets" -- a future arm label (e.g. a
    threshold_v2, or any rule_version this module doesn't yet know about)
    would stay pending FOREVER with zero signal that it's being silently
    skipped. This is a cheap loudness guard, not a resolution attempt: any
    pending row whose rule_version is in NEITHER known set is counted and
    named in a WARNING system event, every call, so an operator watching
    system_events would notice within one scheduler cycle rather than
    discovering a growing, permanently-stuck backlog by accident."""
    known = BASELINE_RULE_VERSIONS + BASELINE_RULE_VERSIONS_HOLD10
    placeholders = ",".join("?" for _ in known)
    orphans = journal.query(
        f"SELECT rule_version, COUNT(*) AS cnt FROM shadow_baseline_decisions "
        f"WHERE replay_status = 'pending' AND rule_version NOT IN ({placeholders}) "
        "GROUP BY rule_version",
        tuple(known),
    )
    if not orphans:
        return
    total = sum(r["cnt"] for r in orphans)
    labels = sorted({r["rule_version"] for r in orphans})
    journal.log_system_event(
        Severity.WARNING, "baseline",
        f"{total} shadow_baseline_decisions row(s) stuck pending under {len(labels)} "
        f"rule_version(s) this resolver does not know about: {labels} -- "
        "these will NEVER resolve until resolve_pending_baseline_decisions() is "
        "extended to cover them.",
    )


def _resolve_pending_rows_for_arm_set(
    journal, bars_provider, limit: int, rule_versions: tuple, window_days: int,
) -> dict:
    """One arm-set's worth of ``resolve_pending_baseline_decisions`` --
    factored out (HOLD-2 audit-fixup HIGH-4) so v1 and hold10 each get their
    own independent query + budget. See the caller's own docstring for why.

    AILEG-1 (2026-08-16): ``window_days`` is now resolved ONCE by the
    caller (from the arm-set's own pinned card id) and passed in constant
    for every row in this arm-set -- no more per-row
    ``row.get("max_holding_days") or DEFAULT_REPLAY_WINDOW_DAYS`` fallback.
    A row's OWN stored ``max_holding_days`` (set at write time in
    ``_write_baseline_row``) is provenance for "what the card said when this
    row was written"; it is no longer read here at all, so the two can never
    silently diverge."""
    counts = {"total": 0, "updated": 0, "completed": 0, "skipped": 0, "unavailable": 0}
    placeholders = ",".join("?" for _ in rule_versions)
    rows = journal.query(
        f"SELECT * FROM shadow_baseline_decisions WHERE replay_status = 'pending' "
        f"AND rule_version IN ({placeholders}) ORDER BY id ASC LIMIT ?",
        (*rule_versions, limit),
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
        # HOLD-2 (STATUS CORRECTION item 3): hold10 rows get a wider
        # calendar give-up -- see HOLD10_UNAVAILABLE_AFTER_DAYS's own
        # module-level docstring for why the shared 15.0 would selectively
        # censor hold10's own 0-R outcomes.
        unavailable_after_days = (
            HOLD10_UNAVAILABLE_AFTER_DAYS
            if (row.get("rule_version") or "").endswith(BASELINE_HOLD10_SUFFIX)
            else UNAVAILABLE_AFTER_DAYS
        )

        if not forward_bars:
            if age_days > unavailable_after_days:
                _mark_baseline_row(journal, row["baseline_decision_id"], "unavailable",
                                   "no_bars_after_window", None, "no_bars_after_window", window_days)
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
                               None, None, replay["replay_exit_reason"], window_days)
            counts["unavailable"] += 1
            continue

        if replay["result"] == "neither" and len(forward_bars) < window_days:
            # The window has NOT actually elapsed yet -- more bars may still
            # arrive next call; a "neither" here would be a premature,
            # not-yet-final mark-to-market read (mirrors
            # update_pending_outcomes' own bars_used>=N completeness gate).
            if age_days > unavailable_after_days:
                _mark_baseline_row(journal, row["baseline_decision_id"], "unavailable",
                                   None, None, "window_never_completed", window_days)
                counts["unavailable"] += 1
            else:
                counts["skipped"] += 1
            continue

        # Resolved: a level was hit, the window genuinely elapsed with
        # neither hit (a real mark-to-market read), or same-bar ambiguity
        # (replay_r stays None in that one sub-case -- matches the primary
        # ledger's own convention of a 'complete' row with a null replay_r).
        _mark_baseline_row(journal, row["baseline_decision_id"], "complete",
                           replay["result"], replay["replay_r"], replay["replay_exit_reason"], window_days)
        counts["updated"] += 1
        counts["completed"] += 1

    return counts


def _mark_baseline_row(journal, baseline_decision_id: str, replay_status: str,
                       replay_result, replay_r, replay_exit_reason,
                       replay_window_days: Optional[int] = None) -> None:
    journal.conn.execute(
        "UPDATE shadow_baseline_decisions SET replay_status = ?, replay_result = ?, "
        "replay_r = ?, replay_exit_reason = ?, replay_window_days = ? WHERE baseline_decision_id = ?",
        (replay_status, replay_result, replay_r, replay_exit_reason, replay_window_days, baseline_decision_id),
    )
    journal.conn.commit()
