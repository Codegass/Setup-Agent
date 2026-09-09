"""Live facade publications feed the certificate adapter; no CertificateView fixture."""

from __future__ import annotations

import hashlib
import json
import shlex
from types import SimpleNamespace

import pytest
from container_evidence_fakes import (
    add_published_mutable_json,
    complete_run_pin,
    strict_published_evidence,
)
from test_receipt_assessor import (
    CURRENT,
    SHA,
    WIRED_REQUIREMENTS,
    ContainerFS,
    _publish_requirements,
    build_action_context,
)

from sag.agent.ci_comparison import PinnedCITarget, build_ci_comparison, render_ci_comparison_lines
from sag.agent.evidence_publications import RUN_PIN_LOGICAL_ARTIFACT_ID
from sag.agent.evidence_state import RunEvidenceState
from sag.agent.invocation_contracts import contract_receipt_fields, current_contract
from sag.agent.invocation_receipts import record_invocation
from sag.agent.phase_gates import claim_identity
from sag.agent.phase_machine import PhaseAttemptRecord, PhaseClaim
from sag.agent.physical_validator import PhysicalValidator
from sag.agent.project_execution_plan import (
    canonical_authored_plan_sha256,
    seal_and_write_project_execution_plan,
    validate_authored_plan,
)
from sag.metrics.target_record import CellTarget, TargetRecord
from sag.tools.base import ToolResult
from sag.tools.build.build_tool import BuildTool

ROOT = "/workspace/proj"
REPORT = ROOT + "/target/surefire-reports/TEST-a.xml"
XML = '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase classname="a.T" name="one"/></testsuite>'


class ComparisonFS(ContainerFS):
    def __init__(self):
        super().__init__(markers={"pom.xml"})
        self.dirty = False
        self.files[ROOT + "/pom.xml"] = (
            "<project><modelVersion>4.0.0</modelVersion><artifactId>p</artifactId></project>"
        )

    def execute_command(self, command, **kwargs):
        if "diff --quiet HEAD --" in command:
            return {"success": not self.dirty, "exit_code": 1 if self.dirty else 0, "output": ""}
        if command.startswith("realpath -e -- "):
            return {"success": True, "exit_code": 0, "output": shlex.split(command)[-1]}
        if "__SAG_GRAPH_FILE__" in command:
            path = shlex.split(command)[3]
            return {
                "success": True,
                "exit_code": 0,
                "output": "__SAG_GRAPH_FILE__" if path in self.files else "__SAG_GRAPH_ABSENT__",
            }
        if "__SAG_GRAPH_DIRECTORY__" in command:
            return {"success": True, "exit_code": 0, "output": "__SAG_GRAPH_ABSENT__"}
        return super().execute_command(command, **kwargs)


class Runner:
    """A deterministic backend emits real immutable facade receipts and bound output."""

    def __init__(self, fs, *, exit_code=0, xml=XML, modules=True, termination_reason=None):
        self.fs, self.exit_code, self.xml, self.modules = fs, exit_code, xml, modules
        self.sequence = 0
        self.termination_reason = termination_reason

    def execute(self, **params):
        self.sequence += 1
        action = params["command"]
        contract = current_contract()
        argv = "mvn " + contract["expected_argv"]
        testing = action in {"test", "verify"}
        output = "BUILD SUCCESS"
        if testing and self.exit_code:
            output = "Tests run: 1, Failures: 1, Errors: 0, Skipped: 0\n[ERROR] Failed to execute goal org.apache.maven.plugins:maven-surefire-plugin:3.5.1:test (default-test) on project p: There are test failures.\nBUILD FAILURE"
        if testing:
            self.fs.files[REPORT] = self.xml
            before, after = {}, {REPORT: hashlib.sha256(self.xml.encode()).hexdigest()}
        else:
            before, after = {}, {ROOT + "/target/classes/a/T.class": "a" * 64}
        metadata = record_invocation(
            self.fs.execute_command,
            tool="maven",
            attempt=self.sequence,
            requested_action=action,
            effective_action=action,
            argv=argv,
            working_directory=ROOT,
            exit_code=self.exit_code if testing else 0,
            before=before,
            after=after,
            requirements=WIRED_REQUIREMENTS,
            output=output,
            lifecycle_state="finished",
            termination_reason=self.termination_reason if testing else None,
            module_outcomes=[{"module": ".", "status": "success"}] if self.modules else None,
            **contract_receipt_fields(argv),
        )
        result = (
            ToolResult.completed_success(output=output)
            if not testing or not self.exit_code
            else ToolResult.completed_failure(output=output, error="test red")
        )
        result.metadata.update(metadata)
        return result


