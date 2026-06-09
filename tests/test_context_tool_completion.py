"""Phase 3 — Completion integrity: gate build/test completion on real evidence.

These tests cover:
  * 3.1 physical-evidence gate (compile -> artifacts, test -> reports),
    scoped to maven/gradle so non-Java tasks are never trapped
  * 3.3 complete_task bypass closure + run-success gate
  * 3.2 unresolved-requirement / remediation gate, overridable by green
    physical evidence
and assert the commons-cli happy path is NOT over-blocked.

Fakes mirror production shapes: ContextManager has NO project_name attribute
(only the trunk context does), and engine-written action history entries only
carry type/tool_name/success/output.
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
    def __init__(self, tasks, project_name="demo"):
        self.todo_list = tasks
        self.project_name = project_name


def _branch_cm(history=None, validator=None):
    """Minimal context manager for direct _validate_task_completion calls.

    Deliberately has NO project_name attribute — the real ContextManager
    doesn't have one either; the gates must read it from the trunk context.
    """
    cm = SimpleNamespace(
        current_task_id="task_4",
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

MAVEN_ACTION = {
    "type": "action",
    "tool_name": "maven",
    "success": True,
    "output": "BUILD SUCCESS",
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


# --- physical gate scoping: never trap non-Java or non-artifact tasks ------


def test_npm_build_task_not_gated_on_java_artifacts():
    """A Node build task must not be judged on .class/JAR presence."""
    validator = FakeValidator(build_success=False, build_system="nodejs")
    npm_action = {"type": "action", "tool_name": "npm", "success": True, "output": "added 120 packages"}
    tool = ContextTool(_branch_cm(history=[npm_action], validator=validator))

    result = tool._validate_task_completion(
        _task("Build project using npm"),
        summary="npm build completed successfully.",
        key_results="dist/ generated.",
    )

    assert result["valid"] is True
    assert validator.build_calls == [], "non-Java task must not trigger the Java artifact probe"


def test_pytest_test_task_not_gated_on_java_test_reports():
    """A pytest task must not be blocked for missing surefire/gradle XML."""
    validator = FakeValidator(has_test_reports=False)
    pytest_action = {"type": "action", "tool_name": "pytest", "success": True, "output": "42 passed"}
    tool = ContextTool(_branch_cm(history=[pytest_action], validator=validator))

    result = tool._validate_task_completion(
        _task("Run Python tests (pytest)"),
        summary="All tests passed with pytest.",
        key_results="42 passed in 3.2s.",
    )

    assert result["valid"] is True
    assert validator.test_calls == [], "non-Java test task must not trigger the report probe"


def test_unknown_build_system_not_blocked():
    """If the probe cannot identify a maven/gradle build, do not block."""
    validator = FakeValidator(build_success=False, build_system="unknown")
    tool = ContextTool(_branch_cm(history=[MAVEN_ACTION], validator=validator))

    result = tool._validate_task_completion(
        _task("Compile project using Maven"),
        summary="Maven compile completed successfully.",
        key_results="BUILD SUCCESS.",
    )

    assert result["valid"] is True
    assert len(validator.build_calls) == 1


def test_dependency_setup_task_not_gated_on_artifacts():
    """Dependency installation legitimately produces no compiled artifacts."""
    validator = FakeValidator(build_success=False, build_system="maven")
    tool = ContextTool(_branch_cm(history=[MAVEN_ACTION], validator=validator))

    result = tool._validate_task_completion(
        _task("Install Maven dependencies and verify build environment"),
        summary="Dependencies resolved successfully with Maven.",
        key_results="mvn dependency:resolve OK.",
    )

    assert result["valid"] is True


def test_test_task_classification_uses_word_boundaries():
    tool = ContextTool(_branch_cm())
    assert tool._is_test_task_description("install latest maven and build the project") is False
    assert tool._is_test_task_description("execute tests using maven") is True
    assert tool._is_test_task_description("run the test suite with gradle") is True


# --- no over-block: commons-cli style happy path ---------------------------


def test_commons_cli_style_task_with_maven_evidence_still_completes():
    validator = FakeValidator(build_success=True, build_system="maven")
    tool = ContextTool(_branch_cm(history=[MAVEN_ACTION], validator=validator))

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

    assert exc.value.error_code == "TASK_COMPLETION_VALIDATION_FAILED"
    assert "artifact" in str(exc.value).lower()
    assert any("force=true" in s.lower() for s in exc.value.suggestions)


def test_complete_task_allows_build_task_with_evidence():
    validator = FakeValidator(build_success=True, build_system="gradle")
    tool = ContextTool(_full_cm(history=[GRADLE_ACTION], validator=validator))

    result = tool.execute(
        action="complete_task",
        summary="Gradle build completed successfully with artifacts.",
    )

    assert result.success is True


def test_completion_gate_probes_trunk_project_name():
    """The gate must read project_name from the trunk context (the real
    ContextManager has no project_name attribute)."""
    validator = FakeValidator(build_success=True, build_system="gradle")
    cm = _full_cm(history=[GRADLE_ACTION], validator=validator)
    tool = ContextTool(cm)

    tool.execute(action="complete_task", summary="Gradle build completed successfully.")

    assert validator.build_calls == ["demo"]


# --- 3.3 run-success gate (state evaluator) --------------------------------


def _report_completion_steps(status=None):
    metadata = {"completion_signal": True}
    if status is not None:
        metadata["status"] = status
    return [
        SimpleNamespace(
            step_type=StepType.ACTION,
            tool_name="report",
            tool_result=SimpleNamespace(success=True, metadata=metadata),
        )
    ]


def _state_cm(build_task="Compile with Gradle"):
    trunk = FakeTrunk([FakeTask("task_5", build_task)], project_name="demo")
    return SimpleNamespace(
        current_task_id=None,
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


def test_run_gate_probes_trunk_project_name():
    validator = FakeValidator(build_success=True, build_system="gradle")
    evaluator = AgentStateEvaluator(_state_cm(), physical_validator=validator)

    evaluator._is_task_complete(_report_completion_steps())

    assert validator.build_calls == ["demo"]


def test_run_gate_lets_failed_report_end_run():
    """An honest status='fail' report must end the run (otherwise it spins to
    max_iterations); only success claims are gated on build evidence."""
    validator = FakeValidator(build_success=False, build_system="gradle")
    evaluator = AgentStateEvaluator(_state_cm(), physical_validator=validator)

    assert evaluator._is_task_complete(_report_completion_steps(status="fail")) is True


def test_run_gate_withholds_explicit_success_claim_without_artifacts():
    validator = FakeValidator(build_success=False, build_system="gradle")
    evaluator = AgentStateEvaluator(_state_cm(), physical_validator=validator)

    assert evaluator._is_task_complete(_report_completion_steps(status="success")) is False


# --- 3.2 unresolved-requirement / remediation gate -------------------------


def test_compile_task_rejected_when_requirement_unmet_and_no_remediation():
    validator = FakeValidator(build_success=False, build_system="gradle")
    history = [
        {
            "type": "observation",
            "content": "ERROR: JAVA_HOME is not set and no 'java' command could be found.",
        },
        GRADLE_ACTION,
    ]
    tool = ContextTool(_branch_cm(history=history, validator=validator))

    result = tool._validate_task_completion(
        _task("Compile with Gradle"),
        summary="Attempted the Gradle compile step.",
        key_results="The Gradle wrapper is present in the repo.",
    )

    assert result["valid"] is False
    reason = result["reason"].lower()
    assert "requirement" in reason or "remediat" in reason or "install" in reason


def test_compile_task_allowed_when_requirement_remediated():
    # Remediation evidence lives in the action OUTPUT — the engine persists
    # only type/tool_name/success/output for actions, never the command text.
    validator = FakeValidator(build_success=True, build_system="gradle")
    history = [
        {
            "type": "observation",
            "content": "ERROR: JAVA_HOME is not set and no 'java' command could be found.",
        },
        {
            "type": "action",
            "tool_name": "bash",
            "success": True,
            "output": "Setting up openjdk-17-jdk-headless (17.0.10+7) ...",
        },
        GRADLE_ACTION,
    ]
    tool = ContextTool(_branch_cm(history=history, validator=validator))

    result = tool._validate_task_completion(
        _task("Compile with Gradle"),
        summary="Installed the JDK then compiled successfully.",
        key_results="openjdk-17 installed; gradle compile produced classes.",
    )

    assert result["valid"] is True


def test_unmet_requirement_text_overridden_by_green_artifacts():
    """'requires Java 17 (already present)' in a summary must not block a
    build that physically produced artifacts."""
    validator = FakeValidator(build_success=True, build_system="gradle")
    history = [
        {
            "type": "observation",
            "content": "ERROR: JAVA_HOME is not set and no 'java' command could be found.",
        },
        GRADLE_ACTION,
    ]
    tool = ContextTool(_branch_cm(history=history, validator=validator))

    result = tool._validate_task_completion(
        _task("Compile with Gradle"),
        summary="Compiled successfully. Project requires Java 17 (already present in image).",
        key_results="BUILD SUCCESSFUL; classes produced.",
    )

    assert result["valid"] is True


def test_remediated_but_still_no_artifacts_blocked_by_physical_gate():
    validator = FakeValidator(build_success=False, build_system="gradle")
    history = [
        {
            "type": "observation",
            "content": "ERROR: JAVA_HOME is not set and no 'java' command could be found.",
        },
        {
            "type": "action",
            "tool_name": "bash",
            "success": True,
            "output": "Setting up openjdk-17-jdk-headless (17.0.10+7) ...",
        },
        GRADLE_ACTION,
    ]
    tool = ContextTool(_branch_cm(history=history, validator=validator))

    result = tool._validate_task_completion(
        _task("Compile with Gradle"),
        summary="Installed the JDK and re-ran the Gradle compile.",
        key_results="JDK installed; compile attempted.",
    )

    assert result["valid"] is False
    assert "artifact" in result["reason"].lower()


def test_documents_unmet_requirement_ignores_thought_entries():
    """Agent musings ('the readme says this requires Java 17') must not arm
    the requirement gate; only observations/action outputs count."""
    thought_history = [
        {"type": "thought", "content": "the readme says this requires java 17"}
    ]
    tool = ContextTool(_branch_cm(history=thought_history))
    assert tool._documents_unmet_requirement("clean summary") is False

    observation_history = [
        {"type": "observation", "content": "ERROR: JAVA_HOME is not set."}
    ]
    tool = ContextTool(_branch_cm(history=observation_history))
    assert tool._documents_unmet_requirement("clean summary") is True
