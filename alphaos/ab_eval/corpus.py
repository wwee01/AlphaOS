"""AB-EVAL-1: the frozen A/B replay corpus -- git-committed JSON fixtures +
a MANIFEST (sha256 per file), same freeze discipline as
``alphaos/canary/corpus.py``.

Unlike CANARY (which replays ``candidate_packets`` through the labeller),
this corpus replays ``openai_evaluations`` rows -- the real market snapshot
PLUS the candidate evidence row at scan time -- through the PRIMARY
evaluator (``OpenAIClient``). Each fixture freezes BOTH the market snapshot
AND a WHITELISTED slice of the joined ``candidates`` row to disk at freeze
time (not just a DB reference): the live ``candidates`` table keeps
mutating after scan time (status/decision/card-assignment columns), so a
manifest that only recorded ``eval_id`` + a hash of the CURRENT row would
silently change what "the same corpus" means every time something else in
the codebase updates that row -- and freezing the FULL current row would
leak the pipeline's own downstream verdicts into the replay prompt (see
``CANDIDATE_CREATION_FIELDS`` below). Freezing the whitelisted content
once, like CANARY's own JSON fixtures, means a re-run replays the
identical inputs it always has, or fails loudly on tamper -- never a
silent drift.
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

# Audit HIGH (2026-07-20): the candidate dict frozen into a fixture is
# serialized into the REAL no-news prompt on a live replay
# (prompt_templates._public keeps every non-underscore key), so freezing
# the whole current `candidates` DB row would feed both models the
# pipeline's own downstream verdicts (status transitions, reject/watch
# reasons, labels, polarity, card-assignment state...) -- biasing the raw
# verdicts this harness exists to measure. Same failure class as the
# logged 2026-07-06 `_public` prompt-leak incident, invisible to mock-only
# tests because mock never builds a prompt. The whitelist below is exactly
# the SCANNER-CREATION column set -- what the in-memory ScanContext.row
# contained when `evaluate()` originally ran -- kept in lockstep with
# `candidate_scanner.py`'s own creation insert by an AST test
# (tests/test_ab_eval.py::test_candidate_whitelist_matches_scanner_creation_insert),
# never by hand-maintenance alone.
CANDIDATE_CREATION_FIELDS = (
    "candidate_id", "scan_id", "scan_batch_id", "symbol", "direction",
    "strategy", "momentum_score", "rel_strength", "unusual_volume",
    "trend_quality", "liquidity_ok", "spread_ok", "news_status",
    "price_snapshot_id", "status", "asset_type", "playbook_name",
    "setup_classification", "card_id", "card_version", "status_reason",
    "price_at_scan", "volume_at_scan", "interest_score",
    "shortlist_reason", "notes_json", "lineage_id", "shadow_tier",
    "instrument_version",
)

# Shadow-tier-only creation fields (the scanner's own `cand.update({...})`
# extension, applied before the same insert) -- included in a fixture only
# when the row is shadow_tier=1, mirroring the creation-time shape exactly
# (a core-tier row never had these keys in memory).
CANDIDATE_CREATION_SHADOW_FIELDS = (
    "bid_size", "ask_size", "quote_age_seconds", "spread_pct_mid",
    "adv_20d_dollar", "volume_today_pct_of_adv", "scan_window", "data_feed",
    "crossed_or_locked_quote", "core_gate_verdict",
    "liquidity_instrumentation_version", "interest_score_version",
)

# Two in-memory additions that were ALSO present on ScanContext.row when
# `evaluate()` originally ran, though not part of the insert dict literal:
# the scanner stamps `cand["last_price"]` immediately after its insert
# (candidate_scanner.py), and the orchestrator assigns `interest_rank`
# before evaluation (orchestrator._rank...). Neither is a downstream
# pipeline outcome; both were genuine prompt inputs.
CANDIDATE_IN_MEMORY_EXTRA_FIELDS = ("last_price", "interest_rank")

# Creation columns whose DB VALUES mutate after evaluation (the status
# machine moves detected -> watch/proposed/rejected once the pipeline
# decides) -- the key itself belongs in the fixture (it was in the
# original prompt), but only pinned back to its creation constant, so a
# replay can never read the pipeline's own verdict out of it.
CANDIDATE_FIELD_NORMALIZATIONS = {"status": "detected", "status_reason": "detected"}


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


def _freeze_candidate(candidate_row: dict) -> dict:
    """Reduces a CURRENT ``candidates`` DB row to the whitelisted
    scanner-creation field set (+ the two documented in-memory extras),
    with post-evaluation-mutated fields pinned back to their creation
    constants -- reconstructing what ``ScanContext.row`` contained when
    ``evaluate()`` originally ran, and structurally excluding every
    downstream pipeline-outcome column from ever reaching a replay
    prompt. ``*_json`` columns are parsed back to objects (the in-memory
    row held dicts, not JSON strings)."""
    fields = list(CANDIDATE_CREATION_FIELDS) + list(CANDIDATE_IN_MEMORY_EXTRA_FIELDS)
    if candidate_row.get("shadow_tier"):
        fields += list(CANDIDATE_CREATION_SHADOW_FIELDS)
    candidate = {}
    for key in fields:
        value = candidate_row.get(key)
        if key in CANDIDATE_FIELD_NORMALIZATIONS:
            value = CANDIDATE_FIELD_NORMALIZATIONS[key]
        elif key.endswith("_json") and isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass  # keep the raw string -- a fixture must freeze, not crash
        candidate[key] = value
    return candidate


def _row_to_fixture(journal, row: dict) -> dict:
    """Freezes ONE ``openai_evaluations`` row into a corpus fixture: the
    parsed market snapshot + the WHITELISTED slice of the joined
    ``candidates`` row (see ``_freeze_candidate`` -- never the full
    current row, which carries pipeline outcomes a replay prompt must
    never see)."""
    candidate_row = journal.one(
        "SELECT * FROM candidates WHERE candidate_id = ?", (row["candidate_id"],),
    ) or {}
    candidate = _freeze_candidate(candidate_row)
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
        "AND substr(created_at_utc, 1, 10) IN (?, ?) ORDER BY created_at_utc ASC, eval_id ASC",
        KILL_ZONE_DATES,
    )
    kill_zone_ids = {r["eval_id"] for r in kill_zone_rows}
    remaining = max(0, total - len(kill_zone_rows))

    later_rows = []
    if remaining:
        candidates = journal.query(
            "SELECT * FROM openai_evaluations WHERE is_mock = 0 AND snapshot_json IS NOT NULL "
            "AND substr(created_at_utc, 1, 10) NOT IN (?, ?) ORDER BY created_at_utc ASC, eval_id ASC",
            KILL_ZONE_DATES,
        )
        candidates = [r for r in candidates if r["eval_id"] not in kill_zone_ids]
        later_rows = _stratified_sample(candidates, remaining)

    return [_row_to_fixture(journal, row) for row in (kill_zone_rows + later_rows)]
