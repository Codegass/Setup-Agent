"""Read real tiny files through the clean transport; never run Maven or Java."""

import base64
from copy import deepcopy
import hashlib
import json
import shlex
import subprocess
import zipfile

import pytest

from sag.agent.worktree_evidence import record_contract_worktree, record_receipt_worktree
from sag.benchmark.native_evidence import artifact_proof, junit_counts
from sag.benchmark.requirements import bound_file, canonical_digest, normalize_task
from sag.benchmark.sag_observer import SAGRequirementObserver


class LocalControl:
    def __init__(self, root):
        self.root = root
        self.env = {
            "MAVEN_SKIP_RC": "true",
            "MAVEN_ARGS": "",
            "MAVEN_CONFIG": "",
            "MAVEN_PROJECTBASEDIR": "",
            "MAVEN_OPTS": "",
            "JAVA_TOOL_OPTIONS": "",
            "_JAVA_OPTIONS": "",
            "JDK_JAVA_OPTIONS": "",
        }
        self.requests = []
        self.break_snapshot = False
        self.break_copy = False
        self.repository = None

    def _default_exec_environment(self):
        return dict(self.env)

    def execute(self, command, **kwargs):
        argv = shlex.split(command)
        request = json.loads(argv[-1])
        self.requests.append(request)
        assert kwargs["truncate_output"] is False
        if request["operation"] == "snapshot" and self.break_snapshot:
            return {"success": True, "exit_code": 0, "output": '{"errors":[]}'}
        if request["operation"] == "probe":
            # Metadata preparation is mocked; only fixed inventory/copy code is executed.
            assert "help:evaluate" in request["argv"]
            assert not {"clean", "install", "verify", "package"} & set(request["argv"])
            payload = {
                "files": [],
                "configs": [],
                "errors": [],
                "head": request["commit"],
                "probe_stdout": base64.b64encode(
                    ("Apache Maven 3.9.9\n" + str(self.repository)).encode()
                ).decode(),
                "probe_stderr": "",
                "probe_exit_code": 0,
                "probe_executable": "/opt/maven/bin/mvn",
                "probe_seconds": 0.25,
            }
            return {"success": True, "exit_code": 0, "output": json.dumps(payload)}
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        output = result.stdout
        if request["operation"] == "chunk" and self.break_copy:
            payload = json.loads(output)
            payload["data"] = base64.b64encode(b"short").decode()
            output = json.dumps(payload)
        return {"success": result.returncode == 0, "exit_code": result.returncode, "output": output}


@pytest.fixture
def setup(tmp_path):
    root = (tmp_path / "project").resolve()
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "source.txt").write_text("frozen\n")
    (root / "pom.xml").write_text("<project/>\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Observer",
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
            "schema_version": 1,
            "repo": "example/tiny",
            "sha": sha,
            "steps": [
                {
                    "id": "verify",
                    "runner": "maven",
                    "argv": ["mvn", "clean", "verify"],
                    "cwd": ".",
                    "java_major": 17,
                }
            ],
        }
    )
    rows = [
        {
            "id": "tests",
            "step_id": "verify",
            "kind": "test",
            "subtype": "unit",
            "module": "tiny",
            "scope": {"status": "declared", "module_path": "."},
            "validation": {"rule": "junit", "goals": ["surefire:test"]},
        },
        {
            "id": "package",
            "step_id": "verify",
            "kind": "package",
            "module": "tiny",
            "scope": {"status": "declared", "module_path": "."},
            "validation": {"rule": "artifact", "goals": ["jar:jar"]},
            "expectations": {
                "artifacts": [
                    {
                        "path": "target/tiny.jar",
                        "role": "main",
                        "module": "tiny",
                        "format": "jar",
                        "extension": "jar",
                    }
                ]
            },
        },
    ]
    spec = {
        "schema_version": 2,
        "policy_version": "ci-requirements-v2",
        "task_sha256": canonical_digest(task),
        "annotation_completeness": {"status": "complete"},
        "preconditions": [{"id": "worktree_integrity"}, {"id": "runtime_conformance"}],
        "requirements": rows,
    }
    control = LocalControl(root)

    def make(change=None):
        t, s = deepcopy(task), deepcopy(spec)
        if change:
            change(t, s)
        t = normalize_task(t)
        s["task_sha256"] = canonical_digest(t)
        observer = SAGRequirementObserver(
            tmp_path / "session", "run-1", str(root), t, s, control.execute
        )
        control.requirement_observer = observer
        return observer

    return root, control, make


