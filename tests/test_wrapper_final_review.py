"""Independent launcher-closure review; fake cached Maven, no Java or network."""

import base64
from copy import deepcopy
import os
import shlex

import pytest

from sag.agent.acceptance_task import AcceptanceTask
from sag.benchmark.sag_adapter import _command_matches
from sag.benchmark.wrapper_review import capture_request, capture_wrapper_inputs, snapshot_inputs
from test_benchmark_wrapper_review import fixture, portable, sag


def test_absent_review_never_becomes_known_after_successful_command(fixture, tmp_path):
    root, task, spec, env, executable = fixture
    del spec["steps"][0]["launcher_review"]
    _, invocation, score = portable(fixture, tmp_path)
    assert invocation["exit_code"] == 0
    assert invocation["effective_execution"]["wrapper_reviewed"] is False
    assert [row["status"] for row in score["requirements"]] == ["unavailable", "unavailable"]
    _, _, verified, completed = sag(fixture, tmp_path)
    assert completed.returncode == 0
    assert verified["inputs_complete"] is False
    assert verified["wrapper_reviewed"] is False


def test_environment_selection_change_rejects_unchanged_wrapper(fixture, tmp_path):
    root, task, spec, env, executable = fixture

    def alter_environment():
        # The same working executable is still discoverable; this is evidence
        # of changed selection inputs, not a simulated command failure.
        env["PATH"] = "/unused-review-path:" + os.environ["PATH"]

    _, _, verified, completed = sag(fixture, tmp_path, alter_environment)
    assert completed.returncode == 0
    assert verified["inputs_complete"] is False
    assert verified["wrapper_reviewed"] is False


@pytest.mark.parametrize("changed", ["head", "tracked", "pinned", "source", "exit", "argv"])
def test_raw_git_witness_is_revalidated_independently(fixture, changed):
    root, task, spec, env, executable = fixture
    step = task["steps"][0]
    review = spec["steps"][0]["launcher_review"]
    request = capture_request(task, step, review, root, "r1", "i1", "acceptance_before", env)
    snapshot = capture_wrapper_inputs(request)
    assert snapshot_inputs(review, task, step, snapshot, "r1", "i1", "acceptance_before")["serial"]
    corrupted = deepcopy(snapshot)
    if changed == "exit":
        corrupted["probes"][0]["exit_code"] = 1
    elif changed == "argv":
        corrupted["probes"][0]["argv"][-1] = "other-revision"
    else:
        index = {"head": 0, "tracked": 1, "pinned": 2, "source": 3}[changed]
        corrupted["probes"][index]["stdout"] = base64.b64encode(b"different bytes").decode()
    with pytest.raises(ValueError, match="revision"):
        snapshot_inputs(review, task, step, corrupted, "r1", "i1", "acceptance_before")


@pytest.mark.parametrize("argv", [["mvn", "clean", "test"], ["./mvnw", "clean", "test", "-DskipTests"]])
def test_sag_exact_receipt_gate_rejects_different_launcher_or_arguments(fixture, argv):
    root, task, spec, env, executable = fixture
    step = AcceptanceTask.model_validate(task).steps[0]
    receipt = {"actual_cwd": str(root), "argv": shlex.join(argv)}
    assert _command_matches(step, {**receipt, "argv": "./mvnw clean test"}, str(root))
    assert _command_matches(step, receipt, str(root)) is False
