"""AB-EVAL-1: the frozen A/B replay corpus -- git-committed JSON fixtures +
a MANIFEST (sha256 per file), same freeze discipline as
``alphaos/canary/corpus.py``.

Unlike CANARY (which replays ``candidate_packets`` through the labeller),
this corpus replays ``openai_evaluations`` rows -- the real market snapshot
PLUS the candidate evidence row at scan time -- through the PRIMARY
evaluator (``OpenAIClient``). Each fixture freezes BOTH the market snapshot
AND the joined ``candidates`` row content to disk at freeze time (not just
a DB reference): the live ``candidates`` table keeps mutating after scan
time (status/decision/card-assignment columns), so a manifest that only
recorded ``eval_id`` + a hash of the CURRENT row would silently change what
"the same corpus" means every time something else in the codebase updates
that row. Freezing full content once, like CANARY's own JSON fixtures,
means a re-run replays the identical inputs it always has, or fails loudly
on tamper -- never a silent drift.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict, deque
from typing import Optional

DEFAULT_CORPUS_DIR = "data/ab_eval"

# Same defense-in-depth as CANARY/EVAL-1's own corpus.py: eval_id is always
# internally generated (alphaos.util.ids.new_id) on every wired call site
# today, but this module's output is operator-hand-editable and feeds a
# filesystem path, so validate before it ever becomes part of one.
_EVAL_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")

# EVAL-1 addendum stamped openai_evaluations.snapshot_json starting
# 2026-07-09 -- rows before this have no snapshot and are not replayable
# (spec's own "Rows before 07-09 have no snapshots and are NOT replayable").
KILL_ZONE_DATES = ("2026-07-09", "2026-07-10")

DEFAULT_TOTAL = 60


class CorpusTamperedError(Exception):
    """Raised by ``load_corpus`` when a fixture file's content no longer
    matches its own frozen MANIFEST sha256 -- the ONE deliberate exception
    to this module's otherwise-never-raises posture, identical contract to
    CANARY's own ``CorpusTamperedError`` (alphaos/canary/corpus.py): a
    hand-tampered or corrupted frozen corpus is qualitatively different
    from "no corpus yet" and must never be silently replayed, so this is
    intentionally left to propagate uncaught through ``run_ab_eval()``."""


def _list_fixture_files(corpus_dir: str) -> list:
    if not os.path.isdir(corpus_dir):
        return []
    return sorted(
        f for f in os.listdir(corpus_dir) if f.endswith(".json") and f != "MANIFEST.json"
    )


def _manifest_path(corpus_dir: str) -> str:
    return os.path.join(corpus_dir, "MANIFEST.json")


def _read_manifest(corpus_dir: str) -> Optional[dict]:
    path = _manifest_path(corpus_dir)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_corpus(corpus_dir: Optional[str] = None) -> tuple:
    """Returns ``(manifest_dict, [fixture_dict, ...])``, or ``(None, [])``
    if the corpus has never been built yet -- an expected, honest empty
    state (an operator hasn't run ``ab_eval_corpus_build`` yet), never an
    error. An ``ab_eval_run`` against an empty corpus is a safe no-op (see
    ``alphaos/ab_eval/run.py``).

    Raises ``CorpusTamperedError`` if any fixture's on-disk content no
    longer matches the sha256 recorded for it in MANIFEST.json at write
    time."""
    corpus_dir = corpus_dir or DEFAULT_CORPUS_DIR
    files = _list_fixture_files(corpus_dir)
    if not files:
        return None, []

    manifest = _read_manifest(corpus_dir)
    expected_sha_by_file = {
        e["file"]: e["sha256"] for e in (manifest or {}).get("evaluations", [])
    }

    fixtures = []
    mismatches = []
    for filename in files:
        full_path = os.path.join(corpus_dir, filename)
        with open(full_path, "rb") as f:
            content = f.read()
        expected_sha = expected_sha_by_file.get(filename)
        if expected_sha is not None:
            actual_sha = hashlib.sha256(content).hexdigest()
            if actual_sha != expected_sha:
                mismatches.append(f"{filename}: expected sha256={expected_sha}, got {actual_sha}")
        fixtures.append(json.loads(content))
    if mismatches:
        raise CorpusTamperedError(
            f"{len(mismatches)} AB-EVAL-1 corpus fixture(s) no longer match their frozen "
            f"MANIFEST sha256 in {corpus_dir!r}: " + "; ".join(mismatches)
        )
    return manifest, fixtures


def write_corpus(corpus_dir: str, new_fixtures: list, as_of_date: str) -> tuple:
    """Writes any NEW fixture files -- never overwrites an existing
    ``eval_id``'s file (corpus growth is additive; an operator who wants to
    REPLACE a fixture's frozen content deletes the file themselves) -- then
    regenerates MANIFEST.json fresh from whatever's actually on disk.
    Returns ``(manifest_dict, [filenames_actually_written])``. Identical
    contract to ``alphaos/canary/corpus.py``'s ``write_corpus``, keyed by
    ``eval_id`` instead of ``packet_id``.

    Raises ``ValueError`` on a malformed ``eval_id``."""
    os.makedirs(corpus_dir, exist_ok=True)
    written = []
    for fixture in new_fixtures:
        eval_id = fixture["eval_id"]
        if not _EVAL_ID_RE.match(eval_id):
            raise ValueError(
                f"refusing to write an AB-EVAL-1 corpus fixture with a malformed eval_id "
                f"{eval_id!r} (must be alphanumeric/underscore only)"
            )
        filename = f"{eval_id}.json"
        path = os.path.join(corpus_dir, filename)
        if os.path.exists(path):
            continue
        with open(path, "w", encoding="utf-8") as f:
            json.dump(fixture, f, indent=2, sort_keys=True)
        written.append(filename)

    existing = _read_manifest(corpus_dir)
    version = existing.get("version", 0) + 1 if (existing and written) else (
        existing.get("version", 1) if existing else 1
    )
    entries = []
    for filename in _list_fixture_files(corpus_dir):
        with open(os.path.join(corpus_dir, filename), "rb") as f:
            content = f.read()
        entries.append({"file": filename, "sha256": hashlib.sha256(content).hexdigest()})
    manifest = {"version": version, "as_of_date": as_of_date, "evaluations": entries}
    with open(_manifest_path(corpus_dir), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return manifest, written


def _stratified_sample(rows: list, n: int) -> list:
    """Round-robin sample across (decision, date) buckets, up to ``n`` rows
    total -- the spec's own "stratified sample ... by (decision, date)"
    selection rule. Deterministic given the input row order (callers pass
    rows already ordered by created_at_utc ASC within each bucket); no RNG."""
    if n <= 0:
        return []
    buckets: dict = defaultdict(deque)
    for row in rows:
        key = (row["decision"], (row["created_at_utc"] or "")[:10])
        buckets[key].append(row)
    keys = sorted(buckets.keys())
    selected: list = []
    while len(selected) < n and any(buckets[k] for k in keys):
        for k in keys:
            if len(selected) >= n:
                break
            if buckets[k]:
                selected.append(buckets[k].popleft())
    return selected


def _row_to_fixture(journal, row: dict) -> dict:
    """Freezes ONE ``openai_evaluations`` row into a corpus fixture: the
    parsed market snapshot + the joined ``candidates`` row (the same
    ``candidates``-table-shaped dict ``ScanContext.row`` always was --
    see ``alphaos/scanner/scan_context.py``'s own docstring -- which is
    exactly the ``candidate`` shape ``OpenAIClient.evaluate()`` expects)."""
    candidate_row = journal.one(
        "SELECT * FROM candidates WHERE candidate_id = ?", (row["candidate_id"],),
    ) or {}
    candidate = {k: v for k, v in candidate_row.items() if k != "id"}
    raw_snapshot = row.get("snapshot_json")
    snapshot = (
        json.loads(raw_snapshot) if isinstance(raw_snapshot, str) else (raw_snapshot or {})
    )
    return {
        "eval_id": row["eval_id"],
        "candidate_id": row["candidate_id"],
        "symbol": row["symbol"],
        "candidate": candidate,
        "snapshot": snapshot,
        "freshness_status": row.get("data_freshness_status") or "usable",
        "provenance": {
            "original_model": row.get("model"),
            "original_decision": row.get("decision"),
            "original_created_at_utc": row.get("created_at_utc"),
        },
    }


def select_default_corpus(journal, total: int = DEFAULT_TOTAL) -> list:
    """Spec's own default selection: ALL real (``is_mock=0``), snapshot-
    bearing rows from the 2026-07-09/07-10 kill-zone (the 34 mini-era rows
    where the floor did 100% of the killing), plus a stratified sample of
    later rows by (decision, date), filling to ``total`` (operator-tunable).
    Returns fixture dicts, NOT yet frozen to disk -- pass to
    ``write_corpus()`` for that; an operator reviews the selection first
    (same "never auto-committed" law as CANARY's own corpus build)."""
    kill_zone_rows = journal.query(
        "SELECT * FROM openai_evaluations WHERE is_mock = 0 AND snapshot_json IS NOT NULL "
        "AND substr(created_at_utc, 1, 10) IN (?, ?) ORDER BY created_at_utc ASC",
        KILL_ZONE_DATES,
    )
    kill_zone_ids = {r["eval_id"] for r in kill_zone_rows}
    remaining = max(0, total - len(kill_zone_rows))

    later_rows = []
    if remaining:
        candidates = journal.query(
            "SELECT * FROM openai_evaluations WHERE is_mock = 0 AND snapshot_json IS NOT NULL "
            "AND substr(created_at_utc, 1, 10) NOT IN (?, ?) ORDER BY created_at_utc ASC",
            KILL_ZONE_DATES,
        )
        candidates = [r for r in candidates if r["eval_id"] not in kill_zone_ids]
        later_rows = _stratified_sample(candidates, remaining)

    return [_row_to_fixture(journal, row) for row in (kill_zone_rows + later_rows)]
