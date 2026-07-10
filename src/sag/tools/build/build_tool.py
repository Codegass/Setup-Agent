"""build(action: deps|compile|test|package) — one tool over all ecosystems."""

import posixpath
import re
import shlex
from typing import Any, Dict, List, Optional

from sag.config.settings import DEFAULT_TEST_PASS_THRESHOLD
from sag.tools.base import BaseTool, ToolResult
from sag.tools.internal.build_preflight import (
    JdkPreflight,
    active_java_major,
    classify_version_error,
    read_build_requirements,
)

from .backends import BUILD_MARKERS, GradleBackend, MavenBackend, PythonBackend

# Verbs that actually invoke the JDK; `deps` resolution is not gated on a
# matching toolchain, so it skips the pre-flight (spec §1b: no-op when moot).
_PREFLIGHT_VERBS = ("compile", "test", "package", "install")


class BuildTool(BaseTool):
    def __init__(
        self,
        docker_orchestrator,
        maven_tool=None,
        gradle_tool=None,
        python_tool=None,
        test_pass_threshold: float = DEFAULT_TEST_PASS_THRESHOLD,
    ):
        super().__init__(
            name="build",
            description=(
                "Build the project: action = deps | compile | test | package. "
                "The build system (maven/gradle/python) is auto-selected from project files, "
                "and the CORRECT toolchain (registered Maven/JDK versions) is resolved "
                "automatically — bash mvn/gradle uses the stale system PATH and often picks "
                "the wrong version, even when project docs show a raw command. "
                "python: deps installs into ./.venv via the project's own tool "
                "(poetry/pipenv/pip ladder); test runs pytest once with JUnit XML. "
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
        if python_tool is not None:
            self._backends["python"] = PythonBackend(python_tool)

    def execute(
        self,
        action: str,
        args: Optional[str] = None,
        working_directory: str = "/workspace",
        timeout: Optional[int] = None,
    ) -> ToolResult:
        verb = (action or "").strip().lower()
        if verb not in ("deps", "compile", "test", "package", "install"):
            return ToolResult(
                success=False,
                output=f"Unknown build action: {action!r}",
                verdict="failed",
                error="invalid action",
                suggestions=["Use action= deps | compile | test | package | install"],
            )

        # Whether the caller scoped this invocation itself. PR #12's
        # orchestration layer owns working-directory injection, so the facade
        # never re-targets; explicitness only gates the [scope] warning below.
        explicitly_scoped = working_directory not in (None, "", "/workspace")

        system, checked = self._detect_system(working_directory)
        if system is None and working_directory in (None, "", "/workspace"):
            # Standard layout: clone creates /workspace/<repo>. The legacy
            # MavenTool probed the project subdirectory before giving up; the
            # facade must too, or build(action=...) without working_directory
            # always returns verdict=unknown.
            project_name = getattr(self.docker_orchestrator, "project_name", None)
            if project_name:
                candidate = f"/workspace/{project_name}"
                fallback_system, fallback_checked = self._detect_system(candidate)
                checked = checked + [f"{candidate}/{marker}" for marker in fallback_checked]
                if fallback_system is not None:
                    system = fallback_system
                    working_directory = candidate
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
                success=False,
                output=f"No backend for {system}",
                verdict="failed",
                error="backend unavailable",
            )

        # --- JDK pre-flight (spec §1b): check-and-fix, never a hard block ---
        # Routing by system: python skips the JDK pre-flight entirely.
        # PythonPreflight already runs inside python_tool.setup_env (the deps
        # verb), and the venv interpreter it provisions is what test/compile/
        # build invoke — running a facade-level pre-flight here would
        # double-provision. The python bounded retry likewise lives inside
        # python_tool (classify_python_version_error), not here.
        preamble_lines: List[str] = []
        jdk_retry_meta: Optional[Dict[str, Optional[str]]] = None
        outcome = None
        if verb in _PREFLIGHT_VERBS and system != "python":
            requirements = read_build_requirements(self.docker_orchestrator)
            outcome = JdkPreflight(self.docker_orchestrator).run(
                requirements.get("java_version"),
                source=requirements.get("java_version_source") or "unknown",
            )
            if outcome.narration:
                preamble_lines.append(outcome.narration)

            # [scope] semantics live HERE (single ownership): warn only when
            # the model explicitly narrows — a working_directory strictly
            # DEEPER than a healthy reactor's recommended build root, or a
            # Maven -pl module selection. -pl is a token match so
            # '-plugin'-shaped args never trip it.
            build_root = (requirements.get("build_root") or "").rstrip("/")
            scoped_deeper = (
                explicitly_scoped
                and requirements.get("root_shape") == "healthy_reactor"
                and build_root
                and (working_directory or "").rstrip("/").startswith(build_root + "/")
            )
            pl_scoped = system == "maven" and bool(
                re.search(r"(^|\s)-pl(\s|=)", args or "")
            )
            if scoped_deeper or pl_scoped:
                narrowed = (
                    working_directory if scoped_deeper else f"-pl selection ({args})"
                )
                preamble_lines.append(
                    f"[scope] {narrowed} is narrower than the recommended "
                    f"reactor root ({build_root or 'root'}) — sibling deps may be "
                    "unresolved; tests outside this module will not run"
                )

        inner = backend.run(verb, args, working_directory, timeout)

        # Bounded retry (spec §1c): a version-shaped failure means the JDK in
        # the error text is authoritative (static analysis cannot always see
        # it); re-provision from it and rerun EXACTLY once, never more.
        if outcome is not None and not inner.success:
            failure_text = "\n".join(t for t in (inner.output, inner.raw_output) if t)
            needed = classify_version_error(failure_text)
            active = outcome.active_version or active_java_major(self.docker_orchestrator)
            if needed and needed != active:
                retry_outcome = JdkPreflight(self.docker_orchestrator).run(
                    needed, source="build-error"
                )
                if retry_outcome.provisioned:
                    preamble_lines.append(
                        f"[pre-flight] build error requires Java {needed}, "
                        "re-provisioned, retry 1/1"
                    )
                    jdk_retry_meta = {"from": active, "to": needed}
                    inner = backend.run(verb, args, working_directory, timeout)

        return self._envelope(inner, system, verb, preamble_lines, jdk_retry_meta)

    def _detect_system(self, working_directory: str):
        checked = []
        for system, markers in BUILD_MARKERS.items():
            for marker in markers:
                checked.append(marker)
                marker_path = posixpath.join(working_directory, marker)
                probe = self.docker_orchestrator.execute_command(
                    f"test -f {shlex.quote(marker_path)} && echo exists || echo missing",
                    workdir=None,
                    timeout=30,
                )
                if "exists" in (probe.get("output") or ""):
                    return system, checked
        return None, checked

    def _envelope(
        self,
        inner: ToolResult,
        system: str,
        verb: str,
        preamble_lines: Optional[List[str]] = None,
        jdk_retry: Optional[Dict[str, Optional[str]]] = None,
    ) -> ToolResult:
        facts: Dict[str, Any] = {"system": system, "action": verb}
        verdict = (
            inner.verdict
            if inner.verdict in ("running", "skipped")
            else ("success" if inner.success else "failed")
        )
        stats = inner.test_stats
        if stats is not None:
            facts.update(
                executed=stats.executed,
                passed=stats.passed,
                failed=stats.failed,
                skipped=stats.skipped,
                pass_rate=stats.pass_rate,
            )
            if inner.success and stats.failed > 0:
                verdict = (
                    "partial" if stats.pass_rate >= self.test_pass_threshold * 100 else "failed"
                )
        # The narration is the feature (transparency-by-construction, spec
        # §§1b-1c, 3): whatever the pre-flight did — or could not do — must be
        # visible in the agent's observation, not just in host logs.
        preamble = ("\n".join(preamble_lines) + "\n") if preamble_lines else ""
        output = inner.output
        raw_output = inner.raw_output
        if preamble:
            output = preamble + (output or "")
            raw_output = preamble + (raw_output or "")
        metadata = dict(inner.metadata)
        if jdk_retry:
            metadata["jdk_retry"] = jdk_retry
        return ToolResult(
            success=inner.success,
            output=output,
            verdict=verdict,
            facts=facts,
            refs=list(inner.refs) + list(inner.evidence_refs),
            suggestions=inner.suggestions,
            error=inner.error,
            error_code=inner.error_code,
            metadata=metadata,
            test_stats=inner.test_stats,
            evidence_refs=inner.evidence_refs,
            raw_output=raw_output,
        )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["deps", "compile", "test", "package", "install"],
                    "description": "What to do; the build system is auto-selected. "
                    "Use install for a multi-module reactor whose modules depend on "
                    "siblings' built artifacts (shaded jars, code-gen).",
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
