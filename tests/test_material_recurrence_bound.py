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


def _refusal(marker=None, error_code="ENV_EXECUTABLE_NOT_FOUND"):
    metadata = {"action": "validate_executable"}
    if marker is not None:
        metadata["material_recurrence_bound"] = marker
    return ToolResult.completed_failure(
        output="",
        error=f"Env overlay registration was refused: {error_code}",
        error_code=error_code,
        metadata=metadata,
    )


def _execution(result, params=None, *, tool_name="project"):
    params = dict(params) if params is not None else {"action": "env", "tool": "maven"}
    return ToolExecution(
        call=ToolCall(name=tool_name, raw_params=params, validated_params=params),
        result=result,
        status="failure" if result.operation_outcome is OperationOutcome.FAILED else "success",
        raw_params=params,
        validated_params=params,
        executed_params=params,
        attempted_execution=True,
    )


class _MissingEverything:
    """Nothing the model names exists: every probe this tool pays for refuses."""

    def __init__(self):
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append(command)
        if command.startswith(("test -x ", "realpath -e -- ")) or command.endswith(" -version"):
            return {"success": False, "output": "", "exit_code": 1}
        return {"success": True, "output": "", "exit_code": 0}

    def write_file(self, path, content):
        return {"success": True, "output": "", "exit_code": 0}


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


def test_a_release_retires_every_wall_it_names_and_no_others():
    """The bound counts a family of refusal codes, so one tool can stand walled
    under more than one. A release states which walls it ends; the ledger
    retires those and leaves the rest, because nothing was observed about them."""
    engine = _engine()
    probe_marker = _marker()
    probe_marker["error_code"] = "ENV_RUNTIME_PROBE_FAILED"
    identity_marker = _marker()
    identity_marker["error_code"] = "ENV_RUNTIME_IDENTITY_MISMATCH"
    for marker in (_marker(), probe_marker, identity_marker):
        engine._apply_tool_execution_loop_effects(
            _execution(_refusal(marker, error_code=marker["error_code"]))
        )
    assert len(engine.run_evidence_state.blockers) == 3

    released = ToolResult.completed_success(
        output="{}",
        metadata={
            "action": "register",
            "material_recurrence_released": {
                "tool": "maven",
                "error_codes": ["ENV_EXECUTABLE_NOT_FOUND", "ENV_RUNTIME_PROBE_FAILED"],
                "reason": "maven registration succeeded",
            },
        },
    )
    engine._apply_tool_execution_loop_effects(_execution(released))

    retired = {
        blocker.failure_signature.split(":")[2]
        for blocker in engine.run_evidence_state.blockers
        if blocker.status == "resolved"
    }
    assert retired == {"ENV_EXECUTABLE_NOT_FOUND", "ENV_RUNTIME_PROBE_FAILED"}
    standing = [
        blocker for blocker in engine.run_evidence_state.blockers if blocker.status == "active"
    ]
    assert len(standing) == 1
    assert standing[0].failure_signature.startswith(
        "material_recurrence_bound:maven:ENV_RUNTIME_IDENTITY_MISMATCH"
    ), "a wall the release did not name is still standing"


def test_a_sealed_ledger_is_never_written_by_the_relay():
    engine = _engine()
    engine.run_evidence_state.seal(finalized_at="2026-08-13T00:00:00Z")

    engine._apply_tool_execution_loop_effects(_execution(_refusal(_marker())))

    assert engine.run_evidence_state.blockers == ()


def test_the_tools_marker_and_the_engines_reader_agree_end_to_end():
    """The seam itself: the shape EnvTool emits is the shape the engine reads.
    Fakes on both sides would let the contract drift silently."""
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


# ---------------------------------------------------------------------------
# The bound may not disarm the rung below it (#46 defect 1)
# ---------------------------------------------------------------------------


def test_the_bound_does_not_reset_the_engines_recurrence_chain():
    """The seam the ladder depends on: `OutcomeKey` is
    (operation_outcome, error_code, failure_signature) (loop_memory.py:145-151)
    and the engine feeds it straight from the tool's result. A bounded refusal
    that re-typed itself would open a NEW LoopMemory chain exactly when the
    tool declared the wall, pushing close_phase from the 5th identical call out
    to the 8th — the second rung disarmed by the third. So the typed identity
    of a bounded refusal is the identity of the refusal it stands in for; the
    bound is carried by the marker, not by a new code."""
    engine = _engine()
    orchestrator = _MissingEverything()
    tool = EnvTool(orchestrator)
    path = ROCKETMQ_PATHS[0]
    params = {"action": "env", "tool": "maven", "executable": path}

    decisions = []
    probes_at_bound = 0
    for attempt in range(5):
        result = tool.execute(action="register", tool="maven", executable=path, activate=True)
        assert result.error_code == "ENV_EXECUTABLE_NOT_FOUND", (
            "every statement of this wall carries the same typed code, bounded or not"
        )
        decisions.append(engine._apply_tool_execution_loop_effects(_execution(result, params)))
        if attempt == 2:
            probes_at_bound = len(orchestrator.commands)

    assert [decision.recurrence_count for decision in decisions] == [1, 2, 3, 4, 5], (
        "one unbroken chain across the bound"
    )
    assert [decision.decision for decision in decisions] == [
        "continue",
        "guide",
        "guide",
        "force_break",
        "close_phase",
    ], "the second rung still closes the phase on the 5th identical call"
    assert len(orchestrator.commands) == probes_at_bound, (
        "and the bound still stops paying for the probe"
    )
    assert len(engine.run_evidence_state.blockers) == 2, (
        "the bound's blocker and the loop force-break's blocker, each stated once"
    )


