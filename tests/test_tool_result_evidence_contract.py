from sag.evidence import EvidenceStatus
from sag.tools.base import ToolResult
from sag.tools.bash import BashTool


def test_tool_result_defaults_status_from_success_boolean():
    success = ToolResult(success=True, output="ok")
    failure = ToolResult(success=False, output="", error="bad")

    assert success.status == EvidenceStatus.SUCCESS
    assert failure.status == EvidenceStatus.BLOCKED
    assert success.success is True
    assert failure.success is False


def test_tool_result_status_can_represent_partial_without_losing_legacy_success():
    result = ToolResult(
        success=True,
        status=EvidenceStatus.PARTIAL,
        output="Build command exited 0 but tests failed.",
        evidence_refs=["output_abc"],
        conflicts=["maven_success_vs_surefire_failures"],
        test_stats={"executed": 214, "passed": 206, "failed": 3, "skipped": 5},
    )

    assert result.success is True
    assert result.status == EvidenceStatus.PARTIAL
    assert result.evidence_refs == ["output_abc"]
    assert result.conflicts == ["maven_success_vs_surefire_failures"]
    assert result.test_stats.pass_rate == 96.3


def test_tool_result_coerces_string_status_inputs():
    result = ToolResult(
        success=True,
        status="partial",
        output="Build command exited 0 but tests failed.",
    )

    assert isinstance(result.status, EvidenceStatus)
    assert result.status == EvidenceStatus.PARTIAL


def test_tool_result_coerces_status_assignment_after_init():
    result = ToolResult(success=True, output="ok")

    result.status = "blocked"

    assert isinstance(result.status, EvidenceStatus)
    assert result.status == EvidenceStatus.BLOCKED


def test_tool_result_updates_default_status_when_success_changes():
    result = ToolResult(success=True, output="ok")

    result.success = False

    assert result.success is False
    assert result.status == EvidenceStatus.BLOCKED


def test_tool_result_preserves_explicit_domain_status_when_success_changes():
    partial = ToolResult(success=True, status=EvidenceStatus.PARTIAL, output="partial")
    conflict = ToolResult(success=False, status=EvidenceStatus.CONFLICT, output="conflict")

    partial.success = False
    conflict.success = True

    assert partial.status == EvidenceStatus.PARTIAL
    assert conflict.status == EvidenceStatus.CONFLICT


class FakeBashOrchestrator:
    def __init__(self, result):
        self.result = result

    def execute_command(
        self,
        command,
        workdir=None,
        capture_stderr=True,
        environment=None,
        timeout=None,
    ):
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
