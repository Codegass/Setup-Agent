"""Native-image is a same-JVM capability, not a synonym for Java 21."""

import base64
import json
import os
import shlex
import subprocess
import sys

import pytest

from sag.benchmark.evaluator import runtime_result
from sag.benchmark.requirements import bound_file, requires_native_image, validate_requirements
from test_benchmark_recorder import setup as recorder_setup
from test_benchmark_requirement_evaluator import fixture, write
from test_sag_requirement_observer import setup as observer_setup, contract, receipt

NATIVE_VERSION = "native-image 21.0.12 2026-07-21\n"


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)


def require(spec, step_id):
    runtime = next(p for p in spec["preconditions"] if p["id"] == "runtime_conformance")
    runtime["steps"] = [{"step_id": step_id, "requires_native_image": True}]


def portable(
    recorder_setup, tmp_path, monkeypatch, *, native="present", print_home=True, required=True
):
    _, make = recorder_setup
    home = tmp_path / "jdk21"
    home.mkdir()
    path_tools = tmp_path / "path-tools"
    java_line = "    java.home = " + str(home) + "\n"
    executable(
        path_tools / "mvn",
        (
            "import os,sys\n"
            "if '--version' in sys.argv:\n"
            " print('Apache Maven 3.9.16\\nJava version: 21.0.12')\n"
            + (
                f" if '-XshowSettings:properties' in os.environ.get('MAVEN_OPTS',''): print({java_line!r})\n"
                if print_home
                else ""
            )
            + "else:\n assert '-XshowSettings:properties' not in os.environ.get('MAVEN_OPTS',''); print('frozen task')\n"
        ),
    )
    # A working binary on PATH must never fill in for the selected JDK.
    executable(path_tools / "native-image", "print('native-image 99 unrelated')\n")
    if native == "present":
        executable(home / "bin/native-image", f"print({NATIVE_VERSION!r}, end='')\n")
    elif native == "error":
        executable(
            home / "bin/native-image", "import sys; print('broken native image'); sys.exit(3)\n"
        )
    elif native == "other-jdk":
        other = tmp_path / "other-jdk/bin/native-image"
        executable(other, f"print({NATIVE_VERSION!r}, end='')\n")
        (home / "bin").mkdir()
        (home / "bin/native-image").symlink_to(other)
    monkeypatch.setenv("PATH", str(path_tools) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("MAVEN_OPTS", raising=False)

    def change_task(task):
        task["steps"][0].update(runner="maven", argv=["mvn", "verify"], java_major=21)

    def change_spec(spec):
        if required:
            require(spec, "test")

    recorder = make(task_change=change_task, spec_change=change_spec)
    recorder.start()
    record = recorder.step("test", timeout=5)
    return recorder, record


@pytest.mark.parametrize(
    "native,expected",
    [
        ("present", "passed"),
        ("missing", "failed"),
        ("error", "failed"),
        ("other-jdk", "unavailable"),
    ],
)
def test_portable_probes_only_observed_launcher_home(
    recorder_setup, tmp_path, monkeypatch, native, expected
):
    recorder, record = portable(recorder_setup, tmp_path, monkeypatch, native=native)
    result = runtime_result(recorder.base, record, recorder.task["steps"][0], True)
    assert result["status"] == expected
    assert result["launcher"]["status"] == "passed"
    probe = record["native_image_probe"]["probe"]
    assert probe["argv"][0] == str(tmp_path / "jdk21/bin/native-image")
    if native == "present":
        assert bound_file(recorder.base, probe).read_text() == NATIVE_VERSION


def test_portable_cannot_infer_home_from_environment(recorder_setup, tmp_path, monkeypatch):
    recorder, record = portable(recorder_setup, tmp_path, monkeypatch, print_home=False)
    assert record["native_image_probe"]["launcher_java_home"] is None
    assert "probe" not in record["native_image_probe"]
    assert (
        runtime_result(recorder.base, record, recorder.task["steps"][0], True)["status"]
        == "unavailable"
    )


def test_old_task_without_native_requirement_does_not_add_probe(
    recorder_setup, tmp_path, monkeypatch
):
    recorder, record = portable(
        recorder_setup, tmp_path, monkeypatch, native="missing", required=False
    )
    assert requires_native_image(recorder.spec, "test") is False
    assert record["native_image_probe"] is None
    assert (
        "java.home"
        not in bound_file(recorder.base, record["runtime"]["launcher_probe"]).read_text()
    )
    assert runtime_result(recorder.base, record, recorder.task["steps"][0])["status"] == "passed"


def live(observer_setup, monkeypatch, *, native="present", home_version=21):
    _, control, make = observer_setup
    original = control.execute
    requests = []

    def dispatch(command, **kwargs):
        request = json.loads(shlex.split(command)[-1])
        if request["operation"] != "probe":
            return original(command, **kwargs)
        requests.append(request)
        argv = request["argv"]
        result = {
            "files": [],
            "configs": [],
            "errors": [],
            "head": request["commit"],
            "probe_seconds": 0.1,
            "probe_status": "completed",
            "probe_exit_code": 0,
            "probe_executable": argv[0],
            "probe_stderr": "",
        }
        if argv[0] == "/jdk21/bin/java":
            output = f'    java.home = /jdk21\nopenjdk version "{home_version}.0.12"\n'
        else:
            assert argv == ["/jdk21/bin/native-image", "--version"]
            output = NATIVE_VERSION
            if native == "missing":
                result.update(
                    probe_status="launch_failed", probe_exit_code=None, probe_executable=None
                )
                output = "No such file"
            elif native == "error":
                result["probe_exit_code"] = 3
            elif native == "other-jdk":
                result["probe_executable"] = "/another-jdk/bin/native-image"
        result["probe_stdout"] = base64.b64encode(output.encode()).decode()
        return {"success": True, "exit_code": 0, "output": json.dumps(result)}

    monkeypatch.setattr(control, "execute", dispatch)

    def definitions(task, spec):
        task["steps"][0]["java_major"] = 21
        require(spec, "verify")

    observer = make(definitions)
    frozen = contract(observer)
    observed_receipt = receipt(
        frozen,
        effective_jdk={
            "runtime_authority": "dispatch_probe",
            "major": "21",
            "provenance": {
                "dispatch_runtime": {
                    "executable": "/jdk21/bin/java",
                    "major": "21",
                    "version": "21.0.12",
                }
            },
        },
    )
    observer.before_contract(frozen)
    observer.after_receipt(observed_receipt)
    record = observer.export_invocation(frozen["contract_id"])
    record.update(
        invocation_id=frozen["contract_id"],
        runtime={
            "source": "sag_host_authorized_receipt",
            "sag_dispatch_receipt": write(observer.base, "receipt.json", observed_receipt),
        },
    )
    return observer, record, requests


@pytest.mark.parametrize(
    "native,expected",
    [
        ("present", "passed"),
        ("missing", "failed"),
        ("error", "failed"),
        ("other-jdk", "unavailable"),
    ],
)
def test_live_capability_has_same_home_and_raw_proof_contract(
    observer_setup, monkeypatch, native, expected
):
    observer, record, requests = live(observer_setup, monkeypatch, native=native)
    result = runtime_result(observer.base, record, observer.task["steps"][0], True)
    assert result["status"] == expected
    assert [r["argv"] for r in requests] == [
        ["/jdk21/bin/java", "-XshowSettings:properties", "-version"],
        ["/jdk21/bin/native-image", "--version"],
    ]
    if native == "present":
        assert (
            bound_file(observer.base, record["native_image_probe"]["probe"]).read_text().strip()
            == NATIVE_VERSION.strip()
        )
        assert result["native_image"]["reason"] == "observed_native_image_capability"


def test_changed_jvm_after_dispatch_cannot_satisfy_capability(observer_setup, monkeypatch):
    observer, record, _ = live(observer_setup, monkeypatch, home_version=17)
    result = runtime_result(observer.base, record, observer.task["steps"][0], True)
    assert result["status"] == "unavailable"


@pytest.mark.parametrize("native_present,expected", [(True, "passed"), (False, "failed")])
def test_live_fixed_transport_captures_version_and_missing_executable(
    observer_setup, monkeypatch, tmp_path, native_present, expected
):
    _, control, make = observer_setup
    home = tmp_path / "observed-jdk"
    executable(
        home / "bin/java",
        f"print('    java.home = {home}'); print('openjdk version \"21.0.12\"')\n",
    )
    if native_present:
        executable(home / "bin/native-image", f"print({NATIVE_VERSION!r}, end='')\n")

    def fixed_transport(command, **kwargs):
        assert kwargs["truncate_output"] is False
        process = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=5)
        return {
            "success": process.returncode == 0,
            "exit_code": process.returncode,
            "output": process.stdout,
        }

    monkeypatch.setattr(control, "execute", fixed_transport)

    def definitions(task, spec):
        task["steps"][0]["java_major"] = 21
        require(spec, "verify")

    observer = make(definitions)
    frozen = contract(observer)
    observed_receipt = receipt(
        frozen,
        effective_jdk={
            "runtime_authority": "dispatch_probe",
            "major": "21",
            "provenance": {
                "dispatch_runtime": {"executable": str(home / "bin/java"), "major": "21"}
            },
        },
    )
    observer.before_contract(frozen)
    observer.after_receipt(observed_receipt)
    record = observer.export_invocation(frozen["contract_id"])
    record.update(
        invocation_id=frozen["contract_id"],
        runtime={
            "source": "sag_host_authorized_receipt",
            "sag_dispatch_receipt": write(observer.base, "receipt.json", observed_receipt),
        },
    )
    result = runtime_result(observer.base, record, observer.task["steps"][0], True)
    assert result["status"] == expected
    assert bound_file(observer.base, record["native_image_probe"]["probe"]["observation"]).is_file()


