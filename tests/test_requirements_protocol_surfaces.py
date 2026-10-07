"""A v2 protocol must be fixed before Docker and survive the host run pin."""

import hashlib
import json

import pytest
import test_cli_project_exit_codes as cli_fakes
from test_cli_project_exit_codes import RecordingSetupAgent, invoke_project
from test_control_layer_replay import _pin_template_agent

from sag.agent.acceptance_task import AcceptanceTask
from sag.benchmark.requirements import POLICY_VERSION, canonical_digest


def inputs(tmp_path):
    task = AcceptanceTask.model_validate({
        "repo": "apache/commons-cli", "sha": "a" * 40,
        "steps": [{"id": "test", "runner": "maven", "argv": ["mvn", "test"],
                   "java_major": 17}],
    })
    spec = {
        "schema_version": 2, "policy_version": POLICY_VERSION,
        "task_sha256": task.sha256, "repository": task.repo, "commit": task.sha,
        "annotation_completeness": {"status": "complete", "gaps": []},
        "ci_alignment": {"test_count_semantics": {
            "schema_version": 1, "status": "unavailable", "reason": "Unmatched smoke fixture"}},
        "preconditions": [{"id": "worktree_integrity"}, {"id": "runtime_conformance"}],
        "requirements": [
            {"id": "compile", "step_id": "test", "kind": "compile",
             "scope": {"status": "resolved", "modules": ["."]},
             "validation": {"rule": "native_goal"}},
            {"id": "test", "step_id": "test", "kind": "test",
             "scope": {"status": "resolved", "modules": ["."]},
             "validation": {"rule": "junit"}},
        ],
    }
    target = {
        "schema_version": 2, "repo": task.repo, "sha": task.sha,
        "harvested_at": "2026-09-22T00:00:00Z", "matched_cell": None,
        "cells": [{"cell_id": "unmatched-fixture", "build": "unknown", "grade": "B",
                   "executed_count": 0, "red_count": 0}],
    }
    paths = {key: tmp_path / f"{key}.json" for key in ("task", "ci", "requirements")}
    paths["task"].write_text(task.model_dump_json())
    paths["ci"].write_text(json.dumps(target))
    paths["requirements"].write_text(json.dumps(spec))
    flags = {"--ref": task.sha, "--acceptance-task-file": str(paths["task"]),
             "--ci-target-file": str(paths["ci"]), "--requirements-file": str(paths["requirements"])}
    return task, spec, target, paths, flags


def invoke(monkeypatch, tmp_path, flags):
    RecordingSetupAgent.calls = []
    argv = [value for pair in flags.items() for value in pair]
    return invoke_project(monkeypatch, tmp_path, RecordingSetupAgent, *argv)


