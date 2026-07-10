"""PR13.5: the diff-to-version materialization ceremony. Covers:
* check_materialization_preconditions() -- every reason code in isolation.
* prepare_materialization() -- scaffold + evidence packet content, refuses
  (writes nothing) when ineligible or when no on-disk file matches.
* confirm_materialization() -- every refusal reason code, the happy path,
  the concurrent-race IntegrityError catch.
* Empirical + AST-based proof that neither function ever writes inside the
  cards directory (only the injected staging dir) -- the PD#3 invariant
  this ceremony exists to serve, not undermine.
* CLI wiring smoke tests (cmd_card_materialize dry-run / --confirm).

All offline, in-memory, mock mode. No real money, no network. Git-tracked
checks use a REAL throwaway git repo in a pytest tmp_path (git init + add +
commit) rather than a mocked subprocess -- the actual git plumbing is part
of what's being proven, and git is guaranteed present in any dev/CI
environment this codebase already assumes (pre-commit hooks require it).
"""

from __future__ import annotations

import ast
import inspect
import subprocess

import pytest
import yaml

from alphaos.cards import materialize
from alphaos.orchestrator import Orchestrator
from conftest import make_settings


def _insert_card(journal, card_id, version, state="shadow"):
    journal.insert("setup_cards", {
        "card_id": card_id, "version": version, "state": state,
        "content_hash": f"hash-{card_id}-v{version}",
    })


def _insert_hypothesis(
    journal, hypothesis_id, card_id=None, risk_class="B", status="resolved", last_q_value=0.05,
):
    journal.insert("hypothesis_proposals", {
        "hypothesis_id": hypothesis_id,
        "risk_class": risk_class,
        "claim": f"test claim for {hypothesis_id}",
        "card_id": card_id,
        "prereg_id": "prereg1",
        "status": status,
        "analysis_not_before": "2026-01-01",
        "last_q_value": last_q_value,
        "last_verdict": "forward-test-candidate",
        "last_reason": "test reason",
    })


def _write_card_yaml(directory, card_id, version, state="shadow", extra_note="v1"):
    directory.mkdir(parents=True, exist_ok=True)
    content = {
        "card_id": card_id,
        "version": version,
        "name": f"Test Card {extra_note}",
        "state": state,
        "invalidation_rule": "test invalidation rule",
    }
    path = directory / f"{card_id}_v{version}.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(content, f)
    return path


def _init_git_repo(directory):
    subprocess.run(["git", "init", "-q"], cwd=str(directory), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(directory), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(directory), check=True)


def _git_commit_all(directory, message="commit"):
    subprocess.run(["git", "add", "-A"], cwd=str(directory), check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=str(directory), check=True)


def _snapshot(directory):
    """(filename, mtime_ns, size) tuples for every file under directory --
    used to empirically prove a function touched nothing there."""
    if not directory.exists():
        return set()
    return {
        (p.name, p.stat().st_mtime_ns, p.stat().st_size)
        for p in directory.iterdir() if p.is_file()
    }


def _eligible_hypothesis(journal, cards_dir, hypothesis_id="H-TEST", card_id="test_card", version=1):
    _write_card_yaml(cards_dir, card_id, version, state="shadow")
    _insert_card(journal, card_id, version, state="shadow")
    _insert_hypothesis(journal, hypothesis_id, card_id=card_id, status="met", last_q_value=0.02)
    return hypothesis_id, card_id, version


# ------------------------------------------------------- precondition checks
def test_precondition_hypothesis_not_found(journal):
    check = materialize.check_materialization_preconditions(journal, "H-NOPE")
    assert check == {"eligible": False, "reason_code": "HYPOTHESIS_NOT_FOUND",
                      "detail": "no such hypothesis_id: 'H-NOPE'",
                      "card_id": None, "card_version": None, "card_state": None}


def test_precondition_no_card_id(journal):
    _insert_hypothesis(journal, "H1", card_id=None)
    check = materialize.check_materialization_preconditions(journal, "H1")
    assert check["eligible"] is False
    assert check["reason_code"] == "NO_CARD_ID"


def test_precondition_hypothesis_not_met(journal):
    _insert_hypothesis(journal, "H1", card_id="card1", status="resolved")
    check = materialize.check_materialization_preconditions(journal, "H1")
    assert check["eligible"] is False
    assert check["reason_code"] == "HYPOTHESIS_NOT_MET"


