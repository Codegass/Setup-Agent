"""Rule-based Java build and test success certificates.

The certificate is deliberately orthogonal to the legacy rate-banded verdict.
It proves closure over typed obligation identities; diagnostic counts such as
class files versus source files or runtime invocations versus static test
declarations never participate in the decision.

This module is pure.  Adapters may project Maven/Gradle plans and receipts into
``JavaCertificateInput`` without giving the evaluator filesystem, process, or
network authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sag.agent.invocation_receipts import RECEIPT_SCHEMA_VERSION
from sag.agent.receipt_suite_totals import SuiteExecutionTotals, receipt_suite_totals

CERTIFICATE_SCHEMA_VERSION = 1

ScopeKind = Literal[
    "repository_default",
    "repository_product_tests",
    "ci_matrix",
    "declared_entrypoint",
    "safe_selected",
    "selected_reactor",
    "module",
    "targeted",
    "unknown",
]
ScopeClosure = Literal["closed", "partial", "unknown"]
Applicability = Literal["required", "not_applicable", "unknown"]
ObligationStatus = Literal["complete", "failed", "incomplete", "not_applicable", "unavailable"]
BuildStatus = Literal["success", "failed", "incomplete", "unverifiable"]
TestExecutionStatus = Literal["complete", "failed", "incomplete", "unverifiable", "not_applicable"]
TestOutcomeStatus = Literal["clean", "red", "empty", "unknown", "not_applicable"]
AssuranceLevel = Literal["legacy_projected", "receipt_bound", "sealed_lineage"]
ProofStatus = Literal["verified", "projected", "partial", "unverifiable"]
CertificateResult = Literal[
    "repository_green",
    "repository_product_test_green",
    "scoped_green",
    "build_failed",
    "test_execution_failed",
    "test_red",
    "build_only",
    "incomplete",
    "unverifiable",
]
ProofAxis = Literal["scope", "build", "test_execution", "test_outcome", "integrity"]
ObligationUnit = Literal[
    "build_plan_step",
    "maven_reactor_module",
    "single_maven_project",
    "gradle_task",
    "test_plan_step",
    "test_module",
    "single_test_project",
    "gradle_test_task",
    "evidence_binding",
]
IntegrityStatus = Literal["complete", "degraded", "unavailable"]
TestResultsAuthority = Literal["receipt_bound", "diagnostic", "unavailable"]

# SAG-MS-1 truth surface.  The internal four values collapse to three in
# public: ``INCONC`` and ``ERROR`` both read ``UNKNOWN`` while the result class
# keeps the species (``incomplete`` versus ``unverifiable``).
TruthValue = Literal["PASS", "FAIL", "UNKNOWN"]
AxisTruth = Literal["PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE"]
Assurance = Literal["PROJECTED", "RECEIPT_BOUND", "SEALED_LINEAGE"]
DefeaterType = Literal[
    "rebutting",
    "undercutting",
    "incompleteness",
    "vacuity",
    "applicability",
]

OVERALL_TRUTH_BY_RESULT: dict[CertificateResult, TruthValue] = {
    "repository_green": "PASS",
    "repository_product_test_green": "PASS",
    "scoped_green": "PASS",
    "build_only": "PASS",
    "build_failed": "FAIL",
    "test_execution_failed": "FAIL",
    "test_red": "FAIL",
    "incomplete": "UNKNOWN",
    "unverifiable": "UNKNOWN",
}
BUILD_AXIS_TRUTH: dict[BuildStatus, AxisTruth] = {
    "success": "PASS",
    "failed": "FAIL",
    "incomplete": "UNKNOWN",
    "unverifiable": "UNKNOWN",
}
TEST_EXECUTION_AXIS_TRUTH: dict[TestExecutionStatus, AxisTruth] = {
    "complete": "PASS",
    "failed": "FAIL",
    "incomplete": "UNKNOWN",
    "unverifiable": "UNKNOWN",
    "not_applicable": "NOT_APPLICABLE",
}
TEST_OUTCOME_AXIS_TRUTH: dict[TestOutcomeStatus, AxisTruth] = {
    "clean": "PASS",
    "red": "FAIL",
    "empty": "UNKNOWN",
    "unknown": "UNKNOWN",
    "not_applicable": "NOT_APPLICABLE",
}
INTEGRITY_AXIS_TRUTH: dict[IntegrityStatus, AxisTruth] = {
    "complete": "PASS",
    "degraded": "UNKNOWN",
    "unavailable": "UNKNOWN",
}
# SAG-MS-1 §6.1: a blocker is a recorded finding that observations on one axis
# fail the binding predicate, so every affected axis names its own undercutting
# defeater on the public surface.
BLOCKED_AXIS_REASON_CODES: dict[ProofAxis, str] = {
    "scope": "SCOPE_AUTHORITY_BLOCKED",
    "build": "BUILD_AUTHORITY_BLOCKED",
    "test_execution": "TEST_EXECUTION_AUTHORITY_BLOCKED",
    "test_outcome": "TEST_OUTCOME_AUTHORITY_BLOCKED",
    "integrity": "INTEGRITY_AUTHORITY_BLOCKED",
}
ASSURANCE_BY_LEVEL: dict[AssuranceLevel, Assurance] = {
    "legacy_projected": "PROJECTED",
    "receipt_bound": "RECEIPT_BOUND",
    "sealed_lineage": "SEALED_LINEAGE",
}
# Provisional until r2 open decision #2 seals which scope classes may promote;
# ``ci_matrix`` deliberately stays out while that decision is open.
PROMOTABLE_SCOPE_KINDS: frozenset[str] = frozenset(
    {"repository_default", "repository_product_tests"}
)


def _canonical_ids(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} contains an empty identity")
        item = value.strip()
        if len(item) > 512:
            raise ValueError(f"{label} identity exceeds the character bound")
        if item in normalized:
            raise ValueError(f"{label} contains a duplicate identity")
        normalized.append(item)
    return tuple(sorted(normalized))


class ScopeSubject(BaseModel):
    """Exact project/run/revision subject shared by every obligation set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    target_sha: str = Field(min_length=7, max_length=64)
    plan_sha256: str = Field(min_length=64, max_length=64)


