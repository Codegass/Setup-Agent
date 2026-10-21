"""The harness does the setup the pinned task names before the first model turn.

Measured on the round-3 ledgers (7 runs): the provision phase cost two to three
model requests per run (14-16 requests, 105k-123k tokens), and every call in it
was read from task.json: `project(action='clone', repo_url, ref)`, one
`project(action='provision', java_version, java_distribution)`, one
`project(action='provision', maven_version)` and `phase(action='done')`.

So when a pinned task is bound, the harness makes those calls itself at loop
start, as controller actions recorded like any dispatched call. When the
checkout landed on the pinned commit and every pinned toolchain item installed,
the harness closes provision through the ordinary gate and policy, with no
model request. Otherwise the model turn proceeds, and the provision brief says
what the harness already did, one `Provisioned: ` line per call.
"""

from test_native_loop_engine import _engine, _phase_turn
from test_turn_records import _BillingClient, _events

from sag.agent.acceptance_task import AcceptanceStep, AcceptanceTask
from sag.agent.control_events import ControlEventSink
from sag.agent.loop_memory import LoopMemory
from sag.agent.output_storage import OutputStorageManager
from sag.agent.phase_machine import PhaseMachine, PhaseOutcome
from sag.agent.react_engine import PHASE_OBJECTIVES, ReActEngine
from sag.agent.task_provisioning import assess_provision_result, plan_task_provision
from sag.agent.token_tracker import TokenTracker
from sag.agent.tool_orchestration import ToolOrchestrator
from sag.agent.verdict_finalizer import RunTerminationStatus
from sag.config.settings import Config
from sag.tools.base import ToolResult
from sag.tools.project_tool import ProjectTool
from sag.trajectory.builder import build_trajectory

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"
REPO = "apache/demo"
URL = "https://github.com/apache/demo.git"
WORKSPACE = "/workspace/demo"
CLOSING = (
    "Provisioned: the lines above are what the harness already did from the pinned task; "
    "continue from there."
)


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _maven(step_id="unit", java_major=17, maven_version="3.9.16"):
    return AcceptanceStep(
        id=step_id,
        runner="maven",
        argv=("mvn", "-B", "test"),
        java_major=java_major,
        maven_version=maven_version,
    )


def _gradle(step_id="unit", java_major=17):
    return AcceptanceStep(
        id=step_id, runner="gradle", argv=("./gradlew", "test"), java_major=java_major
    )


def _task(*steps):
    return AcceptanceTask(repo=REPO, sha=SHA, steps=steps or (_maven(),))


class _Setup:
    """The clone delegate behind the real `project` facade."""

    def __init__(self, *, resolved=SHA, error=None):
        self.resolved = resolved
        self.error = error
        self.calls = []

    def execute(self, action, repository_url=None, ref=None, target_directory=None):
        self.calls.append({"action": action, "repository_url": repository_url, "ref": ref})
        if self.error:
            return ToolResult.completed_failure(
                output="", error=self.error, error_code="CLONE_FAILED"
            )
        return ToolResult.completed_success(
            output=f"Repository cloned into {WORKSPACE}",
            metadata={
                "repository_url": repository_url,
                "clone_path": WORKSPACE,
                "ref": ref,
                "resolved_commit": self.resolved,
            },
        )


class _System:
    """The provision delegate behind the real `project` facade."""

    def __init__(self, *, fail=()):
        self.fail = set(fail)
        self.calls = []

    def execute(
        self,
        action,
        packages=None,
        java_version=None,
        maven_version=None,
        java_distribution=None,
        java_capabilities=None,
        requirement=None,
    ):
        call = {"action": action}
        if java_version is not None:
            call.update(java_version=java_version, java_distribution=java_distribution)
        if maven_version is not None:
            call.update(maven_version=maven_version)
        self.calls.append(call)
        if action in self.fail:
            return ToolResult.completed_failure(
                output="",
                error=f"{action} failed:\n  apt could not reach the mirror " + "x" * 400,
                error_code="INSTALL_FAILED",
            )
        return ToolResult.completed_success(output=f"{action} done")


