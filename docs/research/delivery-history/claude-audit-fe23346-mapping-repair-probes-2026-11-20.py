"""Claude audit probes for Codex fe23346 (EXPLORE-1 mapping repair). Run from clone root on COPIES only."""
import sys, json, shutil, subprocess, collections, urllib.request, io
from dataclasses import replace
from alphaos.journal.journal_store import JournalStore
from alphaos.learning import close_ref_tracker as tr, close_ref_lineage as lin, close_ref_mapping as mp
SNAP = sys.argv[1]; WORK = sys.argv[2]
res = {}
def fresh(name):
    p = f"{WORK}/{name}.db"; shutil.copy(SNAP, p); return JournalStore(p)
def table(j):
    rows = j.query("select population,arm,candidate_id,source_table,source_id,symbol,scan_batch_id,enrollment_status,unavailable_reason,reference_session,decision_json from explore_close_outcomes")
    return {(r["population"],r["arm"],r["candidate_id"],r["source_table"],r["source_id"]):
            (r["enrollment_status"],r["unavailable_reason"],r["reference_session"],
             json.loads(r["decision_json"]).get("ranking_eligible"), json.loads(r["decision_json"]).get("ranking_exclusion_reason")) for r in rows}, rows
real_run = subprocess.run
# G1 healthy baseline
j0 = fresh("healthy"); lin.committed_ordering.cache_clear()
o1 = tr.enroll(j0, provider="alpaca", feed="iex", limit=100000)
o2 = tr.enroll(j0, provider="alpaca", feed="iex", limit=100000)
base, rows0 = table(j0)
s = o2["summary"]
res["G1_healthy"] = {"inserted_pass1": o1["inserted"], "inserted_repeat": o2["inserted"], "deferred": o1["source_proof_scans_deferred"],
                     "total": s["total"], "enrollment": s["enrollment"], "evaluator_ranking": s["evaluator_ranking"]}
# G2 D1: the five 18-Nov rows + every OPENAI_REJECT original is failsafe, none ranking-eligible
evs = {r["eval_id"]: r for r in j0.query("select eval_id,risk_flags_json,created_at_utc,symbol from openai_evaluations")}
fs = [(k, v) for k, v in base.items() if k[0]=="evaluator" and "OPENAI_REJECT" in (evs[k[4]]["risk_flags_json"] or "")]
res["G2_failsafe"] = {"reject_originals": len(fs), "all_failsafe_reason": all(v[4]=="evaluator_failsafe" for _, v in fs),
    "none_eligible": all(v[3] is False for _, v in fs),
    "nov18_symbols_ready_measurement": sorted({evs[k[4]]["symbol"] for k, v in fs if evs[k[4]]["created_at_utc"].startswith("2026-11-18") and v[0]=="ready"}),
    "eligible_ready_genuine": sum(1 for k,v in base.items() if k[0]=="evaluator" and v[3] and v[0]=="ready")}
# G3 failsafe consumes no key: earlier failsafe twin + genuine same key -> genuine stays ready
recs, proofs, _ = tr.source_records(j0)
gen = [r for r in recs if r.population=="evaluator" and r.ranking_eligible and r.status=="ready" and r.decision_at_utc]
g = gen[0]
twin = replace(g, candidate_id=g.candidate_id+"-fs", source_id=g.source_id+"-fs", ranking_eligible=False,
               ranking_exclusion_reason="evaluator_failsafe", decision_at_utc=g.decision_at_utc.replace(g.decision_at_utc[11:13], "%02d" % (int(g.decision_at_utc[11:13])-1)))
out = {r.identity: r for r in mp.deduplicate([twin, g])}
res["G3_no_key"] = {"genuine": (out[g.identity].status, out[g.identity].reason), "failsafe": (out[twin.identity].status, out[twin.identity].ranking_exclusion_reason)}
# G4 D2: transient timeout in pass 1, healthy pass 2 WITHOUT cache_clear; must equal healthy baseline
j1 = fresh("transient"); lin.committed_ordering.cache_clear()
def boom(*a, **k): raise subprocess.TimeoutExpired(a[0], 2)
lin.subprocess.run = boom
p1 = tr.enroll(j1, provider="alpaca", feed="iex", limit=100000)
lin.subprocess.run = real_run
p2 = tr.enroll(j1, provider="alpaca", feed="iex", limit=100000)
t1, _ = table(j1)
res["G4_transient_timeout"] = {"pass1_inserted": p1["inserted"], "pass1_deferred": p1["source_proof_scans_deferred"], "pass1_reasons": p1["source_proof_deferred_reasons"],
                              "pass2_inserted": p2["inserted"], "pass2_deferred": p2["source_proof_scans_deferred"], "equals_healthy": t1 == base}
