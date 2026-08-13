"""Agent-facing tool for runtime environment overlays."""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from sag.agent.loop_memory import COMPLETION_CLAIM_CAP
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

# Standing advice, true of every refused path and never a move that can end
# the wall on its own — which is why the bound carries the productive moves
# separately.
_GENERIC_REGISTRATION_MOVES = (
    "Use bash to verify the exact installed executable path before registering it.",
    "For downloaded runtimes, register the actual bin executable path under /workspace, /opt, /tmp, or /usr/local.",
    "Use env inspect to review the current active candidate before retrying a build.",
)
_EXECUTABLE_NOT_FOUND = "ENV_EXECUTABLE_NOT_FOUND"
_REFUSAL_BOUND_REACHED = "ENV_REFUSAL_BOUND_REACHED"
# The typed facts this tool states about its own recurrence. The engine is the
# only writer of run evidence, so the tool never records the blocker itself; it
# reports what it observed and the engine decides what the ledger says.
MATERIAL_RECURRENCE_BOUND_MARKER = "material_recurrence_bound"
MATERIAL_RECURRENCE_RELEASE_MARKER = "material_recurrence_released"
# D2 2026-08-12: rocketmq-externals refused ~689 times across three Maven paths
# while the provision route rendered 5 times — the advice fires and the model
# continues, so the missing rung is not more guidance but an end to the
# repetition.  Three is `LoopMemory.completion_claim_cap`'s existing cap, reused
# rather than a second invented number
# (docs/superpowers/specs/2026-08-13-material-recurrence-bound-design.md §3).
_MATERIAL_RECURRENCE_BOUND = COMPLETION_CLAIM_CAP