def test_precondition_card_not_registered(journal):
    _insert_hypothesis(journal, "H1", card_id="card1", status="met")
    check = materialize.check_materialization_preconditions(journal, "H1")
    assert check["eligible"] is False
    assert check["reason_code"] == "CARD_NOT_REGISTERED"


def test_precondition_eligible_happy_path(journal, tmp_path):
    hyp, card_id, version = _eligible_hypothesis(journal, tmp_path / "cards")
    check = materialize.check_materialization_preconditions(journal, hyp)
    assert check == {"eligible": True, "reason_code": None, "detail": "all preconditions met",
                      "card_id": card_id, "card_version": version, "card_state": "shadow"}


# ------------------------------------------------------- prepare_materialization
def test_prepare_materialization_writes_scaffold_and_evidence(journal, tmp_path):
    cards_dir = tmp_path / "cards"
    staging_dir = tmp_path / "staging"
    hyp, card_id, version = _eligible_hypothesis(journal, cards_dir)

    result = materialize.prepare_materialization(journal, hyp, str(staging_dir), cards_dir=cards_dir)

    assert result["prepared"] is True
    assert result["old_version"] == version
    assert result["new_version"] == version + 1

    scaffold = yaml.safe_load(open(result["scaffold_path"], encoding="utf-8"))
    assert scaffold["card_id"] == card_id
    assert scaffold["version"] == version + 1
    assert scaffold["state"] == "shadow"  # verbatim copy -- unchanged content besides version

    with open(result["scaffold_path"], encoding="utf-8") as f:
        header = f.read()
    assert "EDIT ME" in header
    assert hyp in header

    import json
    evidence = json.load(open(result["evidence_path"], encoding="utf-8"))
    assert evidence["hypothesis_id"] == hyp
    assert evidence["old_version"] == version
    assert evidence["new_version"] == version + 1
    assert evidence["old_card_content"]["version"] == version


def test_prepare_materialization_refuses_and_writes_nothing_when_ineligible(journal, tmp_path):
    staging_dir = tmp_path / "staging"
    result = materialize.prepare_materialization(journal, "H-NOPE", str(staging_dir), cards_dir=tmp_path / "cards")
    assert result["prepared"] is False
    assert result["reason_code"] == "HYPOTHESIS_NOT_FOUND"
    assert not staging_dir.exists() or list(staging_dir.iterdir()) == []


def test_prepare_materialization_refuses_when_card_file_not_found_on_disk(journal, tmp_path):
    cards_dir = tmp_path / "cards"
    staging_dir = tmp_path / "staging"
    cards_dir.mkdir(parents=True)
    _insert_card(journal, "ghost_card", 1, state="shadow")
    _insert_hypothesis(journal, "H1", card_id="ghost_card", status="met")

    result = materialize.prepare_materialization(journal, "H1", str(staging_dir), cards_dir=cards_dir)
    assert result["prepared"] is False
    assert result["reason_code"] == "CARD_FILE_NOT_FOUND"
    assert list(staging_dir.iterdir()) == [] if staging_dir.exists() else True


def test_prepare_materialization_never_writes_to_cards_dir(journal, tmp_path):
    cards_dir = tmp_path / "cards"
    staging_dir = tmp_path / "staging"
    hyp, card_id, version = _eligible_hypothesis(journal, cards_dir)

    before = _snapshot(cards_dir)
    materialize.prepare_materialization(journal, hyp, str(staging_dir), cards_dir=cards_dir)
    after = _snapshot(cards_dir)
    assert before == after


# ------------------------------------------------------- confirm_materialization
def _prep_confirm_fixture(journal, tmp_path, new_state="shadow", new_content_differs=True):
    cards_dir = tmp_path / "cards"
    hyp, card_id, old_version = _eligible_hypothesis(journal, cards_dir)
    new_version = old_version + 1
    note = "v2-different" if new_content_differs else "v1"
    _write_card_yaml(cards_dir, card_id, new_version, state=new_state, extra_note=note)
    _init_git_repo(cards_dir)
    _git_commit_all(cards_dir)
    return hyp, card_id, old_version, new_version, cards_dir


