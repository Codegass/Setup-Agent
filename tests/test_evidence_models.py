from sag.evidence import EvidenceFinding, EvidenceStatus, TestStats, aggregate_evidence_status


def test_evidence_status_values_are_constrained():
    assert {state.value for state in EvidenceStatus} == {
        "success",
        "partial",
        "blocked",
        "conflict",
        "unknown",
    }


def test_aggregate_evidence_status_uses_blocked_conflict_partial_precedence():
    assert aggregate_evidence_status([EvidenceStatus.SUCCESS]) == EvidenceStatus.SUCCESS
    assert aggregate_evidence_status([EvidenceStatus.SUCCESS, EvidenceStatus.PARTIAL]) == EvidenceStatus.PARTIAL
    assert aggregate_evidence_status([EvidenceStatus.PARTIAL, EvidenceStatus.CONFLICT]) == EvidenceStatus.CONFLICT
    assert aggregate_evidence_status([EvidenceStatus.CONFLICT, EvidenceStatus.BLOCKED]) == EvidenceStatus.BLOCKED
    assert aggregate_evidence_status([]) == EvidenceStatus.UNKNOWN


def test_test_stats_preserve_counts_and_percentages():
    stats = TestStats(executed=214, passed=206, failed=3, skipped=5, discovered=460)

    assert stats.pass_rate == 96.3
    assert stats.execution_rate == 46.5
    assert stats.as_summary() == "206 / 214 passed, 96.3% pass rate, 3 failed, 5 skipped"


def test_evidence_finding_serializes_status_as_json_safe_string():
    finding = EvidenceFinding(type="validator", reason="partial pass", status=EvidenceStatus.PARTIAL)

    assert finding.status == EvidenceStatus.PARTIAL
    assert finding.model_dump()["status"] == "partial"
