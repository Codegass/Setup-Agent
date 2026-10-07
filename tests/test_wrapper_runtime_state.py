"""Producer-reviewed runtime state never becomes a blanket .mvn exemption."""

import base64
from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest

from sag.benchmark.requirements import bound_file, canonical_digest
from sag.benchmark.wrapper_review import (
    make_launcher_review,
    snapshot_inputs,
    certified_invocation_inputs,
)
from test_benchmark_wrapper_review import fixture, portable, sag

STATE = ".mvn/.develocity/develocity-workspace-id"


def extension(fixture, version="2.5.0"):
    root, task, spec, env, executable = fixture
    manifest = root / ".mvn/extensions.xml"
    manifest.write_text(
        "<extensions><extension><groupId>com.gradle</groupId><artifactId>develocity-maven-extension</artifactId><version>"
        + version
        + "</version></extension></extensions>"
    )
    subprocess.run(["git", "-C", str(root), "add", ".mvn/extensions.xml"], check=True)
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
            "extension fixture",
        ],
        check=True,
    )
    task["sha"] = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    paths = [item["path"] for item in spec["steps"][0]["launcher_review"]["files"]] + [
        ".mvn/extensions.xml"
    ]
    spec["steps"][0]["launcher_review"] = make_launcher_review(
        task["sha"], {path: (root / path).read_bytes() for path in paths}
    )
    spec["task_sha256"] = canonical_digest(task)


def state(root, value):
    path = root / STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


@pytest.mark.parametrize("transition", ["absent", "unchanged", "created", "updated"])
def test_state_is_recorded_and_can_change_without_changing_launcher_inputs(
    fixture, tmp_path, transition
):
    extension(fixture)
    root, task, spec, _, executable = fixture
    if transition in {"unchanged", "updated"}:
        state(root, b"a" * 26)
    if transition in {"created", "updated"}:
        executable.write_text(
            executable.read_text().replace(
                "print('[INFO] BUILD SUCCESS')",
                "q=Path('"
                + STATE
                + "');q.parent.mkdir(parents=True,exist_ok=True);q.write_bytes(b'b'*26)\nprint('[INFO] BUILD SUCCESS')",
            )
        )
    recorder, invocation, score = portable(fixture, tmp_path)
    assert [r["status"] for r in score["requirements"]] == ["passed", "passed"]
    assert invocation["effective_execution"]["inputs_complete"]
    evidence = invocation["effective_execution"]["wrapper_evidence"]
    observations = [
        json.loads(bound_file(recorder.base, evidence[key]).read_text())["runtime_state"][0]
        for key in ("before", "after")
    ]
    assert [r["present"] for r in observations] == {
        "absent": [False, False],
        "unchanged": [True, True],
        "created": [False, True],
        "updated": [True, True],
    }[transition]
    for r in observations:
        if r["present"]:
            assert r["bytes"] == 26
            assert len(base64.b64decode(r["data"])) == 26
    if transition in {"created", "absent"}:
        (root / STATE).unlink(missing_ok=True)
    elif transition == "updated":
        state(root, b"a" * 26)
    observer, observed, verified, command = sag(fixture, tmp_path)
    assert command.returncode == 0 and verified["inputs_complete"] and verified["serial"]
    evidence = observed["effective_execution"]["wrapper_evidence"]
    again = [
        json.loads(bound_file(observer.base, evidence[key]).read_text())["runtime_state"][0]
        for key in ("before", "after")
    ]
    assert again == observations


