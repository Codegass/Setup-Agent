"""SAG-MS-1 conformance for the additive certificate v2 truth surface."""

import json
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError
from test_java_success_certificates import _obligations, _payload, _scope

from sag.agent import java_success_certificates as certificates
from sag.agent.java_success_certificates import (
    ASSURANCE_BY_LEVEL,
    BUILD_AXIS_TRUTH,
    INTEGRITY_AXIS_TRUTH,
    OVERALL_TRUTH_BY_RESULT,
    TEST_EXECUTION_AXIS_TRUTH,
    TEST_OUTCOME_AXIS_TRUTH,
    JavaSuccessCertificate,
)
from sag.agent.java_success_certificates import TestCounts as RuntimeTestCounts
from sag.agent.java_success_certificates import (
    evaluate_java_success_certificate,
    evaluate_java_success_manifest,
)

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "java_success_certificates"
    / "d2r6-five-project-inputs.json"
)


def _no_tests_payload(**overrides):
    values = {
        "documented_no_automated_tests": True,
        "test_steps": _obligations("test_plan_step", (), (), applicability="not_applicable"),
        "test_targets": _obligations("single_test_project", (), (), applicability="not_applicable"),
        "test_results_authority": "unavailable",
        "test_counts": None,
    }
    values.update(overrides)
    return _payload(**values)


RESULT_PAYLOADS = {
    "repository_green": lambda: _payload(),
    "repository_product_test_green": lambda: _payload(
        scope=_scope(kind="repository_product_tests")
    ),
    "scoped_green": lambda: _payload(scope=_scope(kind="selected_reactor")),
    "build_failed": lambda: _payload(
        build_units=_obligations(
            "maven_reactor_module", ("root", "core"), ("root",), failed=("core",)
        )
    ),
    "test_red": lambda: _payload(
        test_targets=_obligations("single_test_project", ("root",), (), failed=("root",)),
        test_counts=RuntimeTestCounts(executed=20, passed=18, failed=1, errors=1, skipped=0),
    ),
    "build_only": _no_tests_payload,
    "incomplete": lambda: _payload(
        test_counts=RuntimeTestCounts(executed=0, passed=0, failed=0, errors=0, skipped=0)
    ),
    "unverifiable": lambda: _payload(test_results_authority="diagnostic"),
}

OBLIGATION_FIELDS = ("build_steps", "build_units", "test_steps", "test_targets")


def _fixture_certificates() -> dict[str, dict]:
    result = evaluate_java_success_manifest(json.loads(FIXTURE.read_text(encoding="utf-8")))
    return {item["project_id"]: item for item in result["certificates"]}


def _obligation_blocks(certificate: dict) -> list[dict]:
    blocks = [certificate[field] for field in OBLIGATION_FIELDS]
    blocks.append(certificate["integrity"]["evidence"])
    return blocks


def test_the_mapping_tables_cover_every_vocabulary_value():
    assert set(OVERALL_TRUTH_BY_RESULT) == set(get_args(certificates.CertificateResult))
    assert set(BUILD_AXIS_TRUTH) == set(get_args(certificates.BuildStatus))
    assert set(TEST_EXECUTION_AXIS_TRUTH) == set(get_args(certificates.TestExecutionStatus))
    assert set(TEST_OUTCOME_AXIS_TRUTH) == set(get_args(certificates.TestOutcomeStatus))
    assert set(INTEGRITY_AXIS_TRUTH) == set(get_args(certificates.IntegrityStatus))
    assert set(ASSURANCE_BY_LEVEL) == set(get_args(certificates.AssuranceLevel))
    assert set(RESULT_PAYLOADS) == set(get_args(certificates.CertificateResult))


@pytest.mark.parametrize("expected_result", sorted(RESULT_PAYLOADS))
def test_every_result_value_carries_its_mapped_public_truth(expected_result):
    certificate = evaluate_java_success_certificate(RESULT_PAYLOADS[expected_result]())

    assert certificate.result == expected_result
    assert certificate.result_class == expected_result
    assert certificate.overall_truth == OVERALL_TRUTH_BY_RESULT[expected_result]


@pytest.mark.parametrize("expected_result", sorted(RESULT_PAYLOADS))
def test_axis_truths_restate_the_axis_statuses(expected_result):
    certificate = evaluate_java_success_certificate(RESULT_PAYLOADS[expected_result]())

    assert certificate.axis_truths.build == BUILD_AXIS_TRUTH[certificate.build_status]
    assert (
        certificate.axis_truths.test_execution
        == TEST_EXECUTION_AXIS_TRUTH[certificate.test_execution_status]
    )
    assert (
        certificate.axis_truths.test_outcome
        == TEST_OUTCOME_AXIS_TRUTH[certificate.test_outcome_status]
    )
    assert certificate.axis_truths.integrity == INTEGRITY_AXIS_TRUTH[certificate.integrity.status]
    closed_scope = certificate.scope.closure == "closed" and certificate.scope.kind != "unknown"
    assert certificate.axis_truths.scope == ("PASS" if closed_scope else "UNKNOWN")


def test_structured_pairs_agree_with_the_integer_counts_over_the_five_projects():
    for certificate in _fixture_certificates().values():
        for block in _obligation_blocks(certificate):
            if block["closure_fraction"] is None:
                assert block["success_fraction"] is None
                assert block["closure_pair"] is None
                assert block["satisfaction_pair"] is None
                assert block["interval"] is None
                continue
            assert block["required"] == block["satisfied"] + block["failed"] + block["missing"]
            assert block["closure_pair"] == {
                "numerator": block["closed"],
                "denominator": block["required"],
            }
            assert block["satisfaction_pair"] == {
                "numerator": block["satisfied"],
                "denominator": block["required"],
            }
            assert block["interval"] == {
                "lower_numerator": block["satisfied"],
                "upper_numerator": block["satisfied"] + block["missing"],
                "denominator": block["required"],
            }


