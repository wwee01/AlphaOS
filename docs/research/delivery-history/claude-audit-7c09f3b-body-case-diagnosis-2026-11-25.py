import sys, os, re, subprocess, collections, json
tree=sys.argv[1]; n=int(sys.argv[2])
# Instrument a COPY of the sibling test only: dump diagnostics before its final assertion.
p=f"{tree}/tests/test_last30days_reddit_deadline.py"; src=open(p).read()
mark='    assert diagnostic["partial_evidence"]["retained_unique_item_count"] == 2\n'
assert src.count(mark)==1
dump='''    pol=[e for e in diagnostic["events"] if e["event"]=="source_policy"]
    print("DIAG", json.dumps({"phase":phase,"outcome":diagnostic["outcome"],"elapsed":round(diagnostic["elapsed_seconds"],3),"refused":[e.get("reason") for e in diagnostic["events"] if e.get("action")=="refused"],"status":diagnostic["partial_evidence"]["status"],"starts":sorted(e["source"] for e in diagnostic["events"] if e["event"]=="stage_start" and e["stage"]=="retrieval"),"ends":sorted(e["source"] for e in diagnostic["events"] if e["event"]=="stage_end" and e["stage"]=="retrieval"),"last_policy":pol[-1]["dispatch_counts"],"mono":all(sum(a["dispatch_counts"].values())<=sum(b["dispatch_counts"].values()) for a,b in zip(pol,pol[1:]))}))
'''
try:
    open(p,"w").write(src.replace(mark,dump+mark))
    c=collections.Counter()
    for _ in range(n):
        r=subprocess.run([".venv/bin/python","-m","pytest","-o","addopts=","-q","-s","-p","no:cacheprovider","tests/test_last30days_reddit_deadline.py","-k","body or executor"],cwd=tree,env={**os.environ,"PYTHONPATH":tree},capture_output=True,text=True)
        for l in re.findall(r"DIAG (\{.*\})", r.stdout):
            d=json.loads(l); e=d.pop("elapsed"); c[json.dumps(d,sort_keys=True)]+=1
    for k,v in c.most_common(): print(v,k)
finally: open(p,"w").write(src)
