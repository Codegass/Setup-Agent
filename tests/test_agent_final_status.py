from types import SimpleNamespace

from sag.agent.agent import SetupAgent


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
