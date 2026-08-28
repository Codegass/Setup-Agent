"""The attainment algebra's four properties, stated as metamorphic tests."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from sag.agent.java_success_certificates import (
    JavaCertificateInput,
    evaluate_java_success_certificate,
)
from sag.metrics.attainment import (
    ATTAINMENT_ORDER,
    BUILD_AXIS_NOT_SUCCESSFUL,
    CERTIFICATE_AUTHORITY_UNAVAILABLE,
    CLEAN_BY_COUNTS_ONLY,
    COUNTS_NOT_RECEIPT_BOUND,
    EXECUTION_BELOW_TARGET,
    MODULES_BELOW_TARGET,
    NEW_RED_BEYOND_TARGET,
    TARGET_TEST_UNIVERSE_EMPTY,
    TARGET_WITHOUT_COUNTS,
    AttainmentResult,
    CertificateView,
    Pair,
    evaluate_attainment,
    pair_leq,
    smaller_pair,
    view_from_certificate,
)
from sag.metrics.target_record import CellTarget, TargetRecord

CERTIFICATE_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "java_success_certificates"
    / "d2r6-five-project-inputs.json"
)


def _cell(**overrides) -> CellTarget:
    payload = {
        "cell_id": "build (17, ubuntu-latest)",
        "build": "ok",
        "executed_count": 4,
        "red_count": 0,
        "grade": "A",
    }
    payload.update(overrides)
    return CellTarget(**payload)


def _record(cell: CellTarget, *, other: CellTarget | None = None) -> TargetRecord:
    cells = (cell,) if other is None else (cell, other)
    return TargetRecord(
        repo="apache/tomcat-jakartaee-migration",
        sha="50c811bffa4da6e48f5661e6a5a7763ffccdad46",
        harvested_at="2026-08-27T12:04:00Z",
        cells=cells,
        matched_cell=cell.cell_id,
    )


def _view(**overrides) -> CertificateView:
    payload = {
        "authority_ok": True,
        "counts_receipt_bound": True,
        "build_ok": True,
        "executed_count": 4,
        "red_count": 0,
    }
    payload.update(overrides)
    return CertificateView(**payload)


def _judge(view: CertificateView, cell: CellTarget) -> AttainmentResult:
    return evaluate_attainment(view, _record(cell))


class TestPairArithmetic:
    def test_fractions_compare_by_cross_multiplication(self):
        assert pair_leq(Pair(numerator=51, denominator=52), Pair(numerator=1, denominator=1))
        assert not pair_leq(Pair(numerator=2, denominator=3), Pair(numerator=1, denominator=2))

    def test_equal_fractions_compare_equal_across_denominators(self):
        assert pair_leq(Pair(numerator=1, denominator=2), Pair(numerator=50, denominator=100))
        assert pair_leq(Pair(numerator=50, denominator=100), Pair(numerator=1, denominator=2))

    def test_the_smaller_fraction_is_the_bottleneck_not_an_average(self):
        half = Pair(numerator=1, denominator=2)
        whole = Pair(numerator=1, denominator=1)
        assert smaller_pair(half, whole) == half
        assert smaller_pair(whole, half) == half

    def test_a_zero_denominator_is_rejected(self):
        with pytest.raises(ValidationError):
            Pair(numerator=0, denominator=0)


class TestCertificateView:
    def test_identities_are_stripped_sorted_and_counted(self):
        view = _view(executed_count=2, executed_ids=(" b#two ", "a#one"))
        assert view.executed_ids == ("a#one", "b#two")

    def test_an_id_list_must_match_its_count(self):
        with pytest.raises(ValidationError, match="does not match the executed identities"):
            _view(executed_count=3, executed_ids=("a#one",))

    def test_red_ids_must_lie_inside_the_executed_universe(self):
        with pytest.raises(ValidationError, match="subset of executed ids"):
            _view(
                executed_count=1,
                executed_ids=("a#one",),
                red_count=1,
                red_ids=("z#nine",),
            )


class TestCertificateAdapter:
    def _tomcat_certificate(self):
        payload = json.loads(CERTIFICATE_FIXTURE.read_text(encoding="utf-8"))
        for item in payload["inputs"]:
            if item["scope"]["subject"]["project_id"] == "tomcat-jakartaee-migration":
                return evaluate_java_success_certificate(JavaCertificateInput.model_validate(item))
        raise AssertionError("the d2r6 fixture no longer carries the tomcat input")

    def test_a_sealed_receipt_bound_certificate_adapts_to_its_numbers(self):
        view = view_from_certificate(self._tomcat_certificate())
        assert view.authority_ok is True
        assert view.counts_receipt_bound is True
        assert view.build_ok is True
        assert view.executed_count == 52
        assert view.red_count == 0
        assert view.modules == (".",)

    def test_diagnostic_counts_never_become_comparable_numbers(self):
        certificate = self._tomcat_certificate().model_copy(
            update={"test_results_authority": "diagnostic"}
        )
        view = view_from_certificate(certificate)
        assert view.counts_receipt_bound is False
        assert view.executed_count == 0
        assert view.red_count == 0

    def test_a_partial_scope_withdraws_authority(self):
        certificate = self._tomcat_certificate()
        certificate = certificate.model_copy(
            update={"scope": certificate.scope.model_copy(update={"closure": "partial"})}
        )
        assert view_from_certificate(certificate).authority_ok is False

    def test_degraded_integrity_withdraws_authority(self):
        certificate = self._tomcat_certificate()
        certificate = certificate.model_copy(
            update={"integrity": certificate.integrity.model_copy(update={"status": "degraded"})}
        )
        assert view_from_certificate(certificate).authority_ok is False


class TestHonestyGate:
    """Property 2: numbers that are not provably the run's own claim nothing."""

    @pytest.mark.parametrize(
        ("authority_ok", "counts_receipt_bound", "expected_code"),
        [
            (False, True, CERTIFICATE_AUTHORITY_UNAVAILABLE),
            (True, False, COUNTS_NOT_RECEIPT_BOUND),
        ],
    )
    def test_perfect_numbers_without_authority_are_invalid(
        self, authority_ok, counts_receipt_bound, expected_code
    ):
        cell = _cell(executed_count=4, red_count=0, modules=("core",))
        perfect = _view(
            authority_ok=authority_ok,
            counts_receipt_bound=counts_receipt_bound,
            executed_count=400,
            modules=("core", "extra"),
        )
        result = _judge(perfect, cell)
        assert result.verdict == "invalid"
        assert expected_code in result.reason_codes
        assert result.alpha is None
        assert result.alpha_test is None
        assert result.alpha_build is None

    def test_the_same_numbers_with_authority_intact_are_met(self):
        cell = _cell(executed_count=4, red_count=0, modules=("core",))
        result = _judge(_view(executed_count=4, modules=("core",)), cell)
        assert result.verdict == "met"

    def test_both_defeaters_are_both_disclosed(self):
        result = _judge(_view(authority_ok=False, counts_receipt_bound=False), _cell())
        assert result.verdict == "invalid"
        assert set(result.reason_codes) == {
            CERTIFICATE_AUTHORITY_UNAVAILABLE,
            COUNTS_NOT_RECEIPT_BOUND,
        }

    def test_no_ungated_view_ever_reads_as_met(self):
        cell = _cell(executed_count=4, red_count=0)
        for authority_ok in (False, True):
            for counts_receipt_bound in (False, True):
                if authority_ok and counts_receipt_bound:
                    continue
                result = _judge(
                    _view(
                        authority_ok=authority_ok,
                        counts_receipt_bound=counts_receipt_bound,
                        executed_count=99,
                    ),
                    cell,
                )
                assert result.verdict == "invalid"


