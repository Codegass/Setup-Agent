"""Version capture must work with actual multiline Gradle output."""

import hashlib
import json
from pathlib import Path
import shlex
import subprocess

import pytest

from sag.agent.invocation_receipts import record_invocation, toolchain_fingerprint

FIXTURES = Path(__file__).parent / "fixtures/gradle_version"
PROVENANCE = json.loads((FIXTURES / "provenance.json").read_text())


def probe(tmp_path, output):
    work = tmp_path / "path with spaces"
    work.mkdir()
    raw = work / "version.txt"
    raw.write_text(output)
    wrapper = work / "gradlew"
    wrapper.write_text("#!/bin/sh\ncat " + shlex.quote(str(raw)) + "\n")
    wrapper.chmod(0o755)
    calls = []

    def execute(command):
        calls.append(command)
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=5)
        return {"output": result.stdout, "exit_code": result.returncode}

    result = toolchain_fingerprint(
        execute,
        executable="./gradlew",
        version_flag="-v",
        working_directory=str(work),
        tool="gradle",
    )
    assert len(calls) == 1
    return result


@pytest.mark.parametrize("observation", PROVENANCE["observations"], ids=lambda r: r["name"])
def test_native_gradle_version_output_survives_a_leading_blank_line(tmp_path, observation):
    body = (FIXTURES / observation["output"]).read_bytes()
    assert hashlib.sha256(body).hexdigest() == observation["sha256"]
    assert body.splitlines()[0] == b""
    result = probe(tmp_path, body.decode())
    assert result["executable"] == "./gradlew"
    assert result["version"] == "Gradle 8.5"


@pytest.mark.parametrize(
    "output",
    [
        "\n",
        "Welcome to Gradle 8.5!\n",
        "ERROR: Could not download gradle-8.5-bin.zip\n",
        "Gradle unknown\n",
    ],
)
def test_diagnostics_and_wrapper_declarations_are_not_measured_versions(tmp_path, output):
    assert probe(tmp_path, output) == {"executable": "./gradlew"}


def test_conflicting_native_version_lines_do_not_pick_a_preferred_version(tmp_path):
    assert probe(tmp_path, "Gradle 8.5\nGradle 8.6\n") == {"executable": "./gradlew"}


def test_version_capture_has_a_fixed_line_bound(tmp_path):
    assert probe(tmp_path, "warning\n" * 64 + "Gradle 8.5\n") == {"executable": "./gradlew"}


@pytest.mark.parametrize("observation", PROVENANCE["observations"], ids=lambda r: r["name"])
def test_gradle_version_is_carried_through_receipt_publication(observation):
    from test_invocation_receipts import FakeExecute, receipts_written
    from test_receipt_v2_and_assessments import V2_MANIFEST, ok

    body = (FIXTURES / observation["output"]).read_text()
    execute = FakeExecute(rules=[("command -v", ok("./gradlew\nSAGTOOLCHAIN\n" + body))])
    record_invocation(
        execute,
        tool="gradle",
        attempt=1,
        requested_action="build",
        effective_action="build",
        argv="./gradlew build",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={},
        output="BUILD SUCCESSFUL",
        requirements=V2_MANIFEST,
    )
    (receipt,) = receipts_written(execute.commands)
    assert receipt["toolchain_fingerprint"] == {"executable": "./gradlew", "version": "Gradle 8.5"}
