"""Pure filesystem-first JVM expectation and reconciliation kernel.

The functions in this module consume only the strict, already-sealed evidence
models from :mod:`sag.agent.jvm_build_models`.  They deliberately perform no
filesystem reads, process execution, publication, rendering, or live-validator
calls.  This keeps the independent denominator, final physical basis, and the
observation fold as three distinct authorities.
"""

from __future__ import annotations

import fnmatch
import posixpath
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence, TypeVar

from pydantic import field_validator, model_validator

from sag.agent.jvm_build_models import (
    ActiveLanguageUnitCounts,
    BuildEvidenceSetSnapshotV1,
    BuildGoalScopeV1,
    BuildPathBasis,
    BuildReconciliationV1,
    ChildScopeCounts,
    ClassificationBasisV1,
    CompileOutputDigestEntry,
    CompileUnitCounts,
    CompileUnitExpectation,
    CompileUnitObservationV1,
    ContainerEvidenceEpochV1,
    CurrentOutputRootV1,
    CurrentPhysicalBasisSnapshotV1,
    CurrentPhysicalMemberV1,
    DelegationEdge,
    EvaluatedBuildModelV1,
    FinalBuildBasisV1,
    FinalCompileUnitBasis,
    FinalGeneratorUnitBasis,
    FinalPackageUnitBasis,
    GeneratedRootContract,
    GeneratorUnitCounts,
    GeneratorUnitExpectation,
    GeneratorUnitObservationV1,
    JavaEdgeCounts,
    JavaSourceCounts,
    JvmBuildExpectationV1,
    JvmLanguage,
    JvmSourceCandidate,
    JvmSourceCensusV1,
    ModuleCounts,
    OutputRootSpec,
    PackageUnitCounts,
    PackageUnitExpectation,
    PackageUnitObservationV1,
    PathKind,
    PhysicalMemberRole,
    PhysicalOutputCounts,
    PhysicalOutputWitnessV1,
    RequestedArtifactSpec,
    RequiredSourceEdge,
    ResolvedDigestEntry,
    ResolvedMemberSpec,
    SelectedChildReconciliation,
    SelectedCompileProof,
    SelectedGeneratorProof,
    SelectedPackageProof,
    SourceCensusCounts,
    SourceClassification,
    SourceDigestEntry,
    StrictBuildEvidenceModel,
    VerificationMode,
    canonical_build_evidence_sha256,
    canonical_content_id,
    compile_unit_identity_hash,
    generator_unit_identity_hash,
    package_unit_identity_hash,
    validate_bounded_sequence,
    validate_bounded_text,
)

_POSITIVE_OUTCOMES = frozenset({"executed_success", "current_noop", "up_to_date", "from_cache"})
_OUT_OF_SCOPE_CLASSIFICATIONS = frozenset(
    {
        "explicitly_excluded",
        "inactive_profile",
        "out_of_lifecycle_scope",
        "vendor",
        "compiler_intermediate",
    }
)

_CanonicalModelT = TypeVar("_CanonicalModelT")
_SourceKind = Literal["main", "test", "generated", "custom"]
_Classification = Literal[
    "required",
    "explicitly_excluded",
    "inactive_profile",
    "out_of_lifecycle_scope",
    "delegated_child_scope",
    "vendor",
    "compiler_intermediate",
    "unsupported_active",
]
_ReconciliationStatus = Literal["complete", "partial", "absent", "unverifiable"]
_SOURCE_SUFFIX_LANGUAGES: dict[str, JvmLanguage] = {
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".groovy": "groovy",
    ".aj": "aspectj",
}


def _expectation_obligation_projection(expectation: JvmBuildExpectationV1) -> Any:
    return expectation.model_dump(
        mode="python",
        exclude={"expectation_id"},
    )


def _final_basis_semantic_projection(final_basis: FinalBuildBasisV1) -> Any:
    return final_basis.model_dump(mode="python", exclude={"basis_id"})


def _canonical_models(
    value: Sequence[_CanonicalModelT],
    *,
    field: str,
    key: Callable[[_CanonicalModelT], str],
) -> tuple[_CanonicalModelT, ...]:
    validate_bounded_sequence(value, field=field)
    items = tuple(value)
    keys = tuple(key(item) for item in items)
    if len(set(keys)) != len(keys):
        raise ValueError(f"{field} contains a duplicate")
    if keys != tuple(sorted(keys)):
        raise ValueError(f"{field} must be in canonical sorted order")
    return items


def _canonical_conflicts(value: Sequence[str], *, field: str = "conflicts") -> tuple[str, ...]:
    items = validate_bounded_sequence(value, field=field)
    normalized = tuple(validate_bounded_text(item, field=field) for item in items)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} contains a duplicate")
    if normalized != tuple(sorted(normalized)):
        raise ValueError(f"{field} must be in canonical sorted order")
    return normalized


class ReconciliationInputs(StrictBuildEvidenceModel):
    """One closed, immutable input boundary for the pure reconciliation fold."""

    epoch: ContainerEvidenceEpochV1
    goal_scope: BuildGoalScopeV1
    census: JvmSourceCensusV1
    build_model: EvaluatedBuildModelV1
    expectation: JvmBuildExpectationV1
    final_basis: FinalBuildBasisV1
    physical_basis: CurrentPhysicalBasisSnapshotV1
    evidence_set: BuildEvidenceSetSnapshotV1

    @model_validator(mode="after")
    def _validate_cross_links(self) -> ReconciliationInputs:
        epoch_id = self.epoch.evidence_epoch_id
        epoch_fields = (
            self.goal_scope.evidence_epoch_id,
            self.census.evidence_epoch_id,
            self.build_model.evidence_epoch_id,
            self.expectation.evidence_epoch_id,
            self.final_basis.evidence_epoch_id,
            self.physical_basis.evidence_epoch_id,
            self.evidence_set.evidence_epoch_id,
        )
        if any(candidate != epoch_id for candidate in epoch_fields):
            raise ValueError("reconciliation input evidence epoch cross-link is inconsistent")
        if self.epoch.project_root != self.census.project_root:
            raise ValueError("epoch and source census project-root cross-link is inconsistent")
        if self.census.project_root != self.build_model.project_root:
            raise ValueError(
                "source census and build model project-root cross-link is inconsistent"
            )
        if self.goal_scope.scope_id != self.build_model.scope_id:
            raise ValueError("goal scope and build model ID cross-link is inconsistent")
        if (
            self.goal_scope.scope_revision != self.build_model.goal_scope_revision
            or self.goal_scope.scope_fingerprint != self.build_model.goal_scope_fingerprint
        ):
            raise ValueError("build model is not current for the goal scope revision/fingerprint")
        if (
            self.expectation.census_id != self.census.census_id
            or self.expectation.scope_id != self.goal_scope.scope_id
            or self.expectation.model_id != self.build_model.model_id
        ):
            raise ValueError("expectation input ID cross-links are inconsistent")
        if self.expectation.scope_fingerprint != self.goal_scope.scope_fingerprint:
            raise ValueError("expectation scope fingerprint is not current")
        if self.expectation.source_state_fingerprint != self.census.source_state_fingerprint:
            raise ValueError("expectation source-state fingerprint cross-link is inconsistent")
        if self.expectation.build_config_fingerprint != self.build_model.build_config_fingerprint:
            raise ValueError("expectation build-config fingerprint cross-link is inconsistent")
        if self.final_basis.expectation_id != self.expectation.expectation_id:
            raise ValueError("final basis expectation ID cross-link is inconsistent")
        if (
            self.final_basis.physical_basis_snapshot_id
            != self.physical_basis.physical_basis_snapshot_id
        ):
            raise ValueError("final basis physical snapshot ID cross-link is inconsistent")
        if self.final_basis.source_state_fingerprint != self.census.source_state_fingerprint:
            raise ValueError("final basis source-state fingerprint cross-link is inconsistent")
        if self.final_basis.build_config_fingerprint != self.build_model.build_config_fingerprint:
            raise ValueError("final basis build-config fingerprint cross-link is inconsistent")
        if (
            self.physical_basis.expectation_id != self.expectation.expectation_id
            or self.physical_basis.build_model_id != self.build_model.model_id
        ):
            raise ValueError("physical basis input ID cross-links are inconsistent")

        census_source_ids = {source.source_id for source in self.census.sources}
        classified_ids = {
            classification.source_id for classification in self.expectation.source_classifications
        }
        unclassified_ids = set(self.expectation.unclassified_source_ids)
        if classified_ids | unclassified_ids != census_source_ids:
            raise ValueError("expectation source classification does not partition the census")

        required_generator_ids = set(self.expectation.required_generator_unit_ids)
        required_compile_ids = set(self.expectation.required_compile_unit_ids)
        required_package_ids = set(self.expectation.required_package_unit_ids)
        model_generator_ids = {
            unit.generator_unit_id
            for unit in self.build_model.generator_units
            if unit.role == "required"
        }
        model_compile_ids = {
            unit.compile_unit_id
            for unit in self.build_model.compile_units
            if unit.role == "required" and unit.adapter_status == "supported"
        }
        model_package_ids = {
            unit.package_unit_id
            for unit in self.build_model.package_units
            if unit.role == "required"
        }
        if (
            required_generator_ids != model_generator_ids
            or required_compile_ids != model_compile_ids
            or required_package_ids != model_package_ids
        ):
            raise ValueError("expectation required-unit IDs differ from the build model")
        if not {
            basis.generator_unit_id for basis in self.final_basis.generator_unit_bases
        }.issubset(required_generator_ids):
            raise ValueError("final generator basis contains an unexpected unit ID")
        if not {basis.compile_unit_id for basis in self.final_basis.compile_unit_bases}.issubset(
            required_compile_ids
        ):
            raise ValueError("final compile basis contains an unexpected unit ID")
        if not {basis.package_unit_id for basis in self.final_basis.package_unit_bases}.issubset(
            required_package_ids
        ):
            raise ValueError("final package basis contains an unexpected unit ID")
        derived = derive_expectation(
            self.census,
            self.goal_scope,
            self.build_model,
        )
        if _expectation_obligation_projection(
            self.expectation
        ) != _expectation_obligation_projection(derived):
            raise ValueError("published expectation differs from the independent scope derivation")
        derived_final_basis = derive_final_basis(
            self.census,
            self.expectation,
            self.build_model,
            self.physical_basis,
            basis_revision=self.final_basis.basis_revision,
        )
        if _final_basis_semantic_projection(self.final_basis) != _final_basis_semantic_projection(
            derived_final_basis
        ):
            raise ValueError(
                "published final basis differs from the independent physical derivation"
            )
        return self


class ReconciliationSelections(StrictBuildEvidenceModel):
    """The exact proof assignment used by one fold and its input digest."""

    selected_generator_proofs: tuple[SelectedGeneratorProof, ...]
    selected_compile_proofs: tuple[SelectedCompileProof, ...]
    selected_package_proofs: tuple[SelectedPackageProof, ...]
    selected_child_reconciliations: tuple[SelectedChildReconciliation, ...]
    conflicts: tuple[str, ...]

    @field_validator("selected_generator_proofs")
    @classmethod
    def _validate_generators(
        cls, value: tuple[SelectedGeneratorProof, ...]
    ) -> tuple[SelectedGeneratorProof, ...]:
        return _canonical_models(
            value,
            field="selected_generator_proofs",
            key=lambda item: item.generator_unit_id,
        )

    @field_validator("selected_compile_proofs")
    @classmethod
    def _validate_compilers(
        cls, value: tuple[SelectedCompileProof, ...]
    ) -> tuple[SelectedCompileProof, ...]:
        return _canonical_models(
            value,
            field="selected_compile_proofs",
            key=lambda item: item.compile_unit_id,
        )

    @field_validator("selected_package_proofs")
    @classmethod
    def _validate_packages(
        cls, value: tuple[SelectedPackageProof, ...]
    ) -> tuple[SelectedPackageProof, ...]:
        return _canonical_models(
            value,
            field="selected_package_proofs",
            key=lambda item: item.package_unit_id,
        )

    @field_validator("selected_child_reconciliations")
    @classmethod
    def _validate_children(
        cls, value: tuple[SelectedChildReconciliation, ...]
    ) -> tuple[SelectedChildReconciliation, ...]:
        children = _canonical_models(
            value,
            field="selected_child_reconciliations",
            key=lambda item: item.child_scope_id,
        )
        for attribute in (
            "child_census_id",
            "child_expectation_id",
            "reconciliation_id",
        ):
            identifiers = tuple(getattr(item, attribute) for item in children)
            if len(set(identifiers)) != len(identifiers):
                raise ValueError(f"selected_child_reconciliations contains a duplicate {attribute}")
        return children

    @field_validator("conflicts")
    @classmethod
    def _validate_conflict_set(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_conflicts(value)


def _fingerprint(kind: str, payload: Any) -> str:
    return str(
        canonical_build_evidence_sha256(
            {
                "fingerprint_kind": kind,
                "schema_version": 1,
                "payload": payload,
            }
        )
    )


def _matches_source_rule(relative_path: str, pattern: str) -> bool:
    path_segments = tuple(relative_path.split("/"))
    pattern_segments = tuple(pattern.split("/"))

    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_segments):
            return path_index == len(path_segments)
        pattern_segment = pattern_segments[pattern_index]
        if pattern_segment == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(path_segments) and match(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_segments)
            and fnmatch.fnmatchcase(path_segments[path_index], pattern_segment)
            and match(path_index + 1, pattern_index + 1)
        )

    return match(0, 0)


def _source_membership(
    source: JvmSourceCandidate,
    unit: CompileUnitExpectation,
) -> tuple[Literal["owned", "excluded", "none"], str | None]:
    for root in unit.source_roots:
        prefix = root.rstrip("/") + "/"
        if not source.path.startswith(prefix):
            continue
        relative = source.path[len(prefix) :]
        if not relative:
            continue
        included = not unit.include_rules or any(
            _matches_source_rule(relative, rule.pattern) for rule in unit.include_rules
        )
        if not included:
            continue
        matching_exclude = next(
            (rule for rule in unit.exclude_rules if _matches_source_rule(relative, rule.pattern)),
            None,
        )
        if matching_exclude is not None:
            return "excluded", matching_exclude.rule_id
        return "owned", None
    return "none", None


def _source_matches_unit(source: JvmSourceCandidate, unit: CompileUnitExpectation) -> bool:
    membership, _ = _source_membership(source, unit)
    return membership == "owned"


def _source_kind(source: JvmSourceCandidate) -> _SourceKind:
    if source.role_hint == "production":
        return "main"
    if source.role_hint == "test":
        return "test"
    return "custom"


