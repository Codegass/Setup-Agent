from sag.evidence import EvidenceAssessment, EvidenceFinding, TestStats, aggregate_evidence_status


def test_evidence_status_values_are_constrained():
    assert {state.value for state in EvidenceAssessment} == {
        "success",
        "partial",
        "blocked",
        "conflict",
        "unknown",
    }


def test_aggregate_evidence_status_uses_blocked_conflict_partial_precedence():
    assert aggregate_evidence_status([EvidenceAssessment.SUCCESS]) == EvidenceAssessment.SUCCESS
    assert aggregate_evidence_status([EvidenceAssessment.SUCCESS, EvidenceAssessment.PARTIAL]) == EvidenceAssessment.PARTIAL
    assert aggregate_evidence_status([EvidenceAssessment.PARTIAL, EvidenceAssessment.CONFLICT]) == EvidenceAssessment.CONFLICT
    assert aggregate_evidence_status([EvidenceAssessment.CONFLICT, EvidenceAssessment.BLOCKED]) == EvidenceAssessment.BLOCKED
    assert aggregate_evidence_status([]) == EvidenceAssessment.UNKNOWN


def test_test_stats_preserve_counts_and_percentages():
    stats = TestStats(executed=214, passed=206, failed=3, skipped=5, discovered=460)

    assert stats.pass_rate == 96.3
    assert stats.execution_rate == 46.5
    assert stats.as_summary() == "206 / 214 passed, 96.3% pass rate, 3 failed, 5 skipped"


def test_test_stats_summary_reports_detected_but_not_executed():
    # Bigtop: a static suite was discovered but the build produced no classes, so
    # nothing ran. The summary must say so, not "0 / 0 passed" (which reads as a pass).
    stats = TestStats(discovered=57, executed=0)
    summary = stats.as_summary()
    assert summary == "0 of 57 detected tests executed (no tests ran)"
    assert "0 / 0 passed" not in summary


def test_test_stats_summary_no_tests_discovered_or_executed():
    assert TestStats(executed=0).as_summary() == "no tests executed"


def test_evidence_finding_serializes_status_as_json_safe_string():
    finding = EvidenceFinding(type="validator", reason="partial pass", status=EvidenceAssessment.PARTIAL)

    assert finding.status == EvidenceAssessment.PARTIAL
    assert finding.model_dump()["status"] == "partial"
