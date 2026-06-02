from datetime import datetime, timezone

from sag.ui.events import EventType, PhaseType, UIEvent
from sag.ui.state_aggregator import UIStateAggregator


def fixed_now():
    return datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)


def test_aggregator_tracks_phase_step_and_status_events():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)

    state = aggregator.handle(UIEvent(EventType.PHASE_START, "Setting up", phase=PhaseType.SETUP))
    assert state.current_phase == PhaseType.SETUP
    assert state.current_status == "Setting up"
    assert state.phases[0].status == "running"

    state = aggregator.handle(
        UIEvent(
            EventType.STEP_START,
            "Create container",
            phase=PhaseType.SETUP,
            details="docker",
        )
    )
    assert state.phases[0].steps[-1]["name"] == "Create container"
    assert state.phases[0].steps[-1]["status"] == "running"

    state = aggregator.handle(
        UIEvent(EventType.STEP_COMPLETE, "Create container", phase=PhaseType.SETUP)
    )
    assert state.phases[0].steps[-1]["status"] == "success"


def test_aggregator_tracks_agent_action_as_active_operation():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)

    aggregator.handle(
        UIEvent(
            EventType.AGENT_THOUGHT,
            "I need to compile the Maven project before testing.",
            metadata={"step_num": 4},
        )
    )
    state = aggregator.handle(
        UIEvent(
            EventType.AGENT_ACTION,
            "Using maven",
            metadata={
                "step_num": 4,
                "tool_name": "maven",
                "tool_params": {
                    "goal": "compile",
                    "working_directory": "/workspace/app",
                },
            },
        )
    )

    assert state.current_phase == PhaseType.BUILD
    assert state.active_operation.tool_name == "maven"
    assert state.active_operation.action == "goal='compile'"
    assert "/workspace/app" in state.active_operation.workdir
    assert state.current_status.startswith("Using maven")


def test_aggregator_records_errors_warnings_completion_and_reports():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)

    warning_state = aggregator.handle(UIEvent(EventType.WARNING, "Retrying", level="warning"))
    assert warning_state.latest_warning.message == "Retrying"

    error_state = aggregator.handle(UIEvent(EventType.ERROR, "Build failed", level="error"))
    assert error_state.latest_error.message == "Build failed"

    report_state = aggregator.handle(
        UIEvent(
            EventType.REPORT_GENERATED,
            "Report generated",
            metadata={"report_path": "reports/setup.md", "status": "failure"},
        )
    )
    assert report_state.report_data["report_path"] == "reports/setup.md"
    assert report_state.evidence[-1].kind == "report"

    final_state = aggregator.handle(UIEvent(EventType.FAILURE, "Project setup incomplete"))
    assert final_state.is_complete is True
    assert final_state.final_status == "failure"
    assert final_state.timeline[-1].failure_classification == "final_failure"


def test_aggregator_records_validation_evidence_and_failure_classification():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)

    validation_state = aggregator.handle(
        UIEvent(
            EventType.VALIDATION_COMPLETE,
            "Validation failed",
            phase=PhaseType.VERIFICATION,
            level="error",
            metadata={"summary": "2 checks failed", "path": "reports/validation.json"},
        )
    )

    assert validation_state.evidence[-1].kind == "validation"
    assert validation_state.evidence[-1].summary == "2 checks failed"
    assert validation_state.latest_error.failure_classification == "verification_failure"


def test_aggregator_degrades_unknown_event_to_warning_timeline_entry():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)
    event = UIEvent(EventType.STATUS_UPDATE, "Known status")
    event.event_type = "unknown_event"

    state = aggregator.handle(event)

    assert state.latest_warning is not None
    assert "unknown_event" in state.latest_warning.message
    assert state.current_status == "Initializing"