def _source_language_conflict(source: JvmSourceCandidate) -> str | None:
    suffix = posixpath.splitext(source.path)[1].lower()
    expected_language = _SOURCE_SUFFIX_LANGUAGES.get(suffix)
    if expected_language is None:
        if source.language == "other":
            return None
        return (
            f"source {source.source_id!r} language {source.language!r} has an "
            f"unrecognized suffix {suffix!r}"
        )
    if source.language == expected_language:
        return None
    return (
        f"source {source.source_id!r} suffix {suffix!r} requires language "
        f"{expected_language!r}, not {source.language!r}"
    )


def _effective_source_language(source: JvmSourceCandidate) -> JvmLanguage:
    suffix = posixpath.splitext(source.path)[1].lower()
    return _SOURCE_SUFFIX_LANGUAGES.get(suffix, "other")


def _relative_child_root(project_root: str, child_root: str) -> str | None:
    prefix = project_root.rstrip("/") + "/"
    if not child_root.startswith(prefix):
        return None
    return child_root[len(prefix) :]


def derive_expectation(
    census: JvmSourceCensusV1,
    goal_scope: BuildGoalScopeV1,
    build_model: EvaluatedBuildModelV1,
) -> JvmBuildExpectationV1:
    """Derive the independent source/unit denominator without task evidence."""

    if census.evidence_epoch_id != goal_scope.evidence_epoch_id:
        raise ValueError("census and goal scope evidence epochs differ")
    if build_model.evidence_epoch_id != goal_scope.evidence_epoch_id:
        raise ValueError("build model and goal scope evidence epochs differ")
    if build_model.scope_id != goal_scope.scope_id:
        raise ValueError("build model scope ID differs from the goal scope")
    if (
        build_model.goal_scope_revision != goal_scope.scope_revision
        or build_model.goal_scope_fingerprint != goal_scope.scope_fingerprint
    ):
        raise ValueError("build model is not current for the goal scope")
    if build_model.project_root != census.project_root:
        raise ValueError("build model and census project roots differ")

    active_units = tuple(
        unit
        for unit in build_model.compile_units
        if unit.role == "required" and unit.scope_disposition == "active"
    )
    required_compile_ids = tuple(
        unit.compile_unit_id for unit in active_units if unit.adapter_status == "supported"
    )
    unsupported_ids = tuple(
        unit.compile_unit_id for unit in active_units if unit.adapter_status == "unsupported_active"
    )
    inactive_units = tuple(
        unit
        for unit in build_model.compile_units
        if unit.role != "required" or unit.scope_disposition != "active"
    )
    child_roots = tuple(
        (
            child,
            _relative_child_root(census.project_root, child.child_project_root),
        )
        for child in build_model.child_build_scopes
    )
    generated_contracts = tuple(build_model.generated_root_contracts)

    classifications: list[SourceClassification] = []
    edges: list[RequiredSourceEdge] = []
    delegations: list[DelegationEdge] = []
    unclassified: list[str] = []

    for source in census.sources:
        source_language = _effective_source_language(source)
        delegated = next(
            (
                (child, relative_root)
                for child, relative_root in child_roots
                if relative_root is not None
                and (
                    source.path == relative_root
                    or source.path.startswith(relative_root.rstrip("/") + "/")
                )
            ),
            None,
        )
        if delegated is not None:
            child, relative_root = delegated
            assert relative_root is not None
            child_path = source.path[len(relative_root) :].lstrip("/")
            classifications.append(
                SourceClassification(
                    source_id=source.source_id,
                    language=source_language,
                    source_kind=_source_kind(source),
                    classification="delegated_child_scope",
                    classification_basis=ClassificationBasisV1(
                        basis_kind="child_scope",
                        model_id=build_model.model_id,
                        basis_ref=child.child_scope_id,
                    ),
                    expected_compile_unit_ids=(),
                    child_scope_id=child.child_scope_id,
                )
            )
            delegations.append(
                DelegationEdge(
                    parent_source_id=source.source_id,
                    child_scope_id=child.child_scope_id,
                    expected_child_path=child_path,
                    expected_child_sha256=source.sha256,
                )
            )
            continue

        intermediate = next(
            (
                contract
                for contract in generated_contracts
                if contract.mode == "same_round_intermediate"
                and (
                    source.path == contract.output_root.path
                    or source.path.startswith(contract.output_root.path.rstrip("/") + "/")
                )
            ),
            None,
        )
        if intermediate is not None:
            classifications.append(
                SourceClassification(
                    source_id=source.source_id,
                    language=source_language,
                    source_kind="generated",
                    classification="compiler_intermediate",
                    classification_basis=ClassificationBasisV1(
                        basis_kind="generated_contract",
                        model_id=build_model.model_id,
                        basis_ref=intermediate.generated_root_id,
                    ),
                    expected_compile_unit_ids=(),
                    child_scope_id=None,
                )
            )
            continue

        owners = tuple(unit for unit in active_units if _source_matches_unit(source, unit))
        if owners:
            owner_ids = tuple(sorted(unit.compile_unit_id for unit in owners))
            supported = tuple(unit for unit in owners if unit.adapter_status == "supported")
            active_classification: _Classification = (
                "required" if supported else "unsupported_active"
            )
            owner = supported[0] if supported else owners[0]
            basis_ref = (
                owner.include_rules[0].rule_id if owner.include_rules else owner.compile_unit_id
            )
            classifications.append(
                SourceClassification(
                    source_id=source.source_id,
                    language=source_language,
                    source_kind=_source_kind(source),
                    classification=active_classification,
                    classification_basis=ClassificationBasisV1(
                        basis_kind=(
                            "model_rule"
                            if active_classification == "required"
                            else "unsupported_adapter"
                        ),
                        model_id=build_model.model_id,
                        basis_ref=basis_ref,
                    ),
                    expected_compile_unit_ids=owner_ids,
                    child_scope_id=None,
                )
            )
            if source_language == "java":
                edges.extend(
                    RequiredSourceEdge(
                        source_id=source.source_id,
                        compile_unit_id=unit_id,
                    )
                    for unit_id in owner_ids
                )
            continue

        active_exclusion = next(
            (
                (unit, basis_ref)
                for unit in active_units
                for membership, basis_ref in (_source_membership(source, unit),)
                if membership == "excluded" and basis_ref is not None
            ),
            None,
        )
        if active_exclusion is not None:
            excluded_unit, basis_ref = active_exclusion
            del excluded_unit
            classifications.append(
                SourceClassification(
                    source_id=source.source_id,
                    language=source_language,
                    source_kind=_source_kind(source),
                    classification="explicitly_excluded",
                    classification_basis=ClassificationBasisV1(
                        basis_kind="model_rule",
                        model_id=build_model.model_id,
                        basis_ref=basis_ref,
                    ),
                    expected_compile_unit_ids=(),
                    child_scope_id=None,
                )
            )
            continue

        inactive_owner = next(
            (unit for unit in inactive_units if _source_matches_unit(source, unit)),
            None,
        )
        if inactive_owner is not None:
            disposition = inactive_owner.scope_disposition
            inactive_classifications: dict[str, _Classification] = {
                "inactive_profile": "inactive_profile",
                "out_of_lifecycle_scope": "out_of_lifecycle_scope",
                "vendor": "vendor",
                "explicit_scope_exclusion": "explicitly_excluded",
                "active": "explicitly_excluded",
            }
            inactive_classification = inactive_classifications[disposition]
            basis_ref = (
                inactive_owner.include_rules[0].rule_id
                if inactive_owner.include_rules
                else inactive_owner.compile_unit_id
            )
            classifications.append(
                SourceClassification(
                    source_id=source.source_id,
                    language=source_language,
                    source_kind=_source_kind(source),
                    classification=inactive_classification,
                    classification_basis=ClassificationBasisV1(
                        basis_kind=(
                            "explicit_scope"
                            if inactive_classification == "explicitly_excluded"
                            else "model_rule"
                        ),
                        model_id=build_model.model_id,
                        basis_ref=basis_ref,
                    ),
                    expected_compile_unit_ids=(),
                    child_scope_id=None,
                )
            )
            continue

        unclassified.append(source.source_id)

    conflicts = tuple(
        sorted(
            {
                *census.conflicts,
                *build_model.conflicts,
                *(f"unreadable census root: {root}" for root in census.unreadable_roots),
                *(
                    f"source {source.source_id!r} has unsafe symlink status {source.symlink_status!r}"
                    for source in census.sources
                    if source.symlink_status not in {"contained", "symlink_contained"}
                ),
                *(
                    language_conflict
                    for source in census.sources
                    for language_conflict in (_source_language_conflict(source),)
                    if language_conflict is not None
                ),
            }
        )
    )
    required_java_edges = tuple(
        sorted(edges, key=lambda item: f"{item.source_id}|{item.compile_unit_id}")
    )
    source_classifications = tuple(sorted(classifications, key=lambda item: item.source_id))
    unclassified_source_ids = tuple(sorted(unclassified))
    required_generator_unit_ids = tuple(
        sorted(
            unit.generator_unit_id
            for unit in build_model.generator_units
            if unit.role == "required"
        )
    )
    required_package_unit_ids = tuple(
        sorted(
            unit.package_unit_id for unit in build_model.package_units if unit.role == "required"
        )
    )
    required_child_scope_ids = tuple(
        sorted(child.child_scope_id for child in build_model.child_build_scopes)
    )
    delegation_edges = tuple(sorted(delegations, key=lambda item: item.parent_source_id))
    identity: dict[str, Any] = {
        "evidence_epoch_id": census.evidence_epoch_id,
        "census_id": census.census_id,
        "scope_id": goal_scope.scope_id,
        "model_id": build_model.model_id,
        "scope_fingerprint": goal_scope.scope_fingerprint,
        "source_state_fingerprint": census.source_state_fingerprint,
        "build_config_fingerprint": build_model.build_config_fingerprint,
        "required_java_edges": required_java_edges,
        "source_classifications": source_classifications,
        "unclassified_source_ids": unclassified_source_ids,
        "required_generator_unit_ids": required_generator_unit_ids,
        "required_compile_unit_ids": tuple(sorted(required_compile_ids)),
        "required_package_unit_ids": required_package_unit_ids,
        "unsupported_active_unit_ids": tuple(sorted(unsupported_ids)),
        "required_child_scope_ids": required_child_scope_ids,
        "delegation_edges": delegation_edges,
    }
    return JvmBuildExpectationV1(
        expectation_id=canonical_content_id("jvm-build-expectation-v1", identity),
        evidence_epoch_id=census.evidence_epoch_id,
        census_id=census.census_id,
        scope_id=goal_scope.scope_id,
        model_id=build_model.model_id,
        scope_fingerprint=goal_scope.scope_fingerprint,
        source_state_fingerprint=census.source_state_fingerprint,
        build_config_fingerprint=build_model.build_config_fingerprint,
        required_java_edges=required_java_edges,
        source_classifications=source_classifications,
        unclassified_source_ids=unclassified_source_ids,
        required_generator_unit_ids=required_generator_unit_ids,
        required_compile_unit_ids=tuple(sorted(required_compile_ids)),
        required_package_unit_ids=required_package_unit_ids,
        unsupported_active_unit_ids=tuple(sorted(unsupported_ids)),
        required_child_scope_ids=required_child_scope_ids,
        delegation_edges=delegation_edges,
        conflicts=conflicts,
    )


@dataclass(frozen=True)
class _ExpectedMember:
    member_id: str
    member_role: PhysicalMemberRole
    path_kind: PathKind
    path: str
    root_id: str | None = None
    verification_mode: VerificationMode | None = None


def _absolute_output_path(project_root: str, relative_path: str) -> str:
    return posixpath.join(project_root.rstrip("/"), relative_path)


def _compile_output_roots(
    unit: CompileUnitExpectation,
    build_model: EvaluatedBuildModelV1,
) -> tuple[OutputRootSpec, ...]:
    roots = list(unit.output_roots)
    root_ids = {root.root_id for root in roots}
    for contract in build_model.generated_root_contracts:
        if (
            contract.producer_unit_kind == "compile"
            and contract.producer_unit_id == unit.compile_unit_id
            and contract.mode == "later_compile_input"
            and contract.generated_root_id not in root_ids
        ):
            roots.append(contract.output_root)
            root_ids.add(contract.generated_root_id)
    return tuple(sorted(roots, key=lambda root: root.root_id))


def _required_units(build_model: EvaluatedBuildModelV1) -> tuple[Any, ...]:
    return (
        *(unit for unit in build_model.generator_units if unit.role == "required"),
        *(unit for unit in build_model.compile_units if unit.role == "required"),
        *(unit for unit in build_model.package_units if unit.role == "required"),
    )


def _expected_physical_members(
    census: JvmSourceCensusV1,
    build_model: EvaluatedBuildModelV1,
) -> dict[str, _ExpectedMember]:
    expected: dict[str, _ExpectedMember] = {}

    def register(item: _ExpectedMember) -> None:
        previous = expected.get(item.member_id)
        if previous is not None and previous != item:
            raise ValueError(f"physical member {item.member_id!r} has conflicting descriptors")
        expected[item.member_id] = item

    def register_path_basis(basis: BuildPathBasis) -> None:
        register(
            _ExpectedMember(
                member_id=basis.member_id,
                member_role=basis.member_role,
                path_kind=basis.path_kind,
                path=basis.path,
            )
        )

    def register_spec(spec: ResolvedMemberSpec) -> None:
        if spec.path is not None:
            register(
                _ExpectedMember(
                    member_id=spec.member_id,
                    member_role=spec.member_role,
                    path_kind=spec.path_kind,
                    path=spec.path,
                )
            )
            return
        root = next(
            (
                root
                for unit in build_model.compile_units
                if unit.compile_unit_id == spec.upstream_unit_id
                for root in unit.output_roots
                if root.root_id == spec.upstream_root_id
            ),
            None,
        )
        if root is None:
            raise ValueError(f"upstream physical member {spec.member_id!r} has no output root")
        register(
            _ExpectedMember(
                member_id=spec.member_id,
                member_role=spec.member_role,
                path_kind=spec.path_kind,
                path=_absolute_output_path(build_model.project_root, root.path),
                root_id=root.root_id,
                verification_mode=root.verification_mode,
            )
        )

    def register_output(root: OutputRootSpec) -> None:
        register(
            _ExpectedMember(
                member_id=root.member_id,
                member_role="other",
                path_kind="tree",
                path=_absolute_output_path(build_model.project_root, root.path),
                root_id=root.root_id,
                verification_mode=root.verification_mode,
            )
        )

    for census_source in census.sources:
        register(
            _ExpectedMember(
                member_id=census_source.source_id,
                member_role="source",
                path_kind="file",
                path=_absolute_output_path(census.project_root, census_source.path),
            )
        )
    for config_spec in build_model.build_config_members:
        register_spec(config_spec)
    for generator_unit in build_model.generator_units:
        if generator_unit.role != "required":
            continue
        register_path_basis(generator_unit.generator_environment_spec.executable_path_basis)
        register_path_basis(generator_unit.generator_environment_spec.toolchain_path_basis)
        for generator_spec in (
            *generator_unit.input_roots,
            *generator_unit.generator_environment_spec.tool_or_plugin_members,
            *generator_unit.generator_environment_spec.dependency_members,
        ):
            register_spec(generator_spec)
        contracts = {
            contract.generated_root_id: contract
            for contract in build_model.generated_root_contracts
        }
        for root_id in generator_unit.generated_root_ids:
            register_output(contracts[root_id].output_root)
    for compile_unit in build_model.compile_units:
        if compile_unit.role != "required":
            continue
        register_path_basis(compile_unit.compiler_environment_spec.executable_path_basis)
        register_path_basis(compile_unit.compiler_environment_spec.toolchain_path_basis)
        for classpath_spec in compile_unit.compiler_environment_spec.classpath_members:
            register_spec(classpath_spec)
        for compile_root in _compile_output_roots(compile_unit, build_model):
            register_output(compile_root)
    for package_unit in build_model.package_units:
        if package_unit.role != "required":
            continue
        register_path_basis(package_unit.packaging_environment_spec.executable_path_basis)
        register_path_basis(package_unit.packaging_environment_spec.toolchain_path_basis)
        for package_spec in (
            *package_unit.resource_roots,
            *package_unit.manifest_or_packaging_config,
            *package_unit.packaging_environment_spec.plugin_or_tool_members,
            *package_unit.packaging_environment_spec.external_dependency_members,
        ):
            register_spec(package_spec)
        for package_root in package_unit.output_roots:
            register_output(package_root)
    return expected


