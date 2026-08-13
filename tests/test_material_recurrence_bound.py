"""The engine relays a tool's bounded recurrence into the one evidence ledger.

The third rung of #42 (docs/superpowers/specs/
2026-08-13-material-recurrence-bound-design.md §3). The tool owns the count,
the bound and the probe it stops paying for; it states the fact in typed
metadata. `RunEvidenceState` is written exclusively by the engine, so what the
ledger says about that fact is decided here — including the record-once rule
and the release, which the tool cannot own because it holds no blocker ids.
"""

from types import SimpleNamespace

from sag.agent.evidence_state import RunEvidenceState
from sag.agent.loop_memory import LoopMemory
from sag.agent.phase_machine import PhaseMachine
from sag.agent.react_engine import ReActEngine
from sag.agent.tool_orchestration import ToolCall, ToolExecution
from sag.evidence import OperationOutcome
from sag.tools.base import ToolResult
from sag.tools.internal.env_tool import EnvTool

ROCKETMQ_PATHS = [
    "/usr/share/maven/bin/mvn",
    "/opt/maven/bin/mvn",
    "/usr/bin/mvn",
]
WRAPPER_MOVE = "This project ships its own maven wrapper at /workspace/rocketmq/mvnw"
PROVISION_MOVE = "project(action='provision', packages=['maven'])"


def _engine():
    engine = ReActEngine.__new__(ReActEngine)
    engine.loop_memory = LoopMemory()
    engine.run_evidence_state = RunEvidenceState(run_id="run-material-bound")
    engine.phase_machine = PhaseMachine(start_phase="build")
    engine.current_iteration = 1
    engine.context_manager = SimpleNamespace()
    engine.guidance = []
    engine._add_system_guidance = lambda message, priority=5: engine.guidance.append(
        (message, priority)
    )
    return engine


def _marker(*, paths=None, moves=None, count=3, refs=("output_first",)):
    return {
        "tool": "maven",
        "error_code": "ENV_EXECUTABLE_NOT_FOUND",
        "refused_executables": list(ROCKETMQ_PATHS if paths is None else paths),
        "remaining_moves": list([WRAPPER_MOVE, PROVISION_MOVE] if moves is None else moves),
        "refusal_count": count,
        "bound": 3,
        "evidence_refs": list(refs),
    }


def _refusal(marker=None):
    metadata = {"action": "validate_executable"}
    if marker is not None:
        metadata["material_recurrence_bound"] = marker
    return ToolResult.completed_failure(
        output="",
        error="Env overlay executable is not executable or does not exist",
        error_code="ENV_EXECUTABLE_NOT_FOUND",
        metadata=metadata,
    )


def _execution(result):
    params = {"action": "env", "tool": "maven"}
    return ToolExecution(
        call=ToolCall(name="project", raw_params=params, validated_params=params),
        result=result,
        status="failure" if result.operation_outcome is OperationOutcome.FAILED else "success",
        raw_params=params,
        validated_params=params,
        executed_params=params,
        attempted_execution=True,
    )


def test_the_relayed_bound_records_one_blocker_naming_tool_paths_and_moves():
    """What §4's first unit asks for, at the writer that owns the ledger."""
    engine = _engine()

    engine._apply_tool_execution_loop_effects(_execution(_refusal(_marker())))

    (blocker,) = engine.run_evidence_state.blockers
    assert blocker.error_code == "MATERIAL_RECURRENCE_BOUND"
    assert blocker.category == "loop"
    signature = blocker.failure_signature
    assert signature.startswith("material_recurrence_bound:maven:ENV_EXECUTABLE_NOT_FOUND")
    assert "3 refusals" in signature
    for path in ROCKETMQ_PATHS:
        assert path in signature, "the blocker names the paths already refused"
    assert WRAPPER_MOVE in signature, "and the wrapper move that remains"
    assert PROVISION_MOVE in signature, "and the provision route that remains"
    assert "output_first" in blocker.evidence_refs, "and cites the refusals it came from"
    assert blocker.source_phase == "build", "the writer knows the phase the tool did not"