def setup_run(
    *,
    repeat=1,
    execute_tests=None,
    xml=XML,
    exit_code=0,
    modules=True,
    test_action="test",
    plan_test_args=None,
    termination_reason=None,
):
    fs = ComparisonFS()
    strict_published_evidence(fs, run_id="run-pytest", target_sha=SHA, run_pin=False)
    _publish_requirements(fs)
    add_published_mutable_json(
        fs,
        fs,
        path="/workspace/.setup_agent/run-pin.json",
        record_kind="run_pin",
        record_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
        payload=complete_run_pin("run-pytest", SHA),
    )
    step = lambda action: {
        "tool": "build",
        "params": {"action": action, "working_directory": ROOT},
        "purpose": "Execute the fixed task.",
        "evidence_refs": ["output_read"],
    }
    plan = validate_authored_plan(
        {
            "summary": "Compile, then execute one fixed test task.",
            "documents_reviewed": [
                {
                    "path": ROOT + "/README.md",
                    "reason": "Documented task.",
                    "evidence_refs": ["output_read"],
                }
            ],
            "build_steps": [step("compile")],
            "build_success_criteria": ["The compile invocation finishes."],
            "test_steps": [
                {
                    **step(test_action),
                    "params": {
                        **step(test_action)["params"],
                        **({"args": plan_test_args} if plan_test_args else {}),
                    },
                }
                for _ in range(repeat)
            ],
            "test_success_criteria": ["The planned calls complete with nonempty tests."],
            "environment_constraints": [],
            "risks": [],
            "unresolved_questions": [],
        }
    )
    claim = PhaseClaim(
        phase="analyze",
        signal="done",
        claimed_outcome="success",
        execution_plan_sha256=canonical_authored_plan_sha256(plan),
    )
    seal_and_write_project_execution_plan(
        plan, fs, source_attempt_id="analyze-1", claim_sha256=claim_identity(claim)
    )
    state = RunEvidenceState(run_id="run-pytest")
    state.record_phase_record(
        PhaseAttemptRecord(
            phase="analyze",
            attempt_id="analyze-1",
            termination="completed",
            outcome="success",
            transition="advance",
            claim=claim,
        )
    )
    runner = Runner(
        fs, xml=xml, exit_code=exit_code, modules=modules, termination_reason=termination_reason
    )
    tool = BuildTool(fs, maven_tool=runner)
    for index, action in enumerate(
        ["compile"] + [test_action] * (repeat if execute_tests is None else execute_tests)
    ):
        with build_action_context(f"envelope-ci-{index+1}", action=action):
            result = tool.execute(action=action, working_directory=ROOT)
        assert result.completed, result
    validator = PhysicalValidator(fs, project_path="/workspace")
    validator.receipt_run_id = state.run_id
    cell = CellTarget(
        cell_id="jdk17",
        build="ok",
        grade="A",
        executed_count=1,
        red_count=0,
        modules=(".",),
        modules_basis="log",
        command="mvn test",
    )
    record = TargetRecord(
        repo="apache/example",
        sha=SHA,
        harvested_at="2026-09-09T00:00:00Z",
        cells=(cell,),
        matched_cell=cell.cell_id,
    )
    target = PinnedCITarget(
        record=record, raw_sha256=hashlib.sha256(record.model_dump_json().encode()).hexdigest()
    )
    return SimpleNamespace(fs=fs, state=state, validator=validator, target=target)


def compare(run, **kwargs):
    return build_ci_comparison(
        run.fs,
        run.state,
        validator=run.validator,
        project_root=ROOT,
        repository="https://github.com/apache/example.git",
        target=kwargs.get("target", run.target),
    )


