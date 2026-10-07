import copy
import shutil

import pytest

from scripts.benchmark_execution_evidence import agent_evidence, require_capture_support
from sag.benchmark.evaluator import evaluate
from sag.benchmark.requirements import aggregate, evaluation_identity
from test_benchmark_requirement_evaluator import fixture, write


def native_source(fixture, tmp_path):
    task, spec, record, run, source, _ = fixture
    campaign = tmp_path / "attempt"
    session = campaign / "sag-native/logs/session_example"
    shutil.copytree(source, session)
    path = "requirements-evidence/example/run.json"
    run = {**copy.deepcopy(run), "execution_origin": "agent",
           "producer": "sag_authorized_receipt_adapter",
           "run_pin": write(session, "run-pin.json", {
               "run_id": "run-1", "target_repo_sha": task["sha"]})}
    run["invocations"] = [write(session, "invocation.json", {**record, "execution_origin": "agent"})]
    write(session, path, run)
    return campaign, session, task, spec, run, path


def test_primary_selector_uses_native_physical_evidence_without_execution(fixture, tmp_path):
    campaign, session, task, spec, run, _ = native_source(fixture, tmp_path)
    before = {str(p): p.read_bytes() for p in campaign.rglob("*") if p.is_file()}
    base, selected = agent_evidence(campaign, "sag", task, spec, "run-1")
    assert base == session and selected == run
    score = evaluate(task, spec, selected, base, required_execution_origin="agent")
    assert score["status"] == "complete"
    assert before == {str(p): p.read_bytes() for p in campaign.rglob("*") if p.is_file()}


@pytest.mark.parametrize("mutation", ["origin", "producer", "run_id", "task", "invocation", "pin"])
def test_selector_rejects_foreign_or_replay_sources(fixture, tmp_path, mutation):
    campaign, session, task, spec, run, path = native_source(fixture, tmp_path)
    if mutation == "origin": run["execution_origin"] = "independent_replay"
    elif mutation == "producer": run["producer"] = "portable-requirements-v2"
    elif mutation == "run_id": run["run_id"] = "other"
    elif mutation == "task": run["evaluation_identity"] = {}
    elif mutation == "pin":
        run["run_pin"] = write(session, "bad-pin.json", {"run_id": "other", "target_repo_sha": task["sha"]})
    else:
        record = {**fixture[2], "execution_origin": "independent_replay"}
        run["invocations"] = [write(session, "invocation.json", record)]
    write(session, path, run)
    with pytest.raises(ValueError): agent_evidence(campaign, "sag", task, spec, "run-1")


def test_missing_native_source_never_uses_a_public_replay(fixture, tmp_path):
    task, spec, _, _, _, _ = fixture
    write(tmp_path, "records/score.json", {"status": "complete"})
    with pytest.raises(FileNotFoundError):
        agent_evidence(tmp_path, "claude", task, spec, "run-1")


@pytest.mark.parametrize("arm", ["claude", "opencode"])
def test_unqualified_baseline_stops_before_model_or_container_work(arm):
    from scripts.run_portable_harness import run
    from types import SimpleNamespace

    # No paths, credentials or Docker settings are needed to reject admission.
    with pytest.raises(ValueError, match="no container or model request"):
        run(SimpleNamespace(arm=arm))
    require_capture_support("sag")


def test_aggregate_cannot_mix_primary_and_replay_scores(fixture):
    task, spec, _, _, _, score = fixture
    first = score()
    second_spec = {**spec, "project_id": "second"}
    second = {**first, "project_id": "second", "evaluation_identity": evaluation_identity(second_spec)}
    first.update(execution_policy="agent-execution-v1", execution_origin="agent", required_execution_origin="agent")
    second.update(execution_policy="agent-execution-v1", execution_origin="independent_replay", required_execution_origin="independent_replay")
    with pytest.raises(ValueError, match="execution boundaries"):
        aggregate({spec["project_id"]: spec, "second": second_spec}, [first, second])
