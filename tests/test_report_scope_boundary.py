"""Observed report ownership does not authorize a new forced build."""

from pathlib import Path

import pytest
from build_requirements_fakes import complete_build_requirements_v1
from test_test_attempt_policy import ManifestOrchestrator

from sag.agent import attempt_policy

ROOT = "/workspace/project"


def _observed_receipt(system="maven", **overrides):
    return {
        "receipt_id": "inv-maven-test-0001",
        "tool": system,
        "working_directory": ROOT,
        "actual_cwd": ROOT,
        "domain_id": ROOT,
        **overrides,
    }


def _orchestrator(system="maven"):
    orch = ManifestOrchestrator()
    orch.manifest = complete_build_requirements_v1(
        project_root=ROOT,
        test_root=ROOT,
        test_system=system,
        test_islands=[],
    )
    if system == "maven":
        snippet = (
            Path(__file__).parent
            / "fixtures/d3r1_remediation/report_scope/httpcomponents-client.pom.xml"
        ).read_text()
        orch.files = {ROOT + "/pom.xml": "<project><profiles>" + snippet + "</profiles></project>"}
    else:
        snippet = (
            Path(__file__).parent
            / "fixtures/d3r1_remediation/report_scope/freemarker.settings.gradle.kts"
        ).read_text()
        orch.files = {ROOT + "/settings.gradle.kts": snippet}
    return orch


@pytest.mark.parametrize("system", ["maven", "gradle"])
def test_observed_scope_survives_an_unresolved_forced_graph(system):
    orch = _orchestrator(system)
    forced = attempt_policy.resolve_survey_test_candidates(orch)
    assert forced.status == "unsafe_coordinates"

    observed = attempt_policy.resolve_test_report_scope(
        orch, receipts=[_observed_receipt(system)], project_root=ROOT
    )
    assert observed.status == "available"
    assert observed.primary_root == ROOT
    assert observed.roots == (ROOT,)
    assert not hasattr(observed, "required_action")
    assert not hasattr(observed, "candidates")
    assert attempt_policy.resolve_survey_test_candidates(orch).status == "unsafe_coordinates"


@pytest.mark.parametrize(
    "receipts",
    [
        [],
        [_observed_receipt("gradle")],
        [_observed_receipt(actual_cwd="/workspace/other")],
        [_observed_receipt(working_directory="/workspace/other")],
        [_observed_receipt(domain_id="/workspace/other")],
        [_observed_receipt(actual_cwd=ROOT + "/child", working_directory=ROOT + "/child")],
    ],
)
def test_unproven_observed_root_is_not_promoted(receipts):
    result = attempt_policy.resolve_test_report_scope(
        _orchestrator(), receipts=receipts, project_root=ROOT
    )
    assert result.status != "available"
    assert result.primary_root is None


def test_report_scope_cannot_use_another_projects_survey():
    result = attempt_policy.resolve_test_report_scope(
        _orchestrator(), receipts=[_observed_receipt()], project_root="/workspace/other"
    )
    assert result.status == "unsafe_coordinates"


@pytest.mark.parametrize(
    "wrong",
    [
        {"tool": "gradle"},
        {"actual_cwd": ROOT + "/other"},
        {"domain_id": ROOT + "/other"},
    ],
)
def test_one_good_receipt_does_not_authorize_another_receipts_claims(wrong):
    result = attempt_policy.resolve_test_report_scope(
        _orchestrator(),
        receipts=[
            _observed_receipt(),
            _observed_receipt(receipt_id="inv-maven-test-0002", **wrong),
        ],
        project_root=ROOT,
    )
    assert result.status == "available"
    assert result.receipt_ids == ("inv-maven-test-0001",)


@pytest.mark.parametrize("primary_system", ["maven", "gradle"])
@pytest.mark.parametrize("include_primary", [True, False], ids=["both-receipts", "aux-only"])
def test_same_root_auxiliary_backend_never_proves_primary(primary_system, include_primary):
    auxiliary_system = "gradle" if primary_system == "maven" else "maven"
    orch = _orchestrator(primary_system)
    orch.manifest["test_islands"] = [{"root": ROOT, "system": auxiliary_system}]
    orch.restamp_survey()
    receipts = [
        _observed_receipt(auxiliary_system, receipt_id="inv-aux-test-0002"),
    ]
    if include_primary:
        receipts.append(_observed_receipt(primary_system, receipt_id="inv-primary-test-0001"))

    result = attempt_policy.resolve_test_report_scope(orch, receipts=receipts, project_root=ROOT)

    assert result.roots == (ROOT,)
    assert result.status == ("available" if include_primary else "receipt_unbound")
    assert result.primary_root == (ROOT if include_primary else None)
    assert result.receipt_ids == (("inv-primary-test-0001",) if include_primary else ())


def test_proven_cwd_alias_keeps_claims_without_repeating_lexical_scope_checks():
    from sag.agent.physical_validator import PhysicalValidator

    orch = _orchestrator()
    orch.realpaths[ROOT + "/alias"] = ROOT
    receipt = _observed_receipt(working_directory=ROOT + "/alias")
    path = ROOT + "/target/surefire-reports/TEST-one.xml"
    receipt["report_delta"] = {"new": [{"path": path, "sha256": "a" * 64}]}
    scope = attempt_policy.resolve_test_report_scope(orch, receipts=[receipt], project_root=ROOT)
    assert scope.status == "available"
    assert PhysicalValidator._verified_report_claims(
        [receipt], scope.primary_root, owner_receipt_ids=scope.receipt_ids
    ) == {path: ["a" * 64]}
    orch.realpaths[ROOT + "/alias"] = "/workspace/other"
    assert (
        attempt_policy.resolve_test_report_scope(orch, receipts=[receipt], project_root=ROOT).status
        == "receipt_unbound"
    )


def test_root_ownership_does_not_reconstruct_unavailable_module_identities():
    from copy import deepcopy

    receipt = _observed_receipt(
        "gradle",
        testcase_execution_rows={
            "status": "unavailable",
            "rows": [],
            "reasons": ["module_qualified_identity_unavailable"],
        },
    )
    before = deepcopy(receipt)
    scope = attempt_policy.resolve_test_report_scope(
        _orchestrator("gradle"), receipts=[receipt], project_root=ROOT
    )
    assert scope.status == "available"
    assert receipt == before
    assert receipt["testcase_execution_rows"]["status"] == "unavailable"