def test_interval_collapse_agrees_with_the_obligation_status_over_the_five_projects():
    for certificate in _fixture_certificates().values():
        for block in _obligation_blocks(certificate):
            interval = block["interval"]
            if interval is None:
                assert block["status"] in {"not_applicable", "unavailable"}
                continue
            capped = interval["upper_numerator"] < interval["denominator"]
            assert capped is (block["failed"] > 0)
            degenerate = (
                interval["lower_numerator"]
                == interval["upper_numerator"]
                == interval["denominator"]
            )
            assert degenerate is (block["status"] == "complete")


def test_interval_separates_unresolved_width_from_a_definite_failure():
    identities = tuple(f"module-{index:05d}" for index in range(10_000))
    unresolved = evaluate_java_success_certificate(
        _payload(build_units=_obligations("maven_reactor_module", identities, identities[:-1]))
    )
    failed = evaluate_java_success_certificate(
        _payload(
            build_units=_obligations(
                "maven_reactor_module", identities, identities[:-1], failed=identities[-1:]
            )
        )
    )

    assert unresolved.build_units.interval.model_dump() == {
        "lower_numerator": 9999,
        "upper_numerator": 10_000,
        "denominator": 10_000,
    }
    assert unresolved.build_units.status == "incomplete"
    assert failed.build_units.interval.model_dump() == {
        "lower_numerator": 9999,
        "upper_numerator": 9999,
        "denominator": 10_000,
    }
    assert failed.build_units.status == "failed"


def test_a_not_applicable_axis_reports_no_measurement_rather_than_zero():
    certificate = evaluate_java_success_certificate(_no_tests_payload())

    assert certificate.test_targets.satisfaction_pair is None
    assert certificate.test_targets.interval is None
    assert certificate.axis_truths.test_execution == "NOT_APPLICABLE"
    assert [item.code for item in certificate.typed_reason_codes] == [
        "DOCUMENTED_NO_AUTOMATED_TESTS"
    ]
    assert certificate.typed_reason_codes[0].defeater_type == "applicability"


def test_every_failing_fixture_certificate_is_witnessed_by_a_rebutting_code():
    fixture = _fixture_certificates()
    failing = [item for item in fixture.values() if item["overall_truth"] == "FAIL"]

    assert failing
    for certificate in failing:
        assert any(
            code["defeater_type"] == "rebutting" for code in certificate["typed_reason_codes"]
        )
    for certificate in fixture.values():
        if certificate["result_class"] == "unverifiable":
            assert any(
                code["defeater_type"] == "undercutting"
                for code in certificate["typed_reason_codes"]
            )


@pytest.mark.parametrize(
    "payload_factory",
    [
        lambda: _payload(test_results_authority="diagnostic"),
        lambda: _payload(scope=_scope(closure="unknown")),
    ],
)
def test_an_unverifiable_certificate_names_an_undercutting_defeater(payload_factory):
    certificate = evaluate_java_success_certificate(payload_factory())

    assert certificate.result_class == "unverifiable"
    assert certificate.overall_truth == "UNKNOWN"
    assert any(item.defeater_type == "undercutting" for item in certificate.typed_reason_codes)


def test_a_failing_certificate_without_a_rebutting_code_cannot_be_constructed():
    certificate = evaluate_java_success_certificate(RESULT_PAYLOADS["build_failed"]())
    payload = certificate.model_dump(mode="python")
    payload["typed_reason_codes"] = [
        code for code in payload["typed_reason_codes"] if code["defeater_type"] != "rebutting"
    ]

    with pytest.raises(ValidationError, match="rebutting reason code"):
        JavaSuccessCertificate.model_validate(payload)


def test_the_result_class_alias_cannot_drift_from_the_canonical_result():
    certificate = evaluate_java_success_certificate(_payload())
    payload = certificate.model_dump(mode="python")
    payload["result_class"] = "scoped_green"

    with pytest.raises(ValidationError, match="alias the canonical result"):
        JavaSuccessCertificate.model_validate(payload)


@pytest.mark.parametrize(
    ("assurance_level", "expected"),
    [
        ("legacy_projected", "PROJECTED"),
        ("receipt_bound", "RECEIPT_BOUND"),
        ("sealed_lineage", "SEALED_LINEAGE"),
    ],
)
def test_assurance_renames_the_level_without_changing_it(assurance_level, expected):
    certificate = evaluate_java_success_certificate(_payload(assurance_level=assurance_level))

    assert certificate.assurance_level == assurance_level
    assert certificate.assurance == expected


def test_projected_fixture_certificates_are_never_promotion_eligible():
    for certificate in _fixture_certificates().values():
        assert certificate["assurance"] == "PROJECTED"
        assert certificate["promotion_eligible"] is False


@pytest.mark.parametrize(
    ("payload_factory", "eligible"),
    [
        (lambda: _payload(), True),
        (lambda: _payload(scope=_scope(kind="repository_product_tests")), True),
        (lambda: _payload(scope=_scope(kind="selected_reactor")), False),
        (lambda: _payload(scope=_scope(kind="ci_matrix")), False),
        (lambda: _payload(assurance_level="receipt_bound"), False),
        (RESULT_PAYLOADS["build_failed"], False),
    ],
)
def test_promotion_requires_a_sealed_pass_in_an_allowed_scope(payload_factory, eligible):
    certificate = evaluate_java_success_certificate(payload_factory())

    assert certificate.promotion_eligible is eligible
