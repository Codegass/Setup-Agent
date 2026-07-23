import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

import sag.agent.agent as agent_module
from sag.agent.agent import SetupAgent, _active_setup_run_id
from sag.agent.control_events import (
    CONTROL_EVENT_SCHEMA_VERSION,
    ControlEvent,
    ControlEventSink,
    RunPin,
    canonical_sha256,
    forced_action_sha256,
    sanitize_config,
)
from sag.agent.current_plan import CurrentPlan
from sag.agent.react_engine import ReActEngine
from sag.agent.reasoning_scheduler import ReasoningScheduler
from sag.agent.replay import ControlReplayRunner, ReplayValidationError
from sag.config.logger import SessionLogger
from sag.config.prompt_loader import PromptConfig
from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome
from sag.tools.base import ToolResult

FIXTURES = Path(__file__).parent / "fixtures" / "control_layer"


def test_live_run_id_uses_the_unique_session_id(monkeypatch):
    monkeypatch.setattr(
        agent_module,
        "get_session_logger",
        lambda: SimpleNamespace(session_id="20260717_190128_88744"),
    )

    assert _active_setup_run_id(7) == "20260717_190128_88744"


@pytest.mark.parametrize(
    "fixture_name",
    ["tvm.jsonl", "bigtop.jsonl", "paramiko.jsonl", "cassandra-java-driver.jsonl"],
)
def test_fixture_replays_to_declared_snapshot_without_external_calls(fixture_name):
    runner = ControlReplayRunner(
        llm_factory=lambda: pytest.fail("replay must not construct an LLM"),
        orchestrator_factory=lambda: pytest.fail("replay must not construct a container"),
    )

    result = runner.run(FIXTURES / fixture_name)

    assert result.header.fixture_kind == "recorded_tool_transcript"
    assert result.header.source_manifest
    assert result.snapshot.model_dump(mode="json") == result.expected_snapshot
    assert result.unconsumed_events == ()
    assert result.produced_event_digest == result.expected_event_digest


def test_tvm_replay_never_enters_test_after_failed_build():
    result = ControlReplayRunner.offline().run(FIXTURES / "tvm.jsonl")

    assert result.phase("build").outcome.value == "failed"
    assert result.phase("test").termination.value == "skipped"
    assert result.loop_decisions[1].decision == "guide"


def test_bigtop_repair_is_dependency_valid_and_append_only():
    result = ControlReplayRunner.offline().run(FIXTURES / "bigtop.jsonl")

    assert [record.attempt_id for record in result.phase_attempts("build")] == [
        "build-1",
        "build-2",
    ]
    assert result.repair_routes[0].edge == ("test", "build")
    assert result.repair_routes[0].accepted is True


def test_paramiko_replay_uses_two_plans_for_six_actions():
    result = ControlReplayRunner.offline().run(FIXTURES / "paramiko.jsonl")

    assert result.planner_response_count == 2
    assert result.executed_envelope_count == 6
    assert result.compatibility_action_model_calls == 0


@pytest.mark.parametrize("mutation", ["duplicate", "out_of_order", "unknown_field"])
def test_transcript_rejects_noncanonical_event_stream(tmp_path, mutation):
    source = (FIXTURES / "paramiko.jsonl").read_text(encoding="utf-8").splitlines()
    if mutation == "duplicate":
        source.insert(2, source[1])
    elif mutation == "out_of_order":
        source[1], source[2] = source[2], source[1]
    else:
        source[1] = source[1][:-1] + ',"invented":true}'
    transcript = tmp_path / "invalid.jsonl"
    transcript.write_text("\n".join(source) + "\n", encoding="utf-8")

    with pytest.raises(ReplayValidationError):
        ControlReplayRunner.offline().run(transcript)


def test_envelope_hash_mismatch_is_rejected(tmp_path):
    text = (FIXTURES / "paramiko.jsonl").read_text(encoding="utf-8")
    transcript = tmp_path / "invalid-envelope.jsonl"
    transcript.write_text(
        text.replace('"envelope_sha256":"', '"envelope_sha256":"bad'), encoding="utf-8"
    )

    with pytest.raises(ReplayValidationError, match="envelope hash"):
        ControlReplayRunner.offline().run(transcript)


