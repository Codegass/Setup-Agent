import pytest

from sag.evidence import EvidenceAssessment
from sag.tools.base import ToolResult
from sag.tools.bash import BashTool, BashToolConfig


def test_tool_result_defaults_status_from_success_boolean():
    success = ToolResult(success=True, output="ok")
    failure = ToolResult(success=False, output="", error="bad")

    assert success.status == EvidenceAssessment.SUCCESS
    assert failure.status == EvidenceAssessment.BLOCKED
    assert success.success is True
    assert failure.success is False


def test_tool_result_status_can_represent_partial_without_losing_legacy_success():
    result = ToolResult(
        success=True,
        status=EvidenceAssessment.PARTIAL,
        output="Build command exited 0 but tests failed.",
        evidence_refs=["output_abc"],
        conflicts=["maven_success_vs_surefire_failures"],
        test_stats={"executed": 214, "passed": 206, "failed": 3, "skipped": 5},
    )

    assert result.success is True
    assert result.status == EvidenceAssessment.PARTIAL
    assert result.evidence_refs == ["output_abc"]
    assert result.conflicts == ["maven_success_vs_surefire_failures"]
    assert result.test_stats.pass_rate == 96.3


def test_tool_result_coerces_string_status_inputs():
    result = ToolResult(
        success=True,
        status="partial",
        output="Build command exited 0 but tests failed.",
    )

    assert isinstance(result.status, EvidenceAssessment)
    assert result.status == EvidenceAssessment.PARTIAL


def test_tool_result_rejects_status_assignment_after_init():
    result = ToolResult(success=True, output="ok")
    before = result.model_dump()

    with pytest.raises(TypeError, match="status.*read-only"):
        result.status = "blocked"

    assert result.model_dump() == before
    assert result.status == EvidenceAssessment.SUCCESS


def test_tool_result_rejects_success_assignment_after_init():
    result = ToolResult(success=True, output="ok")
    before = result.model_dump()

    with pytest.raises(TypeError, match="success.*read-only"):
        result.success = False

    assert result.model_dump() == before
    assert result.success is True
    assert result.status == EvidenceAssessment.SUCCESS


def test_tool_result_preserves_explicit_domain_status_after_rejected_success_change():
    partial = ToolResult(success=True, status=EvidenceAssessment.PARTIAL, output="partial")
    conflict = ToolResult(success=False, status=EvidenceAssessment.CONFLICT, output="conflict")
    partial_before = partial.model_dump()
    conflict_before = conflict.model_dump()

    with pytest.raises(TypeError, match="success.*read-only"):
        partial.success = False
    with pytest.raises(TypeError, match="success.*read-only"):
        conflict.success = True

    assert partial.model_dump() == partial_before
    assert conflict.model_dump() == conflict_before
    assert partial.status == EvidenceAssessment.PARTIAL
    assert conflict.status == EvidenceAssessment.CONFLICT


class FakeBashOrchestrator:
    def __init__(self, result):
        self.container_name = "demo-container"
        self.result = result

    def execute_command(
        self,
        command,
        workdir=None,
        capture_stderr=True,
        environment=None,
        timeout=None,
    ):
        if "test -d /workspace" in command:
            return {
                "success": True,
                "output": "EXISTS",
                "exit_code": 0,
                "stdout": "EXISTS",
                "stderr": "",
            }
        return self.result

    def execute_command_with_monitoring(self, **kwargs):
        return self.result


def test_bash_success_reports_execution_facts_without_domain_status():
    tool = BashTool(
        docker_orchestrator=FakeBashOrchestrator(
            {
                "success": True,
                "exit_code": 0,
                "output": "BUILD SUCCESS",
                "duration": 1.25,
            }
        )
    )

    result = tool.execute(command="mvn test", working_directory="/workspace/project", timeout=30)

    assert result.success is True
    assert result.metadata["execution"]["executed"] is True
    assert result.metadata["execution"]["exit_code"] == 0
    assert result.metadata["execution"]["timed_out"] is False
    assert "domain_status" not in result.metadata


