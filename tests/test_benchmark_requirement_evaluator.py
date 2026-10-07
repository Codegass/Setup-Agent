"""Adversarial protocol fixtures, independent of an LLM or a Java installation."""

import copy
import json
import subprocess
import zipfile

import pytest

from sag.agent.worktree_evidence import WorktreeEvidenceRecorder
from sag.benchmark.evaluator import evaluate
from sag.benchmark.requirements import (
    aggregate,
    canonical_digest,
    evaluation_identity,
    file_digest,
    load_requirements,
    repository_artifact_path,
)


def write(base, name, value):
    path = base / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    return {"path": name, "sha256": file_digest(path)}


def requirement(id, kind, goal, **extra):
    return {
        "id": id,
        "step_id": "verify",
        "kind": kind,
        "subtype": "unit" if kind == "test" else None,
        "module": "demo",
        "scope": {"status": "declared", "module_path": "."},
        "depends_on": [],
        "dependencies_complete": True,
        "validation": {
            "rule": (
                "junit" if kind == "test" else "artifact" if kind == "package" else "native_goal"
            ),
            "goals": [goal],
            "plan_resolved": True,
            "position": 0,
        },
        **extra,
    }


def banner(goal):
    plugin, name = goal.split(":")
    return f"[INFO] --- maven-{plugin}-plugin:1.0:{name} (default) @ demo ---\n"


@pytest.fixture
def fixture(tmp_path):
    checkout, base = tmp_path / "checkout", tmp_path / "records"
    checkout.mkdir()
    base.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    (checkout / "pom.xml").write_text("<project/>")
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    sha = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    task = {
        "schema_version": 1,
        "repo": "example/demo",
        "sha": sha,
        "steps": [
            {
                "id": "verify",
                "runner": "maven",
                "argv": ["mvn", "verify"],
                "cwd": ".",
                "java_major": 17,
            }
        ],
    }
    rows = [
        requirement("compile", "compile", "compiler:compile"),
        requirement("tests", "test", "surefire:test"),
    ]
    rows[1].update(depends_on=["compile"])
    rows[1]["validation"]["position"] = 1
    spec = {
        "schema_version": 2,
        "policy_version": "ci-requirements-v2",
        "project_id": "demo",
        "task_sha256": canonical_digest(task),
        "preconditions": [{"id": "worktree_integrity"}, {"id": "runtime_conformance"}],
        "annotation_completeness": {"status": "complete"},
        "requirements": rows,
    }
    log = (
        banner("compiler:compile")
        + "[INFO] Compiling 2 source files\n"
        + banner("surefire:test")
        + "[INFO] BUILD SUCCESS\n"
    )
    record = {
        "invocation_id": "inv-1",
        "run_id": "run-1",
        "evaluation_identity": evaluation_identity(spec),
        "step_id": "verify",
        "sequence": 0,
        "commit": sha,
        **task["steps"][0],
        "log": write(base, "log.txt", log),
        "log_complete": True,
        "status": "completed",
        "exit_code": 0,
        "effective_execution": {
            "inputs_complete": True,
            "serial": True,
            "wrapper_reviewed": True,
            "argv": ["mvn", "verify"],
            "maven_config": [],
            "maven_args": [],
        },
        "runtime": {
            "launcher_probe": {
                **write(base, "runtime.txt", "Apache Maven 3.9.16\nJava version: 17.0.12\n"),
                "exit_code": 0,
                "executable": "/tools/maven/bin/mvn",
            }
        },
        "reports_collection_complete": True,
        "reports": [
            {
                **write(
                    base,
                    "TEST.xml",
                    '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="a"/></testsuite>',
                ),
                "module": "demo",
                "fresh": True,
            }
        ],
    }
    recorder = WorktreeEvidenceRecorder(base, run_id="run-1", project_root=str(checkout))
    for boundary in ("task_start", "acceptance_before", "acceptance_after", "evidence_close"):
        recorder.capture(
            boundary, invocation_id="inv-1" if boundary.startswith("acceptance_") else None
        )
    run = {
        "agent": "fixture",
        "run_id": "run-1",
        "evaluation_identity": evaluation_identity(spec),
        "commit": sha,
        "repo": task["repo"],
        "worktree": [
            {"path": str(p.relative_to(base)), "sha256": file_digest(p)}
            for p in (base / "worktree-evidence").glob("*.json")
        ],
    }

    def score():
        record["evaluation_identity"] = run["evaluation_identity"] = evaluation_identity(spec)
        run["invocations"] = [write(base, "invocation.json", record)]
        return evaluate(task, spec, run, base)

    return task, spec, record, run, base, score


def test_test_only_task_needs_no_jar_and_historical_human_count_unknown(fixture):
    score = fixture[-1]()
    assert score["status"] == "complete"
    assert score["groups"]["build"] == score["groups"]["test"] == "complete"
    assert score["human_interventions"] is None
    assert score["autonomous_success"] is None
    assert score["ci_scope_attainment"] is None


