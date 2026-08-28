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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
TestExecutionStatus = Literal["complete", "incomplete", "unverifiable", "not_applicable"]
TestOutcomeStatus = Literal["clean", "red", "empty", "unknown", "not_applicable"]
AssuranceLevel = Literal["legacy_projected", "receipt_bound", "sealed_lineage"]
ProofStatus = Literal["verified", "projected", "partial", "unverifiable"]
CertificateResult = Literal[
    "repository_green",
    "repository_product_test_green",
    "scoped_green",
    "build_failed",
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

    @model_validator(mode="after")
    def _validate_identity_sets(self) -> "TypedObligationSet":
        required = _canonical_ids(self.required_ids, label="required ids")
        satisfied = _canonical_ids(self.satisfied_ids, label="satisfied ids")
        failed = _canonical_ids(self.failed_ids, label="failed ids")
        unexpected = _canonical_ids(self.unexpected_ids, label="unexpected ids")
        refs = _canonical_ids(self.evidence_refs, label="obligation evidence refs")
        required_set = set(required)
        satisfied_set = set(satisfied)
        failed_set = set(failed)
        unexpected_set = set(unexpected)

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
        return self


class TestCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    executed: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    errors: int = Field(ge=0)
    skipped: int = Field(ge=0)

    @model_validator(mode="after")
    def _reconcile(self) -> "TestCounts":
        if self.executed != self.passed + self.failed + self.errors + self.skipped:
            raise ValueError("test outcome counts do not reconcile")
        return self


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
            targets_are_red = bool(self.test_targets.failed_ids)
            steps_are_red = bool(self.test_steps.failed_ids)
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

    @model_validator(mode="after")
    def _validate_single_measurement_object(self) -> "ObligationMetrics":
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
    )


def _blocked(payload: JavaCertificateInput, axis: ProofAxis) -> bool:
    return any(axis in blocker.affects for blocker in payload.blockers)


def _scope_axis_truth(scope: ScopeClaim) -> AxisTruth:
    if scope.closure == "closed" and scope.kind != "unknown":
        return "PASS"
    return "UNKNOWN"


def _typed_reason_codes(
    *,
    build_status: BuildStatus,
    test_outcome_status: TestOutcomeStatus,
    test_results_authority: TestResultsAuthority,
    integrity_status: IntegrityStatus,
    scope_truth: AxisTruth,
    documented_no_automated_tests: bool,
    obligations: tuple[ObligationMetrics, ...],
) -> tuple[TypedReasonCode, ...]:
    """Derive the typed defeater summary from the sealed certificate surface."""

    codes: dict[str, DefeaterType] = {}
    if build_status == "failed":
        codes["AUTHORITATIVE_BUILD_FAILURE"] = "rebutting"
    if test_outcome_status == "red":
        codes["TEST_OUTCOME_RED"] = "rebutting"
    if any(item.applicability == "required" and item.missing > 0 for item in obligations):
        codes["MISSING_REQUIRED_IDENTITY"] = "incompleteness"
    if test_outcome_status == "empty":
        codes["EMPTY_VERDICT_BEARING_RESULT"] = "vacuity"
    if test_results_authority == "diagnostic":
        codes["DIAGNOSTIC_ONLY_OBSERVATION"] = "undercutting"
    if integrity_status in {"degraded", "unavailable"}:
        codes["LINEAGE_UNAVAILABLE"] = "undercutting"
    if scope_truth == "UNKNOWN":
        codes["UNSEALED_DENOMINATOR"] = "undercutting"
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

    if (
        evidence.status == "complete"
        and payload.unsettled_jobs == 0
        and payload.terminal_receipts_unpersisted == 0
        and not _blocked(payload, "integrity")
    ):
        integrity_status: Literal["complete", "degraded", "unavailable"] = "complete"
    elif evidence.status in {"unavailable", "failed"} or _blocked(payload, "integrity"):
        integrity_status = "unavailable"
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
    elif _blocked(payload, "test_outcome"):
        result = "unverifiable"
        proof_status = "unverifiable"
    elif (
        build_success
        and test_execution_complete
        and test_outcome_status == "red"
        and test_targets.failed > 0
    ):
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

    scope_truth = _scope_axis_truth(payload.scope)
    overall_truth = OVERALL_TRUTH_BY_RESULT[result]
    assurance = ASSURANCE_BY_LEVEL[payload.assurance_level]
    typed_reason_codes = _typed_reason_codes(
        build_status=build_status,
        test_outcome_status=test_outcome_status,
        test_results_authority=payload.test_results_authority,
        integrity_status=integrity_status,
        scope_truth=scope_truth,
        documented_no_automated_tests=payload.documented_no_automated_tests,
        obligations=(build_steps, build_units, test_steps, test_targets, evidence),
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
            test_outcome=TEST_OUTCOME_AXIS_TRUTH[test_outcome_status],
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
