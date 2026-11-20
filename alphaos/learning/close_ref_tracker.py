"""Additive campaign outcome writer; no registry, legacy outcome or trade writes.

Read source rows in one SQLite snapshot, resolve membership before prices, and
freeze each unsigned leg once. Classification outages are not repaired here.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict

from alphaos.learning import close_ref_engine as engine
from alphaos.learning import close_ref_mapping as mapping
from alphaos.learning.close_ref_lineage import scan_proof
from alphaos.learning.close_ref_sources import SliceYielded, make_sources

TABLE = "explore_close_outcomes"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _dump(decision):
    return mapping.decision_to_dict(decision)


def _load(row):
    value = json.loads(row["decision_json"])
    # A terminal measurement exclusion can never become eligible on a retry.
    value["status"], value["reason"] = row["enrollment_status"], row["unavailable_reason"]
    return mapping.decision_from_dict(value)


def source_records(journal):
    """No provider calls. Incomplete scans/proofs produce no frozen enrollment."""
    if journal.conn.in_transaction:
        raise RuntimeError("close_ref requires no caller transaction")
    journal.conn.execute("BEGIN")
    try:
        candidates = journal.query("SELECT * FROM candidates ORDER BY id")
        batches = journal.query("SELECT * FROM scan_batches ORDER BY id")
        evaluations = journal.query("SELECT * FROM openai_evaluations ORDER BY id")
        proposals = journal.query("SELECT * FROM trade_proposals WHERE policy_version='explore_1' ORDER BY id")
        labels = journal.query("SELECT * FROM candidate_labels WHERE relabel_of IS NULL AND shadow_tier=1 ORDER BY id")
        lineages = {r["lineage_id"]: r for r in journal.query("SELECT * FROM lineage_snapshots")}
        journal.conn.commit()
    except BaseException:
        journal.conn.rollback()
        raise
    # A running scan may have opinions but not all opinions, or only some arm
    # updates. Wait for the producer's terminal write, never infer from its age.
    closed = {b["scan_batch_id"]: b for b in batches if b.get("completed_at_utc")}
    by_scan, by_candidate = defaultdict(list), {c["candidate_id"]: c for c in candidates}
    for c in candidates:
        by_scan[c.get("scan_batch_id")].append(c)
    eval_by_scan, proposals_by_scan = defaultdict(list), defaultdict(list)
    for e in evaluations:
        eval_by_scan[by_candidate.get(e["candidate_id"], {}).get("scan_batch_id")].append(e)
    for p in proposals:
        proposals_by_scan[p.get("scan_batch_id")].append(p)
    proofs, commits, proven, deferred = {}, {}, set(), set()
    for scan_id, batch in closed.items():
        try:
            proof = scan_proof(by_scan[scan_id], eval_by_scan[scan_id], lineages)
            if type(proof.get("verified")) is not bool:
                raise ValueError("invalid source proof")
            # Only established source identity/digest failures are permanent.
            # Unknown failure results also wait, rather than freezing a verdict
            # based on a transient or unsupported verification path.
            retry = proof.get("reason") == "source_commit_unavailable" or (
                not proof["verified"] and proof.get("reason") not in {
                    "source_commit_missing", "scan_source_identity_unproven", "source_ordering_unreviewed",
                }
            )
            proof = dict(proof)
        except Exception as exc:
            proof = {"verified": False, "reason": "source_proof_exception", "error_type": type(exc).__name__}
            retry = True
        if retry:
            proof["deferred"] = True
            deferred.add(scan_id)
        proofs[scan_id] = proof
        if retry:
            continue
        if proof.get("commit"):
            commits[scan_id] = proof["commit"]
        if proof["verified"]:
            proven.add(scan_id)
    admitted = {scan_id: batch for scan_id, batch in closed.items() if scan_id not in deferred}
    records = []
    for scan_id, batch in admitted.items():
        if batch.get("explore_draw_json") is not None:
            records.extend(mapping.routing_records(
                batch, by_scan[scan_id], eval_by_scan[scan_id], proposals_by_scan[scan_id],
                source_commit=commits.get(scan_id) if scan_id in proven else None,
            ))
    available = [c for c in candidates if c.get("scan_batch_id") in admitted]
    records += mapping.evaluator_records(available, list(admitted.values()), evaluations,
                                          non_degraded_scan_ids=proven, source_commits=commits)
    records += mapping.shadow_records(available, list(admitted.values()), labels, source_commits=commits)
    return records, proofs, len(batches) - len(closed)


def enroll(journal, *, provider, feed, adjustment="split", limit=500, should_yield=None):
    if type(limit) is not int or limit < 0 or not provider or not feed or adjustment != "split":
        raise ValueError("invalid close_ref enrollment request")
    should_yield = should_yield or (lambda: False)
    if not limit or should_yield():
        return {"inserted": 0, "yielded": bool(should_yield())}
    records, proofs, open_scans = source_records(journal)
    saved = journal.query(f"SELECT * FROM {TABLE} ORDER BY id")
    old = {_load(row).identity: row for row in saved}
    # Existing source membership/instants are immutable. Source edits never
    # replace a frozen decision, even if they would rescue an unavailable row.
    merged = {_load(row).identity: _load(row) for row in saved}
    for record in records:
        if record.status != "out_of_scope":
            merged.setdefault(record.identity, record)
    resolved = mapping.deduplicate(list(merged.values()))
    inserted = excluded = 0
    for record in resolved:
        if should_yield():
            break
        prior = old.get(record.identity)
        if prior is not None:
            if (prior["enrollment_status"] in ("ready", "duplicate_in_session")
                    and record.status not in ("ready", prior["enrollment_status"])):
                # A newly discovered earlier/overlapping decision invalidates
                # membership only; frozen prices/returns and evidence stay put.
                journal.conn.execute(
                    f"UPDATE {TABLE} SET enrollment_status=?,unavailable_reason=? "
                    "WHERE outcome_id=? AND enrollment_status IN ('ready','duplicate_in_session')",
                    (record.status, record.reason, prior["outcome_id"]),
                )
                journal.conn.commit()
                excluded += 1
            continue
        if inserted >= limit:
            continue
        w = record.window
        value = _dump(record)
        journal.insert(TABLE, {
            "outcome_id": "close_" + hashlib.sha256(_json(record.identity).encode()).hexdigest(),
            "method": engine.METHOD, "population": record.population, "arm": record.arm,
            "candidate_id": record.candidate_id, "symbol": record.symbol,
            "scan_batch_id": record.scan_batch_id, "source_table": record.source_table,
            "source_id": record.source_id, "source_commit": record.source_commit,
            "source_ordering_json": proofs.get(record.scan_batch_id, {}), "basis": record.basis,
            "decision_instant_utc": record.decision_at_utc,
            "lower_bound_utc": record.lower_bound_utc, "upper_bound_utc": record.upper_bound_utc,
            "reference_session": w.reference.date if w else None,
            "endpoint_5d": w.endpoint_5.date if w else None,
            "endpoint_10d": w.endpoint_10.date if w else None,
            "calendar_version": record.calendar_version, "enrollment_status": record.status,
            "unavailable_reason": record.reason, "decision_json": value,
            "provider": provider, "feed": feed, "adjustment": adjustment,
            "status_5d": "pending" if record.status == "ready" else "excluded",
            "status_10d": "pending" if record.status == "ready" else "excluded",
        })
        inserted += 1
    deferred = {scan_id: proof for scan_id, proof in proofs.items() if proof.get("deferred")}
    return {"inserted": inserted, "newly_excluded": excluded, "open_scans_deferred": open_scans,
            "source_proof_scans_deferred": len(deferred),
            "source_proof_deferred_reasons": dict(Counter(p.get("reason") or "source_proof_unavailable" for p in deferred.values())),
            "source_proof_deferred": deferred,
            "out_of_scope": sum(r.status == "out_of_scope" for r in records),
            "yielded": bool(should_yield()), "summary": summary(journal)}


def _leg(value):
    return engine.PriceLeg(**json.loads(value)["leg"]) if value else None


def update(journal, sources, *, limit=500, should_yield=None):
    if type(limit) is not int or limit < 0:
        raise ValueError("invalid close_ref update limit")
    should_yield = should_yield or (lambda: False)
    if sources is None or not limit or should_yield():
        return {"attempted": 0, "completed": 0, "unavailable": 0, "yielded": bool(should_yield()),
                "source_unavailable": sources is None}
    if journal.conn.in_transaction:
        raise RuntimeError("close_ref price requests require no caller transaction")
    rows = journal.query(
        f"SELECT * FROM {TABLE} WHERE enrollment_status='ready' "
        "AND (status_5d='pending' OR status_10d='pending') "
        "ORDER BY COALESCE(last_attempt_at_utc,''),id LIMIT ?", (limit,),
    )
    result = {"attempted": 0, "completed": 0, "unavailable": 0, "yielded": False}
    for row in rows:
        if should_yield():
            break
        if (row["provider"], row["feed"], row["adjustment"]) != (sources.provider, sources.feed, sources.adjustment):
            raise ValueError("close_ref stored price source changed")
        decision = _load(row)
        w = decision.window
        if w is None:
            raise ValueError("ready close_ref row has no pinned window")
        journal.conn.execute(f"UPDATE {TABLE} SET last_attempt_at_utc=? WHERE id=?",
                             (sources.now().isoformat(), row["id"]))
        journal.conn.commit()
        result["attempted"] += 1
        for horizon in (5, 10):
            if should_yield():
                break
            if row[f"status_{horizon}d"] != "pending":
                continue
            akey, bkey = f"candidate_{horizon}d_json", f"benchmark_{horizon}d_json"
            for key, symbol in ((akey, row["symbol"]), (bkey, "SPY")):
                if should_yield():
                    break
                prior_leg = _leg(row[key])
                if prior_leg and prior_leg.status in ("complete", "unavailable"):
                    continue
                try:
                    observation = sources.observe(w, horizon, symbol)
                except SliceYielded:
                    result["yielded"] = True
                    return result
                encoded = _json(observation)
                # The leg evidence and its unsigned return have one durable
                # write. An interruption cannot leave a frozen complete leg
                # without its scalar, or cause that leg to be fetched again.
                observed_leg = _leg(encoded)
                scalar = f"r_{horizon}d" if key == akey else f"b_{horizon}d"
                scalar_value = (observed_leg.return_fraction
                                if observed_leg and observed_leg.status == "complete" else None)
                cursor = journal.conn.execute(
                    f"UPDATE {TABLE} SET {key}=?,{scalar}=? WHERE id=? AND {key} IS ?",
                    (encoded, scalar_value, row["id"], row[key]),
                )
                if cursor.rowcount != 1:
                    journal.conn.rollback()
                    raise RuntimeError("close_ref concurrent result writer")
                journal.conn.commit()
                row[key] = encoded
            a, b = _leg(row[akey]), _leg(row[bkey])
            if a and b:
                combined = engine.combine_legs(a, b)
                status = combined["status"]
                journal.conn.execute(
                    f"UPDATE {TABLE} SET status_{horizon}d=?,r_{horizon}d=?,b_{horizon}d=? WHERE id=?",
                    (status, a.return_fraction if a.status == "complete" else None,
                     b.return_fraction if b.status == "complete" else None, row["id"]),
                )
                journal.conn.commit()
                if status in ("complete", "unavailable"):
                    result["completed" if status == "complete" else "unavailable"] += 1
    result["yielded"] = bool(should_yield())
    return result


def summary(journal):
    rows = journal.query(f"SELECT population,arm,enrollment_status,unavailable_reason,status_5d,status_10d,decision_json FROM {TABLE}")
    # Measurement enrollment and evaluator ranking are different denominators.
    # Read the frozen original qualification, never relabel historical opinions.
    evaluator_ranking: Counter[str] = Counter()
    ranking_reasons: Counter[str] = Counter()
    for row in rows:
        if row["population"] != "evaluator":
            continue
        decision = json.loads(row["decision_json"])
        eligible = row["enrollment_status"] == "ready" and decision.get("ranking_eligible") is True
        evaluator_ranking["eligible" if eligible else "excluded"] += 1
        if not eligible:
            ranking_reasons[decision.get("ranking_exclusion_reason") or row["unavailable_reason"]
                            or (row["enrollment_status"] if row["enrollment_status"] != "ready" else "ranking_unproven")] += 1
    populations = {}
    for key in sorted({f"{r['population']}:{r['arm']}" for r in rows}):
        group = [r for r in rows if f"{r['population']}:{r['arm']}" == key]
        eligible = [r for r in group if r["enrollment_status"] == "ready"]
        populations[key] = {
            "total": len(group), "eligible": len(eligible), "excluded": len(group) - len(eligible),
            "complete_5d": sum(r["status_5d"] == "complete" for r in eligible),
            "complete_10d": sum(r["status_10d"] == "complete" for r in eligible),
            "enrollment": dict(Counter(r["enrollment_status"] for r in group)),
        }
    return {"total": len(rows), "populations": populations,
            "evaluator_ranking": {"total": sum(evaluator_ranking.values()),
                                  "eligible": evaluator_ranking["eligible"], "excluded": evaluator_ranking["excluded"],
                                  "exclusion_reasons": dict(ranking_reasons)},
            "enrollment": dict(Counter(r["enrollment_status"] for r in rows)),
            "exclusion_reasons": dict(Counter(r["unavailable_reason"] for r in rows if r["unavailable_reason"])),
            "horizon_5d": dict(Counter(r["status_5d"] for r in rows if r["enrollment_status"] == "ready")),
            "horizon_10d": dict(Counter(r["status_10d"] for r in rows if r["enrollment_status"] == "ready"))}


def run_close_ref(orch, bars_provider, *, limit=500, should_yield=None):
    should_yield = should_yield or (lambda: False)
    sources = make_sources(orch, bars_provider, should_yield=should_yield)
    # Enroll even while a provider is temporarily absent. No model/provider call
    # is needed to preserve the original evidence and its permanent exclusions.
    seeded = enroll(orch.journal, provider="alpaca", feed=orch.settings.market_data_feed,
                    limit=limit, should_yield=should_yield)
    updated = update(orch.journal, sources, limit=limit, should_yield=should_yield)
    return {"seeded": seeded, "updated": updated}
