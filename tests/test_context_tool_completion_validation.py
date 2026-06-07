from types import SimpleNamespace

from sag.tools.context_tool import ContextTool


def _tool():
    return ContextTool(SimpleNamespace())


def _tool_with_history(history):
    context_manager = SimpleNamespace(
        current_task_id="task_4",
        load_branch_history=lambda task_id: SimpleNamespace(history=history),
    )
    return ContextTool(context_manager)


def _task(description):
    return SimpleNamespace(id="task_4", description=description)


def test_compile_task_rejects_blocked_maven_completion():
    result = _tool()._validate_task_completion(
        _task("Compile project using Maven"),
        summary="Maven compile is blocked by the installed Maven version.",
        key_results=(
            "Build failed at maven-enforcer-plugin: RequireMavenVersion; "
            "Detected Maven Version 3.6.3 is not in the allowed range [3.9,). "
            "No compilation artifacts produced."
        ),
    )

    assert result["valid"] is False
    assert "failure" in result["reason"].lower() or "blocked" in result["reason"].lower()


def test_compile_task_allows_resolved_error_language():
    result = _tool()._validate_task_completion(
        _task("Compile project using Maven"),
        summary="Build completed successfully after the Maven version error was fixed.",
        key_results="BUILD SUCCESS; compilation successful; no errors remain.",
    )

    assert result["valid"] is True


def test_compile_task_rejects_completion_without_build_tool_action():
    result = _tool_with_history(
        [
            {"type": "action", "tool_name": "manage_context", "success": True},
        ]
    )._validate_task_completion(
        _task("Compile project using Maven"),
        summary="Completed the Maven compile step for /workspace/demo.",
        key_results="Project uses Maven; next task is tests.",
    )

    assert result["valid"] is False
    assert "tool execution" in result["reason"].lower()


def test_compile_task_allows_successful_maven_build_action():
    result = _tool_with_history(
        [
            {
                "type": "action",
                "tool_name": "maven",
                "success": True,
                "output": "BUILD SUCCESS",
            },
        ]
    )._validate_task_completion(
        _task("Compile project using Maven"),
        summary="Build completed successfully.",
        key_results="BUILD SUCCESS; compilation successful.",
    )

    assert result["valid"] is True


def test_test_task_rejects_completion_without_test_tool_action():
    result = _tool_with_history(
        [
            {"type": "action", "tool_name": "manage_context", "success": True},
        ]
    )._validate_task_completion(
        _task("Run tests using documented commands: mvn without arguments"),
        summary="Executed the documented Maven default test goal.",
        key_results="Project uses Maven; report next.",
    )

    assert result["valid"] is False
    assert "tool execution" in result["reason"].lower()
