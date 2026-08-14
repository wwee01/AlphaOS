"""VOCAB-1: the AST vocabulary guard (seven-lens review P0-C).

The defect this guards against: ``reports/baseline_report.py`` and
``reports/regime_arming_scorer.py`` both filtered ``outcome_status =
'resolved'`` -- a literal ``alphaos/learning/outcomes_tracker.py`` (the
ONLY writer of ``candidate_outcomes.outcome_status``) has never once
emitted. ``n_paired_total`` was silently 0 for 35 days while the daily
digest printed a healthy-looking ``n_shadow_resolved`` count, and every
existing test stayed green because the SAME phantom literal was baked into
six test fixtures too.

Audit-fixup GROUP-A round 1: this walker was itself audited (both blind
Opus reviewers, convergent findings) and found to be vacuous or blind in
several ways -- see each fix's own comment below for what changed and why:

* FIX-1/FIX-4 -- coverage. The original walker only matched
  ``x["field"]``/``x.get("field")`` Compare forms and literal-only SQL. It
  is now extended to also see: a bare local variable named after the field
  (``replay_result == "..."``), a same-file module-level name/collection
  constant on either side of a comparison (single-file only -- no
  cross-import resolution, a documented boundary), parameterized SQL
  (``field = ?`` with the literal living in a separate params tuple,
  positionally matched), ``NOT IN``/``<>``/``!=`` operators, and a
  positional tuple-unpack assignment (``a, b, c = "x", "y", "z"``).
* FIX-2 -- docstrings. String constants that ARE a module/class/function
  docstring are never scanned as SQL text (a docstring merely quoting or
  discussing a bad literal, e.g. in this very file's own module docstring
  above, must never make a guard "pass" by accident).
* FIX-3 -- table-awareness. ``outcome_status`` is a column on TWO tables
  with genuinely DIFFERENT vocabularies (``candidate_outcomes`` and
  ``user_decision_overrides`` -- see ``constants.OverrideOutcomeStatus``).
  The walker now does best-effort table attribution (SQL FROM/JOIN/UPDATE/
  INTO alias resolution; ``X("table", ...)``-shaped call detection for
  ``count_rows``/``insert``-style helpers) and checks each finding against
  its OWN table's vocabulary. A finding whose table can't be resolved
  (mostly the pure-Python Compare/Dict forms, which carry no table context
  at all) falls back to the UNION of every known table's vocabulary for
  that field -- documented explicitly, per FIX-3's own instruction, rather
  than silently assumed.
* FIX-5 -- scope. The walker now scans ``tests/`` too (excluding its own
  file), not just ``alphaos/`` -- the six phantom fixtures that hid the
  original bug for 35 days lived in tests/, and a guard that never looks
  there again can't catch a repeat.
* FIX-6 -- two more guarded fields: ``outcome_status_10d`` and
  ``replay_status`` (both parallel vocabularies on ``candidate_outcomes``/
  ``shadow_baseline_decisions`` that were previously unguarded).

Known, honestly-stated remaining gaps (completeness was never the bar --
see FIX-4's own instruction): cross-file constant resolution (an imported
name from another module resolves to nothing); f-strings and dynamically
built SQL (``.format()``, ``+``-concatenation across non-adjacent
expressions, an f-string UPDATE target like ``_update_row``'s own
``f"UPDATE candidate_outcomes SET {set_clause} ..."``) are invisible to
both the SQL-text and table-attribution paths; attribute-style row access
(``row.outcome_status`` on a non-dict object) isn't recognized, only
subscript/``.get()``/bare-name; walrus assignments aren't a resolvable
name source. Every one of these is a real, currently-unfixed boundary, not
an oversight glossed over.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from alphaos.constants import LabelSource, OUTCOME_STATUSES, OverrideOutcomeStatus

_TESTS_DIR = Path(__file__).resolve().parent
_ALPHAOS_ROOT = _TESTS_DIR.parent / "alphaos"
_REPO_ROOT = _TESTS_DIR.parent
_SELF_FILE = Path(__file__).resolve()
# FIX-5: both trees are walked by default now -- the fixture purge that hid
# the original bug for 35 days happened in tests/, so the guard must too.
_DEFAULT_ROOTS = (_ALPHAOS_ROOT, _TESTS_DIR)


@dataclass(frozen=True)
class Finding:
    path: Path
    lineno: int
    literal: str
    table: Optional[str]  # best-effort table attribution; None = unresolved


# ===================================================================== SQL
_QUOTED_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")
# FROM/JOIN/UPDATE/INTO <table> [[AS] <alias>] -- FIX-3's alias resolver.
_FROM_JOIN_RE = re.compile(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?", re.IGNORECASE)
# FIX-4(b)/(c): NOT IN and <> added alongside the original =/!=/IN.
_OPERATORS = r"(?:NOT\s+IN|<>|!=|=|IN)"


def _sql_literal_pattern(field: str) -> "re.Pattern[str]":
    return re.compile(
        rf"(?:(\w+)\.)?\b{re.escape(field)}\b\s*{_OPERATORS}\s*\(?\s*"
        r"((?:'[^']*'|\"[^\"]*\")(?:\s*,\s*(?:'[^']*'|\"[^\"]*\"))*)",
        re.IGNORECASE,
    )


def _placeholder_pattern(field: str) -> "re.Pattern[str]":
    """Same shape as ``_sql_literal_pattern`` but matching one-or-more ``?``
    placeholders instead of quoted literals -- FIX-4(a)."""
    return re.compile(
        rf"(?:(\w+)\.)?\b{re.escape(field)}\b\s*{_OPERATORS}\s*\(?\s*(\?(?:\s*,\s*\?)*)",
        re.IGNORECASE,
    )


def _alias_table_map(text: str) -> dict:
    """Best-effort alias -> table map from FROM/JOIN/UPDATE/INTO clauses in
    ONE SQL string. A table with no alias is ALSO registered under its own
    name, so a bare (unaliased) field reference still resolves when the
    query touches exactly one such table."""
    out: dict = {}
    for table, alias in _FROM_JOIN_RE.findall(text):
        out[table] = table
        if alias:
            out[alias] = table
    return out


def _resolve_table(text: str, alias: Optional[str]) -> Optional[str]:
    amap = _alias_table_map(text)
    if alias:
        return amap.get(alias)
    tables = set(amap.values())
    return next(iter(tables)) if len(tables) == 1 else None


def _extract_sql_literals(text: str, field: str) -> list:
    """Returns ``(table_or_None, literal)`` pairs for every quoted-literal
    comparison against ``field`` in one SQL string."""
    out = []
    for m in _sql_literal_pattern(field).finditer(text):
        table = _resolve_table(text, m.group(1))
        for s, d in _QUOTED_RE.findall(m.group(2)):
            out.append((table, s or d))
    return out


def _placeholder_literals(path: Path, sql_node: ast.Constant, params_node, field: str) -> list:
    """FIX-4(a): positionally matches ``?`` placeholders for ``field`` in a
    parameterized query string to the corresponding elements of a sibling
    params tuple/list, extracting any that are raw string literals (a Name
    reference, e.g. a properly constants-sourced value, is deliberately
    NOT flagged -- that is the correct end state, not a gap)."""
    text = sql_node.value
    m = _placeholder_pattern(field).search(text)
    if not m:
        return []
    start = text[: m.start(2)].count("?")
    n = m.group(2).count("?")
    table = _resolve_table(text, m.group(1))
    elts = params_node.elts
    out = []
    for i in range(start, min(start + n, len(elts))):
        el = elts[i]
        if isinstance(el, ast.Constant) and isinstance(el.value, str):
            out.append(Finding(path, sql_node.lineno, el.value, table))
    return out


# ================================================================ Python
def _is_field_access(node: ast.AST, field: str) -> bool:
    """True for ``<expr>["field"]``, ``<expr>.get("field")``, or a bare
    local variable NAMED after the field (FIX-1's cited gap: ``replay_result
    = co_row.get("replay_result")`` followed by ``replay_result ==
    "ambiguous_same_bar"`` a few lines later -- the second comparison has no
    subscript/``.get()`` shape at all, just the variable's own name)."""
    if isinstance(node, ast.Subscript):
        idx = node.slice
        if isinstance(idx, ast.Constant) and idx.value == field:
            return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
        if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == field:
            return True
    if isinstance(node, ast.Name) and node.id == field:
        return True
    return False


def _literal_strings_in(node: ast.AST, name_values: dict) -> list:
    """String constants directly on ``node``; a same-file resolved name
    (FIX-1's ``_REPLAY_TERMINAL_HIT = frozenset({...})`` case, and FIX-4(d)
    "comparison against a named constant"); or a tuple/list/set/frozenset(
    {...}) literal of string constants."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name):
        return list(name_values.get(node.id, ()))
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        out = []
        for e in node.elts:
            out.extend(_literal_strings_in(e, name_values))
        return out
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "frozenset" and node.args:
        return _literal_strings_in(node.args[0], name_values)
    return []


def _docstring_node_ids(tree: ast.AST) -> set:
    """FIX-2: id()s of every Constant node that IS a module/class/function
    docstring -- excluded from SQL-text scanning so prose quoting a literal
    (e.g. this very file's own module docstring) can never be mistaken for
    a real comparison."""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _name_values(tree: ast.AST) -> dict:
    """Same-file, simple ``NAME = "literal"`` / ``NAME = ("a", "b")`` /
    ``NAME = frozenset({"a", "b"})`` resolution (module-level or nested --
    ``ast.walk`` finds every ``Assign``, not just top-level ones). Used to
    see through a bare name on either side of a comparison. Deliberately
    single-file and purely syntactic -- no cross-import resolution, no
    partial evaluation of expressions -- a documented boundary, not an
    attempt at full data-flow analysis."""
    out: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            values = _literal_strings_in(node.value, {})
            if values:
                out[node.targets[0].id] = values
    return out


def collect_field_literals(field: str, roots=_DEFAULT_ROOTS) -> list:
    """Walk every ``.py`` file under ``roots`` (skipping this guard's own
    file), returning a ``Finding`` per string literal discovered for
    ``field`` via any of: raw SQL text (``=``/``!=``/``<>``/``IN``/``NOT
    IN``, literal or ``?``-placeholder forms, docstrings excluded, with
    best-effort table attribution), a Python-level ``Compare``
    (subscript/``.get()``/bare-name access, literal or same-file-resolved-
    name comparator), a dict-literal write (optionally table-tagged when
    it's the 2nd positional arg of an ``X("table", {...})``-shaped call), a
    table-hinted predicate string (2nd positional arg of an ``X("table",
    "predicate")``-shaped call, e.g. ``count_rows``), or a positional
    tuple-unpack assignment."""
    found: list = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if path.resolve() == _SELF_FILE:
                continue
            source = path.read_text()
            tree = ast.parse(source, filename=str(path))
            docstring_ids = _docstring_node_ids(tree)
            names = _name_values(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if id(node) in docstring_ids:
                        continue
                    for table, lit in _extract_sql_literals(node.value, field):
                        found.append(Finding(path, node.lineno, lit, table))
                elif isinstance(node, ast.Compare):
                    left_is_field = _is_field_access(node.left, field)
                    for comparator in node.comparators:
                        right_is_field = _is_field_access(comparator, field)
                        if left_is_field and not right_is_field:
                            for lit in _literal_strings_in(comparator, names):
                                found.append(Finding(path, node.lineno, lit, None))
                        elif right_is_field and not left_is_field:
                            for lit in _literal_strings_in(node.left, names):
                                found.append(Finding(path, node.lineno, lit, None))
                elif isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and key.value == field:
                            for lit in _literal_strings_in(value, names):
                                found.append(Finding(path, node.lineno, lit, None))
                elif isinstance(node, ast.Assign):
                    # Positional tuple-unpack: `a, b, c = "x", "y", "z"`
                    # (baseline/tracker.py:108's own
                    # `replay_status, replay_result, replay_r = "complete",
                    # "no_action", 0.0` shape -- FIX-1's cited gap).
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Tuple)
                            and isinstance(node.value, ast.Tuple)
                            and len(target.elts) == len(node.value.elts)
                        ):
                            for t_elt, v_elt in zip(target.elts, node.value.elts):
                                if isinstance(t_elt, ast.Name) and t_elt.id == field:
                                    for lit in _literal_strings_in(v_elt, names):
                                        found.append(Finding(path, node.lineno, lit, None))
                elif isinstance(node, ast.Call):
                    if (
                        len(node.args) >= 2
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                    ):
                        table_hint = node.args[0].value
                        second = node.args[1]
                        if (
                            isinstance(second, ast.Constant)
                            and isinstance(second.value, str)
                            and re.search(rf"\b{re.escape(field)}\b", second.value)
                        ):
                            # `X("table", "field = 'lit'")` -- count_rows-style.
                            for _, lit in _extract_sql_literals(second.value, field):
                                found.append(Finding(path, node.lineno, lit, table_hint))
                        elif isinstance(second, ast.Dict):
                            # `X("table", {"field": "lit"})` -- insert-style.
                            for key, value in zip(second.keys, second.values):
                                if isinstance(key, ast.Constant) and key.value == field:
                                    for lit in _literal_strings_in(value, names):
                                        found.append(Finding(path, node.lineno, lit, table_hint))
                    # Parameterized SQL -- FIX-4(a): a string arg containing
                    # both '?' and the field name, paired with a Tuple/List
                    # params arg anywhere else in this SAME call's args
                    # (position-independent, unlike the table-hint check
                    # above, so it fires regardless of argument order).
                    sql_arg = None
                    params_arg = None
                    for a in node.args:
                        if (
                            isinstance(a, ast.Constant)
                            and isinstance(a.value, str)
                            and "?" in a.value
                            and re.search(rf"\b{re.escape(field)}\b", a.value)
                        ):
                            sql_arg = a
                        elif isinstance(a, (ast.Tuple, ast.List)):
                            params_arg = a
                    if sql_arg is not None and params_arg is not None:
                        found.extend(_placeholder_literals(path, sql_arg, params_arg, field))
    return _dedupe_prefer_tabled(found)


def _dedupe_prefer_tabled(findings: list) -> list:
    """The same literal is often discoverable via more than one detection
    path (e.g. a generic Dict scan AND the table-hinted Call scan both see
    a `journal.insert("table", {...})` write) -- harmless duplication for
    correctness, but collapsed here for a clean report, preferring the
    more-specific (table-resolved) instance."""
    best: dict = {}
    for f in findings:
        key = (f.path, f.lineno, f.literal)
        cur = best.get(key)
        if cur is None or (cur.table is None and f.table is not None):
            best[key] = f
    return list(best.values())


def _format_findings(findings: list) -> str:
    return "\n".join(
        f"{f.path.relative_to(_REPO_ROOT)}:{f.lineno}: {f.literal!r} (table={f.table!r})" for f in findings
    )


def _check_against_table_vocab(findings: list, table_vocab: dict) -> list:
    """A finding is bad if its literal isn't in its OWN resolved table's
    vocabulary; a finding whose table couldn't be resolved is checked
    against the UNION of every table's vocabulary instead (FIX-3's own
    "at minimum accept the union, and say so explicitly")."""
    union = set().union(*table_vocab.values()) if table_vocab else set()
    bad = []
    for f in findings:
        allowed = table_vocab.get(f.table, union) if f.table else union
        if f.literal not in allowed:
            bad.append(f)
    return bad


# ===================================================== outcome_status guard
# FIX-3: outcome_status is a column on TWO tables with genuinely different
# vocabularies. candidate_outcomes is the VOCAB-1 subject (constants.
# OUTCOME_STATUSES); user_decision_overrides is a SEPARATE state machine
# (constants.OverrideOutcomeStatus: pending/won/lost/breakeven/cancelled/
# expired, written exclusively via `.value` enum access in production --
# every literal comparison found against it today happens to be 'pending',
# which is coincidentally valid in BOTH vocabularies, so this table split
# was previously "safe by luck", not by design).
_OUTCOME_STATUS_TABLE_VOCAB = {
    "candidate_outcomes": set(OUTCOME_STATUSES),
    "user_decision_overrides": {e.value for e in OverrideOutcomeStatus},
}


def test_outcome_status_literals_match_the_writer_vocabulary():
    findings = collect_field_literals("outcome_status")
    assert findings, "the walker found nothing at all -- almost certainly a walker bug, not a clean codebase"
    # FIX-5 proof folded in: at least one finding must come from tests/ --
    # otherwise "walks tests/ too" would be true only in the docstring.
    assert any(_TESTS_DIR in f.path.parents for f in findings), (
        "no outcome_status finding came from tests/ -- the walker isn't actually scanning it"
    )
    bad = _check_against_table_vocab(findings, _OUTCOME_STATUS_TABLE_VOCAB)
    assert not bad, (
        "outcome_status literal(s) outside their own table's known vocabulary "
        f"{ {k: sorted(v) for k, v in _OUTCOME_STATUS_TABLE_VOCAB.items()} }:\n{_format_findings(bad)}"
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
    findings = collect_field_literals("outcome_status", roots=(pkg,))
    literals = {f.literal for f in findings}
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
    findings = collect_field_literals("outcome_status", roots=(pkg,))
    bad = _check_against_table_vocab(findings, _OUTCOME_STATUS_TABLE_VOCAB)
    assert bad == []


def test_ast_guard_is_not_fooled_by_a_docstring(tmp_path):
    """FIX-2 proof: a file whose ONLY content is a docstring QUOTING a bad
    literal (the auditor's own example) must produce zero findings -- a
    docstring is prose, not a real comparison, and scanning it would let a
    developer's own explanatory comment silently prop up (or defeat) the
    guard's non-vacuity check."""
    pkg = tmp_path / "fake_alphaos_docstring"
    pkg.mkdir()
    (pkg / "prose_only.py").write_text(
        '"""A module whose docstring merely DISCUSSES the bug: '
        "outcome_status = 'resolved' by mistake, once, historically. "
        'Never a real query.\n"""\n'
        "\n"
        "def noop():\n"
        '    """Docstring on a function too: outcome_status = \'resolved\'."""\n'
        "    return 1\n"
    )
    findings = collect_field_literals("outcome_status", roots=(pkg,))
    assert findings == [], f"docstring prose was scanned as real SQL text: {_format_findings(findings)}"


def test_ast_guard_table_aware_distinguishes_the_override_table(tmp_path):
    """FIX-3 proof: the SAME literal ('won') is invalid for
    candidate_outcomes but valid for user_decision_overrides -- the guard
    must tell them apart via each query's own FROM/UPDATE table, not treat
    outcome_status as one flat vocabulary."""
    pkg = tmp_path / "fake_alphaos_tables"
    pkg.mkdir()
    (pkg / "two_tables.py").write_text(
        "def bad(journal):\n"
        "    return journal.query(\n"
        "        \"SELECT * FROM candidate_outcomes co WHERE co.outcome_status = 'won'\"\n"
        "    )\n"
        "\n"
        "def good(journal):\n"
        "    return journal.query(\n"
        "        \"SELECT * FROM user_decision_overrides WHERE outcome_status = 'won'\"\n"
        "    )\n"
    )
    findings = collect_field_literals("outcome_status", roots=(pkg,))
    by_table = {(f.table, f.literal) for f in findings}
    assert ("candidate_outcomes", "won") in by_table
    assert ("user_decision_overrides", "won") in by_table
    bad = _check_against_table_vocab(findings, _OUTCOME_STATUS_TABLE_VOCAB)
    bad_tables = {f.table for f in bad}
    assert bad_tables == {"candidate_outcomes"}, (
        f"expected ONLY the candidate_outcomes query flagged, got: {_format_findings(bad)}"
    )


def test_ast_guard_catches_parameterized_and_not_in_mutations(tmp_path):
    """FIX-4(a)/(b)/(c) proof: a raw bad literal living in a parameterized
    query's OWN params tuple (not inline in the SQL string), and bad
    literals reached via NOT IN / <> -- all previously invisible forms."""
    pkg = tmp_path / "fake_alphaos_coverage"
    pkg.mkdir()
    (pkg / "coverage.py").write_text(
        "def placeholder(journal):\n"
        "    return journal.query(\n"
        "        \"SELECT * FROM candidate_outcomes WHERE outcome_status = ?\",\n"
        "        (\"resolved\",),\n"
        "    )\n"
        "\n"
        "def not_in(journal):\n"
        "    return journal.query(\n"
        "        \"SELECT * FROM candidate_outcomes WHERE outcome_status NOT IN ('resolved')\"\n"
        "    )\n"
        "\n"
        "def not_equal(journal):\n"
        "    return journal.query(\n"
        "        \"SELECT * FROM candidate_outcomes WHERE outcome_status <> 'resolved'\"\n"
        "    )\n"
    )
    findings = collect_field_literals("outcome_status", roots=(pkg,))
    literals_by_line = {f.lineno: f.literal for f in findings}
    assert "resolved" in literals_by_line.values(), _format_findings(findings)
    assert len(findings) == 3, f"expected exactly 3 findings (placeholder/NOT IN/<>), got: {_format_findings(findings)}"
    bad = _check_against_table_vocab(findings, _OUTCOME_STATUS_TABLE_VOCAB)
    assert len(bad) == 3


def test_ast_guard_catches_a_tuple_unpack_mutation(tmp_path):
    """FIX-1/FIX-4 proof: baseline/tracker.py:108's own
    ``replay_status, replay_result, replay_r = "complete", "no_action",
    0.0`` shape -- a positional tuple-unpack assignment, invisible to both
    the SQL-text and Dict/Compare paths."""
    pkg = tmp_path / "fake_alphaos_unpack"
    pkg.mkdir()
    (pkg / "unpack.py").write_text(
        "def branch(decision):\n"
        "    if decision == 'no_action':\n"
        "        replay_status, replay_result, replay_r = 'complete', 'resolved', 0.0\n"
        "    return replay_status, replay_result, replay_r\n"
    )
    findings = collect_field_literals("replay_result", roots=(pkg,))
    literals = {f.literal for f in findings}
    assert "resolved" in literals, _format_findings(findings)


def test_ast_guard_catches_a_named_constant_mutation(tmp_path):
    """FIX-4(d) proof: a comparison against a same-file NAMED constant
    (``if status == _BAD_STATUS:``) rather than an inline literal --
    exactly the ``_REPLAY_TERMINAL_HIT = frozenset({...})`` shape
    ``attribution/resolve.py`` uses for real, but with a single scalar
    constant instead of a collection, and directly for outcome_status this
    time (the field this codebase's own writer never uses this pattern
    for -- purely a coverage proof for the mechanism)."""
    pkg = tmp_path / "fake_alphaos_named_const"
    pkg.mkdir()
    (pkg / "named_const.py").write_text(
        "_BAD_STATUS = 'resolved'\n"
        "\n"
        "def check(row):\n"
        "    return row.get('outcome_status') == _BAD_STATUS\n"
    )
    findings = collect_field_literals("outcome_status", roots=(pkg,))
    literals = {f.literal for f in findings}
    assert "resolved" in literals, _format_findings(findings)


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
# hand -- baseline_report.py's own SQL only ever reads sbd.replay_r/
# sbd.replay_status, never sbd.replay_result), so this is legitimate
# per-table variation, not a genuine vocabulary mismatch -- the union below
# is asserted as a permanent guard rather than left as a one-off note, so a
# FUTURE genuine typo still gets caught. Both tables collapse to the SAME
# vocabulary here (table-split enforcement adds no extra power for this
# specific field, unlike outcome_status), so a flat set suffices.
_REPLAY_RESULT_VOCAB = {
    "stop_hit", "target_hit", "ambiguous_same_bar", "neither", "unavailable", "no_action",
}


def test_replay_result_literals_match_the_known_two_table_vocabulary():
    """Audit-fixup GROUP-A round 1 (FIX-1): this test was VACUOUS on the
    real tree (0 findings -- the walker was structurally blind to
    attribution/resolve.py's frozenset constant and bare-local-variable
    comparisons, and to baseline/tracker.py's tuple-unpack literals). Now
    non-vacuous (see the assert below) thanks to FIX-1/FIX-4's coverage
    additions."""
    findings = collect_field_literals("replay_result")
    assert findings, "still vacuous -- the walker found zero replay_result literals on the real tree"
    bad = [f for f in findings if f.literal not in _REPLAY_RESULT_VOCAB]
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
# classify there). The bare-literal CONSUMER comparisons found (production:
# scheduler/digest.py's shadow_fail_safe_today count, r["label_source"] ==
# "fail_safe"; tests: test_cost_guard.py's "label_source": "openai" fixture)
# match LabelSource.FAIL_SAFE.value / .OPENAI.value exactly. No genuine
# mismatch found; nothing to fix.
def test_label_source_literals_match_the_labelsource_enum():
    allowed = {e.value for e in LabelSource}
    findings = collect_field_literals("label_source")
    assert findings, "the walker found nothing at all -- almost certainly a walker bug, not a clean codebase"
    bad = [f for f in findings if f.literal not in allowed]
    assert not bad, f"label_source literal(s) outside LabelSource {sorted(allowed)!r}:\n{_format_findings(bad)}"


# ============================================ outcome_status_10d (FIX-6)
# HOLD-1's additive 10-trading-day shadow horizon, on candidate_outcomes
# ONLY (see outcomes_tracker.py's _update_hold1_10d_family docstring). The
# writer emits exactly {'complete', 'partial'} -- unlike outcome_status
# itself, this column has no 'pending'/'unavailable' member: a row that has
# no forward bars yet simply isn't touched (stays NULL), and the "give up,
# converge to unavailable" path outcome_status has doesn't exist here (see
# that module's own "Known gap (accepted, not fixed here...)" docstring).
_OUTCOME_STATUS_10D_VOCAB = {"complete", "partial"}


def test_outcome_status_10d_literals_match_the_writer_vocabulary():
    findings = collect_field_literals("outcome_status_10d")
    assert findings, "the walker found nothing at all -- almost certainly a walker bug, not a clean codebase"
    bad = [f for f in findings if f.literal not in _OUTCOME_STATUS_10D_VOCAB]
    assert not bad, (
        f"outcome_status_10d literal(s) outside {sorted(_OUTCOME_STATUS_10D_VOCAB)!r}:\n{_format_findings(bad)}"
    )


# =================================================== replay_status (FIX-6)
# shadow_baseline_decisions.replay_status, written by baseline/tracker.py's
# own decision-branch (complete/unavailable/pending -- no 'partial' member;
# confirmed by hand, every write site enumerated). SEPARATE naming
# collision found while adding this guard: alphaos/attribution/resolve.py
# returns its OWN computed dict with a key ALSO spelled "replay_status",
# populated from candidate_outcomes.replay_result-shaped values (stop_hit/
# target_hit/ambiguous_same_bar/neither/unavailable) -- a pure-function
# return shape, not a database column, that happens to reuse the name. The
# walker's table-attribution can't (and structurally shouldn't try to)
# resolve a table for that dict, so those findings arrive as table=None and
# fall to the union fallback -- which is why the ALLOWED set below is the
# union of both real vocabularies rather than the stricter
# shadow_baseline_decisions-only set: same "at minimum accept the union,
# and say so explicitly" treatment FIX-3 authorizes.
_REPLAY_STATUS_VOCAB = {"complete", "unavailable", "pending"} | _REPLAY_RESULT_VOCAB


def test_replay_status_literals_match_the_known_vocabulary():
    findings = collect_field_literals("replay_status")
    assert findings, "the walker found nothing at all -- almost certainly a walker bug, not a clean codebase"
    bad = [f for f in findings if f.literal not in _REPLAY_STATUS_VOCAB]
    assert not bad, (
        "replay_status literal(s) outside the known shadow_baseline_decisions/"
        f"attribution-return-shape union {sorted(_REPLAY_STATUS_VOCAB)!r}:\n{_format_findings(bad)}"
    )