class TestMonotonicity:
    """Properties 1 and 3: the denominator is external, and more never hurts."""

    def test_more_executions_never_lower_the_verdict_or_the_fraction(self):
        cell = _cell(executed_count=52, red_count=0, modules=("core",))
        previous_rank = -1
        previous_alpha = Pair(numerator=0, denominator=1)
        for executed in range(0, 60):
            result = _judge(_view(executed_count=executed, modules=("core",)), cell)
            assert result.alpha is not None
            assert ATTAINMENT_ORDER[result.verdict] >= previous_rank
            assert pair_leq(previous_alpha, result.alpha)
            previous_rank = ATTAINMENT_ORDER[result.verdict]
            previous_alpha = result.alpha

    def test_more_modules_never_lower_the_verdict_or_the_fraction(self):
        cell = _cell(executed_count=4, red_count=0, modules=("core", "io", "net"))
        previous_rank = -1
        previous_alpha = Pair(numerator=0, denominator=1)
        for count in range(0, 4):
            built = ("core", "io", "net")[:count]
            result = _judge(_view(executed_count=4, modules=built), cell)
            assert result.alpha is not None
            assert ATTAINMENT_ORDER[result.verdict] >= previous_rank
            assert pair_leq(previous_alpha, result.alpha)
            previous_rank = ATTAINMENT_ORDER[result.verdict]
            previous_alpha = result.alpha

    def test_the_denominator_is_the_cell_whatever_the_run_planned(self):
        cell = _cell(executed_count=52, red_count=0)
        for executed in (0, 1, 26, 52, 5_000):
            result = _judge(_view(executed_count=executed), cell)
            assert result.alpha_test is not None
            assert result.alpha_test.denominator == 52
            assert result.executed_target == 52

    def test_a_shrunken_scope_lowers_the_fraction_it_cannot_lower_the_bar(self):
        cell = _cell(executed_count=52, red_count=0)
        full = _judge(_view(executed_count=52), cell)
        shrunk = _judge(_view(executed_count=4), cell)
        assert full.alpha == Pair(numerator=52, denominator=52)
        assert shrunk.alpha == Pair(numerator=4, denominator=52)
        assert shrunk.verdict == "partial"
        assert EXECUTION_BELOW_TARGET in shrunk.reason_codes

    def test_alpha_is_the_bottleneck_and_never_an_average(self):
        cell = _cell(executed_count=4, red_count=0, modules=("core", "io", "net", "web"))
        result = _judge(_view(executed_count=4, modules=("core",)), cell)
        assert result.alpha_test == Pair(numerator=4, denominator=4)
        assert result.alpha_build == Pair(numerator=1, denominator=4)
        assert result.alpha == Pair(numerator=1, denominator=4)
        assert result.verdict == "partial"
        assert MODULES_BELOW_TARGET in result.reason_codes


