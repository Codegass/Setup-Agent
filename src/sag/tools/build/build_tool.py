"""build(action: deps|compile|test|package) — one tool over all ecosystems."""

from typing import Any, Dict, Optional

from sag.config.settings import DEFAULT_TEST_PASS_THRESHOLD
from sag.tools.base import BaseTool, ToolResult

from .backends import BUILD_MARKERS, GradleBackend, MavenBackend


class BuildTool(BaseTool):
    def __init__(self, docker_orchestrator, maven_tool=None, gradle_tool=None,
                 test_pass_threshold: float = DEFAULT_TEST_PASS_THRESHOLD):
        super().__init__(
            name="build",
            description=(
                "Build the project: action = deps | compile | test | package. "
                "The build system (maven/gradle) is auto-selected from project files. "
                "Long builds run detached and hand back a log ref — never killed."
            ),
        )
        self.docker_orchestrator = docker_orchestrator
        self.test_pass_threshold = test_pass_threshold
        self._backends = {}
        if maven_tool is not None:
            self._backends["maven"] = MavenBackend(maven_tool)
        if gradle_tool is not None:
            self._backends["gradle"] = GradleBackend(gradle_tool)

    def execute(self, action: str, args: Optional[str] = None,
                working_directory: str = "/workspace",
                timeout: Optional[int] = None) -> ToolResult:
        verb = (action or "").strip().lower()
        if verb not in ("deps", "compile", "test", "package"):
            return ToolResult(
                success=False, output=f"Unknown build action: {action!r}", verdict="failed",
                error="invalid action",
                suggestions=["Use action= deps | compile | test | package"],
            )

        system, checked = self._detect_system(working_directory)
        if system is None:
            return ToolResult(
                success=False,
                output=(
                    f"No known build system marker found in {working_directory}. "
                    "This is a detection result, not ground truth."
                ),
                verdict="unknown",
                facts={"checked": checked},
                suggestions=[
                    f"Inspect the directory: search('file:{working_directory}', '.') or bash ls",
                    "If a wrapper script or build file exists deeper, cd there and retry",
                ],
            )

        backend = self._backends.get(system)
        if backend is None:
            return ToolResult(
                success=False, output=f"No backend for {system}", verdict="failed",
                error="backend unavailable",
            )

        inner = backend.run(verb, args, working_directory, timeout)
        return self._envelope(inner, system, verb)

    def _detect_system(self, working_directory: str):
        checked = []
        for system, markers in BUILD_MARKERS.items():
            for marker in markers:
                checked.append(marker)
                probe = self.docker_orchestrator.execute_command(
                    f"test -f {working_directory}/{marker} && echo exists || echo missing",
                    workdir=None, timeout=30,
                )
                if "exists" in (probe.get("output") or ""):
                    return system, checked
        return None, checked

    def _envelope(self, inner: ToolResult, system: str, verb: str) -> ToolResult:
        facts: Dict[str, Any] = {"system": system, "action": verb}
        verdict = inner.verdict if inner.verdict in ("running", "skipped") else (
            "success" if inner.success else "failed"
        )
        stats = inner.test_stats
        if stats is not None:
            facts.update(
                executed=stats.executed, passed=stats.passed,
                failed=stats.failed, skipped=stats.skipped, pass_rate=stats.pass_rate,
            )
            if inner.success and stats.failed > 0:
                verdict = (
                    "partial"
                    if stats.pass_rate >= self.test_pass_threshold * 100
                    else "failed"
                )
        return ToolResult(
            success=inner.success,
            output=inner.output,
            verdict=verdict,
            facts=facts,
            refs=list(inner.refs) + list(inner.evidence_refs),
            suggestions=inner.suggestions,
            error=inner.error,
            error_code=inner.error_code,
            metadata=inner.metadata,
            test_stats=inner.test_stats,
            evidence_refs=inner.evidence_refs,
            raw_output=inner.raw_output,
        )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["deps", "compile", "test", "package"],
                    "description": "What to do; the build system is auto-selected",
                },
                "args": {
                    "type": "string",
                    "description": "Extra flags passed through to the underlying tool",
                },
                "working_directory": {"type": "string", "default": "/workspace"},
                "timeout": {
                    "type": "integer",
                    "description": "Soft window in seconds; long builds detach, never killed",
                },
            },
            "required": ["action"],
        }
