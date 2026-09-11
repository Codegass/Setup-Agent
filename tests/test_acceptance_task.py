"""Required task closure uses real facade publications, independently of plans."""

import json
import shlex

import pytest
from container_evidence_fakes import add_published_mutable_json, complete_run_pin
from test_ci_comparison import ROOT, SHA, Runner, setup_run
from test_fixed_command_verification import dispatch

from sag.agent.acceptance_task import (
    AcceptanceTask,
    TaskCompletionSnapshot,
    build_task_completion,
    load_acceptance_task,
    render_task_completion_lines,
)
from sag.agent.action_intents import action_fingerprint
from sag.agent.evidence_publications import RUN_PIN_LOGICAL_ARTIFACT_ID
from sag.agent.evidence_state import EvidenceRole, RunEvidenceState, StateScope
from sag.agent.invocation_contracts import action_context
from sag.agent.output_storage import OutputStorageManager, attach_durable_output_ref
from sag.agent.verdict_finalizer import EvidenceCloseReason, VerdictFinalizer
from sag.tools.build.build_tool import BuildTool


def task_run(tmp_path, *commands, native=False):
    run = setup_run(execute_tests=0)
    run.state = RunEvidenceState(run_id=run.state.run_id)
    run.task = AcceptanceTask.model_validate(
        {
            "repo": run.target.record.repo,
            "sha": SHA,
            "steps": [
                {
                    "id": f"step-{i}",
                    "runner": "native" if native else "maven",
                    "argv": shlex.split(c),
                }
                for i, c in enumerate(commands)
            ],
        }
    )
    pin_task(run)
    run.fs.acceptance_task = run.task
    run.fs.acceptance_task_root = ROOT
    run.fs.acceptance_task_repository = run.task.repo
    run.storage = OutputStorageManager(tmp_path / "output")
    return run


def pin_task(run, value=None):
    pin = complete_run_pin(run.state.run_id, SHA)
    pin["sanitized_config"]["acceptance_task"] = (
        value
        if value is not None
        else {
            "sha256": run.task.sha256,
            "definition": run.task.model_dump(mode="json"),
        }
    )
    add_published_mutable_json(
        run.fs,
        run.fs,
        path="/workspace/.setup_agent/run-pin.json",
        record_kind="run_pin",
        record_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
        payload=pin,
    )


def retain(run, result, *, tool="build"):
    result = attach_durable_output_ref(
        result,
        run.storage,
        task_id="required-task",
        tool_name=tool,
    )
    run.state.ingest_tool_result(
        StateScope.ARTIFACTS,
        tool,
        result,
        roles=(EvidenceRole.BUILD, EvidenceRole.TEST),
    )
    return result


def completion(run, *, task="selected"):
    return build_task_completion(
        run.fs,
        run.state,
        validator=run.validator,
        project_root=ROOT,
        repository=run.target.record.repo,
        task=run.task if task == "selected" else task,
        output_storage=run.storage,
    )


def test_one_receipt_completes_task_without_an_accepted_model_plan(tmp_path):
    run = task_run(tmp_path, "mvn clean install")
    result = retain(run, dispatch(run, "mvn clean install"))
    closed = completion(run)
    assert closed.status == "complete"
    assert closed.steps[0].receipt_id == result.metadata["receipt_id"]
    assert run.state.phase_records == ()
    assert not run.state.sealed
    assert "1/1 steps" in render_task_completion_lines(closed)[0]


@pytest.mark.parametrize("field", ["raw_output", "output"])
def test_retained_output_must_match_the_receipts_native_bytes(tmp_path, field):
    run = task_run(tmp_path, "mvn test")
    result = dispatch(run, "mvn test")
    if field == "output":
        result.raw_output = None
    setattr(result, field, "Unrelated output, stored durably under the same receipt id")
    retain(run, result)
    assert completion(run).steps[0].status == "unavailable"


