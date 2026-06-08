from types import SimpleNamespace

from sag.agent.agent import SetupAgent
from sag.agent.physical_validator import PhysicalValidator


class FakePhysicalValidator:
    def __init__(self, build_status, test_status, analysis_status=None):
        self.build_status = build_status
        self.test_status = test_status
        self.analysis_status = analysis_status or {"analyzed": False}
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


def test_verified_final_status_rejects_failed_tests():
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
            },
            analysis_status={
                "analyzed": True,
                "has_static_test_count": True,
                "static_test_count": 460,
            },
        )
    )

    assert agent._get_verified_final_status(react_engine_success=True) is False


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
    assert result["reason"] == "No build evidence found (no artifacts or build fingerprints)"
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
