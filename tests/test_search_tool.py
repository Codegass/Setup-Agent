# tests/test_search_tool.py
"""search(target, pattern): one retrieval tool for refs, files, job logs, web.

Spec §4: target = ref id | file:<path> | job:<id> | web:<query>.
The ref/web paths DELEGATE to the existing OutputSearchTool/WebSearchTool
internals (stage-1 consolidates the surface, not the implementations).
"""

from types import SimpleNamespace

from sag.tools.base import ToolResult
from sag.tools.search_tool import SearchTool


class FakeOrchestrator:
    def __init__(self, responses=None):
        self.commands = []
        self.responses = responses or {}

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        for marker, resp in self.responses.items():
            if marker in command:
                return resp
        return {"success": True, "output": "", "exit_code": 0}


def test_file_target_greps_in_container():
    orch = FakeOrchestrator(
        responses={"pom.xml": {"success": True, "output": "42:<requireMavenVersion>", "exit_code": 0}}
    )
    tool = SearchTool(orch, output_search=None, web_search=None)

    result = tool.execute(target="file:/workspace/p/pom.xml", pattern="requireMavenVersion")

    assert result.success is True
    assert "requireMavenVersion" in result.output
    assert any("grep" in c and "pom.xml" in c for c in orch.commands)


def test_job_target_greps_job_log():
    orch = FakeOrchestrator(
        responses={"sag_jobs/abc.log": {"success": True, "output": "BUILD SUCCESSFUL", "exit_code": 0}}
    )
    tool = SearchTool(orch, output_search=None, web_search=None)

    result = tool.execute(target="job:abc", pattern="BUILD")

    assert "BUILD SUCCESSFUL" in result.output
    assert any("/tmp/sag_jobs/abc.log" in c for c in orch.commands)


def test_ref_target_delegates_to_output_search():
    calls = []

    class FakeOutputSearch:
        def execute(self, **kwargs):
            calls.append(kwargs)
            return ToolResult(success=True, output="matched line")

    tool = SearchTool(FakeOrchestrator(), output_search=FakeOutputSearch(), web_search=None)

    result = tool.execute(target="output_5b9a", pattern="FAIL")

    assert result.success and "matched line" in result.output
    assert calls[0]["ref_id"] == "output_5b9a"
    assert calls[0]["grep_pattern"] == "FAIL"


def test_web_target_delegates_to_web_search():
    calls = []

    class FakeWebSearch:
        def execute(self, query, max_results=5):
            calls.append(query)
            return ToolResult(success=True, output="result snippet")

    tool = SearchTool(FakeOrchestrator(), output_search=None, web_search=FakeWebSearch())

    result = tool.execute(target="web:gradle develocity plugin 3.19")

    assert result.success
    assert calls == ["gradle develocity plugin 3.19"]


def test_unknown_target_is_failed_with_options():
    tool = SearchTool(FakeOrchestrator(), output_search=None, web_search=None)
    result = tool.execute(target="bogus^target", pattern="x")
    assert result.verdict == "failed"
    assert any("file:" in s or "job:" in s for s in result.suggestions)