def test_live_facade_receipts_produce_a_verified_maven_comparison():
    run = setup_run()
    result = compare(run)
    assert result.status == "evaluated", result
    assert result.certificate.test_counts.reported == 1
    assert result.certificate.flags.test_execution_closed
    assert result.attainment.verdict == "met", result.model_dump()
    assert len(result.receipt_ids) == 2
    assert compare(run) == result


def test_plan_multiplicity_requires_distinct_receipts():
    run = setup_run(repeat=2)
    result = compare(run)
    assert result.certificate.test_steps.closure_fraction == "2/2", result
    assert result.certificate.test_counts.reported == 2
    assert len(result.receipt_ids) == 3
    assert result.attainment.alpha is None
    assert "CI_TEST_SCOPE_OVERLAP_UNRESOLVED" in result.reasons


def test_one_receipt_cannot_discharge_two_planned_calls():
    result = compare(setup_run(repeat=2, execute_tests=1))
    assert result.attainment.alpha is None
    assert result.certificate.test_steps.closure_fraction == "1/2"


@pytest.mark.parametrize("remove", ["report", "receipt", "assessment", "plan", "pin"])
def test_deleting_authorized_evidence_cannot_improve_the_positive_result(remove):
    run = setup_run()
    assert compare(run).attainment.verdict == "met"
    if remove == "report":
        del run.fs.files[REPORT]
    else:
        marker = {
            "receipt": "/invocation_receipts/",
            "assessment": "/evidence_assessments/",
            "plan": "project_execution_plan.json",
            "pin": "run-pin.json",
        }[remove]
        key = next(path for path in run.fs.files if marker in path)
        del run.fs.files[key]
    changed = compare(run)
    assert changed.attainment is None or changed.attainment.alpha is None, changed


def test_single_maven_project_requires_independent_graph_evidence():
    run = setup_run(modules=False)
    assert compare(run).attainment.verdict == "met"
    del run.fs.files[ROOT + "/pom.xml"]
    changed = compare(run)
    assert changed.attainment is None or changed.attainment.alpha is None


def test_project_assertion_red_is_closed_but_not_ci_green():
    xml = XML.replace('failures="0"', 'failures="1"').replace(
        "/></testsuite>", '><failure message="wrong"/></testcase></testsuite>'
    )
    result = compare(setup_run(xml=xml, exit_code=1))
    assert result.certificate.flags.test_execution_closed, result
    assert result.certificate.test_counts.failed == 1
    assert result.attainment.verdict == "not_met"


def test_old_checkout_cannot_score_even_with_the_old_green_reports():
    run = setup_run()
    assert compare(run).attainment.verdict == "met"
    run.fs.sha = "a" * 40
    result = compare(run)
    assert result.attainment is None or result.attainment.alpha is None


def test_wrong_target_subject_cannot_score():
    run = setup_run()
    payload = run.target.record.model_dump(mode="json")
    payload["repo"] = "apache/another"
    target = PinnedCITarget(record=TargetRecord.model_validate(payload), raw_sha256="f" * 64)
    result = compare(run, target=target)
    assert result.attainment.alpha is None
    assert "COMPARISON_SUBJECT_MISMATCH" in result.reasons


def test_deleting_authority_does_not_reconstruct_a_certificate_from_container_json():
    from sag.agent.evidence_publications import (
        install_evidence_publication_authority,
        reset_evidence_publication_authority,
        unavailable_evidence_publication_authority,
    )

    run = setup_run()
    assert compare(run).attainment.verdict == "met"
    token = install_evidence_publication_authority(
        unavailable_evidence_publication_authority("deleted for ablation", run_id=run.state.run_id),
        orchestrator=run.fs,
    )
    try:
        result = compare(run)
        assert result.attainment is None or result.attainment.alpha is None
    finally:
        reset_evidence_publication_authority(token)


def test_all_skipped_reports_do_not_close_required_test_bodies():
    xml = XML.replace('skipped="0"', 'skipped="1"').replace(
        "/></testsuite>", "><skipped/></testcase></testsuite>"
    )
    result = compare(setup_run(xml=xml))
    assert result.certificate.test_counts.skipped == 1
    assert not result.certificate.flags.test_execution_closed
    assert result.attainment.alpha is None


