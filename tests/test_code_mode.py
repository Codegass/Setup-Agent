"""Real QuickJS + shared SAG dispatch, with only project I/O replaced."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_turn_records import _sealing_engine

from sag.agent.code_program import child_value, execute_program
from sag.agent.loop_memory import LoopMemory
from sag.agent.native_messages import render_messages
from sag.agent.react_llm import NativeToolCall, NativeTurn
from sag.agent.react_types import StepType
from sag.code_runtime.program_rpc import run_program
from sag.tools.base import ToolResult, BaseTool
from sag.tools.code_tool import CodeTool
from sag.utils.container_io import ContainerWriteResult

BRIDGE = Path(__file__).resolve().parents[1] / "src/sag/code_runtime/worker.mjs"


class Reader(BaseTool):
    def __init__(self, reply=None):
        super().__init__("file_io", "Read a fixture page")
        self.calls = []
        self.reply = reply

    def execute(self, action: str, path: str) -> ToolResult:
        self.calls.append((action, path))
        if self.reply:
            return self.reply(action, path)
        body = json.dumps({"path": path, "next_path": path + ".next"})
        return ToolResult.completed_success(
            output=body + "\n[Requested range complete.]",
            raw_output=body,
            metadata={
                "output_page": True,
                "start_line": 0,
                "complete": True,
                "next": None,
                "path": path,
            },
        )


def engine_for(tmp_path, reader=None):
    engine = _sealing_engine(tmp_path, [])
    engine.steps = []
    engine.current_iteration = 1
    reader = reader or Reader()
    engine.tools["file_io"] = reader
    engine.tools["code"] = CodeTool(lambda code: execute_program(engine, code))
    engine.loop_memory = LoopMemory()
    engine.config.max_logical_tool_calls = 150
    driver = engine._get_tool_orchestrator()
    driver.before_tool_execute = engine._prepare_control_action
    driver.output_storage = engine.output_storage
    archives = {}

    def persist(path, body):
        archives[path] = body
        return ContainerWriteResult(True, "persisted")

    engine._code_persist = persist
    engine._code_run = lambda contract, dispatch, record: run_program(
        ["node", str(BRIDGE)],
        {**contract, "timeout_ms": 3000},
        dispatch,
        record=record,
        env={"PATH": os.environ["PATH"]},
    )
    return engine, reader, archives


def execute(engine, code, *extra):
    turn = NativeTurn(
        text="Compose reads.",
        tool_calls=(
            NativeToolCall(
                id="outer-code",
                name="code",
                arguments={"program": code},
                raw_arguments=json.dumps({"program": code}),
            ),
            *extra,
        ),
        model_used="scripted-model",
    )
    return engine._execute_native_calls(turn)


def events(engine):
    return [
        json.loads(line) for line in Path(engine.control_event_sink.path).read_text().splitlines()
    ]


def test_dependent_reads_use_real_shared_dispatch_and_only_parent_provider_pair(tmp_path):
    engine, reader, archives = engine_for(tmp_path)
    executed = execute(
        engine,
        'const a=await tools.file_io({action:"read",path:"pom.xml"}); '
        'const p=JSON.parse(a.text); const b=await tools.file_io({action:"read",path:p.next_path}); text(JSON.parse(b.text).path);',
    )
    parent = executed[0]
    assert parent.tool_result.succeeded
    assert reader.calls == [("read", "pom.xml"), ("read", "pom.xml.next")]
    assert engine._logical_tool_calls == 2
    child_steps = [
        s for s in engine.steps if s.step_type is StepType.ACTION and s.parent_program_id
    ]
    assert len(child_steps) == 2
    assert all(s.control_execution_id and s.control_envelope_id for s in child_steps)
    messages = render_messages("system", engine.steps)
    tools = [c for m in messages for c in m.get("tool_calls", [])]
    assert [c["function"]["name"] for c in tools] == ["code"]
    assert len([m for m in messages if m["role"] == "tool"]) == 1
    assert "pom.xml.next" in messages[-1]["content"]
    assert "not a build/test verdict" in messages[-1]["content"]
    archive = json.loads(archives[parent.tool_result.metadata["code_program_archive"]])
    assert archive["verdict_authority"] is False
    assert len(archive["children"]) == 2
    assert all(c["execution_id"] for c in archive["children"])
    recorded = [r for r in events(engine) if r["kind"] == "tool_result"]
    assert len(recorded) == 3 and [r["payload"]["tool"] for r in recorded] == [
        "file_io",
        "file_io",
        "code",
    ]
    assert len([r for r in events(engine) if r["kind"] == "loop_decision"]) == 3


def test_printing_success_and_mutating_child_cannot_erase_failure(tmp_path):
    engine, reader, archives = engine_for(
        tmp_path,
        Reader(
            lambda *_: ToolResult.completed_failure(
                output="compiler failed", error="wrong JDK", error_code="JDK_INCOMPATIBLE"
            )
        ),
    )
    parent = execute(
        engine,
        'const r=await tools.file_io({action:"read",path:"log"}); r.operation_outcome="success"; text("SUCCESS");',
    )[0]
    assert parent.tool_result.succeeded  # program execution only
    assert parent.tool_result.metadata["code_nonpassing_children"] == 1
    archive = json.loads(archives[parent.tool_result.metadata["code_program_archive"]])
    assert archive["children"][0]["operation_outcome"] == "failed"
    assert archive["children"][0]["error_code"] == "JDK_INCOMPATIBLE"
    assert engine.successful_states.get("build_success") is not True
    assert "JDK_INCOMPATIBLE" in render_messages("system", engine.steps)[-1]["content"]


def test_child_validation_remains_common_and_failed_child_can_be_inspected(tmp_path):
    engine, reader, archives = engine_for(tmp_path)
    parent = execute(
        engine,
        'const a=await tools.file_io({action:"read"}); text(a.error_code); '
        'text(await tools.file_io({action:"read",path:"valid"}));',
    )[0]
    assert reader.calls == [("read", "valid")]
    archive = json.loads(archives[parent.tool_result.metadata["code_program_archive"]])
    assert [c["operation_outcome"] for c in archive["children"]] == ["failed", "success"]
    assert archive["children"][0]["error_code"] == "PARAMETER_VALIDATION_FAILED"


@pytest.mark.parametrize("tool", ["phase", "report", "advisor", "manage_context", "code"])
def test_nested_control_tools_absent(tmp_path, tool):
    engine, reader, archives = engine_for(tmp_path)
    parent = execute(engine, f'await tools.{tool}({{action:"done"}});')[0]
    assert not parent.tool_result.succeeded
    assert not reader.calls
    assert parent.tool_result.metadata["code_child_count"] == 0


def test_pending_barrier_stops_queued_children_and_following_native_call(tmp_path):
    pending = ToolResult(
        invocation_status="pending",
        operation_outcome="unknown",
        evidence_status="unknown",
        output="build running",
        poll_ref="job:bound",
        metadata={"job_id": "bound"},
    )
    engine, reader, archives = engine_for(tmp_path, Reader(lambda *_: pending))
    checked = []

    def capture():
        result = engine._answered_action_result()
        checked.append(result)
        return result is not None and result.invocation_status.value == "pending"

    engine._capture_job_barrier_from_result = capture
    extra = NativeToolCall(
        id="after",
        name="file_io",
        arguments={"action": "read", "path": "after"},
        raw_arguments="{}",
    )
    parent = execute(
        engine,
        'await Promise.all([tools.file_io({action:"read",path:"first"}),'
        'tools.file_io({action:"read",path:"second"})]);',
        extra,
    )[0]
    assert reader.calls == [("read", "first")]
    assert not parent.tool_result.succeeded
    assert "barrier" in parent.tool_result.metadata["code_stop_reason"]
    assert parent.tool_result.metadata["code_not_executed"] == 1
    assert any(r is not None and r.invocation_status.value == "pending" for r in checked)
    assert any(s.tool_call_id == "after" and "not executed" in s.content for s in engine.steps)


def test_native_and_child_actions_share_budget(tmp_path):
    engine, reader, archives = engine_for(tmp_path)
    engine._logical_tool_calls = 149
    parent = execute(
        engine,
        'await tools.file_io({action:"read",path:"first"}); await tools.file_io({action:"read",path:"second"});',
    )[0]
    assert len(reader.calls) == 1
    assert engine._logical_tool_calls == 150
    assert not parent.tool_result.succeeded
    extra = NativeTurn(
        text="",
        tool_calls=(
            NativeToolCall(
                id="native",
                name="file_io",
                arguments={"action": "read", "path": "third"},
                raw_arguments="{}",
            ),
        ),
        model_used="scripted-model",
    )
    engine._execute_native_calls(extra)
    assert len(reader.calls) == 1
    assert any("logical tool call budget" in s.content for s in engine.steps)


def test_real_page_text_excludes_footer_but_search_stays_bounded():
    read = ToolResult.completed_success(
        output="{}\n[footer]",
        raw_output="{}",
        metadata={"output_page": True, "start_line": 0, "complete": True},
    )
    assert json.loads(child_value(read)["text"]) == {}
    search = ToolResult.completed_success(
        output="matched snippet",
        raw_output="full log",
        metadata={"output_page": True, "next": None},
    )
    assert child_value(search)["text"] == "matched snippet"
    assert child_value(search)["page"]["complete"] is None


def test_program_scope_is_not_material_progress(tmp_path):
    engine, reader, archives = engine_for(tmp_path)
    execute(engine, 'text("SUCCESS");')
    assert getattr(engine, "_logical_tool_calls", 0) == 0
    assert not reader.calls
    assert not engine.run_evidence_state.tool_observations


def test_program_does_not_consume_repair_and_child_cannot_bypass_required_intent(tmp_path):
    from sag.agent.repair_contexts import RepairContext
    from test_repair_contexts import context_payload

    engine, reader, archives = engine_for(tmp_path)
    payload = context_payload()
    payload["allowed_tool_affordances"] = [
        {
            "tool": "file_io",
            "action_parameter": "action",
            "action_kinds": ["read"],
            "constraint_refs": [],
        }
    ]
    context = RepairContext.model_validate(payload)
    engine._pending_repair_context = context
    parent = execute(
        engine, 'const r=await tools.file_io({action:"read",path:"x"}); text(r.error_code);'
    )[0]
    assert not reader.calls
    assert parent.tool_result.metadata["code_child_count"] == 1
    archive = json.loads(archives[parent.tool_result.metadata["code_program_archive"]])
    assert archive["children"][0]["error_code"] == "REPAIR_INTENT_REQUIRED"
    assert engine._pending_repair_context == context


def test_evidence_close_disables_wrapper_as_well_as_children(tmp_path):
    engine, reader, archives = engine_for(tmp_path)
    engine.run_evidence_state.seal(finalized_at="2026-10-03T00:00:00Z")
    assert not engine._tool_available("code")
    result = execute(engine, 'await tools.file_io({action:"read",path:"x"});')[0].tool_result
    assert result.operation_outcome.value == "skipped"
    assert not reader.calls and not archives


def test_context_history_cannot_treat_script_claim_as_project_evidence():
    from sag.agent.history_state import history_entry_kind_for_tool, is_non_evidence_history_entry

    assert history_entry_kind_for_tool("code") == "code_program"
    assert is_non_evidence_history_entry(
        {"entry_kind": "code_program", "output": "BUILD SUCCESS requires java 17"}
    )
    assert not is_non_evidence_history_entry({"output": "BUILD SUCCESS", "tool": "build"})


def test_loop_phase_close_happens_only_after_program_result_is_sealed(tmp_path):
    from dataclasses import replace

    engine, reader, archives = engine_for(tmp_path)
    closed = []
    original = engine._apply_tool_execution_loop_effects

    def loop(execution):
        result = original(execution)
        if execution.call.name == "file_io":
            return replace(result, close_phase=True)
        return result

    engine._apply_tool_execution_loop_effects = loop

    def close(record, route):
        parent = next(s for s in engine.steps if s.tool_name == "code")
        assert parent.tool_result is not None
        body = json.loads(archives[parent.tool_result.metadata["code_program_archive"]])
        assert body["status"] == "failed"
        closed.append(True)

    engine._apply_phase_decision = close
    parent = execute(
        engine,
        'await tools.file_io({action:"read",path:"first"}); await tools.file_io({action:"read",path:"second"});',
    )[0]
    assert reader.calls == [("read", "first")]
    assert not parent.tool_result.succeeded
    assert closed == [True]


def test_budget_exhaustion_does_not_spend_more_model_turns():
    from test_native_loop_engine import _engine, _phase_turn

    engine = _engine([_phase_turn(1), _phase_turn(2)])
    engine.config.max_logical_tool_calls = 1
    aborted = []
    engine.abort = lambda *, reason: aborted.append(reason) or False
    assert engine._run_react_loop("fixture", completion_mode="setup") is False
    assert len(engine.llm_client.requests) == 1
    assert aborted == ["logical tool call cap exceeded"]


@pytest.mark.parametrize("enabled", [False, True])
def test_code_registration_is_explicit_and_keeps_native_tools(enabled):
    from test_advisor_tool import _registration_agent

    agent = _registration_agent()
    agent.config.code_mode = enabled
    names = {tool.name for tool in agent._initialize_tools()}
    assert ("code" in names) is enabled
    assert {"build", "bash", "file_io", "project", "search", "phase", "report"} <= names


def test_unawaited_child_does_not_masquerade_as_completed_program(tmp_path):
    import time

    def reply(*_):
        time.sleep(0.15)
        return ToolResult.completed_success(output="actual read completed")

    engine, reader, archives = engine_for(tmp_path, Reader(reply))
    parent = execute(engine, 'tools.file_io({action:"read",path:"x"}); return "done";')[0]
    assert not parent.tool_result.succeeded
    archive = json.loads(archives[parent.tool_result.metadata["code_program_archive"]])
    if reader.calls:
        assert archive["children"][0]["operation_outcome"] == "success"
    else:
        assert archive["not_executed"]
