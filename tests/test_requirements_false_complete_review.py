"""Bounded independent review of evidence that could overstate task completion."""

from copy import deepcopy

import pytest

from sag.benchmark.evaluator import native_requirement
from sag.benchmark.native_evidence import maven_events
from sag.benchmark.requirements import report_scope_errors, summarize, validate_requirements
from test_benchmark_requirement_evaluator import fixture, write
from test_sag_requirement_observer import setup as observer_setup, contract, receipt


@pytest.mark.parametrize("namespace", ["subtype", "module"])
def test_one_report_directory_cannot_certify_two_namespaces(fixture, namespace):
    task, spec, _, _, _, _ = fixture
    first = spec["requirements"][1]
    first["validation"]["report_directories"] = ["target/shared-reports"]
    second = deepcopy(first)
    second["id"] = "other-tests"
    second[namespace] = "integration" if namespace == "subtype" else "other-module"
    spec["requirements"].append(second)
    with pytest.raises(ValueError, match="[Rr]eport.*(?:overlap|scope)|[Tt]est report"):
        validate_requirements(spec, task)


def test_actual_observer_shared_xml_does_not_certify_unit_and_integration(observer_setup):
    root, _, make = observer_setup

    def definitions(task, spec):
        first = spec["requirements"][0]
        first["validation"]["report_directories"] = ["target/shared-reports"]
        second = deepcopy(first)
        second.update(id="integration-tests", subtype="integration")
        second["validation"]["goals"] = ["failsafe:integration-test"]
        spec["requirements"] = [first, second]
        # Draft annotations may be collected, but overlapping scope stays unknown.
        spec["annotation_completeness"] = {
            "status": "review_required", "gaps": ["shared report scope"]
        }

    observer = make(definitions)
    frozen = contract(observer)
    observer.before_contract(frozen)
    folder = root / "target/shared-reports"
    folder.mkdir(parents=True)
    (folder / "TEST-Tiny.xml").write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase name="one" classname="Tiny"/></testsuite>'
    )
    observer.after_receipt(receipt(frozen))
    collected = observer.export_invocation(frozen["contract_id"], base=observer.base)
    # Real observer output, not hand-authored duplicate references.
    assert {r["producer_relative_path"] for r in collected["reports"]} == {
        "target/shared-reports/TEST-Tiny.xml"
    }
    assert len({r["sha256"] for r in collected["reports"]}) == 1
    text = (
        "[INFO] --- maven-surefire-plugin:3.5.0:test (default-test) @ tiny ---\n"
        "[INFO] Tests run: 1, Failures: 0, Errors: 0, Skipped: 0\n"
        "[INFO] --- maven-failsafe-plugin:3.5.0:integration-test (default) @ tiny ---\n"
        "[INFO] No tests to run.\n"
        "[INFO] BUILD SUCCESS\n"
    )
    invocation = {
        **collected, "invocation_id": frozen["contract_id"], "runner": "maven",
        "status": "completed", "exit_code": 0, "log_complete": True,
        "log": write(observer.base, "native-log.txt", text),
    }
    errors = report_scope_errors(observer.spec["requirements"], {"verify": "maven"})
    rows = [
        native_requirement(
            row, invocation, observer.base, text,
            maven_events(text, terminal=True, serial=True), errors.get(row["id"])
        )
        for row in observer.spec["requirements"]
    ]
    assert [row["status"] for row in rows] == ["unavailable", "not_run"], rows
    summary = summarize(
        observer.spec, rows, [{"id": "runtime", "status": "passed"}],
        [{"id": "verify", "status": "passed"}],
    )
    assert summary["status"] == summary["groups"]["test"] == "incomplete"


def test_draft_lower_bound_does_not_certify_entire_build_or_test(fixture):
    _, spec, _, _, _, score = fixture
    spec["annotation_completeness"] = {
        "status": "review_required", "gaps": ["unreviewed additional requirements"]
    }
    result = score()
    assert {row["status"] for row in result["requirements"]} == {"passed"}
    assert result["status"] == "unavailable"
    assert set(result["groups"].values()) == {"unavailable"}


def test_old_ci_totals_never_supply_new_ci_or_case_scores(fixture):
    _, spec, record, run, _, score = fixture
    legacy = {"executed_count": 1, "passed_count": 1, "executed_ids": ["Tiny.one"]}
    for document in (spec, record, run):
        document["ci_target"] = deepcopy(legacy)
        document["ci_attainment"] = {"build": 1.0, "test": 1.0}
    result = score()
    assert result["status"] == "complete"
    assert result["ci_scope_attainment"] is None
    assert result["ci_test_count_attainment"] is None
    assert result["ci_case_identity_equivalence"] is None
