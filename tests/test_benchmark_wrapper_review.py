"""The actual reviewed script forwards to a fake cached Maven; no JVM/network."""

from copy import deepcopy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from sag.benchmark.evaluator import evaluate
from sag.benchmark.recorder import Recorder
from sag.benchmark.requirements import bound_file, canonical_digest, normalize_task
from sag.benchmark.sag_observer import SAGRequirementObserver
from sag.benchmark.wrapper_review import (
    make_launcher_review,
    capture_request,
    capture_wrapper_inputs,
    snapshot_inputs,
    certified_invocation_inputs,
    POLICY,
)
from test_sag_requirement_observer import contract, receipt

URL = "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/3.9.16/apache-maven-3.9.16-bin.zip"


class Control:
    def __init__(self, env):
        self.env = env
        self.calls = []

    def _default_exec_environment(self):
        return dict(self.env)

    def execute(self, command, **kwargs):
        argv = shlex.split(command)
        request = json.loads(argv[-1])
        self.calls.append(request)
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "output": proc.stdout,
        }


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    files = {
        "mvnw": (
            Path(__file__).parent / "fixtures/maven-wrapper-only-script-3.3.4/mvnw"
        ).read_bytes(),
        ".mvn/wrapper/maven-wrapper.properties": (
            "wrapperVersion=3.3.4\ndistributionType=only-script\ndistributionUrl=" + URL + "\n"
        ).encode(),
        ".mvn/maven.config": b"-V\n",
    }
    for path, raw in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    (root / "mvnw").chmod(0o755)
    (root / "pom.xml").write_text("<project/>\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
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
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    task = normalize_task(
        {
            "repo": "arbitrary/fixture",
            "sha": sha,
            "steps": [
                {
                    "id": "test",
                    "runner": "maven",
                    "argv": ["./mvnw", "clean", "test"],
                    "cwd": ".",
                    "java_major": 17,
                }
            ],
        }
    )
    review = make_launcher_review(sha, files)
    spec = {
        "schema_version": 2,
        "policy_version": "ci-requirements-v2",
        "task_sha256": canonical_digest(task),
        "annotation_completeness": {"status": "complete"},
        "preconditions": [{"id": "worktree_integrity"}, {"id": "runtime_conformance"}],
        "steps": [{"step_id": "test", "launcher_review": review}],
        "requirements": [
            {
                "id": "compile",
                "step_id": "test",
                "kind": "compile",
                "module": "tiny",
                "scope": {"status": "declared", "module_path": "."},
                "validation": {"rule": "native_goal", "goals": ["compiler:compile"]},
            },
            {
                "id": "tests",
                "step_id": "test",
                "kind": "test",
                "subtype": "unit",
                "module": "tiny",
                "scope": {"status": "declared", "module_path": "."},
                "validation": {"rule": "junit", "goals": ["surefire:test"]},
            },
        ],
    }
    env = {
        name: ""
        for name in (
            "MAVEN_ARGS",
            "MAVEN_CONFIG",
            "MAVEN_PROJECTBASEDIR",
            "MAVEN_BASEDIR",
            "MVNW_REPOURL",
            "MVNW_VERBOSE",
            "MAVEN_OPTS",
            "JAVA_TOOL_OPTIONS",
            "_JAVA_OPTIONS",
            "JDK_JAVA_OPTIONS",
        )
    }
    env.update(MAVEN_SKIP_RC="true", MAVEN_USER_HOME=str(tmp_path / "cache"))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    hashed = 0
    for char in URL:
        hashed = (31 * hashed + ord(char)) % 2**32
    executable = (
        Path(env["MAVEN_USER_HOME"]) / f"wrapper/dists/apache-maven-3.9.16/{hashed:x}/bin/mvn"
    )
    executable.parent.mkdir(parents=True)
    executable.write_text("#!" + sys.executable + """
import sys,json
from pathlib import Path
print('Apache Maven 3.9.16')
print('Java version: 17.0.1, vendor: Test')
if '--version' in sys.argv: raise SystemExit(0)
if 'help:evaluate' in sys.argv:
 print(str(Path.home()/'.m2/repository')); raise SystemExit(0)
assert sys.argv[1:]==['clean','test'],sys.argv
print('[INFO] --- compiler:3.14.0:compile (default-compile) @ tiny ---')
print('[INFO] Compiling 1 source file')
print('[INFO] --- surefire:3.5.0:test (default-test) @ tiny ---')
print('[INFO] Tests run: 1, Failures: 0, Errors: 0, Skipped: 0')
p=Path('target/surefire-reports/TEST-Tiny.xml');p.parent.mkdir(parents=True,exist_ok=True)
p.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase classname="Tiny" name="one"/></testsuite>')
print('[INFO] BUILD SUCCESS')
""")
    executable.chmod(0o755)
    return root, task, spec, env, executable


def portable(fixture, tmp_path):
    root, task, spec, env, _ = fixture
    recorder = Recorder(
        task=task, requirements=spec, repo_root=root, records=tmp_path / "portable", run_id="r1"
    )
    recorder.start()
    invocation = recorder.step("test", timeout=10)
    run = recorder.close()
    return recorder, invocation, evaluate(task, spec, run, recorder.base)


def sag(fixture, tmp_path, mutate=None):
    root, task, spec, env, _ = fixture
    control = Control(env)
    observer = SAGRequirementObserver(
        tmp_path / "sag", "r1", str(root), task, spec, control.execute
    )
    c = contract(observer)
    observer.before_contract(c)
    if mutate:
        mutate()
    completed = subprocess.run(
        task["steps"][0]["argv"],
        cwd=root,
        env={**os.environ, **env},
        capture_output=True,
        check=False,
    )
    observer.after_receipt(receipt(c, argv=shlex.join(task["steps"][0]["argv"])))
    observation = observer.export_invocation(c["contract_id"])
    inv = {**observation, "invocation_id": c["contract_id"]}
    verified = certified_invocation_inputs(spec, task, task["steps"][0], inv, observer.base)
    return observer, observation, verified, completed


def test_actual_reviewed_wrapper_both_collectors_keep_argv_and_can_pass(fixture, tmp_path):
    recorder, invocation, score = portable(fixture, tmp_path)
    assert invocation["argv"] == ["./mvnw", "clean", "test"]
    assert invocation["effective_execution"]["wrapper_status"] == POLICY
    assert invocation["effective_execution"]["serial"] is True
    assert [r["status"] for r in score["requirements"]] == ["passed", "passed"]
    observer, observation, verified, command = sag(fixture, tmp_path)
    assert command.returncode == 0
    assert verified["inputs_complete"] and verified["wrapper_reviewed"] and verified["serial"]
    assert observation["reports"][0]["fresh"]
    assert (
        observation["reports"][0]["producer_relative_path"]
        == "target/surefire-reports/TEST-Tiny.xml"
    )


@pytest.mark.parametrize("wrong_java", [False, True])
def test_portable_cli_ignores_wrapper_maven_hint_and_still_checks_runtime(fixture, tmp_path, wrong_java):
    from scripts.run_portable_harness import recorder_hint_options

    root, task, spec, env, executable = fixture
    if wrong_java:
        executable.write_text(executable.read_text().replace("Java version: 17.0.1", "Java version: 8.0.1"))
    recorder = Recorder(task=task, requirements=spec, repo_root=root,
                        records=tmp_path / "records", run_id="r1")
    recorder.start()
    task_path, spec_path = tmp_path / "task.json", tmp_path / "requirements.json"
    task_path.write_text(json.dumps(task)); spec_path.write_text(json.dumps(spec))
    options, audit = recorder_hint_options(task['steps'][0], {
        'test': {'maven_bin': '/irrelevant/maven/bin/mvn'}})
    command = [sys.executable, '-m', 'sag.benchmark.recorder', 'step',
               '--task', str(task_path), '--requirements', str(spec_path),
               '--repo-root', str(root), '--records', str(recorder.base),
               '--run-id', 'r1', '--step-id', 'test', '--timeout', '10', *options]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)['exit_code'] == 0
    invocation = json.loads(next((recorder.base / 'invocations').glob('*/invocation.json')).read_text())
    assert invocation['effective_argv'] == ['./mvnw', 'clean', 'test']
    assert audit['maven_bin']['status'] == 'ignored'
    score = evaluate(task, spec, recorder.close(), recorder.base)
    runtime = next(r for r in score['preconditions'] if r['id'] == 'runtime_conformance')
    assert runtime['status'] == ('failed' if wrong_java else 'passed')
    assert score['status'] == ('incomplete' if wrong_java else 'complete')


