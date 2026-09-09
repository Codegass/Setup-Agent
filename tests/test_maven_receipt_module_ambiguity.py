"""Repeated Maven display names cannot authorize a complete module scope."""

from copy import deepcopy

import pytest
from test_coverage_basis import (
    _current_build_receipt,
    _maven_reactor_validator,
    _met_expectation_validator,
    _receipt_validator,
)

from sag.agent.physical_validator import PhysicalValidator
from sag.agent.receipt_structure import structure_from_receipt


def _receipt(labels, *, sequence=1, tool="maven"):
    return _current_build_receipt(
        {
            "receipt_id": f"inv-{tool}-1-{sequence:04d}",
            "tool": tool,
            "exit_code": 0,
            "compliance": "exact",
            "module_outcomes": [{"module": label, "status": "success"} for label in labels],
        }
    )


@pytest.mark.parametrize(
    "labels",
    [
        ("Camel :: Common", "Camel :: Common"),
        ("Camel :: Core", "Other :: Core"),
        ("m-0", "m0"),
    ],
)
def test_ambiguous_maven_names_never_promote_a_deduplicated_structure(labels):
    receipt = _receipt(labels)
    original = deepcopy(receipt)

    assert structure_from_receipt(receipt) is None
    assert receipt == original


def test_repeated_gradle_task_coordinates_keep_the_existing_structure_contract():
    structure = structure_from_receipt(_receipt(("core", "core"), tool="gradle"))

    assert structure["modules"] == ["core"]


def test_ambiguous_maven_receipt_keeps_claims_but_cannot_narrow_the_denominator():
    receipt = _receipt(("m0", "m0"))
    original = deepcopy(receipt)
    evidence = _receipt_validator([receipt])._attempted_module_evidence("/workspace/proj")

    assert evidence.modules == ("m0",)
    assert evidence.narrowing_licensed is False
    assert evidence.cap == "build_receipt_module_identity_ambiguous"
    assert receipt == original


def test_same_module_in_separate_maven_receipts_is_not_a_display_name_collision():
    receipts = [_receipt(("m0",), sequence=number) for number in (1, 2)]

    evidence = _receipt_validator(receipts)._attempted_module_evidence("/workspace/proj")

    assert evidence.modules == ("m0",)
    assert evidence.narrowing_licensed is True
    assert evidence.cap is None


def test_unavailable_reactor_scope_cannot_disappear_beside_a_narrow_retry():
    incomplete = _receipt((), sequence=1)
    incomplete.pop("module_outcomes")
    incomplete["evidence_omissions"] = [
        {
            "field": "module_outcomes",
            "status": "unavailable",
            "reasons": ["maven_reactor_summary_boundaries_unavailable"],
        }
    ]
    validator = _receipt_validator([incomplete, _receipt(("m0",), sequence=2)])
    evidence = validator._attempted_module_evidence("/workspace/proj")
    assert evidence.modules == ("m0",)
    assert evidence.narrowing_licensed is False
    assert evidence.cap == "build_receipt_module_scope_unavailable"


@pytest.mark.parametrize("labels", [("m0", "m0"), ("m-0", "m0")])
def test_ambiguous_maven_receipt_cannot_state_terminal_module_totals(labels):
    validator = PhysicalValidator(project_path="/workspace")
    validator._current_scoped_receipts = lambda _project_dir: [_receipt(labels)]

    assert validator._terminal_root_maven_reactor_receipt("/workspace/proj") is None


def test_distinct_maven_module_names_keep_terminal_success_authority():
    validator = PhysicalValidator(project_path="/workspace")
    validator._current_scoped_receipts = lambda _project_dir: [_receipt(("m0", "m1"))]

    result = validator._terminal_root_maven_reactor_receipt("/workspace/proj")

    assert result["modules_succeeded"] == result["modules_total"] == 2


def test_duplicate_display_names_cannot_hide_missing_modules_in_the_build_verdict():
    validator = _maven_reactor_validator(receipts=[_receipt(("m0", "m0"))])

    result = validator.validate_build_status("proj")

    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "build_receipt_module_identity_ambiguous" in result["conflicts"]
    assert result["evidence"]["modules_attempted"] == ["m0"]
    assert not result["evidence"].get("modules_untried")
    assert any(
        "repeated or colliding Maven module display names" in warning
        for warning in result["evidence"]["warnings"]
    )


def test_ambiguous_receipt_caps_even_when_the_surveys_expected_outputs_all_exist():
    validator = _met_expectation_validator(scan=None, receipts=[_receipt(("m0", "m0"))])

    result = validator.validate_build_status("proj")

    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "build_receipt_module_identity_ambiguous" in result["conflicts"]
    assert "repeated or colliding Maven module display names" in result["reason"]
