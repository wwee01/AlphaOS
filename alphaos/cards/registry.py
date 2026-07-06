"""PR10 Setup Cards v1: the versioned join key for the whole learning loop.

Cards are declarative YAML in this directory (``alphaos/cards/*.yaml``) --
reviewable, diffable, git-versioned -- PLUS a ``setup_cards`` DB registry
synced at orchestrator startup (idempotent upsert keyed by (card_id,
version)), so every ledger row can join without filesystem access. Registry
rows are append-only per version: a content change WITHOUT a version bump is
refused loudly at startup (Prime Directive 7 -- a silently mutated card is
exactly the failure mode that exists to prevent).

v1 ships with exactly ONE card (``DEFAULT_CARD_ID``) -- a faithful
transcription of the pre-card pipeline's existing behavior. This module
changes NO decision behavior; it makes existing behavior addressable. No
card-promotion machinery yet (PR13); every stamping call site just uses
``get_default_card()``.

Cards are read fresh from disk on every call -- a handful of tiny YAML files
read a few times per scan is not a hot path, and caching would only buy
test-isolation risk (a test rewriting a fixture file between two calls would
see stale content) for no real benefit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from alphaos import lineage
from alphaos.config.settings import SettingsError
from alphaos.lineage.hashing import stable_hash

CARDS_DIR = Path(__file__).parent
DEFAULT_CARD_ID = "catalyst_momentum_v1"

_REQUIRED_FIELDS = ("card_id", "version", "name", "state", "invalidation_rule")


def _validate_card(card: dict, source: str) -> None:
    if not isinstance(card, dict):
        raise SettingsError(f"Setup card {source} did not parse to a mapping.")
    missing = [f for f in _REQUIRED_FIELDS if not card.get(f)]
    if missing:
        raise SettingsError(f"Setup card {source} is missing required field(s): {missing}")
    if not isinstance(card["version"], int) or card["version"] < 1:
        raise SettingsError(f"Setup card {source} has an invalid version: {card.get('version')!r}")


def load_card_files(cards_dir: Optional[Path] = None) -> list[dict]:
    """Parse every ``*.yaml`` file in ``cards_dir`` (default: this package's
    own directory) into a card dict, validated against ``_REQUIRED_FIELDS``.
    A malformed card raises loudly -- a card silently failing to load would
    be just as dangerous as one mutated without a version bump."""
    directory = Path(cards_dir) if cards_dir is not None else CARDS_DIR
    cards = []
    for path in sorted(directory.glob("*.yaml")):
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        _validate_card(raw, path.name)
        cards.append(raw)
    return cards


def get_default_card(cards_dir: Optional[Path] = None) -> dict:
    """The v1 single active card (``DEFAULT_CARD_ID``). Every stamping call
    site uses this -- v1 has exactly one card, so "the card that produced
    this candidate/proposal" and "the default card" are the same thing."""
    for card in load_card_files(cards_dir):
        if card["card_id"] == DEFAULT_CARD_ID:
            return card
    raise SettingsError(
        f"Default setup card {DEFAULT_CARD_ID!r} not found in {cards_dir or CARDS_DIR}"
    )


def sync_registry(journal, settings, cards_dir: Optional[Path] = None) -> list[str]:
    """Idempotent upsert of every card file into the ``setup_cards`` DB
    registry. Same (card_id, version) with an unchanged content hash -> no-op.
    Same (card_id, version) with a DIFFERENT content hash -> raise
    SettingsError (refuse to start): a card's content changing without a
    version bump is the exact silent-mutation failure mode Prime Directive 7
    exists to prevent. Returns the "card_id:vN" strings newly inserted."""
    synced = []
    for card in load_card_files(cards_dir):
        card_id, version = card["card_id"], card["version"]
        content_hash = stable_hash(card)
        existing = journal.one(
            "SELECT content_hash FROM setup_cards WHERE card_id = ? AND version = ?",
            (card_id, version),
        )
        if existing is None:
            journal.insert("setup_cards", {
                "card_id": card_id,
                "version": version,
                "name": card.get("name"),
                "state": card.get("state"),
                "content_hash": content_hash,
                "content_json": card,
                "lineage_id": lineage.get_or_create_lineage_id(journal, settings),
            })
            synced.append(f"{card_id}:v{version}")
        elif existing["content_hash"] != content_hash:
            raise SettingsError(
                f"Setup card {card_id} v{version} content changed without a version "
                f"bump (stored hash {existing['content_hash']}, current hash "
                f"{content_hash}). Bump the version in the card's YAML file instead "
                "of editing it in place -- registry rows are append-only per version."
            )
        # else: identical content already registered -- idempotent no-op.
    return synced