@pytest.mark.parametrize("change", ["script", "config", "new_config", "symlink", "untracked"])
def test_changed_inputs_do_not_gain_certification_but_original_command_runs(
    fixture, tmp_path, change
):
    root, task, spec, env, _ = fixture

    def mutate():
        if change == "script":
            with (root / "mvnw").open("a") as f:
                f.write("\n# actor changed\n")
        elif change == "config":
            (root / ".mvn/maven.config").write_text("-V -T2\n")
        elif change == "new_config":
            (root / ".mvn/jvm.config").write_text("-Dtest=secret\n")
        elif change == "symlink":
            (root / ".mvn/maven.config").unlink()
            (root / ".mvn/maven.config").symlink_to(root / "pom.xml")
        else:
            subprocess.run(
                ["git", "-C", str(root), "rm", "--cached", ".mvn/maven.config"],
                check=True,
                capture_output=True,
            )

    _, observation, verified, command = sag(fixture, tmp_path, mutate)
    assert command.returncode == 0
    assert not observation["effective_execution"]["inputs_complete"]
    assert not verified["serial"] and not verified["wrapper_reviewed"]


@pytest.mark.parametrize(
    "name,value",
    [
        ("MAVEN_ARGS", "-T2 secret-token"),
        ("MVNW_REPOURL", "https://user:secret-token@example.invalid"),
        ("MAVEN_OPTS", "-Dpassword=secret-token"),
        ("MVNW_VERBOSE", "debug"),
    ],
)
def test_unreviewed_environment_is_unknown_without_retaining_its_secret(
    fixture, tmp_path, monkeypatch, name, value
):
    root, task, spec, env, _ = fixture
    env[name] = value
    monkeypatch.setenv(name, value)
    control = Control(env)
    observer = SAGRequirementObserver(
        tmp_path / "sag", "r1", str(root), task, spec, control.execute
    )
    c = contract(observer)
    observer.before_contract(c)
    pending = observer._pending[c["contract_id"]]["effective_execution"]
    assert not pending["wrapper_reviewed"]
    assert not any(r.get("operation") == "probe" for r in control.calls)
    files = [p.read_bytes() for p in observer.base.rglob("*") if p.is_file()]
    assert all(b"secret-token" not in data for data in files)
    recorder = Recorder(
        task=task, requirements=spec, repo_root=root, records=tmp_path / "portable", run_id="r1"
    )
    out = tmp_path / "portable" / "test"
    out.mkdir(parents=True)
    snapshot, ref = recorder._wrapper_snapshot(
        task["steps"][0], dict(os.environ), out, "i1", "acceptance_before"
    )
    with pytest.raises(ValueError, match="environment"):
        snapshot_inputs(
            spec["steps"][0]["launcher_review"],
            task,
            task["steps"][0],
            snapshot,
            "r1",
            "i1",
            "acceptance_before",
        )
    assert b"secret-token" not in bound_file(recorder.base, ref).read_bytes()