@dataclass
class _BasisResolver:
    expected: Mapping[str, _ExpectedMember]
    physical: CurrentPhysicalBasisSnapshotV1
    conflicts: set[str]

    def __post_init__(self) -> None:
        self.current = {item.member_id: item for item in self.physical.current_members}
        self.current_roots = {item.member_id: item for item in self.physical.current_output_roots}
        self.missing = {item.member_id: item for item in self.physical.missing_members}
        self.unreadable = {item.member_id: item for item in self.physical.unreadable_members}

    def validate_partition(self) -> None:
        expected_ids = set(self.expected)
        requested_ids = set(self.physical.requested_member_ids)
        missing = sorted(expected_ids - requested_ids)
        extra = sorted(requested_ids - expected_ids)
        if missing:
            self.conflicts.add(f"physical requested-member set omits {missing!r}")
        if extra:
            self.conflicts.add(f"physical requested-member set has extras {extra!r}")
        self.conflicts.update(self.physical.conflicts)
        for member_id, unreadable_member in self.unreadable.items():
            descriptor = self.expected.get(member_id)
            if descriptor is None:
                continue
            if unreadable_member.path != descriptor.path:
                self.conflicts.add(
                    f"unreadable member {member_id!r} is rebound to an unexpected path"
                )
            self.conflicts.add(f"physical member {member_id!r} is unreadable")
        for member_id, missing_member in self.missing.items():
            descriptor = self.expected.get(member_id)
            if descriptor is not None and missing_member.path != descriptor.path:
                self.conflicts.add(f"missing member {member_id!r} is rebound to an unexpected path")
        for member_id, current_member in self.current.items():
            descriptor = self.expected.get(member_id)
            if descriptor is None:
                continue
            if (
                current_member.member_role != descriptor.member_role
                or current_member.path_kind != descriptor.path_kind
                or current_member.path != descriptor.path
            ):
                self.conflicts.add(
                    f"physical member {member_id!r} is rebound to a different descriptor"
                )
        for member_id, current_root in self.current_roots.items():
            descriptor = self.expected.get(member_id)
            if descriptor is None:
                continue
            if (
                current_root.root_id != descriptor.root_id
                or current_root.path != descriptor.path
                or current_root.verification_mode != descriptor.verification_mode
            ):
                self.conflicts.add(
                    f"physical output root {member_id!r} is rebound to a different descriptor"
                )

    def digest_for(self, member_id: str) -> ResolvedDigestEntry | None:
        descriptor = self.expected.get(member_id)
        if descriptor is None:
            self.conflicts.add(f"unit references unknown physical member {member_id!r}")
            return None
        if member_id in self.missing:
            return None
        if member_id in self.unreadable:
            return None
        current = self.current.get(member_id)
        if current is not None:
            if (
                current.member_role != descriptor.member_role
                or current.path_kind != descriptor.path_kind
                or current.path != descriptor.path
            ):
                return None
            return ResolvedDigestEntry(
                member_id=current.member_id,
                member_role=current.member_role,
                path_kind=current.path_kind,
                path=current.path,
                byte_count=current.byte_count,
                sha256=current.sha256,
            )
        root = self.current_roots.get(member_id)
        if root is not None and descriptor.root_id is not None:
            if (
                root.root_id != descriptor.root_id
                or root.path != descriptor.path
                or root.verification_mode != descriptor.verification_mode
            ):
                return None
            byte_count = sum(
                entry.byte_count
                for entry in self.physical.current_output_entries
                if entry.root_id == root.root_id
            )
            return ResolvedDigestEntry(
                member_id=member_id,
                member_role=descriptor.member_role,
                path_kind=descriptor.path_kind,
                path=descriptor.path,
                byte_count=byte_count,
                sha256=root.tree_or_owned_entries_sha256,
            )
        self.conflicts.add(f"physical requested member {member_id!r} has no partition row")
        return None


def _resolved_entries(
    resolver: _BasisResolver,
    specs: Sequence[ResolvedMemberSpec],
) -> tuple[ResolvedDigestEntry, ...] | None:
    entries: list[ResolvedDigestEntry] = []
    for spec in specs:
        item = resolver.digest_for(spec.member_id)
        if item is None:
            return None
        entries.append(item)
    return tuple(entries)


def _path_digest(resolver: _BasisResolver, basis: BuildPathBasis) -> ResolvedDigestEntry | None:
    return resolver.digest_for(basis.member_id)


def derive_final_basis(
    census: JvmSourceCensusV1,
    expectation: JvmBuildExpectationV1,
    build_model: EvaluatedBuildModelV1,
    physical: CurrentPhysicalBasisSnapshotV1,
    *,
    basis_revision: int,
) -> FinalBuildBasisV1:
    """Join typed current physical facts onto the independent model."""

    if expectation.evidence_epoch_id != census.evidence_epoch_id:
        raise ValueError("expectation and census evidence epochs differ")
    if build_model.evidence_epoch_id != census.evidence_epoch_id:
        raise ValueError("build model and census evidence epochs differ")
    if physical.evidence_epoch_id != census.evidence_epoch_id:
        raise ValueError("physical basis and census evidence epochs differ")
    if expectation.census_id != census.census_id:
        raise ValueError("expectation census ID cross-link is inconsistent")
    if expectation.model_id != build_model.model_id:
        raise ValueError("expectation build model ID cross-link is inconsistent")
    if physical.expectation_id != expectation.expectation_id:
        raise ValueError("physical basis expectation ID cross-link is inconsistent")
    if physical.build_model_id != build_model.model_id:
        raise ValueError("physical basis model ID cross-link is inconsistent")
    if expectation.source_state_fingerprint != census.source_state_fingerprint:
        raise ValueError("expectation source-state fingerprint differs from census")
    if expectation.build_config_fingerprint != build_model.build_config_fingerprint:
        raise ValueError("expectation build-config fingerprint differs from model")

    expected_members = _expected_physical_members(census, build_model)
    conflicts: set[str] = set()
    resolver = _BasisResolver(expected_members, physical, conflicts)
    resolver.validate_partition()
    globally_invalid_basis = bool(
        physical.conflicts or set(physical.requested_member_ids) != set(expected_members)
    )
    invalid_descriptor_ids = {item.member_id for item in physical.unreadable_members}
    invalid_descriptor_ids.update(
        item.member_id
        for item in physical.missing_members
        if (
            (descriptor := expected_members.get(item.member_id)) is not None
            and item.path != descriptor.path
        )
    )
    invalid_descriptor_ids.update(
        item.member_id
        for item in physical.current_members
        if (
            (descriptor := expected_members.get(item.member_id)) is not None
            and (
                item.member_role != descriptor.member_role
                or item.path_kind != descriptor.path_kind
                or item.path != descriptor.path
            )
        )
    )
    invalid_descriptor_ids.update(
        item.member_id
        for item in physical.current_output_roots
        if (
            (descriptor := expected_members.get(item.member_id)) is not None
            and (
                item.root_id != descriptor.root_id
                or item.path != descriptor.path
                or item.verification_mode != descriptor.verification_mode
            )
        )
    )

    sources = {census_source.source_id: census_source for census_source in census.sources}
    source_ids_by_unit: dict[str, set[str]] = {}
    for classification in expectation.source_classifications:
        if classification.classification not in {"required", "unsupported_active"}:
            continue
        for unit_id in classification.expected_compile_unit_ids:
            source_ids_by_unit.setdefault(unit_id, set()).add(classification.source_id)

    physical_sources: dict[str, ResolvedDigestEntry] = {}
    for census_source in census.sources:
        current_source = resolver.digest_for(census_source.source_id)
        if current_source is None:
            continue
        if (
            current_source.path != _absolute_output_path(census.project_root, census_source.path)
            or current_source.sha256 != census_source.sha256
            or current_source.member_role != "source"
            or current_source.path_kind != "file"
        ):
            conflicts.add(
                f"source {census_source.source_id!r} current bytes do not match the census"
            )
            continue
        physical_sources[census_source.source_id] = current_source

    config_entries = _resolved_entries(resolver, build_model.build_config_members)
    config_available = config_entries is not None
    if config_entries is not None:
        current_config_fingerprint = _fingerprint(
            "jvm-build-config-members",
            config_entries,
        )
        if current_config_fingerprint != build_model.build_config_fingerprint:
            conflicts.add("current build-config member fingerprint differs from the model")
            config_available = False

    generator_bases: list[FinalGeneratorUnitBasis] = []
    contracts = {
        contract.generated_root_id: contract for contract in build_model.generated_root_contracts
    }
    for generator_unit in build_model.generator_units:
        if generator_unit.generator_unit_id not in expectation.required_generator_unit_ids:
            continue
        if not config_available:
            continue
        generator_inputs = _resolved_entries(resolver, generator_unit.input_roots)
        generator_executable = _path_digest(
            resolver, generator_unit.generator_environment_spec.executable_path_basis
        )
        generator_tools = _resolved_entries(
            resolver, generator_unit.generator_environment_spec.tool_or_plugin_members
        )
        generator_dependencies = _resolved_entries(
            resolver, generator_unit.generator_environment_spec.dependency_members
        )
        generator_toolchain = _path_digest(
            resolver, generator_unit.generator_environment_spec.toolchain_path_basis
        )
        if None in (
            generator_inputs,
            generator_executable,
            generator_tools,
            generator_dependencies,
            generator_toolchain,
        ):
            continue
        assert generator_inputs is not None
        assert generator_executable is not None
        assert generator_tools is not None
        assert generator_dependencies is not None
        assert generator_toolchain is not None
        generator_roots = tuple(
            contracts[root_id].output_root for root_id in generator_unit.generated_root_ids
        )
        if any(root.member_id in invalid_descriptor_ids for root in generator_roots):
            continue
        generator_fields: dict[str, Any] = {
            "generator_unit_id": generator_unit.generator_unit_id,
            "build_config_fingerprint": build_model.build_config_fingerprint,
            "input_entries": generator_inputs,
            "generator_executable_path": generator_executable.path,
            "generator_executable_sha256": generator_executable.sha256,
            "generator_tool_entries": generator_tools,
            "generator_dependency_entries": generator_dependencies,
            "generator_args_fingerprint": _fingerprint(
                "generator-arguments",
                generator_unit.generator_environment_spec.generator_args,
            ),
            "generator_toolchain_fingerprint": generator_toolchain.sha256,
            "generated_output_roots": generator_roots,
        }
        generator_bases.append(
            FinalGeneratorUnitBasis(
                **generator_fields,
                generator_unit_identity_hash=generator_unit_identity_hash(generator_fields),
            )
        )

    compile_units = {
        compile_unit.compile_unit_id: compile_unit for compile_unit in build_model.compile_units
    }
    compile_bases: dict[str, FinalCompileUnitBasis] = {}
    pending = set(expectation.required_compile_unit_ids) if config_available else set()
    while pending:
        progressed = False
        for unit_id in sorted(tuple(pending)):
            compile_unit = compile_units.get(unit_id)
            if compile_unit is None:
                conflicts.add(f"required compile unit {unit_id!r} is missing from the model")
                pending.remove(unit_id)
                progressed = True
                continue
            upstream_ids = tuple(
                dict.fromkeys(
                    spec.upstream_unit_id
                    for spec in compile_unit.compiler_environment_spec.classpath_members
                    if spec.upstream_unit_id is not None
                )
            )
            if any(upstream_id in pending for upstream_id in upstream_ids):
                continue
            if any(upstream_id not in compile_bases for upstream_id in upstream_ids):
                pending.remove(unit_id)
                progressed = True
                continue

            source_entries: list[SourceDigestEntry] = []
            source_available = True
            for source_id in sorted(source_ids_by_unit.get(unit_id, ())):
                compile_source = sources.get(source_id)
                physical_source = physical_sources.get(source_id)
                if compile_source is None:
                    conflicts.add(
                        f"compile unit {unit_id!r} references unknown census source {source_id!r}"
                    )
                    source_available = False
                    continue
                if physical_source is None:
                    source_available = False
                    continue
                if (
                    physical_source.path
                    != _absolute_output_path(census.project_root, compile_source.path)
                    or physical_source.sha256 != compile_source.sha256
                    or physical_source.member_role != "source"
                    or physical_source.path_kind != "file"
                ):
                    conflicts.add(f"source {source_id!r} current bytes do not match the census")
                    source_available = False
                    continue
                source_entries.append(
                    SourceDigestEntry(
                        source_id=compile_source.source_id,
                        path=compile_source.path,
                        sha256=compile_source.sha256,
                    )
                )
            compile_executable = _path_digest(
                resolver, compile_unit.compiler_environment_spec.executable_path_basis
            )
            compile_classpath = _resolved_entries(
                resolver, compile_unit.compiler_environment_spec.classpath_members
            )
            compile_toolchain = _path_digest(
                resolver, compile_unit.compiler_environment_spec.toolchain_path_basis
            )
            if (
                not source_available
                or compile_executable is None
                or compile_classpath is None
                or compile_toolchain is None
            ):
                pending.remove(unit_id)
                progressed = True
                continue
            dependency_hashes = tuple(
                compile_bases[upstream_id].compile_unit_identity_hash
                for upstream_id in upstream_ids
            )
            compile_fields: dict[str, Any] = {
                "compile_unit_id": unit_id,
                "build_config_fingerprint": build_model.build_config_fingerprint,
                "required_source_entries": tuple(source_entries),
                "source_set_fingerprint": _fingerprint("compile-source-set", tuple(source_entries)),
                "compiler_executable_path": compile_executable.path,
                "compiler_executable_sha256": compile_executable.sha256,
                "compiler_version": compile_unit.compiler_environment_spec.compiler_version,
                "compiler_args_fingerprint": _fingerprint(
                    "compiler-arguments",
                    compile_unit.compiler_environment_spec.compiler_args,
                ),
                "classpath_entries": compile_classpath,
                "dependency_unit_identity_hashes": dependency_hashes,
                "toolchain_fingerprint": compile_toolchain.sha256,
                "output_roots": _compile_output_roots(compile_unit, build_model),
            }
            if any(
                root.member_id in invalid_descriptor_ids for root in compile_fields["output_roots"]
            ):
                pending.remove(unit_id)
                progressed = True
                continue
            compile_bases[unit_id] = FinalCompileUnitBasis(
                **compile_fields,
                compile_unit_identity_hash=compile_unit_identity_hash(compile_fields),
            )
            pending.remove(unit_id)
            progressed = True
        if not progressed:
            conflicts.add("required compile-unit basis graph could not be resolved")
            break

    package_bases: list[FinalPackageUnitBasis] = []
    package_units = {
        package_unit.package_unit_id: package_unit for package_unit in build_model.package_units
    }
    roots_by_compile = {
        compile_unit_id: compile_basis.output_roots
        for compile_unit_id, compile_basis in compile_bases.items()
    }
    current_roots_by_id = {root.root_id: root for root in physical.current_output_roots}
    for unit_id in expectation.required_package_unit_ids:
        if not config_available:
            continue
        package_unit = package_units.get(unit_id)
        if package_unit is None:
            conflicts.add(f"required package unit {unit_id!r} is missing from the model")
            continue
        if any(input_id not in compile_bases for input_id in package_unit.input_compile_unit_ids):
            continue
        input_identities = tuple(
            compile_bases[input_id].compile_unit_identity_hash
            for input_id in package_unit.input_compile_unit_ids
        )
        output_digests: list[CompileOutputDigestEntry] = []
        outputs_available = True
        for input_id in package_unit.input_compile_unit_ids:
            for root_spec in roots_by_compile[input_id]:
                root = current_roots_by_id.get(root_spec.root_id)
                if root is None:
                    outputs_available = False
                    continue
                output_digests.append(
                    CompileOutputDigestEntry(
                        compile_unit_id=input_id,
                        root_id=root.root_id,
                        sha256=root.tree_or_owned_entries_sha256,
                    )
                )
        package_resources = _resolved_entries(resolver, package_unit.resource_roots)
        package_config = _resolved_entries(resolver, package_unit.manifest_or_packaging_config)
        package_executable = _path_digest(
            resolver, package_unit.packaging_environment_spec.executable_path_basis
        )
        package_tools = _resolved_entries(
            resolver, package_unit.packaging_environment_spec.plugin_or_tool_members
        )
        package_dependencies = _resolved_entries(
            resolver, package_unit.packaging_environment_spec.external_dependency_members
        )
        package_toolchain = _path_digest(
            resolver, package_unit.packaging_environment_spec.toolchain_path_basis
        )
        if (
            not outputs_available
            or package_resources is None
            or package_config is None
            or package_executable is None
            or package_tools is None
            or package_dependencies is None
            or package_toolchain is None
            or any(root.member_id in invalid_descriptor_ids for root in package_unit.output_roots)
        ):
            continue
        package_fields: dict[str, Any] = {
            "package_unit_id": package_unit.package_unit_id,
            "build_config_fingerprint": build_model.build_config_fingerprint,
            "input_compile_unit_identity_hashes": input_identities,
            "input_compile_output_digests": tuple(output_digests),
            "resource_entries": package_resources,
            "packaging_executable_path": package_executable.path,
            "packaging_executable_sha256": package_executable.sha256,
            "packaging_tool_entries": package_tools,
            "external_dependency_entries": package_dependencies,
            "packaging_args_fingerprint": _fingerprint(
                "packaging-arguments",
                package_unit.packaging_environment_spec.packaging_args,
            ),
            "packaging_toolchain_fingerprint": package_toolchain.sha256,
            "packaging_config_fingerprint": _fingerprint("packaging-config", package_config),
            "output_roots": package_unit.output_roots,
            "requested_artifacts": package_unit.requested_artifacts,
        }
        package_bases.append(
            FinalPackageUnitBasis(
                **package_fields,
                package_unit_identity_hash=package_unit_identity_hash(package_fields),
            )
        )

    if globally_invalid_basis:
        generator_bases = []
        compile_bases = {}
        package_bases = []

    return FinalBuildBasisV1(
        basis_id=canonical_content_id(
            "final-build-basis-v1",
            {
                "evidence_epoch_id": census.evidence_epoch_id,
                "expectation_id": expectation.expectation_id,
            },
        ),
        evidence_epoch_id=census.evidence_epoch_id,
        basis_revision=basis_revision,
        expectation_id=expectation.expectation_id,
        physical_basis_snapshot_id=physical.physical_basis_snapshot_id,
        source_state_fingerprint=census.source_state_fingerprint,
        build_config_fingerprint=build_model.build_config_fingerprint,
        generator_unit_bases=tuple(
            sorted(generator_bases, key=lambda item: item.generator_unit_id)
        ),
        compile_unit_bases=tuple(
            sorted(compile_bases.values(), key=lambda item: item.compile_unit_id)
        ),
        package_unit_bases=tuple(sorted(package_bases, key=lambda item: item.package_unit_id)),
        conflicts=tuple(sorted(conflicts)),
    )