class TestCleanliness:
    """Property 3's other half: new red is a project fact, discovered not caused."""

    def _identified_cell(self, **overrides) -> CellTarget:
        payload = {
            "executed_count": 4,
            "executed_ids": ("t#1", "t#2", "t#3", "t#4"),
            "red_count": 1,
            "red_ids": ("t#3",),
            "flaky_ids": ("t#4",),
        }
        payload.update(overrides)
        return _cell(**payload)

    def _identified_view(self, red_ids: tuple[str, ...]) -> CertificateView:
        return _view(
            executed_count=4,
            executed_ids=("t#1", "t#2", "t#3", "t#4"),
            red_count=len(red_ids),
            red_ids=red_ids,
        )

    def test_red_the_target_already_had_stays_met(self):
        result = _judge(self._identified_view(("t#3",)), self._identified_cell())
        assert result.clean_form == "ids"
        assert result.clean is True
        assert result.verdict == "met"

    def test_red_the_target_recorded_as_flaky_stays_met(self):
        result = _judge(self._identified_view(("t#4",)), self._identified_cell())
        assert result.clean is True
        assert result.verdict == "met"

    def test_red_outside_the_union_is_not_met(self):
        result = _judge(self._identified_view(("t#1",)), self._identified_cell())
        assert result.verdict == "not_met"
        assert result.unexpected_red_ids == ("t#1",)
        assert NEW_RED_BEYOND_TARGET in result.reason_codes

    def test_one_new_red_beside_an_expected_one_is_still_not_met(self):
        result = _judge(self._identified_view(("t#1", "t#3")), self._identified_cell())
        assert result.verdict == "not_met"
        assert result.unexpected_red_ids == ("t#1",)

    def test_not_met_outranks_a_full_execution_count(self):
        result = _judge(self._identified_view(("t#1", "t#2")), self._identified_cell())
        assert result.executed_observed == result.executed_target
        assert result.verdict == "not_met"

    def test_without_identities_cleanliness_falls_back_to_counts(self):
        cell = _cell(executed_count=4, red_count=1, red_ids=("t#3",), executed_ids=(), grade="B")
        result = _judge(_view(executed_count=4, red_count=1), cell)
        assert result.clean_form == "counts"
        assert result.clean is True
        assert CLEAN_BY_COUNTS_ONLY in result.reason_codes

    def test_the_count_form_still_catches_more_red_than_the_cell_had(self):
        cell = _cell(executed_count=4, red_count=1, red_ids=("t#3",), grade="B")
        result = _judge(_view(executed_count=4, red_count=2), cell)
        assert result.clean is False
        assert result.verdict == "not_met"


