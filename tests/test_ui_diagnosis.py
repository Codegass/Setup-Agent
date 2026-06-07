from dataclasses import replace
from datetime import datetime, timezone

from sag.ui.diagnosis import build_final_diagnosis
from sag.ui.events import EventType, PhaseType, UIEvent
from sag.ui.state import initial_run_state
from sag.ui.state_aggregator import UIStateAggregator


def fixed_now():
    return datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)


def test_success_diagnosis_summarizes_completed_phases_and_evidence():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)
    aggregator.handle(UIEvent(EventType.PHASE_START, "Build", phase=PhaseType.BUILD))
    aggregator.handle(UIEvent(EventType.PHASE_COMPLETE, "Build passed", phase=PhaseType.BUILD))
    aggregator.handle(
        UIEvent(
            EventType.REPORT_GENERATED,
            "Report generated",
            metadata={"report_path": "reports/setup.md", "status": "success"},
        )
    )
    state = aggregator.handle(UIEvent(EventType.SUCCESS, "Project setup completed"))

    diagnosis = build_final_diagnosis(state)

    assert diagnosis.status == "success"
    assert "Build" in diagnosis.outcome
    assert any("reports/setup.md" in item for item in diagnosis.evidence)
    assert diagnosis.next_actions == ()


def test_failure_diagnosis_includes_error_recovery_and_next_action():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)
    aggregator.handle(UIEvent(EventType.PHASE_START, "Build", phase=PhaseType.BUILD))
    aggregator.handle(
        UIEvent(
            EventType.WARNING,
            "Retrying with fallback",
            level="warning",
            metadata={"recovery_strategy": "fallback"},
        )
    )
    aggregator.handle(
        UIEvent(
            EventType.ERROR,
            "Maven compile failed",
            phase=PhaseType.BUILD,
            details="Missing dependency",
            level="error",
        )
    )
    state = aggregator.handle(UIEvent(EventType.FAILURE, "Project setup incomplete"))

    diagnosis = build_final_diagnosis(state)

    assert diagnosis.status == "failure"
    assert any("Maven compile failed" in item for item in diagnosis.failures)
    assert any("Retrying with fallback" in item for item in diagnosis.recovery)
    assert "tool_failure" in diagnosis.failure_classifications
    assert "warning" in diagnosis.failure_classifications
    assert diagnosis.next_actions


def test_diagnosis_surfaces_tool_recovery_fallback_decision():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)
    aggregator.handle(
        UIEvent(
            EventType.TOOL_RECOVERY,
            "Retrying maven in discovered project directory",
            level="warning",
            metadata={
                "recovery_strategy": "maven_known_working_directory",
                "guidance": "Retrying in discovered project directory",
                "recovery_params": {
                    "goal": "compile",
                    "working_directory": "/workspace/app",
                },
                "parameter_diff": {"working_directory": [None, "/workspace/app"]},
                "recovery": {"attempted": True, "success": True},
            },
        )
    )
    state = aggregator.handle(UIEvent(EventType.FAILURE, "Project setup incomplete"))

    diagnosis = build_final_diagnosis(state)

    assert any("maven_known_working_directory" in item for item in diagnosis.recovery)
    assert any("/workspace/app" in item for item in diagnosis.recovery)
    assert any("working_directory" in item for item in diagnosis.recovery)
    assert any("success" in item for item in diagnosis.recovery)


def test_complete_state_without_final_status_is_unknown_with_next_action():
    state = replace(
        initial_run_state("commons-cli", fixed_now()),
        is_complete=True,
        current_status="Finished without final status",
    )

    diagnosis = build_final_diagnosis(state)

    assert diagnosis.status == "unknown"
    assert diagnosis.next_actions


def test_recovery_only_includes_explicit_recovery_signals():
    aggregator = UIStateAggregator("commons-cli", clock=fixed_now)
    aggregator.handle(UIEvent(EventType.WARNING, "Disk space is low", level="warning"))
    aggregator.handle(
        UIEvent(
            EventType.WARNING,
            "Retrying with fallback",
            level="warning",
            metadata={"recovery_strategy": "fallback"},
        )
    )
    aggregator.handle(
        UIEvent(
            EventType.ERROR,
            "Fallback retry failed",
            level="error",
            metadata={"retry": True},
        )
    )
    state = aggregator.handle(UIEvent(EventType.FAILURE, "Project setup incomplete"))

    diagnosis = build_final_diagnosis(state)

    assert all("Disk space is low" not in item for item in diagnosis.recovery)
    assert any("Retrying with fallback" in item for item in diagnosis.recovery)
    assert any("Fallback retry failed" in item for item in diagnosis.recovery)