@dataclass(frozen=True)
class _WitnessCheck:
    witness: PhysicalOutputWitnessV1 | None
    state: Literal["verified", "missing", "invalid", "unverifiable", "not_required"]
    entry_states: tuple[tuple[str, str, str], ...]


def _basis_identity_equal(observation: Any, basis: Any, kind: str) -> bool:
    if kind == "generator":
        return bool(
            observation.generator_unit_identity_hash == basis.generator_unit_identity_hash
            and observation.build_config_fingerprint == basis.build_config_fingerprint
            and observation.input_entries == basis.input_entries
            and observation.generator_executable_path == basis.generator_executable_path
            and observation.generator_executable_sha256 == basis.generator_executable_sha256
            and observation.generator_tool_entries == basis.generator_tool_entries
            and observation.generator_dependency_entries == basis.generator_dependency_entries
            and observation.generator_args_fingerprint == basis.generator_args_fingerprint
            and observation.generator_toolchain_fingerprint == basis.generator_toolchain_fingerprint
            and observation.generated_output_roots == basis.generated_output_roots
        )
    if kind == "compile":
        return bool(
            observation.compile_unit_identity_hash == basis.compile_unit_identity_hash
            and observation.build_config_fingerprint == basis.build_config_fingerprint
            and observation.source_entries == basis.required_source_entries
            and observation.source_set_fingerprint == basis.source_set_fingerprint
            and observation.compiler_executable_path == basis.compiler_executable_path
            and observation.compiler_executable_sha256 == basis.compiler_executable_sha256
            and observation.compiler_version == basis.compiler_version
            and observation.compiler_args_fingerprint == basis.compiler_args_fingerprint
            and observation.classpath_entries == basis.classpath_entries
            and observation.dependency_unit_identity_hashes == basis.dependency_unit_identity_hashes
            and observation.toolchain_fingerprint == basis.toolchain_fingerprint
            and observation.observed_output_roots == basis.output_roots
        )
    return bool(
        observation.package_unit_identity_hash == basis.package_unit_identity_hash
        and observation.build_config_fingerprint == basis.build_config_fingerprint
        and observation.input_compile_unit_identity_hashes
        == basis.input_compile_unit_identity_hashes
        and observation.input_compile_output_digests == basis.input_compile_output_digests
        and observation.resource_entries == basis.resource_entries
        and observation.packaging_executable_path == basis.packaging_executable_path
        and observation.packaging_executable_sha256 == basis.packaging_executable_sha256
        and observation.packaging_tool_entries == basis.packaging_tool_entries
        and observation.external_dependency_entries == basis.external_dependency_entries
        and observation.packaging_args_fingerprint == basis.packaging_args_fingerprint
        and observation.packaging_toolchain_fingerprint == basis.packaging_toolchain_fingerprint
        and observation.packaging_config_fingerprint == basis.packaging_config_fingerprint
        and observation.output_roots == basis.output_roots
        and observation.requested_artifacts == basis.requested_artifacts
        and tuple(sorted(observation.observed_artifact_paths))
        == tuple(sorted(item.path for item in basis.requested_artifacts))
    )


_MAVEN_PHASE_ORDER = {
    phase: index
    for index, phase in enumerate(
        (
            "validate",
            "initialize",
            "generate-sources",
            "process-sources",
            "generate-resources",
            "process-resources",
            "compile",
            "process-classes",
            "generate-test-sources",
            "process-test-sources",
            "generate-test-resources",
            "process-test-resources",
            "test-compile",
            "process-test-classes",
            "test",
            "prepare-package",
            "package",
            "pre-integration-test",
            "integration-test",
            "post-integration-test",
            "verify",
            "install",
            "deploy",
        )
    )
}


def _maven_selector_covers(
    unit: GeneratorUnitExpectation | CompileUnitExpectation | PackageUnitExpectation,
    build_model: EvaluatedBuildModelV1,
    *,
    selected: set[str],
    also_make: bool,
) -> bool:
    if not selected:
        return True
    if unit.module_coordinate in selected or unit.task_or_execution in selected:
        return True
    if not also_make:
        return False

    all_units: tuple[
        GeneratorUnitExpectation | CompileUnitExpectation | PackageUnitExpectation,
        ...,
    ] = (
        *build_model.generator_units,
        *build_model.compile_units,
        *build_model.package_units,
    )
    selected_modules = {
        model_unit.module_coordinate
        for model_unit in all_units
        if model_unit.module_coordinate in selected or model_unit.task_or_execution in selected
    }
    selected_modules.update(
        module.module_coordinate
        for module in build_model.modules
        if module.module_coordinate in selected
    )
    reachable_compile_ids = {
        compile_unit.compile_unit_id
        for compile_unit in build_model.compile_units
        if compile_unit.module_coordinate in selected_modules
    }
    changed = True
    while changed:
        changed = False
        for edge in build_model.compile_dependency_edges:
            if (
                edge.downstream_compile_unit_id in reachable_compile_ids
                and edge.upstream_compile_unit_id not in reachable_compile_ids
            ):
                reachable_compile_ids.add(edge.upstream_compile_unit_id)
                changed = True
    upstream_modules = {
        compile_unit.module_coordinate
        for compile_unit in build_model.compile_units
        if compile_unit.compile_unit_id in reachable_compile_ids
    }
    return unit.module_coordinate in upstream_modules


def _invocation_covers(
    observation: Any,
    unit: GeneratorUnitExpectation | CompileUnitExpectation | PackageUnitExpectation,
    build_model: EvaluatedBuildModelV1,
) -> bool:
    scope = observation.actual_invocation_scope
    module = next(
        (item for item in build_model.modules if item.module_coordinate == unit.module_coordinate),
        None,
    )
    if module is None or scope.build_system != module.build_system:
        return False
    selected = set(scope.selected_modules_or_tasks)
    lifecycle = set(scope.lifecycle_or_tasks)
    if scope.build_system == "gradle":
        if (
            selected
            and unit.module_coordinate not in selected
            and unit.task_or_execution not in selected
        ):
            return False
        return unit.task_or_execution in lifecycle
    if not _maven_selector_covers(
        unit,
        build_model,
        selected=selected,
        also_make=scope.also_make,
    ):
        return False
    if unit.task_or_execution in lifecycle:
        return True
    if isinstance(unit, CompileUnitExpectation):
        required_phase = "test-compile" if unit.source_set == "test" else "compile"
    elif isinstance(unit, GeneratorUnitExpectation):
        required_phase = (
            unit.task_or_execution
            if unit.task_or_execution in _MAVEN_PHASE_ORDER
            else "generate-sources"
        )
    else:
        required_phase = (
            unit.task_or_execution if unit.task_or_execution in _MAVEN_PHASE_ORDER else "package"
        )
    required_rank = _MAVEN_PHASE_ORDER[required_phase]
    return any(
        _MAVEN_PHASE_ORDER.get(actual_phase, -1) >= required_rank for actual_phase in lifecycle
    )