def test_presentation_changes_do_not_replace_preserved_native_output(tmp_path):
    run = task_run(tmp_path, "mvn test")
    result = dispatch(run, "mvn test")
    result.output = "A different human-readable summary"
    retain(run, result)
    assert completion(run).status == "complete"


def _publish_settled_output(run, result):
    from test_job_settlement import TERMINAL_IDENTITY

    from sag.agent.job_obligations import build_obligation, write_obligation

    (receipt,) = [
        value
        for value in run.validator._current_scoped_receipts(ROOT)
        if value["receipt_id"] == result.metadata["receipt_id"]
    ]
    log_path = "/tmp/sag_jobs/task-output.log"
    run.fs.files[log_path] = result.raw_output
    record = build_obligation(
        job_id="task-output",
        run_id=run.state.run_id,
        tool="maven",
        attempt=1,
        requested_action=receipt["requested_action"],
        effective_action=receipt["effective_action"],
        argv=receipt["argv"],
        working_directory=ROOT,
        before={},
        log_path=log_path,
        exit_code_path=log_path + ".exit",
        contract_id=receipt["contract_id"],
        contract_hash=receipt["contract_hash"],
        execution_binding=receipt["execution_binding"],
        compliance=receipt["compliance"],
        **TERMINAL_IDENTITY,
    )
    assert write_obligation(run.fs.execute_command, record)
    record.update(
        process_state="terminal",
        settlement_state="pending",
        terminal_exit_code=receipt["exit_code"],
        terminal_marker_ref="docker-exec:" + TERMINAL_IDENTITY["docker_exec_id"],
        terminal_observed_at="2026-09-10T19:00:00Z",
    )
    assert write_obligation(run.fs.execute_command, record)
    record.update(settlement_attempts=1, attempted_receipt_id=receipt["receipt_id"])
    assert write_obligation(run.fs.execute_command, record)
    record.update(settlement_state="settled", settled_receipt_id=receipt["receipt_id"])
    assert write_obligation(run.fs.execute_command, record)
    return log_path


@pytest.mark.parametrize("remove", [None, "log", "obligation"])
def test_task_consumes_published_job_output_without_a_terminal_tool_observation(tmp_path, remove):
    run = task_run(tmp_path, "mvn test")
    result = dispatch(run, "mvn test")
    log_path = _publish_settled_output(run, result)
    if remove == "log":
        del run.fs.files[log_path]
    elif remove == "obligation":
        del run.fs.files["/workspace/.setup_agent/job_obligations/task-output.json"]
    assert run.state.tool_observations == ()
    closed = completion(run)
    assert closed.steps[0].status == ("complete" if remove is None else "unavailable")
    assert run.state.tool_observations == ()


def test_green_first_step_cannot_close_an_unrun_later_step(tmp_path):
    run = task_run(tmp_path, "mvn clean install", "mvn verify")
    retain(run, dispatch(run, "mvn clean install"))
    closed = completion(run)
    assert closed.status == "incomplete"
    assert [step.status for step in closed.steps] == ["complete", "missing"]
    snapshot = VerdictFinalizer(
        run.fs,
        validator=run.validator,
        project_name="proj",
        repository=run.target.record.repo,
        acceptance_task=run.task,
        output_storage=run.storage,
    ).finalize(run.state, EvidenceCloseReason.TEST_TERMINATED)
    assert snapshot.verdict != "success"
    assert snapshot.task_completion == closed
    assert "acceptance_task_incomplete" in snapshot.conflicts


