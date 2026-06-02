from datetime import datetime, timezone

from sag.ui.events import PhaseType
from sag.ui.state import (
    ActiveOperation,
    RecoverySnapshot,
    UIEvidenceRecord,
    UIRunState,
    UITimelineEntry,
    initial_run_state,
)


def test_initial_run_state_has_all_phases_pending():
    state = initial_run_state(
        project_name="commons-cli",
        start_time=datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc),
    )

    assert state.project_name == "commons-cli"
    assert state.current_phase is None
    assert [phase.phase for phase in state.phases] == [
        PhaseType.SETUP,
        PhaseType.BUILD,
        PhaseType.TEST,
        PhaseType.VERIFICATION,
    ]
    assert [phase.status for phase in state.phases] == ["pending"] * 4
    assert state.timeline == ()
    assert state.evidence == ()


def test_run_state_is_a_read_model_with_tuple_history():
    entry = UITimelineEntry(
        timestamp=datetime(2026, 6, 2, 12, 1, tzinfo=timezone.utc),
        kind="tool",
        message="maven compile",
        level="info",
    )
    evidence = UIEvidenceRecord(
        timestamp=datetime(2026, 6, 2, 12, 2, tzinfo=timezone.utc),
        kind="command",
        summary="maven compile passed",
    )
    state = UIRunState(
        project_name="commons-cli",
        start_time=datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc),
        current_phase=PhaseType.BUILD,
        phases=initial_run_state(
            "commons-cli", datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
        ).phases,
        current_status="Using maven",
        active_operation=ActiveOperation(tool_name="maven", action="compile"),
        recovery=RecoverySnapshot(active=False),
        timeline=(entry,),
        evidence=(evidence,),
    )

    assert state.timeline[0].message == "maven compile"
    assert state.evidence[0].summary == "maven compile passed"
    assert state.active_operation.tool_name == "maven"


def test_timeline_entries_can_carry_failure_classification():
    entry = UITimelineEntry(
        timestamp=datetime(2026, 6, 2, 12, 3, tzinfo=timezone.utc),
        kind="error",
        message="Command timed out",
        level="error",
        failure_classification="command_timeout",
    )

    assert entry.failure_classification == "command_timeout"
