"""Claude audit probes for Codex 0a7cafe (scripts/register_explore1.py), 2026-11-18.

Run with the production venv, PYTHONPATH=<checkout at 0a7cafe>, against COPIES of the ledger only:
  PYTHONPATH=<worktree> python probes.py <worktree> <pristine_copy.db> <workdir>
Never points at data/alphaos.db.
"""
import hashlib, importlib.util, json, os, shutil, sqlite3, sys, threading, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

WT, PRISTINE, WD = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
spec = importlib.util.spec_from_file_location("reg", WT / "scripts/register_explore1.py")
reg = importlib.util.module_from_spec(spec); spec.loader.exec_module(reg)
PIN = hashlib.sha256((WT / "scripts/register_explore1.py").read_bytes()).hexdigest()
results = []

def fresh(name, wal=False):
    p = WD / f"{name}.db"
    for s in ("", "-wal", "-shm"):
        Path(str(p) + s).unlink(missing_ok=True)
    shutil.copy(PRISTINE, p)
    if wal:
        c = sqlite3.connect(p); c.execute("PRAGMA journal_mode=WAL"); c.close()
    return p

def state(p):
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    n = c.execute("select count(*) from preregistrations").fetchone()[0]
    seq = c.execute("select seq from sqlite_sequence where name='preregistrations'").fetchone()[0]
    h = hashlib.sha256(repr(c.execute("select * from preregistrations order by id").fetchall()).encode()).hexdigest()
    hp = hashlib.sha256(repr(c.execute("select * from hypothesis_proposals order by id").fetchall()).encode()).hexdigest()
    c.close(); return (n, seq, h, hp)

BASE = None
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail)); print(("PASS " if cond else "FAIL ") + name, detail)

def conn(p):
    c = sqlite3.connect(p, timeout=10); c.row_factory = sqlite3.Row; return c

def expect_refusal(fn):
    try:
        fn(); return None
    except BaseException as e:
        return f"{type(e).__name__}: {e}"

BASE = state(PRISTINE)
check("baseline 15 rows / seq 15", BASE[0] == 15 and BASE[1] == 15, str(BASE[:2]))

# P1 failure on the third insert -> nothing written
p = fresh("p1"); c = conn(p); orig = reg.register_hypothesis; calls = {"n": 0}
def boom(*a, **k):
    calls["n"] += 1
    if calls["n"] == 3: raise RuntimeError("injected after two inserts")
    return orig(*a, **k)
reg.register_hypothesis = boom
err = expect_refusal(lambda: reg.register_family(c)); reg.register_hypothesis = orig; c.close()
check("P1 third-insert failure rolls back whole family", err and calls["n"] == 3 and state(p) == BASE, err)

# P2 commit_guard failure -> rollback
p = fresh("p2"); c = conn(p)
def guard(): raise RuntimeError("target replaced")
err = expect_refusal(lambda: reg.register_family(c, commit_guard=guard)); c.close()
check("P2 commit-guard failure rolls back", err and state(p) == BASE, err)

# P3 deadline edges
real_now = reg.timeutils.now_utc
def at(dt):
    reg.timeutils.now_utc = lambda: dt
for label, dt, ok in (("20:59:59.999", datetime(2026,11,20,20,59,59,999000,tzinfo=timezone.utc), True),
                      ("21:00:00.000", datetime(2026,11,20,21,0,0,tzinfo=timezone.utc), False),
                      ("naive 12:00", datetime(2026,11,20,12,0,0), False)):
    p = fresh("p3"); c = conn(p); at(dt)
    err = expect_refusal(lambda: reg.register_family(c)); reg.timeutils.now_utc = real_now; c.close()
    s = state(p)
    check(f"P3 deadline {label} -> {'commit' if ok else 'refuse'}",
          (err is None and s[0] == 18) if ok else (err and s == BASE), err or str(s[:2]))

# P4 deadline crossed between BEGIN and COMMIT (third clock read is late)
p = fresh("p4"); c = conn(p); n = {"i": 0}
def clock():
    n["i"] += 1
    return datetime(2026,11,20,21,0,1,tzinfo=timezone.utc) if n["i"] >= 3 else datetime(2026,11,20,20,59,0,tzinfo=timezone.utc)
reg.timeutils.now_utc = clock
err = expect_refusal(lambda: reg.register_family(c)); reg.timeutils.now_utc = real_now; c.close()
check("P4 deadline crossed inside transaction -> rollback", err and state(p) == BASE, err)

# P5 WAL ledger, another writer holds the write lock -> busy, refused, nothing written, receipt says uncertain
p = fresh("p5", wal=True); holder = sqlite3.connect(p, isolation_level=None); holder.execute("BEGIN IMMEDIATE")
rc = WD / "p5-receipt.json"; rc.unlink(missing_ok=True); t0 = time.time()
err = expect_refusal(lambda: reg.main(["--db", str(p), "--mode", "apply", "--receipt", str(rc), "--expected-script-sha256", PIN]))
dt = time.time() - t0; holder.execute("ROLLBACK"); holder.close()
rec = json.loads(rc.read_text())
check("P5 contended WAL write lock -> refused after timeout, nothing written",
      err and "locked" in err and state(p) == BASE and rec["status"] == "refused_or_commit_uncertain" and 9 <= dt <= 15, f"{err} after {dt:.1f}s")