def contract(observer, index=0, suffix="000000000001"):
    step = observer.task["steps"][index]
    return {
        "contract_id": "ic-" + suffix,
        "contract_hash": "a" * 64,
        "run_id": observer.run_id,
        "target_sha": observer.task["sha"],
        "effective_tool": step["runner"] if step["runner"] != "native" else "bash",
        "expected_argv": shlex.join(step["argv"][1:]),
        "expected_cwd": (
            str(observer.root) if step["cwd"] == "." else str(observer.root) + "/" + step["cwd"]
        ),
        "requested_call": {"params": {"command": shlex.join(step["argv"])}},
    }


def receipt(c, **changes):
    return {
        **c,
        "receipt_id": "receipt-1",
        "actual_cwd": c["expected_cwd"],
        "exit_code": 0,
        "lifecycle": "finished",
        **changes,
    }


def outputs(root):
    reports = root / "target/surefire-reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "TEST-Tiny.xml").write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="one" classname="Tiny"/></testsuite>'
    )
    with zipfile.ZipFile(root / "target/tiny.jar", "w") as archive:
        archive.writestr("Tiny.class", b"compiled bytes")


def test_dispatch_hooks_capture_fresh_declared_artifact_and_report_bytes(setup):
    root, control, make = setup
    observer = make()
    c = contract(observer)
    record_contract_worktree(control.execute, c)
    outputs(root)
    record_receipt_worktree(control.execute, receipt(c))
    record = observer.export_invocation(c["contract_id"])
    assert record["reports_collection_complete"] is True
    assert record["artifacts_collection_complete"] is True
    assert record["effective_execution"]["inputs_complete"] is True
    assert record["effective_execution"]["serial"] is True
    assert junit_counts(observer.base, record["reports"])["passed"] == 1
    assert artifact_proof(
        observer.base,
        observer.spec["requirements"][1]["expectations"]["artifacts"],
        record["artifacts"],
    )
    assert record["artifact_observations"][0]["state"] == "present"
    for ref in record["reports"] + record["artifacts"]:
        assert ref["fresh"] is True
        assert bound_file(observer.base, ref).is_file()
    (root / "target/tiny.jar").unlink()
    assert bound_file(observer.base, record["artifacts"][0]).is_file()
    assert record["preparation_seconds"] >= 0 and record["observation_seconds"] >= 0


@pytest.mark.parametrize("stale", [False, True])
def test_same_artifact_bytes_preserve_each_declared_role(setup, stale):
    root, control, make = setup

    def change(task, spec):
        augmented = deepcopy(spec["requirements"][1])
        augmented["id"] = "augmented-package"
        augmented["validation"]["goals"] = ["moditect:add-module-info"]
        augmented["expectations"]["artifacts"][0]["role"] = "augmented"
        spec["requirements"].append(augmented)

    if stale:
        outputs(root)
    observer = make(change)
    c = contract(observer)
    record_contract_worktree(control.execute, c)
    if not stale:
        outputs(root)
    record_receipt_worktree(control.execute, receipt(c))
    record = observer.export_invocation(c["contract_id"])
    assert {ref["role"] for ref in record["artifacts"]} == {"main", "augmented"}
    assert len({ref["sha256"] for ref in record["artifacts"]}) == 1
    assert all(ref["fresh"] is (not stale) for ref in record["artifacts"])
    for row in observer.spec["requirements"][1:]:
        assert artifact_proof(observer.base, row["expectations"]["artifacts"], record["artifacts"]) is (not stale)


@pytest.mark.parametrize("replace", [False, True])
def test_native_observer_recognizes_replacement_with_identical_bytes_and_mtime(setup, replace):
    import shutil

    root, control, make = setup
    outputs(root)
    artifact = root / "target/tiny.jar"
    original_inode, original_mtime = artifact.stat().st_ino, artifact.stat().st_mtime_ns
    observer = make()
    c = contract(observer)
    record_contract_worktree(control.execute, c)
    if replace:
        temporary = artifact.with_suffix(".replacement")
        shutil.copy2(artifact, temporary)
        temporary.replace(artifact)
    assert artifact.stat().st_mtime_ns == original_mtime
    assert (artifact.stat().st_ino != original_inode) is replace
    record_receipt_worktree(control.execute, receipt(c))
    record = observer.export_invocation(c["contract_id"])
    assert record["artifacts"][0]["fresh"] is replace
    assert artifact_proof(observer.base, observer.spec["requirements"][1]["expectations"]["artifacts"], record["artifacts"]) is replace


