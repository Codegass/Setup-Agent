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
    pinned_acceptance_task,
    task_maven_requirement,
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


def with_maven_version(run, version="3.9.16"):
    data = run.task.model_dump(mode="json")
    for step in data["steps"]:
        step["maven_version"] = version
    run.task = AcceptanceTask.model_validate(data)
    pin_task(run)
    return run


@pytest.mark.parametrize("version", ["3.9.16", "3.9.14", None])
def test_fixed_maven_requires_actual_receipt_version(tmp_path, monkeypatch, version):
    run = with_maven_version(task_run(tmp_path, "mvn test"))

    def measured(*args, **kwargs):
        return (
            {"executable": "/opt/maven/bin/mvn", "version": f"Apache Maven {version}"}
            if version
            else None
        )

    monkeypatch.setattr("sag.agent.invocation_receipts.toolchain_fingerprint", measured)
    retain(run, dispatch(run, "mvn test"))
    snapshot = completion(run)
    assert snapshot.status == ("complete" if version == "3.9.16" else "unavailable")
    if version != "3.9.16":
        assert "Required Apache Maven 3.9.16" in snapshot.steps[0].reason


def test_unversioned_tasks_keep_their_original_hash(tmp_path):
    from sag.agent.control_events import canonical_sha256

    run = task_run(tmp_path, "mvn test")
    data = run.task.model_dump(mode="json")
    assert "maven_version" not in data["steps"][0]
    assert run.task.sha256 == canonical_sha256(data)
    versioned = with_maven_version(run)
    assert versioned.task.sha256 != canonical_sha256(data)
    assert "Maven exactly 3.9.16" in versioned.task.prompt(ROOT)


def test_maven_requirement_reads_published_task_even_when_model_parameter_absent(tmp_path):
    run = with_maven_version(task_run(tmp_path, "mvn test"))
    contract = {"run_id": run.state.run_id, "expected_cwd": ROOT, "expected_argv": "test"}
    # The in-memory task is intentionally still the old task from task_run.
    required = task_maven_requirement(run.fs, contract)
    assert required.raw == "3.9.16" and required.source == "acceptance_task"
    assert (
        task_maven_requirement(run.fs, {**contract, "expected_argv": "help:effective-pom"}) is None
    )
    with pytest.raises(ValueError, match="task_dispatch_contract_unavailable"):
        task_maven_requirement(run.fs, None)
    pin_task(run, {"sha256": "f" * 64, "definition": run.task.model_dump(mode="json")})
    with pytest.raises(ValueError, match="missing_or_changed"):
        task_maven_requirement(run.fs, contract)


def task_ci(run):
    from sag.agent.ci_comparison import build_ci_comparison

    return build_ci_comparison(
        run.fs,
        run.state,
        validator=run.validator,
        project_root=ROOT,
        repository=run.task.repo,
        target=run.target,
        task_completion=completion(run),
    )


def test_ci_bridge_consumes_fixed_task_without_old_command_or_plan(tmp_path):
    run = task_run(tmp_path, "mvn test")
    retain(run, dispatch(run, "mvn test"))
    assert run.target.execution_command is None
    assert run.state.phase_records == ()
    result = task_ci(run)
    assert result.status == "evaluated", result
    assert result.certificate.test_counts.reported == 1
    assert result.attainment.verdict == "met", result
    assert len(result.receipt_ids) == 1


@pytest.mark.parametrize("complete_second", [False, True])
def test_ci_task_preserves_later_command_obligation(tmp_path, monkeypatch, complete_second):
    import test_ci_comparison as runner_module

    original = runner_module.record_invocation

    def reports_only(*args, **kwargs):
        # Real Maven report snapshots contain XML, not compiler class files.
        kwargs["after"] = {p: h for p, h in kwargs["after"].items() if p.endswith(".xml")}
        return original(*args, **kwargs)

    monkeypatch.setattr(runner_module, "record_invocation", reports_only)
    run = task_run(tmp_path, "mvn compile", "mvn test")
    retain(run, dispatch(run, "mvn compile", suffix="1"))
    if complete_second:
        retain(run, dispatch(run, "mvn test", suffix="2"))
    result = task_ci(run)
    assert result.certificate is not None, result
    assert result.certificate.flags.test_execution_closed is complete_second
    if complete_second:
        assert len(result.certificate.build_steps.satisfied_ids) == 2
        assert result.certificate.test_counts.reported == 1
    else:
        assert result.attainment.verdict != "met"


