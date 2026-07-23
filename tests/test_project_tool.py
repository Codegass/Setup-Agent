# tests/test_project_tool.py
"""project(action: clone|provision|analyze|env) — facade over the four
setup-time tools (project_setup, project_analyzer, system, env). Spec §4.

The delegated sub-action vocabularies are the REAL ones:
ProjectSetupTool: action='clone' with repository_url;
SystemTool: install_java (JDKs) / install (packages);
ProjectAnalyzerTool: action='analyze';
EnvTool: inspect|register|activate|block|clear — register sets env
vars/executables (there is no 'set' action).
"""

from sag.tools.base import ToolResult
from sag.tools.project_tool import ProjectTool


class Recorder:
    def __init__(self, name):
        self.name = name
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return ToolResult.completed_success(output=f"{self.name} ok")


def _tool():
    setup, analyzer, system, env = (
        Recorder("setup"),
        Recorder("analyzer"),
        Recorder("system"),
        Recorder("env"),
    )
    tool = ProjectTool(setup_tool=setup, analyzer_tool=analyzer, system_tool=system, env_tool=env)
    return tool, setup, analyzer, system, env


def test_clone_routes_to_setup():
    tool, setup, *_ = _tool()
    result = tool.execute(action="clone", repo_url="https://github.com/x/y.git")
    assert result.succeeded
    # ProjectSetupTool's real signature takes repository_url, not repo_url.
    assert setup.calls and setup.calls[0]["repository_url"] == "https://github.com/x/y.git"
    assert setup.calls[0]["action"] == "clone"


def test_analyze_routes_to_analyzer():
    tool, _, analyzer, *_ = _tool()
    tool.execute(action="analyze", project_path="/workspace/p")
    assert analyzer.calls and analyzer.calls[0]["project_path"] == "/workspace/p"
    assert analyzer.calls[0]["action"] == "analyze"


def test_env_routes_to_env_tool():
    tool, *_, env = _tool()
    tool.execute(
        action="env",
        tool="maven",
        executable="/opt/apache-maven-3.9.9/bin/mvn",
    )
    assert env.calls
    # EnvTool's real "set env vars/executables" verb is register.
    assert env.calls[0]["action"] == "register"
    assert env.calls[0]["activate"] is True


def test_env_facade_rejects_explicit_inactive_registration():
    tool, *_, env = _tool()

    result = tool.execute(
        action="env",
        tool="maven",
        executable="/opt/apache-maven-3.9.9/bin/mvn",
        activate=False,
    )

    assert result.succeeded is False
    assert result.error_code == "PROJECT_ENV_ACTIVATION_REQUIRED"
    assert env.calls == []


def test_env_facade_safe_execute_cannot_bypass_activation_requirement():
    tool, *_, env = _tool()

    result = tool.safe_execute(
        action="env",
        tool="maven",
        executable="/opt/apache-maven-3.9.9/bin/mvn",
        activate=False,
    )

    assert result.succeeded is False
    assert env.calls == []


def test_env_schema_describes_atomic_activation_default():
    tool, *_ = _tool()

    schema = tool.get_parameter_schema()

    assert schema["properties"]["activate"]["default"] is True
    assert schema["properties"]["activate"]["enum"] == [True]
    assert "activate" in schema["properties"]["executable"]["description"]
    assert "requirement" in schema["properties"]


def test_provision_routes_to_system():
    tool, _, _, system, _ = _tool()
    tool.execute(action="provision", java_version="17")
    assert system.calls
    assert system.calls[0]["action"] == "install_java"
    assert system.calls[0]["java_version"] == "17"


def test_provision_with_packages_uses_system_install():
    tool, _, _, system, _ = _tool()
    tool.execute(action="provision", packages=["maven"])
    assert system.calls and system.calls[0]["action"] == "install"
    assert system.calls[0]["packages"] == ["maven"]


def test_unknown_action_fails_with_options():
    tool, *_ = _tool()
    result = tool.execute(action="dance")
    assert result.operation_outcome.value == "failed"
    assert any("clone" in s for s in result.suggestions)
