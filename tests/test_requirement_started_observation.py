"""Actual entry, resolved outcomes and absent evidence are different metrics."""

from copy import deepcopy

import pytest

from sag.benchmark.evaluator import evaluate, native_requirement, requirement_started
from sag.benchmark.native_evidence import maven_events
from sag.benchmark.requirements import aggregate, canonical_digest, evaluation_identity
from test_benchmark_requirement_evaluator import fixture, banner, requirement, write


def by_id(score):
    return {r["id"]: r for r in score["requirements"]}


def test_native_entry_survives_missing_completion_and_output_proof(fixture):
    _, _, record, _, base, score = fixture
    record.update(status="timeout", exit_code=None, log_complete=False,
                  log=write(base, "log.txt", banner("compiler:compile") + "compiling...\n"))
    rows = by_id(score())
    assert rows["compile"]["status"] == "unavailable" and rows["compile"]["started"] is True
    assert rows["tests"]["status"] == "unavailable" and rows["tests"]["started"] is None


@pytest.mark.parametrize("exit_code", [-9, None, 0])
def test_timeout_is_an_executed_incomplete_command_not_a_skipped_command(fixture, exit_code):
    _, _, record, _, base, score = fixture
    record.update(status="timeout", exit_code=exit_code, log_complete=True,
                  log=write(base, "log.txt", banner("compiler:compile") + "compiling...\n"))
    outcome = score()
    command = outcome["commands"][0]
    assert command["status"] == "failed"
    assert command["reason"] == "execution_timeout"
    assert command["execution_status"] == "timeout" and command["started"] is True
    assert outcome["status"] == "incomplete"
    assert by_id(outcome)["compile"]["status"] == "unavailable"
    assert by_id(outcome)["tests"]["status"] == "unavailable"


def test_timeout_with_unbound_log_does_not_claim_observed_execution(fixture):
    _, _, record, _, base, score = fixture
    record.update(status="timeout", exit_code=-9)
    (base / record["log"]["path"]).write_text("changed")
    command = score()["commands"][0]
    assert command["status"] == "unavailable"
    assert command.get("started") is None


def test_junit_read_failure_cannot_erase_observed_test_start(fixture):
    _, _, record, _, base, score = fixture
    record["reports"][0].update(write(base, "TEST.xml", "<broken"))
    result = by_id(score())["tests"]
    assert result["status"] == "unavailable" and result["started"] is True
    assert result["evidence_refs"] == [record["log"]]


@pytest.mark.parametrize("skip", ["Tests are skipped.", "No tests to run."])
def test_explicit_maven_skip_does_not_count_as_actual_requirement_start(fixture, skip):
    _, _, record, _, base, score = fixture
    record["log"] = write(base, "log.txt", banner("compiler:compile") + "[INFO] Compiling 2 source files\n"
                          + banner("surefire:test") + "[INFO] " + skip + "\n[INFO] BUILD SUCCESS\n")
    result = by_id(score())["tests"]
    assert result["status"] == "not_run" and result["started"] is False


def test_confirmed_same_module_failfast_has_observed_nonstart(fixture):
    _, _, record, _, base, score = fixture
    record.update(exit_code=1, reports=[], log=write(base, "log.txt", banner("compiler:compile")
        + "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:1.0:compile (default) on project demo: bad source\n[INFO] BUILD FAILURE\n"))
    rows = by_id(score())
    assert rows["compile"]["started"] is True
    assert rows["tests"]["status"] == "not_run" and rows["tests"]["started"] is False


@pytest.mark.parametrize("problem", ["missing_log", "wrong_identity", "wrong_command"])
def test_unbound_or_missing_evidence_does_not_observe_start_or_nonstart(fixture, problem):
    _, _, record, run, base, score = fixture
    if problem == "missing_log": record["log"]["path"] = "missing.log"
    elif problem == "wrong_identity": run["run_id"] = "foreign"
    else: record["argv"] = ["mvn", "different"]
    assert all(r["started"] is None for r in score()["requirements"])


@pytest.mark.parametrize("mode,expected", [("real_first", True), ("first_skip_only", None), ("all_skipped", False)])
def test_compound_native_requirement_starts_when_any_member_really_enters(fixture, mode, expected):
    _, _, record, _, base, _ = fixture
    row = requirement("compound", "quality_check", "javadoc:javadoc")
    row["validation"].update(goals=["javadoc:javadoc", "checkstyle:check"], native_bindings=[
        {"goal": goal, "execution": "default", "occurrence": 0, "position": position}
        for position, goal in enumerate(["javadoc:javadoc", "checkstyle:check"])
    ])
    text = banner("javadoc:javadoc")
    if mode != "real_first": text += "[INFO] Skipping javadoc generation\n"
    if mode == "all_skipped": text += banner("checkstyle:check") + "[INFO] Skipping checkstyle execution\n"
    events = maven_events(text, terminal=False, serial=True)
    result = native_requirement(row, record, base, text, events)
    assert result["started"] is expected
    assert result["status"] != "passed"


