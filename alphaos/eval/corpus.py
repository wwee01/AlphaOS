"""EVAL-1: the frozen golden corpus -- git-committed JSON fixtures + a
MANIFEST (sha256 per file), mirroring CANARY's own spec'd ``data/canary/``
layout (the spec explicitly says the two "share corpus machinery"). Each
packet fixture stores enough to reconstruct a ``CandidatePacket`` for replay
(packet_id/candidate_id/interest_rank + the full whitelisted packet_json
fields) plus provenance (what the system historically decided) and an
operator-editable ``ground_truth_label`` -- starts ``None`` for every
packet the builder selects, and stays that way until a human fills it in
by hand. This module never fabricates a ground-truth label.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

DEFAULT_CORPUS_DIR = "data/eval"

# PR9.1's prompt-leak fix merged 2026-07-06 14:43:48 SGT (commit b70ff2e);
# candidate_packets/candidate_labels rows before this may carry a leaked
# prompt. Seeding strictly after this instant keeps the golden corpus clean
# by construction, per the spec's own "cleanest post-PR9.1 week" guidance.
CLEAN_SINCE_UTC = "2026-07-06T14:45:00+00:00"

DEFAULT_SEED_LIMIT = 30


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
    honest empty state (an operator hasn't run ``eval_corpus_build`` and/or
    adjudicated anything), never an error."""
    corpus_dir = corpus_dir or DEFAULT_CORPUS_DIR
    files = _list_packet_files(corpus_dir)
    if not files:
        return None, []
    packets = []
    for filename in files:
        with open(os.path.join(corpus_dir, filename), encoding="utf-8") as f:
            packets.append(json.load(f))
    return _read_manifest(corpus_dir), packets


def write_corpus(corpus_dir: str, new_packets: list, as_of_date: str) -> tuple:
    """Writes any NEW packet fixture files -- never overwrites an existing
    packet_id's file (corpus growth is additive; an operator who wants to
    REPLACE a packet's frozen content deletes the file themselves, a
    deliberate action, never an automatic one) -- then regenerates
    MANIFEST.json fresh from whatever's actually on disk, so the manifest
    can never silently drift from the real file listing. Returns
    ``(manifest_dict, [filenames_actually_written])``."""
    os.makedirs(corpus_dir, exist_ok=True)
    written = []
    for packet in new_packets:
        filename = f"{packet['packet_id']}.json"
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


def select_seed_packets(journal, limit: int = DEFAULT_SEED_LIMIT) -> list:
    """Selects up to ``limit`` real, clean (post-PR9.1), already-labelled
    candidate_packets rows -- a spread across distinct symbols first, then
    filling any remaining slots with the next-oldest clean rows -- and
    shapes each into a corpus fixture dict (ready for ``write_corpus``).
    ``ground_truth_label`` is always ``None`` here; this function only ever
    selects real historical packets, it never adjudicates them."""
    rows = journal.query(
        "SELECT cp.packet_id, cp.candidate_id, cp.interest_rank, cp.packet_json, cp.symbol, "
        "cp.created_at_utc, cl.primary_label, cl.label_decision "
        "FROM candidate_packets cp JOIN candidate_labels cl ON cl.packet_id = cp.packet_id "
        "WHERE cl.is_mock = 0 AND cp.created_at_utc >= ? "
        "ORDER BY cp.created_at_utc ASC",
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
            },
            "ground_truth_label": None,
        })
    return fixtures


def ground_truth_coverage(packets: list) -> dict:
    """How many corpus packets have an operator-adjudicated ground_truth_label
    yet -- surfaced honestly rather than silently treating "0 adjudicated" as
    "0% agreement"."""
    total = len(packets)
    labeled = sum(1 for p in packets if p.get("ground_truth_label"))
    return {"total": total, "labeled": labeled}
