"""Phase 3 — Completion integrity: gate build/test completion on real evidence.

These tests cover:
  * 3.1 physical-evidence gate (compile -> artifacts, test -> reports)
  * 3.3 complete_task bypass closure + run-success gate
  * 3.2 unresolved-requirement / remediation gate
and assert the commons-cli happy path is NOT over-blocked.
"""

from types import SimpleNamespace

import pytest

from sag.agent.agent_state_evaluator import AgentStateEvaluator
from sag.agent.react_types import StepType
from sag.tools.base import ToolError
from sag.tools.context_tool import ContextTool


# --- fakes -----------------------------------------------------------------


class FakeValidator:
    """Stand-in for PhysicalValidator with scripted build/test verdicts."""

    def __init__(self, build_success=True, build_system="gradle", has_test_reports=True):
        self._build_success = build_success
        self._build_system = build_system
        self._has_test_reports = has_test_reports
        self.build_calls = []
        self.test_calls = []

    def validate_build_status(self, project_name=None):
        self.build_calls.append(project_name)
        return {
            "success": self._build_success,
            "evidence": {"build_system": self._build_system},
            "reason": "scripted",
        }

    def validate_test_status(self, project_name=None):
        self.test_calls.append(project_name)
        return {"has_test_reports": self._has_test_reports, "status": "scripted"}


class FakeTask:
    def __init__(self, task_id, description, status="in_progress"):
        self.id = task_id
        self.description = description
        self.status = SimpleNamespace(value=status)


class FakeTrunk:
    def __init__(self, tasks):
        self.todo_list = tasks


def _branch_cm(history=None, validator=None, project_name="demo"):
    """Minimal context manager for direct _validate_task_completion calls."""
    cm = SimpleNamespace(
        current_task_id="task_4",
        project_name=project_name,
        load_branch_history=lambda task_id: SimpleNamespace(history=history or []),
    )
    if validator is not None:
        cm.physical_validator = validator
    return cm


def _full_cm(history=None, validator=None, description="Compile with Gradle"):
    """Context manager wired enough to drive execute(action='complete_task')."""
    task = FakeTask("task_4", description)
    trunk = FakeTrunk([task])
    cm = SimpleNamespace(
        current_task_id="task_4",
        project_name="demo",
        load_branch_history=lambda task_id: SimpleNamespace(history=history or []),
        load_trunk_context=lambda: trunk,
        complete_branch=lambda task_id, summary: {
            "progress": "1/1 complete",
            "all_tasks_completed": True,
        },
    )
    if validator is not None:
        cm.physical_validator = validator
    return cm


def _task(description):
    return SimpleNamespace(id="task_4", description=description)


# A successful gradle build action so the existing tool-execution gate passes,
# letting the physical-evidence gate be the deciding factor.
GRADLE_ACTION = {
    "type": "action",
    "tool_name": "gradle",
    "success": True,
    "output": "compileJava",
}


# --- 3.1 physical-evidence gate -------------------------------------------


def test_compile_task_rejected_when_no_physical_artifacts():
    validator = FakeValidator(build_success=False, build_system="gradle")
    tool = ContextTool(_branch_cm(history=[GRADLE_ACTION], validator=validator))

    result = tool._validate_task_completion(
        _task("Compile with Gradle"),
        summary="Ran the Gradle compile task.",
        key_results="Gradle wrapper present; compileJava invoked.",
    )

    assert result["valid"] is False
    assert "artifact" in result["reason"].lower() or "evidence" in result["reason"].lower()
    assert validator.build_calls, "physical validator should have been consulted"


def test_compile_task_allowed_when_artifacts_present():
    validator = FakeValidator(build_success=True, build_system="gradle")
    tool = ContextTool(_branch_cm(history=[GRADLE_ACTION], validator=validator))

    result = tool._validate_task_completion(
        _task("Compile with Gradle"),
        summary="Gradle build completed successfully.",
        key_results="BUILD SUCCESSFUL; classes and jar produced.",
    )

    assert result["valid"] is True