def test_a_result_without_the_marker_records_nothing():
    """The first two refusals carry no marker, so the ledger stays silent."""
    engine = _engine()

    engine._apply_tool_execution_loop_effects(_execution(_refusal()))

    assert engine.run_evidence_state.blockers == ()


def test_the_same_wall_restated_from_a_further_path_is_recorded_once():
    """Past the bound an untried path is still probed and still refused, which
    restates the same identity. The identity — not the paths, not the count —
    is what the record-once rule keys on."""
    engine = _engine()

    engine._apply_tool_execution_loop_effects(_execution(_refusal(_marker())))
    engine._apply_tool_execution_loop_effects(
        _execution(_refusal(_marker(paths=[*ROCKETMQ_PATHS, "/usr/local/bin/mvn"], count=4)))
    )

    assert len(engine.run_evidence_state.blockers) == 1


def test_a_second_tools_wall_is_its_own_blocker():
    """The bound is per (tool, error_code), and so is the record-once rule."""
    engine = _engine()

    engine._apply_tool_execution_loop_effects(_execution(_refusal(_marker())))
    gradle = _marker(paths=["/usr/bin/gradle"])
    gradle["tool"] = "gradle"
    engine._apply_tool_execution_loop_effects(_execution(_refusal(gradle)))

    assert len(engine.run_evidence_state.blockers) == 2


def test_a_reported_release_retires_the_blocker_and_the_next_wall_is_recordable():
    """A blocker standing against a tool that just registered would be a false
    statement in the ledger; the successful registration retires it, and a
    later wall is new information rather than the same one."""
    engine = _engine()
    engine._apply_tool_execution_loop_effects(_execution(_refusal(_marker())))

    released = ToolResult.completed_success(
        output="{}",
        metadata={
            "action": "register",
            "material_recurrence_released": {
                "tool": "maven",
                "error_code": "ENV_EXECUTABLE_NOT_FOUND",
                "reason": "maven registration succeeded",
            },
        },
    )
    engine._apply_tool_execution_loop_effects(_execution(released))

    (blocker,) = engine.run_evidence_state.blockers
    assert blocker.status == "resolved"
    assert blocker.resolution == "maven registration succeeded"

    engine._apply_tool_execution_loop_effects(_execution(_refusal(_marker())))

    assert len(engine.run_evidence_state.blockers) == 2, "a wall after a real success is new"


def test_a_sealed_ledger_is_never_written_by_the_relay():
    engine = _engine()
    engine.run_evidence_state.seal(finalized_at="2026-08-13T00:00:00Z")

    engine._apply_tool_execution_loop_effects(_execution(_refusal(_marker())))

    assert engine.run_evidence_state.blockers == ()


def test_the_tools_marker_and_the_engines_reader_agree_end_to_end():
    """The seam itself: the shape EnvTool emits is the shape the engine reads.
    Fakes on both sides would let the contract drift silently."""

    class _MissingEverything:
        def __init__(self):
            self.commands = []

        def execute_command(self, command, workdir=None, timeout=None):
            self.commands.append(command)
            if command.startswith(("test -x ", "realpath -e -- ")) or command.endswith(
                " -version"
            ):
                return {"success": False, "output": "", "exit_code": 1}
            return {"success": True, "output": "", "exit_code": 0}

        def write_file(self, path, content):
            return {"success": True, "output": "", "exit_code": 0}

    engine = _engine()
    tool = EnvTool(_MissingEverything())

    for path in ROCKETMQ_PATHS:
        result = tool.execute(action="register", tool="maven", executable=path, activate=True)
        engine._apply_tool_execution_loop_effects(_execution(result))

    (blocker,) = engine.run_evidence_state.blockers
    assert blocker.failure_signature.startswith(
        "material_recurrence_bound:maven:ENV_EXECUTABLE_NOT_FOUND"
    )
    for path in ROCKETMQ_PATHS:
        assert path in blocker.failure_signature
    assert PROVISION_MOVE in blocker.failure_signature
