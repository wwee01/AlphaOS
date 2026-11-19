import sys, json, collections, subprocess
sys.path.insert(0, "clone")
from alphaos.journal.journal_store import JournalStore
from alphaos.learning import close_ref_tracker as tr, close_ref_lineage as lin
real_run = subprocess.run
def slow(*a, **k):
    raise subprocess.TimeoutExpired(a[0], 2)
lin.committed_ordering.cache_clear()
lin.subprocess.run = slow                     # one transient git timeout during the first enrollment pass
j = JournalStore("transient.db")
tr.enroll(j, provider="alpaca", feed="iex", limit=100000)
lin.subprocess.run = real_run                 # git healthy again
lin.committed_ordering.cache_clear()
tr.enroll(j, provider="alpaca", feed="iex", limit=100000)
rows = j.query("select population, enrollment_status, unavailable_reason, decision_json from explore_close_outcomes")
c = collections.Counter()
for r in rows:
    d = json.loads(r["decision_json"])
    c[(r["population"], r["enrollment_status"], r["unavailable_reason"], d.get("ranking_exclusion_reason") if r["population"]=="evaluator" else None)] += 1
for k, v in sorted(c.items(), key=str): print(k, v)