def test_bash_nonzero_reports_executed_nonzero_without_domain_status():
    tool = BashTool(
        docker_orchestrator=FakeBashOrchestrator(
            {
                "success": False,
                "exit_code": 2,
                "output": "tests failed",
            }
        )
    )

    result = tool.execute(command="npm test", working_directory="/workspace/project", timeout=30)

    assert result.success is False
    assert result.metadata["execution"]["executed"] is True
    assert result.metadata["execution"]["exit_code"] == 2
    assert result.status.value == "blocked"


def test_bash_blocked_command_reports_pre_execution_facts():
    tool = BashTool(
        docker_orchestrator=FakeBashOrchestrator({"success": True, "exit_code": 0, "output": "ok"}),
        config=BashToolConfig(blocked_commands=["rm"]),
    )

    result = tool.execute(command="rm -rf /tmp/demo", working_directory="/workspace/project")

    assert result.success is False
    assert result.error_code == "COMMAND_BLOCKED"
    assert result.metadata["execution"]["executed"] is False
    assert result.metadata["execution"]["exit_code"] is None
    assert result.metadata["execution"]["timed_out"] is False


def test_bash_no_orchestrator_reports_pre_execution_facts():
    tool = BashTool(docker_orchestrator=None)

    result = tool.execute(command="echo hi", working_directory="/workspace/project")

    assert result.success is False
    assert result.error_code == "NO_ORCHESTRATOR"
    assert result.metadata["execution"]["executed"] is False
    assert result.metadata["execution"]["exit_code"] is None
    assert result.metadata["execution"]["timed_out"] is False
    assert "Docker is running" in result.suggestions[0]


def test_bash_interactive_command_reports_pre_execution_facts():
    tool = BashTool(docker_orchestrator=FakeBashOrchestrator({"success": True, "output": "ok"}))

    result = tool.execute(command="vim file.txt", working_directory="/workspace/project")

    assert result.success is False
    assert result.error_code == "INTERACTIVE_COMMAND"
    assert result.metadata["execution"]["executed"] is False
    assert result.metadata["execution"]["exit_code"] is None
    assert result.metadata["execution"]["timed_out"] is False


def test_bash_background_success_reports_execution_facts():
    tool = BashTool(
        docker_orchestrator=FakeBashOrchestrator(
            {
                "success": True,
                "output": "1234",
                "exit_code": 0,
            }
        )
    )

    result = tool.execute(command="sleep 60 &", working_directory="/workspace/project")

    assert result.success is True
    assert result.metadata["execution"]["executed"] is True
    assert result.metadata["execution"]["exit_code"] is None
    assert result.metadata["execution"]["timed_out"] is False
    assert result.metadata["background_pids"] == [1234]


def test_bash_background_failed_start_preserves_execution_facts():
    tool = BashTool(
        docker_orchestrator=FakeBashOrchestrator(
            {
                "success": False,
                "output": "permission denied",
                "exit_code": 126,
            }
        )
    )

    result = tool.execute(command="sleep 60 &", working_directory="/workspace/project")

    assert result.success is False
    assert result.error_code == "BACKGROUND_START_FAILED"
    assert result.metadata["execution"]["executed"] is True
    assert result.metadata["execution"]["exit_code"] == 126
    assert result.metadata["execution"]["timed_out"] is False


def test_bash_safe_execute_missing_command_backfills_pre_execution_metadata():
    tool = BashTool(None)

    result = tool.safe_execute()

    assert result.success is False
    assert result.error_code == "MISSING_PARAMETERS"
    assert result.metadata["execution"]["executed"] is False
    assert result.metadata["execution"]["exit_code"] is None
    assert result.metadata["execution"]["timed_out"] is False
    assert result.metadata["failure_category"] == "validation"
    assert result.metadata["retryable"] is True


def test_bash_safe_execute_no_orchestrator_preserves_execution_metadata():
    tool = BashTool(None)

    result = tool.safe_execute(command="echo hi")

    assert result.success is False
    assert result.error_code == "NO_ORCHESTRATOR"
    assert result.metadata["execution"]["executed"] is False
    assert result.metadata["execution"]["exit_code"] is None
    assert result.metadata["execution"]["timed_out"] is False
    assert result.metadata["failure_category"] == "execution"