@pytest.mark.parametrize("identity", [None, "", [], {}])
def test_malformed_invocation_identity_is_unavailable(fixture, identity):
    fixture[2]["invocation_id"] = identity
    score = fixture[-1]()
    assert score["status"] == "unavailable"


def test_compound_maven_extension_keeps_coordinate_path_binding():
    item = {
        "coordinates": {"group_id": "org.example", "artifact_id": "demo", "version": "1"},
        "extension": "spdx.json",
        "classifier": None,
        "repository_relative_path": "org/example/demo/1/demo-1.spdx.json",
    }
    assert repository_artifact_path(item) == item["repository_relative_path"]
    item["extension"] = "../json"
    with pytest.raises(ValueError):
        repository_artifact_path(item)


def test_javadoc_failure_keeps_compile_test_passed(fixture):
    _, spec, record, _, base, score = fixture
    spec["requirements"].append(requirement("docs", "documentation", "javadoc:javadoc"))
    log = (
        (base / "log.txt")
        .read_text()
        .replace(
            "[INFO] BUILD SUCCESS\n",
            banner("javadoc:javadoc")
            + "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-javadoc-plugin:1.0:javadoc (default) on project demo: bad doc\n[INFO] BUILD FAILURE\n",
        )
    )
    record.update(log=write(base, "log.txt", log), exit_code=1)
    out = score()
    assert out["status"] == "incomplete"
    assert [r["status"] for r in out["requirements"]] == ["passed", "passed", "failed"]
    assert out["known_blockers"] == ["docs"]


@pytest.mark.parametrize(
    "constraint",
    [
        None,
        "fae",
        "fn",
        "quiet",
        "truncated",
        "wrapper_unknown",
        "config_unknown",
        "forked",
        "unresolved_plan",
        "different_module",
    ],
)
def test_compiler_failure_not_run_requires_all_fail_fast_conditions(fixture, constraint):
    _, spec, record, _, base, score = fixture
    log = (
        banner("compiler:compile")
        + "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:1.0:compile (default) on project demo: bad source\n[INFO] BUILD FAILURE\n"
    )
    record.update(log=write(base, "log.txt", log), exit_code=1, reports=[])
    if constraint in {"fae", "fn", "quiet"}:
        record["effective_execution"]["maven_config"] = [
            {"fae": "--fail-at-end", "fn": "-fn", "quiet": "-q"}[constraint]
        ]
    elif constraint == "truncated":
        record["log_complete"] = False
    elif constraint == "wrapper_unknown":
        record["effective_execution"]["wrapper_reviewed"] = False
    elif constraint == "config_unknown":
        record["effective_execution"]["inputs_complete"] = False
    elif constraint == "forked":
        record["log"] = write(base, "log.txt", "[INFO] >>> fork >>>\n" + log)
    elif constraint == "unresolved_plan":
        spec["requirements"][1]["validation"]["plan_resolved"] = False
    elif constraint == "different_module":
        spec["requirements"][1]["module"] = "other"
    out = score()
    assert out["requirements"][1]["status"] == ("not_run" if constraint is None else "unavailable")


@pytest.mark.parametrize(
    "mutation", ["missing", "sources", "tests", "stale", "unreadable", "valid"]
)
def test_main_jar_role_identity_and_freshness(fixture, mutation):
    _, spec, record, _, base, score = fixture
    spec["requirements"].append(
        requirement(
            "package",
            "package",
            "jar:jar",
            expectations={
                "artifacts": [
                    {"path": "target/demo.jar", "role": "main", "module": "demo", "format": "jar"}
                ]
            },
        )
    )
    record["log"] = write(
        base,
        "log.txt",
        (base / "log.txt")
        .read_text()
        .replace("[INFO] BUILD SUCCESS", banner("jar:jar") + "[INFO] BUILD SUCCESS"),
    )
    jar = base / "demo.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("A.class", b"bytes")
    if mutation == "unreadable":
        jar.write_bytes(b"not a jar")
    a = {
        "path": "demo.jar",
        "sha256": file_digest(jar),
        "producer_relative_path": "target/demo.jar",
        "role": "main",
        "module": "demo",
        "fresh": True,
    }
    if mutation in {"sources", "tests"}:
        a["role"] = mutation
    if mutation == "stale":
        a["fresh"] = False
    record["artifacts"] = [] if mutation == "missing" else [a]
    out = score()
    assert out["status"] == ("complete" if mutation == "valid" else "unavailable")


def test_wrong_jdk_even_zero_exit_is_incomplete(fixture):
    _, _, record, _, base, score = fixture
    record["runtime"]["launcher_probe"].update(
        write(base, "runtime.txt", "Apache Maven 3.9.16\nJava version: 8.0.402\n")
    )
    out = score()
    assert out["status"] == "incomplete"
    assert out["preconditions"][1]["status"] == "failed"


