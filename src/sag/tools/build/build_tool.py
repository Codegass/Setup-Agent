"""build(action: deps|compile|test|package|install|native) — one tool over all
ecosystems."""

import posixpath
import re
import shlex
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from sag.agent.action_intents import bounded_exact_params
from sag.agent.evidence_assessments import (
    CAPABILITY_PREFIX,
    ControlAssessment,
    assess_dispatch,
    next_control_event_id,
    read_receipt,
    write_assessment,
)
from sag.agent.invocation_contracts import (
    CONTRACT_AUTHORITY_MISSING,
    CONTRACT_DIR,
    CONTRACT_PERSIST_FAILED,
    authorized_facade_envelope_id,
    current_action_context,
    dispatch_contract,
    freeze_contract,
)
from sag.agent.invocation_receipts import (
    active_receipt_run_id,
    nearest_domain_fact_epoch,
    nearest_domain_root,
    producer_observations_sha256,
    python_import_targets,
)
from sag.agent.invocation_receipts import target_sha as probe_target_sha
from sag.runtime.env_overlay import (
    RUNTIME_REQUIREMENT_CONFLICT,
    EnvOverlayStore,
)
from sag.tools.base import BaseTool, ToolResult
from sag.tools.internal.build_preflight import (
    REQUIREMENTS_PATH,
    JdkPreflight,
    active_java_major,
    active_java_runtime,
    classify_runner_java_requirement,
    classify_version_error,
    read_live_build_requirements,
)

from .backends import (
    BUILD_MARKERS,
    NATIVE_DEFINITION_KEY,
    NATIVE_DEFINITION_VALUES,
    NATIVE_FEATURE_RESOLVER,
    GradleBackend,
    MavenBackend,
    PythonBackend,
    native_definition_feature,
    native_feature_definition,
)

_ACTIONS = ("deps", "compile", "test", "package", "install", "native")

# Verbs that actually invoke the JDK; `deps` resolution is not gated on a
# matching toolchain, so it skips the pre-flight (spec §1b: no-op when moot).
# `native` is python-system machinery and never reaches a JVM toolchain.
_PREFLIGHT_VERBS = ("compile", "test", "package", "install")

# Verbs the domain-edge execution law governs (spec §C2). They are the verbs
# that PRODUCE something; `deps` resolves coordinates and env/probe verbs only
# inspect, and refusing those would hide the very mismatch the edge records.
# `native` repairs the ENVIRONMENT a consumer builds in rather than consuming a
# producer's artifact, so a locked edge is not a reason to refuse it.
_EDGE_GATED_VERBS = ("compile", "test", "package", "install")

# --- the typed native affordance (spec §C8, plan §Stage E) ------------------
# The refusal the plan names verbatim: provenance is necessary for a repair and
# the harness has none, so the honest answer is that nothing is known to fix.
NATIVE_WITHOUT_PROVENANCE = "NATIVE_WITHOUT_PROVENANCE"
NATIVE_WITHOUT_PROVENANCE_CODE = "native_without_provenance"
NATIVE_UNSOURCED_CLAUSE = "no project-owned repair policy — the state is unknown, not repairable"
# The allowlist refusals. Separate codes because they are separate defects: an
# unknown feature is a request the platform cannot resolve, a bad definition is
# a token the environment must never carry, and an inconsistent pair is a
# request that contradicts itself (spec §C8: "`features` and definitions must
# be consistent").
NATIVE_FEATURE_UNKNOWN = "NATIVE_FEATURE_UNKNOWN"
NATIVE_FEATURE_UNKNOWN_CODE = "native_feature_unknown"
NATIVE_DEFINITION_REJECTED = "NATIVE_DEFINITION_REJECTED"
NATIVE_DEFINITION_REJECTED_CODE = "native_definition_rejected"
NATIVE_DEFINITIONS_INCONSISTENT = "NATIVE_DEFINITIONS_INCONSISTENT"
NATIVE_DEFINITIONS_INCONSISTENT_CODE = "native_definitions_inconsistent"
NATIVE_SYSTEM_UNSUPPORTED = "NATIVE_SYSTEM_UNSUPPORTED"
NATIVE_SYSTEM_UNSUPPORTED_CODE = "native_system_unsupported"

# --- direct Python dependency targets remain disabled ----------------------
PIN_WITHOUT_PROVENANCE = "PIN_WITHOUT_PROVENANCE"
PIN_WITHOUT_PROVENANCE_CODE = "pin_without_provenance"
# The edge statuses that lock a consumer, worst first. `compatible` unlocks and
# `not_applicable` disposes, so neither appears here.
_EDGE_REFUSALS = {
    "version_incompatible": ("DOMAIN_EDGE_BLOCKED", "domain_edge_blocked"),
    "unverified": ("DOMAIN_EDGE_UNVERIFIED", "domain_edge_unverified"),
}
RUNTIME_REQUIREMENT_STATE_UNAVAILABLE = "java_runtime_requirement_state_unavailable"
BUILD_PARAMETER_INVALID = "BUILD_PARAMETER_INVALID"
BUILD_REQUIREMENTS_UNAVAILABLE = "BUILD_REQUIREMENTS_UNAVAILABLE"


def _absolute_root(value: Any) -> Optional[str]:
    """A normalized absolute container path, or None when it is not one."""
    raw = str(value or "").strip()
    if not raw or not raw.startswith("/") or "\x00" in raw or "\n" in raw:
        return None
    return posixpath.normpath(raw)


