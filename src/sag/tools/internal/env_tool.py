"""Agent-facing tool for runtime environment overlays."""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from typing import Any, Optional

from sag.runtime.env_overlay import EnvOverlayStore

from ..base import BaseTool, ToolResult
from .toolchain_manager import (
    ToolchainManager,
    ToolVersionRequirement,
    record_registered_runtime,
)

_MAVEN_VERSION_RE = re.compile(r"(?:^|\n)\s*Apache Maven\s+([0-9]+(?:\.[0-9]+){0,3})\b")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MAVEN_RUNTIME_ROOT_GROUPS = (
    ("/workspace",),
    ("/tmp",),
    ("/opt", "/usr", "/bin", "/sbin"),
)


class EnvTool(BaseTool):
    """Manage agent-maintained runtime environment overlay entries."""

    def __init__(self, orchestrator: Any, store: Optional[EnvOverlayStore] = None):
        super().__init__(
            name="env",
            description=(
                "Manage runtime env overlay entries for tool executable paths, PATH prefixes, "
                "and environment variables. Use bash to download or install runtimes, then use "
                "env register after installation; Maven registration probes the executable and "
                "can enforce requirement before persistence. Use env activate before retrying a build. Use "
                "env block for exact executable/version negative evidence from build errors. Do "
                "not use env to edit project build files, and do not use env to install or "
                "download software."
            ),
        )
        self.store = store or EnvOverlayStore(orchestrator)

    def execute(
        self,
        action: str | dict[str, Any],
        tool: Optional[str] = None,
        executable: Optional[str] = None,
        version: Optional[str] = None,
        source: Optional[str] = None,
        env: Optional[dict[str, Any]] = None,
        path_prepend: Optional[list[str] | str] = None,
        activate: bool = False,
        requirement: Optional[str] = None,
        working_directory: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> ToolResult:
        """Execute an env overlay action."""
        params = self._normalize_request(
            action=action,
            tool=tool,
            executable=executable,
            version=version,
            source=source,
            env=env,
            path_prepend=path_prepend,
            activate=activate,
            requirement=requirement,
            working_directory=working_directory,
            reason=reason,
        )

        try:
            action_name = params["action"]
            if action_name == "inspect":
                overlay = self.store.inspect()
                return self._result("inspect", overlay)

            if action_name == "register":
                params["tool"] = self.store._normalize_tool(params["tool"])
                if params["tool"] == "maven":
                    canonical_executable, canonical_error = self._canonicalize_maven_executable(
                        params["executable"]
                    )
                    if canonical_error:
                        return canonical_error
                    params["executable"] = canonical_executable
                validation_error = self._validate_executable(params["executable"])
                if validation_error:
                    return validation_error
                measured_version: Optional[str] = None
                if params["tool"] == "maven":
                    measured_version, probe_error = self._probe_maven_runtime(
                        params["executable"],
                        requirement=params.get("requirement"),
                    )
                    if probe_error:
                        return probe_error
                    # A caller-provided version is only a claim.  Maven's own
                    # `-version` output is the registration fact persisted for
                    # both resolution and reporting.
                    params["version"] = measured_version
                activate_requested = bool(params.get("activate", False))
                overlay = self.store.register(
                    params["tool"],
                    params["executable"],
                    version=params.get("version"),
                    source=params.get("source", "agent_registered"),
                    env=params.get("env"),
                    path_prepend=params.get("path_prepend"),
                    activate=activate_requested,
                )
                active_candidate = (
                    self.store.active_candidate(
                        params["tool"],
                        overlay=overlay,
                    )
                    if activate_requested
                    else None
                )
                if activate_requested and (
                    active_candidate is None
                    or active_candidate.get("executable") != params["executable"]
                ):
                    return ToolResult.completed_failure(
                        output="",
                        error=(
                            "Runtime registration did not activate the requested executable: "
                            f"{params['executable']}"
                        ),
                        error_code="ENV_ACTIVATION_NOT_CONFIRMED",
                        suggestions=[
                            "Inspect the runtime overlay before retrying the build",
                            "Do not retry the stale executable while activation is unconfirmed",
                        ],
                        raw_data={
                            "action": "register",
                            "requested_executable": params["executable"],
                            "active_candidate": active_candidate,
                            "overlay": overlay,
                        },
                        metadata={"action": "register", "activation_confirmed": False},
                    )
                self._record_registered_runtime(
                    params["tool"],
                    params["executable"],
                    version=params.get("version"),
                )
                return self._result(
                    "register",
                    overlay,
                    active_candidate=active_candidate,
                    measured_version=measured_version,
                )

            if action_name == "activate":
                params["tool"] = self.store._normalize_tool(params["tool"])
                if params["tool"] == "maven":
                    canonical_executable, canonical_error = self._canonicalize_maven_executable(
                        params["executable"]
                    )
                    if canonical_error:
                        return canonical_error
                    params["executable"] = canonical_executable
                validation_error = self._validate_executable(params["executable"])
                if validation_error:
                    return validation_error
                overlay = self.store.activate(params["tool"], params["executable"])
                active_candidate = self.store.active_candidate(params["tool"])
                self._record_registered_runtime(
                    params["tool"],
                    params["executable"],
                    version=(active_candidate or {}).get("version"),
                )
                return self._result(
                    "activate",
                    overlay,
                    active_candidate=active_candidate,
                )

            if action_name == "block":
                params["tool"] = self.store._normalize_tool(params["tool"])
                overlay = self.store.block(
                    params["tool"],
                    params["executable"],
                    version=params.get("version"),
                    requirement=params.get("requirement"),
                    reason=params.get("reason"),
                    source=params.get("source", "build_error"),
                )
                return self._result("block", overlay)

            if action_name == "clear":
                overlay = self.store.clear(params.get("tool"))
                return self._result("clear", overlay)

            return ToolResult.completed_failure(
                output="",
                error=f"Invalid env action: {action_name}",
                error_code="ENV_INVALID_ACTION",
                suggestions=[
                    "Use one of: inspect, register, activate, block, clear.",
                ],
                raw_data={"action": action_name},
            )
        except KeyError as exc:
            return ToolResult.completed_failure(
                output="",
                error=f"Missing required env parameter: {exc.args[0]}",
                error_code="ENV_MISSING_PARAMETER",
                suggestions=[
                    "Provide tool and executable for register, activate, and block actions."
                ],
                raw_data={"action": params.get("action"), "missing": exc.args[0]},
            )
        except ValueError as exc:
            return ToolResult.completed_failure(
                output="",
                error=str(exc),
                error_code="ENV_VALIDATION_ERROR",
                suggestions=[
                    "Inspect the overlay and register a valid candidate before activating it."
                ],
                raw_data={"action": params.get("action")},
            )
        except Exception as exc:
            return ToolResult.completed_failure(
                output="",
                error=f"Env overlay operation failed: {exc}",
                error_code="ENV_OPERATION_FAILED",
                suggestions=[
                    "Check that the Docker workspace is writable and try the env action again."
                ],
                raw_data={"action": params.get("action")},
            )

    def _record_registered_runtime(
        self,
        tool: str,
        executable: str,
        *,
        version: Optional[str] = None,
    ) -> None:
        """Mirror one registration into the toolchain registry.

        The overlay is the execution consumer — the dispatch shell sources it.
        The registry is the toolchain state a dispatch's identity is taken
        over. Live polaris and camel-quarkus both registered a runtime and were
        then refused the very build that would have used it, because only the
        overlay moved. Registration writes both, and never fails on the second.
        """
        record_registered_runtime(
            getattr(self.store, "orchestrator", None),
            tool,
            executable,
            version=version,
            source="registered",
        )

    def _normalize_request(self, **kwargs: Any) -> dict[str, Any]:
        action = kwargs.pop("action")
        if isinstance(action, dict):
            params = {key: value for key, value in action.items() if value is not None}
        else:
            params = {"action": action}
            params.update({key: value for key, value in kwargs.items() if value is not None})

        if "action" not in params or not str(params["action"]).strip():
            raise ValueError("action is required")
        params["action"] = str(params["action"]).strip().lower()
        return params

    def _canonicalize_maven_executable(
        self,
        executable: str,
    ) -> tuple[Optional[str], Optional[ToolResult]]:
        """Resolve one Maven executable to a stable, trusted container path."""
        requested = str(executable or "").strip()
        if not requested or not posixpath.isabs(requested):
            return None, ToolResult.completed_failure(
                output="",
                error="Maven executable must be an absolute container path",
                error_code="ENV_EXECUTABLE_PATH_NOT_ABSOLUTE",
                suggestions=[
                    "Provide the full container path to the downloaded distribution's bin/mvn."
                ],
                raw_data={"executable": requested, "tool": "maven"},
            )

        normalized_requested = posixpath.normpath(requested)
        requested_group = self._runtime_root_group(normalized_requested)
        if requested_group is None:
            return None, ToolResult.completed_failure(
                output="",
                error=(
                    "Maven executable is outside the allowed container runtime roots: "
                    f"{normalized_requested}"
                ),
                error_code="ENV_EXECUTABLE_PATH_OUTSIDE_RUNTIME_ROOTS",
                suggestions=[
                    "Install Maven under /workspace, /tmp, /opt, or a system /usr path, "
                    "then register its exact bin/mvn."
                ],
                raw_data={"executable": normalized_requested, "tool": "maven"},
            )

        orchestrator = getattr(self.store, "orchestrator", None)
        if orchestrator is None or not hasattr(orchestrator, "execute_command"):
            return None, ToolResult.completed_failure(
                output="",
                error="Cannot resolve the Maven executable realpath without a runtime executor",
                error_code="ENV_EXECUTABLE_REALPATH_UNAVAILABLE",
                suggestions=["Retry through project(action='env') in an active SAG container."],
                raw_data={"executable": normalized_requested, "tool": "maven"},
            )

        resolved = orchestrator.execute_command(
            f"realpath -e -- {shlex.quote(normalized_requested)}",
            timeout=30,
        )
        resolved_lines = [
            line.strip() for line in str(resolved.get("output") or "").splitlines() if line.strip()
        ]
        if (
            resolved.get("exit_code") != 0
            or resolved.get("success") is False
            or len(resolved_lines) != 1
            or not posixpath.isabs(resolved_lines[0])
        ):
            # Preserve the more actionable pre-existing error for a path that
            # simply does not exist or is not executable.  A path that passes
            # that check but cannot be canonicalized remains a distinct
            # fail-closed realpath error.
            validation_error = self._validate_executable(normalized_requested)
            if validation_error:
                return None, validation_error
            return None, ToolResult.completed_failure(
                output=str(resolved.get("output") or ""),
                error=f"Could not resolve an exact Maven executable realpath: {normalized_requested}",
                error_code="ENV_EXECUTABLE_REALPATH_FAILED",
                suggestions=[
                    "Verify the absolute path exists and resolves to one executable before "
                    "registering it."
                ],
                raw_data={
                    "executable": normalized_requested,
                    "tool": "maven",
                    "realpath_exit_code": resolved.get("exit_code"),
                },
            )

        canonical = posixpath.normpath(resolved_lines[0])
        if not self._path_in_roots(canonical, requested_group):
            return None, ToolResult.completed_failure(
                output="",
                error=(
                    "Maven executable realpath escaped its trusted runtime root: "
                    f"{normalized_requested} -> {canonical}"
                ),
                error_code="ENV_EXECUTABLE_REALPATH_ESCAPE",
                suggestions=[
                    "Register a Maven executable whose symlink target remains in the same "
                    "trusted runtime root."
                ],
                raw_data={
                    "executable": normalized_requested,
                    "resolved_executable": canonical,
                    "tool": "maven",
                },
            )

        if posixpath.basename(canonical) != "mvn":
            return None, ToolResult.completed_failure(
                output="",
                error=f"Canonical Maven executable must be named mvn: {canonical}",
                error_code="ENV_MAVEN_EXECUTABLE_NAME_MISMATCH",
                suggestions=["Register the distribution's exact canonical bin/mvn path."],
                raw_data={
                    "executable": normalized_requested,
                    "resolved_executable": canonical,
                    "tool": "maven",
                },
            )
        return canonical, None

    def _runtime_root_group(self, path: str) -> Optional[tuple[str, ...]]:
        return next(
            (roots for roots in _MAVEN_RUNTIME_ROOT_GROUPS if self._path_in_roots(path, roots)),
            None,
        )

    def _path_in_roots(self, path: str, roots: tuple[str, ...]) -> bool:
        return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)

    def _validate_executable(self, executable: str) -> Optional[ToolResult]:
        orchestrator = getattr(self.store, "orchestrator", None)
        if orchestrator is None or not hasattr(orchestrator, "execute_command"):
            return None

        result = orchestrator.execute_command(
            f"test -x {shlex.quote(executable)} && echo EXISTS || echo MISSING"
        )
        output = result.get("output") or ""
        if result.get("exit_code") == 0 and "EXISTS" in output:
            return None

        return ToolResult.completed_failure(
            output="",
            error=f"Env overlay executable is not executable or does not exist: {executable}",
            error_code="ENV_EXECUTABLE_NOT_FOUND",
            suggestions=[
                "Use bash to verify the exact installed executable path before registering it.",
                "For downloaded runtimes, register the actual bin executable path under /workspace, /opt, /tmp, or /usr/local.",
                "Use env inspect to review the current active candidate before retrying a build.",
            ],
            raw_data={"executable": executable},
            metadata={"action": "validate_executable"},
        )

    def _probe_maven_runtime(
        self,
        executable: str,
        *,
        requirement: Optional[str],
    ) -> tuple[Optional[str], Optional[ToolResult]]:
        """Prove Maven identity/version before mutating the shared overlay."""
        orchestrator = getattr(self.store, "orchestrator", None)
        if orchestrator is None or not hasattr(orchestrator, "execute_command"):
            return None, ToolResult.completed_failure(
                output="",
                error="Cannot verify Maven without a runtime command executor",
                error_code="ENV_RUNTIME_PROBE_UNAVAILABLE",
                suggestions=["Retry through project(action='env') in an active SAG container."],
                raw_data={"executable": executable, "tool": "maven"},
            )

        probe = orchestrator.execute_command(
            f"{shlex.quote(executable)} -version",
            timeout=30,
        )
        output = probe.get("output") or ""
        if probe.get("exit_code") != 0 or probe.get("success") is False:
            return None, ToolResult.completed_failure(
                output=output,
                error=f"Maven runtime probe failed for {executable}",
                error_code="ENV_RUNTIME_PROBE_FAILED",
                suggestions=[
                    "Run the exact executable with -version and fix its runtime dependencies "
                    "before registering it."
                ],
                raw_data={
                    "executable": executable,
                    "tool": "maven",
                    "probe_exit_code": probe.get("exit_code"),
                },
            )

        match = _MAVEN_VERSION_RE.search(_ANSI_ESCAPE_RE.sub("", output))
        if not match:
            return None, ToolResult.completed_failure(
                output=output,
                error=f"Executable did not identify itself as Apache Maven: {executable}",
                error_code="ENV_RUNTIME_IDENTITY_MISMATCH",
                suggestions=[
                    "Register the distribution's exact bin/mvn executable, not a similarly "
                    "named script or archive."
                ],
                raw_data={"executable": executable, "tool": "maven"},
            )

        measured_version = match.group(1)
        explicit_requirement = ToolVersionRequirement.from_raw(
            requirement,
            source="tool_parameter",
        )
        observed_requirements = [
            ToolVersionRequirement.from_raw(
                record.get("raw"),
                source="registered_state",
            )
            # Registration changes a process-wide active overlay.  A caller
            # therefore cannot narrow persisted constraints by supplying an
            # arbitrary working directory; the candidate must satisfy every
            # observed Maven contract.  Build-time resolution remains scoped.
            for record in self.store.observed_requirements("maven")
        ]
        requirements = []
        for candidate_requirement in [*observed_requirements, explicit_requirement]:
            if candidate_requirement and candidate_requirement.raw not in {
                item.raw for item in requirements
            }:
                requirements.append(candidate_requirement)

        manager = ToolchainManager(orchestrator)
        failed_requirement = next(
            (
                candidate_requirement
                for candidate_requirement in requirements
                if not manager.matches_requirement(measured_version, candidate_requirement)
            ),
            None,
        )
        if failed_requirement:
            return None, ToolResult.completed_failure(
                output=output,
                error=(
                    f"Measured Maven {measured_version} does not satisfy "
                    f"{failed_requirement.raw}"
                ),
                error_code="ENV_RUNTIME_REQUIREMENT_MISMATCH",
                suggestions=[
                    "Download a Maven distribution satisfying the same requirement; do not "
                    "weaken or omit the requirement."
                ],
                raw_data={
                    "executable": executable,
                    "tool": "maven",
                    "measured_version": measured_version,
                    "requirement": failed_requirement.raw,
                    "requirement_source": failed_requirement.source,
                },
            )
        return measured_version, None

    def _result(
        self,
        action: str,
        overlay: dict[str, Any],
        *,
        active_candidate: Optional[dict[str, Any]] = None,
        measured_version: Optional[str] = None,
    ) -> ToolResult:
        raw_data: dict[str, Any] = {"action": action, "overlay": overlay}
        if active_candidate is not None:
            raw_data["active_candidate"] = active_candidate
        if measured_version is not None:
            raw_data["measured_version"] = measured_version
        return ToolResult.completed_success(
            output=json.dumps(raw_data, indent=2, sort_keys=True),
            raw_data=raw_data,
            metadata={"action": action},
        )

    def _get_parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["inspect", "register", "activate", "block", "clear"],
                    "description": "Env overlay action to perform.",
                },
                "tool": {
                    "type": "string",
                    "description": "Tool name, such as maven.",
                },
                "executable": {
                    "type": "string",
                    "description": (
                        "Exact executable path to register, activate, or block. Maven register "
                        "and activate require an absolute container path and persist its "
                        "canonical realpath."
                    ),
                },
                "version": {
                    "type": "string",
                    "description": "Observed executable version.",
                },
                "source": {
                    "type": "string",
                    "description": (
                        "Evidence source for the overlay entry. Defaults to "
                        "agent_registered for register and build_error for block."
                    ),
                },
                "env": {
                    "type": "object",
                    "description": "Environment variables to export when this candidate is active.",
                },
                "path_prepend": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "PATH entries to prepend when this candidate is active.",
                },
                "activate": {
                    "type": "boolean",
                    "description": "Activate the candidate during register.",
                    "default": False,
                },
                "requirement": {
                    "type": "string",
                    "description": (
                        "For register, a version requirement the measured runtime must satisfy; "
                        "for block, the requirement the executable failed to satisfy."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "Human-readable reason for a block entry.",
                },
            },
            "required": ["action"],
        }
