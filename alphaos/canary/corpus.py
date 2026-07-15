"""CANARY: the frozen golden corpus -- git-committed JSON fixtures + a
MANIFEST (sha256 per file). Deliberately the SAME on-disk shape as EVAL-1's
``data/eval/`` corpus (the spec explicitly says the two "share corpus
machinery") -- only the directory differs. Each packet fixture stores enough
to reconstruct a ``CandidatePacket`` for replay (packet_id/candidate_id/
interest_rank + the full whitelisted packet_json fields) plus provenance.

Unlike EVAL-1, CANARY has no ``ground_truth_label`` (it measures whether the
model's IDENTITY/behavior changed under us, not whether a label is
"correct") -- selection here prefers TASK-R-relabelled packets (the cleanest
post-PR9.1 evidence, already hand-verified once) plus a spread across
symbols, per the spec's own guidance.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Optional

DEFAULT_CORPUS_DIR = "data/canary"

# Same defense-in-depth as EVAL-1's corpus.py (alphaos/eval/corpus.py) --
# packet_id is always internally generated (alphaos.util.ids.new_id) on every
# wired call site today, but this module's output is operator-hand-editable
# and its input could in principle later feed a write path this module
# doesn't control, so validate before it ever becomes part of a filesystem
# path.
_PACKET_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")

# Same rationale as EVAL-1: PR9.1's prompt-leak fix merged 2026-07-06
# 14:43:48 SGT (commit b70ff2e); packets before this may carry a leaked
# prompt. Seeding strictly after this instant keeps the corpus clean by
# construction.
CLEAN_SINCE_UTC = "2026-07-06T14:45:00+00:00"

DEFAULT_SEED_LIMIT = 20


class CorpusTamperedError(Exception):
    """Raised by ``load_corpus`` when a fixture file's content no longer
    matches its own frozen MANIFEST sha256 -- the ONE deliberate exception
    to this module's otherwise-never-raises posture. Per spec: this must be
    a loud, fuse-eligible job failure (a hand-tampered or corrupted golden
    corpus is qualitatively different from "no corpus yet" -- it means the
    thing CANARY is replaying is no longer the thing it was frozen as, which
    could mask or fabricate a drift verdict), so it is intentionally left
    to propagate uncaught through run_canary() and up to JobRunner.run_job's
    own exception handler (which marks job_runs 'failed' and pages)."""


def _list_packet_files(corpus_dir: str) -> list:
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
    """Returns ``(manifest_dict, [packet_fixture_dict, ...])``, or
    ``(None, [])`` if the corpus has never been built yet -- an expected,
    honest empty state (an operator hasn't run ``canary_corpus_build`` and
    reviewed the selection yet), never an error. A `canary run` against an
    empty corpus is a safe no-op (see canary/run.py).

    Raises ``CorpusTamperedError`` if any fixture's on-disk content no
    longer matches the sha256 recorded for it in MANIFEST.json at write
    time (spec's own "corpus tamper (sha mismatch) -> loud job failure,
    fuse-eligible" requirement) -- a frozen golden corpus is meant to be
    exactly that, frozen; any divergence from its own manifest means either
    tampering or disk corruption, either way not safe to silently replay."""
    corpus_dir = corpus_dir or DEFAULT_CORPUS_DIR
    files = _list_packet_files(corpus_dir)
    if not files:
        return None, []

    manifest = _read_manifest(corpus_dir)
    expected_sha_by_file = {e["file"]: e["sha256"] for e in (manifest or {}).get("packets", [])}

    packets = []
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
        packets.append(json.loads(content))
    if mismatches:
        raise CorpusTamperedError(
            f"{len(mismatches)} corpus fixture(s) no longer match their frozen MANIFEST sha256 "
            f"in {corpus_dir!r}: " + "; ".join(mismatches)
        )
    return manifest, packets


def write_corpus(corpus_dir: str, new_packets: list, as_of_date: str) -> tuple:
    """Writes any NEW packet fixture files -- never overwrites an existing
    packet_id's file (corpus growth is additive; an operator who wants to
    REPLACE a packet's frozen content deletes the file themselves) -- then
    regenerates MANIFEST.json fresh from whatever's actually on disk, so the
    manifest can never silently drift from the real file listing. Returns
    ``(manifest_dict, [filenames_actually_written])``.

    Raises ``ValueError`` on a malformed ``packet_id`` (every wired caller
    feeds this internally generated, always-well-formed ids -- a malformed
    one means an actual upstream invariant was violated, worth failing loud
    on a manually invoked CLI command)."""
    os.makedirs(corpus_dir, exist_ok=True)
    written = []
    for packet in new_packets:
        packet_id = packet["packet_id"]
        if not _PACKET_ID_RE.match(packet_id):
            raise ValueError(
                f"refusing to write a corpus fixture with a malformed packet_id {packet_id!r} "
                "(must be alphanumeric/underscore only)"
            )
        filename = f"{packet_id}.json"
        path = os.path.join(corpus_dir, filename)
        if os.path.exists(path):
            continue
        with open(path, "w", encoding="utf-8") as f:
            json.dump(packet, f, indent=2, sort_keys=True)
        written.append(filename)

    existing = _read_manifest(corpus_dir)
    version = existing.get("version", 0) + 1 if (existing and written) else (
        existing.get("version", 1) if existing else 1
    )
    entries = []
    for filename in _list_packet_files(corpus_dir):
        with open(os.path.join(corpus_dir, filename), "rb") as f:
            content = f.read()
        entries.append({"file": filename, "sha256": hashlib.sha256(content).hexdigest()})
    manifest = {"version": version, "as_of_date": as_of_date, "packets": entries}
    with open(_manifest_path(corpus_dir), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return manifest, written


def select_seed_packets(journal, limit: int = DEFAULT_SEED_LIMIT, include_shadow: bool = False) -> list:
    """Selects up to ``limit`` real, clean (post-PR9.1), already-labelled
    candidate_packets rows for an operator to review before committing to
    ``data/canary/``. Packets with a TASK-R relabel (``candidate_labels.
    relabel_of IS NOT NULL``) sort first -- already hand-verified once, per
    the spec's own "prefer TASK-R's relabelled seven" guidance -- then a
    spread across distinct symbols, then the next-oldest clean rows filling
    any remaining slots. This function only selects; it never adjudicates or
    writes anything.

    EXP-1 mechanism 9(f)/12: defaults to EXCLUDING shadow-tier packets (LEFT
    JOINed via candidate_id -> candidates.shadow_tier) -- CANARY's golden
    corpus stays megacap-weighted for now (small/mids are harder to
    adjudicate confidently); a future dedicated shadow slice is an explicit
    corpus_version bump the spec defers, not a side effect of this default."""
    shadow_clause = "" if include_shadow else "AND COALESCE(c.shadow_tier, 0) = 0 "
    rows = journal.query(
        "SELECT cp.packet_id, cp.candidate_id, cp.interest_rank, cp.packet_json, cp.symbol, "
        "cp.created_at_utc, cl.primary_label, cl.label_decision, cl.relabel_of "
        "FROM candidate_packets cp JOIN candidate_labels cl ON cl.packet_id = cp.packet_id "
        "LEFT JOIN candidates c ON c.candidate_id = cp.candidate_id "
        f"WHERE cl.is_mock = 0 AND cp.created_at_utc >= ? {shadow_clause}"
        # Same most-recent-label-per-packet pin as EVAL-1's select_seed_packets
        # (alphaos/eval/corpus.py) -- candidate_labels has no uniqueness
        # constraint on packet_id, so pin to the highest id per packet rather
        # than relying on an ungoverned GROUP BY's arbitrary-row semantics.
        "AND cl.id = (SELECT MAX(cl2.id) FROM candidate_labels cl2 "
        "WHERE cl2.packet_id = cp.packet_id AND cl2.is_mock = 0) "
        "ORDER BY (cl.relabel_of IS NOT NULL) DESC, cp.created_at_utc ASC",
        (CLEAN_SINCE_UTC,),
    )
    seen_symbols: set = set()
    spread: list = []
    rest: list = []
    for row in rows:
        (rest if row["symbol"] in seen_symbols else spread).append(row)
        seen_symbols.add(row["symbol"])
    selected = (spread + rest)[:limit]

    fixtures = []
    for row in selected:
        raw_packet_json = row["packet_json"]
        packet_json = (
            json.loads(raw_packet_json) if isinstance(raw_packet_json, str) else (raw_packet_json or {})
        )
        fixtures.append({
            "packet_id": row["packet_id"],
            "candidate_id": row["candidate_id"],
            "interest_rank": row["interest_rank"],
            **packet_json,
            "provenance": {
                "seeded_from_created_at_utc": row["created_at_utc"],
                "historical_primary_label": row["primary_label"],
                "historical_label_decision": row["label_decision"],
                "is_task_r_relabel": row["relabel_of"] is not None,
            },
        })
    return fixtures
