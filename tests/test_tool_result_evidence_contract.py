import pytest

from sag.evidence import EvidenceAssessment
from sag.tools.base import ToolResult
from sag.tools.bash import BashTool, BashToolConfig


def test_tool_result_factories_default_assessment_from_operation_outcome():
    success = ToolResult.completed_success(output="ok")
    failure = ToolResult.completed_failure(output="", error="bad")

    assert success.evidence_assessment == EvidenceAssessment.SUCCESS
    assert failure.evidence_assessment == EvidenceAssessment.BLOCKED
    assert success.succeeded is True
    assert failure.succeeded is False


def test_evidence_assessment_can_be_partial_when_operation_succeeds():
    result = ToolResult.completed_success(
        evidence_assessment=EvidenceAssessment.PARTIAL,
        output="Build command exited 0 but tests failed.",
        evidence_refs=["output_abc"],
        conflicts=["maven_success_vs_surefire_failures"],
        test_stats={"executed": 214, "passed": 206, "failed": 3, "skipped": 5},
    )

    assert result.succeeded is True
    assert result.evidence_assessment == EvidenceAssessment.PARTIAL
    assert result.evidence_refs == ["output_abc"]
    assert result.conflicts == ["maven_success_vs_surefire_failures"]
    assert result.test_stats.pass_rate == 96.3


def test_tool_result_coerces_string_evidence_assessment_inputs():
    result = ToolResult.completed_success(
        evidence_assessment="partial",
        output="Build command exited 0 but tests failed.",
    )

    assert isinstance(result.evidence_assessment, EvidenceAssessment)
    assert result.evidence_assessment == EvidenceAssessment.PARTIAL


def test_tool_result_rejects_evidence_assessment_assignment_after_init():
    result = ToolResult.completed_success(output="ok")
    before = result.model_dump()

    with pytest.raises(TypeError, match="evidence_assessment.*read-only"):
        result.evidence_assessment = "blocked"

    assert result.model_dump() == before
    assert result.evidence_assessment == EvidenceAssessment.SUCCESS


def test_tool_result_rejects_operation_outcome_assignment_after_init():
    result = ToolResult.completed_success(output="ok")
    before = result.model_dump()

    with pytest.raises(TypeError, match="operation_outcome.*read-only"):
        result.operation_outcome = "failed"

    assert result.model_dump() == before
    assert result.succeeded is True
    assert result.evidence_assessment == EvidenceAssessment.SUCCESS


def test_tool_result_preserves_assessment_after_rejected_outcome_change():
    partial = ToolResult.completed_success(
        evidence_assessment=EvidenceAssessment.PARTIAL, output="partial"
    )
    conflict = ToolResult.completed_failure(
        evidence_assessment=EvidenceAssessment.CONFLICT, output="conflict"
    )
    partial_before = partial.model_dump()
    conflict_before = conflict.model_dump()

    with pytest.raises(TypeError, match="operation_outcome.*read-only"):
        partial.operation_outcome = "failed"
    with pytest.raises(TypeError, match="operation_outcome.*read-only"):
        conflict.operation_outcome = "success"

    assert partial.model_dump() == partial_before
    assert conflict.model_dump() == conflict_before
    assert partial.evidence_assessment == EvidenceAssessment.PARTIAL
    assert conflict.evidence_assessment == EvidenceAssessment.CONFLICT


class FakeBashOrchestrator:
    def __init__(self, result):
        self.container_name = "demo-container"
        self.result = result
        self.detached_calls = []

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

    def execute_command_detached(self, command, workdir=None, environment=None):
        self.detached_calls.append(
            {"command": command, "workdir": workdir, "environment": environment}
        )
        if not self.result.get("success"):
            return {
                "started": False,
                "job_id": "bash-background",
                "launch_output": self.result.get("output", ""),
            }
        return {
            "started": True,
            "job_id": "bash-background",
            "pid": 1234,
            "log_path": "/tmp/sag_jobs/bash-background.log",
        }


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

    assert result.succeeded is True
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

    assert result.succeeded is False
    assert result.metadata["execution"]["executed"] is True
    assert result.metadata["execution"]["exit_code"] == 2
    assert result.evidence_assessment.value == "blocked"


