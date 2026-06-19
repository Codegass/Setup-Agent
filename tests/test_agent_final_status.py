from types import SimpleNamespace

from sag.agent.agent import SetupAgent
from sag.agent.physical_validator import PhysicalValidator
from sag.config.settings import DEFAULT_TEST_PASS_THRESHOLD
from sag.tools.report_tool import ReportTool


class FakePhysicalValidator:
    def __init__(
        self,
        build_status,
        test_status,
        analysis_status=None,
        test_pass_threshold=DEFAULT_TEST_PASS_THRESHOLD,
    ):
        self.build_status = build_status
        self.test_status = test_status
        self.analysis_status = analysis_status or {"analyzed": False}
        # Mirror the real PhysicalValidator attribute so the run-success gate
        # reads the configured threshold (not a hardcoded default).
        self.test_pass_threshold = test_pass_threshold
        self.build_project_names = []
        self.test_project_names = []
        self.analysis_project_names = []

    def validate_build_status(self, project_name):
        self.build_project_names.append(project_name)
        return self.build_status

    def validate_test_status(self, project_name):
        self.test_project_names.append(project_name)
        return self.test_status

    def validate_project_analysis_status(self, project_name):
        self.analysis_project_names.append(project_name)
        return self.analysis_status


def _agent_with_validator(validator):
    agent = object.__new__(SetupAgent)
    agent.orchestrator = SimpleNamespace(project_name="demo")
    agent.project_name = "demo"
    agent.context_manager = SimpleNamespace(project_name="demo")
    agent.physical_validator = validator
    return agent


class MetadataOrchestrator:
    def __init__(self, docker_label, metadata):
        self.project_name = docker_label
        self.metadata = metadata

    def execute_command(self, command):
        if command == "cat /workspace/.setup_agent/project_meta.json 2>/dev/null":
            return {"exit_code": 0, "output": self.metadata}
        return {"exit_code": 1, "output": ""}


def test_verified_final_status_rejects_missing_test_reports_when_tests_expected():
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={"success": True, "reason": "Build fingerprints found"},
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
            analysis_status={
                "analyzed": True,
                "has_static_test_count": True,
                "static_test_count": 12,
            },
        )
    )

    assert agent._get_verified_final_status(react_engine_success=False) is False


def test_verified_final_status_allows_build_only_project_without_detected_tests():
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={"success": True, "reason": "Build fingerprints found"},
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
            analysis_status={
                "analyzed": True,
                "has_static_test_count": True,
                "static_test_count": 0,
            },
        )
    )

    assert agent._get_verified_final_status(react_engine_success=True) is True


def test_verified_final_status_allows_skipped_tests_without_failures():
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={"success": True, "reason": "Build fingerprints found"},
            test_status={
                "has_test_reports": True,
                "status": "PARTIAL",
                "reason": "Tests partially passed",
                "pass_rate": 97.7,
                "total_tests": 430,
                "passed_tests": 420,
                "failed_tests": 0,
                "error_tests": 0,
                "skipped_tests": 10,
                "test_exclusions": [],
                "modules_without_tests": [],
            },
            analysis_status={
                "analyzed": True,
                "has_static_test_count": True,
                "static_test_count": 460,
            },
        )
    )

    assert agent._get_verified_final_status(react_engine_success=True) is True


def test_verified_final_status_accepts_partial_pass_above_threshold():
    """A build-green run with failures but pass rate >= threshold is a SUCCESS
    (partial pass), matching the single verdict policy used by the report.

    Previously this path applied zero tolerance (any failure -> fail); the run
    gate now delegates to evaluate_run_verdict so it agrees with the report.
    """
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={"success": True, "reason": "Build fingerprints found"},
            test_status={
                "has_test_reports": True,
                "status": "PARTIAL",
                "reason": "Tests partially passed",
                "pass_rate": 97.7,
                "total_tests": 430,
                "passed_tests": 420,
                "failed_tests": 1,
                "error_tests": 0,
                "skipped_tests": 9,
                "test_exclusions": [],
                "modules_without_tests": [],
                # The real validator restates counted failures as conflicts;
                # they must not demote a threshold pass (round-6 review).
                "conflicts": ["test_failures_detected"],
            },
            analysis_status={
                "analyzed": True,
                "has_static_test_count": True,
                "static_test_count": 460,
            },
        )
    )

    assert agent._get_verified_final_status(react_engine_success=True) is True


