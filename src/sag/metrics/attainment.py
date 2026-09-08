"""The attainment algebra: one run's own numbers against one CI cell.

The certificate says whether a run's numbers are its own.  The target record
says what the project's CI proved on the same revision.  This module is the
only place the two meet, and it meets them under three rules:

* **The honesty gate comes first.** A comparison is admissible only when the
  certificate's authority is intact, the compared counts are receipt-bound,
  and both subjects identify the same repository and exact revision.
  An inadmissible comparison yields ``invalid`` and no fraction at all —
  quoting CI's public numbers must never read as attainment.
* **The denominator is external.** Every fraction is measured against the
  matched cell's universe, never against the run's own plan, so shrinking the
  sealed scope can only lower the fraction. A cell without modules supports
  only a build conclusion, disclosed as ``build_form="conclusion"``; it
  supplies no module denominator and cannot establish scope attainment.
* **The bottleneck is the score.** ``alpha`` is the smaller of the build and
  test fractions, never their average: a reactor half built is not repaired by
  a test suite fully run.

Fractions are integer numerator/denominator pairs (:class:`Pair`); nothing here
stores a float, and every comparison between fractions is a cross-multiplication
of integers.  The module is pure: no filesystem, process, or network authority.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sag.agent.java_success_certificates import JavaSuccessCertificate
from sag.metrics.module_keys import module_keys
from sag.metrics.parity import LifecycleParity, parity_from_texts
from sag.metrics.target_record import (
    CellGrade,
    CellTarget,
    ModulesBasis,
    TargetRecord,
    _canonical_ids,
)

AttainmentVerdict = Literal["invalid", "not_met", "partial", "met", "exceeded"]
# How ``clean`` was decided: by comparing test identities, or, when either side
# lacks them, by comparing counts.  The count form is weaker and says so.
CleanForm = Literal["ids", "counts"]
BuildForm = Literal["modules", "conclusion"]

# The knowledge ordering the monotonicity property is stated over.  It is an
# ordering of claims, not a score: nothing may be summed over it.
ATTAINMENT_ORDER: dict[AttainmentVerdict, int] = {
    "invalid": 0,
    "not_met": 1,
    "partial": 2,
    "met": 3,
    "exceeded": 4,
}

# Gate codes: defeaters of the comparison itself.
CERTIFICATE_AUTHORITY_UNAVAILABLE = "CERTIFICATE_AUTHORITY_UNAVAILABLE"
COUNTS_NOT_RECEIPT_BOUND = "COUNTS_NOT_RECEIPT_BOUND"
TARGET_WITHOUT_COUNTS = "TARGET_WITHOUT_COUNTS"
TARGET_TEST_UNIVERSE_EMPTY = "TARGET_TEST_UNIVERSE_EMPTY"
TARGET_BUILD_UNIVERSE_UNAVAILABLE = "TARGET_BUILD_UNIVERSE_UNAVAILABLE"
COMPARISON_SUBJECT_UNAVAILABLE = "COMPARISON_SUBJECT_UNAVAILABLE"
COMPARISON_SUBJECT_MISMATCH = "COMPARISON_SUBJECT_MISMATCH"
# Finding codes: what the admitted comparison found.
NEW_RED_BEYOND_TARGET = "NEW_RED_BEYOND_TARGET"
CLEAN_BY_COUNTS_ONLY = "CLEAN_BY_COUNTS_ONLY"
MODULES_BELOW_TARGET = "MODULES_BELOW_TARGET"
BUILD_AXIS_NOT_SUCCESSFUL = "BUILD_AXIS_NOT_SUCCESSFUL"
EXECUTION_BELOW_TARGET = "EXECUTION_BELOW_TARGET"


class Pair(BaseModel):
    """An exact fraction as two integers; percentages are presentation only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    numerator: int = Field(ge=0)
    denominator: int = Field(gt=0)


def pair_leq(left: Pair, right: Pair) -> bool:
    """Whether ``left`` is at most ``right``, by cross-multiplication."""

    return left.numerator * right.denominator <= right.numerator * left.denominator


def smaller_pair(left: Pair, right: Pair) -> Pair:
    """Return the smaller of two fractions; ties return the left one."""

    return left if pair_leq(left, right) else right


def _attainment_fractions(
    *, admissible: bool, executed: int, target: int, matched: int, modules: int
) -> tuple[Pair | None, Pair | None, Pair | None]:
    """Project the echoed counts once for both evaluation and result readers."""
    if not admissible:
        return None, None, None
    tests = Pair(numerator=min(executed, target), denominator=target) if target else None
    build = Pair(numerator=matched, denominator=modules) if modules else None
    # Preserve the test denominator when the two fractions tie.
    alpha = smaller_pair(tests, build) if tests is not None and build is not None else None
    return alpha, tests, build


