# tests/test_cli_report_verdict_mirror.py
"""The run-level (CLI) verdict must mirror the report snapshot's capped verdict.

Live-run 2026-06-24 (cayenne probe) divergence: the generated report read
"SETUP COMPLETED: ⚠️ PARTIAL" with Conflicts: reactor_scope_narrowed (tests ran
in only 2 of the test-bearing modules), but the run ended with
"Project setup completed: success=True, verdict=success".

Root cause: conflicts emitted at the REPORT-SNAPSHOT stage (report_tool's
_build_report_snapshot appends reactor_scope_narrowed / tests_not_fully_executed
/ build_modules_incomplete next to the stored kernel verdict) never flow back to
the agent's finalization — _get_verified_final_status only sees the conflicts
validate_test_status produced, so run_verdict() got an empty conflict list and
said success while the report said partial.

Fix under test: _get_verified_final_status consumes the SAME conflict-capped
kernel verdict the snapshot stored (react_engine.successful_states
["report_snapshot"]["status"]["verdict"]) as a cap — mirroring the PR #9
pattern ("green build with 0 executed tests is PARTIAL on the CLI, not
FAILED"). The mirror caps at partial only (conflict caps are partial-level by
kernel design); it never promotes, and runs that never generated a report keep
today's behavior byte-for-byte.
"""

from types import SimpleNamespace

from sag.agent.physical_validator import evaluate_run_verdict

from test_agent_final_status import FakePhysicalValidator, _agent_with_validator


def _green_validator(pass_rate=99.3, total=286, passed=284):
    """Build green + tests above threshold -> the raw run verdict is success."""
    return FakePhysicalValidator(
        build_status={
            "success": True,
            "build_complete": True,
            "reason": "Build fingerprints found",
        },
        test_status={
            "has_test_reports": True,
            "status": "OK",
            "reason": "tests ran",
            "pass_rate": pass_rate,
            "total_tests": total,
            "passed_tests": passed,
            "failed_tests": total - passed,
            "error_tests": 0,
            "skipped_tests": 0,
            "test_exclusions": [],
            "modules_without_tests": [],
        },
        analysis_status={
            "analyzed": True,
            "has_static_test_count": True,
            "static_test_count": total,
        },
    )


def _attach_report_snapshot(agent, verdict, conflicts=()):
    """Simulate a generated report: react_engine stored the snapshot whose
    status.verdict is the kernel verdict WITH all report-stage conflict caps."""
    agent.react_engine = SimpleNamespace(
        successful_states={
            "report_snapshot": {
                "status": {"verdict": verdict},
                "evidence_result": {"conflicts": list(conflicts)},
            }
        }
    )
    return agent


# ---------------------------------------------------------------------------
# 1. LIVE-RUN REPRODUCTION (cayenne): snapshot partial via reactor_scope_narrowed
#    while the raw policy says success -> the finalized run verdict is partial.
# ---------------------------------------------------------------------------


def test_snapshot_partial_via_reactor_scope_narrowed_caps_cli_verdict():
    # Premise: the raw single-policy verdict for this run is SUCCESS —
    # the divergence is real, not an artifact of the fixture.
    assert evaluate_run_verdict(True, 99.3) == "success"

    agent = _attach_report_snapshot(
        _agent_with_validator(_green_validator(pass_rate=99.3)),
        verdict="partial",
        conflicts=["reactor_scope_narrowed"],
    )

    ok = agent._get_verified_final_status(react_engine_success=True)

    assert ok is True  # flow-control boolean keeps its pre-mirror behavior
    assert agent.final_verdict == "partial"  # was "success" before the mirror


def test_snapshot_cap_reason_names_the_report_conflicts():
    agent = _attach_report_snapshot(
        _agent_with_validator(_green_validator()),
        verdict="partial",
        conflicts=["reactor_scope_narrowed"],
    )

    agent._get_verified_final_status(react_engine_success=True)

    assert "reactor_scope_narrowed" in agent.final_verdict_reason


# ---------------------------------------------------------------------------
# 2. No-report runs keep today's behavior exactly.
# ---------------------------------------------------------------------------


def test_no_report_snapshot_keeps_success():
    """No react_engine at all (sag run --task, legacy): behavior unchanged."""
    agent = _agent_with_validator(_green_validator())

    ok = agent._get_verified_final_status(react_engine_success=True)

    assert ok is True
    assert agent.final_verdict == "success"


def test_empty_report_snapshot_keeps_success():
    """Engine present but no report was ever generated (successful_states
    initializes report_snapshot to None): behavior unchanged."""
    agent = _agent_with_validator(_green_validator())
    agent.react_engine = SimpleNamespace(
        successful_states={"report_snapshot": None}
    )

    ok = agent._get_verified_final_status(react_engine_success=True)

    assert ok is True
    assert agent.final_verdict == "success"


# ---------------------------------------------------------------------------
# 3. The mirror is a cap, never a promotion.
# ---------------------------------------------------------------------------


def test_failed_stays_failed_despite_snapshot_partial():
    """A physically failed run must not be promoted by a rosier snapshot."""
    agent = _attach_report_snapshot(
        _agent_with_validator(
            FakePhysicalValidator(
                build_status={"success": False, "reason": "No build evidence found"},
                test_status={
                    "has_test_reports": False,
                    "status": "WARNING",
                    "reason": "No test reports found",
                    "pass_rate": 0.0,
                    "total_tests": 0,
                    "passed_tests": 0,
                    "test_exclusions": [],
                    "modules_without_tests": [],
                },
            )
        ),
        verdict="partial",
        conflicts=["tests_not_fully_executed"],
    )

    ok = agent._get_verified_final_status(react_engine_success=False)

    assert ok is False
    assert agent.final_verdict == "failed"


def test_snapshot_success_leaves_success_untouched():
    agent = _attach_report_snapshot(
        _agent_with_validator(_green_validator()),
        verdict="success",
        conflicts=[],
    )

    ok = agent._get_verified_final_status(react_engine_success=True)

    assert ok is True
    assert agent.final_verdict == "success"
