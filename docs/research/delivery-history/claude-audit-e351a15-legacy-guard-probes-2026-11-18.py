"""Claude audit probes for Codex e351a15 (EXPLORE-1 legacy-refusal guard), 2026-11-18.

Usage (copies only; never data/alphaos.db):
  PYTHONPATH=<checkout> python probes.py <snapshot.db> <workdir> <label>
Run once with the guard checkout (e351a15) and once with the parent (fc08f73);
the parent run documents the hazard, the guard run must refuse.
"""
import hashlib, json, shutil, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path
import alphaos
SNAP, WD, LABEL = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
print("alphaos from", alphaos.__file__)
from alphaos.journal.journal_store import JournalStore
from alphaos.stats import preregistration as pr
from alphaos.stats import research_validity as rv
from alphaos.hypotheses import resolver
EXP = ["prereg_c1b61ea18ba4", "prereg_33d3f0c7ea57", "prereg_699b00b2a9b9"]
out = {}

def fresh(n):
    p = WD / f"{LABEL}-{n}.db"; shutil.copy(SNAP, p); return p
def prereg_hash(p):
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    h = hashlib.sha256(repr(c.execute("select * from preregistrations order by id").fetchall()).encode()).hexdigest()
    c.close(); return h

# G1 direct legacy evaluate_hypothesis on each production row (on a copy)
p = fresh("g1"); before = prereg_hash(p); j = JournalStore(str(p)); g1 = {}
rows = [{"symbol": f"S{i}", "decision_date": f"2026-11-{16+i%10:02d}", "max_holding_days": 5, "v": 0.01*i} for i in range(60)]
for pid in EXP:
    try:
        r = pr.evaluate_hypothesis(j, pid, rows, "v"); g1[pid] = f"EVALUATED status={r['evidence_status']}"
    except Exception as e:
        g1[pid] = f"{type(e).__name__}: {str(e)[:90]}"
j.conn.close(); out["G1_direct"] = {"results": g1, "registry_unchanged": prereg_hash(p) == before}

# G2 accidental links through the scheduled resolver, with a metric function that would succeed
p = fresh("g2"); before = prereg_hash(p); j = JournalStore(str(p))
resolver.METRIC_FUNCTIONS["h_probe_rows"] = lambda journal: (rows, "v", None)
import alphaos.hypotheses.resolver as rmod
cols = [r[1] for r in j.conn.execute("PRAGMA table_info(hypothesis_proposals)")]
tmpl = dict(zip(cols, j.conn.execute("select * from hypothesis_proposals where hypothesis_id='H-TTL-1'").fetchone()))
for i, pid in enumerate(EXP):
    r = dict(tmpl); r.pop("id"); r.update(hypothesis_id=f"H-PROBE-{i}", prereg_id=pid, metric_fn_name="h_probe_rows",
                                       status="testing", analysis_not_before="2026-01-01", resolved_at_utc=None)
    j.conn.execute(f"INSERT INTO hypothesis_proposals ({','.join(r)}) VALUES ({','.join('?'*len(r))})", tuple(r.values()))
j.conn.commit()
summ = resolver.resolve_due_hypotheses(j, now=datetime(2027, 3, 1, 2, tzinfo=timezone.utc))
exp_after = [dict(x) for x in j.conn.execute(f"select prereg_id, evaluated_at_utc, evidence_status from preregistrations where prereg_id in (?,?,?)", EXP)]
j.conn.close()
out["G2_scheduled_linked"] = {"evaluated": summ.get("evaluated"), "legacy_refused": [x["hypothesis_id"] for x in summ.get("legacy_refused", [])],
                              "quarantined": [x["hypothesis_id"] for x in summ.get("quarantined", [])], "errors": summ.get("errors"),
                              "exp1_after": exp_after}

# G3 quarantine compatibility: classification of every registry row (with and without its proposal)
c = sqlite3.connect(f"file:{SNAP}?mode=ro", uri=True); c.row_factory = sqlite3.Row
props = {r["prereg_id"]: dict(r) for r in c.execute("select hypothesis_id, metric_fn_name, prereg_id from hypothesis_proposals")}
cls = {}
for r in c.execute("select * from preregistrations order by id"):
    r = dict(r)
    cls[r["prereg_id"]] = [rv.registration_validity(r), rv.registration_validity(r, props.get(r["prereg_id"]))]
out["G3_classification"] = cls

# G4 two-arm writer with EXPLORE-1 rows (A as pos, POOL as neg)
p = fresh("g4"); before = prereg_hash(p); j = JournalStore(str(p))
try:
    res = pr.evaluate_two_arm_hypothesis_pair(j, EXP[0], EXP[1], as_of_utc="2027-03-01T02:00:00Z")
    g4 = {k: res.get(k) for k in ("outcome", "reason")}
except Exception as e:
    g4 = f"{type(e).__name__}: {str(e)[:90]}"
j.conn.close(); out["G4_two_arm"] = {"result": g4, "registry_unchanged": prereg_hash(p) == before}

# G5 an ordinary new (non-EXPLORE) registration still evaluates through the legacy path
p = fresh("g5"); j = JournalStore(str(p))
pid = pr.register_hypothesis(j, "H-PROBE-ORDINARY: probe", "probe_metric", 1, 0.0, "2026-01-01", params={"probe": True})
r = pr.evaluate_hypothesis(j, pid, rows, "v"); j.conn.close()
out["G5_ordinary_still_evaluates"] = bool(r["evaluated_at_utc"]) and r["evidence_status"] in ("ok", "insufficient_data")
(WD / f"{LABEL}-result.json").write_text(json.dumps(out, indent=1, default=str))
print(json.dumps({k: v for k, v in out.items() if k != "G3_classification"}, indent=1, default=str))