def _attainment_verdict(
    *,
    admissible: bool,
    clean: bool,
    built: bool,
    executed: int,
    target: int,
    modules: int,
    extra_modules: bool,
) -> AttainmentVerdict:
    """The claim threshold shared by evaluation and validated result reads."""
    if not admissible:
        return "invalid"
    if not clean:
        return "not_met"
    if not (built and target > 0 and modules > 0 and executed >= target):
        return "partial"
    return "exceeded" if executed > target or extra_modules else "met"


class CertificateView(BaseModel):
    """Everything the attainment algebra is allowed to read about one run.

    The view is the only door: a number that did not come through it cannot be
    compared to a target, which is what makes the honesty gate enforceable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    authority_ok: bool
    counts_receipt_bound: bool
    build_ok: bool
    executed_count: int = Field(ge=0)
    executed_ids: tuple[str, ...] = ()
    red_count: int = Field(ge=0)
    red_ids: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    repo: str | None = Field(default=None, max_length=256)
    target_sha: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _validate_identity_sets(self) -> "CertificateView":
        executed = _canonical_ids(self.executed_ids, label="executed ids")
        red = _canonical_ids(self.red_ids, label="red ids")
        modules = _canonical_ids(module_keys(self.modules), label="modules")

        if executed and self.executed_count != len(executed):
            raise ValueError("executed count does not match the executed identities")
        if red and self.red_count != len(red):
            raise ValueError("red count does not match the red identities")
        if red and self.red_count > self.executed_count:
            raise ValueError("more tests are red than were executed")
        if executed and not set(red).issubset(set(executed)):
            raise ValueError("red ids must be a subset of executed ids")

        object.__setattr__(self, "executed_ids", executed)
        object.__setattr__(self, "red_ids", red)
        object.__setattr__(self, "modules", modules)
        object.__setattr__(self, "commands", tuple(c.strip() for c in self.commands if c.strip()))
        object.__setattr__(self, "repo", self.repo.strip() or None if self.repo else None)
        object.__setattr__(
            self, "target_sha", self.target_sha.strip() or None if self.target_sha else None
        )
        return self


def view_from_certificate(
    certificate: JavaSuccessCertificate, *, repo: str | None = None
) -> CertificateView:
    """Adapt a finished certificate into the only view attainment may compare.

    Counts survive the adaptation only when the certificate calls them
    receipt-bound; a diagnostic count is dropped to zero here rather than
    carried forward as a comparable number.

    The certificate names a project, not an owner/repository. The caller must
    supply that repository from the run's subject evidence; it must not copy
    it from the target to make the comparison pass. An absent repository
    leaves the view inadmissible.
    """

    counts = certificate.test_counts
    counts_receipt_bound = (
        certificate.test_results_authority == "receipt_bound" and counts is not None
    )
    return CertificateView(
        repo=repo,
        target_sha=certificate.target_sha,
        authority_ok=(
            certificate.integrity.status == "complete" and certificate.scope.closure == "closed"
        ),
        counts_receipt_bound=counts_receipt_bound,
        build_ok=certificate.build_status == "success",
        # §22 is the one door where the certificate's §9.1 vocabulary
        # (`reported`) meets the target algebra's (`executed`); the rename is
        # deliberate and happens nowhere else.
        executed_count=counts.reported if counts_receipt_bound and counts is not None else 0,
        red_count=(
            counts.failed + counts.errors if counts_receipt_bound and counts is not None else 0
        ),
        # The certificate carries obligation identities, not test identities, so
        # a view built from one always compares red by count.
        modules=certificate.build_units.satisfied_ids,
    )


class AttainmentResult(BaseModel):
    """One run judged against one cell, with every compared number echoed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: AttainmentVerdict
    cell_id: str = Field(min_length=1, max_length=128)
    cell_grade: CellGrade
    valid: bool
    target_usable: bool
    clean: bool
    clean_form: CleanForm
    built: bool
    alpha: Pair | None = None
    alpha_test: Pair | None = None
    alpha_build: Pair | None = None
    executed_observed: int = Field(ge=0)
    executed_target: int = Field(ge=0)
    red_observed: int = Field(ge=0)
    red_target: int = Field(ge=0)
    modules_matched: int = Field(ge=0)
    modules_target: int = Field(ge=0)
    missing_module_ids: tuple[str, ...] = ()
    build_form: BuildForm = "conclusion"
    modules_basis: ModulesBasis | None = None
    unmatched_observed_module_ids: tuple[str, ...] = ()
    lifecycle_parity: LifecycleParity | None = None
    unexpected_red_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_claim(self) -> "AttainmentResult":
        if self.modules_matched > self.modules_target:
            raise ValueError("more target modules matched than the target names")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("reason codes contain a duplicate code")
        expected_usable = not (self.cell_grade == "A" and self.executed_target == 0)
        if self.target_usable != expected_usable:
            raise ValueError("target usability contradicts its grade and test universe")
        authority_defeaters = {
            CERTIFICATE_AUTHORITY_UNAVAILABLE,
            COUNTS_NOT_RECEIPT_BOUND,
            COMPARISON_SUBJECT_UNAVAILABLE,
            COMPARISON_SUBJECT_MISMATCH,
        }
        if self.valid and authority_defeaters.intersection(self.reason_codes):
            raise ValueError("validity contradicts an authority defeater")
        if self.build_form == "conclusion" and self.alpha_build is not None:
            raise ValueError("a build conclusion supplies no module fraction")
        if (self.build_form == "modules") != (self.modules_target > 0):
            raise ValueError("build form contradicts the echoed module universe")
        if (self.modules_basis is not None) != (self.modules_target > 0):
            raise ValueError("module provenance contradicts the echoed module universe")
        for name in ("missing_module_ids", "unmatched_observed_module_ids", "unexpected_red_ids"):
            object.__setattr__(self, name, _canonical_ids(getattr(self, name), label=name))
        if len(self.missing_module_ids) != self.modules_target - self.modules_matched:
            raise ValueError("missing module identities contradict the matched count")
        if set(self.missing_module_ids).intersection(self.unmatched_observed_module_ids):
            raise ValueError("a missing target module cannot also be an extra observed module")
        if self.build_form == "conclusion" and self.unmatched_observed_module_ids:
            raise ValueError("a build conclusion cannot identify extra target modules")
        if self.built and (
            self.modules_matched < self.modules_target
            or BUILD_AXIS_NOT_SUCCESSFUL in self.reason_codes
        ):
            raise ValueError("build completion contradicts the echoed build evidence")
        expected_clean = (
            self.red_observed <= self.red_target
            if self.clean_form == "counts"
            else not self.unexpected_red_ids
        )
        if self.clean != expected_clean:
            raise ValueError("cleanliness contradicts the echoed red evidence")
        if self.clean_form == "counts" and self.unexpected_red_ids:
            raise ValueError("count comparison cannot identify unexpected red tests")
        if len(self.unexpected_red_ids) > self.red_observed:
            raise ValueError("unexpected red identities exceed the observed red count")
        admissible = self.valid and self.target_usable
        expected_fractions = _attainment_fractions(
            admissible=admissible,
            executed=self.executed_observed,
            target=self.executed_target,
            matched=self.modules_matched,
            modules=self.modules_target,
        )
        if (self.alpha, self.alpha_test, self.alpha_build) != expected_fractions:
            raise ValueError("attainment fractions contradict the echoed counts or validity")
        expected_verdict = _attainment_verdict(
            admissible=admissible,
            clean=self.clean,
            built=self.built,
            executed=self.executed_observed,
            target=self.executed_target,
            modules=self.modules_target,
            extra_modules=bool(self.unmatched_observed_module_ids),
        )
        if self.verdict != expected_verdict:
            raise ValueError("attainment verdict contradicts the echoed comparison")
        return self