@pytest.mark.parametrize(
    "mutation", ["run", "invocation", "home", "home_executable", "argv", "bytes", "missing"]
)
def test_native_capability_is_not_borrowed_or_derived_from_unbound_metadata(
    observer_setup, monkeypatch, mutation
):
    observer, record, _ = live(observer_setup, monkeypatch)
    observation = record["native_image_probe"]
    if mutation == "run":
        observation["run_id"] = "another"
    elif mutation == "invocation":
        observation["invocation_id"] = "another"
    elif mutation == "home":
        observation["launcher_java_home"] = "/another"
    elif mutation == "home_executable":
        observation["java_home_probe"]["executable"] = "/another/bin/java"
    elif mutation == "argv":
        observation["probe"]["argv"] = ["native-image", "--version"]
    elif mutation == "bytes":
        (observer.base / observation["probe"]["path"]).write_text("replaced")
    else:
        record.pop("native_image_probe")
    assert (
        runtime_result(observer.base, record, observer.task["steps"][0], True)["status"]
        == "unavailable"
    )


def test_declared_precondition_is_consumed_by_shared_evaluate(fixture):
    _, spec, _, _, _, score = fixture
    require(spec, "verify")
    result = score()
    assert result["preconditions"][1]["status"] == "unavailable"
    assert result["status"] != "complete"


@pytest.mark.parametrize("value", ["true", 1, None])
def test_capability_declaration_cannot_hide_non_boolean_values(fixture, value):
    task, spec, _, _, _, _ = fixture
    require(spec, "verify")
    spec["preconditions"][1]["steps"][0]["requires_native_image"] = value
    with pytest.raises(ValueError, match="boolean"):
        validate_requirements(spec, task)


def test_conflicting_duplicate_runtime_steps_are_invalid(fixture):
    task, spec, _, _, _, _ = fixture
    require(spec, "verify")
    spec["preconditions"][1]["steps"].append({"step_id": "verify", "requires_native_image": False})
    with pytest.raises(ValueError, match="Duplicate runtime"):
        validate_requirements(spec, task)