class TestExceededStrictness:
    """Equal is met; only strictly more is exceeded."""

    def test_equal_counts_and_equal_modules_are_met_not_exceeded(self):
        cell = _cell(executed_count=52, red_count=0, modules=("core", "io"))
        result = _judge(_view(executed_count=52, modules=("core", "io")), cell)
        assert result.verdict == "met"
        assert result.alpha == Pair(numerator=52, denominator=52)

    def test_one_more_test_is_exceeded(self):
        cell = _cell(executed_count=52, red_count=0, modules=("core",))
        result = _judge(_view(executed_count=53, modules=("core",)), cell)
        assert result.verdict == "exceeded"
        assert result.alpha == Pair(numerator=52, denominator=52)

    def test_one_more_module_is_exceeded(self):
        cell = _cell(executed_count=52, red_count=0, modules=("core",))
        result = _judge(_view(executed_count=52, modules=("core", "extra")), cell)
        assert result.verdict == "exceeded"

    def test_a_target_without_modules_is_not_exceeded_by_naming_some(self):
        cell = _cell(executed_count=52, red_count=0, modules=())
        result = _judge(_view(executed_count=52, modules=("core", "extra")), cell)
        assert result.verdict == "met"
        assert result.alpha_build == Pair(numerator=1, denominator=1)

    def test_an_unmeasured_test_universe_is_never_strictly_beaten(self):
        """A grade-B zero is unmeasured, not measured at zero, so no run beats it."""

        cell = _cell(executed_count=0, red_count=0, grade="B")
        result = _judge(_view(executed_count=5), cell)
        assert result.verdict == "met"
        assert result.alpha_test is None
        assert TARGET_TEST_UNIVERSE_EMPTY in result.reason_codes

    def test_extra_modules_are_exceeded_even_below_the_execution_target(self):
        """Spec 4.3 puts the strict-superset arm ahead of the coverage arm."""

        cell = _cell(executed_count=10, red_count=0, modules=("core",))
        result = _judge(_view(executed_count=5, modules=("core", "extra")), cell)
        assert result.verdict == "exceeded"
        # The shortfall is not swallowed by the verdict: alpha and the reason
        # code still carry it.
        assert result.alpha == Pair(numerator=5, denominator=10)
        assert EXECUTION_BELOW_TARGET in result.reason_codes

    def test_a_failed_build_axis_without_target_modules_is_partial(self):
        cell = _cell(executed_count=52, red_count=0, modules=())
        result = _judge(_view(executed_count=52, build_ok=False), cell)
        assert result.built is False
        assert result.alpha_build == Pair(numerator=0, denominator=1)
        assert result.verdict == "partial"
        assert BUILD_AXIS_NOT_SUCCESSFUL in result.reason_codes