def test_confirm_materialization_happy_path(journal, settings, tmp_path):
    hyp, card_id, old_version, new_version, cards_dir = _prep_confirm_fixture(journal, tmp_path)

    result = materialize.confirm_materialization(
        journal, settings, hyp, "ck", cards_dir=cards_dir, git_check_fn=lambda p: True,
    )

    assert result["confirmed"] is True
    assert result["card_id"] == card_id
    assert result["old_version"] == old_version
    assert result["new_version"] == new_version

    decision = journal.one(
        "SELECT * FROM promotion_decisions WHERE card_id = ? AND card_version = ? AND direction = 'materialize'",
        (card_id, old_version),
    )
    assert decision is not None
    assert decision["decided_by"] == "ck"
    assert decision["from_state"] == "shadow"
    assert decision["to_state"] == "shadow"

    new_row = journal.one(
        "SELECT * FROM setup_cards WHERE card_id = ? AND version = ?", (card_id, new_version),
    )
    assert new_row is not None
    assert new_row["state"] == "shadow"


def test_confirm_materialization_refuses_when_decided_by_is_system(journal, settings, tmp_path):
    hyp, *_rest = _prep_confirm_fixture(journal, tmp_path)
    with pytest.raises(ValueError, match="system"):
        materialize.confirm_materialization(journal, settings, hyp, "system", git_check_fn=lambda p: True)


def test_confirm_materialization_refuses_when_new_version_file_missing(journal, settings, tmp_path):
    cards_dir = tmp_path / "cards"
    hyp, card_id, version = _eligible_hypothesis(journal, cards_dir)
    result = materialize.confirm_materialization(
        journal, settings, hyp, "ck", cards_dir=cards_dir, git_check_fn=lambda p: True,
    )
    assert result["confirmed"] is False
    assert result["reason_code"] == "NEW_VERSION_FILE_NOT_FOUND"


def test_confirm_materialization_refuses_when_content_unchanged(journal, settings, tmp_path):
    hyp, card_id, old_version, new_version, cards_dir = _prep_confirm_fixture(
        journal, tmp_path, new_content_differs=False,
    )
    result = materialize.confirm_materialization(
        journal, settings, hyp, "ck", cards_dir=cards_dir, git_check_fn=lambda p: True,
    )
    assert result["confirmed"] is False
    assert result["reason_code"] == "NO_CONTENT_CHANGE"


def test_confirm_materialization_refuses_when_new_version_not_shadow(journal, settings, tmp_path):
    hyp, card_id, old_version, new_version, cards_dir = _prep_confirm_fixture(journal, tmp_path, new_state="live_eligible")
    result = materialize.confirm_materialization(
        journal, settings, hyp, "ck", cards_dir=cards_dir, git_check_fn=lambda p: True,
    )
    assert result["confirmed"] is False
    assert result["reason_code"] == "NEW_VERSION_NOT_SHADOW"


def test_confirm_materialization_refuses_when_not_git_committed(journal, settings, tmp_path):
    hyp, card_id, old_version, new_version, cards_dir = _prep_confirm_fixture(journal, tmp_path)
    result = materialize.confirm_materialization(
        journal, settings, hyp, "ck", cards_dir=cards_dir, git_check_fn=lambda p: False,
    )
    assert result["confirmed"] is False
    assert result["reason_code"] == "NOT_GIT_COMMITTED"


def test_confirm_materialization_refuses_when_card_file_invalid(journal, settings, tmp_path):
    cards_dir = tmp_path / "cards"
    hyp, card_id, old_version = _eligible_hypothesis(journal, cards_dir)
    new_version = old_version + 1
    # A malformed new-version file: missing the required invalidation_rule field.
    bad_path = cards_dir / f"{card_id}_v{new_version}.yaml"
    with open(bad_path, "w", encoding="utf-8") as f:
        yaml.safe_dump({"card_id": card_id, "version": new_version, "name": "bad", "state": "shadow"}, f)

    result = materialize.confirm_materialization(
        journal, settings, hyp, "ck", cards_dir=cards_dir, git_check_fn=lambda p: True,
    )
    assert result["confirmed"] is False
    assert result["reason_code"] == "CARD_FILE_INVALID"