def test_test_task_rejected_when_no_test_reports():
    validator = FakeValidator(build_success=True, has_test_reports=False)
    tool = ContextTool(_branch_cm(history=[GRADLE_ACTION], validator=validator))

    result = tool._validate_task_completion(
        _task("Run tests with Gradle"),
        summary="Ran the Gradle test task.",
        key_results="Invoked gradle test goal.",
    )

    assert result["valid"] is False
    assert "report" in result["reason"].lower()
    assert validator.test_calls, "test validator should have been consulted"


# --- no over-block: commons-cli style happy path ---------------------------


def test_commons_cli_style_task_with_maven_evidence_still_completes():
    validator = FakeValidator(build_success=True, build_system="maven")
    maven_action = {
        "type": "action",
        "tool_name": "maven",
        "success": True,
        "output": "BUILD SUCCESS",
    }
    tool = ContextTool(_branch_cm(history=[maven_action], validator=validator))

    result = tool._validate_task_completion(
        _task("Build and test with Maven"),
        summary="Maven build and tests completed successfully.",
        key_results="BUILD SUCCESS; Tests run: 184, Failures: 0; jars produced.",
    )

    assert result["valid"] is True


# --- 3.3 close the complete_task bypass ------------------------------------


def test_complete_task_rejects_build_task_without_evidence():
    validator = FakeValidator(build_success=False, build_system="gradle")
    tool = ContextTool(_full_cm(history=[GRADLE_ACTION], validator=validator))

    with pytest.raises(ToolError) as exc:
        tool.execute(action="complete_task", summary="Ran the Gradle compile.")

    assert "validation failed" in str(exc.value).lower()


def test_complete_task_allows_build_task_with_evidence():
    validator = FakeValidator(build_success=True, build_system="gradle")
    tool = ContextTool(_full_cm(history=[GRADLE_ACTION], validator=validator))

    result = tool.execute(
        action="complete_task",
        summary="Gradle build completed successfully with artifacts.",
    )

    assert result.success is True


# --- 3.3 run-success gate (state evaluator) --------------------------------


def _report_completion_steps():
    return [
        SimpleNamespace(
            step_type=StepType.ACTION,
            tool_name="report",
            tool_result=SimpleNamespace(
                success=True, metadata={"completion_signal": True}
            ),
        )
    ]


def _state_cm(build_task="Compile with Gradle"):
    trunk = FakeTrunk([FakeTask("task_5", build_task)])
    return SimpleNamespace(
        current_task_id=None,
        project_name="demo",
        load_trunk_context=lambda: trunk,
    )


def test_run_success_false_when_build_task_has_no_artifacts():
    validator = FakeValidator(build_success=False, build_system="gradle")
    evaluator = AgentStateEvaluator(_state_cm(), physical_validator=validator)

    assert evaluator._is_task_complete(_report_completion_steps()) is False


def test_run_success_true_when_build_task_has_artifacts():
    validator = FakeValidator(build_success=True, build_system="gradle")
    evaluator = AgentStateEvaluator(_state_cm(), physical_validator=validator)

    assert evaluator._is_task_complete(_report_completion_steps()) is True


# --- 3.2 unresolved-requirement / remediation gate -------------------------


def test_compile_task_rejected_when_requirement_unmet_and_no_remediation():
    history = [
        {
            "type": "observation",
            "content": "ERROR: JAVA_HOME is not set and no 'java' command could be found.",
        },
        GRADLE_ACTION,
    ]
    tool = ContextTool(_branch_cm(history=history))

    result = tool._validate_task_completion(
        _task("Compile with Gradle"),
        summary="Attempted the Gradle compile step.",
        key_results="The Gradle wrapper is present in the repo.",
    )

    assert result["valid"] is False
    reason = result["reason"].lower()
    assert "requirement" in reason or "remediat" in reason or "install" in reason


def test_compile_task_allowed_when_requirement_remediated():
    history = [
        {
            "type": "observation",
            "content": "ERROR: JAVA_HOME is not set and no 'java' command could be found.",
        },
        {
            "type": "action",
            "tool_name": "bash",
            "success": True,
            "command": "apt-get install -y openjdk-17-jdk",
        },
        GRADLE_ACTION,
    ]
    tool = ContextTool(_branch_cm(history=history))

    result = tool._validate_task_completion(
        _task("Compile with Gradle"),
        summary="Installed the JDK then compiled successfully.",
        key_results="openjdk-17 installed; gradle compile produced classes.",
    )

    assert result["valid"] is True
