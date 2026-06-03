from datetime import datetime, timezone

from sag.ui.diagnosis import build_final_diagnosis
from sag.ui.events import EventType, PhaseType, UIEvent
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
    aggregator.handle(UIEvent(EventType.WARNING, "Retrying with fallback", level="warning"))
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