def test_bash_blocked_command_reports_pre_execution_facts():
    tool = BashTool(
        docker_orchestrator=FakeBashOrchestrator({"success": True, "exit_code": 0, "output": "ok"}),
        config=BashToolConfig(blocked_commands=["rm"]),
    )

    result = tool.execute(command="rm -rf /tmp/demo", working_directory="/workspace/project")

    assert result.succeeded is False
    assert result.error_code == "COMMAND_BLOCKED"
    assert result.metadata["execution"]["executed"] is False
    assert result.metadata["execution"]["exit_code"] is None
    assert result.metadata["execution"]["timed_out"] is False


def test_bash_no_orchestrator_reports_pre_execution_facts():
    tool = BashTool(docker_orchestrator=None)

    result = tool.execute(command="echo hi", working_directory="/workspace/project")

    assert result.succeeded is False
    assert result.error_code == "NO_ORCHESTRATOR"
    assert result.metadata["execution"]["executed"] is False
    assert result.metadata["execution"]["exit_code"] is None
    assert result.metadata["execution"]["timed_out"] is False
    assert "Docker is running" in result.suggestions[0]


def test_bash_interactive_command_reports_pre_execution_facts():
    tool = BashTool(docker_orchestrator=FakeBashOrchestrator({"success": True, "output": "ok"}))

    result = tool.execute(command="vim file.txt", working_directory="/workspace/project")

    assert result.succeeded is False
    assert result.error_code == "INTERACTIVE_COMMAND"
    assert result.metadata["execution"]["executed"] is False
    assert result.metadata["execution"]["exit_code"] is None
    assert result.metadata["execution"]["timed_out"] is False


def test_bash_background_dispatch_is_pending_and_uses_canonical_job_handle():
    orchestrator = FakeBashOrchestrator(
        {
            "success": True,
            "output": "1234",
            "exit_code": 0,
        }
    )
    tool = BashTool(docker_orchestrator=orchestrator)

    result = tool.execute(command="sleep 60 &", working_directory="/workspace/project")

    assert result.invocation_status.value == "pending"
    assert result.operation_outcome.value == "unknown"
    assert result.evidence_status.value == "unknown"
    assert result.poll_ref == "job:bash-background"
    assert orchestrator.detached_calls == [
        {
            "command": "sleep 60",
            "workdir": "/workspace/project",
            "environment": {"SAG_CLI": "1"},
        }
    ]


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

    assert result.succeeded is False
    assert result.error_code == "BACKGROUND_START_FAILED"
    assert result.metadata["execution"]["executed"] is False
    assert result.metadata["execution"]["exit_code"] is None
    assert result.metadata["execution"]["timed_out"] is False


def test_bash_safe_execute_missing_command_backfills_pre_execution_metadata():
    tool = BashTool(None)

    result = tool.safe_execute()

    assert result.succeeded is False
    assert result.error_code == "MISSING_PARAMETERS"
    assert result.metadata["execution"]["executed"] is False
    assert result.metadata["execution"]["exit_code"] is None
    assert result.metadata["execution"]["timed_out"] is False
    assert result.metadata["failure_category"] == "validation"
    assert result.metadata["retryable"] is True


def test_bash_safe_execute_no_orchestrator_preserves_execution_metadata():
    tool = BashTool(None)

    result = tool.safe_execute(command="echo hi")

    assert result.succeeded is False
    assert result.error_code == "NO_ORCHESTRATOR"
    assert result.metadata["execution"]["executed"] is False
    assert result.metadata["execution"]["exit_code"] is None
    assert result.metadata["execution"]["timed_out"] is False
    assert result.metadata["failure_category"] == "execution"