def forbid_container_work(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Invalid protocol must be rejected before container construction")
    monkeypatch.setattr(cli_fakes.FakeProjectOrchestrator, "__init__", forbidden)


@pytest.mark.parametrize("missing", ["--ref", "--acceptance-task-file", "--ci-target-file"])
def test_requirements_cli_requires_explicit_ref_task_and_ci_before_docker(tmp_path, monkeypatch, missing):
    _task, _spec, _target, _paths, flags = inputs(tmp_path)
    flags.pop(missing)
    forbid_container_work(monkeypatch)
    result = invoke(monkeypatch, tmp_path, flags)
    assert result.exit_code != 0
    assert "Invalid requirements protocol" in result.output
    assert "explicit frozen --ref" in result.output
    assert RecordingSetupAgent.calls == []


@pytest.mark.parametrize("mutation", ["task_digest", "repo", "commit", "incomplete", "unresolved_scope", "optional", "policy"])
def test_requirements_cli_rejects_wrong_or_weakened_annotations(tmp_path, monkeypatch, mutation):
    _task, spec, _target, paths, flags = inputs(tmp_path)
    if mutation == "task_digest":
        spec["task_sha256"] = "b" * 64
    elif mutation == "repo":
        spec["repository"] = "apache/other"
    elif mutation == "commit":
        spec["commit"] = "b" * 40
    elif mutation == "incomplete":
        spec["annotation_completeness"]["status"] = "review_required"
    elif mutation == "unresolved_scope":
        spec["requirements"][0]["scope"]["status"] = "unknown"
    elif mutation == "optional":
        spec["requirements"][0]["optional"] = True
    else:
        spec["policy_version"] = "unrecognized"
    paths["requirements"].write_text(json.dumps(spec))
    forbid_container_work(monkeypatch)
    result = invoke(monkeypatch, tmp_path, flags)
    assert result.exit_code != 0
    assert "Invalid requirements protocol" in result.output
    assert RecordingSetupAgent.calls == []


@pytest.mark.parametrize("field,value", [("repo", "apache/other"), ("sha", "b" * 40)])
def test_requirements_cli_rejects_ci_from_a_different_subject(tmp_path, monkeypatch, field, value):
    _task, _spec, target, paths, flags = inputs(tmp_path)
    target[field] = value
    paths["ci"].write_text(json.dumps(target))
    forbid_container_work(monkeypatch)
    result = invoke(monkeypatch, tmp_path, flags)
    assert result.exit_code != 0
    assert "same repository and revision" in result.output
    assert RecordingSetupAgent.calls == []


def test_requirements_cli_rejects_a_second_acceptance_command(tmp_path, monkeypatch):
    _task, _spec, _target, _paths, flags = inputs(tmp_path)
    flags["--acceptance-command"] = "mvn package"
    forbid_container_work(monkeypatch)
    result = invoke(monkeypatch, tmp_path, flags)
    assert result.exit_code != 0
    assert "ordered task, not --acceptance-command" in result.output
    assert RecordingSetupAgent.calls == []


def test_requirements_cli_rejects_ambiguous_duplicate_json_fields(tmp_path, monkeypatch):
    _task, spec, _target, paths, flags = inputs(tmp_path)
    raw = json.dumps(spec)
    paths["requirements"].write_text(raw[:-1] + ', "task_sha256": "' + "b" * 64 + '"}')
    forbid_container_work(monkeypatch)
    result = invoke(monkeypatch, tmp_path, flags)
    assert result.exit_code != 0
    assert "Duplicate JSON field" in result.output
    assert RecordingSetupAgent.calls == []


def test_cli_protocol_identity_preserves_parsed_and_raw_input_hashes_in_run_pin(tmp_path, monkeypatch):
    task, spec, _target, paths, flags = inputs(tmp_path)
    raw_hash = hashlib.sha256(paths["requirements"].read_bytes()).hexdigest()
    result = invoke(monkeypatch, tmp_path, flags)
    assert result.exit_code == 0, result.output
    submitted = RecordingSetupAgent.calls[0]
    expected = {
        "protocol": "requirements-v2", "task_sha256": task.sha256,
        "requirements_sha256": canonical_digest(spec),
        "requirements_file_sha256": raw_hash, "policy_version": POLICY_VERSION,
    }
    assert submitted["evaluation_protocol"] == expected
    assert submitted["acceptance_task"] == task and submitted["project_ref"] == task.sha
    for path in paths.values():
        path.write_text("{}")
    agent = _pin_template_agent(tmp_path)
    agent._setup_evaluation_protocol = submitted["evaluation_protocol"]
    agent._setup_acceptance_task = submitted["acceptance_task"]
    agent._setup_ci_target = submitted["ci_target"]
    agent._initialize_run_pin_template()
    # Neither the host pin template nor persisted pin may alias mutable caller state.
    submitted["evaluation_protocol"]["requirements_sha256"] = "c" * 64
    assert agent._run_pin_template["sanitized_config"]["evaluation_protocol"] == expected
    pin = json.loads((tmp_path / "run-pin.json").read_text())
    assert pin["sanitized_config"]["evaluation_protocol"] == expected
    assert pin["sanitized_config"]["acceptance_task"]["sha256"] == task.sha256
    assert pin["sanitized_config"]["ci_target"]["record_sha256"] == submitted["ci_target"].raw_sha256


def test_whitespace_changes_byte_identity_without_changing_requirement_semantics(tmp_path, monkeypatch):
    _task, _spec, _target, paths, flags = inputs(tmp_path)
    first = invoke(monkeypatch, tmp_path, flags)
    assert first.exit_code == 0, first.output
    identity_a = RecordingSetupAgent.calls[0]["evaluation_protocol"]
    paths["requirements"].write_text(paths["requirements"].read_text() + "\n")
    second = invoke(monkeypatch, tmp_path, flags)
    assert second.exit_code == 0, second.output
    identity_b = RecordingSetupAgent.calls[0]["evaluation_protocol"]
    assert identity_a["requirements_sha256"] == identity_b["requirements_sha256"]
    assert identity_a["requirements_file_sha256"] != identity_b["requirements_file_sha256"]


def test_cli_refuses_requirement_bytes_changed_during_validation(tmp_path, monkeypatch):
    import sag.benchmark.requirements as requirements_module

    _task, _spec, _target, paths, flags = inputs(tmp_path)
    original = requirements_module.load_requirements

    def change_after_parsing(path, task=None):
        parsed = original(path, task=task)
        paths["requirements"].write_text("{}")
        return parsed

    monkeypatch.setattr(requirements_module, "load_requirements", change_after_parsing)
    forbid_container_work(monkeypatch)
    result = invoke(monkeypatch, tmp_path, flags)
    assert result.exit_code != 0
    assert "requirements bytes changed during validation" in result.output
    assert RecordingSetupAgent.calls == []
