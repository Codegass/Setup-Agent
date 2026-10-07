"""Independent builds cannot earn autonomous-execution credit.

These exercise the common physical evaluator, not an alternate success rule.
Origin is assigned by the trusted recorder/adapter, never by model prose.
"""

import copy
import json
import subprocess
import sys

import pytest

from sag.benchmark.evaluator import evaluate
from test_benchmark_requirement_evaluator import fixture, write


def marked(fixture, origin):
    task, spec, record, run, base, _ = fixture
    run, record = copy.deepcopy(run), copy.deepcopy(record)
    run["execution_origin"] = origin
    record["execution_origin"] = origin
    run["invocations"] = [write(base, "invocation-origin.json", record)]
    return base, task, spec, run


def test_replay_pass_is_not_agent_success(fixture):
    base, task, spec, run = marked(fixture, "independent_replay")
    replay = evaluate(task, spec, run, base, required_execution_origin="independent_replay")
    primary = evaluate(task, spec, run, base, required_execution_origin="agent")
    assert replay["status"] == "complete"
    assert replay["autonomous_success"] is None
    assert primary["status"] == "unavailable"
    assert primary["autonomous_success"] is not True
    assert all(c["status"] == "unavailable" for c in primary["commands"])


def test_agent_evidence_still_uses_all_existing_physical_checks(fixture):
    base, task, spec, run = marked(fixture, "agent")
    result = evaluate(task, spec, run, base, required_execution_origin="agent")
    assert result["status"] == "complete"
    # No intervention observation: successful commands cannot invent autonomy.
    assert result["autonomous_success"] is None
    (base / "TEST.xml").write_text("broken evidence")
    assert evaluate(task, spec, run, base, required_execution_origin="agent")["status"] != "complete"


def test_changing_run_label_cannot_import_a_replay_invocation(fixture):
    base, task, spec, run = marked(fixture, "independent_replay")
    run["execution_origin"] = "agent"
    result = evaluate(task, spec, run, base, required_execution_origin="agent")
    assert result["status"] == "unavailable"
    assert any("Invocation execution origin" in e for e in result["record_errors"])
    assert evaluate(task, spec, run, base)["status"] == "unavailable"


def test_historical_origin_is_unknown_for_new_primary_but_still_replayable(fixture):
    task, spec, _, run, base, score = fixture
    score()
    assert evaluate(task, spec, run, base)["status"] == "complete"
    strict = evaluate(task, spec, run, base, required_execution_origin="agent")
    assert strict["status"] == "unavailable"
    assert strict["execution_origin"] is None


def test_no_invocations_cannot_be_completed_by_evaluation(fixture):
    base, task, spec, run = marked(fixture, "agent")
    run["invocations"] = []
    before = {p.relative_to(base): p.read_bytes() for p in base.rglob("*") if p.is_file()}
    result = evaluate(task, spec, run, base, required_execution_origin="agent")
    after = {p.relative_to(base): p.read_bytes() for p in base.rglob("*") if p.is_file()}
    assert result["status"] == "unavailable"
    assert after == before


@pytest.mark.parametrize("status,code", [("running", None), ("unavailable", None), ("completed", 1)])
def test_pending_or_failed_command_cannot_pass(fixture, status, code):
    task, spec, record, run, base, _ = fixture
    base, task, spec, run = marked(fixture, "agent")
    record = {**record, "execution_origin": "agent", "status": status, "exit_code": code}
    run["invocations"] = [write(base, "invocation-origin.json", record)]
    assert evaluate(task, spec, run, base, required_execution_origin="agent")["status"] != "complete"


def test_invalid_scoring_boundary_is_an_error(fixture):
    task, spec, _, run, base, _ = fixture
    with pytest.raises(ValueError, match="execution origin"):
        evaluate(task, spec, run, base, required_execution_origin="automatic")


def test_offline_cli_cannot_score_replay_as_agent_execution(fixture):
    base, task, spec, run = marked(fixture, "independent_replay")
    for name, value in [("task.json", task), ("requirements.json", spec), ("run.json", run)]:
        write(base, name, value)
    output = base / "primary-score.json"
    command = [sys.executable, "-m", "sag.benchmark", "score", "--task", str(base / "task.json"),
               "--requirements", str(base / "requirements.json"), "--run", str(base / "run.json"),
               "--required-execution-origin", "agent", "--output", str(output)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["status"] == "unavailable"


@pytest.mark.parametrize("origin,expected", [("agent", True), ("independent_replay", None)])
def test_observed_zero_interventions_does_not_make_replay_autonomous(fixture, origin, expected):
    base, task, spec, run = marked(fixture, origin)
    run["telemetry"] = write(base, "telemetry.json", {
        "source": "trusted_noninteractive_runner", "run_id": "run-1", "coverage": "complete",
        "runner_version": "v2", "started_at": "2026-09-26T12:00:00Z", "finished_at": "2026-09-26T12:01:00Z",
        "events": [], "human_interventions": 0,
        "input_policy": {"stdin": "DEVNULL", "external_channels": "enforced"}})
    result = evaluate(task, spec, run, base, required_execution_origin=origin)
    assert result["status"] == "complete"
    assert result["human_interventions"] == 0
    assert result["autonomous_success"] is expected