@pytest.mark.parametrize("state,expected", [
    ("", True), ("FAILED", True), ("SKIPPED", False), ("NO-SOURCE", False),
    ("UP-TO-DATE", False), ("FROM-CACHE", False), (None, None),
])
def test_gradle_start_does_not_depend_on_success_or_serial_completion(fixture, state, expected):
    _, _, record, _, base, _ = fixture
    row = requirement("test", "test", "surefire:test")
    row["validation"] = {"rule": "junit", "tasks": [":test"]}
    record.update(runner="gradle", status="timeout", exit_code=None, log_complete=False)
    text = "" if state is None else "> Task :test" + (" " + state if state else "") + "\n"
    result = native_requirement(row, record, base, text, [])
    assert result["started"] is expected
    assert result["status"] == "unavailable"


def test_compound_gradle_partial_skip_does_not_prove_whole_nonstart(fixture):
    _, _, record, _, _, _ = fixture
    row = requirement("test", "test", "surefire:test")
    row["validation"]["tasks"] = [":unitTest", ":integrationTest"]
    record["runner"] = "gradle"
    assert requirement_started(row, record, "> Task :unitTest SKIPPED\n", []) is None
    assert requirement_started(row, record, "> Task :unitTest SKIPPED\n> Task :integrationTest\n", []) is True


def test_actual_started_counts_do_not_reuse_conditional_reached(fixture):
    _, spec, record, _, base, score = fixture
    record["reports"] = []  # Native test entry remains, fresh output does not.
    first = score()
    assert by_id(first)["tests"]["started"] is True
    assert by_id(first)["tests"]["status"] == "unavailable"
    skipped = deepcopy(spec)
    skipped["project_id"] = "skipped"
    second = deepcopy(first)
    second.update(project_id="skipped", evaluation_identity=evaluation_identity(skipped), status="incomplete")
    by_id(second)["tests"].update(status="not_run", started=False)
    missing = deepcopy(spec)
    missing["project_id"] = "missing"
    out = aggregate({"demo": spec, "skipped": skipped, "missing": missing}, [first, second])
    c = out["conditional_requirements"]["test"]
    assert (c["planned"], c["started"], c["not_started"], c["started_unknown"], c["reached"]) == (3, 1, 1, 1, 0)
    assert c["conditional_pass_rate"] is None and c["prerequisites_satisfied"] == 2
    for group in (out["requirement_stage_success"]["test"], out["task_success_by_requirement_group"]["test"]):
        assert group["denominator"] == 3
        assert (group["started"], group["not_started"], group["started_unknown"]) == (1, 1, 1)


def test_started_group_has_one_project_vote_and_old_missing_fields_stay_unknown(fixture):
    _, spec, _, _, _, score = fixture
    first = score()
    for i in range(3):
        row = deepcopy(spec["requirements"][0])
        row["id"] = "another-compile-" + str(i)
        spec["requirements"].append(row)
        first["requirements"].append({"id": row["id"], "status": "passed", "started": True})
    first["evaluation_identity"] = evaluation_identity(spec)
    assert aggregate({"demo": spec}, [first])["conditional_requirements"]["compile"]["started"] == 1
    for row in first["requirements"]: row.pop("started", None)
    c = aggregate({"demo": spec}, [first])["conditional_requirements"]["compile"]
    assert c["started"] == c["not_started"] == 0 and c["started_unknown"] == 1


@pytest.mark.parametrize("state,mutation,expected", [
    ("completed", None, True), ("timeout", None, True), ("launch_failed", None, False),
    ("planned", None, None), ("unavailable", None, None),
    ("completed", "missing_lineage", None), ("completed", "wrong_argv", None),
    ("completed", "wrong_run", None), ("timeout", "changed_binary", None),
])
def test_native_started_requires_bound_actual_launch_and_verified_binary(fixture, state, mutation, expected):
    task, spec, producer, run, base, _ = fixture
    step = {"id": "native-run", "runner": "native", "argv": ["./target/program"], "cwd": ".", "java_major": None}
    task["steps"].append(step)
    row = requirement("native-test", "test", "unused:goal")
    row.update(step_id=step["id"], subtype="native", expectations={"executable": "target/program"})
    row["validation"] = {"rule": "native_exit"}
    spec["requirements"].append(row)
    spec["task_sha256"] = canonical_digest(task)
    identity = evaluation_identity(spec)
    producer["evaluation_identity"] = run["evaluation_identity"] = identity
    binary = write(base, "native-program", "frozen binary bytes")
    producer["artifacts"] = [{**binary, "producer_relative_path": "target/program", "fresh": True,
                              "role": "native_executable", "module": "demo"}]
    invocation = {**step, "step_id": step["id"], "invocation_id": "native-1", "run_id": run["run_id"],
                  "evaluation_identity": identity, "commit": task["sha"], "sequence": 1,
                  "status": state, "exit_code": -9 if state == "timeout" else 0 if state == "completed" else None,
                  "log_complete": state in {"completed", "timeout"}, "log": write(base, "native.log", "native observation"),
                  "native_executable": {**binary, "producer_relative_path": "target/program", "producer_invocation_id": "inv-1"}}
    if mutation == "missing_lineage": invocation.pop("native_executable")
    elif mutation == "wrong_argv": invocation["argv"] = ["./target/other"]
    elif mutation == "wrong_run": invocation["run_id"] = "other-run"
    elif mutation == "changed_binary": (base / binary["path"]).write_text("changed after observation")
    run["invocations"] = [write(base, "producer.json", producer), write(base, "native.json", invocation)]
    native = by_id(evaluate(task, spec, run, base))["native-test"]
    assert native["started"] is expected
    if state != "completed" or mutation:
        assert native["status"] != "passed"
