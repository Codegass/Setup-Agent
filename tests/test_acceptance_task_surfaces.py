"""The host input and sealed task result survive the real CLI/report/API paths."""

import json

import pytest
from test_cli_project_exit_codes import RecordingSetupAgent, invoke_project
from test_control_layer_replay import _pin_template_agent
from test_snapshot_surface_agreement import (
    VERDICT_PATH,
    SnapshotOrchestrator,
    SurfaceHarness,
    _phase_trunk,
    snapshot_factory,
)

from sag.agent.acceptance_task import (
    AcceptanceTask,
    TaskCompletionSnapshot,
    render_task_completion_lines,
)
from sag.agent.evidence_publications import (
    VERDICT_LOGICAL_ARTIFACT_ID,
    evidence_publication_authority_for,
)
from sag.agent.verdict_finalizer import RunVerdictSnapshot, validate_verdict_snapshot_v3
from sag.web.session_registry import _session_detail, _setup_artifact_item


def _block_task_fragments(completion):
    """What the result block must state about a sealed required task."""

    steps = tuple(completion.steps or ())
    if not steps:
        return [str(completion.status), *completion.reasons]
    complete = sum(1 for step in steps if step.status == "complete")
    return [
        f"{completion.status} {complete}/{len(steps)} steps",
        *[f"{step.id}: {step.status} \u2014 {step.command}" for step in steps],
    ]


def input_task():
    return AcceptanceTask.model_validate(
        {
            "repo": "apache/commons-cli",
            "sha": "a" * 40,
            "steps": [{"id": "verify", "runner": "maven", "argv": ["mvn", "verify"]}],
        }
    )


def test_cli_loads_task_once_and_pins_the_requested_software_version(tmp_path, monkeypatch):
    task = input_task()
    path = tmp_path / "task.json"
    path.write_text(task.model_dump_json())
    RecordingSetupAgent.calls = []
    result = invoke_project(
        monkeypatch, tmp_path, RecordingSetupAgent, "--acceptance-task-file", str(path)
    )
    assert result.exit_code == 0, result.output
    submitted = RecordingSetupAgent.calls[0]
    assert submitted["acceptance_task"] == task
    assert submitted["project_ref"] == task.sha
    path.write_text("{}")
    agent = _pin_template_agent(tmp_path)
    agent._setup_acceptance_task = submitted["acceptance_task"]
    agent._initialize_run_pin_template()
    pin = json.loads((tmp_path / "run-pin.json").read_text())
    assert pin["sanitized_config"]["acceptance_task"] == {
        "sha256": task.sha256,
        "definition": task.model_dump(mode="json"),
    }


@pytest.mark.parametrize("mismatch", ["repo", "ref"])
def test_cli_rejects_mismatched_fixed_inputs_before_container_work(tmp_path, monkeypatch, mismatch):
    payload = input_task().model_dump(mode="json")
    if mismatch == "repo":
        payload["repo"] = "apache/other"
    path = tmp_path / "task.json"
    path.write_text(json.dumps(payload))
    RecordingSetupAgent.calls = []
    args = ["--acceptance-task-file", str(path)]
    if mismatch == "ref":
        args += ["--ref", "b" * 40]
    result = invoke_project(monkeypatch, tmp_path, RecordingSetupAgent, *args)
    assert result.exit_code != 0
    assert not RecordingSetupAgent.calls


@pytest.mark.parametrize("status", ["complete", "incomplete", "unavailable"])
def test_required_task_result_is_identical_across_sealed_surfaces(snapshot_factory, status):
    completion = TaskCompletionSnapshot.model_validate(
        {
            "run_id": "run-1",
            "task_sha256": "b" * 64,
            "status": status,
            "steps": [
                {
                    "id": "build",
                    "command": "mvn verify",
                    "status": "complete",
                    "receipt_id": "display-fixture",
                    "exit_code": 0,
                }
            ]
            + (
                [{"id": "native", "command": "./app", "status": "missing"}]
                if status == "incomplete"
                else []
            ),
        }
        if status != "unavailable"
        else {
            "run_id": "run-1",
            "task_sha256": "b" * 64,
            "status": status,
            "reasons": ["task_definition_missing_or_changed"],
        }
    )
    base = snapshot_factory(
        verdict="success", unique_total=12, unique_passed=12, unique_errors=0, raw_executions=12
    )
    snapshot = RunVerdictSnapshot.model_validate(
        {
            **base.model_dump(mode="json"),
            "run_id": "run-1",
            "verdict": "success" if status == "complete" else "partial",
            "task_completion": completion,
        }
    )
    assert validate_verdict_snapshot_v3(snapshot.model_dump(mode="json")) == snapshot
    assert snapshot.test_stats == base.test_stats and snapshot.rates == base.rates
    expected = render_task_completion_lines(completion)
    surfaces = SurfaceHarness().render_all(snapshot)
    assert surfaces.condensed.verdict == snapshot.verdict
    assert all(line in surfaces.condensed.text for line in expected)
    # The report and the block print the same result card, so both say the same
    # result in its words; the agreement is checked against the sealed task
    # itself rather than against the condensed log's phrasing.
    for surface in (surfaces.markdown, surfaces.cli):
        assert surface.verdict == snapshot.verdict
        assert all(fragment in surface.text for fragment in _block_task_fragments(completion))
    orch = SnapshotOrchestrator(
        {
            VERDICT_PATH: snapshot.model_dump_json(),
            "/workspace/.setup_agent/contexts/trunk_tvm.json": _phase_trunk(),
        }
    )
    item = _setup_artifact_item(orch, "sag-tvm")
    detail = _session_detail(item, "sag-tvm", None).model_dump(mode="json", by_alias=True)
    assert detail["taskCompletion"] == completion.model_dump(mode="json")
    # The Workbench serves the card rather than rendering it, so the same
    # fragments the block and the report print are read out of the task row.
    task = next(row for row in detail["resultCard"]["rows"] if row["key"] == "task")
    task_text = " ".join(
        [
            task["status"],
            task["headline"],
            task["detail"] or "",
            task["reason"] or "",
            *task["items"],
        ]
    )
    for fragment in _block_task_fragments(completion):
        assert fragment in task_text
    authority = evidence_publication_authority_for(orch)
    head = authority.latest_head(VERDICT_LOGICAL_ARTIFACT_ID)
    authority.revoke_latest(
        record_kind="verdict",
        record_id=VERDICT_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=VERDICT_LOGICAL_ARTIFACT_ID,
        expected_previous_raw_sha256=head.raw_sha256,
    )
    assert _setup_artifact_item(orch, "sag-tvm")["task_completion"] is None


def test_snapshot_constructor_and_copy_revalidation_cannot_green_missing_steps(snapshot_factory):
    task = TaskCompletionSnapshot(
        run_id="run-1",
        task_sha256="b" * 64,
        status="incomplete",
        steps=({"id": "native", "command": "./app", "status": "missing"},),
    )
    base = snapshot_factory(verdict="success")
    with pytest.raises(ValueError):
        RunVerdictSnapshot.model_validate(
            {**base.model_dump(), "run_id": "run-1", "task_completion": task}
        )
    changed = task.model_copy(update={"status": "complete"})
    with pytest.raises(ValueError):
        render_task_completion_lines(changed)