# G5 git exits non-zero (object missing) then healthy
j2 = fresh("nonzero"); lin.committed_ordering.cache_clear()
lin.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a[0], 128, b"", b"fatal")
q1 = tr.enroll(j2, provider="alpaca", feed="iex", limit=100000)
lin.subprocess.run = real_run
q2 = tr.enroll(j2, provider="alpaca", feed="iex", limit=100000)
t2, _ = table(j2)
res["G5_nonzero"] = {"pass1_inserted": q1["inserted"], "pass1_reasons": q1["source_proof_deferred_reasons"], "pass2_inserted": q2["inserted"], "equals_healthy": t2 == base}
# G6 arbitrary exception inside scan_proof
j3 = fresh("exc"); lin.committed_ordering.cache_clear()
orig_sp = tr.scan_proof
tr.scan_proof = lambda *a, **k: (_ for _ in ()).throw(KeyError("x"))
e1 = tr.enroll(j3, provider="alpaca", feed="iex", limit=100000)
tr.scan_proof = orig_sp
e2 = tr.enroll(j3, provider="alpaca", feed="iex", limit=100000)
t3, _ = table(j3)
res["G6_exception"] = {"pass1_inserted": e1["inserted"], "pass1_reasons": e1["source_proof_deferred_reasons"], "pass2_inserted": e2["inserted"], "equals_healthy": t3 == base}
# G7 permanent reasons still freeze: digest unreviewed
j4 = fresh("unrev"); lin.committed_ordering.cache_clear()
saved = lin._APPROVED_PRODUCERS.copy(); lin._APPROVED_PRODUCERS.clear()
u1 = tr.enroll(j4, provider="alpaca", feed="iex", limit=100000)
lin._APPROVED_PRODUCERS.update(saved); lin.committed_ordering.cache_clear()
u2 = tr.enroll(j4, provider="alpaca", feed="iex", limit=100000)
t4, rows4 = table(j4)
res["G7_unreviewed_permanent"] = {"pass1_inserted": u1["inserted"], "pass1_deferred": u1["source_proof_scans_deferred"], "pass2_inserted": u2["inserted"],
    "differs_from_healthy": t4 != base, "reasons": dict(collections.Counter(r["unavailable_reason"] for r in rows4 if r["unavailable_reason"]))}
# G8 deferral ordering: defer the scan holding a cross-scan ready key, heal, compare with healthy
cross = collections.defaultdict(set)
for k, v in base.items():
    pass
keyscan = collections.defaultdict(list)
for r in rows0:
    d = json.loads(r["decision_json"])
    if r["enrollment_status"] in ("ready","duplicate_in_session"):
        keyscan[(r["population"], r["arm"], r["symbol"], r["reference_session"])].append((r["enrollment_status"], r["scan_batch_id"]))
multi = {k: v for k, v in keyscan.items() if len({s for _, s in v}) > 1}
target = None
for k, v in multi.items():
    ready_scans = [s for st, s in v if st=="ready"]
    if ready_scans: target = ready_scans[0]; break
res["G8_cross_scan_keys"] = len(multi)
if target:
    j5 = fresh("order"); lin.committed_ordering.cache_clear()
    def sp(c, e, l):
        if c and c[0].get("scan_batch_id") == target: raise OSError("transient")
        return orig_sp(c, e, l)
    tr.scan_proof = sp
    f1 = tr.enroll(j5, provider="alpaca", feed="iex", limit=100000)
    tr.scan_proof = orig_sp
    f2 = tr.enroll(j5, provider="alpaca", feed="iex", limit=100000)
    t5, _ = table(j5)
    diffs = {str(k): (t5.get(k), base.get(k)) for k in set(t5)|set(base) if t5.get(k) != base.get(k)}
    res["G8_late_admission"] = {"target": target, "pass1_inserted": f1["inserted"], "pass2_inserted": f2["inserted"], "pass2_newly_excluded": f2["newly_excluded"],
                                "equals_healthy": t5 == base, "diffs": diffs}
# G9 split request: URL carries adjustment=split; default stays raw; bad value refused; sources refuse raw
from alphaos.data.providers import alpaca_bars as ab
from alphaos.learning.close_ref_sources import CloseRefSources
urls = []
class FakeResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False
def fake_urlopen(req, timeout=None):
    urls.append(req.full_url); return FakeResp(json.dumps({"bars": []}).encode())
ab.urllib.request.urlopen = fake_urlopen
class St: has_alpaca_keys=True; market_data_feed="iex"; alpaca_api_key="k"; alpaca_secret_key="s"; alpaca_api_key_id="k"; alpaca_api_secret_key="s"
prov = ab.AlpacaBarsProvider.__new__(ab.AlpacaBarsProvider); prov.settings = St(); prov.journal = None
prov.get_daily_bars("AAPL", "2026-11-18", "2026-11-26", adjustment="split"); prov.get_daily_bars("AAPL", "2026-11-18", "2026-11-26")
try: prov.get_daily_bars("AAPL", "2026-11-18", "2026-11-26", adjustment="all"); bad = "accepted"
except ValueError: bad = "refused"
try: CloseRefSources(None, prov, provider="alpaca", feed="iex", adjustment="raw"); raw_src = "accepted"
except ValueError: raw_src = "refused"
try: tr.enroll(fresh("rawenroll"), provider="alpaca", feed="iex", adjustment="raw"); raw_en = "accepted"
except ValueError: raw_en = "refused"
res["G9_split"] = {"urls": [u.split("?")[1] for u in urls], "invalid_adjustment": bad, "sources_raw": raw_src, "enroll_raw": raw_en}
print(json.dumps(res, indent=1, default=str))