def _is_contained(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _manifest_domain_edges(requirements: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The manifest's domain edges — projected key first, recommendation second.

    Same dual read every other recommendation fact gets (`attempt_policy`), so
    a manifest written before the projection existed still carries the law.
    Anything that is not a list of mappings is no graph, not a broken one.
    """
    raw = requirements.get("domain_edges")
    if raw is None:
        recommendation = requirements.get("build_recommendation")
        if isinstance(recommendation, Mapping):
            raw = recommendation.get("domain_edges")
    if not isinstance(raw, (list, tuple)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _runtime_domain_scope(
    requirements: Mapping[str, Any],
    working_directory: str,
) -> Dict[str, str]:
    """Exact dynamic-runtime scope; nearest surveyed domain wins.

    This mirrors the contract/receipt domain lookup but always names a root:
    a survey with no domain projection scopes the runtime to the invocation's
    own root instead of granting a checkout-wide default.
    """
    directory = _absolute_root(working_directory)
    if not directory:
        return {}
    best: Optional[Mapping[str, Any]] = None
    best_root = ""
    facts = requirements.get("domain_facts") if isinstance(requirements, Mapping) else None
    for fact in facts if isinstance(facts, (list, tuple)) else ():
        if not isinstance(fact, Mapping):
            continue
        root = _absolute_root(fact.get("root"))
        if not root or not _is_contained(directory, root):
            continue
        if len(root) >= len(best_root):
            best, best_root = fact, root
    scope = {"domain_root": best_root or directory}
    domain_id = str((best or {}).get("domain_id") or "").strip()
    if domain_id:
        scope["domain_id"] = domain_id
    return scope


def _effective_jdk_binding(
    resolution: Mapping[str, Any],
    outcome: Any,
) -> Optional[Dict[str, Any]]:
    """The runtime a dispatch will see plus why its requirement won."""
    required = str(resolution.get("required_major") or "").strip()
    active = str(getattr(outcome, "active_version", None) or "").strip()
    authority = str(resolution.get("authority") or "").strip()
    if not any((required, active, authority)):
        return None
    binding: Dict[str, Any] = {}
    if active:
        binding["major"] = active
    if required:
        binding["requirement_major"] = required
    if authority:
        binding["requirement_authority"] = authority
    provenance = resolution.get("provenance")
    if isinstance(provenance, Mapping) and provenance:
        binding["provenance"] = dict(provenance)
    elif active and not required:
        # No requirement was asserted, but a registered runtime made the
        # preflight probe the dispatch environment.  State that observation's
        # source instead of emitting an unprovenanced effective major.
        binding["runtime_authority"] = "dispatch_probe"
        binding["provenance"] = {"source": "java_runtime_probe"}
    return binding


class BuildTool(BaseTool):
    def __init__(
        self,
        docker_orchestrator,
        maven_tool=None,
        gradle_tool=None,
        python_tool=None,
    ):
        super().__init__(
            name="build",
            description=(
                "Project build runner facade: action = deps | compile | test | package. "
                "It detects maven, gradle, or python project markers, resolves the registered "
                "toolchain, and records each dispatched runner in a durable invocation receipt. "
                "Python deps uses the project's installer and ./.venv; Python test records JUnit "
                "XML. Long-running dispatches return a controller-owned job reference."
            ),
        )
        self.docker_orchestrator = docker_orchestrator
        self._backends = {}
        if maven_tool is not None:
            self._backends["maven"] = MavenBackend(maven_tool)
        if gradle_tool is not None:
            self._backends["gradle"] = GradleBackend(gradle_tool)
        if python_tool is not None:
            self._backends["python"] = PythonBackend(python_tool)

    @staticmethod
    def _public_parameter_problem(
        *,
        verb: str,
        args: Optional[str],
        timeout: Optional[int],
        maven_version_requirement: Optional[str],
        features: Optional[Sequence[str]],
        definitions: Optional[Mapping[str, str]],
    ) -> Optional[str]:
        """Return the first public parameter no selected operation can consume.

        This is deliberately independent of project discovery, so invalid
        model input cannot trigger even a marker probe. Ecosystem-specific
        validation follows immediately after the one necessary marker read.
        """

        if timeout is not None and (type(timeout) is not int or timeout <= 0):
            return "timeout must be a positive integer (boolean is not an integer timeout)"
        if args is not None and (
            not isinstance(args, str) or not args.strip() or args != args.strip()
        ):
            return "args must be omitted or canonical non-empty text"
        if verb != "native" and (features is not None or definitions is not None):
            return "features and definitions are consumed only by action='native'"
        if maven_version_requirement is not None and (
            not isinstance(maven_version_requirement, str)
            or not maven_version_requirement.strip()
            or maven_version_requirement != maven_version_requirement.strip()
        ):
            return "maven_version_requirement must be omitted or canonical non-empty text"
        return None

    @staticmethod
    def _parameter_refusal(
        *,
        detail: str,
        verb: str,
        working_directory: str,
        system: Optional[str] = None,
    ) -> ToolResult:
        facts: Dict[str, Any] = {
            "requested_action": verb,
            "working_directory": working_directory,
        }
        if system:
            facts["system"] = system
        return ToolResult.completed_failure(
            output=f"[build parameter] {detail}; no project runner was dispatched",
            error=detail,
            error_code=BUILD_PARAMETER_INVALID,
            facts=facts,
            metadata={"runner_dispatched": False},
            suggestions=["Submit only parameters consumed by the selected public action"],
        )

    def execute(
        self,
        action: str,
        args: Optional[str] = None,
        working_directory: str = "/workspace",
        timeout: Optional[int] = None,
        maven_version_requirement: Optional[str] = None,
        features: Optional[Sequence[str]] = None,
        definitions: Optional[Mapping[str, str]] = None,
    ) -> ToolResult:
        verb = (action or "").strip().lower()
        if verb not in _ACTIONS:
            return ToolResult.completed_failure(
                output=f"Unknown build action: {action!r}",
                error="invalid action",
                suggestions=[f"Use action= {' | '.join(_ACTIONS)}"],
            )

        # Contract authority is checked before marker detection, manifest
        # reads, native/pin gates, toolchain preflight or backend
        # materialization. The facade may use a unique unrecorded identity only
        # for a sinkless non-repair engine intent; empty scope, active repair,
        # and a live control recorder all fail closed with zero runner calls.
        scope = current_action_context()
        envelope_id = authorized_facade_envelope_id(scope)
        if envelope_id is None:
            return ToolResult.completed_failure(
                output=(
                    "[contract] this build call has no engine-owned action envelope "
                    "or authorized sinkless intent; no discovery, preflight or runner "
                    "was dispatched"
                ),
                error="invocation contract authority missing",
                error_code=CONTRACT_AUTHORITY_MISSING,
                facts={
                    "requested_action": verb,
                    "working_directory": working_directory,
                },
                metadata={"runner_dispatched": False},
            )

        # The normalizer has already materialized schema defaults before the
        # engine minted ActionIntent. Reconstruct that exact public call from
        # this Python invocation and require byte-for-byte JSON equality with
        # the engine-owned scope before even probing a build marker. The facade
        # cannot silently add seven optional nulls or freeze a nearby call.
        actual_public_params: Dict[str, Any] = {
            "action": action,
            "working_directory": working_directory,
        }
        for key, value in (
            ("args", args),
            ("timeout", timeout),
            ("maven_version_requirement", maven_version_requirement),
            ("features", list(features) if features is not None else None),
            ("definitions", dict(definitions) if definitions is not None else None),
        ):
            # Preserve the engine-owned exact shape: a normalizer may retain
            # an explicit JSON null, while a genuinely absent default remains
            # absent. Non-null runtime values can never be omitted.
            if value is not None or key in (scope.intent_exact_params or {}):
                actual_public_params[key] = value
        try:
            actual_public_params = bounded_exact_params(actual_public_params)
        except (TypeError, ValueError):
            actual_public_params = {}
        if actual_public_params != scope.intent_exact_params:
            return ToolResult.completed_failure(
                output=(
                    "[contract] the BuildTool invocation differs from its engine-owned "
                    "ActionIntent; no discovery, preflight or runner was dispatched"
                ),
                error="invocation contract authority mismatch",
                error_code=CONTRACT_AUTHORITY_MISSING,
                facts={"requested_action": verb, "working_directory": working_directory},
                metadata={"runner_dispatched": False},
            )

        parameter_problem = self._public_parameter_problem(
            verb=verb,
            args=args,
            timeout=timeout,
            maven_version_requirement=maven_version_requirement,
            features=features,
            definitions=definitions,
        )
        if parameter_problem:
            return self._parameter_refusal(
                detail=parameter_problem,
                verb=verb,
                working_directory=working_directory,
            )

        # Whether the caller scoped this invocation itself. The normalized
        # working directory is already frozen model intent; the facade never
        # replaces it with a project-name-derived path.
        explicitly_scoped = working_directory not in (None, "", "/workspace")
        # The call AS SUBMITTED. Contract cwd and runner cwd remain identical.
        # `provenance` is deliberately NOT a parameter here (spec §C8): the
        # supporting claim ids are looked up from stored evidence, so nothing
        # the model writes can appear in them.
        requested_call_params = dict(scope.intent_exact_params)

        # Every routing decision below (ecosystem, domain edge, island goal,
        # JDK and the contract pins themselves) must descend from the exact
        # host-published manifest revision. A readable container mirror is
        # forensic data only: accepting it here would let a tampered manifest
        # mint a fresh, apparently-authoritative invocation contract.
        manifest_read = read_live_build_requirements(self.docker_orchestrator)
        if (
            not manifest_read.complete
            or manifest_read.conflict is not None
            or manifest_read.payload is None
        ):
            conflict = manifest_read.conflict or "absent"
            return ToolResult.completed_failure(
                output=(
                    "[evidence] build requirements are not a complete current "
                    "host-published revision; no project probe, preflight or runner "
                    "was dispatched"
                ),
                error="live build requirements unavailable",
                error_code=BUILD_REQUIREMENTS_UNAVAILABLE,
                facts={
                    "requested_action": verb,
                    "working_directory": working_directory,
                    "build_requirements_status": conflict,
                },
                metadata={"runner_dispatched": False, "blocker_owner": "harness"},
            )
        requirements = dict(manifest_read.payload)

        system, checked = self._detect_system(working_directory)
        if system is None:
            return ToolResult.completed(
                operation_outcome="unknown",
                evidence_status="unknown",
                output=(
                    f"No known build system marker found in {working_directory}. "
                    "This is a detection result, not ground truth."
                ),
                error_code="BUILD_SYSTEM_NOT_DETECTED",
                facts={
                    "checked": checked,
                    "working_directory": working_directory,
                },
                suggestions=[
                    "Observed fact: no supported build marker exists at the submitted root.",
                    "Constraint: working_directory is never inferred or retargeted.",
                ],
                metadata={
                    "runner_dispatched": False,
                    "working_directory": working_directory,
                },
            )

        if maven_version_requirement is not None and system != "maven":
            return self._parameter_refusal(
                detail=(
                    "maven_version_requirement is consumed only by the Maven backend; "
                    f"the detected backend is {system}"
                ),
                verb=verb,
                working_directory=working_directory,
                system=system,
            )

        backend = self._backends.get(system)
        if backend is None:
            return ToolResult.completed_failure(
                output=f"No backend for {system}",
                error="backend unavailable",
                error_code="BUILD_BACKEND_UNAVAILABLE",
                metadata={"runner_dispatched": False},
            )

        # On Python ``deps`` the public args value is a direct installer
        # target.  That path is entirely disabled until a typed shared
        # PolicyClaim authority exists, and the refusal precedes manifest or
        # preflight reads. Empty/whitespace args were already rejected above;
        # omitting args installs only project-declared dependencies.
        pin_refusal = self._pin_without_provenance_refusal(
            verb=verb,
            system=system,
            args=args,
            working_directory=working_directory,
        )
        if pin_refusal is not None:
            return pin_refusal
        dependency_pin_claim_ids: List[str] = []

        # --- typed native affordance (spec §C8) — PRE-MATERIALIZATION -------
        # Validate the allowlists, resolve the platform feature and PROVE the
        # provenance before anything is materialized. A native call that gets
        # past here is one the evidence directory already authorizes; one that
        # does not never reaches a backend, so it cannot install a package or
        # set an environment variable on the strength of a model parameter.
        native_bundle: Optional[Dict[str, Any]] = None
        if verb == "native":
            gate = self._native_intent(
                features=features,
                definitions=definitions,
                system=system,
                working_directory=working_directory,
            )
            if isinstance(gate, ToolResult):
                return gate
            native_bundle = gate
        # --- end typed native affordance ------------------------------------

        # --- claim-backed exact pins on `deps` (plan §Stage E item 3) -------
        # `deps` args are a passthrough for the JVM backends (a `-pl` selection
        # is the caller's own scoping). On the python backend an arg IS an
        # install target, so it may only ever be a literal pin a dependency
        # claim already states.
        # --- end claim-backed exact pins ------------------------------------

        # --- domain-edge execution law (spec §C2) — PRE-MATERIALIZATION -----
        # Runs before the island promotion, the JDK pre-flight and any backend
        # touches argv, so a doomed consumer is structurally incapable of
        # reaching a runner. Everything below this block assumes the edges
        # already permitted this invocation.
        edge_refusal = self._domain_edge_refusal(verb, working_directory, requirements)
        if edge_refusal is not None:
            return edge_refusal
        # --- end domain-edge execution law ----------------------------------

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
        jdk_store: Optional[EnvOverlayStore] = None
        runtime_target_sha: Optional[str] = None
        runtime_scope: Dict[str, str] = {}
        runtime_resolution: Dict[str, Any] = {}
        effective_jdk: Optional[Dict[str, Any]] = None
        if effective_verb != verb:
            preamble_lines.append(
                "[island] "
                f"requested {verb}; surveyed goal {island_context['manifest_goal']} "
                f"at {island_context['island_root']}; executing install"
            )
        if effective_verb in _PREFLIGHT_VERBS and system != "python":
            runtime_target_sha = probe_target_sha(
                self.docker_orchestrator.execute_command,
                working_directory,
            )
            runtime_scope = _runtime_domain_scope(requirements, working_directory)
            static_major = str(requirements.get("java_version") or "").strip() or None
            static_source = str(requirements.get("java_version_source") or "").strip() or "unknown"
            if runtime_target_sha and runtime_scope:
                jdk_store = EnvOverlayStore(self.docker_orchestrator)
                try:
                    runtime_resolution = jdk_store.resolve_runtime_requirement(
                        "java",
                        target_sha=runtime_target_sha,
                        domain_root=runtime_scope["domain_root"],
                        domain_id=runtime_scope.get("domain_id"),
                        static_major=static_major,
                        static_source=static_source,
                    )
                except Exception as exc:
                    # An unreadable dynamic store cannot honestly license a
                    # static downgrade: whether a newer runner fact exists is
                    # unknown.  Refuse before contract freeze / dispatch.
                    logger.warning(f"dynamic Java requirement state unavailable: {exc}")
                    return ToolResult.completed_failure(
                        output=(
                            "[pre-flight] dynamic Java requirement state could not be read; "
                            "no JVM runner was dispatched"
                        ),
                        error="dynamic Java requirement state unavailable",
                        error_code=RUNTIME_REQUIREMENT_STATE_UNAVAILABLE,
                        facts={
                            "target_sha": runtime_target_sha,
                            **runtime_scope,
                        },
                        metadata={"runner_dispatched": False},
                    )
            elif static_major:
                runtime_resolution = {
                    "required_major": static_major,
                    "authority": "static_survey",
                    "provenance": {"source": static_source},
                }
            runtime_conflict = runtime_resolution.get("conflict")
            if isinstance(runtime_conflict, Mapping):
                conflict_detail = dict(runtime_conflict)
                write_assessment(
                    self.docker_orchestrator.execute_command,
                    ControlAssessment(
                        event_or_intent_id=next_control_event_id("jdk-runtime"),
                        stage="precondition",
                        typed_code=RUNTIME_REQUIREMENT_CONFLICT,
                        detail=(
                            f"{runtime_target_sha} {runtime_scope} has conflicting Java "
                            f"requirements {conflict_detail.get('required_majors')}"
                        ),
                    ),
                )
                return ToolResult.completed_failure(
                    output=(
                        "[pre-flight] runner evidence states conflicting Java runtime "
                        "requirements for this exact checkout and build domain; no JVM "
                        "runner was dispatched"
                    ),
                    error="conflicting Java runtime requirements",
                    error_code=RUNTIME_REQUIREMENT_CONFLICT,
                    facts={
                        "target_sha": runtime_target_sha,
                        **runtime_scope,
                        **conflict_detail,
                    },
                    metadata={"runner_dispatched": False},
                )
            outcome = JdkPreflight(self.docker_orchestrator).run(
                runtime_resolution.get("required_major"),
                source=(
                    str(runtime_resolution.get("authority") or "unknown")
                    + ":"
                    + str(
                        (runtime_resolution.get("provenance") or {}).get("source_ref")
                        or (runtime_resolution.get("provenance") or {}).get("source")
                        or "unknown"
                    )
                ),
            )
            if not outcome.active_version:
                dispatch_runtime = active_java_runtime(self.docker_orchestrator)
                if dispatch_runtime.get("major"):
                    outcome.active_version = dispatch_runtime["major"]
            if (
                outcome.provisioned
                and jdk_store is not None
                and runtime_target_sha
                and runtime_scope
                and runtime_resolution.get("authority") == "persisted_dynamic"
            ):
                provenance = runtime_resolution.get("provenance") or {}
                source_ref = str(provenance.get("source_ref") or "").strip()
                if source_ref:
                    try:
                        post_runtime = active_java_runtime(self.docker_orchestrator)
                        jdk_store.confirm_runtime_requirement(
                            "java",
                            target_sha=runtime_target_sha,
                            domain_root=runtime_scope["domain_root"],
                            domain_id=runtime_scope.get("domain_id"),
                            source_ref=source_ref,
                            active_runtime=post_runtime,
                        )
                        refreshed = [
                            record
                            for record in jdk_store.scoped_runtime_requirements(
                                "java",
                                target_sha=runtime_target_sha,
                                domain_root=runtime_scope["domain_root"],
                                domain_id=runtime_scope.get("domain_id"),
                            )
                            if record.get("source_ref") == source_ref
                            and record.get("required_major")
                            == runtime_resolution.get("required_major")
                        ]
                        if refreshed:
                            runtime_resolution["provenance"] = max(
                                refreshed,
                                key=lambda record: int(record.get("observed_sequence", 0)),
                            )
                    except Exception as exc:
                        logger.warning(f"dynamic Java activation state did not persist: {exc}")
                        return ToolResult.completed_failure(
                            output=(
                                "[pre-flight] Java provisioning passed its dispatch probe, "
                                "but the scoped activation fact did not persist; no JVM "
                                "runner was dispatched"
                            ),
                            error="dynamic Java activation state unavailable",
                            error_code=RUNTIME_REQUIREMENT_STATE_UNAVAILABLE,
                            facts={
                                "target_sha": runtime_target_sha,
                                **runtime_scope,
                                "required_major": runtime_resolution.get("required_major"),
                            },
                            metadata={"runner_dispatched": False},
                        )
            effective_jdk = _effective_jdk_binding(runtime_resolution, outcome)
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

        # --- Pre-dispatch contract freeze (Plan 6 Stage B, spec §C3) ---
        # Materialize the effective action and its argv WITHOUT dispatching,
        # freeze that materialization, and dispatch only once the contract is
        # on disk. A contract that did not land has no dispatch authority, so
        # the refusal below is the whole point: the physical command never runs.
        if system == "maven":
            materialized = backend.materialize(
                effective_verb,
                args,
                working_directory,
                timeout,
                maven_version_requirement=maven_version_requirement,
            )
        elif native_bundle is not None:
            materialized = backend.materialize(
                effective_verb,
                args,
                working_directory,
                timeout,
                native=native_bundle["native"],
            )
        else:
            materialized = backend.materialize(effective_verb, args, working_directory, timeout)

        effective_action = backend.effective_action(materialized)
        expected_argv = backend.expected_argv(materialized)

        contract = freeze_contract(
            self.docker_orchestrator.execute_command,
            run_id=active_receipt_run_id(),
            envelope_id=envelope_id,
            tool=self.name,
            params=requested_call_params,
            effective_tool=system,
            effective_action=effective_action,
            expected_cwd=working_directory,
            expected_argv=expected_argv,
            execution_binding=backend.EXECUTION_BINDING,
            intent_source=scope.intent_source,
            intent_id=scope.intent_id,
            intent_domain_id=scope.intent_domain_id,
            intent_exact_params=scope.intent_exact_params,
            action_fingerprint=scope.action_fingerprint,
            trigger_assessment_id=scope.trigger_assessment_id,
            repair_context_id=scope.repair_context_id,
            repair_context_sha256=scope.repair_context_sha256,
            requirements=requirements,
            # Plan 6 Stage F1: the document-map pin the assessor compares
            # against (`_current_fingerprints` already reads the same stamp).
            # It comes from the survey manifest this call ALREADY holds, so the
            # pin costs no probe; absent from the stamp means absent from the
            # contract, which is what a session with no map states.
            document_map_fingerprint=(
                (requirements.get("survey") or {}).get("document_map_fingerprint")
                if isinstance(requirements, Mapping)
                else None
            ),
            # Spec §C8: "the contract stores claim IDs". The ids come from the
            # gate's lookup of stored evidence, never from a call parameter,
            # so a frozen native contract carries the provenance that
            # authorized it and a reader can follow it back to the documents.
            supporting_claim_ids=(
                (native_bundle or {}).get("supporting_claim_ids") or dependency_pin_claim_ids
            ),
            predecessor_contract_id=scope.predecessor_contract_id,
            effective_jdk=effective_jdk,
            target_sha_value=runtime_target_sha,
        )
        if contract is None:
            write_assessment(
                self.docker_orchestrator.execute_command,
                ControlAssessment(
                    event_or_intent_id=envelope_id,
                    stage="materialization",
                    typed_code=CONTRACT_PERSIST_FAILED,
                    detail=(
                        f"{system} {effective_verb} at {working_directory} was not "
                        "dispatched: its invocation contract did not reach disk"
                    ),
                ),
            )
            return ToolResult.completed_failure(
                output="\n".join(
                    preamble_lines
                    + [
                        "[contract] the invocation contract for this dispatch could not be "
                        f"persisted under {CONTRACT_DIR}, so {system} {effective_verb} was "
                        "NOT run. Nothing was built and no evidence was produced."
                    ]
                ),
                error="invocation contract not persisted",
                error_code=CONTRACT_PERSIST_FAILED,
                facts={
                    "system": system,
                    "requested_action": verb,
                    "effective_action": effective_verb,
                    "working_directory": working_directory,
                },
                metadata={"runner_dispatched": False},
                suggestions=[
                    "Check that /workspace/.setup_agent is writable in the container",
                    "Retry the same build call once the workspace accepts writes",
                ],
            )

        def _execute_backend():
            if system == "maven":
                return backend.execute(
                    effective_verb,
                    args,
                    working_directory,
                    timeout,
                    maven_version_requirement=maven_version_requirement,
                    params=materialized,
                    requirements=requirements,
                )
            if system == "gradle":
                return backend.execute(
                    effective_verb,
                    args,
                    working_directory,
                    timeout,
                    params=materialized,
                    requirements=requirements,
                )
            return backend.execute(
                effective_verb,
                args,
                working_directory,
                timeout,
                params=materialized,
            )

        # Each physical dispatch owns the runtime-bound contract active at that
        # moment.  A retry under a runner-observed JDK must not borrow the first
        # dispatch's static-runtime commitment.
        actual_executions = []
        execution_contracts: List[Mapping[str, Any]] = []
        with dispatch_contract(contract):
            actual_executions.append(_execute_backend())
        execution_contracts.append(contract)
        inner = actual_executions[-1].result

        # Bounded retry (spec §1c): one physical failure may authorize at most
        # one retry.  The runner's version wording is persisted BEFORE the
        # environment changes, then the same dispatch environment must verify
        # the new runtime before a new contract can freeze.
        if outcome is not None and not inner.succeeded:
            failure_text = "\n".join(t for t in (inner.output, inner.raw_output) if t)
            needed = classify_version_error(failure_text)
            active = outcome.active_version or active_java_major(self.docker_orchestrator)
            if needed and needed != active:
                retry_resolution: Dict[str, Any] = {
                    "required_major": needed,
                    "authority": "runner_observed",
                    "provenance": {},
                }
                source_ref = str(
                    (inner.metadata or {}).get("receipt_id")
                    or getattr(inner, "output_ref", None)
                    or (inner.metadata or {}).get("output_ref_id")
                    or ""
                ).strip()
                observation: Optional[Dict[str, Any]] = None
                if jdk_store is not None and runtime_target_sha and runtime_scope:
                    if not source_ref:
                        preamble_lines.append(
                            "[pre-flight] the Java requirement had no durable receipt/output "
                            "reference; automatic runtime retry was not authorized"
                        )
                        needed = None
                    else:
                        observed_runtime = active_java_runtime(self.docker_orchestrator)
                        try:
                            jdk_store.record_runtime_requirement(
                                "java",
                                target_sha=runtime_target_sha,
                                domain_root=runtime_scope["domain_root"],
                                domain_id=runtime_scope.get("domain_id"),
                                required_major=needed,
                                source_ref=source_ref,
                                observed_runtime=observed_runtime,
                            )
                            observation = next(
                                record
                                for record in jdk_store.scoped_runtime_requirements(
                                    "java",
                                    target_sha=runtime_target_sha,
                                    domain_root=runtime_scope["domain_root"],
                                    domain_id=runtime_scope.get("domain_id"),
                                )
                                if record.get("source_ref") == source_ref
                                and record.get("required_major") == needed
                            )
                            retry_resolution = jdk_store.resolve_runtime_requirement(
                                "java",
                                target_sha=runtime_target_sha,
                                domain_root=runtime_scope["domain_root"],
                                domain_id=runtime_scope.get("domain_id"),
                                static_major=requirements.get("java_version"),
                                static_source=requirements.get("java_version_source"),
                                runner_observed=observation,
                            )
                        except Exception as exc:
                            logger.warning(f"runner-observed Java requirement not persisted: {exc}")
                            preamble_lines.append(
                                "[pre-flight] the runner-observed Java requirement could not "
                                "be persisted; automatic runtime retry was not authorized"
                            )
                            needed = None

                retry_conflict = retry_resolution.get("conflict")
                if isinstance(retry_conflict, Mapping):
                    inner.metadata["runtime_requirement_conflict"] = dict(retry_conflict)
                    if RUNTIME_REQUIREMENT_CONFLICT not in inner.conflicts:
                        inner.conflicts.append(RUNTIME_REQUIREMENT_CONFLICT)
                    write_assessment(
                        self.docker_orchestrator.execute_command,
                        ControlAssessment(
                            event_or_intent_id=next_control_event_id("jdk-runtime"),
                            stage="postcondition",
                            typed_code=RUNTIME_REQUIREMENT_CONFLICT,
                            detail=(
                                f"runner observation {source_ref or 'current-output'} conflicts "
                                f"with same-scope Java requirements "
                                f"{retry_conflict.get('required_majors')}"
                            ),
                        ),
                    )
                    preamble_lines.append(
                        "[pre-flight] the runner observation conflicts with an existing "
                        "same-scope Java requirement; automatic runtime retry was not authorized"
                    )
                    needed = None

                if needed:
                    retry_outcome = JdkPreflight(self.docker_orchestrator).run(
                        needed,
                        source=f"runner_observed:{source_ref or 'current-output'}",
                    )
                    if retry_outcome.provisioned:
                        post_runtime = active_java_runtime(self.docker_orchestrator)
                        if jdk_store is not None and observation is not None:
                            assert runtime_target_sha is not None
                            assert runtime_scope.get("domain_root")
                            try:
                                jdk_store.confirm_runtime_requirement(
                                    "java",
                                    target_sha=runtime_target_sha,
                                    domain_root=runtime_scope["domain_root"],
                                    domain_id=runtime_scope.get("domain_id"),
                                    source_ref=source_ref,
                                    active_runtime=post_runtime,
                                )
                                # Carry the confirmed record, including active
                                # runtime, into contract/receipt provenance.
                                observation = next(
                                    record
                                    for record in jdk_store.scoped_runtime_requirements(
                                        "java",
                                        target_sha=runtime_target_sha,
                                        domain_root=runtime_scope["domain_root"],
                                        domain_id=runtime_scope.get("domain_id"),
                                    )
                                    if record.get("source_ref") == source_ref
                                    and record.get("required_major") == needed
                                )
                                retry_resolution = {
                                    "required_major": needed,
                                    "authority": "runner_observed",
                                    "provenance": observation,
                                }
                            except Exception as exc:
                                logger.warning(f"confirmed Java runtime state not persisted: {exc}")
                                preamble_lines.append(
                                    "[pre-flight] Java changed, but its exact-scope "
                                    "postcondition record did not persist; retry was not dispatched"
                                )
                                retry_outcome.provisioned = False
                        if retry_outcome.provisioned:
                            retry_effective_jdk = _effective_jdk_binding(
                                retry_resolution,
                                retry_outcome,
                            )
                            retry_contract = freeze_contract(
                                self.docker_orchestrator.execute_command,
                                run_id=active_receipt_run_id(),
                                envelope_id=envelope_id,
                                tool=self.name,
                                params=requested_call_params,
                                effective_tool=system,
                                effective_action=effective_action,
                                expected_cwd=working_directory,
                                expected_argv=expected_argv,
                                execution_binding=backend.EXECUTION_BINDING,
                                intent_source=scope.intent_source,
                                intent_id=scope.intent_id,
                                intent_domain_id=scope.intent_domain_id,
                                intent_exact_params=scope.intent_exact_params,
                                action_fingerprint=scope.action_fingerprint,
                                trigger_assessment_id=scope.trigger_assessment_id,
                                repair_context_id=scope.repair_context_id,
                                repair_context_sha256=scope.repair_context_sha256,
                                requirements=requirements,
                                document_map_fingerprint=(
                                    (requirements.get("survey") or {}).get(
                                        "document_map_fingerprint"
                                    )
                                    if isinstance(requirements, Mapping)
                                    else None
                                ),
                                supporting_claim_ids=(
                                    (native_bundle or {}).get("supporting_claim_ids")
                                    or dependency_pin_claim_ids
                                ),
                                predecessor_contract_id=contract.get("contract_id"),
                                effective_jdk=retry_effective_jdk,
                                target_sha_value=runtime_target_sha,
                            )
                            if retry_contract is not None:
                                preamble_lines.append(
                                    f"[pre-flight] build error requires Java {needed}, "
                                    "re-provisioned, retry 1/1"
                                )
                                jdk_retry_meta = {"from": active, "to": needed}
                                with dispatch_contract(retry_contract):
                                    actual_executions.append(_execute_backend())
                                execution_contracts.append(retry_contract)
                                contract = retry_contract
                                inner = actual_executions[-1].result

        steering = self._java_provision_steering(inner, outcome, jdk_retry_meta)
        if steering:
            preamble_lines.append(steering)

        # --- contract-vs-receipt assessment (Plan 6 Stage C, spec §C5) ------
        # What the dispatch MEANT is decided here, against the contract that
        # authorized it — never inside the runner, which only knows what it did.
        self._assess_receipts(requirements, actual_executions, execution_contracts)

        # Computed last so it lands FIRST: the model must read what actually ran
        # before it reasons about the result (spec §Stage D contract 4).
        delta_line = self._semantic_delta_line(
            backend=backend,
            requested_verb=verb,
            effective_verb=effective_verb,
            params=actual_executions[-1].params,
            args=args,
            island_context=island_context,
        )
        if delta_line:
            preamble_lines.insert(0, delta_line)

        return self._envelope(
            inner,
            system,
            verb,
            effective_verb,
            working_directory,
            island_context,
            preamble_lines,
            jdk_retry_meta,
            contract,
        ).with_execution_trace(actual_executions)

    @staticmethod
    def _java_provision_steering(
        inner: ToolResult,
        outcome: Any,
        jdk_retry: Optional[Dict[str, Optional[str]]],
    ) -> str:
        """One sentence when a runner named its JDK and the engine did not move.

        Live geode d2r4: `build(action='test')` came back with the Gradle
        plugin's own "Java version 17 or later required, but was 11.0.31", the
        bounded retry above recognized no such wording, and the observation said
        nothing about it. The model's next call was `project(action='env',
        tool='gradle', executable='/usr/lib/jvm/java-17-openjdk-amd64/bin/java')`
        — a guessed amd64 path on an arm64 host — while
        `project(action='provision', java_version='17')`, the same call that had
        installed Java 11 in that very run, was never tried.

        It stays a sentence. Widening the automatic re-provision to these looser
        wordings would let a stray phrase swap the JDK under a working build;
        naming the call costs nothing and cannot corrupt runtime state. It is
        withheld in the two cases where it would be false: a retry that already
        moved to that major has done the thing, and a runtime that already IS
        that major failed for some other reason.
        """
        if outcome is None or inner is None or inner.succeeded:
            return ""
        failure_text = "\n".join(t for t in (inner.output, inner.raw_output) if t)
        needed = classify_runner_java_requirement(failure_text)
        if not needed:
            return ""
        if jdk_retry and str(jdk_retry.get("to") or "") == needed:
            return ""
        active = str(getattr(outcome, "active_version", "") or "")
        if active and active == needed:
            return ""
        # The prose and the typed fact are one statement: `_envelope` copies this
        # metadata onto the result, so what the sentence says is assertable
        # without reading the sentence.
        inner.metadata["runner_java_requirement"] = {
            "required_major": needed,
            "source": "runner_output",
        }
        return (
            f"[toolchain] the runner states it requires Java {needed}; provision it with "
            f"project(action='provision', java_version='{needed}') and re-dispatch this build."
        )

    # --- typed native affordance (spec §C8) ---------------------------------

    def _native_intent(
        self,
        *,
        features: Optional[Sequence[str]],
        definitions: Optional[Mapping[str, str]],
        system: str,
        working_directory: str,
    ) -> Any:
        """The validated, provenance-backed native bundle, or a ToolResult refusal.

        Four gates, in this order, because each one makes the next meaningful:

        1. the ALLOWLISTS — a feature the platform resolver does not name and a
           definition outside `USE_*`/`BUILD_TESTING` = `ON|OFF` are refused
           before they are ever echoed into a command line;
        2. CONSISTENCY — a requested feature whose switch is not turned on, and
           a switch for a feature nobody requested, are the two directions of
           the same contradiction (spec §C8);
        3. the SYSTEM — this is python machinery, and a maven/gradle tree gets
           a plain answer rather than a native install it cannot use;
        4. PROVENANCE — a typed current-run capability assessment AND a typed
           current project claim for every definition, both sealed by the
           host publication ledger. That complete live reader is not wired
           yet, so legacy container JSON cannot pass this gate and native
           dispatch remains fail-closed.

        Every refusal records a `ControlAssessment`: it mints no receipt, and a
        reader of the evidence directory must still learn what was stopped.
        """
        requested = list(features or ())
        supplied = dict(definitions or {})

        unknown = [
            str(feature)
            for feature in requested
            if not isinstance(feature, str) or feature.strip() not in NATIVE_FEATURE_RESOLVER
        ]
        if not requested or unknown:
            named = ", ".join(repr(item) for item in unknown) or "nothing"
            return self._native_refusal(
                error_code=NATIVE_FEATURE_UNKNOWN,
                typed_code=NATIVE_FEATURE_UNKNOWN_CODE,
                headline=(
                    f"[native] the platform resolver states no feature {named}; "
                    f"it resolves {', '.join(sorted(NATIVE_FEATURE_RESOLVER))}"
                ),
                closing=(
                    "a feature is resolved to packages and a probe by the harness, never "
                    "described by the call — an unresolvable name installs nothing"
                ),
                working_directory=working_directory,
                suggestions=[
                    "Name a feature the resolver already carries, or record the "
                    "capability as a project fact first",
                ],
            )
        resolved = [feature.strip() for feature in requested]

        rejected = [
            f"{key}={value}"
            for key, value in supplied.items()
            if not NATIVE_DEFINITION_KEY.match(str(key))
            or str(value) not in NATIVE_DEFINITION_VALUES
        ]
        if not supplied or rejected:
            named = ", ".join(repr(item) for item in rejected) or "nothing"
            return self._native_refusal(
                error_code=NATIVE_DEFINITION_REJECTED,
                typed_code=NATIVE_DEFINITION_REJECTED_CODE,
                headline=(
                    f"[native] {named} is not an allowlisted native definition: keys must "
                    f"match {NATIVE_DEFINITION_KEY.pattern} and values must be "
                    f"{' or '.join(NATIVE_DEFINITION_VALUES)}"
                ),
                closing=(
                    "compiler launchers, toolchain files and escaped or absolute paths are "
                    "not definitions — they are commands wearing a definition's shape"
                ),
                working_directory=working_directory,
                suggestions=[
                    "State the capability switch itself (USE_<FEATURE>=ON|OFF)",
                    "Configure a toolchain through the project's own build files, not a call",
                ],
            )
        validated = {str(key): str(value) for key, value in supplied.items()}

        inconsistent = self._native_inconsistency(resolved, validated)
        if inconsistent:
            return self._native_refusal(
                error_code=NATIVE_DEFINITIONS_INCONSISTENT,
                typed_code=NATIVE_DEFINITIONS_INCONSISTENT_CODE,
                headline=f"[native] {inconsistent}",
                closing=(
                    "features and definitions state the same request twice; a request that "
                    "disagrees with itself names no capability to build"
                ),
                working_directory=working_directory,
                suggestions=[
                    "Turn on exactly the features you name: features=[f] with "
                    "definitions={USE_F: ON}",
                ],
            )

        if system != "python":
            return self._native_refusal(
                error_code=NATIVE_SYSTEM_UNSUPPORTED,
                typed_code=NATIVE_SYSTEM_UNSUPPORTED_CODE,
                headline=(
                    f"[native] build(action='native') re-materializes a PYTHON project's "
                    f"own editable install under CMAKE_ARGS; {working_directory} is a "
                    f"{system} project"
                ),
                closing=(
                    f"a {system} build configures its native parts through its own build "
                    "files, and this facade will not install packages on its behalf"
                ),
                working_directory=working_directory,
                suggestions=[
                    f"Run the {system} build itself: build(action='compile', "
                    f"working_directory='{working_directory}')",
                ],
            )

        assessed = self._capability_absences()
        missing_evidence = [
            f"{CAPABILITY_PREFIX}{feature}"
            for feature in resolved
            if f"{CAPABILITY_PREFIX}{feature}" not in assessed
        ]
        claims = self._definition_claims(validated)
        unsupported = sorted(set(validated) - set(claims))
        if missing_evidence or unsupported:
            missing = "; ".join(
                part
                for part in (
                    (
                        f"no assessment on record states {', '.join(missing_evidence)}"
                        if missing_evidence
                        else ""
                    ),
                    (f"no project claim states {', '.join(unsupported)}" if unsupported else ""),
                )
                if part
            )
            return self._native_refusal(
                error_code=NATIVE_WITHOUT_PROVENANCE,
                typed_code=NATIVE_WITHOUT_PROVENANCE_CODE,
                headline=f"[native] {missing}",
                closing=NATIVE_UNSOURCED_CLAUSE,
                working_directory=working_directory,
                suggestions=[
                    "Required evidence is absent: only a completed project build/test "
                    "receipt can prove the capability absent",
                    "Any subsequent model-owned ordinary ActionIntent must cite the stored "
                    "CI/CMake claims and capability assessment it relies on",
                ],
            )

        supporting: List[str] = []
        for key in sorted(claims):
            for identifier in claims[key]:
                if identifier not in supporting:
                    supporting.append(identifier)
        return {
            "native": {"features": resolved, "definitions": validated},
            "supporting_claim_ids": supporting,
        }

    @staticmethod
    def _native_inconsistency(
        features: Sequence[str],
        definitions: Mapping[str, str],
    ) -> Optional[str]:
        """Why these features and definitions disagree, or None when they agree.

        Both directions, because both are ways to build something nobody asked
        for: a named feature whose switch is absent or OFF requests a
        capability the definitions do not turn on, and a `USE_X` switch whose
        feature is unnamed turns on a capability the request never declared.
        """
        for feature in features:
            key = native_feature_definition(feature)
            value = definitions.get(key)
            if value != "ON":
                stated = f"{key}={value}" if value is not None else f"no {key}"
                return (
                    f"features name {feature!r} but the definitions state {stated} — "
                    f"a requested feature must be turned on by {key}=ON"
                )
        named = {str(feature).strip().lower() for feature in features}
        for key in sorted(definitions):
            feature = native_definition_feature(key)
            if feature is not None and feature not in named:
                return (
                    f"definitions state {key}={definitions[key]} but features do not name "
                    f"{feature!r} — a capability switch needs the feature that resolves it"
                )
        return None

    def _capability_absences(self) -> set:
        """No capability is live authority until its typed binding is complete.

        Historical assessment JSON remains useful for display, but the native
        facade cannot consume it through the forgiving directory reader.  A
        future positive path must bind a host-published assessment to the
        current receipt/run/target/domain and to the exact native producer
        observation before returning a capability here.
        """
        return set()

    def _definition_claims(self, definitions: Mapping[str, str]) -> Dict[str, List[str]]:
        """No definition claim is live authority through the loose JSON view.

        The positive native path is intentionally closed alongside capability
        assessment authority. It reopens only when the claim reader validates
        and host-binds the exact current PolicyClaim set; arbitrary persisted
        strings or supporting ids must never authorize an install.
        """
        return {}

    def _native_refusal(
        self,
        *,
        error_code: str,
        typed_code: str,
        headline: str,
        closing: str,
        working_directory: str,
        suggestions: List[str],
    ) -> ToolResult:
        """One refusal shape for every native gate: no receipt, one control fact."""
        facts: Dict[str, Any] = {
            "requested_action": "native",
            "working_directory": working_directory,
        }
        metadata: Dict[str, Any] = dict(facts)
        metadata["runner_dispatched"] = False
        write_assessment(
            self.docker_orchestrator.execute_command,
            ControlAssessment(
                event_or_intent_id=next_control_event_id("build-native"),
                stage="precondition",
                typed_code=typed_code,
                detail=headline,
            ),
        )
        return ToolResult.completed_failure(
            output="\n".join((headline, closing)),
            error=headline,
            error_code=error_code,
            facts=facts,
            metadata=metadata,
            suggestions=suggestions,
        )

    # --- direct Python dependency targets remain disabled -----------------

    def _pin_without_provenance_refusal(
        self,
        *,
        verb: str,
        system: str,
        args: Optional[str],
        working_directory: str,
    ) -> Optional[ToolResult]:
        """Refuse every non-empty Python `deps` target, or return None.

        On the python backend a `deps` arg is an install TARGET — the harness
        would type it into a `pip install`.  Until the facade and judge share
        one typed PolicyClaim verifier, even an exact-looking pin plus an
        opaque supporting id is insufficient authority. The empty-args path
        still installs the dependency set declared by the project itself.
        """
        requested = str(args or "").strip()
        if verb != "deps" or system != "python" or not requested:
            return None
        detail = f"[deps] direct install target {requested!r} is disabled"
        facts: Dict[str, Any] = {
            "requested_action": verb,
            "requested_args": requested,
            "working_directory": working_directory,
            "system": system,
        }
        metadata: Dict[str, Any] = dict(facts)
        metadata["runner_dispatched"] = False
        write_assessment(
            self.docker_orchestrator.execute_command,
            ControlAssessment(
                event_or_intent_id=next_control_event_id("build-deps-pin"),
                stage="precondition",
                typed_code=PIN_WITHOUT_PROVENANCE_CODE,
                detail=detail,
            ),
        )
        return ToolResult.completed_failure(
            output="\n".join(
                (
                    detail,
                    "a python dependency install runs the literal a project document "
                    "already states; an unsourced version is a choice, not a repair",
                )
            ),
            error=detail,
            error_code=PIN_WITHOUT_PROVENANCE,
            facts=facts,
            metadata=metadata,
            suggestions=[
                "Call build(action='deps') with no args to install the project's own "
                "declared dependencies",
                "Record a project dependency in its own config instead of injecting a pin",
            ],
        )

    # --- contract-vs-receipt assessment (spec §C5) --------------------------

    def _assess_receipts(
        self,
        requirements: Mapping[str, Any],
        executions: List[Any],
        contracts: Sequence[Mapping[str, Any]],
    ) -> None:
        """Assess every receipt this facade call minted, and persist the verdicts.

        Each physical dispatch is assessed on its own — the JDK-driven rerun is
        a second dispatch under a new runtime-bound contract, and reading only
        the last receipt would erase the first one's meaning.

        This never changes the result and never raises: the model is waiting on
        a build, and a verdict that could not be written is a missing evidence
        record, not a failed build. A DISPATCHED receipt with no assessment is
        an evidence-closure hole the phase gate should refuse to close over;
        that gate reads the assessment directory and lands in Stage D.
        """
        assessed = set()
        for execution, contract in zip(executions, contracts):
            result = getattr(execution, "result", None)
            metadata = getattr(result, "metadata", None) or {}
            receipt_id = str(metadata.get("receipt_id") or "").strip()
            if not receipt_id or receipt_id in assessed:
                continue
            assessed.add(receipt_id)
            try:
                receipt = read_receipt(self.docker_orchestrator.execute_command, receipt_id)
                if not receipt:
                    logger.debug(f"receipt {receipt_id} could not be read back; not assessed")
                    continue
                assess_dispatch(
                    self.docker_orchestrator.execute_command,
                    contract=contract,
                    receipt=receipt,
                    current_fingerprints=self._current_fingerprints(requirements, receipt),
                    dispatch_status=getattr(result.invocation_status, "value", None),
                    error_code=result.error_code,
                    # The complete runner text, while the facade still holds it:
                    # the receipt keeps only its hash, and a fault the build
                    # stated in prose is readable nowhere else.
                    output=getattr(result, "raw_output", None) or getattr(result, "output", None),
                    evidence_ref=(
                        str(
                            getattr(result, "output_ref", None)
                            or metadata.get("output_ref_id")
                            or ""
                        ).strip()
                        or None
                    ),
                )
            except Exception as exc:  # evidence never breaks the build result
                logger.debug(f"receipt {receipt_id} was not assessed: {exc}")

    @staticmethod
    def _current_fingerprints(
        requirements: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """The pins the harness can state NOW, without a second probe.

        The survey stamp is the current config/document-map pin, and the
        receipt's own target sha is the most recent observation of the tree —
        it was probed AFTER the dispatch, where the contract's was probed
        before. A pin nobody currently states stays absent, so it can never be
        read as a mismatch.
        """
        current: Dict[str, Any] = {}
        survey = requirements.get("survey") if isinstance(requirements, Mapping) else None
        if isinstance(survey, Mapping):
            for key in ("config_fingerprint", "document_map_fingerprint", "survey_fingerprint"):
                value = str(survey.get(key) or "").strip()
                if value:
                    current[key] = value
        target_sha = str((receipt or {}).get("target_sha") or "").strip()
        if target_sha:
            current["target_sha"] = target_sha
        current_cwd = str(
            (receipt or {}).get("actual_cwd") or (receipt or {}).get("working_directory") or ""
        ).strip()
        domain_root = nearest_domain_root(requirements, current_cwd)
        if domain_root:
            current["domain_id"] = domain_root
        fact_epoch = nearest_domain_fact_epoch(requirements, current_cwd)
        if fact_epoch is not None:
            current["fact_epoch"] = fact_epoch
        import_targets = (
            python_import_targets(requirements)
            if str((receipt or {}).get("tool") or "").strip().lower() == "python"
            else None
        )
        if import_targets is not None:
            current["python_import_targets"] = import_targets
            current["python_import_targets_sha256"] = producer_observations_sha256(import_targets)
        return current

    # --- domain-edge execution law (spec §C2) -------------------------------

    def _domain_edge_refusal(
        self,
        verb: str,
        working_directory: str,
        requirements: Dict[str, Any],
    ) -> Optional[ToolResult]:
        """Refuse a build the manifest's edges already doom, or None to proceed.

        Spec §C2 gives dependency edges an EXECUTION law: a
        `version_incompatible` consumer "is sealed blocked ... and receives no
        runner invocation", and an `unverified` consumer's build/test dispatch
        "is locked". Plan 5 derived both statuses honestly and dispatched
        anyway, so every stale bigtop consumer spent a full reactor run
        rediscovering a mismatch the manifest had already stated.

        The refusal is a control fact, not a build result: it mints no receipt,
        so it also records a `ControlAssessment` — a reader who only sees the
        evidence directory must still learn that this intent was stopped and
        why. Persisting that is best effort and never gates the refusal.
        """
        if verb not in _EDGE_GATED_VERBS:
            return None
        edge = self._binding_domain_edge(working_directory, requirements)
        if edge is None:
            return None
        status = str(edge.get("status") or "").strip().lower()
        error_code, typed_code = _EDGE_REFUSALS[status]
        consumer = _absolute_root(edge.get("consumer")) or str(edge.get("consumer") or "")
        producer = _absolute_root(edge.get("producer")) or str(edge.get("producer") or "")
        # Raw detail is project-authored survey material.  Keep it in the typed
        # internal assessment for audit, but never let arbitrary prose select a
        # producer-first repair order in the model-facing result.
        detail = str(edge.get("detail") or "").strip()

        if status == "version_incompatible":
            headline = (
                "[domain-edge] status=version_incompatible "
                f"consumer={consumer} producer={producer}"
            )
            closing = (
                "runner dispatch is blocked because the recorded edge status is "
                "version_incompatible"
            )
            error = f"domain edge blocks {consumer}"
        else:
            headline = "[domain-edge] status=unverified " f"consumer={consumer} producer={producer}"
            closing = "runner dispatch remains locked; the edge has no current verification receipt"
            error = f"domain edge is unverified for {consumer}"

        facts: Dict[str, Any] = {
            "domain_edge_status": status,
            "domain_edge_consumer": consumer,
            "domain_edge_producer": producer,
            "requested_action": verb,
            "working_directory": working_directory,
        }
        metadata: Dict[str, Any] = dict(facts)
        metadata["runner_dispatched"] = False
        edge_id = str(edge.get("edge_id") or "").strip()
        if edge_id:
            facts["edge_id"] = edge_id
            metadata["edge_id"] = edge_id
        write_assessment(
            self.docker_orchestrator.execute_command,
            ControlAssessment(
                event_or_intent_id=next_control_event_id("build-domain-edge"),
                stage="precondition",
                typed_code=typed_code,
                detail=detail or headline,
            ),
        )
        return ToolResult.completed_failure(
            output="\n".join((headline, closing)),
            error=error,
            error_code=error_code,
            facts=facts,
            metadata=metadata,
            suggestions=[],
        )

    @staticmethod
    def _binding_domain_edge(
        working_directory: str,
        requirements: Dict[str, Any],
    ) -> Optional[Mapping[str, Any]]:
        """The edge that governs this invocation, or None when none does.

        Nearest-root binding. The invocation binds to the DEEPEST edge endpoint
        that contains its working directory, and only a binding to a CONSUMER
        refuses:

        * an aggregator above both endpoints contains neither, so it is nobody's
          consumer — a reactor build from the top is not the blocked module;
        * a producer nested inside a consumer root binds to the producer,
          because producing first is exactly what the edge asks for.

        `version_incompatible` outranks `unverified` at the same root: proven
        wrong is a stronger fact than unknown.
        """
        target = _absolute_root(working_directory)
        if target is None:
            return None
        edges = _manifest_domain_edges(requirements)
        nearest = ""
        for item in edges:
            for role in ("consumer", "producer"):
                root = _absolute_root(item.get(role))
                if root is not None and _is_contained(target, root) and len(root) > len(nearest):
                    nearest = root
        if not nearest:
            return None
        for status in ("version_incompatible", "unverified"):
            for item in edges:
                if str(item.get("status") or "").strip().lower() != status:
                    continue
                if _absolute_root(item.get("consumer")) == nearest:
                    return item
        return None

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

        survey_root = str(((requirements.get("survey") or {}).get("project_path") or "")).strip()

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

    @staticmethod
    def _semantic_delta_line(
        *,
        backend,
        requested_verb: str,
        effective_verb: str,
        params: Dict[str, Any],
        args: Optional[str],
        island_context: Optional[Dict[str, str]],
    ) -> Optional[str]:
        """`[build] requested X -> executing Y (why)` when the action MUTATED.

        Pure task-name translation stays silent: compile -> compileJava renames
        the same lifecycle. A promotion, a verb substitution or an added skip flag
        changes what the build MEANS, and every live failure of that kind started
        with a mutation the model was never shown (spec §Stage D contract 4).
        """
        executed = backend.executed_action(effective_verb, params, args)
        reasons: List[str] = []
        if effective_verb != requested_verb:
            goal = (island_context or {}).get("manifest_goal") or effective_verb
            reasons.append(f"promoted to {effective_verb} by the surveyed island goal {goal}")
        reasons.extend(executed.reasons)
        if not reasons:
            return None
        return (
            f"[build] requested '{requested_verb}' -> executing "
            f"'{executed.argv_fragment}' ({'; '.join(reasons)})"
        )

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
        contract: Optional[Dict[str, Any]] = None,
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
        # The judgment word is the dispatch's own: did the invocation run its
        # selected tests to a terminal state (spec §2.1). A pass rate used to
        # rewrite it here — 79% of a suite that ran completely became "failed"
        # while 81% became "partial". Red counts stay exact sealed facts on the
        # facts dict and the receipt; they adjudicate nothing.
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
        # The contract this dispatch was frozen against travels with the result
        # (plan §Stage B): envelope -> contract -> receipt is the chain the
        # verifier walks, and the control event only ever sees the metadata.
        for key in ("contract_id", "contract_hash"):
            value = str((contract or {}).get(key) or "").strip()
            if value:
                metadata[key] = value
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
                    "enum": list(_ACTIONS),
                    "description": "What to do; the build system is auto-selected. "
                    "Use install for a multi-module reactor whose modules depend on "
                    "siblings' built artifacts (shaded jars, code-gen). native "
                    "re-installs a python project with a native capability enabled, "
                    "and only after a receipt proved that capability absent.",
                },
                "args": {
                    "type": "string",
                    "description": "Extra flags passed through to the underlying tool. "
                    "Omit it for Python deps: that action installs only dependencies "
                    "declared by the project; direct install targets are disabled.",
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
                "features": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "action=native only: the named capabilities to enable, for "
                        f"example {list(sorted(NATIVE_FEATURE_RESOLVER))}. The harness "
                        "resolves each to packages and a probe; the call never names "
                        "either. Dispatch remains unavailable until the typed "
                        "current-run capability binding is present."
                    ),
                },
                "definitions": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "action=native only: build definitions, "
                        f"{NATIVE_DEFINITION_KEY.pattern} = "
                        f"{'|'.join(NATIVE_DEFINITION_VALUES)}. Must agree with "
                        "features, and each key needs a host-published typed current "
                        "project claim; loose stored JSON never authorizes dispatch."
                    ),
                },
            },
            "required": ["action"],
        }