def test_complete_fresh_counts_survive_the_identity_sample_cap():
    cases = "".join(f'<testcase classname="a.T" name="case{index}"/>' for index in range(2049))
    cases += '<testcase classname="a.T" name="red"><failure message="wrong"/></testcase>'
    cases += '<testcase classname="a.T" name="skip"><skipped/></testcase>'
    xml = '<testsuite tests="2051" failures="1" errors="0" skipped="1">' + cases + "</testsuite>"
    run = setup_run(xml=xml, exit_code=1)
    result = compare(run)
    assert result.certificate.flags.test_execution_closed, result
    assert result.certificate.test_counts.model_dump() == {
        "reported": 2051,
        "passed": 2049,
        "failed": 1,
        "errors": 0,
        "skipped": 1,
    }
    assert result.attainment.verdict == "not_met"
    receipts = run.validator._current_scoped_receipts(ROOT)
    test_receipt = next(receipt for receipt in receipts if receipt["effective_action"] == "test")
    assert len(test_receipt["testcase_execution_rows"]["rows"]) == 2048
    assert test_receipt["testcase_row_disclosure"]["rows_truncated"]["dropped_green"] == 3


def test_renderer_uses_fractions_and_lifecycle_fields():
    result = compare(setup_run())
    lines = render_ci_comparison_lines(result)
    assert "scope score 1/1" in lines[0]
    assert lines[-1] == "CI lifecycle parity: equivalent"
    assert not any("numerator=" in line or '{"' in line for line in lines)


def test_verify_receipt_can_close_a_planned_integration_lifecycle():
    result = compare(setup_run(test_action="verify"))
    assert result.attainment.verdict == "met", result
    assert result.commands[-1].endswith("verify")


def test_a_different_selection_cannot_discharge_the_plan():
    result = compare(setup_run(plan_test_args="-Dtest=FullSuite"))
    assert result.attainment is None or result.attainment.alpha is None
    assert result.certificate.test_steps.closure_fraction == "0/1"


def test_timeout_keeps_existing_green_reports_but_withdraws_comparison():
    result = compare(setup_run(exit_code=137, termination_reason="timeout"))
    assert result.certificate.test_counts.reported == 1
    assert not result.certificate.flags.test_execution_closed
    assert result.attainment.alpha is None


def test_report_read_bound_never_becomes_a_complete_zero(monkeypatch):
    import sag.agent.receipt_test_rows as rows

    monkeypatch.setattr(rows, "DELTA_REPORT_MAX_BYTES", 64)
    result = compare(setup_run())
    assert result.certificate.test_counts is None
    assert result.attainment.alpha is None


@pytest.mark.parametrize("missing", ["module", "tests"])
def test_deleting_external_scope_removes_the_positive_score(missing):
    run = setup_run()
    assert compare(run).attainment.verdict == "met"
    payload = run.target.record.model_dump(mode="json")
    cell = payload["cells"][0]
    if missing == "module":
        cell["modules"] = []
        cell["modules_basis"] = None
    else:
        cell["executed_count"] = 0
    target = PinnedCITarget(record=TargetRecord.model_validate(payload), raw_sha256="e" * 64)
    result = compare(run, target=target)
    assert result.attainment.alpha is None
    assert result.attainment.verdict not in {"met", "exceeded"}


def test_modified_model_cannot_supply_the_missing_single_module_scope():
    run = setup_run(modules=False)
    assert compare(run).attainment.verdict == "met"
    run.fs.dirty = True
    result = compare(run)
    assert result.attainment.alpha is None
    assert "BUILD_MODULE_SCOPE_UNAVAILABLE" in result.reasons


def test_existing_receipt_module_proof_does_not_depend_on_current_pom_cleanliness():
    run = setup_run()
    run.fs.dirty = True
    assert compare(run).attainment.verdict == "met"
    assert not any("diff --quiet HEAD --" in command for command in run.fs.commands)


def target_with(run, **fields):
    payload = run.target.record.model_dump(mode="json")
    payload["cells"][0].update(fields)
    record = TargetRecord.model_validate(payload)
    return PinnedCITarget(
        record=record, raw_sha256=hashlib.sha256(record.model_dump_json().encode()).hexdigest()
    )


