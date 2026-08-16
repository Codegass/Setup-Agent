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
from .java_versions import java_major, names_bare_java_major
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
# The env-registration refusals that all state the same kind of fact: THIS
# registration cannot succeed as asked, and asking again for the same path
# cannot change the answer. Each is counted under its own (tool, error_code)
# identity — separate codes are separate walls, because they answer different
# asks and carry different remaining moves.
#   ENV_EXECUTABLE_NOT_FOUND ..................... nothing is at the path; the
#       D2 wall itself (rocketmq-externals x453, spark-kubernetes-operator
#       x151, tapestry-5 x71).
#   ENV_EXECUTABLE_PATH_NOT_ABSOLUTE ............. a relative spelling is not
#       resolvable in the container and stays unresolvable when respelled the
#       same way.
#   ENV_EXECUTABLE_PATH_OUTSIDE_RUNTIME_ROOTS .... the trusted-root policy is a
#       property of the path, not of the container's current state.
#   ENV_EXECUTABLE_REALPATH_ESCAPE ............... likewise a property of what
#       the symlink at that path points at.
#   ENV_MAVEN_EXECUTABLE_NAME_MISMATCH ........... the canonical basename does
#       not become `mvn` by registering the same path again.
#   ENV_RUNTIME_IDENTITY_MISMATCH ................ the binary at the path is
#       not Maven, however often it is offered as Maven.
#   ENV_RUNTIME_PROBE_FAILED ..................... the runtime at the path does
#       not complete its own -version probe.
# Deliberately NOT counted, because each states a transient or harness
# condition the next call can legitimately find changed:
# ENV_RUNTIME_PROBE_UNAVAILABLE and ENV_EXECUTABLE_REALPATH_UNAVAILABLE (no
# command executor at all), ENV_EXECUTABLE_REALPATH_FAILED (the executor
# answered with something unreadable), ENV_ACTIVATION_NOT_CONFIRMED (overlay
# state a retry can settle), ENV_RUNTIME_REQUIREMENT_MISMATCH and
# ENV_RUNTIME_VERSION_UNVERIFIED (the observed requirement set is mutable run
# state, not a property of the executable, and the very next call can state
# the version this one omitted), and
# the schema/exception codes ENV_MISSING_PARAMETER, ENV_INVALID_ACTION,
# ENV_VALIDATION_ERROR, ENV_OPERATION_FAILED. The project facade's
# PROJECT_ENV_ACTIVATION_REQUIRED never reaches this tool: project_tool.py
# refuses that call before the delegate is invoked.
# Every refusal that names an executable is routed through `_count_refusal`,
# so this set — not which call site happened to remember — is what decides.
_BOUNDED_REFUSAL_CODES = frozenset(
    {
        _EXECUTABLE_NOT_FOUND,
        "ENV_EXECUTABLE_PATH_NOT_ABSOLUTE",
        "ENV_EXECUTABLE_PATH_OUTSIDE_RUNTIME_ROOTS",
        "ENV_EXECUTABLE_REALPATH_ESCAPE",
        "ENV_MAVEN_EXECUTABLE_NAME_MISMATCH",
        "ENV_RUNTIME_IDENTITY_MISMATCH",
        "ENV_RUNTIME_PROBE_FAILED",
    }
)
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
    error_code: str = _EXECUTABLE_NOT_FOUND
    count: int = 0
    paths: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    moves: tuple[str, ...] = ()
    # What this chain already answered for each path, so a restatement past the
    # bound can be identical in the two fields the engine keys recurrence on.
    answers: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def bound_reached(self) -> bool:
        return self.count >= _MATERIAL_RECURRENCE_BOUND

    def marker(self) -> dict[str, Any]:
        """The structured fact, never pre-rendered prose: the engine renders."""
        return {
            "tool": self.tool,
            "error_code": self.error_code,
            "refused_executables": list(self.paths),
            "remaining_moves": list(self.moves),
            "refusal_count": self.count,
            "bound": _MATERIAL_RECURRENCE_BOUND,
            "evidence_refs": list(self.evidence_refs),
        }

    def observe(
        self,
        executable: str,
        moves: tuple[str, ...],
        *,
        error: str = "",
        failure_signature: str = "",
    ) -> None:
        self.count += 1
        if executable and not self.already_refused(executable):
            self.paths.append(executable)
        # The last refusal's moves are the current ones: the overlay and the
        # workspace can both change under a run.
        self.moves = moves
        if executable:
            self.answers[posixpath.normpath(str(executable))] = (error, failure_signature)

    def answer_for(self, executable: str) -> tuple[str, str]:
        """The refusal this chain already gave for one path, verbatim."""
        return self.answers.get(posixpath.normpath(str(executable or "")), ("", ""))

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
                "env register after installation; Maven registration probes the executable, and "
                "any requirement in force is enforced against the registered version — which "
                "must therefore be stated — before persistence. Use env activate before retrying a build. Use "
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
        self._released_codes: list[str] = []

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
                if measured_version is None:
                    # No probe measured this tool, so its version is a claim.
                    # A requirement in force still decides — and an absent
                    # version satisfies nothing.
                    unproven = self._unproven_version_refusal(
                        params["tool"],
                        params["executable"],
                        params.get("version"),
                        params.get("requirement"),
                    )
                    if unproven is not None:
                        return unproven
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
                    return self._count_refusal(
                        ToolResult.completed_failure(
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
                        ),
                        executable=params["executable"],
                        tool=params["tool"],
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
            return None, self._count_refusal(
                ToolResult.completed_failure(
                    output="",
                    error="Maven executable must be an absolute container path",
                    error_code="ENV_EXECUTABLE_PATH_NOT_ABSOLUTE",
                    suggestions=[
                        "Provide the full container path to the downloaded distribution's bin/mvn."
                    ],
                    raw_data={"executable": requested, "tool": "maven"},
                ),
                executable=requested,
                tool="maven",
                unusable="this path is not an absolute container path",
            )

        normalized_requested = posixpath.normpath(requested)
        requested_group = self._runtime_root_group(normalized_requested)
        if requested_group is None:
            return None, self._count_refusal(
                ToolResult.completed_failure(
                    output="",
                    error=(
                        "Maven executable is outside the allowed container runtime roots: "
                        f"{normalized_requested}"
                    ),
                    error_code="ENV_EXECUTABLE_PATH_OUTSIDE_RUNTIME_ROOTS",
                    suggestions=[
                        "Constraint: registered runtimes must resolve beneath an allowed "
                        "container root"
                    ],
                    raw_data={"executable": normalized_requested, "tool": "maven"},
                ),
                executable=normalized_requested,
                tool="maven",
                unusable="this path is outside the allowed container runtime roots",
            )

        orchestrator = getattr(self.store, "orchestrator", None)
        if orchestrator is None or not hasattr(orchestrator, "execute_command"):
            return None, self._count_refusal(
                ToolResult.completed_failure(
                    output="",
                    error="Cannot resolve the Maven executable realpath without a runtime executor",
                    error_code="ENV_EXECUTABLE_REALPATH_UNAVAILABLE",
                    suggestions=["Observed capability: runtime realpath executor is unavailable"],
                    raw_data={"executable": normalized_requested, "tool": "maven"},
                ),
                executable=normalized_requested,
                tool="maven",
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
            return None, self._count_refusal(
                ToolResult.completed_failure(
                    output=str(resolved.get("output") or ""),
                    error=(
                        "Could not resolve an exact Maven executable realpath: "
                        f"{normalized_requested}"
                    ),
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
                ),
                executable=normalized_requested,
                tool="maven",
            )

        canonical = posixpath.normpath(resolved_lines[0])
        if not self._path_in_roots(canonical, requested_group):
            return None, self._count_refusal(
                ToolResult.completed_failure(
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
                ),
                executable=normalized_requested,
                tool="maven",
                unusable="this path resolves outside its trusted runtime root",
            )

        if posixpath.basename(canonical) != "mvn":
            # The moves are computed at the FIRST refusal, not only at the bound.
            # ignite d2r4 seq 25 refused /workspace/ignite/mvnw with one
            # suggestion — "register the exact canonical bin/mvn path" — naming a
            # file that container did not have; the model answered with a
            # /tmp/mvnshim/mvn symlink, then /workspace/ignite/bin/mvn, and the
            # run sealed zero classes. A wall this flat owes its way out on the
            # first statement of it.
            unusable = "this path does not resolve to an executable named mvn"
            moves = self._remaining_moves(normalized_requested, "maven", unusable=unusable)
            return None, self._count_refusal(
                ToolResult.completed_failure(
                    output="",
                    error=f"Canonical Maven executable must be named mvn: {canonical}",
                    error_code="ENV_MAVEN_EXECUTABLE_NAME_MISMATCH",
                    suggestions=[
                        *moves,
                        "Register the distribution's exact canonical bin/mvn path.",
                    ],
                    raw_data={
                        "executable": normalized_requested,
                        "resolved_executable": canonical,
                        "tool": "maven",
                    },
                ),
                executable=normalized_requested,
                tool="maven",
                moves=moves,
                unusable=unusable,
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

        registered = self._registered_candidates(tool)
        # Only these moves can end this wall; the standing advice appended
        # below cannot, which is why the bound carries the moves alone into the
        # marker and into every later refusal.
        moves = self._remaining_moves(
            executable,
            tool,
            registered=registered,
            unusable="this path does not exist",
        )
        refusal = ToolResult.completed_failure(
            output="",
            error=f"Env overlay executable is not executable or does not exist: {executable}",
            error_code=_EXECUTABLE_NOT_FOUND,
            suggestions=[*moves, *_GENERIC_REGISTRATION_MOVES],
            raw_data={
                "executable": executable,
                **({"registered_candidates": registered} if registered else {}),
            },
            metadata={"action": "validate_executable"},
        )
        return self._count_refusal(refusal, executable=executable, tool=tool, moves=moves)

    def _remaining_moves(
        self,
        executable: Any,
        tool: Optional[str],
        *,
        registered: Optional[list] = None,
        unusable: str,
    ) -> list[str]:
        """The moves that can still end this wall, productive first.

        Live p7b-camel-quarkus: the model asked for
        /usr/lib/jvm/java-17-openjdk-AMD64/bin/java on an arm64 machine while
        the arm64 path for the same JDK was already registered, and the refusal
        said only that the path does not exist. What the overlay already knows
        is the cheapest correction there is, so it is stated.
        """
        candidates = self._registered_candidates(tool) if registered is None else registered
        moves = []
        # D2 2026-08-12: six runs were spent looping here (rocketmq-externals
        # x453, spark-kubernetes-operator x151, tapestry-5 x71), every one of
        # them finishing with zero compiled classes. The refusal must name what
        # the harness can SEE, productive move first.
        named_tool = tool or self._tool_from_executable(executable)
        wrapper = self._project_wrapper_for(named_tool)
        dispatch = self._build_dispatch_move(named_tool, wrapper)
        if dispatch:
            moves.append(dispatch)
        if candidates:
            moves.append("Already registered and executable: " + ", ".join(candidates[:6]))
        elif named_tool:
            # No usable candidate exists anywhere: registering other paths
            # cannot succeed either. The one productive move is installing the
            # tool (live 2026-08-09: the model probed absent /usr/bin/mvn in a
            # loop because nothing named the provision route). The tool is
            # inferred from the basename when the call did not name one,
            # because the loops recurred through exactly those bare calls.
            moves.append(
                f"No {named_tool} is registered and {unusable} — if "
                f"{named_tool} is not installed in the container, install it first: "
                f"{self._install_call_for(named_tool)}"
            )
        return moves

    @staticmethod
    def _install_call_for(tool: str) -> str:
        """The provision call that can actually install this tool.

        geode d2r4 seq 227: the model registered
        /usr/lib/jvm/java-17-openjdk-amd64/bin/java on an arm64 host and the
        refusal answered `project(action='provision', packages=['java'])`. No
        apt package is named `java`; that call installs nothing. SystemTool
        routes a JDK by MAJOR (`install_java`) and apt packages by name
        (`install`), and the facade picks the route from which parameter is
        present — so the JDK's only working spelling is java_version.
        """
        if tool == "java":
            return "project(action='provision', java_version='<major>')"
        return f"project(action='provision', packages=['{tool}'])"

    def _build_dispatch_move(self, tool: str, wrapper: str) -> str:
        """The dispatch that acquires the toolchain this registration cannot.

        Live proof, ignite d2r3 against d2r4 (logs/d2r4-20260815/slices/ignite.md
        Part 5): one repo, one ref, one resolved commit. d2r4 spent four env
        registrations on Maven — /usr/bin/mvn, the checkout's own ./mvnw, a
        /tmp/mvnshim/mvn symlink to it, /workspace/ignite/bin/mvn — and sealed
        compiled_classes 0. d2r3 answered the identical ENV_EXECUTABLE_NOT_FOUND
        by dispatching build(action='compile'); the engine ran the wrapper with
        `_env_preflight:false`, published env-overlay revision 2 itself
        (evidence_publication control-000169), auto-installed JDK 11, and sealed
        8,562 .class files. Nothing in the refusal named that move.

        The three engine facts this sentence rests on, each verified in the
        source before it was written: BuildTool runs `JdkPreflight` for every
        compile/test/package/install dispatch and `_register_overlay` persists
        what it provisions (build_tool.py, build_preflight.py); MavenTool calls
        `_install_maven` when no candidate resolves and GradleTool calls
        `_install_gradle`; and `build` really does take action='compile' and
        action='test' (`_ACTIONS`).
        """
        build_root = self._build_root_for(tool, wrapper)
        if not build_root:
            # Nothing on disk says where a dispatch would run. Naming one anyway
            # is the #19 defect class — guidance that cannot be acted on.
            return ""
        opening = (
            # NOT offered as a registration: both build tools already prefer the
            # wrapper on their own (maven_tool runs ./mvnw when use_wrapper is
            # unset; gradle_tool defaults to it), and the Maven canonicalizer
            # refuses any executable not named `mvn`.
            f"This project ships its own {tool} wrapper at {wrapper}, and the build "
            f"tool already uses it"
            if wrapper
            else f"This project's {tool} build coordinates are on disk"
        )
        return (
            f"{opening} — dispatch the build instead of registering a runtime: "
            f"build(action='compile') or build(action='test') at {build_root}. The "
            f"build facade's own pre-flight provisions the JDK the build asks for, "
            f"installs {tool} when no runtime resolves, and registers what it "
            f"acquired in the runtime overlay — the dispatch acquires the toolchain, "
            f"so no registration is owed first."
        )

    def _build_root_for(self, tool: Optional[str], wrapper: str) -> str:
        """Where a dispatch for this tool would run, when the container says so.

        The wrapper already answers it for a project that ships one, at no extra
        probe. jackrabbit d2r4 shipped none — its own search proved
        `mvnw|.mvn|apache-maven*|maven*` matched nothing under the checkout —
        while /workspace/jackrabbit/pom.xml was the reactor root all along, so
        the second look is what that project needed. Best effort and bounded:
        one shallow find on an error path the caller was already paying for.
        """
        if wrapper:
            return posixpath.dirname(wrapper)
        names = self._TOOL_BUILD_FILES.get(str(tool or "").strip().lower())
        orchestrator = getattr(self.store, "orchestrator", None)
        if not names or orchestrator is None or not hasattr(orchestrator, "execute_command"):
            return ""
        expression = " -o ".join(f"-name {shlex.quote(name)}" for name in names)
        try:
            result = orchestrator.execute_command(
                f"find /workspace -maxdepth 2 \\( {expression} \\) -type f -print -quit",
                workdir=None,
                timeout=30,
            )
        except Exception as exc:
            logger.debug(f"build-root probe unavailable: {exc}")
            return ""
        if not isinstance(result, dict) or result.get("exit_code") not in (0, None):
            return ""
        lines = str(result.get("output") or "").strip().splitlines()
        return posixpath.dirname(lines[0].strip()) if lines else ""

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

    def _refusal_tool(self, executable: Any, tool: Optional[str]) -> str:
        """The tool half of the identity — never the path, which is what cycled."""
        named = str(tool or "").strip().lower() or self._tool_from_executable(executable)
        # An unnamed, unmapped tool falls back to the executable's own basename
        # so three unrelated launchers are not merged into one identity.
        fallback = str(executable or "").rstrip("/").rsplit("/", 1)[-1].strip().lower()
        return named or fallback

    def _refusal_identity(
        self,
        executable: Any,
        tool: Optional[str],
        error_code: str,
    ) -> tuple[str, str]:
        """`(tool, error_code)`: two codes for one tool are two walls."""
        return (self._refusal_tool(executable, tool), error_code)

    def _refusal_chain(
        self,
        executable: Any,
        tool: Optional[str],
        error_code: str,
    ) -> _RefusalChain:
        identity = self._refusal_identity(executable, tool, error_code)
        return self._refusal_chains.setdefault(
            identity,
            _RefusalChain(tool=identity[0], error_code=identity[1]),
        )

    def _count_refusal(
        self,
        refusal: ToolResult,
        *,
        executable: Any,
        tool: Optional[str],
        moves: Optional[list[str]] = None,
        unusable: str = "the runtime at this path cannot be registered",
    ) -> ToolResult:
        """Count one refusal of the bounded family and state the bound at it.

        Every code in `_BOUNDED_REFUSAL_CODES` says the same thing about a
        repetition — this ask cannot succeed — so each keeps its own count. A
        code outside the family, or a call that named no path, is returned
        untouched.
        """
        code = str(refusal.error_code or "")
        path = str(executable or "").strip()
        if code not in _BOUNDED_REFUSAL_CODES or not path:
            return refusal
        chain = self._refusal_chain(path, tool, code)
        if moves is None:
            # Two shallow probes, and only for the refusal that reaches the
            # bound: rung 1's promise is kept by this refusal's own
            # suggestions, and `chain.moves` is read only once a wall is stated.
            moves = (
                self._remaining_moves(path, tool, unusable=unusable)
                if chain.count + 1 >= _MATERIAL_RECURRENCE_BOUND
                else list(chain.moves)
            )
        chain.observe(
            path,
            tuple(moves),
            error=str(refusal.error or ""),
            failure_signature=str(refusal.failure_signature or ""),
        )
        if chain.bound_reached:
            stated = list(refusal.suggestions or ())
            refusal.suggestions = [
                *(move for move in chain.moves if move not in stated),
                *stated,
                self._bound_notice(chain),
            ]
            refusal.metadata[MATERIAL_RECURRENCE_BOUND_MARKER] = chain.marker()
        # This result's own ref belongs to the next statement of the wall; the
        # engine unions it in for the one being made now.
        if refusal.output_ref:
            chain.evidence_refs.append(refusal.output_ref)
        return refusal

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

        The restatement is the SAME refusal, not a new one: it carries the
        typed code and the failure signature this wall already answered with,
        because `OutcomeKey` is (outcome, error_code, failure_signature) and a
        re-typed refusal would restart the engine's recurrence chain at exactly
        the moment the tool declared the wall (#46 defect 1). The bound is
        carried by the marker, the notice and the error text instead.
        """
        path = str(executable or "").strip()
        if not path:
            # A call with no executable is a schema error, and the existing
            # missing-parameter refusal is the more useful answer.
            return None
        tool_key = self._refusal_tool(path, tool)
        chain = next(
            (
                candidate
                for (chain_tool, _code), candidate in self._refusal_chains.items()
                if chain_tool == tool_key
                and candidate.bound_reached
                and candidate.already_refused(path)
            ),
            None,
        )
        if chain is None:
            return None
        answered, signature = chain.answer_for(path)
        stated = answered or (
            f"{chain.tool} registration was refused with {chain.error_code}: {path}"
        )
        return ToolResult.completed_failure(
            output="",
            error=(
                f"{stated} (refused without probing: {chain.error_code} recurred "
                f"{chain.count} times for {', '.join(chain.paths)})"
            ),
            error_code=chain.error_code,
            failure_signature=signature or None,
            suggestions=[*chain.moves, self._bound_notice(chain)],
            raw_data={
                "executable": path,
                "tool": chain.tool,
                "probed": False,
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
        """A registration that succeeded ends this tool's walls.

        A later failure after a real success is new information, not the same
        wall, so the count restarts. Every wall that had been stated is
        reported as released on the successful result — by code, since one tool
        can have been refused under several of them — so the engine can retire
        blockers that would otherwise stand against a tool that now registers.
        """
        normalized = str(tool or "").strip().lower()
        for identity in [key for key in self._refusal_chains if key[0] == normalized]:
            chain = self._refusal_chains.pop(identity)
            if chain.bound_reached:
                self._released_tool = normalized
                self._released_codes.append(chain.error_code)

    # A system path the model reached for, mapped to the tool it wanted. Only
    # the launchers whose absence produced the D2 loops need an entry.
    _EXECUTABLE_TOOLS = {"gradle": "gradle", "mvn": "maven", "maven": "maven", "java": "java"}
    # The runner a project ships for itself, which needs no network at all.
    _TOOL_WRAPPERS = {"gradle": "gradlew", "maven": "mvnw"}
    # The file that marks a root a dispatch can target when the project ships no
    # wrapper. Only the two JVM build systems `build` dispatches by action have
    # an entry; a tool with no entry gets no dispatch move, which is the honest
    # answer for `java` (there is no build(action=...) that targets a JDK).
    _TOOL_BUILD_FILES = {
        "maven": ("pom.xml",),
        "gradle": ("build.gradle", "build.gradle.kts", "settings.gradle"),
    }

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

    def _unsatisfied_requirement(
        self,
        tool: str,
        version: Optional[str],
        requirement: Optional[str],
    ) -> Optional[ToolVersionRequirement]:
        """The first constraint in force this version fails, if any.

        `matches_requirement` already answers the absence case the way this
        tool must: a hard requirement is not satisfied by an unknown version,
        while a merely preferred one is not a requirement at all.
        """
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
            # observed contract for the tool.  Build-time resolution remains
            # scoped.
            for record in self.store.observed_requirements(tool)
        ]
        requirements: list[ToolVersionRequirement] = []
        for candidate_requirement in [*observed_requirements, explicit_requirement]:
            if candidate_requirement and candidate_requirement.raw not in {
                item.raw for item in requirements
            }:
                requirements.append(candidate_requirement)
        if not requirements:
            return None

        manager = ToolchainManager(getattr(self.store, "orchestrator", None))
        return next(
            (
                candidate_requirement
                for candidate_requirement in requirements
                if not self._satisfies(manager, tool, version, candidate_requirement)
            ),
            None,
        )

    @staticmethod
    def _satisfies(
        manager: ToolchainManager,
        tool: str,
        version: Optional[str],
        requirement: ToolVersionRequirement,
    ) -> bool:
        """Whether one version meets one constraint, read as its tool reads it."""
        if manager.matches_requirement(version, requirement):
            return True
        if tool != "java" or requirement.kind != "exact" or not version:
            return False
        # A Java requirement that names a BARE major is met by any runtime of
        # that major: "21" and "21.0.9" are one JDK, and "1.8" is major 8 —
        # the same rule build_preflight compares activations with. Refusing
        # `version="21.0.9"` against `requirement="21"` would refuse the one
        # honest answer the model can give.
        #
        # A DOTTED requirement is a different statement, and this fallback used
        # to major BOTH sides of it: "21.0.1" accepted 21.0.9. An exact
        # requirement is read exactly (`ToolchainManager._matches_requirement`
        # -> `_same_version`), and a minimum has its own spelling (">=21.0.1"),
        # so widening the caller's constraint here would be this tool refusing
        # to enforce the one it was handed.
        if not names_bare_java_major(requirement.raw):
            return False
        required_major = java_major(requirement.raw)
        return bool(required_major) and java_major(version) == required_major

    def _unproven_version_refusal(
        self,
        tool: str,
        executable: str,
        version: Optional[str],
        requirement: Optional[str],
    ) -> Optional[ToolResult]:
        """Refuse a registration whose own requirement its version cannot meet.

        Live lucene d2r2 (seq 86/88): `env register tool=java
        executable=/usr/bin/java requirement="[21,24]" activate=true` returned
        success and persisted `"version": null`.  The requirement was stated in
        the same call and checked against nothing, so the overlay sealed — and
        activated — a runtime that was already known to be Java 17.

        Not a bounded-refusal code: the requirement set is mutable run state,
        so provisioning a satisfying runtime makes the very same call succeed.
        """
        failed_requirement = self._unsatisfied_requirement(tool, version, requirement)
        if failed_requirement is None:
            return None
        stated_version = str(version or "").strip()
        raw_data = {
            "executable": executable,
            "tool": tool,
            "version": stated_version or None,
            "requirement": failed_requirement.raw,
            "requirement_source": failed_requirement.source,
        }
        install_move = (
            f"If no installed {tool} satisfies it, provision one: "
            "project(action='provision', java_version='<major>')"
            if tool == "java"
            else f"If no installed {tool} satisfies it, install one before registering it"
        )
        if not stated_version:
            return ToolResult.completed_failure(
                output="",
                error=(
                    f"{tool} registration states no version to check against "
                    f"{failed_requirement.raw}"
                ),
                error_code="ENV_RUNTIME_VERSION_UNVERIFIED",
                suggestions=[
                    f"Observe the runtime's own version and register it: run "
                    f"`{executable} -version` in bash, then pass version=<observed>",
                    f"Constraint: {failed_requirement.raw} is in force for {tool}, and an "
                    "unstated version cannot satisfy it",
                    install_move,
                ],
                raw_data=raw_data,
                metadata={"action": "register"},
            )
        return ToolResult.completed_failure(
            output="",
            error=f"Registered {tool} {stated_version} does not satisfy {failed_requirement.raw}",
            error_code="ENV_RUNTIME_REQUIREMENT_MISMATCH",
            suggestions=[
                f"Constraint: {failed_requirement.raw} is in force for {tool}; do not weaken "
                "or omit the requirement",
                install_move,
            ],
            raw_data=raw_data,
            metadata={"action": "register"},
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
            return None, self._count_refusal(
                ToolResult.completed_failure(
                    output="",
                    error="Cannot verify Maven without a runtime command executor",
                    error_code="ENV_RUNTIME_PROBE_UNAVAILABLE",
                    suggestions=[
                        "Observed capability: runtime version probe executor is unavailable"
                    ],
                    raw_data={"executable": executable, "tool": "maven"},
                ),
                executable=executable,
                tool="maven",
            )

        probe = orchestrator.execute_command(
            f"{shlex.quote(executable)} -version",
            timeout=30,
        )
        output = probe.get("output") or ""
        if probe.get("exit_code") != 0 or probe.get("success") is False:
            return None, self._count_refusal(
                ToolResult.completed_failure(
                    output=output,
                    error=f"Maven runtime probe failed for {executable}",
                    error_code="ENV_RUNTIME_PROBE_FAILED",
                    suggestions=[
                        "Constraint: the submitted executable must complete its "
                        "identity/version probe"
                    ],
                    raw_data={
                        "executable": executable,
                        "tool": "maven",
                        "probe_exit_code": probe.get("exit_code"),
                    },
                ),
                executable=executable,
                tool="maven",
                unusable="the runtime at this path fails its own version probe",
            )

        match = _MAVEN_VERSION_RE.search(_ANSI_ESCAPE_RE.sub("", output))
        if not match:
            return None, self._count_refusal(
                ToolResult.completed_failure(
                    output=output,
                    error=f"Executable did not identify itself as Apache Maven: {executable}",
                    error_code="ENV_RUNTIME_IDENTITY_MISMATCH",
                    suggestions=[
                        "Register the distribution's exact bin/mvn executable, not a similarly "
                        "named script or archive."
                    ],
                    raw_data={"executable": executable, "tool": "maven"},
                ),
                executable=executable,
                tool="maven",
                unusable="the runtime at this path is not Apache Maven",
            )

        measured_version = match.group(1)
        failed_requirement = self._unsatisfied_requirement(
            "maven",
            measured_version,
            requirement,
        )
        if failed_requirement:
            return None, self._count_refusal(
                ToolResult.completed_failure(
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
                ),
                executable=executable,
                tool="maven",
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
        codes, self._released_codes = list(dict.fromkeys(self._released_codes)), []
        if released:
            metadata[MATERIAL_RECURRENCE_RELEASE_MARKER] = {
                "tool": released,
                # Every wall this success ends, named: one tool can have been
                # refused under several codes of the bounded family.
                "error_codes": codes,
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
                    "description": (
                        "Observed executable version, as the executable itself reported it. "
                        "Required whenever a version requirement is in force for the tool: an "
                        "unstated version satisfies no requirement and the registration is "
                        "refused."
                    ),
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