@dataclass
class _RefusalChain:
    """One `(tool, error_code)` identity's refusals inside a single run.

    Identity is deliberately NOT the executable path: rocketmq cycled three
    Maven paths for one tool, and a path-keyed bound would never have fired.
    """

    tool: str
    count: int = 0
    paths: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    moves: tuple[str, ...] = ()

    @property
    def bound_reached(self) -> bool:
        return self.count >= _MATERIAL_RECURRENCE_BOUND

    def marker(self) -> dict[str, Any]:
        """The structured fact, never pre-rendered prose: the engine renders."""
        return {
            "tool": self.tool,
            "error_code": _EXECUTABLE_NOT_FOUND,
            "refused_executables": list(self.paths),
            "remaining_moves": list(self.moves),
            "refusal_count": self.count,
            "bound": _MATERIAL_RECURRENCE_BOUND,
            "evidence_refs": list(self.evidence_refs),
        }

    def observe(self, executable: str, moves: tuple[str, ...]) -> None:
        self.count += 1
        if executable and not self.already_refused(executable):
            self.paths.append(executable)
        # The last refusal's moves are the current ones: the overlay and the
        # workspace can both change under a run.
        self.moves = moves

    def already_refused(self, executable: str) -> bool:
        """A trailing or doubled slash is the same path, not a new probe."""
        candidate = posixpath.normpath(str(executable or ""))
        return any(posixpath.normpath(known) == candidate for known in self.paths)


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
        # Run-scoped: this tool object lives exactly as long as one run, so the
        # counter needs no reset hook beyond the successful-registration one.
        self._refusal_chains: dict[tuple[str, str], _RefusalChain] = {}
        self._released_tool = ""

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
                bounded = self._bounded_refusal(params.get("executable"), params["tool"])
                if bounded is not None:
                    return bounded
                if params["tool"] == "maven":
                    canonical_executable, canonical_error = self._canonicalize_maven_executable(
                        params["executable"]
                    )
                    if canonical_error:
                        return canonical_error
                    params["executable"] = canonical_executable
                validation_error = self._validate_executable(params["executable"], params.get("tool"))
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
                bounded = self._bounded_refusal(params.get("executable"), params["tool"])
                if bounded is not None:
                    return bounded
                if params["tool"] == "maven":
                    canonical_executable, canonical_error = self._canonicalize_maven_executable(
                        params["executable"]
                    )
                    if canonical_error:
                        return canonical_error
                    params["executable"] = canonical_executable
                validation_error = self._validate_executable(params["executable"], params.get("tool"))
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
        The registry is durable runtime inventory used by resolution and
        reporting. Registration writes both, and never fails on the second.

        Both terminal success paths (register and activate) arrive here, which
        makes this the one place a working runtime for this tool is observed —
        and therefore where the refusal bound for it is released.
        """
        self._reset_refusal_bound(tool)
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
                    "Constraint: registered runtimes must resolve beneath an allowed container root"
                ],
                raw_data={"executable": normalized_requested, "tool": "maven"},
            )

        orchestrator = getattr(self.store, "orchestrator", None)
        if orchestrator is None or not hasattr(orchestrator, "execute_command"):
            return None, ToolResult.completed_failure(
                output="",
                error="Cannot resolve the Maven executable realpath without a runtime executor",
                error_code="ENV_EXECUTABLE_REALPATH_UNAVAILABLE",
                suggestions=["Observed capability: runtime realpath executor is unavailable"],
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
            validation_error = self._validate_executable(normalized_requested, "maven")
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

    def _validate_executable(
        self, executable: str, tool: Optional[str] = None
    ) -> Optional[ToolResult]:
        # The bound is read here as well as at the `execute` entry, because
        # this is where the refusal is born and where the probe is paid for.
        bounded = self._bounded_refusal(executable, tool)
        if bounded is not None:
            return bounded

        orchestrator = getattr(self.store, "orchestrator", None)
        if orchestrator is None or not hasattr(orchestrator, "execute_command"):
            return None

        result = orchestrator.execute_command(
            f"test -x {shlex.quote(executable)} && echo EXISTS || echo MISSING"
        )
        output = result.get("output") or ""
        if result.get("exit_code") == 0 and "EXISTS" in output:
            return None

        # Live p7b-camel-quarkus: the model asked for
        # /usr/lib/jvm/java-17-openjdk-AMD64/bin/java on an arm64 machine while
        # the arm64 path for the same JDK was already registered, and the
        # refusal said only that the path does not exist. What the overlay
        # already knows is the cheapest correction there is, so it is stated.
        registered = self._registered_candidates(tool)
        moves = []
        # D2 2026-08-12: six runs were spent looping here (rocketmq-externals
        # x453, spark-kubernetes-operator x151, tapestry-5 x71), every one of
        # them finishing with zero compiled classes. The refusal must name what
        # the harness can SEE, productive move first.
        named_tool = tool or self._tool_from_executable(executable)
        wrapper = self._project_wrapper_for(named_tool)
        if wrapper:
            # The wrapper needs no network and is the runner the project itself
            # ships — tapestry-5 had gradlew on disk while the model burned its
            # run on a nonexistent /usr/bin/gradle.
            moves.append(
                f"This project ships its own {named_tool} wrapper at {wrapper} — "
                f"register that instead of a system path."
            )
        if registered:
            moves.append(
                "Already registered and executable: " + ", ".join(registered[:6])
            )
        elif named_tool:
            # No candidate exists anywhere: registering other paths cannot
            # succeed either. The one productive move is installing the tool
            # (live 2026-08-09: the model probed absent /usr/bin/mvn in a loop
            # because nothing named the provision route). The tool is inferred
            # from the basename when the call did not name one, because the
            # loops recurred through exactly those bare calls.
            moves.append(
                f"No {named_tool} is registered and this path does not exist — if "
                f"{named_tool} is not installed in the container, install it first: "
                f"project(action='provision', packages=['{named_tool}'])"
            )
        # Only the moves above can end this wall; the standing advice appended
        # below cannot, which is why the bound carries the moves alone into the
        # marker and into every later refusal.
        chain = self._refusal_chain(executable, tool)
        chain.observe(executable, tuple(moves))
        suggestions = [*moves, *_GENERIC_REGISTRATION_MOVES]
        metadata: dict[str, Any] = {"action": "validate_executable"}
        if chain.bound_reached:
            suggestions.append(self._bound_notice(chain))
            metadata[MATERIAL_RECURRENCE_BOUND_MARKER] = chain.marker()
        refusal = ToolResult.completed_failure(
            output="",
            error=f"Env overlay executable is not executable or does not exist: {executable}",
            error_code=_EXECUTABLE_NOT_FOUND,
            suggestions=suggestions,
            raw_data={
                "executable": executable,
                **({"registered_candidates": registered} if registered else {}),
            },
            metadata=metadata,
        )
        # This result's own ref belongs to the next statement of the wall; the
        # engine unions it in for the one being made now.
        chain.evidence_refs.append(refusal.output_ref)
        return refusal

    # ------------------------------------------------------------------
    # The third rung: a material action may not recur without bound (#42).
    # The first rung is the refusal naming a productive move; the second is
    # LoopMemory's advisor redirect at recurrence >= 2. Both fired ~689 times
    # on rocketmq-externals and neither could end the repetition, so the third
    # converts it into a stated fact. The tool owns the count, the bound and
    # the probe it no longer pays for; it states the fact in typed metadata
    # and the engine — the only writer of run evidence — records the blocker.
    # Nothing is force-closed and no action is synthesized.
    # ------------------------------------------------------------------

    def _refusal_identity(self, executable: Any, tool: Optional[str]) -> tuple[str, str]:
        """`(tool, error_code)` — never the path, which is what cycled."""
        named = str(tool or "").strip().lower() or self._tool_from_executable(executable)
        # An unnamed, unmapped tool falls back to the executable's own basename
        # so three unrelated launchers are not merged into one identity.
        fallback = str(executable or "").rstrip("/").rsplit("/", 1)[-1].strip().lower()
        return (named or fallback, _EXECUTABLE_NOT_FOUND)

    def _refusal_chain(self, executable: Any, tool: Optional[str]) -> _RefusalChain:
        identity = self._refusal_identity(executable, tool)
        return self._refusal_chains.setdefault(identity, _RefusalChain(tool=identity[0]))

    def _bound_notice(self, chain: _RefusalChain) -> str:
        return (
            f"{chain.tool} registration has now been refused {chain.count} times "
            f"({', '.join(chain.paths)}). These paths are no longer probed; "
            "take one of the moves above instead of naming them again."
        )

    def _bounded_refusal(self, executable: Any, tool: Optional[str]) -> Optional[ToolResult]:
        """The refusal owed to a re-probe past the bound, at the cost of none.

        §3 justifies dropping the probe because "the probe cannot change its
        answer".  That is true of a path this identity already refused, and
        false of one it has never tried — the wrapper the refusal itself
        recommends, or a binary the recommended provision just installed. So
        the identity owns the bound and the count, and the paths it already
        refused are the ones that stop costing a round trip; a first look at a
        new path is not a re-probe and is never refused unseen.
        """
        path = str(executable or "").strip()
        if not path:
            # A call with no executable is a schema error, and the existing
            # missing-parameter refusal is the more useful answer.
            return None
        chain = self._refusal_chains.get(self._refusal_identity(path, tool))
        if chain is None or not chain.bound_reached:
            return None
        if not chain.already_refused(path):
            return None
        return ToolResult.completed_failure(
            output="",
            error=(
                f"{chain.tool} registration refused without probing: "
                f"{_EXECUTABLE_NOT_FOUND} recurred {chain.count} times for "
                f"{', '.join(chain.paths)}"
            ),
            error_code=_REFUSAL_BOUND_REACHED,
            suggestions=[*chain.moves, self._bound_notice(chain)],
            raw_data={
                "executable": path,
                "tool": chain.tool,
                "refused_executables": list(chain.paths),
                "refusal_count": chain.count,
                "bound": _MATERIAL_RECURRENCE_BOUND,
            },
            metadata={
                "action": "validate_executable",
                MATERIAL_RECURRENCE_BOUND_MARKER: chain.marker(),
            },
        )

    def _reset_refusal_bound(self, tool: str) -> None:
        """A registration that succeeded ends this tool's wall.

        A later failure after a real success is new information, not the same
        wall, so the count restarts. A wall that had been stated is reported as
        released on the successful result, so the engine can retire a blocker
        that would otherwise stand against a tool that now registers.
        """
        normalized = str(tool or "").strip().lower()
        for identity in [key for key in self._refusal_chains if key[0] == normalized]:
            chain = self._refusal_chains.pop(identity)
            if chain.bound_reached:
                self._released_tool = normalized

    # A system path the model reached for, mapped to the tool it wanted. Only
    # the launchers whose absence produced the D2 loops need an entry.
    _EXECUTABLE_TOOLS = {"gradle": "gradle", "mvn": "maven", "maven": "maven", "java": "java"}
    # The runner a project ships for itself, which needs no network at all.
    _TOOL_WRAPPERS = {"gradle": "gradlew", "maven": "mvnw"}

    @classmethod
    def _tool_from_executable(cls, executable: Any) -> str:
        basename = str(executable or "").rstrip("/").rsplit("/", 1)[-1].strip().lower()
        return cls._EXECUTABLE_TOOLS.get(basename, "")

    def _project_wrapper_for(self, tool: Optional[str]) -> str:
        """The project's own wrapper for this tool, if one is on disk.

        Best effort and bounded: one shallow probe on an error path the caller
        was already paying a probe for. An unreadable workspace costs the caller
        nothing beyond the refusal it was already getting.
        """
        wrapper = self._TOOL_WRAPPERS.get(str(tool or "").strip().lower())
        if not wrapper:
            return ""
        orchestrator = getattr(self.store, "orchestrator", None)
        if orchestrator is None or not hasattr(orchestrator, "execute_command"):
            return ""
        try:
            result = orchestrator.execute_command(
                f"find /workspace -maxdepth 2 -name {shlex.quote(wrapper)} -type f -print -quit",
                workdir=None,
                timeout=30,
            )
        except Exception as exc:
            logger.debug(f"wrapper probe unavailable: {exc}")
            return ""
        if not isinstance(result, dict) or result.get("exit_code") not in (0, None):
            return ""
        return str(result.get("output") or "").strip().splitlines()[0].strip() if result.get(
            "output"
        ) else ""

    def _registered_candidates(self, tool: Optional[str]) -> list:
        """Executables the overlay already holds for this tool, blocked ones out.

        Best effort by design: an unreadable overlay costs the caller nothing
        beyond the refusal it was already getting.
        """
        try:
            overlay = self.store.inspect()
        except Exception as exc:
            logger.debug(f"registered-candidate lookup skipped: {exc}")
            return []
        entry = ((overlay or {}).get("tools") or {}).get(str(tool or "").strip().lower()) or {}
        blocked = {
            str((item or {}).get("executable") or "")
            for item in entry.get("blocked") or ()
        }
        return [
            path for path in sorted((entry.get("candidates") or {}).keys()) if path not in blocked
        ]

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
                suggestions=["Observed capability: runtime version probe executor is unavailable"],
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
                    "Constraint: the submitted executable must complete its identity/version probe"
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
        metadata: dict[str, Any] = {"action": action}
        released, self._released_tool = self._released_tool, ""
        if released:
            metadata[MATERIAL_RECURRENCE_RELEASE_MARKER] = {
                "tool": released,
                "error_code": _EXECUTABLE_NOT_FOUND,
                "reason": f"{released} registration succeeded",
            }
        return ToolResult.completed_success(
            output=json.dumps(raw_data, indent=2, sort_keys=True),
            raw_data=raw_data,
            metadata=metadata,
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
                    # No oneOf: providers reject union schemas (OpenAI's rule
                    # is top-level, but the wire stays uniform). The execute
                    # signature still accepts a single string and normalizes.
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "PATH entries to prepend when this candidate is active "
                        "(a single string is also accepted)."
                    ),
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