def test_stale_outputs_are_not_certified_and_missing_is_explicit(setup):
    root, _, make = setup
    outputs(root)
    observer = make()
    c = contract(observer)
    observer.before_contract(c)
    (root / "target/tiny.jar").unlink()
    observer.after_receipt(receipt(c))
    record = observer.export_invocation(c["contract_id"])
    assert record["reports"][0]["fresh"] is False
    assert record["artifacts"] == []
    assert record["artifact_observations"][0]["state"] == "missing"
    assert record["artifacts_collection_complete"] is True


@pytest.mark.parametrize("failure", ["before", "after", "copy"])
def test_failed_inventory_or_copy_never_certifies_outputs(setup, failure):
    root, control, make = setup
    observer = make()
    c = contract(observer)
    control.break_snapshot = failure == "before"
    observer.before_contract(c)
    outputs(root)
    control.break_snapshot = failure == "after"
    control.break_copy = failure == "copy"
    observer.after_receipt(receipt(c))
    record = observer.export_invocation(c["contract_id"])
    assert record["reports_collection_complete"] is False
    assert record["artifacts_collection_complete"] is False
    assert not any(r["fresh"] for r in record["reports"] + record["artifacts"])


def test_symlink_report_directory_never_escapes_frozen_scope(setup, tmp_path):
    root, _, make = setup
    outsider = tmp_path / "outside"
    outsider.mkdir()
    (outsider / "TEST-Secret.xml").write_text("not allowed")
    (root / "target").mkdir()
    (root / "target/surefire-reports").symlink_to(outsider)
    observer = make()
    c = contract(observer)
    observer.before_contract(c)
    observer.after_receipt(receipt(c))
    record = observer.export_invocation(c["contract_id"])
    assert record["reports"] == [] and record["reports_collection_complete"] is False
    assert "symlink" in str(record["errors"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "different"),
        ("target_sha", "b" * 40),
        ("contract_hash", "b" * 64),
        ("actual_cwd", "/another"),
    ],
)
def test_mismatched_receipt_does_not_create_observation(setup, field, value):
    _, _, make = setup
    observer = make()
    c = contract(observer)
    observer.before_contract(c)
    observer.after_receipt(receipt(c, **{field: value}))
    assert observer.export_invocation(c["contract_id"]) is None


def test_changed_maven_arguments_disable_serial_inference(setup):
    root, control, make = setup
    observer = make()
    c = contract(observer)
    observer.before_contract(c)
    control.env["MAVEN_ARGS"] = "-T 4"
    outputs(root)
    observer.after_receipt(receipt(c))
    inputs = observer.export_invocation(c["contract_id"])["effective_execution"]
    assert inputs["inputs_complete"] is False and inputs["serial"] is False
    assert "changed" in " ".join(inputs["errors"])


@pytest.mark.parametrize("source", ["MAVEN_OPTS", "JAVA_TOOL_OPTIONS", "jvm.config"])
def test_jvm_property_inputs_cannot_bypass_execution_semantics(setup, source):
    root, control, make = setup
    if source == "jvm.config":
        (root / ".mvn").mkdir()
        (root / ".mvn/jvm.config").write_text("-DskipTests=true")
    else:
        control.env[source] = "-DskipTests=true"
    observer = make(install_spec)
    c = contract(observer)
    observer.before_contract(c)
    observer.after_receipt(receipt(c))
    record = observer.export_invocation(c["contract_id"])
    assert record["effective_execution"]["inputs_complete"] is False
    assert not any(r["operation"] == "probe" for r in control.requests)