def _witness_check(
    *,
    observation: Any,
    kind: str,
    roots: tuple[OutputRootSpec, ...],
    evidence_set: BuildEvidenceSetSnapshotV1,
    physical: CurrentPhysicalBasisSnapshotV1,
    project_root: str,
    required: bool,
    requested_artifacts: tuple[RequestedArtifactSpec, ...] = (),
) -> _WitnessCheck:
    if not required:
        return _WitnessCheck(None, "not_required", ())
    witnesses = tuple(
        witness
        for witness in evidence_set.output_witnesses
        if witness.producer_observation_id == observation.observation_id
        and witness.producer_observation_kind == kind
    )
    if not witnesses:
        return _WitnessCheck(None, "missing", ())
    witness = witnesses[0]
    expected_roots = tuple((root.root_id, root.path, root.verification_mode) for root in roots)
    witnessed_roots = tuple(
        (root.root_id, root.path, root.verification_mode) for root in witness.roots
    )
    if witnessed_roots != expected_roots:
        return _WitnessCheck(witness, "invalid", ())
    root_specs = {root.root_id: root for root in roots}
    physical_roots = {root.root_id: root for root in physical.current_output_roots}
    physical_entries = {
        (entry.root_id, entry.relative_path): entry for entry in physical.current_output_entries
    }
    missing_member_ids = {item.member_id for item in physical.missing_members}
    unreadable_member_ids = {item.member_id for item in physical.unreadable_members}
    result_state: Literal["verified", "missing", "invalid", "unverifiable"] = "verified"
    entry_states: list[tuple[str, str, str]] = []
    for witness_root in witness.roots:
        spec = root_specs[witness_root.root_id]
        absolute_root = _absolute_output_path(project_root, spec.path)
        if spec.member_id in unreadable_member_ids:
            result_state = "unverifiable"
            for entry in witness.entries:
                if entry.root_id == spec.root_id:
                    entry_states.append(
                        (
                            posixpath.join(absolute_root, entry.relative_path),
                            entry.kind,
                            "mismatched",
                        )
                    )
            continue
        if spec.member_id in missing_member_ids or spec.root_id not in physical_roots:
            if result_state != "unverifiable":
                result_state = "missing"
            for entry in witness.entries:
                if entry.root_id == spec.root_id:
                    entry_states.append(
                        (posixpath.join(absolute_root, entry.relative_path), entry.kind, "missing")
                    )
            continue
        current_root = physical_roots[spec.root_id]
        root_matches = (
            current_root.member_id == spec.member_id
            and current_root.path == absolute_root
            and current_root.verification_mode == spec.verification_mode
            and current_root.tree_or_owned_entries_sha256
            == witness_root.tree_or_owned_entries_sha256
            and current_root.scan_complete
        )
        root_entries = tuple(entry for entry in witness.entries if entry.root_id == spec.root_id)
        if spec.output_requirement == "nonempty" and not root_entries:
            root_matches = False
        for entry in root_entries:
            current = physical_entries.get((entry.root_id, entry.relative_path))
            absolute_path = posixpath.join(absolute_root, entry.relative_path)
            if current is None:
                entry_states.append((absolute_path, entry.kind, "missing"))
                root_matches = False
                if result_state != "unverifiable":
                    result_state = "invalid"
            elif (
                current.kind != entry.kind
                or current.byte_count != entry.byte_count
                or current.sha256 != entry.sha256
            ):
                entry_states.append((absolute_path, entry.kind, "mismatched"))
                root_matches = False
                if result_state != "unverifiable":
                    result_state = "invalid"
            else:
                entry_states.append((absolute_path, entry.kind, "verified"))
        if spec.verification_mode == "exclusive_tree":
            current_keys = {key for key in physical_entries if key[0] == spec.root_id}
            witness_keys = {(entry.root_id, entry.relative_path) for entry in root_entries}
            if current_keys != witness_keys:
                root_matches = False
        if not root_matches and result_state not in {"unverifiable", "missing"}:
            result_state = "invalid"
            entry_states = [
                (path, entry_kind, "mismatched" if state == "verified" else state)
                for path, entry_kind, state in entry_states
            ]
    witness_entries = {(entry.root_id, entry.relative_path): entry for entry in witness.entries}
    for artifact in requested_artifacts:
        root = root_specs.get(artifact.root_id)
        if root is None:
            result_state = "invalid"
            continue
        prefix = root.path.rstrip("/") + "/"
        if not artifact.path.startswith(prefix):
            result_state = "invalid"
            continue
        relative_path = artifact.path[len(prefix) :]
        artifact_entry = witness_entries.get((artifact.root_id, relative_path))
        expected_kind = artifact.kind if artifact.kind in {"jar", "war", "resource"} else "other"
        if artifact_entry is None or artifact_entry.kind != expected_kind:
            if result_state != "unverifiable":
                result_state = "invalid"
    return _WitnessCheck(witness, result_state, tuple(entry_states))


_JVM_SOURCE_SUFFIXES = frozenset({".java", ".kt", ".kts", ".scala", ".groovy", ".aj"})


def _generated_handoff_is_current(
    contract: GeneratedRootContract,
    check: _WitnessCheck | None,
    inputs: ReconciliationInputs,
) -> bool:
    if check is None or check.state != "verified" or check.witness is None:
        return False
    classifications_by_source = {
        classification.source_id: classification
        for classification in inputs.expectation.source_classifications
    }
    consumer_ids = set(contract.consumer_unit_ids)
    root_prefix = contract.output_root.path.rstrip("/") + "/"
    expected_sources: dict[str, str] = {}
    for source in inputs.census.sources:
        if not (source.path == contract.output_root.path or source.path.startswith(root_prefix)):
            continue
        classification = classifications_by_source.get(source.source_id)
        if classification is None or not consumer_ids.intersection(
            classification.expected_compile_unit_ids
        ):
            continue
        expected_sources[source.path] = source.sha256

    witnessed_sources: dict[str, str] = {}
    for entry in check.witness.entries:
        if entry.root_id != contract.generated_root_id:
            continue
        source_path = posixpath.join(contract.output_root.path, entry.relative_path)
        suffix = posixpath.splitext(source_path)[1].lower()
        if source_path in expected_sources or suffix in _JVM_SOURCE_SUFFIXES:
            witnessed_sources[source_path] = entry.sha256
    return witnessed_sources == expected_sources


def _record_statuses(snapshot: BuildEvidenceSetSnapshotV1) -> dict[str, str]:
    return {row.record_kind: row.status for row in snapshot.record_sets}


def _child_graph_is_valid(inputs: ReconciliationInputs, conflicts: set[str]) -> bool:
    root_scope = inputs.expectation.scope_id
    expectations_by_scope: dict[str, JvmBuildExpectationV1] = {root_scope: inputs.expectation}
    for expectation in inputs.evidence_set.child_expectations:
        if expectation.scope_id in expectations_by_scope:
            conflicts.add(f"child scope {expectation.scope_id!r} has duplicate expectations")
            return False
        expectations_by_scope[expectation.scope_id] = expectation
    parents: dict[str, set[str]] = {}
    graph: dict[str, set[str]] = {}
    for scope_id, expectation in expectations_by_scope.items():
        graph[scope_id] = set(expectation.required_child_scope_ids)
        for child_scope_id in expectation.required_child_scope_ids:
            parents.setdefault(child_scope_id, set()).add(scope_id)
            if child_scope_id not in expectations_by_scope:
                conflicts.add(f"child graph references missing scope {child_scope_id!r}")
    if any(len(parent_ids) > 1 for parent_ids in parents.values()):
        conflicts.add("child scope graph assigns one child to multiple parents")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(scope_id: str) -> None:
        if scope_id in visiting:
            conflicts.add("child scope graph contains a cycle")
            return
        if scope_id in visited:
            return
        visiting.add(scope_id)
        for child_scope_id in graph.get(scope_id, set()):
            visit(child_scope_id)
        visiting.remove(scope_id)
        visited.add(scope_id)

    visit(root_scope)
    if conflicts:
        return False

    censuses = {item.census_id: item for item in inputs.evidence_set.child_censuses}
    expectations = {item.expectation_id: item for item in inputs.evidence_set.child_expectations}
    reconciliations = {
        item.reconciliation_id: item for item in inputs.evidence_set.child_reconciliations
    }
    for reconciliation in inputs.evidence_set.child_reconciliations:
        expectation = expectations[reconciliation.expectation_id]
        expected_child_ids = set(expectation.required_child_scope_ids)
        selected_child_ids = {
            item.child_scope_id for item in reconciliation.selected_child_reconciliations
        }
        if reconciliation.status == "complete" and selected_child_ids != expected_child_ids:
            conflicts.add(
                f"child reconciliation {reconciliation.reconciliation_id!r} omits a child proof"
            )
            continue
        for selected in reconciliation.selected_child_reconciliations:
            nested = reconciliations.get(selected.reconciliation_id)
            nested_expectation = expectations.get(selected.child_expectation_id)
            nested_census = censuses.get(selected.child_census_id)
            if (
                nested is None
                or nested_expectation is None
                or nested_census is None
                or nested.goal_scope_id != selected.child_scope_id
                or nested.expectation_id != selected.child_expectation_id
                or nested.source_census_id != selected.child_census_id
            ):
                conflicts.add("selected child reconciliation cross-link is inconsistent")
    return not conflicts


@dataclass(frozen=True)
class _ChildProjection:
    project_expected: int
    project_current: int
    project_missing: int
    project_stale: int


def _authorized_child_projection(
    scope_id: str,
    *,
    censuses_by_id: Mapping[str, JvmSourceCensusV1],
    expectations_by_scope: Mapping[str, JvmBuildExpectationV1],
    reconciliations_by_scope: Mapping[str, BuildReconciliationV1],
    cache: dict[str, _ChildProjection | None],
    visiting: set[str],
) -> _ChildProjection | None:
    if scope_id in cache:
        return cache[scope_id]
    if scope_id in visiting:
        cache[scope_id] = None
        return None
    expectation = expectations_by_scope.get(scope_id)
    reconciliation = reconciliations_by_scope.get(scope_id)
    if expectation is None or reconciliation is None:
        cache[scope_id] = None
        return None
    census = censuses_by_id.get(expectation.census_id)
    if census is None or reconciliation.source_census_id != census.census_id:
        cache[scope_id] = None
        return None
    visiting.add(scope_id)

    census_source_ids = {source.source_id for source in census.sources}
    classified_ids = {item.source_id for item in expectation.source_classifications}
    if classified_ids | set(expectation.unclassified_source_ids) != census_source_ids:
        visiting.remove(scope_id)
        cache[scope_id] = None
        return None

    reason_counts: dict[str, int] = {}
    for classification in expectation.source_classifications:
        if classification.classification in _OUT_OF_SCOPE_CLASSIFICATIONS:
            reason_counts[classification.classification] = (
                reason_counts.get(classification.classification, 0) + 1
            )
    expected_source_census = SourceCensusCounts(
        total_candidates=len(census.sources),
        classified=len(expectation.source_classifications),
        unclassified=len(expectation.unclassified_source_ids),
        delegated=sum(
            item.classification == "delegated_child_scope"
            for item in expectation.source_classifications
        ),
        out_of_scope_by_reason=dict(sorted(reason_counts.items())),
    )
    if reconciliation.source_census != expected_source_census:
        visiting.remove(scope_id)
        cache[scope_id] = None
        return None

    selected_compile_ids = {item.compile_unit_id for item in reconciliation.selected_compile_proofs}
    selected_generator_ids = {
        item.generator_unit_id for item in reconciliation.selected_generator_proofs
    }
    selected_package_ids = {item.package_unit_id for item in reconciliation.selected_package_proofs}
    if (
        not selected_compile_ids.issubset(expectation.required_compile_unit_ids)
        or not selected_generator_ids.issubset(expectation.required_generator_unit_ids)
        or not selected_package_ids.issubset(expectation.required_package_unit_ids)
    ):
        visiting.remove(scope_id)
        cache[scope_id] = None
        return None

    edge_current = sum(
        required_edge.compile_unit_id in selected_compile_ids
        for required_edge in expectation.required_java_edges
    )
    edges_by_source: dict[str, set[str]] = {}
    for required_edge in expectation.required_java_edges:
        edges_by_source.setdefault(required_edge.source_id, set()).add(
            required_edge.compile_unit_id
        )
    local_expected = len(edges_by_source)
    local_current = sum(
        unit_ids.issubset(selected_compile_ids) for unit_ids in edges_by_source.values()
    )

    nested_expected = 0
    nested_current = 0
    nested_missing = 0
    nested_stale = 0
    selected_children = {
        item.child_scope_id: item for item in reconciliation.selected_child_reconciliations
    }
    if not set(selected_children).issubset(expectation.required_child_scope_ids):
        visiting.remove(scope_id)
        cache[scope_id] = None
        return None
    child_complete = 0
    for child_scope_id in expectation.required_child_scope_ids:
        child_projection = _authorized_child_projection(
            child_scope_id,
            censuses_by_id=censuses_by_id,
            expectations_by_scope=expectations_by_scope,
            reconciliations_by_scope=reconciliations_by_scope,
            cache=cache,
            visiting=visiting,
        )
        child_expectation = expectations_by_scope.get(child_scope_id)
        child_reconciliation = reconciliations_by_scope.get(child_scope_id)
        if child_projection is None or child_expectation is None or child_reconciliation is None:
            visiting.remove(scope_id)
            cache[scope_id] = None
            return None
        nested_expected += child_projection.project_expected
        nested_current += child_projection.project_current
        nested_missing += child_projection.project_missing
        nested_stale += child_projection.project_stale
        selected = selected_children.get(child_scope_id)
        if selected is not None:
            child_census = censuses_by_id.get(child_expectation.census_id)
            if (
                child_census is None
                or child_reconciliation.status != "complete"
                or selected.child_census_id != child_census.census_id
                or selected.child_expectation_id != child_expectation.expectation_id
                or selected.reconciliation_id != child_reconciliation.reconciliation_id
            ):
                visiting.remove(scope_id)
                cache[scope_id] = None
                return None
            child_complete += 1
        elif child_reconciliation.status == "complete":
            visiting.remove(scope_id)
            cache[scope_id] = None
            return None

    sources_by_id = {source.source_id: source for source in census.sources}
    for delegation_edge in expectation.delegation_edges:
        parent_source = sources_by_id.get(delegation_edge.parent_source_id)
        child_expectation = expectations_by_scope.get(delegation_edge.child_scope_id)
        if parent_source is None or child_expectation is None:
            visiting.remove(scope_id)
            cache[scope_id] = None
            return None
        child_census = censuses_by_id.get(child_expectation.census_id)
        child_source = (
            next(
                (
                    item
                    for item in child_census.sources
                    if item.path == delegation_edge.expected_child_path
                ),
                None,
            )
            if child_census is not None
            else None
        )
        if (
            parent_source.sha256 != delegation_edge.expected_child_sha256
            or child_source is None
            or child_source.sha256 != delegation_edge.expected_child_sha256
        ):
            visiting.remove(scope_id)
            cache[scope_id] = None
            return None

    project_expected = local_expected + nested_expected
    project_current = local_current + nested_current
    expected_active = len(expectation.required_compile_unit_ids) + len(
        expectation.unsupported_active_unit_ids
    )
    scalar_checks = (
        reconciliation.java_sources.local_expected == local_expected,
        reconciliation.java_sources.local_current == local_current,
        reconciliation.java_sources.delegated_expected == nested_expected,
        reconciliation.java_sources.delegated_current == nested_current,
        reconciliation.java_sources.project_expected == project_expected,
        reconciliation.java_sources.project_current == project_current,
        reconciliation.java_edges.expected == len(expectation.required_java_edges),
        reconciliation.java_edges.current == edge_current,
        reconciliation.generator_units.expected >= len(expectation.required_generator_unit_ids),
        reconciliation.generator_units.current == len(selected_generator_ids),
        reconciliation.compile_units.expected == len(expectation.required_compile_unit_ids),
        reconciliation.compile_units.coverage_current == len(selected_compile_ids),
        reconciliation.package_units.expected >= len(expectation.required_package_unit_ids),
        reconciliation.package_units.current == len(selected_package_ids),
        reconciliation.child_scopes.expected == len(expectation.required_child_scope_ids),
        reconciliation.child_scopes.complete == child_complete,
        reconciliation.active_language_units.expected == expected_active,
        reconciliation.active_language_units.supported_current == len(selected_compile_ids),
    )
    if not all(scalar_checks):
        visiting.remove(scope_id)
        cache[scope_id] = None
        return None
    if reconciliation.status == "complete" and (
        project_current != project_expected
        or len(selected_generator_ids) != len(expectation.required_generator_unit_ids)
        or len(selected_compile_ids) != len(expectation.required_compile_unit_ids)
        or len(selected_package_ids) != len(expectation.required_package_unit_ids)
        or child_complete != len(expectation.required_child_scope_ids)
        or expectation.unclassified_source_ids
        or expectation.unsupported_active_unit_ids
    ):
        visiting.remove(scope_id)
        cache[scope_id] = None
        return None

    projection = _ChildProjection(
        project_expected=project_expected,
        project_current=project_current,
        project_missing=reconciliation.java_sources.project_missing,
        project_stale=reconciliation.java_sources.project_stale,
    )
    if (
        projection.project_expected
        != projection.project_current + projection.project_missing + projection.project_stale
    ):
        visiting.remove(scope_id)
        cache[scope_id] = None
        return None
    visiting.remove(scope_id)
    cache[scope_id] = projection
    return projection