@pytest.mark.parametrize(
    "bad",
    [
        "unknown_producer",
        "new_version",
        "invalid_content",
        "invalid_size",
        "symlink",
        "extra_file",
        "tracked_config",
    ],
)
def test_only_the_reviewed_state_is_exempt_and_primary_errors_are_retained(fixture, tmp_path, bad):
    if bad != "unknown_producer":
        extension(fixture, "2.6.0" if bad == "new_version" else "2.5.0")
    root, task, spec, _, executable = fixture
    state(root, b"a" * 26)
    if bad == "invalid_content":
        state(root, b"8" * 26)
    elif bad == "invalid_size":
        state(root, b"a" * 27)
    elif bad == "symlink":
        (root / STATE).unlink()
        (root / STATE).symlink_to(root / "pom.xml")
    elif bad == "extra_file":
        (root / ".mvn/.develocity/options").write_text("-T8")
    elif bad == "tracked_config":
        executable.write_text(
            executable.read_text().replace(
                "print('[INFO] BUILD SUCCESS')",
                "Path('.mvn/maven.config').write_text('-V -T8\\n')\nprint('[INFO] BUILD SUCCESS')",
            )
        )
    recorder, invocation, score = portable(fixture, tmp_path)
    assert invocation["exit_code"] == 0
    assert not invocation["effective_execution"]["inputs_complete"]
    assert [r["status"] for r in score["requirements"]] == ["unavailable", "unavailable"]
    reasons = invocation["effective_execution"]["errors"]
    assert reasons and not any("Missing evidence file reference" in r for r in reasons)
    assert any(
        any(word in r for word in ("inventory", "identifier", "Symlink", "pinned bytes"))
        for r in reasons
    )
    _, observed, verified, command = sag(fixture, tmp_path)
    assert command.returncode == 0
    assert not verified["inputs_complete"]
    assert observed["effective_execution"]["errors"]


def test_removing_state_observation_cannot_invent_absence(fixture, tmp_path):
    extension(fixture)
    root, task, spec, _, _ = fixture
    state(root, b"a" * 26)
    recorder, invocation, _ = portable(fixture, tmp_path)
    proof = invocation["effective_execution"]["wrapper_evidence"]
    before = json.loads(bound_file(recorder.base, proof["before"]).read_text())
    before.pop("runtime_state")
    with pytest.raises(ValueError, match="state observations"):
        snapshot_inputs(
            spec["steps"][0]["launcher_review"],
            task,
            task["steps"][0],
            before,
            "r1",
            invocation["invocation_id"],
            "acceptance_before",
        )


def test_runtime_state_rule_cannot_be_claimed_without_the_declared_producer(fixture, tmp_path):
    from sag.benchmark.wrapper_review import (
        DEVELOCITY_STATE,
        capture_request,
        capture_wrapper_inputs,
    )

    root, task, spec, env, _ = fixture
    review = deepcopy(spec["steps"][0]["launcher_review"])
    review["runtime_state"] = [deepcopy(DEVELOCITY_STATE)]
    with pytest.raises(ValueError, match="Unreviewed runtime state"):
        capture_request(task, task["steps"][0], review, root, "r1", "i1", "acceptance_before", env)


def test_tracked_identifier_remains_an_immutable_source_input(fixture):
    extension(fixture)
    root, task, spec, env, _ = fixture
    state(root, b"a" * 26)
    paths = [item["path"] for item in spec["steps"][0]["launcher_review"]["files"]] + [STATE]
    review = make_launcher_review(task["sha"], {path: (root / path).read_bytes() for path in paths})
    assert "runtime_state" not in review
    assert STATE in {item["path"] for item in review["files"]}


@pytest.mark.parametrize("ambiguous", ["wrong_root", "duplicate_version"])
def test_extension_declaration_must_have_one_unambiguous_reviewed_producer(fixture, ambiguous):
    from sag.benchmark.wrapper_review import runtime_state_for_files

    declaration = "<extension><groupId>com.gradle</groupId><artifactId>develocity-maven-extension</artifactId><version>2.5.0</version></extension>"
    if ambiguous == "wrong_root":
        xml = "<other>" + declaration + "</other>"
    else:
        xml = "<extensions>" + declaration + declaration.replace("2.5.0", "2.6.0") + "</extensions>"
    assert runtime_state_for_files({".mvn/extensions.xml": xml.encode()}) == []