# ---------------------------------------------------------------------------
# A wall the model walked around is not a standing blocker (#46 defect 3)
# ---------------------------------------------------------------------------


def _build_result(system, *, receipt_id="receipt_maven_1", succeeded=True):
    """One build-facade result as the runner reports it.

    `receipt_id` is present exactly when this dispatch's invocation receipt was
    persisted (maven_tool.py:1088, gradle_tool.py:785); a dispatch that never
    ran, or whose receipt never landed, carries no receipt id.
    """
    payload = {
        "output": "BUILD SUCCESS" if succeeded else "BUILD FAILURE",
        "facts": {"system": system},
        "metadata": {"system": system, "working_directory": "/workspace"},
    }
    if receipt_id:
        payload["metadata"]["receipt_id"] = receipt_id
    if succeeded:
        return ToolResult.completed_success(**payload)
    return ToolResult.completed_failure(
        error="the reactor failed", error_code="MAVEN_BUILD_FAILED", **payload
    )


def _build_execution(system, **kwargs):
    params = {"action": "build", "working_directory": "/workspace"}
    return _execution(_build_result(system, **kwargs), params, tool_name="build")


def test_a_terminal_build_receipt_for_the_walled_tools_system_retires_the_blocker():
    """c0339ae steers the model AWAY from registering: "the build tool already
    uses the wrapper — dispatch the build instead". A model that takes that
    advice and builds never registers anything, so a release keyed only on a
    successful REGISTRATION leaves the wall restated under ACTIVE BLOCKERS at
    every phase entry to the end of the run. The runner that just ran is the
    proof the wall was walked around."""
    engine = _engine()
    engine._apply_tool_execution_loop_effects(_execution(_refusal(_marker())))
    (blocker,) = engine.run_evidence_state.blockers
    assert blocker.status == "active"

    engine._apply_tool_execution_loop_effects(_build_execution("maven"))

    (blocker,) = engine.run_evidence_state.blockers
    assert blocker.status == "resolved"
    assert blocker.resolution == "a maven build succeeded via its own runner"


def test_a_build_of_another_system_leaves_the_walled_tools_blocker_standing():
    """A Gradle build says nothing about a Maven registration wall, and a
    ledger that retired it would be stating a fact no receipt supports."""
    engine = _engine()
    engine._apply_tool_execution_loop_effects(_execution(_refusal(_marker())))

    engine._apply_tool_execution_loop_effects(
        _build_execution("gradle", receipt_id="receipt_gradle_1")
    )

    (blocker,) = engine.run_evidence_state.blockers
    assert blocker.status == "active"


def test_a_build_result_without_a_terminal_receipt_leaves_the_blocker_standing():
    """The release is owed to a receipt, not to an output string: a facade
    result whose runner never dispatched (pre-flight refusal, resolution
    failure) carries no receipt id and has proven nothing about the runtime."""
    engine = _engine()
    engine._apply_tool_execution_loop_effects(_execution(_refusal(_marker())))

    engine._apply_tool_execution_loop_effects(_build_execution("maven", receipt_id=""))

    (blocker,) = engine.run_evidence_state.blockers
    assert blocker.status == "active"


def test_a_failed_build_leaves_the_blocker_standing_because_the_reason_would_be_false():
    """`resolution` is a statement in the ledger. "a maven build succeeded via
    its own runner" is only true of a build that succeeded, so a terminal
    receipt for a FAILED build does not buy the release."""
    engine = _engine()
    engine._apply_tool_execution_loop_effects(_execution(_refusal(_marker())))

    engine._apply_tool_execution_loop_effects(_build_execution("maven", succeeded=False))

    (blocker,) = engine.run_evidence_state.blockers
    assert blocker.status == "active"


def test_a_build_release_does_not_touch_a_blocker_the_relay_never_wrote():
    """The release is scoped to this run's material-recurrence blockers; every
    other blocker in the ledger is another writer's statement."""
    engine = _engine()
    engine.run_evidence_state.record_blocker(
        category="loop",
        error_code="LOOP_WITHOUT_PROGRESS",
        failure_signature="loop_without_progress:maven:abcdef123456",
    )

    engine._apply_tool_execution_loop_effects(_build_execution("maven"))

    (blocker,) = engine.run_evidence_state.blockers
    assert blocker.status == "active"


def test_a_sealed_ledger_is_never_released_by_a_build_either():
    engine = _engine()
    engine._apply_tool_execution_loop_effects(_execution(_refusal(_marker())))
    engine.run_evidence_state.seal(finalized_at="2026-08-13T00:00:00Z")

    engine._apply_tool_execution_loop_effects(_build_execution("maven"))

    (blocker,) = engine.run_evidence_state.blockers
    assert blocker.status == "active"