def test_memory_options_allowed_but_new_startup_rc_is_unknown(fixture, tmp_path, monkeypatch):
    root, task, spec, env, _ = fixture
    env["MAVEN_OPTS"] = "-Xmx2g -XX:MaxMetaspaceSize=512m"
    monkeypatch.setenv("MAVEN_OPTS", env["MAVEN_OPTS"])
    _, _, verified, _ = sag(fixture, tmp_path)
    assert verified["inputs_complete"]
    home = tmp_path / "home"
    home.mkdir()
    (home / ".mavenrc").write_text("secret-token")
    env.update(HOME=str(home), MAVEN_SKIP_RC="")
    request = capture_request(
        task,
        task["steps"][0],
        spec["steps"][0]["launcher_review"],
        root,
        "r1",
        "i1",
        "acceptance_before",
        env,
    )
    snapshot = capture_wrapper_inputs(request)
    with pytest.raises(ValueError, match="environment"):
        snapshot_inputs(
            spec["steps"][0]["launcher_review"],
            task,
            task["steps"][0],
            snapshot,
            "r1",
            "i1",
            "acceptance_before",
        )
    assert "secret-token" not in json.dumps(snapshot)


def test_version_mismatch_is_not_a_reviewed_launcher(fixture, tmp_path):
    root, task, spec, env, exe = fixture
    exe.write_text(exe.read_text().replace("Apache Maven 3.9.16", "Apache Maven 3.9.9"))
    _, invocation, score = portable(fixture, tmp_path)
    assert invocation["exit_code"] == 0
    assert invocation["effective_execution"]["wrapper_reviewed"] is False
    assert [r["status"] for r in score["requirements"]] == ["unavailable", "unavailable"]


