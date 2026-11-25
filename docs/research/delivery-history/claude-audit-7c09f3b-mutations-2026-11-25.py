import subprocess, pathlib, os, re, time
root = pathlib.Path(os.environ["MUT"]); R = root/"alphaos/research"
M = [
 ("M1 partial evidence never attached", "last30days_provider.py", 'diagnostic["partial_evidence"] = read_partial(', 'diagnostic["partial_evidence_x"] = read_partial('),
 ("M2 receipts drop all items", "last30days_partial.py", "                items.append(row)\n", "                omitted += 1\n"),
 ("M3 no-items mislabelled as evidence", "last30days_partial.py", 'else "no_retained_source_items"', 'else "incomplete_source_evidence"'),
 ("M4 unfinished calls hidden", "last30days_partial.py", "unfinished_calls=sorted(set(starts) - set(ends))", "unfinished_calls=[]"),
 ("M5 dispatch not counted", "last30days_source_policy.py", "self.observed[source] += 1", "self.observed[source] += 0"),
 ("M6 title leaked into receipt", "last30days_partial.py", '"date_confidence": getattr(item, "date_confidence", None),\n        "engagement": {},', '"date_confidence": getattr(item, "title", None),\n        "engagement": {},'),
 ("M7 wrong deadline passed to process", "last30days_provider.py", "proc.communicate(timeout=timeout)", "proc.communicate(timeout=timeout * 2)"),
 ("M8 capture window halved", "last30days_partial.py", "0 <= elapsed <= self.deadline", "0 <= elapsed <= self.deadline / 2"),
 ("M9 partial marked research complete", "last30days_partial.py", '"research_complete": False', '"research_complete": True'),
 ("M10 partial marked model eligible", "last30days_partial.py", '"model_context_eligible": False', '"model_context_eligible": True'),
 ("M11 receipt recorder broken", "last30days_partial.py", "            written = os.write(self.fd, payload)", "            raise RuntimeError('x')"),
 ("M12 worker dies before barrier", "last30days_worker.py", 'telemetry.emit("worker_start")', 'raise SystemExit(3)'),
 ("M13 timeout outcome mislabelled", "last30days_provider.py", 'diagnostic["outcome"] = "timeout"', 'diagnostic["outcome"] = "returned"'),
 ("M14 source errors claimed observed", "last30days_partial.py", '"not_observed_internal_errors_may_be_suppressed"', '"observed"'),
 ("CONTROL no mutation", None, None, None),
]
for name, f, a, b in M:
    subprocess.run(["git","checkout","-q","--","."],cwd=root,check=True)
    if f:
        p = R/f; s = p.read_text(); assert s.count(a) >= 1, (name, s.count(a)); p.write_text(s.replace(a, b, 1))
    t=time.time()
    r = subprocess.run([".venv/bin/python","-m","pytest","-o","addopts=","-q","-p","no:cacheprovider","tests/test_last30days_partial_flow.py"],cwd=root,env={**os.environ,"PYTHONPATH":str(root)},capture_output=True,text=True)
    failed = re.findall(r"FAILED \S+::(\S+)", r.stdout)
    print(f"{name:42s} exit={r.returncode} {time.time()-t:5.1f}s {'KILLED' if r.returncode else 'survived'} {[x.replace('test_','')[:38] for x in failed]}", flush=True)
subprocess.run(["git","checkout","-q","--","."],cwd=root,check=True)
