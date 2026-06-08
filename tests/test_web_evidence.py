from datetime import datetime, timezone

from sag.ui.state import UIEvidenceRecord
from sag.web.evidence import EvidenceIndex


def test_evidence_index_groups_runtime_records_by_source():
    records = [
        UIEvidenceRecord(
            timestamp=datetime(2026, 6, 6, 2, 13, tzinfo=timezone.utc),
            kind="command",
            summary="maven clean package passed",
            metadata={"tool_name": "maven", "status": "success", "ref": "logs/maven.log"},
        ),
        UIEvidenceRecord(
            timestamp=datetime(2026, 6, 6, 2, 16, tzinfo=timezone.utc),
            kind="validation",
            summary="312/320 tests passed",
            metadata={
                "source": "Test validator",
                "status": "partial",
                "ref": "target/surefire-reports",
            },
        ),
    ]

    groups = EvidenceIndex().from_ui_records(records)

    assert groups[0].source == "Build tool · Maven"
    assert groups[0].status == "success"
    assert groups[0].records[0].ref == "logs/maven.log"
    assert groups[0].records[0].title == "Command"
    assert groups[0].records[0].detail == "maven clean package passed"
    assert groups[1].source == "Test validator"
    assert groups[1].status == "partial"
    assert groups[1].records[0].ref == "target/surefire-reports"
    assert groups[1].records[0].title == "Validation"
    assert groups[1].records[0].detail == "312/320 tests passed"


def test_evidence_index_preserves_source_order_and_merges_status_severity():
    records = [
        UIEvidenceRecord(
            timestamp=datetime(2026, 6, 6, 2, 13, tzinfo=timezone.utc),
            kind="command",
            summary="gradle test started",
            details="Running Gradle verification",
            metadata={"tool_name": "gradle", "status": "info", "output_ref": "logs/gradle.log"},
        ),
        UIEvidenceRecord(
            timestamp=datetime(2026, 6, 6, 2, 14, tzinfo=timezone.utc),
            kind="command",
            summary="gradle test failed",
            details="3 tests failed",
            metadata={"tool_name": "gradle", "status": "failed"},
        ),
        UIEvidenceRecord(
            timestamp=datetime(2026, 6, 6, 2, 15, tzinfo=timezone.utc),
            kind="validation",
            summary="validator observed partial results",
            metadata={"source": "Test validator", "status": "partial"},
        ),
    ]

    groups = EvidenceIndex().from_ui_records(records)

    assert [group.source for group in groups] == ["Build tool · Gradle", "Test validator"]
    assert groups[0].status == "failure"
    assert groups[0].counts == "2 records"
    assert groups[0].time == "02:14"
    assert groups[0].summary == "gradle test failed"
    assert groups[0].records[0].ref == "logs/gradle.log"
    assert groups[0].records[0].detail == "gradle test started"
    assert groups[0].records[1].ref == "runtime"
    assert groups[0].records[1].detail == "gradle test failed"


def test_evidence_index_normalizes_completed_command_status_to_success():
    records = [
        UIEvidenceRecord(
            timestamp=datetime(2026, 6, 6, 2, 13, tzinfo=timezone.utc),
            kind="command",
            summary="maven test completed",
            metadata={"tool_name": "maven", "status": "completed"},
        ),
    ]

    groups = EvidenceIndex().from_ui_records(records)

    assert groups[0].source == "Build tool · Maven"
    assert groups[0].status == "success"
    assert groups[0].records[0].status == "success"


def test_evidence_index_prefers_evidence_status_metadata_and_supports_severe_states():
    records = [
        UIEvidenceRecord(
            timestamp=datetime(2026, 6, 6, 2, 13, tzinfo=timezone.utc),
            kind="validation",
            summary="validator could not prove setup",
            metadata={
                "source": "Verifier",
                "status": "completed",
                "evidence_status": "blocked",
            },
        ),
        UIEvidenceRecord(
            timestamp=datetime(2026, 6, 6, 2, 14, tzinfo=timezone.utc),
            kind="validation",
            summary="runtime facts conflict",
            metadata={
                "source": "Verifier",
                "evidenceStatus": "conflict",
            },
        ),
        UIEvidenceRecord(
            timestamp=datetime(2026, 6, 6, 2, 15, tzinfo=timezone.utc),
            kind="analysis",
            summary="status is not known yet",
            metadata={
                "source": "Analyzer",
                "evidenceStatus": "unknown",
            },
        ),
    ]

    groups = EvidenceIndex().from_ui_records(records)

    assert groups[0].status == "blocked"
    assert [record.status for record in groups[0].records] == ["blocked", "conflict"]
    assert groups[1].status == "unknown"
    assert groups[1].records[0].status == "unknown"


def test_evidence_index_handles_empty_or_unexpected_metadata():
    records = [
        UIEvidenceRecord(
            timestamp=datetime(2026, 6, 6, 2, 13, tzinfo=timezone.utc),
            kind="observation",
            summary="runtime verifier emitted evidence",
            details=None,
            metadata=None,  # type: ignore[arg-type]
        ),
        UIEvidenceRecord(
            timestamp=datetime(2026, 6, 6, 2, 14, tzinfo=timezone.utc),
            kind="custom tool",
            summary="tool output captured",
            metadata={"tool_name": "custom runner"},
        ),
    ]

    groups = EvidenceIndex().from_ui_records(records)

    assert groups[0].source == "Observation"
    assert groups[0].status == "info"
    assert groups[0].summary == "runtime verifier emitted evidence"
    assert groups[0].records[0].detail == "runtime verifier emitted evidence"
    assert groups[1].source == "Custom Runner"
