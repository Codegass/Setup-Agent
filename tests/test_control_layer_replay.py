import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

import sag.agent.agent as agent_module
from sag.agent.agent import SetupAgent, _active_setup_run_id
from sag.agent.control_events import (
    ControlEvent,
    ControlEventSink,
    RunPin,
    canonical_sha256,
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
        "dependency_cache_state": "warm",
        "host_arch": "arm64",
    }
    agent.agent_logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None)

    agent._record_target_repo_sha("a" * 40)

    pin = RunPin.model_validate_json((tmp_path / "run-pin.json").read_text(encoding="utf-8"))
    assert pin.target_repo_sha == "a" * 40
    assert mirrored == [(tmp_path / "run-pin.json").read_text(encoding="utf-8")]


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