@pytest.mark.parametrize(
    "change", ["failed_untracked_probe", "tracked_changed", "ordinary_untracked"]
)
def test_worktree_raw_probes_not_boolean_assumptions(fixture, change):
    _, _, _, run, base, score = fixture
    ref = run["worktree"][-1]
    snapshot = json.loads((base / ref["path"]).read_text())
    if change == "failed_untracked_probe":
        snapshot["probes"]["untracked"]["exit_code"] = 1
    elif change == "tracked_changed":
        snapshot["probes"]["tracked"]["stdout"] = write(base, "tracked.bin", "pom.xml\0")
    else:
        snapshot["probes"]["untracked"]["stdout"] = write(base, "untracked.bin", "agent-note.txt\0")
    run["worktree"][-1] = write(base, ref["path"], snapshot)
    assert (
        score()["status"]
        == {
            "failed_untracked_probe": "unavailable",
            "tracked_changed": "incomplete",
            "ordinary_untracked": "complete",
        }[change]
    )


def test_mixed_attempt_bytes_cannot_make_false_failure(fixture):
    task, spec, record, run, base, _ = fixture
    record.update(run_id="another-run", exit_code=1)
    run["invocations"] = [write(base, "invocation.json", record)]
    assert evaluate(task, spec, run, base)["status"] == "unavailable"


def test_group_planned_denominator_and_metadata_identity(fixture):
    _, spec, _, _, _, score = fixture
    out = score()
    other = copy.deepcopy(spec)
    other["project_id"] = "missing"
    aggregate_result = aggregate({"demo": spec, "missing": other}, [out])
    assert aggregate_result["task_success"] == {
        "complete": 1,
        "incomplete": 0,
        "unavailable": 1,
        "denominator": 2,
        "rate": 0.5,
    }
    spec["requirements"][0]["scope"]["status"] = "unresolved"
    with pytest.raises(ValueError, match="different requirement versions"):
        aggregate({"demo": spec}, [out])


def test_requirements_schema_rejects_duplicate_and_optional(fixture):
    task, spec, _, _, base, _ = fixture
    path = base / "requirements.json"
    path.write_text('{"schema_version": 2, "schema_version": 2}')
    with pytest.raises(ValueError, match="Duplicate"):
        load_requirements(path)
    spec["requirements"][0]["optional"] = True
    write(base, "requirements.json", spec)
    with pytest.raises(ValueError, match="optional"):
        load_requirements(path, task)


def test_telemetry_requires_all_channels_observed(fixture):
    _, _, _, run, base, score = fixture
    telemetry = {
        "source": "trusted_noninteractive_runner",
        "run_id": "run-1",
        "coverage": "complete",
        "runner_version": "v2",
        "started_at": "2026-09-22T00:00:00Z",
        "finished_at": "2026-09-22T00:01:00Z",
        "events": [],
        "human_interventions": 0,
        "input_policy": {"stdin": "DEVNULL", "external_channels": "enforced"},
    }
    run["telemetry"] = write(base, "telemetry.json", telemetry)
    assert score()["autonomous_success"] is True
    telemetry["input_policy"]["external_channels"] = "unobserved"
    run["telemetry"] = write(base, "telemetry.json", telemetry)
    assert score()["autonomous_success"] is None


def test_same_normalized_evidence_is_independent_of_agent_name(fixture):
    _, _, _, run, _, score = fixture
    run["agent"] = "sag"
    left = score()
    run["agent"] = "another_coding_agent"
    right = score()
    left.pop("agent")
    right.pop("agent")
    assert left == right


def test_failed_attempt_costs_and_all_model_roles_remain_in_aggregate(fixture):
    _, spec, record, run, base, score = fixture
    record["exit_code"] = 1
    calls = [
        {"role": role, "input_tokens": 10, "output_tokens": 5}
        for role in ("actor", "advisor", "summary", "retry")
    ]
    telemetry = {
        "source": "trusted_runner",
        "run_id": "run-1",
        "model_calls": calls,
        "model_calls_complete": True,
        "unattended_seconds": 12.5,
    }
    run["telemetry"] = write(base, "costs.json", telemetry)
    out = score()
    assert out["status"] == "incomplete"
    assert out["total_tokens"] == 60
    summary = aggregate({"demo": spec}, [out])
    assert summary["costs_including_failures"]["total_tokens"]["complete_cohort_sum"] == 60
    telemetry["model_calls_complete"] = False
    run["telemetry"] = write(base, "costs.json", telemetry)
    out = score()
    assert out["known_tokens"] == 60 and out["total_tokens"] is None


def test_paired_subset_does_not_replace_planned_denominator(fixture):
    from sag.benchmark.requirements import paired_comparison

    _, spec, _, run, _, score = fixture
    left = score()
    run["agent"] = "comparison"
    right = score()
    test_result = next(r for r in right["requirements"] if r["id"] == "tests")
    test_result["status"] = "unavailable"
    right["status"] = "unavailable"
    out = paired_comparison({"demo": spec}, [left], [right])
    assert out["left_all_tasks"]["task_success"]["denominator"] == 1
    assert out["right_all_tasks"]["task_success"]["denominator"] == 1
    assert out["paired_conditional_groups"]["compile"]["both_observable"] == 1
    assert out["paired_conditional_groups"]["test"]["both_observable"] == 0
