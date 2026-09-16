"""Production run assembly must not retain the previous command's authority."""

from types import SimpleNamespace

import pytest
from test_verdict_finalizer import FakeVerdictOrchestrator

import sag.agent.agent as agent_module
import sag.agent.react_engine as engine_module
from sag.agent.agent import SetupAgent
from sag.agent.context_journal import ContextJournal
from sag.agent.evidence_state import RunEvidenceState
from sag.agent.phase_machine import PhaseMachine
from sag.agent.verdict_finalizer import VerdictFinalizer
from sag.config import Config


@pytest.fixture
def assembled_agent(monkeypatch):
    config = Config(workspace_path="/workspace", ui_mode=False)
    monkeypatch.setattr(engine_module, "get_config", lambda: config)
    monkeypatch.setattr(
        engine_module, "ReactLLMClient", lambda **kwargs: SimpleNamespace(setup=lambda: None)
    )
    monkeypatch.setattr(agent_module, "get_session_logger", lambda: None)
    monkeypatch.setattr(
        "sag.agent.error_logger.ErrorLogger.get_instance", lambda **kwargs: SimpleNamespace()
    )
    agent = SetupAgent(config, FakeVerdictOrchestrator())
    return agent


def prepare_setup(agent, run_id):
    agent.run_id = run_id
    agent.project_name = run_id
    agent.phase_machine = PhaseMachine()
    agent.context_journal = ContextJournal(agent.orchestrator)
    agent.run_evidence_state = RunEvidenceState(run_id=run_id)
    agent.verdict_finalizer = VerdictFinalizer(agent.orchestrator)
    agent._initialize_context_and_tools("setup")


def assert_current_setup_graph(agent):
    engine = agent.react_engine
    phase = engine.tools["phase"]
    assert engine.run_evidence_state is agent.run_evidence_state
    assert phase.run_evidence_state is agent.run_evidence_state
    assert engine.phase_machine is agent.phase_machine is phase.machine
    assert engine.verdict_finalizer is agent.verdict_finalizer
    assert agent.verdict_finalizer.validator is agent.physical_validator
    assert engine.physical_validator is phase.validator is agent.physical_validator
    assert engine.context_manager.physical_validator is agent.physical_validator
    assert agent.report_tool.physical_validator is agent.physical_validator
    assert agent.physical_validator.receipt_run_id == agent.run_id
    assert agent.verdict_finalizer.project_name == agent.project_name


def test_second_setup_reassembles_all_run_consumers(assembled_agent):
    agent = assembled_agent
    prepare_setup(agent, "first-run")
    previous = (
        agent.react_engine,
        agent.physical_validator,
        agent.context_manager,
        agent.report_tool,
    )
    prepare_setup(agent, "second-run")
    assert_current_setup_graph(agent)
    assert all(
        current is not old
        for current, old in zip(
            (
                agent.react_engine,
                agent.physical_validator,
                agent.context_manager,
                agent.report_tool,
            ),
            previous,
        )
    )


@pytest.mark.parametrize("mode", ["run_task", "legacy"])
def test_setup_to_legacy_rebuilds_surface_and_discards_setup_state(assembled_agent, mode):
    agent = assembled_agent
    prepare_setup(agent, "setup-run")
    old_engine = agent.react_engine
    agent.run_id = "next-command"
    agent.project_name = "next-project"
    agent.pre_finalize_evidence_callback = lambda: {"old": True}
    agent._observed_target_repo_sha = "a" * 40
    agent._run_pin_template = {"run_id": "setup-run"}
    agent._setup_ci_target = object()
    agent._bootstrap_continuation_overlay = lambda workflow_mode: None
    agent._initialize_context_and_tools(mode)
    assert agent.react_engine is not old_engine
    assert agent.react_engine.phase_machine is None
    assert agent.react_engine.run_evidence_state is None
    assert agent.react_engine.verdict_finalizer is None
    assert agent.react_engine.context_journal is None
    assert "phase" not in agent.react_engine.tools
    assert "manage_context" in agent.react_engine.tools
    assert agent.report_tool.workflow_mode == "legacy"
    assert agent.react_engine.pre_finalize_evidence_callback is None
    assert agent.react_engine.physical_validator is agent.physical_validator
    assert agent.physical_validator.receipt_run_id == "next-command"
    assert agent._run_pin_template is None
    assert agent._observed_target_repo_sha is None
    assert agent._setup_ci_target is None


def test_two_public_setup_commands_bind_the_new_run(assembled_agent, monkeypatch):
    from sag.agent.verdict_finalizer import RunTermination, RunTerminationStatus

    agent = assembled_agent
    monkeypatch.setattr(agent, "_setup_docker_environment", lambda name: True)
    monkeypatch.setattr(
        agent_module.ContextManager,
        "create_trunk_context",
        lambda self, **kwargs: SimpleNamespace(context_id="test-context"),
    )
    monkeypatch.setattr(agent_module.ContextManager, "get_current_context_info", lambda self: {})
    monkeypatch.setattr(agent, "_save_project_metadata", lambda **kwargs: True)
    completed = RunTermination(
        termination=RunTerminationStatus.COMPLETED, report_delivery_status="skipped"
    )
    monkeypatch.setattr(agent, "_run_unified_setup", lambda *args, **kwargs: completed)
    for project in ("first-project", "second-project"):
        termination = agent.setup_project("https://example.invalid/repo", project, "build")
        assert termination == completed
        assert_current_setup_graph(agent)
        assert agent.project_name == project
        assert agent.react_engine.pre_finalize_evidence_callback is None


