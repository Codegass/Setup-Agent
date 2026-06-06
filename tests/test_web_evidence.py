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
    assert groups[1].source == "Test validator"
    assert groups[1].records[0].ref == "target/surefire-reports"


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
    assert groups[0].summary == "3 tests failed"
    assert groups[0].records[0].ref == "logs/gradle.log"
    assert groups[0].records[1].ref == "runtime"


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