def _matched_cell(target: TargetRecord) -> CellTarget:
    if target.matched_cell is None:
        raise ValueError("attainment needs a matched cell; the target record names none")
    for cell in target.cells:
        if cell.cell_id == target.matched_cell:
            return cell
    raise ValueError("the matched cell is not among the harvested cells")


def evaluate_attainment(view: CertificateView, target: TargetRecord) -> AttainmentResult:
    """Judge one run's view against the target record's matched cell.

    ``clean``, ``built`` and the echoed numbers describe the comparison that
    was attempted, whether or not it was admissible; ``verdict`` and the
    fractions describe only comparisons that were.
    """

    cell = _matched_cell(target)
    gate_reasons: list[str] = []
    finding_reasons: list[str] = []

    valid = view.authority_ok and view.counts_receipt_bound
    if not view.authority_ok:
        gate_reasons.append(CERTIFICATE_AUTHORITY_UNAVAILABLE)
    if not view.counts_receipt_bound:
        gate_reasons.append(COUNTS_NOT_RECEIPT_BOUND)
    if view.repo is None or view.target_sha is None:
        valid = False
        gate_reasons.append(COMPARISON_SUBJECT_UNAVAILABLE)
    elif view.repo != target.repo or view.target_sha != target.sha:
        valid = False
        gate_reasons.append(COMPARISON_SUBJECT_MISMATCH)

    # A grade-A cell claims to know its test identities, so an empty universe
    # is a broken harvest, not a project without tests.  A grade-B cell knows
    # only its conclusion; there the empty universe is disclosed, not fatal.
    target_usable = not (cell.grade == "A" and cell.executed_count == 0)
    if not target_usable:
        gate_reasons.append(TARGET_WITHOUT_COUNTS)
    elif cell.executed_count == 0:
        gate_reasons.append(TARGET_TEST_UNIVERSE_EMPTY)

    target_red = set(cell.red_ids) | set(cell.flaky_ids)
    # The id form needs a run that can name what it ran: red identities without
    # an executed universe are not a set the run can vouch for.
    view_red_known = bool(view.red_ids) or view.red_count == 0
    # Both halves of the cell's union must be nameable: a cell that counted a
    # flaky test it could not store would otherwise read as having none, and the
    # run's rerun of that same test would be reported as new red.
    cell_red_known = (bool(cell.red_ids) or cell.red_count == 0) and (
        bool(cell.flaky_ids) or cell.flaky_count == 0
    )
    unexpected_red: tuple[str, ...] = ()
    if view_red_known and cell_red_known and view.executed_ids:
        clean_form: CleanForm = "ids"
        unexpected_red = tuple(sorted(set(view.red_ids) - target_red))
        clean = not unexpected_red
    else:
        clean_form = "counts"
        clean = view.red_count <= cell.red_count
        finding_reasons.append(CLEAN_BY_COUNTS_ONLY)
    if not clean:
        finding_reasons.append(NEW_RED_BEYOND_TARGET)

    observed_modules = set(view.modules)
    target_modules = set(cell.modules)
    missing_modules: tuple[str, ...] = ()
    unmatched_observed: tuple[str, ...] = ()
    build_form: BuildForm = "modules" if target_modules else "conclusion"
    if target_modules:
        missing_modules = tuple(sorted(target_modules - observed_modules))
        unmatched_observed = tuple(sorted(observed_modules - target_modules))
        built = view.build_ok and not missing_modules
        if missing_modules:
            finding_reasons.append(MODULES_BELOW_TARGET)
    else:
        # A successful build is still disclosed, but it cannot replace a
        # missing external universe with a synthetic 1/1 scope score.
        built = view.build_ok
        finding_reasons.append(TARGET_BUILD_UNIVERSE_UNAVAILABLE)
    if not view.build_ok:
        finding_reasons.append(BUILD_AXIS_NOT_SUCCESSFUL)

    if view.executed_count < cell.executed_count:
        finding_reasons.append(EXECUTION_BELOW_TARGET)

    admissible = valid and target_usable
    alpha, alpha_test, alpha_build = _attainment_fractions(
        admissible=admissible,
        executed=view.executed_count,
        target=cell.executed_count,
        matched=len(observed_modules & target_modules),
        modules=len(target_modules),
    )
    verdict = _attainment_verdict(
        admissible=admissible,
        clean=clean,
        built=built,
        executed=view.executed_count,
        target=cell.executed_count,
        modules=len(target_modules),
        extra_modules=bool(unmatched_observed),
    )

    reason_codes = tuple(gate_reasons) if not admissible else tuple(gate_reasons + finding_reasons)
    lifecycle_parity = (
        parity_from_texts(cell.command, view.commands) if cell.command and view.commands else None
    )
    return AttainmentResult(
        verdict=verdict,
        cell_id=cell.cell_id,
        cell_grade=cell.grade,
        valid=valid,
        target_usable=target_usable,
        clean=clean,
        clean_form=clean_form,
        built=built,
        alpha=alpha,
        alpha_test=alpha_test,
        alpha_build=alpha_build,
        executed_observed=view.executed_count,
        executed_target=cell.executed_count,
        red_observed=view.red_count,
        red_target=cell.red_count,
        modules_matched=len(observed_modules & target_modules),
        modules_target=len(target_modules),
        missing_module_ids=missing_modules,
        build_form=build_form,
        modules_basis=cell.modules_basis,
        unmatched_observed_module_ids=unmatched_observed,
        lifecycle_parity=lifecycle_parity,
        unexpected_red_ids=unexpected_red,
        reason_codes=reason_codes,
    )
