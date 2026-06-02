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


def test_aggregator_tracks_phase_completion_status_and_timeline():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)

    aggregator.handle(UIEvent(EventType.PHASE_START, "Building", phase=PhaseType.BUILD))
    state = aggregator.handle(
        UIEvent(EventType.PHASE_COMPLETE, "Build complete", phase=PhaseType.BUILD)
    )

    build_phase = next(phase for phase in state.phases if phase.phase == PhaseType.BUILD)
    assert build_phase.status == "success"
    assert state.current_status == "Build complete"
    assert state.timeline[-1].kind == "phase"
    assert state.timeline[-1].message == "Build complete"


def test_phase_error_uses_fixed_classification_over_conflicting_metadata():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)

    verification_state = aggregator.handle(
        UIEvent(
            EventType.PHASE_ERROR,
            "Verification timed out",
            phase=PhaseType.VERIFICATION,
            level="error",
            metadata={"failure_type": "custom_failure", "error_code": "timeout"},
        )
    )
    assert verification_state.latest_error.failure_classification == "verification_failure"

    setup_state = aggregator.handle(
        UIEvent(
            EventType.PHASE_ERROR,
            "Setup timed out",
            phase=PhaseType.SETUP,
            level="error",
            metadata={"failure_type": "custom_failure", "error_code": "timeout"},
        )
    )
    assert setup_state.latest_error.failure_classification == "tool_failure"


def test_step_error_marks_running_step_and_records_latest_error():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)

    aggregator.handle(UIEvent(EventType.STEP_START, "Install dependencies", phase=PhaseType.BUILD))
    state = aggregator.handle(
        UIEvent(
            EventType.STEP_ERROR,
            "Install dependencies",
            phase=PhaseType.BUILD,
            details="maven failed",
            level="error",
        )
    )

    build_phase = next(phase for phase in state.phases if phase.phase == PhaseType.BUILD)
    assert build_phase.steps[-1]["status"] == "error"
    assert build_phase.steps[-1]["details"] == "maven failed"
    assert state.latest_error.message == "Install dependencies"
    assert state.timeline[-1].kind == "error"


def test_status_update_updates_current_status_and_timeline():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)

    state = aggregator.handle(UIEvent(EventType.STATUS_UPDATE, "Waiting for container"))

    assert state.current_status == "Waiting for container"
    assert state.timeline[-1].kind == "status"
    assert state.timeline[-1].message == "Waiting for container"


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


def test_agent_observation_updates_active_operation_detail_current_status_and_timeline():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)

    aggregator.handle(
        UIEvent(
            EventType.AGENT_ACTION,
            "Using bash",
            metadata={
                "tool_name": "bash",
                "tool_params": {"command": "mvn test", "working_directory": "/workspace/app"},
            },
        )
    )
    state = aggregator.handle(
        UIEvent(
            EventType.AGENT_OBSERVATION,
            "Build successfully completed\nTests are ready",
        )
    )

    assert state.active_operation.detail == "Build successfully completed"
    assert state.current_status == "Build successfully completed"
    assert state.timeline[-1].kind == "observation"


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


def test_success_marks_complete_final_status_success_and_completion_timeline():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)

    state = aggregator.handle(UIEvent(EventType.SUCCESS, "Project setup complete"))

    assert state.is_complete is True
    assert state.final_status == "success"
    assert state.current_status == "Project setup complete"
    assert state.timeline[-1].kind == "completion"
    assert state.timeline[-1].failure_classification is None


def test_warning_and_failure_use_fixed_classification_over_conflicting_metadata():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)

    warning_state = aggregator.handle(
        UIEvent(
            EventType.WARNING,
            "Retrying timeout",
            level="warning",
            metadata={"failure_type": "custom_failure", "error_code": "timeout"},
        )
    )
    assert warning_state.latest_warning.failure_classification == "warning"

    failure_state = aggregator.handle(
        UIEvent(
            EventType.FAILURE,
            "Project setup incomplete after timeout",
            level="error",
            metadata={"failure_type": "custom_failure", "error_code": "timeout"},
        )
    )
    assert failure_state.timeline[-1].failure_classification == "final_failure"


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


def test_error_level_validation_uses_fixed_classification_over_conflicting_metadata():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)

    validation_state = aggregator.handle(
        UIEvent(
            EventType.VALIDATION_CHECK,
            "Validation timeout",
            phase=PhaseType.VERIFICATION,
            level="error",
            metadata={
                "summary": "check failed",
                "failure_type": "custom_failure",
                "error_code": "timeout",
            },
        )
    )

    assert validation_state.latest_error.failure_classification == "verification_failure"


def test_report_data_and_evidence_metadata_are_defensive_copies():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)
    metadata = {
        "report_path": "reports/setup.md",
        "status": "success",
        "nested": {"result": "stable"},
    }

    state = aggregator.handle(
        UIEvent(EventType.REPORT_GENERATED, "Report generated", metadata=metadata)
    )
    metadata["nested"]["result"] = "mutated"
    metadata["report_path"] = "reports/changed.md"

    assert state.report_data["report_path"] == "reports/setup.md"
    assert state.report_data["nested"]["result"] == "stable"
    assert state.evidence[-1].metadata["report_path"] == "reports/setup.md"
    assert state.evidence[-1].metadata["nested"]["result"] == "stable"


def test_aggregator_degrades_unknown_event_to_warning_timeline_entry():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)
    event = UIEvent(EventType.STATUS_UPDATE, "Known status")
    event.event_type = "unknown_event"

    state = aggregator.handle(event)

    assert state.latest_warning is not None
    assert "unknown_event" in state.latest_warning.message
    assert state.current_status == "Initializing"
