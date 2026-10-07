"""Real tiny local invocations exercise recorder evidence, without Java or LLMs."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from sag.benchmark.evaluator import evaluate
from sag.benchmark.recorder import Recorder, normalize_task
from sag.benchmark.requirements import bound_file, canonical_digest, load_json


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "source.txt").write_text("frozen source\n")
    (root / "pom.xml").write_text("<project/>\n")
    git(root, "add", ".")
    git(root, "-c", "user.name=Recorder Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    task = {"schema_version": 1, "repo": "example/tiny", "sha": git(root, "rev-parse", "HEAD"), "steps": [{
        "id": "test", "runner": "shell", "argv": [sys.executable, "-c", "print('done')"], "cwd": ".", "java_major": None}]}
    req = {"id": "test-unit", "step_id": "test", "kind": "test", "subtype": "unit", "module": "tiny",
           "scope": {"status": "declared", "module_path": "."}, "depends_on": [], "dependencies_complete": True,
           "validation": {"rule": "junit", "goals": ["surefire:test"], "plan_resolved": True, "position": 0}}
    spec = {"schema_version": 2, "policy_version": "ci-requirements-v2", "project_id": "tiny",
            "task_sha256": canonical_digest(task), "annotation_completeness": {"status": "complete"},
            "preconditions": [{"id": "worktree_integrity"}, {"id": "runtime_conformance"}], "requirements": [req]}

    def make(task_change=None, spec_change=None, records=None, run_id="run-1"):
        t, s = copy.deepcopy(task), copy.deepcopy(spec)
        if task_change:
            task_change(t)
        if spec_change:
            spec_change(s)
        s["task_sha256"] = canonical_digest(normalize_task(t))
        return Recorder(task=t, requirements=s, repo_root=root, records=records or tmp_path / "records", run_id=run_id)
    return root, make


def test_separate_start_step_close_records_real_boundaries_and_never_invents_human_zero(setup):
    root, make = setup
    recorder = make()
    started = recorder.start(agent="test-agent")
    (root / "agent-notes.txt").write_text("retained untracked input")
    invocation = make().step("test", timeout=5)
    run = make().close()
    assert started["admission"] == "ready"
    assert invocation["status"] == "completed" and invocation["exit_code"] == 0
    assert bound_file(recorder.base, invocation["log"]).read_text() == "done\n"
    snapshots = [load_json(bound_file(recorder.base, ref)) for ref in run["worktree"]]
    assert [s["boundary"] for s in snapshots] == ["task_start", "acceptance_before", "acceptance_after", "evidence_close"]
    assert "agent-notes.txt" in snapshots[-1]["untracked_paths"]
    assert run["telemetry"] is None
    score = evaluate(recorder.task, recorder.spec, run, recorder.base)
    assert score["preconditions"][0]["status"] == "passed"
    assert score["human_interventions"] is None


def test_step_cannot_create_late_start_and_closed_attempt_cannot_be_reopened(setup):
    _, make = setup
    with pytest.raises(FileNotFoundError):
        make().step("test", timeout=5)
    make().start()
    with pytest.raises(ValueError, match="new attempt"):
        make().start()
    make().close()
    with pytest.raises(ValueError, match="closed"):
        make().step("test", timeout=5)


def test_invocation_binds_before_and_after_class_inventories(setup):
    root,make=setup
    def task(t):
        t['steps'][0]['argv']=[sys.executable,'-c',
            "from pathlib import Path;p=Path('target/classes/Main.class');p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(bytes.fromhex('cafebabe0000003d'))"]
    def spec(s):
        s['steps']=[{'step_id':'test','modules':[{'id':'tiny','path':'.'}]}]
    recorder=make(task_change=task,spec_change=spec)
    recorder.start();result=recorder.step('test',timeout=5)
    inv=result['output_inventory']
    before=load_json(bound_file(recorder.base,inv['before']))
    after=load_json(bound_file(recorder.base,inv['after']))
    assert before['counts'] == {} and after['counts'] == {'class:main':1}
    assert after['invocation_id'] == before['invocation_id'] == result['invocation_id']
    assert after['run_id'] == before['run_id'] == recorder.run_id


@pytest.mark.parametrize("stale", [False, True])
def test_one_artifact_path_preserves_both_declared_roles(setup, tmp_path, stale):
    from sag.benchmark.native_evidence import artifact_proof
    import zipfile

    root, make = setup
    launcher = tmp_path / "mvn"
    launcher.write_text(f"#!{sys.executable}\n" + f"""
