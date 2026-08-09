#!/usr/bin/env python3
"""Category-3 anchor evaluator.

Pure functions that compute each probe's anchor predicates from the STRUCTURED
artifacts ONLY — never from summary text:

* the sealed verdict.json (top-level ``verdict``; ``build_evidence.{judgment,
  source,green,compiled_classes}``; ``test_stats.unique.{executed,failed,
  errors}``);
* the control record's ``tool_result`` events. Real runs emit the consolidated
  ``build`` facade (``tool='build'``, ``params['action']`` in deps/compile/
  test/package/install); the python/maven/gradle backend detail — the pytest
  ``command`` string and the structured ``collected``/
  ``collected_after_deselection`` counts — is projected into
  ``result['metadata']`` (react_engine ``_control_result_projection``). The
  anchors read ``params['action']``, ``params['working_directory']``, invocation
  success, and ``result['metadata']`` — the REAL schema, never an invented
  ``selection`` envelope. The authoritative project root comes from the
  ``project`` clone/analyze events, never derived from the test invocations;
* the stamped manifest (``build_requirements.json``: existence, the
  ``survey`` stamp, and ``python_packages``).

Each predicate returns an :class:`AnchorResult` (pass/fail + a named reason). A
MISSING field is an anchor FAIL with a named reason, NEVER a crash. Per-probe
arm verdicts follow the spec's three-outcome logic
(P-pass ∧ F-pass = delete vote; P-pass ∧ F-fail = stage-2 needed; P-fail =
invalid experiment).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from sag.agent.evidence_publications import (
    BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
    MutablePublicationObservation,
)
from sag.tools.internal.build_preflight import validate_build_requirements_v1
from scripts.collect_control_layer_ab import (
    AuthorizedCurrentRun,
    CollectionError,
    load_authorized_current_run,
)

# Pre-registered absolute floors (mirror of logs/panel-category3/panel-lock.json;
# these are the machine-checkable constants the anchors read).
BIGTOP_COMPILED_FLOOR = 96
BIGTOP_EXECUTED_FLOOR = 50
HTTPCOMPONENTS_EXECUTED_FLOOR = 1500
TVM_COLLECTED_MAX = 50


class EvaluationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolInvocation:
    """One control-record ``tool_result`` reduced to the fields the anchors read.

    Real runs emit the consolidated ``build`` facade: ``tool == "build"`` and
    ``params["action"] in {deps, compile, test, package, install}`` (see
    ``src/sag/tools/build/build_tool.py``). The python/maven/gradle backend
    details live in the projected ``result["metadata"]`` map — the pytest
    command string, the ``collected``/``collected_after_deselection`` counts,
    etc. (``src/sag/tools/internal/python_tool.py`` -> react_engine
    ``_control_result_projection`` nests them at ``result["metadata"]``). The
    anchors therefore read the REAL projected schema, never an invented
    ``selection`` envelope.
    """

    tool: str
    action: str
    working_directory: str | None
    success: bool
    params: Mapping[str, Any] = field(default_factory=dict)
    result: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def command(self) -> str:
        """The executed command string, as recorded by the python/pytest tool."""
        return str(self.metadata.get("command") or "")

    def is_pytest(self) -> bool:
        """True iff this build(action='test') ran pytest (the python backend).

        The python backend records a ``pytest`` command and a structured
        ``collected`` count in ``result["metadata"]``; maven/gradle test
        invocations record neither. That structured signal — not the tool
        name (all three surface as ``build``) — is what marks a pytest run.
        """
        if self.tool != "build" or str(self.action).lower() != "test":
            return False
        command = self.command().lower()
        if "pytest" in command:
            return True
        # A recorded structured collection count is the python backend's mark
        # even if the command string is unavailable.
        return isinstance(self.metadata.get("collected"), int) and not isinstance(
            self.metadata.get("collected"), bool
        )

    def collected_after_deselection(self) -> int | None:
        """The structured python-tool result field (never summary text).

        Projected at ``result["metadata"]["collected_after_deselection"]``.
        """
        value = self.metadata.get("collected_after_deselection")
        if isinstance(value, bool):  # guard: bool is an int subclass
            return None
        if isinstance(value, int):
            return value
        return None

    def has_node_or_k_filter(self) -> bool:
        """True iff this pytest invocation carries a node-id path or a -k filter.

        Parses the recorded pytest command. A node id (``::``) or a ``-k``
        expression SELECTS. ``--maxfail`` alone does NOT select (round-2
        review) and ``--deselect`` does NOT select either — it runs everything
        EXCEPT the named tests (round-review P2-2); the spec allows only a
        node-id path or ``-k`` (analyzer-diet.md:445).
        """
        tokens = self.command().split()
        skip_next = False
        for index, token in enumerate(tokens):
            if skip_next:
                skip_next = False
                continue
            # -k SELECTS (both "-k expr" and "-kexpr").
            if token == "-k":
                return True
            if token.startswith("-k") and len(token) > 2:
                return True
            # --deselect NODEID runs everything EXCEPT the named test — it does
            # NOT select (round-review P2-2). Consume its argument so the node
            # id below is not miscounted as a positional selection.
            if token == "--deselect":
                skip_next = True
                continue
            if token.startswith("--deselect="):
                continue
            # A positional node id (path::test) SELECTS. Only positional tokens
            # count — a "::" inside a --deselect argument was already consumed.
            if "::" in token and not token.startswith("-"):
                return True
        return False


@dataclass(frozen=True)
class RunArtifacts:
    """The structured truth of one run, distilled for the anchor predicates."""

    run_id: str
    verdict: str
    build_judgment: str = "unknown"
    build_source: str = "none"
    build_green: bool = False
    compiled_classes: int | None = None
    unique_executed: int = 0
    unique_failed: int = 0
    unique_errors: int = 0
    invocations: Sequence[ToolInvocation] = ()
    manifest_present: bool = False
    manifest_python_packages: list[str] | None = None
    manifest_stamped: bool = False
    # The authoritative clone/analyze project root from the control record —
    # NOT derived from the test invocations themselves (round-review P2-4).
    project_root: str | None = None

    def build_test_invocations(self) -> list[ToolInvocation]:
        """Every build(action='test') invocation from the control record."""
        return [
            i for i in self.invocations if i.tool == "build" and str(i.action).lower() == "test"
        ]

    def pytest_invocations(self) -> list[ToolInvocation]:
        """Every pytest test invocation from the control record.

        Real runs surface pytest through the ``build`` facade (``tool='build'``,
        ``action='test'``); the python backend is distinguished by its recorded
        pytest command / structured collection count in ``result['metadata']``.
        """
        return [i for i in self.invocations if i.is_pytest()]


@dataclass(frozen=True)
class AnchorResult:
    name: str
    passed: bool
    reason: str


def _ok(name: str) -> AnchorResult:
    return AnchorResult(name=name, passed=True, reason="")


def _fail(name: str, reason: str) -> AnchorResult:
    return AnchorResult(name=name, passed=False, reason=reason)


# --------------------------------------------------------------------------
# bigtop
# --------------------------------------------------------------------------
def evaluate_bigtop(art: RunArtifacts) -> list[AnchorResult]:
    results: list[AnchorResult] = []

    if art.verdict == "unknown":
        results.append(_fail("verdict_not_unknown", "verdict is 'unknown'"))
    else:
        results.append(_ok("verdict_not_unknown"))

    # phantom-green guard: a success verdict with zero compiled classes is a lie.
    if art.verdict == "success" and (art.compiled_classes or 0) == 0:
        results.append(_fail("phantom_green_guard", "verdict=='success' with compiled_classes==0"))
    else:
        results.append(_ok("phantom_green_guard"))

    if art.compiled_classes is None:
        results.append(
            _fail("compiled_classes_floor", "build_evidence.compiled_classes is missing")
        )
    elif art.compiled_classes < BIGTOP_COMPILED_FLOOR:
        results.append(
            _fail(
                "compiled_classes_floor",
                f"compiled_classes {art.compiled_classes} < floor {BIGTOP_COMPILED_FLOOR}",
            )
        )
    else:
        results.append(_ok("compiled_classes_floor"))

    if _has_successful_data_generators_build(art.invocations):
        results.append(_ok("data_generators_build_success"))
    else:
        results.append(
            _fail(
                "data_generators_build_success",
                "no successful data-generators build invocation in the control record",
            )
        )

    if art.unique_executed < BIGTOP_EXECUTED_FLOOR:
        results.append(
            _fail(
                "unique_executed_floor",
                f"unique.executed {art.unique_executed} < floor {BIGTOP_EXECUTED_FLOOR}",
            )
        )
    else:
        results.append(_ok("unique_executed_floor"))

    if art.unique_failed == 0:
        results.append(_ok("unique_failed_zero"))
    else:
        results.append(_fail("unique_failed_zero", f"unique.failed == {art.unique_failed}"))

    return results


def _has_successful_data_generators_build(invocations: Sequence[ToolInvocation]) -> bool:
    for i in invocations:
        if i.tool != "build" or not i.success:
            continue
        workdir = str(i.working_directory or "").lower()
        target = " ".join(
            str(i.params.get(k) or "") for k in ("module", "target", "command", "args")
        ).lower()
        if "data-generators" in workdir or "data_generators" in workdir:
            return True
        if "data-generators" in target or "data_generators" in target:
            return True
    return False


# --------------------------------------------------------------------------
# httpcomponents-client
# --------------------------------------------------------------------------
def evaluate_httpcomponents(
    art: RunArtifacts, *, project_root: str | None = None
) -> list[AnchorResult]:
    results: list[AnchorResult] = []

    # EXECUTION-BEARING test invocations ONLY (round-review item 3): an event
    # with an EMPTY recorded command and success==False is a REJECTED ATTEMPT
    # (the build tool declined to run — e.g. an mvn call at /workspace that
    # produced no execution), NOT a test execution. The two failing rerun
    # campaigns' non-root events were all such rejected attempts; scoring them
    # as mis-scoped tests punished arm-independent noise. The anchor now reads
    # only invocations that actually ran a command.
    execution_bearing = [i for i in art.build_test_invocations() if _is_execution_bearing(i)]
    # Root comes from the explicit arg, else the control record's clone/analyze
    # project root — NEVER the test invocations themselves (round-review P2-4:
    # deriving root from the invocations lets a fully mis-scoped run, where
    # every test ran in the same submodule, pass vacuously).
    root = _normalize(project_root if project_root is not None else art.project_root)
    if root is None:
        results.append(
            _fail(
                "test_phase_workdir_is_root",
                "no authoritative project root (clone/analyze event missing)",
            )
        )
    elif not execution_bearing:
        # A run with zero REAL test executions cannot demonstrate root scoping
        # (all events were rejected attempts, or there was no test phase at all).
        results.append(
            _fail(
                "test_phase_workdir_is_root",
                "no execution-bearing test invocation (only rejected attempts or none)",
            )
        )
    else:
        # EVERY execution-bearing invocation must run at the reactor root AND at
        # least one such root execution must exist — a single mis-scoped
        # submodule test is the 16-test failure this anchor forbids.
        mis_scoped = [
            _normalize(i.working_directory)
            for i in execution_bearing
            if _normalize(i.working_directory) != root
        ]
        if mis_scoped:
            results.append(
                _fail(
                    "test_phase_workdir_is_root",
                    f"{len(mis_scoped)} execution-bearing test invocation(s) not at "
                    f"project root {root!r}: {mis_scoped!r}",
                )
            )
        else:
            results.append(_ok("test_phase_workdir_is_root"))

    if art.unique_executed >= HTTPCOMPONENTS_EXECUTED_FLOOR:
        results.append(_ok("unique_executed_floor"))
    else:
        results.append(
            _fail(
                "unique_executed_floor",
                f"unique.executed {art.unique_executed} < floor {HTTPCOMPONENTS_EXECUTED_FLOOR}",
            )
        )

    if art.verdict == "success":
        results.append(_ok("verdict_success"))
    else:
        results.append(_fail("verdict_success", f"verdict is {art.verdict!r}, not 'success'"))

    if art.build_source == "physical":
        results.append(_ok("build_evidence_physical"))
    else:
        results.append(
            _fail("build_evidence_physical", f"build_evidence.source is {art.build_source!r}")
        )

    return results


def _is_execution_bearing(inv: ToolInvocation) -> bool:
    """True iff a build(action='test') invocation actually RAN a test command.

    A REJECTED ATTEMPT — the build tool declined to run (empty recorded command
    AND a non-success outcome) — is not a test execution (round-review item 3).
    The two failing rerun campaigns' non-root events were exactly such: empty
    command, operation_outcome/evidence_status 'unknown' (success==False). A run
    that DID execute records its command string (e.g. the mvn verify at root);
    a success even without a captured command still counts (it ran)."""
    return bool(inv.command().strip()) or inv.success


def _normalize(path: str | None) -> str | None:
    if path is None:
        return None
    text = str(path).strip()
    if text != "/" and text.endswith("/"):
        text = text.rstrip("/")
    return text or None


# --------------------------------------------------------------------------
# tvm
# --------------------------------------------------------------------------
def tvm_smoke_liveness(art: RunArtifacts) -> int:
    """REPORTED METRIC (not a per-run anchor): the count of REAL smoke reps.

    Reviewer split: smoke liveness — whether the agent actually landed a
    targeted smoke instead of doing nothing — is a fleet-level HEALTH signal
    aggregated across reps, NOT a per-run must-pass gate (a per-run smoke gate
    re-punished arm-independent 5/6 compliance noise). One run contributes at
    most 1: a "real smoke" is an EXECUTION-BEARING pytest invocation (a
    recorded pytest command) that carries a node-id/-k selection filter. Zero
    pytest invocations => 0 (nothing swept, but also nothing smoked)."""
    return int(
        any(i.command().strip() and i.has_node_or_k_filter() for i in art.pytest_invocations())
    )


def evaluate_tvm(art: RunArtifacts) -> list[AnchorResult]:
    results: list[AnchorResult] = []

    failed_physical = art.build_judgment == "failed" and art.build_source == "physical"
    # Review: never a generic partial. The honest native-core middle state is
    # EXACTLY partial/physical with a non-green build (the native library
    # absent while pure-python evidence exists); anything green is strictly
    # better outright.
    honest_native_partial = (
        art.build_judgment == "partial" and art.build_source == "physical" and not art.build_green
    )
    strictly_better = art.verdict in {"partial", "success"} and art.build_green
    if failed_physical or honest_native_partial or strictly_better:
        results.append(_ok("build_failed_physical_or_better"))
    else:
        results.append(
            _fail(
                "build_failed_physical_or_better",
                f"build_evidence.judgment={art.build_judgment!r} source={art.build_source!r} "
                f"verdict={art.verdict!r} green={art.build_green}: neither "
                "failed+physical nor strictly-better",
            )
        )

    # HARD per-run SAFETY anchor "never sweep the full suite while the native
    # core is unbuilt" (reviewer split of the old tvm anchors). While unbuilt,
    # EVERY execution-bearing pytest invocation must carry a node-id/-k
    # selection AND collect <= TVM_COLLECTED_MAX after deselection — that is the
    # sweep this anchor forbids (356 identical collection errors). CRITICAL:
    # ZERO pytest invocations PASSES — nothing was swept (the old
    # "no pytest invocation to check" FAIL conflated an idle-but-safe run with a
    # sweeping one, and re-punished arm-independent smoke-compliance noise; that
    # liveness signal is now the reported metric tvm_smoke_liveness, not a gate).
    #
    # The predicate holds ONLY while the native core is unbuilt
    # (analyzer-diet.md:445): a strictly-better run (build green) is allowed a
    # legitimate full-suite pytest run and passes by construction (round-review
    # P2-1); the build anchor above already scored that branch.
    if strictly_better:
        results.append(_ok("never_sweep_while_unbuilt"))
        if art.verdict == "unknown":
            results.append(_fail("verdict_not_unknown", "verdict is 'unknown'"))
        else:
            results.append(_ok("verdict_not_unknown"))
        return results

    execution_bearing = [i for i in art.pytest_invocations() if i.command().strip()]
    violations: list[str] = []
    for i in execution_bearing:
        if not i.has_node_or_k_filter():
            violations.append(
                "unfiltered pytest (node-id/-k absent; --maxfail alone does not select)"
            )
            continue
        collected = i.collected_after_deselection()
        if collected is None:
            violations.append(
                "filtered pytest missing the structured collected_after_deselection field"
            )
        elif collected > TVM_COLLECTED_MAX:
            violations.append(f"collected_after_deselection {collected} > max {TVM_COLLECTED_MAX}")
    if violations:
        results.append(
            _fail(
                "never_sweep_while_unbuilt",
                f"{len(violations)} sweeping pytest invocation(s): " + "; ".join(violations),
            )
        )
    else:
        # No execution-bearing pytest invocation swept — this includes the
        # zero-invocation case (nothing swept) which PASSES the safety anchor.
        results.append(_ok("never_sweep_while_unbuilt"))

    if art.verdict == "unknown":
        results.append(_fail("verdict_not_unknown", "verdict is 'unknown'"))
    else:
        results.append(_ok("verdict_not_unknown"))

    return results


# --------------------------------------------------------------------------
# pyyaml
# --------------------------------------------------------------------------
def evaluate_pyyaml(art: RunArtifacts, *, executed_floor: int | None) -> list[AnchorResult]:
    results: list[AnchorResult] = []

    # STAMPED manifest (analyzer-diet.md:446): existence alone is not enough —
    # the survey stamp (analyzer_version/project_path/config_fingerprint,
    # project_analyzer.py:1019) must be present, or a stampless/stale manifest
    # would pass (round-review P2-3).
    if not art.manifest_present:
        results.append(
            _fail(
                "stamped_manifest_exists", "stamped manifest (build_requirements.json) is missing"
            )
        )
    elif not art.manifest_stamped:
        results.append(
            _fail(
                "stamped_manifest_exists",
                "manifest present but carries no survey stamp "
                "(survey.analyzer_version/project_path/config_fingerprint)",
            )
        )
    else:
        results.append(_ok("stamped_manifest_exists"))

    packages = art.manifest_python_packages
    if packages is None:
        results.append(_fail("manifest_python_packages", "manifest python_packages is missing"))
    elif "yaml" in list(packages):
        # Calibration evidence (pyyaml-cal-r1): the C-extension package _yaml
        # (lib/_yaml/__init__.py) is REAL and discovered beside yaml — exact
        # equality mis-failed a correct discovery. The anchor's meaning is
        # "yaml discovered from the package_dir layout".
        results.append(_ok("manifest_python_packages"))
    else:
        results.append(
            _fail(
                "manifest_python_packages",
                f"python_packages == {list(packages)!r} does not contain 'yaml'",
            )
        )

    if art.verdict in ("success", "partial") and art.unique_failed == 0:
        # Calibration evidence (pyyaml-cal-r1): 1281/1281 tests passed with a
        # stamped manifest, and the HONEST ladder still verdicts partial —
        # the C extension is unbuilt, so build evidence caps below success.
        # 'success'-only was unachievable-by-construction (the bigtop round-3
        # anchor lesson: encode what the baseline actually achieves). The
        # no-regression essence is green tests at scale over honest evidence;
        # the phantom-green guard is the executed floor + failed==0 beside it.
        results.append(_ok("verdict_green"))
    else:
        results.append(
            _fail(
                "verdict_green",
                f"verdict {art.verdict!r} with unique.failed={art.unique_failed} — "
                "needs success-or-partial with zero failures",
            )
        )

    if executed_floor is None:
        results.append(
            _fail(
                "unique_executed_floor",
                "no calibrated executed floor (calibration never ran or was not supplied)",
            )
        )
    elif art.unique_executed >= executed_floor:
        results.append(_ok("unique_executed_floor"))
    else:
        results.append(
            _fail(
                "unique_executed_floor",
                f"unique.executed {art.unique_executed} < calibrated floor {executed_floor}",
            )
        )

    if art.unique_failed == 0:
        results.append(_ok("unique_failed_zero"))
    else:
        results.append(_fail("unique_failed_zero", f"unique.failed == {art.unique_failed}"))

    return results


# --------------------------------------------------------------------------
# dispatch + three-outcome verdict
# --------------------------------------------------------------------------
# The spec's probe set is EXACTLY these four (analyzer-diet.md:442-447). The
# earlier cloudstack/dubbo "baseline-only" evaluator was an invented detour
# (round-review P1-3) and is removed; the real panel precondition is the suite
# baseline-reds gate in the runner.
_EVALUATORS = {
    "bigtop": evaluate_bigtop,
    "httpcomponents-client": evaluate_httpcomponents,
    "tvm": evaluate_tvm,
    "pyyaml": evaluate_pyyaml,
}


def evaluate_probe(probe: str, art: RunArtifacts, **kwargs: Any) -> list[AnchorResult]:
    try:
        evaluator = _EVALUATORS[probe]
    except KeyError as exc:
        raise KeyError(f"no Category-3 evaluator for probe {probe!r}") from exc
    return evaluator(art, **kwargs)


def all_anchors_pass(results: Sequence[AnchorResult]) -> bool:
    return bool(results) and all(r.passed for r in results)


def probe_arm_verdict(*, p_pass: bool, f_pass: bool) -> str:
    """The spec's three-outcome per-probe verdict.

    * P pass ∧ F pass -> ``delete`` (the probe votes to delete the prescriptions);
    * P pass ∧ F fail -> ``stage-2`` (attributable regression -> ablation);
    * P fail (either F) -> ``invalid`` (shared failure / invalid experiment).
    """
    if not p_pass:
        return "invalid"
    return "delete" if f_pass else "stage-2"


# --------------------------------------------------------------------------
# loader: build RunArtifacts from a sealed session directory
# --------------------------------------------------------------------------
def _load_strict_json_object(path: Path) -> tuple[dict[str, Any], bytes]:
    """Read one duplicate-free JSON object while preserving its exact bytes."""

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EvaluationError(f"cannot read structured artifact {path}: {exc}") from exc
    if not raw or len(raw) > 16 * 1024 * 1024:
        raise EvaluationError(f"structured artifact {path} has an invalid byte length")

    def strict_object(pairs):
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    try:
        payload = json.loads(raw, object_pairs_hook=strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EvaluationError(f"cannot read strict structured artifact {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvaluationError(f"structured artifact {path} is not one JSON object")
    return payload, raw


def load_authorized_build_requirements(
    session: str | Path,
    current: AuthorizedCurrentRun,
) -> dict[str, Any] | None:
    """Return the exact current manifest, or a host-verified absence.

    The archived container file is only a mirror.  Its bytes become usable
    when the recovered host publication authority says that they are the one
    complete current ``build_requirements`` mutable ledger.  A missing mirror
    is accepted only when the same authority verifies an empty current set
    (genesis or a tombstone).
    """

    manifest_path = Path(session) / ".setup_agent" / "build_requirements.json"
    if not manifest_path.is_file():
        check = current.authority.verify_latest_record_set("build_requirements", {})
        if not check.authorized:
            raise EvaluationError(
                "build_requirements mirror is missing while the host authority has a current head"
            )
        return None

    manifest, raw = _load_strict_json_object(manifest_path)
    head = current.authority.latest_head(BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID)
    if (
        head is None
        or head.publication_state != "present"
        or head.record_id != BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID
        or head.logical_artifact_id != BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID
        or head.record_kind not in {"build_requirements", "receipt_structure"}
    ):
        raise EvaluationError(
            "build_requirements mirror has no matching current host publication head"
        )
    observation = MutablePublicationObservation(
        logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        byte_count=len(raw),
        run_id=current.pin.run_id,
        contract_id=head.contract_id,
        contract_hash=head.contract_hash,
    )
    check = current.authority.verify_latest_record_set(
        "build_requirements",
        {BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID: observation},
    )
    if not check.authorized:
        raise EvaluationError("build_requirements mirror is not the exact current host publication")
    try:
        return validate_build_requirements_v1(manifest)
    except (TypeError, ValueError) as exc:
        raise EvaluationError("build_requirements mirror is not a valid v1 manifest") from exc


def _invocation_success(result: Mapping[str, Any]) -> bool:
    outcome = str(result.get("operation_outcome") or "").lower()
    if outcome:
        return outcome == "success"
    return str(result.get("evidence_status") or "").lower() in {"verified", "green"}


def load_run_artifacts(session_path: str | Path) -> RunArtifacts:
    """Distill one archived session into :class:`RunArtifacts`.

    Reads only host-authorized archived state.  The host run pin and host
    control stream recover the run authority; the container verdict and
    manifest are accepted only as exact current mirrors.  Rendered/summary
    artifacts and session-root/container fallback files are never consulted.
    """
    session = Path(session_path)
    try:
        current = load_authorized_current_run(session)
    except CollectionError as exc:
        raise EvaluationError(f"archived run authority is invalid: {exc}") from exc
    verdict = current.snapshot.model_dump(mode="json")
    build_evidence = verdict["build_evidence"]
    unique = verdict["test_stats"]["unique"]

    invocations: list[ToolInvocation] = []
    project_root: str | None = None
    for event in current.events:
        if event.kind != "tool_result":
            continue
        payload = event.payload
        params = payload.get("params") or {}
        result = payload.get("result") or {}
        metadata = result.get("metadata")
        if not isinstance(metadata, Mapping):
            metadata = {}
        tool = str(payload.get("tool") or "")
        action = str(params.get("action") or "")
        # Authoritative project root from the clone/analyze events (the
        # control record's structured truth — not derived from later test
        # invocations, round-review P2-4).
        if project_root is None and tool == "project":
            if action == "clone" and metadata.get("clone_path"):
                project_root = str(metadata["clone_path"])
            elif action == "analyze":
                candidate = params.get("project_path") or metadata.get("project_path")
                if candidate:
                    project_root = str(candidate)
        invocations.append(
            ToolInvocation(
                tool=tool,
                action=action,
                working_directory=params.get("working_directory"),
                success=_invocation_success(result),
                params=params,
                result=result,
                metadata=metadata,
            )
        )

    manifest = load_authorized_build_requirements(session, current)
    manifest_present = manifest is not None
    manifest_packages: list[str] | None = None
    manifest_stamped = False
    if manifest is not None:
        raw = manifest.get("python_packages")
        if isinstance(raw, list):
            manifest_packages = [str(item) for item in raw]
        # The survey stamp completes the staleness contract
        # (project_analyzer.py:1019): a manifest is STAMPED only when it
        # carries survey.{analyzer_version, project_path, config_fingerprint}.
        survey = manifest.get("survey")
        if isinstance(survey, Mapping):
            manifest_stamped = (
                survey.get("analyzer_version") is not None
                and bool(survey.get("project_path"))
                and "config_fingerprint" in survey
            )

    return RunArtifacts(
        run_id=current.pin.run_id,
        verdict=str(verdict.get("verdict") or "unknown"),
        build_judgment=str(build_evidence.get("judgment") or "unknown"),
        build_source=str(build_evidence.get("source") or "none"),
        build_green=bool(build_evidence.get("green")),
        compiled_classes=build_evidence.get("compiled_classes"),
        unique_executed=int(unique.get("executed") or 0),
        unique_failed=int(unique.get("failed") or 0),
        unique_errors=int(unique.get("errors") or 0),
        invocations=tuple(invocations),
        manifest_present=manifest_present,
        manifest_python_packages=manifest_packages,
        manifest_stamped=manifest_stamped,
        project_root=project_root,
    )
