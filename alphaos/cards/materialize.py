"""PR13.5: the diff-to-version materialization ceremony.

Per a focused Fable5 consult (2026-07-10, following directly on from PR13
slice 2's own graduation/mutation distinction, see
``alphaos.cards.promotion``'s module docstring): PR13.5's original spec
text ("card_promote renders the promoted diff") rests on a
``proposed_diff_json`` artifact PR12 never built and nothing else
produces -- confirmed by direct inspection, that field exists only in the
pre-build spec text (docs/roadmap/alphaos-pr-implementation-specs.md),
never in ``hypothesis_proposals``, the seed constants, or any code path
anywhere in this package. The unbuildable part of the original spec was
decoration; the load-bearing part is the VERSIONING CEREMONY itself --
"only an operator-committed YAML version changes card behavior" (Prime
Directive 3, applied here to real CONTENT changes, not just STATE changes
the way ``alphaos.cards.promotion`` applies it).

This module NEVER writes to ``alphaos/cards/*.yaml`` itself (same law as
``alphaos.cards.promotion``) -- ``prepare_materialization()`` writes a
proposed SCAFFOLD (a verbatim copy of the current version's content, with
only the version field bumped) plus an evidence packet to a STAGING
directory (``settings.card_promotion_staging_dir``, default
``data/promotions/`` -- outside the cards directory and never git-add'd or
committed by this module). The operator's own edit + ``mv`` into
``alphaos/cards/`` + ``git commit`` is the authorship act;
``confirm_materialization()`` verifies that act happened (git-tracked,
clean, content actually changed, new version born ``state: shadow``) before
registering the new version and journaling the decision -- it never writes
the YAML file itself.

Deliberately a DIFFERENT CLI command (``card_materialize``) from
``alphaos.cards.promotion``'s own ``card_promote`` -- that command already
shipped, already audited, and does something unrelated (GRADUATION: an
EXISTING version's STATE moves shadow -> live_eligible, no content/version
change at all). Overloading a shipped, audited command with a second,
unrelated ceremony was rejected as a build-time judgment call in favor of
a clearly separate name.

Explicitly deferred (same consult, per PD#4): any mechanism that PROPOSES
*what* should change in the new version. No AI, no heuristic, no seed-time
diff schema -- the operator authors the new version's content entirely
themselves, using the rendered evidence packet purely as context. A
seed-time-authored diff (drafted before the evidence exists) was
considered and rejected: it manufactures rubber-stamp pressure at
promotion time, the exact failure mode PD#3 exists to prevent, just
laundered through an earlier timestamp. This stays deferred until a real
MET hypothesis with a real desired change exists to design a diff-schema
against -- designing one earlier would be speculative, against a lesson
this project already learned once (PR12's own two-arm-hypothesis scope
cut, logged in HANDOVER.md's reversible decision #1).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Callable, Optional

import yaml

from alphaos import lineage
from alphaos.cards.registry import CARDS_DIR, load_card_files
from alphaos.hypotheses.constants import HypothesisStatus
from alphaos.lineage.hashing import stable_hash
from alphaos.util import timeutils
from alphaos.util.ids import new_id


def _latest_card_row(journal, card_id: str) -> Optional[dict]:
    """Same query as ``alphaos.cards.promotion``'s own private helper of
    the same name -- kept as its own small copy here rather than importing
    a private (underscore-prefixed) name across a module boundary; it is a
    single trivial SELECT, not worth a shared-helper refactor of
    already-shipped, already-audited code for this one extra call site."""
    return journal.one(
        "SELECT card_id, version AS card_version, state FROM setup_cards "
        "WHERE card_id = ? ORDER BY version DESC LIMIT 1",
        (card_id,),
    )


def check_materialization_preconditions(journal, hypothesis_id: str) -> dict:
    """PURE READ. Named reason codes, checked in order, returning the
    FIRST unmet one: ``HYPOTHESIS_NOT_FOUND``, ``NO_CARD_ID``,
    ``HYPOTHESIS_NOT_MET``, ``CARD_NOT_REGISTERED``. Returns
    ``{"eligible", "reason_code", "detail", "card_id", "card_version",
    "card_state"}``.

    Deliberately does NOT reuse
    ``alphaos.cards.promotion.check_promotion_preconditions()``'s later
    checks (``FLOORS_NOT_MET``/``Q_VALUE_FLOOR``/``ALREADY_PROMOTED``/
    ``CARD_NOT_SHADOW``/``CARD_VERSION_TERMINALLY_DEMOTED``) -- those gate
    the GRADUATION decision on the CURRENT version (should this exact
    version go live); drafting a NEW version is a materially different
    question (is there real, operator-adjudicated evidence justifying
    letting the operator iterate on this card at all) with its own,
    smaller gate. A card mid-demotion or already live_eligible can still
    have a hypothesis-justified next version drafted against it.
    """
    h = journal.one("SELECT * FROM hypothesis_proposals WHERE hypothesis_id = ?", (hypothesis_id,))
    if h is None:
        return {"eligible": False, "reason_code": "HYPOTHESIS_NOT_FOUND",
                "detail": f"no such hypothesis_id: {hypothesis_id!r}",
                "card_id": None, "card_version": None, "card_state": None}

    if not h["card_id"]:
        return {"eligible": False, "reason_code": "NO_CARD_ID",
                "detail": f"{hypothesis_id} does not gate any card",
                "card_id": None, "card_version": None, "card_state": None}

    card_id = h["card_id"]

    if h["status"] != HypothesisStatus.MET.value:
        return {"eligible": False, "reason_code": "HYPOTHESIS_NOT_MET",
                "detail": f"{hypothesis_id} status is {h['status']!r}, not 'met' -- an operator must "
                          "adjudicate it via mark_hypothesis_status() first",
                "card_id": card_id, "card_version": None, "card_state": None}

    card = _latest_card_row(journal, card_id)
    if card is None:
        return {"eligible": False, "reason_code": "CARD_NOT_REGISTERED",
                "detail": f"no setup_cards row for card_id={card_id!r}",
                "card_id": card_id, "card_version": None, "card_state": None}

    return {"eligible": True, "reason_code": None, "detail": "all preconditions met",
            "card_id": card_id, "card_version": card["card_version"], "card_state": card["state"]}


def prepare_materialization(journal, hypothesis_id: str, staging_dir: str, cards_dir: Optional[Path] = None) -> dict:
    """PURE W.R.T. ``cards_dir``: writes ONLY inside ``staging_dir``, never
    inside ``cards_dir``. Returns eligibility + written file paths (or the
    ineligible reason with NOTHING written). Re-runnable: calling this
    again after a successful ``confirm_materialization()`` naturally
    targets ``latest_version + 1`` again (``check_materialization_
    preconditions()`` always re-derives the latest registered version
    fresh), so there is no separate "already prepared" state to track.

    Raises ``ValueError`` if ``staging_dir`` resolves to a path inside
    ``cards_dir``/``CARDS_DIR`` -- a misconfigured staging dir must never
    make this function's writes land where ``sync_registry()`` would then
    auto-register an un-reviewed, operator-authorless version (scope/
    safety-audit LOW-1: the module docstring's "never writes to cards/"
    claim must hold even under a bad ``CARD_PROMOTION_STAGING_DIR``, not
    only by convention).
    """
    directory = Path(cards_dir) if cards_dir is not None else CARDS_DIR
    staging = Path(staging_dir)
    if staging.resolve() == directory.resolve() or directory.resolve() in staging.resolve().parents:
        raise ValueError(
            f"prepare_materialization: staging_dir {staging_dir!r} resolves inside the cards "
            f"directory ({directory}) -- refusing to risk writing an un-reviewed version there"
        )

    check = check_materialization_preconditions(journal, hypothesis_id)
    if not check["eligible"]:
        return {"prepared": False, **check}

    h = journal.one("SELECT * FROM hypothesis_proposals WHERE hypothesis_id = ?", (hypothesis_id,))
    card_id = check["card_id"]
    old_version = check["card_version"]
    new_version = old_version + 1

    try:
        matches = [c for c in load_card_files(cards_dir) if c["card_id"] == card_id]
    except Exception as exc:  # noqa: BLE001 -- surface the operator's own YAML error verbatim, never swallow it
        return {"prepared": False, "eligible": False, "reason_code": "CARD_FILE_INVALID",
                "detail": str(exc), "card_id": card_id, "card_version": old_version,
                "card_state": check["card_state"]}
    old_card_matches = [c for c in matches if c["version"] == old_version]
    if not old_card_matches:
        return {"prepared": False, "eligible": False, "reason_code": "CARD_FILE_NOT_FOUND",
                "detail": f"no on-disk YAML for card_id={card_id!r} version={old_version} in "
                          f"{cards_dir or CARDS_DIR}",
                "card_id": card_id, "card_version": old_version, "card_state": check["card_state"]}
    old_card = old_card_matches[0]

    staging.mkdir(parents=True, exist_ok=True)

    scaffold = dict(old_card)
    scaffold["version"] = new_version
    scaffold_path = staging / f"{card_id}_v{new_version}.yaml"
    with open(scaffold_path, "w", encoding="utf-8") as f:
        f.write(
            f"# EDIT ME -- the operator is the sole author of this file's content.\n"
            f"# Scaffold copy of {card_id} v{old_version}, with version bumped to {new_version}.\n"
            f"# When ready: edit freely, `mv` into the real cards directory, `git add` + commit it,\n"
            f"# then run: alphaos card_materialize {hypothesis_id} --decided-by <you> --confirm\n"
        )
        yaml.safe_dump(scaffold, f, sort_keys=False)

    evidence = {
        "hypothesis_id": hypothesis_id,
        "card_id": card_id,
        "old_version": old_version,
        "new_version": new_version,
        "hypothesis_claim": h["claim"],
        "hypothesis_status": h["status"],
        "last_verdict": h["last_verdict"],
        "last_q_value": h["last_q_value"],
        "last_reason": h["last_reason"],
        "old_card_content": old_card,
    }
    evidence_path = staging / f"{card_id}_v{new_version}.evidence.json"
    with open(evidence_path, "w", encoding="utf-8") as f:
        json.dump(evidence, f, indent=2, sort_keys=True, default=str)
        f.write("\n")

    return {
        "prepared": True, "eligible": True, "reason_code": None,
        "card_id": card_id, "old_version": old_version, "new_version": new_version,
        "scaffold_path": str(scaffold_path), "evidence_path": str(evidence_path),
    }


def _default_git_check(path: Path) -> bool:
    """True iff ``path`` is committed to git with no pending changes
    (staged or unstaged) -- proof the operator actually committed it, not
    just dropped a file on disk. Fails safe: any git-invocation problem
    (not a repo, git missing, unexpected error) is treated as NOT clean,
    never as verified -- same "unknown never safe" law this codebase uses
    for freshness/ADV/missing-data everywhere else."""
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path)],
            capture_output=True, text=True, cwd=str(path.parent),
        )
        if tracked.returncode != 0:
            return False
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", str(path)],
            capture_output=True, text=True, cwd=str(path.parent),
        )
        return status.returncode == 0 and status.stdout.strip() == ""
    except OSError:
        return False


def confirm_materialization(
    journal, settings, hypothesis_id: str, decided_by: str,
    cards_dir: Optional[Path] = None,
    git_check_fn: Callable[[Path], bool] = _default_git_check,
) -> dict:
    """The write action. Re-derives eligibility + the expected new_version
    FRESH from the journal (never trusts a stale ``prepare_materialization``
    -time snapshot -- same close-the-race-window law as ``card_demote``'s
    own dry-run fix). Refuses unless ALL hold: hypothesis still MET, card
    still registered, a YAML file for (card_id, old_version + 1) exists on
    disk with valid card schema, its content actually differs from the old
    version, its own ``state`` field is ``'shadow'`` (a new version is
    NEVER born live_eligible here -- it must separately earn graduation
    through ``alphaos.cards.promotion``'s own scoreboard-gated
    ``card_promote``, exactly like any other version; skipping that gate
    would turn this ceremony into a promotion bypass), and the file is
    git-tracked with no pending changes (``git_check_fn``, injectable for
    tests -- proof of "operator-committed," not just "present on disk").

    On success: journals ONE ``promotion_decisions`` row
    (``direction='materialize'`` -- a third value, alongside 'promote'/
    'demote'; the existing exact-match filters on those two literals
    elsewhere in this codebase are unaffected by a new distinct value) and
    registers the new version into ``setup_cards`` directly (the same
    upsert shape ``alphaos.cards.registry.sync_registry()`` performs, so
    the version is immediately queryable without waiting for the next
    orchestrator startup; a concurrent ``sync_registry()`` re-scan of the
    same file afterwards is a content-hash-identical no-op, by
    construction). NEVER writes to ``cards_dir`` itself.
    """
    if decided_by == "system":
        raise ValueError("confirm_materialization: decided_by must be a real operator identity, not 'system'")

    check = check_materialization_preconditions(journal, hypothesis_id)
    if not check["eligible"]:
        return {"confirmed": False, **check}

    card_id = check["card_id"]
    old_version = check["card_version"]
    new_version = old_version + 1

    try:
        all_cards = load_card_files(cards_dir)
    except Exception as exc:  # noqa: BLE001 -- surface the operator's own YAML error verbatim, never swallow it
        return {"confirmed": False, "eligible": False, "reason_code": "CARD_FILE_INVALID",
                "detail": str(exc), "card_id": card_id, "card_version": old_version}

    new_matches = [c for c in all_cards if c["card_id"] == card_id and c["version"] == new_version]
    if not new_matches:
        return {"confirmed": False, "eligible": False, "reason_code": "NEW_VERSION_FILE_NOT_FOUND",
                "detail": f"no YAML in {cards_dir or CARDS_DIR} has card_id={card_id!r} version={new_version} yet -- "
                          "edit the staged scaffold, move it into the cards directory, and git commit it first",
                "card_id": card_id, "card_version": old_version}
    new_card = new_matches[0]

    old_matches = [c for c in all_cards if c["card_id"] == card_id and c["version"] == old_version]
    old_card = old_matches[0] if old_matches else None
    # Compare content EXCLUDING the version AND state fields. version always
    # differs by construction (that is the whole point of a version bump).
    # state is excluded too (correctness-audit MEDIUM-1): a new version off
    # a non-shadow parent is FORCED to set state='shadow' by the check
    # below, so a state-only flip with no real strategy change would
    # otherwise read as "content changed" and slip past this guard.
    if old_card is not None:
        new_without_version = {k: v for k, v in new_card.items() if k not in ("version", "state")}
        old_without_version = {k: v for k, v in old_card.items() if k not in ("version", "state")}
        if stable_hash(new_without_version) == stable_hash(old_without_version):
            return {"confirmed": False, "eligible": False, "reason_code": "NO_CONTENT_CHANGE",
                    "detail": f"{card_id} v{new_version}'s content is identical to v{old_version} (besides "
                              "the version number and state) -- a no-op version bump is refused (use "
                              "card_promote to graduate the existing version instead)",
                    "card_id": card_id, "card_version": old_version}

    if new_card.get("state") != "shadow":
        return {"confirmed": False, "eligible": False, "reason_code": "NEW_VERSION_NOT_SHADOW",
                "detail": f"{card_id} v{new_version}'s state is {new_card.get('state')!r}, not 'shadow' -- "
                          "a new version is always born shadow; use card_promote to graduate it once its "
                          "own scoreboard clears the floor",
                "card_id": card_id, "card_version": old_version}

    directory = Path(cards_dir) if cards_dir is not None else CARDS_DIR
    new_card_path = next(
        (p for p in sorted(directory.glob("*.yaml"))
         if _safe_load_card_id_version(p) == (card_id, new_version)),
        None,
    )
    if new_card_path is None or not git_check_fn(new_card_path):
        return {"confirmed": False, "eligible": False, "reason_code": "NOT_GIT_COMMITTED",
                "detail": f"the {card_id} v{new_version} YAML file is not committed to git (or has "
                          "pending changes) -- an operator-committed YAML version is the law (PD#3); "
                          "git add + commit it first",
                "card_id": card_id, "card_version": old_version}

    now = timeutils.stamp()
    decision_id = new_id("promodec")
    evidence = {
        "kind": "materialize",
        "old_version": old_version,
        "new_version": new_version,
        "new_card_path": str(new_card_path),
    }
    try:
        journal.insert("promotion_decisions", {
            "decision_id": decision_id,
            "card_id": card_id,
            "card_version": old_version,
            "from_state": check["card_state"],
            "to_state": check["card_state"],
            "direction": "materialize",
            "trigger": "manual",
            "hypothesis_id": hypothesis_id,
            "preregistration_id": None,
            "decided_by": decided_by,
            "research_ref": None,
            "evidence_json": evidence,
            "decided_at_utc": now.utc,
            "decided_at_sgt": now.local_sgt,
        })
    except sqlite3.IntegrityError as exc:
        return {"confirmed": False, "eligible": False, "reason_code": "CONCURRENT_MATERIALIZATION",
                "detail": f"{card_id} v{old_version} was materialized by a concurrent decision between "
                          f"the check and this write: {exc}",
                "card_id": card_id, "card_version": old_version}

    try:
        journal.insert("setup_cards", {
            "card_id": card_id,
            "version": new_version,
            "name": new_card.get("name"),
            "state": new_card.get("state"),
            "content_hash": stable_hash(new_card),
            "content_json": new_card,
            "lineage_id": lineage.get_or_create_lineage_id(journal, settings),
        })
    except sqlite3.IntegrityError:
        pass  # Already registered (a concurrent sync_registry() startup scan won
        # the race) -- same content, computed the same way; a benign no-op.

    return {
        "confirmed": True, "eligible": True, "reason_code": None,
        "card_id": card_id, "old_version": old_version, "new_version": new_version,
        "decision_id": decision_id,
    }


def _safe_load_card_id_version(path: Path) -> Optional[tuple]:
    """Best-effort single-file (card_id, version) read for locating the
    exact file ``confirm_materialization`` just proved exists via
    ``load_card_files`` -- returns None on any parse error rather than
    raising (a malformed OTHER file in the same directory must never block
    finding the valid one being confirmed)."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if isinstance(raw, dict) and raw.get("card_id") and isinstance(raw.get("version"), int):
            return (raw["card_id"], raw["version"])
    except (OSError, yaml.YAMLError):
        pass
    return None