def test_task_command_alone_does_not_create_missing_ci_scope(tmp_path):
    run = task_run(tmp_path, "mvn test")
    retain(run, dispatch(run, "mvn test"))
    data = run.target.model_dump(mode="json")
    data["record"]["cells"][0]["modules"] = []
    data["record"]["cells"][0]["modules_basis"] = None
    data["record"]["cells"][0]["executed_ids"] = []
    run.target = type(run.target).model_validate(data)
    result = task_ci(run)
    assert result.certificate.test_counts.reported == 1
    assert result.attainment.verdict not in {"met", "exceeded"}
    assert result.attainment.alpha is None


def test_removing_only_maven_version_verification_exposes_false_task_completion(
    tmp_path, monkeypatch
):
    """The same successful receipt must not satisfy a different pinned Maven."""
    import sag.agent.acceptance_task as tasks

    run = with_maven_version(task_run(tmp_path, "mvn test"))
    monkeypatch.setattr(
        "sag.agent.invocation_receipts.toolchain_fingerprint",
        lambda *a, **k: {
            "executable": "/opt/maven/bin/mvn",
            "version": "Apache Maven 3.9.14",
        },
    )
    result = retain(run, dispatch(run, "mvn test"))
    after = completion(run)
    with monkeypatch.context() as disabled:
        disabled.setattr(tasks, "task_maven_version_problem", lambda *a: None)
        before = completion(run)
    assert before.status == "complete" and after.status == "unavailable"
    assert before.steps[0].receipt_id == after.steps[0].receipt_id == result.metadata["receipt_id"]
    _export_protocol_ablation(
        "maven_version",
        {
            "before": before.status,
            "after": after.status,
            "actual": "3.9.14",
            "required": "3.9.16",
            "same_receipt": True,
        },
    )


def test_fixed_task_ci_bridge_against_frozen_adapter(tmp_path, monkeypatch):
    """Optional historical replay uses the same task, receipts and assessments."""
    import os
    import runpy
    from pathlib import Path
    import sag.agent.ci_comparison as ci

    runner_path = Path("logs/twenty-project-repairs-20260914/ablation.py")
    if not os.environ.get("SAG_REPAIR_ABLATION_OUTPUT"):
        pytest.skip("Frozen-source replay is run explicitly with the ablation artifacts")
    loader = runpy.run_path(str(runner_path))["old_function"]
    old = loader(ci, "_certificate_input")
    run = task_run(tmp_path, "mvn test")
    retain(run, dispatch(run, "mvn test"))
    with monkeypatch.context() as disabled:
        disabled.setattr(ci, "_certificate_input", lambda *args: old(*args[:6]))
        before = task_ci(run)
    after = task_ci(run)
    assert (
        before.status == "unavailable" and "accepted_execution_plan_unavailable" in before.reasons
    )
    assert after.status == "evaluated" and after.attainment.verdict == "met"
    _export_protocol_ablation(
        "ci_task_bridge",
        {
            "before": before.status,
            "before_reasons": before.reasons,
            "after": after.status,
            "test_count": after.certificate.test_counts.reported,
            "task_steps": 1,
            "same_task_receipts_and_assessments": True,
            "scope": "production readers with in-memory published fixture; not a model run",
        },
    )


def _export_protocol_ablation(name, data):
    import os
    from pathlib import Path

    if directory := os.environ.get("SAG_REPAIR_ABLATION_OUTPUT"):
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n")


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


@pytest.mark.parametrize("measured", [None, "17"])
def test_unknown_or_wrong_runtime_does_not_complete_the_step(tmp_path, monkeypatch, measured):
    run = task_run(tmp_path, "mvn test")
    run.task = AcceptanceTask.model_validate(
        {
            **run.task.model_dump(mode="json"),
            "steps": [{**run.task.steps[0].model_dump(mode="json"), "java_major": 21}],
        }
    )
    pin_task(run)
    if measured is not None:
        monkeypatch.setattr(
            "sag.tools.build.build_tool.active_java_runtime",
            lambda _: {"major": measured, "version": measured + ".0.1"},
        )
    retain(run, dispatch(run, "mvn test"))
    step = completion(run).steps[0]
    assert step.status == "unavailable"
    if measured:
        assert step.reason == "Required launcher Java 21; dispatch observed Java 17."
    else:
        assert step.reason == (
            "Required launcher Java 21; the receipt has no authorized "
            "dispatch JVM probe (recorded major: unknown)."
        )


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
