"""The finalizer seals the measured dimensions beside the word (spec 2026-10-22).

Change 1: verdict.json carries `ci_command`, `official_ci`, `sag_checks` and
`run_control`, from inputs the finalizer already holds.
"""

import json

import pytest
from result_card_fakes import snapshot_dict
from test_verdict_finalizer import (
    FakeVerdictOrchestrator,
    RunEvidenceState,
    VerdictFinalizer,
    _RateValidator,
    _set_rate_test_rollup,
)

import sag.agent.verdict_finalizer as verdict_finalizer
from sag.agent.acceptance_task import AcceptanceTask, TaskCompletionSnapshot
from sag.agent.ci_comparison import PinnedCITarget
from sag.agent.verdict_finalizer import (
    VERDICT_SCHEMA_VERSION,
    EvidenceCloseReason,
    RunVerdictSnapshot,
    read_verdict_snapshot,
    validate_verdict_snapshot_v3,
)
from sag.metrics.target_record import TargetRecord

TASK = AcceptanceTask.model_validate(
    {
        "repo": "acme/widget",
        "sha": "a" * 40,
        "steps": [
            {
                "id": "build",
                "runner": "maven",
                "argv": ["mvn", "verify"],
                "java_major": 17,
                "maven_version": "3.9.16",
            }
        ],
    }
)

RECEIPT = {
    "receipt_id": "inv-maven-1-aaaaaaaaaaaa-0001",
    "tool": "maven",
    "argv": "/opt/apache-maven/bin/mvn verify",
    "actual_cwd": "/workspace/project",
    "exit_code": 0,
    "effective_jdk": {"major": "17", "runtime_authority": "dispatch_probe"},
    "toolchain_fingerprint": {
        "executable": "/opt/apache-maven/bin/mvn",
        "version": "Apache Maven 3.9.16 (abc)",
    },
    "module_outcomes": [
        {"module": "core", "status": "success"},
        {"module": "io", "status": "success"},
    ],
    "testcase_execution_totals": {
        "reported": 100,
        "skipped": 0,
        "passed": 100,
        "failed": 0,
        "errors": 0,
    },
}


def _target():
    record = TargetRecord.model_validate(
        {
            "repo": "acme/widget",
            "sha": "a" * 40,
            "harvested_at": "2026-10-21T00:00:00Z",
            "cells": [
                {
                    "cell_id": "build (17)",
                    "build": "ok",
                    "executed_count": 100,
                    "red_count": 0,
                    "skipped": 0,
                    "modules": ["core", "io"],
                    "modules_basis": "log",
                    "grade": "B",
                }
            ],
            "matched_cell": "build (17)",
        }
    )
    return PinnedCITarget(record=record, raw_sha256="c" * 64)


class _ReceiptValidator(_RateValidator):
    def __init__(self, receipts=(RECEIPT,)):
        self.receipts = list(receipts)

    def _current_scoped_receipts(self, _project_root):
        return list(self.receipts)


def completion_for(run_id, *steps, status=None, source_changes=()):
    rows = list(steps)
    if status is None:
        status = (
            "complete"
            if rows and all(step["status"] == "complete" for step in rows)
            else "incomplete"
        )
    return TaskCompletionSnapshot(
        run_id=run_id,
        task_sha256=TASK.sha256,
        status=status,
        steps=tuple(rows),
        source_changes_by_task=tuple(source_changes),
    )


def step(status="complete", exit_code=0, receipt_id=RECEIPT["receipt_id"], step_id="build"):
    row = {"id": step_id, "command": "mvn verify", "status": status}
    if status != "missing":
        row.update(receipt_id=receipt_id, exit_code=exit_code)
    return row


def bind_task(monkeypatch, completion):
    monkeypatch.setattr(verdict_finalizer, "build_task_completion", lambda *a, **k: completion)


def finalize(state, *, task=None, target=None, validator=None, reason=None):
    orchestrator = FakeVerdictOrchestrator()
    snapshot = VerdictFinalizer(
        orchestrator,
        validator=validator or _ReceiptValidator(),
        project_name="project",
        acceptance_task=task,
        ci_target=target,
    ).finalize(state, reason or EvidenceCloseReason.TEST_TERMINATED)
    return snapshot, orchestrator


def green_state(run_id):
    state = RunEvidenceState(run_id=run_id)
    _set_rate_test_rollup(state, passed=100, failed=0, errors=0, driven_modules=["core", "io"])
    return state