def _provision_engine(
    tmp_path,
    turns,
    *,
    task,
    setup=None,
    system=None,
    workspace_present=True,
):
    """The scripted native loop with a real ledger, the real `project` facade and
    the real provision brief; later phases keep the loop fixture's intro."""
    engine = _engine(turns)
    container = engine.verdict_finalizer.orchestrator
    container_execute = container.execute_command

    def execute_command(command, **kwargs):
        if command.startswith("test -d "):
            output = "exists\n" if workspace_present else "missing\n"
            return {"success": True, "exit_code": 0, "output": output}
        return container_execute(command, **kwargs)

    container.execute_command = execute_command
    container.acceptance_task = task
    engine.orchestrator = container
    engine.context_manager.project_name = "demo"
    engine.control_event_sink = ControlEventSink(
        tmp_path / "control_events.jsonl", run_id=engine.run_evidence_state.run_id
    )
    engine.output_storage = OutputStorageManager(tmp_path / "contexts")
    engine.token_tracker = TokenTracker()
    engine.loop_memory = LoopMemory()
    engine.llm_client = _BillingClient(turns, engine.token_tracker)
    engine.tools["project"] = ProjectTool(
        setup_tool=setup if setup is not None else _Setup(),
        system_tool=system if system is not None else _System(),
    )
    stub_intro = engine._phase_intro_step
    engine._phase_intro_step = lambda: (
        ReActEngine._phase_intro_step(engine)
        if engine.phase_machine.current_phase == "provision"
        else stub_intro()
    )
    # Production wires the intent-minting boundary in front of every tool.
    engine._pending_repair_context = None
    engine._last_invocation_contract_id = None
    orchestrator = ToolOrchestrator(
        tools=engine.tools,
        context_manager=engine.context_manager,
        recent_tool_executions=engine.recent_tool_executions,
        successful_states=engine.successful_states,
        repository_url=engine.repository_url,
        track_tool_execution=lambda *a, **k: None,
        update_successful_states=lambda *a, **k: None,
        add_system_guidance=lambda *a, **k: None,
        get_timestamp=lambda: "2026-10-21T00:00:00Z",
        output_storage=None,
        before_tool_execute=engine._prepare_control_action,
    )
    engine._get_tool_orchestrator = lambda: orchestrator
    return engine


def _turns_after_provision():
    return [_phase_turn(index) for index in range(2, 6)]


def _all_turns():
    return [_phase_turn(index) for index in range(1, 6)]


def _project_envelopes(engine):
    return [
        row["payload"]
        for row in _events(engine, "action_envelope")
        if row["payload"]["tool"] == "project"
    ]


def _provision_brief(engine):
    """The provision intro the model read in its first request."""
    request = engine.llm_client.requests[0]
    return request[1]["content"]


def _provisioned_block(brief):
    """The `Provisioned: ` lines, which must sit right under the Objective."""
    lines = brief.splitlines()
    anchor = lines.index(f"Objective: {PHASE_OBJECTIVES['provision']}") + 1
    block = []
    for line in lines[anchor:]:
        if not line.startswith("Provisioned: "):
            break
        block.append(line)
    assert [line for line in lines if line.startswith("Provisioned: ")] == block
    return block


# ---------------------------------------------------------------------------
# (a) java and maven pinned: the harness provisions and closes, no model turn
# ---------------------------------------------------------------------------