# P6 WAL ledger, concurrent reader + a writer on another table committing just before -> apply succeeds; other table write preserved
p = fresh("p6", wal=True); w = sqlite3.connect(p); w.execute("CREATE TABLE IF NOT EXISTS _probe(x)"); w.execute("INSERT INTO _probe VALUES (1)"); w.commit()
reader = sqlite3.connect(f"file:{p}?mode=ro", uri=True); reader.execute("BEGIN"); reader.execute("select count(*) from preregistrations").fetchone()
rc = WD / "p6-receipt.json"; rc.unlink(missing_ok=True)
err = expect_refusal(lambda: reg.main(["--db", str(p), "--mode", "apply", "--receipt", str(rc), "--expected-script-sha256", PIN]))
reader.execute("ROLLBACK"); reader.close(); w.close()
s = state(p); rec = json.loads(rc.read_text())
check("P6 WAL apply beside an open reader commits 3 rows; durable readback in receipt",
      err is None and s[0] == 18 and s[1] == 18 and rec["status"] == "committed" and len(rec["committed_readback"]) == 3, err or "")

# P7 any H-EXP1 mention (case-insensitive) in an existing proposal row -> refuse
p = fresh("p7"); w = sqlite3.connect(p)
cols = [r[1] for r in w.execute("PRAGMA table_info(hypothesis_proposals)")]
row = dict(zip(cols, w.execute("select * from hypothesis_proposals limit 1").fetchone()))
tcol = next(k for k, v in row.items() if isinstance(v, str) and k not in ("hypothesis_id", "prereg_id"))
w.execute(f"UPDATE hypothesis_proposals SET {tcol} = {tcol} || ' see h-exp1-a' WHERE id = ?", (row["id"],)); w.commit(); w.close()
base7 = state(p); c = conn(p); err = expect_refusal(lambda: reg.register_family(c)); c.close()
check(f"P7 'h-exp1' in proposals.{tcol} -> refuse", err and "existing H-EXP1" in err and state(p) == base7, err)

# P8 trigger on preregistrations -> refuse
p = fresh("p8"); w = sqlite3.connect(p); w.execute("CREATE TRIGGER t AFTER INSERT ON preregistrations BEGIN SELECT 1; END"); w.commit(); w.close()
base8 = state(p); c = conn(p); err = expect_refusal(lambda: reg.register_family(c)); c.close()
check("P8 registry trigger -> refuse", err and "trigger" in err and state(p) == base8, err)

# P9 symlink and hard-link targets refused by main()
p = fresh("p9"); sl = WD / "p9-link.db"; hl = WD / "p9-hard.db"
for x in (sl, hl): x.unlink(missing_ok=True)
os.symlink(p, sl); os.link(p, hl)
for label, target in (("symlink", sl), ("hardlink", hl)):
    rc = WD / f"p9-{label}.json"; rc.unlink(missing_ok=True)
    err = expect_refusal(lambda: reg.main(["--db", str(target), "--mode", "apply", "--receipt", str(rc), "--expected-script-sha256", PIN]))
    check(f"P9 {label} ledger -> refuse before connecting", err and "nonsymlink" in err and state(p) == BASE and not rc.exists(), err)
hl.unlink(); sl.unlink()

# P10 existing receipt -> refuse before connecting
p = fresh("p10"); rc = WD / "p10.json"; rc.write_text("old")
err = expect_refusal(lambda: reg.main(["--db", str(p), "--mode", "apply", "--receipt", str(rc), "--expected-script-sha256", PIN]))
check("P10 pre-existing receipt -> refuse, nothing written", err and "FileExists" in err and state(p) == BASE and rc.read_text() == "old", err)

# P11 scheduled path on the applied copy: seed_all + resolve_due_hypotheses at 2027-01-17 and 2027-03-01 leave H-EXP1 untouched
p = fresh("p11"); c = conn(p); reg.register_family(c); c.close()
def exp1(pth):
    k = sqlite3.connect(f"file:{pth}?mode=ro", uri=True)
    r = k.execute("select * from preregistrations where id > 15 order by id").fetchall()
    links = k.execute("select count(*) from hypothesis_proposals where prereg_id in (select prereg_id from preregistrations where id > 15)").fetchone()[0]
    k.close(); return r, links
before, _ = exp1(p)
from alphaos.journal.journal_store import JournalStore
from alphaos.hypotheses import resolve_due_hypotheses, seed_all
j = JournalStore(str(p))
summaries = []
for when in (datetime(2027,1,17,2,tzinfo=timezone.utc), datetime(2027,3,1,2,tzinfo=timezone.utc)):
    try:
        seed_all(j)
    except TypeError:
        seed_all(j, now=when)
    summaries.append(resolve_due_hypotheses(j, now=when))
j.conn.close()
after, links = exp1(p)
ids = {r[1] for r in before}
mentioned = [s for s in summaries if any(x in json.dumps(s, default=str) for x in ids) or "H-EXP1" in json.dumps(s, default=str)]
check("P11 scheduled seed+resolve after analysis floor leaves H-EXP1 rows byte-identical, unlinked, unmentioned",
      before == after and links == 0 and not mentioned and all(r[ -0] is not None for r in after),
      f"links={links} evaluated={[s.get('evaluated') for s in summaries]}")

# P12 (documents the known hazard, not a defect of 0a7cafe): direct legacy call spends the look
from alphaos.stats.preregistration import evaluate_hypothesis
j = JournalStore(str(p)); pid = before[0][1]
err = expect_refusal(lambda: evaluate_hypothesis(j, pid, [], "v"))
row = j.one("select evaluated_at_utc, evidence_status from preregistrations where prereg_id=?", (pid,)); j.conn.close()
check("P12 KNOWN HAZARD: direct legacy evaluate_hypothesis is NOT refused today (look spent, status insufficient_data)",
      err is None and row["evaluated_at_utc"] and row["evidence_status"] == "insufficient_data", f"{row}")

print(json.dumps({"passed": sum(r[1] for r in results), "total": len(results)}))
sys.exit(0 if all(r[1] for r in results) else 1)
