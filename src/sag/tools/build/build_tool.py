"""build(action: deps|compile|test|package|install|native) — one tool over all
ecosystems."""

import posixpath
import re
import shlex
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from sag.agent.claim_records import CLAIM_DIR
from sag.agent.evidence_assessments import (
    ASSESSMENT_DIR,
    CAPABILITY_PREFIX,
    ControlAssessment,
    assess_dispatch,
    next_control_event_id,
    read_receipt,
    write_assessment,
)
from sag.agent.invocation_contracts import (
    CONTRACT_DIR,
    CONTRACT_PERSIST_FAILED,
    current_action_context,
    dispatch_contract,
    freeze_contract,
    unrecorded_envelope_id,
)
from sag.agent.repair_contracts import read_records
from sag.agent.retry_authority import (
    RETRY_WITHOUT_DELTA,
    RETRY_WITHOUT_DELTA_CODE,
    blocking_entry,
    candidate_contract,
    read_ledger,
)
from sag.config.settings import DEFAULT_TEST_PASS_THRESHOLD
from sag.tools.base import BaseTool, ToolResult
from sag.tools.internal.build_preflight import (
    JdkPreflight,
    REQUIREMENTS_PATH,
    active_java_major,
    classify_version_error,
    read_build_requirements,
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

# --- claim-backed exact pins on the deps verb (plan §Stage E item 3) --------
PIN_WITHOUT_PROVENANCE = "PIN_WITHOUT_PROVENANCE"
PIN_WITHOUT_PROVENANCE_CODE = "pin_without_provenance"
# `pkg==literal` and nothing else. A range, an extra, a flag or a URL is not an
# exact pin, and the only args this verb accepts are exact pins a claim states.
EXACT_PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.*+!_-]*$")
# The claim scopes whose typed_value may state a native definition. `env`
# claims carry all four (a CI `CMAKE_ARGS` assignment, a `-DUSE_X=ON` inside
# it, `set(USE_X ...)` and `option(USE_X ...)`), and nothing else does.
NATIVE_CLAIM_SCOPES = ("environment", "cmake_definition", "cmake_set", "cmake_option")

