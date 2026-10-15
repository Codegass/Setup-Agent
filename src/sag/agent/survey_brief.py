"""The survey facts as a few lines of the analyze brief.

The harness survey runs at analyze entry and records a compact ``survey_facts``
dict on the trunk's ``environment_summary``. This module renders that dict as
deterministic lines, each prefixed ``Survey: `` so the bounded phase contract
can keep them when it has to drop lines. It reads nothing and runs nothing:
the lines restate what the survey already observed, so the model need not
re-read the build file, README and CI configuration to learn the same facts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, List

SURVEY_LINE_PREFIX = "Survey: "
MAX_SURVEY_BRIEF_CHARS = 900

_CLOSING_LINE = (
    "Survey: these are observed facts recorded by the harness, not a plan or a runner choice."
)
# Per-line cap so one long fact cannot crowd every later line out of the budget.
_LINE_CAP = 300
# A fact line cut below this length says too little to be worth keeping.
_MIN_CUT_LINE = 40
# Line positions (structure, java, tests, dependencies, commands) in the order
# the 900-character budget is spent.
_BUDGET_ORDER = (0, 1, 2, 4, 3)

_SHAPE_TEXT = {
    "single_module": "single module",
    "healthy_reactor": "multi-module root",
    "pathological_aggregator": "the project root only aggregates modules",
}


def _text(value: Any, limit: int = 256) -> str:
    """One-line, bounded text for a fact value (newlines never open a line)."""
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _has(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (Mapping, list, tuple)):
        return bool(value)
    return True


def _items(value: Any, *, first: int, limit: int) -> List[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [_text(item, limit) for item in list(value)[:first] if _has(item)]


def _total(facts: Mapping[str, Any], key: str, shown: int) -> int:
    total = facts.get(f"{key}_total")
    if isinstance(total, int) and not isinstance(total, bool) and total >= shown:
        return total
    raw = facts.get(key)
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return max(len(raw), shown)
    return shown


def _listed(names: List[str], total: int) -> str:
    return ", ".join(names) + (", …" if total > len(names) else "")


def _structure_line(facts: Mapping[str, Any]) -> str:
    parts = []
    if _has(facts.get("project_type")):
        parts.append(f"project type {_text(facts['project_type'], 60)}")
    if _has(facts.get("build_system")):
        build = f"build system {_text(facts['build_system'], 60)}"
        build_root = facts.get("build_root")
        if _has(build_root):
            build += f" at {_text(build_root, 160)}"
        shape = _SHAPE_TEXT.get(str(facts.get("root_shape") or ""))
        if shape is None and isinstance(facts.get("is_multi_module"), bool):
            shape = "multi-module" if facts["is_multi_module"] else "single module"
        modules = _items(facts.get("modules"), first=8, limit=40)
        structure = [shape] if shape else []
        if modules:
            total = _total(facts, "modules", len(modules))
            structure.append(f"{total} modules: {_listed(modules, total)}")
        if structure:
            build += f" ({'; '.join(structure)})"
        parts.append(build)
        test_root = facts.get("test_root")
        if _has(test_root) and str(test_root).rstrip("/") != str(build_root or "").rstrip("/"):
            parts.append(f"tests at {_text(test_root, 160)}")
    files = _items(facts.get("existing_files"), first=16, limit=40)
    if files:
        parts.append(f"files found: {', '.join(files)}")
    return "; ".join(parts)


def _java_line(facts: Mapping[str, Any]) -> str:
    parts = []
    if _has(facts.get("java_version")):
        java = f"java version in build config: {_text(facts['java_version'], 40)}"
        meta = []
        if _has(facts.get("java_version_source")):
            meta.append(f"source {_text(facts['java_version_source'], 40)}")
        enforced = facts.get("java_version_enforced")
        if isinstance(enforced, bool):
            meta.append(f"enforced: {'yes' if enforced else 'no'}")
        if meta:
            java += f" ({'; '.join(meta)})"
        parts.append(java)
    documentation = facts.get("documentation")
    if isinstance(documentation, Mapping) and _has(documentation.get("java_version_requirement")):
        requirement = _text(documentation["java_version_requirement"], 60)
        if not requirement.lower().startswith(("java", "jdk")):
            requirement = f"Java {requirement}"
        parts.append(f"documented requirement: {requirement}")
    return "; ".join(parts)


def _test_line(facts: Mapping[str, Any]) -> str:
    parts = []
    if _has(facts.get("test_framework")):
        parts.append(f"test framework {_text(facts['test_framework'], 60)}")
    count = facts.get("static_test_count")
    if not _has(count):
        count = facts.get("method_count")
    repeated = facts.get("parameterized") is True
    if _has(count):
        counted = f"{_text(count, 20)} test method annotations"
        if repeated:
            counted += " (parameterized tests run more than once)"
        parts.append(counted)
    elif repeated:
        parts.append("parameterized tests run more than once")
    return "; ".join(parts)


def _dependency_line(facts: Mapping[str, Any]) -> str:
    dependencies = _items(facts.get("dependencies"), first=5, limit=60)
    if not dependencies:
        return ""
    total = _total(facts, "dependencies", len(dependencies))
    return f"dependencies {total} ({_listed(dependencies, total)})"


def _command_line(facts: Mapping[str, Any]) -> str:
    documentation = facts.get("documentation")
    if not isinstance(documentation, Mapping):
        return ""
    parts = []
    for key, label in (("build_commands", "build"), ("test_commands", "test")):
        commands = _items(documentation.get(key), first=3, limit=120)
        if commands:
            parts.append(f"documented {label} commands: {', '.join(commands)}")
    return "; ".join(parts)


def _cut(line: str, limit: int) -> str:
    return line if len(line) <= limit else line[: limit - 1] + "…"


def render_survey_brief(environment_summary: Any) -> List[str]:
    """The survey facts as ``Survey: `` lines, at most 900 characters in all.

    Returns ``[]`` when the trunk carries no ``survey_facts``. A line whose
    facts are missing is left out; lines that do not fit are cut at the end
    (with ``…``) or dropped, so the closing line always survives.
    """
    if not isinstance(environment_summary, Mapping):
        return []
    facts = environment_summary.get("survey_facts")
    if not isinstance(facts, Mapping) or not facts:
        return []
    renders = (_structure_line, _java_line, _test_line, _dependency_line, _command_line)
    fact_lines = {
        position: _cut(SURVEY_LINE_PREFIX + body, _LINE_CAP)
        for position, body in enumerate(render(facts) for render in renders)
        if body
    }
    if not fact_lines:
        return []
    # The budget is spent in order of use to the model (documented commands
    # before the dependency sample); the lines are printed in their own order.
    used = len(_CLOSING_LINE)
    kept = {}
    for position in sorted(fact_lines, key=_BUDGET_ORDER.index):
        line = fact_lines[position]
        room = MAX_SURVEY_BRIEF_CHARS - used - 1
        if len(line) > room:
            if room < _MIN_CUT_LINE:
                continue
            line = _cut(line, room)
        kept[position] = line
        used += len(line) + 1
    return [*(kept[position] for position in sorted(kept)), _CLOSING_LINE]