def test_later_native_failure_is_not_hidden_by_older_green_receipt(tmp_path):
    run = task_run(tmp_path, "mvn test")
    retain(run, dispatch(run, "mvn test", suffix="1"))
    params = {"command": "mvn test", "working_directory": ROOT}
    domain = "test:" + ROOT
    with action_context(
        envelope_id="envelope-failed-retry",
        intent_id="intent-failed-retry",
        intent_domain_id=domain,
        intent_exact_params=params,
        action_fingerprint=action_fingerprint(domain_id=domain, tool="build", params=params),
    ):
        retain(run, BuildTool(run.fs, maven_tool=Runner(run.fs, exit_code=1)).execute(**params))
    closed = completion(run)
    assert closed.status == "incomplete"
    assert closed.steps[0].status == "failed"
    assert closed.steps[0].exit_code == 1


def test_same_call_cannot_satisfy_two_explicit_ordered_invocations(tmp_path):
    run = task_run(tmp_path, "mvn test", "mvn test")
    retain(run, dispatch(run, "mvn test", suffix="1"))
    assert completion(run).status == "incomplete"
    retain(run, dispatch(run, "mvn test", suffix="2"))
    closed = completion(run)
    assert closed.status == "complete"
    assert len({step.receipt_id for step in closed.steps}) == 2


def test_reversed_task_steps_are_visible_without_refusing_execution(tmp_path):
    run = task_run(tmp_path, "mvn clean install", "mvn verify")
    retain(run, dispatch(run, "mvn verify", suffix="1"))
    retain(run, dispatch(run, "mvn clean install", suffix="2"))
    closed = completion(run)
    assert closed.status == "incomplete"
    assert closed.steps[1].status == "out_of_order"


@pytest.mark.parametrize("remove", ["task", "output", "receipt", "contract"])
def test_removing_evidence_cannot_complete_the_fixed_task(tmp_path, remove):
    run = task_run(tmp_path, "mvn test")
    result = dispatch(run, "mvn test")
    if remove != "output":
        retain(run, result)
    if remove in {"receipt", "contract"}:
        fragment = "/invocation_receipts/" if remove == "receipt" else "/invocation_contracts/"
        path = next(p for p in run.fs.files if fragment in p)
        del run.fs.files[path]
    closed = completion(run, task=None if remove == "task" else "selected")
    assert closed.status != "complete"


@pytest.mark.parametrize("command", ["mvn test -DskipTests", "mvn test -pl child"])
def test_a_narrower_command_does_not_discharge_the_original_task(tmp_path, command):
    run = task_run(tmp_path, "mvn test")
    retain(run, dispatch(run, command))
    assert completion(run).steps[0].status == "missing"


def test_changed_task_input_cannot_redefine_obligations(tmp_path):
    run = task_run(tmp_path, "mvn test", "mvn verify")
    retain(run, dispatch(run, "mvn test"))
    shortened = AcceptanceTask.model_validate(
        {
            **run.task.model_dump(mode="json"),
            "steps": [run.task.steps[0].model_dump(mode="json")],
        }
    )
    closed = completion(run, task=shortened)
    assert closed.status == "unavailable"
    assert closed.task_sha256 == run.task.sha256


def test_unknown_or_wrong_runtime_does_not_complete_the_step(tmp_path):
    run = task_run(tmp_path, "mvn test")
    run.task = AcceptanceTask.model_validate(
        {
            **run.task.model_dump(mode="json"),
            "steps": [{**run.task.steps[0].model_dump(mode="json"), "java_major": 21}],
        }
    )
    pin_task(run)
    retain(run, dispatch(run, "mvn test"))
    assert completion(run).steps[0].status == "unavailable"


@pytest.mark.parametrize("value", [False, "broken", {"sha256": 3}, {"sha256": "bad"}])
def test_malformed_task_pin_stays_unavailable_without_breaking_receipt_read(tmp_path, value):
    run = task_run(tmp_path, "mvn test")
    pin_task(run, value)
    assert completion(run).status == "unavailable"


def test_loader_and_schema_do_not_accept_duplicate_fields_or_empty_completion(tmp_path):
    path = tmp_path / "task.json"
    path.write_text('{"repo":"apache/a","repo":"apache/b"}')
    with pytest.raises(ValueError, match="duplicate"):
        load_acceptance_task(path)
    with pytest.raises(ValueError):
        TaskCompletionSnapshot(run_id="run-1", status="complete", task_sha256="a" * 64)


