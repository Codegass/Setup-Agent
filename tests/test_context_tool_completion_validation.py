from types import SimpleNamespace

from sag.tools.context_tool import ContextTool


def _tool():
    return ContextTool(SimpleNamespace())


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