class TestTargetShapes:
    def test_a_grade_a_cell_without_counts_is_an_unusable_target(self):
        cell = _cell(executed_count=0, red_count=0, grade="A")
        result = _judge(_view(executed_count=52), cell)
        assert result.verdict == "invalid"
        assert result.target_usable is False
        assert TARGET_WITHOUT_COUNTS in result.reason_codes
        assert result.alpha is None

    def test_a_grade_b_cell_without_counts_is_vacuously_covered_and_says_so(self):
        cell = _cell(executed_count=0, red_count=0, grade="B")
        result = _judge(_view(executed_count=0), cell)
        assert result.verdict == "met"
        assert result.alpha_test is None
        assert result.alpha == Pair(numerator=1, denominator=1)
        assert TARGET_TEST_UNIVERSE_EMPTY in result.reason_codes

    def test_a_record_without_a_matched_cell_cannot_be_compared(self):
        record = TargetRecord(
            repo="apache/tomcat-jakartaee-migration",
            sha="50c811bffa4da6e48f5661e6a5a7763ffccdad46",
            harvested_at="2026-08-27T12:04:00Z",
            cells=(_cell(),),
        )
        with pytest.raises(ValueError, match="matched cell"):
            evaluate_attainment(_view(), record)

    def test_only_the_matched_cell_is_an_obligation(self):
        matched = _cell(cell_id="build (17, ubuntu-latest)", executed_count=52, red_count=0)
        other = _cell(cell_id="build (21, ubuntu-latest)", executed_count=5_000, red_count=0)
        result = evaluate_attainment(_view(executed_count=52), _record(matched, other=other))
        assert result.cell_id == "build (17, ubuntu-latest)"
        assert result.executed_target == 52
        assert result.verdict == "met"


class TestTomcatShaped:
    """D2R6 tomcat-jakartaee-migration: 52 executed, 0 red, on the JDK17 cell."""

    def _cell(self) -> CellTarget:
        return CellTarget(
            cell_id="build (17, ubuntu-latest)",
            build="ok",
            executed_count=52,
            red_count=0,
            modules=(".",),
            grade="A",
            evidence_refs=("workflow:ci.yml",),
        )

    def test_the_run_that_matched_the_cell_is_met(self):
        view = _view(executed_count=52, red_count=0, modules=(".",))
        result = _judge(view, self._cell())
        assert result.verdict == "met"
        assert result.alpha == Pair(numerator=52, denominator=52)
        assert result.alpha_test == Pair(numerator=52, denominator=52)
        assert result.alpha_build == Pair(numerator=1, denominator=1)
        assert result.reason_codes == (CLEAN_BY_COUNTS_ONLY,)

    def test_one_test_short_is_partial_at_fifty_one_over_fifty_two(self):
        view = _view(executed_count=51, red_count=0, modules=(".",))
        result = _judge(view, self._cell())
        assert result.verdict == "partial"
        assert result.alpha == Pair(numerator=51, denominator=52)
        assert result.alpha_test == Pair(numerator=51, denominator=52)
        assert EXECUTION_BELOW_TARGET in result.reason_codes

    def test_the_real_certificate_reaches_the_cell_end_to_end(self):
        payload = json.loads(CERTIFICATE_FIXTURE.read_text(encoding="utf-8"))
        inputs = {item["scope"]["subject"]["project_id"]: item for item in payload["inputs"]}
        certificate = evaluate_java_success_certificate(
            JavaCertificateInput.model_validate(inputs["tomcat-jakartaee-migration"])
        )
        result = _judge(view_from_certificate(certificate), self._cell())
        assert result.verdict == "met"
        assert result.executed_observed == 52
        assert result.red_observed == 0
        assert result.alpha == Pair(numerator=52, denominator=52)