def test_task_feedback_precedes_seal_and_accepts_an_honest_partial_claim(tmp_path):
    from types import SimpleNamespace
    from sag.agent.phase_gates import ValidatorState, validate_phase_claim
    from sag.agent.phase_machine import PhaseClaim, PhaseOutcome
    from sag.tools.phase_tool import PhaseTool

    run = task_run(tmp_path, "mvn test", "mvn verify")
    retain(run, dispatch(run, "mvn test"))
    tool = PhaseTool(
        SimpleNamespace(current_phase="test"),
        run.validator,
        run.fs,
        "proj",
        run_evidence_state=run.state,
        gate_fn=lambda phase, claim, *args, **kwargs: validate_phase_claim(
            claim, ValidatorState.GREEN
        ),
    )
    tool.bind_execution_plan_evidence(run.storage)
    green = tool._grade(
        PhaseClaim(phase="test", signal="done", claimed_outcome="success"), "test", sealed=False
    )
    assert not green.accepted
    assert green.code == "required_task_incomplete"
    assert green.validated_facts["physical_test_validator_state"] == "green"
    assert green.validated_facts["task_completion"]["steps"][1]["status"] == "missing"
    partial = tool._grade(
        PhaseClaim(phase="test", signal="done", claimed_outcome="partial"), "test", sealed=False
    )
    assert partial.accepted and partial.validated_outcome is PhaseOutcome.PARTIAL
    assert not run.state.sealed
    retain(run, dispatch(run, "mvn verify", suffix="after-feedback"))
    assert tool._grade(
        PhaseClaim(phase="test", signal="done", claimed_outcome="success"), "test", sealed=False
    ).accepted


def test_lost_output_bytes_cannot_be_replaced_by_an_indexed_reference(tmp_path, monkeypatch):
    run = task_run(tmp_path, "mvn test")
    retain(run, dispatch(run, "mvn test"))
    assert completion(run).status == "complete"
    monkeypatch.setattr(run.storage, "retrieve_output", lambda _: None)
    assert completion(run).steps[0].status == "unavailable"


def test_source_edits_do_not_prove_the_fixed_software_revision(tmp_path):
    run = task_run(tmp_path, "mvn test")
    retain(run, dispatch(run, "mvn test"))
    run.fs.dirty = True
    assert completion(run).reasons == ("task_source_changed_or_unverified",)


def test_unsupported_shell_scope_is_retained_instead_of_dropped(tmp_path):
    run = task_run(tmp_path, "mvn test")
    run.task = AcceptanceTask.model_validate(
        {
            **run.task.model_dump(mode="json"),
            "steps": [
                run.task.steps[0].model_dump(mode="json"),
                {
                    "id": "custom-check",
                    "runner": "shell",
                    "argv": ["bash", "-c", "false; true"],
                },
            ],
        }
    )
    pin_task(run)
    retain(run, dispatch(run, "mvn test"))
    closed = completion(run)
    assert closed.status == "unavailable"
    assert len(closed.steps) == 2 and closed.steps[0].status == "complete"
    assert closed.steps[1].status == "unavailable"


@pytest.mark.parametrize(
    "change",
    [
        {"cwd": "../elsewhere"},
        {"cwd": "/workspace"},
        {"cwd": "x/../."},
        {"argv": ["mvn", "test\ntrue"]},
        {"runner": "gradle"},
    ],
)
def test_task_step_bounds_reject_ambiguous_inputs(tmp_path, change):
    run = task_run(tmp_path, "mvn test")
    with pytest.raises(ValueError):
        AcceptanceTask.model_validate(
            {
                **run.task.model_dump(mode="json"),
                "steps": [{**run.task.steps[0].model_dump(mode="json"), **change}],
            }
        )