def test_a_pinned_task_is_provisioned_and_closed_without_a_model_turn(tmp_path):
    setup, system = _Setup(), _System()
    engine = _provision_engine(
        tmp_path, _turns_after_provision(), task=_task(), setup=setup, system=system
    )

    result = engine.run_setup_loop("set up the project", max_iterations=12)

    assert result.termination is RunTerminationStatus.COMPLETED
    assert engine.phase_machine.is_complete
    # analyze, build, test, report — nothing in provision.
    assert len(engine.llm_client.requests) == 4
    assert "=== PHASE: ANALYZE ===" in engine.llm_client.requests[0][1]["content"]
    # The run picture says provision completed; the close is not resent in analyze.
    analyze_request = engine.llm_client.requests[0]
    assert not any("PHASE_CLOSED" in str(message["content"]) for message in analyze_request)

    assert setup.calls == [{"action": "clone", "repository_url": URL, "ref": SHA}]
    assert system.calls == [
        {"action": "install_java", "java_version": "17", "java_distribution": "openjdk"},
        {"action": "install_maven", "maven_version": "3.9.16"},
    ]

    envelopes = _project_envelopes(engine)
    assert [envelope["intent_source"] for envelope in envelopes] == ["controller"] * 3
    assert [envelope["exact_params"] for envelope in envelopes] == [
        {"action": "clone", "repo_url": URL, "ref": SHA},
        {"action": "provision", "java_version": "17", "java_distribution": "openjdk"},
        {"action": "provision", "maven_version": "3.9.16"},
    ]
    results = {row["payload"]["envelope_id"] for row in _events(engine, "tool_result")}
    assert {envelope["envelope_id"] for envelope in envelopes} <= results

    records = [row["payload"] for row in _events(engine, "turn_record")]
    action_turns = [
        row
        for row in records
        if row["envelope_ref"] in {envelope["envelope_id"] for envelope in envelopes}
    ]
    assert [(row["actor"], row["phase"]) for row in action_turns] == [
        ("controller", "provision")
    ] * 3
    assert [row for row in records if row["actor"] == "model" and row["phase"] == "provision"] == []
    assert [row["turn_id"] for row in records] == list(range(1, len(records) + 1))

    gates = [row["payload"] for row in _events(engine, "gate_decision")]
    provision_gates = [gate for gate in gates if gate["phase"] == "provision"]
    assert len(provision_gates) == 1
    gate = provision_gates[0]
    assert gate["code"] == "workspace_present"
    assert gate["expected_accepted"] is True
    assert gate["claimed_outcome"] == "success"
    assert gate["key_results"] == (
        f"Checked out {REPO} at {SHA} in {WORKSPACE}; java 17 (openjdk) and maven 3.9.16 "
        "installed and active, as the pinned task requires."
    )
    # The claim cites each call's output and the workspace.
    claim_refs = gate["phase_claim"]["evidence_refs"]
    assert claim_refs[-1] == WORKSPACE
    assert len([ref for ref in claim_refs if ref.startswith("output_")]) == 3
    gate_turns = [row for row in records if row["gate_decision_id"] == gate["decision_id"]]
    assert [(row["actor"], row["phase"]) for row in gate_turns] == [("controller", "provision")]

    transitions = [row["payload"] for row in _events(engine, "phase_transition")]
    assert (transitions[0]["expected_kind"], transitions[0]["expected_target"]) == (
        "advance",
        "analyze",
    )
    provision = engine.phase_machine.records[0]
    assert (provision.phase, provision.outcome) == ("provision", PhaseOutcome.SUCCESS)
    assert engine.run_evidence_state.fact_value("provision.workspace_ready") is True
    assert engine.run_evidence_state.fact_provenance("provision.workspace_ready") == WORKSPACE

    # The derived view reads provision as the harness's turns, with no hole.
    trajectory = build_trajectory(tmp_path)
    provision_turns = [turn for turn in trajectory.turns if turn.phase == "provision"]
    assert [(turn.actor, turn.call.tool if turn.call else None) for turn in provision_turns] == [
        ("controller", "project"),
        ("controller", "project"),
        ("controller", "project"),
        ("controller", None),
    ]
    assert trajectory.warnings == []


def test_a_landed_call_without_an_output_ref_still_closes_the_phase(tmp_path):
    """A small tool output has no output_ref; the claim cites what exists."""
    engine = _provision_engine(tmp_path, _turns_after_provision(), task=_task(_gradle()))
    engine.steps = [engine._phase_intro_step()]
    checkout, java = plan_task_provision(_task(_gradle())).actions
    cloned = ToolResult.completed_success(
        output="cloned",
        output_ref="output_clone",
        metadata={"clone_path": WORKSPACE, "resolved_commit": SHA},
    )
    installed = ToolResult.completed_success(output="ok")
    assert installed.output_ref is None

    assert engine._close_provision_from_task(
        _task(_gradle()), [(checkout, cloned), (java, installed)]
    )

    assert engine.phase_machine.current_phase == "analyze"
    gate = next(
        row["payload"]
        for row in _events(engine, "gate_decision")
        if row["payload"]["phase"] == "provision"
    )
    assert gate["expected_accepted"] is True
    assert gate["phase_claim"]["evidence_refs"] == ["output_clone", WORKSPACE]


