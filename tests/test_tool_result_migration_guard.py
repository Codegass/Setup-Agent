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
        self.known_result_paths: list[set[str]] = [set()]
        self.known_factory_paths: list[set[str]] = [set()]
        self.violations: list[str] = []

    @staticmethod
    def _expression_path(node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = _ToolResultLegacyVisitor._expression_path(node.value)
            return f"{parent}.{node.attr}" if parent else None
        if isinstance(node, ast.Subscript):
            parent = _ToolResultLegacyVisitor._expression_path(node.value)
            if parent:
                return f"{parent}[{ast.dump(node.slice, include_attributes=False)}]"
        return None

    @staticmethod
    def _is_tool_result_factory(node: ast.AST | None) -> bool:
        if isinstance(node, ast.Name):
            return node.id == "ToolResult"
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "ToolResult"
            and node.attr in {"completed", "completed_success", "completed_failure"}
        )

    def _known_in_scopes(self, path: str | None, scopes: list[set[str]]) -> bool:
        return path is not None and any(path in scope for scope in reversed(scopes))

    def _is_factory_expression(self, node: ast.AST | None) -> bool:
        return self._is_tool_result_factory(node) or self._known_in_scopes(
            self._expression_path(node), self.known_factory_paths
        )

    def _is_result_expression(self, node: ast.AST | None) -> bool:
        if self._known_in_scopes(self._expression_path(node), self.known_result_paths):
            return True
        return isinstance(node, ast.Call) and self._is_factory_expression(node.func)

    @staticmethod
    def _scope_function_defs(
        statements: list[ast.stmt],
    ) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
        return [
            statement
            for statement in statements
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]

    def _function_returns_result(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        factory_names: set[str],
    ) -> bool:
        result_names = {
            arg.arg
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            if _annotation_name(arg.annotation) == "ToolResult"
        }
        factory_aliases = set(factory_names)

        def is_factory(expression: ast.AST | None) -> bool:
            return self._is_tool_result_factory(expression) or (
                isinstance(expression, ast.Name) and expression.id in factory_aliases
            )

        def is_result(expression: ast.AST | None) -> bool:
            return (isinstance(expression, ast.Name) and expression.id in result_names) or (
                isinstance(expression, ast.Call) and is_factory(expression.func)
            )

        class _ReturnFlow(ast.NodeVisitor):
            returns_result = False

            def visit_FunctionDef(self, inner: ast.FunctionDef) -> None:
                return None

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, inner: ast.ClassDef) -> None:
                return None

            def visit_Assign(self, inner: ast.Assign) -> None:
                self.generic_visit(inner.value)
                for target in inner.targets:
                    if isinstance(target, ast.Name):
                        if is_result(inner.value):
                            result_names.add(target.id)
                        elif is_factory(inner.value):
                            factory_aliases.add(target.id)

            def visit_AnnAssign(self, inner: ast.AnnAssign) -> None:
                self.generic_visit(inner.value) if inner.value is not None else None
                if isinstance(inner.target, ast.Name):
                    if _annotation_name(inner.annotation) == "ToolResult" or is_result(inner.value):
                        result_names.add(inner.target.id)

            def visit_Return(self, inner: ast.Return) -> None:
                if is_result(inner.value):
                    self.returns_result = True

        flow = _ReturnFlow()
        for statement in node.body:
            flow.visit(statement)
        return flow.returns_result

    def _register_scope_factories(self, statements: list[ast.stmt]) -> None:
        functions = self._scope_function_defs(statements)
        factory_names = {
            function.name
            for function in functions
            if _annotation_name(function.returns) == "ToolResult"
        }
        changed = True
        while changed:
            changed = False
            for function in functions:
                if function.name not in factory_names and self._function_returns_result(
                    function, factory_names
                ):
                    factory_names.add(function.name)
                    changed = True
        self.known_factory_paths[-1].update(factory_names)

    def _record(self, node: ast.AST, field: str) -> None:
        relative = self.path.relative_to(SOURCE_ROOT.parent.parent)
        self.violations.append(f"{relative}:{node.lineno}: ToolResult.{field}")

    def visit_Module(self, node: ast.Module) -> None:
        self._register_scope_factories(node.body)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        known = {
            arg.arg
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            if _annotation_name(arg.annotation) == "ToolResult"
        }
        self.known_result_paths.append(known)
        self.known_factory_paths.append(set())
        self._register_scope_factories(node.body)
        for statement in node.body:
            self.visit(statement)
        self.known_factory_paths.pop()
        self.known_result_paths.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.generic_visit(node)
        target_path = self._expression_path(node.target)
        if _annotation_name(node.annotation) == "ToolResult" or self._is_result_expression(
            node.value
        ):
            if target_path:
                self.known_result_paths[-1].add(target_path)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        for target in node.targets:
            target_path = self._expression_path(target)
            if target_path and self._is_result_expression(node.value):
                self.known_result_paths[-1].add(target_path)
            elif target_path and self._is_factory_expression(node.value):
                self.known_factory_paths[-1].add(target_path)

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_factory_expression(node.func):
            for keyword in node.keywords:
                if keyword.arg in LEGACY_FIELDS:
                    self._record(keyword, keyword.arg)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            isinstance(node.ctx, ast.Load)
            and node.attr in LEGACY_FIELDS
            and self._is_result_expression(node.value)
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

def make_result():
    return ToolResult.completed_success(output="ok")

def escapes():
    factory_result = ToolResult.completed_failure(output="failed")
    alias = factory_result
    holder.result = alias
    return (
        factory_result.status,
        alias.verdict,
        make_result().success,
        ToolResult.completed_success(output="ok").verdict,
        holder.result.success,
    )

def consume_raw(result: dict):
    return result.get("success")
"""
    visitor = _ToolResultLegacyVisitor(SOURCE_ROOT / "_guard_fixture.py")
    visitor.visit(ast.parse(source))

    assert visitor.violations == [
        "src/sag/_guard_fixture.py:3: ToolResult.success",
        "src/sag/_guard_fixture.py:13: ToolResult.status",
        "src/sag/_guard_fixture.py:14: ToolResult.verdict",
        "src/sag/_guard_fixture.py:15: ToolResult.success",
        "src/sag/_guard_fixture.py:16: ToolResult.verdict",
        "src/sag/_guard_fixture.py:17: ToolResult.success",
    ]
