"""Exact CI environment inputs are evidence, not a blanket -D exemption."""
import hashlib

import pytest

from sag.benchmark import jvm_inputs
from sag.benchmark.evaluator import evaluate
from sag.benchmark.recorder import Recorder
from sag.benchmark.sag_observer import SAGRequirementObserver
from sag.benchmark.wrapper_review import snapshot_inputs
from test_benchmark_wrapper_review import fixture, Control
from test_benchmark_recorder import setup


def declaration(task, step, base):
    value = "-Xmx256m -Dhttp.keepAlive=false"
    raw = ("MAVEN_OPTS: " + value + "\n").encode()
    path = base / "official-job.log"
    path.write_bytes(raw)
    return {
        "policy": "pinned-jvm-environment-v1", "source_commit": task["sha"],
        "repository": task["repo"], "step_id": step["id"], "argv": step["argv"],
        "reviewed_by": "test fixture", "review_basis": "Synthetic explicit task environment",
        "values": {"MAVEN_OPTS": {"value": value, "source_line": 1,
            "source": {"path": path.name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)},
            "properties": [{"argument": "-Dhttp.keepAlive=false", "effect": "runtime_property",
                            "basis": "Fixture transport setting; no test selection mutation"}]}},
    }


@pytest.mark.parametrize("mutation", [None, "missing", "extra", "different", "other_variable", "missing_review"])
def test_both_direct_collectors_check_the_complete_frozen_value(setup, tmp_path, mutation):
    root, make = setup
    def maven(task):
        task["steps"][0].update(runner="maven", argv=["mvn", "test"], java_major=17)
    recorder = make(task_change=maven)
    step = recorder.task["steps"][0]
    review = declaration(recorder.task, step, tmp_path)
    env = {"MAVEN_SKIP_RC": "true", "MAVEN_OPTS": review["values"]["MAVEN_OPTS"]["value"]}
    recorder.spec["steps"] = [{"step_id": step["id"], "environment": dict(env), "jvm_environment_review": review}]
    if mutation == "missing": env.pop("MAVEN_OPTS")
    elif mutation == "extra": env["MAVEN_OPTS"] += " -DskipTests=true"
    elif mutation == "different": env["MAVEN_OPTS"] = env["MAVEN_OPTS"].replace("false", "true")
    elif mutation == "other_variable": env["JAVA_TOOL_OPTIONS"] = "-DskipTests=true"
    elif mutation == "missing_review": recorder.spec["steps"][0].pop("jvm_environment_review")
    out = recorder.base / "input-observation"; out.mkdir(parents=True)
    portable = recorder._effective_execution(step, env, out)
    observer = SAGRequirementObserver(tmp_path / "session", "run-1", str(root), recorder.task, recorder.spec, lambda *_: None)
    live = observer._inputs(step, {"configs": [], "environment_observed": True,
                                 "runtime_inputs": env, "startup_rc_present": False})
    assert portable["inputs_complete"] == live["inputs_complete"] == (mutation is None)


@pytest.mark.parametrize("mutation", [None, "missing", "extra", "different", "other_variable", "missing_review"])
def test_wrapper_snapshot_and_replay_use_exact_reviewed_environment(fixture, tmp_path, mutation):
    root, task, spec, env, _ = fixture
    step = task["steps"][0]
    review = declaration(task, step, tmp_path)
    expected = review["values"]["MAVEN_OPTS"]["value"]
    spec["steps"][0].update(environment={"MAVEN_OPTS": expected}, jvm_environment_review=review)
    env["MAVEN_OPTS"] = expected
    if mutation == "missing": env.pop("MAVEN_OPTS")
    elif mutation == "extra": env["MAVEN_OPTS"] += " -DskipTests=true"
    elif mutation == "different": env["MAVEN_OPTS"] = expected.replace("false", "true")
    elif mutation == "other_variable": env["JAVA_TOOL_OPTIONS"] = "-DskipTests=true"
    elif mutation == "missing_review": spec["steps"][0].pop("jvm_environment_review")
    recorder = Recorder(task=task, requirements=spec, repo_root=root, records=tmp_path / "records", run_id="r1")
    out = recorder.base / "probe"; out.mkdir(parents=True)
    observed, _ = recorder._wrapper_snapshot(step, env, out, "i1", "acceptance_before")
    args = (spec["steps"][0]["launcher_review"], task, step, observed, "r1", "i1", "acceptance_before")
    if mutation is None:
        assert snapshot_inputs(*args, environment_review=review)["maven_args"] == []
    else:
        with pytest.raises(ValueError):
            snapshot_inputs(*args, environment_review=jvm_inputs.environment_review_for(spec, step))
    observer = SAGRequirementObserver(tmp_path / "sag", "r1", str(root), task, spec, Control(env).execute)
    out = observer.base / "probe"; out.mkdir(parents=True)
    live = observer._wrapper_inputs(step, out, "i1", "acceptance_before")
    assert live["inputs_complete"] is (mutation is None), live


@pytest.mark.parametrize("mutation", [None, "missing_source", "corrupt_source", "missing_review"])
def test_frozen_options_do_not_replace_their_archived_source(fixture, tmp_path, mutation):
    root, task, spec, env, _ = fixture
    step = task["steps"][0]
    review = declaration(task, step, tmp_path)
    spec["steps"][0].update(environment={"MAVEN_OPTS": review["values"]["MAVEN_OPTS"]["value"]}, jvm_environment_review=review)
    if mutation == "missing_review": spec["steps"][0].pop("jvm_environment_review")
    recorder = Recorder(task=task, requirements=spec, repo_root=root, records=tmp_path / "records", run_id="r1")
    recorder.start(); recorder.step(step["id"], timeout=10); run = recorder.close()
    if mutation == "missing_source": (tmp_path / "official-job.log").unlink()
    elif mutation == "corrupt_source": (tmp_path / "official-job.log").write_text("different")
    result = evaluate(task, spec, run, recorder.base, ci_source_base=tmp_path)
    assert (result["status"] == "complete") == (mutation is None), result


@pytest.mark.parametrize("mutation", ["other_commit", "other_command", "property_review_missing", "wrong_line", "argfile"])
def test_environment_review_requires_its_exact_task_and_source(fixture, tmp_path, mutation):
    _, task, _, _, _ = fixture
    step = task["steps"][0]; review = declaration(task, step, tmp_path)
    value = review["values"]["MAVEN_OPTS"]
    if mutation == "other_commit": review["source_commit"] = "b" * 40
    elif mutation == "other_command": review["argv"] = ["./mvnw", "test", "-DskipTests"]
    elif mutation == "property_review_missing": value["properties"] = []
    elif mutation == "wrong_line": value["source_line"] = 2
    elif mutation == "argfile": value["value"] = "@unreviewed.args"
    with pytest.raises(ValueError):
        jvm_inputs.validate_environment_review(review, task, step, base=tmp_path)
