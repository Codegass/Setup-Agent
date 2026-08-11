"""Strict boundaries for filesystem-first JVM build evidence models."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from sag.agent.jvm_build_models import (
    BUILD_EVIDENCE_GENESIS_SHA256,
    BUILD_EVIDENCE_MAX_CANONICAL_BYTES,
    BUILD_EVIDENCE_MAX_ENTRIES,
    BUILD_EVIDENCE_MAX_PATH_CHARS,
    BUILD_EVIDENCE_MAX_RAW_BYTES,
    BUILD_EVIDENCE_MAX_TEXT_CHARS,
    BUILD_EVIDENCE_SCHEMA_VERSION,
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
    SealedBuildEvidenceV1,
    SelectedChildReconciliation,
    SelectedCompileProof,
    SelectedGeneratorProof,
    SelectedPackageProof,
    SourceClassification,
    SourceDigestEntry,
    SourcePatternRuleV1,
    UnreadablePhysicalMemberV1,
    canonical_build_evidence_bytes,
    canonical_build_evidence_sha256,
    canonical_content_id,
    compile_unit_identity_hash,
    generator_unit_identity_hash,
    package_unit_identity_hash,
    physical_output_aggregate_sha256,
    validate_build_evidence_json,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
PROJECT_ROOT = "/workspace/project"
CONTAINER_ID = "container-01"
EPOCH_ID = canonical_content_id(
    "container-evidence-epoch-v1",
    {"immutable_container_id": CONTAINER_ID, "project_root": PROJECT_ROOT},
)
SCOPE_ID = canonical_content_id(
    "build-goal-scope-v1",
    {"evidence_epoch_id": EPOCH_ID, "authority_ref": "request-01"},
)
CENSUS_ID = canonical_content_id(
    "jvm-source-census-v1",
    {"evidence_epoch_id": EPOCH_ID, "project_root": PROJECT_ROOT},
)
MODEL_ID = canonical_content_id(
    "evaluated-build-model-v1",
    {
        "evidence_epoch_id": EPOCH_ID,
        "scope_id": SCOPE_ID,
        "project_root": PROJECT_ROOT,
    },
)
EXPECTATION_ID = canonical_content_id(
    "jvm-build-expectation-v1",
    {
        "evidence_epoch_id": EPOCH_ID,
        "census_id": CENSUS_ID,
        "scope_id": SCOPE_ID,
        "model_id": MODEL_ID,
        "scope_fingerprint": SHA_B,
        "source_state_fingerprint": SHA_A,
        "build_config_fingerprint": SHA_B,
        "required_java_edges": (),
        "source_classifications": (),
        "unclassified_source_ids": (),
        "required_generator_unit_ids": (),
        "required_compile_unit_ids": (),
        "required_package_unit_ids": (),
        "unsupported_active_unit_ids": (),
        "required_child_scope_ids": (),
        "delegation_edges": (),
    },
)
PHYSICAL_BASIS_ID = canonical_content_id(
    "current-physical-basis-v1",
    {
        "evidence_epoch_id": EPOCH_ID,
        "expectation_id": EXPECTATION_ID,
        "build_model_id": MODEL_ID,
        "requested_member_ids": (),
        "current_members": (),
        "current_output_roots": (),
        "current_output_entries": (),
        "missing_members": (),
        "unreadable_members": (),
    },
)
FINAL_BASIS_ID = canonical_content_id(
    "final-build-basis-v1",
    {"evidence_epoch_id": EPOCH_ID, "expectation_id": EXPECTATION_ID},
)


def _maven_invocation_scope() -> ActualInvocationScopeV1:
    return ActualInvocationScopeV1(
        build_system="maven",
        selected_modules_or_tasks=(),
        lifecycle_or_tasks=("package",),
        profiles_or_variants=(),
        resume_from=None,
        also_make=False,
        selector_fingerprint=SHA_A,
    )


def _source_payload(**updates: object) -> dict[str, object]:
    projection = {
        "path": updates.get("path", "src/main/java/example/App.java"),
        "sha256": updates.get("sha256", SHA_A),
    }
    payload: dict[str, object] = {
        "source_id": updates.get("source_id", canonical_content_id("jvm-source", projection)),
        **projection,
        "language": "java",
        "origin": "repository",
        "role_hint": "production",
        "discovered_root": "src/main/java",
        "symlink_status": "contained",
    }
    payload.update(updates)
    return payload


def _source() -> JvmSourceCandidate:
    return JvmSourceCandidate.model_validate(_source_payload())


def _complete_set(record_kind: str) -> EvidenceRecordSetCompletenessV1:
    return EvidenceRecordSetCompletenessV1(
        record_kind=record_kind,
        status="complete",
        expected_record_ids=(),
        observed_record_ids=(),
        observed_record_hashes=(),
        tombstoned_record_ids=(),
        conflicts=(),
    )


def _evidence_set_payload() -> dict[str, object]:
    identity: dict[str, object] = {
        "evidence_epoch_id": EPOCH_ID,
        "admitted_run_ids": (),
        "generator_observations": (),
        "compile_observations": (),
        "package_observations": (),
        "output_witnesses": (),
        "child_censuses": (),
        "child_expectations": (),
        "child_reconciliations": (),
        "record_sets": tuple(
            _complete_set(kind)
            for kind in (
                "generator_observation",
                "compile_observation",
                "package_observation",
                "output_witness",
                "child_census",
                "child_expectation",
                "child_reconciliation",
            )
        ),
    }
    return {
        "snapshot_id": canonical_content_id("build-evidence-set-v1", identity),
        **identity,
        "conflicts": (),
    }


def _refresh_evidence_set_id(payload: dict[str, object]) -> dict[str, object]:
    identity = {
        key: value for key, value in payload.items() if key not in {"snapshot_id", "conflicts"}
    }
    payload["snapshot_id"] = canonical_content_id("build-evidence-set-v1", identity)
    return payload


def _refresh_physical_basis_id(payload: dict[str, object]) -> dict[str, object]:
    identity = {
        key: value
        for key, value in payload.items()
        if key not in {"physical_basis_snapshot_id", "conflicts"}
    }
    payload["physical_basis_snapshot_id"] = canonical_content_id(
        "current-physical-basis-v1", identity
    )
    return payload


def _refresh_expectation_id(payload: dict[str, object]) -> dict[str, object]:
    identity = {
        key: value for key, value in payload.items() if key not in {"expectation_id", "conflicts"}
    }
    payload["expectation_id"] = canonical_content_id("jvm-build-expectation-v1", identity)
    return payload


def _compile_observation() -> CompileUnitObservationV1:
    source = SourceDigestEntry(
        source_id=_source().source_id,
        path=_source().path,
        sha256=_source().sha256,
    )
    classpath = ResolvedDigestEntry(
        member_id="classpath-dependency-01",
        member_role="classpath",
        path_kind="file",
        path="/workspace/project/deps/dependency.jar",
        byte_count=10,
        sha256=SHA_A,
    )
    output_root = OutputRootSpec(
        member_id="classes-root-member",
        root_id="classes-root",
        path="target/classes",
        verification_mode="exclusive_tree",
        output_requirement="nonempty",
    )
    basis_projection: dict[str, object] = {
        "compile_unit_id": "compile-01",
        "build_config_fingerprint": SHA_B,
        "required_source_entries": (source,),
        "source_set_fingerprint": SHA_A,
        "compiler_executable_path": "/opt/jdk/bin/javac",
        "compiler_executable_sha256": SHA_A,
        "compiler_version": "21.0.1",
        "compiler_args_fingerprint": SHA_A,
        "classpath_entries": (classpath,),
        "dependency_unit_identity_hashes": (),
        "toolchain_fingerprint": SHA_B,
        "output_roots": (output_root,),
    }
    identity_hash = compile_unit_identity_hash(basis_projection)
    observation_id = canonical_content_id(
        "compile-observation-v1",
        {
            "evidence_epoch_id": EPOCH_ID,
            "run_id": "run-01",
            "receipt_id": "receipt-01",
            "contract_id": "contract-01",
            "compile_unit_id": "compile-01",
        },
    )
    return CompileUnitObservationV1(
        observation_id=observation_id,
        evidence_epoch_id=EPOCH_ID,
        run_id="run-01",
        receipt_id="receipt-01",
        contract_id="contract-01",
        compile_unit_id="compile-01",
        build_config_fingerprint=SHA_B,
        compile_unit_identity_hash=identity_hash,
        source_entries=(source,),
        source_set_fingerprint=SHA_A,
        compiler_executable_path="/opt/jdk/bin/javac",
        compiler_executable_sha256=SHA_A,
        compiler_version="21.0.1",
        compiler_args_fingerprint=SHA_A,
        classpath_entries=(classpath,),
        dependency_unit_identity_hashes=(),
        toolchain_fingerprint=SHA_B,
        observed_output_roots=(output_root,),
        outcome="executed_success",
        actual_invocation_scope=_maven_invocation_scope(),
        generated_during_compile=(),
        conflicts=(),
    )


def _generator_observation() -> GeneratorUnitObservationV1:
    generator_input = ResolvedDigestEntry(
        member_id="generator-input-01",
        member_role="generator_input",
        path_kind="file",
        path="/workspace/project/schema/service.proto",
        byte_count=10,
        sha256=SHA_A,
    )
    generator_tool = ResolvedDigestEntry(
        member_id="generator-tool-01",
        member_role="generator_tool",
        path_kind="file",
        path="/opt/tools/protoc-plugin",
        byte_count=10,
        sha256=SHA_A,
    )
    generator_dependency = ResolvedDigestEntry(
        member_id="generator-dependency-01",
        member_role="generator_dependency",
        path_kind="file",
        path="/opt/tools/protobuf.jar",
        byte_count=10,
        sha256=SHA_B,
    )
    identity_fields: dict[str, object] = {
        "generator_unit_id": "generator-01",
        "build_config_fingerprint": SHA_B,
        "input_entries": (generator_input,),
        "generator_executable_path": "/workspace/project/tools/protoc",
        "generator_executable_sha256": SHA_A,
        "generator_tool_entries": (generator_tool,),
        "generator_dependency_entries": (generator_dependency,),
        "generator_args_fingerprint": SHA_A,
        "generator_toolchain_fingerprint": SHA_B,
        "generated_output_roots": (
            OutputRootSpec(
                member_id="generated-root-member-01",
                root_id="generated-root-01",
                path="target/generated-sources/protobuf",
                verification_mode="shared_owned_entries",
                output_requirement="nonempty",
            ),
        ),
    }
    return GeneratorUnitObservationV1(
        observation_id=canonical_content_id(
            "generator-observation-v1",
            {
                "evidence_epoch_id": EPOCH_ID,
                "run_id": "run-01",
                "receipt_id": "receipt-01",
                "contract_id": "contract-01",
                "generator_unit_id": "generator-01",
            },
        ),
        evidence_epoch_id=EPOCH_ID,
        run_id="run-01",
        receipt_id="receipt-01",
        contract_id="contract-01",
        **identity_fields,
        generator_unit_identity_hash=generator_unit_identity_hash(identity_fields),
        outcome="executed_success",
        actual_invocation_scope=_maven_invocation_scope(),
        conflicts=(),
    )


def _package_observation() -> PackageUnitObservationV1:
    output_digest = CompileOutputDigestEntry(
        compile_unit_id="compile-01",
        root_id="classes-root",
        sha256=SHA_A,
    )
    artifact = RequestedArtifactSpec(
        artifact_id="artifact-01",
        root_id="package-output-root-01",
        path="target/app.jar",
        kind="jar",
    )
    resource = ResolvedDigestEntry(
        member_id="resource-01",
        member_role="resource",
        path_kind="file",
        path="/workspace/project/src/main/resources/app.conf",
        byte_count=10,
        sha256=SHA_A,
    )
    packaging_tool = ResolvedDigestEntry(
        member_id="packaging-tool-01",
        member_role="packaging_tool",
        path_kind="file",
        path="/opt/maven/plugins/maven-jar-plugin.jar",
        byte_count=10,
        sha256=SHA_A,
    )
    external_dependency = ResolvedDigestEntry(
        member_id="runtime-dependency-01",
        member_role="external_dependency",
        path_kind="file",
        path="/workspace/.m2/repository/runtime.jar",
        byte_count=10,
        sha256=SHA_B,
    )
    identity_fields: dict[str, object] = {
        "package_unit_id": "package-01",
        "build_config_fingerprint": SHA_B,
        "input_compile_unit_identity_hashes": (SHA_A,),
        "input_compile_output_digests": (output_digest,),
        "resource_entries": (resource,),
        "packaging_executable_path": "/usr/bin/jar",
        "packaging_executable_sha256": SHA_A,
        "packaging_tool_entries": (packaging_tool,),
        "external_dependency_entries": (external_dependency,),
        "packaging_args_fingerprint": SHA_A,
        "packaging_toolchain_fingerprint": SHA_B,
        "packaging_config_fingerprint": SHA_A,
        "output_roots": (
            OutputRootSpec(
                member_id="package-output-member-01",
                root_id="package-output-root-01",
                path="target",
                verification_mode="shared_owned_entries",
                output_requirement="nonempty",
            ),
        ),
        "requested_artifacts": (artifact,),
    }
    return PackageUnitObservationV1(
        observation_id=canonical_content_id(
            "package-observation-v1",
            {
                "evidence_epoch_id": EPOCH_ID,
                "run_id": "run-01",
                "receipt_id": "receipt-01",
                "contract_id": "contract-01",
                "package_unit_id": "package-01",
            },
        ),
        evidence_epoch_id=EPOCH_ID,
        run_id="run-01",
        receipt_id="receipt-01",
        contract_id="contract-01",
        **identity_fields,
        package_unit_identity_hash=package_unit_identity_hash(identity_fields),
        observed_artifact_paths=("target/app.jar",),
        outcome="executed_success",
        actual_invocation_scope=_maven_invocation_scope(),
        conflicts=(),
    )


def _compile_output_witness(
    observation: CompileUnitObservationV1 | None = None,
) -> PhysicalOutputWitnessV1:
    observation = observation or _compile_observation()
    observed_root = observation.observed_output_roots[0]
    entry = PhysicalOutputEntry(
        root_id=observed_root.root_id,
        relative_path="example/App.class",
        kind="class",
        byte_count=24,
        sha256=SHA_A,
    )
    root = OutputRootWitness(
        root_id=observed_root.root_id,
        path=observed_root.path,
        verification_mode=observed_root.verification_mode,
        tree_or_owned_entries_sha256=SHA_B,
        entry_count=1,
    )
    return PhysicalOutputWitnessV1(
        witness_id=canonical_content_id(
            "physical-output-witness-v1",
            {
                "evidence_epoch_id": EPOCH_ID,
                "producer_observation_kind": "compile",
                "producer_observation_id": observation.observation_id,
            },
        ),
        evidence_epoch_id=EPOCH_ID,
        producer_observation_kind="compile",
        producer_observation_id=observation.observation_id,
        producer_unit_id=observation.compile_unit_id,
        producer_unit_identity_hash=observation.compile_unit_identity_hash,
        roots=(root,),
        entries=(entry,),
        aggregate_sha256=physical_output_aggregate_sha256((root,), (entry,)),
    )


def _path_basis(
    path: str,
    basis_ref: str,
    *,
    member_role: str = "compiler",
) -> BuildPathBasis:
    return BuildPathBasis(
        member_id=canonical_content_id(
            "physical-member-v1",
            {"member_role": member_role, "path": path},
        ),
        member_role=member_role,
        path=path,
        path_kind="file",
        resolution_provenance=basis_ref,
    )


def _toolchain_basis() -> BuildPathBasis:
    return BuildPathBasis(
        member_id="jdk-toolchain-01",
        member_role="toolchain",
        path="/opt/jdk",
        path_kind="tree",
        resolution_provenance="maven-toolchain:jdk",
    )


def _source_rule(
    compile_unit_id: str,
    pattern: str,
    *,
    model_provenance: str = "maven-effective-model",
) -> SourcePatternRuleV1:
    projection = {
        "compile_unit_id": compile_unit_id,
        "dialect": "posix_glob_v1",
        "pattern": pattern,
        "model_provenance": model_provenance,
    }
    return SourcePatternRuleV1(
        rule_id=canonical_content_id("source-pattern-rule-v1", projection),
        **projection,
    )


def _member_spec(
    member_id: str,
    member_role: str,
    path: str,
    basis_ref: str,
    *,
    path_kind: str = "file",
) -> ResolvedMemberSpec:
    return ResolvedMemberSpec(
        member_id=member_id,
        member_role=member_role,
        path_kind=path_kind,
        path=path,
        coordinate=None,
        upstream_unit_id=None,
        upstream_root_id=None,
        resolution_provenance=basis_ref,
    )


def _compiler_environment(
    *, upstream_compile_output: tuple[str, str] | None = None
) -> CompilerEnvironmentSpec:
    if upstream_compile_output is None:
        classpath_members = (
            _member_spec(
                "classpath-base",
                "classpath",
                "/workspace/project/deps/base.jar",
                "maven-dependency-resolution",
            ),
        )
    else:
        upstream_unit_id, upstream_root_id = upstream_compile_output
        classpath_members = (
            ResolvedMemberSpec(
                member_id="upstream-output-base",
                member_role="upstream_output",
                path_kind="tree",
                path=None,
                coordinate=None,
                upstream_unit_id=upstream_unit_id,
                upstream_root_id=upstream_root_id,
                resolution_provenance="maven-reactor-classpath",
            ),
        )
    return CompilerEnvironmentSpec(
        executable_path_basis=_path_basis("/opt/jdk/bin/javac", "maven-toolchain:compile"),
        compiler_version="javac 21.0.4",
        compiler_args=("-parameters",),
        classpath_members=classpath_members,
        toolchain_path_basis=_toolchain_basis(),
        resolution_provenance="maven-effective-model",
    )


def _generator_expectation() -> GeneratorUnitExpectation:
    return GeneratorUnitExpectation(
        generator_unit_id="generator-01",
        module_coordinate="com.example:app",
        task_or_execution="generate-sources",
        role="required",
        input_roots=(
            _member_spec(
                "generator-input-01",
                "generator_input",
                "/workspace/project/schema",
                "maven-plugin-input",
                path_kind="tree",
            ),
        ),
        generator_environment_spec=GeneratorEnvironmentSpec(
            executable_path_basis=_path_basis(
                "/workspace/project/tools/protoc",
                "maven-plugin:protoc",
                member_role="generator_tool",
            ),
            tool_or_plugin_members=(
                _member_spec(
                    "generator-tool-01",
                    "generator_tool",
                    "/opt/maven/plugins/protoc-plugin.jar",
                    "maven-plugin-resolution",
                ),
            ),
            dependency_members=(
                _member_spec(
                    "generator-dependency-01",
                    "generator_dependency",
                    "/workspace/.m2/repository/protobuf.jar",
                    "maven-dependency-resolution",
                ),
            ),
            generator_args=("--java_out=target/generated-sources",),
            toolchain_path_basis=_toolchain_basis(),
        ),
        generated_root_ids=("generated-root-01",),
        model_provenance="maven-effective-model",
    )


def _compile_expectation(
    compile_unit_id: str,
    source_root: str,
    output_root_id: str,
    output_path: str,
    *,
    upstream_compile_output: tuple[str, str] | None = None,
) -> CompileUnitExpectation:
    return CompileUnitExpectation(
        compile_unit_id=compile_unit_id,
        domain_id="domain-app",
        build_system="maven",
        module_coordinate="com.example:app",
        source_set="main",
        language="java",
        adapter_status="supported",
        task_or_execution=f"compile:{compile_unit_id}",
        role="required",
        scope_disposition="active",
        source_roots=(source_root,),
        include_rules=(_source_rule(compile_unit_id, "**/*.java"),),
        exclude_rules=(),
        compiler_environment_spec=_compiler_environment(
            upstream_compile_output=upstream_compile_output
        ),
        output_roots=(
            OutputRootSpec(
                member_id=f"{output_root_id}-member",
                root_id=output_root_id,
                path=output_path,
                verification_mode="shared_owned_entries",
                output_requirement="nonempty",
            ),
        ),
        output_requirement="nonempty",
        output_requirement_basis="supported-java-compile",
        requested_lifecycle_basis="package",
        model_provenance="maven-effective-model",
    )


def _package_expectation() -> PackageUnitExpectation:
    return PackageUnitExpectation(
        package_unit_id="package-01",
        module_coordinate="com.example:app",
        task_or_execution="package",
        role="required",
        input_compile_unit_ids=("compile-app", "compile-base"),
        resource_roots=(
            _member_spec(
                "resource-root-01",
                "resource",
                "/workspace/project/src/main/resources",
                "maven-resource-roots",
                path_kind="tree",
            ),
        ),
        manifest_or_packaging_config=(
            _member_spec(
                "packaging-config-01",
                "packaging_input",
                "/workspace/project/app/pom.xml",
                "maven-effective-model",
            ),
        ),
        packaging_environment_spec=PackagingEnvironmentSpec(
            executable_path_basis=_path_basis(
                "/usr/bin/jar",
                "maven-jar-plugin:executable",
                member_role="packaging_tool",
            ),
            plugin_or_tool_members=(
                _member_spec(
                    "packaging-tool-01",
                    "packaging_tool",
                    "/opt/maven/plugins/maven-jar-plugin.jar",
                    "maven-plugin-resolution",
                ),
            ),
            external_dependency_members=(
                _member_spec(
                    "runtime-dependency-01",
                    "external_dependency",
                    "/workspace/.m2/repository/runtime.jar",
                    "maven-dependency-resolution",
                ),
            ),
            packaging_args=("--create",),
            toolchain_path_basis=_toolchain_basis(),
        ),
        output_roots=(
            OutputRootSpec(
                member_id="package-output-member-01",
                root_id="package-output-root-01",
                path="target",
                verification_mode="shared_owned_entries",
                output_requirement="nonempty",
            ),
        ),
        requested_artifacts=(
            RequestedArtifactSpec(
                artifact_id="artifact-01",
                root_id="package-output-root-01",
                path="target/app.jar",
                kind="jar",
            ),
        ),
        model_provenance="maven-effective-model",
    )


def _nonempty_build_model() -> EvaluatedBuildModelV1:
    base_compile = _compile_expectation(
        "compile-base", "base/src/main/java", "base-classes", "base/target/classes"
    )
    app_compile = _compile_expectation(
        "compile-app",
        "app/src/main/java",
        "app-classes",
        "app/target/classes",
        upstream_compile_output=("compile-base", "base-classes"),
    )
    return EvaluatedBuildModelV1(
        model_id=MODEL_ID,
        evidence_epoch_id=EPOCH_ID,
        scope_id=SCOPE_ID,
        goal_scope_revision=1,
        goal_scope_fingerprint=SHA_B,
        project_root=PROJECT_ROOT,
        model_revision=1,
        build_config_fingerprint=SHA_A,
        active_profiles_or_variants=("default",),
        build_config_members=(
            _member_spec(
                "build-config-pom-01",
                "config",
                "/workspace/project/pom.xml",
                "maven-effective-model",
            ),
        ),
        modules=(
            EvaluatedModuleV1(
                module_id="module-app",
                domain_id="domain-app",
                build_system="maven",
                module_coordinate="com.example:app",
                project_path="app",
                role="required",
                model_provenance="maven-effective-model",
            ),
        ),
        generator_units=(_generator_expectation(),),
        compile_units=(app_compile, base_compile),
        package_units=(_package_expectation(),),
        compile_dependency_edges=(
            CompileDependencyEdge(
                upstream_compile_unit_id="compile-base",
                upstream_output_root_id="base-classes",
                downstream_compile_unit_id="compile-app",
                dependency_basis="maven-reactor-classpath",
            ),
        ),
        generated_root_contracts=(
            GeneratedRootContract(
                generated_root_id="generated-root-01",
                output_root=OutputRootSpec(
                    member_id="generated-root-member-01",
                    root_id="generated-root-01",
                    path="target/generated-sources/protobuf",
                    verification_mode="shared_owned_entries",
                    output_requirement="nonempty",
                ),
                producer_unit_kind="generator",
                producer_unit_id="generator-01",
                consumer_unit_ids=("compile-app",),
                mode="later_compile_input",
                active_profile_or_variant="default",
                model_provenance="maven-effective-model",
            ),
        ),
        child_build_scopes=(
            ChildScopeExpectation(
                child_scope_id="child-scope-01",
                parent_scope_id=SCOPE_ID,
                child_project_root="/workspace/project/child",
                build_system="maven",
                ownership_basis="document-map:child-island",
            ),
        ),
        conflicts=(),
    )


def test_epoch_scope_and_census_are_closed_frozen_models() -> None:
    epoch = ContainerEvidenceEpochV1(
        evidence_epoch_id=EPOCH_ID,
        immutable_container_id=CONTAINER_ID,
        project_root=PROJECT_ROOT,
        created_at="2026-08-11T12:00:00Z",
        initial_source_state_fingerprint=SHA_A,
        baseline_output_root_digests=(),
    )
    scope = BuildGoalScopeV1(
        scope_id=SCOPE_ID,
        evidence_epoch_id=EPOCH_ID,
        authority_ref="request-01",
        scope_revision=1,
        predecessor_scope_hash=None,
        validation_target="project",
        requested_action="build",
        requested_lifecycle="package",
        requested_modules_or_domains=(),
        requested_profiles_or_variants=(),
        source_set_roles=("production",),
        scope_fingerprint=SHA_B,
    )
    census = JvmSourceCensusV1(
        census_id=CENSUS_ID,
        evidence_epoch_id=EPOCH_ID,
        project_root=PROJECT_ROOT,
        census_revision=1,
        source_state_fingerprint=SHA_A,
        sources=(_source(),),
        unreadable_roots=(),
        conflicts=(),
    )

    with pytest.raises(ValidationError, match="extra"):
        JvmSourceCandidate.model_validate({**_source_payload(), "surprise": True})
    with pytest.raises(ValidationError, match="frozen"):
        census.census_revision = 2  # type: ignore[misc]

    assert epoch.evidence_epoch_id == scope.evidence_epoch_id == census.evidence_epoch_id

    for value, id_field, wrong_id in (
        (
            epoch,
            "evidence_epoch_id",
            canonical_content_id(
                "container-evidence-epoch-v1",
                {
                    "immutable_container_id": "container-foreign",
                    "project_root": PROJECT_ROOT,
                },
            ),
        ),
        (
            scope,
            "scope_id",
            canonical_content_id(
                "build-goal-scope-v1",
                {"evidence_epoch_id": EPOCH_ID, "authority_ref": "request-foreign"},
            ),
        ),
        (
            census,
            "census_id",
            canonical_content_id(
                "jvm-source-census-v1",
                {
                    "evidence_epoch_id": EPOCH_ID,
                    "project_root": "/workspace/foreign",
                },
            ),
        ),
    ):
        with pytest.raises(ValidationError, match=f"{id_field}|identity"):
            value.model_copy(update={id_field: wrong_id})


def test_strict_integer_fields_reject_booleans_and_negative_values() -> None:
    base = {
        "census_id": CENSUS_ID,
        "evidence_epoch_id": EPOCH_ID,
        "project_root": PROJECT_ROOT,
        "source_state_fingerprint": SHA_A,
        "sources": (),
        "unreadable_roots": (),
        "conflicts": (),
    }
    with pytest.raises(ValidationError):
        JvmSourceCensusV1.model_validate({**base, "census_revision": True})
    with pytest.raises(ValidationError):
        JvmSourceCensusV1.model_validate({**base, "census_revision": -1})


def test_sha256_fields_reject_non_sha256_text() -> None:
    with pytest.raises(ValidationError, match="sha256"):
        JvmSourceCandidate.model_validate(_source_payload(sha256="not-a-digest"))


@pytest.mark.parametrize(
    "path",
    (
        "/absolute/App.java",
        "src/../App.java",
        "src/App.java\x00",
        "src/App.java\rforged",
        "src/App.java\nforged",
    ),
)
def test_relative_source_paths_reject_unsafe_or_noncanonical_values(path: str) -> None:
    with pytest.raises(ValidationError, match="path"):
        JvmSourceCandidate.model_validate(_source_payload(path=path))


def test_duplicate_json_keys_fail_before_the_parser_can_collapse_them() -> None:
    raw = json.dumps(_source_payload(), sort_keys=True, separators=(",", ":"))
    duplicate = raw[:-1] + ',"language":"kotlin"}'

    with pytest.raises(ValueError, match="duplicate JSON key"):
        validate_build_evidence_json(JvmSourceCandidate, duplicate)


def test_model_copy_revalidates_path_bounds() -> None:
    source = _source()

    with pytest.raises(ValidationError, match="path"):
        source.model_copy(update={"path": "x" * (BUILD_EVIDENCE_MAX_PATH_CHARS + 1)})
    with pytest.raises(ValidationError, match="path"):
        JvmSourceCandidate.model_construct(**_source_payload(path="../escape.java"))
    with pytest.raises(ValidationError, match="path"):
        source.copy(update={"path": "../legacy-copy-escape.java"})
    with pytest.raises(TypeError, match="strict|override"):
        JvmSourceCandidate.model_validate(_source_payload(), strict=False)
    with pytest.raises(TypeError, match="string|coerc"):
        JvmSourceCandidate.model_validate_strings(_source_payload())


def test_canonical_bytes_and_ids_ignore_input_mapping_order() -> None:
    left = JvmSourceCandidate.model_validate(_source_payload())
    right = JvmSourceCandidate.model_validate(dict(reversed(tuple(_source_payload().items()))))

    assert canonical_build_evidence_bytes(left) == canonical_build_evidence_bytes(right)
    assert canonical_build_evidence_sha256(left) == canonical_build_evidence_sha256(right)
    assert left.source_id == right.source_id
    with pytest.raises(ValidationError, match="source_id|identity"):
        left.model_copy(
            update={
                "source_id": canonical_content_id(
                    "jvm-source",
                    {"path": "src/main/java/Other.java", "sha256": SHA_A},
                )
            }
        )


def test_typed_collections_reject_duplicates_and_noncanonical_order() -> None:
    left = _source()
    right = JvmSourceCandidate.model_validate(
        _source_payload(path="src/main/java/example/Zed.java", sha256=SHA_B)
    )
    ordered = tuple(sorted((left, right), key=lambda item: item.source_id))
    census = JvmSourceCensusV1(
        census_id=CENSUS_ID,
        evidence_epoch_id=EPOCH_ID,
        project_root=PROJECT_ROOT,
        census_revision=1,
        source_state_fingerprint=SHA_A,
        sources=ordered,
        unreadable_roots=(),
        conflicts=(),
    )
    assert census.sources == ordered

    with pytest.raises(ValidationError, match="duplicate"):
        census.model_copy(update={"sources": (left, left)})
    with pytest.raises(ValidationError, match="order|sorted|canonical"):
        census.model_copy(update={"sources": tuple(reversed(ordered))})


def test_current_physical_snapshot_is_an_exact_requested_member_partition() -> None:
    current = CurrentPhysicalMemberV1(
        member_id="member-source",
        member_role="source",
        path_kind="file",
        path="/workspace/project/src/main/java/example/App.java",
        byte_count=12,
        sha256=SHA_A,
    )
    output = CurrentOutputEntryV1(
        root_id="classes-root",
        relative_path="example/App.class",
        kind="class",
        byte_count=24,
        sha256=SHA_B,
    )
    output_root = CurrentOutputRootV1(
        member_id="member-output-root",
        root_id="classes-root",
        path="/workspace/project/target/classes",
        verification_mode="exclusive_tree",
        tree_or_owned_entries_sha256=SHA_B,
        entry_count=1,
        scan_complete=True,
    )
    snapshot_identity: dict[str, object] = {
        "evidence_epoch_id": EPOCH_ID,
        "expectation_id": EXPECTATION_ID,
        "build_model_id": MODEL_ID,
        "requested_member_ids": (
            "member-missing",
            "member-output-root",
            "member-read",
            "member-source",
        ),
        "current_members": (current,),
        "missing_members": (
            MissingPhysicalMemberV1(
                member_id="member-missing", path="/workspace/project/deps/x.jar"
            ),
        ),
        "unreadable_members": (
            UnreadablePhysicalMemberV1(
                member_id="member-read",
                path="/opt/jdk/bin/javac",
                reason="permission_denied",
            ),
        ),
        "current_output_roots": (output_root,),
        "current_output_entries": (output,),
    }
    snapshot = CurrentPhysicalBasisSnapshotV1(
        physical_basis_snapshot_id=canonical_content_id(
            "current-physical-basis-v1", snapshot_identity
        ),
        **snapshot_identity,
        conflicts=(),
    )
    assert snapshot.current_output_entries[0].sha256 == SHA_B

    bad_partition = snapshot.model_dump(mode="python")
    bad_partition["missing_members"] = (
        MissingPhysicalMemberV1(member_id="member-source", path="/workspace/project/deps/x.jar"),
    )
    with pytest.raises(ValidationError, match="partition|duplicate|requested"):
        CurrentPhysicalBasisSnapshotV1.model_validate(_refresh_physical_basis_id(bad_partition))

    duplicate_requested = snapshot.model_dump(mode="python")
    duplicate_requested["requested_member_ids"] = (
        "member-missing",
        "member-output-root",
        "member-read",
        "member-source",
        "member-source",
    )
    with pytest.raises(ValidationError, match="requested_member_ids|duplicate"):
        CurrentPhysicalBasisSnapshotV1.model_validate(
            _refresh_physical_basis_id(duplicate_requested)
        )

    duplicate_current = snapshot.model_dump(mode="python")
    duplicate_current["current_members"] = (current, current)
    with pytest.raises(ValidationError, match="current_members|duplicate|partition"):
        CurrentPhysicalBasisSnapshotV1.model_validate(_refresh_physical_basis_id(duplicate_current))

    conflicting_current = snapshot.model_dump(mode="python")
    conflicting_current["current_members"] = (
        current,
        current.model_copy(update={"sha256": SHA_B}),
    )
    with pytest.raises(ValidationError, match="current_members|duplicate|member_id|conflict"):
        CurrentPhysicalBasisSnapshotV1.model_validate(
            _refresh_physical_basis_id(conflicting_current)
        )

    duplicate_root = snapshot.model_dump(mode="python")
    duplicate_root["current_output_roots"] = (output_root, output_root)
    with pytest.raises(ValidationError, match="output_roots|duplicate|partition"):
        CurrentPhysicalBasisSnapshotV1.model_validate(_refresh_physical_basis_id(duplicate_root))

    conflicting_root = snapshot.model_dump(mode="python")
    conflicting_root["current_output_roots"] = (
        output_root,
        output_root.model_copy(update={"tree_or_owned_entries_sha256": SHA_A}),
    )
    with pytest.raises(ValidationError, match="output_roots|duplicate|root_id|member_id|conflict"):
        CurrentPhysicalBasisSnapshotV1.model_validate(_refresh_physical_basis_id(conflicting_root))

    duplicate_output_entry = snapshot.model_dump(mode="python")
    duplicate_output_entry["current_output_roots"] = (
        output_root.model_copy(update={"entry_count": 2}),
    )
    duplicate_output_entry["current_output_entries"] = (output, output)
    with pytest.raises(ValidationError, match="output_entries|duplicate"):
        CurrentPhysicalBasisSnapshotV1.model_validate(
            _refresh_physical_basis_id(duplicate_output_entry)
        )

    conflicting_output_entry = snapshot.model_dump(mode="python")
    conflicting_output_entry["current_output_roots"] = (
        output_root.model_copy(update={"entry_count": 2}),
    )
    conflicting_output_entry["current_output_entries"] = (
        output,
        output.model_copy(update={"sha256": SHA_A}),
    )
    with pytest.raises(ValidationError, match="output_entries|duplicate|relative_path|conflict"):
        CurrentPhysicalBasisSnapshotV1.model_validate(
            _refresh_physical_basis_id(conflicting_output_entry)
        )

    incomplete_scan = snapshot.model_dump(mode="python")
    incomplete_scan["current_output_roots"] = (
        output_root.model_copy(update={"scan_complete": False}),
    )
    with pytest.raises(ValidationError, match="scan_complete|output"):
        CurrentPhysicalBasisSnapshotV1.model_validate(_refresh_physical_basis_id(incomplete_scan))

    missing_output_entry = snapshot.model_dump(mode="python")
    missing_output_entry["current_output_entries"] = ()
    with pytest.raises(ValidationError, match="output|entry_count|entries"):
        CurrentPhysicalBasisSnapshotV1.model_validate(
            _refresh_physical_basis_id(missing_output_entry)
        )

    extra_member = snapshot.model_dump(mode="python")
    extra_member["current_members"] = (
        current.model_copy(
            update={
                "member_id": "member-extra",
                "path": "/workspace/project/extra.txt",
            }
        ),
        current,
    )
    with pytest.raises(ValidationError, match="extra|partition|requested"):
        CurrentPhysicalBasisSnapshotV1.model_validate(_refresh_physical_basis_id(extra_member))

    unknown_output_root = snapshot.model_dump(mode="python")
    unknown_output_root["current_output_entries"] = (
        output.model_copy(update={"root_id": "unknown-root"}),
    )
    with pytest.raises(ValidationError, match="root|output"):
        CurrentPhysicalBasisSnapshotV1.model_validate(
            _refresh_physical_basis_id(unknown_output_root)
        )

    wrong_entry_count = snapshot.model_dump(mode="python")
    wrong_entry_count["current_output_roots"] = (output_root.model_copy(update={"entry_count": 2}),)
    with pytest.raises(ValidationError, match="entry_count|output"):
        CurrentPhysicalBasisSnapshotV1.model_validate(_refresh_physical_basis_id(wrong_entry_count))

    with pytest.raises(ValidationError, match="physical_basis_snapshot_id|identity"):
        snapshot.model_copy(
            update={"current_members": (current.model_copy(update={"sha256": SHA_B}),)}
        )


def test_complete_empty_evidence_set_is_distinct_from_unavailable_empty() -> None:
    complete = BuildEvidenceSetSnapshotV1.model_validate(_evidence_set_payload())
    unavailable_set = EvidenceRecordSetCompletenessV1(
        record_kind="compile_observation",
        status="unavailable",
        expected_record_ids=(),
        observed_record_ids=(),
        observed_record_hashes=(),
        tombstoned_record_ids=(),
        conflicts=("host_partition_unreadable",),
    )
    payload = _evidence_set_payload()
    payload["record_sets"] = tuple(
        unavailable_set if item.record_kind == "compile_observation" else item
        for item in payload["record_sets"]  # type: ignore[union-attr]
    )
    unavailable = BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(payload))

    complete_sets = {item.record_kind: item for item in complete.record_sets}
    unavailable_sets = {item.record_kind: item for item in unavailable.record_sets}
    assert set(complete_sets) == {
        "generator_observation",
        "compile_observation",
        "package_observation",
        "output_witness",
        "child_census",
        "child_expectation",
        "child_reconciliation",
    }
    assert complete.child_censuses == ()
    assert complete.child_expectations == ()
    assert complete_sets["compile_observation"].status == "complete"
    assert unavailable_sets["compile_observation"].status == "unavailable"

    missing_payload = _evidence_set_payload()
    missing_payload["record_sets"] = tuple(complete.record_sets[:-1])
    with pytest.raises(ValidationError, match="record_sets|missing"):
        BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(missing_payload))

    duplicate_payload = _evidence_set_payload()
    duplicate_payload["record_sets"] = complete.record_sets[:-1] + (complete.record_sets[0],)
    with pytest.raises(ValidationError, match="record_sets|duplicate"):
        BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(duplicate_payload))
    with pytest.raises(ValidationError, match="record_kind"):
        EvidenceRecordSetCompletenessV1(
            record_kind="unknown",
            status="complete",
            expected_record_ids=(),
            observed_record_ids=(),
            observed_record_hashes=(),
            tombstoned_record_ids=(),
            conflicts=(),
        )


def test_nonempty_record_set_binds_ids_to_host_authorized_raw_hashes() -> None:
    record = EvidenceRecordDigestV1(
        record_id=canonical_content_id(
            "compile-observation-v1",
            {
                "evidence_epoch_id": EPOCH_ID,
                "run_id": "run-01",
                "receipt_id": "receipt-01",
                "contract_id": "contract-01",
                "compile_unit_id": "compile-01",
            },
        ),
        raw_sha256=SHA_A,
    )
    complete = EvidenceRecordSetCompletenessV1(
        record_kind="compile_observation",
        status="complete",
        expected_record_ids=(record.record_id,),
        observed_record_ids=(record.record_id,),
        observed_record_hashes=(record,),
        tombstoned_record_ids=(),
        conflicts=(),
    )
    assert complete.observed_record_hashes == (record,)

    with pytest.raises(ValidationError, match="expected|observed"):
        complete.model_copy(update={"expected_record_ids": ("other-observation",)})
    with pytest.raises(ValidationError, match="hash|observed"):
        complete.model_copy(update={"observed_record_hashes": ()})
    with pytest.raises(ValidationError, match="duplicate"):
        complete.model_copy(update={"observed_record_hashes": (record, record)})
    with pytest.raises(ValidationError, match="tombstone|complete"):
        complete.model_copy(update={"tombstoned_record_ids": (record.record_id,)})

    tombstoned = complete.model_copy(
        update={
            "status": "tombstoned",
            "observed_record_ids": (),
            "observed_record_hashes": (),
            "tombstoned_record_ids": (record.record_id,),
            "conflicts": ("host_revoked_record",),
        }
    )
    assert tombstoned.status == "tombstoned"

    set_mismatch = EvidenceRecordSetCompletenessV1(
        record_kind="compile_observation",
        status="set_mismatch",
        expected_record_ids=(record.record_id,),
        observed_record_ids=(),
        observed_record_hashes=(),
        tombstoned_record_ids=(),
        conflicts=("host_expected_set_mismatch",),
    )
    assert set_mismatch.status == "set_mismatch"

    conflict = EvidenceRecordSetCompletenessV1(
        record_kind="compile_observation",
        status="conflict",
        expected_record_ids=(record.record_id,),
        observed_record_ids=(record.record_id,),
        observed_record_hashes=(record,),
        tombstoned_record_ids=(),
        conflicts=("same_id_different_bytes",),
    )
    assert conflict.status == "conflict"

    with pytest.raises(ValidationError, match="conflicts|set_mismatch"):
        set_mismatch.model_copy(update={"conflicts": ()})
    with pytest.raises(ValidationError, match="conflicts|conflict"):
        conflict.model_copy(update={"conflicts": ()})


def test_evidence_snapshot_nonempty_typed_objects_match_complete_record_ids() -> None:
    observation = _compile_observation()
    host_raw = json.dumps(observation.model_dump(mode="json"), indent=2).encode("utf-8")
    record = EvidenceRecordDigestV1(
        record_id=observation.observation_id,
        raw_sha256=hashlib.sha256(host_raw).hexdigest(),
    )
    assert record.raw_sha256 != canonical_build_evidence_sha256(observation)
    payload = _evidence_set_payload()
    payload["admitted_run_ids"] = (observation.run_id,)
    payload["compile_observations"] = (observation,)
    payload["record_sets"] = tuple(
        (
            EvidenceRecordSetCompletenessV1(
                record_kind=item.record_kind,
                status="complete",
                expected_record_ids=(record.record_id,),
                observed_record_ids=(record.record_id,),
                observed_record_hashes=(record,),
                tombstoned_record_ids=(),
                conflicts=(),
            )
            if item.record_kind == "compile_observation"
            else item
        )
        for item in payload["record_sets"]  # type: ignore[union-attr]
    )
    snapshot = BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(payload))
    assert snapshot.compile_observations == (observation,)

    missing_object = snapshot.model_dump(mode="python")
    missing_object["compile_observations"] = ()
    with pytest.raises(ValidationError, match="compile_observation|record"):
        BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(missing_object))

    duplicate_object = snapshot.model_dump(mode="python")
    duplicate_object["compile_observations"] = (
        observation,
        observation.model_copy(update={"outcome": "up_to_date"}),
    )
    with pytest.raises(ValidationError, match="duplicate|observation_id"):
        BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(duplicate_object))

    with pytest.raises(ValidationError, match="snapshot_id|identity"):
        snapshot.model_copy(update={"admitted_run_ids": ("run-01", "run-02")})

    unadmitted = snapshot.model_dump(mode="python")
    unadmitted["admitted_run_ids"] = ()
    with pytest.raises(ValidationError, match="admitted|run"):
        BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(unadmitted))

    duplicate_runs = snapshot.model_dump(mode="python")
    duplicate_runs["admitted_run_ids"] = ("run-01", "run-01")
    with pytest.raises(ValidationError, match="admitted_run_ids|duplicate"):
        BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(duplicate_runs))

    unordered_runs = snapshot.model_dump(mode="python")
    unordered_runs["admitted_run_ids"] = ("run-02", "run-01")
    with pytest.raises(ValidationError, match="admitted_run_ids|order|sorted"):
        BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(unordered_runs))


def test_generator_package_and_witness_sets_bind_nonempty_typed_objects() -> None:
    generator = _generator_observation()
    compile_observation = _compile_observation()
    package = _package_observation()
    witness = _compile_output_witness(compile_observation)
    objects = {
        "generator_observation": (generator.observation_id, generator),
        "compile_observation": (
            compile_observation.observation_id,
            compile_observation,
        ),
        "package_observation": (package.observation_id, package),
        "output_witness": (witness.witness_id, witness),
    }
    payload = _evidence_set_payload()
    payload.update(
        {
            "admitted_run_ids": ("run-01",),
            "generator_observations": (generator,),
            "compile_observations": (compile_observation,),
            "package_observations": (package,),
            "output_witnesses": (witness,),
        }
    )
    payload["record_sets"] = tuple(
        (
            EvidenceRecordSetCompletenessV1(
                record_kind=item.record_kind,
                status="complete",
                expected_record_ids=(objects[item.record_kind][0],),
                observed_record_ids=(objects[item.record_kind][0],),
                observed_record_hashes=(
                    EvidenceRecordDigestV1(
                        record_id=objects[item.record_kind][0],
                        raw_sha256=hashlib.sha256(
                            json.dumps(
                                objects[item.record_kind][1].model_dump(mode="json"),
                                indent=2,
                            ).encode("utf-8")
                        ).hexdigest(),
                    ),
                ),
                tombstoned_record_ids=(),
                conflicts=(),
            )
            if item.record_kind in objects
            else item
        )
        for item in payload["record_sets"]  # type: ignore[union-attr]
    )
    snapshot = BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(payload))
    assert snapshot.generator_observations == (generator,)
    assert snapshot.package_observations == (package,)
    assert snapshot.output_witnesses == (witness,)

    orphan_witness = snapshot.model_dump(mode="python")
    orphan_witness["compile_observations"] = ()
    orphan_witness["record_sets"] = tuple(
        (
            _complete_set("compile_observation")
            if item.record_kind == "compile_observation"
            else item
        )
        for item in snapshot.record_sets
    )
    with pytest.raises(ValidationError, match="witness|producer|observation"):
        BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(orphan_witness))

    mismatched_witness = witness.model_copy(update={"producer_unit_identity_hash": SHA_B})
    mismatched_witness_payload = snapshot.model_dump(mode="python")
    mismatched_witness_payload["output_witnesses"] = (mismatched_witness,)
    mismatched_witness_payload["record_sets"] = tuple(
        (
            item.model_copy(
                update={
                    "observed_record_hashes": (
                        EvidenceRecordDigestV1(
                            record_id=mismatched_witness.witness_id,
                            raw_sha256=canonical_build_evidence_sha256(mismatched_witness),
                        ),
                    )
                }
            )
            if item.record_kind == "output_witness"
            else item
        )
        for item in snapshot.record_sets
    )
    with pytest.raises(ValidationError, match="witness|unit|identity|producer"):
        BuildEvidenceSetSnapshotV1.model_validate(
            _refresh_evidence_set_id(mismatched_witness_payload)
        )

    mismatched_root = witness.roots[0].model_copy(
        update={"verification_mode": "shared_owned_entries"}
    )
    mismatched_root_witness = witness.model_copy(
        update={
            "roots": (mismatched_root,),
            "aggregate_sha256": physical_output_aggregate_sha256(
                (mismatched_root,), witness.entries
            ),
        }
    )
    mismatched_root_payload = snapshot.model_dump(mode="python")
    mismatched_root_payload["output_witnesses"] = (mismatched_root_witness,)
    mismatched_root_payload["record_sets"] = tuple(
        (
            item.model_copy(
                update={
                    "observed_record_hashes": (
                        EvidenceRecordDigestV1(
                            record_id=mismatched_root_witness.witness_id,
                            raw_sha256=canonical_build_evidence_sha256(mismatched_root_witness),
                        ),
                    )
                }
            )
            if item.record_kind == "output_witness"
            else item
        )
        for item in snapshot.record_sets
    )
    with pytest.raises(ValidationError, match="witness|root|observation|verification_mode"):
        BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(mismatched_root_payload))

    object_fields = {
        "generator_observation": "generator_observations",
        "compile_observation": "compile_observations",
        "package_observation": "package_observations",
        "output_witness": "output_witnesses",
    }
    for record_kind, field in object_fields.items():
        missing_object = snapshot.model_dump(mode="python")
        missing_object[field] = ()
        with pytest.raises(ValidationError, match=f"{record_kind}|record"):
            BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(missing_object))

        mismatched_hash_row = snapshot.model_dump(mode="python")
        mismatched_hash_row["record_sets"] = tuple(
            (
                item.model_copy(
                    update={
                        "expected_record_ids": ("foreign-record-id",),
                        "observed_record_ids": ("foreign-record-id",),
                        "observed_record_hashes": (
                            EvidenceRecordDigestV1(
                                record_id="foreign-record-id",
                                raw_sha256=SHA_A,
                            ),
                        ),
                    }
                )
                if item.record_kind == record_kind
                else item
            )
            for item in snapshot.record_sets
        )
        with pytest.raises(ValidationError, match=f"{record_kind}|hash|observed"):
            BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(mismatched_hash_row))


def test_child_closure_carries_census_expectation_and_reconciliation_cross_links() -> None:
    child_root = "/workspace/project/child"
    child_census_id = canonical_content_id(
        "jvm-source-census-v1",
        {"evidence_epoch_id": EPOCH_ID, "project_root": child_root},
    )
    child_scope_id = canonical_content_id(
        "build-goal-scope-v1",
        {"evidence_epoch_id": EPOCH_ID, "authority_ref": "child:scope-01"},
    )
    child_model_id = canonical_content_id(
        "evaluated-build-model-v1",
        {
            "evidence_epoch_id": EPOCH_ID,
            "scope_id": child_scope_id,
            "project_root": child_root,
        },
    )
    child_census = JvmSourceCensusV1(
        census_id=child_census_id,
        evidence_epoch_id=EPOCH_ID,
        project_root=child_root,
        census_revision=1,
        source_state_fingerprint=SHA_A,
        sources=(),
        unreadable_roots=(),
        conflicts=(),
    )
    child_expectation_identity: dict[str, object] = {
        "evidence_epoch_id": EPOCH_ID,
        "census_id": child_census.census_id,
        "scope_id": child_scope_id,
        "model_id": child_model_id,
        "scope_fingerprint": SHA_B,
        "source_state_fingerprint": SHA_A,
        "build_config_fingerprint": SHA_B,
        "required_java_edges": (),
        "source_classifications": (),
        "unclassified_source_ids": (),
        "required_generator_unit_ids": (),
        "required_compile_unit_ids": (),
        "required_package_unit_ids": (),
        "unsupported_active_unit_ids": (),
        "required_child_scope_ids": (),
        "delegation_edges": (),
    }
    child_expectation = JvmBuildExpectationV1(
        expectation_id=canonical_content_id("jvm-build-expectation-v1", child_expectation_identity),
        **child_expectation_identity,
        conflicts=(),
    )
    reconciliation_payload = _empty_reconciliation_payload()
    reconciliation_payload["input_set_digest"] = SHA_B
    reconciliation_payload.update(
        {
            "reconciliation_id": canonical_content_id(
                "build-reconciliation-v1",
                {"evidence_epoch_id": EPOCH_ID, "input_set_digest": SHA_B},
            ),
            "source_census_id": child_census.census_id,
            "goal_scope_id": child_expectation.scope_id,
            "build_model_id": child_expectation.model_id,
            "expectation_id": child_expectation.expectation_id,
        }
    )
    child_reconciliation = BuildReconciliationV1.model_validate(reconciliation_payload)

    payload = _evidence_set_payload()
    payload.update(
        {
            "child_censuses": (child_census,),
            "child_expectations": (child_expectation,),
            "child_reconciliations": (child_reconciliation,),
        }
    )
    child_objects = {
        "child_census": (child_census.census_id, child_census),
        "child_expectation": (child_expectation.expectation_id, child_expectation),
        "child_reconciliation": (
            child_reconciliation.reconciliation_id,
            child_reconciliation,
        ),
    }
    payload["record_sets"] = tuple(
        (
            EvidenceRecordSetCompletenessV1(
                record_kind=item.record_kind,
                status="complete",
                expected_record_ids=(child_objects[item.record_kind][0],),
                observed_record_ids=(child_objects[item.record_kind][0],),
                observed_record_hashes=(
                    EvidenceRecordDigestV1(
                        record_id=child_objects[item.record_kind][0],
                        raw_sha256=canonical_build_evidence_sha256(
                            child_objects[item.record_kind][1]
                        ),
                    ),
                ),
                tombstoned_record_ids=(),
                conflicts=(),
            )
            if item.record_kind in child_objects
            else item
        )
        for item in payload["record_sets"]  # type: ignore[union-attr]
    )
    snapshot = BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(payload))
    assert snapshot.child_reconciliations == (child_reconciliation,)

    drifted_payload = child_expectation.model_dump(mode="python")
    drifted_payload["census_id"] = "foreign-census"
    drifted = JvmBuildExpectationV1.model_validate(_refresh_expectation_id(drifted_payload))
    bad_snapshot = snapshot.model_dump(mode="python")
    bad_snapshot["child_expectations"] = (drifted,)
    bad_snapshot["record_sets"] = tuple(
        (
            item.model_copy(
                update={
                    "expected_record_ids": (drifted.expectation_id,),
                    "observed_record_ids": (drifted.expectation_id,),
                    "observed_record_hashes": (
                        EvidenceRecordDigestV1(
                            record_id=drifted.expectation_id,
                            raw_sha256=canonical_build_evidence_sha256(drifted),
                        ),
                    ),
                }
            )
            if item.record_kind == "child_expectation"
            else item
        )
        for item in snapshot.record_sets
    )
    with pytest.raises(ValidationError, match="child|census|cross"):
        BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(bad_snapshot))

    drifted_reconciliation_payload = child_reconciliation.model_dump(mode="python")
    drifted_reconciliation_payload.update(
        {
            "source_census_id": "foreign-census",
            "input_set_digest": SHA_A,
            "reconciliation_id": canonical_content_id(
                "build-reconciliation-v1",
                {"evidence_epoch_id": EPOCH_ID, "input_set_digest": SHA_A},
            ),
        }
    )
    drifted_reconciliation = BuildReconciliationV1.model_validate(drifted_reconciliation_payload)
    bad_reconciliation_snapshot = snapshot.model_dump(mode="python")
    bad_reconciliation_snapshot["child_reconciliations"] = (drifted_reconciliation,)
    bad_reconciliation_snapshot["record_sets"] = tuple(
        (
            item.model_copy(
                update={
                    "expected_record_ids": (drifted_reconciliation.reconciliation_id,),
                    "observed_record_ids": (drifted_reconciliation.reconciliation_id,),
                    "observed_record_hashes": (
                        EvidenceRecordDigestV1(
                            record_id=drifted_reconciliation.reconciliation_id,
                            raw_sha256=canonical_build_evidence_sha256(drifted_reconciliation),
                        ),
                    ),
                }
            )
            if item.record_kind == "child_reconciliation"
            else item
        )
        for item in snapshot.record_sets
    )
    with pytest.raises(ValidationError, match="child|census|cross"):
        BuildEvidenceSetSnapshotV1.model_validate(
            _refresh_evidence_set_id(bad_reconciliation_snapshot)
        )

    child_fields = {
        "child_census": "child_censuses",
        "child_expectation": "child_expectations",
        "child_reconciliation": "child_reconciliations",
    }
    for record_kind, field in child_fields.items():
        missing_object = snapshot.model_dump(mode="python")
        missing_object[field] = ()
        with pytest.raises(ValidationError, match=f"{record_kind}|child|record"):
            BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(missing_object))

        mismatched_hash_row = snapshot.model_dump(mode="python")
        mismatched_hash_row["record_sets"] = tuple(
            (
                item.model_copy(
                    update={
                        "expected_record_ids": ("foreign-record-id",),
                        "observed_record_ids": ("foreign-record-id",),
                        "observed_record_hashes": (
                            EvidenceRecordDigestV1(
                                record_id="foreign-record-id",
                                raw_sha256=SHA_A,
                            ),
                        ),
                    }
                )
                if item.record_kind == record_kind
                else item
            )
            for item in snapshot.record_sets
        )
        with pytest.raises(ValidationError, match=f"{record_kind}|hash|observed"):
            BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(mismatched_hash_row))


def test_generator_and_package_expectations_require_full_environment_closure() -> None:
    assert set(GeneratorEnvironmentSpec.model_fields) == {
        "executable_path_basis",
        "tool_or_plugin_members",
        "dependency_members",
        "generator_args",
        "toolchain_path_basis",
    }
    assert set(PackagingEnvironmentSpec.model_fields) == {
        "executable_path_basis",
        "plugin_or_tool_members",
        "external_dependency_members",
        "packaging_args",
        "toolchain_path_basis",
    }
    assert set(CompilerEnvironmentSpec.model_fields) == {
        "executable_path_basis",
        "compiler_version",
        "compiler_args",
        "classpath_members",
        "toolchain_path_basis",
        "resolution_provenance",
    }
    assert CompileUnitExpectation.model_fields["compiler_environment_spec"].is_required()
    assert GeneratorUnitExpectation.model_fields["generator_environment_spec"].is_required()
    assert PackageUnitExpectation.model_fields["packaging_environment_spec"].is_required()
    assert JvmBuildExpectationV1.model_fields["required_package_unit_ids"].is_required()

    for environment in (
        _compiler_environment(),
        _generator_expectation().generator_environment_spec,
        _package_expectation().packaging_environment_spec,
    ):
        for field in type(environment).model_fields:
            missing = environment.model_dump(mode="python")
            missing.pop(field)
            with pytest.raises(ValidationError, match=f"{field}|required"):
                type(environment).model_validate(missing)


def test_nested_build_model_contracts_are_closed_and_nonempty() -> None:
    assert set(BuildPathBasis.model_fields) == {
        "member_id",
        "member_role",
        "path",
        "path_kind",
        "resolution_provenance",
    }
    assert set(ResolvedMemberSpec.model_fields) == {
        "member_id",
        "member_role",
        "path_kind",
        "path",
        "coordinate",
        "upstream_unit_id",
        "upstream_root_id",
        "resolution_provenance",
    }
    assert set(CompileDependencyEdge.model_fields) == {
        "upstream_compile_unit_id",
        "upstream_output_root_id",
        "downstream_compile_unit_id",
        "dependency_basis",
    }
    assert set(GeneratedRootContract.model_fields) == {
        "generated_root_id",
        "output_root",
        "producer_unit_kind",
        "producer_unit_id",
        "consumer_unit_ids",
        "mode",
        "active_profile_or_variant",
        "model_provenance",
    }
    assert set(ChildScopeExpectation.model_fields) == {
        "child_scope_id",
        "parent_scope_id",
        "child_project_root",
        "build_system",
        "ownership_basis",
    }

    model = _nonempty_build_model()
    assert len(model.generator_units) == 1
    assert len(model.compile_units) == 2
    assert len(model.package_units) == 1
    assert len(model.compile_dependency_edges) == 1
    assert len(model.generated_root_contracts) == 1
    assert len(model.child_build_scopes) == 1

    unknown_dependency = model.compile_dependency_edges[0].model_copy(
        update={"downstream_compile_unit_id": "compile-unknown"}
    )
    with pytest.raises(ValidationError, match="dependency|compile-unknown|cross"):
        model.model_copy(update={"compile_dependency_edges": (unknown_dependency,)})

    unknown_consumer = model.generated_root_contracts[0].model_copy(
        update={"consumer_unit_ids": ("compile-unknown",)}
    )
    with pytest.raises(ValidationError, match="generated|consumer|compile-unknown"):
        model.model_copy(update={"generated_root_contracts": (unknown_consumer,)})

    cyclic_child = model.child_build_scopes[0].model_copy(update={"child_scope_id": SCOPE_ID})
    with pytest.raises(ValidationError, match="child|self|cycle"):
        model.model_copy(update={"child_build_scopes": (cyclic_child,)})

    unknown_upstream = model.compile_dependency_edges[0].model_copy(
        update={"upstream_compile_unit_id": "compile-unknown"}
    )
    with pytest.raises(ValidationError, match="dependency|upstream|compile-unknown"):
        model.model_copy(update={"compile_dependency_edges": (unknown_upstream,)})

    unknown_upstream_root = model.compile_dependency_edges[0].model_copy(
        update={"upstream_output_root_id": "unknown-root"}
    )
    with pytest.raises(ValidationError, match="dependency|root|unknown-root"):
        model.model_copy(update={"compile_dependency_edges": (unknown_upstream_root,)})

    bad_package = model.package_units[0].model_copy(
        update={"input_compile_unit_ids": ("compile-unknown",)}
    )
    with pytest.raises(ValidationError, match="package|compile-unknown|input"):
        model.model_copy(update={"package_units": (bad_package,)})

    bad_generator = model.generator_units[0].model_copy(
        update={"generated_root_ids": ("unbound-generated-root",)}
    )
    with pytest.raises(ValidationError, match="generator|generated_root|unbound"):
        model.model_copy(update={"generator_units": (bad_generator,)})

    unknown_producer = model.generated_root_contracts[0].model_copy(
        update={"producer_unit_id": "generator-unknown"}
    )
    with pytest.raises(ValidationError, match="producer|generator-unknown"):
        model.model_copy(update={"generated_root_contracts": (unknown_producer,)})

    inactive_contract = model.generated_root_contracts[0].model_copy(
        update={"active_profile_or_variant": "inactive"}
    )
    with pytest.raises(ValidationError, match="profile|variant|inactive"):
        model.model_copy(update={"generated_root_contracts": (inactive_contract,)})

    same_round = model.generated_root_contracts[0].model_copy(
        update={
            "producer_unit_kind": "compile",
            "producer_unit_id": "compile-app",
            "consumer_unit_ids": ("compile-app",),
            "mode": "same_round_intermediate",
        }
    )
    same_round_model = model.model_copy(
        update={
            "generator_units": (),
            "generated_root_contracts": (same_round,),
        }
    )
    assert same_round_model.generated_root_contracts == (same_round,)

    same_round_other_consumer = same_round.model_copy(
        update={"consumer_unit_ids": ("compile-base",)}
    )
    with pytest.raises(ValidationError, match="same_round|producer|consumer"):
        same_round_model.model_copy(
            update={"generated_root_contracts": (same_round_other_consumer,)}
        )

    with pytest.raises(ValidationError, match="compile_units|duplicate"):
        model.model_copy(
            update={
                "compile_units": (
                    model.compile_units[0],
                    model.compile_units[0],
                    model.compile_units[1],
                )
            }
        )

    conflicting_compile_unit = model.compile_units[0].model_copy(
        update={"task_or_execution": "compile-conflicting"}
    )
    with pytest.raises(ValidationError, match="compile_units|duplicate|compile_unit_id|conflict"):
        model.model_copy(
            update={
                "compile_units": (
                    model.compile_units[0],
                    conflicting_compile_unit,
                    model.compile_units[1],
                )
            }
        )

    duplicate_root = model.compile_units[0].output_roots[0]
    with pytest.raises(ValidationError, match="output_roots|duplicate"):
        model.compile_units[0].model_copy(update={"output_roots": (duplicate_root, duplicate_root)})

    with pytest.raises(ValidationError, match="modules|duplicate"):
        model.model_copy(update={"modules": (model.modules[0], model.modules[0])})

    for collection, unit in (
        ("generator_units", model.generator_units[0]),
        ("compile_units", model.compile_units[0]),
        ("package_units", model.package_units[0]),
    ):
        foreign_module = unit.model_copy(update={"module_coordinate": "com.example:foreign"})
        replacement = tuple(
            foreign_module if candidate is unit else candidate
            for candidate in getattr(model, collection)
        )
        with pytest.raises(ValidationError, match="module_coordinate|foreign|module"):
            model.model_copy(update={collection: replacement})


def test_resolved_member_spec_requires_exactly_one_resolution_form() -> None:
    path_member = _member_spec(
        "path-member",
        "classpath",
        "/workspace/project/deps/path.jar",
        "resolved-path",
    )
    coordinate_member = ResolvedMemberSpec(
        member_id="coordinate-member",
        member_role="classpath",
        path_kind="file",
        path="/workspace/.m2/repository/com/example/dep/1.0/dep-1.0.jar",
        coordinate="com.example:dep:1.0",
        upstream_unit_id=None,
        upstream_root_id=None,
        resolution_provenance="maven-coordinate",
    )
    upstream_member = ResolvedMemberSpec(
        member_id="upstream-member",
        member_role="upstream_output",
        path_kind="tree",
        path=None,
        coordinate=None,
        upstream_unit_id="compile-base",
        upstream_root_id="base-classes",
        resolution_provenance="reactor-output",
    )
    assert path_member.path is not None
    assert coordinate_member.coordinate is not None
    assert upstream_member.upstream_root_id == "base-classes"

    base = upstream_member.model_dump(mode="python")
    with pytest.raises(ValidationError, match="exactly one|resolution|path|coordinate"):
        ResolvedMemberSpec.model_validate(
            {
                **base,
                "path": "/workspace/project/deps/also.jar",
            }
        )
    with pytest.raises(ValidationError, match="upstream|root|pair"):
        ResolvedMemberSpec.model_validate({**base, "upstream_root_id": None})
    with pytest.raises(ValidationError, match="resolution|path|coordinate|upstream"):
        ResolvedMemberSpec.model_validate(
            {
                **base,
                "upstream_unit_id": None,
                "upstream_root_id": None,
            }
        )

    with pytest.raises(ValidationError, match="coordinate|resolved|path"):
        coordinate_member.model_copy(update={"path": None})


@pytest.mark.parametrize(
    "pattern",
    (
        "/workspace/**/*.java",
        "../src/**/*.java",
        "src/../main/**/*.java",
        "src\\main\\**\\*.java",
        "src/**suffix/*.java",
        "src/[abc/*.java",
    ),
)
def test_source_pattern_rule_has_a_closed_safe_posix_glob_grammar(pattern: str) -> None:
    projection = {
        "compile_unit_id": "compile-app",
        "dialect": "posix_glob_v1",
        "pattern": pattern,
        "model_provenance": "maven-effective-model",
    }
    with pytest.raises(ValidationError, match="pattern|glob|relative|unsafe|canonical"):
        SourcePatternRuleV1(
            rule_id=canonical_content_id("source-pattern-rule-v1", projection),
            **projection,
        )


def test_source_pattern_rules_bind_owner_scope_disposition_and_primary_id() -> None:
    rule = _source_rule("compile-app", "**/*.java")
    assert rule.dialect == "posix_glob_v1"

    with pytest.raises(ValidationError, match="dialect"):
        rule.model_copy(update={"dialect": "future_glob_v2"})
    with pytest.raises(ValidationError, match="rule_id|identity"):
        rule.model_copy(update={"rule_id": "source-rule-foreign"})

    compile_unit = _compile_expectation(
        "compile-app",
        "app/src/main/java",
        "app-classes",
        "app/target/classes",
    )
    foreign_rule = _source_rule("compile-foreign", "**/*.java")
    with pytest.raises(ValidationError, match="rule|owner|compile-foreign"):
        compile_unit.model_copy(update={"include_rules": (foreign_rule,)})
    with pytest.raises(ValidationError, match="required|active|scope"):
        compile_unit.model_copy(update={"scope_disposition": "inactive_profile"})
    with pytest.raises(ValidationError, match="non-required|active|scope"):
        compile_unit.model_copy(update={"role": "optional"})

    inactive = compile_unit.model_copy(
        update={"role": "optional", "scope_disposition": "inactive_profile"}
    )
    assert inactive.scope_disposition == "inactive_profile"


def test_package_and_generated_outputs_are_closed_root_owned_contracts() -> None:
    package = _package_expectation()
    with pytest.raises(ValidationError, match="required|output|artifact"):
        package.model_copy(update={"output_roots": (), "requested_artifacts": ()})

    unknown_root_artifact = package.requested_artifacts[0].model_copy(
        update={"root_id": "package-output-unknown"}
    )
    with pytest.raises(ValidationError, match="artifact|root|unknown"):
        package.model_copy(update={"requested_artifacts": (unknown_root_artifact,)})

    escaped_artifact = package.requested_artifacts[0].model_copy(
        update={"path": "elsewhere/app.jar"}
    )
    with pytest.raises(ValidationError, match="artifact|contained|output root"):
        package.model_copy(update={"requested_artifacts": (escaped_artifact,)})

    generated = _nonempty_build_model().generated_root_contracts[0]
    with pytest.raises(ValidationError, match="generated_root_id|output root|match"):
        generated.model_copy(
            update={
                "output_root": generated.output_root.model_copy(
                    update={"root_id": "generated-root-foreign"}
                )
            }
        )
    with pytest.raises(ValidationError, match="output_requirement"):
        generated.model_copy(
            update={
                "output_root": generated.output_root.model_copy(
                    update={"output_requirement": "none"}
                )
            }
        )

    with pytest.raises(ValidationError, match="required generator|output root"):
        _generator_expectation().model_copy(update={"generated_root_ids": ()})


def test_output_root_path_overlap_requires_shared_outer_ownership() -> None:
    model = _nonempty_build_model()
    package = model.package_units[0]

    def package_at(path: str, verification_mode: str) -> PackageUnitExpectation:
        root = package.output_roots[0].model_copy(
            update={"path": path, "verification_mode": verification_mode}
        )
        artifact = package.requested_artifacts[0].model_copy(update={"path": f"{path}/app.jar"})
        return package.model_copy(
            update={"output_roots": (root,), "requested_artifacts": (artifact,)}
        )

    shared_same_path = package_at("app/target/classes", "shared_owned_entries")
    assert model.model_copy(update={"package_units": (shared_same_path,)})

    exclusive_same_path = package_at("app/target/classes", "exclusive_tree")
    with pytest.raises(ValidationError, match="co-located|shared_owned_entries"):
        model.model_copy(update={"package_units": (exclusive_same_path,)})

    shared_outer = package_at("app/target", "shared_owned_entries")
    assert model.model_copy(update={"package_units": (shared_outer,)})

    exclusive_outer = package_at("app/target", "exclusive_tree")
    with pytest.raises(ValidationError, match="outer|nested|shared_owned_entries"):
        model.model_copy(update={"package_units": (exclusive_outer,)})


def test_build_model_rejects_root_collisions_and_member_semantic_rebinding() -> None:
    model = _nonempty_build_model()
    package = model.package_units[0]
    colliding_root = package.output_roots[0].model_copy(update={"root_id": "app-classes"})
    colliding_artifact = package.requested_artifacts[0].model_copy(
        update={"root_id": "app-classes"}
    )
    colliding_package = package.model_copy(
        update={
            "output_roots": (colliding_root,),
            "requested_artifacts": (colliding_artifact,),
        }
    )
    with pytest.raises(ValidationError, match="output root|duplicate|collision"):
        model.model_copy(update={"package_units": (colliding_package,)})

    packaging_config = package.manifest_or_packaging_config[0]
    rebound_config = _member_spec(
        packaging_config.member_id,
        "config",
        "/workspace/project/alternate-pom.xml",
        "maven-effective-model",
    )
    with pytest.raises(ValidationError, match="member_id|semantic|body|rebind"):
        model.model_copy(update={"build_config_members": (rebound_config,)})

    coordinate_left = ResolvedMemberSpec(
        member_id="coordinate-shared",
        member_role="classpath",
        path_kind="file",
        path="/workspace/.m2/repository/dep/1/dep-1.jar",
        coordinate="example:dep:1",
        upstream_unit_id=None,
        upstream_root_id=None,
        resolution_provenance="maven-coordinate",
    )
    coordinate_right = coordinate_left.model_copy(
        update={"path": "/workspace/.m2/repository/dep/2/dep-2.jar"}
    )
    app, base = model.compile_units
    app_environment = app.compiler_environment_spec.model_copy(
        update={
            "classpath_members": (
                app.compiler_environment_spec.classpath_members[0],
                coordinate_right,
            )
        }
    )
    base_environment = base.compiler_environment_spec.model_copy(
        update={"classpath_members": (coordinate_left,)}
    )
    with pytest.raises(ValidationError, match="member_id|semantic|body|rebind"):
        model.model_copy(
            update={
                "compile_units": (
                    app.model_copy(update={"compiler_environment_spec": app_environment}),
                    base.model_copy(update={"compiler_environment_spec": base_environment}),
                )
            }
        )


def test_compile_dependency_edges_and_upstream_classpath_are_bijective() -> None:
    model = _nonempty_build_model()
    with pytest.raises(ValidationError, match="upstream classpath|dependency edge|matching"):
        model.model_copy(update={"compile_dependency_edges": ()})

    app, base = model.compile_units
    replacement_member = _member_spec(
        "resolved-base-jar",
        "classpath",
        "/workspace/project/base/target/base.jar",
        "maven-reactor-path-only",
    )
    app_without_upstream = app.model_copy(
        update={
            "compiler_environment_spec": app.compiler_environment_spec.model_copy(
                update={"classpath_members": (replacement_member,)}
            )
        }
    )
    with pytest.raises(ValidationError, match="dependency edge|matching downstream|classpath"):
        model.model_copy(update={"compile_units": (app_without_upstream, base)})


def test_model_and_expectation_bind_the_current_goal_scope_head() -> None:
    model = _nonempty_build_model()
    assert model.goal_scope_revision == 1
    assert model.goal_scope_fingerprint == SHA_B
    for field in ("goal_scope_revision", "goal_scope_fingerprint"):
        payload = model.model_dump(mode="python")
        payload.pop(field)
        with pytest.raises(ValidationError, match=f"{field}|required"):
            EvaluatedBuildModelV1.model_validate(payload)

    expectation = JvmBuildExpectationV1(
        expectation_id=EXPECTATION_ID,
        evidence_epoch_id=EPOCH_ID,
        census_id=CENSUS_ID,
        scope_id=SCOPE_ID,
        model_id=MODEL_ID,
        scope_fingerprint=SHA_B,
        source_state_fingerprint=SHA_A,
        build_config_fingerprint=SHA_B,
        required_java_edges=(),
        source_classifications=(),
        unclassified_source_ids=(),
        required_generator_unit_ids=(),
        required_compile_unit_ids=(),
        required_package_unit_ids=(),
        unsupported_active_unit_ids=(),
        required_child_scope_ids=(),
        delegation_edges=(),
        conflicts=(),
    )
    with pytest.raises(ValidationError, match="expectation_id|identity"):
        expectation.model_copy(update={"scope_fingerprint": SHA_A})


def test_required_non_java_source_keeps_compile_ownership_without_java_edges() -> None:
    source_id = canonical_content_id(
        "jvm-source",
        {"path": "src/main/kotlin/example/App.kt", "sha256": SHA_A},
    )
    classification = SourceClassification(
        source_id=source_id,
        language="kotlin",
        source_kind="main",
        classification="required",
        classification_basis=ClassificationBasisV1(
            basis_kind="model_rule",
            model_id=MODEL_ID,
            basis_ref="compile-kotlin:main",
        ),
        expected_compile_unit_ids=("compile-kotlin",),
        child_scope_id=None,
    )
    identity: dict[str, object] = {
        "evidence_epoch_id": EPOCH_ID,
        "census_id": CENSUS_ID,
        "scope_id": SCOPE_ID,
        "model_id": MODEL_ID,
        "scope_fingerprint": SHA_B,
        "source_state_fingerprint": SHA_A,
        "build_config_fingerprint": SHA_B,
        "required_java_edges": (),
        "source_classifications": (classification,),
        "unclassified_source_ids": (),
        "required_generator_unit_ids": (),
        "required_compile_unit_ids": ("compile-kotlin",),
        "required_package_unit_ids": (),
        "unsupported_active_unit_ids": (),
        "required_child_scope_ids": (),
        "delegation_edges": (),
    }
    expectation = JvmBuildExpectationV1(
        expectation_id=canonical_content_id("jvm-build-expectation-v1", identity),
        **identity,
        conflicts=(),
    )
    assert expectation.required_java_edges == ()
    assert expectation.source_classifications[0].expected_compile_unit_ids == ("compile-kotlin",)

    false_java_edge = RequiredSourceEdge(
        source_id=source_id,
        compile_unit_id="compile-kotlin",
    )
    payload = expectation.model_dump(mode="python")
    payload["required_java_edges"] = (false_java_edge,)
    with pytest.raises(ValidationError, match="non-Java|Java source edge"):
        JvmBuildExpectationV1.model_validate(_refresh_expectation_id(payload))


def test_unsupported_active_sources_preserve_java_denominator_only_for_java() -> None:
    def unsupported_classification(language: str, source_id: str) -> SourceClassification:
        return SourceClassification(
            source_id=source_id,
            language=language,
            source_kind="main",
            classification="unsupported_active",
            classification_basis=ClassificationBasisV1(
                basis_kind="unsupported_adapter",
                model_id=MODEL_ID,
                basis_ref=f"compile-{language}",
            ),
            expected_compile_unit_ids=(f"compile-{language}",),
            child_scope_id=None,
        )

    java_source_id = canonical_content_id(
        "jvm-source",
        {"path": "src/custom/java/example/App.java", "sha256": SHA_A},
    )
    java_classification = unsupported_classification("java", java_source_id)
    java_edge = RequiredSourceEdge(
        source_id=java_source_id,
        compile_unit_id="compile-java",
    )
    java_identity: dict[str, object] = {
        "evidence_epoch_id": EPOCH_ID,
        "census_id": CENSUS_ID,
        "scope_id": SCOPE_ID,
        "model_id": MODEL_ID,
        "scope_fingerprint": SHA_B,
        "source_state_fingerprint": SHA_A,
        "build_config_fingerprint": SHA_B,
        "required_java_edges": (java_edge,),
        "source_classifications": (java_classification,),
        "unclassified_source_ids": (),
        "required_generator_unit_ids": (),
        "required_compile_unit_ids": (),
        "required_package_unit_ids": (),
        "unsupported_active_unit_ids": ("compile-java",),
        "required_child_scope_ids": (),
        "delegation_edges": (),
    }
    java_expectation = JvmBuildExpectationV1(
        expectation_id=canonical_content_id("jvm-build-expectation-v1", java_identity),
        **java_identity,
        conflicts=(),
    )
    assert java_expectation.required_java_edges == (java_edge,)

    missing_java_edge = java_expectation.model_dump(mode="python")
    missing_java_edge["required_java_edges"] = ()
    with pytest.raises(ValidationError, match="unsupported active Java|edge sets"):
        JvmBuildExpectationV1.model_validate(_refresh_expectation_id(missing_java_edge))

    kotlin_source_id = canonical_content_id(
        "jvm-source",
        {"path": "src/main/kotlin/example/App.kt", "sha256": SHA_B},
    )
    kotlin_classification = unsupported_classification("kotlin", kotlin_source_id)
    kotlin_identity = {
        **java_identity,
        "required_java_edges": (),
        "source_classifications": (kotlin_classification,),
        "unsupported_active_unit_ids": ("compile-kotlin",),
    }
    kotlin_expectation = JvmBuildExpectationV1(
        expectation_id=canonical_content_id("jvm-build-expectation-v1", kotlin_identity),
        **kotlin_identity,
        conflicts=(),
    )
    assert kotlin_expectation.required_java_edges == ()

    false_kotlin_edge = kotlin_expectation.model_dump(mode="python")
    false_kotlin_edge["required_java_edges"] = (
        RequiredSourceEdge(
            source_id=kotlin_source_id,
            compile_unit_id="compile-kotlin",
        ),
    )
    with pytest.raises(ValidationError, match="non-Java|Java source edge"):
        JvmBuildExpectationV1.model_validate(_refresh_expectation_id(false_kotlin_edge))


def test_one_java_source_can_span_supported_and_unsupported_active_units() -> None:
    source_id = canonical_content_id(
        "jvm-source",
        {"path": "src/main/java/example/Mixed.java", "sha256": SHA_A},
    )
    classification = SourceClassification(
        source_id=source_id,
        language="java",
        source_kind="main",
        classification="required",
        classification_basis=ClassificationBasisV1(
            basis_kind="model_rule",
            model_id=MODEL_ID,
            basis_ref="mixed-active-ownership",
        ),
        expected_compile_unit_ids=("compile-supported", "compile-unsupported"),
        child_scope_id=None,
    )
    edges = (
        RequiredSourceEdge(
            source_id=source_id,
            compile_unit_id="compile-supported",
        ),
        RequiredSourceEdge(
            source_id=source_id,
            compile_unit_id="compile-unsupported",
        ),
    )
    identity: dict[str, object] = {
        "evidence_epoch_id": EPOCH_ID,
        "census_id": CENSUS_ID,
        "scope_id": SCOPE_ID,
        "model_id": MODEL_ID,
        "scope_fingerprint": SHA_B,
        "source_state_fingerprint": SHA_A,
        "build_config_fingerprint": SHA_B,
        "required_java_edges": edges,
        "source_classifications": (classification,),
        "unclassified_source_ids": (),
        "required_generator_unit_ids": (),
        "required_compile_unit_ids": ("compile-supported",),
        "required_package_unit_ids": (),
        "unsupported_active_unit_ids": ("compile-unsupported",),
        "required_child_scope_ids": (),
        "delegation_edges": (),
    }
    expectation = JvmBuildExpectationV1(
        expectation_id=canonical_content_id("jvm-build-expectation-v1", identity),
        **identity,
        conflicts=(),
    )

    assert expectation.required_java_edges == edges
    assert expectation.source_classifications[0].classification == "required"


def test_ordered_classpath_members_preserve_order_in_compile_identity() -> None:
    entries = (
        ResolvedDigestEntry(
            member_id="z-classpath",
            member_role="classpath",
            path_kind="file",
            path="/deps/z.jar",
            byte_count=1,
            sha256=SHA_A,
        ),
        ResolvedDigestEntry(
            member_id="a-classpath",
            member_role="classpath",
            path_kind="file",
            path="/deps/a.jar",
            byte_count=1,
            sha256=SHA_B,
        ),
    )
    observation = _compile_observation()
    projection = {
        "compile_unit_id": observation.compile_unit_id,
        "build_config_fingerprint": observation.build_config_fingerprint,
        "required_source_entries": observation.source_entries,
        "source_set_fingerprint": observation.source_set_fingerprint,
        "compiler_executable_path": observation.compiler_executable_path,
        "compiler_executable_sha256": observation.compiler_executable_sha256,
        "compiler_version": observation.compiler_version,
        "compiler_args_fingerprint": observation.compiler_args_fingerprint,
        "classpath_entries": entries,
        "dependency_unit_identity_hashes": observation.dependency_unit_identity_hashes,
        "toolchain_fingerprint": observation.toolchain_fingerprint,
        "output_roots": observation.observed_output_roots,
    }
    forward = FinalCompileUnitBasis(
        **projection,
        compile_unit_identity_hash=compile_unit_identity_hash(projection),
    )
    reverse_projection = {**projection, "classpath_entries": tuple(reversed(entries))}
    reverse = FinalCompileUnitBasis(
        **reverse_projection,
        compile_unit_identity_hash=compile_unit_identity_hash(reverse_projection),
    )

    assert tuple(item.member_id for item in forward.classpath_entries) == (
        "z-classpath",
        "a-classpath",
    )
    assert forward.compile_unit_identity_hash != reverse.compile_unit_identity_hash


def test_ordered_package_inputs_and_tools_preserve_order_in_identity() -> None:
    observation = _package_observation()
    tools = (
        ResolvedDigestEntry(
            member_id="z-packaging-tool",
            member_role="packaging_tool",
            path_kind="file",
            path="/tools/z.jar",
            byte_count=1,
            sha256=SHA_A,
        ),
        ResolvedDigestEntry(
            member_id="a-packaging-tool",
            member_role="packaging_tool",
            path_kind="file",
            path="/tools/a.jar",
            byte_count=1,
            sha256=SHA_B,
        ),
    )
    output_digests = (
        CompileOutputDigestEntry(
            compile_unit_id="compile-z",
            root_id="root-z",
            sha256=SHA_A,
        ),
        CompileOutputDigestEntry(
            compile_unit_id="compile-a",
            root_id="root-a",
            sha256=SHA_B,
        ),
    )
    projection = {
        "package_unit_id": observation.package_unit_id,
        "build_config_fingerprint": observation.build_config_fingerprint,
        "input_compile_unit_identity_hashes": (SHA_B, SHA_A),
        "input_compile_output_digests": output_digests,
        "resource_entries": observation.resource_entries,
        "packaging_executable_path": observation.packaging_executable_path,
        "packaging_executable_sha256": observation.packaging_executable_sha256,
        "packaging_tool_entries": tools,
        "external_dependency_entries": observation.external_dependency_entries,
        "packaging_args_fingerprint": observation.packaging_args_fingerprint,
        "packaging_toolchain_fingerprint": observation.packaging_toolchain_fingerprint,
        "packaging_config_fingerprint": observation.packaging_config_fingerprint,
        "output_roots": observation.output_roots,
        "requested_artifacts": observation.requested_artifacts,
    }
    forward = FinalPackageUnitBasis(
        **projection,
        package_unit_identity_hash=package_unit_identity_hash(projection),
    )
    reverse_projection = {
        **projection,
        "input_compile_unit_identity_hashes": (SHA_A, SHA_B),
        "input_compile_output_digests": tuple(reversed(output_digests)),
        "packaging_tool_entries": tuple(reversed(tools)),
    }
    reverse = FinalPackageUnitBasis(
        **reverse_projection,
        package_unit_identity_hash=package_unit_identity_hash(reverse_projection),
    )

    assert tuple(item.member_id for item in forward.packaging_tool_entries) == (
        "z-packaging-tool",
        "a-packaging-tool",
    )
    assert forward.input_compile_unit_identity_hashes == (SHA_B, SHA_A)
    assert forward.package_unit_identity_hash != reverse.package_unit_identity_hash


@pytest.mark.parametrize(
    ("model_type", "expected_fields"),
    (
        (
            ActualInvocationScopeV1,
            {
                "build_system",
                "selected_modules_or_tasks",
                "lifecycle_or_tasks",
                "profiles_or_variants",
                "resume_from",
                "also_make",
                "selector_fingerprint",
            },
        ),
        (
            EvaluatedModuleV1,
            {
                "module_id",
                "domain_id",
                "build_system",
                "module_coordinate",
                "project_path",
                "role",
                "model_provenance",
            },
        ),
        (RequiredSourceEdge, {"source_id", "compile_unit_id"}),
        (
            DelegationEdge,
            {
                "parent_source_id",
                "child_scope_id",
                "expected_child_path",
                "expected_child_sha256",
            },
        ),
        (
            SourceClassification,
            {
                "source_id",
                "language",
                "source_kind",
                "classification",
                "classification_basis",
                "expected_compile_unit_ids",
                "child_scope_id",
            },
        ),
        (
            OutputRootSpec,
            {"member_id", "root_id", "path", "verification_mode", "output_requirement"},
        ),
        (RequestedArtifactSpec, {"artifact_id", "root_id", "path", "kind"}),
        (
            SourcePatternRuleV1,
            {
                "rule_id",
                "compile_unit_id",
                "dialect",
                "pattern",
                "model_provenance",
            },
        ),
        (
            CompileUnitExpectation,
            {
                "compile_unit_id",
                "domain_id",
                "build_system",
                "module_coordinate",
                "source_set",
                "language",
                "adapter_status",
                "task_or_execution",
                "role",
                "scope_disposition",
                "source_roots",
                "include_rules",
                "exclude_rules",
                "compiler_environment_spec",
                "output_roots",
                "output_requirement",
                "output_requirement_basis",
                "requested_lifecycle_basis",
                "model_provenance",
            },
        ),
        (
            GeneratorUnitExpectation,
            {
                "generator_unit_id",
                "module_coordinate",
                "task_or_execution",
                "role",
                "input_roots",
                "generator_environment_spec",
                "generated_root_ids",
                "model_provenance",
            },
        ),
        (
            PackageUnitExpectation,
            {
                "package_unit_id",
                "module_coordinate",
                "task_or_execution",
                "role",
                "input_compile_unit_ids",
                "resource_roots",
                "manifest_or_packaging_config",
                "packaging_environment_spec",
                "output_roots",
                "requested_artifacts",
                "model_provenance",
            },
        ),
        (
            FinalGeneratorUnitBasis,
            {
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
                "generator_unit_identity_hash",
            },
        ),
        (
            FinalCompileUnitBasis,
            {
                "compile_unit_id",
                "build_config_fingerprint",
                "required_source_entries",
                "source_set_fingerprint",
                "compiler_executable_path",
                "compiler_executable_sha256",
                "compiler_version",
                "compiler_args_fingerprint",
                "classpath_entries",
                "dependency_unit_identity_hashes",
                "toolchain_fingerprint",
                "output_roots",
                "compile_unit_identity_hash",
            },
        ),
        (
            FinalPackageUnitBasis,
            {
                "package_unit_id",
                "build_config_fingerprint",
                "input_compile_unit_identity_hashes",
                "input_compile_output_digests",
                "resource_entries",
                "packaging_executable_path",
                "packaging_executable_sha256",
                "packaging_tool_entries",
                "external_dependency_entries",
                "packaging_args_fingerprint",
                "packaging_toolchain_fingerprint",
                "packaging_config_fingerprint",
                "output_roots",
                "requested_artifacts",
                "package_unit_identity_hash",
            },
        ),
        (
            CurrentPhysicalMemberV1,
            {"member_id", "member_role", "path_kind", "path", "byte_count", "sha256"},
        ),
        (
            CurrentOutputRootV1,
            {
                "member_id",
                "root_id",
                "path",
                "verification_mode",
                "tree_or_owned_entries_sha256",
                "entry_count",
                "scan_complete",
            },
        ),
        (
            CurrentOutputEntryV1,
            {"root_id", "relative_path", "kind", "byte_count", "sha256"},
        ),
        (MissingPhysicalMemberV1, {"member_id", "path"}),
        (UnreadablePhysicalMemberV1, {"member_id", "path", "reason"}),
        (
            OutputRootWitness,
            {
                "root_id",
                "path",
                "verification_mode",
                "tree_or_owned_entries_sha256",
                "entry_count",
            },
        ),
        (
            PhysicalOutputEntry,
            {"root_id", "relative_path", "kind", "byte_count", "sha256"},
        ),
        (EvidenceRecordDigestV1, {"record_id", "raw_sha256"}),
    ),
)
def test_nested_public_contracts_have_exact_required_fields(
    model_type: type[object], expected_fields: set[str]
) -> None:
    assert set(model_type.model_fields) == expected_fields  # type: ignore[attr-defined]
    optional_defaults = {"dialect"} if model_type is SourcePatternRuleV1 else set()
    assert all(  # type: ignore[attr-defined]
        field.is_required() or name in optional_defaults
        for name, field in model_type.model_fields.items()
    )


def test_required_source_edges_are_typed_and_match_required_classifications() -> None:
    source = _source()
    edge = RequiredSourceEdge(
        source_id=source.source_id,
        compile_unit_id="compile-app",
    )
    classification = SourceClassification(
        source_id=source.source_id,
        language="java",
        source_kind="main",
        classification="required",
        classification_basis=ClassificationBasisV1(
            basis_kind="model_rule",
            model_id=MODEL_ID,
            basis_ref="compile-app:main",
        ),
        expected_compile_unit_ids=("compile-app",),
        child_scope_id=None,
    )
    identity: dict[str, object] = {
        "evidence_epoch_id": EPOCH_ID,
        "census_id": CENSUS_ID,
        "scope_id": SCOPE_ID,
        "model_id": MODEL_ID,
        "scope_fingerprint": SHA_B,
        "source_state_fingerprint": SHA_A,
        "build_config_fingerprint": SHA_B,
        "required_java_edges": (edge,),
        "source_classifications": (classification,),
        "unclassified_source_ids": (),
        "required_generator_unit_ids": ("generator-01",),
        "required_compile_unit_ids": ("compile-app",),
        "required_package_unit_ids": ("package-01",),
        "unsupported_active_unit_ids": (),
        "required_child_scope_ids": (),
        "delegation_edges": (),
    }
    expectation = JvmBuildExpectationV1(
        expectation_id=canonical_content_id("jvm-build-expectation-v1", identity),
        **identity,
        conflicts=(),
    )
    assert expectation.required_java_edges == (edge,)

    foreign_model_id = canonical_content_id(
        "evaluated-build-model-v1",
        {
            "evidence_epoch_id": EPOCH_ID,
            "scope_id": SCOPE_ID,
            "project_root": "/workspace/foreign",
        },
    )
    foreign_basis = classification.classification_basis.model_copy(
        update={"model_id": foreign_model_id}
    )
    foreign_classification = classification.model_copy(
        update={"classification_basis": foreign_basis}
    )
    foreign_basis_payload = expectation.model_dump(mode="python")
    foreign_basis_payload["source_classifications"] = (foreign_classification,)
    with pytest.raises(ValidationError, match="classification_basis|model_id|model"):
        JvmBuildExpectationV1.model_validate(_refresh_expectation_id(foreign_basis_payload))

    unknown_unit = expectation.model_dump(mode="python")
    unknown_unit["required_java_edges"] = (
        edge.model_copy(update={"compile_unit_id": "compile-unknown"}),
    )
    with pytest.raises(ValidationError, match="edge|compile-unknown|required"):
        JvmBuildExpectationV1.model_validate(_refresh_expectation_id(unknown_unit))

    missing_edge = expectation.model_dump(mode="python")
    missing_edge["required_java_edges"] = ()
    with pytest.raises(ValidationError, match="edge|required|classification"):
        JvmBuildExpectationV1.model_validate(_refresh_expectation_id(missing_edge))


@pytest.mark.parametrize(
    ("value", "field", "unknown"),
    (
        (_source(), "language", "future-language"),
        (_source(), "role_hint", "future-role"),
        (
            CurrentPhysicalMemberV1(
                member_id="member-01",
                member_role="source",
                path_kind="file",
                path="/workspace/project/App.java",
                byte_count=1,
                sha256=SHA_A,
            ),
            "path_kind",
            "future-kind",
        ),
        (
            CurrentPhysicalMemberV1(
                member_id="member-01",
                member_role="source",
                path_kind="file",
                path="/workspace/project/App.java",
                byte_count=1,
                sha256=SHA_A,
            ),
            "member_role",
            "future-role",
        ),
        (
            _compile_expectation("compile-01", "src", "classes", "target/classes"),
            "build_system",
            "future-build-system",
        ),
        (
            _compile_expectation("compile-01", "src", "classes", "target/classes"),
            "language",
            "future-language",
        ),
        (
            _compile_expectation("compile-01", "src", "classes", "target/classes"),
            "role",
            "future-role",
        ),
        (
            _compile_expectation("compile-01", "src", "classes", "target/classes"),
            "adapter_status",
            "future-adapter",
        ),
        (
            _compile_expectation("compile-01", "src", "classes", "target/classes"),
            "output_requirement",
            "future-output",
        ),
        (_compile_observation(), "outcome", "future-outcome"),
        (_generator_observation(), "outcome", "future-outcome"),
        (_package_observation(), "outcome", "future-outcome"),
        (
            _compile_expectation("compile-01", "src", "classes", "target/classes").output_roots[0],
            "verification_mode",
            "future-mode",
        ),
        (
            SourceClassification(
                source_id=_source().source_id,
                language="java",
                source_kind="main",
                classification="required",
                classification_basis=ClassificationBasisV1(
                    basis_kind="model_rule",
                    model_id=MODEL_ID,
                    basis_ref="compile-01:main",
                ),
                expected_compile_unit_ids=("compile-01",),
                child_scope_id=None,
            ),
            "classification",
            "future-classification",
        ),
        (
            SourceClassification(
                source_id=_source().source_id,
                language="java",
                source_kind="main",
                classification="required",
                classification_basis=ClassificationBasisV1(
                    basis_kind="model_rule",
                    model_id=MODEL_ID,
                    basis_ref="compile-01:main",
                ),
                expected_compile_unit_ids=("compile-01",),
                child_scope_id=None,
            ),
            "source_kind",
            "future-source-kind",
        ),
        (
            ClassificationBasisV1(
                basis_kind="model_rule",
                model_id=MODEL_ID,
                basis_ref="compile-01:main",
            ),
            "basis_kind",
            "future-basis",
        ),
        (
            PhysicalOutputEntry(
                root_id="classes-root",
                relative_path="App.class",
                kind="class",
                byte_count=1,
                sha256=SHA_A,
            ),
            "kind",
            "future-output-kind",
        ),
        (_compile_output_witness(), "producer_observation_kind", "future-producer"),
        (_complete_set("compile_observation"), "status", "future-status"),
        (
            _nonempty_build_model().generated_root_contracts[0],
            "producer_unit_kind",
            "future-producer",
        ),
        (_nonempty_build_model().generated_root_contracts[0], "mode", "future-mode"),
    ),
)
def test_closed_enum_families_reject_future_values(value: object, field: str, unknown: str) -> None:
    with pytest.raises(ValidationError, match=field):
        value.model_copy(update={field: unknown})  # type: ignore[union-attr]


def test_build_model_and_final_basis_round_trip_as_closed_empty_heads() -> None:
    build_model = EvaluatedBuildModelV1(
        model_id=MODEL_ID,
        evidence_epoch_id=EPOCH_ID,
        scope_id=SCOPE_ID,
        goal_scope_revision=1,
        goal_scope_fingerprint=SHA_B,
        project_root=PROJECT_ROOT,
        model_revision=1,
        build_config_fingerprint=SHA_A,
        active_profiles_or_variants=(),
        build_config_members=(),
        modules=(),
        generator_units=(),
        compile_units=(),
        package_units=(),
        compile_dependency_edges=(),
        generated_root_contracts=(),
        child_build_scopes=(),
        conflicts=(),
    )
    final_basis = FinalBuildBasisV1(
        basis_id=FINAL_BASIS_ID,
        evidence_epoch_id=EPOCH_ID,
        basis_revision=1,
        expectation_id=EXPECTATION_ID,
        physical_basis_snapshot_id=PHYSICAL_BASIS_ID,
        source_state_fingerprint=SHA_A,
        build_config_fingerprint=SHA_B,
        generator_unit_bases=(),
        compile_unit_bases=(),
        package_unit_bases=(),
        conflicts=(),
    )

    for value in (build_model, final_basis):
        raw = canonical_build_evidence_bytes(value)
        assert validate_build_evidence_json(type(value), raw) == value
        with pytest.raises(ValidationError, match="extra"):
            type(value).model_validate({**value.model_dump(mode="python"), "future": True})

    with pytest.raises(ValidationError, match="model_id|identity"):
        build_model.model_copy(
            update={
                "model_id": canonical_content_id(
                    "evaluated-build-model-v1",
                    {
                        "evidence_epoch_id": EPOCH_ID,
                        "scope_id": SCOPE_ID,
                        "project_root": "/workspace/foreign",
                    },
                )
            }
        )
    with pytest.raises(ValidationError, match="basis_id|identity"):
        final_basis.model_copy(
            update={
                "basis_id": canonical_content_id(
                    "final-build-basis-v1",
                    {
                        "evidence_epoch_id": EPOCH_ID,
                        "expectation_id": canonical_content_id(
                            "jvm-build-expectation-v1", {"wrong": True}
                        ),
                    },
                )
            }
        )


def test_all_top_level_records_round_trip_and_reject_unknown_fields() -> None:
    epoch = ContainerEvidenceEpochV1(
        evidence_epoch_id=EPOCH_ID,
        immutable_container_id=CONTAINER_ID,
        project_root=PROJECT_ROOT,
        created_at="2026-08-11T12:00:00Z",
        initial_source_state_fingerprint=SHA_A,
        baseline_output_root_digests=(),
    )
    scope = BuildGoalScopeV1(
        scope_id=SCOPE_ID,
        evidence_epoch_id=EPOCH_ID,
        authority_ref="request-01",
        scope_revision=1,
        predecessor_scope_hash=None,
        validation_target="project",
        requested_action="build",
        requested_lifecycle="package",
        requested_modules_or_domains=(),
        requested_profiles_or_variants=(),
        source_set_roles=("production",),
        scope_fingerprint=SHA_B,
    )
    census = JvmSourceCensusV1(
        census_id=CENSUS_ID,
        evidence_epoch_id=EPOCH_ID,
        project_root=PROJECT_ROOT,
        census_revision=1,
        source_state_fingerprint=SHA_A,
        sources=(),
        unreadable_roots=(),
        conflicts=(),
    )
    build_model = EvaluatedBuildModelV1(
        model_id=MODEL_ID,
        evidence_epoch_id=EPOCH_ID,
        scope_id=SCOPE_ID,
        goal_scope_revision=1,
        goal_scope_fingerprint=SHA_B,
        project_root=PROJECT_ROOT,
        model_revision=1,
        build_config_fingerprint=SHA_A,
        active_profiles_or_variants=(),
        build_config_members=(),
        modules=(),
        generator_units=(),
        compile_units=(),
        package_units=(),
        compile_dependency_edges=(),
        generated_root_contracts=(),
        child_build_scopes=(),
        conflicts=(),
    )
    expectation = JvmBuildExpectationV1(
        expectation_id=EXPECTATION_ID,
        evidence_epoch_id=EPOCH_ID,
        census_id=CENSUS_ID,
        scope_id=SCOPE_ID,
        model_id=MODEL_ID,
        scope_fingerprint=SHA_B,
        source_state_fingerprint=SHA_A,
        build_config_fingerprint=SHA_B,
        required_java_edges=(),
        source_classifications=(),
        unclassified_source_ids=(),
        required_generator_unit_ids=(),
        required_compile_unit_ids=(),
        required_package_unit_ids=(),
        unsupported_active_unit_ids=(),
        required_child_scope_ids=(),
        delegation_edges=(),
        conflicts=(),
    )
    physical = CurrentPhysicalBasisSnapshotV1(
        physical_basis_snapshot_id=PHYSICAL_BASIS_ID,
        evidence_epoch_id=EPOCH_ID,
        expectation_id=EXPECTATION_ID,
        build_model_id=MODEL_ID,
        requested_member_ids=(),
        current_members=(),
        current_output_roots=(),
        current_output_entries=(),
        missing_members=(),
        unreadable_members=(),
        conflicts=(),
    )
    final_basis = FinalBuildBasisV1(
        basis_id=FINAL_BASIS_ID,
        evidence_epoch_id=EPOCH_ID,
        basis_revision=1,
        expectation_id=EXPECTATION_ID,
        physical_basis_snapshot_id=PHYSICAL_BASIS_ID,
        source_state_fingerprint=SHA_A,
        build_config_fingerprint=SHA_B,
        generator_unit_bases=(),
        compile_unit_bases=(),
        package_unit_bases=(),
        conflicts=(),
    )
    records = (
        epoch,
        scope,
        census,
        build_model,
        expectation,
        physical,
        final_basis,
        _generator_observation(),
        _compile_observation(),
        _package_observation(),
        _compile_output_witness(),
        BuildEvidenceSetSnapshotV1.model_validate(_evidence_set_payload()),
        BuildReconciliationV1.model_validate(_empty_reconciliation_payload()),
    )
    for record in records:
        raw = canonical_build_evidence_bytes(record)
        assert validate_build_evidence_json(type(record), raw) == record
        with pytest.raises(ValidationError, match="extra"):
            type(record).model_validate({**record.model_dump(mode="python"), "future": True})


def test_compile_basis_and_observation_require_one_exact_identity_projection() -> None:
    source = SourceDigestEntry(
        source_id=_source().source_id,
        path=_source().path,
        sha256=_source().sha256,
    )
    classpath = ResolvedDigestEntry(
        member_id="classpath-dependency-01",
        member_role="classpath",
        path_kind="file",
        path="/workspace/project/deps/dependency.jar",
        byte_count=10,
        sha256=SHA_A,
    )
    output_root = OutputRootSpec(
        member_id="classes-root-member",
        root_id="classes-root",
        path="target/classes",
        verification_mode="exclusive_tree",
        output_requirement="nonempty",
    )
    identity_fields: dict[str, object] = {
        "compile_unit_id": "compile-01",
        "build_config_fingerprint": SHA_B,
        "required_source_entries": (source,),
        "source_set_fingerprint": SHA_A,
        "compiler_executable_path": "/opt/jdk/bin/javac",
        "compiler_executable_sha256": SHA_A,
        "compiler_version": "21.0.1",
        "compiler_args_fingerprint": SHA_A,
        "classpath_entries": (classpath,),
        "dependency_unit_identity_hashes": (),
        "toolchain_fingerprint": SHA_B,
        "output_roots": (output_root,),
    }
    identity_hash = compile_unit_identity_hash(identity_fields)
    observation_id = canonical_content_id(
        "compile-observation-v1",
        {
            "evidence_epoch_id": EPOCH_ID,
            "run_id": "run-01",
            "receipt_id": "receipt-01",
            "contract_id": "contract-01",
            "compile_unit_id": "compile-01",
        },
    )
    basis = FinalCompileUnitBasis(
        **identity_fields,
        compile_unit_identity_hash=identity_hash,
    )
    observation_fields = {
        **identity_fields,
        "source_entries": identity_fields["required_source_entries"],
        "observed_output_roots": identity_fields["output_roots"],
    }
    observation_fields.pop("required_source_entries")
    observation_fields.pop("output_roots")
    observation = CompileUnitObservationV1(
        observation_id=observation_id,
        evidence_epoch_id=EPOCH_ID,
        run_id="run-01",
        receipt_id="receipt-01",
        contract_id="contract-01",
        **observation_fields,
        compile_unit_identity_hash=identity_hash,
        outcome="executed_success",
        actual_invocation_scope=_maven_invocation_scope(),
        generated_during_compile=(),
        conflicts=(),
    )
    assert observation.compile_unit_identity_hash == basis.compile_unit_identity_hash

    with pytest.raises(ValidationError, match="observation_id|identity"):
        observation.model_copy(
            update={
                "observation_id": canonical_content_id(
                    "compile-observation-v1",
                    {
                        "evidence_epoch_id": EPOCH_ID,
                        "run_id": "run-foreign",
                        "receipt_id": observation.receipt_id,
                        "contract_id": observation.contract_id,
                        "compile_unit_id": observation.compile_unit_id,
                    },
                )
            }
        )

    mutations = (
        ("compile_unit_id", "compile_unit_id", "compile-02"),
        ("build_config_fingerprint", "build_config_fingerprint", SHA_A),
        ("required_source_entries", "source_entries", ()),
        ("source_set_fingerprint", "source_set_fingerprint", SHA_B),
        ("compiler_executable_path", "compiler_executable_path", "/opt/jdk/bin/javac-next"),
        ("compiler_executable_sha256", "compiler_executable_sha256", SHA_B),
        ("compiler_version", "compiler_version", "22.0.0"),
        ("compiler_args_fingerprint", "compiler_args_fingerprint", SHA_B),
        (
            "classpath_entries",
            "classpath_entries",
            (classpath.model_copy(update={"sha256": SHA_B}),),
        ),
        ("dependency_unit_identity_hashes", "dependency_unit_identity_hashes", (SHA_A,)),
        ("toolchain_fingerprint", "toolchain_fingerprint", SHA_A),
        (
            "output_roots",
            "observed_output_roots",
            (output_root.model_copy(update={"path": "target/other-classes"}),),
        ),
    )
    for basis_field, observation_field, changed in mutations:
        with pytest.raises(ValidationError, match="identity"):
            basis.model_copy(update={basis_field: changed})
        with pytest.raises(ValidationError, match="identity"):
            observation.model_copy(update={observation_field: changed})


def test_generator_basis_and_observation_recompute_full_environment_identity() -> None:
    observation = _generator_observation()
    identity_fields = {
        field: getattr(observation, field)
        for field in (
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
        )
    }
    identity_hash = generator_unit_identity_hash(identity_fields)
    basis = FinalGeneratorUnitBasis(
        **identity_fields,
        generator_unit_identity_hash=identity_hash,
    )
    assert observation.generator_unit_identity_hash == basis.generator_unit_identity_hash

    with pytest.raises(ValidationError, match="observation_id|identity"):
        observation.model_copy(
            update={
                "observation_id": canonical_content_id(
                    "generator-observation-v1",
                    {
                        "evidence_epoch_id": EPOCH_ID,
                        "run_id": "run-foreign",
                        "receipt_id": observation.receipt_id,
                        "contract_id": observation.contract_id,
                        "generator_unit_id": observation.generator_unit_id,
                    },
                )
            }
        )

    mutations = (
        ("generator_unit_id", "generator-02"),
        ("build_config_fingerprint", SHA_A),
        (
            "input_entries",
            (observation.input_entries[0].model_copy(update={"sha256": SHA_B}),),
        ),
        ("generator_executable_path", "/workspace/project/tools/protoc-next"),
        ("generator_executable_sha256", SHA_B),
        (
            "generator_tool_entries",
            (observation.generator_tool_entries[0].model_copy(update={"sha256": SHA_B}),),
        ),
        (
            "generator_dependency_entries",
            (observation.generator_dependency_entries[0].model_copy(update={"sha256": SHA_A}),),
        ),
        ("generator_args_fingerprint", SHA_B),
        ("generator_toolchain_fingerprint", SHA_A),
        (
            "generated_output_roots",
            (
                observation.generated_output_roots[0].model_copy(
                    update={"path": "target/generated-sources/foreign"}
                ),
            ),
        ),
    )
    for field, changed in mutations:
        with pytest.raises(ValidationError, match="identity"):
            basis.model_copy(update={field: changed})
        with pytest.raises(ValidationError, match="identity"):
            observation.model_copy(update={field: changed})


def test_package_basis_and_observation_bind_outputs_dependencies_and_packager() -> None:
    observation = _package_observation()
    identity_fields = {
        field: getattr(observation, field)
        for field in (
            "package_unit_id",
            "build_config_fingerprint",
            "input_compile_unit_identity_hashes",
            "input_compile_output_digests",
            "resource_entries",
            "packaging_executable_path",
            "packaging_executable_sha256",
            "packaging_tool_entries",
            "external_dependency_entries",
            "packaging_args_fingerprint",
            "packaging_toolchain_fingerprint",
            "packaging_config_fingerprint",
            "output_roots",
            "requested_artifacts",
        )
    }
    identity_hash = package_unit_identity_hash(identity_fields)
    basis = FinalPackageUnitBasis(
        **identity_fields,
        package_unit_identity_hash=identity_hash,
    )
    assert observation.package_unit_identity_hash == basis.package_unit_identity_hash

    with pytest.raises(ValidationError, match="observation_id|identity"):
        observation.model_copy(
            update={
                "observation_id": canonical_content_id(
                    "package-observation-v1",
                    {
                        "evidence_epoch_id": EPOCH_ID,
                        "run_id": "run-foreign",
                        "receipt_id": observation.receipt_id,
                        "contract_id": observation.contract_id,
                        "package_unit_id": observation.package_unit_id,
                    },
                )
            }
        )

    mutations = (
        ("package_unit_id", "package-02"),
        ("build_config_fingerprint", SHA_A),
        ("input_compile_unit_identity_hashes", (SHA_B,)),
        (
            "input_compile_output_digests",
            (observation.input_compile_output_digests[0].model_copy(update={"sha256": SHA_B}),),
        ),
        (
            "resource_entries",
            (observation.resource_entries[0].model_copy(update={"sha256": SHA_B}),),
        ),
        ("packaging_executable_path", "/usr/bin/jar-next"),
        ("packaging_executable_sha256", SHA_B),
        (
            "packaging_tool_entries",
            (observation.packaging_tool_entries[0].model_copy(update={"sha256": SHA_B}),),
        ),
        (
            "external_dependency_entries",
            (observation.external_dependency_entries[0].model_copy(update={"sha256": SHA_A}),),
        ),
        ("packaging_args_fingerprint", SHA_B),
        ("packaging_toolchain_fingerprint", SHA_A),
        ("packaging_config_fingerprint", SHA_B),
        (
            "output_roots",
            (
                observation.output_roots[0].model_copy(
                    update={"verification_mode": "exclusive_tree"}
                ),
            ),
        ),
        (
            "requested_artifacts",
            (observation.requested_artifacts[0].model_copy(update={"kind": "war"}),),
        ),
    )
    for field, changed in mutations:
        with pytest.raises(ValidationError, match="identity"):
            basis.model_copy(update={field: changed})
        with pytest.raises(ValidationError, match="identity"):
            observation.model_copy(update={field: changed})


def test_output_witness_entries_are_typed_unique_bounded_and_root_owned() -> None:
    observation = _compile_observation()
    entry = PhysicalOutputEntry(
        root_id="classes-root",
        relative_path="example/App.class",
        kind="class",
        byte_count=24,
        sha256=SHA_A,
    )
    root = OutputRootWitness(
        root_id="classes-root",
        path="target/classes",
        verification_mode="shared_owned_entries",
        tree_or_owned_entries_sha256=SHA_B,
        entry_count=1,
    )
    aggregate = physical_output_aggregate_sha256((root,), (entry,))
    witness_id = canonical_content_id(
        "physical-output-witness-v1",
        {
            "evidence_epoch_id": EPOCH_ID,
            "producer_observation_kind": "compile",
            "producer_observation_id": observation.observation_id,
        },
    )
    witness = PhysicalOutputWitnessV1(
        witness_id=witness_id,
        evidence_epoch_id=EPOCH_ID,
        producer_observation_kind="compile",
        producer_observation_id=observation.observation_id,
        producer_unit_id="compile-01",
        producer_unit_identity_hash=observation.compile_unit_identity_hash,
        roots=(root,),
        entries=(entry,),
        aggregate_sha256=aggregate,
    )
    assert witness.aggregate_sha256 == aggregate

    with pytest.raises(ValidationError, match="witness_id|identity"):
        witness.model_copy(
            update={
                "witness_id": canonical_content_id(
                    "physical-output-witness-v1",
                    {
                        "evidence_epoch_id": EPOCH_ID,
                        "producer_observation_kind": "compile",
                        "producer_observation_id": "foreign-observation-id",
                    },
                )
            }
        )

    with pytest.raises(ValidationError, match="duplicate"):
        witness.model_copy(update={"entries": (entry, entry)})
    conflicting_entry = entry.model_copy(update={"sha256": SHA_B})
    duplicate_key_root = root.model_copy(update={"entry_count": 2})
    conflicting_entry_payload = witness.model_dump(mode="python")
    conflicting_entry_payload.update(
        {
            "roots": (duplicate_key_root,),
            "entries": (entry, conflicting_entry),
            "aggregate_sha256": physical_output_aggregate_sha256(
                (duplicate_key_root,), (entry, conflicting_entry)
            ),
        }
    )
    with pytest.raises(ValidationError, match="duplicate|relative_path|entry|conflict"):
        PhysicalOutputWitnessV1.model_validate(conflicting_entry_payload)
    with pytest.raises(ValidationError, match="root"):
        witness.model_copy(update={"entries": (entry.model_copy(update={"root_id": "unknown"}),)})
    with pytest.raises(ValidationError, match="aggregate|identity"):
        witness.model_copy(update={"entries": (entry.model_copy(update={"sha256": SHA_B}),)})
    with pytest.raises(ValidationError, match="aggregate|identity"):
        witness.model_copy(
            update={"roots": (root.model_copy(update={"tree_or_owned_entries_sha256": SHA_A}),)}
        )
    mismatched_count_root = root.model_copy(update={"entry_count": 2})
    mismatched_count = witness.model_dump(mode="python")
    mismatched_count.update(
        {
            "roots": (mismatched_count_root,),
            "aggregate_sha256": physical_output_aggregate_sha256(
                (mismatched_count_root,), (entry,)
            ),
        }
    )
    with pytest.raises(ValidationError, match="entry_count|entries|root"):
        PhysicalOutputWitnessV1.model_validate(mismatched_count)
    oversized_entries = tuple(
        entry.model_copy(update={"relative_path": f"generated/{index:05d}.class"})
        for index in range(BUILD_EVIDENCE_MAX_ENTRIES + 1)
    )
    with pytest.raises(ValidationError, match="entries|length"):
        witness.model_copy(update={"entries": oversized_entries})


def _empty_reconciliation_payload() -> dict[str, object]:
    return {
        "reconciliation_id": canonical_content_id(
            "build-reconciliation-v1",
            {"evidence_epoch_id": EPOCH_ID, "input_set_digest": SHA_A},
        ),
        "evidence_epoch_id": EPOCH_ID,
        "source_census_id": CENSUS_ID,
        "goal_scope_id": SCOPE_ID,
        "build_model_id": MODEL_ID,
        "expectation_id": EXPECTATION_ID,
        "final_basis_id": FINAL_BASIS_ID,
        "input_set_digest": SHA_A,
        "requested_action": "build",
        "status": "complete",
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
        "generator_units": {
            "expected": 0,
            "current": 0,
            "missing_or_invalid": 0,
            "unverifiable": 0,
        },
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
        "package_units": {
            "expected": 0,
            "current": 0,
            "missing_or_invalid": 0,
            "unverifiable": 0,
        },
        "active_language_units": {
            "expected": 0,
            "supported_current": 0,
            "unsupported_or_unverifiable": 0,
        },
        "child_scopes": {
            "expected": 0,
            "complete": 0,
            "incomplete_or_unverifiable": 0,
        },
        "modules": {"expected": 0, "built": 0, "partial": 0, "unverifiable": 0},
        "selected_generator_proofs": (),
        "selected_compile_proofs": (),
        "selected_package_proofs": (),
        "selected_child_reconciliations": (),
        "conflicts": (),
        "evidence_refs": (),
    }


def _nonempty_reconciliation_payload() -> dict[str, object]:
    generator = _generator_observation()
    compile_observation = _compile_observation()
    package = _package_observation()
    generator_witness_id = canonical_content_id(
        "physical-output-witness-v1",
        {
            "evidence_epoch_id": EPOCH_ID,
            "producer_observation_kind": "generator",
            "producer_observation_id": generator.observation_id,
        },
    )
    compile_witness_id = _compile_output_witness(compile_observation).witness_id
    package_witness_id = canonical_content_id(
        "physical-output-witness-v1",
        {
            "evidence_epoch_id": EPOCH_ID,
            "producer_observation_kind": "package",
            "producer_observation_id": package.observation_id,
        },
    )
    selected_generator = SelectedGeneratorProof(
        generator_unit_id=generator.generator_unit_id,
        generator_unit_identity_hash=generator.generator_unit_identity_hash,
        observation_id=generator.observation_id,
        witness_id=generator_witness_id,
    )
    selected_compile = SelectedCompileProof(
        compile_unit_id=compile_observation.compile_unit_id,
        compile_unit_identity_hash=compile_observation.compile_unit_identity_hash,
        observation_id=compile_observation.observation_id,
        witness_id=compile_witness_id,
    )
    selected_package = SelectedPackageProof(
        package_unit_id=package.package_unit_id,
        package_unit_identity_hash=package.package_unit_identity_hash,
        observation_id=package.observation_id,
        witness_id=package_witness_id,
    )
    selected_child = SelectedChildReconciliation(
        child_scope_id="child-scope-01",
        child_census_id="child-census-01",
        child_expectation_id="child-expectation-01",
        reconciliation_id="child-reconciliation-01",
    )
    input_set_digest = SHA_B
    payload = _empty_reconciliation_payload()
    payload.update(
        {
            "reconciliation_id": canonical_content_id(
                "build-reconciliation-v1",
                {
                    "evidence_epoch_id": EPOCH_ID,
                    "input_set_digest": input_set_digest,
                },
            ),
            "input_set_digest": input_set_digest,
            "status": "complete",
            "source_census": {
                "total_candidates": 2,
                "classified": 2,
                "unclassified": 0,
                "delegated": 1,
                "out_of_scope_by_reason": {},
            },
            "java_sources": {
                "local_expected": 1,
                "local_current": 1,
                "delegated_expected": 1,
                "delegated_current": 1,
                "project_expected": 2,
                "project_current": 2,
                "project_missing": 0,
                "project_stale": 0,
            },
            "java_edges": {"expected": 1, "current": 1, "missing": 0, "stale": 0},
            "generator_units": {
                "expected": 1,
                "current": 1,
                "missing_or_invalid": 0,
                "unverifiable": 0,
            },
            "compile_units": {
                "expected": 1,
                "coverage_current": 1,
                "coverage_missing_or_invalid": 0,
                "coverage_unverifiable": 0,
            },
            "physical_outputs": {
                "observed_class_count": 1,
                "witnesses_expected": 3,
                "witnesses_verified": 3,
                "sealed_entries": 3,
                "verified_entries": 3,
                "missing_entries": 0,
                "mismatched_entries": 0,
            },
            "package_units": {
                "expected": 1,
                "current": 1,
                "missing_or_invalid": 0,
                "unverifiable": 0,
            },
            "active_language_units": {
                "expected": 1,
                "supported_current": 1,
                "unsupported_or_unverifiable": 0,
            },
            "child_scopes": {
                "expected": 1,
                "complete": 1,
                "incomplete_or_unverifiable": 0,
            },
            "modules": {
                "expected": 1,
                "built": 1,
                "partial": 0,
                "unverifiable": 0,
            },
            "selected_generator_proofs": (selected_generator,),
            "selected_compile_proofs": (selected_compile,),
            "selected_package_proofs": (selected_package,),
            "selected_child_reconciliations": (selected_child,),
            "evidence_refs": tuple(
                sorted(
                    (
                        generator.observation_id,
                        generator_witness_id,
                        compile_observation.observation_id,
                        compile_witness_id,
                        package.observation_id,
                        package_witness_id,
                        selected_child.reconciliation_id,
                    )
                )
            ),
        }
    )
    return payload


def test_reconciliation_count_blocks_are_closed_nonnegative_and_conservative() -> None:
    reconciliation = BuildReconciliationV1.model_validate(_empty_reconciliation_payload())
    assert reconciliation.java_sources.project_expected == 0

    with pytest.raises(TypeError):
        reconciliation.source_census.out_of_scope_by_reason["mutated"] = 1

    with pytest.raises(ValidationError, match="reconciliation_id|identity"):
        reconciliation.model_copy(
            update={
                "reconciliation_id": canonical_content_id(
                    "build-reconciliation-v1",
                    {"evidence_epoch_id": EPOCH_ID, "input_set_digest": SHA_B},
                )
            }
        )
    with pytest.raises(ValidationError, match="status"):
        reconciliation.model_copy(update={"status": "future-status"})

    with pytest.raises(ValidationError, match="extra"):
        BuildReconciliationV1.model_validate(
            {**_empty_reconciliation_payload(), "invented_projection": {}}
        )

    bad = _empty_reconciliation_payload()
    bad["java_sources"] = {
        **bad["java_sources"],  # type: ignore[misc]
        "project_expected": 1,
    }
    with pytest.raises(ValidationError, match="project_expected|sum|count"):
        BuildReconciliationV1.model_validate(bad)

    bad_bool = _empty_reconciliation_payload()
    bad_bool["physical_outputs"] = {
        **bad_bool["physical_outputs"],  # type: ignore[misc]
        "observed_class_count": True,
    }
    with pytest.raises(ValidationError):
        BuildReconciliationV1.model_validate(bad_bool)


def test_reconciliation_selected_proofs_are_typed_and_counts_are_conservative() -> None:
    assert set(SelectedGeneratorProof.model_fields) == {
        "generator_unit_id",
        "generator_unit_identity_hash",
        "observation_id",
        "witness_id",
    }
    assert set(SelectedCompileProof.model_fields) == {
        "compile_unit_id",
        "compile_unit_identity_hash",
        "observation_id",
        "witness_id",
    }
    assert set(SelectedPackageProof.model_fields) == {
        "package_unit_id",
        "package_unit_identity_hash",
        "observation_id",
        "witness_id",
    }
    assert set(SelectedChildReconciliation.model_fields) == {
        "child_scope_id",
        "child_census_id",
        "child_expectation_id",
        "reconciliation_id",
    }

    reconciliation = BuildReconciliationV1.model_validate(_nonempty_reconciliation_payload())
    assert len(reconciliation.selected_compile_proofs) == 1
    assert reconciliation.modules.built == 1

    missing_selected = reconciliation.model_dump(mode="python")
    missing_selected["selected_compile_proofs"] = ()
    with pytest.raises(ValidationError, match="selected_compile|coverage_current|count"):
        BuildReconciliationV1.model_validate(missing_selected)

    for field, message in (
        ("selected_generator_proofs", "generator"),
        ("selected_package_proofs", "package"),
        ("selected_child_reconciliations", "child"),
    ):
        missing_proof = reconciliation.model_dump(mode="python")
        missing_proof[field] = ()
        with pytest.raises(ValidationError, match=f"{message}|selected|count"):
            BuildReconciliationV1.model_validate(missing_proof)

    duplicate_selected = reconciliation.model_dump(mode="python")
    duplicate_selected["selected_compile_proofs"] = (
        reconciliation.selected_compile_proofs[0],
        reconciliation.selected_compile_proofs[0],
    )
    with pytest.raises(ValidationError, match="selected_compile|duplicate"):
        BuildReconciliationV1.model_validate(duplicate_selected)

    duplicate_child_evidence = reconciliation.model_dump(mode="python")
    original_child = reconciliation.selected_child_reconciliations[0]
    duplicate_child_evidence["selected_child_reconciliations"] = (
        original_child,
        original_child.model_copy(update={"child_scope_id": "child-scope-02"}),
    )
    duplicate_child_evidence["child_scopes"] = {
        "expected": 2,
        "complete": 2,
        "incomplete_or_unverifiable": 0,
    }
    with pytest.raises(
        ValidationError,
        match="child.*(census|expectation|reconciliation).*duplicate|duplicate.*child",
    ):
        BuildReconciliationV1.model_validate(duplicate_child_evidence)

    bad_source_conservation = reconciliation.model_dump(mode="python")
    bad_source_conservation["java_sources"] = {
        **bad_source_conservation["java_sources"],  # type: ignore[misc]
        "project_current": 1,
    }
    with pytest.raises(ValidationError, match="project_current|missing|stale|count"):
        BuildReconciliationV1.model_validate(bad_source_conservation)

    bad_unit_conservation = reconciliation.model_dump(mode="python")
    bad_unit_conservation["generator_units"] = {
        **bad_unit_conservation["generator_units"],  # type: ignore[misc]
        "expected": 2,
    }
    with pytest.raises(ValidationError, match="generator|expected|count"):
        BuildReconciliationV1.model_validate(bad_unit_conservation)

    bad_module_conservation = reconciliation.model_dump(mode="python")
    bad_module_conservation["modules"] = {
        **bad_module_conservation["modules"],  # type: ignore[misc]
        "expected": 2,
    }
    with pytest.raises(ValidationError, match="modules|expected|count"):
        BuildReconciliationV1.model_validate(bad_module_conservation)

    count_mutations = (
        ("source_census", "total_candidates", 3, "source_census|classified|count"),
        ("java_edges", "expected", 2, "java_edges|expected|count"),
        ("compile_units", "expected", 2, "compile|expected|count"),
        ("physical_outputs", "sealed_entries", 4, "sealed|verified|entry"),
        ("package_units", "expected", 2, "package|expected|count"),
        (
            "active_language_units",
            "expected",
            2,
            "language|expected|count",
        ),
        ("child_scopes", "expected", 2, "child|expected|count"),
    )
    for block, field, value, message in count_mutations:
        bad_count = reconciliation.model_dump(mode="python")
        bad_count[block] = {
            **bad_count[block],  # type: ignore[misc]
            field: value,
        }
        with pytest.raises(ValidationError, match=message):
            BuildReconciliationV1.model_validate(bad_count)

    missing_evidence_ref = reconciliation.model_dump(mode="python")
    compile_observation_id = reconciliation.selected_compile_proofs[0].observation_id
    missing_evidence_ref["evidence_refs"] = tuple(
        ref for ref in reconciliation.evidence_refs if ref != compile_observation_id
    )
    with pytest.raises(ValidationError, match="evidence_refs|selected|observation"):
        BuildReconciliationV1.model_validate(missing_evidence_ref)


def test_compile_coverage_remains_current_when_its_output_witness_is_missing() -> None:
    observation = _compile_observation()
    payload = _empty_reconciliation_payload()
    payload.update(
        {
            "status": "partial",
            "source_census": {
                "total_candidates": 1,
                "classified": 1,
                "unclassified": 0,
                "delegated": 0,
                "out_of_scope_by_reason": {},
            },
            "java_sources": {
                "local_expected": 1,
                "local_current": 1,
                "delegated_expected": 0,
                "delegated_current": 0,
                "project_expected": 1,
                "project_current": 1,
                "project_missing": 0,
                "project_stale": 0,
            },
            "java_edges": {"expected": 1, "current": 1, "missing": 0, "stale": 0},
            "compile_units": {
                "expected": 1,
                "coverage_current": 1,
                "coverage_missing_or_invalid": 0,
                "coverage_unverifiable": 0,
            },
            "physical_outputs": {
                "observed_class_count": 0,
                "witnesses_expected": 1,
                "witnesses_verified": 0,
                "sealed_entries": 1,
                "verified_entries": 0,
                "missing_entries": 1,
                "mismatched_entries": 0,
            },
            "active_language_units": {
                "expected": 1,
                "supported_current": 1,
                "unsupported_or_unverifiable": 0,
            },
            "modules": {"expected": 1, "built": 0, "partial": 1, "unverifiable": 0},
            "selected_compile_proofs": (
                SelectedCompileProof(
                    compile_unit_id=observation.compile_unit_id,
                    compile_unit_identity_hash=observation.compile_unit_identity_hash,
                    observation_id=observation.observation_id,
                    witness_id=None,
                ),
            ),
            "evidence_refs": (observation.observation_id,),
        }
    )

    reconciliation = BuildReconciliationV1.model_validate(payload)
    assert reconciliation.compile_units.coverage_current == 1
    assert reconciliation.java_sources.project_current == 1
    assert reconciliation.physical_outputs.witnesses_verified == 0
    assert reconciliation.status == "partial"

    false_complete = reconciliation.model_dump(mode="python")
    false_complete["status"] = "complete"
    false_complete["modules"] = {
        "expected": 1,
        "built": 1,
        "partial": 0,
        "unverifiable": 0,
    }
    false_complete["physical_outputs"] = {
        **false_complete["physical_outputs"],
        "sealed_entries": 0,
        "missing_entries": 0,
    }
    with pytest.raises(ValidationError, match="complete|witness|blocker"):
        BuildReconciliationV1.model_validate(false_complete)


def test_compile_with_no_output_requirement_can_complete_without_a_witness() -> None:
    observation = _compile_observation()
    payload = _empty_reconciliation_payload()
    payload.update(
        {
            "status": "complete",
            "source_census": {
                "total_candidates": 1,
                "classified": 1,
                "unclassified": 0,
                "delegated": 0,
                "out_of_scope_by_reason": {},
            },
            "java_sources": {
                "local_expected": 1,
                "local_current": 1,
                "delegated_expected": 0,
                "delegated_current": 0,
                "project_expected": 1,
                "project_current": 1,
                "project_missing": 0,
                "project_stale": 0,
            },
            "java_edges": {"expected": 1, "current": 1, "missing": 0, "stale": 0},
            "compile_units": {
                "expected": 1,
                "coverage_current": 1,
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
                "expected": 1,
                "supported_current": 1,
                "unsupported_or_unverifiable": 0,
            },
            "modules": {"expected": 1, "built": 1, "partial": 0, "unverifiable": 0},
            "selected_compile_proofs": (
                SelectedCompileProof(
                    compile_unit_id=observation.compile_unit_id,
                    compile_unit_identity_hash=observation.compile_unit_identity_hash,
                    observation_id=observation.observation_id,
                    witness_id=None,
                ),
            ),
            "evidence_refs": (observation.observation_id,),
        }
    )

    reconciliation = BuildReconciliationV1.model_validate(payload)
    assert reconciliation.status == "complete"
    assert reconciliation.physical_outputs.witnesses_expected == 0


def test_delegated_jvm_candidate_count_is_not_forced_to_equal_delegated_java() -> None:
    payload = _nonempty_reconciliation_payload()
    payload["source_census"] = {
        "total_candidates": 3,
        "classified": 3,
        "unclassified": 0,
        # One Java and one Kotlin candidate are conserved through the same
        # required child scope.  The Java grain must count only the Java file.
        "delegated": 2,
        "out_of_scope_by_reason": {},
    }

    reconciliation = BuildReconciliationV1.model_validate(payload)
    assert reconciliation.source_census.delegated == 2
    assert reconciliation.java_sources.delegated_expected == 1
    assert reconciliation.status == "complete"


def test_raw_and_canonical_inputs_are_bounded_before_materialization() -> None:
    oversized_raw = b"{" + b" " * BUILD_EVIDENCE_MAX_RAW_BYTES + b"}"
    with pytest.raises(ValueError, match="raw|bytes|large"):
        validate_build_evidence_json(JvmSourceCandidate, oversized_raw)

    with pytest.raises(ValueError, match="canonical|bytes|large"):
        canonical_content_id(
            "oversized",
            {"text": "x" * (BUILD_EVIDENCE_MAX_CANONICAL_BYTES + 1)},
        )

    with pytest.raises(ValidationError, match="compiler_version|text|length"):
        _compile_observation().model_copy(
            update={"compiler_version": "x" * (BUILD_EVIDENCE_MAX_TEXT_CHARS + 1)}
        )

    too_many_ids = tuple(f"run-{index:05d}" for index in range(BUILD_EVIDENCE_MAX_ENTRIES + 1))
    oversized_evidence_set = _evidence_set_payload()
    oversized_evidence_set["admitted_run_ids"] = too_many_ids
    with pytest.raises(ValidationError, match="admitted_run_ids|length|entries"):
        BuildEvidenceSetSnapshotV1.model_validate(_refresh_evidence_set_id(oversized_evidence_set))

    with pytest.raises(ValidationError, match="profiles|variants|length|entries"):
        _nonempty_build_model().model_copy(update={"active_profiles_or_variants": too_many_ids})


def test_delegation_edge_preserves_child_path_and_content_hash() -> None:
    edge = DelegationEdge(
        parent_source_id=_source().source_id,
        child_scope_id="child-scope-01",
        expected_child_path="src/main/java/example/App.java",
        expected_child_sha256=SHA_A,
    )

    assert edge.expected_child_sha256 == _source().sha256
    with pytest.raises(ValidationError):
        edge.model_copy(update={"expected_child_sha256": "short"})


def test_delegated_classification_has_one_conserving_required_child_edge() -> None:
    source = _source()
    classification = SourceClassification(
        source_id=source.source_id,
        language="java",
        source_kind="main",
        classification="delegated_child_scope",
        classification_basis=ClassificationBasisV1(
            basis_kind="child_scope",
            model_id=MODEL_ID,
            basis_ref="child-scope-01",
        ),
        expected_compile_unit_ids=(),
        child_scope_id="child-scope-01",
    )
    edge = DelegationEdge(
        parent_source_id=source.source_id,
        child_scope_id="child-scope-01",
        expected_child_path=source.path,
        expected_child_sha256=source.sha256,
    )
    expectation_identity: dict[str, object] = {
        "evidence_epoch_id": EPOCH_ID,
        "census_id": CENSUS_ID,
        "scope_id": SCOPE_ID,
        "model_id": MODEL_ID,
        "scope_fingerprint": SHA_B,
        "source_state_fingerprint": SHA_A,
        "build_config_fingerprint": SHA_B,
        "required_java_edges": (),
        "source_classifications": (classification,),
        "unclassified_source_ids": (),
        "required_generator_unit_ids": (),
        "required_compile_unit_ids": (),
        "required_package_unit_ids": (),
        "unsupported_active_unit_ids": (),
        "required_child_scope_ids": ("child-scope-01",),
        "delegation_edges": (edge,),
    }
    expectation = JvmBuildExpectationV1(
        expectation_id=canonical_content_id("jvm-build-expectation-v1", expectation_identity),
        **expectation_identity,
        conflicts=(),
    )
    assert expectation.delegation_edges == (edge,)

    foreign_model_id = canonical_content_id(
        "evaluated-build-model-v1",
        {
            "evidence_epoch_id": EPOCH_ID,
            "scope_id": SCOPE_ID,
            "project_root": "/workspace/foreign",
        },
    )
    foreign_model_classification = classification.model_copy(
        update={
            "classification_basis": classification.classification_basis.model_copy(
                update={"model_id": foreign_model_id}
            )
        }
    )
    foreign_model_payload = expectation.model_dump(mode="python")
    foreign_model_payload["source_classifications"] = (foreign_model_classification,)
    with pytest.raises(ValidationError, match="classification_basis|model_id|model"):
        JvmBuildExpectationV1.model_validate(_refresh_expectation_id(foreign_model_payload))

    wrong_basis_ref_classification = classification.model_copy(
        update={
            "classification_basis": classification.classification_basis.model_copy(
                update={"basis_ref": "child-scope-02"}
            )
        }
    )
    wrong_basis_ref_payload = expectation.model_dump(mode="python")
    wrong_basis_ref_payload["source_classifications"] = (wrong_basis_ref_classification,)
    with pytest.raises(ValidationError, match="basis_ref|child_scope|delegat"):
        JvmBuildExpectationV1.model_validate(_refresh_expectation_id(wrong_basis_ref_payload))

    missing_edge = expectation.model_dump(mode="python")
    missing_edge["delegation_edges"] = ()
    with pytest.raises(ValidationError, match="delegat|edge"):
        JvmBuildExpectationV1.model_validate(_refresh_expectation_id(missing_edge))

    duplicate_edge = expectation.model_dump(mode="python")
    duplicate_edge["delegation_edges"] = (edge, edge)
    with pytest.raises(ValidationError, match="duplicate|delegat"):
        JvmBuildExpectationV1.model_validate(_refresh_expectation_id(duplicate_edge))

    with pytest.raises(ValidationError, match="child_scope_id|delegat|required"):
        classification.model_copy(update={"child_scope_id": None})

    wrong_parent = expectation.model_dump(mode="python")
    wrong_parent["delegation_edges"] = (
        edge.model_copy(
            update={
                "parent_source_id": canonical_content_id(
                    "jvm-source",
                    {"path": "src/main/java/Other.java", "sha256": SHA_A},
                )
            }
        ),
    )
    with pytest.raises(ValidationError, match="parent|source|delegat"):
        JvmBuildExpectationV1.model_validate(_refresh_expectation_id(wrong_parent))

    wrong_child = expectation.model_dump(mode="python")
    wrong_child["delegation_edges"] = (
        edge.model_copy(update={"child_scope_id": "child-scope-02"}),
    )
    with pytest.raises(ValidationError, match="child_scope|delegat|required"):
        JvmBuildExpectationV1.model_validate(_refresh_expectation_id(wrong_child))

    missing_required_child = expectation.model_dump(mode="python")
    missing_required_child["required_child_scope_ids"] = ()
    with pytest.raises(ValidationError, match="required_child|delegat"):
        JvmBuildExpectationV1.model_validate(_refresh_expectation_id(missing_required_child))

    with pytest.raises(ValidationError, match="expectation_id|identity"):
        expectation.model_copy(update={"build_config_fingerprint": SHA_A})
    with pytest.raises(ValidationError, match="child_scope_id|delegat"):
        classification.model_copy(
            update={"classification": "explicitly_excluded", "child_scope_id": "child-scope-01"}
        )


def test_sealed_envelope_rejects_future_schema_and_changed_content_under_same_id() -> None:
    body = _compile_observation()
    envelope = SealedBuildEvidenceV1.seal(
        record_kind="compile_observation",
        body=body,
        evidence_epoch_id=EPOCH_ID,
        producer_role="physical_validator",
        created_at="2026-08-11T12:00:00Z",
    )
    assert envelope.schema_version == BUILD_EVIDENCE_SCHEMA_VERSION

    with pytest.raises(ValidationError, match="schema_version"):
        envelope.model_copy(update={"schema_version": BUILD_EVIDENCE_SCHEMA_VERSION + 1})
    with pytest.raises(ValidationError, match="record_id|content_sha256"):
        envelope.model_copy(update={"body": body.model_copy(update={"outcome": "up_to_date"})})
    with pytest.raises(ValidationError, match="record_kind|body"):
        envelope.model_copy(update={"record_kind": "package_observation"})


def test_sealed_envelope_maps_every_immutable_body_to_its_primary_id_and_epoch() -> None:
    epoch = ContainerEvidenceEpochV1(
        evidence_epoch_id=EPOCH_ID,
        immutable_container_id=CONTAINER_ID,
        project_root=PROJECT_ROOT,
        created_at="2026-08-11T12:00:00Z",
        initial_source_state_fingerprint=SHA_A,
        baseline_output_root_digests=(),
    )
    expectation = JvmBuildExpectationV1(
        expectation_id=EXPECTATION_ID,
        evidence_epoch_id=EPOCH_ID,
        census_id=CENSUS_ID,
        scope_id=SCOPE_ID,
        model_id=MODEL_ID,
        scope_fingerprint=SHA_B,
        source_state_fingerprint=SHA_A,
        build_config_fingerprint=SHA_B,
        required_java_edges=(),
        source_classifications=(),
        unclassified_source_ids=(),
        required_generator_unit_ids=(),
        required_compile_unit_ids=(),
        required_package_unit_ids=(),
        unsupported_active_unit_ids=(),
        required_child_scope_ids=(),
        delegation_edges=(),
        conflicts=(),
    )
    physical = CurrentPhysicalBasisSnapshotV1(
        physical_basis_snapshot_id=PHYSICAL_BASIS_ID,
        evidence_epoch_id=EPOCH_ID,
        expectation_id=EXPECTATION_ID,
        build_model_id=MODEL_ID,
        requested_member_ids=(),
        current_members=(),
        current_output_roots=(),
        current_output_entries=(),
        missing_members=(),
        unreadable_members=(),
        conflicts=(),
    )
    generator = _generator_observation()
    compile_observation = _compile_observation()
    package = _package_observation()
    witness = _compile_output_witness(compile_observation)
    evidence_set = BuildEvidenceSetSnapshotV1.model_validate(_evidence_set_payload())
    reconciliation = BuildReconciliationV1.model_validate(_empty_reconciliation_payload())
    immutable_records = (
        ("container_evidence_epoch", epoch, epoch.evidence_epoch_id),
        ("jvm_build_expectation", expectation, expectation.expectation_id),
        (
            "current_physical_basis",
            physical,
            physical.physical_basis_snapshot_id,
        ),
        ("generator_observation", generator, generator.observation_id),
        (
            "compile_observation",
            compile_observation,
            compile_observation.observation_id,
        ),
        ("package_observation", package, package.observation_id),
        ("output_witness", witness, witness.witness_id),
        ("build_evidence_set", evidence_set, evidence_set.snapshot_id),
        (
            "build_reconciliation",
            reconciliation,
            reconciliation.reconciliation_id,
        ),
    )
    wrong_primary_ids = {
        "container_evidence_epoch": canonical_content_id(
            "container-evidence-epoch-v1",
            {
                "immutable_container_id": "container-foreign",
                "project_root": PROJECT_ROOT,
            },
        ),
        "jvm_build_expectation": canonical_content_id(
            "jvm-build-expectation-v1", {"wrong_projection": True}
        ),
        "current_physical_basis": canonical_content_id(
            "current-physical-basis-v1", {"wrong_projection": True}
        ),
        "generator_observation": canonical_content_id(
            "generator-observation-v1",
            {
                "evidence_epoch_id": EPOCH_ID,
                "run_id": "run-foreign",
                "receipt_id": "receipt-foreign",
                "contract_id": "contract-foreign",
                "generator_unit_id": "generator-foreign",
            },
        ),
        "compile_observation": canonical_content_id(
            "compile-observation-v1",
            {
                "evidence_epoch_id": EPOCH_ID,
                "run_id": "run-foreign",
                "receipt_id": "receipt-foreign",
                "contract_id": "contract-foreign",
                "compile_unit_id": "compile-foreign",
            },
        ),
        "package_observation": canonical_content_id(
            "package-observation-v1",
            {
                "evidence_epoch_id": EPOCH_ID,
                "run_id": "run-foreign",
                "receipt_id": "receipt-foreign",
                "contract_id": "contract-foreign",
                "package_unit_id": "package-foreign",
            },
        ),
        "output_witness": canonical_content_id(
            "physical-output-witness-v1",
            {
                "evidence_epoch_id": EPOCH_ID,
                "producer_observation_kind": "compile",
                "producer_observation_id": "compile-observation-foreign",
            },
        ),
        "build_evidence_set": canonical_content_id(
            "build-evidence-set-v1", {"wrong_projection": True}
        ),
        "build_reconciliation": canonical_content_id(
            "build-reconciliation-v1",
            {"evidence_epoch_id": EPOCH_ID, "input_set_digest": SHA_B},
        ),
    }
    foreign_epoch_id = canonical_content_id(
        "container-evidence-epoch-v1",
        {
            "immutable_container_id": "container-foreign",
            "project_root": PROJECT_ROOT,
        },
    )
    for record_kind, body, primary_id in immutable_records:
        sealed = SealedBuildEvidenceV1.seal(
            record_kind=record_kind,
            body=body,
            evidence_epoch_id=EPOCH_ID,
            producer_role="physical_validator",
            created_at="2026-08-11T12:00:00Z",
        )
        assert sealed.record_id == primary_id
        assert sealed.evidence_epoch_id == body.evidence_epoch_id

        wrong_record_id = wrong_primary_ids[record_kind]
        with pytest.raises(ValidationError, match="record_id|primary|identity"):
            SealedBuildEvidenceV1.seal(
                record_kind=record_kind,
                record_id=wrong_record_id,
                body=body,
                evidence_epoch_id=EPOCH_ID,
                producer_role="physical_validator",
                created_at="2026-08-11T12:00:00Z",
            )
        with pytest.raises(ValidationError, match="epoch|evidence_epoch_id"):
            SealedBuildEvidenceV1.seal(
                record_kind=record_kind,
                body=body,
                evidence_epoch_id=foreign_epoch_id,
                producer_role="physical_validator",
                created_at="2026-08-11T12:00:00Z",
            )


def test_all_mutable_envelopes_bind_body_revision_and_logical_identity() -> None:
    scope = BuildGoalScopeV1(
        scope_id=SCOPE_ID,
        evidence_epoch_id=EPOCH_ID,
        authority_ref="request-01",
        scope_revision=1,
        predecessor_scope_hash=None,
        validation_target="project",
        requested_action="build",
        requested_lifecycle="package",
        requested_modules_or_domains=(),
        requested_profiles_or_variants=(),
        source_set_roles=("production",),
        scope_fingerprint=SHA_B,
    )
    census = JvmSourceCensusV1(
        census_id=CENSUS_ID,
        evidence_epoch_id=EPOCH_ID,
        project_root=PROJECT_ROOT,
        census_revision=1,
        source_state_fingerprint=SHA_A,
        sources=(),
        unreadable_roots=(),
        conflicts=(),
    )
    build_model = EvaluatedBuildModelV1(
        model_id=MODEL_ID,
        evidence_epoch_id=EPOCH_ID,
        scope_id=SCOPE_ID,
        goal_scope_revision=1,
        goal_scope_fingerprint=SHA_B,
        project_root=PROJECT_ROOT,
        model_revision=1,
        build_config_fingerprint=SHA_A,
        active_profiles_or_variants=(),
        build_config_members=(),
        modules=(),
        generator_units=(),
        compile_units=(),
        package_units=(),
        compile_dependency_edges=(),
        generated_root_contracts=(),
        child_build_scopes=(),
        conflicts=(),
    )
    final_basis = FinalBuildBasisV1(
        basis_id=FINAL_BASIS_ID,
        evidence_epoch_id=EPOCH_ID,
        basis_revision=1,
        expectation_id=EXPECTATION_ID,
        physical_basis_snapshot_id=PHYSICAL_BASIS_ID,
        source_state_fingerprint=SHA_A,
        build_config_fingerprint=SHA_B,
        generator_unit_bases=(),
        compile_unit_bases=(),
        package_unit_bases=(),
        conflicts=(),
    )
    mutable_records = (
        ("build_goal_scope", scope, scope.scope_id),
        ("jvm_source_census", census, census.census_id),
        ("evaluated_build_model", build_model, build_model.model_id),
        ("final_build_basis", final_basis, final_basis.basis_id),
    )
    wrong_logical_ids = {
        "build_goal_scope": canonical_content_id(
            "build-goal-scope-v1",
            {"evidence_epoch_id": EPOCH_ID, "authority_ref": "request-foreign"},
        ),
        "jvm_source_census": canonical_content_id(
            "jvm-source-census-v1",
            {"evidence_epoch_id": EPOCH_ID, "project_root": "/workspace/foreign"},
        ),
        "evaluated_build_model": canonical_content_id(
            "evaluated-build-model-v1",
            {
                "evidence_epoch_id": EPOCH_ID,
                "scope_id": SCOPE_ID,
                "project_root": "/workspace/foreign",
            },
        ),
        "final_build_basis": canonical_content_id(
            "final-build-basis-v1",
            {
                "evidence_epoch_id": EPOCH_ID,
                "expectation_id": canonical_content_id(
                    "jvm-build-expectation-v1", {"wrong_projection": True}
                ),
            },
        ),
    }
    for record_kind, body, logical_id in mutable_records:
        sealed = SealedBuildEvidenceV1.seal(
            record_kind=record_kind,
            record_id=logical_id,
            logical_artifact_id=logical_id,
            revision=1,
            predecessor_content_sha256=BUILD_EVIDENCE_GENESIS_SHA256,
            body=body,
            evidence_epoch_id=EPOCH_ID,
            producer_role="physical_validator",
            created_at="2026-08-11T12:00:00Z",
        )
        assert sealed.record_id == logical_id
        with pytest.raises(ValidationError, match="revision|body"):
            sealed.model_copy(
                update={
                    "revision": 2,
                    "predecessor_content_sha256": sealed.content_sha256,
                }
            )
        wrong_logical_id = wrong_logical_ids[record_kind]
        with pytest.raises(ValidationError, match="record_id|logical|primary|identity"):
            SealedBuildEvidenceV1.seal(
                record_kind=record_kind,
                record_id=wrong_logical_id,
                logical_artifact_id=wrong_logical_id,
                revision=1,
                predecessor_content_sha256=BUILD_EVIDENCE_GENESIS_SHA256,
                body=body,
                evidence_epoch_id=EPOCH_ID,
                producer_role="physical_validator",
                created_at="2026-08-11T12:00:00Z",
            )

    scope_v1_envelope = SealedBuildEvidenceV1.seal(
        record_kind="build_goal_scope",
        record_id=scope.scope_id,
        logical_artifact_id=scope.scope_id,
        revision=1,
        predecessor_content_sha256=BUILD_EVIDENCE_GENESIS_SHA256,
        body=scope,
        evidence_epoch_id=EPOCH_ID,
        producer_role="physical_validator",
        created_at="2026-08-11T12:00:00Z",
    )
    scope_v2 = scope.model_copy(
        update={
            "scope_revision": 2,
            "predecessor_scope_hash": scope_v1_envelope.content_sha256,
        }
    )
    scope_v2_envelope = SealedBuildEvidenceV1.seal(
        record_kind="build_goal_scope",
        record_id=scope.scope_id,
        logical_artifact_id=scope.scope_id,
        revision=2,
        predecessor_content_sha256=scope_v1_envelope.content_sha256,
        body=scope_v2,
        evidence_epoch_id=EPOCH_ID,
        producer_role="physical_validator",
        created_at="2026-08-11T12:01:00Z",
    )
    assert scope_v2_envelope.revision == 2

    with pytest.raises(ValidationError, match="scope.*predecessor|predecessor.*scope"):
        SealedBuildEvidenceV1.seal(
            record_kind="build_goal_scope",
            record_id=scope.scope_id,
            logical_artifact_id=scope.scope_id,
            revision=2,
            predecessor_content_sha256=SHA_A,
            body=scope_v2,
            evidence_epoch_id=EPOCH_ID,
            producer_role="physical_validator",
            created_at="2026-08-11T12:01:00Z",
        )


def test_mutable_envelope_requires_stable_logical_revision_predecessor_chain() -> None:
    census = JvmSourceCensusV1(
        census_id=CENSUS_ID,
        evidence_epoch_id=EPOCH_ID,
        project_root="/workspace/project",
        census_revision=1,
        source_state_fingerprint=SHA_A,
        sources=(_source(),),
        unreadable_roots=(),
        conflicts=(),
    )
    envelope = SealedBuildEvidenceV1.seal(
        record_kind="jvm_source_census",
        record_id=CENSUS_ID,
        logical_artifact_id=CENSUS_ID,
        revision=1,
        predecessor_content_sha256=BUILD_EVIDENCE_GENESIS_SHA256,
        body=census,
        evidence_epoch_id=EPOCH_ID,
        producer_role="physical_validator",
        created_at="2026-08-11T12:00:00Z",
    )
    assert envelope.revision == 1

    census_v2 = census.model_copy(update={"census_revision": 2, "source_state_fingerprint": SHA_B})
    envelope_v2 = SealedBuildEvidenceV1.seal(
        record_kind="jvm_source_census",
        record_id=CENSUS_ID,
        logical_artifact_id=CENSUS_ID,
        revision=2,
        predecessor_content_sha256=envelope.content_sha256,
        body=census_v2,
        evidence_epoch_id=EPOCH_ID,
        producer_role="physical_validator",
        created_at="2026-08-11T12:01:00Z",
    )
    assert envelope_v2.predecessor_content_sha256 == envelope.content_sha256

    with pytest.raises(ValidationError, match="revision|predecessor"):
        envelope.model_copy(update={"revision": 0})
    with pytest.raises(ValidationError, match="logical_artifact_id|record_id"):
        envelope.model_copy(update={"logical_artifact_id": "other-head"})
    with pytest.raises(ValidationError, match="revision|body"):
        envelope.model_copy(
            update={
                "revision": 2,
                "predecessor_content_sha256": envelope.content_sha256,
            }
        )
    with pytest.raises(ValidationError, match="genesis|predecessor"):
        SealedBuildEvidenceV1.seal(
            record_kind="jvm_source_census",
            record_id=CENSUS_ID,
            logical_artifact_id=CENSUS_ID,
            revision=1,
            predecessor_content_sha256=SHA_A,
            body=census,
            evidence_epoch_id=EPOCH_ID,
            producer_role="physical_validator",
            created_at="2026-08-11T12:00:00Z",
        )
    with pytest.raises(ValidationError, match="genesis|predecessor"):
        SealedBuildEvidenceV1.seal(
            record_kind="jvm_source_census",
            record_id=CENSUS_ID,
            logical_artifact_id=CENSUS_ID,
            revision=2,
            predecessor_content_sha256=BUILD_EVIDENCE_GENESIS_SHA256,
            body=census_v2,
            evidence_epoch_id=EPOCH_ID,
            producer_role="physical_validator",
            created_at="2026-08-11T12:01:00Z",
        )
    with pytest.raises(ValidationError, match="immutable|logical|revision"):
        SealedBuildEvidenceV1.seal(
            record_kind="compile_observation",
            record_id=_compile_observation().observation_id,
            logical_artifact_id=_compile_observation().observation_id,
            revision=1,
            predecessor_content_sha256=BUILD_EVIDENCE_GENESIS_SHA256,
            body=_compile_observation(),
            evidence_epoch_id=EPOCH_ID,
            producer_role="physical_validator",
            created_at="2026-08-11T12:00:00Z",
        )
    with pytest.raises(ValidationError, match="epoch|evidence_epoch_id"):
        SealedBuildEvidenceV1.seal(
            record_kind="jvm_source_census",
            record_id=CENSUS_ID,
            logical_artifact_id=CENSUS_ID,
            revision=1,
            predecessor_content_sha256=BUILD_EVIDENCE_GENESIS_SHA256,
            body=census,
            evidence_epoch_id=canonical_content_id(
                "container-evidence-epoch-v1",
                {
                    "immutable_container_id": "container-foreign",
                    "project_root": PROJECT_ROOT,
                },
            ),
            producer_role="physical_validator",
            created_at="2026-08-11T12:00:00Z",
        )