def _authorized_child_expected(
    scope_id: str,
    *,
    expectations_by_scope: Mapping[str, JvmBuildExpectationV1],
    cache: dict[str, int],
    visiting: set[str],
) -> int:
    """Project the sealed expectation denominator without trusting a result row."""

    if scope_id in cache:
        return cache[scope_id]
    if scope_id in visiting:
        return 0
    expectation = expectations_by_scope.get(scope_id)
    if expectation is None:
        return 0
    visiting.add(scope_id)
    local_expected = len({edge.source_id for edge in expectation.required_java_edges})
    nested_expected = sum(
        _authorized_child_expected(
            child_scope_id,
            expectations_by_scope=expectations_by_scope,
            cache=cache,
            visiting=visiting,
        )
        for child_scope_id in expectation.required_child_scope_ids
    )
    visiting.remove(scope_id)
    expected = local_expected + nested_expected
    cache[scope_id] = expected
    return expected


def reconciliation_input_set_digest(
    inputs: ReconciliationInputs,
    selections: ReconciliationSelections,
) -> str:
    """Hash every typed head, host raw digest row, and selected assignment."""

    return str(
        canonical_build_evidence_sha256(
            {
                "digest_kind": "jvm-build-reconciliation-input-set-v1",
                "digest_schema_version": 1,
                "inputs": inputs,
                "selections": selections,
            }
        )
    )


def _module_coordinates_for_path(
    path: str,
    build_model: EvaluatedBuildModelV1,
) -> tuple[str, ...]:
    required_modules = tuple(module for module in build_model.modules if module.role == "required")
    if not required_modules:
        return ()
    project_prefix = build_model.project_root.rstrip("/") + "/"
    relative_path = path[len(project_prefix) :] if path.startswith(project_prefix) else path
    candidates = tuple(
        module
        for module in required_modules
        if relative_path == module.project_path
        or relative_path.startswith(module.project_path.rstrip("/") + "/")
    )
    if candidates:
        deepest = max(len(module.project_path.split("/")) for module in candidates)
        return tuple(
            sorted(
                module.module_coordinate
                for module in candidates
                if len(module.project_path.split("/")) == deepest
            )
        )
    if len(required_modules) == 1:
        return (required_modules[0].module_coordinate,)
    return ()