def test_verified_final_status_rejects_below_threshold_tests():
    """Build green but pass rate below test_pass_threshold -> failure."""
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={"success": True, "reason": "Build fingerprints found"},
            test_status={
                "has_test_reports": True,
                "status": "FAILED",
                "reason": "Tests below threshold",
                "pass_rate": 50.0,
                "total_tests": 100,
                "passed_tests": 50,
                "failed_tests": 50,
                "error_tests": 0,
                "skipped_tests": 0,
                "test_exclusions": [],
                "modules_without_tests": [],
            },
            analysis_status={
                "analyzed": True,
                "has_static_test_count": True,
                "static_test_count": 100,
            },
        )
    )

    assert agent._get_verified_final_status(react_engine_success=True) is False


def test_verified_final_status_honors_configured_threshold():
    """The configured test_pass_threshold must change the run-success verdict.

    Proves SAG_TEST_PASS_THRESHOLD / Config.test_pass_threshold is wired through
    PhysicalValidator into the run gate (not the hardcoded 0.8 default): the same
    85% build-green run passes under the default 0.8 but fails under a 0.9 gate.
    """

    def _profile(threshold):
        return FakePhysicalValidator(
            build_status={"success": True, "reason": "Build fingerprints found"},
            test_status={
                "has_test_reports": True,
                "status": "PARTIAL",
                "reason": "Tests partially passed",
                "pass_rate": 85.0,
                "total_tests": 100,
                "passed_tests": 85,
                "failed_tests": 15,
                "error_tests": 0,
                "skipped_tests": 0,
                "test_exclusions": [],
                "modules_without_tests": [],
            },
            analysis_status={
                "analyzed": True,
                "has_static_test_count": True,
                "static_test_count": 100,
            },
            test_pass_threshold=threshold,
        )

    default_agent = _agent_with_validator(_profile(0.8))
    strict_agent = _agent_with_validator(_profile(0.9))

    assert default_agent._get_verified_final_status(react_engine_success=True) is True
    assert strict_agent._get_verified_final_status(react_engine_success=True) is False


def test_verified_final_status_matches_report_verdict_for_commons_vfs(monkeypatch):
    """commons-vfs (build green, 177/184 = 96.2%, 7 failing) is a SUCCESS on
    BOTH the run-success gate and the report verdict (no second-gate divergence).
    """
    accomplishments = {
        "repository_cloned": True,
        "build_success": True,
        "physical_validation": {
            "test_analysis": {"total_tests": 184, "passed_tests": 177}
        },
    }

    validator = PhysicalValidator(project_path="/workspace")
    monkeypatch.setattr(
        validator,
        "parse_test_reports_with_catalog",
        lambda project_dir: {
            "valid": True,
            "total_tests": 184,
            "passed_tests": 177,
            "failed_tests": 5,
            "error_tests": 2,
            "skipped_tests": 0,
            "test_exclusions": [],
            "modules_without_tests": [],
            "report_files": [
                "/workspace/commons-vfs/target/surefire-reports/TEST-Vfs.xml"
            ],
            "parsing_errors": [],
        },
    )
    monkeypatch.setattr(
        validator,
        "validate_build_status",
        lambda project_name: {"success": True, "reason": "Build fingerprints found"},
    )
    monkeypatch.setattr(
        validator,
        "validate_project_analysis_status",
        lambda project_name: {
            "analyzed": True,
            "has_static_test_count": True,
            "static_test_count": 184,
        },
    )

    agent = _agent_with_validator(validator)
    run_status = agent._get_verified_final_status(react_engine_success=True)

    tool = ReportTool(docker_orchestrator=None, physical_validator=validator)
    report_verdict = tool._determine_actual_status(accomplishments)

    assert run_status is True
    assert report_verdict == "success"
    # The two paths must agree on the same run.
    assert run_status == (report_verdict == "success")