import sys, pathlib, zipfile
if '--version' in sys.argv:
    print('Apache Maven 3.9.16\\nJava version: 17.0.12\\nJava home: /jdk')
else:
    if not {stale!r}:
        pathlib.Path('target').mkdir(exist_ok=True)
        with zipfile.ZipFile('target/tiny.jar', 'w') as archive:
            archive.writestr('Tiny.class', b'compiled fixture')
    print('[INFO] --- jar:1.0:jar (default-jar) @ tiny ---')
    print('[INFO] --- moditect:1.0:add-module-info (augment) @ tiny ---')
    print('[INFO] BUILD SUCCESS')
""")
    launcher.chmod(0o755)
    if stale:
        (root / "target").mkdir()
        with zipfile.ZipFile(root / "target/tiny.jar", "w") as archive:
            archive.writestr("Tiny.class", b"existing fixture")

    def task_change(task):
        task["steps"][0].update(runner="maven", argv=["mvn", "verify"])

    def spec_change(spec):
        spec["requirements"] = [{
            "id": role, "step_id": "test", "kind": "package", "module": "tiny",
            "scope": {"status": "declared", "module_path": "."},
            "validation": {"rule": "artifact", "goals": [goal]},
            "expectations": {"artifacts": [{"path": "target/tiny.jar", "module": "tiny", "role": role, "format": "jar"}]},
        } for role, goal in [("main_archive", "jar:jar"), ("augmented_archive", "moditect:add-module-info")]]

    recorder = make(task_change=task_change, spec_change=spec_change)
    recorder.start()
    record = recorder.step("test", timeout=5, maven_bin=launcher)
    assert record["exit_code"] == 0
    assert len(record["artifacts"]) == (0 if stale else 2)
    expected = [r["expectations"]["artifacts"][0] for r in recorder.spec["requirements"]]
    assert artifact_proof(recorder.base, expected, record["artifacts"]) is (not stale)
    if not stale:
        assert {a["role"] for a in record["artifacts"]} == {"main_archive", "augmented_archive"}
        assert len({a["sha256"] for a in record["artifacts"]}) == 1


def test_tracked_change_at_start_is_preserved_failed_admission_and_can_close(setup):
    root, make = setup
    (root / "source.txt").write_text("changed")
    assert make().start()["admission"] == "failed"
    with pytest.raises(ValueError, match="integrity"):
        make().step("test", timeout=5)
    run = make().close()
    assert run["status"] == "closed"


@pytest.mark.parametrize("mutation", ["run", "task", "requirements"])
def test_started_identity_cannot_be_changed(setup, mutation):
    _, make = setup
    make().start()
    kwargs = {"run_id": "other"} if mutation == "run" else {"task_change": lambda t: t["steps"][0]["argv"].append("other")} if mutation == "task" else {"spec_change": lambda s: s.update(note="different requirement digest")}
    with pytest.raises(ValueError, match="identity changed"):
        make(**kwargs).step("test", timeout=5)


def test_one_final_attempt_per_step_and_order(setup):
    _, make = setup
    def two_steps(task):
        task["steps"].append({**task["steps"][0], "id": "second"})
    def two_rows(spec):
        spec["requirements"].append({**spec["requirements"][0], "id": "second-test", "step_id": "second"})
    r = make(task_change=two_steps, spec_change=two_rows)
    r.start()
    with pytest.raises(ValueError, match="in order"):
        r.step("second", timeout=5)
    first = r.step("test", timeout=5)
    with pytest.raises(ValueError, match="in order"):
        r.step("test", timeout=5)
    second = r.step("second", timeout=5)
    assert first["sequence"] == 0 and second["sequence"] == 1
    assert second["predecessor_sha256"] is not None


@pytest.mark.parametrize('first_exit', [0, 7])
def test_cli_exit_code_drives_real_ordered_step_execution(setup, tmp_path, first_exit):
    _, make = setup

    def steps(task):
        task['steps'][0]['argv'] = [sys.executable, '-c', f'import sys; sys.exit({first_exit})']
        task['steps'].append({**task['steps'][0], 'id': 'second',
                              'argv': [sys.executable, '-c', "print('second ran')"]})

    def rows(spec):
        spec['requirements'].append({**spec['requirements'][0], 'id': 'second-test', 'step_id': 'second'})

    recorder = make(task_change=steps, spec_change=rows)
    recorder.start()
    task_path, spec_path = tmp_path / 'task.json', tmp_path / 'requirements.json'
    task_path.write_text(json.dumps(recorder.task)); spec_path.write_text(json.dumps(recorder.spec))
    observed = []
    for step in recorder.task['steps']:
        proc = subprocess.run([sys.executable, '-m', 'sag.benchmark.recorder', 'step',
            '--task', str(task_path), '--requirements', str(spec_path),
            '--repo-root', str(recorder.root), '--records', str(recorder.base),
            '--run-id', recorder.run_id, '--step-id', step['id'], '--timeout', '10'],
            capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout)
        observed.append(result)
        if result.get('exit_code') != 0:
            break
    assert [r['exit_code'] for r in observed] == ([0, 0] if first_exit == 0 else [7])
    assert len(recorder.close()['invocations']) == (2 if first_exit == 0 else 1)


def test_stdin_is_devnull_and_timeout_log_is_retained(setup):
    _, make = setup
    def timeout(task):
        task["steps"][0]["argv"] = [sys.executable, "-c", "import sys,time; print(repr(sys.stdin.read()),flush=True); time.sleep(20)"]
    r = make(task_change=timeout)
    r.start()
    # Budget includes the Git boundary capture and interpreter startup; 50 ms
    # could expire before this test's first print rather than during its sleep.
    record = r.step("test", timeout=0.5)
    assert record["status"] == "timeout"
    assert record["log_complete"] is True
    assert bound_file(r.base, record["log"]).read_text().strip() == "''"
    assert record["input_policy"]["stdin"] == "DEVNULL"


def test_fresh_junit_is_saved_but_stale_report_is_not_reused(setup):
    root, make = setup
    directory = root / "build/test-results/test"
    directory.mkdir(parents=True)
    (directory / "TEST-stale.xml").write_text('<testsuite tests="1"/>')
    def script(task):
        task["steps"][0]["argv"] = [sys.executable, "-c", "from pathlib import Path; Path('build/test-results/test/TEST-new.xml').write_text('<testsuite tests=\"1\"/>')"]
    r = make(task_change=script)
    r.start()
    record = r.step("test", timeout=5)
    assert [r["producer_relative_path"] for r in record["reports"]] == ["build/test-results/test/TEST-new.xml"]
    assert record["reports"][0]["fresh"] is True
    assert record["reports"][0]["freshness"]["before"] is None
    assert record["reports_collection_complete"] is True


def test_declared_artifact_role_is_copied_only_when_fresh(setup):
    root, make = setup
    (root / "target").mkdir()
    (root / "target/main.jar").write_bytes(b"stale")
    def artifact(spec):
        spec["requirements"][0]["expectations"] = {"artifacts": [
            {"path": "target/main.jar", "role": "main", "module": "tiny"},
            {"path": "target/tests.jar", "role": "tests", "module": "tiny"}]}
    def script(task):
        task["steps"][0]["argv"] = [sys.executable, "-c", "from pathlib import Path; Path('target/tests.jar').write_bytes(b'fresh')"]
    r = make(task_change=script, spec_change=artifact)
    r.start()
    record = r.step("test", timeout=5)
    assert [(a["role"], a["producer_relative_path"]) for a in record["artifacts"]] == [("tests", "target/tests.jar")]


def test_output_directory_and_report_symlink_escapes_rejected(setup, tmp_path):
    root, make = setup
    with pytest.raises(ValueError, match="separate trees"):
        make(records=root / "records")
    (tmp_path / "alias").symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlink"):
        make(records=tmp_path / "alias/records")
    (root / "build").symlink_to(tmp_path, target_is_directory=True)
    r = make()
    r.start()
    result = r.step("test", timeout=5)
    assert result["status"] == "completed"
    assert any("Symlink" in message for message in result["collection_errors"])
    assert result["reports_collection_complete"] is False
    assert result["reports"] == []


def test_real_launcher_probe_and_effective_maven_flags_are_retained(setup, tmp_path, monkeypatch):
    root, make = setup
    executable = tmp_path / "mvn"
    executable.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then printf "Apache Maven 3.9.16\\nJava version: 17.0.12\\n"; else printf "done\\n"; fi\n')
    executable.chmod(0o755)
    (root / ".mvn").mkdir()
    (root / ".mvn/maven.config").write_text("-fae\n")
    monkeypatch.setenv("MAVEN_ARGS", "--quiet")
    def maven(task):
        task["steps"][0].update(runner="maven", argv=["mvn", "test"], java_major=17, maven_version="3.9.16")
    r = make(task_change=maven)
    r.start()
    record = r.step("test", timeout=5, maven_bin=executable)
    probe = record["runtime"]["launcher_probe"]
    assert probe["executable"] == str(executable)
    assert probe["exit_code"] == 0
    assert "Java version: 17" in bound_file(r.base, probe).read_text()
    assert record["effective_execution"]["maven_config"] == ["-fae"]
    assert record["effective_execution"]["maven_args"] == ["--quiet"]
    assert record["argv"] == ["mvn", "test"]
    assert record["effective_argv"][0] == str(executable)


def test_failed_launcher_probe_is_not_zero_and_wrapper_is_not_assumed_reviewed(setup):
    _, make = setup
    def wrapper(task):
        task["steps"][0].update(runner="maven", argv=["./absent-mvnw", "test"], java_major=17)
    r = make(task_change=wrapper)
    r.start()
    record = r.step("test", timeout=5)
    assert record["runtime"]["launcher_probe"]["exit_code"] is None
    assert record["status"] == "unavailable"
    assert record["effective_execution"]["wrapper_reviewed"] is False


def test_cli_roundtrip_uses_stdlib_modules_only(setup, tmp_path):
    root, make = setup
    r = make()
    task, spec = tmp_path / "task.json", tmp_path / "requirements.json"
    task.write_text(json.dumps(r.task)); spec.write_text(json.dumps(r.spec))
    prefix = [sys.executable, "-m", "sag.benchmark.recorder"]
    suffix = ["--task", str(task), "--requirements", str(spec), "--repo-root", str(root), "--records", str(r.base), "--run-id", r.run_id]
    for action in ["start", "step", "close"]:
        extra = ["--step-id", "test", "--timeout", "5"] if action == "step" else []
        subprocess.run(prefix + [action] + suffix + extra, check=True, capture_output=True)
    assert load_json(r.base / "run.json")["status"] == "closed"


@pytest.mark.parametrize("main_artifact", [False, True])
@pytest.mark.parametrize("budget", [5, 60])
def test_actual_local_repository_probe_and_coordinate_bound_fresh_installs(setup, tmp_path, main_artifact, budget, monkeypatch):
    _, make = setup
    repository = tmp_path / "isolated-m2"
    repository.mkdir()
    executable = tmp_path / "mvn"
    relative = "org/example/tiny/1/tiny-1"
    extra = f"printf 'new jar' > '{repository}/{relative}.jar'\n" if main_artifact else ""
    executable.write_text(f'''#!/bin/sh
if [ "$1" = "--version" ]; then
  printf 'Apache Maven 3.9.16\\nJava version: 17.0.12\\n'
elif [ "$1" = "help:evaluate" ]; then
  printf '%s\\n' '{repository}'
else
  mkdir -p '{repository}/org/example/tiny/1'
  printf '<project/>' > '{repository}/{relative}.pom'
  {extra}
fi
''')
    executable.chmod(0o755)
    coordinates = {"group_id": "org.example", "artifact_id": "tiny", "version": "1"}
    artifacts = [{"module": "tiny", "role": "installed_pom", "coordinates": coordinates,
                  "extension": "pom", "classifier": None, "repository_relative_path": relative + ".pom"}]
    if main_artifact:
        artifacts.append({**artifacts[0], "role": "installed_main", "extension": "jar", "repository_relative_path": relative + ".jar"})
    def maven(task):
        task["steps"][0].update(runner="maven", argv=["mvn", "install"], java_major=17)
    def install(spec):
        spec["requirements"][0].update(kind="install", validation={"rule": "install", "goals": ["install:install"], "plan_resolved": True, "position": 0}, expectations={"artifacts": artifacts})
    r = make(task_change=maven, spec_change=install)
    original_run = subprocess.run
    def cold_probe(argv, **kwargs):
        if "help:evaluate" in argv and budget == 60:
            # Model a cold project whose metadata needs more than 30 seconds,
            # without adding a 30-second sleep to every test run.
            if kwargs["timeout"] < 31:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return original_run(argv, **kwargs)
    monkeypatch.setattr(subprocess, "run", cold_probe)
    r.start()
    record = r.step("test", timeout=budget, maven_bin=executable)
    assert record["local_repository"]["path"] == str(repository)
    assert record["local_repository"]["source"] == "maven_settings_probe"
    assert record["local_repository"]["exit_code"] == 0
    assert 0 < record["local_repository"]["probe"]["timeout_seconds"] <= budget
    bound_file(r.base, record["local_repository"]["probe"])
    assert {a["role"] for a in record["artifacts"]} == ({"installed_pom", "installed_main"} if main_artifact else {"installed_pom"})
    assert all(a["fresh"] for a in record["artifacts"])
    assert record["preparation_seconds"] > 0


@pytest.mark.parametrize("replace", [False, True])
def test_recreated_identical_artifacts_with_preserved_mtime_are_observed(setup, tmp_path, replace):
    """Atomic Maven copies can keep content/mtime while replacing the actual file."""
    from sag.benchmark.native_evidence import artifact_proof

    root, make = setup
    repository = tmp_path / "local-repository"
    relative = "org/example/tiny/1/tiny-1.pom"
    descriptor = root / "target/tiny-site.xml"
    installed = repository / relative
    for path in [descriptor, installed]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<project/>\n")
    initial = [(path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes()) for path in [descriptor, installed]]
    launcher = tmp_path / "mvn"
    launcher.write_text(f"#!{sys.executable}\n" + f"""
