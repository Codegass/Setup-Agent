"""build(action: deps|compile|test|package) — one tool over all ecosystems."""

import posixpath
import re
import shlex
from typing import Any, Dict, List, Optional

from sag.config.settings import DEFAULT_TEST_PASS_THRESHOLD
from sag.tools.base import BaseTool, ToolResult
from sag.tools.internal.build_preflight import (
    JdkPreflight,
    REQUIREMENTS_PATH,
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
        maven_version_requirement: Optional[str] = None,
    ) -> ToolResult:
        verb = (action or "").strip().lower()
        if verb not in ("deps", "compile", "test", "package", "install"):
            return ToolResult.completed_failure(
                output=f"Unknown build action: {action!r}",
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
            return ToolResult.completed(
                operation_outcome="unknown",
                evidence_status="unknown",
                output=(
                    f"No known build system marker found in {working_directory}. "
                    "This is a detection result, not ground truth."
                ),
                facts={"checked": checked},
                suggestions=[
                    f"Inspect the directory: search('file:{working_directory}', '.') or bash ls",
                    "If a wrapper script or build file exists deeper, cd there and retry",
                ],
            )

        backend = self._backends.get(system)
        if backend is None:
            return ToolResult.completed_failure(
                output=f"No backend for {system}",
                error="backend unavailable",
            )

        requirements = read_build_requirements(self.docker_orchestrator)
        effective_verb, island_context = self._effective_island_action(
            requested_verb=verb,
            system=system,
            working_directory=working_directory,
            requirements=requirements,
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
        if effective_verb != verb:
            preamble_lines.append(
                "[island] "
                f"requested {verb}; surveyed goal {island_context['manifest_goal']} "
                f"at {island_context['island_root']}; executing install"
            )
        if effective_verb in _PREFLIGHT_VERBS and system != "python":
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
            pl_scoped = system == "maven" and bool(re.search(r"(^|\s)-pl(\s|=)", args or ""))
            if scoped_deeper or pl_scoped:
                narrowed = working_directory if scoped_deeper else f"-pl selection ({args})"
                preamble_lines.append(
                    f"[scope] {narrowed} is narrower than the recommended "
                    f"reactor root ({build_root or 'root'}) — sibling deps may be "
                    "unresolved; tests outside this module will not run"
                )

        def _execute_backend():
            if system == "maven":
                return backend.execute(
                    effective_verb,
                    args,
                    working_directory,
                    timeout,
                    maven_version_requirement=maven_version_requirement,
                )
            return backend.execute(effective_verb, args, working_directory, timeout)

        actual_executions = [_execute_backend()]
        inner = actual_executions[-1].result

        # Bounded retry (spec §1c): a version-shaped failure means the JDK in
        # the error text is authoritative (static analysis cannot always see
        # it); re-provision from it and rerun EXACTLY once, never more.
        if outcome is not None and not inner.succeeded:
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
                    actual_executions.append(_execute_backend())
                    inner = actual_executions[-1].result

        return self._envelope(
            inner,
            system,
            verb,
            effective_verb,
            working_directory,
            island_context,
            preamble_lines,
            jdk_retry_meta,
        ).with_execution_trace(actual_executions)

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

    @staticmethod
    def _effective_island_action(
        *,
        requested_verb: str,
        system: str,
        working_directory: str,
        requirements: Dict[str, Any],
    ) -> tuple[str, Dict[str, str]]:
        """Apply the manifest's exact-island local-artifact policy.

        Only compile/package may be promoted, and only on a pathological
        aggregator at an exact surveyed island root. Tests and dependency
        probes retain the caller's action.
        """
        if requirements.get("root_shape") != "pathological_aggregator":
            return requested_verb, {}

        survey_root = str(
            ((requirements.get("survey") or {}).get("project_path") or "")
        ).strip()

        def normalized(path: Any) -> str:
            value = str(path or "").strip()
            if value and not value.startswith("/") and survey_root:
                value = posixpath.join(survey_root, value)
            return posixpath.normpath(value) if value else ""

        requested_root = normalized(working_directory)
        for raw_island in requirements.get("build_islands") or []:
            if not isinstance(raw_island, dict):
                continue
            island_root = normalized(raw_island.get("root"))
            island_system = str(raw_island.get("system") or "").strip().lower()
            goal = str(raw_island.get("goal") or "").strip()
            if island_root != requested_root or island_system != system:
                continue
            context = {
                "island_root": island_root,
                "manifest_goal": goal,
                "action_source": f"{REQUIREMENTS_PATH}#build_islands",
            }
            if requested_verb in {"compile", "package"} and goal.lower() in {
                "install",
                "publishtomavenlocal",
            }:
                return "install", context
            return requested_verb, context
        return requested_verb, {}

    def _envelope(
        self,
        inner: ToolResult,
        system: str,
        requested_verb: str,
        effective_verb: str,
        working_directory: str,
        island_context: Optional[Dict[str, str]] = None,
        preamble_lines: Optional[List[str]] = None,
        jdk_retry: Optional[Dict[str, Optional[str]]] = None,
    ) -> ToolResult:
        facts: Dict[str, Any] = {
            "system": system,
            "action": effective_verb,
            "requested_action": requested_verb,
            "effective_action": effective_verb,
        }
        if island_context:
            facts.update(
                {
                    "island_root": island_context["island_root"],
                    "manifest_goal": island_context["manifest_goal"],
                }
            )
        operation_outcome = inner.operation_outcome
        stats = inner.test_stats
        if stats is not None:
            facts.update(
                executed=stats.executed,
                passed=stats.passed,
                failed=stats.failed,
                skipped=stats.skipped,
                pass_rate=stats.pass_rate,
            )
            if inner.succeeded and stats.failed > 0:
                operation_outcome = (
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
        metadata.update(
            {
                "system": system,
                "working_directory": working_directory,
                "requested_action": requested_verb,
                "effective_action": effective_verb,
            }
        )
        if island_context:
            metadata.update(island_context)
        if jdk_retry:
            metadata["jdk_retry"] = jdk_retry
        payload = {
            "output": output,
            "facts": facts,
            "refs": list(inner.refs) + list(inner.evidence_refs),
            "suggestions": inner.suggestions,
            "error": inner.error,
            "error_code": inner.error_code,
            "metadata": metadata,
            "test_stats": inner.test_stats,
            "evidence_refs": inner.evidence_refs,
            "conflicts": inner.conflicts,
            "raw_output": raw_output,
            "raw_data": inner.raw_data,
        }
        for field_name in ("failure_signature", "error_tail_preview", "output_ref"):
            value = getattr(inner, field_name)
            if value:
                payload[field_name] = value
        if inner.invocation_status.value == "completed":
            return ToolResult.completed(
                operation_outcome=operation_outcome,
                evidence_status=inner.evidence_status,
                evidence_assessment=inner.evidence_assessment,
                **payload,
            )
        return ToolResult(
            invocation_status=inner.invocation_status,
            operation_outcome=operation_outcome,
            evidence_status=inner.evidence_status,
            evidence_assessment=inner.evidence_assessment,
            poll_ref=inner.poll_ref,
            **payload,
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
                "maven_version_requirement": {
                    "type": "string",
                    "description": (
                        "Maven-only constraint preserved across registration and retry "
                        "(for example '[3.9,)'). Never omit a detected requirement."
                    ),
                },
            },
            "required": ["action"],
        }
