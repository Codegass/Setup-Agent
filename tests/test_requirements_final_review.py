"""Independent cross-adapter regressions found during protocol review."""

import json
import zipfile

import pytest

from sag.benchmark.evaluator import evaluate, native_requirement
from sag.benchmark.native_evidence import artifact_proof, maven_events
from sag.benchmark.recorder import normalize_task
from sag.benchmark.recorder import Recorder
from sag.benchmark.requirements import (
    canonical_digest,
    evaluation_identity,
    file_digest,
    validate_requirements,
)


def requirement(kind="documentation", rule="native_goal", goal="javadoc:javadoc"):
    return {
        "id": "r",
        "step_id": "step",
        "kind": kind,
        "module": "demo",
        "subtype": None,
        "scope": {"status": "declared", "module_path": "."},
        "depends_on": [],
        "dependencies_complete": True,
        "validation": {
            "rule": rule,
            "goals": [goal],
            "execution": "default-cli",
            "occurrence": 0,
            "plan_resolved": True,
            "position": 0,
        },
    }


def write(base, name, data):
    path = base / name
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return {"path": name, "sha256": file_digest(path)}


def invocation(base, log):
    return {
        "invocation_id": "i",
        "runner": "maven",
        "status": "completed",
        "exit_code": 0,
        "log_complete": True,
        "log": write(base, "log.txt", log),
    }


@pytest.mark.parametrize(
    "kind,goal,skip",
    [
        ("documentation", "javadoc:javadoc", "Skipping javadoc generation"),
        ("quality_check", "enforcer:enforce", "Skipping Rule Enforcement."),
    ],
)
def test_explicitly_skipped_native_check_never_gets_passed(tmp_path, kind, goal, skip):
    plugin, target = goal.split(":")
    text = f"[INFO] --- maven-{plugin}-plugin:3.0:{target} (default-cli) @ demo ---\n[INFO] {skip}\n[INFO] BUILD SUCCESS\n"
    record = invocation(tmp_path, text)
    result = native_requirement(
        requirement(kind, goal=goal),
        record,
        tmp_path,
        text,
        maven_events(text, terminal=True, serial=True),
    )
    assert result["status"] != "passed"


def test_install_repository_probe_with_preserved_show_version_is_usable(tmp_path):
    text = "[INFO] --- maven-install-plugin:3.1:install (default-cli) @ demo ---\n[INFO] BUILD SUCCESS\n"
    record = invocation(tmp_path, text)
    coords = {"group_id": "org.example", "artifact_id": "demo", "version": "1"}
    artifact = {
        "module": "demo",
        "role": "installed_pom",
        "coordinates": coords,
        "classifier": None,
        "extension": "pom",
        "repository_relative_path": "org/example/demo/1/demo-1.pom",
    }
    record["artifacts"] = [
        {**artifact, **write(tmp_path, "installed.pom", "<project/>"), "fresh": True}
    ]
    record["local_repository"] = {
        "source": "maven_settings_probe",
        "path": "/root/.m2/repository",
        "exit_code": 0,
        "probe": write(
            tmp_path,
            "repository-probe.txt",
            "Apache Maven 3.9.16\nJava version: 17.0.12\n/root/.m2/repository\n",
        ),
    }
    row = requirement("install", "install", "install:install")
    row["expectations"] = {"artifacts": [artifact]}
    result = native_requirement(
        row, record, tmp_path, text, maven_events(text, terminal=True, serial=True)
    )
    assert result["status"] == "passed", result


@pytest.mark.parametrize("observed", [{"classifier": "tests"}, {"extension": "war"}])
def test_frozen_artifact_identity_conflicts_not_ignored(tmp_path, observed):
    path = tmp_path / "main.jar"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("A.class", b"class bytes")
    expected = {
        "path": "target/main.jar",
        "role": "main",
        "module": "demo",
        "classifier": None,
        "extension": "jar",
    }
    artifact = {
        "path": "main.jar",
        "sha256": file_digest(path),
        "producer_relative_path": expected["path"],
        "role": "main",
        "module": "demo",
        "classifier": None,
        "extension": "jar",
        "fresh": True,
        **observed,
    }
    assert not artifact_proof(tmp_path, [expected], [artifact])


def spec_for(task, row):
    return {
        "schema_version": 2,
        "policy_version": "ci-requirements-v2",
        "task_sha256": canonical_digest(normalize_task(task)),
        "annotation_completeness": {"status": "complete"},
        "requirements": [row],
        "preconditions": [{"id": "worktree_integrity"}, {"id": "runtime_conformance"}],
    }