def test_the_harness_calls_are_answered_in_the_window_like_any_call(tmp_path):
    """A provision window the model does see (the clone failed) pairs every call."""
    engine = _provision_engine(
        tmp_path, _all_turns(), task=_task(), setup=_Setup(error="network unreachable")
    )

    engine.run_setup_loop("set up the project", max_iterations=12)

    request = engine.llm_client.requests[0]
    calls = [call["id"] for message in request for call in message.get("tool_calls") or ()]
    answers = [message["tool_call_id"] for message in request if message["role"] == "tool"]
    assert len(calls) == 3 and all(call.startswith("harness-provision-") for call in calls)
    assert calls == answers


# ---------------------------------------------------------------------------
# (b) a failed clone keeps the model turn, and the brief says what was done
# ---------------------------------------------------------------------------


def test_a_failed_clone_keeps_the_model_turn_and_the_brief_lists_it(tmp_path):
    system = _System()
    engine = _provision_engine(
        tmp_path,
        _all_turns(),
        task=_task(),
        setup=_Setup(error="fatal: unable to access the repository:\n  network unreachable"),
        system=system,
    )

    result = engine.run_setup_loop("set up the project", max_iterations=12)

    assert result.termination is RunTerminationStatus.COMPLETED
    assert len(engine.llm_client.requests) == 5
    # The toolchain does not depend on the checkout, so it is still installed.
    assert [call["action"] for call in system.calls] == ["install_java", "install_maven"]
    assert _provisioned_block(_provision_brief(engine)) == [
        f"Provisioned: checkout {REPO} at {SHA} → failed: fatal: unable to access the "
        "repository: network unreachable",
        "Provisioned: java 17 (openjdk) → installed and active (ok)",
        "Provisioned: maven 3.9.16 → installed and active (ok)",
        CLOSING,
    ]
    # The objective itself is unchanged.
    assert f"Objective: {PHASE_OBJECTIVES['provision']}" in _provision_brief(engine)
    # The model closed provision itself; the harness sealed no provision gate.
    records = [row["payload"] for row in _events(engine, "turn_record")]
    assert [
        row["actor"] for row in records if row["phase"] == "provision" and row["gate_decision_id"]
    ] == []
    assert any(row["actor"] == "model" and row["phase"] == "provision" for row in records)


def test_a_checkout_at_another_commit_is_not_the_pinned_one(tmp_path):
    engine = _provision_engine(
        tmp_path, _all_turns(), task=_task(), setup=_Setup(resolved=OTHER_SHA)
    )

    engine.run_setup_loop("set up the project", max_iterations=12)

    assert len(engine.llm_client.requests) == 5
    assert _provisioned_block(_provision_brief(engine))[0] == (
        f"Provisioned: checkout {REPO} at {SHA} → failed: the checkout in {WORKSPACE} is at "
        f"{OTHER_SHA}, not the pinned commit"
    )


def test_a_checkout_whose_commit_cannot_be_read_is_not_the_pinned_one():
    """An existing directory can clone "successfully" with no commit on record."""
    action = plan_task_provision(_task()).actions[0]
    result = ToolResult.completed_success(
        output="Repository cloned successfully to demo",
        metadata={"repository_url": URL, "target_directory": "demo"},
    )

    assert assess_provision_result(action, result) == (
        False,
        f"Provisioned: checkout {REPO} at {SHA} → failed: the harness could not read which "
        "commit the checkout in demo is at",
    )


def test_a_failed_install_keeps_the_model_turn_and_its_error_is_one_bounded_line(tmp_path):
    engine = _provision_engine(
        tmp_path, _all_turns(), task=_task(), system=_System(fail={"install_java"})
    )

    engine.run_setup_loop("set up the project", max_iterations=12)

    assert len(engine.llm_client.requests) == 5
    block = _provisioned_block(_provision_brief(engine))
    assert block[0] == f"Provisioned: checkout {REPO} at {SHA} → {WORKSPACE} (ok)"
    prefix = "Provisioned: java 17 (openjdk) → failed: "
    assert block[1].startswith(prefix + "install_java failed: apt could not reach the mirror x")
    assert len(block[1]) - len(prefix) <= 300
    assert block[2:] == ["Provisioned: maven 3.9.16 → installed and active (ok)", CLOSING]


# ---------------------------------------------------------------------------
# (c) no task bound: nothing happens
# ---------------------------------------------------------------------------


