"""Read-only replay of production live candidates (since 2026-10-01) through explore_policy.route."""
import json, sqlite3, sys, collections
from types import SimpleNamespace
import alphaos
from alphaos import explore_policy as ep
from alphaos.ai.openai_client import OpenAIEvaluation
assert "audit-1c3d3ea" in alphaos.__file__, alphaos.__file__
db = sqlite3.connect("file:/Users/ck/Documents/Claude Playground/AlphaOS/data/alphaos.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row
settings = SimpleNamespace(min_reward_risk=1.2, stop_loss_pct=0.03, target_reward_risk=1.5)
rows = db.execute("""
SELECT c.candidate_id, c.symbol, c.direction cdir, c.scan_batch_id, c.price_at_scan, c.shadow_tier,
       e.eval_id, e.direction edir, e.entry, e.stop, e.target, e.max_holding_days, e.expected_r, e.confidence,
       e.decision, e.data_freshness_status, e.risk_flags_json, e.validation_status evs,
       l.label_decision, l.direction ldir, l.validation_status lvs, l.symbol lsym, l.candidate_id lcid,
       b.started_at_utc
FROM candidates c JOIN openai_evaluations e ON e.candidate_id=c.candidate_id
JOIN scan_batches b ON b.scan_batch_id=c.scan_batch_id
LEFT JOIN candidate_labels l ON l.candidate_id=c.candidate_id AND l.relabel_of IS NULL
WHERE c.created_at_utc >= '2026-10-01' AND c.shadow_tier=0 ORDER BY b.started_at_utc, c.id""").fetchall()
by_batch = collections.OrderedDict()
stats = collections.Counter(); src = collections.Counter(); why_none = collections.Counter()
for r in rows:
    cand = SimpleNamespace(snapshot={"last_price": r["price_at_scan"]})
    d = {"candidate_id": r["candidate_id"], "symbol": r["symbol"], "direction": r["cdir"], "shadow_tier": r["shadow_tier"]}
    class C(dict):
        snapshot = {"last_price": r["price_at_scan"]}
    cand = C(d)
    ev = OpenAIEvaluation(r["eval_id"], r["candidate_id"], r["symbol"], "m", r["edir"], r["entry"], r["stop"], r["target"],
                          r["max_holding_days"], r["expected_r"], r["confidence"], r["decision"], "",
                          data_freshness_status=r["data_freshness_status"], risk_flags=json.loads(r["risk_flags_json"] or "[]"),
                          validation_status=r["evs"])
    lab = None
    if r["label_decision"] is not None:
        lab = SimpleNamespace(candidate_id=r["lcid"], symbol=r["lsym"], direction=r["ldir"], label_decision=r["label_decision"], validation_status=r["lvs"])
    out = ep.route(cand, ev, lab, settings, 10)
    b = by_batch.setdefault(r["scan_batch_id"], {"start": r["started_at_utc"], "arms": [], "pool": 0, "n": 0})
    b["n"] += 1
    if out is None:
        stats["ineligible"] += 1
        why = ("cand_short" if r["cdir"]=="short" else "eval_short" if r["edir"]!="long" else
               "no_label" if lab is None else "label_not_long" if r["ldir"]!="long" else "other")
        why_none[(r["decision"], why)] += 1
        continue
    arm, plan = out
    stats[arm or "pool"] += 1
    src[(arm or "pool", plan.stop_source or "evaluator")] += 1
    if arm: b["arms"].append(arm)
    else: b["pool"] += 1
print("candidates", len(rows), "batches", len(by_batch))
print("route outcome:", dict(stats))
print("level source:", {f"{k[0]}/{k[1]}": v for k, v in sorted(src.items())})
print("ineligible by (eval decision, reason):", {f"{k[0]}/{k[1]}": v for k, v in sorted(why_none.items(), key=lambda kv: -kv[1])})
days = len({b["start"][:10] for b in by_batch.values()})
ab = sum(len(b["arms"]) for b in by_batch.values())
c_draws = sum(1 for b in by_batch.values() if b["pool"] > 0)
print(f"trading days {days}; A/B/A+B proposals {ab} ({ab/days:.2f}/day); scans with non-empty control pool {c_draws}/{len(by_batch)} ({c_draws/days:.2f}/day)")
per_scan = collections.Counter(len(b["arms"]) + (1 if b["pool"] else 0) for b in by_batch.values())
print("staged proposals per scan (arms + 1 control if pool):", dict(sorted(per_scan.items())))
print("pool size per scan: min/median/max", sorted(b["pool"] for b in by_batch.values())[0], sorted(b["pool"] for b in by_batch.values())[len(by_batch)//2], max(b["pool"] for b in by_batch.values()))
print("cands per scan median", sorted(b["n"] for b in by_batch.values())[len(by_batch)//2])