import sys, pathlib, shutil
if '--version' in sys.argv:
    print('Apache Maven 3.9.16\\nJava version: 17.0.12\\n')
elif 'help:evaluate' in sys.argv:
    print({str(repository)!r})
else:
    if {replace!r}:
        for path in [pathlib.Path({str(descriptor)!r}), pathlib.Path({str(installed)!r})]:
            temporary = path.with_suffix('.replacement')
            shutil.copy2(path, temporary)
            temporary.replace(path)
    print('[INFO] --- site:1.0:attach-descriptor (attach) @ tiny ---')
    print('[INFO] --- install:1.0:install (default-install) @ tiny ---')
    print('[INFO] BUILD SUCCESS')
""")
    launcher.chmod(0o755)
    artifacts = [
        {"module": "tiny", "path": "target/tiny-site.xml", "role": "attached", "format": "xml"},
        {"module": "tiny", "coordinates": {"group_id": "org.example", "artifact_id": "tiny", "version": "1"},
         "repository_relative_path": relative, "role": "installed_pom", "extension": "pom", "classifier": None},
    ]
    def task_change(task):
        task["steps"][0].update(runner="maven", argv=["mvn", "install"], java_major=17)
    def spec_change(spec):
        spec["requirements"] = [{
            "id": str(index), "step_id": "test", "kind": kind, "module": "tiny",
            "scope": {"status": "declared", "module_path": "."},
            "validation": {"rule": rule, "goals": [goal]},
            "expectations": {"artifacts": [artifact]},
        } for index, (kind, rule, goal, artifact) in enumerate([
            ("package", "artifact", "site:attach-descriptor", artifacts[0]),
            ("install", "install", "install:install", artifacts[1]),
        ])]
    recorder = make(task_change=task_change, spec_change=spec_change)
    recorder.start()
    record = recorder.step("test", timeout=5, maven_bin=launcher)
    assert record["exit_code"] == 0
    for path, (inode, mtime, content) in zip([descriptor, installed], initial):
        assert path.stat().st_mtime_ns == mtime and path.read_bytes() == content
        assert (path.stat().st_ino != inode) is replace
    assert len(record["artifacts"]) == (2 if replace else 1)
    if not replace:
        # Preserve the unchanged destination as an observation. Without a
        # reviewed fresh source and copy witness it still earns no credit.
        assert record["artifacts"][0]["fresh"] is False
    assert artifact_proof(recorder.base, artifacts, record["artifacts"]) is replace


def test_installed_coordinate_path_cannot_point_at_unrelated_file():
    with pytest.raises(ValueError, match="differs from frozen coordinates"):
        Recorder._repository_path({"coordinates": {"group_id": "org.example", "artifact_id": "tiny", "version": "1"},
                                   "extension": "pom", "repository_relative_path": "../../secret"})


def test_failed_repository_probe_is_not_assumed_default(setup, tmp_path):
    _, make = setup
    executable = tmp_path / "mvn"
    executable.write_text('#!/bin/sh\nif [ "$1" = "help:evaluate" ]; then printf "/guessed/repo\\n"; exit 1; fi\nexit 0\n')
    executable.chmod(0o755)
    def maven(task):
        task["steps"][0].update(runner="maven", argv=["mvn", "install"], java_major=17)
    def install(spec):
        spec["requirements"][0].update(kind="install", validation={"rule": "install", "goals": ["install:install"], "plan_resolved": True, "position": 0}, expectations={"artifacts": [{"role": "installed_pom", "module": "tiny", "coordinates": {"group_id": "org.example", "artifact_id": "tiny", "version": "1"}, "extension": "pom", "classifier": None, "repository_relative_path": "org/example/tiny/1/tiny-1.pom"}]})
    r = make(task_change=maven, spec_change=install)
    r.start()
    record = r.step("test", timeout=5, maven_bin=executable)
    assert record["local_repository"]["path"] is None
    assert record["local_repository"]["exit_code"] == 1


@pytest.mark.parametrize("alter_binary", [False, True])
def test_native_input_requires_exact_previous_producer_bytes(setup, alter_binary):
    root, make = setup
    def task_steps(task):
        task["steps"][0].update(argv=[sys.executable, "-c", "from pathlib import Path; p=Path('native-check'); p.write_text('#!/bin/sh\\nexit 0\\n'); p.chmod(0o755)"])
        task["steps"].append({"id": "native-run", "runner": "native", "argv": ["./native-check"], "cwd": ".", "java_major": None})
    def requirements(spec):
        spec["requirements"][0].update(kind="native_compile", validation={"rule": "artifact", "goals": ["native:compile"], "plan_resolved": True, "position": 0}, expectations={"artifacts": [{"path": "native-check", "role": "native_executable", "module": "tiny"}]})
        spec["requirements"].append({**spec["requirements"][0], "id": "native-run-test", "step_id": "native-run", "kind": "test", "subtype": "native", "validation": {"rule": "native_exit", "goals": [], "plan_resolved": True, "position": 1}, "expectations": {"executable": "./native-check"}})
    r = make(task_change=task_steps, spec_change=requirements)
    r.start()
    producer = r.step("test", timeout=5)
    if alter_binary:
        (root / "native-check").write_text("#!/bin/sh\nexit 0\n# changed\n")
    native = r.step("native-run", timeout=5)
    proof = native["native_executable"]
    assert proof["producer_invocation_id"] == (None if alter_binary else producer["invocation_id"])
    assert bound_file(r.base, proof).read_bytes() == (root / "native-check").read_bytes()


def test_gradle_parallel_state_not_inferred_from_maven_flags(setup):
    _, make = setup
    r = make()
    step = {"runner": "gradle", "argv": ["./gradlew", "build"]}
    r.base.mkdir()
    state = r._effective_execution(step, {}, r.base)
    assert state["serial"] is False
    assert state["inputs_complete"] is False


@pytest.mark.parametrize('enabled,cleanup_status', [(False, None), (True, 'quiescent'), (True, 'unavailable')])
def test_container_gradle_cleanup_is_explicit_and_never_changes_task(setup, monkeypatch, enabled, cleanup_status):
    _, make = setup
    r = make(task_change=lambda task: task['steps'][0].update(runner='gradle'),
             spec_change=lambda spec: spec['requirements'][0]['validation'].update(
                 goals=[':test'], report_directories=['build/test-results/test']))
    r.start()
    calls = []
    def cleanup(launcher, cwd, env, out, timeout):
        calls.append((launcher, timeout))
        return {'status': cleanup_status}
    monkeypatch.setattr(r, '_quiesce_gradle', cleanup)
    record = r.step('test', timeout=5, quiesce_gradle=enabled)
    assert bool(calls) is enabled
    assert record['argv'] == record['effective_argv'] == r.task['steps'][0]['argv']
    assert record['input_policy']['quiesce_gradle'] is enabled
    if cleanup_status == 'unavailable':
        assert record['exit_code'] is None and record['status'] == 'unavailable'
        assert 'Gradle daemon quiescence unavailable' in bound_file(r.base, record['log']).read_text()
    else:
        assert record['exit_code'] == 0 and record['status'] == 'completed'
    if enabled:
        assert 0 < calls[0][1] <= 5


def test_gradle_cleanup_flag_does_not_stop_other_runners(setup, monkeypatch):
    _, make = setup
    r = make();r.start()
    monkeypatch.setattr(r, '_quiesce_gradle', lambda *a: pytest.fail('Only Gradle steps may stop Gradle daemons'))
    assert r.step('test', timeout=5, quiesce_gradle=True)['exit_code'] == 0


def test_repository_metadata_probe_cannot_execute_goals_hidden_in_maven_args(setup, monkeypatch):
    _, make = setup
    r = make()
    step = {"id": "test", "runner": "maven", "argv": ["mvn", "install"]}
    r.spec["requirements"][0]["validation"]["rule"] = "install"
    inputs = {"inputs_complete": True, "wrapper_reviewed": True, "maven_config": [], "maven_args": ["verify"]}
    monkeypatch.setattr(r, "_probe", lambda *args, **kwargs: pytest.fail("must not launch this metadata probe"))
    assert r._local_repository(step, step["argv"], r.root, {}, r.base, 5, inputs) is None


def test_failed_before_inventory_stays_unknown_even_if_after_scan_works(setup, monkeypatch):
    root, make = setup
    directory = root / "build/test-results/test"
    directory.mkdir(parents=True)
    (directory / "TEST-old.xml").write_text('<testsuite tests="1"/>')
    r = make()
    r.start()
    def unreadable(*args, **kwargs):
        raise PermissionError("before scan not observed")
    monkeypatch.setattr(r, "_snapshot", unreadable)
    result = r.step("test", timeout=5)
    assert result["status"] == "completed" and result["exit_code"] == 0
    assert result["reports_collection_complete"] is False
    assert result["reports"] == result["artifacts"] == []
    assert result["collection_errors"] == ["before inventory: before scan not observed"]


def test_jvm_memory_options_are_observed_without_disabling_direct_maven(setup):
    root, make = setup
    r = make()
    r.base.mkdir()
    (root / ".mvn").mkdir()
    (root / ".mvn/jvm.config").write_text("-Xmx512m\n")
    step = {"id": "test", "runner": "maven", "argv": ["mvn", "test"]}
    inputs = r._effective_execution(step, {"MAVEN_SKIP_RC": "true", "MAVEN_OPTS": "-Xms128m"}, r.base)
    assert inputs["inputs_complete"] is inputs["serial"] is True
    assert inputs["jvm_config"] == ["-Xmx512m"]
    refs = {ref["source"]: ref for ref in inputs["configuration_evidence"]}
    assert bound_file(r.base, refs[".mvn/jvm.config"]).read_bytes() == b"-Xmx512m\n"
    assert load_json(bound_file(r.base, refs["recorder_process_environment"]))["MAVEN_OPTS"] == "-Xms128m"


@pytest.mark.parametrize("source", ["jvm.config", "MAVEN_OPTS", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS"])
def test_hidden_jvm_properties_disable_execution_inference_and_metadata_probe(setup, monkeypatch, source):
    root, make = setup
    r = make()
    r.base.mkdir()
    env = {"MAVEN_SKIP_RC": "true"}
    if source == "jvm.config":
        (root / ".mvn").mkdir()
        (root / ".mvn/jvm.config").write_text("-DskipTests=true\n")
    else:
        env[source] = "-DskipTests=true"
    step = {"id": "test", "runner": "maven", "argv": ["mvn", "install"]}
    inputs = r._effective_execution(step, env, r.base)
    assert inputs["inputs_complete"] is inputs["serial"] is False
    r.spec["requirements"][0]["validation"]["rule"] = "install"
    monkeypatch.setattr(r, "_probe", lambda *args, **kwargs: pytest.fail("Ambiguous inputs must not run an extra metadata goal"))
    assert r._local_repository(step, step["argv"], root, env, r.base, 5, inputs) is None


@pytest.mark.parametrize("args,env", [
    (["@args.txt"], {}), (["-f", "other/pom.xml"], {}), (["--file=other/pom.xml"], {}),
    ([], {"MAVEN_CONFIG": "/other/config"}), ([], {"MAVEN_PROJECTBASEDIR": "/other/project"}),
    ([], {"JAVA_TOOL_OPTIONS": "@jvm-args.txt"}),
])
def test_alternate_unobserved_maven_inputs_remain_unknown(setup, args, env):
    _, make = setup
    r = make()
    r.base.mkdir()
    step = {"id": "test", "runner": "maven", "argv": ["mvn", *args, "test"]}
    inputs = r._effective_execution(step, {"MAVEN_SKIP_RC": "true", **env}, r.base)
    assert inputs["inputs_complete"] is inputs["serial"] is False
    assert inputs["errors"]


@pytest.mark.parametrize("selector", [["-f", "pom.xml"], ["-f", "./pom.xml"], ["--file=pom.xml"]])
def test_original_dbutils_literal_pom_selector_remains_observed(setup, selector):
    _, make = setup
    r = make()
    r.base.mkdir()
    step = {"id": "test", "runner": "maven", "cwd": ".",
            "argv": ["mvn", "-B", *selector, "-V", "clean", "test", "--batch-mode"]}
    inputs = r._effective_execution(step, {"MAVEN_SKIP_RC": "true"}, r.base)
    assert inputs["inputs_complete"] is inputs["serial"] is True
    refs = {ref["source"]: ref for ref in inputs["configuration_evidence"]}
    assert bound_file(r.base, refs["pom.xml"]).read_bytes() == b"<project/>\n"
    proof = load_json(bound_file(r.base, refs["selected_pom_tracked_revision_proof"]))
    assert all(probe["exit_code"] == 0 for probe in proof["probes"])
    assert bound_file(r.base, proof["probes"][1]["stdout"]).read_bytes() == b"<project/>\n"


@pytest.mark.parametrize("condition", ["changed", "untracked", "missing", "runtime_override"])
def test_unknown_or_changed_selected_pom_never_gets_observed_policy(setup, condition):
    root, make = setup
    r = make()
    r.base.mkdir()
    env = {"MAVEN_SKIP_RC": "true"}
    if condition == "changed":
        (root / "pom.xml").write_text("<project>changed</project>")
    elif condition == "untracked":
        git(root, "rm", "--cached", "pom.xml")
    elif condition == "missing":
        (root / "pom.xml").unlink()
    else:
        env["MAVEN_ARGS"] = "-f pom.xml"
    step = {"id": "test", "runner": "maven", "cwd": ".", "argv": ["mvn", "-f", "pom.xml", "test"]}
    inputs = r._effective_execution(step, env, r.base)
    assert inputs["inputs_complete"] is inputs["serial"] is False
    assert inputs["errors"]


def test_compact_unreviewed_pom_selector_is_not_silently_ignored(setup):
    _, make = setup
    r = make()
    r.base.mkdir()
    step = {"id": "test", "runner": "maven", "cwd": ".", "argv": ["mvn", "-fpom.xml", "test"]}
    inputs = r._effective_execution(step, {"MAVEN_SKIP_RC": "true"}, r.base)
    assert inputs["inputs_complete"] is inputs["serial"] is False


def test_portable_install_mapping_uses_shared_compound_extension_rule():
    item = {"coordinates": {"group_id": "a", "artifact_id": "b", "version": "1"},
            "extension": "spdx.json", "classifier": None,
            "repository_relative_path": "a/b/1/b-1.spdx.json"}
    assert Recorder._repository_path(item) == "a/b/1/b-1.spdx.json"