@pytest.mark.parametrize("selector", [["-f", "pom.xml"], ["-f", "./pom.xml"], ["--file=pom.xml"]])
def test_original_dbutils_frozen_pom_selector_is_observed_with_pinned_bytes(setup, selector):
    _, _, make = setup

    def change(task, spec):
        # Literal original DbUtils CI command; the fixture supplies only a tiny
        # tracked POM and no Maven/build execution is performed.
        task["steps"][0]["argv"] = ["mvn", "-B", *selector, "-V", "clean", "test", "--batch-mode"]

    observer = make(change)
    c = contract(observer)
    observer.before_contract(c)
    observer.after_receipt(receipt(c))
    observed = observer.export_invocation(c["contract_id"])
    inputs = observed["effective_execution"]
    assert inputs["inputs_complete"] is inputs["serial"] is True
    assert inputs["selected_pom"]["producer_relative_path"] == "pom.xml"
    before = json.loads(bound_file(observer.base, inputs["configuration_evidence"][0]).read_bytes())
    proof = before["selected_pom"]
    assert base64.b64decode(proof["data"]) == b"<project/>\n"
    assert base64.b64decode(proof["probes"][0]["stdout"]) == b"pom.xml\0"
    assert base64.b64decode(proof["probes"][1]["stdout"]) == b"<project/>\n"
    assert all(p["exit_code"] == 0 for p in proof["probes"])


@pytest.mark.parametrize(
    "condition",
    [
        "changed",
        "untracked",
        "missing",
        "symlink",
        "runtime_override",
        "config_override",
        "alternate",
        "compact",
    ],
)
def test_unreviewed_pom_selection_never_gets_complete_inputs(setup, tmp_path, condition):
    root, control, make = setup
    if condition == "changed":
        (root / "pom.xml").write_text("<project>changed</project>")
    elif condition == "untracked":
        subprocess.run(
            ["git", "-C", str(root), "rm", "--cached", "pom.xml"], check=True, capture_output=True
        )
    elif condition == "missing":
        (root / "pom.xml").unlink()
    elif condition == "symlink":
        outside = tmp_path / "outside-pom.xml"
        outside.write_text("<project/>\n")
        (root / "pom.xml").unlink()
        (root / "pom.xml").symlink_to(outside)
    elif condition == "runtime_override":
        control.env["MAVEN_ARGS"] = "-f pom.xml"
    elif condition == "config_override":
        (root / ".mvn").mkdir()
        (root / ".mvn/maven.config").write_text("--file=pom.xml")

    def change(task, spec):
        selector = (
            ["-f", "other.xml"]
            if condition == "alternate"
            else ["-fpom.xml"] if condition == "compact" else ["-f", "pom.xml"]
        )
        task["steps"][0]["argv"] = ["mvn", *selector, "test"]

    observer = make(change)
    c = contract(observer)
    observer.before_contract(c)
    observer.after_receipt(receipt(c))
    inputs = observer.export_invocation(c["contract_id"])["effective_execution"]
    assert inputs["inputs_complete"] is inputs["serial"] is False
    assert inputs["errors"]


def test_selected_pom_changed_after_dispatch_invalidates_execution_inputs(setup):
    root, _, make = setup

    def change(task, spec):
        task["steps"][0]["argv"] = ["mvn", "-f", "pom.xml", "test"]

    observer = make(change)
    c = contract(observer)
    observer.before_contract(c)
    (root / "pom.xml").write_text("<project>late change</project>")
    observer.after_receipt(receipt(c))
    inputs = observer.export_invocation(c["contract_id"])["effective_execution"]
    assert inputs["inputs_complete"] is inputs["serial"] is False


def install_spec(task, spec):
    task["steps"][0]["argv"] = ["mvn", "--show-version", "clean", "install"]
    spec["requirements"][1].update(
        kind="install", validation={"rule": "install", "goals": ["install:install"]}
    )
    artifacts = []
    for extension, role in (("jar", "installed_main"), ("pom", "pom")):
        artifacts.append(
            {
                "repository_relative_path": f"org/example/tiny/1.0/tiny-1.0.{extension}",
                "role": role,
                "module": "tiny",
                "coordinates": {"group_id": "org.example", "artifact_id": "tiny", "version": "1.0"},
                "extension": extension,
            }
        )
    spec["requirements"][1]["expectations"]["artifacts"] = artifacts


