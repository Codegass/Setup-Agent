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
        return ToolResult(success=True, output=f"{self.name} ok")


def _tool():
    setup, analyzer, system, env = Recorder("setup"), Recorder("analyzer"), Recorder("system"), Recorder("env")
    tool = ProjectTool(setup_tool=setup, analyzer_tool=analyzer, system_tool=system, env_tool=env)
    return tool, setup, analyzer, system, env


def test_clone_routes_to_setup():
    tool, setup, *_ = _tool()
    result = tool.execute(action="clone", repo_url="https://github.com/x/y.git")
    assert result.success
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
    tool.execute(action="env", env={"JAVA_HOME": "/usr/lib/jvm/x"})
    assert env.calls
    # EnvTool's real "set env vars/executables" verb is register.
    assert env.calls[0]["action"] == "register"


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
    assert result.verdict == "failed"
    assert any("clone" in s for s in result.suggestions)