def scope_subject_sha256(subject: ScopeSubject) -> str:
    """Return the canonical identity digest used by every obligation set."""

    return hashlib.sha256(
        json.dumps(
            subject.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


class ScopeClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)
    evidence_epoch: str = Field(min_length=1, max_length=256)
    subject: ScopeSubject
    kind: ScopeKind
    closure: ScopeClosure
    basis_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _normalize_refs(self) -> "ScopeClaim":
        if self.evidence_epoch != self.subject.run_id:
            raise ValueError("scope evidence epoch does not match the sealed subject run")
        refs = _canonical_ids(self.basis_refs, label="scope basis refs")
        object.__setattr__(self, "basis_refs", refs)
        return self


class TypedObligationSet(BaseModel):
    """One finite universe with successful, failed, missing, and extra identities."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: ObligationUnit
    basis_ref: str = Field(min_length=1, max_length=512)
    subject_sha256: str = Field(min_length=64, max_length=64)
    scope_id: str = Field(min_length=1, max_length=256)
    scope_revision: int = Field(ge=1)
    evidence_epoch: str = Field(min_length=1, max_length=256)
    applicability: Applicability = "required"
    required_ids: tuple[str, ...] = ()
    satisfied_ids: tuple[str, ...] = ()
    failed_ids: tuple[str, ...] = ()
    unexpected_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    execution_failed_ids: tuple[str, ...] = ()
    product_red_ids: tuple[str, ...] = ()
    integrity_conflict_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_identity_sets(self) -> "TypedObligationSet":
        required = _canonical_ids(self.required_ids, label="required ids")
        satisfied = _canonical_ids(self.satisfied_ids, label="satisfied ids")
        failed = _canonical_ids(self.failed_ids, label="failed ids")
        unexpected = _canonical_ids(self.unexpected_ids, label="unexpected ids")
        refs = _canonical_ids(self.evidence_refs, label="obligation evidence refs")
        execution_failed = _canonical_ids(self.execution_failed_ids, label="execution failed ids")
        product_red = _canonical_ids(self.product_red_ids, label="product red ids")
        integrity_conflict = _canonical_ids(
            self.integrity_conflict_ids, label="integrity conflict ids"
        )
        required_set = set(required)
        satisfied_set = set(satisfied)
        failed_set = set(failed)
        unexpected_set = set(unexpected)

        attributed: set[str] = set()
        for kind_ids in (execution_failed, product_red, integrity_conflict):
            kind_set = set(kind_ids)
            if not kind_set.issubset(failed_set):
                raise ValueError("a failure kind can only attribute a failed identity")
            if kind_set & attributed:
                raise ValueError("a failed identity cannot carry two failure kinds")
            attributed |= kind_set

        if self.applicability == "required" and not required:
            raise ValueError("a required obligation set must name at least one identity")
        if self.applicability != "required" and (required or satisfied or failed):
            raise ValueError("a non-required obligation set cannot claim obligation outcomes")
        if not satisfied_set.issubset(required_set):
            raise ValueError("satisfied ids must be a subset of required ids")
        if not failed_set.issubset(required_set):
            raise ValueError("failed ids must be a subset of required ids")
        if satisfied_set & failed_set:
            raise ValueError("an obligation cannot be both satisfied and failed")
        if unexpected_set & required_set:
            raise ValueError("unexpected ids must be outside the required identity universe")

        object.__setattr__(self, "required_ids", required)
        object.__setattr__(self, "satisfied_ids", satisfied)
        object.__setattr__(self, "failed_ids", failed)
        object.__setattr__(self, "unexpected_ids", unexpected)
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "execution_failed_ids", execution_failed)
        object.__setattr__(self, "product_red_ids", product_red)
        object.__setattr__(self, "integrity_conflict_ids", integrity_conflict)
        return self


class TestCounts(BaseModel):
    """MS-1 §9.1 counts, in the standard's own words.

    ``reported`` is the §9.1 name — ``passed + failed + errors + skipped`` by
    definition, so a tuple that does not add up is unconstructible.  The word
    ``executed`` is reserved to the legacy verdict's observation grain and does
    not appear on a certificate count; the §22 attainment view is the one
    place the two vocabularies meet, and it translates explicitly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # strict: a bool riding an int field is a type error, not a count of one.
    reported: int = Field(ge=0, strict=True)
    passed: int = Field(ge=0, strict=True)
    failed: int = Field(ge=0, strict=True)
    errors: int = Field(ge=0, strict=True)
    skipped: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _reconcile(self) -> "TestCounts":
        if self.reported != self.passed + self.failed + self.errors + self.skipped:
            raise ValueError("test outcome counts do not reconcile")
        return self

    @property
    def assessed(self) -> int:
        """§9.1: the tests that produced a verdict-bearing result."""

        return self.passed + self.failed + self.errors

    @property
    def red(self) -> int:
        """Failures and errors together — the §7.4 outcome test's input."""

        return self.failed + self.errors

    def __add__(self, other: "TestCounts") -> "TestCounts":
        """§9.1: certificate-level counts are the sums over bound receipts."""

        if not isinstance(other, TestCounts):
            return NotImplemented
        return TestCounts(
            reported=self.reported + other.reported,
            passed=self.passed + other.passed,
            failed=self.failed + other.failed,
            errors=self.errors + other.errors,
            skipped=self.skipped + other.skipped,
        )


class CertificateBlocker(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, max_length=128)
    affects: tuple[ProofAxis, ...]
    owner: Literal["agent", "harness", "project", "environment", "unknown"]
    reason: str = Field(min_length=1, max_length=2_000)
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_lists(self) -> "CertificateBlocker":
        if not self.affects:
            raise ValueError("a certificate blocker must affect at least one proof axis")
        if len(set(self.affects)) != len(self.affects):
            raise ValueError("a certificate blocker cannot repeat a proof axis")
        refs = _canonical_ids(self.evidence_refs, label="blocker evidence refs")
        object.__setattr__(self, "evidence_refs", refs)
        return self


class DiagnosticMetric(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, max_length=128)
    observations: dict[str, int | float | str]
    note: str = Field(min_length=1, max_length=2_000)
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_observations(self) -> "DiagnosticMetric":
        if not self.observations:
            raise ValueError("a diagnostic metric must contain an observation")
        if any(not str(key).strip() for key in self.observations):
            raise ValueError("a diagnostic metric contains an empty observation key")
        refs = _canonical_ids(self.evidence_refs, label="diagnostic evidence refs")
        object.__setattr__(self, "evidence_refs", refs)
        return self


def _product_red_bearing_failures(obligation: TypedObligationSet) -> tuple[str, ...]:
    """The failed identities that assert a red test outcome.

    A step that never dispatched and an identity conflict are failures about
    the run, not about the product, so they carry no claim over the counts.
    """

    excused = set(obligation.execution_failed_ids) | set(obligation.integrity_conflict_ids)
    return tuple(item for item in obligation.failed_ids if item not in excused)


class JavaCertificateInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = CERTIFICATE_SCHEMA_VERSION
    assurance_level: AssuranceLevel
    scope: ScopeClaim
    build_steps: TypedObligationSet
    build_units: TypedObligationSet
    test_steps: TypedObligationSet
    test_targets: TypedObligationSet
    evidence_items: TypedObligationSet
    test_results_authority: TestResultsAuthority
    test_counts: TestCounts | None = None
    documented_no_automated_tests: bool = False
    unsettled_jobs: int = Field(default=0, ge=0)
    terminal_receipts_unpersisted: int = Field(default=0, ge=0)
    blockers: tuple[CertificateBlocker, ...] = ()
    diagnostics: tuple[DiagnosticMetric, ...] = ()

    @model_validator(mode="after")
    def _validate_shared_identity(self) -> "JavaCertificateInput":
        if self.schema_version != CERTIFICATE_SCHEMA_VERSION:
            raise ValueError("unsupported Java certificate input schema")
        identity = (self.scope.scope_id, self.scope.revision, self.scope.evidence_epoch)
        for label, obligation in (
            ("build step", self.build_steps),
            ("build", self.build_units),
            ("test step", self.test_steps),
            ("test target", self.test_targets),
            ("evidence", self.evidence_items),
        ):
            observed = (
                obligation.scope_id,
                obligation.scope_revision,
                obligation.evidence_epoch,
            )
            if observed != identity:
                raise ValueError(f"{label} obligations do not share the sealed scope identity")

        expected_units: tuple[tuple[str, TypedObligationSet, set[str]], ...] = (
            ("build step", self.build_steps, {"build_plan_step"}),
            (
                "build unit",
                self.build_units,
                {"maven_reactor_module", "single_maven_project", "gradle_task"},
            ),
            ("test step", self.test_steps, {"test_plan_step"}),
            (
                "test target",
                self.test_targets,
                {"test_module", "single_test_project", "gradle_test_task"},
            ),
            ("evidence", self.evidence_items, {"evidence_binding"}),
        )
        for label, obligation, allowed_units in expected_units:
            if obligation.unit not in allowed_units:
                raise ValueError(f"{label} obligations use the wrong identity unit")
            if obligation.subject_sha256 != scope_subject_sha256(self.scope.subject):
                raise ValueError(f"{label} obligations do not share the sealed scope subject")

        if self.documented_no_automated_tests:
            if self.test_targets.applicability != "not_applicable":
                raise ValueError("documented no-tests requires not-applicable test targets")
            if self.test_steps.applicability != "not_applicable":
                raise ValueError("documented no-tests cannot carry required test steps")
            if self.test_counts is not None or self.test_results_authority != "unavailable":
                raise ValueError("documented no-tests cannot claim runtime test results")
        elif (
            self.test_steps.applicability == "not_applicable"
            or self.test_targets.applicability == "not_applicable"
        ):
            raise ValueError("test obligations are not applicable only with documented no-tests")

        if self.test_results_authority == "receipt_bound" and self.test_counts is None:
            raise ValueError("receipt-bound test results require reconciled counts")
        if self.test_results_authority == "unavailable" and self.test_counts is not None:
            raise ValueError("unavailable test results cannot carry counts")
        if self.test_results_authority == "receipt_bound" and self.test_counts is not None:
            counts_are_red = bool(self.test_counts.failed or self.test_counts.errors)
            targets_are_red = bool(_product_red_bearing_failures(self.test_targets))
            steps_are_red = bool(_product_red_bearing_failures(self.test_steps))
            if counts_are_red and not targets_are_red:
                raise ValueError("red receipt-bound counts require a failed test-target identity")
            if not counts_are_red and (targets_are_red or steps_are_red):
                raise ValueError(
                    "clean receipt-bound counts conflict with a failed test obligation"
                )
        return self


class CountPair(BaseModel):
    """An exact fraction as integers; percentages are presentation only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    numerator: int = Field(ge=0)
    denominator: int = Field(ge=1)

    @model_validator(mode="after")
    def _within_the_universe(self) -> "CountPair":
        if self.numerator > self.denominator:
            raise ValueError("a fraction over a required identity universe cannot exceed one")
        return self


class SatisfactionInterval(BaseModel):
    """The exact range of satisfaction over every completion of one structure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lower_numerator: int = Field(ge=0)
    upper_numerator: int = Field(ge=0)
    denominator: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered_within_the_universe(self) -> "SatisfactionInterval":
        if self.lower_numerator > self.upper_numerator:
            raise ValueError("the satisfaction interval lower endpoint exceeds its upper endpoint")
        if self.upper_numerator > self.denominator:
            raise ValueError("the satisfaction interval cannot exceed one")
        return self


class FailureAttribution(BaseModel):
    """r2 §18/3: the failed identities split by the kind of defeat they record.

    ``failed_ids`` alone cannot tell a runner that never ran from a product
    test that went red from an identity contradiction.  ``null`` on an
    obligation set means no attribution was recorded, which is never the same
    claim as an attribution of zero.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_failed: int = Field(ge=0)
    product_red: int = Field(ge=0)
    integrity_conflict: int = Field(ge=0)
    execution_failed_ids: tuple[str, ...] = ()
    product_red_ids: tuple[str, ...] = ()
    integrity_conflict_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _counts_restate_the_identities(self) -> "FailureAttribution":
        if (self.execution_failed, self.product_red, self.integrity_conflict) != (
            len(self.execution_failed_ids),
            len(self.product_red_ids),
            len(self.integrity_conflict_ids),
        ):
            raise ValueError("a failure-kind count disagrees with its identity list")
        if not (self.execution_failed or self.product_red or self.integrity_conflict):
            raise ValueError("an unattributed failure set is null, never a zero record")
        return self


class ObligationMetrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: ObligationUnit
    basis_ref: str
    required_set_sha256: str
    applicability: Applicability
    status: ObligationStatus
    required: int
    closed: int
    satisfied: int
    failed: int
    missing: int
    unexpected: int
    closure_fraction: str | None
    success_fraction: str | None
    closure_pair: CountPair | None = None
    satisfaction_pair: CountPair | None = None
    interval: SatisfactionInterval | None = None
    required_ids: tuple[str, ...]
    satisfied_ids: tuple[str, ...]
    failed_ids: tuple[str, ...]
    missing_ids: tuple[str, ...]
    unexpected_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    failure_attribution: FailureAttribution | None = None

    @model_validator(mode="after")
    def _validate_single_measurement_object(self) -> "ObligationMetrics":
        attribution = self.failure_attribution
        if attribution is not None and (
            attribution.execution_failed + attribution.product_red + attribution.integrity_conflict
            > self.failed
        ):
            raise ValueError("the attributed failure kinds exceed the failed identity count")
        measured = self.closure_fraction is not None
        if (self.success_fraction is not None) is not measured:
            raise ValueError("the closure and success fractions disagree about measurement")
        closure_pair = self.closure_pair
        satisfaction_pair = self.satisfaction_pair
        interval = self.interval
        if closure_pair is None or satisfaction_pair is None or interval is None:
            if not (closure_pair is None and satisfaction_pair is None and interval is None):
                raise ValueError("the structured obligation measures are partially populated")
            if measured:
                raise ValueError("a measured obligation set must carry structured measures")
            return self
        if not measured:
            raise ValueError("an unmeasured obligation set cannot carry structured measures")
        if self.required != self.satisfied + self.failed + self.missing:
            raise ValueError("the obligation counts do not partition the required universe")
        if self.closed != self.satisfied + self.failed:
            raise ValueError("the closed count does not equal the terminal identities")
        if self.closure_fraction != f"{self.closed}/{self.required}":
            raise ValueError("the closure fraction disagrees with the obligation counts")
        if self.success_fraction != f"{self.satisfied}/{self.required}":
            raise ValueError("the success fraction disagrees with the obligation counts")
        if (closure_pair.numerator, closure_pair.denominator) != (self.closed, self.required):
            raise ValueError("the closure pair disagrees with the obligation counts")
        if (satisfaction_pair.numerator, satisfaction_pair.denominator) != (
            self.satisfied,
            self.required,
        ):
            raise ValueError("the satisfaction pair disagrees with the obligation counts")
        if (
            interval.lower_numerator,
            interval.upper_numerator,
            interval.denominator,
        ) != (self.satisfied, self.satisfied + self.missing, self.required):
            raise ValueError("the satisfaction interval disagrees with the obligation counts")
        # The interval collapse is the status: a capped upper endpoint is a
        # definite failure, and a degenerate interval is exact completion.
        if (interval.upper_numerator < interval.denominator) is not (self.failed > 0):
            raise ValueError("the interval collapse disagrees with the failed identity count")
        degenerate = interval.lower_numerator == interval.upper_numerator == interval.denominator
        if degenerate is not (self.status == "complete"):
            raise ValueError("the degenerate interval disagrees with the obligation status")
        return self


class IntegrityAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: IntegrityStatus
    evidence: ObligationMetrics
    unsettled_jobs: int
    terminal_receipts_unpersisted: int


class CertificateFlags(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    build_axis_success: bool
    test_execution_closed: bool
    authoritative_test_clean: bool
    green_proof_verified: bool


class AxisTruths(BaseModel):
    """Per-axis public truth.  ``NOT_APPLICABLE`` is applicability, not truth."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: AxisTruth
    build: AxisTruth
    test_execution: AxisTruth
    test_outcome: AxisTruth
    integrity: AxisTruth


class TypedReasonCode(BaseModel):
    """A reason code carrying the defeater kind that bounds what it can do."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, max_length=128)
    defeater_type: DefeaterType


class JavaSuccessCertificate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = CERTIFICATE_SCHEMA_VERSION
    project_id: str
    run_id: str
    target_sha: str
    plan_sha256: str
    assurance_level: AssuranceLevel
    assurance: Assurance
    scope: ScopeClaim
    proof_status: ProofStatus
    result: CertificateResult
    result_class: str
    overall_truth: TruthValue
    axis_truths: AxisTruths
    typed_reason_codes: tuple[TypedReasonCode, ...]
    promotion_eligible: bool
    build_status: BuildStatus
    build_steps: ObligationMetrics
    build_units: ObligationMetrics
    test_execution_status: TestExecutionStatus
    test_steps: ObligationMetrics
    test_targets: ObligationMetrics
    test_outcome_status: TestOutcomeStatus
    test_counts: TestCounts | None
    test_results_authority: TestResultsAuthority
    integrity: IntegrityAssessment
    flags: CertificateFlags
    blockers: tuple[CertificateBlocker, ...]
    diagnostics: tuple[DiagnosticMetric, ...]

    @model_validator(mode="after")
    def _validate_truth_surface(self) -> "JavaSuccessCertificate":
        if self.result_class != self.result:
            raise ValueError("the v2 result class must alias the canonical result")
        if self.overall_truth != OVERALL_TRUTH_BY_RESULT[self.result]:
            raise ValueError("the overall truth disagrees with the result class")
        if self.assurance != ASSURANCE_BY_LEVEL[self.assurance_level]:
            raise ValueError("the assurance alias disagrees with the assurance level")
        codes = tuple(item.code for item in self.typed_reason_codes)
        if len(set(codes)) != len(codes):
            raise ValueError("a typed reason code is repeated")
        if tuple(sorted(codes)) != codes:
            raise ValueError("typed reason codes are not ordered by code")
        # SAG-MS-1 C4: every FAIL is witnessed by an intact rebutting defeater.
        # A certificate that fails without one is a bug, not a renderable state.
        if self.overall_truth == "FAIL" and not any(
            item.defeater_type == "rebutting" for item in self.typed_reason_codes
        ):
            raise ValueError("a failing certificate must carry a rebutting reason code")
        # The dual C4 clause: ``unverifiable`` is the authority-undercut species
        # of UNKNOWN, so the defeater that removed the authority must be named.
        if self.result == "unverifiable" and not any(
            item.defeater_type == "undercutting" for item in self.typed_reason_codes
        ):
            raise ValueError("an unverifiable certificate must carry an undercutting reason code")
        return self

    def model_dump_json(self, **kwargs: Any) -> str:
        indent = kwargs.pop("indent", None)
        if kwargs:
            return super().model_dump_json(indent=indent, **kwargs)
        separators = None if indent is not None else (",", ":")
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=separators,
            ensure_ascii=True,
            indent=indent,
        )


def _obligation_metrics(obligation: TypedObligationSet, subject: ScopeSubject) -> ObligationMetrics:
    required = set(obligation.required_ids)
    observed = set(obligation.satisfied_ids) | set(obligation.failed_ids)
    missing_ids = tuple(item for item in obligation.required_ids if item not in observed)
    closure_pair: CountPair | None = None
    satisfaction_pair: CountPair | None = None
    interval: SatisfactionInterval | None = None
    if obligation.applicability == "not_applicable":
        status: ObligationStatus = "not_applicable"
        closure_fraction = None
        success_fraction = None
    elif obligation.applicability == "unknown":
        status = "unavailable"
        closure_fraction = None
        success_fraction = None
    else:
        if obligation.failed_ids:
            status = "failed"
        elif missing_ids:
            status = "incomplete"
        else:
            status = "complete"
        closure_fraction = f"{len(observed)}/{len(required)}"
        success_fraction = f"{len(obligation.satisfied_ids)}/{len(required)}"
        closure_pair = CountPair(numerator=len(observed), denominator=len(required))
        satisfaction_pair = CountPair(
            numerator=len(obligation.satisfied_ids), denominator=len(required)
        )
        interval = SatisfactionInterval(
            lower_numerator=len(obligation.satisfied_ids),
            upper_numerator=len(obligation.satisfied_ids) + len(missing_ids),
            denominator=len(required),
        )
    identity_payload = {
        "applicability": obligation.applicability,
        "basis_ref": obligation.basis_ref,
        "evidence_epoch": obligation.evidence_epoch,
        "required_ids": list(obligation.required_ids),
        "scope_id": obligation.scope_id,
        "scope_revision": obligation.scope_revision,
        "subject": subject.model_dump(mode="json"),
        "subject_sha256": obligation.subject_sha256,
        "unit": obligation.unit,
    }
    required_set_sha256 = hashlib.sha256(
        json.dumps(
            identity_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    attribution: FailureAttribution | None = None
    if (
        obligation.execution_failed_ids
        or obligation.product_red_ids
        or obligation.integrity_conflict_ids
    ):
        attribution = FailureAttribution(
            execution_failed=len(obligation.execution_failed_ids),
            product_red=len(obligation.product_red_ids),
            integrity_conflict=len(obligation.integrity_conflict_ids),
            execution_failed_ids=obligation.execution_failed_ids,
            product_red_ids=obligation.product_red_ids,
            integrity_conflict_ids=obligation.integrity_conflict_ids,
        )
    return ObligationMetrics(
        unit=obligation.unit,
        basis_ref=obligation.basis_ref,
        required_set_sha256=required_set_sha256,
        applicability=obligation.applicability,
        status=status,
        required=len(obligation.required_ids),
        closed=len(observed),
        satisfied=len(obligation.satisfied_ids),
        failed=len(obligation.failed_ids),
        missing=len(missing_ids),
        unexpected=len(obligation.unexpected_ids),
        closure_fraction=closure_fraction,
        success_fraction=success_fraction,
        closure_pair=closure_pair,
        satisfaction_pair=satisfaction_pair,
        interval=interval,
        required_ids=obligation.required_ids,
        satisfied_ids=obligation.satisfied_ids,
        failed_ids=obligation.failed_ids,
        missing_ids=missing_ids,
        unexpected_ids=obligation.unexpected_ids,
        evidence_refs=obligation.evidence_refs,
        failure_attribution=attribution,
    )


def _blocked(payload: JavaCertificateInput, axis: ProofAxis) -> bool:
    return any(axis in blocker.affects for blocker in payload.blockers)


def _blocked_axes(payload: JavaCertificateInput) -> frozenset[ProofAxis]:
    return frozenset(axis for blocker in payload.blockers for axis in blocker.affects)


def _execution_failures(*obligations: ObligationMetrics) -> int:
    return sum(
        item.failure_attribution.execution_failed
        for item in obligations
        if item.failure_attribution is not None
    )


def _integrity_conflicts(*obligations: ObligationMetrics) -> int:
    return sum(
        item.failure_attribution.integrity_conflict
        for item in obligations
        if item.failure_attribution is not None
    )


def _scope_axis_truth(scope: ScopeClaim, *, blocked: bool) -> AxisTruth:
    # A blocker on the scope axis undercuts the sealing proof itself, so the
    # axis cannot keep publishing PASS while the certificate reads unverifiable.
    if blocked:
        return "UNKNOWN"
    if scope.closure == "closed" and scope.kind != "unknown":
        return "PASS"
    return "UNKNOWN"


def _test_outcome_axis_truth(
    status: TestOutcomeStatus,
    *,
    test_results_authority: TestResultsAuthority,
    blocked: bool,
) -> AxisTruth:
    # SAG-MS-1 Theorem 6.2 (ii): unbound observations grade nothing.  An
    # unbound clean run cannot read PASS and an unbound red run cannot read
    # FAIL, whatever the diagnostic counts say.
    if status == "not_applicable":
        return "NOT_APPLICABLE"
    if blocked or test_results_authority != "receipt_bound":
        return "UNKNOWN"
    return TEST_OUTCOME_AXIS_TRUTH[status]


def _typed_reason_codes(
    *,
    build_status: BuildStatus,
    test_execution_status: TestExecutionStatus,
    test_outcome_status: TestOutcomeStatus,
    test_outcome_truth: AxisTruth,
    test_results_authority: TestResultsAuthority,
    integrity_status: IntegrityStatus,
    scope_truth: AxisTruth,
    documented_no_automated_tests: bool,
    blocked_axes: frozenset[ProofAxis],
    obligations: tuple[ObligationMetrics, ...],
) -> tuple[TypedReasonCode, ...]:
    """Derive the typed defeater summary from the sealed certificate surface."""

    codes: dict[str, DefeaterType] = {}
    if build_status == "failed":
        codes["AUTHORITATIVE_BUILD_FAILURE"] = "rebutting"
    if test_execution_status == "failed":
        codes["TEST_EXECUTION_FAILURE"] = "rebutting"
    if test_outcome_truth == "FAIL":
        codes["TEST_OUTCOME_RED"] = "rebutting"
    if any(item.applicability == "required" and item.missing > 0 for item in obligations):
        codes["MISSING_REQUIRED_IDENTITY"] = "incompleteness"
    if test_outcome_status == "empty":
        codes["EMPTY_VERDICT_BEARING_RESULT"] = "vacuity"
    if _integrity_conflicts(*obligations):
        codes["IDENTITY_CONFLICT"] = "undercutting"
    if test_results_authority == "diagnostic":
        codes["DIAGNOSTIC_ONLY_OBSERVATION"] = "undercutting"
    if test_results_authority == "unavailable" and not documented_no_automated_tests:
        codes["TEST_RESULTS_UNAVAILABLE"] = "undercutting"
    if integrity_status in {"degraded", "unavailable"}:
        codes["LINEAGE_UNAVAILABLE"] = "undercutting"
    if scope_truth == "UNKNOWN" or any(item.applicability == "unknown" for item in obligations):
        codes["UNSEALED_DENOMINATOR"] = "undercutting"
    for axis in blocked_axes:
        codes[BLOCKED_AXIS_REASON_CODES[axis]] = "undercutting"
    if documented_no_automated_tests:
        codes["DOCUMENTED_NO_AUTOMATED_TESTS"] = "applicability"
    return tuple(TypedReasonCode(code=code, defeater_type=codes[code]) for code in sorted(codes))


def evaluate_java_success_certificate(payload: JavaCertificateInput) -> JavaSuccessCertificate:
    """Derive one deterministic certificate from sealed, typed observations."""

    # ``model_copy(update=...)`` is intentionally not a validation API in
    # Pydantic. Reconstruct at this public boundary so a poisoned nested model
    # cannot bypass the identity-set invariants.
    payload = JavaCertificateInput.model_validate(payload.model_dump(mode="python"))
    subject = payload.scope.subject
    build_steps = _obligation_metrics(payload.build_steps, subject)
    build_units = _obligation_metrics(payload.build_units, subject)
    test_steps = _obligation_metrics(payload.test_steps, subject)
    test_targets = _obligation_metrics(payload.test_targets, subject)
    evidence = _obligation_metrics(payload.evidence_items, subject)

    obligations = (build_steps, build_units, test_steps, test_targets, evidence)
    if (
        evidence.status in {"unavailable", "failed"}
        or _blocked(payload, "integrity")
        or _integrity_conflicts(*obligations)
    ):
        integrity_status: Literal["complete", "degraded", "unavailable"] = "unavailable"
    elif (
        evidence.status == "complete"
        and payload.unsettled_jobs == 0
        and payload.terminal_receipts_unpersisted == 0
    ):
        integrity_status = "complete"
    else:
        integrity_status = "degraded"
    integrity = IntegrityAssessment(
        status=integrity_status,
        evidence=evidence,
        unsettled_jobs=payload.unsettled_jobs,
        terminal_receipts_unpersisted=payload.terminal_receipts_unpersisted,
    )

    if _blocked(payload, "build") or integrity_status == "unavailable":
        build_status: BuildStatus = "unverifiable"
    elif build_steps.status == "complete" and build_units.status == "complete":
        build_status = "success"
    elif build_steps.status == "failed" or build_units.status == "failed":
        build_status = "failed"
    elif build_steps.status == "unavailable" or build_units.status == "unavailable":
        build_status = "unverifiable"
    else:
        build_status = "incomplete"

    if payload.documented_no_automated_tests:
        test_execution_status: TestExecutionStatus = "not_applicable"
    elif (
        _execution_failures(test_steps, test_targets)
        and integrity_status != "unavailable"
        and not _blocked(payload, "test_execution")
    ):
        test_execution_status = "failed"
    elif (
        test_steps.applicability == "required"
        and test_targets.applicability == "required"
        and test_steps.missing == 0
        and test_targets.missing == 0
        and payload.test_results_authority == "receipt_bound"
        and payload.test_counts is not None
        and payload.test_counts.passed + payload.test_counts.failed + payload.test_counts.errors > 0
        and integrity_status == "complete"
        and not _blocked(payload, "test_execution")
    ):
        test_execution_status = "complete"
    elif (
        test_steps.status == "incomplete"
        or test_targets.status == "incomplete"
        or test_steps.missing > 0
        or test_targets.missing > 0
        or (
            payload.test_counts is not None
            and payload.test_counts.passed + payload.test_counts.failed + payload.test_counts.errors
            == 0
        )
    ):
        test_execution_status = "incomplete"
    elif (
        test_steps.status == "unavailable"
        or test_targets.status == "unavailable"
        or payload.test_results_authority in {"diagnostic", "unavailable"}
        or integrity_status == "unavailable"
        or _blocked(payload, "test_execution")
    ):
        test_execution_status = "unverifiable"
    else:
        test_execution_status = "incomplete"

    if payload.documented_no_automated_tests:
        test_outcome_status: TestOutcomeStatus = "not_applicable"
    elif payload.test_counts is None:
        test_outcome_status = "unknown"
    elif payload.test_counts.failed or payload.test_counts.errors:
        test_outcome_status = "red"
    elif payload.test_counts.passed == 0:
        test_outcome_status = "empty"
    else:
        test_outcome_status = "clean"

    blocked_axes = _blocked_axes(payload)
    scope_truth = _scope_axis_truth(payload.scope, blocked=_blocked(payload, "scope"))
    test_outcome_truth = _test_outcome_axis_truth(
        test_outcome_status,
        test_results_authority=payload.test_results_authority,
        blocked=_blocked(payload, "test_outcome"),
    )
    build_success = build_status == "success"
    test_execution_complete = test_execution_status == "complete"
    test_obligations_successful = (
        test_steps.status == "complete" and test_targets.status == "complete"
    )
    test_clean = (
        payload.test_results_authority == "receipt_bound"
        and test_outcome_status == "clean"
        and not _blocked(payload, "test_outcome")
    )
    scope_closed = payload.scope.closure == "closed" and not _blocked(payload, "scope")
    repository_scope = payload.scope.kind in {"repository_default", "ci_matrix"}
    repository_product_test_scope = payload.scope.kind == "repository_product_tests"
    repository_green = (
        scope_closed
        and repository_scope
        and build_success
        and test_execution_complete
        and test_obligations_successful
        and test_clean
        and integrity_status == "complete"
    )
    scoped_green = (
        scope_closed
        and not repository_scope
        and not repository_product_test_scope
        and payload.scope.kind != "unknown"
        and build_success
        and test_execution_complete
        and test_obligations_successful
        and test_clean
        and integrity_status == "complete"
    )
    repository_product_test_green = (
        scope_closed
        and repository_product_test_scope
        and build_success
        and test_execution_complete
        and test_obligations_successful
        and test_clean
        and integrity_status == "complete"
    )

    positive_proof: ProofStatus = (
        "verified" if payload.assurance_level == "sealed_lineage" else "projected"
    )
    scope_unverifiable = (
        payload.scope.kind == "unknown"
        or payload.scope.closure == "unknown"
        or _blocked(payload, "scope")
    )

    if integrity_status == "unavailable" or build_status == "unverifiable":
        result: CertificateResult = "unverifiable"
        proof_status: ProofStatus = "unverifiable"
    elif scope_unverifiable:
        result = "unverifiable"
        proof_status = "unverifiable"
    elif build_status == "failed":
        result = "build_failed"
        proof_status = positive_proof
    elif repository_green:
        result = "repository_green"
        proof_status = positive_proof
    elif repository_product_test_green:
        result = "repository_product_test_green"
        proof_status = positive_proof
    elif scoped_green:
        result = "scoped_green"
        proof_status = positive_proof
    elif test_execution_status == "failed":
        result = "test_execution_failed"
        proof_status = positive_proof
    elif _blocked(payload, "test_outcome"):
        result = "unverifiable"
        proof_status = "unverifiable"
    elif test_execution_complete and test_outcome_truth == "FAIL" and test_targets.failed > 0:
        # SAG-MS-1 Definition 7.2 / Lemma 7.3: an intact FAIL on one axis is the
        # overall truth whatever an unrelated axis still leaves open.
        result = "test_red"
        proof_status = positive_proof
    elif (
        build_success
        and test_execution_status == "not_applicable"
        and scope_closed
        and integrity_status == "complete"
    ):
        result = "build_only"
        proof_status = positive_proof
    elif test_execution_status == "unverifiable":
        result = "unverifiable"
        proof_status = "unverifiable"
    else:
        result = "incomplete"
        proof_status = "partial"

    overall_truth = OVERALL_TRUTH_BY_RESULT[result]
    assurance = ASSURANCE_BY_LEVEL[payload.assurance_level]
    typed_reason_codes = _typed_reason_codes(
        build_status=build_status,
        test_execution_status=test_execution_status,
        test_outcome_status=test_outcome_status,
        test_outcome_truth=test_outcome_truth,
        test_results_authority=payload.test_results_authority,
        integrity_status=integrity_status,
        scope_truth=scope_truth,
        documented_no_automated_tests=payload.documented_no_automated_tests,
        blocked_axes=blocked_axes,
        obligations=obligations,
    )
    # Provisional promotion policy: r2 open decision #2 has not sealed which
    # scope classes may promote, so only the two repository scopes qualify.
    promotion_eligible = (
        overall_truth == "PASS"
        and assurance == "SEALED_LINEAGE"
        and payload.scope.kind in PROMOTABLE_SCOPE_KINDS
    )

    return JavaSuccessCertificate(
        project_id=subject.project_id,
        run_id=subject.run_id,
        target_sha=subject.target_sha,
        plan_sha256=subject.plan_sha256,
        assurance_level=payload.assurance_level,
        assurance=assurance,
        scope=payload.scope,
        proof_status=proof_status,
        result=result,
        result_class=result,
        overall_truth=overall_truth,
        axis_truths=AxisTruths(
            scope=scope_truth,
            build=BUILD_AXIS_TRUTH[build_status],
            test_execution=TEST_EXECUTION_AXIS_TRUTH[test_execution_status],
            test_outcome=test_outcome_truth,
            integrity=INTEGRITY_AXIS_TRUTH[integrity_status],
        ),
        typed_reason_codes=typed_reason_codes,
        promotion_eligible=promotion_eligible,
        build_status=build_status,
        build_steps=build_steps,
        build_units=build_units,
        test_execution_status=test_execution_status,
        test_steps=test_steps,
        test_targets=test_targets,
        test_outcome_status=test_outcome_status,
        test_counts=payload.test_counts,
        test_results_authority=payload.test_results_authority,
        integrity=integrity,
        flags=CertificateFlags(
            build_axis_success=build_success,
            test_execution_closed=test_execution_complete,
            authoritative_test_clean=test_clean,
            green_proof_verified=(
                proof_status == "verified"
                and result
                in {
                    "repository_green",
                    "repository_product_test_green",
                    "scoped_green",
                }
            ),
        ),
        blockers=payload.blockers,
        diagnostics=payload.diagnostics,
    )


def evaluate_java_success_manifest(payload: Any) -> dict[str, Any]:
    """Validate and evaluate one canonical collection of certificate inputs."""

    if not isinstance(payload, dict) or set(payload) != {"schema_version", "inputs"}:
        raise ValueError("certificate manifest fields are not canonical")
    if payload.get("schema_version") != CERTIFICATE_SCHEMA_VERSION:
        raise ValueError("certificate manifest schema version is not supported")
    raw_inputs = payload.get("inputs")
    if not isinstance(raw_inputs, list) or not raw_inputs:
        raise ValueError("certificate manifest inputs must be a non-empty list")

    certificates = [
        evaluate_java_success_certificate(JavaCertificateInput.model_validate(item))
        for item in raw_inputs
    ]
    project_ids = [certificate.project_id for certificate in certificates]
    if len(project_ids) != len(set(project_ids)):
        raise ValueError("certificate manifest contains duplicate project ids")
    return {
        "schema_version": CERTIFICATE_SCHEMA_VERSION,
        "certificates": [certificate.model_dump(mode="json") for certificate in certificates],
    }


# ---------------------------------------------------------------------------
# The receipt tier (plan r2 T5; MS-1 §18 step 7, C7 adapter-only inputs).
#
# In live mode every certificate input is produced by an adapter from the
# evidence layer; hand-authored inputs are admissible only at PROJECTED
# assurance.  This tier is the first live piece of that adapter: it turns a
# Gradle dispatch's own receipt into the certificate's §9.1 counts, so no
# caller ever has an occasion to type a count in.  The counts come from the
# receipt's suite TOTALS (`receipt_suite_totals`), never from the bounded
# identity sample beside them (principle P-A): kafka's measured run seals
# 2,048 identity rows over 27,219 executions, and a certificate built from
# the sample would state a fifteenth of the run and call it the whole.
# ---------------------------------------------------------------------------

# §9.2's one verdict-bearing value. A count that cannot claim it is not a
# weaker certificate input; it is not a certificate input.
TEST_RESULTS_AUTHORITY_RECEIPT_BOUND: TestResultsAuthority = "receipt_bound"
# The receipt fields the binding predicate (MS-1 §12) reads. Identity of the
# receipt, of the run, of the checkout and of the domain, plus the contract the
# facade froze BEFORE the dispatch — which is what makes the numbers the
# outcome of a SEALED test step rather than of some command that happened.
CHAIN_IDENTITY_FIELDS = ("receipt_id", "run_id", "target_sha", "domain_id")
CHAIN_CONTRACT_FIELDS = ("contract_id", "contract_hash", "execution_binding")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ReceiptBoundTestResults:
    """Counts plus the authority and disclosure that decide what they may grade.

    `receipt_ids` is the evidence ref §9.1 requires beside every per-receipt
    tuple: a certificate states which dispatches it counted, so a reader can go
    back to the receipts and recount.
    """

    counts: TestCounts
    test_results_authority: TestResultsAuthority
    receipt_ids: tuple[str, ...]
    counts_complete: bool
    disclosed_bounds: tuple[str, ...] = ()


def _chain_intact(
    receipt: Mapping[str, Any],
    *,
    run_id: Optional[str],
    target_sha: Optional[str],
) -> Optional[str]:
    """The receipt's own binding fields, or ``None`` when one of them is broken.

    Returns the receipt id, because a caller that has the chain has the
    evidence ref too and nothing else needs to re-derive it.
    """

    if not isinstance(receipt, Mapping):
        return None
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        return None
    identity: dict[str, str] = {}
    for field in CHAIN_IDENTITY_FIELDS:
        value = receipt.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
        identity[field] = value.strip()
    # The contract binding is validated as one complete tuple or none at all,
    # so requiring the tuple here is requiring the seal, not three coincidences.
    for field in CHAIN_CONTRACT_FIELDS:
        value = receipt.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
    for expected, field in ((run_id, "run_id"), (target_sha, "target_sha")):
        wanted = str(expected or "").strip()
        if wanted and identity[field] != wanted:
            return None
    # P-B: the counts are only ever a reading of bytes the delta claims, so a
    # receipt whose claim set is malformed backs nothing. An EMPTY claim set is
    # the same refusal for a different reason — totals summed from no claimed
    # report would be another dispatch's tests wearing this receipt's identity.
    delta = receipt.get("report_delta")
    if not isinstance(delta, Mapping):
        return None
    claims: set[tuple[str, str]] = set()
    claimed = 0
    for bucket in ("new", "changed", "cached"):
        entries = delta.get(bucket)
        if entries is None:
            continue
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, Mapping):
                return None
            path = str(entry.get("path") or "").strip()
            digest = str(entry.get("sha256") or "").strip().lower()
            if not path or _SHA256_RE.fullmatch(digest) is None:
                return None
            claims.add((path, digest))
            claimed += 1
    if not claims or len(claims) != claimed:
        return None
    return identity["receipt_id"]


def _counts_of(totals: SuiteExecutionTotals) -> TestCounts:
    return TestCounts(
        reported=totals.tests,
        passed=totals.passed,
        failed=totals.failed,
        errors=totals.errors,
        skipped=totals.skipped,
    )


def receipt_bound_test_counts(
    receipt: Mapping[str, Any],
    *,
    run_id: Optional[str] = None,
    target_sha: Optional[str] = None,
) -> Optional[ReceiptBoundTestResults]:
    """One receipt's certificate counts, or ``None`` when it may not state any.

    ``None`` is the honest answer for every refusal this makes — an unbound
    count is a diagnostic (§9.2) and the certificate's `counts_receipt_bound`
    is simply false. It is never a zero: a zero would be a measured claim that
    the dispatch ran nothing.
    """

    receipt_id = _chain_intact(receipt, run_id=run_id, target_sha=target_sha)
    if receipt_id is None:
        return None
    totals = receipt_suite_totals(receipt)
    if totals is None:
        return None
    return ReceiptBoundTestResults(
        counts=_counts_of(totals),
        test_results_authority=TEST_RESULTS_AUTHORITY_RECEIPT_BOUND,
        receipt_ids=(receipt_id,),
        counts_complete=totals.complete_claims,
        disclosed_bounds=totals.disclosed_bounds,
    )


def certificate_test_counts(
    receipts: Sequence[Mapping[str, Any]],
    *,
    run_id: Optional[str] = None,
    target_sha: Optional[str] = None,
) -> Optional[ReceiptBoundTestResults]:
    """§9.1: the certificate's counts are the sum over every bound receipt.

    A receipt that cannot bind contributes nothing and is not an error — a run
    may hold receipts for other domains, other targets and other tools. What it
    may never do is contribute a count without contributing its binding.
    """

    folded: Optional[TestCounts] = None
    receipt_ids: list[str] = []
    complete = True
    bounds: set[str] = set()
    for receipt in receipts or ():
        bound = receipt_bound_test_counts(receipt, run_id=run_id, target_sha=target_sha)
        if bound is None:
            continue
        folded = bound.counts if folded is None else folded + bound.counts
        receipt_ids.extend(bound.receipt_ids)
        complete = complete and bound.counts_complete
        bounds.update(bound.disclosed_bounds)
    if folded is None:
        return None
    return ReceiptBoundTestResults(
        counts=folded,
        test_results_authority=TEST_RESULTS_AUTHORITY_RECEIPT_BOUND,
        receipt_ids=tuple(receipt_ids),
        counts_complete=complete,
        disclosed_bounds=tuple(sorted(bounds)),
    )
