"""Claude audit probes: SourcePolicy publication race, both directions (offline; real Telemetry, SourcePolicy,
PartialCapture, provider._read_telemetry and read_partial; only upstream pipeline is synthetic).
Interleaving is forced with events, never sleeps. Prototype repairs exist only as monkeypatches in this probe."""
import json, sys, tempfile, threading, time
from datetime import datetime, timezone
from types import SimpleNamespace
from alphaos.research import last30days_provider as prov, last30days_source_policy as sp, last30days_partial as part
from alphaos.research.last30days_worker import Telemetry

NONCE = "n"
def pipeline():
    def run(*, requested_sources, topic): pass
    def _ensure_jobs_in_plan(plan, available, *, explicit, topic): pass
    def _retrieve_stream(*, source):
        return ([{"id": "one"}] if source == "hackernews" else []), {}
    def _run_supplemental_searches(*, depth, mock): pass
    def _retry_thin_sources(*, plan): pass
    def _normalize_score_dedupe(source, raw, *a, **k):
        return [SimpleNamespace(item_id=r["id"], source=source, published_at=None, date_confidence="high", engagement={}) for r in raw]
    def plan_query(*, available_sources, requested_sources): pass
    return (SimpleNamespace(run=run, _ensure_jobs_in_plan=_ensure_jobs_in_plan, _retrieve_stream=_retrieve_stream,
            _run_supplemental_searches=_run_supplemental_searches, _retry_thin_sources=_retry_thin_sources,
            _normalize_score_dedupe=_normalize_score_dedupe, normalize_requested_sources=lambda s: s),
            SimpleNamespace(plan_query=plan_query))

def serialized(policy):
    """Codex's proposed shape: one publication lock around snapshot creation + emission."""
    lock = threading.Lock(); raw = policy.emit; make = policy.summary
    return lock, raw, make

class Rig:
    def __init__(self, repair):
        self.tf, self.pf = tempfile.TemporaryFile(), tempfile.TemporaryFile()
        self.started, self.utc = time.monotonic(), datetime.now(timezone.utc).isoformat()
        self.tel = Telemetry(self.tf.fileno())
        self.hold = None            # (thread name, reached event, release event)
        def emit(kind, **d):
            self._pause(); return self.tel.emit(kind, **d)
        self.policy = sp.SourcePolicy(["reddit", "hackernews"], emit)
        if repair:                  # serialize snapshot creation together with emission
            pub, state_summary = threading.Lock(), self.policy.summary
            def lazy(status): return _Lazy(status)
            class _Lazy(dict):
                def __init__(s, status): super().__init__(_status=status)
            self.policy.summary = lazy
            def publish(kind, **d):
                with pub:
                    snap = state_summary(d["_status"])   # created under pub lock
                    self._pause()
                    return self.tel.emit(kind, **snap)
            self.policy.emit = publish
        self.p, planner = pipeline()
        self.policy.install(self.p, planner)
        self.cap = part.PartialCapture(self.pf.fileno(), NONCE, self.policy.requested, sp.POLICY_VERSION, self.utc, self.started, 30.0)
        self.cap.install(self.p); self.tel.partial = self.cap
        self.policy.emit("source_policy", **self.policy.summary("installed"))
        self.p._retrieve_stream = self.tel.wrap(self.p._retrieve_stream, "retrieval")
    def _pause(self):
        h = self.hold
        if h and threading.current_thread().name == h[0]:
            h[1].set(); h[2].wait(10)
    def get(self, source, err):
        try:
            raw, _ = self.p._retrieve_stream(source=source); self.p._normalize_score_dedupe(source, raw)
        except sp.SourcePolicyError as e: err.append(type(e).__name__)
    def read(self):
        diag = prov._read_telemetry(self.tf)
        pe = part.read_partial(self.pf, nonce=NONCE, requested=["hackernews", "reddit"], policy_version=sp.POLICY_VERSION,
                               started_utc=self.utc, deadline=30.0, diagnostics=diag)
        seq = [(e["status"], e["dispatch_counts"], e["violations"]) for e in diag["events"] if e["event"] == "source_policy"]
        return {"status": pe["status"], "limitations": pe["limitations"] if pe["status"] == "unverified" else "ok",
                "items": len(pe["items"]), "snapshots": seq}

def interleave(repair, first, second, prelude=()):
    """`first` is held immediately before its policy snapshot is written; `second` then runs; then first resumes."""
    r = Rig(repair); err = []
    for s in prelude: r.get(s, err)
    reached, release = threading.Event(), threading.Event()
    r.hold = ("A", reached, release)
    a = threading.Thread(target=r.get, args=(first, err), name="A"); a.start(); assert reached.wait(5)
    b = threading.Thread(target=r.get, args=(second, err), name="B"); b.start()
    b.join(0.5 if repair else 5); blocked = b.is_alive()
    release.set(); a.join(5); b.join(5); assert not a.is_alive() and not b.is_alive()
    return {**r.read(), "second_waited_for_first": blocked, "refusals": err}

out = {}
out["P1_stale_count_current"]   = interleave(False, "reddit", "hackernews")
out["P1_stale_count_repaired"]  = interleave(True,  "reddit", "hackernews")
out["P2_stale_VIOLATION_current"]  = interleave(False, "hackernews", "x", prelude=["reddit"])
out["P2_stale_VIOLATION_repaired"] = interleave(True,  "hackernews", "x", prelude=["reddit"])
# P3 serial control: a violation with no interleaving must be rejected
r = Rig(False); e = []; [r.get(s, e) for s in ("reddit", "hackernews", "x")]; out["P3_serial_violation_control"] = r.read()
# P4 kill window: stage_start written, process killed before the policy snapshot (no ordering involved)
for rep in (False, True):
    r = Rig(rep); e = []; r.get("hackernews", e)
    r.tel.emit("stage_start", stage="retrieval", source="reddit", call_id="1:2")
    out[f"P4_kill_between_start_and_snapshot_{'repaired' if rep else 'current'}"] = r.read()
# P5 sticky-violation reader rule alone (no publisher repair) on the P2 sequence
seq = out["P2_stale_VIOLATION_current"]["snapshots"]
out["P5_reader_sticky_rule_would_reject_P2"] = any(v != 0 for _, _, v in seq)
out["P5_sticky_rule_on_clean_sequence_rejects"] = any(v != 0 for _, _, v in out["P1_stale_count_repaired"]["snapshots"])
print(json.dumps(out, indent=1))

# ---- P6: violation recorded but process killed before ITS snapshot is written (no reordering; publisher repair cannot help)
def starts_rule(tf, requested):
    d = prov._read_telemetry(tf)
    return any(e["event"] == "stage_start" and e.get("stage") == "retrieval" and e.get("source") not in requested for e in d["events"])
p6 = {}
for rep in (False, True):
    r = Rig(rep); e = []; [r.get(s, e) for s in ("reddit", "hackernews")]
    r.tel.emit("stage_start", stage="retrieval", source="x", call_id="9:9")  # real wrapper writes this BEFORE policy.retrieve refuses
    r.policy.violations += 1                                                # counted; killed before publish
    p6["repaired" if rep else "current"] = {"status": r.read()["status"], "items": r.read()["items"],
        "reader_rule_nonrequested_start_rejects": starts_rule(r.tf, {"reddit", "hackernews"})}
print(json.dumps({"P6_violation_then_kill_before_snapshot": p6}, indent=1))
