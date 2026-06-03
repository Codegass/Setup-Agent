from rich.console import Console

from sag.ui.events import EventType, PhaseType, UIEvent
from sag.ui.ui_manager import UIManager


def make_manager():
    console = Console(record=True, width=100)
    return UIManager(project_name="commons-cli", console=console)


def test_ui_manager_updates_snapshot_when_handling_events():
    manager = make_manager()

    manager.handle_event(UIEvent(EventType.PHASE_START, "Building", phase=PhaseType.BUILD))
    manager.handle_event(
        UIEvent(
            EventType.AGENT_ACTION,
            "Using maven",
            metadata={"tool_name": "maven", "tool_params": {"goal": "compile"}},
        )
    )

    snapshot = manager.snapshot()
    assert snapshot.current_phase == PhaseType.BUILD
    assert snapshot.active_operation.tool_name == "maven"


def test_ui_manager_handles_unknown_event_without_crashing():
    manager = make_manager()
    event = UIEvent(EventType.STATUS_UPDATE, "Known")
    event.event_type = "unknown_event"

    manager.handle_event(event)

    assert manager.snapshot().latest_warning is not None


def test_ui_manager_handles_invalid_phase_without_polluting_legacy_state():
    manager = make_manager()

    event = UIEvent(EventType.PHASE_START, "bad")
    event.phase = "not-a-phase"

    manager.handle_event(event)

    assert manager.snapshot().latest_warning is not None
    assert manager.current_phase is None
    assert manager.current_status == "Initializing"


def test_ui_manager_live_update_failure_does_not_abort_event_handling():
    manager = make_manager()

    class FakeLive:
        def update(self, renderable):
            raise RuntimeError("live update exploded")

    manager.live = FakeLive()

    manager.handle_event(UIEvent(EventType.STATUS_UPDATE, "Still running"))

    assert manager.snapshot().current_status == "Still running"
    assert manager.snapshot().latest_warning is not None
    assert "render" in manager.snapshot().latest_warning.message.lower()


def test_ui_manager_does_not_render_when_live_display_is_not_started(monkeypatch):
    manager = make_manager()
    render_calls = 0

    def broken_render():
        nonlocal render_calls
        render_calls += 1
        raise RuntimeError("render should not run")

    monkeypatch.setattr(manager, "_render_display", broken_render)

    manager.handle_event(UIEvent(EventType.STATUS_UPDATE, "Still running"))
    manager.handle_event(UIEvent(EventType.STATUS_UPDATE, "Still running again"))

    assert render_calls == 0
    assert manager.snapshot().current_status == "Still running again"
    assert manager.snapshot().latest_warning is None


def test_ui_manager_handles_malformed_agent_tool_params_without_crashing_legacy_path():
    manager = make_manager()
    manager.handle_event(
        UIEvent(
            EventType.AGENT_THOUGHT,
            "I need to compile the project.",
            metadata={"step_num": 1},
        )
    )

    manager.handle_event(
        UIEvent(
            EventType.AGENT_ACTION,
            "Using maven",
            metadata={"tool_name": "maven", "tool_params": "not-dict"},
        )
    )

    assert manager.snapshot().active_operation.tool_name == "maven"
    assert manager.current_status == "Using maven"


def test_ui_manager_start_render_failure_does_not_abort_ui_mode(monkeypatch):
    manager = make_manager()

    def broken_render():
        raise RuntimeError("initial render exploded")

    monkeypatch.setattr(manager, "_render_display", broken_render)

    manager.start()

    assert manager.live is None
    assert manager.snapshot().latest_warning is not None
    assert "initial render" in manager.snapshot().latest_warning.message.lower()


def test_display_final_summary_is_idempotent_no_op_with_snapshot_diagnosis():
    manager = make_manager()
    manager.handle_event(UIEvent(EventType.PHASE_START, "Building", phase=PhaseType.BUILD))
    manager.handle_event(UIEvent(EventType.PHASE_ERROR, "Build failed", phase=PhaseType.BUILD))
    manager.handle_event(UIEvent(EventType.FAILURE, "Project setup incomplete"))

    manager.display_final_summary()
    manager.display_final_summary()
    output = manager.console.export_text()

    assert output.count("Detailed Execution Log") == 1
    assert output.count("Project setup incomplete") == 1