@pytest.mark.parametrize("budget", [None, 10, 1800])
def test_installed_jar_and_pom_use_observed_repository_and_frozen_coordinates(setup, tmp_path, budget, monkeypatch):
    root, control, make = setup
    control.repository = (tmp_path / "actual-repository").resolve()
    control.repository.mkdir()
    observer = make(install_spec)
    c = contract(observer)
    c["requested_call"]["params"]["timeout"] = budget
    original_execute = observer.execute
    def checked_transport(_owner, command, **kwargs):
        request = json.loads(shlex.split(command)[-1])
        if request["operation"] == "probe":
            assert request["timeout"] == (600 if budget is None else budget)
            assert kwargs["timeout"] > request["timeout"]
        return original_execute(command, **kwargs)
    from types import MethodType
    monkeypatch.setattr(observer, "execute", MethodType(checked_transport, control))
    observer.before_contract(c)
    outputs(root)
    destination = control.repository / "org/example/tiny/1.0"
    destination.mkdir(parents=True)
    (destination / "tiny-1.0.jar").write_bytes((root / "target/tiny.jar").read_bytes())
    (destination / "tiny-1.0.pom").write_text("<project/>")
    observer.after_receipt(receipt(c))
    record = observer.export_invocation(c["contract_id"])
    assert record["local_repository"]["path"] == str(control.repository)
    assert record["local_repository"]["source"] == "maven_settings_probe"
    assert record["local_repository"]["seconds"] == 0.25
    assert len(record["artifacts"]) == 2
    assert {a["role"] for a in record["artifacts"]} == {"installed_main", "pom"}
    assert artifact_proof(
        observer.base,
        observer.spec["requirements"][1]["expectations"]["artifacts"],
        record["artifacts"],
    )
    assert record["artifacts_collection_complete"] is True
    assert len([r for r in control.requests if r["operation"] == "probe"]) == 1


@pytest.mark.parametrize("source", ["MAVEN_ARGS", "config", "wrapper", "MAVEN_CONFIG"])
def test_unknown_or_goal_appending_inputs_never_dispatch_metadata_probe(setup, source):
    root, control, make = setup
    if source == "config":
        (root / ".mvn").mkdir()
        (root / ".mvn/maven.config").write_text("clean install")
    elif source in {"MAVEN_ARGS", "MAVEN_CONFIG"}:
        control.env[source] = "clean install"

    def change(task, spec):
        install_spec(task, spec)
        if source == "wrapper":
            task["steps"][0]["argv"][0] = "./mvnw"

    observer = make(change)
    c = contract(observer)
    observer.before_contract(c)
    outputs(root)
    observer.after_receipt(receipt(c))
    record = observer.export_invocation(c["contract_id"])
    assert record["local_repository"]["status"] == "unavailable"
    assert not any(r["operation"] == "probe" for r in control.requests)
    assert record["reports_collection_complete"] is True
    assert record["artifacts_collection_complete"] is False


def test_native_execution_requires_identical_previously_produced_binary(setup):
    root, _, make = setup

    def change(task, spec):
        spec["requirements"][1]["expectations"]["artifacts"] = [
            {"path": "target/tiny", "role": "native_executable", "module": "tiny"}
        ]
        task["steps"].append(
            {"id": "native", "runner": "native", "argv": ["./target/tiny", "--smoke"], "cwd": "."}
        )
        spec["requirements"].append(
            {
                "id": "native-test",
                "step_id": "native",
                "kind": "test",
                "scope": {"status": "declared", "module_path": "."},
                "validation": {"rule": "native_exit"},
            }
        )

    observer = make(change)
    c = contract(observer)
    observer.before_contract(c)
    outputs(root)
    binary = root / "target/tiny"
    binary.write_bytes(b"\x7fELFstand-in-data-never-executed")
    observer.after_receipt(receipt(c))
    native = contract(observer, 1, "000000000002")
    observer.before_contract(native)
    proof = {
        "feature": "native_executable_sha256",
        "probe_exit_code": "0",
        "observation": hashlib.sha256(binary.read_bytes()).hexdigest(),
    }
    observer.after_receipt(receipt(native, capability_observations=[proof]))
    observed = observer.export_invocation(native["contract_id"])
    assert observed["native_executable"]["producer_invocation_id"] == c["contract_id"]
    assert observed["native_executable"]["producer_relative_path"] == "target/tiny"
