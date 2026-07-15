"""Static guard against reintroducing legacy ToolResult truth fields."""

import ast
from pathlib import Path

from sag.tools.base import ToolResult

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "sag"
LEGACY_FIELDS = {"success", "status", "verdict"}


def _annotation_name(annotation: ast.expr | None) -> str | None:
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    if isinstance(annotation, ast.Subscript):
        return _annotation_name(annotation.value)
    return None


class _ToolResultLegacyVisitor(ast.NodeVisitor):
    def __init__(self, path: Path):
        self.path = path
        self.known_result_names: list[set[str]] = [set()]
        self.violations: list[str] = []

    def _record(self, node: ast.AST, field: str) -> None:
        relative = self.path.relative_to(SOURCE_ROOT.parent.parent)
        self.violations.append(f"{relative}:{node.lineno}: ToolResult.{field}")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        known = {
            arg.arg
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            if _annotation_name(arg.annotation) == "ToolResult"
        }
        self.known_result_names.append(known)
        self.generic_visit(node)
        self.known_result_names.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and _annotation_name(node.annotation) == "ToolResult":
            self.known_result_names[-1].add(node.target.id)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Call) and _annotation_name(node.value.func) == "ToolResult":
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.known_result_names[-1].add(target.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _annotation_name(node.func) == "ToolResult":
            for keyword in node.keywords:
                if keyword.arg in LEGACY_FIELDS:
                    self._record(keyword, keyword.arg)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            isinstance(node.ctx, ast.Load)
            and node.attr in LEGACY_FIELDS
            and isinstance(node.value, ast.Name)
            and any(node.value.id in scope for scope in reversed(self.known_result_names))
        ):
            self._record(node, node.attr)
        self.generic_visit(node)


def test_tool_result_has_only_canonical_truth_fields():
    assert LEGACY_FIELDS.isdisjoint(ToolResult.model_fields)
    assert not hasattr(ToolResult, "temporary_legacy_adapter")
    assert "temporary_legacy_adapter_marker" not in ToolResult.model_fields


def test_source_has_no_legacy_tool_result_truth_sites():
    violations = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        visitor = _ToolResultLegacyVisitor(path)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        violations.extend(visitor.violations)

    assert violations == []


def test_guard_flags_known_tool_result_reads_without_flagging_raw_dicts():
    source = """
def consume(result: ToolResult):
    return result.success

def consume_raw(result: dict):
    return result.get("success")
"""
    visitor = _ToolResultLegacyVisitor(SOURCE_ROOT / "_guard_fixture.py")
    visitor.visit(ast.parse(source))

    assert visitor.violations == ["src/sag/_guard_fixture.py:3: ToolResult.success"]