def test_missing_or_tampered_raw_proof_cannot_be_repaired_by_boolean_flags(fixture, tmp_path):
    recorder, invocation, _ = portable(fixture, tmp_path)
    base = invocation["effective_execution"]["wrapper_evidence"]
    for mutate in ("remove", "tamper", "foreign"):
        inv = deepcopy(invocation)
        evidence = inv["effective_execution"]["wrapper_evidence"]
        if mutate == "remove":
            del evidence["after"]
        elif mutate == "tamper":
            evidence["before"]["sha256"] = "0" * 64
        else:
            inv["invocation_id"] = "some-other-invocation"
        actual = certified_invocation_inputs(
            recorder.spec, recorder.task, recorder.task["steps"][0], inv, recorder.base
        )
        assert not actual["serial"] and not actual["wrapper_reviewed"]


def test_unsupported_jar_wrapper_is_never_approved_by_a_version_banner(fixture):
    root, task, spec, env, _ = fixture
    files = {
        item["path"]: (root / item["path"]).read_bytes()
        for item in spec["steps"][0]["launcher_review"]["files"]
    }
    files["mvnw"] = b'#!/bin/sh\nexec java org.apache.maven.wrapper.MavenWrapperMain "$@"\n'
    with pytest.raises(ValueError, match="not been reviewed"):
        make_launcher_review(task["sha"], files)


@pytest.mark.parametrize("mutation", [None, "missing_source", "review_mismatch", "version"])
def test_importer_uses_the_shared_profile_and_rejects_incomplete_source_review(
    fixture, tmp_path, mutation
):
    from scripts.build_benchmark_requirements import import_launcher_review
    from sag.benchmark.recorder import reference

    root, task, spec, _, _ = fixture
    review = deepcopy(spec["steps"][0]["launcher_review"])
    entries = [
        {
            "source_path": entry["path"],
            "kind": "file",
            "pinned_bytes_equal": True,
            **reference(root, root / entry["path"]),
        }
        for entry in review["files"]
    ]
    index = tmp_path / "inventory.json"
    index.write_text(json.dumps({"commit": task["sha"], "files": entries}))
    additional = {"launcher_inventory": reference(tmp_path, index)}
    additional.update(
        {
            "launcher_source:"
            + entry["source_path"]: reference(tmp_path, root / entry["source_path"])
            for entry in entries
        }
    )
    reviewed = {"launcher_review": review, "additional_sources": additional}
    step = deepcopy(task["steps"][0])
    if mutation == "missing_source":
        additional.pop("launcher_source:.mvn/maven.config")
    elif mutation == "review_mismatch":
        review["maven_version"] = "3.9.9"
    elif mutation == "version":
        step["maven_version"] = "3.9.9"
    if mutation:
        with pytest.raises(ValueError):
            import_launcher_review(reviewed, tmp_path, task, step, tmp_path / "export")
    else:
        actual, sources = import_launcher_review(
            reviewed, tmp_path, task, step, tmp_path / "export"
        )
        assert actual == review
        assert len(sources) == len(entries) + 1