def test_same_pool_replacement_red_is_detected_by_identity_not_just_equal_counts():
    def xml(red):
        cases = "".join(
            '<testcase classname="a.T" name="'
            + name
            + '">'
            + ('<failure message="wrong"/>' if name == red else "")
            + "</testcase>"
            for name in ("A", "B")
        )
        return '<testsuite tests="2" failures="1" errors="0" skipped="0">' + cases + "</testsuite>"

    baseline = setup_run(xml=xml("A"), exit_code=1)
    target = target_with(
        baseline, executed_count=2, executed_ids=["a.T#A", "a.T#B"], red_count=1, red_ids=["a.T#A"]
    )
    positive = compare(baseline, target=target)
    assert positive.certificate.flags.test_execution_closed, positive
    assert positive.attainment.verdict == "met", positive
    assert positive.attainment.clean_form == "ids"
    changed = compare(setup_run(xml=xml("B"), exit_code=1), target=target)
    assert changed.certificate.flags.test_execution_closed, changed
    assert changed.certificate.test_counts.red == positive.certificate.test_counts.red == 1
    assert changed.attainment.clean_form == "ids"
    assert changed.attainment.unexpected_red_ids == ("a.T#B",)
    assert changed.attainment.verdict == "not_met"
    assert "NEW_RED_BEYOND_TARGET" in changed.reasons
    # Dropping either side's identity proof cannot turn the new red into met.
    missing_target_ids = target_with(baseline, executed_count=2, red_count=1)
    missing = compare(setup_run(xml=xml("B"), exit_code=1), target=missing_target_ids)
    assert missing.attainment.alpha is None
    assert "CI_TEST_IDENTITIES_NOT_COMPARABLE" in missing.reasons
    missing_local_ids = compare(
        setup_run(xml=xml("B").replace('classname="a.T"', 'classname="unknown.T"'), exit_code=1),
        target=target,
    )
    assert missing_local_ids.attainment.alpha is None
    assert "CI_TEST_IDENTITIES_NOT_COMPARABLE" in missing_local_ids.reasons


@pytest.mark.parametrize("name", ["other", "a.T#one", "one[0]"])
def test_different_raw_identity_pool_is_not_repaired_by_aliases(name):
    run = setup_run(xml=XML.replace('name="one"', f'name="{name}"'))
    target = target_with(run, executed_ids=["a.T#one"])
    result = compare(run, target=target)
    assert result.attainment.alpha is None
    assert "CI_TEST_IDENTITIES_NOT_COMPARABLE" in result.reasons


def test_duplicate_testcase_identity_does_not_become_a_complete_identity_set():
    xml = (
        '<testsuite tests="2" failures="0" errors="0" skipped="0">'
        + '<testcase classname="a.T" name="one"/>' * 2
        + "</testsuite>"
    )
    run = setup_run(xml=xml)
    result = compare(
        run, target=target_with(run, executed_count=2, executed_ids=["a.T#one", "a.T#two"])
    )
    assert result.certificate.test_counts.reported == 2
    assert result.attainment.alpha is None
    assert "CI_TEST_IDENTITIES_NOT_COMPARABLE" in result.reasons


def test_complete_green_counts_over_cap_stay_separate_from_complete_identity_claims():
    xml = (
        '<testsuite tests="2049" failures="0" errors="0" skipped="0">'
        + "".join(f'<testcase classname="a.T" name="case{i}"/>' for i in range(2049))
        + "</testsuite>"
    )
    run = setup_run(xml=xml)
    count_target = target_with(run, executed_count=2049)
    positive = compare(run, target=count_target)
    assert positive.attainment.verdict == "met", positive
    assert positive.attainment.clean_form == "counts"
    assert positive.certificate.test_counts.reported == 2049
    identity_target = target_with(
        run, executed_count=2049, executed_ids=[f"a.T#case{i}" for i in range(2049)]
    )
    bounded = compare(run, target=identity_target)
    assert bounded.attainment.alpha is None
    assert "CI_TEST_IDENTITIES_NOT_COMPARABLE" in bounded.reasons
    del run.fs.files[REPORT]
    changed = compare(run, target=count_target)
    assert changed.attainment is None or changed.attainment.alpha is None
