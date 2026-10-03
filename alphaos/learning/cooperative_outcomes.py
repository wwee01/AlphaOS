"""Scheduled slices of unchanged outcome measurement, with durable fair turns."""

from alphaos.learning import outcomes_tracker as candidate
from alphaos.learning import work_continuation as turns


def _baseline(journal, provider, limit, should_yield):
    from alphaos.baseline import tracker as baseline

    counts = {"total": 0, "updated": 0, "completed": 0, "skipped": 0, "unavailable": 0}
    details = {}
    if should_yield() or provider is None:
        return {
            **counts,
            "yielded": bool(should_yield()),
            "arms_not_started": ["v1", "hold10"],
        }
    baseline._warn_on_orphaned_pending_rows(journal)
    windows = baseline.resolve_baseline_arm_windows()
    stage, previous, _ = turns.turn(journal, "baseline")
    for _ in range(2):
        if should_yield():
            break
        next_stage, previous = turns.claim_turn(journal, "baseline", stage, previous)
        rules = (
            baseline.BASELINE_RULE_VERSIONS
            if stage == "v1"
            else baseline.BASELINE_RULE_VERSIONS_HOLD10
        )
        result = baseline._resolve_pending_rows_for_arm_set(
            journal,
            provider,
            limit,
            rules,
            windows[rules[0]],
            should_yield=should_yield,
            cursor_family=f"baseline_{stage}_v1",
        )
        details[stage] = result
        for key in counts:
            counts[key] += result[key]
        stage = next_stage
    admission = {
        ("threshold_v1" if arm == "v1" else "threshold_v1_hold10"): value[
            "session_admission"
        ]
        for arm, value in details.items()
        if "session_admission" in value
    }
    return {
        **counts,
        "yielded": bool(should_yield()),
        "arms": details,
        "session_admission_by_arm": admission,
        "arms_not_started": [arm for arm in ("v1", "hold10") if arm not in details],
    }


def run_cooperative_outcomes(orch, *, limit, should_yield):
    from alphaos.scheduler.ownership import worker_ownership

    if type(limit) is not int or limit < 0:
        raise ValueError("outcome processing limit must be a nonnegative integer")
    if orch.journal.conn.in_transaction:
        raise RuntimeError("cooperative work requires no caller transaction")
    if should_yield():
        return {
            "yielded": True,
            "stages": {},
            "stages_not_started": list(turns.DOMAINS["outer"]),
        }
    with worker_ownership(orch.journal, "research") as acquired:
        if not acquired:
            return {
                "yielded": True,
                "reason": "research_owner_busy",
                "stages": {},
                "stages_not_started": list(turns.DOMAINS["outer"]),
            }
        return _run_owned(orch, limit=limit, should_yield=should_yield)


def _run_owned(orch, *, limit, should_yield):
    if type(limit) is not int or limit < 0:
        raise ValueError("outcome processing limit must be a nonnegative integer")
    result: dict = {
        "yielded": False,
        "stages": {},
        "stages_not_started": list(turns.DOMAINS["outer"]),
    }
    if limit == 0 or should_yield():
        result["yielded"] = bool(should_yield())
        return result
    from alphaos.data.providers.alpaca_bars import make_bars_provider

    provider = make_bars_provider(orch.settings, orch.journal)
    stage, previous, _ = turns.turn(orch.journal, "outer")
    for _ in range(len(turns.DOMAINS["outer"])):
        if should_yield():
            break
        next_stage, previous = turns.claim_turn(orch.journal, "outer", stage, previous)
        detail: dict
        if stage == "candidate":
            seeded = candidate.seed_pending_outcomes(
                orch.journal, limit=limit, should_yield=should_yield
            )
            updated = (
                {"not_started": True, "yielded": True}
                if should_yield()
                else candidate.update_pending_outcomes(
                    orch.journal, provider, limit=limit, should_yield=should_yield
                )
            )
            result["seeded"], result["updated"] = seeded, updated
            detail = {"seeded": seeded, "updated": updated}
        elif stage.startswith("attribution_"):
            if orch.settings.attribution_enabled:
                from alphaos import attribution

                function = (
                    attribution.discover_events
                    if stage == "attribution_discover"
                    else attribution.resolve_pending
                )
                # These stages read only already committed outcome rows. A slice
                # may consume a prior invocation's progress, never future replay.
                detail = function(orch.journal, orch.settings, limit=limit)
                result.setdefault("attribution", {})[
                    "discovered" if stage.endswith("discover") else "resolved"
                ] = detail
            else:
                detail = {"disabled": True}
        else:
            detail = (
                _baseline(orch.journal, provider, limit, should_yield)
                if orch.settings.baseline_enabled
                else {"disabled": True}
            )
            result["baseline"] = detail
        result["stages"][stage] = detail
        result["stages_not_started"].remove(stage)
        stage = next_stage
    result["yielded"] = bool(should_yield())
    return result