# The edge statuses that lock a consumer, worst first. `compatible` unlocks and
# `not_applicable` disposes, so neither appears here.
_EDGE_REFUSALS = {
    "version_incompatible": ("DOMAIN_EDGE_BLOCKED", "domain_edge_blocked"),
    "unverified": ("DOMAIN_EDGE_UNVERIFIED", "domain_edge_unverified"),
}
_SEALED_CLAUSE = "this consumer is sealed blocked; record the mismatch, do not silently alias"


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

        # Whether the caller scoped this invocation itself. PR #12's
        # orchestration layer owns working-directory injection, so the facade
        # never re-targets; explicitness only gates the [scope] warning below.
        explicitly_scoped = working_directory not in (None, "", "/workspace")
        # The call AS SUBMITTED. The facade may re-target the working directory
        # below; the contract records both, because a normalization the caller
        # never asked for is exactly the kind of fact §C3 keeps separate.
        # `provenance` is deliberately NOT a parameter here (spec §C8): the
        # supporting claim ids are looked up from stored evidence, so nothing
        # the model writes can appear in them.
        requested_call_params = {
            "action": verb,
            "args": args,
            "working_directory": working_directory,
            "timeout": timeout,
            "maven_version_requirement": maven_version_requirement,
            "features": list(features) if features is not None else None,
            "definitions": dict(definitions) if definitions is not None else None,
        }

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
        pin_refusal = self._pin_without_provenance_refusal(
            verb=verb, system=system, args=args, working_directory=working_directory
        )
        if pin_refusal is not None:
            return pin_refusal
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

        # --- material-progress retry law (spec §C7) — PRE-FREEZE ------------
        # The CONTROLLER signs recurrence after each failure-class assessment;
        # this facade only validates it and keeps no second store of its own.
        # A refused dispatch must leave nothing behind, so the check runs
        # BEFORE the freeze: no contract, no receipt, no runner.
        retry_refusal = self._retry_without_delta_refusal(
            system=system,
            requested_verb=verb,
            effective_verb=effective_verb,
            effective_action=effective_action,
            working_directory=working_directory,
            expected_argv=expected_argv,
            requirements=requirements,
            preamble_lines=preamble_lines,
        )
        if retry_refusal is not None:
            return retry_refusal
        # --- end material-progress retry law --------------------------------

        scope = current_action_context()
        envelope_id = scope.envelope_id or unrecorded_envelope_id()
        contract = freeze_contract(
            self.docker_orchestrator.execute_command,
            envelope_id=envelope_id,
            tool=self.name,
            params=requested_call_params,
            effective_action=effective_action,
            expected_cwd=working_directory,
            expected_argv=expected_argv,
            intent_source=scope.intent_source,
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
            supporting_claim_ids=(native_bundle or {}).get("supporting_claim_ids"),
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
                )
            return backend.execute(
                effective_verb,
                args,
                working_directory,
                timeout,
                params=materialized,
            )

        # The contract is bound for the dispatch only: the runner reads it to
        # bind its receipt back, and nothing outside this block inherits it.
        with dispatch_contract(contract):
            actual_executions = [_execute_backend()]
            inner = actual_executions[-1].result

            # Bounded retry (spec §1c): a version-shaped failure means the JDK in
            # the error text is authoritative (static analysis cannot always see
            # it); re-provision from it and rerun EXACTLY once, never more. The
            # rerun is the SAME materialized argv, so it runs under the same
            # frozen contract.
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

        # --- contract-vs-receipt assessment (Plan 6 Stage C, spec §C5) ------
        # What the dispatch MEANT is decided here, against the contract that
        # authorized it — never inside the runner, which only knows what it did.
        self._assess_receipts(contract, requirements, actual_executions)

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
        4. PROVENANCE — a `capability_absent_<feature>` assessment on record
           AND a stored claim for every definition. Model parameters carry no
           provenance, so this is the only place authority can come from.

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
                    "Run the project's own test/build first — a receipt is what proves a "
                    "capability absent",
                    "Let targeted retrieval read the project's CI/CMake documents, then "
                    "accept the repair it proposes",
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
        """Every `capability_absent_<name>` code the assessment directory holds.

        The assessments are the c2 assessor's own output: a capability is
        absent because a RECEIPT said so, never because a call said so.
        """
        return {
            str(record.get("typed_code") or "").strip()
            for record in read_records(self.docker_orchestrator, ASSESSMENT_DIR)
            if str(record.get("typed_code") or "").strip().startswith(CAPABILITY_PREFIX)
        }

    def _definition_claims(self, definitions: Mapping[str, str]) -> Dict[str, List[str]]:
        """`{definition key: [claim_id, ...]}` for the keys stored claims state.

        A key with no claim is simply absent from the mapping — that absence is
        what the provenance gate refuses on. Matching is on the definition NAME
        the claim carries, because the claim is what proves the switch is
        project-owned; which value the project's own default happens to be is a
        separate fact, and reading it as consent would let a documented
        `set(USE_X OFF)` authorize nothing at all.
        """
        found: Dict[str, List[str]] = {}
        for record in read_records(self.docker_orchestrator, CLAIM_DIR):
            if str(record.get("kind") or "").strip() != "env":
                continue
            typed_value = record.get("typed_value")
            if not isinstance(typed_value, Mapping):
                continue
            if str(typed_value.get("scope") or "").strip() not in NATIVE_CLAIM_SCOPES:
                continue
            name = str(typed_value.get("name") or "").strip()
            identifier = str(record.get("claim_id") or "").strip()
            if not identifier or name not in definitions:
                continue
            found.setdefault(name, [])
            if identifier not in found[name]:
                found[name].append(identifier)
        return found

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

    # --- claim-backed exact pins on `deps` (plan §Stage E item 3) -----------

    def _pin_without_provenance_refusal(
        self,
        *,
        verb: str,
        system: str,
        args: Optional[str],
        working_directory: str,
    ) -> Optional[ToolResult]:
        """Refuse a python `deps` arg no dependency claim states, or None.

        On the python backend a `deps` arg is an install TARGET — the harness
        would type it into a `pip install`. So it may only ever be an exact pin
        (`pkg==literal`) that a stored dependency claim already carries. Every
        other arg, pin-shaped or not, is a package choice with no project
        source behind it.
        """
        requested = str(args or "").strip()
        if verb != "deps" or system != "python" or not requested:
            return None
        pinned = EXACT_PIN.match(requested) is not None
        if pinned and self._pin_claim_ids(requested):
            return None
        detail = (
            f"[deps] {requested!r} is not an exact pin (pkg==literal)"
            if not pinned
            else f"[deps] no dependency claim states the pin {requested!r}"
        )
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
                "Accept a repair proposal — its pin is cited from a stored claim",
            ],
        )

    def _pin_claim_ids(self, pin: str) -> List[str]:
        """The dependency claims whose typed_value carries this literal pin."""
        found: List[str] = []
        for record in read_records(self.docker_orchestrator, CLAIM_DIR):
            if str(record.get("kind") or "").strip() != "dependency":
                continue
            typed_value = record.get("typed_value")
            if not isinstance(typed_value, Mapping):
                continue
            package = str(typed_value.get("package") or "").strip()
            specifier = str(typed_value.get("specifier") or "").strip()
            version = str(typed_value.get("version") or "").strip()
            literals = {f"{package}{specifier}{version}"} | {
                str(value).strip() for value in typed_value.values() if isinstance(value, str)
            }
            identifier = str(record.get("claim_id") or "").strip()
            if identifier and pin in literals and identifier not in found:
                found.append(identifier)
        return found

    # --- contract-vs-receipt assessment (spec §C5) --------------------------

    def _assess_receipts(
        self,
        contract: Optional[Mapping[str, Any]],
        requirements: Mapping[str, Any],
        executions: List[Any],
    ) -> None:
        """Assess every receipt this facade call minted, and persist the verdicts.

        Each physical dispatch is assessed on its own — the JDK-driven rerun is
        a second dispatch under the same contract, and reading only the last
        receipt would erase the first one's meaning.

        This never changes the result and never raises: the model is waiting on
        a build, and a verdict that could not be written is a missing evidence
        record, not a failed build. A DISPATCHED receipt with no assessment is
        an evidence-closure hole the phase gate should refuse to close over;
        that gate reads the assessment directory and lands in Stage D.
        """
        assessed = set()
        for execution in executions:
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
                )
            except Exception as exc:  # evidence never breaks the build result
                logger.debug(f"receipt {receipt_id} was not assessed: {exc}")

    @staticmethod
    def _current_fingerprints(
        requirements: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> Dict[str, str]:
        """The pins the harness can state NOW, without a second probe.

        The survey stamp is the current config/document-map pin, and the
        receipt's own target sha is the most recent observation of the tree —
        it was probed AFTER the dispatch, where the contract's was probed
        before. A pin nobody currently states stays absent, so it can never be
        read as a mismatch.
        """
        current: Dict[str, str] = {}
        survey = requirements.get("survey") if isinstance(requirements, Mapping) else None
        if isinstance(survey, Mapping):
            for key in ("config_fingerprint", "document_map_fingerprint", "survey_fingerprint"):
                value = str(survey.get(key) or "").strip()
                if value:
                    current[key] = value
        target_sha = str((receipt or {}).get("target_sha") or "").strip()
        if target_sha:
            current["target_sha"] = target_sha
        return current

    # --- material-progress retry law (spec §C7) -----------------------------

    def _retry_without_delta_refusal(
        self,
        *,
        system: str,
        requested_verb: str,
        effective_verb: str,
        effective_action: str,
        working_directory: str,
        expected_argv: Optional[str],
        requirements: Mapping[str, Any],
        preamble_lines: List[str],
    ) -> Optional[ToolResult]:
        """Refuse a dispatch the ledger has already seen fail, or None to proceed.

        Spec §C7 gives deterministic failures a law: the same action, against
        the same tree, in the same environment, that already failed the same
        typed way may not simply be run again. It needs a material delta — a
        different argv, a different environment fingerprint, an accepted repair
        or a newer fact epoch — and prose, revisions and restated expectations
        are not deltas.

        The recurrence state is the controller's (`retry_authority`); this
        facade reads it and answers. The refusal mints no receipt, so it also
        records a `ControlAssessment`: a reader of the evidence directory must
        still learn that this intent was stopped and why. Persisting that is
        best effort and never gates the refusal.

        Never raises: a ledger this facade cannot read states no recurrence,
        and an unreadable file is not authority to stop a build.
        """
        execute = self.docker_orchestrator.execute_command
        try:
            # A run that has recorded no failure has no recurrence to validate,
            # so the ledger read is the whole cost of the law on a first
            # dispatch — the candidate (and its target-sha probe) is only built
            # when there is something to compare it against.
            ledger = read_ledger(execute)
            if not ledger:
                return None
            candidate = candidate_contract(
                execute,
                tool=self.name,
                effective_action=effective_action,
                expected_cwd=working_directory,
                expected_argv=expected_argv,
                requirements=requirements,
            )
            blocked = blocking_entry(execute, candidate, ledger=ledger)
        except Exception as exc:  # the authority never breaks a first dispatch
            logger.debug(f"retry authority not consulted for {working_directory}: {exc}")
            return None
        if blocked is None:
            return None
        retry_key, entry = blocked
        typed_code = str(entry.get("typed_code") or "").strip()
        count = entry.get("count")
        headline = (
            f"[retry] {system} {effective_verb} at {working_directory} already failed "
            f"as {typed_code} ×{count} with this exact action, tree and environment"
        )
        closing = (
            "a repeat needs material progress: change the argv, change the toolchain "
            "or environment, accept a repair proposal, or record a new project fact — "
            "a rerun on its own cannot fail differently"
        )
        facts: Dict[str, Any] = {
            "retry_key": retry_key,
            "prior_typed_code": typed_code,
            "prior_failure_count": count,
            "requested_action": requested_verb,
            "effective_action": effective_verb,
            "working_directory": working_directory,
            "system": system,
        }
        metadata: Dict[str, Any] = dict(facts)
        metadata["runner_dispatched"] = False
        write_assessment(
            self.docker_orchestrator.execute_command,
            ControlAssessment(
                event_or_intent_id=next_control_event_id("build-retry"),
                stage="precondition",
                typed_code=RETRY_WITHOUT_DELTA_CODE,
                detail=(
                    f"{retry_key} already failed as {typed_code} ×{count}; this dispatch "
                    "states no material delta"
                ),
            ),
        )
        return ToolResult.completed_failure(
            output="\n".join(preamble_lines + [headline, closing]),
            error=f"identical retry after {typed_code}",
            error_code=RETRY_WITHOUT_DELTA,
            facts=facts,
            metadata=metadata,
            suggestions=[
                "Read the recorded failure before rerunning: search(target='output_...') "
                "on the prior attempt's output ref",
                "Change the invocation itself (args, working_directory, action) or the "
                "environment it runs in, then retry",
            ],
        )

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
        # Verbatim: the detail carries the mismatched coordinates, and a
        # paraphrase is how a mismatch becomes an alias.
        detail = str(edge.get("detail") or "").strip()

        if status == "version_incompatible":
            headline = (
                f"[domain-edge] {consumer} consumes an artifact of {producer} "
                "across a version_incompatible edge"
            )
            closing = _SEALED_CLAUSE
            error = f"domain edge blocks {consumer}"
            suggestions = [
                f"Record the mismatch as a project fact, then build {producer} "
                "at the version this consumer requires",
                "Do not retarget the consumer at a different artifact version",
            ]
        else:
            headline = (
                f"[domain-edge] {consumer} consumes an artifact {producer} builds, "
                "on an unverified edge"
            )
            closing = (
                f"{producer} must produce first; this consumer stays locked until "
                "that edge is verified"
            )
            error = f"domain edge is unverified for {consumer}"
            suggestions = [
                f"Build {producer} first, then retry this consumer",
                f"Read {producer}'s declared coordinates to verify or refute the edge",
            ]

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
        if detail:
            facts["domain_edge_detail"] = detail

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
            output="\n".join(line for line in (headline, detail, closing) if line),
            error=error,
            error_code=error_code,
            facts=facts,
            metadata=metadata,
            suggestions=suggestions,
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
                    "On a python deps call this is an install target, so it accepts "
                    "only an exact pin (pkg==literal) a project document states.",
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
                        "either."
                    ),
                },
                "definitions": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "action=native only: build definitions, "
                        f"{NATIVE_DEFINITION_KEY.pattern} = "
                        f"{'|'.join(NATIVE_DEFINITION_VALUES)}. Must agree with "
                        "features, and each key needs a stored project claim."
                    ),
                },
            },
            "required": ["action"],
        }