def test_a_bound_task_seals_all_four_dimensions(monkeypatch):
    state = green_state("run-dims-bound")
    bind_task(monkeypatch, completion_for(state.run_id, step()))
    snapshot, orchestrator = finalize(state, task=TASK, target=_target())

    assert snapshot.schema_version == VERDICT_SCHEMA_VERSION == 6
    assert snapshot.ci_command.result == "passed"
    assert (snapshot.ci_command.steps_passed, snapshot.ci_command.steps_total) == (1, 1)
    assert snapshot.ci_command.toolchain_as_pinned == "yes"
    assert snapshot.ci_command.source_unchanged == "yes"
    assert snapshot.official_ci.basis == "ci-target cell summary"
    assert snapshot.official_ci.cell == "build (17)"
    assert snapshot.official_ci.ci_modules == ("core", "io")
    assert snapshot.official_ci.agent_modules_built == ("core", "io")
    assert (snapshot.official_ci.ci_tests_executed, snapshot.official_ci.agent_tests_executed) == (
        100,
        100,
    )
    assert [check.confirmed for check in snapshot.sag_checks] == [True, True, True, True]
    assert snapshot.run_control.ended == "normally"

    sealed = json.loads(orchestrator.files["/workspace/.setup_agent/verdict.json"])
    for key in ("ci_command", "official_ci", "sag_checks", "run_control"):
        assert key in sealed
    assert read_verdict_snapshot(orchestrator) == snapshot
    assert validate_verdict_snapshot_v3(snapshot.model_dump(mode="json")) == snapshot


def test_without_a_task_there_is_no_ci_command_but_the_other_three_are_sealed():
    state = green_state("run-dims-free")
    snapshot, orchestrator = finalize(state)
    assert snapshot.ci_command is None
    assert snapshot.official_ci.basis == "unavailable"
    assert snapshot.official_ci.reason == "no official CI record was supplied for this run"
    assert len(snapshot.sag_checks) == 4
    assert snapshot.run_control.ended == "normally"
    sealed = json.loads(orchestrator.files["/workspace/.setup_agent/verdict.json"])
    assert "ci_command" not in sealed


def test_java_gap_and_live_jobs_reach_the_sealed_checks(monkeypatch):
    state = green_state("run-dims-gaps")
    state.record_conflict("build_requirements_unavailable")
    state.record_conflict("job_live_at_close:job-a")
    state.set_fact(
        "job_live_at_close.job-a",
        {"job_id": "job-a", "close_reason": "deadline", "obligation_ref": "x", "log_ref": "y"},
        evidence_ref="obligation://job-a",
    )
    bind_task(monkeypatch, completion_for(state.run_id, step(status="missing")))
    monkeypatch.setattr(
        verdict_finalizer,
        "_java_requirement_gaps",
        lambda orchestrator, validator: ("/workspace/project/pom.xml:profile_activation:playground",),
    )
    monkeypatch.setattr(
        verdict_finalizer,
        "_live_obligations",
        lambda orchestrator: {
            "job-a": {
                "tool": "maven",
                "argv": "/opt/apache-maven/bin/mvn verify",
                "working_directory": "/workspace/project",
            }
        },
    )
    snapshot, _ = finalize(state, task=TASK)
    assert snapshot.sag_checks[0].reason == (
        "the 'playground' profile's activation could not be evaluated"
    )
    assert snapshot.run_control.ended == "time_limit"
    assert snapshot.run_control.commands_still_running == 1
    assert snapshot.ci_command.result == "not_finished"


def test_a_historical_v5_payload_keeps_its_shape_and_cannot_carry_the_new_fields():
    payload = snapshot_dict(schema_version=5)
    snapshot = validate_verdict_snapshot_v3(payload)
    dumped = snapshot.model_dump(mode="json")
    for key in (
        "ci_command",
        "official_ci",
        "sag_checks",
        "run_control",
        "project_conflicts",
        "knowledge_conflicts",
    ):
        assert key not in dumped
    with pytest.raises(ValueError):
        validate_verdict_snapshot_v3(
            {**payload, "run_control": {"ended": "normally", "commands_still_running": 0}}
        )


def _current(**overrides):
    payload = snapshot_dict(schema_version=VERDICT_SCHEMA_VERSION)
    payload.update(overrides)
    return payload


def test_sealed_checks_are_exactly_the_four_in_order():
    checks = [
        {"check": "java requirement decided", "confirmed": True, "reason": None},
        {"check": "built modules confirmed", "confirmed": True, "reason": None},
        {"check": "test outcome graded", "confirmed": True, "reason": None},
        {"check": "test reports tied to the command", "confirmed": True, "reason": None},
    ]
    assert len(validate_verdict_snapshot_v3(_current(sag_checks=checks)).sag_checks) == 4
    with pytest.raises(ValueError):
        validate_verdict_snapshot_v3(_current(sag_checks=checks[:3]))
    with pytest.raises(ValueError):
        validate_verdict_snapshot_v3(_current(sag_checks=[checks[1], checks[0], *checks[2:]]))


def test_a_sealed_command_result_must_agree_with_the_task_steps():
    good = {
        "result": "passed",
        "steps_passed": 1,
        "steps_total": 1,
        "first_unpassed_step": None,
        "toolchain_as_pinned": "unavailable",
        "toolchain_reason": "the task pins no Java or Maven version",
        "source_unchanged": "yes",
    }
    assert validate_verdict_snapshot_v3(_current(ci_command=good)).ci_command.result == "passed"
    wrong = {
        **good,
        "result": "failed",
        "steps_passed": 0,
        "first_unpassed_step": {"id": "smoke-build-test", "status": "failed", "exit_code": 1},
    }
    with pytest.raises(ValueError):
        validate_verdict_snapshot_v3(_current(ci_command=wrong))
    with pytest.raises(ValueError):
        RunVerdictSnapshot.model_validate(_current(ci_command=good, task_completion=None))
