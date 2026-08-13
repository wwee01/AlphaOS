"""VOCAB-1: the AST vocabulary guard (seven-lens review P0-C).

The defect this guards against: ``reports/baseline_report.py`` and
``reports/regime_arming_scorer.py`` both filtered ``outcome_status =
'resolved'`` -- a literal ``alphaos/learning/outcomes_tracker.py`` (the
ONLY writer of ``candidate_outcomes.outcome_status``) has never once
emitted. ``n_paired_total`` was silently 0 for 35 days while the daily
digest printed a healthy-looking ``n_shadow_resolved`` count, and every
existing test stayed green because the SAME phantom literal was baked into
six test fixtures too.

This module walks the AST of every ``.py`` file under ``alphaos/`` (never
``tests/`` -- the fixture purge is a separate, one-time task, not something
this guard re-checks on every run) and collects every string literal
compared against a target field, in two forms:

* raw SQL text embedded in a Python string constant (e.g.
  ``"WHERE co.outcome_status = 'resolved'"``);
* Python-level access -- ``row["field"]`` / ``row.get("field")`` compared
  with ``==``, ``!=``, or ``in``, and dict-literal writes
  (``{"field": "literal"}``).

``test_outcome_status_literals_match_the_writer_vocabulary`` asserts every
literal found this way is a member of ``constants.OUTCOME_STATUSES`` -- the
SAME tuple ``outcomes_tracker.py`` unpacks its own status literals from
(see that module's ``_STATUS_*`` aliases), so the reader and the writer can
never spell the vocabulary apart again.

``test_ast_guard_catches_a_mutation`` proves the guard is not vacuously
true: it points the SAME walker at a synthetic fixture directory containing
one bad literal and asserts the walker actually flags it (never touches the
real source tree -- see that test's own docstring for why this is safer
than reverting-and-restoring a real file mid-suite).

Item 4 of VOCAB-1 ("same sweep for replay_result and label_source... report
what it finds; fix only genuine mismatches, list anything it can't
classify") is covered by the two sections below the outcome_status guard --
see each one's own docstring for what the sweep found.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from alphaos.constants import LabelSource, OUTCOME_STATUSES

_TESTS_DIR = Path(__file__).resolve().parent
_ALPHAOS_ROOT = _TESTS_DIR.parent / "alphaos"

# Matches `<field> = 'lit'`, `<field> != "lit"`, or `<field> IN ('a', 'b')`
# inside a raw SQL string. SQL keywords in this codebase are written
# consistently upper-case, but matched case-insensitively to be safe.
_SQL_LITERAL_TEMPLATE = (
    r"\b{field}\b\s*(?:=|!=|IN)\s*\(?\s*"
    r"((?:'[^']*'|\"[^\"]*\")(?:\s*,\s*(?:'[^']*'|\"[^\"]*\"))*)"
)
_QUOTED_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def _extract_sql_literals(text: str, field: str) -> list[str]:
    pattern = re.compile(_SQL_LITERAL_TEMPLATE.format(field=re.escape(field)), re.IGNORECASE)
    out = []
    for m in pattern.finditer(text):
        for lit_single, lit_double in _QUOTED_RE.findall(m.group(1)):
            out.append(lit_single or lit_double)
    return out


def _is_field_access(node: ast.AST, field: str) -> bool:
    """True for ``<expr>["field"]``, ``<expr>.get("field")``, or
    ``<expr>.get("field", default)``."""
    if isinstance(node, ast.Subscript):
        idx = node.slice
        if isinstance(idx, ast.Constant) and idx.value == field:
            return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
        if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == field:
            return True
    return False


def _literal_strings_in(node: ast.AST) -> list[str]:
    """String constants directly on ``node``, or inside a tuple/list/set
    literal of string constants (for ``in (...)`` comparisons)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def collect_field_literals(root: Path, field: str) -> list[tuple[Path, int, str]]:
    """Walk every ``.py`` file under ``root``, returning ``(file, lineno,
    literal)`` for every string literal found associated with ``field`` --
    SQL text, a Python-level Compare (``==``/``!=``/``in``), or a
    dict-literal write (``{"field": "literal"}``)."""
    found: list[tuple[Path, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for lit in _extract_sql_literals(node.value, field):
                    found.append((path, node.lineno, lit))
            elif isinstance(node, ast.Compare):
                left_is_field = _is_field_access(node.left, field)
                for comparator in node.comparators:
                    right_is_field = _is_field_access(comparator, field)
                    if left_is_field and not right_is_field:
                        found.extend((path, node.lineno, lit) for lit in _literal_strings_in(comparator))
                    elif right_is_field and not left_is_field:
                        found.extend((path, node.lineno, lit) for lit in _literal_strings_in(node.left))
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == field:
                        for lit in _literal_strings_in(value):
                            found.append((path, node.lineno, lit))
    return found


def _format_findings(findings: list[tuple[Path, int, str]]) -> str:
    return "\n".join(f"{p.relative_to(_ALPHAOS_ROOT.parent)}:{lineno}: {lit!r}" for p, lineno, lit in findings)


# ===================================================== outcome_status guard
def test_outcome_status_literals_match_the_writer_vocabulary():
    allowed = set(OUTCOME_STATUSES)
    findings = collect_field_literals(_ALPHAOS_ROOT, "outcome_status")
    assert findings, "the walker found nothing at all -- almost certainly a walker bug, not a clean codebase"
    bad = [(p, ln, lit) for p, ln, lit in findings if lit not in allowed]
    assert not bad, (
        "outcome_status literal(s) outside constants.OUTCOME_STATUSES "
        f"{sorted(allowed)!r}:\n{_format_findings(bad)}"
    )


def test_ast_guard_catches_a_mutation(tmp_path):
    """Mutation-test: the guard must not be vacuously true. Points the SAME
    walker at a synthetic one-file package (never the real source tree --
    editing and restoring a real file mid-suite-run is exactly the kind of
    shared-mutable-state hazard pytest-xdist/parallel runs are sensitive to,
    and the point of THIS test is only to prove the walker's own detection
    logic works, not to re-prove the production fix, which
    test_outcome_status_literals_match_the_writer_vocabulary already does
    every run) containing the exact phantom literal VOCAB-1 fixed."""
    pkg = tmp_path / "fake_alphaos"
    pkg.mkdir()
    (pkg / "bad_report.py").write_text(
        "def q(journal):\n"
        "    return journal.query(\n"
        "        \"SELECT * FROM candidate_outcomes WHERE outcome_status = 'resolved'\"\n"
        "    )\n"
    )
    findings = collect_field_literals(pkg, "outcome_status")
    literals = {lit for _, _, lit in findings}
    assert "resolved" in literals, "walker failed to detect the planted phantom literal"
    assert "resolved" not in set(OUTCOME_STATUSES)


def test_ast_guard_finds_nothing_bad_in_a_clean_synthetic_file(tmp_path):
    """Complement to the mutation test above: a file using ONLY real
    vocabulary members must produce zero flagged literals, proving the
    guard doesn't just flag everything indiscriminately."""
    pkg = tmp_path / "fake_alphaos_clean"
    pkg.mkdir()
    (pkg / "good_report.py").write_text(
        "def q(journal):\n"
        "    return journal.query(\n"
        "        \"SELECT * FROM candidate_outcomes WHERE outcome_status = 'complete'\"\n"
        "    )\n"
    )
    findings = collect_field_literals(pkg, "outcome_status")
    bad = [lit for _, _, lit in findings if lit not in set(OUTCOME_STATUSES)]
    assert bad == []


# ================================================== replay_result sweep
# VOCAB-1 item 4. Hand-classified finding (documented in the GROUP-A build
# report): candidate_outcomes.replay_result and shadow_baseline_decisions.
# replay_result share a column NAME across TWO DIFFERENT tables with two
# DELIBERATELY different vocabularies -- both ultimately produced by
# alphaos/learning/outcomes_engine.py's replay_bracket() ({'stop_hit',
# 'target_hit', 'ambiguous_same_bar', 'neither', 'unavailable'}), but
# alphaos/baseline/tracker.py additionally stamps its own 'no_action'
# sentinel for a shadow_baseline_decisions row that never had a live entry
# attempt at all (a candidate_outcomes row is only ever created for a real
# candidate/proposal/reject, so 'no_action' has no analogue there). No
# consumer anywhere reads the two columns interchangeably (confirmed by
# hand -- baseline_report.py's own SQL only ever reads sbd.replay_r /
# sbd.replay_status, never sbd.replay_result), so this is legitimate
# per-table variation, not a genuine vocabulary mismatch -- the union below
# is asserted as a permanent guard rather than left as a one-off note, so a
# FUTURE genuine typo still gets caught.
_REPLAY_RESULT_VOCAB = {
    "stop_hit", "target_hit", "ambiguous_same_bar", "neither", "unavailable", "no_action",
}


def test_replay_result_literals_match_the_known_two_table_vocabulary():
    findings = collect_field_literals(_ALPHAOS_ROOT, "replay_result")
    bad = [(p, ln, lit) for p, ln, lit in findings if lit not in _REPLAY_RESULT_VOCAB]
    assert not bad, (
        "replay_result literal(s) outside the known candidate_outcomes/"
        f"shadow_baseline_decisions union {sorted(_REPLAY_RESULT_VOCAB)!r} -- classify by hand "
        f"before adding to the allowed set (see this test's own docstring):\n{_format_findings(bad)}"
    )


# ==================================================== label_source sweep
# VOCAB-1 item 4. Hand-classified finding: the writer
# (alphaos/ai/playbook_classifier.py) NEVER spells a label_source literal --
# every write goes through LabelSource.OPENAI.value / .MOCK.value /
# .FAIL_SAFE.value (enum access, not a string constant, so the AST walker
# structurally can't and shouldn't flag those -- there is nothing to
# classify there). The one bare-literal CONSUMER comparison found
# (alphaos/scheduler/digest.py's shadow_fail_safe_today count, r["label_
# source"] == "fail_safe") matches LabelSource.FAIL_SAFE.value exactly. No
# genuine mismatch found; nothing to fix.
def test_label_source_literals_match_the_labelsource_enum():
    allowed = {e.value for e in LabelSource}
    findings = collect_field_literals(_ALPHAOS_ROOT, "label_source")
    bad = [(p, ln, lit) for p, ln, lit in findings if lit not in allowed]
    assert not bad, f"label_source literal(s) outside LabelSource {sorted(allowed)!r}:\n{_format_findings(bad)}"