def test_portable_score_accepts_same_default_normalization_as_recorder():
    raw_task = {
        "schema_version": 1,
        "repo": "example/demo",
        "sha": "a" * 40,
        "steps": [{"id": "step", "runner": "maven", "argv": ["mvn", "javadoc:javadoc"]}],
    }
    spec = spec_for(raw_task, requirement())
    assert validate_requirements(spec, raw_task) == spec


def test_complete_install_scope_rejects_coordinate_path_conflict():
    task = {
        "schema_version": 1,
        "repo": "example/demo",
        "sha": "a" * 40,
        "steps": [
            {
                "id": "step",
                "runner": "maven",
                "argv": ["mvn", "install"],
                "cwd": ".",
                "java_major": 17,
            }
        ],
    }
    row = requirement("install", "install", "install:install")
    row["expectations"] = {
        "artifacts": [
            {
                "module": "demo",
                "role": "installed_pom",
                "extension": "pom",
                "classifier": None,
                "coordinates": {"group_id": "org.example", "artifact_id": "demo", "version": "1"},
                "repository_relative_path": "org/other/unrelated/2/unrelated-2.pom",
            }
        ]
    }
    with pytest.raises(ValueError):
        validate_requirements(spec_for(task, row), task)


def test_native_execution_proof_must_describe_the_executable_actually_invoked(tmp_path):
    task = {
        "schema_version": 1,
        "repo": "example/demo",
        "sha": "a" * 40,
        "steps": [
            {
                "id": "producer",
                "runner": "gradle",
                "argv": ["./gradlew", ":nativeCompile"],
                "cwd": ".",
                "java_major": None,
            },
            {
                "id": "step",
                "runner": "native",
                "argv": ["./bin/required-program"],
                "cwd": ".",
                "java_major": None,
            },
        ],
    }
    row = requirement("test", "native_exit", "unused:goal")
    row.update(subtype="native", expectations={"executable": "./bin/required-program"})
    producer_row = requirement("native_compile", "artifact", "unused:compile")
    producer_row.update(
        id="compile",
        step_id="producer",
        expectations={
            "artifacts": [
                {"path": "bin/other-program", "role": "native_executable", "module": "demo"}
            ]
        },
    )
    producer_row["validation"]["tasks"] = [":nativeCompile"]
    spec = spec_for(task, row)
    spec["requirements"] = [producer_row, row]
    identity = evaluation_identity(spec)
    source = write(tmp_path, "other-program", b"other binary bytes")
    artifact = {
        **source,
        "producer_relative_path": "bin/other-program",
        "fresh": True,
        "role": "native_executable",
        "module": "demo",
    }
    records = []
    for index, step in enumerate(task["steps"]):
        record = {
            **step,
            "step_id": step["id"],
            "invocation_id": step["id"],
            "run_id": "run",
            "evaluation_identity": identity,
            "commit": task["sha"],
            "sequence": index,
            "status": "completed",
            "exit_code": 0,
            "log_complete": True,
            "log": write(
                tmp_path,
                f"log-{index}.txt",
                "> Task :nativeCompile\nBUILD SUCCESSFUL\n" if not index else "native exit zero\n",
            ),
        }
        if index == 0:
            record["artifacts"] = [artifact]
        else:
            record["native_executable"] = {
                **source,
                "producer_relative_path": "bin/other-program",
                "producer_invocation_id": "producer",
            }
        records.append(write(tmp_path, f"invocation-{index}.json", json.dumps(record)))
    run = {
        "run_id": "run",
        "evaluation_identity": identity,
        "repo": task["repo"],
        "commit": task["sha"],
        "invocations": records,
        "worktree": [],
    }
    result = evaluate(task, spec, run, tmp_path)
    assert next(r for r in result["requirements"] if r["id"] == "r")["status"] != "passed"


@pytest.mark.parametrize("problem", ["read_error", "nested_symlink"])
def test_portable_report_inventory_cannot_silently_omit_unreadable_scope(
    tmp_path, monkeypatch, problem
):
    root = tmp_path / "repo"
    reports = root / "build/test-results"
    reports.mkdir(parents=True)
    recorder = Recorder.__new__(Recorder)
    recorder.root = root
    row = requirement("test", "junit", "unused:goal")
    row["subtype"] = "unit"
    recorder.spec = {"requirements": [row]}
    if problem == "read_error":

        def broken_walk(path, *, followlinks=False, onerror=None):
            if onerror:
                onerror(PermissionError("report subtree cannot be read"))
            return iter(())

        monkeypatch.setattr("sag.benchmark.recorder.os.walk", broken_walk)
    else:
        (reports / "hidden").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        recorder._files({"id": "step", "runner": "gradle"})