def test_no_task_bound_means_no_harness_action(tmp_path):
    setup, system = _Setup(), _System()
    engine = _provision_engine(tmp_path, _all_turns(), task=None, setup=setup, system=system)

    result = engine.run_setup_loop("set up the project", max_iterations=12)

    assert result.termination is RunTerminationStatus.COMPLETED
    assert len(engine.llm_client.requests) == 5
    assert setup.calls == [] and system.calls == []
    assert _project_envelopes(engine) == []
    assert "Provisioned: " not in _provision_brief(engine)


# ---------------------------------------------------------------------------
# (d) a gradle task pins java only: java installed, no maven, phase closed
# ---------------------------------------------------------------------------


def test_a_gradle_task_installs_java_only_and_closes(tmp_path):
    setup, system = _Setup(), _System()
    engine = _provision_engine(
        tmp_path,
        _turns_after_provision(),
        task=_task(_gradle()),
        setup=setup,
        system=system,
    )

    result = engine.run_setup_loop("set up the project", max_iterations=12)

    assert result.termination is RunTerminationStatus.COMPLETED
    assert len(engine.llm_client.requests) == 4
    assert len(setup.calls) == 1
    assert system.calls == [
        {"action": "install_java", "java_version": "17", "java_distribution": "openjdk"}
    ]
    gate = next(
        row["payload"]
        for row in _events(engine, "gate_decision")
        if row["payload"]["phase"] == "provision"
    )
    assert gate["key_results"] == (
        f"Checked out {REPO} at {SHA} in {WORKSPACE}; java 17 (openjdk) installed and active, "
        "as the pinned task requires."
    )


# ---------------------------------------------------------------------------
# (e) no java_major pinned: clone only, the model turn kept with the lines
# ---------------------------------------------------------------------------


def test_a_task_without_a_java_major_clones_only_and_keeps_the_model_turn(tmp_path):
    setup, system = _Setup(), _System()
    engine = _provision_engine(
        tmp_path,
        _all_turns(),
        task=_task(_maven(java_major=None, maven_version=None)),
        setup=setup,
        system=system,
    )

    result = engine.run_setup_loop("set up the project", max_iterations=12)

    assert result.termination is RunTerminationStatus.COMPLETED
    assert len(engine.llm_client.requests) == 5
    assert len(setup.calls) == 1 and system.calls == []
    assert [envelope["intent_source"] for envelope in _project_envelopes(engine)] == ["controller"]
    assert _provisioned_block(_provision_brief(engine)) == [
        f"Provisioned: checkout {REPO} at {SHA} → {WORKSPACE} (ok)",
        CLOSING,
    ]


def test_steps_that_name_different_java_versions_keep_the_model_turn(tmp_path):
    system = _System()
    engine = _provision_engine(
        tmp_path,
        _all_turns(),
        task=_task(_maven("unit", java_major=11), _maven("it", java_major=17)),
        system=system,
    )

    engine.run_setup_loop("set up the project", max_iterations=12)

    assert len(engine.llm_client.requests) == 5
    assert system.calls[0] == {
        "action": "install_java",
        "java_version": "11",
        "java_distribution": "openjdk",
    }
    assert _provisioned_block(_provision_brief(engine)) == [
        f"Provisioned: checkout {REPO} at {SHA} → {WORKSPACE} (ok)",
        "Provisioned: java 11 (openjdk) → installed and active (ok)",
        "Provisioned: maven 3.9.16 → installed and active (ok)",
        "Provisioned: the task steps name different Java versions (11, 17); the harness "
        "handled only the first, java 11.",
        CLOSING,
    ]


def test_a_gate_that_does_not_accept_keeps_the_model_turn(tmp_path):
    engine = _provision_engine(tmp_path, _all_turns(), task=_task(), workspace_present=False)

    engine.run_setup_loop("set up the project", max_iterations=12)

    assert len(engine.llm_client.requests) == 5
    assert len(_project_envelopes(engine)) == 3
    assert _provisioned_block(_provision_brief(engine))[-1] == CLOSING


# ---------------------------------------------------------------------------
# (f) the switch turns it off
# ---------------------------------------------------------------------------


def test_the_switch_turns_the_harness_provisioning_off(tmp_path):
    setup, system = _Setup(), _System()
    engine = _provision_engine(tmp_path, _all_turns(), task=_task(), setup=setup, system=system)
    engine.config.provision_from_task = False

    engine.run_setup_loop("set up the project", max_iterations=12)

    assert len(engine.llm_client.requests) == 5
    assert setup.calls == [] and system.calls == []
    assert _project_envelopes(engine) == []
    assert "Provisioned: " not in _provision_brief(engine)


