# tests/test_no_project_name_policy.py
"""No project name may condition production behaviour.

Plan 6 Stage F2 item 2, spec §6 ("No project branches": production runtime
contains no `commons-cli`, `bigtop`, `bigpetstore` or `tvm` name-conditioned
policy).

Every anchor this harness was tuned against is one live repository, so the
cheapest way to make an anchor pass is to branch on its name. That defect is
invisible to the anchor suites themselves — they only ever run the project
they are named after — so it needs its own guard, and the guard has to be
structural rather than a grep: the names are all over the tree as evidence
notes ("live bigtop: the reactor summary is a lie"), and deleting that
provenance would cost more than it buys.

So this walks `src/sag/**/*.py` with `ast` and flags a name only where it can
CONDITION something:

* a string literal in a comparison, a condition or a dict/subscript lookup key
  — the shapes `if root == "bigtop"`, `TABLE = {"tvm": ...}`, `cfg["bigtop"]`;
* a function, class, attribute, parameter, variable or import name.

Explicitly allowed, because none of them can branch:

* comments (`tokenize`) and docstrings (`ast.get_docstring`) — provenance;
* type annotations, including `Literal[...]` value spaces. `ReplayHeader.probe`
  in `sag/agent/replay.py` is the one real case: it names which recorded replay
  fixture a transcript came from. It is declared and never read — nothing in
  `src/sag` compares against it — so it constrains a fixture LABEL, not
  behaviour. `test_the_replay_probe_label_is_the_only_annotation_case` pins
  that down, and the moment anyone writes `if header.probe == "bigtop"` the
  comparison rule above catches it.

The guard is self-tested against synthetic modules in both directions, so a
green result cannot mean "the walker found nothing anywhere".
"""

import ast
import io
import tokenize
from pathlib import Path

import pytest

RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "src" / "sag"

# The anchor projects, lowercase. `tvm` is deliberately a bare substring: a
# `libtvm.so` special case is exactly the kind of policy this forbids.
PROJECT_NAMES = ("commons-cli", "bigtop", "bigpetstore", "tvm")

# Subscripts that are type-level value spaces rather than runtime lookups.
TYPE_SUBSCRIPTS = ("Literal", "Annotated")


def _runtime_sources():
    return sorted(RUNTIME_ROOT.rglob("*.py"))


def _names_in(text):
    lowered = str(text).lower()
    return [name for name in PROJECT_NAMES if name in lowered]


def _names_in_identifier(identifier):
    """`-` and `_` are the same word break for this purpose.

    `commons_cli_root` is `commons-cli` policy wearing a Python spelling, so
    the identifier is normalized before it is searched.
    """
    normalized = str(identifier or "").lower().replace("_", "-").replace(".", "-")
    return [name for name in PROJECT_NAMES if name in normalized]


def _docstring_nodes(tree):
    """Every string constant that IS a docstring — allowed provenance."""
    allowed = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None) or []
        if body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                allowed.add(id(value))
    return allowed


def _annotation_nodes(tree):
    """Every node inside a type annotation or a `Literal[...]` value space."""
    allowed = set()
    regions = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            regions.append(node.annotation)
        elif isinstance(node, ast.arg):
            regions.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            regions.append(node.returns)
        elif isinstance(node, ast.Subscript):
            base = node.value
            label = getattr(base, "id", None) or getattr(base, "attr", None)
            if label in TYPE_SUBSCRIPTS:
                regions.append(node.slice)
    for region in regions:
        if region is None:
            continue
        for sub in ast.walk(region):
            allowed.add(id(sub))
    return allowed