def test_inventory_reader_accepts_review_without_claiming_runtime_proof(fixture):
    from sag.benchmark.requirements import validate_requirements

    _, _, spec, _, _ = fixture
    assert validate_requirements(spec) is spec


def test_portable_after_boundary_catches_wrapper_mutation_inside_command(fixture, tmp_path):
    _, _, _, _, executable = fixture
    executable.write_text(
        executable.read_text().replace(
            "print('[INFO] BUILD SUCCESS')",
            "Path('mvnw').write_bytes(Path('mvnw').read_bytes()+b'\\n# changed\\n')\nprint('[INFO] BUILD SUCCESS')",
        )
    )
    _, invocation, score = portable(fixture, tmp_path)
    assert invocation["exit_code"] == 0
    assert not invocation["effective_execution"]["wrapper_reviewed"]
    assert [r["status"] for r in score["requirements"]] == ["unavailable", "unavailable"]


def test_frozen_parallel_configuration_does_not_become_serial(fixture, tmp_path):
    root, task, spec, env, executable = fixture
    (root / ".mvn/maven.config").write_text("-V -T2\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "parallel fixture",
        ],
        check=True,
    )
    task["sha"] = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    files = {
        entry["path"]: (root / entry["path"]).read_bytes()
        for entry in spec["steps"][0]["launcher_review"]["files"]
    }
    spec["steps"][0]["launcher_review"] = make_launcher_review(task["sha"], files)
    spec["task_sha256"] = canonical_digest(task)
    _, invocation, score = portable(fixture, tmp_path)
    assert invocation["effective_execution"]["inputs_complete"]
    assert invocation["effective_execution"]["serial"] is False
    assert [r["status"] for r in score["requirements"]] == ["unavailable", "unavailable"]
    _, _, verified, _ = sag(fixture, tmp_path)
    assert verified["inputs_complete"] and not verified["serial"]


@pytest.mark.parametrize("missing", ["errors", "jvm_options", "selection_fingerprints"])
def test_removing_snapshot_completeness_fields_does_not_create_clean_evidence(fixture, missing):
    root, task, spec, env, _ = fixture
    review = spec["steps"][0]["launcher_review"]
    request = capture_request(
        task, task["steps"][0], review, root, "r1", "i1", "acceptance_before", env
    )
    snapshot = capture_wrapper_inputs(request)
    snapshot_inputs(review, task, task["steps"][0], snapshot, "r1", "i1", "acceptance_before")
    if missing == "errors":
        del snapshot[missing]
    else:
        del snapshot["environment"][missing]
    with pytest.raises(ValueError):
        snapshot_inputs(review, task, task["steps"][0], snapshot, "r1", "i1", "acceptance_before")


@pytest.mark.parametrize("probe_index", [1, 2])
def test_truncated_raw_inventory_is_not_complete(fixture, probe_index):
    import base64

    root, task, spec, env, _ = fixture
    review = spec["steps"][0]["launcher_review"]
    request = capture_request(
        task, task["steps"][0], review, root, "r1", "i1", "acceptance_before", env
    )
    snapshot = capture_wrapper_inputs(request)
    probe = snapshot["probes"][probe_index]
    raw = base64.b64decode(probe["stdout"])
    assert raw.endswith(b"\0")
    probe["stdout"] = base64.b64encode(raw[:-1]).decode()
    with pytest.raises(ValueError):
        snapshot_inputs(review, task, task["steps"][0], snapshot, "r1", "i1", "acceptance_before")