def test_second_setup_startup_failure_cannot_abort_previous_engine(assembled_agent, monkeypatch):
    from sag.agent.verdict_finalizer import RunTerminationStatus

    agent = assembled_agent
    prepare_setup(agent, "previous-run")
    previous_engine = agent.react_engine
    previous_state = agent.run_evidence_state
    aborted = []
    monkeypatch.setattr(previous_engine, "abort", lambda **kwargs: aborted.append(kwargs))
    monkeypatch.setattr(agent, "_setup_docker_environment", lambda name: False)
    result = agent.setup_project("https://example.invalid/repo", "next-project", "build")
    assert result.termination is RunTerminationStatus.ABORTED
    assert aborted == []
    assert previous_state.sealed is False
    assert agent.run_evidence_state is None
    assert agent.react_engine is None


def test_reassembly_preserves_container_history_files(assembled_agent):
    agent = assembled_agent
    prepare_setup(agent, "first-run")
    historical = "/workspace/.setup_agent/contexts/trunk_previous.json"
    agent.orchestrator.files[historical] = '{"project":"previous"}'
    prepare_setup(agent, "second-run")
    assert agent.orchestrator.files[historical] == '{"project":"previous"}'


@pytest.mark.parametrize("command", ["setup", "continue", "task"])
def test_every_command_retires_previous_authority_before_container_startup(
    assembled_agent, monkeypatch, command
):
    from sag.agent.evidence_publications import current_evidence_publication_authority
    from sag.agent.invocation_receipts import active_receipt_run_id

    agent = assembled_agent
    prepare_setup(agent, "previous-run")
    previous_engine = agent.react_engine
    previous_state = agent.run_evidence_state
    agent.final_verdict = "success"
    agent.final_verdict_reason = "previous result"
    agent._last_test_status = {"success": True}
    agent._last_build_status = {"success": True}
    agent._run_pin_template = {"run_id": "previous-run"}
    agent._observed_target_repo_sha = "a" * 40
    seen = []

    attributes = (
        "react_engine",
        "tools",
        "context_manager",
        "physical_validator",
        "report_tool",
        "command_tracker",
        "phase_machine",
        "context_journal",
        "run_evidence_state",
        "verdict_finalizer",
        "control_event_sink",
        "_run_pin_template",
        "_run_pin_host_path",
        "_run_pin_mirror",
        "_observed_target_repo_sha",
        "final_verdict",
        "_last_test_status",
        "_last_build_status",
    )

    def startup(_project_name):
        seen.append(
            {
                "run_id": agent.run_id,
                "project_name": agent.project_name,
                "receipt_run_id": active_receipt_run_id(),
                "authority": current_evidence_publication_authority(agent.orchestrator),
                "components": {attr: getattr(agent, attr, None) for attr in attributes},
            }
        )
        return False

    monkeypatch.setattr(agent, "_setup_docker_environment", startup)
    monkeypatch.setattr(agent, "_ensure_container_running", startup)
    if command == "setup":
        agent.setup_project("https://example.invalid/repo", "next-project", "build")
    elif command == "continue":
        assert agent.continue_project("next-project") is False
    else:
        assert agent.run_task("next-project", "test") is False
    assert len(seen) == 1
    observed = seen[0]
    assert observed["project_name"] == "next-project"
    assert observed["run_id"] != "previous-run"
    assert observed["receipt_run_id"] == observed["run_id"]
    assert not observed["authority"].available
    assert observed["authority"].run_id == observed["run_id"]
    assert all(value is None for value in observed["components"].values()), observed["components"]
    assert not previous_state.sealed
    assert previous_engine.run_evidence_state is previous_state


@pytest.mark.parametrize("next_run", ["previous-run", "new-command"])
def test_reassembly_recovers_only_its_own_repair_authority(
    assembled_agent, monkeypatch, tmp_path, next_run
):
    from test_repair_lineage_foundation import _strict_events, _v3_repair_rows

    from sag.agent.control_events import ControlEventSink
    from sag.agent.repair_contexts import repair_context_sha256

    rows = _v3_repair_rows()
    opened = next(i for i, row in enumerate(rows) if row.get("kind") == "repair_context_opened")
    context_payload = rows[opened]["payload"]["context"]
    context_payload["fingerprints"] = {"target_sha": "a" * 40}
    rows[opened]["payload"]["context_sha256"] = repair_context_sha256(context_payload)
    sink = ControlEventSink(tmp_path / "control_events.jsonl", run_id="previous-run")
    for event in _strict_events(rows[: opened + 1]):
        sink.emit(event.kind, event.payload, source=event.source)
    session = SimpleNamespace(
        session_id="same-log-session",
        run_pin_path=tmp_path / "run-pin.json",
        get_control_event_sink=lambda **kwargs: sink,
    )
    monkeypatch.setattr(agent_module, "get_session_logger", lambda: session)

    prepare_setup(assembled_agent, next_run)

    assert_current_setup_graph(assembled_agent)
    assert assembled_agent.control_event_sink.run_id == next_run
    pending = assembled_agent.react_engine._pending_repair_context
    if next_run == "previous-run":
        assert pending is not None
        assert pending.repair_context_id == context_payload["repair_context_id"]
    else:
        assert pending is None