def test_replay_checks_recorded_loop_recurrence_count(tmp_path):
    rows = [
        json.loads(line)
        for line in (FIXTURES / "tvm.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    loop = next(row for row in rows if row.get("kind") == "loop_decision")
    loop["payload"]["event"]["recurrence_count"] = 99
    transcript = tmp_path / "invalid-recurrence.jsonl"
    transcript.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReplayValidationError, match="recurrence count"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def _write_replay_rows(path, rows):
    for sequence, row in enumerate(rows[1:], 1):
        row["sequence"] = sequence
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_live_shaped_action_scheduler_decisions_do_not_double_advance(tmp_path):
    source = [
        json.loads(line)
        for line in (FIXTURES / "paramiko.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rows = [source[0]]
    for row in source[1:]:
        if row.get("kind") == "action_envelope":
            rows.append(
                {
                    "kind": "scheduler_decision",
                    "payload": {
                        "mode": "action",
                        "reasons": [],
                        "plan_index": row["payload"]["plan_index"],
                    },
                    "source": row["source"],
                }
            )
        rows.append(row)
    transcript = tmp_path / "live-shaped-paramiko.jsonl"
    _write_replay_rows(transcript, rows)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.executed_envelope_count == 6
    assert result.snapshot.verdict == "success"


def test_live_normalized_envelope_can_add_tool_defaults(tmp_path):
    source = [
        json.loads(line)
        for line in (FIXTURES / "paramiko.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    planner = next(row for row in source if row.get("kind") == "planner_response")
    planner["payload"]["plan"]["steps"][0]["exact_params"].pop("timeout")
    planner["payload"]["response_sha256"] = canonical_sha256(planner["payload"]["plan"])
    first_envelope = next(row for row in source if row.get("kind") == "action_envelope")
    source.insert(
        source.index(first_envelope),
        {
            "kind": "scheduler_decision",
            "payload": {"mode": "action", "reasons": [], "plan_index": 0},
            "source": first_envelope["source"],
        },
    )
    transcript = tmp_path / "normalized-paramiko.jsonl"
    _write_replay_rows(transcript, source)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.snapshot.verdict == "success"


def _forced_bigtop_rows():
    fixture = [
        json.loads(line)
        for line in (FIXTURES / "bigtop.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    header = fixture[0]
    header["schema_version"] = 2
    header["initial_state"] = {
        "start_phase": "test",
        "heartbeat_actions": 5,
        "available_tools": ["build", "search", "project"],
        "facts": [
            {
                "key": "build.test_entry_ready",
                "value": True,
                "evidence_ref": "artifact://test-ready",
                "scope": "artifacts",
            }
        ],
        "conflicts": [],
        "repair_global_remaining": 2,
        "repair_phase_remaining": {"test": 1, "build": 1},
    }
    source_excerpt = fixture[1]["source"]
    coordinate = "/workspace/bigtop/bigtop-bigpetstore/bigpetstore-transaction-queue"
    exact_params = {
        "action": "test",
        "working_directory": coordinate,
    }
    resolution = {
        "status": "available",
        "workspace_root": "/workspace",
        "project_root": "/workspace/bigtop",
        "candidates": [{"root": coordinate, "system": "maven"}],
    }
    forced_payload = {
        "envelope_id": "forced-bigtop-test-1",
        "policy": "test_attempt_required",
        "trigger": "phase_floor",
        "phase": "test",
        "source_attempt_id": "test-1",
        "reason_code": "test_receipt_missing",
        "tool": "build",
        "exact_params": exact_params,
        "candidate_root": coordinate,
        "candidate_system": "maven",
        "parent_execution_id": None,
        "candidate_resolution": resolution,
    }
    forced_payload["action_sha256"] = forced_action_sha256(
        policy=forced_payload["policy"],
        trigger=forced_payload["trigger"],
        phase=forced_payload["phase"],
        source_attempt_id=forced_payload["source_attempt_id"],
        reason_code=forced_payload["reason_code"],
        tool=forced_payload["tool"],
        exact_params=forced_payload["exact_params"],
        candidate_root=forced_payload["candidate_root"],
        candidate_system=forced_payload["candidate_system"],
        parent_execution_id=None,
        candidate_resolution=resolution,
    )
    rows = [
        header,
        {
            "kind": "forced_action",
            "payload": forced_payload,
            "source": source_excerpt,
        },
        {
            "kind": "tool_result",
            "payload": {
                "envelope_id": forced_payload["envelope_id"],
                "execution_id": "forced-bigtop-execution-1",
                "tool": "build",
                "params": exact_params,
                "scope": "test_runtime",
                "roles": ["test"],
                "result": {
                    "invocation_status": "completed",
                    "operation_outcome": "failed",
                    "evidence_status": "verified",
                    "output": "Maven reached the test runner and failed.",
                    "output_ref": "output_forced_bigtop",
                    "evidence_assessment": "blocked",
                    "metadata": {
                        "command": "mvn test",
                        "runner_dispatched": True,
                        "exit_code": 1,
                    },
                    "evidence_refs": ["output_forced_bigtop"],
                    "conflicts": [],
                    "validator_findings": [],
                    "facts": {"system": "maven"},
                    "refs": ["output_forced_bigtop"],
                    "error": "test failure",
                    "error_code": "MAVEN_BUILD_FAILED",
                    "failure_signature": "maven:test:failed",
                    "error_tail_preview": "test failure",
                },
                "source_phase": "test",
                "source_attempt_id": "test-1",
            },
            "source": source_excerpt,
        },
        {
            "kind": "gate_decision",
            "payload": {
                "phase": "test",
                "signal": "done",
                "claimed_outcome": "failed",
                "validator_state": "red",
                "expected_accepted": True,
                "expected_outcome": "failed",
                "reason": "the runner reached a terminal test failure",
                "key_results": "test runner reached",
                "evidence_refs": ["output_forced_bigtop"],
                "validated_facts": {},
                "source_attempt_id": "test-1",
                "test_candidate_resolution": resolution,
            },
            "source": source_excerpt,
        },
        {
            "kind": "phase_transition",
            "payload": {
                "expected_kind": "evidence_close",
                "expected_target": None,
                "expected_reason_code": "test_terminal",
                "repair_request": None,
            },
            "source": source_excerpt,
        },
        {
            "kind": "evidence_close",
            "payload": {"reason": "test_terminated"},
            "source": source_excerpt,
        },
    ]
    outer_result = rows[2]["payload"]
    outer_result["actual_executions"] = [
        {
            "execution_id": "forced-bigtop-backend-execution-1",
            "tool": "maven",
            "params": {
                "command": "test",
                "working_directory": coordinate,
            },
            "scope": "test_runtime",
            "roles": ["test"],
            "result": json.loads(json.dumps(outer_result["result"])),
        }
    ]
    return rows


def test_engine_owned_forced_action_replays_without_scheduler_plan(tmp_path):
    source = _forced_bigtop_rows()
    transcript = tmp_path / "forced-action-bigtop.jsonl"
    _write_replay_rows(transcript, source)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.executed_envelope_count == 1
    assert result.unconsumed_events == ()


def test_forced_result_replays_the_live_plan_invalidation(tmp_path):
    source = _forced_bigtop_rows()
    result_index = next(
        index for index, row in enumerate(source) if row.get("kind") == "tool_result"
    )
    plan = {
        "steps": [
            {
                "tool": "project",
                "exact_params": {"action": "analyze"},
                "preconditions": [],
                "expected_evidence": ["fresh survey"],
                "success_criteria": ["survey persisted"],
            }
        ],
        "invalidate_on": ["failure", "conflict", "unknown", "phase_change"],
    }
    source[result_index + 1 : result_index + 1] = [
        {
            "kind": "scheduler_decision",
            "payload": {
                "mode": "think",
                "reasons": ["initial", "plan_exhausted"],
                "plan_index": None,
            },
            "source": source[1]["source"],
        },
        {
            "kind": "planner_response",
            "payload": {
                "plan_id": "after-forced-action",
                "plan": plan,
                "response_sha256": canonical_sha256(plan),
            },
            "source": source[1]["source"],
        },
    ]
    transcript = tmp_path / "forced-action-plan-invalidation.jsonl"
    _write_replay_rows(transcript, source)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.planner_response_count == 1
    assert result.unconsumed_events == ()


def test_forced_action_is_versioned_as_control_schema_v2(tmp_path):
    assert CONTROL_EVENT_SCHEMA_VERSION == 2
    source = _forced_bigtop_rows()
    source[0]["schema_version"] = 1
    transcript = tmp_path / "forced-action-v1.jsonl"
    _write_replay_rows(transcript, source)

    with pytest.raises(ReplayValidationError, match="requires control-event schema version 2"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_replay_rejects_unknown_control_schema_version(tmp_path):
    source = _forced_bigtop_rows()
    source[0]["schema_version"] = 3
    transcript = tmp_path / "unknown-control-schema.jsonl"
    _write_replay_rows(transcript, source)

    with pytest.raises(ReplayValidationError):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


@pytest.mark.parametrize("mutation", ["missing_leaf", "wrong_root", "wrong_system"])
def test_forced_build_receipt_is_bound_to_the_backend_leaf(tmp_path, mutation):
    source = _forced_bigtop_rows()
    result = next(row for row in source if row.get("kind") == "tool_result")
    leaves = result["payload"]["actual_executions"]
    if mutation == "missing_leaf":
        result["payload"]["actual_executions"] = []
    elif mutation == "wrong_root":
        leaves[0]["params"]["working_directory"] = "/workspace/bigtop"
    else:
        leaves[0]["tool"] = "gradle"
        leaves[0]["params"] = {
            "tasks": "test",
            "working_directory": leaves[0]["params"]["working_directory"],
        }
    transcript = tmp_path / f"forced-backend-{mutation}.jsonl"
    _write_replay_rows(transcript, source)

    with pytest.raises(ReplayValidationError):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_forced_preexecution_refusal_replays_as_control_not_test_evidence(tmp_path):
    source = _forced_bigtop_rows()
    forced = source[1]["payload"]
    result = source[2]["payload"]
    marker = {
        "phase": "test",
        "source_attempt_id": "test-1",
        "root": forced["candidate_root"],
        "system": forced["candidate_system"],
        "actual_root": forced["candidate_root"],
        "actual_system": "maven",
        "disposition": "no_runner_dispatch",
        "reason_code": "MAVEN_PREFLIGHT_REJECTED",
    }
    conflict = (
        "forced_test_attempt_nonreceipt:test-1:"
        f"{forced['candidate_root']}:maven:no_runner_dispatch:"
        "MAVEN_PREFLIGHT_REJECTED"
    )
    for projection in [
        result["result"],
        result["actual_executions"][0]["result"],
    ]:
        projection["metadata"] = {
            "harness_forced_test_attempt": marker,
        }
        projection["conflicts"] = [conflict]
    gate = source[3]["payload"]
    gate.update(
        {
            "claimed_outcome": "unknown",
            "validator_state": "unavailable",
            "expected_outcome": "unknown",
            "reason": "runner dispatch was deterministically refused",
        }
    )
    transcript = tmp_path / "forced-refusal.jsonl"
    _write_replay_rows(transcript, source)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.executed_envelope_count == 1
    assert conflict in result.snapshot.conflicts
    assert result.phase("test").outcome.value == "unknown"


def test_facade_only_forced_no_runner_receipt_replays_and_stays_non_green(tmp_path):
    source = _forced_bigtop_rows()
    forced = source[1]["payload"]
    result = source[2]["payload"]
    result["actual_executions"] = []
    result["result"]["metadata"] = {
        "harness_forced_test_attempt": {
            "phase": "test",
            "source_attempt_id": "test-1",
            "root": forced["candidate_root"],
            "system": forced["candidate_system"],
            "actual_root": forced["candidate_root"],
            "actual_system": "maven",
            "disposition": "no_runner_dispatch",
            "reason_code": "MAVEN_PREFLIGHT_REJECTED",
        }
    }
    gate = source[3]["payload"]
    gate.update(
        {
            "claimed_outcome": "unknown",
            "validator_state": "unavailable",
            "expected_outcome": "unknown",
            "reason": "runner dispatch was refused at the facade",
        }
    )
    transcript = tmp_path / "forced-facade-refusal.jsonl"
    _write_replay_rows(transcript, source)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.phase("test").outcome.value == "unknown"


def test_facade_only_forced_command_is_not_a_replayable_runner_receipt(tmp_path):
    source = _forced_bigtop_rows()
    forced = source[1]["payload"]
    result = source[2]["payload"]
    result["actual_executions"] = []
    result["result"]["metadata"]["harness_forced_test_attempt"] = {
        "phase": "test",
        "source_attempt_id": "test-1",
        "root": forced["candidate_root"],
        "system": forced["candidate_system"],
        "actual_root": forced["candidate_root"],
        "actual_system": "maven",
        "disposition": "candidate_mismatch",
        "reason_code": "FORCED_TEST_CANDIDATE_MISMATCH",
    }
    transcript = tmp_path / "forced-facade-command.jsonl"
    _write_replay_rows(transcript, source)

    with pytest.raises(ReplayValidationError, match="facade-only forced build"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_forced_preexecution_refusal_cannot_replay_as_a_green_gate(tmp_path):
    source = _forced_bigtop_rows()
    forced = source[1]["payload"]
    result = source[2]["payload"]
    marker = {
        "phase": "test",
        "source_attempt_id": "test-1",
        "root": forced["candidate_root"],
        "system": forced["candidate_system"],
        "actual_root": forced["candidate_root"],
        "actual_system": "maven",
        "disposition": "no_runner_dispatch",
        "reason_code": "MAVEN_PREFLIGHT_REJECTED",
    }
    for projection in [
        result["result"],
        result["actual_executions"][0]["result"],
    ]:
        projection["metadata"] = {"harness_forced_test_attempt": marker}
    gate = source[3]["payload"]
    gate.update(
        {
            "claimed_outcome": "success",
            "validator_state": "green",
            "expected_outcome": "success",
            "reason": "stale artifacts looked green",
        }
    )
    transcript = tmp_path / "forced-refusal-green.jsonl"
    _write_replay_rows(transcript, source)

    with pytest.raises(ReplayValidationError, match="non-green gate"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


@pytest.mark.parametrize(
    "mutation",
    ["empty_result_attempt", "missing_gate_attempt", "missing_gate_resolution"],
)
def test_forced_action_replay_fails_closed_on_missing_lineage(tmp_path, mutation):
    source = _forced_bigtop_rows()
    forced = next(row for row in source if row.get("kind") == "forced_action")
    result = next(
        row
        for row in source
        if row.get("kind") == "tool_result"
        and row["payload"]["envelope_id"] == forced["payload"]["envelope_id"]
    )
    gate = next(
        row
        for row in source
        if row.get("kind") == "gate_decision"
        and row["payload"].get("phase") == "test"
        and row["payload"].get("source_attempt_id") == "test-1"
    )
    if mutation == "empty_result_attempt":
        result["payload"]["source_attempt_id"] = ""
    elif mutation == "missing_gate_attempt":
        gate["payload"].pop("source_attempt_id")
    else:
        gate["payload"].pop("test_candidate_resolution")
    transcript = tmp_path / f"forced-{mutation}.jsonl"
    _write_replay_rows(transcript, source)

    with pytest.raises(ReplayValidationError):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def _pending_forced_bigtop_rows():
    source = _forced_bigtop_rows()
    dispatch = source[2]
    dispatch["payload"]["result"] = {
        "invocation_status": "pending",
        "operation_outcome": "unknown",
        "evidence_status": "unknown",
        "output": "Maven test is running.",
        "evidence_assessment": "unknown",
        "poll_ref": "job:test-123",
        "metadata": {
            "command": "mvn test",
            "runner_dispatched": True,
            "dispatch_status": "running_detached",
            "job_id": "test-123",
        },
        "evidence_refs": [],
        "conflicts": [],
        "validator_findings": [],
        "facts": {"system": "maven"},
        "refs": ["job:test-123"],
    }
    dispatch["payload"]["actual_executions"][0]["result"] = json.loads(
        json.dumps(dispatch["payload"]["result"])
    )
    first = source[1]["payload"]
    poll_payload = {
        "envelope_id": "forced-bigtop-poll-1",
        "policy": "test_attempt_required",
        "trigger": "phase_floor",
        "phase": "test",
        "source_attempt_id": "test-1",
        "reason_code": "pending_test_poll_required",
        "tool": "search",
        "exact_params": {"target": "job:test-123"},
        "candidate_root": first["candidate_root"],
        "candidate_system": first["candidate_system"],
        "parent_execution_id": dispatch["payload"]["actual_executions"][0]["execution_id"],
        "candidate_resolution": first["candidate_resolution"],
    }
    poll_payload["action_sha256"] = forced_action_sha256(
        policy=poll_payload["policy"],
        trigger=poll_payload["trigger"],
        phase=poll_payload["phase"],
        source_attempt_id=poll_payload["source_attempt_id"],
        reason_code=poll_payload["reason_code"],
        tool=poll_payload["tool"],
        exact_params=poll_payload["exact_params"],
        candidate_root=poll_payload["candidate_root"],
        candidate_system=poll_payload["candidate_system"],
        parent_execution_id=poll_payload["parent_execution_id"],
        candidate_resolution=poll_payload["candidate_resolution"],
    )
    poll_events = [
        {
            "kind": "forced_action",
            "payload": poll_payload,
            "source": source[1]["source"],
        },
        {
            "kind": "tool_result",
            "payload": {
                "envelope_id": poll_payload["envelope_id"],
                "execution_id": "forced-bigtop-poll-execution-1",
                "tool": "search",
                "params": {"target": "job:test-123"},
                "scope": "test_runtime",
                "roles": [],
                "result": {
                    "invocation_status": "completed",
                    "operation_outcome": "success",
                    "evidence_status": "unknown",
                    "output": "Detached Maven test completed.",
                    "evidence_assessment": "unknown",
                    "poll_ref": "job:test-123",
                    "metadata": {
                        "dispatch_status": "completed_detached",
                        "job_id": "test-123",
                    },
                    "evidence_refs": [],
                    "conflicts": [],
                    "validator_findings": [],
                    "facts": {},
                    "refs": ["job:test-123"],
                },
                "source_phase": "test",
                "source_attempt_id": "test-1",
            },
            "source": source[1]["source"],
        },
    ]
    source[3:3] = poll_events
    return source


def test_forced_poll_replays_with_parent_dispatch_lineage(tmp_path):
    source = _pending_forced_bigtop_rows()
    transcript = tmp_path / "forced-poll.jsonl"
    _write_replay_rows(transcript, source)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.executed_envelope_count == 2


def test_forced_action_rejects_stale_pending_parent_lineage(tmp_path):
    source = _pending_forced_bigtop_rows()
    forced = [row for row in source if row.get("kind") == "forced_action"][1]
    forced["payload"]["parent_execution_id"] = "not-the-dispatch"
    forced["payload"]["action_sha256"] = forced_action_sha256(
        policy=forced["payload"]["policy"],
        trigger=forced["payload"]["trigger"],
        phase=forced["payload"]["phase"],
        source_attempt_id=forced["payload"]["source_attempt_id"],
        reason_code=forced["payload"]["reason_code"],
        tool=forced["payload"]["tool"],
        exact_params=forced["payload"]["exact_params"],
        candidate_root=forced["payload"]["candidate_root"],
        candidate_system=forced["payload"]["candidate_system"],
        parent_execution_id=forced["payload"]["parent_execution_id"],
        candidate_resolution=forced["payload"]["candidate_resolution"],
    )
    transcript = tmp_path / "forced-wrong-parent.jsonl"
    _write_replay_rows(transcript, source)

    with pytest.raises(ReplayValidationError):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_replay_rejects_candidate_snapshot_outside_recorded_project_root(tmp_path):
    source = _forced_bigtop_rows()
    forced = source[1]["payload"]
    forced["candidate_resolution"]["candidates"][0]["root"] = "/workspace/other/tests"
    forced["action_sha256"] = forced_action_sha256(
        policy=forced["policy"],
        trigger=forced["trigger"],
        phase=forced["phase"],
        source_attempt_id=forced["source_attempt_id"],
        reason_code=forced["reason_code"],
        tool=forced["tool"],
        exact_params=forced["exact_params"],
        candidate_root=forced["candidate_root"],
        candidate_system=forced["candidate_system"],
        parent_execution_id=forced["parent_execution_id"],
        candidate_resolution=forced["candidate_resolution"],
    )
    transcript = tmp_path / "forced-candidate-snapshot-escape.jsonl"
    _write_replay_rows(transcript, source)

    with pytest.raises(ReplayValidationError):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_rejected_planner_response_replays_the_scheduler_fault(tmp_path):
    source = [
        json.loads(line)
        for line in (FIXTURES / "paramiko.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    first_planner = next(row for row in source if row.get("kind") == "planner_response")
    insertion = source.index(first_planner)
    source[insertion:insertion] = [
        {
            "kind": "planner_response",
            "payload": {
                "plan_id": "rejected-malformed-plan-0001",
                "plan": {"rejected": True, "code": "malformed_plan"},
                "response_sha256": "f" * 64,
            },
            "source": first_planner["source"],
        },
        {
            "kind": "scheduler_decision",
            "payload": {
                "mode": "think",
                "reasons": ["malformed_plan"],
                "plan_index": None,
            },
            "source": first_planner["source"],
        },
    ]
    transcript = tmp_path / "rejected-plan-paramiko.jsonl"
    _write_replay_rows(transcript, source)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.planner_response_count == 3
    assert result.snapshot.verdict == "success"


def test_session_logger_control_sink_appends_host_and_mirror(tmp_path):
    mirrored = []
    session_logger = object.__new__(SessionLogger)
    session_logger.session_log_dir = tmp_path
    session_logger._control_event_sink = None

    sink = session_logger.get_control_event_sink(
        mirror=mirrored.append,
        clock=lambda: "2026-07-17T12:00:00Z",
        id_factory=lambda sequence: f"live-{sequence}",
    )
    sink.emit("scheduler_decision", {"mode": "think", "reasons": ["initial"]})

    event = ControlEvent.model_validate_json(
        (tmp_path / "control_events.jsonl").read_text(encoding="utf-8")
    )
    assert event.sequence == 1
    assert event.event_id == "live-1"
    assert mirrored == [(tmp_path / "control_events.jsonl").read_text(encoding="utf-8")]


def test_live_engine_emits_scheduler_envelope_and_redacted_result(tmp_path):
    sink = ControlEventSink(
        tmp_path / "control_events.jsonl",
        clock=lambda: "2026-07-17T12:00:00Z",
        id_factory=lambda sequence: f"live-{sequence}",
    )
    params = {"action": "build", "working_directory": "/workspace/demo"}
    plan = CurrentPlan.model_validate(
        {
            "steps": [
                {
                    "tool": "build",
                    "exact_params": params,
                    "preconditions": [],
                    "expected_evidence": ["compiled artifacts"],
                    "success_criteria": ["build succeeds"],
                }
            ]
        }
    )
    engine = object.__new__(ReActEngine)
    engine.control_event_sink = sink
    engine.reasoning_scheduler = ReasoningScheduler(available_tools=["build"])
    engine._scheduler_active = True
    engine._scheduled_turn = None
    engine.phase_machine = None

    assert engine._should_use_thinking_model() is True
    engine.reasoning_scheduler.accept_plan(plan)
    engine._emit_control_planner_response(plan)
    assert engine._should_use_thinking_model() is False
    envelope_id = engine._emit_control_action_envelope("build", params)
    result = ToolResult(
        invocation_status=InvocationStatus.COMPLETED,
        operation_outcome=OperationOutcome.SUCCESS,
        evidence_status=EvidenceStatus.VERIFIED,
        output_ref="output_live_build",
        output="secret build output " * 100,
        raw_output="never serialize this full body",
        facts={"compiled_classes": 41},
        refs=["output_live_build"],
        evidence_refs=["output_live_build"],
    )
    engine._emit_control_tool_result(
        envelope_id=envelope_id,
        execution_id="execution-live-1",
        tool="build",
        params=params,
        result=result,
    )

    text = (tmp_path / "control_events.jsonl").read_text(encoding="utf-8")
    events = [ControlEvent.model_validate_json(line) for line in text.splitlines()]
    assert [event.kind for event in events] == [
        "scheduler_decision",
        "planner_response",
        "scheduler_decision",
        "action_envelope",
        "tool_result",
    ]
    assert events[-1].payload["result"]["output"] == "stored as output_live_build"
    assert events[-1].payload["result"]["facts"] == {"compiled_classes": 41}
    assert "secret build output" not in text
    assert "never serialize" not in text


def test_sanitized_config_excludes_secrets_and_api_endpoints():
    sanitized = sanitize_config(
        {
            "thinking_model": "gpt-5",
            "openai_api_key": "secret",
            "openai_base_url": "https://secret.example/v1",
            "nested": {"token": "secret", "safe": 3},
        }
    )

    assert sanitized == {"nested": {"safe": 3}, "thinking_model": "gpt-5"}


def test_setup_agent_updates_complete_run_pin_after_clone(tmp_path):
    mirrored = []
    agent = object.__new__(SetupAgent)
    agent._run_pin_host_path = tmp_path / "run-pin.json"
    agent._run_pin_mirror = mirrored.append
    agent._run_pin_template = {
        "container_image_digest": "sha256:" + "b" * 64,
        "sag_git_sha": "c" * 40,
        "thinking_model": "thinking-model",
        "action_model": "action-model",
        "sanitized_config": {"max_iterations": 50},
        "prompt_bundle_sha256": "d" * 64,
        "feature_flags": {"control_events": True},
        "random_seed_or_null": None,
        # The runner assigns a real run-order index; it must reach the pin.
        "run_order_index": 5,
        "dependency_cache_state": "warm",
        "host_arch": "arm64",
    }
    agent.agent_logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None)

    agent._record_target_repo_sha("a" * 40)

    pin = RunPin.model_validate_json((tmp_path / "run-pin.json").read_text(encoding="utf-8"))
    assert pin.target_repo_sha == "a" * 40
    assert pin.run_order_index == 5
    assert mirrored == [(tmp_path / "run-pin.json").read_text(encoding="utf-8")]


def _pin_template_agent(tmp_path):
    class PinConfig(BaseModel):
        thinking_model: str = "thinking-model"
        action_model: str = "action-model"
        max_iterations: int = 50

    agent = object.__new__(SetupAgent)
    agent._run_pin_host_path = tmp_path / "run-pin.json"
    agent._run_pin_mirror = None
    agent.config = PinConfig()
    agent.react_engine = SimpleNamespace(prompts=PromptConfig({"system": "sys"}))
    agent.phase_machine = object()
    agent.agent_logger = SimpleNamespace(warning=lambda *_a, **_k: None)
    agent._resolve_sag_git_sha = lambda: "a" * 40
    agent._resolve_container_image_digest = lambda: "sha256:" + "b" * 64
    return agent


def test_run_pin_is_written_at_startup_even_without_a_target_sha(tmp_path):
    """Item-3 regression: the pin write previously fired ONLY inside
    _record_target_repo_sha, so a run that never observed a target SHA left NO
    pin file at all (real 2026-07-19 S2-00000-r3). The template init now writes
    the pin UNCONDITIONALLY with a null target SHA."""
    agent = _pin_template_agent(tmp_path)

    agent._initialize_run_pin_template()

    pin_path = tmp_path / "run-pin.json"
    assert pin_path.is_file(), "run pin must exist after startup even with no target SHA"
    pin = RunPin.model_validate_json(pin_path.read_text(encoding="utf-8"))
    assert pin.target_repo_sha is None
    # The rest of the provenance is already complete at startup.
    assert pin.sag_git_sha == "a" * 40
    assert pin.container_image_digest == "sha256:" + "b" * 64


def test_observed_target_sha_rewrites_the_startup_pin(tmp_path):
    """The startup pin's null SHA is replaced the moment a real one is
    observed — the collector's current-run validation requires the real SHA."""
    agent = _pin_template_agent(tmp_path)
    agent._initialize_run_pin_template()
    pin_path = tmp_path / "run-pin.json"
    assert RunPin.model_validate_json(pin_path.read_text(encoding="utf-8")).target_repo_sha is None

    agent._record_target_repo_sha("f" * 40)

    assert (
        RunPin.model_validate_json(pin_path.read_text(encoding="utf-8")).target_repo_sha == "f" * 40
    )


def test_run_pin_hashes_the_complete_prompt_bundle(tmp_path):
    class PinConfig(BaseModel):
        thinking_model: str = "thinking-model"
        action_model: str = "action-model"
        max_iterations: int = 50

    agent = object.__new__(SetupAgent)
    agent._run_pin_host_path = tmp_path / "run-pin.json"
    agent.config = PinConfig()
    agent.react_engine = SimpleNamespace(prompts=PromptConfig({"system": "x" * 600 + "a"}))
    agent.phase_machine = object()
    agent.agent_logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None)
    agent._resolve_sag_git_sha = lambda: "a" * 40
    agent._resolve_container_image_digest = lambda: "sha256:" + "b" * 64

    agent._initialize_run_pin_template()
    first = agent._run_pin_template["prompt_bundle_sha256"]
    agent.react_engine.prompts = PromptConfig({"system": "x" * 600 + "b"})
    agent._initialize_run_pin_template()

    assert agent._run_pin_template["prompt_bundle_sha256"] != first


def test_run_pin_template_reads_run_order_index_from_env(tmp_path, monkeypatch):
    class PinConfig(BaseModel):
        thinking_model: str = "thinking-model"
        action_model: str = "action-model"
        max_iterations: int = 50

    agent = object.__new__(SetupAgent)
    agent._run_pin_host_path = tmp_path / "run-pin.json"
    agent.config = PinConfig()
    agent.react_engine = SimpleNamespace(prompts=PromptConfig({"system": "sys"}))
    agent.phase_machine = object()
    agent.agent_logger = SimpleNamespace(warning=lambda *_a, **_k: None)
    agent._resolve_sag_git_sha = lambda: "a" * 40
    agent._resolve_container_image_digest = lambda: "sha256:" + "b" * 64

    # The runner injects SAG_RUN_ORDER_INDEX; the template must carry it.
    monkeypatch.setenv("SAG_RUN_ORDER_INDEX", "11")
    agent._initialize_run_pin_template()
    assert agent._run_pin_template["run_order_index"] == 11

    # Absent (ad-hoc run) -> None, never a crash.
    monkeypatch.delenv("SAG_RUN_ORDER_INDEX", raising=False)
    agent._initialize_run_pin_template()
    assert agent._run_pin_template["run_order_index"] is None
