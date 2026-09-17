"""The half of the Workbench's copy that is written in Python.

`webui/src/test/copy.test.ts` lints the string literals in `webui/src`. It
cannot see the other producer: the web API composes display strings of its own
— an evidence group's heading, the build facet's Tool and Command values — and
the browser prints them verbatim. Those strings are Workbench copy that happens
to be typed in Python, and a structural lint over TSX will never read them. So
this is the second leg of the same lint, and it asserts the two legs forbid the
same nine words.

It walks the syntax tree rather than grepping, because a docstring is not copy,
a comment is not copy, and an identifier is not copy. What it looks at is a
single-line string literal that reads like something a person would be shown:
it starts with a letter, and it has a space in it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: The sources that compose user-visible strings for the Workbench. `src/sag/web`
#: serves every screen; `src/sag/result_card` writes the seven rows the band,
#: the terminal and the report all print verbatim.
COPY_SOURCES = (REPO / "src" / "sag" / "web", REPO / "src" / "sag" / "result_card")

#: The same nine words as `constraints.md`, and the same nine as the TypeScript
#: leg — `test_both_legs_of_the_lint_forbid_the_same_words` is what keeps that true.
FORBIDDEN = (
    "sealed",
    "canonical",
    "claimed",
    "quarantined",
    "subject",
    "snapshot",
    "metrics-v2",
    "verdict-bearing",
    "promoting",
)

#: A raw reason code is a machine handle, not prose: `UNSEALED_DENOMINATOR`
#: beside its plain-English gloss is what makes an issue reportable.
_RAW_CODE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")

#: Something a person would be shown: opens with a letter, and has a space.
#: `"not_attempted"` and `"canonicalVerdict"` are field values and field names;
#: `"Sealed run inputs"` is a heading.
_PROSE = re.compile(r"^[A-Za-z][a-z].*\s")

_LINT = REPO / "webui" / "src" / "test" / "copy.test.ts"


def offending_word(text: str) -> str | None:
    """The first retired word in ``text``, or ``None``."""

    lowered = _RAW_CODE.sub(" ", text).lower()
    return next((word for word in FORBIDDEN if word in lowered), None)


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """Every string node that is a docstring, by identity."""

    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if ast.get_docstring(node, clean=False) is None:
            continue
        found.add(id(node.body[0].value))
    return found


def _visible_strings(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = _docstring_nodes(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings or "\n" in node.value:
            continue
        if _PROSE.match(node.value):
            found.append((node.lineno, node.value))
    return found


def _sources() -> list[Path]:
    return sorted(
        path
        for root in COPY_SOURCES
        for path in root.rglob("*.py")
        if "static" not in path.parts and "__pycache__" not in path.parts
    )


def test_the_python_half_of_the_workbench_never_says_a_retired_word():
    """A string the API composes is copy just as much as one written in TSX."""

    sources = _sources()
    assert len(sources) > 20, f"the copy lint found only {len(sources)} Python sources to read"

    scanned = 0
    offenders: list[str] = []
    for path in sources:
        for lineno, text in _visible_strings(path):
            scanned += 1
            word = offending_word(text)
            if word:
                offenders.append(f"{path.relative_to(REPO)}:{lineno} says {word!r}: {text}")

    assert scanned > 200, f"the lint read only {scanned} display strings; its rule stopped matching"
    assert not offenders, "retired vocabulary reaches a Workbench screen:\n" + "\n".join(offenders)


def test_a_raw_reason_code_is_exempt_but_the_sentence_beside_it_is_not():
    assert offending_word("UNSEALED_DENOMINATOR") is None
    assert offending_word("COMPARISON_SUBJECT_MISMATCH: the two runs covered different tests") is None
    assert offending_word("UNSEALED_DENOMINATOR: the sealed total is unknown") == "sealed"


def test_both_legs_of_the_lint_forbid_the_same_words():
    """One list, two languages. A word dropped on one side is a hole on that side."""

    source = _LINT.read_text(encoding="utf-8")
    _, _, rest = source.partition("export const FORBIDDEN = [")
    assert rest, f"{_LINT.name} no longer declares FORBIDDEN as an array literal"
    body, closed, _ = rest.partition("]")
    assert closed, f"{_LINT.name}'s FORBIDDEN array is not closed"
    mirror = tuple(re.findall(r'"([^"]+)"', body))
    assert mirror == FORBIDDEN, (
        "the browser lint and the Python lint forbid different words: "
        f"browser {mirror}, Python {FORBIDDEN}"
    )
