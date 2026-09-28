"""Journal-aware orchestration for the counterfactual outcome ledger.

Two phases, both idempotent and both safe to call repeatedly / on a schedule:

* ``seed_pending_outcomes`` — finds candidates/proposals/rejects/armed-watch
  rows and user-decision-overrides that don't have a ``candidate_outcomes``
  row yet, and creates one each (status ``pending``). PURE READ of existing
  decision tables + one INSERT per new row; never touches the decision tables
  themselves, never influences scanning/eval/labelling/risk/execution.
* ``update_pending_outcomes`` — for rows still ``pending``/``partial``, fetches
  bars observed AFTER the decision and computes forward 1/3/5-day returns +
  bracket replay (see ``outcomes_engine``), plus (EVID-1) a market-adjusted
  return per horizon against the already-captured SPY ``benchmark_bars``
  cache, and the bar index (time-to-excursion) of each horizon's MFE/MAE.
  Writes outcomes and separate scheduling cursors; never reads back into any
  trading decision.

Status filters protect final evidence. Pending work rotates through durable,
bounded cycles; incomplete rows are retried without starving later rows. A
separate durable turn alternates the first family across normal invocations,
and leaves the other family first if either block is interrupted.

RESEARCH-REPAIR (2026-09-12): the five-day outcome family retains its own
completion status. Newly seeded rows carry a separate versioned replay
status; a ten-day replay still pending after day five continues in the main
loop without rewriting completed forward statistics. This also continues
explicitly pending legacy replays, without stamping those historical rows
as prospective v2 evidence. Legacy complete replays remain unchanged/unverified.
Explicit retrospective corrections are append-only sidecar evidence, never
a scheduled rewrite.

HOLD-1 (2026-07-24, ``docs/roadmap/alphaos-hold1-10day-shadow-horizon-spec.md``):
``update_pending_outcomes`` also drives a SEPARATE continuation pass
(``_update_hold1_10d_family``) for the additive 10-trading-day shadow
horizon. Kept structurally apart from the 1d/3d/5d loop above -- deliberately
NOT folded into it -- for two reasons: (1) a 10-day window can only resolve
strictly LATER than the 5-day window (10 > 5 trading days, always), so a row
still in ``pending``/``partial`` (5d not yet resolved) can never have a
resolved 10d family either -- there is nothing for a combined pass to do for
those rows that a later call, after the row reaches ``outcome_status =
'complete'``, doesn't already cover; (2) it keeps the existing 1d/3d/5d/
replay computation and write path for already-``complete`` rows byte-for-byte
unchanged (the ticket's own non-goal: "any change to the 1/3/5-day columns
or their consumers" is out of scope) -- the continuation pass reads rows the
main loop above no longer touches (``outcome_status = 'complete'``) and
writes ONLY the 6 HOLD-1 columns + its own ``outcome_status_10d`` marker,
never ``forward_1d_*``/``forward_3d_*``/``forward_5d_*``/``replay_*``. When
HOLD1 runs first, rows newly completed by the later forward block wait for a
later invocation under the existing cursor/cycle limits; there is no extra HOLD1 burst above its
per-call limit. Same
bar source (``bars_provider.get_daily_bars``), same pure ``forward_window_
stats()`` call the 1d/3d/5d family already uses -- no second excursion
engine.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Optional

from alphaos.cards.registry import get_card_by_id
from alphaos.constants import OUTCOME_STATUSES
from alphaos.learning.outcomes_engine import (
    REPLAY_METHOD_VERSION, forward_window_stats, replay_bracket_with_maturity,
)
from alphaos.learning.session_admission import admit_completed_forward_bars, record_admission_diagnostic
from alphaos.util import timeutils
from alphaos.util.ids import new_id

# AILEG-1 (2026-08-16, docs/roadmap/alphaos-aileg1-replay-window-coherence-
# spec.md, spec section 2): the AI leg's replay window is the
# `max_holding_days_default` of the card stamped on the CANDIDATE
# (candidates.card_id), resolved BY EXPLICIT ID -- never
# alphaos.cards.registry.get_default_card()/DEFAULT_CARD_ID, which tracks
# the LIVE ACTIVE_CARD_ID and can move (the HOLD-2 lesson: a frozen replay
# window must never follow the live default -- see
# alphaos.baseline.tracker.BASELINE_V1_PINNED_CARD_ID's own docstring for
# the identical law already applied to the baseline leg). This fallback is
# used ONLY when the candidate carries no card_id at all (pre-card-stamping
# legacy rows, or a user_override/reject row sourced from a candidate that
# never got a card) -- pinned by explicit literal, deliberately NOT an
# import of DEFAULT_CARD_ID (which is itself allowed to move, e.g. INSTR-1
# already moved it once).
AI_LEG_FALLBACK_CARD_ID = "catalyst_momentum_v2"

# Stamped into data_quality_status (never a NEW column -- the spec's own
# section 3 scopes journal/schema.py to exactly two additive columns, one
# per table, both named replay_window_days) whenever the fallback path
# above was used, so a reader can tell which of the two §2 paths produced a
# given row's window without re-deriving it from candidates.card_id. Never
# overwrites a more specific existing data_quality_status value (e.g.
# 'decision_time_unrecoverable') -- same "don't clobber" law
# _repair_missing_decision_at_utc already follows for this column.
REPLAY_WINDOW_FALLBACK_DQ_STATUS = "replay_window_fallback_pinned_v2"


def resolve_ai_replay_window(journal, candidate_id: Optional[str]) -> tuple[int, bool]:
    """AILEG-1 spec section 2: resolve the AI leg's replay window for one
    candidate_outcomes row. Returns ``(window_days, used_fallback)`` --
    ``used_fallback`` is True only when the candidate carries no card_id at
    all (never for any other reason). Raises ``SettingsError`` (propagated,
    never swallowed) if the resolved card_id isn't in the registry or has no
    usable ``max_holding_days_default`` -- a malformed/missing card is a
    genuinely unexpected registry state, not something to silently paper
    over with a fabricated window (same law
    ``record_shadow_baseline_decisions`` already applies to its own card
    lookups)."""
    card_id = None
    if candidate_id:
        cand = journal.candidate_by_id(candidate_id)
        card_id = (cand or {}).get("card_id")
    used_fallback = not card_id
    resolved_card_id = card_id or AI_LEG_FALLBACK_CARD_ID
    card = get_card_by_id(resolved_card_id)
    window_days = card.get("max_holding_days_default")
    if not isinstance(window_days, int) or isinstance(window_days, bool) or window_days <= 0:
        raise ValueError(
            f"card {resolved_card_id!r} has no usable max_holding_days_default "
            f"(got {window_days!r}) -- cannot resolve an AI-leg replay window."
        )
    return window_days, used_fallback

# AlphaOS-side classification a candidate can resolve to (the "primary" row,
# one per candidate_id). 'user_override' is seeded separately, in parallel.
_ALPHAOS_SIDE_TYPES = ("proposal", "blocked", "armed_watch", "reject", "candidate")

# VOCAB-1: this module is THE single writer of candidate_outcomes.
# outcome_status -- every literal it stamps is one of these four names, kept
# consistent with the shared constants.OUTCOME_STATUSES tuple by the
# assertion below (not a positional unpack -- audit-fixup GROUP-A round 1:
# ``_STATUS_PENDING, _STATUS_PARTIAL, _STATUS_COMPLETE, _STATUS_UNAVAILABLE
# = OUTCOME_STATUSES`` would crash this module's own import the moment a
# 5th status is ever added to the tuple, or silently reorder the aliases if
# an existing member's position changes -- neither of which VOCAB-1's own
# "never spell the vocabulary apart again" intent should tolerate as an
# import-time landmine). Named constants + an explicit set-equality check
# give the SAME single-source-of-truth guarantee (drift is caught, not
# silently possible) without being fragile to the tuple's own length or
# order.
_STATUS_PENDING = "pending"
_STATUS_PARTIAL = "partial"
_STATUS_COMPLETE = "complete"
_STATUS_UNAVAILABLE = "unavailable"
assert {_STATUS_PENDING, _STATUS_PARTIAL, _STATUS_COMPLETE, _STATUS_UNAVAILABLE} == set(OUTCOME_STATUSES), (
    "outcomes_tracker's own status aliases have drifted from constants.OUTCOME_STATUSES -- "
    "update both together"
)

# If we still have zero forward bars this many calendar days after a decision
# was recorded, treat it as genuinely unavailable (not a transient gap) so the
# row converges instead of being retried forever.
UNAVAILABLE_AFTER_DAYS = 15.0

# EVID-1: the benchmark this codebase already captures daily (PR9.5's
# benchmark_capture.py -> benchmark_bars) -- same symbol relative_performance.py
# compares paper equity against. Reused, not a second benchmark source.
BENCHMARK_SYMBOL = "SPY"


# --------------------------------------------------------------------- seed
#
# candidate_type is a SNAPSHOT frozen at first seed, not a live view: if a
# 'candidate' or 'reject' later grows a proposal (e.g. via a user override),
# the ORIGINAL row keeps its original type — a separate 'user_override' row
# captures the new path in parallel. This is deliberate (each row is a fixed
# counterfactual observation), not a bug.
def _classify_candidate(journal, cand: dict) -> dict:
    """AlphaOS-side classification + level/decision sourcing for one candidate.
    Priority: proposal(blocked) > proposal > armed_watch > reject > candidate.
    ``decision_at_utc`` is the SOURCE row's own timestamp (proposal/reject/
    decision_adjustments/candidate) — the actual moment AlphaOS decided —
    which is what forward outcomes must anchor on, NOT when this
    candidate_outcomes row happens to get seeded (that can lag, e.g. when
    catching up on a backlog)."""
    candidate_id = cand["candidate_id"]
    ev = journal.evaluation_for_candidate(candidate_id) or {}
    adj = journal.one(
        "SELECT * FROM decision_adjustments WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
        (candidate_id,)) or {}
    proposal = journal.one(
        "SELECT * FROM trade_proposals WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
        (candidate_id,))
    reject = journal.one(
        "SELECT * FROM rejected_candidates WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
        (candidate_id,))

    if proposal:
        candidate_type = "blocked" if proposal.get("status") == "blocked" else "proposal"
        entry, stop, target = proposal.get("entry"), proposal.get("stop"), proposal.get("target")
        direction = proposal.get("direction") or ev.get("direction") or cand.get("direction")
        playbook = proposal.get("playbook_name") or cand.get("playbook_name")
        decision_at_utc = proposal.get("created_at_utc") or cand.get("created_at_utc")
        lineage_id = proposal.get("lineage_id") or cand.get("lineage_id")
    elif cand.get("armed_watch"):
        candidate_type = "armed_watch"
        entry, stop, target = ev.get("entry"), ev.get("stop"), ev.get("target")
        direction = ev.get("direction") or cand.get("direction")
        playbook = cand.get("playbook_name")
        decision_at_utc = adj.get("created_at_utc") or cand.get("created_at_utc")
        lineage_id = adj.get("lineage_id") or cand.get("lineage_id")
    elif reject:
        candidate_type = "reject"
        if ev.get("entry") is not None:
            entry, stop, target = ev.get("entry"), ev.get("stop"), ev.get("target")
        else:
            entry, stop, target = reject.get("would_be_entry"), reject.get("would_be_stop"), None
        direction = reject.get("direction") or ev.get("direction") or cand.get("direction")
        playbook = cand.get("playbook_name")
        decision_at_utc = reject.get("created_at_utc") or cand.get("created_at_utc")
        lineage_id = reject.get("lineage_id") or cand.get("lineage_id")
    else:
        candidate_type = "candidate"
        entry, stop, target = ev.get("entry"), ev.get("stop"), ev.get("target")
        direction = ev.get("direction") or cand.get("direction")
        playbook = cand.get("playbook_name")
        decision_at_utc = cand.get("created_at_utc")
        lineage_id = cand.get("lineage_id")

    final_decision = adj.get("final_decision") or cand.get("label_decision") or cand.get("status")
    return {
        "candidate_type": candidate_type,
        "eval_decision": ev.get("decision"),
        "label_decision": cand.get("label_decision"),
        "final_decision": final_decision,
        # Frozen at seed time — AlphaOS's original call, for counterfactual
        # comparison against whatever final_decision later becomes.
        "original_decision": final_decision,
        "entry_reference_price": entry, "stop_price": stop, "target_price": target,
        "direction_hint": direction, "playbook_id": playbook,
        "decision_at_utc": decision_at_utc,
        # PR4: preserve the SOURCE decision's lineage_id (same anchor-on-source,
        # not anchor-on-seed-time, principle as decision_at_utc above) rather
        # than computing a fresh "current" snapshot -- an outcome row measures
        # the original decision, so it must carry that decision's own lineage,
        # not whatever code/config happens to be running when this row is seeded.
        "lineage_id": lineage_id,
    }


def _source_from_override(journal, ov: dict) -> dict:
    """Level/decision sourcing for a user-override counterfactual row. Unlike
    the AlphaOS-side row, ``final_decision`` here is the USER's final decision
    and ``original_decision`` is AlphaOS's original (frozen) call — the pair a
    future ΔR comparison needs. ``decision_at_utc`` is the override's OWN
    timestamp (when the user actually made their call) — that is the decision
    whose forward outcome this row measures, not the original candidate scan."""
    entry = stop = target = None
    if ov.get("proposal_id"):
        prop = journal.proposal_by_id(ov["proposal_id"])
        if prop:
            entry, stop, target = prop.get("entry"), prop.get("stop"), prop.get("target")
    if entry is None:
        ev = journal.evaluation_for_candidate(ov.get("candidate_id")) or {}
        entry, stop, target = ev.get("entry"), ev.get("stop"), ev.get("target")
    direction = ov.get("user_direction") or ov.get("alphaos_direction")
    return {
        "candidate_type": "user_override",
        "eval_decision": ov.get("alphaos_eval_decision"),
        "label_decision": ov.get("alphaos_label_decision"),
        "final_decision": ov.get("user_final_decision"),
        "original_decision": ov.get("alphaos_final_decision"),
        "entry_reference_price": entry, "stop_price": stop, "target_price": target,
        "direction_hint": direction, "playbook_id": None,
        "decision_at_utc": ov.get("created_at_utc"),
        # PR4: the override row's own lineage (the environment/config in
        # effect when the USER made this override) -- not the original
        # AlphaOS decision's lineage, since this row measures the override.
        "lineage_id": ov.get("lineage_id"),
    }


def _insert_outcome_row(journal, *, candidate_id: str, symbol: Optional[str],
                        scan_id: Optional[str], scan_batch_id: Optional[str],
                        armed_watch: bool, info: dict, override_flag: bool) -> None:
    journal.insert("candidate_outcomes", {
        "outcome_id": new_id("cout"),
        "scan_id": scan_id,
        "scan_batch_id": scan_batch_id,
        "candidate_id": candidate_id,
        "symbol": symbol,
        "candidate_type": info["candidate_type"],
        "decision_at_utc": info.get("decision_at_utc"),
        "original_decision": info["original_decision"],
        "eval_decision": info["eval_decision"],
        "label_decision": info["label_decision"],
        "final_decision": info["final_decision"],
        "armed_watch": 1 if armed_watch else 0,
        "user_override": 1 if override_flag else 0,
        "playbook_id": info.get("playbook_id"),
        "entry_reference_price": info.get("entry_reference_price"),
        "stop_price": info.get("stop_price"),
        "target_price": info.get("target_price"),
        "direction_hint": info.get("direction_hint"),
        "outcome_status": _STATUS_PENDING,
        "replay_status": _STATUS_PENDING,
        "replay_method_version": REPLAY_METHOD_VERSION,
        "lineage_id": info.get("lineage_id"),
    })


def seed_pending_outcomes(journal, limit: int = 500) -> dict:
    """Create missing candidate_outcomes rows. Returns counts by type + total.
    NEVER writes to candidates/proposals/rejects/overrides — read-only there."""
    counts = {t: 0 for t in (*_ALPHAOS_SIDE_TYPES, "user_override")}

    candidates = journal.query(
        "SELECT c.* FROM candidates c WHERE NOT EXISTS ("
        "  SELECT 1 FROM candidate_outcomes co WHERE co.candidate_id = c.candidate_id "
        "  AND co.candidate_type IN ('proposal','blocked','armed_watch','reject','candidate')"
        ") ORDER BY c.id ASC LIMIT ?", (limit,))
    for cand in candidates:
        info = _classify_candidate(journal, cand)
        _insert_outcome_row(
            journal, candidate_id=cand["candidate_id"], symbol=cand.get("symbol"),
            scan_id=cand.get("scan_id"), scan_batch_id=cand.get("scan_batch_id"),
            armed_watch=bool(cand.get("armed_watch")), info=info, override_flag=False)
        counts[info["candidate_type"]] += 1

    overrides = journal.query(
        "SELECT o.* FROM user_decision_overrides o WHERE o.candidate_id IS NOT NULL AND NOT EXISTS ("
        "  SELECT 1 FROM candidate_outcomes co WHERE co.candidate_id = o.candidate_id "
        "  AND co.candidate_type = 'user_override'"
        ") ORDER BY o.id ASC LIMIT ?", (limit,))
    for ov in overrides:
        info = _source_from_override(journal, ov)
        cand = journal.candidate_by_id(ov["candidate_id"])
        _insert_outcome_row(
            journal, candidate_id=ov["candidate_id"], symbol=ov.get("symbol") or (cand or {}).get("symbol"),
            scan_id=(cand or {}).get("scan_id"), scan_batch_id=(cand or {}).get("scan_batch_id"),
            armed_watch=bool((cand or {}).get("armed_watch")), info=info, override_flag=True)
        counts["user_override"] += 1

    counts["total"] = sum(counts.values())
    return counts


# ------------------------------------------------------------------- update
def _benchmark_reference_and_forward_bars(journal, decision_date: str, candidate_bar_dates) -> tuple:
    """EVID-1: the benchmark's own reference price (last close AT OR BEFORE
    decision_date -- the same "reference point" role entry_reference_price
    plays for the candidate) and forward bars from the already-captured
    ``benchmark_bars`` cache. Read-only; never fetches over the network
    (benchmark_capture.py's own once-daily job owns that).

    Audit-fixup (correctness MED): forward bars are restricted to
    ``candidate_bar_dates`` (the candidate's OWN forward-bar dates) rather
    than every benchmark bar after decision_date. Without this, "first N
    benchmark bars" and "first N candidate bars" are aligned by POSITION
    only -- if the candidate's own bar series has a gap the benchmark's
    doesn't (a halt) or vice versa, the Nth bar on each side can fall on
    DIFFERENT calendar dates, silently comparing a 5-trading-day candidate
    move against e.g. a 6-calendar-day benchmark move under the "identical
    forward window" claim. Restricting to the intersection means position N
    in the returned list is always the same date as position N in the
    candidate's own bars (any date the candidate has that the benchmark
    lacks simply isn't in this list -- correctly read as the benchmark
    window not yet reaching N, never as a mismatched date standing in for
    it).

    Returns ``(reference_close_or_None, forward_bars)`` -- forward_bars is
    always a list (possibly empty) shaped like the candidate provider's own
    bars (``date``/``close`` keys), so it can be passed straight into
    ``forward_window_stats`` unchanged."""
    ref_row = journal.one(
        "SELECT close FROM benchmark_bars WHERE symbol = ? AND bar_date <= ? "
        "ORDER BY bar_date DESC LIMIT 1",
        (BENCHMARK_SYMBOL, decision_date),
    )
    reference = ref_row["close"] if ref_row else None
    if reference is None:
        return None, []
    all_forward_bars = journal.query(
        "SELECT bar_date AS date, close FROM benchmark_bars WHERE symbol = ? AND bar_date > ? "
        "ORDER BY bar_date ASC",
        (BENCHMARK_SYMBOL, decision_date),
    )
    candidate_dates = set(candidate_bar_dates)
    forward_bars = [b for b in all_forward_bars if b["date"] in candidate_dates]
    return reference, forward_bars


def _market_adjusted_return_pct(candidate_stats: dict, benchmark_stats: dict, n_days: int):
    """Candidate's own directional forward return minus what the SAME
    directional bet (long/short matching the candidate's direction_hint) on
    the benchmark would have returned over the identical forward window --
    the same modest "excess return" framing as
    reports/relative_performance.py (never a CAPM/risk-adjusted alpha), just
    computed per-candidate instead of on the portfolio equity curve. None
    whenever either leg is unavailable.

    Audit-fixup (correctness MED): requires BOTH legs to have resolved the
    full ``n_days`` window (``bars_used >= n_days`` on each side), not just
    the benchmark side. The original gate only checked the benchmark,
    reasoning that a candidate row's completion status (which stops it
    being revisited forever) is governed solely by the candidate's own bar
    count -- true, but it missed that ``update_pending_outcomes`` also
    reads and stores this value on a still-``partial`` row (candidate not
    yet at n_days either), which would silently pair a partial candidate
    return against a fully-resolved benchmark return under the "Nd" name.
    Requiring both sides keeps every published market_adjusted_return_Nd_pct
    an apples-to-apples N-day-vs-N-day comparison; a lagging OR
    still-accumulating side just leaves the field honestly None instead."""
    if candidate_stats is None or benchmark_stats is None:
        return None
    if candidate_stats.get("bars_used", 0) < n_days or benchmark_stats.get("bars_used", 0) < n_days:
        return None
    candidate_return_pct = candidate_stats.get("return_pct")
    bench_return_pct = benchmark_stats.get("return_pct")
    if candidate_return_pct is None or bench_return_pct is None:
        return None
    return round(candidate_return_pct - bench_return_pct, 4)


def _update_row(journal, outcome_id: str, fields: dict) -> None:
    st = timeutils.stamp()
    fields = dict(fields)
    fields["updated_at_utc"] = st.utc
    fields["updated_at_sgt"] = st.local_sgt
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    journal.conn.execute(
        f"UPDATE candidate_outcomes SET {set_clause} WHERE outcome_id = ?",
        (*fields.values(), outcome_id),
    )
    journal.conn.commit()


def _lookup_decision_timestamp(journal, candidate_id: str, candidate_type: str) -> Optional[str]:
    """Re-derive a row's original decision timestamp from its (already-decided)
    candidate_type's source table — the same mapping ``_classify_candidate``/
    ``_source_from_override`` use at seed time. Used only to REPAIR legacy rows
    seeded before ``decision_at_utc`` existed; never re-classifies the type."""
    if candidate_type in ("proposal", "blocked"):
        row = journal.one(
            "SELECT created_at_utc FROM trade_proposals WHERE candidate_id = ? "
            "ORDER BY id DESC LIMIT 1", (candidate_id,))
    elif candidate_type == "armed_watch":
        row = journal.one(
            "SELECT created_at_utc FROM decision_adjustments WHERE candidate_id = ? "
            "ORDER BY id DESC LIMIT 1", (candidate_id,))
    elif candidate_type == "reject":
        row = journal.one(
            "SELECT created_at_utc FROM rejected_candidates WHERE candidate_id = ? "
            "ORDER BY id DESC LIMIT 1", (candidate_id,))
    elif candidate_type == "user_override":
        row = journal.one(
            "SELECT created_at_utc FROM user_decision_overrides WHERE candidate_id = ? "
            "ORDER BY id DESC LIMIT 1", (candidate_id,))
    else:  # 'candidate' catch-all
        row = None
    if row and row.get("created_at_utc"):
        return row["created_at_utc"]
    # Last resort for every type (including 'candidate', and any type whose
    # specific source row is gone): the candidates table itself.
    cand = journal.candidate_by_id(candidate_id)
    return (cand or {}).get("created_at_utc")


def _repair_missing_decision_at_utc(journal, rows: list[dict]) -> list[dict]:
    """Backfill decision_at_utc on legacy rows (seeded before this column
    existed) by re-deriving it from the linked source row. Additive, idempotent
    — only rows with decision_at_utc IS NULL are touched. When the source row
    can no longer be found, falls back to created_at_utc (so the row can still
    resolve) but marks data_quality_status so seed-time is never silently
    mistaken for decision-time. Returns ``rows`` with decision_at_utc filled in
    (in-memory) so the caller's SAME pass anchors correctly."""
    repaired = []
    for row in rows:
        if row.get("decision_at_utc"):
            repaired.append(row)
            continue
        ts = _lookup_decision_timestamp(journal, row["candidate_id"], row["candidate_type"])
        if ts:
            _update_row(journal, row["outcome_id"], {"decision_at_utc": ts})
            row = dict(row)
            row["decision_at_utc"] = ts
        else:
            # Source row is gone too — created_at_utc is the only timestamp
            # left. Use it so the row isn't stuck forever, but flag it: this is
            # NOT a reliable decision timestamp, unlike the normal case.
            fallback = row.get("created_at_utc")
            _update_row(journal, row["outcome_id"], {
                "decision_at_utc": fallback, "data_quality_status": "decision_time_unrecoverable"})
            row = dict(row)
            row["decision_at_utc"] = fallback
            row["data_quality_status"] = "decision_time_unrecoverable"
        repaired.append(row)
    return repaired


_FORWARD_ELIGIBILITY = (
    "outcome_status IN (?, ?) OR (outcome_status = ? AND replay_status = 'pending' "
    "AND replay_window_days > 0 AND replay_r IS NULL AND replay_result IS NULL "
    "AND (replay_method_version IS NULL OR replay_method_version = ?))"
)
_FORWARD_PARAMS = (_STATUS_PENDING, _STATUS_PARTIAL, _STATUS_COMPLETE, REPLAY_METHOD_VERSION)
_HOLD10_ELIGIBILITY = (
    "outcome_status = ? AND (outcome_status_10d IS NULL OR outcome_status_10d != 'complete')"
)
_FORWARD_FAMILY = "forward_replay_v1"
_HOLD10_FAMILY = "hold1_10d_v1"


def _advance_dispatch_turn(journal, family: Optional[str] = None,
                           expected_revision: Optional[int] = None) -> tuple[str, int]:
    """Commit the opposite family before entering a block, without provider work.

    With no family argument, claim the persisted first turn (forward initially).
    Explicit families are used before the second block, and after successful
    completion with the original first family to alternate the next first turn.
    Interruption in either block therefore leaves the other first on recovery.
    Revision checks prevent a stale overlapping dispatcher from overwriting a
    newer turn. The existing research-worker lock remains the process owner;
    this metadata is not a new lease or an automatic failure-fuse bypass.
    """
    journal.conn.execute("BEGIN IMMEDIATE")
    try:
        state = journal.one(
            "SELECT * FROM outcome_family_dispatch WHERE dispatcher='candidate_outcomes'"
        )
        revision = state["revision"] if state else 0
        if expected_revision is not None and revision != expected_revision:
            raise RuntimeError("outcome family dispatch changed during processing")
        selected = family or (state["next_family"] if state else _FORWARD_FAMILY)
        if selected not in (_FORWARD_FAMILY, _HOLD10_FAMILY):
            raise ValueError("unknown outcome processing family")
        next_family = _HOLD10_FAMILY if selected == _FORWARD_FAMILY else _FORWARD_FAMILY
        revision += 1
        journal.conn.execute(
            "INSERT INTO outcome_family_dispatch (dispatcher,next_family,revision,updated_at_utc) "
            "VALUES ('candidate_outcomes',?,?,?) ON CONFLICT(dispatcher) DO UPDATE SET "
            "next_family=excluded.next_family,revision=excluded.revision,"
            "updated_at_utc=excluded.updated_at_utc",
            (next_family, revision, timeutils.now_utc().isoformat()),
        )
        journal.conn.commit()
        return selected, revision
    except BaseException:
        journal.conn.rollback()
        raise


def _claim_outcome(journal, family: str, predicate: str, params: tuple, cycle: Optional[int]):
    """Advance one durable cursor before work, in a short serialized transaction.

    The cycle's frozen high-water ID makes retries fair even if new rows arrive
    continuously. A crash after claiming sacrifices this attempt, not the row:
    it is due again in the next cycle if still eligible. No evidence is changed
    by a claim, and no provider request runs in this transaction. Concurrent
    claimers cannot claim the same ID in the same cycle; a caller stops if
    another caller has already started a newer cycle. This is scheduling state,
    not a worker lease; the scheduled research worker owns its process lock.
    """
    journal.conn.execute("BEGIN IMMEDIATE")
    try:
        state = journal.one("SELECT * FROM outcome_processing_cursors WHERE family=?", (family,))
        if state is None or (cycle is None and state["last_row_id"] >= state["cycle_max_id"]):
            maximum = journal.scalar("SELECT COALESCE(MAX(id), 0) FROM candidate_outcomes")
            number = state["cycle_number"] + 1 if state else 1
            journal.conn.execute(
                "INSERT INTO outcome_processing_cursors "
                "(family, cycle_number, cycle_max_id, last_row_id) VALUES (?, ?, ?, 0) "
                "ON CONFLICT(family) DO UPDATE SET cycle_number=excluded.cycle_number, "
                "cycle_max_id=excluded.cycle_max_id, last_row_id=0",
                (family, number, maximum),
            )
            state = {"cycle_number": number, "cycle_max_id": maximum, "last_row_id": 0}
        number = state["cycle_number"]
        if cycle is not None and number != cycle:
            journal.conn.commit()
            return None, cycle
        row = journal.one(
            f"SELECT * FROM candidate_outcomes WHERE ({predicate}) "
            "AND id > ? AND id <= ? ORDER BY id ASC LIMIT 1",
            (*params, state["last_row_id"], state["cycle_max_id"]),
        )
        if row is None:
            journal.conn.execute(
                "UPDATE outcome_processing_cursors SET last_row_id=cycle_max_id WHERE family=?",
                (family,),
            )
        else:
            journal.conn.execute(
                "UPDATE outcome_processing_cursors SET last_row_id=?, last_attempt_at_utc=? WHERE family=?",
                (row["id"], timeutils.now_utc().isoformat(), family),
            )
        journal.conn.commit()
        return row, number
    except BaseException:
        journal.conn.rollback()
        raise


def _fair_outcome_rows(journal, family: str, predicate: str, params: tuple, limit: int):
    """At most limit claims and one cycle per call; retries are due next cycle."""
    cycle = None
    for _ in range(limit):
        row, cycle = _claim_outcome(journal, family, predicate, params, cycle)
        if row is None:
            break
        yield row


def _record_processing_error(counts: dict, row: dict, exc: Exception, kind: str):
    counts["errors"] = counts.get("errors", 0) + 1
    counts[f"{kind}_errors"] = counts.get(f"{kind}_errors", 0) + 1
    by_type = counts.setdefault(f"{kind}_error_types", {})
    error_type = type(exc).__name__
    by_type[error_type] = by_type.get(error_type, 0) + 1
    samples = counts.setdefault(f"{kind}_error_samples", [])
    if len(samples) < 5:
        samples.append({"outcome_id": row["outcome_id"], "error_type": error_type})


@contextmanager
def _calculation_boundary(row: dict, counts: dict):
    """Isolate malformed row/bar values, never configuration or SQLite errors."""
    try:
        yield
    except (ValueError, TypeError, KeyError) as exc:
        _record_processing_error(counts, row, exc, "calculation")


def _fetch_outcome_bars(bars_provider, row: dict, start: str, end: str, counts: dict):
    """A provider failure leaves evidence intact but does not stop the queue.

    The caller's persisted job summary gets exact error counts and a bounded
    sample of affected IDs/types, never exception messages or provider payloads.
    Malformed calculation inputs are counted separately by the caller. Other
    programming/registry/SQLite failures still propagate; the already-committed
    cursor ensures the next run can move past the claimed row.
    """
    try:
        requested = timeutils.now_utc()
        bars = bars_provider.get_daily_bars(row["symbol"], start, end) or []
        admitted, receipt = admit_completed_forward_bars(
            bars, start, requested_at=requested, received_at=timeutils.now_utc(),
        )
        record_admission_diagnostic(counts, receipt)
        return admitted, receipt
    except OSError as exc:
        _record_processing_error(counts, row, exc, "provider")
        return None


def update_pending_outcomes(journal, bars_provider=None, limit: int = 200) -> dict:
    """Run each bounded family once, with durable alternating first turns.

    New HOLD1 eligibility created while forward runs first is processed in the
    same call. When HOLD1 runs first it waits for a later invocation under
    the ordinary cursor/cycle limits, not necessarily the immediately next call.
    Limits, counters, maturity/provenance and existing job fuses are unchanged.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("outcome processing limit must be a nonnegative integer")
    if bars_provider is None or limit == 0:
        return _update_forward_replay_family(journal, bars_provider, limit)

    first, revision = _advance_dispatch_turn(journal)
    second = _HOLD10_FAMILY if first == _FORWARD_FAMILY else _FORWARD_FAMILY
    results = {}
    for index, family in enumerate((first, second)):
        if index:
            _, revision = _advance_dispatch_turn(journal, family, revision)
        if family == _FORWARD_FAMILY:
            results[family] = _update_forward_replay_family(journal, bars_provider, limit)
        else:
            results[family] = _update_hold1_10d_family(journal, bars_provider, limit)
    # A normal pass also alternates its FIRST family. Before this final short
    # commit an interruption still leaves the opposite of the last block first.
    _advance_dispatch_turn(journal, first, revision)
    counts = results[_FORWARD_FAMILY]
    counts["hold1_10d"] = results[_HOLD10_FAMILY]
    return counts


def _update_forward_replay_family(journal, bars_provider=None, limit: int = 200) -> dict:
    """Resolve pending/partial candidate_outcomes rows with forward 1/3/5-day
    returns + bracket replay. A complete five-day family may still have an
    explicitly pending bracket replay; continue that replay alone, including
    legacy rows, without changing their evidence provenance. Completed
    five-day rows with a final replay and unavailable outcome rows are never
    revisited. Missing
    bars are handled safely — no provider or a transient empty fetch just
    leaves the row pending (retried next call); only after
    ``UNAVAILABLE_AFTER_DAYS`` with zero bars does a row convert to
    ``unavailable``. Forward windows anchor on ``decision_at_utc`` (the
    original decision time), NOT ``created_at_utc`` (when this row was
    seeded) — seeding can lag the decision by days/weeks when catching up on
    a backlog, and anchoring on seed time would mislabel a multi-week-old
    candidate's next bar as a "1-day" return."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("outcome processing limit must be a nonnegative integer")
    counts = {"total": 0, "updated": 0, "completed": 0, "skipped": 0, "unavailable": 0,
              "hold1_10d": {"updated": 0, "completed": 0, "skipped": 0}}
    if bars_provider is None:
        rows = journal.query(
            f"SELECT id FROM candidate_outcomes WHERE {_FORWARD_ELIGIBILITY} LIMIT ?",
            (*_FORWARD_PARAMS, limit),
        )
        counts["total"] = counts["skipped"] = len(rows)
        return counts

    now = timeutils.now_utc()
    for row in _fair_outcome_rows(journal, _FORWARD_FAMILY, _FORWARD_ELIGIBILITY,
                                  _FORWARD_PARAMS, limit):
        counts["total"] += 1
        with _calculation_boundary(row, counts):
            row = _repair_missing_decision_at_utc(journal, [row])[0]
            decision_at = timeutils.parse_iso(row.get("decision_at_utc"))
            if decision_at is None:
                counts["skipped"] += 1
                continue
            age_days = (now - decision_at).total_seconds() / 86400.0
            decision_date = timeutils.market_date(decision_at).isoformat()
            fetched = _fetch_outcome_bars(bars_provider, row, decision_date, now.date().isoformat(), counts)
            if fetched is None:
                continue
            bars, admission = fetched
            # Bars strictly AFTER the decision day — never let the decision's own
            # day count as "forward" (no lookahead leakage into the replay).
            forward_bars = [b for b in bars if b.get("date") and b["date"] > decision_date]

            if not forward_bars:
                if admission["waiting_for_admissible_sessions"]:
                    counts["skipped"] += 1
                    continue
                if age_days > UNAVAILABLE_AFTER_DAYS:
                    # Don't clobber an already-flagged unrecoverable decision_at_utc
                    # (repair may have just set it this same pass) — that provenance
                    # signal matters more than the more-obvious no-bars reason.
                    dq = row.get("data_quality_status") or "no_bars_after_window"
                    unavailable = {"replay_status": _STATUS_UNAVAILABLE, "data_quality_status": dq}
                    if row["outcome_status"] != _STATUS_COMPLETE:
                        unavailable["outcome_status"] = _STATUS_UNAVAILABLE
                    _update_row(journal, row["outcome_id"], unavailable)
                    counts["unavailable"] += 1
                else:
                    counts["skipped"] += 1
                continue

            ref, stop, direction = row.get("entry_reference_price"), row.get("stop_price"), row.get("direction_hint")
            f1 = forward_window_stats(ref, stop, direction, forward_bars, 1)
            f3 = forward_window_stats(ref, stop, direction, forward_bars, 3)
            f5 = forward_window_stats(ref, stop, direction, forward_bars, 5)

            # EVID-1: market-adjusted return, computed from the SAME
            # already-captured benchmark_bars cache used by relative_performance.py
            # -- a DB read, never a network fetch. Reuses forward_window_stats
            # verbatim (stop=None so only return_pct is computed; no R/excursion
            # for a benchmark, which has no stop to normalize against). Benchmark
            # forward bars are restricted to the candidate's OWN bar dates (see
            # _benchmark_reference_and_forward_bars's own docstring) so the two
            # legs are aligned by DATE, never merely by position.
            candidate_bar_dates = [b["date"] for b in forward_bars if b.get("date")]
            bench_ref, bench_forward_bars = _benchmark_reference_and_forward_bars(
                journal, decision_date, candidate_bar_dates)
            bench_f1 = bench_f3 = bench_f5 = None
            if bench_ref is not None:
                bench_f1 = forward_window_stats(bench_ref, None, direction, bench_forward_bars, 1)
                bench_f3 = forward_window_stats(bench_ref, None, direction, bench_forward_bars, 3)
                bench_f5 = forward_window_stats(bench_ref, None, direction, bench_forward_bars, 5)

            update = {
                "forward_1d_return_pct": f1["return_pct"], "forward_1d_r": f1["r"],
                "max_favorable_1d_r": f1["max_favorable_r"], "max_adverse_1d_r": f1["max_adverse_r"],
                "bars_to_favorable_1d": f1["bars_to_favorable"], "bars_to_adverse_1d": f1["bars_to_adverse"],
                "market_adjusted_return_1d_pct": _market_adjusted_return_pct(f1, bench_f1, 1),
                "forward_3d_return_pct": f3["return_pct"], "forward_3d_r": f3["r"],
                "max_favorable_3d_r": f3["max_favorable_r"], "max_adverse_3d_r": f3["max_adverse_r"],
                "bars_to_favorable_3d": f3["bars_to_favorable"], "bars_to_adverse_3d": f3["bars_to_adverse"],
                "market_adjusted_return_3d_pct": _market_adjusted_return_pct(f3, bench_f3, 3),
                "forward_5d_return_pct": f5["return_pct"], "forward_5d_r": f5["r"],
                "max_favorable_5d_r": f5["max_favorable_r"], "max_adverse_5d_r": f5["max_adverse_r"],
                "bars_to_favorable_5d": f5["bars_to_favorable"], "bars_to_adverse_5d": f5["bars_to_adverse"],
                "market_adjusted_return_5d_pct": _market_adjusted_return_pct(f5, bench_f5, 5),
            }

            target = row.get("target_price")
            used_fallback_window = False
            if ref is not None and stop and target:
                # AILEG-1 spec section 2: the AI leg's window comes from the
                # card that governed THIS candidate, resolved by id -- never
                # DEFAULT_REPLAY_WINDOW_DAYS (see outcomes_engine's own updated
                # docstring; this is now the one production call site that used
                # to omit max_days entirely).
                window_days = row.get("replay_window_days")
                if window_days is None:
                    window_days, used_fallback_window = resolve_ai_replay_window(journal, row.get("candidate_id"))
                replay = replay_bracket_with_maturity(
                    ref, stop, target, direction, forward_bars, max_days=window_days,
                )
                update["replay_result"] = replay["result"]
                update["replay_r"] = replay["replay_r"]
                update["replay_exit_reason"] = replay["replay_exit_reason"]
                update["replay_window_days"] = window_days
                update["replay_status"] = replay["replay_status"]
                update["replay_bars_used"] = replay["replay_bars_used"]
                # Deliberately retain the row's original method stamp. A legacy
                # continuation uses this engine but did not enter the repaired
                # prospective cohort at seeding; assigning v2 here would admit it
                # to baseline_report's verified cohort. No backfill is needed.
            else:
                update["replay_status"] = _STATUS_UNAVAILABLE

            resolved = f5["bars_used"] >= 5
            update["outcome_status"] = _STATUS_COMPLETE if resolved else _STATUS_PARTIAL
            # Five-day outcome semantics remain intact. Only the separately
            # pending replay continues; already-complete forward values and
            # legacy complete replay records are never automatically restated.
            if row["outcome_status"] == _STATUS_COMPLETE:
                update = {k: v for k, v in update.items() if k.startswith("replay_")}
            # Don't clobber an unrecoverable-decision_at_utc flag from repair just
            # because the forward-return math itself succeeded — the anchor being
            # a fallback (not the true decision time) is still worth knowing.
            # AILEG-1: the fallback-card stamp is the SECOND-priority data
            # quality signal -- a pre-existing flag (e.g.
            # 'decision_time_unrecoverable') always wins, since that flag
            # already implies "this row's provenance is degraded" and is the
            # more specific/urgent fact of the two.
            existing_dq = row.get("data_quality_status")
            if existing_dq:
                update["data_quality_status"] = existing_dq
            elif used_fallback_window:
                update["data_quality_status"] = REPLAY_WINDOW_FALLBACK_DQ_STATUS
            else:
                update["data_quality_status"] = "ok"
            _update_row(journal, row["outcome_id"], update)
            counts["updated"] += 1
            if resolved and row["outcome_status"] != _STATUS_COMPLETE:
                counts["completed"] += 1

    return counts


def _update_hold1_10d_family(journal, bars_provider, limit: int = 200) -> dict:
    """HOLD-1 continuation pass: resolve the additive 10-trading-day shadow
    horizon for rows whose 5d family has already gone ``outcome_status =
    'complete'`` but whose 10d family (``outcome_status_10d``) has not.
    Writes ONLY the 6 HOLD-1 value columns + ``outcome_status_10d`` --
    ``outcome_status`` itself, and every 1d/3d/5d/replay column, are never
    touched here (see module docstring). Same bar source and the same pure
    ``forward_window_stats()`` the 1d/3d/5d family already uses -- no second
    excursion engine, no new data fetch.

    Known gap (accepted, not fixed here — see the spec's own narrow scope):
    unlike the row-level ``UNAVAILABLE_AFTER_DAYS`` convergence for a row
    with ZERO forward bars at all, a row whose forward bars exist but
    permanently plateau below 10 (e.g. a delisted symbol) has no analogous
    give-up path here -- it stays ``outcome_status_10d = 'partial'``
    indefinitely rather than ever converging to a 10d-specific
    'unavailable'. Low practical risk (this codebase's core-book universe is
    continuously-traded megacaps) but a real limitation if ever observed."""
    if bars_provider is None:
        return {"updated": 0, "completed": 0, "skipped": 0}

    counts = {"updated": 0, "completed": 0, "skipped": 0}
    now = timeutils.now_utc()
    for row in _fair_outcome_rows(journal, _HOLD10_FAMILY, _HOLD10_ELIGIBILITY,
                                  (_STATUS_COMPLETE,), limit):
        with _calculation_boundary(row, counts):
            decision_at = timeutils.parse_iso(row.get("decision_at_utc"))
            if decision_at is None:   # pragma: no cover -- defensive only; a row
                counts["skipped"] += 1  # can't reach outcome_status='complete'
                continue                # without decision_at_utc already set.
            decision_date = timeutils.market_date(decision_at).isoformat()
            fetched = _fetch_outcome_bars(bars_provider, row, decision_date, now.date().isoformat(), counts)
            if fetched is None:
                continue
            bars, admission = fetched
            forward_bars = [b for b in bars if b.get("date") and b["date"] > decision_date]
            if not forward_bars:
                counts["skipped"] += 1
                continue

            ref, stop, direction = row.get("entry_reference_price"), row.get("stop_price"), row.get("direction_hint")
            f10 = forward_window_stats(ref, stop, direction, forward_bars, 10)
            resolved_10d = f10["bars_used"] >= 10
            _update_row(journal, row["outcome_id"], {
                "forward_10d_return_pct": f10["return_pct"], "forward_10d_r": f10["r"],
                "max_favorable_10d_r": f10["max_favorable_r"], "max_adverse_10d_r": f10["max_adverse_r"],
                "bars_to_favorable_10d": f10["bars_to_favorable"], "bars_to_adverse_10d": f10["bars_to_adverse"],
                "outcome_status_10d": "complete" if resolved_10d else "partial",
            })
            counts["updated"] += 1
            if resolved_10d:
                counts["completed"] += 1

    return counts