def test_the_switch_defaults_on_and_reads_its_environment_variable(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SAG_PROVISION_FROM_TASK", raising=False)
    assert Config().provision_from_task is True
    assert Config.from_env().provision_from_task is True
    monkeypatch.setenv("SAG_PROVISION_FROM_TASK", "false")
    assert Config.from_env().provision_from_task is False


# ---------------------------------------------------------------------------
# (g) one attempt per run
# ---------------------------------------------------------------------------


def test_the_harness_attempts_provisioning_once(tmp_path):
    setup, system = _Setup(error="network unreachable"), _System()
    engine = _provision_engine(tmp_path, _all_turns(), task=_task(), setup=setup, system=system)
    engine.steps = [engine._phase_intro_step()]

    assert engine._provision_from_task() is False
    assert len(setup.calls) == 1 and len(system.calls) == 2
    window = list(engine.steps)

    assert engine._provision_from_task() is False
    assert len(setup.calls) == 1 and len(system.calls) == 2
    assert engine.steps == window
    assert len(_project_envelopes(engine)) == 3


# ---------------------------------------------------------------------------
# (h) an exception restores the window and the model turn proceeds
# ---------------------------------------------------------------------------


def test_an_exception_in_the_harness_actions_restores_the_window(tmp_path):
    system = _System()
    engine = _provision_engine(tmp_path, _all_turns(), task=_task(), system=system)
    execute = engine._execute_tool_call
    raised = []

    def flaky(call):
        params = call.raw_params
        if call.name == "project" and params.get("java_version") and not raised:
            raised.append(call.action_intent.source)
            raise RuntimeError("java dispatch lost")
        return execute(call)

    engine._execute_tool_call = flaky

    result = engine.run_setup_loop("set up the project", max_iterations=12)

    assert raised == ["controller"]
    assert result.termination is RunTerminationStatus.COMPLETED
    assert len(engine.llm_client.requests) == 5
    # The window was put back: the model saw the brief and nothing else.
    request = engine.llm_client.requests[0]
    assert [message["role"] for message in request] == ["system", "user"]
    # The clone that ran stays on the record, and the brief lists what was done.
    envelopes = _project_envelopes(engine)
    assert [envelope["exact_params"]["action"] for envelope in envelopes[:1]] == ["clone"]
    assert envelopes[0]["intent_source"] == "controller"
    assert _provisioned_block(_provision_brief(engine)) == [
        f"Provisioned: checkout {REPO} at {SHA} → {WORKSPACE} (ok)",
        "Provisioned: java 17 (openjdk) → failed: java dispatch lost",
        CLOSING,
    ]
    assert system.calls == []
    assert engine._provision_from_task_attempted is True


# ---------------------------------------------------------------------------
# the brief: Provisioned lines survive the whole-intro cut
# ---------------------------------------------------------------------------


class _RecordingStorage:
    def __init__(self):
        self.calls = []

    def store_output(self, **kwargs):
        self.calls.append(kwargs)
        return "output_test123"


def test_provisioned_lines_survive_the_bounded_phase_contract():
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = PhaseMachine()
    engine.output_storage = _RecordingStorage()
    provisioned = [
        f"Provisioned: checkout {REPO} at {SHA} → failed: network unreachable",
        CLOSING,
    ]
    filler = [f"- observed line {index:03d} with a long description" for index in range(200)]
    # Placed after the filler, so only their prefix can keep them.
    contract = "\n".join(
        [
            "=== PHASE: PROVISION ===",
            f"Objective: {PHASE_OBJECTIVES['provision']}",
            *filler,
            *provisioned,
        ]
    )

    text, ref = engine._bounded_phase_contract(contract, 1200)

    assert ref == "output_test123"
    lines = text.splitlines()
    for line in provisioned:
        assert line in lines
    assert "Provisioned" not in engine.output_storage.calls[0]["output"]


def test_the_provision_brief_has_no_provisioned_lines_before_the_harness_acts(tmp_path):
    engine = _provision_engine(tmp_path, _all_turns(), task=_task())
    brief = engine._phase_intro_step().content
    assert "Provisioned: " not in brief
    assert f"Objective: {PHASE_OBJECTIVES['provision']}" in brief