def _conditioning_strings(tree):
    """(node, context) for every string constant in a CONDITIONING position."""
    found = []

    def collect(node, context):
        if node is None:
            return
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                found.append((sub, context))

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            collect(node.left, "comparison")
            for comparator in node.comparators:
                collect(comparator, "comparison")
        elif isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            collect(node.test, "condition")
        elif isinstance(node, ast.BoolOp):
            for value in node.values:
                collect(value, "condition")
        elif isinstance(node, ast.comprehension):
            for guard in node.ifs:
                collect(guard, "condition")
        elif isinstance(node, ast.Match):
            collect(node.subject, "match")
        elif isinstance(node, ast.match_case):
            collect(node.pattern, "match")
        elif isinstance(node, ast.Subscript):
            collect(node.slice, "lookup key")
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                collect(key, "dict key")
    return found


def _identifiers(tree):
    """(node, identifier, context) for every name production code declares."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append((node, node.name, "function name"))
        elif isinstance(node, ast.ClassDef):
            found.append((node, node.name, "class name"))
        elif isinstance(node, ast.Name):
            found.append((node, node.id, "variable name"))
        elif isinstance(node, ast.Attribute):
            found.append((node, node.attr, "attribute name"))
        elif isinstance(node, ast.arg):
            found.append((node, node.arg, "parameter name"))
        elif isinstance(node, ast.keyword) and node.arg:
            found.append((node, node.arg, "keyword name"))
        elif isinstance(node, ast.alias):
            found.append((node, node.name, "import name"))
            if node.asname:
                found.append((node, node.asname, "import alias"))
    return found


def scan_source(source, label="<module>"):
    """Every place `source` lets a project name condition behaviour.

    One node is reported ONCE even when several rules cover it — `if x ==
    "bigtop"` is both a condition and a comparison, and that is one defect, not
    two. The first rule to reach the node names it, which `ast.walk` makes
    deterministic.
    """
    tree = ast.parse(source)
    exempt = _docstring_nodes(tree) | _annotation_nodes(tree)
    offenders = []
    seen = set()
    for node, context in _conditioning_strings(tree):
        if id(node) in exempt or id(node) in seen:
            continue
        seen.add(id(node))
        for name in _names_in(node.value):
            offenders.append(f"{label}:{node.lineno}: {context} {name!r} in {node.value[:60]!r}")
    for node, identifier, context in _identifiers(tree):
        if id(node) in exempt or (id(node), identifier) in seen:
            continue
        seen.add((id(node), identifier))
        for name in _names_in_identifier(identifier):
            offenders.append(
                f"{label}:{getattr(node, 'lineno', 0)}: {context} {name!r} in {identifier!r}"
            )
    return offenders


def comments_in(source):
    """Every `#` comment, via `tokenize` — `ast` drops them entirely."""
    return [
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]


# --------------------------------------------------------------------------- #
# 1) The policy itself.
# --------------------------------------------------------------------------- #
def test_production_code_never_conditions_on_a_project_name():
    offenders = []
    for path in _runtime_sources():
        offenders.extend(
            scan_source(path.read_text(encoding="utf-8"), path.relative_to(RUNTIME_ROOT))
        )

    assert offenders == []


def test_every_runtime_source_parses_and_is_actually_scanned():
    """A guard that silently skipped files would pass for the wrong reason."""
    sources = _runtime_sources()

    assert len(sources) > 50
    for path in sources:
        ast.parse(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 2) The allowances are real, and exercised by the real tree.
# --------------------------------------------------------------------------- #
def test_the_tree_really_does_carry_project_names_in_comments():
    """The allowance is load-bearing: this evidence exists and stays legal."""
    carriers = [
        path.relative_to(RUNTIME_ROOT)
        for path in _runtime_sources()
        if any(_names_in(comment) for comment in comments_in(path.read_text(encoding="utf-8")))
    ]

    assert carriers, "no runtime comment names an anchor project"


def test_the_tree_really_does_carry_project_names_in_docstrings():
    carriers = []
    for path in _runtime_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            if _names_in(ast.get_docstring(node) or ""):
                carriers.append(path.relative_to(RUNTIME_ROOT))
                break

    assert carriers, "no runtime docstring names an anchor project"


def test_the_replay_probe_label_is_the_only_annotation_case():
    """The one annotation-borne occurrence, pinned so it cannot quietly grow.

    A `Literal` is a value space, not a branch: pydantic rejects an unlisted
    label, and nothing in `src/sag` ever reads `ReplayHeader.probe`. If that
    changes, the comparison rule — not this test — is what catches it.
    """
    carriers = {}
    for path in _runtime_sources():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        annotated = _annotation_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in annotated and _names_in(node.value):
                carriers.setdefault(str(path.relative_to(RUNTIME_ROOT)), []).append(node.value)

    assert set(carriers) == {"agent/replay.py"}
    assert sorted(carriers["agent/replay.py"]) == ["bigtop", "tvm"]


def test_the_replay_probe_label_is_never_read_back():
    """Declared and unread — so it cannot condition anything today."""
    readers = []
    for path in _runtime_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "probe":
                readers.append(f"{path.relative_to(RUNTIME_ROOT)}:{node.lineno}")

    assert readers == []


# --------------------------------------------------------------------------- #
# 3) The guard is self-tested in both directions.
# --------------------------------------------------------------------------- #
ALLOWED_SOURCES = (
    pytest.param('"""Live bigtop: the reactor summary is a lie."""\n', id="module docstring"),
    pytest.param("def f():\n    '''Seen on tvm.'''\n    return 1\n", id="function docstring"),
    pytest.param("value = 1  # live bigtop burned 13 attempts here\n", id="trailing comment"),
    pytest.param("# commons-cli is the canary\nvalue = 1\n", id="standalone comment"),
    pytest.param(
        "from typing import Literal\n\nclass H:\n    probe: Literal['tvm', 'bigtop']\n",
        id="literal annotation",
    ),
    pytest.param("def f(x: 'tvm.Thing') -> 'bigtop.Thing':\n    return x\n", id="annotations"),
)


@pytest.mark.parametrize("source", ALLOWED_SOURCES)
def test_the_guard_allows_provenance_and_type_labels(source):
    assert scan_source(source) == []


FORBIDDEN_SOURCES = (
    pytest.param('if root == "bigtop":\n    pass\n', id="equality condition"),
    pytest.param('x = 1 if root == "tvm" else 2\n', id="conditional expression"),
    pytest.param('if "bigpetstore" in root:\n    pass\n', id="membership condition"),
    pytest.param('TABLE = {"commons-cli": 921}\n', id="dict key"),
    pytest.param('value = config["bigtop"]\n', id="subscript lookup"),
    pytest.param("def bigtop_reactor_fix():\n    pass\n", id="function name"),
    pytest.param("class TvmSmokePolicy:\n    pass\n", id="class name"),
    pytest.param("self.commons_cli_root = 1\n", id="attribute name"),
    pytest.param("def f(bigtop_root):\n    return bigtop_root\n", id="parameter name"),
    pytest.param('assert system == "tvm"\n', id="assert condition"),
    pytest.param('roots = [r for r in all_roots if r == "bigtop"]\n', id="comprehension guard"),
    pytest.param('flag = ready and system == "tvm"\n', id="boolean operand"),
    pytest.param("import tvm_policy\n", id="import name"),
)


@pytest.mark.parametrize("source", FORBIDDEN_SOURCES)
def test_the_guard_flags_name_conditioned_policy(source):
    assert scan_source(source), source


def test_the_guard_reports_where_and_why():
    (offender,) = scan_source('flag = (root == "bigtop")\n', label="mod.py")

    assert offender == "mod.py:1: comparison 'bigtop' in 'bigtop'"


def test_one_node_covered_by_two_rules_is_one_offence():
    """`if x == "bigtop"` is a condition AND a comparison — but one defect."""
    (offender,) = scan_source('if root == "bigtop":\n    pass\n', label="mod.py")

    assert offender == "mod.py:1: condition 'bigtop' in 'bigtop'"


def test_a_comment_cannot_smuggle_a_branch_past_the_guard():
    """The allowance is for the comment TEXT, not the line it sits on."""
    offenders = scan_source('if root == "bigtop":  # live bigtop anchor\n    pass\n')

    assert len(offenders) == 1