def reconcile_build(inputs: ReconciliationInputs) -> BuildReconciliationV1:
    """Fold one closed typed evidence set into a full bounded reconciliation."""

    conflicts: set[str] = set()
    unowned_module_blockers: set[str] = set()
    module_integrity_blockers: set[str] = set()
    generator_integrity_blockers: set[str] = set()
    compile_integrity_blockers: set[str] = set()
    package_integrity_blockers: set[str] = set()
    basis_generators = {
        item.generator_unit_id: item for item in inputs.final_basis.generator_unit_bases
    }
    basis_compilers = {item.compile_unit_id: item for item in inputs.final_basis.compile_unit_bases}
    basis_packages = {item.package_unit_id: item for item in inputs.final_basis.package_unit_bases}
    model_compilers = {item.compile_unit_id: item for item in inputs.build_model.compile_units}
    model_generators = {item.generator_unit_id: item for item in inputs.build_model.generator_units}
    model_packages = {item.package_unit_id: item for item in inputs.build_model.package_units}

    if any(
        (
            inputs.census.conflicts,
            inputs.build_model.conflicts,
            inputs.physical_basis.conflicts,
            inputs.evidence_set.conflicts,
        )
    ):
        unowned_module_blockers.add("top-level integrity evidence cannot be assigned to one module")

    generator_member_owners: dict[str, set[str]] = {}
    compile_member_owners: dict[str, set[str]] = {}
    package_member_owners: dict[str, set[str]] = {}

    def own(member_owners: dict[str, set[str]], member_id: str, unit_id: str) -> None:
        member_owners.setdefault(member_id, set()).add(unit_id)

    contracts_by_id = {
        contract.generated_root_id: contract
        for contract in inputs.build_model.generated_root_contracts
    }
    for generator_unit_id in inputs.expectation.required_generator_unit_ids:
        generator_unit = model_generators[generator_unit_id]
        own(
            generator_member_owners,
            generator_unit.generator_environment_spec.executable_path_basis.member_id,
            generator_unit_id,
        )
        own(
            generator_member_owners,
            generator_unit.generator_environment_spec.toolchain_path_basis.member_id,
            generator_unit_id,
        )
        for generator_member in (
            *generator_unit.input_roots,
            *generator_unit.generator_environment_spec.tool_or_plugin_members,
            *generator_unit.generator_environment_spec.dependency_members,
        ):
            own(generator_member_owners, generator_member.member_id, generator_unit_id)
        for generated_root_id in generator_unit.generated_root_ids:
            own(
                generator_member_owners,
                contracts_by_id[generated_root_id].output_root.member_id,
                generator_unit_id,
            )
    for compile_unit_id in inputs.expectation.required_compile_unit_ids:
        compile_unit = model_compilers[compile_unit_id]
        own(
            compile_member_owners,
            compile_unit.compiler_environment_spec.executable_path_basis.member_id,
            compile_unit_id,
        )
        own(
            compile_member_owners,
            compile_unit.compiler_environment_spec.toolchain_path_basis.member_id,
            compile_unit_id,
        )
        for classpath_member in compile_unit.compiler_environment_spec.classpath_members:
            own(compile_member_owners, classpath_member.member_id, compile_unit_id)
        for compile_root in _compile_output_roots(compile_unit, inputs.build_model):
            own(compile_member_owners, compile_root.member_id, compile_unit_id)
    for package_unit_id in inputs.expectation.required_package_unit_ids:
        package_unit = model_packages[package_unit_id]
        own(
            package_member_owners,
            package_unit.packaging_environment_spec.executable_path_basis.member_id,
            package_unit_id,
        )
        own(
            package_member_owners,
            package_unit.packaging_environment_spec.toolchain_path_basis.member_id,
            package_unit_id,
        )
        for package_member in (
            *package_unit.resource_roots,
            *package_unit.manifest_or_packaging_config,
            *package_unit.packaging_environment_spec.plugin_or_tool_members,
            *package_unit.packaging_environment_spec.external_dependency_members,
        ):
            own(package_member_owners, package_member.member_id, package_unit_id)
        for package_root in package_unit.output_roots:
            own(package_member_owners, package_root.member_id, package_unit_id)
        for input_compile_unit_id in package_unit.input_compile_unit_ids:
            for compile_root in _compile_output_roots(
                model_compilers[input_compile_unit_id], inputs.build_model
            ):
                own(package_member_owners, compile_root.member_id, package_unit_id)

    classifications_by_source = {
        classification.source_id: classification
        for classification in inputs.expectation.source_classifications
    }
    census_sources_by_id = {source.source_id: source for source in inputs.census.sources}
    for classification in inputs.expectation.source_classifications:
        for compile_unit_id in classification.expected_compile_unit_ids:
            own(compile_member_owners, classification.source_id, compile_unit_id)

    def block_source(source_id: str) -> None:
        classification = classifications_by_source.get(source_id)
        unit_ids = classification.expected_compile_unit_ids if classification is not None else ()
        if unit_ids:
            compile_integrity_blockers.update(unit_ids)
            return
        source = census_sources_by_id.get(source_id)
        coordinates = (
            _module_coordinates_for_path(source.path, inputs.build_model)
            if source is not None
            else ()
        )
        if coordinates:
            module_integrity_blockers.update(coordinates)
        else:
            unowned_module_blockers.add(
                f"source integrity blocker {source_id!r} has no module owner"
            )

    expected_members = _expected_physical_members(inputs.census, inputs.build_model)
    if set(inputs.physical_basis.requested_member_ids) != set(expected_members):
        unowned_module_blockers.add("physical requested-member partition differs from the model")
    invalid_member_ids = {member.member_id for member in inputs.physical_basis.unreadable_members}
    invalid_member_ids.update(
        member.member_id
        for member in inputs.physical_basis.missing_members
        if (
            (descriptor := expected_members.get(member.member_id)) is not None
            and member.path != descriptor.path
        )
    )
    invalid_member_ids.update(
        member.member_id
        for member in inputs.physical_basis.current_members
        if (
            (descriptor := expected_members.get(member.member_id)) is not None
            and (
                member.member_role != descriptor.member_role
                or member.path_kind != descriptor.path_kind
                or member.path != descriptor.path
            )
        )
    )
    invalid_member_ids.update(
        member.member_id
        for member in inputs.physical_basis.current_output_roots
        if (
            (descriptor := expected_members.get(member.member_id)) is not None
            and (
                member.root_id != descriptor.root_id
                or member.path != descriptor.path
                or member.verification_mode != descriptor.verification_mode
            )
        )
    )
    for physical_source in inputs.physical_basis.current_members:
        census_source = census_sources_by_id.get(physical_source.member_id)
        if census_source is None:
            continue
        if (
            physical_source.member_role != "source"
            or physical_source.path_kind != "file"
            or physical_source.path
            != _absolute_output_path(inputs.census.project_root, census_source.path)
            or physical_source.sha256 != census_source.sha256
        ):
            invalid_member_ids.add(physical_source.member_id)

    config_member_ids = {member.member_id for member in inputs.build_model.build_config_members}
    for invalid_member_id in invalid_member_ids:
        if invalid_member_id in config_member_ids:
            unowned_module_blockers.add(
                f"build-config member {invalid_member_id!r} is not physically trustworthy"
            )
            continue
        owned = False
        generator_owners = generator_member_owners.get(invalid_member_id, set())
        compile_owners = compile_member_owners.get(invalid_member_id, set())
        package_owners = package_member_owners.get(invalid_member_id, set())
        if generator_owners:
            generator_integrity_blockers.update(generator_owners)
            owned = True
        if compile_owners:
            compile_integrity_blockers.update(compile_owners)
            owned = True
        if package_owners:
            package_integrity_blockers.update(package_owners)
            owned = True
        if not owned and invalid_member_id in census_sources_by_id:
            block_source(invalid_member_id)
            owned = True
        if not owned:
            unowned_module_blockers.add(
                f"physical member {invalid_member_id!r} has no required-unit owner"
            )

    for source in inputs.census.sources:
        if (
            source.symlink_status not in {"contained", "symlink_contained"}
            or _source_language_conflict(source) is not None
        ):
            block_source(source.source_id)
    for source_id in inputs.expectation.unclassified_source_ids:
        block_source(source_id)
    for unreadable_root in inputs.census.unreadable_roots:
        coordinates = _module_coordinates_for_path(unreadable_root, inputs.build_model)
        if coordinates:
            module_integrity_blockers.update(coordinates)
        else:
            unowned_module_blockers.add(
                f"unreadable census root {unreadable_root!r} has no module owner"
            )
    if inputs.final_basis.conflicts and not invalid_member_ids:
        unowned_module_blockers.add(
            "final-basis integrity conflict cannot be assigned to a physical member"
        )

    statuses = _record_statuses(inputs.evidence_set)
    incomplete_rows = {kind for kind, status in statuses.items() if status != "complete"}
    for row in inputs.evidence_set.record_sets:
        if row.status != "complete":
            conflicts.add(f"evidence record set {row.record_kind!r} is {row.status}")
    conflicts.update(inputs.census.conflicts)
    conflicts.update(inputs.build_model.conflicts)
    conflicts.update(inputs.expectation.conflicts)
    conflicts.update(inputs.final_basis.conflicts)
    conflicts.update(inputs.physical_basis.conflicts)
    conflicts.update(inputs.evidence_set.conflicts)
    if inputs.census.unreadable_roots:
        conflicts.add("source census contains unreadable roots")
    if inputs.expectation.unclassified_source_ids:
        conflicts.add("source census contains unclassified sources")
    if inputs.expectation.unsupported_active_unit_ids:
        conflicts.add("build model contains unsupported active language units")

    selected_generators: list[SelectedGeneratorProof] = []
    selected_compilers: list[SelectedCompileProof] = []
    selected_packages: list[SelectedPackageProof] = []
    selected_children: list[SelectedChildReconciliation] = []
    entry_states: dict[str, tuple[str, str]] = {}
    selected_claims: dict[str, tuple[str, str]] = {}

    def absorb_entries(
        check: _WitnessCheck,
        *,
        proof_owner: str,
        selected: bool,
    ) -> None:
        for absolute_path, entry_kind, state in check.entry_states:
            previous_state = entry_states.get(absolute_path)
            if previous_state is None:
                entry_states[absolute_path] = (entry_kind, state)
            elif previous_state != (entry_kind, state):
                entry_states[absolute_path] = (entry_kind, "mismatched")
            if selected:
                previous_owner = selected_claims.get(absolute_path)
                if previous_owner is not None and previous_owner[0] != proof_owner:
                    conflicts.add(f"selected output witnesses overlap at {absolute_path!r}")
                    unowned_module_blockers.add(
                        "selected output ownership conflict spans proof units"
                    )
                else:
                    selected_claims[absolute_path] = (proof_owner, entry_kind)

    generator_state: dict[str, str] = {}
    generator_checks: dict[str, _WitnessCheck] = {}
    generator_observation_row_ok = "generator_observation" not in incomplete_rows
    witness_row_ok = "output_witness" not in incomplete_rows
    for unit_id in inputs.expectation.required_generator_unit_ids:
        generator_basis = basis_generators.get(unit_id)
        if unit_id in generator_integrity_blockers:
            generator_state[unit_id] = "unverifiable"
            continue
        if not generator_observation_row_ok or not witness_row_ok:
            generator_state[unit_id] = "unverifiable"
            continue
        if generator_basis is None:
            generator_state[unit_id] = "missing"
            continue
        generator_observations = tuple(
            generator_observation
            for generator_observation in inputs.evidence_set.generator_observations
            if generator_observation.generator_unit_id == unit_id
        )
        exact_generator_observations = tuple(
            generator_observation
            for generator_observation in generator_observations
            if _basis_identity_equal(generator_observation, generator_basis, "generator")
        )
        if any(
            generator_observation.conflicts
            for generator_observation in exact_generator_observations
        ):
            conflicts.add(f"generator unit {unit_id!r} has a conflict-bearing current observation")
            generator_state[unit_id] = "unverifiable"
            continue
        failed_generator_observations = tuple(
            generator_observation
            for generator_observation in exact_generator_observations
            if generator_observation.outcome == "failed"
            and _invocation_covers(
                generator_observation,
                model_generators[unit_id],
                inputs.build_model,
            )
        )
        if failed_generator_observations:
            conflicts.add(f"generator unit {unit_id!r} has a current failed observation")
            generator_state[unit_id] = "unverifiable"
            continue
        generator_candidates: list[tuple[GeneratorUnitObservationV1, _WitnessCheck]] = []
        for generator_observation in exact_generator_observations:
            if (
                generator_observation.outcome not in _POSITIVE_OUTCOMES
                or generator_observation.conflicts
                or not _invocation_covers(
                    generator_observation,
                    model_generators[unit_id],
                    inputs.build_model,
                )
            ):
                continue
            generator_check = _witness_check(
                observation=generator_observation,
                kind="generator",
                roots=generator_basis.generated_output_roots,
                evidence_set=inputs.evidence_set,
                physical=inputs.physical_basis,
                project_root=inputs.build_model.project_root,
                required=True,
            )
            generator_candidates.append((generator_observation, generator_check))
        verified_generator_candidates = tuple(
            item for item in generator_candidates if item[1].state == "verified"
        )
        if verified_generator_candidates:
            selected_generator_observation, selected_generator_check = sorted(
                verified_generator_candidates,
                key=lambda item: item[0].observation_id,
            )[0]
            selected_generators.append(
                SelectedGeneratorProof(
                    generator_unit_id=unit_id,
                    generator_unit_identity_hash=generator_basis.generator_unit_identity_hash,
                    observation_id=selected_generator_observation.observation_id,
                    witness_id=(
                        selected_generator_check.witness.witness_id
                        if selected_generator_check.witness
                        else ""
                    ),
                )
            )
            generator_state[unit_id] = "current"
            generator_checks[unit_id] = selected_generator_check
            absorb_entries(
                selected_generator_check,
                proof_owner=f"generator:{unit_id}",
                selected=True,
            )
        elif generator_candidates:
            rejected_generator_observation, rejected_generator_check = sorted(
                generator_candidates, key=lambda item: item[0].observation_id
            )[0]
            del rejected_generator_observation
            generator_checks[unit_id] = rejected_generator_check
            absorb_entries(
                rejected_generator_check,
                proof_owner=f"generator:{unit_id}",
                selected=False,
            )
            generator_state[unit_id] = (
                "unverifiable" if rejected_generator_check.state == "unverifiable" else "missing"
            )
        else:
            generator_state[unit_id] = "stale" if generator_observations else "missing"

    generated_consumer_conflicts: set[str] = set()
    for contract in inputs.build_model.generated_root_contracts:
        if contract.mode != "later_compile_input" or contract.producer_unit_kind != "generator":
            continue
        producer_check = generator_checks.get(contract.producer_unit_id)
        if generator_state.get(
            contract.producer_unit_id
        ) != "current" or not _generated_handoff_is_current(
            contract,
            producer_check,
            inputs,
        ):
            generated_consumer_conflicts.update(contract.consumer_unit_ids)
    if generated_consumer_conflicts:
        conflicts.add("generated source handoff bytes do not match the consuming census")

    compile_state: dict[str, str] = {}
    compile_output_state: dict[str, str] = {}
    compile_checks: dict[str, _WitnessCheck] = {}
    compile_observation_row_ok = "compile_observation" not in incomplete_rows
    for unit_id in inputs.expectation.required_compile_unit_ids:
        compile_basis = basis_compilers.get(unit_id)
        compile_unit = model_compilers[unit_id]
        if unit_id in compile_integrity_blockers:
            compile_state[unit_id] = "unverifiable"
            compile_output_state[unit_id] = "unverifiable"
            continue
        if not compile_observation_row_ok:
            compile_state[unit_id] = "unverifiable"
            compile_output_state[unit_id] = "unverifiable"
            continue
        if unit_id in generated_consumer_conflicts:
            compile_state[unit_id] = "unverifiable"
            compile_output_state[unit_id] = "unverifiable"
            continue
        if compile_basis is None:
            compile_state[unit_id] = "missing"
            compile_output_state[unit_id] = (
                "not_required"
                if not _compile_output_roots(compile_unit, inputs.build_model)
                else "missing"
            )
            continue
        compile_observations = tuple(
            compile_observation
            for compile_observation in inputs.evidence_set.compile_observations
            if compile_observation.compile_unit_id == unit_id
        )
        exact_compile_observations = tuple(
            compile_observation
            for compile_observation in compile_observations
            if _basis_identity_equal(compile_observation, compile_basis, "compile")
        )
        if any(compile_observation.conflicts for compile_observation in exact_compile_observations):
            conflicts.add(f"compile unit {unit_id!r} has a conflict-bearing current observation")
            compile_state[unit_id] = "unverifiable"
            compile_output_state[unit_id] = "unverifiable"
            continue
        failed_compile_observations = tuple(
            compile_observation
            for compile_observation in exact_compile_observations
            if compile_observation.outcome == "failed"
            and _invocation_covers(compile_observation, compile_unit, inputs.build_model)
        )
        if failed_compile_observations:
            conflicts.add(f"compile unit {unit_id!r} has a current failed observation")
            compile_state[unit_id] = "unverifiable"
            compile_output_state[unit_id] = "unverifiable"
            continue
        positive_compile_observations = tuple(
            compile_observation
            for compile_observation in exact_compile_observations
            if compile_observation.outcome in _POSITIVE_OUTCOMES
            and not compile_observation.conflicts
            and _invocation_covers(compile_observation, compile_unit, inputs.build_model)
        )
        if not positive_compile_observations:
            compile_state[unit_id] = "stale" if compile_observations else "missing"
            compile_output_state[unit_id] = (
                "not_required"
                if not _compile_output_roots(compile_unit, inputs.build_model)
                else "missing"
            )
            continue
        compile_candidates: list[tuple[CompileUnitObservationV1, _WitnessCheck]] = []
        for compile_observation in positive_compile_observations:
            if compile_basis.output_roots and not witness_row_ok:
                compile_check = _WitnessCheck(None, "unverifiable", ())
            else:
                compile_check = _witness_check(
                    observation=compile_observation,
                    kind="compile",
                    roots=compile_basis.output_roots,
                    evidence_set=inputs.evidence_set,
                    physical=inputs.physical_basis,
                    project_root=inputs.build_model.project_root,
                    required=bool(compile_basis.output_roots),
                )
            compile_candidates.append((compile_observation, compile_check))
        compile_candidates.sort(
            key=lambda item: (
                item[1].state not in {"verified", "not_required"},
                item[0].observation_id,
            )
        )
        selected_compile_observation, selected_compile_check = compile_candidates[0]
        compile_state[unit_id] = "current"
        compile_output_state[unit_id] = selected_compile_check.state
        compile_checks[unit_id] = selected_compile_check
        selected_compilers.append(
            SelectedCompileProof(
                compile_unit_id=unit_id,
                compile_unit_identity_hash=compile_basis.compile_unit_identity_hash,
                observation_id=selected_compile_observation.observation_id,
                witness_id=(
                    selected_compile_check.witness.witness_id
                    if selected_compile_check.state == "verified"
                    and selected_compile_check.witness is not None
                    else None
                ),
            )
        )
        absorb_entries(
            selected_compile_check,
            proof_owner=f"compile:{unit_id}",
            selected=selected_compile_check.state == "verified",
        )

    compile_generated_consumer_conflicts: set[str] = set()
    for contract in inputs.build_model.generated_root_contracts:
        if contract.mode != "later_compile_input" or contract.producer_unit_kind != "compile":
            continue
        producer_check = compile_checks.get(contract.producer_unit_id)
        if compile_state.get(
            contract.producer_unit_id
        ) != "current" or not _generated_handoff_is_current(
            contract,
            producer_check,
            inputs,
        ):
            compile_generated_consumer_conflicts.update(contract.consumer_unit_ids)
    if compile_generated_consumer_conflicts:
        conflicts.add("generated source handoff bytes do not match the consuming census")
        for consumer_id in compile_generated_consumer_conflicts:
            if consumer_id in compile_state:
                compile_state[consumer_id] = "unverifiable"
                compile_output_state[consumer_id] = "unverifiable"
            proof_owner = f"compile:{consumer_id}"
            for absolute_path, owner in tuple(selected_claims.items()):
                if owner[0] == proof_owner:
                    del selected_claims[absolute_path]
        selected_compilers = [
            proof
            for proof in selected_compilers
            if proof.compile_unit_id not in compile_generated_consumer_conflicts
        ]

    package_state: dict[str, str] = {}
    package_observation_row_ok = "package_observation" not in incomplete_rows
    for unit_id in inputs.expectation.required_package_unit_ids:
        package_basis = basis_packages.get(unit_id)
        if unit_id in package_integrity_blockers:
            package_state[unit_id] = "unverifiable"
            continue
        if not package_observation_row_ok or not witness_row_ok:
            package_state[unit_id] = "unverifiable"
            continue
        if package_basis is None:
            package_state[unit_id] = "missing"
            continue
        package_observations = tuple(
            package_observation
            for package_observation in inputs.evidence_set.package_observations
            if package_observation.package_unit_id == unit_id
        )
        exact_package_observations = tuple(
            package_observation
            for package_observation in package_observations
            if _basis_identity_equal(package_observation, package_basis, "package")
        )
        if any(package_observation.conflicts for package_observation in exact_package_observations):
            conflicts.add(f"package unit {unit_id!r} has a conflict-bearing current observation")
            package_state[unit_id] = "unverifiable"
            continue
        failed_package_observations = tuple(
            package_observation
            for package_observation in exact_package_observations
            if package_observation.outcome == "failed"
            and _invocation_covers(
                package_observation,
                model_packages[unit_id],
                inputs.build_model,
            )
        )
        if failed_package_observations:
            conflicts.add(f"package unit {unit_id!r} has a current failed observation")
            package_state[unit_id] = "unverifiable"
            continue
        package_candidates: list[tuple[PackageUnitObservationV1, _WitnessCheck]] = []
        for package_observation in exact_package_observations:
            if (
                package_observation.outcome not in _POSITIVE_OUTCOMES
                or package_observation.conflicts
                or not _invocation_covers(
                    package_observation,
                    model_packages[unit_id],
                    inputs.build_model,
                )
            ):
                continue
            package_check = _witness_check(
                observation=package_observation,
                kind="package",
                roots=package_basis.output_roots,
                evidence_set=inputs.evidence_set,
                physical=inputs.physical_basis,
                project_root=inputs.build_model.project_root,
                required=True,
                requested_artifacts=package_basis.requested_artifacts,
            )
            package_candidates.append((package_observation, package_check))
        verified_package_candidates = tuple(
            item for item in package_candidates if item[1].state == "verified"
        )
        if verified_package_candidates:
            selected_package_observation, selected_package_check = sorted(
                verified_package_candidates,
                key=lambda item: item[0].observation_id,
            )[0]
            selected_packages.append(
                SelectedPackageProof(
                    package_unit_id=unit_id,
                    package_unit_identity_hash=package_basis.package_unit_identity_hash,
                    observation_id=selected_package_observation.observation_id,
                    witness_id=(
                        selected_package_check.witness.witness_id
                        if selected_package_check.witness
                        else ""
                    ),
                )
            )
            package_state[unit_id] = "current"
            absorb_entries(
                selected_package_check,
                proof_owner=f"package:{unit_id}",
                selected=True,
            )
        elif package_candidates:
            rejected_package_observation, rejected_package_check = sorted(
                package_candidates, key=lambda item: item[0].observation_id
            )[0]
            del rejected_package_observation
            absorb_entries(
                rejected_package_check,
                proof_owner=f"package:{unit_id}",
                selected=False,
            )
            package_state[unit_id] = (
                "unverifiable" if rejected_package_check.state == "unverifiable" else "missing"
            )
        else:
            package_state[unit_id] = "stale" if package_observations else "missing"

    child_authority_ok = (
        not {
            "child_census",
            "child_expectation",
            "child_reconciliation",
        }
        & incomplete_rows
    )
    child_graph_conflicts: set[str] = set()
    child_graph_ok = _child_graph_is_valid(inputs, child_graph_conflicts)
    if child_graph_conflicts:
        conflicts.update(child_graph_conflicts)
        unowned_module_blockers.update(child_graph_conflicts)
    child_expectations_by_scope: dict[str, JvmBuildExpectationV1] = {}
    for published_child_expectation in inputs.evidence_set.child_expectations:
        child_expectations_by_scope.setdefault(
            published_child_expectation.scope_id,
            published_child_expectation,
        )
    child_reconciliations_by_scope = {
        published_child_reconciliation.goal_scope_id: published_child_reconciliation
        for published_child_reconciliation in inputs.evidence_set.child_reconciliations
    }
    child_censuses_by_id = {
        published_child_census.census_id: published_child_census
        for published_child_census in inputs.evidence_set.child_censuses
    }
    parent_sources = {source.source_id: source for source in inputs.census.sources}
    current_physical_members = {
        member.member_id: member for member in inputs.physical_basis.current_members
    }
    missing_physical_member_ids = {
        member.member_id for member in inputs.physical_basis.missing_members
    }
    unreadable_physical_member_ids = {
        member.member_id for member in inputs.physical_basis.unreadable_members
    }
    delegation_by_child: dict[str, list[DelegationEdge]] = {}
    for parent_delegation_edge in inputs.expectation.delegation_edges:
        delegation_by_child.setdefault(parent_delegation_edge.child_scope_id, []).append(
            parent_delegation_edge
        )
    child_projection_cache: dict[str, _ChildProjection | None] = {}
    child_expected_cache: dict[str, int] = {}
    child_state: dict[str, str] = {}
    delegated_expected = 0
    delegated_current = 0
    delegated_missing = 0
    delegated_stale = 0
    for scope_id in inputs.expectation.required_child_scope_ids:
        child_expectation = child_expectations_by_scope.get(scope_id)
        child_reconciliation = child_reconciliations_by_scope.get(scope_id)
        projection = _authorized_child_projection(
            scope_id,
            censuses_by_id=child_censuses_by_id,
            expectations_by_scope=child_expectations_by_scope,
            reconciliations_by_scope=child_reconciliations_by_scope,
            cache=child_projection_cache,
            visiting=set(),
        )
        authorized_expected = _authorized_child_expected(
            scope_id,
            expectations_by_scope=child_expectations_by_scope,
            cache=child_expected_cache,
            visiting=set(),
        )
        valid = child_authority_ok and child_graph_ok and projection is not None
        child_census = None
        if child_expectation is None or child_reconciliation is None:
            valid = False
        else:
            child_census = child_censuses_by_id.get(child_expectation.census_id)
            if child_census is None:
                valid = False
        if (
            child_census is not None
            and child_expectation is not None
            and child_reconciliation is not None
        ):
            if (
                child_census.unreadable_roots
                or child_census.conflicts
                or child_expectation.conflicts
            ):
                valid = False
            child_sources_by_path = {source.path: source for source in child_census.sources}
            seen_child_sources: set[str] = set()
            for handoff_edge in delegation_by_child.get(scope_id, ()):
                parent = parent_sources.get(handoff_edge.parent_source_id)
                child_source = child_sources_by_path.get(handoff_edge.expected_child_path)
                current_parent = (
                    current_physical_members.get(parent.source_id) if parent is not None else None
                )
                physical_parent_is_current = (
                    parent is not None
                    and parent.source_id not in missing_physical_member_ids
                    and parent.source_id not in unreadable_physical_member_ids
                    and current_parent is not None
                    and current_parent.member_role == "source"
                    and current_parent.path_kind == "file"
                    and current_parent.path
                    == _absolute_output_path(inputs.census.project_root, parent.path)
                    and current_parent.sha256 == parent.sha256
                )
                if (
                    parent is None
                    or parent.sha256 != handoff_edge.expected_child_sha256
                    or child_source is None
                    or child_source.sha256 != handoff_edge.expected_child_sha256
                    or child_source.source_id in seen_child_sources
                    or not physical_parent_is_current
                ):
                    valid = False
                    continue
                seen_child_sources.add(child_source.source_id)
        delegated_floor = len(delegation_by_child.get(scope_id, ()))
        child_expected = max(authorized_expected, delegated_floor)
        delegated_expected += child_expected
        if valid and projection is not None:
            delegated_current += min(projection.project_current, child_expected)
            delegated_missing += projection.project_missing
            delegated_stale += (
                projection.project_stale + child_expected - projection.project_expected
            )
        else:
            delegated_stale += child_expected
        if not valid:
            conflicts.add(f"child scope {scope_id!r} handoff or evidence is unverifiable")
            child_state[scope_id] = "unverifiable"
            continue
        assert child_expectation is not None
        assert child_reconciliation is not None
        assert child_census is not None
        if child_reconciliation.status == "complete":
            child_state[scope_id] = "current"
            selected_children.append(
                SelectedChildReconciliation(
                    child_scope_id=scope_id,
                    child_census_id=child_census.census_id,
                    child_expectation_id=child_expectation.expectation_id,
                    reconciliation_id=child_reconciliation.reconciliation_id,
                )
            )
        elif child_reconciliation.status == "unverifiable":
            conflicts.add(f"child scope {scope_id!r} reconciliation is unverifiable")
            child_state[scope_id] = "unverifiable"
        else:
            child_state[scope_id] = "missing"

    required_edges = tuple(inputs.expectation.required_java_edges)
    edge_states: list[str] = []
    for required_edge in required_edges:
        if required_edge.compile_unit_id in inputs.expectation.unsupported_active_unit_ids:
            edge_states.append("stale")
        else:
            state = compile_state.get(required_edge.compile_unit_id, "missing")
            edge_states.append(
                "current"
                if state == "current"
                else "stale" if state in {"stale", "unverifiable"} else "missing"
            )
    edges_by_source: dict[str, list[str]] = {}
    for required_edge, state in zip(required_edges, edge_states, strict=True):
        edges_by_source.setdefault(required_edge.source_id, []).append(state)
    local_expected = len(edges_by_source)
    local_current = sum(
        all(state == "current" for state in states) for states in edges_by_source.values()
    )
    local_stale = sum(
        any(state == "stale" for state in states) for states in edges_by_source.values()
    )
    local_missing = local_expected - local_current - local_stale

    classifications = inputs.expectation.source_classifications
    reason_counts: dict[str, int] = {}
    for classification in classifications:
        if classification.classification in _OUT_OF_SCOPE_CLASSIFICATIONS:
            reason_counts[classification.classification] = (
                reason_counts.get(classification.classification, 0) + 1
            )
    source_census_counts = SourceCensusCounts(
        total_candidates=len(inputs.census.sources),
        classified=len(classifications),
        unclassified=len(inputs.expectation.unclassified_source_ids),
        delegated=sum(item.classification == "delegated_child_scope" for item in classifications),
        out_of_scope_by_reason=dict(sorted(reason_counts.items())),
    )
    java_source_counts = JavaSourceCounts(
        local_expected=local_expected,
        local_current=local_current,
        delegated_expected=delegated_expected,
        delegated_current=delegated_current,
        project_expected=local_expected + delegated_expected,
        project_current=local_current + delegated_current,
        project_missing=local_missing + delegated_missing,
        project_stale=local_stale + delegated_stale,
    )
    java_edge_counts = JavaEdgeCounts(
        expected=len(required_edges),
        current=edge_states.count("current"),
        missing=edge_states.count("missing"),
        stale=edge_states.count("stale"),
    )

    generator_counts = GeneratorUnitCounts(
        expected=len(inputs.expectation.required_generator_unit_ids),
        current=sum(state == "current" for state in generator_state.values()),
        missing_or_invalid=sum(state in {"missing", "stale"} for state in generator_state.values()),
        unverifiable=sum(state == "unverifiable" for state in generator_state.values()),
    )
    compile_counts = CompileUnitCounts(
        expected=len(inputs.expectation.required_compile_unit_ids),
        coverage_current=sum(state == "current" for state in compile_state.values()),
        coverage_missing_or_invalid=sum(
            state in {"missing", "stale"} for state in compile_state.values()
        ),
        coverage_unverifiable=sum(state == "unverifiable" for state in compile_state.values()),
    )
    package_counts = PackageUnitCounts(
        expected=len(inputs.expectation.required_package_unit_ids),
        current=sum(state == "current" for state in package_state.values()),
        missing_or_invalid=sum(state in {"missing", "stale"} for state in package_state.values()),
        unverifiable=sum(state == "unverifiable" for state in package_state.values()),
    )
    witnesses_expected = (
        len(inputs.expectation.required_generator_unit_ids)
        + sum(
            bool(_compile_output_roots(model_compilers[unit_id], inputs.build_model))
            for unit_id in inputs.expectation.required_compile_unit_ids
        )
        + len(inputs.expectation.required_package_unit_ids)
    )
    witnesses_verified = (
        len(selected_generators)
        + sum(item.witness_id is not None for item in selected_compilers)
        + len(selected_packages)
    )
    sealed_entries = len(entry_states)
    verified_entries = sum(state == "verified" for _, state in entry_states.values())
    missing_entries = sum(state == "missing" for _, state in entry_states.values())
    mismatched_entries = sealed_entries - verified_entries - missing_entries
    physical_counts = PhysicalOutputCounts(
        observed_class_count=sum(entry_kind == "class" for entry_kind, _ in entry_states.values()),
        witnesses_expected=witnesses_expected,
        witnesses_verified=witnesses_verified,
        sealed_entries=sealed_entries,
        verified_entries=verified_entries,
        missing_entries=missing_entries,
        mismatched_entries=mismatched_entries,
    )

    active_expected = len(inputs.expectation.required_compile_unit_ids) + len(
        inputs.expectation.unsupported_active_unit_ids
    )
    active_counts = ActiveLanguageUnitCounts(
        expected=active_expected,
        supported_current=compile_counts.coverage_current,
        unsupported_or_unverifiable=active_expected - compile_counts.coverage_current,
    )
    child_counts = ChildScopeCounts(
        expected=len(inputs.expectation.required_child_scope_ids),
        complete=sum(state == "current" for state in child_state.values()),
        incomplete_or_unverifiable=sum(state != "current" for state in child_state.values()),
    )

    uncertainty = bool(conflicts) or any(
        (
            generator_counts.unverifiable,
            compile_counts.coverage_unverifiable,
            package_counts.unverifiable,
            source_census_counts.unclassified,
            len(inputs.expectation.unsupported_active_unit_ids),
            any(state == "unverifiable" for state in child_state.values()),
            any(state == "unverifiable" for state in compile_output_state.values()),
        )
    )
    known_missing = any(
        (
            java_source_counts.project_missing,
            java_source_counts.project_stale,
            java_edge_counts.missing,
            java_edge_counts.stale,
            generator_counts.missing_or_invalid,
            compile_counts.coverage_missing_or_invalid,
            package_counts.missing_or_invalid,
            witnesses_expected - witnesses_verified,
            physical_counts.missing_entries,
            physical_counts.mismatched_entries,
            child_counts.incomplete_or_unverifiable,
        )
    )
    current_work = any(
        (
            generator_counts.current,
            compile_counts.coverage_current,
            package_counts.current,
            child_counts.complete,
            java_source_counts.project_current,
            witnesses_verified,
        )
    )
    status: _ReconciliationStatus
    if uncertainty:
        status = "unverifiable"
    elif known_missing:
        status = "partial" if current_work else "absent"
    else:
        status = "complete"

    required_modules = tuple(
        module for module in inputs.build_model.modules if module.role == "required"
    )
    child_scopes_by_id = {
        child_scope.child_scope_id: child_scope
        for child_scope in inputs.build_model.child_build_scopes
    }
    module_states: list[Literal["built", "partial", "unverifiable"]] = []
    for module in required_modules:
        obligation_states: list[str] = []
        for generator_unit_id in inputs.expectation.required_generator_unit_ids:
            if model_generators[generator_unit_id].module_coordinate == module.module_coordinate:
                obligation_states.append(generator_state.get(generator_unit_id, "missing"))
        for compile_unit_id in inputs.expectation.required_compile_unit_ids:
            if model_compilers[compile_unit_id].module_coordinate == module.module_coordinate:
                obligation_states.append(compile_state.get(compile_unit_id, "missing"))
                obligation_states.append(compile_output_state.get(compile_unit_id, "missing"))
        for unsupported_unit_id in inputs.expectation.unsupported_active_unit_ids:
            if model_compilers[unsupported_unit_id].module_coordinate == module.module_coordinate:
                obligation_states.append("unverifiable")
        for package_unit_id in inputs.expectation.required_package_unit_ids:
            if model_packages[package_unit_id].module_coordinate == module.module_coordinate:
                obligation_states.append(package_state.get(package_unit_id, "missing"))
        for child_scope_id in inputs.expectation.required_child_scope_ids:
            child_scope = child_scopes_by_id[child_scope_id]
            if module.module_coordinate in _module_coordinates_for_path(
                child_scope.child_project_root,
                inputs.build_model,
            ):
                obligation_states.append(child_state.get(child_scope_id, "missing"))

        if (
            unowned_module_blockers
            or module.module_coordinate in module_integrity_blockers
            or "unverifiable" in obligation_states
        ):
            module_states.append("unverifiable")
        elif all(state in {"current", "verified", "not_required"} for state in obligation_states):
            module_states.append("built")
        else:
            module_states.append("partial")
    module_counts = ModuleCounts(
        expected=len(required_modules),
        built=module_states.count("built"),
        partial=module_states.count("partial"),
        unverifiable=module_states.count("unverifiable"),
    )

    selected_generators.sort(key=lambda item: item.generator_unit_id)
    selected_compilers.sort(key=lambda item: item.compile_unit_id)
    selected_packages.sort(key=lambda item: item.package_unit_id)
    selected_children.sort(key=lambda item: item.child_scope_id)
    selections = ReconciliationSelections(
        selected_generator_proofs=tuple(selected_generators),
        selected_compile_proofs=tuple(selected_compilers),
        selected_package_proofs=tuple(selected_packages),
        selected_child_reconciliations=tuple(selected_children),
        conflicts=tuple(sorted(conflicts)),
    )
    input_set_digest = reconciliation_input_set_digest(inputs, selections)
    evidence_refs = tuple(
        sorted(
            {
                *(item.observation_id for item in selected_generators),
                *(item.witness_id for item in selected_generators),
                *(item.observation_id for item in selected_compilers),
                *(item.witness_id for item in selected_compilers if item.witness_id is not None),
                *(item.observation_id for item in selected_packages),
                *(item.witness_id for item in selected_packages),
                *(item.reconciliation_id for item in selected_children),
            }
        )
    )
    return BuildReconciliationV1(
        reconciliation_id=canonical_content_id(
            "build-reconciliation-v1",
            {
                "evidence_epoch_id": inputs.epoch.evidence_epoch_id,
                "input_set_digest": input_set_digest,
            },
        ),
        evidence_epoch_id=inputs.epoch.evidence_epoch_id,
        source_census_id=inputs.census.census_id,
        goal_scope_id=inputs.goal_scope.scope_id,
        build_model_id=inputs.build_model.model_id,
        expectation_id=inputs.expectation.expectation_id,
        final_basis_id=inputs.final_basis.basis_id,
        input_set_digest=input_set_digest,
        requested_action=inputs.goal_scope.requested_action,
        status=status,
        source_census=source_census_counts,
        java_sources=java_source_counts,
        java_edges=java_edge_counts,
        generator_units=generator_counts,
        compile_units=compile_counts,
        physical_outputs=physical_counts,
        package_units=package_counts,
        active_language_units=active_counts,
        child_scopes=child_counts,
        modules=module_counts,
        selected_generator_proofs=selections.selected_generator_proofs,
        selected_compile_proofs=selections.selected_compile_proofs,
        selected_package_proofs=selections.selected_package_proofs,
        selected_child_reconciliations=selections.selected_child_reconciliations,
        conflicts=selections.conflicts,
        evidence_refs=evidence_refs,
    )


__all__ = [
    "ReconciliationInputs",
    "ReconciliationSelections",
    "derive_expectation",
    "derive_final_basis",
    "reconcile_build",
    "reconciliation_input_set_digest",
]