def test_failed_test_validation_carries_evidence_state(monkeypatch):
    validator = PhysicalValidator(project_path="/workspace")

    monkeypatch.setattr(
        validator,
        "parse_test_reports_with_catalog",
        lambda project_dir: {
            "valid": True,
            "total_tests": 430,
            "passed_tests": 420,
            "failed_tests": 1,
            "error_tests": 2,
            "skipped_tests": 9,
            "test_exclusions": [],
            "modules_without_tests": [],
            "report_files": [
                "/workspace/demo/target/surefire-reports/TEST-com.example.DemoTest.xml"
            ],
            "parsing_errors": ["Error parsing /workspace/demo/target/surefire-reports/TEST-bad.xml"],
        },
    )

    result = validator.validate_test_status("demo")

    assert result["evidence_status"] == "partial"
    assert result["test_stats"]["executed"] == 430
    assert result["test_stats"]["passed"] == 420
    assert result["test_stats"]["failed"] > 0
    assert result["test_stats"]["skipped"] == 9
    assert result["test_stats"]["pass_rate"] == 97.7
    assert result["conflicts"] == [
        "test_failures_detected",
        "test_errors_detected",
        "test_report_parse_error",
    ]
    assert result["parsing_errors"] == [
        "Error parsing /workspace/demo/target/surefire-reports/TEST-bad.xml"
    ]
    assert result["evidence_refs"] == [
        "/workspace/demo/target/surefire-reports/TEST-com.example.DemoTest.xml"
    ]


def test_build_validation_refs_prefer_artifact_samples(monkeypatch):
    validator = PhysicalValidator(project_path="/workspace")

    monkeypatch.setattr(validator, "_detect_build_system", lambda project_dir: "maven")
    monkeypatch.setattr(
        validator,
        "_check_build_artifacts_complete",
        lambda project_dir: {"exist": True, "count": 2, "jar_count": 1, "class_count": 1},
    )
    monkeypatch.setattr(
        validator,
        "_validate_maven_fingerprints",
        lambda project_dir: {"valid": False, "details": {}, "modules": []},
    )
    monkeypatch.setattr(validator, "_get_expected_artifacts", lambda project_dir, build_system: [])
    monkeypatch.setattr(
        validator,
        "_check_class_files",
        lambda project_dir: {
            "count": 1,
            "paths": ["/workspace/demo/target/classes/com/example/Demo.class"],
        },
    )
    monkeypatch.setattr(
        validator,
        "_check_jar_files",
        lambda project_dir: {
            "count": 1,
            "paths": ["/workspace/demo/target/demo-1.0.jar"],
        },
    )

    result = validator.validate_build_status("demo")

    assert result["success"] is True
    assert result["conflicts"] == []
    assert result["evidence_refs"] == [
        "/workspace/demo/target/classes/com/example/Demo.class",
        "/workspace/demo/target/demo-1.0.jar",
    ]


def test_failed_build_validation_uses_stable_conflict_and_project_fallback(monkeypatch):
    validator = PhysicalValidator(project_path="/workspace")

    monkeypatch.setattr(validator, "_detect_build_system", lambda project_dir: "maven")
    monkeypatch.setattr(
        validator,
        "_check_build_artifacts_complete",
        lambda project_dir: {"exist": False, "count": 0, "jar_count": 0, "class_count": 0},
    )
    monkeypatch.setattr(
        validator,
        "_validate_maven_fingerprints",
        lambda project_dir: {"valid": False, "details": {}, "modules": []},
    )

    result = validator.validate_build_status("demo")

    assert result["success"] is False
    # Maven with zero compiled classes and no artifacts is caught by the hard JVM
    # compiled-evidence gate (no phantom green); the conflict + project-fallback
    # evidence_refs remain stable.
    assert "No compiled .class files" in result["reason"]
    assert result["conflicts"] == ["build_validation_failed"]
    assert result["evidence_refs"] == ["/workspace/demo"]


def test_test_validation_without_reports_does_not_emit_empty_test_stats(monkeypatch):
    validator = PhysicalValidator(project_path="/workspace")

    monkeypatch.setattr(
        validator,
        "parse_test_reports_with_catalog",
        lambda project_dir: {
            "valid": False,
            "total_tests": 0,
            "passed_tests": 0,
            "failed_tests": 0,
            "error_tests": 0,
            "skipped_tests": 0,
            "test_exclusions": [],
            "modules_without_tests": [],
            "report_files": [],
            "parsing_errors": [],
        },
    )

    result = validator.validate_test_status("demo")

    assert result["evidence_status"] == "unknown"
    assert result["has_test_reports"] is False
    assert result["test_stats"] is None
    assert result["evidence_refs"] == ["/workspace/demo"]


