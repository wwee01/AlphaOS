"""Replay recorded scheduler/heartbeat JSON results through old and new exit rules (read-only log parse)."""
import json, sys, collections
from alphaos.scheduler.worker import _job_fault
def old_fault(r): return r.get("status") in ("failed", "fused", "blocked")
n = collections.Counter(); why = collections.Counter(); still = collections.Counter()
for line in open(sys.argv[1], errors="replace"):
    line = line.strip()
    if not line.startswith("{"): continue
    try: d = json.loads(line)
    except Exception: continue
    if d.get("role") != "research" or "results" not in d: continue
    h = d.get("monitor_health", {})
    old = int(any(old_fault(r) for r in d["results"]) or not h.get("ok"))
    new = int(any(_job_fault(r) for r in d["results"]) or not h.get("ok"))
    n[(old, new)] += 1
    if old and not new:
        for r in d["results"]:
            if old_fault(r): why[(r.get("job_type"), r.get("status"), r.get("reason"), tuple(r.get("monitor_health", {}).get("issue_codes", [])))] += 1
    if new:
        for r in d["results"]:
            if _job_fault(r): still[(r.get("job_type"), r.get("status"), str(r.get("reason"))[:60], tuple(r.get("monitor_health", {}).get("issue_codes", [])))] += 1
        if not h.get("ok"): still[("health_not_ok", tuple(h.get("issue_codes", [])))] += 1
print("ticks (old_exit, new_exit):", dict(n))
print("now exit 0 because:", dict(why))
print("still exit 1 because:", dict(still))
