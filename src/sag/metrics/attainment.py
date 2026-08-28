"""The attainment algebra: one run's own numbers against one CI cell.

The certificate says whether a run's numbers are its own.  The target record
says what the project's CI proved on the same revision.  This module is the
only place the two meet, and it meets them under three rules:

* **The honesty gate comes first.** A comparison is admissible only when the
  certificate's authority is intact and the compared counts are receipt-bound.
  An inadmissible comparison yields ``invalid`` and no fraction at all —
  quoting CI's public numbers must never read as attainment.
* **The denominator is external.** Every fraction is measured against the
  matched cell's universe, never against the run's own plan, so shrinking the
  sealed scope can only lower the fraction.
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
from sag.metrics.target_record import CellGrade, CellTarget, TargetRecord, _canonical_ids

AttainmentVerdict = Literal["invalid", "not_met", "partial", "met", "exceeded"]
# How ``clean`` was decided: by comparing test identities, or, when either side
# lacks them, by comparing counts.  The count form is weaker and says so.
CleanForm = Literal["ids", "counts"]

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

    @model_validator(mode="after")
    def _validate_identity_sets(self) -> "CertificateView":
        executed = _canonical_ids(self.executed_ids, label="executed ids")
        red = _canonical_ids(self.red_ids, label="red ids")
        modules = _canonical_ids(self.modules, label="modules")

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
        return self


def view_from_certificate(certificate: JavaSuccessCertificate) -> CertificateView:
    """Adapt a finished certificate into the only view attainment may compare.

    Counts survive the adaptation only when the certificate calls them
    receipt-bound; a diagnostic count is dropped to zero here rather than
    carried forward as a comparable number.
    """

    counts = certificate.test_counts
    counts_receipt_bound = (
        certificate.test_results_authority == "receipt_bound" and counts is not None
    )
    return CertificateView(
        authority_ok=(
            certificate.integrity.status == "complete" and certificate.scope.closure == "closed"
        ),
        counts_receipt_bound=counts_receipt_bound,
        build_ok=certificate.build_status == "success",
        executed_count=counts.executed if counts_receipt_bound and counts is not None else 0,
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
    unexpected_red_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_claim(self) -> "AttainmentResult":
        if self.modules_matched > self.modules_target:
            raise ValueError("more target modules matched than the target names")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("reason codes contain a duplicate code")
        # An inadmissible comparison states no fraction at all: the whole point
        # of the gate is that unproven numbers produce no score to quote.
        inadmissible = self.verdict == "invalid"
        if inadmissible and not (
            self.alpha is None and self.alpha_test is None and self.alpha_build is None
        ):
            raise ValueError("an invalid attainment cannot carry a fraction")
        if not inadmissible and (self.alpha is None or self.alpha_build is None):
            raise ValueError("an admitted attainment must carry its fractions")
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
    cell_red_known = bool(cell.red_ids) or bool(cell.flaky_ids) or cell.red_count == 0
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
    if target_modules:
        missing_modules = tuple(sorted(target_modules - observed_modules))
        built = not missing_modules
        alpha_build: Pair | None = Pair(
            numerator=len(observed_modules & target_modules),
            denominator=len(target_modules),
        )
        if not built:
            finding_reasons.append(MODULES_BELOW_TARGET)
    else:
        # The cell names no modules, so the build axis of the certificate is
        # the only build evidence there is; the fraction is 1 or 0, never 1/0.
        built = view.build_ok
        alpha_build = (
            Pair(numerator=1, denominator=1) if built else Pair(numerator=0, denominator=1)
        )
        if not built:
            finding_reasons.append(BUILD_AXIS_NOT_SUCCESSFUL)

    alpha_test: Pair | None = None
    if cell.executed_count > 0:
        alpha_test = Pair(
            numerator=min(view.executed_count, cell.executed_count),
            denominator=cell.executed_count,
        )
    alpha: Pair | None = alpha_build
    if alpha_test is not None and alpha_build is not None:
        # A tie reports the test fraction: its denominator is the cell's own
        # test universe, the number a reader needs to see.
        alpha = smaller_pair(alpha_test, alpha_build)

    covered = view.executed_count >= cell.executed_count
    if not covered:
        finding_reasons.append(EXECUTION_BELOW_TARGET)

    admissible = valid and target_usable
    if not admissible:
        verdict: AttainmentVerdict = "invalid"
        alpha = None
        alpha_test = None
        alpha_build = None
    elif not clean:
        verdict = "not_met"
    elif built and covered:
        # A strict superset of the target's modules only counts as beating the
        # target when the target named modules at all.
        beats_modules = bool(target_modules) and observed_modules > target_modules
        verdict = (
            "exceeded" if view.executed_count > cell.executed_count or beats_modules else "met"
        )
    else:
        verdict = "partial"

    reason_codes = tuple(gate_reasons) if not admissible else tuple(gate_reasons + finding_reasons)
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
        unexpected_red_ids=unexpected_red,
        reason_codes=reason_codes,
    )