def test_verified_final_status_uses_project_metadata_over_docker_label():
    validator = FakePhysicalValidator(
        build_status={"success": True, "reason": "Build fingerprints found"},
        test_status={
            "has_test_reports": True,
            "status": "SUCCESS",
            "reason": "All tests passed",
            "pass_rate": 100.0,
            "total_tests": 551,
            "passed_tests": 551,
            "failed_tests": 0,
            "error_tests": 0,
            "skipped_tests": 0,
            "test_exclusions": [],
            "modules_without_tests": [],
        },
        analysis_status={
            "analyzed": True,
            "has_static_test_count": True,
            "static_test_count": 551,
        },
    )
    agent = object.__new__(SetupAgent)
    agent.orchestrator = MetadataOrchestrator(
        docker_label="commons-vfs-utf8-check",
        metadata='{"project_name": "commons-vfs", "docker_label": "commons-vfs-utf8-check"}',
    )
    agent.project_name = "commons-vfs-utf8-check"
    agent.context_manager = SimpleNamespace(project_name="commons-vfs-utf8-check")
    agent.physical_validator = validator

    assert agent._get_verified_final_status(react_engine_success=True) is True
    assert validator.build_project_names == ["commons-vfs"]
    assert validator.test_project_names == ["commons-vfs"]
    assert validator.analysis_project_names == ["commons-vfs"]


# --- tri-state verdict surface (beam 06-10: 🎉 success with 0 tests) ---------


def _no_reports_test_status():
    return {
        "has_test_reports": False,
        "status": "WARNING",
        "reason": "No test reports found",
        "pass_rate": 0.0,
        "total_tests": 0,
        "passed_tests": 0,
        "failed_tests": 0,
        "error_tests": 0,
        "skipped_tests": 0,
        "test_exclusions": [],
    }


def test_build_green_without_test_evidence_is_partial_not_success():
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={"success": True, "reason": "Build fingerprints found"},
            test_status=_no_reports_test_status(),
            analysis_status={"analyzed": False},
        )
    )

    result = agent._get_verified_final_status(react_engine_success=False)

    assert result is True, "build-green/no-expectation keeps the flow-control bool"
    assert agent.final_verdict == "partial", "but the surfaced verdict must be partial"


def test_build_green_missing_expected_tests_is_partial_and_false():
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={"success": True, "reason": "Build fingerprints found"},
            test_status=_no_reports_test_status(),
            analysis_status={"analyzed": True, "static_test_count": 13847},
        )
    )

    result = agent._get_verified_final_status(react_engine_success=False)

    assert result is False
    assert agent.final_verdict == "partial"


def test_threshold_pass_is_full_success_verdict():
    # The real validator ALWAYS emits restated-failure conflicts when
    # failed_tests > 0 (physical_validator.validate_test_status); omitting the
    # key here let the suite mask a production demotion to 'partial'
    # (round-6 review: conflicts double-adjudicated the counted failures the
    # threshold policy already accepted).
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={"success": True, "reason": "Build fingerprints found"},
            test_status={
                "has_test_reports": True,
                "status": "PARTIAL",
                "reason": "",
                "pass_rate": 96.3,
                "total_tests": 214,
                "passed_tests": 206,
                "failed_tests": 3,
                "error_tests": 0,
                "skipped_tests": 5,
                "test_exclusions": [],
                "modules_without_tests": [],
                "conflicts": ["test_failures_detected"],
            },
        )
    )

    result = agent._get_verified_final_status(react_engine_success=True)

    assert result is True
    assert agent.final_verdict == "success"


def test_build_failure_is_failed_verdict():
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={"success": False, "reason": "no artifacts"},
            test_status=_no_reports_test_status(),
        )
    )

    result = agent._get_verified_final_status(react_engine_success=False)

    assert result is False
    assert agent.final_verdict == "failed"


# --- phase-machine capping (stage-2 Task 8): the machine's honest outcome ----
# caps the verdict; it never promotes (physical validation still rules).