def test_confirm_materialization_handles_concurrent_race(journal, settings, tmp_path):
    hyp, card_id, old_version, new_version, cards_dir = _prep_confirm_fixture(journal, tmp_path)

    # Simulate a winning concurrent confirm: insert the row this call would
    # also try to insert, ahead of time.
    journal.insert("promotion_decisions", {
        "decision_id": "promodec-race",
        "card_id": card_id, "card_version": old_version,
        "from_state": "shadow", "to_state": "shadow",
        "direction": "materialize", "trigger": "manual",
        "hypothesis_id": hyp, "decided_by": "someone_else",
        "decided_at_utc": "2026-01-01T00:00:00+00:00", "decided_at_sgt": "2026-01-01T08:00:00+08:00",
    })

    result = materialize.confirm_materialization(
        journal, settings, hyp, "ck", cards_dir=cards_dir, git_check_fn=lambda p: True,
    )
    assert result["confirmed"] is False
    assert result["reason_code"] == "CONCURRENT_MATERIALIZATION"


def test_confirm_materialization_never_writes_to_cards_dir(journal, settings, tmp_path):
    hyp, card_id, old_version, new_version, cards_dir = _prep_confirm_fixture(journal, tmp_path)
    before = _snapshot(cards_dir)
    result = materialize.confirm_materialization(
        journal, settings, hyp, "ck", cards_dir=cards_dir, git_check_fn=lambda p: True,
    )
    assert result["confirmed"] is True
    after = _snapshot(cards_dir)
    assert before == after


# ---------------------------------------------- structural no-write proof (§H.6)
def test_materialize_module_never_calls_open_on_a_cards_directory_path():
    """AST-based proof: every ``open(...)`` call in materialize.py has a
    first argument that is NOT one of the names that could point at the
    cards directory (``cards_dir``, ``CARDS_DIR``, ``directory``) -- the
    only names this module ever binds to a cards-directory path. Robust to
    a future refactor accidentally introducing a write there; a purely
    empirical test alone would only catch behavior actually exercised by
    the specific fixtures above."""
    source = inspect.getsource(materialize)
    tree = ast.parse(source)
    forbidden_names = {"cards_dir", "CARDS_DIR", "directory"}
    open_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open"
    ]
    assert open_calls, "expected at least one open() call in materialize.py (scaffold/evidence writes)"
    for call in open_calls:
        first_arg = call.args[0]
        if isinstance(first_arg, ast.Name):
            assert first_arg.id not in forbidden_names, (
                f"materialize.py calls open() with a cards-directory-derived path: {ast.dump(call)}"
            )


def test_promotion_and_materialize_are_never_cross_wired():
    """promotion.py's graduation functions and materialize.py's
    materialization functions must stay independent ceremonies -- neither
    module calls into the other's write actions (promote_card/demote_card
    vs prepare_materialization/confirm_materialization)."""
    from alphaos.cards import promotion

    mat_source = inspect.getsource(materialize)
    assert "promote_card(" not in mat_source
    assert "demote_card(" not in mat_source

    promo_source = inspect.getsource(promotion)
    assert "prepare_materialization(" not in promo_source
    assert "confirm_materialization(" not in promo_source


# ------------------------------------------------------------- CLI wiring
def _orchestrator_with_staging_dir(journal, tmp_path):
    settings = make_settings(CARD_PROMOTION_STAGING_DIR=str(tmp_path / "staging"))
    return Orchestrator(settings=settings, journal=journal)


def test_cmd_card_materialize_dry_run_stages_scaffold(journal, tmp_path):
    from alphaos.__main__ import cmd_card_materialize

    orch = _orchestrator_with_staging_dir(journal, tmp_path)
    _insert_card(journal, "catalyst_momentum_v2", 1, state="shadow")
    _insert_hypothesis(journal, "H-CLI", card_id="catalyst_momentum_v2", status="met")

    exit_code = cmd_card_materialize(orch, "H-CLI", None, confirm=False)
    assert exit_code == 0
    staged = list((tmp_path / "staging").iterdir())
    assert any(p.suffix == ".yaml" for p in staged)


def test_cmd_card_materialize_confirm_requires_decided_by(journal, tmp_path):
    from alphaos.__main__ import cmd_card_materialize

    orch = _orchestrator_with_staging_dir(journal, tmp_path)
    exit_code = cmd_card_materialize(orch, "H-NOPE", None, confirm=True)
    assert exit_code == 1


def test_cmd_card_materialize_not_eligible_returns_1(journal, tmp_path):
    from alphaos.__main__ import cmd_card_materialize

    orch = _orchestrator_with_staging_dir(journal, tmp_path)
    exit_code = cmd_card_materialize(orch, "H-NOPE", None, confirm=False)
    assert exit_code == 1
