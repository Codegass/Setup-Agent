"""Acceptance kernel for pure filesystem-first JVM build reconciliation.

The fixtures in this file are intentionally self-contained.  They build only
typed Task 1 evidence and never import helpers from another test module, so the
shadow kernel cannot accidentally depend on a live validator or filesystem
reader.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import pytest
from pydantic import ValidationError

from sag.agent.jvm_build_models import (
    ActualInvocationScopeV1,
    BuildEvidenceSetSnapshotV1,
    BuildGoalScopeV1,
    BuildPathBasis,
    BuildReconciliationV1,
    ChildScopeExpectation,
    ClassificationBasisV1,
    CompileDependencyEdge,
    CompileOutputDigestEntry,
    CompilerEnvironmentSpec,
    CompileUnitExpectation,
    CompileUnitObservationV1,
    ContainerEvidenceEpochV1,
    CurrentOutputEntryV1,
    CurrentOutputRootV1,
    CurrentPhysicalBasisSnapshotV1,
    CurrentPhysicalMemberV1,
    DelegationEdge,
    EvaluatedBuildModelV1,
    EvaluatedModuleV1,
    EvidenceRecordDigestV1,
    EvidenceRecordSetCompletenessV1,
    FinalBuildBasisV1,
    FinalCompileUnitBasis,
    FinalGeneratorUnitBasis,
    FinalPackageUnitBasis,
    GeneratedRootContract,
    GeneratorEnvironmentSpec,
    GeneratorUnitExpectation,
    GeneratorUnitObservationV1,
    JvmBuildExpectationV1,
    JvmSourceCandidate,
    JvmSourceCensusV1,
    MissingPhysicalMemberV1,
    OutputRootSpec,
    OutputRootWitness,
    PackageUnitExpectation,
    PackageUnitObservationV1,
    PackagingEnvironmentSpec,
    PhysicalOutputEntry,
    PhysicalOutputWitnessV1,
    RequestedArtifactSpec,
    RequiredSourceEdge,
    ResolvedDigestEntry,
    ResolvedMemberSpec,
    SelectedChildReconciliation,
    SelectedCompileProof,
    SelectedGeneratorProof,
    SelectedPackageProof,
    SourceClassification,
    SourceDigestEntry,
    SourcePatternRuleV1,
    UnreadablePhysicalMemberV1,
    canonical_build_evidence_sha256,
    canonical_content_id,
    compile_unit_identity_hash,
    generator_unit_identity_hash,
    package_unit_identity_hash,
    physical_output_aggregate_sha256,
)
from sag.agent.jvm_build_reconciliation import (
    ReconciliationInputs,
    ReconciliationSelections,
    derive_expectation,
    derive_final_basis,
    reconcile_build,
    reconciliation_input_set_digest,
)

PROJECT_ROOT = "/workspace/project"
RECORD_KINDS = (
    "generator_observation",
    "compile_observation",
    "package_observation",
    "output_witness",
    "child_census",
    "child_expectation",
    "child_reconciliation",
)
POSITIVE_OUTCOMES = (
    "executed_success",
    "current_noop",
    "up_to_date",
    "from_cache",
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _build_config_fingerprint(entries: Sequence[ResolvedDigestEntry]) -> str:
    return canonical_build_evidence_sha256(
        {
            "schema_version": 1,
            "fingerprint_kind": "jvm-build-config-members",
            "payload": tuple(entries),
        }
    )


def _sorted_by(items: Iterable[Any], key: Any) -> tuple[Any, ...]:
    return tuple(sorted(items, key=key))


def _source(path: str, *, digest: str | None = None, language: str = "java") -> JvmSourceCandidate:
    digest = digest or _sha(f"source:{path}")
    return JvmSourceCandidate(
        source_id=canonical_content_id("jvm-source", {"path": path, "sha256": digest}),
        path=path,
        sha256=digest,
        language=language,
        origin="generated" if path.startswith("target/generated") else "repository",
        role_hint="production",
        discovered_root=path.rsplit("/", 1)[0],
        symlink_status="contained",
    )


def _path_basis(
    path: str,
    *,
    member_id: str,
    member_role: str,
    path_kind: str = "file",
) -> BuildPathBasis:
    return BuildPathBasis(
        member_id=member_id,
        member_role=member_role,
        path=path,
        path_kind=path_kind,
        resolution_provenance="fixture:mechanical-model",
    )


def _source_rule(compile_unit_id: str, pattern: str) -> SourcePatternRuleV1:
    projection = {
        "compile_unit_id": compile_unit_id,
        "dialect": "posix_glob_v1",
        "pattern": pattern,
        "model_provenance": "fixture:effective-model",
    }
    return SourcePatternRuleV1(
        rule_id=canonical_content_id("source-pattern-rule-v1", projection),
        **projection,
    )


def _configured_source_root(path: str) -> str:
    for marker in ("src/main/java", "src/main/kotlin"):
        token = f"{marker}/"
        index = path.find(token)
        if index >= 0:
            return f"{path[:index]}{marker}"
    return path.rsplit("/", 1)[0]


def _invocation(run_id: str) -> ActualInvocationScopeV1:
    return ActualInvocationScopeV1(
        build_system="maven",
        selected_modules_or_tasks=("com.example:app",),
        lifecycle_or_tasks=("package",),
        profiles_or_variants=("default",),
        resume_from=None,
        also_make=True,
        selector_fingerprint=_sha(f"selector:{run_id}"),
    )


def _compiler_environment() -> CompilerEnvironmentSpec:
    return CompilerEnvironmentSpec(
        executable_path_basis=_path_basis(
            "/opt/jdk/bin/javac",
            member_id="compiler-javac",
            member_role="compiler",
        ),
        compiler_version="21.0.1",
        compiler_args=("-parameters",),
        classpath_members=(),
        toolchain_path_basis=_path_basis(
            "/opt/jdk",
            member_id="toolchain-jdk",
            member_role="toolchain",
            path_kind="tree",
        ),
        resolution_provenance="fixture:effective-model",
    )


@dataclass(frozen=True)
class ObservationSpec:
    unit_id: str
    source_paths: tuple[str, ...]
    run_id: str = "run-01"
    outcome: str = "executed_success"
    witness: bool = True
    witness_state: str = "current"
    class_entries: tuple[str, ...] | None = None


@dataclass(frozen=True)
class World:
    inputs: ReconciliationInputs
    epoch: ContainerEvidenceEpochV1
    scope: BuildGoalScopeV1
    census: JvmSourceCensusV1
    model: EvaluatedBuildModelV1
    expectation: JvmBuildExpectationV1
    physical: CurrentPhysicalBasisSnapshotV1
    final_basis: FinalBuildBasisV1
    evidence_set: BuildEvidenceSetSnapshotV1


def _record_row(
    record_kind: str,
    objects: Sequence[Any],
    *,
    status: str = "complete",
    raw_hash_overrides: Mapping[str, str] | None = None,
) -> EvidenceRecordSetCompletenessV1:
    ids = tuple(_record_id(record_kind, item) for item in objects)
    raw_hash_overrides = raw_hash_overrides or {}
    hashes = tuple(
        EvidenceRecordDigestV1(
            record_id=record_id,
            raw_sha256=raw_hash_overrides.get(record_id, _sha(f"host-raw:{record_id}")),
        )
        for record_id in ids
    )
    expected_ids = ids
    tombstones: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    if status == "set_mismatch":
        expected_ids = tuple(sorted((*ids, f"missing-{record_kind}")))
        conflicts = (f"{record_kind} set mismatch",)
    elif status == "tombstoned":
        tombstone_id = f"tombstoned-{record_kind}"
        expected_ids = tuple(sorted((*ids, tombstone_id)))
        tombstones = (tombstone_id,)
        conflicts = (f"{record_kind} tombstoned",)
    elif status in {"unavailable", "conflict"}:
        conflicts = (f"{record_kind} {status}",)
    return EvidenceRecordSetCompletenessV1(
        record_kind=record_kind,
        expected_record_ids=expected_ids,
        observed_record_ids=ids,
        observed_record_hashes=hashes,
        status=status,
        tombstoned_record_ids=tombstones,
        conflicts=conflicts,
    )


def _record_id(record_kind: str, item: Any) -> str:
    field = {
        "generator_observation": "observation_id",
        "compile_observation": "observation_id",
        "package_observation": "observation_id",
        "output_witness": "witness_id",
        "child_census": "census_id",
        "child_expectation": "expectation_id",
        "child_reconciliation": "reconciliation_id",
    }[record_kind]
    return getattr(item, field)


def _evidence_set(
    *,
    epoch_id: str,
    generator_observations: Sequence[GeneratorUnitObservationV1] = (),
    compile_observations: Sequence[CompileUnitObservationV1] = (),
    package_observations: Sequence[PackageUnitObservationV1] = (),
    witnesses: Sequence[PhysicalOutputWitnessV1] = (),
    child_censuses: Sequence[JvmSourceCensusV1] = (),
    child_expectations: Sequence[JvmBuildExpectationV1] = (),
    child_reconciliations: Sequence[BuildReconciliationV1] = (),
    record_statuses: Mapping[str, str] | None = None,
    raw_hash_overrides: Mapping[str, str] | None = None,
) -> BuildEvidenceSetSnapshotV1:
    typed: dict[str, tuple[Any, ...]] = {
        "generator_observation": _sorted_by(
            generator_observations, lambda item: item.observation_id
        ),
        "compile_observation": _sorted_by(compile_observations, lambda item: item.observation_id),
        "package_observation": _sorted_by(package_observations, lambda item: item.observation_id),
        "output_witness": _sorted_by(witnesses, lambda item: item.witness_id),
        "child_census": _sorted_by(child_censuses, lambda item: item.census_id),
        "child_expectation": _sorted_by(child_expectations, lambda item: item.expectation_id),
        "child_reconciliation": _sorted_by(
            child_reconciliations, lambda item: item.reconciliation_id
        ),
    }
    record_statuses = record_statuses or {}
    rows = tuple(
        _record_row(
            kind,
            typed[kind],
            status=record_statuses.get(kind, "complete"),
            raw_hash_overrides=raw_hash_overrides,
        )
        for kind in RECORD_KINDS
    )
    admitted_run_ids = tuple(
        sorted(
            {
                observation.run_id
                for observations in (
                    typed["generator_observation"],
                    typed["compile_observation"],
                    typed["package_observation"],
                )
                for observation in observations
            }
        )
    )
    identity = {
        "evidence_epoch_id": epoch_id,
        "admitted_run_ids": admitted_run_ids,
        "generator_observations": typed["generator_observation"],
        "compile_observations": typed["compile_observation"],
        "package_observations": typed["package_observation"],
        "output_witnesses": typed["output_witness"],
        "child_censuses": typed["child_census"],
        "child_expectations": typed["child_expectation"],
        "child_reconciliations": typed["child_reconciliation"],
        "record_sets": rows,
    }
    return BuildEvidenceSetSnapshotV1(
        snapshot_id=canonical_content_id("build-evidence-set-v1", identity),
        **identity,
        conflicts=(),
    )


def _make_world(
    *,
    epoch_tag: str = "primary",
    source_paths: Sequence[str] = ("src/main/java/example/App.java",),
    assignments: Mapping[str, Sequence[str]] | None = None,
    observations: Sequence[ObservationSpec] | None = None,
    output_requirements: Mapping[str, str] | None = None,
    output_paths: Mapping[str, str] | None = None,
    verification_modes: Mapping[str, str] | None = None,
    unclassified_paths: Sequence[str] = (),
    unsupported_language: bool = False,
    record_statuses: Mapping[str, str] | None = None,
    raw_hash_overrides: Mapping[str, str] | None = None,
) -> World:
    container_id = f"container-{epoch_tag}"
    epoch_id = canonical_content_id(
        "container-evidence-epoch-v1",
        {"immutable_container_id": container_id, "project_root": PROJECT_ROOT},
    )
    scope_id = canonical_content_id(
        "build-goal-scope-v1",
        {"evidence_epoch_id": epoch_id, "authority_ref": "request-01"},
    )
    census_id = canonical_content_id(
        "jvm-source-census-v1",
        {"evidence_epoch_id": epoch_id, "project_root": PROJECT_ROOT},
    )
    model_id = canonical_content_id(
        "evaluated-build-model-v1",
        {
            "evidence_epoch_id": epoch_id,
            "scope_id": scope_id,
            "project_root": PROJECT_ROOT,
        },
    )
    epoch = ContainerEvidenceEpochV1(
        evidence_epoch_id=epoch_id,
        immutable_container_id=container_id,
        project_root=PROJECT_ROOT,
        created_at="2026-08-11T12:00:00Z",
        initial_source_state_fingerprint=_sha(f"initial:{epoch_tag}"),
        baseline_output_root_digests=(),
    )
    scope = BuildGoalScopeV1(
        scope_id=scope_id,
        evidence_epoch_id=epoch_id,
        authority_ref="request-01",
        scope_revision=1,
        predecessor_scope_hash=None,
        validation_target="project",
        requested_action="build",
        requested_lifecycle="package",
        requested_modules_or_domains=(),
        requested_profiles_or_variants=("default",),
        source_set_roles=("production",),
        scope_fingerprint=_sha(f"scope:{epoch_tag}"),
    )
    sources = [_source(path) for path in source_paths]
    if unsupported_language:
        sources.append(_source("src/main/kotlin/example/K.kt", language="kotlin"))
    sources = list(_sorted_by(sources, lambda item: item.source_id))
    source_by_path = {source.path: source for source in sources}
    source_state_fingerprint = _sha(
        "source-state:" + ":".join(f"{item.path}:{item.sha256}" for item in sources)
    )
    census = JvmSourceCensusV1(
        census_id=census_id,
        evidence_epoch_id=epoch_id,
        project_root=PROJECT_ROOT,
        census_revision=1,
        source_state_fingerprint=source_state_fingerprint,
        sources=tuple(sources),
        unreadable_roots=(),
        conflicts=(),
    )
    if assignments is None:
        assignments = {"compile-main": tuple(source_paths)} if source_paths else {}
    output_requirements = output_requirements or {}
    output_paths = output_paths or {}
    verification_modes = verification_modes or {}
    compile_units = tuple(
        sorted(
            (
                CompileUnitExpectation(
                    compile_unit_id=unit_id,
                    domain_id="domain-app",
                    build_system="maven",
                    module_coordinate="com.example:app",
                    source_set="main",
                    language="java",
                    adapter_status="supported",
                    task_or_execution=f"compile:{unit_id}",
                    role="required",
                    scope_disposition="active",
                    source_roots=(
                        tuple(sorted({_configured_source_root(path) for path in paths}))
                        if paths
                        else ("src/main/java",)
                    ),
                    include_rules=(_source_rule(unit_id, "**/*.java"),),
                    exclude_rules=(),
                    compiler_environment_spec=_compiler_environment(),
                    output_roots=(
                        (
                            OutputRootSpec(
                                member_id=f"root-{unit_id}",
                                root_id=f"root-{unit_id}",
                                path=output_paths.get(unit_id, f"target/{unit_id}"),
                                verification_mode=verification_modes.get(
                                    unit_id, "shared_owned_entries"
                                ),
                                output_requirement=output_requirements.get(unit_id, "nonempty"),
                            ),
                        )
                        if output_requirements.get(unit_id, "nonempty") != "none"
                        else ()
                    ),
                    output_requirement=output_requirements.get(unit_id, "nonempty"),
                    output_requirement_basis="fixture:supported-java-compile",
                    requested_lifecycle_basis="package",
                    model_provenance="fixture:effective-model",
                )
                for unit_id, paths in assignments.items()
            ),
            key=lambda item: item.compile_unit_id,
        )
    )
    if unsupported_language:
        compile_units = _sorted_by(
            (
                *compile_units,
                CompileUnitExpectation(
                    compile_unit_id="compile-kotlin",
                    domain_id="domain-app",
                    build_system="maven",
                    module_coordinate="com.example:app",
                    source_set="main",
                    language="kotlin",
                    adapter_status="unsupported_active",
                    task_or_execution="compile:kotlin",
                    role="required",
                    scope_disposition="active",
                    source_roots=("src/main/kotlin",),
                    include_rules=(_source_rule("compile-kotlin", "**/*.kt"),),
                    exclude_rules=(),
                    compiler_environment_spec=_compiler_environment(),
                    output_roots=(
                        OutputRootSpec(
                            member_id="root-compile-kotlin",
                            root_id="root-compile-kotlin",
                            path="target/kotlin",
                            verification_mode="shared_owned_entries",
                            output_requirement="nonempty",
                        ),
                    ),
                    output_requirement="nonempty",
                    output_requirement_basis="fixture:unsupported-language",
                    requested_lifecycle_basis="package",
                    model_provenance="fixture:effective-model",
                ),
            ),
            lambda item: item.compile_unit_id,
        )
    modules = (
        (
            EvaluatedModuleV1(
                module_id="module-app",
                domain_id="domain-app",
                build_system="maven",
                module_coordinate="com.example:app",
                project_path="app",
                role="required",
                model_provenance="fixture:effective-model",
            ),
        )
        if compile_units
        else ()
    )
    model = EvaluatedBuildModelV1(
        model_id=model_id,
        evidence_epoch_id=epoch_id,
        scope_id=scope_id,
        goal_scope_revision=scope.scope_revision,
        goal_scope_fingerprint=scope.scope_fingerprint,
        project_root=PROJECT_ROOT,
        model_revision=1,
        build_config_fingerprint=_build_config_fingerprint(()),
        active_profiles_or_variants=("default",),
        build_config_members=(),
        modules=modules,
        generator_units=(),
        compile_units=compile_units,
        package_units=(),
        compile_dependency_edges=(),
        generated_root_contracts=(),
        child_build_scopes=(),
        conflicts=(),
    )
    classifications: list[SourceClassification] = []
    required_edges: list[RequiredSourceEdge] = []
    unclassified_ids: list[str] = []
    unclassified_paths = set(unclassified_paths)
    compile_units_by_id = {unit.compile_unit_id: unit for unit in compile_units}
    for source in sources:
        if source.path in unclassified_paths:
            unclassified_ids.append(source.source_id)
            continue
        owner_units = tuple(
            sorted(unit_id for unit_id, paths in assignments.items() if source.path in paths)
        )
        if source.language != "java" and unsupported_language:
            classifications.append(
                SourceClassification(
                    source_id=source.source_id,
                    language=source.language,
                    source_kind="main",
                    classification="unsupported_active",
                    classification_basis=ClassificationBasisV1(
                        basis_kind="unsupported_adapter",
                        model_id=model_id,
                        basis_ref=compile_units_by_id["compile-kotlin"].include_rules[0].rule_id,
                    ),
                    expected_compile_unit_ids=("compile-kotlin",),
                    child_scope_id=None,
                )
            )
        elif owner_units:
            classifications.append(
                SourceClassification(
                    source_id=source.source_id,
                    language="java",
                    source_kind="main",
                    classification="required",
                    classification_basis=ClassificationBasisV1(
                        basis_kind="model_rule",
                        model_id=model_id,
                        basis_ref=compile_units_by_id[owner_units[0]].include_rules[0].rule_id,
                    ),
                    expected_compile_unit_ids=owner_units,
                    child_scope_id=None,
                )
            )
            required_edges.extend(
                RequiredSourceEdge(source_id=source.source_id, compile_unit_id=unit_id)
                for unit_id in owner_units
            )
        else:
            unclassified_ids.append(source.source_id)
    expectation_identity = {
        "evidence_epoch_id": epoch_id,
        "census_id": census_id,
        "scope_id": scope_id,
        "model_id": model_id,
        "scope_fingerprint": scope.scope_fingerprint,
        "source_state_fingerprint": source_state_fingerprint,
        "build_config_fingerprint": model.build_config_fingerprint,
        "required_java_edges": _sorted_by(
            required_edges, lambda item: f"{item.source_id}|{item.compile_unit_id}"
        ),
        "source_classifications": _sorted_by(classifications, lambda item: item.source_id),
        "unclassified_source_ids": tuple(sorted(unclassified_ids)),
        "required_generator_unit_ids": (),
        "required_compile_unit_ids": tuple(
            unit.compile_unit_id
            for unit in compile_units
            if unit.role == "required" and unit.adapter_status == "supported"
        ),
        "required_package_unit_ids": (),
        "unsupported_active_unit_ids": ("compile-kotlin",) if unsupported_language else (),
        "required_child_scope_ids": (),
        "delegation_edges": (),
    }
    expectation = JvmBuildExpectationV1(
        expectation_id=canonical_content_id("jvm-build-expectation-v1", expectation_identity),
        **expectation_identity,
        conflicts=(),
    )
    final_compile_bases: list[FinalCompileUnitBasis] = []
    for unit in compile_units:
        if unit.adapter_status != "supported":
            continue
        unit_sources = _sorted_by(
            (
                SourceDigestEntry(
                    source_id=source_by_path[path].source_id,
                    path=path,
                    sha256=source_by_path[path].sha256,
                )
                for path in assignments.get(unit.compile_unit_id, ())
            ),
            lambda item: item.source_id,
        )
        fields: dict[str, Any] = {
            "compile_unit_id": unit.compile_unit_id,
            "build_config_fingerprint": model.build_config_fingerprint,
            "required_source_entries": unit_sources,
            "source_set_fingerprint": _sha(
                "source-set:"
                + unit.compile_unit_id
                + ":"
                + ":".join(item.sha256 for item in unit_sources)
            ),
            "compiler_executable_path": "/opt/jdk/bin/javac",
            "compiler_executable_sha256": _sha("javac"),
            "compiler_version": "21.0.1",
            "compiler_args_fingerprint": _sha("-parameters"),
            "classpath_entries": (),
            "dependency_unit_identity_hashes": (),
            "toolchain_fingerprint": _sha("jdk-21"),
            "output_roots": unit.output_roots,
        }
        final_compile_bases.append(
            FinalCompileUnitBasis(
                **fields,
                compile_unit_identity_hash=compile_unit_identity_hash(fields),
            )
        )
    final_compile_bases = list(_sorted_by(final_compile_bases, lambda item: item.compile_unit_id))
    physical_member_list = [
        CurrentPhysicalMemberV1(
            member_id=source.source_id,
            member_role="source",
            path_kind="file",
            path=f"{PROJECT_ROOT}/{source.path}",
            byte_count=10,
            sha256=source.sha256,
        )
        for source in sources
    ]
    if compile_units:
        physical_member_list.extend(
            (
                CurrentPhysicalMemberV1(
                    member_id="compiler-javac",
                    member_role="compiler",
                    path_kind="file",
                    path="/opt/jdk/bin/javac",
                    byte_count=128,
                    sha256=_sha("javac"),
                ),
                CurrentPhysicalMemberV1(
                    member_id="toolchain-jdk",
                    member_role="toolchain",
                    path_kind="tree",
                    path="/opt/jdk",
                    byte_count=1_024,
                    sha256=_sha("jdk-21"),
                ),
            )
        )
    physical_members = tuple(sorted(physical_member_list, key=lambda item: item.member_id))
    physical_roots: list[CurrentOutputRootV1] = []
    physical_entries: list[CurrentOutputEntryV1] = []
    missing_members: list[MissingPhysicalMemberV1] = []
    if observations is None:
        observations = tuple(
            ObservationSpec(unit.compile_unit_id, tuple(assignments[unit.compile_unit_id]))
            for unit in compile_units
            if unit.adapter_status == "supported"
        )
    basis_by_unit = {item.compile_unit_id: item for item in final_compile_bases}
    compile_observations: list[CompileUnitObservationV1] = []
    witnesses: list[PhysicalOutputWitnessV1] = []
    for index, spec in enumerate(observations):
        basis = basis_by_unit[spec.unit_id]
        observed_sources = _sorted_by(
            (
                SourceDigestEntry(
                    source_id=source_by_path[path].source_id,
                    path=path,
                    sha256=source_by_path[path].sha256,
                )
                for path in spec.source_paths
            ),
            lambda item: item.source_id,
        )
        identity_fields = {
            "compile_unit_id": spec.unit_id,
            "build_config_fingerprint": basis.build_config_fingerprint,
            "required_source_entries": observed_sources,
            "source_set_fingerprint": _sha(
                "source-set:"
                + spec.unit_id
                + ":"
                + ":".join(item.sha256 for item in observed_sources)
            ),
            "compiler_executable_path": basis.compiler_executable_path,
            "compiler_executable_sha256": basis.compiler_executable_sha256,
            "compiler_version": basis.compiler_version,
            "compiler_args_fingerprint": basis.compiler_args_fingerprint,
            "classpath_entries": basis.classpath_entries,
            "dependency_unit_identity_hashes": basis.dependency_unit_identity_hashes,
            "toolchain_fingerprint": basis.toolchain_fingerprint,
            "output_roots": basis.output_roots,
        }
        observation_id = canonical_content_id(
            "compile-observation-v1",
            {
                "evidence_epoch_id": epoch_id,
                "run_id": spec.run_id,
                "receipt_id": f"receipt-{spec.run_id}-{index}",
                "contract_id": f"contract-{spec.run_id}-{index}",
                "compile_unit_id": spec.unit_id,
            },
        )
        observation = CompileUnitObservationV1(
            observation_id=observation_id,
            evidence_epoch_id=epoch_id,
            run_id=spec.run_id,
            receipt_id=f"receipt-{spec.run_id}-{index}",
            contract_id=f"contract-{spec.run_id}-{index}",
            compile_unit_id=spec.unit_id,
            build_config_fingerprint=basis.build_config_fingerprint,
            compile_unit_identity_hash=compile_unit_identity_hash(identity_fields),
            source_entries=observed_sources,
            source_set_fingerprint=identity_fields["source_set_fingerprint"],
            compiler_executable_path=basis.compiler_executable_path,
            compiler_executable_sha256=basis.compiler_executable_sha256,
            compiler_version=basis.compiler_version,
            compiler_args_fingerprint=basis.compiler_args_fingerprint,
            classpath_entries=basis.classpath_entries,
            dependency_unit_identity_hashes=basis.dependency_unit_identity_hashes,
            toolchain_fingerprint=basis.toolchain_fingerprint,
            observed_output_roots=basis.output_roots,
            outcome=spec.outcome,
            actual_invocation_scope=_invocation(spec.run_id),
            generated_during_compile=(),
            conflicts=(),
        )
        compile_observations.append(observation)
        if not spec.witness or not basis.output_roots:
            continue
        class_entries = (
            spec.class_entries
            if spec.class_entries is not None
            else (f"example/{spec.unit_id}.class",)
        )
        witness_entries = tuple(
            PhysicalOutputEntry(
                root_id=basis.output_roots[0].root_id,
                relative_path=path,
                kind="class",
                byte_count=24,
                sha256=_sha(f"class:{spec.unit_id}:{path}"),
            )
            for path in sorted(class_entries)
        )
        witness_root = OutputRootWitness(
            root_id=basis.output_roots[0].root_id,
            path=basis.output_roots[0].path,
            verification_mode=basis.output_roots[0].verification_mode,
            tree_or_owned_entries_sha256=_sha(f"owned:{observation.observation_id}"),
            entry_count=len(witness_entries),
        )
        witness = PhysicalOutputWitnessV1(
            witness_id=canonical_content_id(
                "physical-output-witness-v1",
                {
                    "evidence_epoch_id": epoch_id,
                    "producer_observation_kind": "compile",
                    "producer_observation_id": observation.observation_id,
                },
            ),
            evidence_epoch_id=epoch_id,
            producer_observation_kind="compile",
            producer_observation_id=observation.observation_id,
            producer_unit_id=observation.compile_unit_id,
            producer_unit_identity_hash=observation.compile_unit_identity_hash,
            roots=(witness_root,),
            entries=witness_entries,
            aggregate_sha256=physical_output_aggregate_sha256((witness_root,), witness_entries),
        )
        witnesses.append(witness)
        if any(root.root_id == basis.output_roots[0].root_id for root in physical_roots):
            # Several historical observations may target the same final root.
            # The physical basis describes that root once; witnesses remain
            # separate candidate proofs in the evidence ledger.
            continue
        if spec.witness_state == "missing":
            missing_members.append(
                MissingPhysicalMemberV1(
                    member_id=basis.output_roots[0].member_id,
                    path=f"{PROJECT_ROOT}/{basis.output_roots[0].path}",
                )
            )
            continue
        current_entries = witness_entries
        current_digest = witness_root.tree_or_owned_entries_sha256
        if spec.witness_state == "mismatch":
            current_entries = tuple(
                CurrentOutputEntryV1(
                    root_id=entry.root_id,
                    relative_path=entry.relative_path,
                    kind=entry.kind,
                    byte_count=entry.byte_count,
                    sha256=_sha(f"changed:{entry.relative_path}"),
                )
                for entry in witness_entries
            )
            current_digest = _sha(f"changed-root:{spec.unit_id}")
        else:
            current_entries = tuple(
                CurrentOutputEntryV1(
                    root_id=entry.root_id,
                    relative_path=entry.relative_path,
                    kind=entry.kind,
                    byte_count=entry.byte_count,
                    sha256=entry.sha256,
                )
                for entry in witness_entries
            )
        physical_roots.append(
            CurrentOutputRootV1(
                member_id=basis.output_roots[0].member_id,
                root_id=basis.output_roots[0].root_id,
                path=f"{PROJECT_ROOT}/{basis.output_roots[0].path}",
                verification_mode=basis.output_roots[0].verification_mode,
                tree_or_owned_entries_sha256=current_digest,
                entry_count=len(current_entries),
                scan_complete=True,
            )
        )
        physical_entries.extend(current_entries)
    witnessed_root_ids = {root.member_id for root in physical_roots}
    missing_root_ids = {member.member_id for member in missing_members}
    for compile_unit in compile_units:
        if not compile_unit.output_roots:
            continue
        root = compile_unit.output_roots[0]
        if root.member_id in witnessed_root_ids or root.member_id in missing_root_ids:
            continue
        physical_roots.append(
            CurrentOutputRootV1(
                member_id=root.member_id,
                root_id=root.root_id,
                path=f"{PROJECT_ROOT}/{root.path}",
                verification_mode=root.verification_mode,
                tree_or_owned_entries_sha256=_sha(f"unobserved:{root.root_id}"),
                entry_count=0,
                scan_complete=True,
            )
        )
    requested_ids = tuple(
        sorted(
            {
                *(member.member_id for member in physical_members),
                *(root.member_id for unit in compile_units for root in unit.output_roots),
            }
        )
    )
    physical_identity = {
        "evidence_epoch_id": epoch_id,
        "expectation_id": expectation.expectation_id,
        "build_model_id": model_id,
        "requested_member_ids": requested_ids,
        "current_members": _sorted_by(physical_members, lambda item: item.member_id),
        "current_output_roots": _sorted_by(physical_roots, lambda item: item.member_id),
        "current_output_entries": _sorted_by(
            physical_entries, lambda item: f"{item.root_id}|{item.relative_path}"
        ),
        "missing_members": _sorted_by(missing_members, lambda item: item.member_id),
        "unreadable_members": (),
    }
    physical = CurrentPhysicalBasisSnapshotV1(
        physical_basis_snapshot_id=canonical_content_id(
            "current-physical-basis-v1", physical_identity
        ),
        **physical_identity,
        conflicts=(),
    )
    final_basis = derive_final_basis(
        census,
        expectation,
        model,
        physical,
        basis_revision=1,
    )
    derived_bases = {basis.compile_unit_id: basis for basis in final_basis.compile_unit_bases}
    canonical_observations: list[CompileUnitObservationV1] = []
    for observation in compile_observations:
        assert observation.compile_unit_id in derived_bases, final_basis.conflicts
        basis = derived_bases[observation.compile_unit_id]
        source_set_fingerprint = canonical_build_evidence_sha256(
            {
                "schema_version": 1,
                "fingerprint_kind": "compile-source-set",
                "payload": observation.source_entries,
            }
        )
        identity_fields = {
            "compile_unit_id": observation.compile_unit_id,
            "build_config_fingerprint": basis.build_config_fingerprint,
            "required_source_entries": observation.source_entries,
            "source_set_fingerprint": source_set_fingerprint,
            "compiler_executable_path": basis.compiler_executable_path,
            "compiler_executable_sha256": basis.compiler_executable_sha256,
            "compiler_version": basis.compiler_version,
            "compiler_args_fingerprint": basis.compiler_args_fingerprint,
            "classpath_entries": basis.classpath_entries,
            "dependency_unit_identity_hashes": basis.dependency_unit_identity_hashes,
            "toolchain_fingerprint": basis.toolchain_fingerprint,
            "output_roots": basis.output_roots,
        }
        payload = observation.model_dump(mode="python")
        payload.update(
            {
                "build_config_fingerprint": basis.build_config_fingerprint,
                "compile_unit_identity_hash": compile_unit_identity_hash(identity_fields),
                "source_set_fingerprint": source_set_fingerprint,
                "compiler_executable_path": basis.compiler_executable_path,
                "compiler_executable_sha256": basis.compiler_executable_sha256,
                "compiler_version": basis.compiler_version,
                "compiler_args_fingerprint": basis.compiler_args_fingerprint,
                "classpath_entries": basis.classpath_entries,
                "dependency_unit_identity_hashes": basis.dependency_unit_identity_hashes,
                "toolchain_fingerprint": basis.toolchain_fingerprint,
                "observed_output_roots": basis.output_roots,
            }
        )
        canonical_observations.append(CompileUnitObservationV1.model_validate(payload))
    compile_observations = canonical_observations
    identities_by_observation = {
        observation.observation_id: observation.compile_unit_identity_hash
        for observation in compile_observations
    }
    canonical_witnesses: list[PhysicalOutputWitnessV1] = []
    for witness in witnesses:
        payload = witness.model_dump(mode="python")
        payload["producer_unit_identity_hash"] = identities_by_observation[
            witness.producer_observation_id
        ]
        canonical_witnesses.append(PhysicalOutputWitnessV1.model_validate(payload))
    witnesses = canonical_witnesses
    evidence_set = _evidence_set(
        epoch_id=epoch_id,
        compile_observations=compile_observations,
        witnesses=witnesses,
        record_statuses=record_statuses,
        raw_hash_overrides=raw_hash_overrides,
    )
    inputs = ReconciliationInputs(
        epoch=epoch,
        goal_scope=scope,
        census=census,
        build_model=model,
        expectation=expectation,
        final_basis=final_basis,
        physical_basis=physical,
        evidence_set=evidence_set,
    )
    return World(
        inputs=inputs,
        epoch=epoch,
        scope=scope,
        census=census,
        model=model,
        expectation=expectation,
        physical=physical,
        final_basis=final_basis,
        evidence_set=evidence_set,
    )


def _with_evidence_set(world: World, evidence_set: BuildEvidenceSetSnapshotV1) -> World:
    inputs = ReconciliationInputs(
        epoch=world.epoch,
        goal_scope=world.scope,
        census=world.census,
        build_model=world.model,
        expectation=world.expectation,
        final_basis=world.final_basis,
        physical_basis=world.physical,
        evidence_set=evidence_set,
    )
    return World(
        inputs,
        world.epoch,
        world.scope,
        world.census,
        world.model,
        world.expectation,
        world.physical,
        world.final_basis,
        evidence_set,
    )


def _physical_from_payload(payload: Mapping[str, Any]) -> CurrentPhysicalBasisSnapshotV1:
    values = dict(payload)
    identity = {
        key: value
        for key, value in values.items()
        if key not in {"physical_basis_snapshot_id", "conflicts"}
    }
    values["physical_basis_snapshot_id"] = canonical_content_id(
        "current-physical-basis-v1", identity
    )
    return CurrentPhysicalBasisSnapshotV1.model_validate(values)


def _expectation_from_payload(payload: Mapping[str, Any]) -> JvmBuildExpectationV1:
    values = dict(payload)
    identity = {
        key: value for key, value in values.items() if key not in {"expectation_id", "conflicts"}
    }
    values["expectation_id"] = canonical_content_id("jvm-build-expectation-v1", identity)
    return JvmBuildExpectationV1.model_validate(values)


def _inputs_with(
    world: World,
    **updates: Any,
) -> ReconciliationInputs:
    values = {field: getattr(world.inputs, field) for field in ReconciliationInputs.model_fields}
    values.update(updates)
    return ReconciliationInputs.model_validate(values)


def _inputs_with_rederived_basis(
    world: World,
    physical: CurrentPhysicalBasisSnapshotV1,
) -> ReconciliationInputs:
    final_basis = derive_final_basis(
        world.census,
        world.expectation,
        world.model,
        physical,
        basis_revision=world.final_basis.basis_revision + 1,
    )
    return _inputs_with(world, physical_basis=physical, final_basis=final_basis)


def _evidence_matching_final_basis(
    world: World,
    final_basis: FinalBuildBasisV1,
) -> BuildEvidenceSetSnapshotV1:
    generator_bases = {basis.generator_unit_id: basis for basis in final_basis.generator_unit_bases}
    generators = []
    for observation in world.evidence_set.generator_observations:
        payload = observation.model_dump(mode="python")
        basis = generator_bases[observation.generator_unit_id]
        for field in FinalGeneratorUnitBasis.model_fields:
            payload[field] = getattr(basis, field)
        generators.append(GeneratorUnitObservationV1.model_validate(payload))

    compile_bases = {basis.compile_unit_id: basis for basis in final_basis.compile_unit_bases}
    compiles = []
    for observation in world.evidence_set.compile_observations:
        payload = observation.model_dump(mode="python")
        basis = compile_bases[observation.compile_unit_id]
        payload.update(
            {
                "build_config_fingerprint": basis.build_config_fingerprint,
                "compile_unit_identity_hash": basis.compile_unit_identity_hash,
                "source_entries": basis.required_source_entries,
                "source_set_fingerprint": basis.source_set_fingerprint,
                "compiler_executable_path": basis.compiler_executable_path,
                "compiler_executable_sha256": basis.compiler_executable_sha256,
                "compiler_version": basis.compiler_version,
                "compiler_args_fingerprint": basis.compiler_args_fingerprint,
                "classpath_entries": basis.classpath_entries,
                "dependency_unit_identity_hashes": basis.dependency_unit_identity_hashes,
                "toolchain_fingerprint": basis.toolchain_fingerprint,
                "observed_output_roots": basis.output_roots,
            }
        )
        compiles.append(CompileUnitObservationV1.model_validate(payload))

    package_bases = {basis.package_unit_id: basis for basis in final_basis.package_unit_bases}
    packages = []
    for observation in world.evidence_set.package_observations:
        payload = observation.model_dump(mode="python")
        basis = package_bases[observation.package_unit_id]
        for field in FinalPackageUnitBasis.model_fields:
            payload[field] = getattr(basis, field)
        packages.append(PackageUnitObservationV1.model_validate(payload))

    identities = {
        observation.observation_id: observation.generator_unit_identity_hash
        for observation in generators
    }
    identities.update(
        {
            observation.observation_id: observation.compile_unit_identity_hash
            for observation in compiles
        }
    )
    identities.update(
        {
            observation.observation_id: observation.package_unit_identity_hash
            for observation in packages
        }
    )
    witnesses = tuple(
        witness.model_copy(
            update={"producer_unit_identity_hash": identities[witness.producer_observation_id]}
        )
        for witness in world.evidence_set.output_witnesses
    )
    return _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        generator_observations=generators,
        compile_observations=compiles,
        package_observations=packages,
        witnesses=witnesses,
    )


def _compile_produced_later_input_world(
    *,
    fault: str = "exact",
    producer_output_requirement: str = "nonempty",
    generated_output_requirement: str = "empty_permitted",
) -> World:
    producer_path = "src/main/java/example/Producer.java"
    generated_root_path = "target/generated-sources/later"
    generated_path = f"{generated_root_path}/Generated.java"
    producer_unit_id = "compile-producer"
    consumer_unit_id = "compile-consumer"
    base = _make_world(
        source_paths=(producer_path, generated_path),
        assignments={
            producer_unit_id: (producer_path,),
            consumer_unit_id: (generated_path,),
        },
        output_requirements={producer_unit_id: producer_output_requirement},
    )
    generated_source = next(
        source for source in base.census.sources if source.path == generated_path
    )
    generated_source_entry = SourceDigestEntry(
        source_id=generated_source.source_id,
        path=generated_source.path,
        sha256=generated_source.sha256,
    )
    generated_root = OutputRootSpec(
        member_id="a-generated-later-root",
        root_id="a-generated-later-root",
        path=generated_root_path,
        verification_mode="shared_owned_entries",
        output_requirement=generated_output_requirement,
    )
    contract = GeneratedRootContract(
        generated_root_id=generated_root.root_id,
        output_root=generated_root,
        producer_unit_kind="compile",
        producer_unit_id=producer_unit_id,
        consumer_unit_ids=(consumer_unit_id,),
        mode="later_compile_input",
        active_profile_or_variant="default",
        model_provenance="fixture:compile-produced-later-input",
    )
    model = base.model.model_copy(update={"generated_root_contracts": (contract,)})
    expectation = derive_expectation(base.census, base.scope, model)

    generated_witness_digest = _sha("compile-produced-generated-root")
    generated_current_entry = CurrentOutputEntryV1(
        root_id=generated_root.root_id,
        relative_path="Generated.java",
        kind="other",
        byte_count=10,
        sha256=(
            _sha("compile-produced-generated-drift")
            if fault == "physical_hash_drift"
            else generated_source.sha256
        ),
    )
    physical_roots = tuple(base.physical.current_output_roots)
    physical_entries = tuple(base.physical.current_output_entries)
    missing_members = tuple(base.physical.missing_members)
    if fault == "physical_missing":
        missing_members = _sorted_by(
            (
                *missing_members,
                MissingPhysicalMemberV1(
                    member_id=generated_root.member_id,
                    path=f"{PROJECT_ROOT}/{generated_root.path}",
                ),
            ),
            lambda item: item.member_id,
        )
    else:
        physical_roots = _sorted_by(
            (
                *physical_roots,
                CurrentOutputRootV1(
                    member_id=generated_root.member_id,
                    root_id=generated_root.root_id,
                    path=f"{PROJECT_ROOT}/{generated_root.path}",
                    verification_mode=generated_root.verification_mode,
                    tree_or_owned_entries_sha256=(
                        _sha("compile-produced-generated-root-drift")
                        if fault == "physical_hash_drift"
                        else generated_witness_digest
                    ),
                    entry_count=1,
                    scan_complete=True,
                ),
            ),
            lambda item: item.member_id,
        )
        physical_entries = _sorted_by(
            (*physical_entries, generated_current_entry),
            lambda item: f"{item.root_id}|{item.relative_path}",
        )
    physical_payload = base.physical.model_dump(mode="python")
    physical_payload.update(
        {
            "expectation_id": expectation.expectation_id,
            "build_model_id": model.model_id,
            "requested_member_ids": tuple(
                sorted((*base.physical.requested_member_ids, generated_root.member_id))
            ),
            "current_output_roots": physical_roots,
            "current_output_entries": physical_entries,
            "missing_members": missing_members,
        }
    )
    physical = _physical_from_payload(physical_payload)
    final_basis = derive_final_basis(
        base.census,
        expectation,
        model,
        physical,
        basis_revision=2,
    )
    assert final_basis.conflicts == ()
    bases = {basis.compile_unit_id: basis for basis in final_basis.compile_unit_bases}
    producer_basis = bases[producer_unit_id]
    expected_producer_roots = tuple(
        sorted(
            (
                *next(
                    unit.output_roots
                    for unit in model.compile_units
                    if unit.compile_unit_id == producer_unit_id
                ),
                generated_root,
            ),
            key=lambda root: root.root_id,
        )
    )
    assert expected_producer_roots[0].root_id == generated_root.root_id
    assert producer_basis.output_roots == expected_producer_roots

    observations: list[CompileUnitObservationV1] = []
    for old_observation in base.evidence_set.compile_observations:
        basis = bases[old_observation.compile_unit_id]
        payload = old_observation.model_dump(mode="python")
        payload.update(
            {
                "build_config_fingerprint": basis.build_config_fingerprint,
                "compile_unit_identity_hash": basis.compile_unit_identity_hash,
                "source_entries": basis.required_source_entries,
                "source_set_fingerprint": basis.source_set_fingerprint,
                "compiler_executable_path": basis.compiler_executable_path,
                "compiler_executable_sha256": basis.compiler_executable_sha256,
                "compiler_version": basis.compiler_version,
                "compiler_args_fingerprint": basis.compiler_args_fingerprint,
                "classpath_entries": basis.classpath_entries,
                "dependency_unit_identity_hashes": basis.dependency_unit_identity_hashes,
                "toolchain_fingerprint": basis.toolchain_fingerprint,
                "observed_output_roots": basis.output_roots,
                "generated_during_compile": (
                    (generated_source_entry,) if basis.compile_unit_id == producer_unit_id else ()
                ),
            }
        )
        observations.append(CompileUnitObservationV1.model_validate(payload))
    observations_by_unit = {
        observation.compile_unit_id: observation for observation in observations
    }
    base_witnesses_by_unit = {
        witness.producer_unit_id: witness for witness in base.evidence_set.output_witnesses
    }
    witnesses: list[PhysicalOutputWitnessV1] = []
    for unit_id, observation in observations_by_unit.items():
        if unit_id != producer_unit_id:
            old_witness = base_witnesses_by_unit[unit_id]
            payload = old_witness.model_dump(mode="python")
            payload["producer_unit_identity_hash"] = observation.compile_unit_identity_hash
            witnesses.append(PhysicalOutputWitnessV1.model_validate(payload))
            continue

        old_witness = base_witnesses_by_unit.get(unit_id)
        roots = tuple(old_witness.roots) if old_witness is not None else ()
        entries = tuple(old_witness.entries) if old_witness is not None else ()
        include_generated_source = fault != "witness_omits_source"
        generated_entry = PhysicalOutputEntry(
            root_id=generated_root.root_id,
            relative_path="Generated.java",
            kind="other",
            byte_count=10,
            sha256=generated_source.sha256,
        )
        generated_witness_root = OutputRootWitness(
            root_id=generated_root.root_id,
            path=generated_root.path,
            verification_mode=generated_root.verification_mode,
            tree_or_owned_entries_sha256=generated_witness_digest,
            entry_count=1 if include_generated_source else 0,
        )
        roots = _sorted_by((*roots, generated_witness_root), lambda root: root.root_id)
        entries = _sorted_by(
            (*entries, *((generated_entry,) if include_generated_source else ())),
            lambda entry: f"{entry.root_id}|{entry.relative_path}",
        )
        witnesses.append(
            PhysicalOutputWitnessV1(
                witness_id=canonical_content_id(
                    "physical-output-witness-v1",
                    {
                        "evidence_epoch_id": base.epoch.evidence_epoch_id,
                        "producer_observation_kind": "compile",
                        "producer_observation_id": observation.observation_id,
                    },
                ),
                evidence_epoch_id=base.epoch.evidence_epoch_id,
                producer_observation_kind="compile",
                producer_observation_id=observation.observation_id,
                producer_unit_id=producer_unit_id,
                producer_unit_identity_hash=observation.compile_unit_identity_hash,
                roots=roots,
                entries=entries,
                aggregate_sha256=physical_output_aggregate_sha256(roots, entries),
            )
        )
    evidence_set = _evidence_set(
        epoch_id=base.epoch.evidence_epoch_id,
        compile_observations=observations,
        witnesses=witnesses,
    )
    inputs = ReconciliationInputs(
        epoch=base.epoch,
        goal_scope=base.scope,
        census=base.census,
        build_model=model,
        expectation=expectation,
        final_basis=final_basis,
        physical_basis=physical,
        evidence_set=evidence_set,
    )
    return World(
        inputs,
        base.epoch,
        base.scope,
        base.census,
        model,
        expectation,
        physical,
        final_basis,
        evidence_set,
    )


def _two_module_reconciliation_world(*, second_source_unreadable: bool = False) -> World:
    paths = (
        "a/src/main/java/a/A.java",
        "b/src/main/java/b/B.java",
    )
    base = _make_world(
        source_paths=paths,
        assignments={"compile-a": (paths[0],), "compile-b": (paths[1],)},
        observations=(ObservationSpec("compile-a", (paths[0],)),),
    )
    coordinates = {"compile-a": "com.example:a", "compile-b": "com.example:b"}
    domains = {"compile-a": "domain-a", "compile-b": "domain-b"}
    compile_units = tuple(
        unit.model_copy(
            update={
                "module_coordinate": coordinates[unit.compile_unit_id],
                "domain_id": domains[unit.compile_unit_id],
            }
        )
        for unit in base.model.compile_units
    )
    modules = (
        EvaluatedModuleV1(
            module_id="module-a",
            domain_id="domain-a",
            build_system="maven",
            module_coordinate="com.example:a",
            project_path="a",
            role="required",
            model_provenance="fixture:two-module-model",
        ),
        EvaluatedModuleV1(
            module_id="module-b",
            domain_id="domain-b",
            build_system="maven",
            module_coordinate="com.example:b",
            project_path="b",
            role="required",
            model_provenance="fixture:two-module-model",
        ),
    )
    model_payload = base.model.model_dump(mode="python")
    model_payload.update({"modules": modules, "compile_units": compile_units})
    model = EvaluatedBuildModelV1.model_validate(model_payload)
    expectation = derive_expectation(base.census, base.scope, model)

    physical_payload = base.physical.model_dump(mode="python")
    physical_payload["expectation_id"] = expectation.expectation_id
    if second_source_unreadable:
        second_source = next(source for source in base.census.sources if source.path == paths[1])
        second_member = next(
            member
            for member in base.physical.current_members
            if member.member_id == second_source.source_id
        )
        physical_payload["current_members"] = tuple(
            member
            for member in base.physical.current_members
            if member.member_id != second_source.source_id
        )
        physical_payload["unreadable_members"] = (
            UnreadablePhysicalMemberV1(
                member_id=second_member.member_id,
                path=second_member.path,
                reason="fixture cannot read module-b source",
            ),
        )
    physical = _physical_from_payload(physical_payload)
    final_basis = derive_final_basis(
        base.census,
        expectation,
        model,
        physical,
        basis_revision=2,
    )
    evidence_set = (
        _evidence_matching_final_basis(base, final_basis)
        if any(basis.compile_unit_id == "compile-a" for basis in final_basis.compile_unit_bases)
        else base.evidence_set
    )
    observation = evidence_set.compile_observations[0]
    observation = observation.model_copy(
        update={
            "actual_invocation_scope": ActualInvocationScopeV1(
                build_system="maven",
                selected_modules_or_tasks=("com.example:a",),
                lifecycle_or_tasks=("package",),
                profiles_or_variants=("default",),
                resume_from=None,
                also_make=False,
                selector_fingerprint=_sha("two-module-a-only"),
            )
        }
    )
    evidence_set = _evidence_set(
        epoch_id=base.epoch.evidence_epoch_id,
        compile_observations=(observation,),
        witnesses=evidence_set.output_witnesses,
    )
    inputs = ReconciliationInputs(
        epoch=base.epoch,
        goal_scope=base.scope,
        census=base.census,
        build_model=model,
        expectation=expectation,
        final_basis=final_basis,
        physical_basis=physical,
        evidence_set=evidence_set,
    )
    return World(
        inputs,
        base.epoch,
        base.scope,
        base.census,
        model,
        expectation,
        physical,
        final_basis,
        evidence_set,
    )


def _multi_root_compile_dependency_world() -> World:
    paths = (
        "upstream/src/main/java/example/Upstream.java",
        "downstream/src/main/java/example/Downstream.java",
    )
    upstream_id = "compile-upstream"
    downstream_id = "compile-downstream"
    base = _make_world(
        source_paths=paths,
        assignments={upstream_id: (paths[0],), downstream_id: (paths[1],)},
    )
    root_a = OutputRootSpec(
        member_id="root-upstream-a",
        root_id="root-upstream-a",
        path="target/upstream-a",
        verification_mode="shared_owned_entries",
        output_requirement="nonempty",
    )
    root_b = OutputRootSpec(
        member_id="root-upstream-b",
        root_id="root-upstream-b",
        path="target/upstream-b",
        verification_mode="shared_owned_entries",
        output_requirement="nonempty",
    )
    alias_b = ResolvedMemberSpec(
        member_id="classpath-upstream-b",
        member_role="upstream_output",
        path_kind="tree",
        path=None,
        coordinate=None,
        upstream_unit_id=upstream_id,
        upstream_root_id=root_b.root_id,
        resolution_provenance="fixture:adapter-order-b-first",
    )
    alias_a = ResolvedMemberSpec(
        member_id="classpath-upstream-a",
        member_role="upstream_output",
        path_kind="tree",
        path=None,
        coordinate=None,
        upstream_unit_id=upstream_id,
        upstream_root_id=root_a.root_id,
        resolution_provenance="fixture:adapter-order-a-second",
    )
    compile_units = []
    for unit in base.model.compile_units:
        if unit.compile_unit_id == upstream_id:
            unit = unit.model_copy(update={"output_roots": (root_a, root_b)})
        else:
            unit = unit.model_copy(
                update={
                    "compiler_environment_spec": unit.compiler_environment_spec.model_copy(
                        update={"classpath_members": (alias_b, alias_a)}
                    )
                }
            )
        compile_units.append(unit)
    edges = (
        CompileDependencyEdge(
            upstream_compile_unit_id=upstream_id,
            upstream_output_root_id=root_a.root_id,
            downstream_compile_unit_id=downstream_id,
            dependency_basis="fixture:upstream-root-a",
        ),
        CompileDependencyEdge(
            upstream_compile_unit_id=upstream_id,
            upstream_output_root_id=root_b.root_id,
            downstream_compile_unit_id=downstream_id,
            dependency_basis="fixture:upstream-root-b",
        ),
    )
    model_payload = base.model.model_dump(mode="python")
    model_payload.update(
        {
            "compile_units": tuple(compile_units),
            "compile_dependency_edges": edges,
        }
    )
    model = EvaluatedBuildModelV1.model_validate(model_payload)
    expectation = derive_expectation(base.census, base.scope, model)

    old_upstream_root_id = "root-compile-upstream"
    old_upstream_root = next(
        root for root in base.physical.current_output_roots if root.root_id == old_upstream_root_id
    )
    old_upstream_entry = next(
        entry
        for entry in base.physical.current_output_entries
        if entry.root_id == old_upstream_root_id
    )
    root_digests = {
        root_a.root_id: _sha("upstream-output-a"),
        root_b.root_id: _sha("upstream-output-b"),
    }
    upstream_current_roots = tuple(
        CurrentOutputRootV1(
            member_id=root.member_id,
            root_id=root.root_id,
            path=f"{PROJECT_ROOT}/{root.path}",
            verification_mode=root.verification_mode,
            tree_or_owned_entries_sha256=root_digests[root.root_id],
            entry_count=1,
            scan_complete=True,
        )
        for root in (root_a, root_b)
    )
    upstream_current_entries = tuple(
        CurrentOutputEntryV1(
            root_id=root.root_id,
            relative_path=(
                "example/Upstream.class"
                if root.root_id == root_a.root_id
                else "META-INF/upstream.marker"
            ),
            kind="class" if root.root_id == root_a.root_id else "resource",
            byte_count=old_upstream_entry.byte_count,
            sha256=_sha(f"entry:{root.root_id}"),
        )
        for root in (root_a, root_b)
    )
    aliases = (
        CurrentPhysicalMemberV1(
            member_id=alias_a.member_id,
            member_role="upstream_output",
            path_kind="tree",
            path=f"{PROJECT_ROOT}/{root_a.path}",
            byte_count=old_upstream_entry.byte_count,
            sha256=root_digests[root_a.root_id],
        ),
        CurrentPhysicalMemberV1(
            member_id=alias_b.member_id,
            member_role="upstream_output",
            path_kind="tree",
            path=f"{PROJECT_ROOT}/{root_b.path}",
            byte_count=old_upstream_entry.byte_count,
            sha256=root_digests[root_b.root_id],
        ),
    )
    physical_payload = base.physical.model_dump(mode="python")
    physical_payload.update(
        {
            "expectation_id": expectation.expectation_id,
            "requested_member_ids": tuple(
                sorted(
                    {
                        *(
                            item
                            for item in base.physical.requested_member_ids
                            if item != old_upstream_root.member_id
                        ),
                        root_a.member_id,
                        root_b.member_id,
                        alias_a.member_id,
                        alias_b.member_id,
                    }
                )
            ),
            "current_members": _sorted_by(
                (*base.physical.current_members, *aliases),
                lambda member: member.member_id,
            ),
            "current_output_roots": _sorted_by(
                (
                    *(
                        root
                        for root in base.physical.current_output_roots
                        if root.root_id != old_upstream_root_id
                    ),
                    *upstream_current_roots,
                ),
                lambda root: root.member_id,
            ),
            "current_output_entries": _sorted_by(
                (
                    *(
                        entry
                        for entry in base.physical.current_output_entries
                        if entry.root_id != old_upstream_root_id
                    ),
                    *upstream_current_entries,
                ),
                lambda entry: f"{entry.root_id}|{entry.relative_path}",
            ),
        }
    )
    physical = _physical_from_payload(physical_payload)
    final_basis = derive_final_basis(
        base.census,
        expectation,
        model,
        physical,
        basis_revision=2,
    )
    bases = {basis.compile_unit_id: basis for basis in final_basis.compile_unit_bases}
    assert final_basis.conflicts == ()
    assert tuple(entry.member_id for entry in bases[downstream_id].classpath_entries) == (
        alias_b.member_id,
        alias_a.member_id,
    )
    assert bases[downstream_id].dependency_unit_identity_hashes == (
        bases[upstream_id].compile_unit_identity_hash,
    )

    observations = []
    for old_observation in base.evidence_set.compile_observations:
        basis = bases[old_observation.compile_unit_id]
        payload = old_observation.model_dump(mode="python")
        payload.update(
            {
                "build_config_fingerprint": basis.build_config_fingerprint,
                "compile_unit_identity_hash": basis.compile_unit_identity_hash,
                "source_entries": basis.required_source_entries,
                "source_set_fingerprint": basis.source_set_fingerprint,
                "compiler_executable_path": basis.compiler_executable_path,
                "compiler_executable_sha256": basis.compiler_executable_sha256,
                "compiler_version": basis.compiler_version,
                "compiler_args_fingerprint": basis.compiler_args_fingerprint,
                "classpath_entries": basis.classpath_entries,
                "dependency_unit_identity_hashes": basis.dependency_unit_identity_hashes,
                "toolchain_fingerprint": basis.toolchain_fingerprint,
                "observed_output_roots": basis.output_roots,
            }
        )
        observations.append(CompileUnitObservationV1.model_validate(payload))
    observations_by_unit = {
        observation.compile_unit_id: observation for observation in observations
    }
    witnesses = []
    for old_witness in base.evidence_set.output_witnesses:
        observation = observations_by_unit[old_witness.producer_unit_id]
        if old_witness.producer_unit_id == upstream_id:
            witness_roots = tuple(
                OutputRootWitness(
                    root_id=root.root_id,
                    path=root.path,
                    verification_mode=root.verification_mode,
                    tree_or_owned_entries_sha256=root_digests[root.root_id],
                    entry_count=1,
                )
                for root in (root_a, root_b)
            )
            witness_entries = tuple(
                PhysicalOutputEntry(
                    root_id=entry.root_id,
                    relative_path=entry.relative_path,
                    kind=entry.kind,
                    byte_count=entry.byte_count,
                    sha256=entry.sha256,
                )
                for entry in upstream_current_entries
            )
            payload = old_witness.model_dump(mode="python")
            payload.update(
                {
                    "producer_unit_identity_hash": observation.compile_unit_identity_hash,
                    "roots": witness_roots,
                    "entries": witness_entries,
                    "aggregate_sha256": physical_output_aggregate_sha256(
                        witness_roots, witness_entries
                    ),
                }
            )
        else:
            payload = old_witness.model_dump(mode="python")
            payload["producer_unit_identity_hash"] = observation.compile_unit_identity_hash
        witnesses.append(PhysicalOutputWitnessV1.model_validate(payload))
    evidence_set = _evidence_set(
        epoch_id=base.epoch.evidence_epoch_id,
        compile_observations=observations,
        witnesses=witnesses,
    )
    inputs = ReconciliationInputs(
        epoch=base.epoch,
        goal_scope=base.scope,
        census=base.census,
        build_model=model,
        expectation=expectation,
        final_basis=final_basis,
        physical_basis=physical,
        evidence_set=evidence_set,
    )
    return World(
        inputs,
        base.epoch,
        base.scope,
        base.census,
        model,
        expectation,
        physical,
        final_basis,
        evidence_set,
    )


def _augment_with_generator_and_package(
    world: World,
    *,
    generator_identity_current: bool = True,
    generated_bytes_match_consumer: bool = True,
    package_change: str | None = None,
) -> World:
    """Add one exact generator and package closure around a compile world."""

    compile_basis = world.final_basis.compile_unit_bases[0]
    compile_observation = world.evidence_set.compile_observations[0]
    compile_witness = world.evidence_set.output_witnesses[0]
    generated_source = world.census.sources[0]
    generated_root_id = "generated-root-main"
    generator_unit_id = "generator-main"
    package_unit_id = "package-main"
    generator_input_spec = ResolvedMemberSpec(
        member_id="generator-input-schema",
        member_role="generator_input",
        path_kind="file",
        path=f"{PROJECT_ROOT}/schema/service.proto",
        coordinate=None,
        upstream_unit_id=None,
        upstream_root_id=None,
        resolution_provenance="fixture:effective-model",
    )
    generated_output_root = OutputRootSpec(
        member_id=generated_root_id,
        root_id=generated_root_id,
        path="target/generated-sources/proto",
        verification_mode="exclusive_tree",
        output_requirement="nonempty",
    )
    package_output_root = OutputRootSpec(
        member_id="package-root-main",
        root_id="package-root-main",
        path="target/package",
        verification_mode="exclusive_tree",
        output_requirement="nonempty",
    )
    artifact = RequestedArtifactSpec(
        artifact_id="artifact-app",
        root_id=package_output_root.root_id,
        path="target/package/app.jar",
        kind="jar",
    )
    generator_expectation = GeneratorUnitExpectation(
        generator_unit_id=generator_unit_id,
        module_coordinate="com.example:app",
        task_or_execution="generate-sources",
        role="required",
        input_roots=(generator_input_spec,),
        generator_environment_spec=GeneratorEnvironmentSpec(
            executable_path_basis=_path_basis(
                "/opt/tools/protoc",
                member_id="generator-protoc",
                member_role="generator_tool",
            ),
            tool_or_plugin_members=(),
            dependency_members=(),
            generator_args=("--java_out=target/generated-sources/proto",),
            toolchain_path_basis=_path_basis(
                "/opt/jdk",
                member_id="toolchain-jdk",
                member_role="toolchain",
                path_kind="tree",
            ),
        ),
        generated_root_ids=(generated_root_id,),
        model_provenance="fixture:effective-model",
    )
    package_expectation = PackageUnitExpectation(
        package_unit_id=package_unit_id,
        module_coordinate="com.example:app",
        task_or_execution="package",
        role="required",
        input_compile_unit_ids=(compile_basis.compile_unit_id,),
        resource_roots=(),
        manifest_or_packaging_config=(),
        packaging_environment_spec=PackagingEnvironmentSpec(
            executable_path_basis=_path_basis(
                "/usr/bin/jar",
                member_id="packaging-jar",
                member_role="packaging_tool",
            ),
            plugin_or_tool_members=(),
            external_dependency_members=(),
            packaging_args=("--create",),
            toolchain_path_basis=_path_basis(
                "/opt/jdk",
                member_id="toolchain-jdk",
                member_role="toolchain",
                path_kind="tree",
            ),
        ),
        output_roots=(package_output_root,),
        requested_artifacts=(artifact,),
        model_provenance="fixture:effective-model",
    )
    model = world.model.model_copy(
        update={
            "generator_units": (generator_expectation,),
            "package_units": (package_expectation,),
            "generated_root_contracts": (
                GeneratedRootContract(
                    generated_root_id=generated_root_id,
                    output_root=generated_output_root,
                    producer_unit_kind="generator",
                    producer_unit_id=generator_unit_id,
                    consumer_unit_ids=(compile_basis.compile_unit_id,),
                    mode="later_compile_input",
                    active_profile_or_variant="default",
                    model_provenance="fixture:effective-model",
                ),
            ),
        }
    )
    expectation_payload = world.expectation.model_dump(mode="python")
    expectation_payload.update(
        {
            "required_generator_unit_ids": (generator_unit_id,),
            "required_package_unit_ids": (package_unit_id,),
            "conflicts": (),
        }
    )
    expectation_identity = {
        key: value
        for key, value in expectation_payload.items()
        if key not in {"expectation_id", "conflicts"}
    }
    expectation_payload["expectation_id"] = canonical_content_id(
        "jvm-build-expectation-v1", expectation_identity
    )
    expectation = JvmBuildExpectationV1.model_validate(expectation_payload)

    generator_input = ResolvedDigestEntry(
        member_id="generator-input-schema",
        member_role="generator_input",
        path_kind="file",
        path=f"{PROJECT_ROOT}/schema/service.proto",
        byte_count=16,
        sha256=_sha("schema-current"),
    )
    generator_basis_fields: dict[str, Any] = {
        "generator_unit_id": generator_unit_id,
        "build_config_fingerprint": model.build_config_fingerprint,
        "input_entries": (generator_input,),
        "generator_executable_path": "/opt/tools/protoc",
        "generator_executable_sha256": _sha("protoc"),
        "generator_tool_entries": (),
        "generator_dependency_entries": (),
        "generator_args_fingerprint": _sha("protoc-args"),
        "generator_toolchain_fingerprint": _sha("jdk-21"),
        "generated_output_roots": (generated_output_root,),
    }
    generator_basis = FinalGeneratorUnitBasis(
        **generator_basis_fields,
        generator_unit_identity_hash=generator_unit_identity_hash(generator_basis_fields),
    )
    generator_observation_fields = dict(generator_basis_fields)
    if not generator_identity_current:
        generator_observation_fields["input_entries"] = (
            generator_input.model_copy(update={"sha256": _sha("schema-stale")}),
        )
    generator_observation = GeneratorUnitObservationV1(
        observation_id=canonical_content_id(
            "generator-observation-v1",
            {
                "evidence_epoch_id": world.epoch.evidence_epoch_id,
                "run_id": "run-generator",
                "receipt_id": "receipt-generator",
                "contract_id": "contract-generator",
                "generator_unit_id": generator_unit_id,
            },
        ),
        evidence_epoch_id=world.epoch.evidence_epoch_id,
        run_id="run-generator",
        receipt_id="receipt-generator",
        contract_id="contract-generator",
        **generator_observation_fields,
        generator_unit_identity_hash=generator_unit_identity_hash(generator_observation_fields),
        outcome="executed_success",
        actual_invocation_scope=_invocation("run-generator"),
        conflicts=(),
    )
    generated_sha = (
        generated_source.sha256
        if generated_bytes_match_consumer
        else _sha("different-generated-bytes")
    )
    generator_entry = PhysicalOutputEntry(
        root_id=generated_root_id,
        relative_path=generated_source.path.rsplit("/", 1)[-1],
        kind="other",
        byte_count=32,
        sha256=generated_sha,
    )
    generator_root = OutputRootWitness(
        root_id=generated_root_id,
        path="target/generated-sources/proto",
        verification_mode="exclusive_tree",
        tree_or_owned_entries_sha256=_sha("generated-root-current"),
        entry_count=1,
    )
    generator_witness = PhysicalOutputWitnessV1(
        witness_id=canonical_content_id(
            "physical-output-witness-v1",
            {
                "evidence_epoch_id": world.epoch.evidence_epoch_id,
                "producer_observation_kind": "generator",
                "producer_observation_id": generator_observation.observation_id,
            },
        ),
        evidence_epoch_id=world.epoch.evidence_epoch_id,
        producer_observation_kind="generator",
        producer_observation_id=generator_observation.observation_id,
        producer_unit_id=generator_unit_id,
        producer_unit_identity_hash=generator_observation.generator_unit_identity_hash,
        roots=(generator_root,),
        entries=(generator_entry,),
        aggregate_sha256=physical_output_aggregate_sha256((generator_root,), (generator_entry,)),
    )

    compile_root = world.physical.current_output_roots[0]
    package_basis_fields: dict[str, Any] = {
        "package_unit_id": package_unit_id,
        "build_config_fingerprint": model.build_config_fingerprint,
        "input_compile_unit_identity_hashes": (compile_basis.compile_unit_identity_hash,),
        "input_compile_output_digests": (
            CompileOutputDigestEntry(
                compile_unit_id=compile_basis.compile_unit_id,
                root_id=compile_root.root_id,
                sha256=compile_root.tree_or_owned_entries_sha256,
            ),
        ),
        "resource_entries": (),
        "packaging_executable_path": "/usr/bin/jar",
        "packaging_executable_sha256": _sha("jar-executable"),
        "packaging_tool_entries": (),
        "external_dependency_entries": (),
        "packaging_args_fingerprint": _sha("jar-args"),
        "packaging_toolchain_fingerprint": _sha("jdk-21"),
        "packaging_config_fingerprint": _sha("package-config"),
        "output_roots": (package_output_root,),
        "requested_artifacts": (artifact,),
    }
    package_basis = FinalPackageUnitBasis(
        **package_basis_fields,
        package_unit_identity_hash=package_unit_identity_hash(package_basis_fields),
    )
    package_observation_fields = dict(package_basis_fields)
    if package_change == "compile_identity":
        package_observation_fields["input_compile_unit_identity_hashes"] = (
            _sha("stale-compile-identity"),
        )
    elif package_change == "compile_output":
        package_observation_fields["input_compile_output_digests"] = (
            package_basis.input_compile_output_digests[0].model_copy(
                update={"sha256": _sha("stale-compile-output")}
            ),
        )
    elif package_change == "resource":
        package_observation_fields["resource_entries"] = (
            ResolvedDigestEntry(
                member_id="resource-app",
                member_role="resource",
                path_kind="file",
                path=f"{PROJECT_ROOT}/src/main/resources/app.conf",
                byte_count=8,
                sha256=_sha("stale-resource"),
            ),
        )
    elif package_change == "executable":
        package_observation_fields["packaging_executable_sha256"] = _sha("stale-jar-executable")
    elif package_change == "tool":
        package_observation_fields["packaging_tool_entries"] = (
            ResolvedDigestEntry(
                member_id="jar-plugin",
                member_role="packaging_tool",
                path_kind="file",
                path="/opt/maven/jar-plugin.jar",
                byte_count=8,
                sha256=_sha("stale-tool"),
            ),
        )
    elif package_change == "external_dependency":
        package_observation_fields["external_dependency_entries"] = (
            ResolvedDigestEntry(
                member_id="runtime-dependency",
                member_role="external_dependency",
                path_kind="file",
                path="/workspace/.m2/runtime.jar",
                byte_count=8,
                sha256=_sha("stale-runtime"),
            ),
        )
    elif package_change == "arguments":
        package_observation_fields["packaging_args_fingerprint"] = _sha("stale-args")
    elif package_change == "toolchain":
        package_observation_fields["packaging_toolchain_fingerprint"] = _sha("stale-toolchain")
    elif package_change == "config":
        package_observation_fields["packaging_config_fingerprint"] = _sha("stale-package-config")
    elif package_change == "artifact":
        package_observation_fields["requested_artifacts"] = (
            artifact.model_copy(update={"path": "target/package/other.jar"}),
        )
    package_observation = PackageUnitObservationV1(
        observation_id=canonical_content_id(
            "package-observation-v1",
            {
                "evidence_epoch_id": world.epoch.evidence_epoch_id,
                "run_id": "run-package",
                "receipt_id": "receipt-package",
                "contract_id": "contract-package",
                "package_unit_id": package_unit_id,
            },
        ),
        evidence_epoch_id=world.epoch.evidence_epoch_id,
        run_id="run-package",
        receipt_id="receipt-package",
        contract_id="contract-package",
        **package_observation_fields,
        package_unit_identity_hash=package_unit_identity_hash(package_observation_fields),
        observed_artifact_paths=tuple(
            item.path for item in package_observation_fields["requested_artifacts"]
        ),
        outcome="executed_success",
        actual_invocation_scope=_invocation("run-package"),
        conflicts=(),
    )
    package_entry = PhysicalOutputEntry(
        root_id=package_output_root.root_id,
        relative_path="app.jar",
        kind="jar",
        byte_count=64,
        sha256=_sha("app-jar"),
    )
    package_root = OutputRootWitness(
        root_id=package_output_root.root_id,
        path=package_output_root.path,
        verification_mode=package_output_root.verification_mode,
        tree_or_owned_entries_sha256=_sha("app-jar-root"),
        entry_count=1,
    )
    package_witness = PhysicalOutputWitnessV1(
        witness_id=canonical_content_id(
            "physical-output-witness-v1",
            {
                "evidence_epoch_id": world.epoch.evidence_epoch_id,
                "producer_observation_kind": "package",
                "producer_observation_id": package_observation.observation_id,
            },
        ),
        evidence_epoch_id=world.epoch.evidence_epoch_id,
        producer_observation_kind="package",
        producer_observation_id=package_observation.observation_id,
        producer_unit_id=package_unit_id,
        producer_unit_identity_hash=package_observation.package_unit_identity_hash,
        roots=(package_root,),
        entries=(package_entry,),
        aggregate_sha256=physical_output_aggregate_sha256((package_root,), (package_entry,)),
    )

    current_members = (
        *world.physical.current_members,
        CurrentPhysicalMemberV1(
            member_id=generator_input.member_id,
            member_role=generator_input.member_role,
            path_kind=generator_input.path_kind,
            path=generator_input.path,
            byte_count=generator_input.byte_count,
            sha256=generator_input.sha256,
        ),
        CurrentPhysicalMemberV1(
            member_id="generator-protoc",
            member_role="generator_tool",
            path_kind="file",
            path="/opt/tools/protoc",
            byte_count=128,
            sha256=_sha("protoc"),
        ),
        CurrentPhysicalMemberV1(
            member_id="packaging-jar",
            member_role="packaging_tool",
            path_kind="file",
            path="/usr/bin/jar",
            byte_count=128,
            sha256=_sha("jar-executable"),
        ),
    )
    current_roots = (
        *world.physical.current_output_roots,
        CurrentOutputRootV1(
            member_id=generated_output_root.member_id,
            root_id=generated_output_root.root_id,
            path=f"{PROJECT_ROOT}/{generated_output_root.path}",
            verification_mode=generated_output_root.verification_mode,
            tree_or_owned_entries_sha256=generator_root.tree_or_owned_entries_sha256,
            entry_count=1,
            scan_complete=True,
        ),
        CurrentOutputRootV1(
            member_id=package_output_root.member_id,
            root_id=package_output_root.root_id,
            path=f"{PROJECT_ROOT}/{package_output_root.path}",
            verification_mode=package_output_root.verification_mode,
            tree_or_owned_entries_sha256=package_root.tree_or_owned_entries_sha256,
            entry_count=1,
            scan_complete=True,
        ),
    )
    current_entries = (
        *world.physical.current_output_entries,
        CurrentOutputEntryV1(
            root_id=generator_entry.root_id,
            relative_path=generator_entry.relative_path,
            kind=generator_entry.kind,
            byte_count=generator_entry.byte_count,
            sha256=generator_entry.sha256,
        ),
        CurrentOutputEntryV1(
            root_id=package_entry.root_id,
            relative_path=package_entry.relative_path,
            kind=package_entry.kind,
            byte_count=package_entry.byte_count,
            sha256=package_entry.sha256,
        ),
    )
    physical_identity = {
        "evidence_epoch_id": world.epoch.evidence_epoch_id,
        "expectation_id": expectation.expectation_id,
        "build_model_id": model.model_id,
        "requested_member_ids": tuple(
            sorted(
                {
                    *(item.member_id for item in current_members),
                    *(item.member_id for item in current_roots),
                }
            )
        ),
        "current_members": _sorted_by(current_members, lambda item: item.member_id),
        "current_output_roots": _sorted_by(current_roots, lambda item: item.member_id),
        "current_output_entries": _sorted_by(
            current_entries, lambda item: f"{item.root_id}|{item.relative_path}"
        ),
        "missing_members": (),
        "unreadable_members": (),
    }
    physical = CurrentPhysicalBasisSnapshotV1(
        physical_basis_snapshot_id=canonical_content_id(
            "current-physical-basis-v1", physical_identity
        ),
        **physical_identity,
        conflicts=(),
    )
    final_basis = derive_final_basis(
        world.census,
        expectation,
        model,
        physical,
        basis_revision=1,
    )
    generator_basis = final_basis.generator_unit_bases[0]
    compile_basis = final_basis.compile_unit_bases[0]
    package_basis = final_basis.package_unit_bases[0]

    generator_fields = {
        field: getattr(generator_basis, field)
        for field in FinalGeneratorUnitBasis.model_fields
        if field != "generator_unit_identity_hash"
    }
    if not generator_identity_current:
        generator_fields["input_entries"] = (
            generator_fields["input_entries"][0].model_copy(
                update={"sha256": _sha("schema-stale")}
            ),
        )
    generator_payload = generator_observation.model_dump(mode="python")
    generator_payload.update(generator_fields)
    generator_payload["generator_unit_identity_hash"] = generator_unit_identity_hash(
        generator_fields
    )
    generator_observation = GeneratorUnitObservationV1.model_validate(generator_payload)

    compile_payload = compile_observation.model_dump(mode="python")
    compile_payload.update(
        {
            "build_config_fingerprint": compile_basis.build_config_fingerprint,
            "compile_unit_identity_hash": compile_basis.compile_unit_identity_hash,
            "source_entries": compile_basis.required_source_entries,
            "source_set_fingerprint": compile_basis.source_set_fingerprint,
            "compiler_executable_path": compile_basis.compiler_executable_path,
            "compiler_executable_sha256": compile_basis.compiler_executable_sha256,
            "compiler_version": compile_basis.compiler_version,
            "compiler_args_fingerprint": compile_basis.compiler_args_fingerprint,
            "classpath_entries": compile_basis.classpath_entries,
            "dependency_unit_identity_hashes": compile_basis.dependency_unit_identity_hashes,
            "toolchain_fingerprint": compile_basis.toolchain_fingerprint,
            "observed_output_roots": compile_basis.output_roots,
        }
    )
    compile_observation = CompileUnitObservationV1.model_validate(compile_payload)

    package_fields = {
        field: getattr(package_basis, field)
        for field in FinalPackageUnitBasis.model_fields
        if field != "package_unit_identity_hash"
    }
    if package_change == "compile_identity":
        package_fields["input_compile_unit_identity_hashes"] = (_sha("stale-compile-identity"),)
    elif package_change == "compile_output":
        package_fields["input_compile_output_digests"] = (
            package_basis.input_compile_output_digests[0].model_copy(
                update={"sha256": _sha("stale-compile-output")}
            ),
        )
    elif package_change == "resource":
        package_fields["resource_entries"] = (
            ResolvedDigestEntry(
                member_id="resource-app",
                member_role="resource",
                path_kind="file",
                path=f"{PROJECT_ROOT}/src/main/resources/app.conf",
                byte_count=8,
                sha256=_sha("stale-resource"),
            ),
        )
    elif package_change == "executable":
        package_fields["packaging_executable_sha256"] = _sha("stale-jar-executable")
    elif package_change == "tool":
        package_fields["packaging_tool_entries"] = (
            ResolvedDigestEntry(
                member_id="jar-plugin",
                member_role="packaging_tool",
                path_kind="file",
                path="/opt/maven/jar-plugin.jar",
                byte_count=8,
                sha256=_sha("stale-tool"),
            ),
        )
    elif package_change == "external_dependency":
        package_fields["external_dependency_entries"] = (
            ResolvedDigestEntry(
                member_id="runtime-dependency",
                member_role="external_dependency",
                path_kind="file",
                path="/workspace/.m2/runtime.jar",
                byte_count=8,
                sha256=_sha("stale-runtime"),
            ),
        )
    elif package_change == "arguments":
        package_fields["packaging_args_fingerprint"] = _sha("stale-args")
    elif package_change == "toolchain":
        package_fields["packaging_toolchain_fingerprint"] = _sha("stale-toolchain")
    elif package_change == "config":
        package_fields["packaging_config_fingerprint"] = _sha("stale-package-config")
    elif package_change == "artifact":
        package_fields["requested_artifacts"] = (
            artifact.model_copy(update={"path": "target/package/other.jar"}),
        )
    package_payload = package_observation.model_dump(mode="python")
    package_payload.update(package_fields)
    package_payload["package_unit_identity_hash"] = package_unit_identity_hash(package_fields)
    package_payload["observed_artifact_paths"] = tuple(
        item.path for item in package_fields["requested_artifacts"]
    )
    package_observation = PackageUnitObservationV1.model_validate(package_payload)

    observation_identities = {
        generator_observation.observation_id: generator_observation.generator_unit_identity_hash,
        compile_observation.observation_id: compile_observation.compile_unit_identity_hash,
        package_observation.observation_id: package_observation.package_unit_identity_hash,
    }
    canonical_witnesses = []
    for witness in (generator_witness, compile_witness, package_witness):
        witness_payload = witness.model_dump(mode="python")
        witness_payload["producer_unit_identity_hash"] = observation_identities[
            witness.producer_observation_id
        ]
        canonical_witnesses.append(PhysicalOutputWitnessV1.model_validate(witness_payload))
    generator_witness, compile_witness, package_witness = canonical_witnesses
    evidence_set = _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        generator_observations=(generator_observation,),
        compile_observations=(compile_observation,),
        package_observations=(package_observation,),
        witnesses=(generator_witness, compile_witness, package_witness),
    )
    inputs = ReconciliationInputs(
        epoch=world.epoch,
        goal_scope=world.scope,
        census=world.census,
        build_model=model,
        expectation=expectation,
        final_basis=final_basis,
        physical_basis=physical,
        evidence_set=evidence_set,
    )
    return World(
        inputs,
        world.epoch,
        world.scope,
        world.census,
        model,
        expectation,
        physical,
        final_basis,
        evidence_set,
    )


def _augment_with_child(
    world: World,
    *,
    child_has_source: bool = True,
    handoff_path: str | None = None,
    handoff_sha256: str | None = None,
    child_status: str = "complete",
    child_requires_parent: bool = False,
    child_record_status: str = "complete",
) -> World:
    parent_source = world.census.sources[0]
    child_scope_id = "child-scope-main"
    child_root = f"{PROJECT_ROOT}/child"
    expected_child_path = parent_source.path.removeprefix("child/")
    observed_child_path = handoff_path or expected_child_path
    observed_child_sha256 = handoff_sha256 or parent_source.sha256
    child_census_id = canonical_content_id(
        "jvm-source-census-v1",
        {
            "evidence_epoch_id": world.epoch.evidence_epoch_id,
            "project_root": child_root,
        },
    )
    child_source = _source(observed_child_path, digest=observed_child_sha256)
    child_census = JvmSourceCensusV1(
        census_id=child_census_id,
        evidence_epoch_id=world.epoch.evidence_epoch_id,
        project_root=child_root,
        census_revision=1,
        source_state_fingerprint=_sha(
            f"child-source-state:{observed_child_path}:{observed_child_sha256}"
        ),
        sources=(child_source,) if child_has_source else (),
        unreadable_roots=(),
        conflicts=(),
    )
    child_model_id = "child-model-main"
    child_edges = (
        (
            RequiredSourceEdge(
                source_id=child_source.source_id,
                compile_unit_id="child-compile-main",
            ),
        )
        if child_has_source
        else ()
    )
    child_classifications = (
        (
            SourceClassification(
                source_id=child_source.source_id,
                language="java",
                source_kind="main",
                classification="required",
                classification_basis=ClassificationBasisV1(
                    basis_kind="model_rule",
                    model_id=child_model_id,
                    basis_ref="child-compile-main",
                ),
                expected_compile_unit_ids=("child-compile-main",),
                child_scope_id=None,
            ),
        )
        if child_has_source
        else ()
    )
    child_expectation_identity = {
        "evidence_epoch_id": world.epoch.evidence_epoch_id,
        "census_id": child_census_id,
        "scope_id": child_scope_id,
        "model_id": child_model_id,
        "scope_fingerprint": _sha(f"scope:{child_scope_id}"),
        "source_state_fingerprint": child_census.source_state_fingerprint,
        "build_config_fingerprint": _sha("child-build-config"),
        "required_java_edges": child_edges,
        "source_classifications": child_classifications,
        "unclassified_source_ids": (),
        "required_generator_unit_ids": (),
        "required_compile_unit_ids": ("child-compile-main",) if child_has_source else (),
        "required_package_unit_ids": (),
        "unsupported_active_unit_ids": (),
        "required_child_scope_ids": (world.scope.scope_id,) if child_requires_parent else (),
        "delegation_edges": (),
    }
    child_expectation = JvmBuildExpectationV1(
        expectation_id=canonical_content_id("jvm-build-expectation-v1", child_expectation_identity),
        **child_expectation_identity,
        conflicts=(),
    )
    current = int(child_has_source and child_status in {"complete", "partial"})
    selected_child_compile = (
        (
            SelectedCompileProof(
                compile_unit_id="child-compile-main",
                compile_unit_identity_hash=_sha("child-compile-identity"),
                observation_id="child-compile-observation",
                witness_id="child-compile-witness",
            ),
        )
        if current
        else ()
    )
    child_digest = _sha(f"child-result:{child_status}:{child_has_source}")
    child_reconciliation = BuildReconciliationV1(
        reconciliation_id=canonical_content_id(
            "build-reconciliation-v1",
            {
                "evidence_epoch_id": world.epoch.evidence_epoch_id,
                "input_set_digest": child_digest,
            },
        ),
        evidence_epoch_id=world.epoch.evidence_epoch_id,
        source_census_id=child_census.census_id,
        goal_scope_id=child_scope_id,
        build_model_id=child_model_id,
        expectation_id=child_expectation.expectation_id,
        final_basis_id="child-final-basis",
        input_set_digest=child_digest,
        requested_action="build",
        status=child_status,
        source_census={
            "total_candidates": int(child_has_source),
            "classified": int(child_has_source),
            "unclassified": 0,
            "delegated": 0,
            "out_of_scope_by_reason": {},
        },
        java_sources={
            "local_expected": int(child_has_source),
            "local_current": current,
            "delegated_expected": 0,
            "delegated_current": 0,
            "project_expected": int(child_has_source),
            "project_current": current,
            "project_missing": int(child_has_source) - current,
            "project_stale": 0,
        },
        java_edges={
            "expected": int(child_has_source),
            "current": current,
            "missing": int(child_has_source) - current,
            "stale": 0,
        },
        generator_units={
            "expected": 0,
            "current": 0,
            "missing_or_invalid": 0,
            "unverifiable": 0,
        },
        compile_units={
            "expected": int(child_has_source),
            "coverage_current": current,
            "coverage_missing_or_invalid": int(child_has_source) - current,
            "coverage_unverifiable": 0,
        },
        physical_outputs={
            "observed_class_count": current,
            "witnesses_expected": int(child_has_source) + int(child_status == "partial"),
            "witnesses_verified": current,
            "sealed_entries": current,
            "verified_entries": current,
            "missing_entries": 0,
            "mismatched_entries": 0,
        },
        package_units={
            "expected": int(child_status == "partial"),
            "current": 0,
            "missing_or_invalid": int(child_status == "partial"),
            "unverifiable": 0,
        },
        active_language_units={
            "expected": int(child_has_source),
            "supported_current": current,
            "unsupported_or_unverifiable": 0,
        },
        child_scopes={
            "expected": 0,
            "complete": 0,
            "incomplete_or_unverifiable": 0,
        },
        modules={
            "expected": int(child_has_source),
            "built": int(child_has_source and child_status == "complete"),
            "partial": int(child_has_source and child_status != "complete"),
            "unverifiable": 0,
        },
        selected_generator_proofs=(),
        selected_compile_proofs=selected_child_compile,
        selected_package_proofs=(),
        selected_child_reconciliations=(),
        conflicts=() if child_status in {"complete", "partial"} else ("child incomplete",),
        evidence_refs=(
            (
                "child-compile-observation",
                "child-compile-witness",
            )
            if current
            else ()
        ),
    )
    model = world.model.model_copy(
        update={
            "child_build_scopes": (
                ChildScopeExpectation(
                    child_scope_id=child_scope_id,
                    parent_scope_id=world.scope.scope_id,
                    child_project_root=child_root,
                    build_system="maven",
                    ownership_basis="fixture:child-island",
                ),
            )
        }
    )
    delegated_classification = SourceClassification(
        source_id=parent_source.source_id,
        language="java",
        source_kind="main",
        classification="delegated_child_scope",
        classification_basis=ClassificationBasisV1(
            basis_kind="child_scope",
            model_id=model.model_id,
            basis_ref=child_scope_id,
        ),
        expected_compile_unit_ids=(),
        child_scope_id=child_scope_id,
    )
    delegation = DelegationEdge(
        parent_source_id=parent_source.source_id,
        child_scope_id=child_scope_id,
        expected_child_path=expected_child_path,
        expected_child_sha256=parent_source.sha256,
    )
    expectation_identity = {
        "evidence_epoch_id": world.epoch.evidence_epoch_id,
        "census_id": world.census.census_id,
        "scope_id": world.scope.scope_id,
        "model_id": model.model_id,
        "scope_fingerprint": world.scope.scope_fingerprint,
        "source_state_fingerprint": world.census.source_state_fingerprint,
        "build_config_fingerprint": model.build_config_fingerprint,
        "required_java_edges": (),
        "source_classifications": (delegated_classification,),
        "unclassified_source_ids": (),
        "required_generator_unit_ids": (),
        "required_compile_unit_ids": (),
        "required_package_unit_ids": (),
        "unsupported_active_unit_ids": (),
        "required_child_scope_ids": (child_scope_id,),
        "delegation_edges": (delegation,),
    }
    expectation = JvmBuildExpectationV1(
        expectation_id=canonical_content_id("jvm-build-expectation-v1", expectation_identity),
        **expectation_identity,
        conflicts=(),
    )
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["expectation_id"] = expectation.expectation_id
    physical_identity = {
        key: value
        for key, value in physical_payload.items()
        if key not in {"physical_basis_snapshot_id", "conflicts"}
    }
    physical_payload["physical_basis_snapshot_id"] = canonical_content_id(
        "current-physical-basis-v1", physical_identity
    )
    physical = CurrentPhysicalBasisSnapshotV1.model_validate(physical_payload)
    final_basis = FinalBuildBasisV1(
        basis_id=canonical_content_id(
            "final-build-basis-v1",
            {
                "evidence_epoch_id": world.epoch.evidence_epoch_id,
                "expectation_id": expectation.expectation_id,
            },
        ),
        evidence_epoch_id=world.epoch.evidence_epoch_id,
        basis_revision=1,
        expectation_id=expectation.expectation_id,
        physical_basis_snapshot_id=physical.physical_basis_snapshot_id,
        source_state_fingerprint=world.census.source_state_fingerprint,
        build_config_fingerprint=model.build_config_fingerprint,
        generator_unit_bases=(),
        compile_unit_bases=(),
        package_unit_bases=(),
        conflicts=(),
    )
    evidence_set = _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        child_censuses=(child_census,),
        child_expectations=(child_expectation,),
        child_reconciliations=(child_reconciliation,),
        record_statuses={"child_reconciliation": child_record_status},
    )
    inputs = ReconciliationInputs(
        epoch=world.epoch,
        goal_scope=world.scope,
        census=world.census,
        build_model=model,
        expectation=expectation,
        final_basis=final_basis,
        physical_basis=physical,
        evidence_set=evidence_set,
    )
    return World(
        inputs,
        world.epoch,
        world.scope,
        world.census,
        model,
        expectation,
        physical,
        final_basis,
        evidence_set,
    )


@dataclass(frozen=True)
class ChildNode:
    census: JvmSourceCensusV1
    expectation: JvmBuildExpectationV1
    reconciliation: BuildReconciliationV1


def _empty_child_node(
    *,
    epoch_id: str,
    scope_id: str,
    children: Sequence[ChildNode] = (),
) -> ChildNode:
    child_root = f"{PROJECT_ROOT}/{scope_id}"
    census_id = canonical_content_id(
        "jvm-source-census-v1",
        {"evidence_epoch_id": epoch_id, "project_root": child_root},
    )
    census = JvmSourceCensusV1(
        census_id=census_id,
        evidence_epoch_id=epoch_id,
        project_root=child_root,
        census_revision=1,
        source_state_fingerprint=_sha(f"source-state:{scope_id}"),
        sources=(),
        unreadable_roots=(),
        conflicts=(),
    )
    model_id = f"model-{scope_id}"
    child_scope_ids = tuple(sorted(item.expectation.scope_id for item in children))
    expectation_identity = {
        "evidence_epoch_id": epoch_id,
        "census_id": census_id,
        "scope_id": scope_id,
        "model_id": model_id,
        "scope_fingerprint": _sha(f"scope:{scope_id}"),
        "source_state_fingerprint": census.source_state_fingerprint,
        "build_config_fingerprint": _sha(f"build-config:{scope_id}"),
        "required_java_edges": (),
        "source_classifications": (),
        "unclassified_source_ids": (),
        "required_generator_unit_ids": (),
        "required_compile_unit_ids": (),
        "required_package_unit_ids": (),
        "unsupported_active_unit_ids": (),
        "required_child_scope_ids": child_scope_ids,
        "delegation_edges": (),
    }
    expectation = JvmBuildExpectationV1(
        expectation_id=canonical_content_id("jvm-build-expectation-v1", expectation_identity),
        **expectation_identity,
        conflicts=(),
    )
    selected_children = tuple(
        sorted(
            (
                SelectedChildReconciliation(
                    child_scope_id=child.expectation.scope_id,
                    child_census_id=child.census.census_id,
                    child_expectation_id=child.expectation.expectation_id,
                    reconciliation_id=child.reconciliation.reconciliation_id,
                )
                for child in children
            ),
            key=lambda item: item.child_scope_id,
        )
    )
    digest = _sha(f"reconciliation:{scope_id}")
    reconciliation = BuildReconciliationV1(
        reconciliation_id=canonical_content_id(
            "build-reconciliation-v1",
            {"evidence_epoch_id": epoch_id, "input_set_digest": digest},
        ),
        evidence_epoch_id=epoch_id,
        source_census_id=census_id,
        goal_scope_id=scope_id,
        build_model_id=model_id,
        expectation_id=expectation.expectation_id,
        final_basis_id=f"basis-{scope_id}",
        input_set_digest=digest,
        requested_action="build",
        status="complete",
        source_census={
            "total_candidates": 0,
            "classified": 0,
            "unclassified": 0,
            "delegated": 0,
            "out_of_scope_by_reason": {},
        },
        java_sources={
            "local_expected": 0,
            "local_current": 0,
            "delegated_expected": 0,
            "delegated_current": 0,
            "project_expected": 0,
            "project_current": 0,
            "project_missing": 0,
            "project_stale": 0,
        },
        java_edges={"expected": 0, "current": 0, "missing": 0, "stale": 0},
        generator_units={
            "expected": 0,
            "current": 0,
            "missing_or_invalid": 0,
            "unverifiable": 0,
        },
        compile_units={
            "expected": 0,
            "coverage_current": 0,
            "coverage_missing_or_invalid": 0,
            "coverage_unverifiable": 0,
        },
        physical_outputs={
            "observed_class_count": 0,
            "witnesses_expected": 0,
            "witnesses_verified": 0,
            "sealed_entries": 0,
            "verified_entries": 0,
            "missing_entries": 0,
            "mismatched_entries": 0,
        },
        package_units={
            "expected": 0,
            "current": 0,
            "missing_or_invalid": 0,
            "unverifiable": 0,
        },
        active_language_units={
            "expected": 0,
            "supported_current": 0,
            "unsupported_or_unverifiable": 0,
        },
        child_scopes={
            "expected": len(children),
            "complete": len(children),
            "incomplete_or_unverifiable": 0,
        },
        modules={"expected": 0, "built": 0, "partial": 0, "unverifiable": 0},
        selected_generator_proofs=(),
        selected_compile_proofs=(),
        selected_package_proofs=(),
        selected_child_reconciliations=selected_children,
        conflicts=(),
        evidence_refs=tuple(sorted(child.reconciliation.reconciliation_id for child in children)),
    )
    return ChildNode(census, expectation, reconciliation)


def _multiple_parent_child_graph_world() -> World:
    world = _make_world(source_paths=(), assignments={}, observations=())
    shared = _empty_child_node(
        epoch_id=world.epoch.evidence_epoch_id,
        scope_id="child-shared",
    )
    left = _empty_child_node(
        epoch_id=world.epoch.evidence_epoch_id,
        scope_id="child-left",
        children=(shared,),
    )
    right = _empty_child_node(
        epoch_id=world.epoch.evidence_epoch_id,
        scope_id="child-right",
        children=(shared,),
    )
    model = world.model.model_copy(
        update={
            "child_build_scopes": (
                ChildScopeExpectation(
                    child_scope_id=left.expectation.scope_id,
                    parent_scope_id=world.scope.scope_id,
                    child_project_root=f"{PROJECT_ROOT}/left",
                    build_system="maven",
                    ownership_basis="fixture:left-child",
                ),
                ChildScopeExpectation(
                    child_scope_id=right.expectation.scope_id,
                    parent_scope_id=world.scope.scope_id,
                    child_project_root=f"{PROJECT_ROOT}/right",
                    build_system="maven",
                    ownership_basis="fixture:right-child",
                ),
            )
        }
    )
    expectation_payload = world.expectation.model_dump(mode="python")
    expectation_payload["required_child_scope_ids"] = tuple(
        sorted((left.expectation.scope_id, right.expectation.scope_id))
    )
    expectation_identity = {
        key: value
        for key, value in expectation_payload.items()
        if key not in {"expectation_id", "conflicts"}
    }
    expectation_payload["expectation_id"] = canonical_content_id(
        "jvm-build-expectation-v1", expectation_identity
    )
    expectation = JvmBuildExpectationV1.model_validate(expectation_payload)
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["expectation_id"] = expectation.expectation_id
    physical_identity = {
        key: value
        for key, value in physical_payload.items()
        if key not in {"physical_basis_snapshot_id", "conflicts"}
    }
    physical_payload["physical_basis_snapshot_id"] = canonical_content_id(
        "current-physical-basis-v1", physical_identity
    )
    physical = CurrentPhysicalBasisSnapshotV1.model_validate(physical_payload)
    final_basis = FinalBuildBasisV1(
        basis_id=canonical_content_id(
            "final-build-basis-v1",
            {
                "evidence_epoch_id": world.epoch.evidence_epoch_id,
                "expectation_id": expectation.expectation_id,
            },
        ),
        evidence_epoch_id=world.epoch.evidence_epoch_id,
        basis_revision=1,
        expectation_id=expectation.expectation_id,
        physical_basis_snapshot_id=physical.physical_basis_snapshot_id,
        source_state_fingerprint=world.census.source_state_fingerprint,
        build_config_fingerprint=model.build_config_fingerprint,
        generator_unit_bases=(),
        compile_unit_bases=(),
        package_unit_bases=(),
        conflicts=(),
    )
    nodes = (left, right, shared)
    evidence_set = _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        child_censuses=tuple(item.census for item in nodes),
        child_expectations=tuple(item.expectation for item in nodes),
        child_reconciliations=tuple(item.reconciliation for item in nodes),
    )
    inputs = ReconciliationInputs(
        epoch=world.epoch,
        goal_scope=world.scope,
        census=world.census,
        build_model=model,
        expectation=expectation,
        final_basis=final_basis,
        physical_basis=physical,
        evidence_set=evidence_set,
    )
    return World(
        inputs,
        world.epoch,
        world.scope,
        world.census,
        model,
        expectation,
        physical,
        final_basis,
        evidence_set,
    )


def _replace_raw_hash(world: World, record_id: str, raw_sha256: str) -> World:
    rows = []
    for row in world.evidence_set.record_sets:
        hashes = tuple(
            (
                digest.model_copy(update={"raw_sha256": raw_sha256})
                if digest.record_id == record_id
                else digest
            )
            for digest in row.observed_record_hashes
        )
        rows.append(row.model_copy(update={"observed_record_hashes": hashes}))
    payload = world.evidence_set.model_dump(mode="python")
    payload["record_sets"] = tuple(rows)
    identity = {
        key: value for key, value in payload.items() if key not in {"snapshot_id", "conflicts"}
    }
    payload["snapshot_id"] = canonical_content_id("build-evidence-set-v1", identity)
    return _with_evidence_set(world, BuildEvidenceSetSnapshotV1.model_validate(payload))


def _status_rank(status: str) -> int:
    return {"unverifiable": 0, "absent": 1, "partial": 2, "complete": 3}[status]


def _assert_full_projection(result: BuildReconciliationV1) -> None:
    expected_blocks = {
        "source_census",
        "java_sources",
        "java_edges",
        "generator_units",
        "compile_units",
        "physical_outputs",
        "package_units",
        "active_language_units",
        "child_scopes",
        "modules",
        "selected_generator_proofs",
        "selected_compile_proofs",
        "selected_package_proofs",
        "selected_child_reconciliations",
        "conflicts",
        "evidence_refs",
    }
    assert expected_blocks <= result.model_fields_set


def test_reconciliation_inputs_are_one_closed_typed_wrapper() -> None:
    world = _make_world()

    assert ReconciliationInputs.model_fields["epoch"].annotation is ContainerEvidenceEpochV1
    assert set(ReconciliationInputs.model_fields) == {
        "epoch",
        "goal_scope",
        "census",
        "build_model",
        "expectation",
        "final_basis",
        "physical_basis",
        "evidence_set",
    }
    assert tuple(inspect.signature(reconcile_build).parameters) == ("inputs",)
    with pytest.raises((TypeError, ValidationError), match="extra|unexpected"):
        ReconciliationInputs(
            **world.inputs.model_dump(mode="python"),
            compile_observations=(),
        )
    with pytest.raises((TypeError, ValidationError, AttributeError)):
        world.inputs.epoch = world.epoch


@pytest.mark.parametrize(
    "field",
    (
        "epoch",
        "goal_scope",
        "census",
        "build_model",
        "expectation",
        "final_basis",
        "physical_basis",
        "evidence_set",
    ),
)
def test_reconciliation_inputs_reject_epoch_and_id_cross_link_drift(field: str) -> None:
    primary = _make_world()
    foreign = _make_world(epoch_tag="foreign")
    payload = {name: getattr(primary.inputs, name) for name in ReconciliationInputs.model_fields}
    payload[field] = getattr(foreign.inputs, field)

    with pytest.raises((TypeError, ValidationError, ValueError), match="epoch|cross|link|ID|id"):
        ReconciliationInputs.model_validate(payload)


@pytest.mark.parametrize(
    "drift",
    (
        "expectation_census_id",
        "expectation_model_id",
        "physical_expectation_id",
        "physical_build_model_id",
        "final_expectation_id",
        "final_physical_basis_snapshot_id",
        "final_source_state_fingerprint",
        "final_build_config_fingerprint",
    ),
)
def test_reconciliation_inputs_reject_same_epoch_head_cross_link_drift(drift: str) -> None:
    world = _make_world()
    payload = {name: getattr(world.inputs, name) for name in ReconciliationInputs.model_fields}

    if drift.startswith("expectation_"):
        expectation_payload = world.expectation.model_dump(mode="python")
        field = drift.removeprefix("expectation_")
        expectation_payload[field] = f"same-epoch-{field}-drift"
        if field == "model_id":
            expectation_payload["source_classifications"] = tuple(
                classification.model_copy(
                    update={
                        "classification_basis": classification.classification_basis.model_copy(
                            update={"model_id": expectation_payload[field]}
                        )
                    }
                )
                for classification in world.expectation.source_classifications
            )
        payload["expectation"] = _expectation_from_payload(expectation_payload)
    elif drift.startswith("physical_"):
        physical_payload = world.physical.model_dump(mode="python")
        field = drift.removeprefix("physical_")
        physical_payload[field] = f"same-epoch-{field}-drift"
        payload["physical_basis"] = _physical_from_payload(physical_payload)
    else:
        final_payload = world.final_basis.model_dump(mode="python")
        field = drift.removeprefix("final_")
        if field.endswith("fingerprint"):
            final_payload[field] = _sha(f"same-epoch-{field}-drift")
        else:
            final_payload[field] = f"same-epoch-{field}-drift"
        if field == "expectation_id":
            final_payload["basis_id"] = canonical_content_id(
                "final-build-basis-v1",
                {
                    "evidence_epoch_id": world.epoch.evidence_epoch_id,
                    "expectation_id": final_payload[field],
                },
            )
        if field == "build_config_fingerprint":
            compile_bases = []
            for compile_basis in world.final_basis.compile_unit_bases:
                compile_payload = compile_basis.model_dump(mode="python")
                compile_payload[field] = final_payload[field]
                identity = {
                    key: value
                    for key, value in compile_payload.items()
                    if key != "compile_unit_identity_hash"
                }
                compile_payload["compile_unit_identity_hash"] = compile_unit_identity_hash(identity)
                compile_bases.append(FinalCompileUnitBasis.model_validate(compile_payload))
            final_payload["compile_unit_bases"] = tuple(compile_bases)
        payload["final_basis"] = FinalBuildBasisV1.model_validate(final_payload)

    assert payload["epoch"].evidence_epoch_id == world.epoch.evidence_epoch_id
    with pytest.raises((TypeError, ValidationError, ValueError)):
        ReconciliationInputs.model_validate(payload)


def test_reconciliation_inputs_reject_self_consistent_narrowed_expectation() -> None:
    paths = ("a/src/main/java/a/A.java", "b/src/main/java/b/B.java")
    world = _make_world(
        source_paths=paths,
        assignments={"compile-a": (paths[0],), "compile-b": (paths[1],)},
    )
    sources_by_path = {source.path: source for source in world.census.sources}
    kept = sources_by_path[paths[0]]
    dropped = sources_by_path[paths[1]]
    kept_classification = next(
        item
        for item in world.expectation.source_classifications
        if item.source_id == kept.source_id
    )
    dropped_classification = SourceClassification(
        source_id=dropped.source_id,
        language=dropped.language,
        source_kind="main",
        classification="explicitly_excluded",
        classification_basis=ClassificationBasisV1(
            basis_kind="explicit_scope",
            model_id=world.model.model_id,
            basis_ref="fixture-narrowed-runner-view",
        ),
        expected_compile_unit_ids=(),
        child_scope_id=None,
    )
    expectation_payload = world.expectation.model_dump(mode="python")
    expectation_payload.update(
        {
            "required_java_edges": (
                RequiredSourceEdge(source_id=kept.source_id, compile_unit_id="compile-a"),
            ),
            "source_classifications": _sorted_by(
                (kept_classification, dropped_classification), lambda item: item.source_id
            ),
        }
    )
    assert expectation_payload["required_compile_unit_ids"] == ("compile-a", "compile-b")
    expectation = _expectation_from_payload(expectation_payload)
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["expectation_id"] = expectation.expectation_id
    physical = _physical_from_payload(physical_payload)
    final_basis = derive_final_basis(
        world.census,
        expectation,
        world.model,
        physical,
        basis_revision=2,
    )

    with pytest.raises((TypeError, ValidationError, ValueError), match="expectation|derive|scope"):
        ReconciliationInputs(
            epoch=world.epoch,
            goal_scope=world.scope,
            census=world.census,
            build_model=world.model,
            expectation=expectation,
            final_basis=final_basis,
            physical_basis=physical,
            evidence_set=world.evidence_set,
        )


def test_reconciliation_rejects_self_consistent_final_basis_that_omits_required_source() -> None:
    world = _make_world()
    baseline_basis = derive_final_basis(
        world.census,
        world.expectation,
        world.model,
        world.physical,
        basis_revision=2,
    )
    baseline_evidence = _evidence_matching_final_basis(world, baseline_basis)
    baseline_inputs = _inputs_with(
        world,
        final_basis=baseline_basis,
        evidence_set=baseline_evidence,
    )
    assert reconcile_build(baseline_inputs).status == "complete"
    assert world.expectation.required_java_edges

    empty_source_set_fingerprint = canonical_build_evidence_sha256(
        {
            "fingerprint_kind": "compile-source-set",
            "schema_version": 1,
            "payload": (),
        }
    )
    compile_payload = baseline_basis.compile_unit_bases[0].model_dump(mode="python")
    compile_payload.update(
        {
            "required_source_entries": (),
            "source_set_fingerprint": empty_source_set_fingerprint,
        }
    )
    compile_identity = {
        field: value
        for field, value in compile_payload.items()
        if field != "compile_unit_identity_hash"
    }
    compile_payload["compile_unit_identity_hash"] = compile_unit_identity_hash(compile_identity)
    forged_compile_basis = FinalCompileUnitBasis.model_validate(compile_payload)
    final_payload = baseline_basis.model_dump(mode="python")
    final_payload["compile_unit_bases"] = (forged_compile_basis,)
    forged_final_basis = FinalBuildBasisV1.model_validate(final_payload)

    observation_payload = baseline_evidence.compile_observations[0].model_dump(mode="python")
    observation_payload.update(
        {
            "source_entries": (),
            "source_set_fingerprint": empty_source_set_fingerprint,
            "compile_unit_identity_hash": forged_compile_basis.compile_unit_identity_hash,
        }
    )
    forged_observation = CompileUnitObservationV1.model_validate(observation_payload)
    witness_payload = baseline_evidence.output_witnesses[0].model_dump(mode="python")
    witness_payload["producer_unit_identity_hash"] = forged_compile_basis.compile_unit_identity_hash
    forged_witness = PhysicalOutputWitnessV1.model_validate(witness_payload)
    forged_evidence = _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        compile_observations=(forged_observation,),
        witnesses=(forged_witness,),
    )

    assert forged_final_basis.basis_id == baseline_basis.basis_id
    assert forged_final_basis.expectation_id == world.expectation.expectation_id
    assert (
        forged_final_basis.physical_basis_snapshot_id == world.physical.physical_basis_snapshot_id
    )
    assert (
        forged_observation.compile_unit_identity_hash == forged_witness.producer_unit_identity_hash
    )
    try:
        forged_inputs = _inputs_with(
            world,
            final_basis=forged_final_basis,
            evidence_set=forged_evidence,
        )
    except (ValidationError, ValueError):
        return

    result = reconcile_build(forged_inputs)
    assert result.status != "complete"
    assert result.compile_units.coverage_current == 0


def test_reconciliation_inputs_pin_scope_revision_and_fingerprint_not_only_scope_id() -> None:
    world = _make_world()
    successor = world.scope.model_copy(
        update={
            "scope_revision": 2,
            "predecessor_scope_hash": _sha("scope-revision-1-envelope"),
            "scope_fingerprint": _sha("scope-revision-2"),
        }
    )

    assert successor.scope_id == world.scope.scope_id
    assert world.model.goal_scope_revision == world.scope.scope_revision
    assert world.model.goal_scope_fingerprint == world.scope.scope_fingerprint
    assert world.expectation.scope_fingerprint == world.scope.scope_fingerprint
    payload = {name: getattr(world.inputs, name) for name in ReconciliationInputs.model_fields}
    payload["goal_scope"] = successor
    with pytest.raises((ValidationError, ValueError), match="revision|fingerprint|scope|current"):
        ReconciliationInputs.model_validate(payload)


def test_reconciliation_selections_are_frozen_deterministic_and_digest_every_id() -> None:
    world = _make_world()
    observation = world.evidence_set.compile_observations[0]
    witness = world.evidence_set.output_witnesses[0]
    proof = SelectedCompileProof(
        compile_unit_id=observation.compile_unit_id,
        compile_unit_identity_hash=observation.compile_unit_identity_hash,
        observation_id=observation.observation_id,
        witness_id=witness.witness_id,
    )
    selections = ReconciliationSelections(
        selected_generator_proofs=(),
        selected_compile_proofs=(proof,),
        selected_package_proofs=(),
        selected_child_reconciliations=(),
        conflicts=(),
    )
    baseline = reconciliation_input_set_digest(world.inputs, selections)
    assert baseline == reconciliation_input_set_digest(world.inputs, selections)
    with pytest.raises((TypeError, ValidationError, AttributeError)):
        selections.conflicts = ("mutated",)

    variants = (
        proof.model_copy(update={"compile_unit_id": "compile-other"}),
        proof.model_copy(update={"compile_unit_identity_hash": _sha("other-identity")}),
        proof.model_copy(update={"observation_id": "observation-other"}),
        proof.model_copy(update={"witness_id": "witness-other"}),
    )
    for changed_proof in variants:
        changed = ReconciliationSelections(
            selected_generator_proofs=(),
            selected_compile_proofs=(changed_proof,),
            selected_package_proofs=(),
            selected_child_reconciliations=(),
            conflicts=(),
        )
        assert reconciliation_input_set_digest(world.inputs, changed) != baseline


def test_typed_snapshot_has_exactly_seven_required_record_rows() -> None:
    world = _make_world()

    assert tuple(row.record_kind for row in world.evidence_set.record_sets) == RECORD_KINDS

    payload = world.evidence_set.model_dump(mode="python")
    for rows in (
        payload["record_sets"][:-1],
        (*payload["record_sets"], payload["record_sets"][0]),
        tuple(reversed(payload["record_sets"])),
    ):
        candidate = dict(payload)
        candidate["record_sets"] = rows
        with pytest.raises(ValidationError, match="record_sets|required|duplicate|misorder"):
            BuildEvidenceSetSnapshotV1.model_validate(candidate)


def test_snapshot_boundary_rejects_missing_duplicate_extra_or_mismatched_raw_hash_rows() -> None:
    world = _make_world()
    compile_index = RECORD_KINDS.index("compile_observation")
    compile_row = world.evidence_set.record_sets[compile_index]
    hashes = compile_row.observed_record_hashes
    malformed_hash_sets = (
        (),
        (*hashes, hashes[0]),
        (*hashes, EvidenceRecordDigestV1(record_id="extra-record", raw_sha256=_sha("extra"))),
        (
            EvidenceRecordDigestV1(
                record_id="mismatched-record",
                raw_sha256=hashes[0].raw_sha256,
            ),
        ),
    )
    for malformed in malformed_hash_sets:
        row_payload = compile_row.model_dump(mode="python")
        row_payload["observed_record_hashes"] = malformed
        with pytest.raises(ValidationError, match="hash|observed|duplicate|match"):
            EvidenceRecordSetCompletenessV1.model_validate(row_payload)


def test_host_authorized_raw_hash_changes_input_digest_even_when_models_match() -> None:
    world = _make_world()
    observation_id = world.evidence_set.compile_observations[0].observation_id
    changed = _replace_raw_hash(world, observation_id, _sha("different-host-raw-bytes"))
    empty = ReconciliationSelections(
        selected_generator_proofs=(),
        selected_compile_proofs=(),
        selected_package_proofs=(),
        selected_child_reconciliations=(),
        conflicts=(),
    )

    assert world.evidence_set.compile_observations == changed.evidence_set.compile_observations
    assert reconciliation_input_set_digest(world.inputs, empty) != reconciliation_input_set_digest(
        changed.inputs, empty
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "epoch_created_at",
        "epoch_initial_source_state_fingerprint",
        "scope_requested_action",
        "census_revision",
        "model_revision",
        "final_basis_revision",
        "census_conflicts",
        "build_model_conflicts",
        "physical_basis_conflicts",
        "evidence_set_conflicts",
    ),
)
def test_input_digest_commits_primary_id_stable_head_state(mutation: str) -> None:
    world = _make_world()
    empty = ReconciliationSelections(
        selected_generator_proofs=(),
        selected_compile_proofs=(),
        selected_package_proofs=(),
        selected_child_reconciliations=(),
        conflicts=(),
    )
    input_name, field = {
        "epoch_created_at": ("epoch", "created_at"),
        "epoch_initial_source_state_fingerprint": (
            "epoch",
            "initial_source_state_fingerprint",
        ),
        "scope_requested_action": ("goal_scope", "requested_action"),
        "census_revision": ("census", "census_revision"),
        "model_revision": ("build_model", "model_revision"),
        "final_basis_revision": ("final_basis", "basis_revision"),
        "census_conflicts": ("census", "conflicts"),
        "build_model_conflicts": ("build_model", "conflicts"),
        "physical_basis_conflicts": ("physical_basis", "conflicts"),
        "evidence_set_conflicts": ("evidence_set", "conflicts"),
    }[mutation]
    original_head = getattr(world.inputs, input_name)
    if field == "created_at":
        changed_value: Any = "2026-08-11T12:00:01Z"
    elif field.endswith("fingerprint"):
        changed_value = _sha(f"changed:{mutation}")
    elif field.endswith("revision"):
        changed_value = getattr(original_head, field) + 1
    elif field == "requested_action":
        changed_value = "build-and-package"
    else:
        changed_value = (f"changed conflict in {input_name}",)
    changed_head = original_head.model_copy(update={field: changed_value})
    updates = {input_name: changed_head}
    if mutation in {"census_conflicts", "build_model_conflicts"}:
        changed_census = changed_head if mutation == "census_conflicts" else world.census
        changed_model = changed_head if mutation == "build_model_conflicts" else world.model
        changed_expectation = derive_expectation(
            changed_census,
            world.scope,
            changed_model,
        )
        physical_payload = world.physical.model_dump(mode="python")
        physical_payload.update(
            {
                "expectation_id": changed_expectation.expectation_id,
                "build_model_id": changed_model.model_id,
            }
        )
        changed_physical = _physical_from_payload(physical_payload)
        updates.update(
            {
                "expectation": changed_expectation,
                "physical_basis": changed_physical,
                "final_basis": derive_final_basis(
                    changed_census,
                    changed_expectation,
                    changed_model,
                    changed_physical,
                    basis_revision=world.final_basis.basis_revision,
                ),
            }
        )
    elif mutation == "physical_basis_conflicts":
        updates["final_basis"] = derive_final_basis(
            world.census,
            world.expectation,
            world.model,
            changed_head,
            basis_revision=world.final_basis.basis_revision,
        )
    changed_inputs = _inputs_with(world, **updates)

    primary_fields = {
        "epoch": "evidence_epoch_id",
        "goal_scope": "scope_id",
        "census": "census_id",
        "build_model": "model_id",
        "expectation": "expectation_id",
        "final_basis": "basis_id",
        "physical_basis": "physical_basis_snapshot_id",
        "evidence_set": "snapshot_id",
    }
    assert getattr(changed_head, primary_fields[input_name]) == getattr(
        original_head, primary_fields[input_name]
    )
    assert reconciliation_input_set_digest(
        changed_inputs, empty
    ) != reconciliation_input_set_digest(world.inputs, empty)


def test_reconciliation_inputs_reject_forged_final_basis_conflict() -> None:
    world = _make_world()
    forged = world.final_basis.model_copy(update={"conflicts": ("producer-forged final conflict",)})

    with pytest.raises((ValidationError, ValueError), match="final basis|physical derivation"):
        _inputs_with(world, final_basis=forged)


def test_generator_package_child_selection_fields_and_conflicts_change_input_digest() -> None:
    closure = _complete_closure_world()
    generator_observation = closure.evidence_set.generator_observations[0]
    generator_witness = next(
        witness
        for witness in closure.evidence_set.output_witnesses
        if witness.producer_observation_kind == "generator"
    )
    generator_proof = SelectedGeneratorProof(
        generator_unit_id=generator_observation.generator_unit_id,
        generator_unit_identity_hash=generator_observation.generator_unit_identity_hash,
        observation_id=generator_observation.observation_id,
        witness_id=generator_witness.witness_id,
    )
    package_observation = closure.evidence_set.package_observations[0]
    package_witness = next(
        witness
        for witness in closure.evidence_set.output_witnesses
        if witness.producer_observation_kind == "package"
    )
    package_proof = SelectedPackageProof(
        package_unit_id=package_observation.package_unit_id,
        package_unit_identity_hash=package_observation.package_unit_identity_hash,
        observation_id=package_observation.observation_id,
        witness_id=package_witness.witness_id,
    )
    child_world = _child_world()
    child_reconciliation = child_world.evidence_set.child_reconciliations[0]
    child_proof = SelectedChildReconciliation(
        child_scope_id=child_reconciliation.goal_scope_id,
        child_census_id=child_reconciliation.source_census_id,
        child_expectation_id=child_reconciliation.expectation_id,
        reconciliation_id=child_reconciliation.reconciliation_id,
    )
    cases = (
        (
            closure,
            "selected_generator_proofs",
            generator_proof,
            tuple(type(generator_proof).model_fields),
        ),
        (
            closure,
            "selected_package_proofs",
            package_proof,
            tuple(type(package_proof).model_fields),
        ),
        (
            child_world,
            "selected_child_reconciliations",
            child_proof,
            tuple(type(child_proof).model_fields),
        ),
    )
    for case_world, selection_field, proof, fields in cases:
        selection_payload = {
            "selected_generator_proofs": (),
            "selected_compile_proofs": (),
            "selected_package_proofs": (),
            "selected_child_reconciliations": (),
            "conflicts": (),
        }
        selection_payload[selection_field] = (proof,)
        baseline = ReconciliationSelections(**selection_payload)
        baseline_digest = reconciliation_input_set_digest(case_world.inputs, baseline)
        for field in fields:
            value = getattr(proof, field)
            changed_value = (
                _sha(f"changed:{selection_field}:{field}")
                if "hash" in field
                else f"changed-{value}"
            )
            changed_proof = proof.model_copy(update={field: changed_value})
            changed_payload = dict(selection_payload)
            changed_payload[selection_field] = (changed_proof,)
            changed = ReconciliationSelections(**changed_payload)
            assert reconciliation_input_set_digest(case_world.inputs, changed) != baseline_digest
        conflicted = ReconciliationSelections(
            **{**selection_payload, "conflicts": (f"selection conflict: {selection_field}",)}
        )
        assert reconciliation_input_set_digest(case_world.inputs, conflicted) != baseline_digest


@pytest.mark.parametrize("record_kind", RECORD_KINDS)
def test_each_authorized_record_raw_hash_changes_input_digest(record_kind: str) -> None:
    world = (
        _complete_closure_world()
        if record_kind
        in {
            "generator_observation",
            "compile_observation",
            "package_observation",
            "output_witness",
        }
        else _child_world()
    )
    row = world.evidence_set.record_sets[RECORD_KINDS.index(record_kind)]
    assert row.observed_record_ids
    changed = _replace_raw_hash(
        world,
        row.observed_record_ids[0],
        _sha(f"changed-raw:{record_kind}"),
    )
    empty = ReconciliationSelections(
        selected_generator_proofs=(),
        selected_compile_proofs=(),
        selected_package_proofs=(),
        selected_child_reconciliations=(),
        conflicts=(),
    )

    assert reconciliation_input_set_digest(
        changed.inputs, empty
    ) != reconciliation_input_set_digest(world.inputs, empty)


def test_one_source_producing_many_classes_is_still_one_of_one() -> None:
    classes = tuple(f"example/App${index}.class" for index in range(25))
    world = _make_world(
        observations=(
            ObservationSpec(
                "compile-main",
                ("src/main/java/example/App.java",),
                class_entries=classes,
            ),
        )
    )

    result = reconcile_build(world.inputs)

    assert result.status == "complete"
    assert (result.java_sources.project_current, result.java_sources.project_expected) == (1, 1)
    assert result.physical_outputs.observed_class_count == len(classes)


def test_legal_source_needs_no_same_named_class_file() -> None:
    world = _make_world(
        observations=(
            ObservationSpec(
                "compile-main",
                ("src/main/java/example/App.java",),
                class_entries=("example/GeneratedBootstrap.class",),
            ),
        )
    )

    result = reconcile_build(world.inputs)

    assert result.status == "complete"
    assert result.java_sources.local_current == 1
    assert all(
        "App.class" not in item.relative_path for item in world.physical.current_output_entries
    )


def test_mechanically_empty_permitted_unit_may_verify_zero_output_entries() -> None:
    world = _make_world(
        output_requirements={"compile-main": "empty_permitted"},
        observations=(
            ObservationSpec(
                "compile-main",
                ("src/main/java/example/App.java",),
                class_entries=(),
            ),
        ),
    )

    result = reconcile_build(world.inputs)

    assert result.status == "complete"
    assert result.java_sources.local_current == 1
    assert result.physical_outputs.observed_class_count == 0
    assert result.physical_outputs.witnesses_verified == 1


def test_mechanically_no_output_unit_is_complete_without_a_witness() -> None:
    world = _make_world(
        output_requirements={"compile-main": "none"},
        observations=(
            ObservationSpec(
                "compile-main",
                ("src/main/java/example/App.java",),
                witness=False,
            ),
        ),
    )

    result = reconcile_build(world.inputs)

    assert result.status == "complete"
    assert result.compile_units.coverage_current == 1
    assert result.physical_outputs.witnesses_expected == 0
    assert result.physical_outputs.witnesses_verified == 0
    assert result.selected_compile_proofs[0].witness_id is None


def test_two_distinct_foo_paths_never_merge_by_basename() -> None:
    paths = ("a/src/main/java/a/Foo.java", "b/src/main/java/b/Foo.java")
    world = _make_world(source_paths=paths, assignments={"compile-main": paths})

    result = reconcile_build(world.inputs)

    assert result.status == "complete"
    assert result.java_sources.local_expected == 2
    assert result.java_sources.local_current == 2
    assert len({source.source_id for source in world.census.sources}) == 2


def test_observation_omitting_one_required_source_is_ineligible_despite_outputs() -> None:
    paths = ("src/main/java/example/A.java", "src/main/java/example/B.java")
    world = _make_world(
        source_paths=paths,
        assignments={"compile-main": paths},
        observations=(ObservationSpec("compile-main", (paths[0],)),),
    )

    result = reconcile_build(world.inputs)

    assert result.status == "absent"
    assert result.compile_units.coverage_current == 0
    assert result.java_edges.current == 0
    assert result.java_sources.local_current == 0


def test_two_half_unit_observations_never_frankenstein_merge() -> None:
    paths = ("src/main/java/example/A.java", "src/main/java/example/B.java")
    world = _make_world(
        source_paths=paths,
        assignments={"compile-main": paths},
        observations=(
            ObservationSpec("compile-main", (paths[0],), run_id="run-a"),
            ObservationSpec("compile-main", (paths[1],), run_id="run-b"),
        ),
    )

    result = reconcile_build(world.inputs)

    assert result.status == "absent"
    assert result.compile_units.coverage_current == 0
    assert result.java_edges.current == 0


def test_compatible_units_may_combine_across_runs_in_one_epoch() -> None:
    paths = ("a/src/main/java/a/A.java", "b/src/main/java/b/B.java")
    world = _make_world(
        source_paths=paths,
        assignments={"compile-a": (paths[0],), "compile-b": (paths[1],)},
        observations=(
            ObservationSpec("compile-a", (paths[0],), run_id="run-a"),
            ObservationSpec("compile-b", (paths[1],), run_id="run-b"),
        ),
    )

    result = reconcile_build(world.inputs)

    assert result.status == "complete"
    assert result.compile_units.coverage_current == 2
    assert {item.observation_id for item in result.selected_compile_proofs} == {
        item.observation_id for item in world.evidence_set.compile_observations
    }


def test_foreign_epoch_evidence_is_rejected_before_it_can_improve_truth() -> None:
    absent = _make_world(observations=())
    foreign = _make_world(epoch_tag="foreign")

    with pytest.raises(ValidationError, match="epoch|cross-link"):
        _evidence_set(
            epoch_id=absent.epoch.evidence_epoch_id,
            compile_observations=foreign.evidence_set.compile_observations,
            witnesses=foreign.evidence_set.output_witnesses,
        )
    assert reconcile_build(absent.inputs).status == "absent"


@pytest.mark.parametrize("outcome", ("no_source", "skipped", "failed"))
def test_non_positive_compile_outcomes_never_select(outcome: str) -> None:
    world = _make_world(
        observations=(
            ObservationSpec(
                "compile-main",
                ("src/main/java/example/App.java",),
                outcome=outcome,
            ),
        )
    )

    result = reconcile_build(world.inputs)

    expected = "unverifiable" if outcome == "failed" else "absent"
    assert result.status == expected
    assert result.compile_units.coverage_current == 0


def test_success_and_failed_observations_for_same_final_unit_are_unverifiable() -> None:
    path = "src/main/java/example/App.java"
    world = _make_world(
        observations=(
            ObservationSpec("compile-main", (path,), run_id="run-success"),
            ObservationSpec(
                "compile-main",
                (path,),
                run_id="run-failed",
                outcome="failed",
            ),
        )
    )

    result = reconcile_build(world.inputs)

    assert result.status == "unverifiable"
    assert result.compile_units.coverage_unverifiable == 1


def test_stale_failed_observation_does_not_erase_exact_success() -> None:
    paths = ("src/main/java/example/A.java", "src/main/java/example/B.java")
    world = _make_world(
        source_paths=paths,
        assignments={"compile-main": paths},
        observations=(
            ObservationSpec("compile-main", paths, run_id="run-success"),
            ObservationSpec(
                "compile-main",
                (paths[0],),
                run_id="run-stale-failed",
                outcome="failed",
            ),
        ),
    )

    result = reconcile_build(world.inputs)

    assert result.status == "complete"
    assert result.compile_units.coverage_current == 1


@pytest.mark.parametrize("outcome", POSITIVE_OUTCOMES)
def test_exact_positive_compile_outcomes_are_equivalent(outcome: str) -> None:
    world = _make_world(
        observations=(
            ObservationSpec(
                "compile-main",
                ("src/main/java/example/App.java",),
                outcome=outcome,
            ),
        )
    )

    result = reconcile_build(world.inputs)

    assert result.status == "complete"
    assert result.compile_units.coverage_current == 1


@pytest.mark.parametrize(
    "invocation_update",
    (
        {
            "lifecycle_or_tasks": ("test",),
            "selector_fingerprint": _sha("maven-test-lifecycle"),
        },
        {
            "profiles_or_variants": ("different-observed-profile",),
            "selector_fingerprint": _sha("maven-different-profile-list"),
        },
    ),
    ids=("test-lifecycle-covers-main-compile", "profile-list-is-not-an-extra-identity-axis"),
)
def test_maven_covering_lifecycle_and_profile_metadata_keep_exact_identity_current(
    invocation_update: Mapping[str, Any],
) -> None:
    world = _make_world()
    observation = world.evidence_set.compile_observations[0]
    changed = observation.model_copy(
        update={
            "actual_invocation_scope": observation.actual_invocation_scope.model_copy(
                update=dict(invocation_update)
            )
        }
    )
    evidence_set = _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        compile_observations=(changed,),
        witnesses=world.evidence_set.output_witnesses,
    )

    result = reconcile_build(_with_evidence_set(world, evidence_set).inputs)

    assert result.status == "complete"
    assert result.compile_units.coverage_current == 1


@pytest.mark.parametrize("incompatibility", ("module", "lifecycle"))
def test_actual_invocation_scope_must_cover_expected_module_and_lifecycle(
    incompatibility: str,
) -> None:
    world = _make_world()
    observation = world.evidence_set.compile_observations[0]
    invocation_update = (
        {
            "selected_modules_or_tasks": ("com.example:other",),
            "also_make": False,
            "selector_fingerprint": _sha("incompatible-module-selector"),
        }
        if incompatibility == "module"
        else {
            "lifecycle_or_tasks": ("validate",),
            "selector_fingerprint": _sha("incompatible-lifecycle-selector"),
        }
    )
    changed = observation.model_copy(
        update={
            "actual_invocation_scope": observation.actual_invocation_scope.model_copy(
                update=invocation_update
            )
        }
    )
    evidence_set = _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        compile_observations=(changed,),
        witnesses=world.evidence_set.output_witnesses,
    )

    result = reconcile_build(_with_evidence_set(world, evidence_set).inputs)

    assert result.status != "complete"
    assert result.compile_units.coverage_current == 0


@pytest.mark.parametrize(
    ("selected_module", "expected_current"),
    (("com.example:selected", 1), ("com.example:unrelated", 0)),
)
def test_maven_also_make_covers_only_graph_proven_transitive_upstream_units(
    selected_module: str,
    expected_current: int,
) -> None:
    world = _make_world()
    upstream = world.model.compile_units[0].model_copy(
        update={"module_coordinate": "com.example:upstream"}
    )
    upstream_root = upstream.output_roots[0]
    upstream_member = ResolvedMemberSpec(
        member_id="selected-classpath-upstream",
        member_role="upstream_output",
        path_kind="tree",
        path=None,
        coordinate=None,
        upstream_unit_id=upstream.compile_unit_id,
        upstream_root_id=upstream_root.root_id,
        resolution_provenance="fixture:compile-dependency-edge",
    )
    downstream = upstream.model_copy(
        update={
            "compile_unit_id": "compile-selected",
            "module_coordinate": "com.example:selected",
            "task_or_execution": "compile:compile-selected",
            "role": "optional",
            "scope_disposition": "explicit_scope_exclusion",
            "include_rules": (_source_rule("compile-selected", "**/*.java"),),
            "compiler_environment_spec": upstream.compiler_environment_spec.model_copy(
                update={"classpath_members": (upstream_member,)}
            ),
            "output_roots": (
                OutputRootSpec(
                    member_id="root-compile-selected",
                    root_id="root-compile-selected",
                    path="target/compile-selected",
                    verification_mode="shared_owned_entries",
                    output_requirement="nonempty",
                ),
            ),
        }
    )
    model_payload = world.model.model_dump(mode="python")
    model_payload.update(
        {
            "modules": (
                EvaluatedModuleV1(
                    module_id="module-selected",
                    domain_id="domain-app",
                    build_system="maven",
                    module_coordinate="com.example:selected",
                    project_path="selected",
                    role="optional",
                    model_provenance="fixture:reactor-model",
                ),
                EvaluatedModuleV1(
                    module_id="module-upstream",
                    domain_id="domain-app",
                    build_system="maven",
                    module_coordinate="com.example:upstream",
                    project_path="upstream",
                    role="required",
                    model_provenance="fixture:reactor-model",
                ),
            ),
            "compile_units": (upstream, downstream),
            "compile_dependency_edges": (
                CompileDependencyEdge(
                    upstream_compile_unit_id=upstream.compile_unit_id,
                    upstream_output_root_id=upstream_root.root_id,
                    downstream_compile_unit_id=downstream.compile_unit_id,
                    dependency_basis="fixture:selected depends on upstream",
                ),
            ),
        }
    )
    model = EvaluatedBuildModelV1.model_validate(model_payload)
    expectation = derive_expectation(world.census, world.scope, model)
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["expectation_id"] = expectation.expectation_id
    physical = _physical_from_payload(physical_payload)
    final_basis = derive_final_basis(
        world.census,
        expectation,
        model,
        physical,
        basis_revision=2,
    )
    evidence_set = _evidence_matching_final_basis(world, final_basis)
    observation = evidence_set.compile_observations[0]
    invocation = observation.actual_invocation_scope.model_copy(
        update={
            "selected_modules_or_tasks": (selected_module,),
            "also_make": True,
            "selector_fingerprint": _sha(f"reactor-selector:{selected_module}"),
        }
    )
    observation = observation.model_copy(update={"actual_invocation_scope": invocation})
    evidence_set = _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        compile_observations=(observation,),
        witnesses=evidence_set.output_witnesses,
    )
    inputs = _inputs_with(
        world,
        build_model=model,
        expectation=expectation,
        physical_basis=physical,
        final_basis=final_basis,
        evidence_set=evidence_set,
    )

    result = reconcile_build(inputs)

    assert result.compile_units.coverage_current == expected_current
    assert result.status == ("complete" if expected_current else "absent")


def test_two_upstream_roots_preserve_classpath_order_but_dedupe_dependency_identity() -> None:
    world = _multi_root_compile_dependency_world()

    result = reconcile_build(world.inputs)

    downstream = next(
        basis
        for basis in world.final_basis.compile_unit_bases
        if basis.compile_unit_id == "compile-downstream"
    )
    upstream = next(
        basis
        for basis in world.final_basis.compile_unit_bases
        if basis.compile_unit_id == "compile-upstream"
    )
    assert tuple(entry.member_id for entry in downstream.classpath_entries) == (
        "classpath-upstream-b",
        "classpath-upstream-a",
    )
    assert downstream.dependency_unit_identity_hashes == (upstream.compile_unit_identity_hash,)
    assert result.status == "complete"
    assert result.compile_units.coverage_current == result.compile_units.expected == 2


@pytest.mark.parametrize(
    ("executed_tasks", "expected_current"),
    (((":app:compileJava",), 1), ((), 0), ((":app:test",), 0)),
    ids=("exact-task", "empty-task-set", "other-task"),
)
def test_gradle_invocation_requires_exact_adapter_normalized_executed_task(
    executed_tasks: tuple[str, ...],
    expected_current: int,
) -> None:
    world = _make_world()
    compile_unit = world.model.compile_units[0].model_copy(
        update={
            "build_system": "gradle",
            "module_coordinate": ":app",
            "task_or_execution": ":app:compileJava",
        }
    )
    module = world.model.modules[0].model_copy(
        update={"build_system": "gradle", "module_coordinate": ":app"}
    )
    model = world.model.model_copy(update={"modules": (module,), "compile_units": (compile_unit,)})
    expectation = derive_expectation(world.census, world.scope, model)
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["expectation_id"] = expectation.expectation_id
    physical = _physical_from_payload(physical_payload)
    final_basis = derive_final_basis(
        world.census,
        expectation,
        model,
        physical,
        basis_revision=2,
    )
    evidence_set = _evidence_matching_final_basis(world, final_basis)
    observation = evidence_set.compile_observations[0].model_copy(
        update={
            "actual_invocation_scope": ActualInvocationScopeV1(
                build_system="gradle",
                selected_modules_or_tasks=executed_tasks,
                lifecycle_or_tasks=executed_tasks,
                profiles_or_variants=("default",),
                resume_from=None,
                also_make=False,
                selector_fingerprint=_sha(f"gradle-tasks:{executed_tasks!r}"),
            )
        }
    )
    evidence_set = _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        compile_observations=(observation,),
        witnesses=evidence_set.output_witnesses,
    )
    inputs = _inputs_with(
        world,
        build_model=model,
        expectation=expectation,
        physical_basis=physical,
        final_basis=final_basis,
        evidence_set=evidence_set,
    )

    result = reconcile_build(inputs)

    assert result.compile_units.coverage_current == expected_current
    assert result.status == ("complete" if expected_current else "absent")


@pytest.mark.parametrize("witness_state", ("missing", "mismatch"))
def test_output_failure_preserves_source_coverage_but_prevents_completion(
    witness_state: str,
) -> None:
    world = _make_world(
        observations=(
            ObservationSpec(
                "compile-main",
                ("src/main/java/example/App.java",),
                witness_state=witness_state,
            ),
        )
    )

    result = reconcile_build(world.inputs)

    assert result.status == "partial"
    assert result.compile_units.coverage_current == 1
    assert result.java_sources.local_current == 1
    assert result.physical_outputs.witnesses_verified == 0
    assert result.selected_compile_proofs[0].witness_id is None


def test_complete_known_empty_witness_ledger_preserves_coverage_but_is_partial() -> None:
    world = _make_world(
        observations=(
            ObservationSpec(
                "compile-main",
                ("src/main/java/example/App.java",),
                witness=False,
            ),
        )
    )

    result = reconcile_build(world.inputs)

    witness_row = world.evidence_set.record_sets[RECORD_KINDS.index("output_witness")]
    assert witness_row.status == "complete"
    assert witness_row.observed_record_ids == ()
    assert result.status == "partial"
    assert result.compile_units.coverage_current == 1
    assert result.physical_outputs.witnesses_verified == 0


@pytest.mark.parametrize(
    "record_status",
    ("unavailable", "set_mismatch", "tombstoned", "conflict"),
)
def test_incomplete_evidence_authority_is_unverifiable(record_status: str) -> None:
    world = _make_world(record_statuses={"compile_observation": record_status})

    result = reconcile_build(world.inputs)

    assert result.status == "unverifiable"
    assert result.compile_units.coverage_unverifiable == 1
    assert result.java_sources.project_expected == 1


@pytest.mark.parametrize("record_kind", RECORD_KINDS)
def test_every_unavailable_record_kind_fails_closed(record_kind: str) -> None:
    result = reconcile_build(_make_world(record_statuses={record_kind: "unavailable"}).inputs)

    assert result.status == "unverifiable"
    assert result.java_sources.project_expected == 1
    _assert_full_projection(result)


def test_unreadable_source_census_root_fails_closed_end_to_end() -> None:
    world = _make_world()
    census = world.census.model_copy(update={"unreadable_roots": ("src/main/java",)})
    expectation = derive_expectation(census, world.scope, world.model)
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["expectation_id"] = expectation.expectation_id
    physical = _physical_from_payload(physical_payload)
    final_basis = derive_final_basis(
        census,
        expectation,
        world.model,
        physical,
        basis_revision=2,
    )

    result = reconcile_build(
        _inputs_with(
            world,
            census=census,
            expectation=expectation,
            physical_basis=physical,
            final_basis=final_basis,
        )
    )

    assert result.status == "unverifiable"
    assert result.source_census.total_candidates == 1
    _assert_full_projection(result)


@pytest.mark.parametrize(
    "head",
    (
        "census",
        "build_model",
        "expectation",
        "final_basis",
        "physical_basis",
        "evidence_set",
    ),
)
def test_every_conflict_bearing_top_level_head_fails_closed_end_to_end(head: str) -> None:
    world = _make_world()
    value = getattr(world.inputs, head).model_copy(
        update={"conflicts": (f"fixture conflict in {head}",)}
    )

    if head in {"expectation", "final_basis"}:
        with pytest.raises(
            (ValidationError, ValueError), match="expectation|final basis|derivation"
        ):
            _inputs_with(world, **{head: value})
        return

    updates = {head: value}
    if head in {"census", "build_model"}:
        changed_census = value if head == "census" else world.census
        changed_model = value if head == "build_model" else world.model
        expectation = derive_expectation(changed_census, world.scope, changed_model)
        physical_payload = world.physical.model_dump(mode="python")
        physical_payload.update(
            {
                "expectation_id": expectation.expectation_id,
                "build_model_id": changed_model.model_id,
            }
        )
        physical = _physical_from_payload(physical_payload)
        updates.update(
            {
                "expectation": expectation,
                "physical_basis": physical,
                "final_basis": derive_final_basis(
                    changed_census,
                    expectation,
                    changed_model,
                    physical,
                    basis_revision=world.final_basis.basis_revision,
                ),
            }
        )
    elif head == "physical_basis":
        updates["final_basis"] = derive_final_basis(
            world.census,
            world.expectation,
            world.model,
            value,
            basis_revision=world.final_basis.basis_revision,
        )

    result = reconcile_build(_inputs_with(world, **updates))

    assert result.status == "unverifiable"
    assert result.conflicts
    _assert_full_projection(result)


def test_unclassified_source_is_unverifiable_and_keeps_full_census() -> None:
    path = "src/main/java/example/Unknown.java"
    world = _make_world(
        source_paths=(path,),
        assignments={},
        observations=(),
        unclassified_paths=(path,),
    )

    result = reconcile_build(world.inputs)

    assert result.status == "unverifiable"
    assert result.source_census.total_candidates == 1
    assert result.source_census.unclassified == 1


def test_unsupported_active_language_caps_otherwise_current_java() -> None:
    world = _make_world(unsupported_language=True)

    result = reconcile_build(world.inputs)

    assert result.status == "unverifiable"
    assert result.java_sources.local_current == 1
    assert result.active_language_units.unsupported_or_unverifiable == 1
    assert result.modules.unverifiable == 1


def test_no_required_obligations_and_no_blocker_is_complete_with_zero_counts() -> None:
    world = _make_world(source_paths=(), assignments={}, observations=())

    result = reconcile_build(world.inputs)

    assert result.status == "complete"
    assert result.java_sources.project_expected == 0
    assert result.java_sources.project_current == 0
    assert result.compile_units.expected == 0
    assert result.physical_outputs.witnesses_expected == 0
    _assert_full_projection(result)


def test_required_work_with_complete_known_empty_ledgers_is_absent() -> None:
    world = _make_world(observations=())

    result = reconcile_build(world.inputs)

    assert result.status == "absent"
    assert result.java_sources.project_expected == 1
    assert result.java_sources.project_current == 0
    assert result.compile_units.coverage_missing_or_invalid == 1


def test_current_plus_known_missing_folds_to_partial() -> None:
    paths = ("a/src/main/java/a/A.java", "b/src/main/java/b/B.java")
    world = _make_world(
        source_paths=paths,
        assignments={"compile-a": (paths[0],), "compile-b": (paths[1],)},
        observations=(ObservationSpec("compile-a", (paths[0],)),),
    )

    result = reconcile_build(world.inputs)

    assert result.status == "partial"
    assert result.compile_units.coverage_current == 1
    assert result.compile_units.coverage_missing_or_invalid == 1
    assert (result.java_sources.project_current, result.java_sources.project_expected) == (1, 2)


def test_exact_compile_and_output_closure_is_complete() -> None:
    result = reconcile_build(_make_world().inputs)

    assert result.status == "complete"
    assert result.compile_units.coverage_current == result.compile_units.expected == 1
    assert result.physical_outputs.witnesses_verified == 1
    assert result.physical_outputs.missing_entries == 0
    assert result.physical_outputs.mismatched_entries == 0


def test_compile_produced_later_input_root_is_derived_sorted_and_complete() -> None:
    world = _compile_produced_later_input_world()

    result = reconcile_build(world.inputs)

    producer_basis = next(
        basis
        for basis in world.final_basis.compile_unit_bases
        if basis.compile_unit_id == "compile-producer"
    )
    assert tuple(root.root_id for root in producer_basis.output_roots) == (
        "a-generated-later-root",
        "root-compile-producer",
    )
    assert result.status == "complete"
    assert result.compile_units.coverage_current == result.compile_units.expected == 2
    assert result.physical_outputs.witnesses_verified == 2


@pytest.mark.parametrize(
    "fault",
    ("physical_missing", "physical_hash_drift", "witness_omits_source"),
)
def test_compile_produced_later_input_requires_exact_physical_handoff(fault: str) -> None:
    world = _compile_produced_later_input_world(fault=fault)

    result = reconcile_build(world.inputs)

    selected_ids = {proof.compile_unit_id for proof in result.selected_compile_proofs}
    assert result.status != "complete"
    assert "compile-consumer" not in selected_ids


def test_later_generated_root_requires_witness_when_compile_unit_declares_no_class_output() -> None:
    world = _compile_produced_later_input_world(
        producer_output_requirement="none",
        generated_output_requirement="nonempty",
    )
    exact = reconcile_build(world.inputs)
    producer_witness_id = next(
        witness.witness_id
        for witness in world.evidence_set.output_witnesses
        if witness.producer_unit_id == "compile-producer"
    )
    without_producer_witness = _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        compile_observations=world.evidence_set.compile_observations,
        witnesses=tuple(
            witness
            for witness in world.evidence_set.output_witnesses
            if witness.witness_id != producer_witness_id
        ),
    )
    missing = reconcile_build(_inputs_with(world, evidence_set=without_producer_witness))

    assert exact.status == "complete"
    assert exact.physical_outputs.witnesses_expected == 2
    assert missing.status != "complete"
    assert missing.physical_outputs.witnesses_expected == 2
    assert "compile-consumer" not in {
        proof.compile_unit_id for proof in missing.selected_compile_proofs
    }


def test_same_round_compile_contract_does_not_require_close_time_output_witness() -> None:
    world = _make_world(output_requirements={"compile-main": "none"})
    contract = GeneratedRootContract(
        generated_root_id="same-round-root",
        output_root=OutputRootSpec(
            member_id="same-round-root",
            root_id="same-round-root",
            path="target/generated-sources/annotations",
            verification_mode="exclusive_tree",
            output_requirement="nonempty",
        ),
        producer_unit_kind="compile",
        producer_unit_id="compile-main",
        consumer_unit_ids=("compile-main",),
        mode="same_round_intermediate",
        active_profile_or_variant="default",
        model_provenance="fixture:same-round-no-close-root",
    )
    model = world.model.model_copy(update={"generated_root_contracts": (contract,)})
    expectation = derive_expectation(world.census, world.scope, model)
    final_basis = derive_final_basis(
        world.census,
        expectation,
        model,
        world.physical,
        basis_revision=2,
    )
    evidence_set = _evidence_matching_final_basis(world, final_basis)
    inputs = _inputs_with(
        world,
        build_model=model,
        expectation=expectation,
        final_basis=final_basis,
        evidence_set=evidence_set,
    )

    result = reconcile_build(inputs)

    assert final_basis.conflicts == ()
    assert final_basis.compile_unit_bases[0].output_roots == ()
    assert result.status == "complete"
    assert result.physical_outputs.witnesses_expected == 0


def test_class_count_changes_alone_never_change_source_state() -> None:
    one = _make_world(
        observations=(
            ObservationSpec(
                "compile-main",
                ("src/main/java/example/App.java",),
                class_entries=("example/App.class",),
            ),
        )
    )
    many = _make_world(
        observations=(
            ObservationSpec(
                "compile-main",
                ("src/main/java/example/App.java",),
                class_entries=tuple(f"example/App${index}.class" for index in range(12)),
            ),
        )
    )
    one_result = reconcile_build(one.inputs)
    many_result = reconcile_build(many.inputs)

    assert one_result.status == many_result.status == "complete"
    assert one_result.java_sources == many_result.java_sources
    assert one_result.java_edges == many_result.java_edges
    assert one_result.physical_outputs.observed_class_count == 1
    assert many_result.physical_outputs.observed_class_count == 12


def test_all_public_statuses_emit_the_full_bounded_projection() -> None:
    paths = ("a/src/main/java/a/A.java", "b/src/main/java/b/B.java")
    worlds = {
        "complete": _make_world(),
        "partial": _make_world(
            source_paths=paths,
            assignments={"compile-a": (paths[0],), "compile-b": (paths[1],)},
            observations=(ObservationSpec("compile-a", (paths[0],)),),
        ),
        "absent": _make_world(observations=()),
        "unverifiable": _make_world(record_statuses={"compile_observation": "unavailable"}),
    }

    for expected_status, world in worlds.items():
        result = reconcile_build(world.inputs)
        assert result.status == expected_status
        _assert_full_projection(result)


def test_module_counts_are_computed_from_each_modules_own_closure() -> None:
    world = _two_module_reconciliation_world()

    result = reconcile_build(world.inputs)

    assert result.status == "partial"
    assert result.modules.expected == 2
    assert result.modules.built == 1
    assert result.modules.partial == 1
    assert result.modules.unverifiable == 0


def test_one_unreadable_module_does_not_erase_an_independent_built_module() -> None:
    world = _two_module_reconciliation_world(second_source_unreadable=True)

    result = reconcile_build(world.inputs)

    assert result.status == "unverifiable"
    assert result.modules.expected == 2
    assert result.modules.built == 1
    assert result.modules.partial == 0
    assert result.modules.unverifiable == 1


def _complete_closure_world(**updates: Any) -> World:
    generated_path = "target/generated-sources/proto/Generated.java"
    core = _make_world(
        source_paths=(generated_path,),
        assignments={"compile-main": (generated_path,)},
    )
    return _augment_with_generator_and_package(core, **updates)


def test_exact_generator_compile_package_and_output_closure_is_complete() -> None:
    result = reconcile_build(_complete_closure_world().inputs)

    assert result.status == "complete"
    assert result.generator_units.current == result.generator_units.expected == 1
    assert result.compile_units.coverage_current == result.compile_units.expected == 1
    assert result.package_units.current == result.package_units.expected == 1
    assert result.physical_outputs.witnesses_verified == 3


def test_generator_witness_extra_java_not_in_census_is_unverifiable() -> None:
    world = _complete_closure_world()
    generator_witness = next(
        witness
        for witness in world.evidence_set.output_witnesses
        if witness.producer_observation_kind == "generator"
    )
    generated_root_id = world.model.generated_root_contracts[0].generated_root_id
    surprise = PhysicalOutputEntry(
        root_id=generated_root_id,
        relative_path="Surprise.java",
        kind="other",
        byte_count=17,
        sha256=_sha("surprise-generated-java"),
    )
    changed_root_digest = _sha("generated-root-with-surprise")
    witness_roots = tuple(
        (
            root.model_copy(
                update={
                    "tree_or_owned_entries_sha256": changed_root_digest,
                    "entry_count": root.entry_count + 1,
                }
            )
            if root.root_id == generated_root_id
            else root
        )
        for root in generator_witness.roots
    )
    witness_entries = _sorted_by(
        (*generator_witness.entries, surprise),
        lambda entry: f"{entry.root_id}|{entry.relative_path}",
    )
    witness_payload = generator_witness.model_dump(mode="python")
    witness_payload.update(
        {
            "roots": witness_roots,
            "entries": witness_entries,
            "aggregate_sha256": physical_output_aggregate_sha256(witness_roots, witness_entries),
        }
    )
    changed_witness = PhysicalOutputWitnessV1.model_validate(witness_payload)

    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["current_output_roots"] = _sorted_by(
        (
            (
                root.model_copy(
                    update={
                        "tree_or_owned_entries_sha256": changed_root_digest,
                        "entry_count": root.entry_count + 1,
                    }
                )
                if root.root_id == generated_root_id
                else root
            )
            for root in world.physical.current_output_roots
        ),
        lambda root: root.member_id,
    )
    physical_payload["current_output_entries"] = _sorted_by(
        (
            *world.physical.current_output_entries,
            CurrentOutputEntryV1(
                root_id=surprise.root_id,
                relative_path=surprise.relative_path,
                kind=surprise.kind,
                byte_count=surprise.byte_count,
                sha256=surprise.sha256,
            ),
        ),
        lambda entry: f"{entry.root_id}|{entry.relative_path}",
    )
    physical = _physical_from_payload(physical_payload)
    final_basis = derive_final_basis(
        world.census,
        world.expectation,
        world.model,
        physical,
        basis_revision=2,
    )
    evidence_set = _evidence_matching_final_basis(world, final_basis)
    evidence_set = _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        generator_observations=evidence_set.generator_observations,
        compile_observations=evidence_set.compile_observations,
        package_observations=evidence_set.package_observations,
        witnesses=tuple(
            (
                changed_witness.model_copy(
                    update={
                        "producer_unit_identity_hash": evidence_set.generator_observations[
                            0
                        ].generator_unit_identity_hash
                    }
                )
                if witness.witness_id == changed_witness.witness_id
                else witness
            )
            for witness in evidence_set.output_witnesses
        ),
    )
    result = reconcile_build(
        _inputs_with(
            world,
            physical_basis=physical,
            final_basis=final_basis,
            evidence_set=evidence_set,
        )
    )

    assert result.status == "unverifiable"
    assert "compile-main" not in {proof.compile_unit_id for proof in result.selected_compile_proofs}


@pytest.mark.parametrize("producer_kind", ("generator", "compile", "package"))
def test_nested_observation_conflict_is_unverifiable_and_never_selected(
    producer_kind: str,
) -> None:
    world = _complete_closure_world()
    generators = world.evidence_set.generator_observations
    compiles = world.evidence_set.compile_observations
    packages = world.evidence_set.package_observations
    if producer_kind == "generator":
        generators = (generators[0].model_copy(update={"conflicts": ("producer-conflict",)}),)
    elif producer_kind == "compile":
        compiles = (compiles[0].model_copy(update={"conflicts": ("producer-conflict",)}),)
    else:
        packages = (packages[0].model_copy(update={"conflicts": ("producer-conflict",)}),)
    evidence_set = _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        generator_observations=generators,
        compile_observations=compiles,
        package_observations=packages,
        witnesses=world.evidence_set.output_witnesses,
    )

    result = reconcile_build(_inputs_with(world, evidence_set=evidence_set))

    selected = {
        "generator": result.selected_generator_proofs,
        "compile": result.selected_compile_proofs,
        "package": result.selected_package_proofs,
    }[producer_kind]
    assert result.status == "unverifiable"
    assert selected == ()
    assert result.conflicts


@pytest.mark.parametrize("producer_kind", ("generator", "package"))
def test_complete_known_empty_generator_or_package_witness_is_known_not_current(
    producer_kind: str,
) -> None:
    world = _complete_closure_world()
    evidence_set = _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        generator_observations=world.evidence_set.generator_observations,
        compile_observations=world.evidence_set.compile_observations,
        package_observations=world.evidence_set.package_observations,
        witnesses=tuple(
            witness
            for witness in world.evidence_set.output_witnesses
            if witness.producer_observation_kind != producer_kind
        ),
    )

    result = reconcile_build(_with_evidence_set(world, evidence_set).inputs)

    assert result.status != "complete"
    if producer_kind == "generator":
        assert result.generator_units.current == 0
        assert result.generator_units.missing_or_invalid == 1
    else:
        assert result.package_units.current == 0
        assert result.package_units.missing_or_invalid == 1


@pytest.mark.parametrize(
    ("producer_kind", "root_id"),
    (("generator", "generated-root-main"), ("package", "package-root-main")),
)
def test_exclusive_generator_or_package_root_digest_mismatch_is_not_current(
    producer_kind: str,
    root_id: str,
) -> None:
    world = _complete_closure_world()
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["current_output_roots"] = _sorted_by(
        (
            (
                root.model_copy(update={"tree_or_owned_entries_sha256": _sha(f"changed:{root_id}")})
                if root.root_id == root_id
                else root
            )
            for root in world.physical.current_output_roots
        ),
        lambda root: root.member_id,
    )
    physical = _physical_from_payload(physical_payload)

    result = reconcile_build(_inputs_with_rederived_basis(world, physical))

    assert result.status != "complete"
    if producer_kind == "generator":
        assert result.generator_units.current == 0
    else:
        assert result.package_units.current == 0


def test_package_witness_with_only_other_artifact_does_not_satisfy_requested_artifact() -> None:
    world = _complete_closure_world()
    original_observation = world.evidence_set.package_observations[0]
    observation_payload = original_observation.model_dump(mode="python")
    observation_payload.update(
        {
            "run_id": "run-package-decoy",
            "receipt_id": "receipt-package-decoy",
            "contract_id": "contract-package-decoy",
            "observed_artifact_paths": ("target/package/other.jar",),
        }
    )
    observation_payload["observation_id"] = canonical_content_id(
        "package-observation-v1",
        {
            "evidence_epoch_id": world.epoch.evidence_epoch_id,
            "run_id": observation_payload["run_id"],
            "receipt_id": observation_payload["receipt_id"],
            "contract_id": observation_payload["contract_id"],
            "package_unit_id": original_observation.package_unit_id,
        },
    )
    decoy_observation = PackageUnitObservationV1.model_validate(observation_payload)
    original_witness = next(
        witness
        for witness in world.evidence_set.output_witnesses
        if witness.producer_observation_kind == "package"
    )
    other_entry = original_witness.entries[0].model_copy(
        update={"relative_path": "other.jar", "sha256": _sha("other-jar")}
    )
    other_root = original_witness.roots[0].model_copy(
        update={"tree_or_owned_entries_sha256": _sha("other-jar-root")}
    )
    witness_payload = original_witness.model_dump(mode="python")
    witness_payload.update(
        {
            "witness_id": canonical_content_id(
                "physical-output-witness-v1",
                {
                    "evidence_epoch_id": world.epoch.evidence_epoch_id,
                    "producer_observation_kind": "package",
                    "producer_observation_id": decoy_observation.observation_id,
                },
            ),
            "producer_observation_id": decoy_observation.observation_id,
            "roots": (other_root,),
            "entries": (other_entry,),
            "aggregate_sha256": physical_output_aggregate_sha256((other_root,), (other_entry,)),
        }
    )
    other_witness = PhysicalOutputWitnessV1.model_validate(witness_payload)
    witnesses = tuple(
        other_witness if witness.witness_id == original_witness.witness_id else witness
        for witness in world.evidence_set.output_witnesses
    )
    evidence_set = _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        generator_observations=world.evidence_set.generator_observations,
        compile_observations=world.evidence_set.compile_observations,
        package_observations=(decoy_observation,),
        witnesses=witnesses,
    )
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["current_output_roots"] = _sorted_by(
        (
            (
                root.model_copy(
                    update={"tree_or_owned_entries_sha256": other_root.tree_or_owned_entries_sha256}
                )
                if root.root_id == other_root.root_id
                else root
            )
            for root in world.physical.current_output_roots
        ),
        lambda root: root.member_id,
    )
    physical_payload["current_output_entries"] = _sorted_by(
        (
            (
                CurrentOutputEntryV1(
                    root_id=other_entry.root_id,
                    relative_path=other_entry.relative_path,
                    kind=other_entry.kind,
                    byte_count=other_entry.byte_count,
                    sha256=other_entry.sha256,
                )
                if entry.root_id == other_entry.root_id
                else entry
            )
            for entry in world.physical.current_output_entries
        ),
        lambda entry: f"{entry.root_id}|{entry.relative_path}",
    )
    physical = _physical_from_payload(physical_payload)
    final_payload = world.final_basis.model_dump(mode="python")
    final_payload.update(
        {
            "basis_revision": 2,
            "physical_basis_snapshot_id": physical.physical_basis_snapshot_id,
        }
    )
    final_basis = FinalBuildBasisV1.model_validate(final_payload)

    result = reconcile_build(
        _inputs_with(
            world,
            evidence_set=evidence_set,
            physical_basis=physical,
            final_basis=final_basis,
        )
    )

    assert decoy_observation.observed_artifact_paths == ("target/package/other.jar",)
    assert (
        decoy_observation.requested_artifacts
        == final_basis.package_unit_bases[0].requested_artifacts
    )
    assert other_witness.producer_observation_id == decoy_observation.observation_id
    assert other_witness.producer_unit_identity_hash == decoy_observation.package_unit_identity_hash
    assert final_basis.package_unit_bases[0].requested_artifacts[0].path.endswith("app.jar")
    assert result.status != "complete"
    assert result.generator_units.current == 1
    assert result.compile_units.coverage_current == 1
    assert result.package_units.current == 0


def test_multiple_requested_artifacts_match_witness_entries_by_path_not_tuple_position() -> None:
    world = _complete_closure_world()
    package_unit = world.model.package_units[0]
    root = package_unit.output_roots[0]
    artifacts = (
        RequestedArtifactSpec(
            artifact_id="artifact-a-sorts-first",
            root_id=root.root_id,
            path=f"{root.path}/z-last-path.jar",
            kind="jar",
        ),
        RequestedArtifactSpec(
            artifact_id="artifact-z-sorts-last",
            root_id=root.root_id,
            path=f"{root.path}/a-first-path.jar",
            kind="jar",
        ),
    )
    package_unit = package_unit.model_copy(update={"requested_artifacts": artifacts})
    model = world.model.model_copy(update={"package_units": (package_unit,)})
    package_root_digest = _sha("two-artifact-root")
    physical_entries = (
        CurrentOutputEntryV1(
            root_id=root.root_id,
            relative_path="a-first-path.jar",
            kind="jar",
            byte_count=41,
            sha256=_sha("a-first-path.jar"),
        ),
        CurrentOutputEntryV1(
            root_id=root.root_id,
            relative_path="z-last-path.jar",
            kind="jar",
            byte_count=43,
            sha256=_sha("z-last-path.jar"),
        ),
    )
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["current_output_roots"] = _sorted_by(
        (
            (
                item.model_copy(
                    update={
                        "tree_or_owned_entries_sha256": package_root_digest,
                        "entry_count": 2,
                    }
                )
                if item.root_id == root.root_id
                else item
            )
            for item in world.physical.current_output_roots
        ),
        lambda item: item.member_id,
    )
    physical_payload["current_output_entries"] = _sorted_by(
        (
            *(
                item
                for item in world.physical.current_output_entries
                if item.root_id != root.root_id
            ),
            *physical_entries,
        ),
        lambda item: f"{item.root_id}|{item.relative_path}",
    )
    physical = _physical_from_payload(physical_payload)
    package_basis_payload = world.final_basis.package_unit_bases[0].model_dump(mode="python")
    package_basis_payload["requested_artifacts"] = artifacts
    package_basis_identity = {
        field: value
        for field, value in package_basis_payload.items()
        if field != "package_unit_identity_hash"
    }
    package_basis_payload["package_unit_identity_hash"] = package_unit_identity_hash(
        package_basis_identity
    )
    package_basis = FinalPackageUnitBasis.model_validate(package_basis_payload)
    final_basis_payload = world.final_basis.model_dump(mode="python")
    final_basis_payload.update(
        {
            "basis_revision": 2,
            "physical_basis_snapshot_id": physical.physical_basis_snapshot_id,
            "package_unit_bases": (package_basis,),
        }
    )
    final_basis = FinalBuildBasisV1.model_validate(final_basis_payload)
    original_observation = world.evidence_set.package_observations[0]
    observation_payload = original_observation.model_dump(mode="python")
    for field in FinalPackageUnitBasis.model_fields:
        observation_payload[field] = getattr(package_basis, field)
    observation_payload["observed_artifact_paths"] = tuple(
        sorted(artifact.path for artifact in artifacts)
    )
    package_observation = PackageUnitObservationV1.model_validate(observation_payload)
    witness_entries = tuple(
        PhysicalOutputEntry(
            root_id=entry.root_id,
            relative_path=entry.relative_path,
            kind=entry.kind,
            byte_count=entry.byte_count,
            sha256=entry.sha256,
        )
        for entry in physical_entries
    )
    witness_root = OutputRootWitness(
        root_id=root.root_id,
        path=root.path,
        verification_mode=root.verification_mode,
        tree_or_owned_entries_sha256=package_root_digest,
        entry_count=2,
    )
    original_witness = next(
        witness
        for witness in world.evidence_set.output_witnesses
        if witness.producer_observation_kind == "package"
    )
    witness_payload = original_witness.model_dump(mode="python")
    witness_payload.update(
        {
            "producer_unit_identity_hash": package_observation.package_unit_identity_hash,
            "roots": (witness_root,),
            "entries": witness_entries,
            "aggregate_sha256": physical_output_aggregate_sha256((witness_root,), witness_entries),
        }
    )
    package_witness = PhysicalOutputWitnessV1.model_validate(witness_payload)
    evidence_set = _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        generator_observations=world.evidence_set.generator_observations,
        compile_observations=world.evidence_set.compile_observations,
        package_observations=(package_observation,),
        witnesses=tuple(
            package_witness if witness.producer_observation_kind == "package" else witness
            for witness in world.evidence_set.output_witnesses
        ),
    )
    inputs = _inputs_with(
        world,
        build_model=model,
        final_basis=final_basis,
        physical_basis=physical,
        evidence_set=evidence_set,
    )

    result = reconcile_build(inputs)

    assert tuple(artifact.path for artifact in artifacts) != tuple(
        f"{root.path}/{entry.relative_path}" for entry in witness_entries
    )
    assert result.status == "complete"
    assert result.package_units.current == 1


@pytest.mark.parametrize("unit_kind", ("compile", "generator"))
def test_recomputed_observation_and_witness_identity_cannot_hide_argument_drift(
    unit_kind: str,
) -> None:
    world = _complete_closure_world()
    if unit_kind == "compile":
        original = world.evidence_set.compile_observations[0]
        observation_payload = original.model_dump(mode="python")
        observation_payload["compiler_args_fingerprint"] = _sha("changed-compiler-args")
        identity_fields = {
            "compile_unit_id": original.compile_unit_id,
            "build_config_fingerprint": original.build_config_fingerprint,
            "required_source_entries": original.source_entries,
            "source_set_fingerprint": original.source_set_fingerprint,
            "compiler_executable_path": original.compiler_executable_path,
            "compiler_executable_sha256": original.compiler_executable_sha256,
            "compiler_version": original.compiler_version,
            "compiler_args_fingerprint": observation_payload["compiler_args_fingerprint"],
            "classpath_entries": original.classpath_entries,
            "dependency_unit_identity_hashes": original.dependency_unit_identity_hashes,
            "toolchain_fingerprint": original.toolchain_fingerprint,
            "output_roots": original.observed_output_roots,
        }
        observation_payload["compile_unit_identity_hash"] = compile_unit_identity_hash(
            identity_fields
        )
        changed_observation = CompileUnitObservationV1.model_validate(observation_payload)
        compile_observations = (changed_observation,)
        generator_observations = world.evidence_set.generator_observations
        old_identity = original.compile_unit_identity_hash
        new_identity = changed_observation.compile_unit_identity_hash
    else:
        original = world.evidence_set.generator_observations[0]
        observation_payload = original.model_dump(mode="python")
        observation_payload["generator_args_fingerprint"] = _sha("changed-generator-args")
        identity_fields = {
            key: value
            for key, value in observation_payload.items()
            if key
            in {
                "generator_unit_id",
                "build_config_fingerprint",
                "input_entries",
                "generator_executable_path",
                "generator_executable_sha256",
                "generator_tool_entries",
                "generator_dependency_entries",
                "generator_args_fingerprint",
                "generator_toolchain_fingerprint",
                "generated_output_roots",
            }
        }
        observation_payload["generator_unit_identity_hash"] = generator_unit_identity_hash(
            identity_fields
        )
        changed_observation = GeneratorUnitObservationV1.model_validate(observation_payload)
        compile_observations = world.evidence_set.compile_observations
        generator_observations = (changed_observation,)
        old_identity = original.generator_unit_identity_hash
        new_identity = changed_observation.generator_unit_identity_hash
    changed_witnesses = tuple(
        (
            witness.model_copy(update={"producer_unit_identity_hash": new_identity})
            if witness.producer_observation_id == original.observation_id
            else witness
        )
        for witness in world.evidence_set.output_witnesses
    )
    evidence_set = _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        generator_observations=generator_observations,
        compile_observations=compile_observations,
        package_observations=world.evidence_set.package_observations,
        witnesses=changed_witnesses,
    )

    assert new_identity != old_identity
    result = reconcile_build(_with_evidence_set(world, evidence_set).inputs)
    assert result.status != "complete"
    if unit_kind == "compile":
        assert result.compile_units.coverage_current == 0
    else:
        assert result.generator_units.current == 0


@pytest.mark.parametrize(
    ("generator_identity_current", "generated_bytes_match_consumer", "expected_current"),
    ((False, True, 0), (True, False, 1)),
)
def test_generator_requires_exact_basis_witness_and_consumer_byte_conservation(
    generator_identity_current: bool,
    generated_bytes_match_consumer: bool,
    expected_current: int,
) -> None:
    world = _complete_closure_world(
        generator_identity_current=generator_identity_current,
        generated_bytes_match_consumer=generated_bytes_match_consumer,
    )

    result = reconcile_build(world.inputs)

    assert result.status != "complete"
    assert result.generator_units.current == expected_current
    if not generated_bytes_match_consumer:
        assert result.status == "unverifiable"
        assert result.compile_units.coverage_unverifiable == 1


def test_generator_witness_must_cover_every_expected_generated_consumer_source() -> None:
    generated_paths = (
        "target/generated-sources/proto/First.java",
        "target/generated-sources/proto/Second.java",
    )
    core = _make_world(
        source_paths=generated_paths,
        assignments={"compile-main": generated_paths},
    )
    world = _augment_with_generator_and_package(core)
    generator_witness = next(
        witness
        for witness in world.evidence_set.output_witnesses
        if witness.producer_observation_kind == "generator"
    )
    generated_root = next(
        root
        for root in world.physical.current_output_roots
        if root.root_id == "generated-root-main"
    )

    assert len(world.final_basis.compile_unit_bases[0].required_source_entries) == 2
    assert len(world.evidence_set.compile_observations[0].source_entries) == 2
    assert len(generator_witness.entries) == 1
    assert generated_root.entry_count == 1
    result = reconcile_build(world.inputs)
    assert result.status == "unverifiable"
    assert result.compile_units.coverage_unverifiable == 1


@pytest.mark.parametrize(
    "package_change",
    (
        "compile_identity",
        "compile_output",
        "resource",
        "executable",
        "tool",
        "external_dependency",
        "arguments",
        "toolchain",
        "config",
        "artifact",
    ),
)
def test_package_proof_is_stale_when_any_completion_input_changes(
    package_change: str,
) -> None:
    world = _complete_closure_world(package_change=package_change)

    result = reconcile_build(world.inputs)

    assert result.status == "partial"
    assert result.compile_units.coverage_current == 1
    assert result.package_units.current == 0
    assert result.package_units.missing_or_invalid == 1


def _child_world(**updates: Any) -> World:
    parent = _make_world(
        source_paths=("child/src/main/java/example/Child.java",),
        assignments={},
        observations=(),
    )
    return _augment_with_child(parent, **updates)


def test_delegated_source_is_conserved_by_one_child_path_and_hash_edge() -> None:
    world = _child_world()

    result = reconcile_build(world.inputs)

    child = world.evidence_set.child_reconciliations[0]
    selected = result.selected_child_reconciliations[0]
    assert result.status == "complete"
    assert result.source_census.delegated == 1
    assert (result.java_sources.delegated_current, result.java_sources.delegated_expected) == (
        1,
        1,
    )
    assert selected == SelectedChildReconciliation(
        child_scope_id="child-scope-main",
        child_census_id=world.evidence_set.child_censuses[0].census_id,
        child_expectation_id=world.evidence_set.child_expectations[0].expectation_id,
        reconciliation_id=child.reconciliation_id,
    )


@pytest.mark.parametrize("fault", ("missing", "hash"))
def test_delegated_parent_source_still_requires_current_physical_bytes(fault: str) -> None:
    world = _child_world()
    source = next(
        member for member in world.physical.current_members if member.member_role == "source"
    )
    physical_payload = world.physical.model_dump(mode="python")
    remaining = tuple(
        member for member in world.physical.current_members if member.member_id != source.member_id
    )
    if fault == "missing":
        physical_payload["current_members"] = remaining
        physical_payload["missing_members"] = (
            MissingPhysicalMemberV1(member_id=source.member_id, path=source.path),
        )
    else:
        physical_payload["current_members"] = _sorted_by(
            (*remaining, source.model_copy(update={"sha256": _sha("delegated-drift")})),
            lambda item: item.member_id,
        )
    physical = _physical_from_payload(physical_payload)
    final_basis = derive_final_basis(
        world.census,
        world.expectation,
        world.model,
        physical,
        basis_revision=2,
    )

    result = reconcile_build(_inputs_with(world, physical_basis=physical, final_basis=final_basis))

    assert result.status == "unverifiable"
    assert result.java_sources.delegated_current == 0


def test_partial_child_contributes_its_exact_recursive_java_fraction_without_selection() -> None:
    result = reconcile_build(_child_world(child_status="partial").inputs)

    assert result.status == "partial"
    assert result.child_scopes.complete == 0
    assert result.child_scopes.incomplete_or_unverifiable == 1
    assert result.selected_child_reconciliations == ()
    assert (result.java_sources.delegated_current, result.java_sources.delegated_expected) == (
        1,
        1,
    )


def test_required_module_owning_a_partial_child_scope_is_not_built() -> None:
    world = _child_world(child_status="partial")
    child_module = EvaluatedModuleV1(
        module_id="module-child",
        domain_id="domain-child",
        build_system="maven",
        module_coordinate="com.example:child",
        project_path="child",
        role="required",
        model_provenance="fixture:child-module-ownership",
    )
    model = world.model.model_copy(update={"modules": (child_module,)})

    result = reconcile_build(_inputs_with(world, build_model=model))

    assert result.status == "partial"
    assert result.child_scopes.complete == 0
    assert result.child_scopes.incomplete_or_unverifiable == 1
    assert result.modules.expected == 1
    assert result.modules.built == 0
    assert result.modules.partial == 1
    assert result.modules.unverifiable == 0


def test_child_self_reported_complete_zero_projection_cannot_override_authorized_expectation() -> (
    None
):
    world = _child_world()
    child = world.evidence_set.child_reconciliations[0]
    payload = child.model_dump(mode="python")
    payload.update(
        {
            "source_census": {
                "total_candidates": 0,
                "classified": 0,
                "unclassified": 0,
                "delegated": 0,
                "out_of_scope_by_reason": {},
            },
            "java_sources": {
                "local_expected": 0,
                "local_current": 0,
                "delegated_expected": 0,
                "delegated_current": 0,
                "project_expected": 0,
                "project_current": 0,
                "project_missing": 0,
                "project_stale": 0,
            },
            "java_edges": {"expected": 0, "current": 0, "missing": 0, "stale": 0},
            "compile_units": {
                "expected": 0,
                "coverage_current": 0,
                "coverage_missing_or_invalid": 0,
                "coverage_unverifiable": 0,
            },
            "physical_outputs": {
                "observed_class_count": 0,
                "witnesses_expected": 0,
                "witnesses_verified": 0,
                "sealed_entries": 0,
                "verified_entries": 0,
                "missing_entries": 0,
                "mismatched_entries": 0,
            },
            "active_language_units": {
                "expected": 0,
                "supported_current": 0,
                "unsupported_or_unverifiable": 0,
            },
            "modules": {"expected": 0, "built": 0, "partial": 0, "unverifiable": 0},
            "selected_compile_proofs": (),
            "evidence_refs": (),
        }
    )
    zero_claim = BuildReconciliationV1.model_validate(payload)
    evidence_set = _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        child_censuses=world.evidence_set.child_censuses,
        child_expectations=world.evidence_set.child_expectations,
        child_reconciliations=(zero_claim,),
    )

    result = reconcile_build(_with_evidence_set(world, evidence_set).inputs)

    assert result.status == "unverifiable"
    assert result.selected_child_reconciliations == ()
    assert result.child_scopes.incomplete_or_unverifiable == 1


@pytest.mark.parametrize("masked_axis", ("source_census", "compile"))
def test_child_cannot_mask_one_authorized_axis_with_a_self_consistent_zero_projection(
    masked_axis: str,
) -> None:
    world = _child_world()
    payload = world.evidence_set.child_reconciliations[0].model_dump(mode="python")
    if masked_axis == "source_census":
        payload["source_census"] = {
            "total_candidates": 0,
            "classified": 0,
            "unclassified": 0,
            "delegated": 0,
            "out_of_scope_by_reason": {},
        }
    else:
        payload.update(
            {
                "compile_units": {
                    "expected": 0,
                    "coverage_current": 0,
                    "coverage_missing_or_invalid": 0,
                    "coverage_unverifiable": 0,
                },
                "physical_outputs": {
                    "observed_class_count": 0,
                    "witnesses_expected": 0,
                    "witnesses_verified": 0,
                    "sealed_entries": 0,
                    "verified_entries": 0,
                    "missing_entries": 0,
                    "mismatched_entries": 0,
                },
                "selected_compile_proofs": (),
                "evidence_refs": (),
            }
        )
    masked = BuildReconciliationV1.model_validate(payload)
    evidence_set = _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        child_censuses=world.evidence_set.child_censuses,
        child_expectations=world.evidence_set.child_expectations,
        child_reconciliations=(masked,),
    )

    result = reconcile_build(_with_evidence_set(world, evidence_set).inputs)

    assert result.status == "unverifiable"
    assert result.selected_child_reconciliations == ()
    assert result.child_scopes.incomplete_or_unverifiable == 1


@pytest.mark.parametrize(
    "updates",
    (
        {"child_has_source": False},
        {"handoff_path": "src/main/java/example/Other.java"},
        {"handoff_sha256": _sha("other-child-bytes")},
        {"child_requires_parent": True},
    ),
    ids=("missing-source", "path-mismatch", "hash-mismatch", "cycle"),
)
def test_invalid_child_handoff_or_dag_is_unverifiable(updates: Mapping[str, Any]) -> None:
    result = reconcile_build(_child_world(**updates).inputs)

    assert result.status == "unverifiable"
    assert result.child_scopes.incomplete_or_unverifiable == 1
    assert result.java_sources.delegated_current == 0


def test_transitive_child_closure_rejects_one_child_with_multiple_parents() -> None:
    world = _multiple_parent_child_graph_world()

    assert len(world.evidence_set.child_reconciliations) == 3
    result = reconcile_build(world.inputs)

    assert result.status == "unverifiable"
    assert result.child_scopes.incomplete_or_unverifiable == 2


def test_nested_selected_partial_reconciliation_cannot_satisfy_a_complete_child_claim() -> None:
    graph = _multiple_parent_child_graph_world()
    child_censuses = {
        census.project_root.rsplit("/", 1)[-1]: census
        for census in graph.evidence_set.child_censuses
    }
    child_expectations = {
        expectation.scope_id: expectation for expectation in graph.evidence_set.child_expectations
    }
    child_reconciliations = {
        reconciliation.goal_scope_id: reconciliation
        for reconciliation in graph.evidence_set.child_reconciliations
    }
    left_scope_id = "child-left"
    nested_scope_id = "child-shared"
    nested_payload = child_reconciliations[nested_scope_id].model_dump(mode="python")
    nested_payload["status"] = "partial"
    nested = BuildReconciliationV1.model_validate(nested_payload)
    left = child_reconciliations[left_scope_id]
    model = graph.model.model_copy(
        update={
            "child_build_scopes": tuple(
                child
                for child in graph.model.child_build_scopes
                if child.child_scope_id == left_scope_id
            )
        }
    )
    expectation_payload = graph.expectation.model_dump(mode="python")
    expectation_payload["required_child_scope_ids"] = (left_scope_id,)
    expectation = _expectation_from_payload(expectation_payload)
    physical_payload = graph.physical.model_dump(mode="python")
    physical_payload["expectation_id"] = expectation.expectation_id
    physical = _physical_from_payload(physical_payload)
    final_basis = FinalBuildBasisV1(
        basis_id=canonical_content_id(
            "final-build-basis-v1",
            {
                "evidence_epoch_id": graph.epoch.evidence_epoch_id,
                "expectation_id": expectation.expectation_id,
            },
        ),
        evidence_epoch_id=graph.epoch.evidence_epoch_id,
        basis_revision=2,
        expectation_id=expectation.expectation_id,
        physical_basis_snapshot_id=physical.physical_basis_snapshot_id,
        source_state_fingerprint=graph.census.source_state_fingerprint,
        build_config_fingerprint=model.build_config_fingerprint,
        generator_unit_bases=(),
        compile_unit_bases=(),
        package_unit_bases=(),
        conflicts=(),
    )
    evidence_set = _evidence_set(
        epoch_id=graph.epoch.evidence_epoch_id,
        child_censuses=(
            next(
                census
                for census in child_censuses.values()
                if census.census_id == child_expectations[left_scope_id].census_id
            ),
            next(
                census
                for census in child_censuses.values()
                if census.census_id == child_expectations[nested_scope_id].census_id
            ),
        ),
        child_expectations=(
            child_expectations[left_scope_id],
            child_expectations[nested_scope_id],
        ),
        child_reconciliations=(left, nested),
    )
    inputs = ReconciliationInputs(
        epoch=graph.epoch,
        goal_scope=graph.scope,
        census=graph.census,
        build_model=model,
        expectation=expectation,
        final_basis=final_basis,
        physical_basis=physical,
        evidence_set=evidence_set,
    )

    result = reconcile_build(inputs)

    assert result.status == "unverifiable"
    assert result.selected_child_reconciliations == ()
    assert result.child_scopes.incomplete_or_unverifiable == 1


@pytest.mark.parametrize("record_status", ("unavailable", "set_mismatch", "tombstoned", "conflict"))
def test_unreadable_or_incomplete_child_authority_is_unverifiable(
    record_status: str,
) -> None:
    result = reconcile_build(_child_world(child_record_status=record_status).inputs)

    assert result.status == "unverifiable"
    assert result.child_scopes.incomplete_or_unverifiable == 1


def test_child_status_projection_without_census_and_expectation_is_rejected_at_boundary() -> None:
    world = _child_world()
    payload = world.evidence_set.model_dump(mode="python")
    payload["child_censuses"] = ()
    payload["child_expectations"] = ()

    with pytest.raises(ValidationError, match="child|cross-link|bijective|observed"):
        BuildEvidenceSetSnapshotV1.model_validate(payload)


def test_duplicate_handoff_and_overlapping_child_roots_are_rejected_at_typed_boundary() -> None:
    world = _child_world()
    delegation = world.expectation.delegation_edges[0]
    expectation_payload = world.expectation.model_dump(mode="python")
    expectation_payload["delegation_edges"] = (delegation, delegation)
    with pytest.raises(ValidationError, match="delegation|duplicate"):
        JvmBuildExpectationV1.model_validate(expectation_payload)

    child = world.model.child_build_scopes[0]
    model_payload = world.model.model_dump(mode="python")
    model_payload["child_build_scopes"] = (
        child,
        child.model_copy(
            update={
                "child_scope_id": "child-scope-nested",
                "child_project_root": f"{child.child_project_root}/nested",
            }
        ),
    )
    with pytest.raises(ValidationError, match="overlap|child"):
        EvaluatedBuildModelV1.model_validate(model_payload)


def test_two_selected_shared_root_witnesses_cannot_claim_one_absolute_entry() -> None:
    paths = ("a/src/main/java/a/A.java", "b/src/main/java/b/B.java")
    world = _make_world(
        source_paths=paths,
        assignments={"compile-a": (paths[0],), "compile-b": (paths[1],)},
        output_paths={
            "compile-a": "target/shared-classes",
            "compile-b": "target/shared-classes",
        },
        observations=(
            ObservationSpec("compile-a", (paths[0],), class_entries=("same/Foo.class",)),
            ObservationSpec("compile-b", (paths[1],), class_entries=("same/Foo.class",)),
        ),
    )

    result = reconcile_build(world.inputs)

    assert result.status == "unverifiable"
    assert result.physical_outputs.observed_class_count == 1
    assert result.modules.unverifiable == 1


def test_shared_witness_absolute_entry_ownership_conflicts_even_when_bytes_are_identical() -> None:
    paths = ("a/src/main/java/a/A.java", "b/src/main/java/b/B.java")
    world = _make_world(
        source_paths=paths,
        assignments={"compile-a": (paths[0],), "compile-b": (paths[1],)},
        output_paths={
            "compile-a": "target/shared-classes",
            "compile-b": "target/shared-classes",
        },
        observations=(
            ObservationSpec("compile-a", (paths[0],), class_entries=("same/Foo.class",)),
            ObservationSpec("compile-b", (paths[1],), class_entries=("same/Foo.class",)),
        ),
    )
    witnesses_by_unit = {
        witness.producer_unit_id: witness for witness in world.evidence_set.output_witnesses
    }
    first_entry = witnesses_by_unit["compile-a"].entries[0]
    second = witnesses_by_unit["compile-b"]
    identical_entry = second.entries[0].model_copy(
        update={"byte_count": first_entry.byte_count, "sha256": first_entry.sha256}
    )
    second_payload = second.model_dump(mode="python")
    second_payload.update(
        {
            "entries": (identical_entry,),
            "aggregate_sha256": physical_output_aggregate_sha256(second.roots, (identical_entry,)),
        }
    )
    identical_second = PhysicalOutputWitnessV1.model_validate(second_payload)
    evidence_set = _evidence_set(
        epoch_id=world.epoch.evidence_epoch_id,
        compile_observations=world.evidence_set.compile_observations,
        witnesses=tuple(
            identical_second if witness.witness_id == second.witness_id else witness
            for witness in world.evidence_set.output_witnesses
        ),
    )
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["current_output_entries"] = _sorted_by(
        (
            (
                entry.model_copy(
                    update={"byte_count": first_entry.byte_count, "sha256": first_entry.sha256}
                )
                if entry.root_id == identical_entry.root_id
                else entry
            )
            for entry in world.physical.current_output_entries
        ),
        lambda entry: f"{entry.root_id}|{entry.relative_path}",
    )
    physical = _physical_from_payload(physical_payload)
    final_payload = world.final_basis.model_dump(mode="python")
    final_payload["physical_basis_snapshot_id"] = physical.physical_basis_snapshot_id
    final_basis = FinalBuildBasisV1.model_validate(final_payload)

    result = reconcile_build(
        _inputs_with(
            world,
            evidence_set=evidence_set,
            physical_basis=physical,
            final_basis=final_basis,
        )
    )

    assert result.status == "unverifiable"
    assert result.physical_outputs.observed_class_count == 1
    assert result.modules.unverifiable == 1


def test_outer_shared_root_and_nested_exclusive_root_are_legal_when_entries_disjoint() -> None:
    paths = ("a/src/main/java/a/A.java", "b/src/main/java/b/B.java")
    world = _make_world(
        source_paths=paths,
        assignments={"compile-a": (paths[0],), "compile-b": (paths[1],)},
        output_paths={
            "compile-a": "target/classes",
            "compile-b": "target/classes/generated",
        },
        verification_modes={
            "compile-a": "shared_owned_entries",
            "compile-b": "exclusive_tree",
        },
        observations=(
            ObservationSpec("compile-a", (paths[0],), class_entries=("a/A.class",)),
            ObservationSpec("compile-b", (paths[1],), class_entries=("b/B.class",)),
        ),
    )

    result = reconcile_build(world.inputs)

    assert result.status == "complete"
    assert result.physical_outputs.observed_class_count == 2
    assert result.physical_outputs.witnesses_verified == 2


def test_nested_roots_expand_entries_before_detecting_cross_witness_ownership_conflict() -> None:
    paths = ("a/src/main/java/a/A.java", "b/src/main/java/b/B.java")
    world = _make_world(
        source_paths=paths,
        assignments={"compile-outer": (paths[0],), "compile-inner": (paths[1],)},
        output_paths={
            "compile-outer": "target/classes",
            "compile-inner": "target/classes/generated",
        },
        verification_modes={
            "compile-outer": "shared_owned_entries",
            "compile-inner": "exclusive_tree",
        },
        observations=(
            ObservationSpec(
                "compile-outer",
                (paths[0],),
                class_entries=("generated/Foo.class",),
            ),
            ObservationSpec(
                "compile-inner",
                (paths[1],),
                class_entries=("Foo.class",),
            ),
        ),
    )

    result = reconcile_build(world.inputs)

    assert result.status == "unverifiable"
    assert result.compile_units.coverage_current == 2
    assert result.physical_outputs.observed_class_count == 1
    assert result.modules.unverifiable == 1


def test_derive_expectation_is_observation_free_and_reproduces_empty_scope() -> None:
    world = _make_world(source_paths=(), assignments={}, observations=())

    assert tuple(inspect.signature(derive_expectation).parameters) == (
        "census",
        "goal_scope",
        "build_model",
    )
    assert derive_expectation(world.census, world.scope, world.model) == world.expectation
    forbidden = {
        "receipt",
        "observation",
        "witness",
        "class",
        "artifact",
        "task_outcome",
    }
    assert not forbidden & set(inspect.signature(derive_expectation).parameters)


def test_derive_expectation_maps_a_nonempty_required_source_and_compile_unit() -> None:
    world = _make_world()

    derived = derive_expectation(world.census, world.scope, world.model)

    assert derived == world.expectation
    source = world.census.sources[0]
    assert derived.required_compile_unit_ids == ("compile-main",)
    assert derived.required_java_edges == (
        RequiredSourceEdge(source_id=source.source_id, compile_unit_id="compile-main"),
    )
    assert derived.source_classifications == (
        SourceClassification(
            source_id=source.source_id,
            language="java",
            source_kind="main",
            classification="required",
            classification_basis=ClassificationBasisV1(
                basis_kind="model_rule",
                model_id=world.model.model_id,
                basis_ref=world.model.compile_units[0].include_rules[0].rule_id,
            ),
            expected_compile_unit_ids=("compile-main",),
            child_scope_id=None,
        ),
    )
    assert derived.unclassified_source_ids == ()


def test_derive_expectation_keeps_unmatched_java_source_unclassified() -> None:
    world = _make_world()
    compile_unit = world.model.compile_units[0].model_copy(
        update={"include_rules": (_source_rule("compile-main", "other/**/*.java"),)}
    )
    model = world.model.model_copy(update={"compile_units": (compile_unit,)})

    derived = derive_expectation(world.census, world.scope, model)

    assert derived.required_compile_unit_ids == ("compile-main",)
    assert derived.required_java_edges == ()
    assert derived.source_classifications == ()
    assert derived.unclassified_source_ids == (world.census.sources[0].source_id,)


@pytest.mark.parametrize("symlink_status", ("contained", "symlink_contained"))
def test_safe_source_symlink_status_keeps_exact_build_complete(symlink_status: str) -> None:
    world = _make_world()
    source = world.census.sources[0].model_copy(update={"symlink_status": symlink_status})
    census = world.census.model_copy(update={"sources": (source,)})
    expectation = derive_expectation(census, world.scope, world.model)
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["expectation_id"] = expectation.expectation_id
    physical = _physical_from_payload(physical_payload)
    final_basis = derive_final_basis(
        census,
        expectation,
        world.model,
        physical,
        basis_revision=2,
    )
    evidence_set = _evidence_matching_final_basis(world, final_basis)
    inputs = _inputs_with(
        world,
        census=census,
        expectation=expectation,
        physical_basis=physical,
        final_basis=final_basis,
        evidence_set=evidence_set,
    )

    assert expectation.conflicts == ()
    assert reconcile_build(inputs).status == "complete"


@pytest.mark.parametrize("symlink_status", ("escaped", "unreadable"))
def test_unsafe_required_source_symlink_status_is_a_blocking_conflict(
    symlink_status: str,
) -> None:
    world = _make_world()
    source = world.census.sources[0].model_copy(update={"symlink_status": symlink_status})
    census = world.census.model_copy(update={"sources": (source,)})
    expectation = derive_expectation(census, world.scope, world.model)

    assert expectation.conflicts
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["expectation_id"] = expectation.expectation_id
    physical = _physical_from_payload(physical_payload)
    final_basis = derive_final_basis(
        census,
        expectation,
        world.model,
        physical,
        basis_revision=2,
    )
    evidence_set = _evidence_matching_final_basis(world, final_basis)
    inputs = _inputs_with(
        world,
        census=census,
        expectation=expectation,
        physical_basis=physical,
        final_basis=final_basis,
        evidence_set=evidence_set,
    )
    assert reconcile_build(inputs).status == "unverifiable"


def test_reconciliation_inputs_reject_stripping_derived_expectation_conflicts() -> None:
    world = _make_world()
    escaped = world.census.sources[0].model_copy(update={"symlink_status": "escaped"})
    census = world.census.model_copy(update={"sources": (escaped,)})
    derived = derive_expectation(census, world.scope, world.model)

    assert derived.expectation_id == world.expectation.expectation_id
    assert derived.conflicts
    assert world.expectation.conflicts == ()
    assert derived.model_dump(mode="python", exclude={"conflicts"}) == (
        world.expectation.model_dump(mode="python", exclude={"conflicts"})
    )
    with pytest.raises((ValidationError, ValueError), match="expectation|derivation|conflict"):
        _inputs_with(world, census=census)


def test_java_suffix_mislabeled_kotlin_keeps_java_denominator_and_blocks() -> None:
    world = _make_world()
    mislabeled = world.census.sources[0].model_copy(update={"language": "kotlin"})
    census = world.census.model_copy(update={"sources": (mislabeled,)})
    expectation = derive_expectation(census, world.scope, world.model)

    assert expectation.conflicts
    assert expectation.source_classifications[0].language == "java"
    assert len(expectation.required_java_edges) == 1
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["expectation_id"] = expectation.expectation_id
    physical = _physical_from_payload(physical_payload)
    final_basis = derive_final_basis(
        census,
        expectation,
        world.model,
        physical,
        basis_revision=2,
    )
    evidence_set = _evidence_matching_final_basis(world, final_basis)
    result = reconcile_build(
        _inputs_with(
            world,
            census=census,
            expectation=expectation,
            physical_basis=physical,
            final_basis=final_basis,
            evidence_set=evidence_set,
        )
    )
    assert result.status == "unverifiable"
    assert result.java_sources.project_expected == 1


def test_kotlin_suffix_mislabeled_java_never_enters_java_denominator() -> None:
    world = _make_world()
    original = world.census.sources[0]
    kotlin_path = "src/main/kotlin/example/K.kt"
    mislabeled = JvmSourceCandidate(
        source_id=canonical_content_id(
            "jvm-source", {"path": kotlin_path, "sha256": original.sha256}
        ),
        path=kotlin_path,
        sha256=original.sha256,
        language="java",
        origin="repository",
        role_hint="production",
        discovered_root="src/main/kotlin/example",
        symlink_status="contained",
    )
    census = world.census.model_copy(
        update={
            "sources": (mislabeled,),
            "source_state_fingerprint": _sha(f"source-state:{mislabeled.path}:{mislabeled.sha256}"),
        }
    )
    compile_unit = world.model.compile_units[0].model_copy(
        update={
            "language": "kotlin",
            "source_roots": ("src/main/kotlin",),
            "include_rules": (_source_rule("compile-main", "**/*.kt"),),
        }
    )
    model = world.model.model_copy(update={"compile_units": (compile_unit,)})
    expectation = derive_expectation(census, world.scope, model)

    assert expectation.conflicts
    assert expectation.source_classifications[0].language == "kotlin"
    assert expectation.required_java_edges == ()
    old_source_id = original.source_id
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload.update(
        {
            "expectation_id": expectation.expectation_id,
            "requested_member_ids": tuple(
                sorted(
                    mislabeled.source_id if item == old_source_id else item
                    for item in world.physical.requested_member_ids
                )
            ),
            "current_members": _sorted_by(
                (
                    (
                        CurrentPhysicalMemberV1(
                            member_id=mislabeled.source_id,
                            member_role="source",
                            path_kind="file",
                            path=f"{PROJECT_ROOT}/{mislabeled.path}",
                            byte_count=10,
                            sha256=mislabeled.sha256,
                        )
                        if member.member_id == old_source_id
                        else member
                    )
                    for member in world.physical.current_members
                ),
                lambda member: member.member_id,
            ),
        }
    )
    physical = _physical_from_payload(physical_payload)
    final_basis = derive_final_basis(
        census,
        expectation,
        model,
        physical,
        basis_revision=2,
    )
    evidence_set = _evidence_matching_final_basis(world, final_basis)
    result = reconcile_build(
        _inputs_with(
            world,
            census=census,
            build_model=model,
            expectation=expectation,
            physical_basis=physical,
            final_basis=final_basis,
            evidence_set=evidence_set,
        )
    )
    assert result.status == "unverifiable"
    assert result.java_sources.project_expected == 0


def test_posix_glob_single_star_does_not_cross_a_path_separator() -> None:
    path = "src/main/java/nested/App.java"
    world = _make_world(source_paths=(path,), assignments={"compile-main": (path,)})
    compile_unit = world.model.compile_units[0].model_copy(
        update={"include_rules": (_source_rule("compile-main", "*.java"),)}
    )
    model = world.model.model_copy(update={"compile_units": (compile_unit,)})

    derived = derive_expectation(world.census, world.scope, model)

    assert derived.required_java_edges == ()
    assert derived.source_classifications == ()
    assert derived.unclassified_source_ids == (world.census.sources[0].source_id,)


def test_active_unit_exclude_rule_classifies_source_as_explicitly_excluded() -> None:
    world = _make_world()
    exclude = _source_rule("compile-main", "example/*.java")
    compile_unit = world.model.compile_units[0].model_copy(update={"exclude_rules": (exclude,)})
    model = world.model.model_copy(update={"compile_units": (compile_unit,)})

    derived = derive_expectation(world.census, world.scope, model)

    assert derived.required_java_edges == ()
    assert derived.unclassified_source_ids == ()
    assert len(derived.source_classifications) == 1
    classification = derived.source_classifications[0]
    assert classification.classification == "explicitly_excluded"
    assert classification.classification_basis.basis_ref == exclude.rule_id


def test_same_round_generated_contract_precedes_normal_source_rule_ownership() -> None:
    generated_root = "target/generated-sources/annotations"
    path = f"{generated_root}/Generated.java"
    world = _make_world(source_paths=(path,), assignments={"compile-main": (path,)})
    contract = GeneratedRootContract(
        generated_root_id="same-round-generated-root",
        output_root=OutputRootSpec(
            member_id="same-round-generated-root",
            root_id="same-round-generated-root",
            path=generated_root,
            verification_mode="exclusive_tree",
            output_requirement="nonempty",
        ),
        producer_unit_kind="compile",
        producer_unit_id="compile-main",
        consumer_unit_ids=("compile-main",),
        mode="same_round_intermediate",
        active_profile_or_variant="default",
        model_provenance="fixture:annotation-processing-model",
    )
    model = world.model.model_copy(update={"generated_root_contracts": (contract,)})

    derived = derive_expectation(world.census, world.scope, model)

    assert derived.required_java_edges == ()
    assert derived.unclassified_source_ids == ()
    assert derived.source_classifications[0].classification == "compiler_intermediate"
    assert (
        derived.source_classifications[0].classification_basis.basis_ref
        == contract.generated_root_id
    )


def test_derive_final_basis_does_not_accept_observations_or_witnesses() -> None:
    signature = inspect.signature(derive_final_basis)
    parameters = signature.parameters

    assert tuple(parameters) == (
        "census",
        "expectation",
        "build_model",
        "physical",
        "basis_revision",
    )
    assert parameters["basis_revision"].kind is inspect.Parameter.KEYWORD_ONLY
    assert not {
        "observations",
        "witnesses",
        "receipts",
        "task_outcomes",
        "class_files",
        "artifact_bytes",
    } & set(parameters)


def test_empty_build_config_ledger_requires_canonical_empty_fingerprint() -> None:
    world = _make_world()
    canonical_empty = _build_config_fingerprint(())

    assert world.model.build_config_members == ()
    assert world.model.build_config_fingerprint == canonical_empty
    assert world.final_basis.conflicts == ()
    assert reconcile_build(world.inputs).status == "complete"

    model = world.model.model_copy(
        update={"build_config_fingerprint": _sha("self-declared-empty-config")}
    )
    expectation = derive_expectation(world.census, world.scope, model)
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload.update(
        {
            "expectation_id": expectation.expectation_id,
            "build_model_id": model.model_id,
        }
    )
    physical = _physical_from_payload(physical_payload)
    final_basis = derive_final_basis(
        world.census,
        expectation,
        model,
        physical,
        basis_revision=2,
    )

    assert final_basis.conflicts
    assert final_basis.compile_unit_bases == ()
    inputs = _inputs_with(
        world,
        build_model=model,
        expectation=expectation,
        physical_basis=physical,
        final_basis=final_basis,
    )
    assert reconcile_build(inputs).status == "unverifiable"


def test_all_pure_reconciliation_apis_trigger_no_filesystem_process_or_network_audit_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _make_world()
    selections = ReconciliationSelections(
        selected_generator_proofs=(),
        selected_compile_proofs=(),
        selected_package_proofs=(),
        selected_child_reconciliations=(),
        conflicts=(),
    )
    active = [False]
    forbidden_events: list[str] = []
    blocked_exact = {
        "open",
        "os.listdir",
        "os.scandir",
        "os.walk",
        "os.system",
        "subprocess.Popen",
        "socket.__new__",
        "socket.bind",
        "socket.connect",
        "socket.getaddrinfo",
    }

    def reject_impure_event(event: str, _args: tuple[Any, ...]) -> None:
        if active[0] and event in blocked_exact:
            forbidden_events.append(event)
            raise AssertionError(f"pure reconciliation API emitted audit event {event}")

    for function_name in ("stat", "lstat", "access", "readlink"):
        original = getattr(os, function_name)

        def guarded_metadata_read(
            *args: Any,
            _event: str = f"os.{function_name}",
            _original: Any = original,
            **kwargs: Any,
        ) -> Any:
            if active[0]:
                forbidden_events.append(_event)
                raise AssertionError(f"pure reconciliation API called {_event}")
            return _original(*args, **kwargs)

        monkeypatch.setattr(os, function_name, guarded_metadata_read)

    sys.addaudithook(reject_impure_event)
    calls = (
        lambda: derive_expectation(world.census, world.scope, world.model),
        lambda: derive_final_basis(
            world.census,
            world.expectation,
            world.model,
            world.physical,
            basis_revision=2,
        ),
        lambda: reconciliation_input_set_digest(world.inputs, selections),
        lambda: reconcile_build(world.inputs),
    )
    for call in calls:
        active[0] = True
        try:
            call()
        finally:
            active[0] = False

    assert forbidden_events == []


def test_derive_final_basis_uses_exact_typed_members_and_host_revision() -> None:
    world = _make_world()

    basis = derive_final_basis(
        world.census,
        world.expectation,
        world.model,
        world.physical,
        basis_revision=7,
    )

    assert basis.basis_revision == 7
    assert basis.expectation_id == world.expectation.expectation_id
    assert basis.physical_basis_snapshot_id == world.physical.physical_basis_snapshot_id
    assert basis.conflicts == ()
    assert len(basis.compile_unit_bases) == 1
    compile_basis = basis.compile_unit_bases[0]
    assert compile_basis.compile_unit_id == "compile-main"
    assert compile_basis.required_source_entries == (
        SourceDigestEntry(
            source_id=world.census.sources[0].source_id,
            path=world.census.sources[0].path,
            sha256=world.census.sources[0].sha256,
        ),
    )
    assert compile_basis.compiler_version == "21.0.1"
    assert compile_basis.output_roots == world.model.compile_units[0].output_roots


@pytest.mark.parametrize("fault", ("path", "hash"))
def test_derive_final_basis_rejects_source_physical_path_or_hash_drift(fault: str) -> None:
    world = _make_world()
    source = next(
        member for member in world.physical.current_members if member.member_role == "source"
    )
    update = (
        {"path": f"{PROJECT_ROOT}/src/main/java/example/Other.java"}
        if fault == "path"
        else {"sha256": _sha("source-bytes-drift")}
    )
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["current_members"] = _sorted_by(
        (
            member.model_copy(update=update) if member.member_id == source.member_id else member
            for member in world.physical.current_members
        ),
        lambda member: member.member_id,
    )
    physical = _physical_from_payload(physical_payload)

    basis = derive_final_basis(
        world.census,
        world.expectation,
        world.model,
        physical,
        basis_revision=2,
    )

    assert basis.conflicts
    assert basis.compile_unit_bases == ()


def test_output_root_physical_path_rebind_conflicts_omits_basis_and_stales_old_evidence() -> None:
    world = _make_world()
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["current_output_roots"] = tuple(
        root.model_copy(update={"path": f"{PROJECT_ROOT}/other/classes"})
        for root in world.physical.current_output_roots
    )
    physical = _physical_from_payload(physical_payload)

    basis = derive_final_basis(
        world.census,
        world.expectation,
        world.model,
        physical,
        basis_revision=2,
    )

    assert basis.conflicts
    assert basis.compile_unit_bases == ()
    result = reconcile_build(_inputs_with(world, physical_basis=physical, final_basis=basis))
    assert result.status != "complete"
    assert result.compile_units.coverage_current == 0


def test_build_config_member_normal_missing_and_hash_drift_are_physically_reconciled() -> None:
    world = _make_world()
    config_spec = ResolvedMemberSpec(
        member_id="build-config-pom",
        member_role="config",
        path_kind="file",
        path=f"{PROJECT_ROOT}/pom.xml",
        coordinate=None,
        upstream_unit_id=None,
        upstream_root_id=None,
        resolution_provenance="fixture:effective-model",
    )
    config_entry = ResolvedDigestEntry(
        member_id=config_spec.member_id,
        member_role=config_spec.member_role,
        path_kind=config_spec.path_kind,
        path=config_spec.path,
        byte_count=80,
        sha256=_sha("pom.xml:current"),
    )
    config_fingerprint = _build_config_fingerprint((config_entry,))
    model = world.model.model_copy(
        update={
            "build_config_members": (config_spec,),
            "build_config_fingerprint": config_fingerprint,
        }
    )
    expectation_payload = world.expectation.model_dump(mode="python")
    expectation_payload["build_config_fingerprint"] = config_fingerprint
    expectation = _expectation_from_payload(expectation_payload)
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["expectation_id"] = expectation.expectation_id
    config_member = CurrentPhysicalMemberV1(
        member_id=config_entry.member_id,
        member_role=config_entry.member_role,
        path_kind=config_entry.path_kind,
        path=config_entry.path,
        byte_count=config_entry.byte_count,
        sha256=config_entry.sha256,
    )
    physical_payload["current_members"] = _sorted_by(
        (*world.physical.current_members, config_member), lambda item: item.member_id
    )
    physical_payload["requested_member_ids"] = tuple(
        sorted((*world.physical.requested_member_ids, config_member.member_id))
    )
    physical = _physical_from_payload(physical_payload)

    current = derive_final_basis(
        world.census,
        expectation,
        model,
        physical,
        basis_revision=2,
    )
    assert current.conflicts == ()
    assert len(current.compile_unit_bases) == 1

    missing_payload = physical.model_dump(mode="python")
    missing_payload["current_members"] = tuple(
        member for member in physical.current_members if member.member_id != config_member.member_id
    )
    missing_payload["missing_members"] = (
        MissingPhysicalMemberV1(member_id=config_member.member_id, path=config_member.path),
    )
    missing = derive_final_basis(
        world.census,
        expectation,
        model,
        _physical_from_payload(missing_payload),
        basis_revision=3,
    )
    assert missing.conflicts == ()
    assert missing.compile_unit_bases == ()

    drift_payload = physical.model_dump(mode="python")
    drift_payload["current_members"] = _sorted_by(
        (
            (
                member.model_copy(update={"sha256": _sha("pom.xml:changed")})
                if member.member_id == config_member.member_id
                else member
            )
            for member in physical.current_members
        ),
        lambda item: item.member_id,
    )
    drift = derive_final_basis(
        world.census,
        expectation,
        model,
        _physical_from_payload(drift_payload),
        basis_revision=4,
    )
    assert drift.conflicts
    assert drift.compile_unit_bases == ()


def test_derive_final_basis_closes_generator_compile_and_package_without_conflict() -> None:
    world = _complete_closure_world()

    basis = derive_final_basis(
        world.census,
        world.expectation,
        world.model,
        world.physical,
        basis_revision=4,
    )

    assert basis.conflicts == ()
    assert len(basis.generator_unit_bases) == 1
    assert len(basis.compile_unit_bases) == 1
    assert len(basis.package_unit_bases) == 1
    assert basis.generator_unit_bases[0].generator_unit_id == "generator-main"
    assert basis.compile_unit_bases[0].compile_unit_id == "compile-main"
    assert basis.package_unit_bases[0].package_unit_id == "package-main"


def test_mixed_language_supported_unit_basis_includes_non_java_expected_sources() -> None:
    world = _make_world()
    kotlin_source = _source("src/main/kotlin/example/Peer.kt", language="kotlin")
    sources = _sorted_by((*world.census.sources, kotlin_source), lambda item: item.source_id)
    census = world.census.model_copy(
        update={
            "sources": sources,
            "source_state_fingerprint": _sha(
                "mixed-source-state:"
                + ":".join(f"{source.path}:{source.sha256}" for source in sources)
            ),
        }
    )
    compile_unit = world.model.compile_units[0].model_copy(
        update={
            "language": "kotlin",
            "source_roots": ("src/main",),
            "include_rules": (),
        }
    )
    model = world.model.model_copy(update={"compile_units": (compile_unit,)})
    expectation = derive_expectation(census, world.scope, model)
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["expectation_id"] = expectation.expectation_id
    kotlin_member = CurrentPhysicalMemberV1(
        member_id=kotlin_source.source_id,
        member_role="source",
        path_kind="file",
        path=f"{PROJECT_ROOT}/{kotlin_source.path}",
        byte_count=10,
        sha256=kotlin_source.sha256,
    )
    physical_payload["current_members"] = _sorted_by(
        (*world.physical.current_members, kotlin_member), lambda item: item.member_id
    )
    physical_payload["requested_member_ids"] = tuple(
        sorted((*world.physical.requested_member_ids, kotlin_member.member_id))
    )
    physical = _physical_from_payload(physical_payload)

    basis = derive_final_basis(
        census,
        expectation,
        model,
        physical,
        basis_revision=2,
    )

    assert basis.conflicts == ()
    assert {entry.path for entry in basis.compile_unit_bases[0].required_source_entries} == {
        source.path for source in sources
    }
    assert len(expectation.required_java_edges) == 1
    assert len(expectation.source_classifications) == 2


@pytest.mark.parametrize("member_id", ("compiler-javac", "toolchain-jdk"))
def test_compiler_or_toolchain_physical_hash_changes_compile_identity_and_stales_old_evidence(
    member_id: str,
) -> None:
    world = _make_world()
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["current_members"] = _sorted_by(
        (
            (
                member.model_copy(update={"sha256": _sha(f"changed:{member_id}")})
                if member.member_id == member_id
                else member
            )
            for member in world.physical.current_members
        ),
        lambda member: member.member_id,
    )
    physical = _physical_from_payload(physical_payload)
    basis = derive_final_basis(
        world.census,
        world.expectation,
        world.model,
        physical,
        basis_revision=2,
    )

    assert basis.conflicts == ()
    assert (
        basis.compile_unit_bases[0].compile_unit_identity_hash
        != world.final_basis.compile_unit_bases[0].compile_unit_identity_hash
    )
    result = reconcile_build(_inputs_with(world, physical_basis=physical, final_basis=basis))
    assert result.status != "complete"
    assert result.compile_units.coverage_current == 0


def test_compile_output_root_digest_changes_package_input_identity_and_stales_old_evidence() -> (
    None
):
    world = _complete_closure_world()
    compile_root_id = world.final_basis.compile_unit_bases[0].output_roots[0].root_id
    changed_digest = _sha("changed-compile-output-root")
    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["current_output_roots"] = _sorted_by(
        (
            (
                root.model_copy(update={"tree_or_owned_entries_sha256": changed_digest})
                if root.root_id == compile_root_id
                else root
            )
            for root in world.physical.current_output_roots
        ),
        lambda root: root.member_id,
    )
    physical = _physical_from_payload(physical_payload)
    basis = derive_final_basis(
        world.census,
        world.expectation,
        world.model,
        physical,
        basis_revision=2,
    )

    package_basis = basis.package_unit_bases[0]
    assert package_basis.input_compile_output_digests[0].sha256 == changed_digest
    assert (
        package_basis.package_unit_identity_hash
        != world.final_basis.package_unit_bases[0].package_unit_identity_hash
    )
    result = reconcile_build(_inputs_with(world, physical_basis=physical, final_basis=basis))
    assert result.status != "complete"
    assert result.package_units.current == 0


@pytest.mark.parametrize(
    ("member_id", "unit_kind"),
    (
        ("generator-input-schema", "generator"),
        ("generator-protoc", "generator"),
        ("packaging-jar", "package"),
    ),
)
def test_closure_physical_input_or_executable_sha_changes_identity_and_stales_old_evidence(
    member_id: str,
    unit_kind: str,
) -> None:
    world = _complete_closure_world()
    baseline_basis = derive_final_basis(
        world.census,
        world.expectation,
        world.model,
        world.physical,
        basis_revision=2,
    )
    baseline_evidence = _evidence_matching_final_basis(world, baseline_basis)
    baseline_inputs = _inputs_with(
        world,
        final_basis=baseline_basis,
        evidence_set=baseline_evidence,
    )
    assert reconcile_build(baseline_inputs).status == "complete"

    physical_payload = world.physical.model_dump(mode="python")
    physical_payload["current_members"] = _sorted_by(
        (
            (
                member.model_copy(update={"sha256": _sha(f"changed:{member_id}")})
                if member.member_id == member_id
                else member
            )
            for member in world.physical.current_members
        ),
        lambda member: member.member_id,
    )
    physical = _physical_from_payload(physical_payload)
    changed_basis = derive_final_basis(
        world.census,
        world.expectation,
        world.model,
        physical,
        basis_revision=3,
    )
    assert changed_basis.conflicts == ()
    if unit_kind == "generator":
        assert (
            changed_basis.generator_unit_bases[0].generator_unit_identity_hash
            != baseline_basis.generator_unit_bases[0].generator_unit_identity_hash
        )
    else:
        assert (
            changed_basis.package_unit_bases[0].package_unit_identity_hash
            != baseline_basis.package_unit_bases[0].package_unit_identity_hash
        )

    result = reconcile_build(
        _inputs_with(
            world,
            physical_basis=physical,
            final_basis=changed_basis,
            evidence_set=baseline_evidence,
        )
    )
    assert result.status != "complete"
    if unit_kind == "generator":
        assert result.generator_units.current == 0
    else:
        assert result.package_units.current == 0


@pytest.mark.parametrize("fault", ("missing", "unreadable", "rebound", "extra"))
def test_derive_final_basis_fails_closed_on_missing_unreadable_rebound_or_extra_member(
    fault: str,
) -> None:
    world = _make_world()
    payload = world.physical.model_dump(mode="python")
    compiler = next(
        item for item in world.physical.current_members if item.member_id == "compiler-javac"
    )
    current_members = tuple(
        item for item in world.physical.current_members if item.member_id != compiler.member_id
    )
    if fault == "missing":
        payload["current_members"] = current_members
        payload["missing_members"] = (
            MissingPhysicalMemberV1(
                member_id=compiler.member_id,
                path=compiler.path,
            ),
        )
    elif fault == "unreadable":
        payload["current_members"] = current_members
        payload["unreadable_members"] = (
            UnreadablePhysicalMemberV1(
                member_id=compiler.member_id,
                path=compiler.path,
                reason="clean read failed",
            ),
        )
    elif fault == "rebound":
        payload["current_members"] = _sorted_by(
            (
                *current_members,
                compiler.model_copy(update={"path": "/opt/other-jdk/bin/javac"}),
            ),
            lambda item: item.member_id,
        )
    else:
        extra = compiler.model_copy(update={"member_id": "compiler-javac-extra"})
        payload["current_members"] = _sorted_by(
            (*world.physical.current_members, extra), lambda item: item.member_id
        )
        payload["requested_member_ids"] = tuple(
            sorted((*world.physical.requested_member_ids, extra.member_id))
        )
    identity = {
        key: value
        for key, value in payload.items()
        if key not in {"physical_basis_snapshot_id", "conflicts"}
    }
    payload["physical_basis_snapshot_id"] = canonical_content_id(
        "current-physical-basis-v1", identity
    )
    physical = CurrentPhysicalBasisSnapshotV1.model_validate(payload)

    basis = derive_final_basis(
        world.census,
        world.expectation,
        world.model,
        physical,
        basis_revision=2,
    )

    assert basis.basis_revision == 2
    assert basis.physical_basis_snapshot_id == physical.physical_basis_snapshot_id
    if fault == "missing":
        assert basis.conflicts == ()
        assert basis.compile_unit_bases == ()
    else:
        assert basis.conflicts
    assert isinstance(basis.compile_unit_bases, tuple)


def test_metamorphic_01_adding_uncovered_active_source_cannot_improve() -> None:
    complete = reconcile_build(_make_world().inputs)
    paths = ("src/main/java/example/App.java", "src/main/java/example/New.java")
    expanded = reconcile_build(
        _make_world(
            source_paths=paths,
            assignments={"compile-main": paths},
            observations=(ObservationSpec("compile-main", (paths[0],)),),
        ).inputs
    )

    assert expanded.java_sources.project_expected == complete.java_sources.project_expected + 1
    assert _status_rank(expanded.status) <= _status_rank(complete.status)


def test_metamorphic_02_adding_class_files_does_not_change_source_coverage() -> None:
    one = reconcile_build(
        _make_world(
            observations=(
                ObservationSpec(
                    "compile-main",
                    ("src/main/java/example/App.java",),
                    class_entries=("example/App.class",),
                ),
            )
        ).inputs
    )
    many = reconcile_build(
        _make_world(
            observations=(
                ObservationSpec(
                    "compile-main",
                    ("src/main/java/example/App.java",),
                    class_entries=tuple(f"example/App${index}.class" for index in range(20)),
                ),
            )
        ).inputs
    )

    assert one.java_sources == many.java_sources
    assert one.java_edges == many.java_edges


def test_metamorphic_03_deleting_or_changing_witness_cannot_improve() -> None:
    complete = reconcile_build(_make_world().inputs)
    missing = reconcile_build(
        _make_world(
            observations=(
                ObservationSpec(
                    "compile-main",
                    ("src/main/java/example/App.java",),
                    witness_state="missing",
                ),
            )
        ).inputs
    )
    changed = reconcile_build(
        _make_world(
            observations=(
                ObservationSpec(
                    "compile-main",
                    ("src/main/java/example/App.java",),
                    witness_state="mismatch",
                ),
            )
        ).inputs
    )

    assert _status_rank(missing.status) <= _status_rank(complete.status)
    assert _status_rank(changed.status) <= _status_rank(complete.status)


def test_metamorphic_04_positive_currentness_forms_leave_result_unchanged() -> None:
    results = tuple(
        reconcile_build(
            _make_world(
                observations=(
                    ObservationSpec(
                        "compile-main",
                        ("src/main/java/example/App.java",),
                        outcome=outcome,
                    ),
                )
            ).inputs
        )
        for outcome in POSITIVE_OUTCOMES
    )

    assert {item.status for item in results} == {"complete"}
    assert len({item.java_sources for item in results}) == 1
    assert len({item.compile_units for item in results}) == 1


def test_metamorphic_05_split_whole_units_is_stable_but_split_one_unit_is_not() -> None:
    paths = ("a/src/main/java/a/A.java", "b/src/main/java/b/B.java")
    whole = reconcile_build(
        _make_world(
            source_paths=paths,
            assignments={"compile-a": (paths[0],), "compile-b": (paths[1],)},
            observations=(
                ObservationSpec("compile-a", (paths[0],), run_id="run-a"),
                ObservationSpec("compile-b", (paths[1],), run_id="run-b"),
            ),
        ).inputs
    )
    split = reconcile_build(
        _make_world(
            source_paths=paths,
            assignments={"compile-main": paths},
            observations=(
                ObservationSpec("compile-main", (paths[0],), run_id="run-a"),
                ObservationSpec("compile-main", (paths[1],), run_id="run-b"),
            ),
        ).inputs
    )

    assert whole.status == "complete"
    assert split.status == "absent"


def test_metamorphic_06_run_order_is_inert_and_foreign_epoch_cannot_improve() -> None:
    paths = ("a/src/main/java/a/A.java", "b/src/main/java/b/B.java")
    forward = _make_world(
        source_paths=paths,
        assignments={"compile-a": (paths[0],), "compile-b": (paths[1],)},
        observations=(
            ObservationSpec("compile-a", (paths[0],), run_id="run-a"),
            ObservationSpec("compile-b", (paths[1],), run_id="run-b"),
        ),
    )
    reverse = _make_world(
        source_paths=paths,
        assignments={"compile-a": (paths[0],), "compile-b": (paths[1],)},
        observations=(
            ObservationSpec("compile-b", (paths[1],), run_id="run-b"),
            ObservationSpec("compile-a", (paths[0],), run_id="run-a"),
        ),
    )

    assert reconcile_build(forward.inputs).status == reconcile_build(reverse.inputs).status
    foreign = _make_world(epoch_tag="foreign")
    with pytest.raises((ValidationError, ValueError), match="epoch|cross|link"):
        ReconciliationInputs(
            epoch=forward.epoch,
            goal_scope=forward.scope,
            census=forward.census,
            build_model=forward.model,
            expectation=forward.expectation,
            final_basis=forward.final_basis,
            physical_basis=forward.physical,
            evidence_set=foreign.evidence_set,
        )


def test_metamorphic_07_activating_extra_obligations_cannot_improve() -> None:
    baseline = reconcile_build(_make_world().inputs)
    paths = ("src/main/java/example/App.java", "profile/src/main/java/Profile.java")
    activated = reconcile_build(
        _make_world(
            source_paths=paths,
            assignments={
                "compile-main": (paths[0],),
                "compile-profile": (paths[1],),
            },
            observations=(ObservationSpec("compile-main", (paths[0],)),),
        ).inputs
    )

    assert activated.java_sources.project_expected > baseline.java_sources.project_expected
    assert _status_rank(activated.status) <= _status_rank(baseline.status)


def test_metamorphic_08_readable_to_unavailable_never_improves_or_shrinks() -> None:
    readable = reconcile_build(_make_world().inputs)
    unavailable = reconcile_build(
        _make_world(record_statuses={"output_witness": "unavailable"}).inputs
    )

    assert _status_rank(unavailable.status) <= _status_rank(readable.status)
    assert unavailable.java_sources.project_expected == readable.java_sources.project_expected


def test_metamorphic_09_shared_root_other_owned_entries_do_not_invalidate_proof() -> None:
    paths = ("a/src/main/java/a/A.java", "b/src/main/java/b/B.java")
    one = reconcile_build(
        _make_world(
            source_paths=(paths[0],),
            assignments={"compile-a": (paths[0],)},
            observations=(ObservationSpec("compile-a", (paths[0],)),),
        ).inputs
    )
    two = reconcile_build(
        _make_world(
            source_paths=paths,
            assignments={"compile-a": (paths[0],), "compile-b": (paths[1],)},
            output_paths={
                "compile-a": "target/shared-classes",
                "compile-b": "target/shared-classes",
            },
            observations=(
                ObservationSpec("compile-a", (paths[0],)),
                ObservationSpec(
                    "compile-b",
                    (paths[1],),
                    class_entries=("b/B.class", "b/B$Owned.class"),
                ),
            ),
        ).inputs
    )

    assert one.status == two.status == "complete"
    assert any(proof.compile_unit_id == "compile-a" for proof in two.selected_compile_proofs)


def test_metamorphic_10_upstream_output_change_stales_package_even_with_same_sources() -> None:
    current = reconcile_build(_complete_closure_world().inputs)
    changed = reconcile_build(_complete_closure_world(package_change="compile_output").inputs)

    assert current.java_sources == changed.java_sources
    assert current.compile_units == changed.compile_units
    assert current.package_units.current == 1
    assert changed.package_units.current == 0


def test_metamorphic_11_any_authorized_raw_input_mutation_changes_final_identity() -> None:
    world = _make_world()
    original = reconcile_build(world.inputs)
    record_id = world.evidence_set.compile_observations[0].observation_id
    changed_world = _replace_raw_hash(world, record_id, _sha("mutated-authorized-raw"))
    changed = reconcile_build(changed_world.inputs)

    assert changed.input_set_digest != original.input_set_digest
    assert changed.reconciliation_id != original.reconciliation_id
    assert _status_rank(changed.status) <= _status_rank(original.status)