def _all_green_validator():
    return FakePhysicalValidator(
        build_status={"success": True, "reason": "Build fingerprints found"},
        test_status={
            "has_test_reports": True,
            "status": "SUCCESS",
            "reason": "All tests passed",
            "pass_rate": 100.0,
            "total_tests": 100,
            "passed_tests": 100,
            "failed_tests": 0,
            "error_tests": 0,
            "skipped_tests": 0,
            "test_exclusions": [],
            "modules_without_tests": [],
        },
        analysis_status={
            "analyzed": True,
            "has_static_test_count": True,
            "static_test_count": 100,
        },
    )


def _attach_phase_machine(agent, block_phase=None):
    from sag.agent.phase_machine import PHASE_NAMES, PhaseMachine

    machine = PhaseMachine()
    for name in PHASE_NAMES:
        if name == block_phase:
            machine.mark_blocked(f"{name} blocked", [])
        else:
            machine.mark_done("ok", [])
    agent.react_engine = SimpleNamespace(phase_machine=machine)
    return machine


def test_machine_failed_outcome_caps_physical_success():
    agent = _agent_with_validator(_all_green_validator())
    _attach_phase_machine(agent, block_phase="build")

    assert agent._get_verified_final_status(react_engine_success=True) is False
    assert agent.final_verdict == "failed"


def test_machine_partial_outcome_caps_success_verdict_to_partial():
    agent = _agent_with_validator(_all_green_validator())
    _attach_phase_machine(agent, block_phase="test")

    assert agent._get_verified_final_status(react_engine_success=True) is True
    assert agent.final_verdict == "partial"


def test_machine_success_still_subject_to_physical_validation():
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={"success": False, "reason": "no artifacts"},
            test_status=_no_reports_test_status(),
        )
    )
    _attach_phase_machine(agent)  # all phases done

    assert agent._get_verified_final_status(react_engine_success=True) is False
    assert agent.final_verdict == "failed"


def test_machine_success_keeps_physical_success():
    agent = _agent_with_validator(_all_green_validator())
    _attach_phase_machine(agent)

    assert agent._get_verified_final_status(react_engine_success=True) is True
    assert agent.final_verdict == "success"


def test_final_verdict_uses_kernel_conflict_cap():
    """Physical success + machine success + evidence conflicts => partial."""
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={"success": True, "reason": "fingerprints"},
            test_status={
                "has_test_reports": True, "status": "PARTIAL", "reason": "",
                "pass_rate": 99.3, "total_tests": 2913, "passed_tests": 2893,
                "failed_tests": 15, "error_tests": 0, "skipped_tests": 5,
                "test_exclusions": [], "modules_without_tests": [],
                "conflicts": ["test_report_parse_error"],
            },
        )
    )
    result = agent._get_verified_final_status(react_engine_success=True)
    assert result is True, "flow-control bool unchanged"
    assert agent.final_verdict == "partial", "conflicts must cap the surfaced verdict"


def test_partial_reason_for_conflict_capped_run():
    """Round-6 vfs: a conflict-capped partial printed 'no test reports found'
    while 96.2% of tests sat in the report. The banner reason must match the
    actual cause."""
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={"success": True, "reason": "fingerprints"},
            test_status={
                "has_test_reports": True, "status": "PARTIAL", "reason": "",
                "pass_rate": 96.2, "total_tests": 184, "passed_tests": 177,
                "failed_tests": 3, "error_tests": 0, "skipped_tests": 4,
                "test_exclusions": [], "modules_without_tests": [],
                "conflicts": ["test_report_parse_error"],
            },
        )
    )
    agent._get_verified_final_status(react_engine_success=True)
    assert agent.final_verdict == "partial"
    assert "conflict" in agent.final_verdict_reason
    assert "no test reports" not in agent.final_verdict_reason


def test_partial_reason_for_missing_test_evidence():
    agent = _agent_with_validator(
        FakePhysicalValidator(
            build_status={"success": True, "reason": "fingerprints"},
            test_status={
                "has_test_reports": False, "status": "WARNING", "reason": "",
                "pass_rate": 0.0, "total_tests": 0, "passed_tests": 0,
                "failed_tests": 0, "error_tests": 0, "skipped_tests": 0,
                "test_exclusions": [],
            },
            analysis_status={"analyzed": False},
        )
    )
    agent._get_verified_final_status(react_engine_success=False)
    assert agent.final_verdict == "partial"
    assert "no test reports" in agent.final_verdict_reason
