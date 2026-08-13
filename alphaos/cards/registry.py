"""PR10 Setup Cards v1: the versioned join key for the whole learning loop.

Cards are declarative YAML in this directory (``alphaos/cards/*.yaml``) --
reviewable, diffable, git-versioned -- PLUS a ``setup_cards`` DB registry
synced at orchestrator startup (idempotent upsert keyed by (card_id,
version)), so every ledger row can join without filesystem access. Registry
rows are append-only per version: a content change WITHOUT a version bump is
refused loudly at startup (Prime Directive 7 -- a silently mutated card is
exactly the failure mode that exists to prevent).

v1 shipped with exactly ONE card (``catalyst_momentum_v1``) -- a faithful
transcription of the pre-card pipeline's existing behavior, changing NO
decision behavior; it made existing behavior addressable. No card-promotion
machinery yet (PR13); every stamping call site just uses
``get_default_card()``.

INSTR-1 (2026-07-09) swapped ``DEFAULT_CARD_ID`` to ``catalyst_momentum_v2``
-- a real behavior change (ATR-scaled stops), the first since PR10 shipped.
``catalyst_momentum_v1``'s own file stays in this directory, unchanged
(append-only per Prime Directive 7): every pre-INSTR-1 candidate/proposal
row still joins to its real, original card, and this registry's own
content-hash check would refuse to start if v1's file were ever edited in
place instead of superseded by a new card_id.

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
from alphaos.config.settings import Settings, SettingsError
from alphaos.lineage.hashing import stable_hash

CARDS_DIR = Path(__file__).parent
# INSTR-1 (2026-07-09): superseded catalyst_momentum_v1 -- see module
# docstring. v1's file remains registered, unchanged, for historical rows.
DEFAULT_CARD_ID = "catalyst_momentum_v2"

_REQUIRED_FIELDS = ("card_id", "version", "name", "state", "invalidation_rule")


def _validate_card(card: dict, source: str) -> None:
    if not isinstance(card, dict):
        raise SettingsError(f"Setup card {source} did not parse to a mapping.")
    missing = [f for f in _REQUIRED_FIELDS if not card.get(f)]
    if missing:
        raise SettingsError(f"Setup card {source} is missing required field(s): {missing}")
    if not isinstance(card["version"], int) or card["version"] < 1:
        raise SettingsError(f"Setup card {source} has an invalid version: {card.get('version')!r}")


# HOLD-2 audit-fixup (MEDIUM-6, audit B / STATUS CORRECTION item 5): the
# extra checks a card must pass to be a valid ACTIVE_CARD_ID -- beyond mere
# id-membership. Bare id-membership accepted the shadow-only PER card
# (post_earnings_reaction, state=shadow, "no trading" by its own card
# YAML) as a valid live default -- one hand-edit away, during the cutover
# ceremony, from silently making a non-trading card the live default. It
# also accepted a card missing max_holding_days_default entirely, which
# then KeyError-crashes the mock evaluator path
# (OpenAIClient._mock_max_holding_days) while the live v4 path silently
# rejects every single PROPOSE (validate_max_holding_days_range() called
# with bound=None). Factored out here (not inlined in settings.py) so it
# is directly testable against a synthetic card dict, no filesystem
# needed.
ACTIVE_CARD_MAX_HOLDING_DAYS_CEILING = 30


def validate_card_as_active_default(card: dict) -> None:
    """Raises ``SettingsError`` unless ``card`` may legally be
    ``ACTIVE_CARD_ID``: ``state == "live_eligible"`` AND an integer
    ``max_holding_days_default`` in ``[1, ACTIVE_CARD_MAX_HOLDING_DAYS_
    CEILING]``. ``catalyst_momentum_v1`` (state live_eligible,
    max_holding_days_default 3) passes -- a deliberate rollback capability,
    not something this check exists to block."""
    card_id = card.get("card_id")
    if card.get("state") != "live_eligible":
        raise SettingsError(
            f"card {card_id!r} has state={card.get('state')!r}, not 'live_eligible' -- "
            "only a live-eligible card may be the live default (e.g. the shadow-only "
            "post_earnings_reaction card must never be set here)."
        )
    hold_days = card.get("max_holding_days_default")
    # bool is an int subclass in Python -- explicitly excluded so a stray
    # `max_holding_days_default: true` in a hand-edited card can't sneak
    # through as "1".
    if (
        not isinstance(hold_days, int) or isinstance(hold_days, bool)
        or not (1 <= hold_days <= ACTIVE_CARD_MAX_HOLDING_DAYS_CEILING)
    ):
        raise SettingsError(
            f"card {card_id!r}'s max_holding_days_default={hold_days!r} must be an integer "
            f"in [1, {ACTIVE_CARD_MAX_HOLDING_DAYS_CEILING}] -- a missing or malformed value "
            "would KeyError-crash the mock evaluator path and silently reject every v4 "
            "PROPOSE on the live path."
        )


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


def get_card_by_id(card_id: str, cards_dir: Optional[Path] = None) -> dict:
    """Load ONE card BY EXPLICIT ID, bypassing ``DEFAULT_CARD_ID``/
    ``settings.active_card_id`` entirely (HOLD-2). Used wherever a card's
    identity must stay PINNED regardless of the active default -- e.g.
    BASELINE's frozen v1 arms, which must never silently move when an
    operator swaps ``ACTIVE_CARD_ID`` (see ``alphaos/baseline/tracker.py``).
    Raises ``SettingsError`` if the id isn't found (never a silent
    fallback)."""
    for card in load_card_files(cards_dir):
        if card["card_id"] == card_id:
            return card
    raise SettingsError(f"Setup card {card_id!r} not found in {cards_dir or CARDS_DIR}")


def get_default_card(cards_dir: Optional[Path] = None, settings: Optional[Settings] = None) -> dict:
    """The single ACTIVE card. Every stamping call site uses this -- there is
    still no per-candidate card SELECTION (PR13), only ever one default at a
    time, so "the card that produced this candidate/proposal" and "the
    default card" are the same thing.

    HOLD-2 (audit-fixup, per the spec's own STATUS CORRECTION item 1):
    which card is "the default" is now an operator config axis
    (``settings.active_card_id``, validated at settings-load time against
    this same on-disk registry -- see ``alphaos/config/settings.py``).
    Every production call site that stamps a candidate/proposal now threads
    ``settings`` through to this function -- ``orchestrator.py``,
    ``scanner/candidate_scanner.py``, ``cards/selector.py`` (via
    ``build_selector_context``, itself reached from
    ``cards/activation.py``'s ``build_scan_card_activation``), and
    ``ai/openai_client.py``'s mock path. ``settings`` stays OPTIONAL only
    for callers OUTSIDE the live stamping path (tests, ad-hoc scripts): when
    omitted, this resolves ``DEFAULT_CARD_ID`` exactly as before HOLD-2. The
    module constant ``DEFAULT_CARD_ID`` remains the single-sourced default
    VALUE of the ``ACTIVE_CARD_ID`` setting itself (see settings.py) --
    never duplicated as a second literal.

    Because ``ACTIVE_CARD_ID`` accepts any ``live_eligible`` card id
    (including a deliberate operator rollback to an older one, e.g.
    ``catalyst_momentum_v1`` -- see settings.py's own validation), a
    "superseded" card is NO LONGER guaranteed to never be returned here
    again; supersession only means it stopped being the DEFAULT, not that
    it was removed or made unselectable."""
    card_id = settings.active_card_id if settings is not None else DEFAULT_CARD_ID
    return get_card_by_id(card_id, cards_dir)


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
